"""Summaries of the no-case report: the AI writes text with {{F012}} placeholders only; code fills the numbers.

The validator rejects: a number written as text (digits outside placeholders), an unknown placeholder, causal
words, the word "case" in no_case mode, and person names (the payload guard's name check). If the AI is off,
unavailable or invalid, fixed templates are used, with the reason.
"""
from __future__ import annotations

import re

from app.core.config import settings

PLACEHOLDER = re.compile(r"\{\{(F\d{3})\}\}")


def _cfg() -> dict:
    return settings()["nocase"]["narrative"]


def problems(text: str, fact_ids: set[str], mode: str) -> list[str]:
    from app.nocase.payloads import problems as payload_problems
    out = []
    unknown = [f for f in PLACEHOLDER.findall(text) if f not in fact_ids]
    if unknown:
        out.append("unknown fact(s): " + ", ".join(sorted(set(unknown))))
    bare = PLACEHOLDER.sub("", text)
    if re.search(r"\d", bare):
        out.append("a number written as text: use {{Fxxx}} placeholders only")
    for w in _cfg()["causal_words"]:
        if re.search(rf"\b{re.escape(w)}\b", bare, re.I):
            out.append(f"causal wording '{w}'")
    if mode == "no_case" and re.search(r"\bcases?\b", bare, re.I):
        out.append("the word 'case' (this data has no case ID)")
    if "a person's name" in payload_problems(bare):
        out.append("a person's name")
    return out


def fill(text: str, facts: dict[str, dict]) -> str:
    return PLACEHOLDER.sub(lambda m: facts[m.group(1)]["display"], text)


def templates(facts: list[dict]) -> dict[str, str]:
    """Fixed texts (no AI): process overview and one sentence per step, placeholders only."""
    by = {f["label"]: f["id"] for f in facts}
    out = {"overview": (f"The work in scope comes to {{{{{by['Active hours']}}}}} of active time by "
                        f"{{{{{by['People (actors)']}}}}} people over {{{{{by['Events in scope']}}}}} events. "
                        f"{{{{{by['Share of hours without a step']}}}}} of the hours have no step yet.")}
    for label, fid in by.items():
        if label.endswith(": active hours") and f"{label.split(':')[0]}: share of hours" in by:
            step = label.split(":")[0]
            out[f"step:{step}"] = (f"{step} takes {{{{{fid}}}}} ({{{{{by[step + ': share of hours']}}}}} of all hours) "
                                   f"in {{{{{by[step + ': step runs']}}}}} step runs.")
        if label.endswith(": runs following the usual pattern"):
            step = label.split(":")[0]
            out[f"inside:{step}"] = (f"In {{{{{fid}}}}} of its runs, {step} follows its usual pattern; work inside the "
                                     f"step is redone in {{{{{by[step + ': runs with rework inside the step']}}}}} of runs.")
    return out


def write(facts: list[dict], mode: str, gw=None) -> dict:
    """{"texts": {section: text with placeholders}, "source": "ai:<model>" | "templates", "reason"?}."""
    base = templates(facts)
    ids = {f["id"] for f in facts}
    if gw is None:
        return {"texts": base, "source": "templates", "reason": "AI not used for this run"}
    lines = "\n".join(f"{f['id']} | {f['label']} | {f['display']} | {f['confidence']}" for f in facts)
    msgs = [{"role": "system", "content": _cfg()["prompt"] + (" Never use the word case." if mode == "no_case" else "")},
            {"role": "user", "content": "Sections: " + ", ".join(base) + "\nFacts (id | what | value | confidence):\n"
             + lines}]

    def check(data):
        texts = data.get("texts")
        if not isinstance(texts, dict) or not texts:
            return ['"texts" must be an object {section: text}']
        errs = []
        for k, t in texts.items():
            errs += [f"{k}: {p}" for p in problems(str(t), ids, mode)]
        return errs
    a = gw.ask("nc_summary", msgs, check)
    if not a.ok:
        return {"texts": base, "source": "templates", "reason": a.reason}
    texts = {**base, **{k: str(v) for k, v in a.data["texts"].items() if k in base}}
    return {"texts": texts, "source": f"ai:{a.model}", "provider": a.provider}
