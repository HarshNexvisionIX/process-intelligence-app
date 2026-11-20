"""The ONLY place that talks to an AI model.

Providers are OpenAI-compatible endpoints (config/ai_providers.yaml, with the user's chosen
models in config/ai_providers.local.yaml). `settings.yaml -> ai.routes` lists, per task, the
providers to try in order. A provider is used only when its key is in the environment (.env)
and a model is chosen; LLM_BASE_URL / LLM_MODEL / LLM_API_KEY form a provider "custom" that is
tried first for every task (the behaviour before routing existed).

Before every request: the privacy guard (external endpoints need ALLOW_EXTERNAL_AI=true), a
budget check (estimated tokens vs tokens per minute, context size, and today's request / token
counters in data/ai_usage.json), and an in-process rate limiter for requests / tokens per
minute. Errors move on to the next provider: 429 waits for Retry-After (at most
ai.max_wait_seconds) and retries once; 5xx / timeouts retry once; 401 / 403 mark the key as
rejected for this session; an unknown model asks the user to choose another one.

Message contents and keys are never logged; only provider, model, task, token estimates and
the outcome. Callers mask their payloads before calling (narrative.payload_mask).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from app.core.config import PROJECT_ROOT, data_dir, settings

log = logging.getLogger(__name__)
PROVIDERS_FILE = PROJECT_ROOT / "config" / "ai_providers.yaml"
LOCAL_FILE = PROJECT_ROOT / "config" / "ai_providers.local.yaml"
TASKS = ("summary", "order_suggestion", "evidence_explain", "definition_draft", "knowledge_extract")
_sleep = time.sleep                     # replaced in tests
_now = time.time
http_client = None                      # tests set an httpx.Client with a MockTransport
_rejected: dict[str, str] = {}          # provider -> reason, for this session (process)
_warned: set[str] = set()
_lock = threading.Lock()
_windows: dict[str, deque] = {}         # provider -> deque of (time, tokens) in the last minute


class AIUnavailable(Exception):
    """Every provider of the route failed or was skipped; `reasons` say why (short, no secrets)."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons) or "no AI provider is configured")

    def short(self) -> str:
        return "; ".join(self.reasons[:4]) or "no AI provider is configured"


@dataclass
class AIResult:
    text: str
    provider: str
    model: str
    attempts: int = 1
    tokens_in_est: int = 0
    tokens_out: int | None = None
    latency_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def providers() -> dict[str, dict]:
    """Every known provider (base config + local overrides), plus "custom" from LLM_* when set."""
    base = (_yaml(PROVIDERS_FILE).get("providers") or {})
    local = (_yaml(LOCAL_FILE).get("providers") or {})
    out = {}
    for key, cfg in base.items():
        merged = {**cfg, **{k: v for k, v in (local.get(key) or {}).items() if k != "limits"}}
        merged["limits"] = {**(cfg.get("limits") or {}), **((local.get(key) or {}).get("limits") or {})}
        merged["model"] = (local.get(key) or {}).get("model") or cfg.get("default_model") or ""
        out[key] = {"key": key, **merged}
    if os.getenv("LLM_BASE_URL") or os.getenv("LLM_MODEL") or os.getenv("LLM_API_KEY"):
        out = {"custom": {"key": "custom", "name": "Custom endpoint (LLM_BASE_URL)",
                          "base_url": os.getenv("LLM_BASE_URL") or None, "key_env": "LLM_API_KEY",
                          "model": os.getenv("LLM_MODEL") or "", "json_mode": True, "context_tokens": None,
                          "limits": {}, "day_reset_tz": "UTC", "extra_body": {}, "free_tier_note": "",
                          "key_optional": True}, **out}
    return out


def has_key(p: dict) -> bool:
    return bool(os.getenv(p.get("key_env") or "")) or bool(p.get("key_optional"))


def usable(p: dict) -> tuple[bool, str | None]:
    """Whether a provider can be used now (key, model, privacy, not rejected), and if not why."""
    from app.core import privacy
    if not has_key(p):
        return False, f"no key for {p['key']} ({p.get('key_env')} is empty)"
    if not p.get("model"):
        return False, f"no model chosen for {p['key']}"
    if p["key"] in _rejected:
        return False, _rejected[p["key"]]
    kind = privacy.classify_endpoint(p.get("base_url") or "")
    if (kind == "external" or (kind == "unset" and p["key"] == "custom")) and not privacy._flag("ALLOW_EXTERNAL_AI"):
        return False, f"{p['key']} is outside your network and ALLOW_EXTERNAL_AI is not true"
    why = privacy.client_block(p)
    if why:
        return False, why
    return True, None


def route(task: str) -> list[str]:
    """The providers to try for a task: "custom" first when configured, then ai.routes[task]."""
    names = list((settings()["ai"].get("routes") or {}).get(task) or [])
    if "custom" in providers():
        names = ["custom"] + [n for n in names if n != "custom"]
    return names


def configured() -> list[str]:
    """Providers that could be used now (for the status line and ai_status)."""
    out = []
    for key, p in providers().items():
        ok, _ = usable(p)
        if ok:
            out.append(key)
    return out


def save_local(provider: str, model: str | None = None, limits: dict | None = None) -> Path:
    """Write the user's choice (model, limit overrides) to config/ai_providers.local.yaml."""
    data = _yaml(LOCAL_FILE)
    entry = (data.setdefault("providers", {}).setdefault(provider, {}))
    if model is not None:
        entry["model"] = model
    if limits:
        entry.setdefault("limits", {}).update(limits)
    fd, tmp = tempfile.mkstemp(dir=LOCAL_FILE.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=True)
    os.replace(tmp, LOCAL_FILE)
    _rejected.pop(provider, None)
    return LOCAL_FILE


# --------------------------------------------------------------------------- #
# Budget: estimates, daily counters, per-minute limiter
# --------------------------------------------------------------------------- #
def estimate_tokens(messages: list[dict], max_output: int | None = None) -> int:
    chars = sum(len(m.get("content") or "") for m in messages)
    return math.ceil(chars / 4) + int(max_output if max_output is not None else settings()["ai"]["max_output_tokens"])


def usage_path() -> Path:
    return data_dir() / "ai_usage.json"


def _day(p: dict) -> str:
    from zoneinfo import ZoneInfo
    tz = p.get("day_reset_tz") or "UTC"
    return datetime.fromtimestamp(_now(), ZoneInfo(tz)).strftime("%Y-%m-%d")


def usage() -> dict:
    path = usage_path()
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        return {}


def today(p: dict) -> dict:
    """Today's {requests, tokens} for a provider (a new day starts at its reset time zone)."""
    entry = usage().get(p["key"]) or {}
    return entry if entry.get("day") == _day(p) else {"day": _day(p), "requests": 0, "tokens": 0}


def _count(p: dict, tokens: int) -> None:
    with _lock:
        data = usage()
        entry = today(p)
        entry.update(requests=entry["requests"] + 1, tokens=entry["tokens"] + int(tokens))
        data[p["key"]] = entry
        path = usage_path()
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


def over_budget(p: dict, tokens: int) -> str | None:
    """Why a request of `tokens` cannot go to this provider now, or None."""
    lim = p.get("limits") or {}
    if p.get("context_tokens") and tokens > int(p["context_tokens"]):
        return f"over context ({tokens:,} > {int(p['context_tokens']):,} tokens) for {p['key']}"
    if lim.get("tpm") and tokens > int(lim["tpm"]):
        return f"over tokens per minute ({tokens:,} > {int(lim['tpm']):,}) for {p['key']}"
    t = today(p)
    if lim.get("rpd") and t["requests"] + 1 > int(lim["rpd"]):
        return f"over requests per day ({int(lim['rpd']):,}) for {p['key']}"
    if lim.get("tpd") and t["tokens"] + tokens > int(lim["tpd"]):
        return f"over tokens per day ({int(lim['tpd']):,}) for {p['key']}"
    return None


def _wait_for_minute(p: dict, tokens: int) -> str | None:
    """Wait (at most ai.max_wait_seconds) until the per-minute limits allow the request."""
    lim = p.get("limits") or {}
    rpm, tpm = lim.get("rpm"), lim.get("tpm")
    if not rpm and not tpm:
        return None
    window = _windows.setdefault(p["key"], deque())
    max_wait = float(settings()["ai"]["max_wait_seconds"])
    while True:
        now = _now()
        while window and now - window[0][0] >= 60:
            window.popleft()
        used = sum(t for _, t in window)
        if (not rpm or len(window) < int(rpm)) and (not tpm or used + tokens <= int(tpm)):
            window.append((now, tokens))
            return None
        wait = 60 - (now - window[0][0]) + 0.01
        if wait > max_wait:
            return f"rate limit per minute reached for {p['key']}"
        _sleep(wait)


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #
def _client(p: dict):
    """An OpenAI-compatible client (Langfuse's traced client when Langfuse is on)."""
    from app.core import observability
    kwargs = {"base_url": p.get("base_url") or None, "api_key": os.getenv(p.get("key_env") or "") or "not-needed",
              "timeout": settings()["ai"]["timeout_seconds"], "max_retries": 0}
    if http_client is not None:
        kwargs["http_client"] = http_client
    return observability.openai_client(**kwargs)


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?```\s*$", re.S)


def clean(text: str) -> str:
    """Remove <think> blocks and code fences around the answer."""
    text = _THINK.sub("", text or "").strip()
    m = _FENCE.match(text)
    return m.group(1).strip() if m else text


def _status(err) -> int | None:
    return getattr(err, "status_code", None) or getattr(getattr(err, "response", None), "status_code", None)


def _retry_after(err) -> float | None:
    headers = getattr(getattr(err, "response", None), "headers", None) or {}
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


def _send(p: dict, messages: list[dict], json_expected: bool, max_output: int | None):
    kwargs = {"model": p["model"], "messages": messages, "temperature": settings()["ai"]["temperature"]}
    if max_output:
        kwargs["max_tokens"] = int(max_output)
    if json_expected and p.get("json_mode"):
        kwargs["response_format"] = {"type": "json_object"}
    if p.get("extra_body"):
        kwargs["extra_body"] = dict(p["extra_body"])
    return _client(p).chat.completions.create(**kwargs)


_last_call: dict[str, float] = {}


def _pace(p: dict) -> None:
    """At least ai.pace_seconds (per provider, or the default) between two calls to the same provider, so a long
    run (Level 4: one call per step and chunk) stays under per-minute limits instead of hitting 429."""
    pace = settings()["ai"].get("pace_seconds") or {}
    gap = float(pace.get(p["key"], pace.get("default", 0)) if isinstance(pace, dict) else pace)
    last = _last_call.get(p["key"])
    if gap and last is not None and _now() - last < gap:
        _sleep(gap - (_now() - last))
    _last_call[p["key"]] = _now()


def _try_provider(p: dict, messages, json_expected, max_output, task, tokens) -> tuple[AIResult | None, list[str]]:
    """One provider, paced (ai.pace_seconds): on 429 up to ai.rate_limit_retries retries with backoff (the
    Retry-After when given, else 2, 4, 8 ... s; never longer than ai.max_wait_seconds), one retry on 5xx or timeout."""
    errors: list[str] = []
    retries = int(settings()["ai"].get("rate_limit_retries", 2))
    for attempt in range(1, retries + 2):
        start = _now()
        _pace(p)
        try:
            reply = _send(p, messages, json_expected, max_output)
        except Exception as err:  # noqa: BLE001 - every failure is a reason, never a crash
            code = _status(err)
            name = type(err).__name__
            if code == 429:
                wait = _retry_after(err)
                limit = float(settings()["ai"]["max_wait_seconds"])
                backoff = wait if wait is not None else min(limit, 2.0 ** attempt)
                if attempt <= retries and backoff <= limit:
                    errors.append(f"{p['key']}: rate limited (429), retrying in {backoff:.0f} s")
                    _sleep(backoff)
                    continue
                errors.append(f"{p['key']}: rate limited (429)")
            elif code in (401, 403):
                _rejected[p["key"]] = f"key rejected by {p['key']} ({code})"
                errors.append(_rejected[p["key"]])
            elif code == 404 or "model" in str(getattr(err, "body", "") or "").lower() and code == 400:
                _rejected[p["key"]] = (f"model {p['model']} not available at {p['key']} — choose another in AI "
                                       "settings")
                errors.append(_rejected[p["key"]])
            elif (code and code >= 500) or "Timeout" in name or "Connection" in name:
                if attempt == 1:
                    errors.append(f"{p['key']}: {code or name}, retrying")
                    continue
                errors.append(f"{p['key']}: {code or name}")
            else:
                errors.append(f"{p['key']}: {code or name}")
            log.info("AI call failed: task=%s provider=%s model=%s tokens_est=%s outcome=%s",
                     task, p["key"], p["model"], tokens, errors[-1])
            return None, errors
        latency = round(_now() - start, 3)
        choice = reply.choices[0].message
        text = clean(getattr(choice, "content", None) or "")
        out_tokens = getattr(getattr(reply, "usage", None), "completion_tokens", None)
        _count(p, tokens if out_tokens is None else tokens - int(settings()["ai"]["max_output_tokens"]) + out_tokens)
        log.info("AI call ok: task=%s provider=%s model=%s tokens_est=%s latency=%.2fs",
                 task, p["key"], p["model"], tokens, latency)
        return AIResult(text, p["key"], p["model"], attempt, tokens, out_tokens, latency, errors), errors
    return None, errors


def complete(task: str, messages: list[dict], json_expected: bool = True, max_output_tokens: int | None = None,
             only: str | None = None, prefer: str | None = None) -> AIResult:
    """Send `messages` (already masked by the caller) to the first provider of the task's route that
    is usable, within budget and answers. `only` restricts to one provider; `prefer` tries one first
    (rule-break retries go to the provider that answered). Raises AIUnavailable."""
    names = [only] if only else route(task)
    if prefer and prefer in names:
        names = [prefer] + [n for n in names if n != prefer]
    known = providers()
    reasons: list[str] = []        # every reason (for AIUnavailable)
    fallback: list[str] = []       # providers that were tried or skipped for a reason (shown when another answers)
    tokens = estimate_tokens(messages, max_output_tokens)
    for name in names:
        p = known.get(name)
        if p is None:
            reasons.append(f"unknown provider {name}")
            continue
        ok, why = usable(p)
        if not ok:
            if why not in _warned:
                _warned.add(why)
                log.info("AI provider skipped: task=%s reason=%s", task, why)
            reasons.append(why)
            if p["key"] in _rejected:                # a key or model refused earlier in this session
                fallback.append(why)
            continue
        why = over_budget(p, tokens) or _wait_for_minute(p, tokens)
        if why:
            reasons.append(why)
            fallback.append(why)
            continue
        result, errors = _try_provider(p, messages, json_expected, max_output_tokens, task, tokens)
        if result is not None:
            result.errors = fallback + result.errors
            return result
        fallback += errors
        reasons += errors[-1:]
    raise AIUnavailable(reasons)


class Session:
    """A routed "client" for one task, used by the callers' existing retry loops: the first call
    picks a provider; later calls (rule-break retries) go to the same provider first."""

    def __init__(self, task: str, only: str | None = None):
        self.task, self.only = task, only or os.getenv("AI_FORCE_PROVIDER") or None
        self.provider: str | None = None
        self.model: str | None = None
        self.results: list[AIResult] = []

    def call(self, messages: list[dict], json_expected: bool = True, max_output_tokens: int | None = None) -> str:
        result = complete(self.task, messages, json_expected, max_output_tokens, only=self.only, prefer=self.provider)
        self.provider, self.model = result.provider, result.model
        self.results.append(result)
        return result.text

    def notes(self) -> list[str]:
        """Routing reasons of every call ("fallback: groq: 503", ...), once each, in order: shows that
        another provider answered or a retry was needed. Never contains keys or message contents."""
        out: list[str] = []
        for r in self.results:
            for e in r.errors:
                note = f"fallback: {e}"
                if note not in out:
                    out.append(note)
        return out


def session(task: str, only: str | None = None) -> Session:
    return Session(task, only)


def chunk_chars(task: str, default: int) -> int:
    """Characters per chunk for long documents (4 characters per token): min(context, tokens per minute) x
    ai.chunk_share, minus the answer, for the SMALLEST usable provider of the route. A chunk then fits every
    provider the route may fall back to (e.g. Gemini rate-limited -> Cerebras with an 8,192-token context);
    `default` when no usable provider states a size."""
    known = providers()
    sizes = []
    for name in route(task):
        p = known.get(name)
        if p and usable(p)[0]:
            sizes += [x for x in (p.get("context_tokens"), (p.get("limits") or {}).get("tpm")) if x]
    if not sizes:
        return default
    tokens = int(min(sizes) * float(settings()["ai"]["chunk_share"])) - int(settings()["ai"]["max_output_tokens"])
    return max(1000, min(default, tokens * 4))


# --------------------------------------------------------------------------- #
# Model lists
# --------------------------------------------------------------------------- #
def parse_models(payload, provider: str) -> list[str]:
    """Model IDs from a /models response: {"data": [{"id"}]}, {"models": [{"name"}]} or a list.
    OpenRouter: only IDs ending in ":free"; Gemini: only "flash" or "pro" models."""
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("models") or []
    else:
        items = payload or []
    ids = []
    for it in items:
        mid = it if isinstance(it, str) else (it.get("id") or it.get("name") or "")
        mid = str(mid)
        if mid.startswith("models/"):
            mid = mid[len("models/"):]
        if mid:
            ids.append(mid)
    if provider == "openrouter":
        ids = [i for i in ids if i.endswith(":free")]
    elif provider == "gemini":
        ids = [i for i in ids if "flash" in i or "pro" in i]
    elif provider == "groq":
        ids = [i for i in ids if not any(x in i for x in ("whisper", "tts", "guard"))]
    return sorted(set(ids))


def list_models(provider: str) -> list[str]:
    """GET {base_url}/models with the provider's key (the key is sent only to that provider)."""
    import httpx
    p = providers()[provider]
    url = (p.get("base_url") or "https://api.openai.com/v1").rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {os.getenv(p.get('key_env') or '') or 'not-needed'}"}
    client = http_client or httpx.Client(timeout=settings()["ai"]["timeout_seconds"])
    resp = client.get(url, headers=headers)
    resp.raise_for_status()
    return parse_models(resp.json(), provider)


def test_provider(provider: str) -> dict:
    """A tiny synthetic request through the provider: {ok, model, latency_s, reason}."""
    try:
        # Reasoning models spend part of the answer budget thinking: a tiny limit cuts the answer off.
        max_output = int(settings()["ai"].get("max_output_tokens") or 500)
        r = complete("summary", [{"role": "user", "content": 'Return {"ok": true} as JSON.'}], True, max_output,
                     only=provider)
        ok = '"ok"' in r.text and "true" in r.text.lower()
        return {"ok": ok, "model": r.model, "latency_s": r.latency_s, "reason": None if ok else "unexpected answer"}
    except AIUnavailable as err:
        return {"ok": False, "model": providers().get(provider, {}).get("model"), "latency_s": None,
                "reason": err.short()}
