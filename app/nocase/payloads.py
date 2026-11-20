"""Every AI payload of the no-case mode is built here, from CLEANED tables only, and passes a final guard.

The guard raises PayloadError when any text looks like an e-mail, a number of 4+ digits, an ID shape (5+ characters
with 3+ digits), a raw window title (a segment ending in an application name), or a person's name (a first name
from config/first_names.txt followed by a capitalised word, as milestones.mask_names finds them). Nothing raw
reaches the AI: no rows, titles, IDs, names or typed text.
"""
from __future__ import annotations

import re

import pandas as pd

from app.core.config import settings

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DIGITS = re.compile(r"\d{4,}")
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)*")


class PayloadError(ValueError):
    """A payload would send something raw to the AI."""


OWN_IDS = re.compile(r"\b[UE]\d{5,6}\b")     # the app's own unit / episode ids (U00012, E000034): not data
# A count the app computed and wrote as a JSON number value ("event_count":41175): an aggregate, not an ID. Only
# bare numbers right after a JSON key are skipped; a number inside any text is still checked.
JSON_COUNT = re.compile(r'(?<=":)-?\d+(?:\.\d+)?(?=[,}\]])')
# A full name written with its role, as documents do ("owner: Jane Smith", "Jane Smith (Finance Lead)"): masked even
# when the first name is not in the first-name list.
_ROLE_WORDS = (r"owner|author|prepared by|approved by|reviewed by|checked by|contact|sent by|requested by|signed by|"
               r"presenter|interviewer|interviewee|(?:from|to|cc)\s*:")
_NAMED_ROLE = re.compile(rf"(?i:\b(?:{_ROLE_WORDS})\s*[:\-]?\s*)([A-Z][a-z]+(?:\s+[A-Z][A-Za-z'\-]+){{1,2}})\b")
_NAME_THEN_ROLE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][A-Za-z'\-]+){1,2})(?=\s*\((?:[^)]*\b(?:Lead|Manager|Analyst|"
                             r"Director|Head|Owner|Accountant|Controller|Specialist|Officer|Clerk|Partner|Assistant|"
                             r"Associate|Consultant|SME)\b[^)]*)\))")


def _role_names(text: str) -> list[str]:
    return [m.group(1) for m in _NAMED_ROLE.finditer(text)] + [m.group(1) for m in _NAME_THEN_ROLE.finditer(text)]


def problems(text: str) -> list[str]:
    from app.core.milestones import NAME_TOKEN, first_names, mask_names
    from app.core.screens import app_names
    cfg = settings()["nocase"]["id_shapes"]
    text = JSON_COUNT.sub("0", OWN_IDS.sub(" ", text))
    out = []
    if _EMAIL.search(text):
        out.append("an e-mail address")
    if _DIGITS.search(text):
        out.append("a number of 4 or more digits")
    for tok in _TOKEN.findall(text):
        if len(tok) >= int(cfg["min_length"]) and sum(ch.isdigit() for ch in tok) >= int(cfg["min_digits"]):
            out.append("an ID-shaped value")
            break
    apps = "|".join(re.escape(a) for a in sorted(app_names(), key=len, reverse=True) if a)
    if apps and re.search(rf"\s[-|–]\s(?:{apps})\s*$", text, re.I | re.M):
        out.append("a raw window title")
    for line in text.splitlines():
        flat = " ".join(line.split())
        if mask_names(flat) != flat and NAME_TOKEN not in flat:
            out.append("a person's name")
            break
    if "a person's name" not in out and _role_names(text):
        out.append("a person's name")
    if "a person's name" not in out:
        from app.core.screens import vocabulary
        ok_words = vocabulary() | {str(w).lower() for w in settings()["nocase"]["payload"]["name_exceptions"]} \
            | {str(w).lower() for w in settings()["level4"].get("label_verbs") or []}
        names = first_names()
        if any(w.lower() in names and w.lower() not in ok_words for w in re.findall(r"\b[A-Z][a-z]+\b", text)):
            out.append("a person's name")
    return sorted(set(out))


MASKS = {"email": "<email>", "number": "<number>", "id": "<id>"}


def scrub_text(text: str) -> tuple[str, dict[str, int]]:
    """Document text made safe for the AI, with the same rules the guard checks: e-mails, ID-shaped values,
    numbers of 4+ digits, window-title endings and person names are replaced by <email>, <id>, <number>,
    (nothing) and <name>. Returns (text, {what: how many were replaced}). Used for business documents
    (SOPs, transcripts) before they are sent: the guard then passes instead of stopping the whole request."""
    from app.core.milestones import NAME_TOKEN, first_names, mask_names
    from app.core.screens import app_names, vocabulary
    cfg = settings()["nocase"]["id_shapes"]
    counts = {"e-mail addresses": 0, "IDs and long numbers": 0, "person names": 0}

    def _sub(pattern, repl, value, key):
        new, n = re.subn(pattern, repl, value)
        counts[key] += n
        return new

    def _id(m):
        tok = m.group(0)
        if len(tok) >= int(cfg["min_length"]) and sum(ch.isdigit() for ch in tok) >= int(cfg["min_digits"]) \
                and not OWN_IDS.fullmatch(tok):
            counts["IDs and long numbers"] += 1
            return MASKS["id"]
        return tok

    ok_words = vocabulary() | {str(w).lower() for w in settings()["nocase"]["payload"]["name_exceptions"]} \
        | {str(w).lower() for w in settings()["level4"].get("label_verbs") or []}
    names = first_names()
    apps = "|".join(re.escape(a) for a in sorted(app_names(), key=len, reverse=True) if a)
    out = []
    for line in text.splitlines():
        for full in _role_names(line):
            counts["person names"] += line.count(full)
            line = line.replace(full, NAME_TOKEN)
        line = _sub(_EMAIL, MASKS["email"], line, "e-mail addresses")
        line = _TOKEN.sub(_id, line)
        line = _sub(_DIGITS, MASKS["number"], line, "IDs and long numbers")
        if apps:
            line = re.sub(rf"\s[-|–]\s(?:{apps})\s*$", "", line, flags=re.I)
        flat = " ".join(line.split())
        masked = mask_names(flat)
        if masked != flat:
            counts["person names"] += masked.count(NAME_TOKEN) - flat.count(NAME_TOKEN)
            line = masked

        def _single(m):
            w = m.group(0)
            if w.lower() in names and w.lower() not in ok_words:
                counts["person names"] += 1
                return NAME_TOKEN
            return w
        out.append(re.sub(r"\b[A-Z][a-z]+\b", _single, line))
    return "\n".join(out), {k: v for k, v in counts.items() if v}


def guard(messages: list[dict]) -> list[dict]:
    """The messages unchanged, or PayloadError naming what was found (never the value itself)."""
    for m in messages:
        found = problems(str(m.get("content") or ""))
        if found:
            raise PayloadError("The AI payload was stopped: it contains " + ", ".join(found) + ".")
    return messages


def unit_lines(units: pd.DataFrame, total: int) -> list[str]:
    """"U0123 | app | screen | click | 1.2%" from the units table (cleaned values only)."""
    out = []
    for r in units.itertuples():
        out.append(f"{r.unit_id} | {r.app or '-'} | {r.screen_clean or '-'} | {r.action_clean or '-'} | "
                   f"{100 * r.events / max(total, 1):.1f}%")
    return out


def step_lines(pack: dict) -> list[str]:
    out = []
    for s in pack.get("stages") or []:
        for st in s.get("steps") or []:
            out.append(f"{st['name']} (stage: {s['name']}): {st.get('description') or ''}".strip())
    return out
