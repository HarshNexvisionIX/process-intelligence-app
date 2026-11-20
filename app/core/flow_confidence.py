"""Flow confidence: does the data (or the business) give a reliable ORDER of steps?

The quality gate (quality.py) scores whether data is CLEAN. A perfectly clean log
can still have no flow: on desktop data people do steps in any order. Flow
confidence is a separate score (0-100) and band that controls what the map, the
facts and the AI may claim. It never changes the data-quality score.

Where the order comes from, in trust order:
    confirmed  the business confirmed it                -> the order is trusted
    template   a process template matched the steps     -> trusted, capped at Medium
    ai         suggested by the AI from step names      -> trusted, capped at Medium
    data       suggested from the data                  -> scored from the evidence
"""
from __future__ import annotations

from app.core.config import settings

SOURCES = ("confirmed", "template", "evidence", "ai", "data (milestones)", "data")
BANDS = ("High", "Medium", "Low", "None")
NOT_CONFIRMED = "order not yet confirmed by the business"

# What each band allows the app to show and say (see README "Data quality vs flow confidence").
ALLOWED = {
    "High":   {"map_title": "Process flow", "arrows": True, "sequence_badge_cap": "High",
               "sequence_in_summary": True, "ai_sequence": "yes"},
    "Medium": {"map_title": "Intended order", "arrows": True, "sequence_badge_cap": "Medium",
               "sequence_in_summary": True, "ai_sequence": "intended order only"},
    "Low":    {"map_title": "Intended order (weak evidence)", "arrows": True, "sequence_badge_cap": "Exploratory",
               "sequence_in_summary": False, "ai_sequence": "no"},
    "None":   {"map_title": "Step overview — no fixed order found", "arrows": False,
               "sequence_badge_cap": "Exploratory", "sequence_in_summary": False, "ai_sequence": "no"},
}
SOURCE_LABELS = {"confirmed": "Confirmed by business", "template": "Template",
                 "evidence": "From your documents, checked against the data — needs confirmation",
                 "ai": "AI suggestion — needs confirmation",
                 "data": "Found in data", "data (milestones)": "Found in data (milestones)"}


def _cfg() -> dict:
    return settings()["flow_confidence"]


def band_of(score: float) -> str:
    b = _cfg()["bands"]
    return "High" if score >= b["high"] else "Medium" if score >= b["medium"] else "Low" if score >= b["low"] else "None"


def strength_score(order_strength: float) -> float:
    """Order strength (0-1) -> 0-100: 0.5+ -> 80+, 0.2-0.5 -> 50-79, below 0.2 -> below 40."""
    p = _cfg()["strength_points"]
    s = max(0.0, min(1.0, float(order_strength or 0.0)))
    if s >= p["strong"]:
        return 80 + (s - p["strong"]) / (1 - p["strong"]) * 20
    if s >= p["partial"]:
        return 50 + (s - p["partial"]) / (p["strong"] - p["partial"]) * 29
    return s / p["partial"] * 39


def order_source_of(order: dict) -> str:
    """The trust level of an order (an Order as dict): confirmed / template / ai / data.

    An order edited by hand but not yet confirmed counts as "template": it is
    knowledge about the process, not evidence from the data, but not confirmed.
    """
    if order.get("confirmed_by_business"):
        return "confirmed"
    source = order.get("source") or "suggested"
    if source in ("template", "evidence", "ai", "data (milestones)"):
        return source
    if source == "edited":
        return "template"
    return "data"


WEAK_STRENGTH, FEW_IN_ORDER, WEAK_TOP = 0.3, 0.25, 50     # a weak data order: band Low, score at most 50 (inside Low)


def flow_confidence(order_strength: float, order_source: str, in_order_share: float | None,
                    pair_order_share: float | None, stability: float | None,
                    caps: list[tuple[str, str]] | None = None, coverage: float | None = None,
                    data_strength: float | None = None, data_in_order: float | None = None) -> dict:
    """Score (0-100), band, plain reasons, what the band allows (map, facts, AI), and `explain`: the formula in one
    line.

    `caps` [(highest band, reason)] limit the band, e.g. for derived cases
    (derived.flow_caps): work sessions at most Medium, time windows at most Low.
    For an order from the data (or its milestones) the evidence is the order strength of ALL the data
    (`data_strength`, when the order itself was found on a subset such as the milestone actions) and the share of
    cases that follow it (`data_in_order`, else `in_order_share`): weak strength (< 0.3) or fewer than 25% of
    cases in order caps the band at Low (score at most 39).
    """
    cfg = _cfg()
    source = order_source if order_source in SOURCES else "data"
    reasons: list[str] = []
    weak, weak_reason = False, ""
    if source in ("data", "data (milestones)"):
        strength = min(float(order_strength), float(data_strength)) if data_strength is not None else float(order_strength)
        in_order = data_in_order if data_in_order is not None else in_order_share
        base = strength_score(float(order_strength))     # the order's own evidence; weak data caps it below
        w = float(cfg["stability_weight"])
        stab = 1.0 if stability is None else float(stability)
        score = base * (1 - w + w * stab)
        band = band_of(score)
        if strength < settings()["reference_map"]["order_strength"]["partial"]:
            reasons.append("no fixed order in the data")
        elif strength < settings()["reference_map"]["order_strength"]["strong"]:
            reasons.append("the data shows a partial order")
        else:
            reasons.append("the data shows a clear order")
        if stability is not None and stability < 0.5:
            reasons.append("the suggested order changes between samples of the cases")
        reasons.append("no business order given")
        weak = strength < WEAK_STRENGTH or (in_order is not None and in_order < FEW_IN_ORDER)
        if weak:
            weak_reason = (f"order strength {strength:.2f}"
                           + (f", {100 * in_order:.1f}% of cases follow the order" if in_order is not None else ""))
        explain = (f"Score from the order strength ({float(order_strength):.2f}; 0 = steps in any order, 1 = always "
                   f"the same order), weighted by how stable the order is across samples; Low with at most "
                   f"{WEAK_TOP} when the strength of all the data ({strength:.2f}) is below {WEAK_STRENGTH} or fewer "
                   f"than {100 * FEW_IN_ORDER:.0f}% of cases follow the order"
                   + (f" ({100 * in_order:.1f}% do)." if in_order is not None else "."))
    else:
        # The order itself is knowledge; the score says how well reality follows it.
        score = 100 * float(pair_order_share or 0.0)
        band = band_of(score)
        if BANDS.index(band) > BANDS.index("Medium"):
            band = "Medium"                     # at least Medium: the order is trusted
        if source != "confirmed":
            band = "Medium"                     # capped at Medium until confirmed
            reasons.append(NOT_CONFIRMED)
        else:
            reasons.append("order confirmed by the business")
        if in_order_share is not None:
            reasons.append(f"{100 * in_order_share:.0f}% of cases follow it exactly")
        explain = (f"Score = share of step pairs done in the intended order ({100 * float(pair_order_share or 0):.0f}%); "
                   "at most Medium until the order is confirmed by the business.")
    caps = list(caps or [])
    if source == "data (milestones)" and coverage is not None and coverage < 0.5:
        reason = f"based on the {coverage:.0%} of cases that record milestone actions"
        reasons.append(reason)
        caps.append(("Medium", reason))
    tops = {"Medium": cfg["bands"]["high"] - 1, "Low": cfg["bands"]["medium"] - 1, "None": cfg["bands"]["low"] - 1}
    for cap, reason in caps or []:
        if BANDS.index(band) < BANDS.index(cap):
            band = cap
            score = min(score, tops[cap])
            if reason not in reasons:
                reasons.append(reason)
    if source in ("data", "data (milestones)") and weak and band != "None":
        # a weak order (strength < 0.3 or < 25% of cases in order) is Low with a score of at most 50 (Low = 40-59)
        band, score = "Low", min(score, WEAK_TOP)
        if weak_reason not in reasons:
            reasons.append(weak_reason)
    return {"score": int(round(score)), "band": band, "source": source, "reasons": reasons,
            "allowed": dict(ALLOWED[band]), "explain": explain}


def describe(flow: dict) -> str:
    """"Flow confidence: 22 None (no fixed order in the data; no business order given)"."""
    return f"Flow confidence: {flow['score']} {flow['band']} ({'; '.join(flow['reasons'])})"


def cap_badge(badge: str, cap: str) -> str:
    """The lower of two badges (High > Medium > Exploratory)."""
    order = ("High", "Medium", "Exploratory")
    return order[max(order.index(badge), order.index(cap))]
