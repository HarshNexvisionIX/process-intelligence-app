"""Screens and typed text for Level 4: what a click means depends on the screen it happens on.

clean_screen   the window title as a short screen label, with privacy first:
               - split on " - ", " | " and " – "; the application part (the given application, or a
                 known application name) is dropped;
               - a segment that looks like a person is dropped: 1-4 words, each starting with a capital
                 letter, none of them in the UI vocabulary (screens.ui_vocabulary + level4.ui_words);
                 a segment with a first name from the lexicon is dropped too;
               - "(Terminated)" and other parenthesised UI words stay, attached to the previous segment;
                 parenthesised names are dropped;
               - #hex# tokens, long hex IDs, e-mails, numbers of 4+ digits, IDs such as P50052761,
                 dates and clock times are removed;
               - file names (".xlsx", ".docx" ...) keep their stem without digits, dates, time zones and
                 person-like words: "Prod Tracker 2026.xlsx" -> "Prod Tracker.xlsx".
               Returns None when nothing readable is left.
is_typed       a click "name" that is really typed text: it contains a greeting or sign-off word
               (whole words only: "hi" never matches "History"), or reads like a sentence (3+ words with
               sentence punctuation) without any UI word. Such clicks are stored only as "<typed text>".
word_pattern   a rule pattern made only of plain words ("email|hi") matches whole words only.
"""
from __future__ import annotations

import re
from functools import lru_cache

from app.core.config import settings

TYPED = "<typed text>"
FILE_EXT = re.compile(r"\.(xlsx|xlsm|xls|csv|docx|doc|pdf|pptx|ppt|txt|msg)$", re.IGNORECASE)
_SPLIT = re.compile(r"\s+[-|–]\s+")
_TZ = {"ist", "utc", "gmt", "cet", "cest", "est", "edt", "pst", "pdt", "bst", "aest", "sast", "eet"}


def _cfg() -> dict:
    return settings().get("screens") or {}


@lru_cache(maxsize=1)
def ui_words() -> frozenset[str]:
    return frozenset(str(w).lower() for w in settings()["level4"]["ui_words"])


@lru_cache(maxsize=1)
def vocabulary() -> frozenset[str]:
    """Words that make a title segment a screen, not a person (UI words plus screens.ui_vocabulary)."""
    return ui_words() | frozenset(str(w).lower() for w in _cfg().get("ui_vocabulary") or [])


@lru_cache(maxsize=1)
def app_names() -> frozenset[str]:
    return frozenset(str(w).lower() for w in _cfg().get("app_names") or [])


@lru_cache(maxsize=1)
def _typed_words() -> re.Pattern:
    words = settings()["level4"].get("typed_words") or ["hello", "hi", "regards"]
    return re.compile(r"\b(?:" + "|".join(re.escape(str(w)) for w in words) + r")\b", re.IGNORECASE)


def is_typed(label: str | None) -> bool:
    """True for typed content (see the module docstring)."""
    text = str(label or "").strip()
    if not text:
        return False
    if _typed_words().search(text):
        return True
    words = re.findall(r"[A-Za-z]+", text)
    sentence = len(words) >= 3 and re.search(r"[,.!?](\s|$)", text) is not None
    return sentence and not any(w.lower() in ui_words() for w in words)


def word_pattern(pattern: str | None) -> str | None:
    """"email|hi" -> r"\b(?:email|hi)\b": plain-word patterns match whole words only."""
    if not pattern or not re.fullmatch(r"[A-Za-z0-9 ]+(\|[A-Za-z0-9 ]+)*", pattern):
        return pattern
    return r"\b(?:" + pattern + r")\b"


def _person_like(segment: str, extra: frozenset[str] = frozenset()) -> bool:
    from app.core.milestones import first_names
    words = segment.split()
    if not 1 <= len(words) <= 4:
        return False
    clean = [w.strip(".,'’") for w in words]
    if any(w.lower() in vocabulary() or w.lower() in app_names() or w.lower() in extra for w in clean):
        return False                          # a UI word makes it a screen ("Mark Complete")
    if any(w.lower() in first_names() for w in clean if w):
        return True
    for w in clean:
        if not w or not w[0].isupper() or not re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", w):
            return False
        if w.lower() in vocabulary() or w.lower() in app_names():
            return False
        if w.isupper() and len(w) <= 4:      # an acronym such as HR or SAP
            return False
    return True


def _scrub(text: str) -> str:
    """Remove codes, e-mails, IDs, long numbers, dates and clock times."""
    text = re.sub(r"#[0-9a-fA-F]+#|\b[0-9a-fA-F]{16,}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", " ", text)
    text = re.sub(r"(?<![\w])-?\d{1,4}[-/._]\d{1,2}(?:[-/._]\d{1,4})?(?![\w])", " ", text)     # dates, 20_10, -06-17
    text = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", " ", text)                                # clock times
    text = re.sub(r"\b[A-Za-z]{0,3}\d{4,}\w*\b|\b\w*\d{4,}\w*\b", " ", text)                  # IDs, 4+ digits
    return text


def _clean_file(name: str, extra: frozenset[str] = frozenset()) -> str | None:
    ext = FILE_EXT.search(name).group(0)
    stem = _scrub(name[: -len(ext)])
    words = [w for w in re.split(r"[\s_]+", stem) if w and not re.search(r"\d", w) and w.lower() not in _TZ]
    stem = " ".join(words).strip(" -_.,")
    parts = [p for p in _SPLIT.split(stem) if p.strip(" -_.,")]
    parts = [p for p in parts if not _person_like(p.strip(" -_.,"), extra)]
    stem = " - ".join(p.strip(" -_.,") for p in parts)
    return f"{stem}{ext}" if stem else None


def clean_screen(title: str | None, application: str | None = None,
                 extra_vocabulary: list[str] | None = None) -> str | None:
    """A screen label from a window title (see the module docstring).

    `extra_vocabulary` (optional, default none: today's behaviour) adds words that make a segment a screen,
    not a person (the no-case mode passes nocase.screen_words)."""
    extra = frozenset(str(w).lower() for w in extra_vocabulary or ())
    from app.core.milestones import NAME_TOKEN, mask_names
    if not title:
        return None
    segments = [s.strip() for s in _SPLIT.split(str(title).strip()) if s.strip()]
    app = str(application or "").strip().lower()
    while segments and (segments[-1].lower() == app or segments[-1].lower() in app_names()):
        segments.pop()
    if segments and (segments[0].lower() == app) and len(segments) > 1:
        segments.pop(0)
    kept: list[str] = []
    for seg in segments:
        if FILE_EXT.search(seg):
            label = _clean_file(seg, extra)
            if label:
                kept.append(label)
            continue
        seg = " ".join(_scrub(seg).split()).strip(" -|–:,")
        if not seg:
            continue
        inner = re.fullmatch(r"\((.*)\)", seg)
        if inner:
            words = inner.group(1).split()
            if words and all(w.lower().strip(".,") in vocabulary() for w in words) and kept:
                kept[-1] = f"{kept[-1]} ({inner.group(1)})"
            continue                                           # parenthesised names (or nothing to attach to)
        tail = re.search(r"\s*\(([^()]*)\)$", seg)              # "Worker (Terminated)" inside one segment
        if tail and not all(w.lower() in vocabulary() for w in tail.group(1).split()):
            seg = seg[: tail.start()].strip()
        if not seg or _person_like(seg, extra):
            continue
        seg = mask_names(seg)
        if not seg.replace(NAME_TOKEN, "").strip(" -|–:,"):
            continue
        kept.append(seg)
    text = " - ".join(kept)
    text = " ".join(text.split())
    return text or None
