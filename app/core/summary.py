"""Day 4: the executive summary, written from FIXED sentence templates (no AI).

Each finding is {"text": "... {{F012}} ...", "facts": ["F012"]}: words with
placeholders for fact IDs. `render` replaces each placeholder with the fact's
formatted value, so every number in the summary comes from the fact store.
Part 5 lets an AI write findings in this same structure; the templates stay as
the fallback and the reference.
"""
from __future__ import annotations

import re

from app.core.facts import Fact, format_fact

PLACEHOLDER = re.compile(r"\{\{([FC]\d{3,})\}\}")   # F: facts, C: claims (evidence.py)


def _ph(f: Fact) -> str:
    return "{{" + f.id + "}}"


def allowed_badges(band: str) -> set[str]:
    """Which facts the summary may use: High and Medium; only High when quality is Medium."""
    if band == "High":
        return {"High", "Medium"}
    if band == "Medium":
        return {"High"}
    return set()


def template_summary(facts: list[Fact], band: str, flow_band: str | None = None,
                     order_source: str | None = None) -> list[dict]:
    """At most 5 findings from fixed templates, using only facts the band allows (hours, cases, shares).

    Bands Low and Not feasible give one finding: the data is not fit for decisions.
    Fewer than 3 findings are returned when too few facts are trustworthy enough.
    `flow_band` / `order_source` (read from the facts when not given): with flow band
    Low or None no path or hand-off is described, and the summary says so.
    """
    flow_fact = next((f for f in facts if f.type == "flow_confidence"), None)
    if flow_band is None and flow_fact is not None:
        flow_band = flow_fact.meta.get("band")
    if order_source is None and flow_fact is not None:
        order_source = flow_fact.meta.get("source")
    sequence_ok = flow_band not in ("Low", "None")
    def find(type_: str, rank: int | None = None) -> Fact | None:
        return next((f for f in facts if f.type == type_ and f.badge in ok
                     and (rank is None or f.meta.get("rank") == rank)), None)

    def finding(text: str, *used: Fact) -> dict:
        return {"text": text, "facts": [f.id for f in used]}

    data_facts = [f for f in facts if f.section == "data"]
    cases = next((f for f in data_facts if f.type == "cases"), None)
    score = next((f for f in data_facts if f.type == "feasibility_score"), None)
    if band in ("Low", "Not feasible"):
        what = ("exploratory only: the numbers describe this extract but should not be used for decisions"
                if band == "Low" else "not reliable enough for a process analysis")
        text = f"The data quality is {band}"
        used = []
        if score:
            text += f" (score {_ph(score)})"
            used.append(score)
        text += f", so the results are {what}. Fix the issues listed under Data confidence first."
        return [finding(text, *used)]

    ok = allowed_badges(band)
    out: list[dict] = []
    events = next((f for f in data_facts if f.type == "events"), None)
    runs = next((f for f in data_facts if f.type == "step_runs_total"), None)
    hours = next((f for f in data_facts if f.type == "active_hours_total"), None)
    if cases and hours and runs and all(f.badge in ok for f in (cases, hours, runs)):
        out.append(finding(f"The analysis covers {_ph(cases)} cases, {_ph(runs)} step runs and {_ph(hours)} of "
                           "active work.", cases, runs, hours))
    elif cases and events and score and all(f.badge in ok for f in (cases, events, score)):
        out.append(finding(f"The analysis covers {_ph(cases)} cases and {_ph(events)} events "
                           f"(data quality score {_ph(score)}).", cases, events, score))
    median, p90 = find("case_duration_median"), find("case_duration_p90")
    if median and p90:
        out.append(finding(f"The median case takes {_ph(median)}; 90% of cases finish within {_ph(p90)}.",
                           median, p90))
    top_step = _largest(facts, "step_hours_share", ok)
    if top_step:
        hrs = next((f for f in facts if f.type == "step_hours" and f.meta.get("activity") == top_step.meta["activity"]
                    and f.badge in ok), None)
        if hrs:
            out.append(finding(f"{top_step.meta['activity']} takes the most work: {_ph(hrs)} of active time, "
                               f"{_ph(top_step)} of all active hours.", hrs, top_step))
    top_app = _largest(facts, "app_hours_share", ok)
    if top_app:
        out.append(finding(f"{top_app.meta['application']} carries {_ph(top_app)} of the active hours.", top_app))
    if flow_fact is not None and not sequence_ok and flow_fact.badge in ok:
        where = ("no business order was given" if order_source == "data"
                 else "the cases rarely follow the intended order")
        out.append(finding(f"The data does not show a reliable order of steps (flow confidence {_ph(flow_fact)}): "
                           f"{where}, so no path or hand-off is described here.", flow_fact))
    wait = find("waiting_total", rank=1) if sequence_ok else None
    wait_med = find("waiting_median", rank=1) if sequence_ok else None
    if wait and wait_med:
        out.append(finding(f"The most waiting time lies between {wait.meta['src']} and {wait.meta['dst']}: "
                           f"{_ph(wait)} over all cases, with a median wait of {_ph(wait_med)}.", wait, wait_med))
    rework = _largest(facts, "step_rework_share", ok)
    if rework:
        out.append(finding(f"{_ph(rework)} of the cases reaching {rework.meta['activity']} return to it later "
                           "(rework after a pause).", rework))
    if not top_step:   # no active-time column: the path facts describe the process instead
        top = find("variant_share", rank=1) if sequence_ok else None
        if top:
            out.append(finding(f"The most common path covers {_ph(top)} of cases.", top))
        cover = find("variant_coverage_top10") if sequence_ok else None
        if cover:
            out.append(finding(f"The 10 most common paths together cover {_ph(cover)} of cases.", cover))
        rep = find("rework_share", rank=1) if not rework else None
        if rep:
            out.append(finding(f"Activity {rep.meta['activity']} is repeated in {_ph(rep)} of its cases.", rep))
    return out[:5]


def _largest(facts: list[Fact], type_: str, ok: set[str]) -> Fact | None:
    """The fact of this type with the largest value, among the allowed badges."""
    cands = [f for f in facts if f.type == type_ and f.badge in ok and f.value is not None]
    return max(cands, key=lambda f: (f.value, -int(f.meta.get("rank") or 0)), default=None)


def template_recommendations(facts: list[Fact], band: str) -> list[dict]:
    """Up to 3 recommendations from fixed templates (where to look first), with the same placeholders."""
    if band in ("Low", "Not feasible"):
        return []
    ok = allowed_badges(band)
    out = []
    step = _largest(facts, "step_hours_share", ok)
    if step:
        out.append({"text": f"Start with {step.meta['activity']}: it takes {_ph(step)} of the active hours, so "
                            "a small saving per run there counts most.", "facts": [step.id]})
    wait = next((f for f in facts if f.type == "waiting_total" and f.meta.get("rank") == 1 and f.badge in ok), None)
    if wait:
        out.append({"text": f"Look at the waiting between {wait.meta['src']} and {wait.meta['dst']} ({_ph(wait)} in "
                            "total): find out what the cases wait for.", "facts": [wait.id]})
    rework = _largest(facts, "step_rework_share", ok)
    if rework and len(out) < 3:
        out.append({"text": f"Check why {_ph(rework)} of the cases reaching {rework.meta['activity']} come back to "
                            "it after a pause.", "facts": [rework.id]})
    app = _largest(facts, "app_hours_share", ok)
    if app and len(out) < 3:
        out.append({"text": f"Review the work in {app.meta['application']}, which takes {_ph(app)} of the active "
                            "hours.", "facts": [app.id]})
    return out[:3]


def render(text: str, facts: list[Fact]) -> str:
    """Replace each {{Fnnn}} with the fact's value; Medium/Exploratory facts get their badge."""
    by_id = {f.id: f for f in facts}

    def one(match: re.Match) -> str:
        f = by_id.get(match.group(1))
        if f is None:
            return "[unknown fact]"
        value = format_fact(f)
        return value if f.badge == "High" else f"{value} [{f.badge}]"

    return PLACEHOLDER.sub(one, text)
