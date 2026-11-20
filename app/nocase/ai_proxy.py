"""The one door through which no-case stages may call an AI (later prompts), in three modes.

    off     no call; the stage uses its rules
    fake    no network: a canned answer, and every payload is RECORDED (tests and the bench check that no
            truth value, raw row, name or ID is ever sent)
    online  the configured providers (ai_client: routes, pacing, 429 retries from 01c), for PUBLIC datasets
            only; a client dataset is refused here, before ai_client's own private-only rule

Callers pass masked summaries only (narrative.payload_mask runs again on every message).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

MODES = ("off", "fake", "online")


class AIRefused(Exception):
    """The AI may not be used for this dataset in this mode."""


@dataclass
class AIProxy:
    mode: str = "off"
    dataset_class: str = "client"
    fake_answer: str = "{}"
    calls: list[dict] = field(default_factory=list)       # what was sent (fake and online)

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"AI mode must be one of {', '.join(MODES)}")

    @property
    def available(self) -> bool:
        return self.mode == "fake" or (self.mode == "online" and self.dataset_class == "public")

    def why_not(self) -> str | None:
        if self.mode == "off":
            return "AI off for this run"
        if self.mode == "online" and self.dataset_class != "public":
            return "online providers are used for public datasets only; this is client data"
        return None

    def complete(self, task: str, messages: list[dict]) -> str:
        """The answer text; raises AIRefused when the AI may not be used."""
        from app.core.narrative import payload_mask
        why = self.why_not()
        if why:
            raise AIRefused(why)
        safe = [{"role": m["role"], "content": payload_mask(m["content"])} for m in messages]
        self.calls.append({"task": task, "messages": safe})
        if self.mode == "fake":
            return self.fake_answer
        from app.core import ai_client
        from app.core.privacy import set_dataset_class
        set_dataset_class(self.dataset_class)
        return ai_client.complete(task, safe).text

    def payload_text(self) -> str:
        """Everything sent so far, as one text (for leak checks)."""
        return json.dumps(self.calls, ensure_ascii=False)
