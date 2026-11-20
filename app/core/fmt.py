"""One way to print numbers, used by the pages, the report, the Excel notes and the AI placeholders.

duration   hours -> "38 s", "4.2 min", "5.3 h", "3.4 days", "2.1 weeks" (weeks only from 14 days).
           Never "< 1 min": short times keep their seconds.
hours      hours of work -> "1,234.5 h"
pct        a share 0-1 -> "45.2%"
count      a whole number -> "10,907"
A missing value (None / NaN) prints "–". A real zero prints as a zero ("0 s", "0.0%", "0"),
except where a caller passes `zero_dash=True` (e.g. a table cell that is empty rather than zero).
Everything is computed on full precision; only these functions round, and only for display.
"""
from __future__ import annotations

import math

DASH = "–"


def _missing(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def duration(h: float | None, zero_dash: bool = False) -> str:
    """A duration given in HOURS, in the unit people use for it."""
    if _missing(h) or (zero_dash and not h):
        return DASH
    s = float(h) * 3600
    if s < 0:
        return "-" + duration(-float(h))
    if s < 60:
        return f"{s:.0f} s"
    if s < 3600:
        return f"{s / 60:.1f} min"
    if h < 48:
        return f"{h:.1f} h"
    if h < 14 * 24:
        return f"{h / 24:.1f} days"
    return f"{h / 168:.1f} weeks"


def duration_s(seconds: float | None, zero_dash: bool = False) -> str:
    """The same for a value in SECONDS."""
    return DASH if _missing(seconds) else duration(float(seconds) / 3600, zero_dash)


def hours(h: float | None, zero_dash: bool = False) -> str:
    """Hours of work with one decimal and a thousands separator: "1,234.5 h"."""
    if _missing(h) or (zero_dash and not h):
        return DASH
    return f"{float(h):,.1f} h"


def pct(share: float | None, zero_dash: bool = False) -> str:
    """A share (0-1) as a percentage with one decimal."""
    if _missing(share) or (zero_dash and not share):
        return DASH
    return f"{100 * float(share):.1f}%"


def count(n: float | None, zero_dash: bool = False) -> str:
    if _missing(n) or (zero_dash and not n):
        return DASH
    return f"{int(round(float(n))):,}"


def number(x: float | None, decimals: int = 1) -> str:
    """A plain number with a thousands separator."""
    if _missing(x):
        return DASH
    return f"{float(x):,.{decimals}f}"
