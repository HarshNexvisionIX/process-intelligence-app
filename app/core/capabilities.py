"""What the app can show with the columns this export has, and what each missing column costs.

`capabilities(mapping)` returns one row per feature with a status:

    available  ✅  the needed columns are there
    fallback   ⚠️  works, but from a fallback (a derived case, gaps between events, a template...)
    missing    ❌  cannot be shown with this data

Page 1 shows it after the mapping, the pipeline prints it, and job.json keeps it
(`capabilities`). It only reads the mapping: no data is touched.
"""
from __future__ import annotations

from app.core.derived import SCREEN_NOTE, TIME_WINDOW_NOTE, WORK_SESSION_NOTE, case_kind, case_label

ICONS = {"available": "✅", "fallback": "⚠️", "missing": "❌"}


def _template_on() -> bool:
    from app.core.config import settings
    return bool(settings()["templates"].get("use_for_order"))


def _row(feature: str, status: str, needs: str, fallback: str = "") -> dict:
    return {"feature": feature, "status": status, "available": status != "missing", "needs": needs,
            "fallback": fallback, "icon": ICONS[status]}


def capabilities(mapping: dict) -> list[dict]:
    """One row per feature: {feature, status, available, needs, fallback, icon}."""
    from app.core.narrative import ai_status   # local import avoids a cycle
    from app.core.privacy import ai_allowed

    kind = case_kind(mapping)
    has = {r: bool(mapping.get(r)) for r in ("case_id", "activity", "timestamp", "end_timestamp", "resource",
                                             "application", "window_title", "stage")}
    has_case = has["case_id"] or kind != "column"
    no_case = kind == "overview" or not has_case
    derived_case = kind in ("text_pattern", "work_session", "time_window", "record_join")
    case_note = {"work_session": WORK_SESSION_NOTE, "time_window": TIME_WINDOW_NOTE}.get(
        kind, case_label(mapping) if derived_case else "")
    act_note = SCREEN_NOTE if mapping.get("derived_activity") else ""
    flow_note = " ".join(x for x in (case_note, act_note) if x)
    levels = bool(mapping.get("level1") or mapping.get("level3"))

    def case_based(feature: str, needs: str) -> dict:
        if no_case:
            return _row(feature, "missing", needs, "No case: activity overview only." if kind == "overview" else "")
        if derived_case or act_note:
            return _row(feature, "fallback", needs, flow_note)
        return _row(feature, "available", needs)

    rows = [
        case_based("Process map", "case, activity, timestamp"),
        case_based("Variants (paths)", "case, activity, timestamp"),
        case_based("Waiting times and bottlenecks", "case, timestamp"),
        case_based("Rework", "case, activity"),
        _row("Resources and handovers", "available" if has["resource"] else "missing", "user / resource column",
             "" if has["resource"] else "No user column: who did the work and handovers are not shown."),
        _row("Applications", "available" if has["application"] else "missing", "application column",
             "" if has["application"] else "No application column: applications are not shown."),
        _row("Application switches", "available" if has["application"] and has["resource"] else
             ("fallback" if has["application"] else "missing"), "application column (+ user column)",
             "" if has["application"] and has["resource"] else
             ("No user column: switches are counted per case, not per person." if has["application"] else
              "No application column: switches cannot be counted.")),
        _row("Stages on the map", "available" if has["stage"] else "fallback", "stage / group column",
             "" if has["stage"] else
             ("No stage column → steps are grouped by the process template or your business definition; without "
              "either, the map has no stage boxes." if _template_on() else
              "No stage column → steps are grouped by your business definition; without it, the map has no stage "
              "boxes.")),
        _row("Sub-process hierarchy", "available" if levels else ("fallback" if has["stage"] else "missing"),
             "process level columns (level 1-3)",
             "" if levels else "Only one level (the stage column) is known." if has["stage"] else
             "No process level columns → no sub-process hierarchy (levels that are missing are skipped)."),
        _row("Milestones", "available" if mapping.get("action") else "missing", "an action / completion column (e.g. submitted, approved)",
             "Completion actions are mapped; frequency and user thresholds apply." if mapping.get("action") else
             "No action column → milestones (submitted, approved, closed) cannot be detected."),
        # Based on the CURRENT mapping: a mapped step-run ID / active time is used, never "estimated".
        _row("Step runs", "available" if mapping.get("step_run") else "fallback", "a step-run ID column",
             f"Runs come from the step-run ID ({mapping['step_run']})." if mapping.get("step_run") else
             "No step-run ID → a run of a step is consecutive events of that step in a case."),
        _row("Active time and effort",
             "available" if mapping.get("active_time") or has["end_timestamp"] else "fallback",
             "an active-time (or end time) column",
             (f"Effort comes from the active-time column ({mapping['active_time']})"
              + (", with waiting from the wait / turnaround columns." if mapping.get("wait_time") or mapping.get("tat_time")
                 else ".")) if mapping.get("active_time") else
             "" if has["end_timestamp"] else "No active time or end time → time spent is estimated from gaps between "
                                             "events."),
        _row("Trends", "available" if has["timestamp"] and not no_case else ("fallback" if has["timestamp"] else "missing"),
             "timestamp", "" if not no_case else "Without cases, trends count events per month."),
        _row("Business definition check", "fallback", "an approved business definition of the process",
             "Until a business definition is approved, the order comes from the business, "
             + ("a template, " if _template_on() else "your documents, ") + "the AI "
             "or the data (page 3)."),
    ]
    ai_ok = ai_status()[0] and ai_allowed()[0]
    rows.append(_row("AI summary", "available" if ai_ok else "fallback", "AI switched on and allowed (.env)",
                     "" if ai_ok else "The summary is written from fixed templates."))
    return rows


def missing_lines(rows: list[dict]) -> list[str]:
    """One plain line per feature that is missing or uses a fallback."""
    return [f"{r['icon']} {r['feature']}: {r['fallback']}" for r in rows if r["status"] != "available" and r["fallback"]]


def as_text(rows: list[dict]) -> str:
    """The matrix as plain text (command line)."""
    width = max(len(r["feature"]) for r in rows)
    lines = [f"  {r['icon']} {r['feature']:<{width}}  {r['fallback'] or 'ok'}" for r in rows]
    return "\n".join(lines)
