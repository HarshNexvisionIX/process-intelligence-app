"""The AI door of the no-case mode: wraps app.core.ai_client.complete (no new client).

    modes    off (rules only) | fake (tests and the bench: a callable answers, every payload is recorded) |
             online (the routes nc_* in settings ai.routes, with ai_client's pacing, 429 retries and fallback)
    privacy  a CLIENT dataset only ever reaches providers with `private: true` in config/ai_providers.yaml; when
             none is usable the call is not made ("no private AI provider for client data")
    payloads every message passes app/nocase/payloads.guard first
    answers  parsed as JSON and checked by the caller's schema check; one retry with the errors on the SAME
             provider, then the caller falls back to rules with the reason. Provider and model are recorded.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from app.core.config import settings
from app.nocase.payloads import guard

MODES = ("off", "fake", "online")


@dataclass
class Answer:
    data: object = None                     # the parsed, valid answer, or None (then use rules)
    provider: str | None = None
    model: str | None = None
    reason: str | None = None               # why there is no answer
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.data is not None


@dataclass
class Gateway:
    dataset_class: str = "client"
    mode: str = "online"
    fake: Callable[[str, list[dict]], str] | None = None
    calls: list[dict] = field(default_factory=list)       # what was sent (after the guard)
    blocked: list[dict] = field(default_factory=list)     # payloads the guard stopped (never sent)

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"AI mode must be one of {', '.join(MODES)}")
        if self.mode == "online" and not settings()["nocase"].get("ai_enabled", True):
            self.mode = "off"

    # ------------------------------------------------------------------ who may answer
    def providers_for(self, task: str) -> list[str]:
        """The task's route, private providers only for a client dataset; usable ones only."""
        from app.core import ai_client
        known = ai_client.providers()
        out = []
        for name in ai_client.route(task):
            p = known.get(name)
            if not p or not ai_client.usable(p)[0]:
                continue
            if self.dataset_class != "public" and not p.get("private"):
                continue
            if p.get("private") and not p.get("base_url"):
                continue                       # a private provider without an address would go to the internet
            out.append(name)
        return out

    def why_not(self, task: str) -> str | None:
        if self.mode == "off":
            return "AI switched off for this run"
        if self.mode == "fake":
            return None if self.fake else "no fake answer given"
        from app.core.narrative import ai_status
        ok, why = ai_status()
        if not ok and self.dataset_class == "public":
            return f"AI not available: {why}"
        if not self.providers_for(task):
            return ("AI not available (client data: private providers only)" if self.dataset_class != "public"
                    else "no usable AI provider for this task")
        return None

    # ------------------------------------------------------------------ one task
    def ask(self, task: str, messages: list[dict], check: Callable[[object], list[str]],
            retry: Callable[[str, list[str]], list[dict]] | None = None) -> Answer:
        """A valid answer (check returns no problems) or the reason why not. Never raises: a payload that breaks
        the privacy guard is NOT sent and comes back as a reason (the run goes on with rules for that part), so
        one AI-written value (an answer fed back on a retry, a proposed label) can never stop the whole analysis."""
        from app.core.narrative import parse_reply
        from app.nocase.payloads import PayloadError
        try:
            guard(messages)
        except PayloadError as exc:
            self.blocked.append({"task": task, "why": str(exc)})
            return Answer(reason=f"not sent: {exc}")
        why = self.why_not(task)
        if why:
            return Answer(reason=why)
        msgs = list(messages)
        provider = model = None
        notes: list[str] = []
        for attempt in (1, 2):
            try:
                text, provider, model = self._call(task, msgs, provider)
            except Exception as err:  # noqa: BLE001 - AI trouble falls back to rules
                short = err.short() if hasattr(err, "short") else f"{type(err).__name__}"
                return Answer(reason=f"AI not available: {short}", notes=notes, provider=provider, model=model)
            data = parse_reply(text)
            errors = ["the answer is not one JSON object"] if not isinstance(data, dict) else check(data)
            if not errors:
                return Answer(data=data, provider=provider, model=model, notes=notes)
            notes.append(f"attempt {attempt}: " + "; ".join(errors[:5]))
            if attempt == 1 and retry is not None:      # the caller builds a smaller retry (large requests)
                msgs = retry(str(text), errors)
                try:
                    guard(msgs)
                except PayloadError as exc:
                    self.blocked.append({"task": task, "why": str(exc)})
                    return Answer(reason=f"retry not sent: {exc}", notes=notes, provider=provider, model=model)
            elif attempt == 1:
                msgs = msgs + [{"role": "assistant", "content": str(text)[:4000]},
                               {"role": "user", "content": "Your answer broke these rules:\n- " + "\n- ".join(errors[:20])
                                + "\nReturn the corrected JSON object only."}]
                try:
                    guard(msgs[-2:])           # the AI's own answer goes back too: it is checked like any payload
                except PayloadError as exc:
                    self.blocked.append({"task": task, "why": str(exc)})
                    return Answer(reason=f"retry not sent: {exc}", notes=notes, provider=provider, model=model)
        return Answer(reason="the AI answer was invalid after one retry: " + notes[-1], notes=notes,
                      provider=provider, model=model)

    def _call(self, task: str, messages: list[dict], prefer: str | None) -> tuple[str, str, str]:
        self.calls.append({"task": task, "messages": messages})
        if self.mode == "fake":
            return self.fake(task, messages), "fake", "fake-model"
        from app.core import ai_client
        from app.core.privacy import set_dataset_class
        set_dataset_class(self.dataset_class)
        names = self.providers_for(task)
        if self.dataset_class != "public":               # client: walk the private providers only
            last = None
            for name in ([prefer] if prefer in names else []) + [n for n in names if n != prefer]:
                try:
                    r = ai_client.complete(task, messages, only=name)
                    return r.text, r.provider, r.model
                except ai_client.AIUnavailable as err:
                    last = err
            raise last or ai_client.AIUnavailable(["AI not available (client data: private providers only)"])
        r = ai_client.complete(task, messages, prefer=prefer)
        return r.text, r.provider, r.model

    def payload_text(self) -> str:
        return json.dumps(self.calls, ensure_ascii=False)


def gateway(dataset_class: str | None, mode: str | None = None, fake=None) -> Gateway:
    return Gateway(dataset_class or "client", mode or "online", fake)
