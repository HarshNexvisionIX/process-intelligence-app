"""Universal screen normaliser for the no-case mode (the case-ID mode's app/core/screens.py is unchanged).

Window titles from any desktop recorder carry the screen name mixed with things that change per record:
file names with codes and periods ("Cost_Model_AB01_CD02_Jul26.xlsx"), entity or company codes
("AB01 vs CD02"), periods ("Aug26", "July 2026", "2026-07"), numbers, and trailing application or browser
names ("- Microsoft Edge"). This module turns a raw title into a stable screen label, generically:

    1. segments    split on " - ", " | ", " – ", " — "; trailing segments that are an application or browser
                   name (the mapped app + nocase.screen.app_suffixes) are removed
    2. file names  an extension is dropped and "_" / "." inside the name become spaces
    3. masking     periods, numbers of 4+ digits and short codes (letters + digits, 2-6 characters) are removed;
                   a code that STARTS the first segment is kept: it names the screen (transaction codes like AB12, F.13)
    4. tidy        dangling "vs", "-", ":", "&" and repeated spaces are removed; segments without a real word
                   (3+ letters, not a code) are dropped

normalise() returns the prepared text (segments joined with " - ") for the existing cleaner;
fallback() picks a label when the existing cleaner returns nothing. Neither stores or returns raw values that
were masked, and neither knows any process: every word list is in settings (nocase.screen).
"""
from __future__ import annotations

import re

from app.core.config import settings

_SEG = re.compile(r"\s+[-–—|]\s+|\s*\|\s*")
_EXT = re.compile(r"\.(xlsx|xlsm|xlsb|xls|csv|docx|doc|pptx|ppt|pdf|txt|msg|eml|xml|json|zip)\b", re.I)
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_PERIODS = [
    re.compile(rf"\b{_MONTH}[\s'_-]?\d{{2,4}}\b", re.I),            # Jul26, Aug 2026, July-2026, Sep'26
    re.compile(rf"\b\d{{1,2}}[\s_-]?{_MONTH}[\s_-]?\d{{2,4}}\b", re.I),   # 3 Aug 2026
    re.compile(r"\b\d{4}[-/._]\d{1,2}(?:[-/._]\d{1,2})?\b"),          # 2026-07, 2026-07-31
    re.compile(r"\b\d{1,2}[-/._]\d{1,2}[-/._]\d{2,4}\b"),             # 31/07/2026
    re.compile(r"\b\d{1,2}[-/._]\d{4}\b"),                            # 07/2026
    re.compile(r"\b(?:Q[1-4]|H[12])\b"),                              # Q3, H1
    re.compile(r"\bFY\s?\d{2,4}\b", re.I),                            # FY26
    re.compile(r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*day\b", re.I),
]
_CODE = re.compile(r"(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9.]{2,6}")
_NUM = re.compile(r"\d{4,}")
_WORD = re.compile(r"[A-Za-z]{3,}")


def _cfg() -> dict:
    return settings()["nocase"].get("screen") or {}


def _is_suffix(seg: str, app: str | None) -> bool:
    s = seg.strip().lower()
    names = {str(x).strip().lower() for x in _cfg().get("app_suffixes") or []}
    if app:
        names.add(str(app).strip().lower())
    return s in names


def _real_word(seg: str) -> bool:
    return any(_WORD.fullmatch(w) for w in re.findall(r"[A-Za-z]+", seg) if not _CODE.fullmatch(w))


def _mask_segment(seg: str, keep_first_code: bool) -> str:
    seg = _EXT.sub("", seg)
    seg = seg.replace("_", " ")
    for p in _PERIODS:
        seg = p.sub(" ", seg)
    out = []
    for j, tok in enumerate(seg.split()):
        core = tok.strip(".,;:()[]#")
        parts = [p for p in re.split(r"[-/]", core) if p]
        def masked(p: str) -> bool:
            return bool(_NUM.fullmatch(p) or _CODE.fullmatch(p) or p.isdigit())
        if keep_first_code and j == 0 and _CODE.fullmatch(core):
            out.append(core)
        elif parts and all(masked(p) for p in parts):
            continue
        else:
            out.append(tok)
    text = " ".join(out)
    text = re.sub(r"(?i)(?:^|\s)(vs\.?|v\.|and|&|-|:|/)(?=\s*$)", " ", text)       # dangling joiners at the end
    text = re.sub(r"(?i)^\s*(vs\.?|&|-|/)\s+", " ", text)
    text = re.sub(r"(?i)\bvs\.?\s+vs\.?\b", " ", text)
    text = re.sub(r"\s+([,:;])", r"\1", text)
    return " ".join(text.split()).strip(" -|–:,&/.")


def normalise(title: str | None, app: str | None = None) -> str | None:
    """The prepared title (kept segments joined with " - "); None only for an empty title."""
    if title is None or not str(title).strip():
        return None
    segs = [s for s in _SEG.split(str(title).strip()) if s and s.strip()]
    while len(segs) > 1 and _is_suffix(segs[-1], app):
        segs.pop()
    kept = []
    for i, seg in enumerate(segs):
        m = _mask_segment(seg, keep_first_code=(i == 0))
        if m and (_real_word(m) or (i == 0 and _CODE.fullmatch(m))):
            kept.append(m)
    return " - ".join(kept) if kept else None


_NAME_SHAPE = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-zA-Z'-]+){1,2}$")


def _maybe_person(seg: str) -> bool:
    """A segment shaped like a person's name ("Ayakha Ningiza") that has no known screen / business word.
    Privacy first: such a segment is never used as a label (the case-ID cleaner drops it the same way)."""
    from app.core.milestones import looks_like_person
    from app.core.screens import vocabulary
    if looks_like_person(seg):
        return True
    if not _NAME_SHAPE.match(seg.strip()):
        return False
    known = set(vocabulary()) | {str(w).lower() for w in settings()["nocase"].get("screen_words") or []} \
        | {str(w).lower() for w in settings()["level4"].get("label_verbs") or []}
    return not any(w.lower() in known for w in seg.split())


def fallback(title: str | None, app: str | None = None) -> str:
    """A label when the existing cleaner returned nothing: the first segment with a real word that is not a
    person's name (a generic first segment such as "RE:" or "Chat" takes the next one with it), else
    "<app> window". Never None."""
    prepared = normalise(title, app)
    segs = [s for s in (prepared or "").split(" - ") if s and not _maybe_person(s)]
    generic = {str(x).strip().lower().rstrip(":") for x in _cfg().get("generic_first_words") or []}
    if segs:
        first = segs[0]
        if first.lower().rstrip(":") in generic and len(segs) > 1:
            return f"{first} {segs[1]}".strip()
        return first
    return f"{app} window" if app else "window"
