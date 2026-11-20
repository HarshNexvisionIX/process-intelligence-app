"""Day 5: the ONLY module that talks to Langfuse (tracing, scores, prompts, datasets).

Langfuse is optional. Without the three LANGFUSE_* settings in .env, or without
the `langfuse` package, every function here is a safe no-op (or returns the
local fallback). Any Langfuse error is logged once as a warning and ignored:
tracing must never break a report.

Everything sent to Langfuse is masked first (`mask`): email addresses, long
digit runs and person-name-shaped text become "[redacted]".

Written for the Langfuse Python SDK v3+ API (tested with 5.1): `Langfuse(mask=...)`,
`start_as_current_observation`, `propagate_attributes`, `create_score`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from app.core import privacy
from app.core.config import settings
from app.core.ingest import EMAIL_RE, PERSON_RE

log = logging.getLogger(__name__)
_warned: set[str] = set()
_client = None

# The ingest patterns match a WHOLE value; here they must find a match inside any text.
_EMAIL_IN_TEXT = re.compile(EMAIL_RE.pattern.strip("^$"))
_PERSON_IN_TEXT = re.compile(r"\b" + PERSON_RE.pattern.strip("^$") + r"\b")
_LONG_DIGITS = re.compile(r"\d{8,}")


def _warn(what: str, err: Exception) -> None:
    """Log a Langfuse problem once per kind of call, then stay quiet."""
    if what not in _warned:
        _warned.add(what)
        log.warning("Langfuse %s failed and is skipped: %s", what, err)


# --------------------------------------------------------------------------- #
# Masking
# --------------------------------------------------------------------------- #
def mask(text: str, allow: set[str] | frozenset[str] | None = None) -> str:
    """Replace emails, 8+ digit runs and person-name-shaped words with "[redacted]".

    Two capitalised words that start with an action word ("Approve Request")
    are kept, as on page 1: they are step names, not people. Names in `allow`
    (the current job's activity names) are never treated as person names, so
    "Block Invoice" stays readable. Emails and long numbers are ALWAYS redacted,
    even inside an allowed name.
    """
    from app.core.mapping import _ACTION_WORDS  # local import avoids a cycle

    allow = allow or frozenset()

    def person(m: re.Match) -> str:
        found = m.group(0)
        if found.split(" ", 1)[0].lower() in _ACTION_WORDS or found in allow:
            return found
        return "[redacted]"

    text = _EMAIL_IN_TEXT.sub("[redacted]", text)
    text = _LONG_DIGITS.sub("[redacted]", text)
    if text in allow:          # the whole string is an activity name of this job
        return text
    return _PERSON_IN_TEXT.sub(person, text)


# Activity names of the job being traced. A context variable keeps two reports
# running at the same time (two browser sessions) from mixing their lists.
_allowed_names: ContextVar[frozenset[str]] = ContextVar("langfuse_allowed_names", default=frozenset())


@contextmanager
def allowed_names(names) -> Iterator[None]:
    """While active, Langfuse masking keeps these activity names readable."""
    token = _allowed_names.set(frozenset(names or ()))
    try:
        yield
    finally:
        _allowed_names.reset(token)


def _mask_data(*, data: Any, **_kwargs) -> Any:
    """Langfuse mask hook: mask every string inside inputs, outputs and metadata."""
    if isinstance(data, str):
        return mask(data, _allowed_names.get())
    if isinstance(data, dict):
        return {k: _mask_data(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_mask_data(data=v) for v in data]
    return data


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    """True only when LANGFUSE_PUBLIC_KEY, _SECRET_KEY and _BASE_URL are set, the server is
    local / on your network (or ALLOW_EXTERNAL_LANGFUSE=true), and langfuse imports."""
    if not all(os.getenv(k) for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")):
        return False
    if not privacy.langfuse_allowed():
        if "external langfuse" not in _warned:
            _warned.add("external langfuse")
            log.warning("Langfuse tracing is off: LANGFUSE_BASE_URL is outside your network "
                        "(set ALLOW_EXTERNAL_LANGFUSE=true to allow).")
        return False
    try:
        import langfuse  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", str(text).lstrip("vV"))[:3])


def check_server_version() -> str | None:
    """Warn (once) when the Langfuse server is older than this SDK needs; never raises.

    One GET to <LANGFUSE_BASE_URL>/api/public/health (that host only), with the
    timeout from settings. Returns the server version, or None when unknown.
    """
    import urllib.request
    cfg = settings()["observability"]
    url = os.getenv("LANGFUSE_BASE_URL", "").rstrip("/") + "/api/public/health"
    try:
        with urllib.request.urlopen(url, timeout=float(cfg["health_check_timeout_seconds"])) as r:
            version = str(json.loads(r.read().decode("utf-8")).get("version") or "")
    except Exception as err:  # noqa: BLE001
        _warn("server version check", err)
        return None
    if version and _version(version) < _version(cfg["langfuse_min_server_version"]):
        log.warning("Langfuse server %s is older than %s, which the installed langfuse SDK needs: "
                    "traces may be rejected. Upgrade the Langfuse server.", version,
                    cfg["langfuse_min_server_version"])
    return version or None


def _langfuse():
    """The one Langfuse client (created on first use, with masking switched on)."""
    global _client
    if _client is None:
        from langfuse import Langfuse
        # A short timeout: a slow or unreachable Langfuse must not hold up a report.
        _client = Langfuse(public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
                           secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
                           base_url=os.getenv("LANGFUSE_BASE_URL"), mask=_mask_data, timeout=5)
        # The server version check runs in the background: it can never delay the app.
        threading.Thread(target=check_server_version, name="langfuse-version-check", daemon=True).start()
    return _client


def openai_client(**kwargs):
    """openai.OpenAI(**kwargs), or Langfuse's traced drop-in class when Langfuse is on, so every
    call is traced (and masked) automatically. Only ai_client.py creates clients."""
    if enabled():
        try:
            _langfuse()  # make sure the traced client uses our masked Langfuse client
            from langfuse.openai import OpenAI as TracedOpenAI
            return TracedOpenAI(**kwargs)
        except Exception as err:  # noqa: BLE001
            _warn("OpenAI wrapper", err)
    from openai import OpenAI
    return OpenAI(**kwargs)


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def get_prompt(name: str, label: str, fallback_text: list[dict], version: int | None = None
               ) -> tuple[list[dict], str]:
    """The chat prompt (system + user messages) from Langfuse, or the local fallback.

    Returns (messages, version). The version is the Langfuse version number as
    text, or "local" when Langfuse is off, unreachable or has no such prompt.
    A specific `version` overrides the label (used by the evaluation script).
    """
    if not enabled():
        return fallback_text, "local"
    try:
        kw = {"version": version} if version is not None else {"label": label}
        p = _langfuse().get_prompt(name, type="chat", max_retries=0, **kw)
        return [{"role": m["role"], "content": m["content"]} for m in p.prompt], str(p.version)
    except Exception as err:  # noqa: BLE001
        _warn("prompt fetch", err)
        return fallback_text, "local"


def push_prompt(name: str, messages: list[dict]) -> int | None:
    """Upload a new version of a chat prompt WITHOUT any label (promotion is a human decision)."""
    if not enabled():
        return None
    try:
        return _langfuse().create_prompt(name=name, prompt=messages, labels=[], type="chat").version
    except Exception as err:  # noqa: BLE001
        _warn("prompt upload", err)
        return None


# --------------------------------------------------------------------------- #
# Traces and scores
# --------------------------------------------------------------------------- #
@contextmanager
def trace(job_id: str, tags: list[str], metadata: dict,
          allow_names: set[str] | None = None, name: str = "report-narrative") -> Iterator[str | None]:
    """Group every call of one report under one trace, with session_id = job_id.

    Yields the trace ID, or None when Langfuse is off or fails. Errors inside the
    `with` block are raised as usual; Langfuse errors are not. `allow_names` (the
    job's activity names) stay readable in the trace (see mask).
    """
    stack = ExitStack()
    stack.enter_context(allowed_names(allow_names))
    trace_id = None
    if enabled():
        try:
            from langfuse import propagate_attributes
            lf = _langfuse()
            stack.enter_context(lf.start_as_current_observation(
                name=name, as_type="span", metadata=metadata))
            stack.enter_context(propagate_attributes(
                session_id=str(job_id), tags=[str(t) for t in tags], trace_name=name))
            trace_id = lf.get_current_trace_id()
        except Exception as err:  # noqa: BLE001
            _warn("trace start", err)
    try:
        yield trace_id
    finally:
        try:
            stack.close()
        except Exception as err:  # noqa: BLE001
            _warn("trace end", err)


def score(name: str, value: float, comment: str | None = None, trace_id: str | None = None) -> None:
    """Attach a score to the current trace (or to `trace_id`, e.g. for user feedback later)."""
    if not enabled():
        return
    try:
        lf = _langfuse()
        if trace_id:
            lf.create_score(name=name, value=value, trace_id=trace_id, comment=comment)
        else:
            lf.score_current_trace(name=name, value=value, comment=comment)
    except Exception as err:  # noqa: BLE001
        _warn("score", err)


def flush() -> None:
    """Send everything queued to Langfuse now (call at the end of a report or script)."""
    if not enabled() or _client is None:
        return
    try:
        _client.flush()
    except Exception as err:  # noqa: BLE001
        _warn("flush", err)


# --------------------------------------------------------------------------- #
# Evaluation datasets
# --------------------------------------------------------------------------- #
def add_dataset_item(dataset: str, item_input: dict, metadata: dict | None = None) -> None:
    """Append one input to a Langfuse dataset (created on first use). No-op when disabled."""
    if not enabled():
        return
    lf = _langfuse()
    try:
        lf.create_dataset(name=dataset, description="Inputs for evaluating the report writer prompt")
    except Exception:  # noqa: BLE001 - it usually exists already
        pass
    try:
        lf.create_dataset_item(dataset_name=dataset, input=_mask_data(data=item_input), metadata=metadata)
    except Exception as err:  # noqa: BLE001
        _warn("dataset item", err)
