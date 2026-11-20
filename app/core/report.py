"""Day 4: the report (HTML and Excel; PDF through the browser's Print -> Save as PDF).

Gist
----
- `generate_report` computes the step order (reference.json), the report numbers (metrics.json,
                    metrics.py: the ONE place they are computed), the facts, the executive summary
                    (narrative.json) and the calculation checks (checks.json, report_checks.py), then
                    writes report.html and report.xlsx.
- `build_context`   gathers everything into the 14 report sections (TITLES). Hours, cases and shares
                    are formatted only at display time, by fmt.py.
- `render_html`     one self-contained HTML file (inline CSS, prints well on A4).
- `write_excel`     the same content as tables, numbers stored as numbers.

The report proves its own numbers: every key number is recomputed independently (report_checks.py)
and the header says "All N calculation checks passed" or the title starts with "DRAFT — K checks failed".
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from app.core import fmt, jobs, metrics as metrics_mod, reference
from app import __version__
from app.core.config import PROJECT_ROOT, settings
from app.core.db import connect
from app.core.facts import Fact, build_facts, format_fact, load_facts, save_facts
from app.core.mapping import ROLE_LABELS
from app.core.derived import SCREEN_NOTE, TIME_WINDOW_NOTE, WORK_SESSION_NOTE, case_kind, case_label
from app.core.mining import describe_switching, to_app_dot
from app.core.narrative import save_narrative, write_summary

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
log = logging.getLogger(__name__)
TITLES = [
    ("key", "Key numbers"), ("scope", "Where the data comes from"), ("summary", "Executive summary"),
    ("process", "Process map and steps"), ("time", "Time by stage and application"),
    ("inside", "Inside each step (Level 4)"), ("conformance", "Flow and conformance"),
    ("waiting", "Waiting between steps"), ("rework", "Rework"), ("people", "People and handovers"),
    ("applications", "Applications and switching"), ("trend", "Trend"), ("variants", "Variants"),
    ("appendix", "Appendix"),
]
# Report section -> the mining.json section whose availability (data-quality band) it follows.
GATE = {"key": "kpis", "process": "map", "time": "kpis", "waiting": "bottlenecks", "rework": "rework",
        "people": "resources", "applications": "app_switching", "trend": "trends", "variants": "variants",
        "conformance": "map"}
DEFINITIONS = [
    ("Events in scope", "rows with a case ID, an activity and a usable time, after the row filter on Import data"),
    ("Active hours of a step", "sum of the active-time column over the step's events in scope"),
    ("Step run", "consecutive events of the same activity in the same case, sorted by time and then by the row "
                 "order in the file"),
    ("Case duration", "time of the last event (its end when recorded) minus time of the first event, in scope; "
                      "a case with one event lasts 0 and is counted"),
    ("Cases reaching a step", "cases with at least one event of the step; % = ÷ all cases in scope"),
    ("Share of hours", "hours ÷ active hours in scope"),
    ("Waiting between steps", "start of the next run − end of the previous run in the same case (overlaps count "
                              "as 0); ranked by the total over all cases"),
    ("Waiting share", "wait-time column ÷ turnaround-time column (or active + wait when no turnaround column); "
                      "idle time is never added to wait"),
    ("Rework", "the same step again in a case after another step, at least report.rework_gap_minutes after the end "
               "of its previous run; a return within the gap is quick switching, not rework"),
    ("Application switch", "two consecutive events of the same case and user with different known applications, "
                           "at most app_switching.session_gap_minutes apart, counted before repeats are merged"),
    ("Conformance", "on first occurrences: the first event of each ordered step (time, then row order) gives its "
                    "place; in order = these places follow the order (cases with 2+ ordered steps)"),
    ("Week", "ISO week (Monday to Sunday, UTC); the first and last week of the data are partial"),
]


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _avail(mining: dict, key: str) -> tuple[bool, str | None]:
    s = mining.get(key) or {}
    return bool(s.get("available")), s.get("reason") or "Not available."


def _drawn_order(ref: dict) -> tuple[reference.Order, list[str]]:
    """The order drawn on the map: when some steps have a real stage, the steps without one are not drawn
    (returned separately, for the "Other activities without a stage" line)."""
    order = reference.Order.from_dict(ref["order"])
    unnamed = ("", "(no stage)")
    if any(s["name"] not in unnamed for s in order.stages):
        left = [step for s in order.stages if s["name"] in unnamed for step in s["steps"]]
        order.stages = [s for s in order.stages if s["name"] not in unnamed]
        return order, left
    return order, []


# --------------------------------------------------------------------------- #
# 1. Context
# --------------------------------------------------------------------------- #
def build_context(job_id: str, use_ai: bool = True, client=None, inside_ai: bool | None = None) -> dict:
    """Everything the report shows, as plain data (used by the HTML and the Excel file).

    The executive summary is written by narrative.write_summary (AI when allowed, otherwise fixed
    templates) and saved as narrative.json; the calculation checks run last (checks.json).
    """
    from app.core import report_checks
    folder = jobs.job_folder(job_id)
    job = jobs.load_job(job_id)
    mapping = _load(folder / "mapping.json") or {}
    quality = _load(folder / "quality.json")
    mining = _load(folder / "mining.json")
    if quality is None or mining is None:
        raise ValueError("Run the quality check and the analysis before generating the report.")
    m = metrics_mod.load(folder)
    if m is None:
        m = metrics_mod.compute(connect(), folder, _load(folder / "reference.json"))
        metrics_mod.save(m, folder)
    ref = _load(folder / "reference.json")
    facts = load_facts(folder)
    rcfg = settings()["report"]
    no_case = bool((mining.get("overview") or {}).get("available"))
    t = m["totals"]
    trusted = m["order"]["trusted"]
    sections: dict[str, dict] = {}

    def gated(key: str) -> dict:
        if no_case:
            return {"available": False, "reason": (mining.get("kpis") or {}).get("reason") or "Not available: no case"}
        ok, why = _avail(mining, GATE[key]) if key in GATE else (True, None)
        return {"available": ok, "reason": None if ok else why}

    # 1. Key numbers
    key = gated("key")
    if no_case:
        key = {"available": True, "reason": None, "no_case": True, "activity_overview": mining["overview"]["data"]}
    else:
        conf = m.get("conformance")
        key["tiles"] = [
            ("Cases", fmt.count(t["cases"]), "distinct case IDs in scope"),
            ("Step runs", fmt.count(t["runs"]), "consecutive events of one step merged"),
            ("Events before merging", fmt.count(t["events"]), "rows in scope"),
            ("Participants", fmt.count(t["participants"], True), "distinct users"),
            ("Applications", fmt.count(t["applications"], True), "distinct applications"),
            ("Active hours in scope", fmt.hours(t["active_hours"]), "sum of active time"),
            ("Median case duration", fmt.duration(t["median_case_h"]), "first to last event"),
            ("90% of cases finish within", fmt.duration(t["p90_case_h"]), "90th percentile"),
        ]
        if trusted and conf:
            key["tiles"].append((f"Cases reaching {conf['last_step']}", fmt.pct(conf["reach_last_share"]),
                                 f"{fmt.count(conf['reach_last'])} of {fmt.count(t['cases'])} cases"))
    sections["key"] = key

    # 2. Where the data comes from (scope.json)
    sections["scope"] = scope_section(m.get("scope"), job)

    # 3. Executive summary
    narrative = write_summary(job_id, facts, mining["band"], client=client, use_ai=use_ai)
    save_narrative(narrative, folder)
    if narrative.source == "ai":
        via = f" ({narrative.model} via {narrative.provider})" if narrative.provider else ""
        provenance = (f"Written by AI{via} from {narrative.facts_sent} verified facts; every number was inserted "
                      f"by the system and checked (prompt version {narrative.prompt_version}).")
    else:
        provenance = f"Generated from fixed templates (AI not used: {narrative.reason})."
    sections["summary"] = {"available": True, "reason": None, "source": narrative.source, "provenance": provenance,
                           "summary": narrative.summary[:5], "findings": narrative.findings[:5],
                           "recommendations": narrative.recommendations[:3]}

    # 4. Process map and the step table (business order when trusted)
    proc = gated("process")
    if proc["available"]:
        staged = m["staged"]
        rows = [r for r in m["steps"] if r["stage"] or not staged]
        others = [r for r in m["steps"] if staged and not r["stage"]]
        proc.update(rows=rows, has_active=m["has"]["active"], has_wait=m["has"]["wait"],
                    other_line=_other_line(others, m["has"]["active"]), sorted_by_cases=not trusted)
        if ref:
            from app.core.flow_confidence import describe as describe_flow
            order, _left = _drawn_order(ref)
            proc.update(dot=reference.to_reference_dot(order, ref["stats"], True, ref.get("flow")),
                        legend=reference.LEGEND, flow_none=bool(ref.get("flow") and ref["flow"]["band"] == "None"),
                        scores_line=(f"Data quality: {quality['feasibility']} {quality['band']} · "
                                     + (describe_flow(ref["flow"]) if ref.get("flow") else "Flow confidence: not computed")),
                        flow_explain=(ref.get("flow") or {}).get("explain"))
        business = business_context(job, folder)
        if business:
            proc["business"] = business
    else:
        business = business_context(job, folder)
        if business:
            proc = {"available": True, "reason": None, "business_only": True, "business": business, "rows": []}
    sections["process"] = proc

    # 5. Time by stage and application
    tm = gated("time")
    if tm["available"]:
        tm.update(stages=m["stages"], matrix=m["matrix"], applications=m["applications"], has_active=m["has"]["active"],
                  total_hours=t["active_hours"], total_events=t["events"])
    sections["time"] = tm

    # 6. Inside each step (Level 4), with the detailed per-step blocks (inside_steps.json)
    sections["inside"] = inside_section(mining, m, no_case)
    if sections["inside"].get("available") and (mining.get("inside") or {}).get("available"):
        sections["inside"]["detail"] = inside_blocks(folder, facts, mining, use_ai if inside_ai is None else inside_ai)

    # 7. Flow and conformance: only with a confirmed, saved or template order
    cf = gated("conformance")
    if cf["available"]:
        conf = m.get("conformance")
        if trusted and conf:
            cf.update(trusted=True, conf=conf, cases=t["cases"])
        else:
            cf.update(trusted=False, line="Step order not confirmed — enter it on page 4 to see conformance.")
    sections["conformance"] = cf

    # 8. Waiting between steps
    wt = gated("waiting")
    if wt["available"]:
        w = m["waiting"]
        minutes = float(rcfg["min_wait_minutes"])
        limit = f"{minutes / 60:g} hour" + ("s" if minutes != 60 else "") if minutes % 60 == 0 else f"{minutes:g} minutes"
        wt.update(rows=w["rows"], left_out_short=w["left_out_short"], left_out_low=w["left_out_low_support"],
                  min_cases=rcfg["min_cases"], limit=limit,
                  empty_line=f"No waiting longer than {limit} between steps (pairs with at least "
                             f"{rcfg['min_cases']} cases).")
    sections["waiting"] = wt

    # 9. Rework (after the gap) vs quick switching
    rw = gated("rework")
    if rw["available"]:
        rw.update(rows=[r for r in m["rework"]["rows"] if r["rework_runs"]], back=m["rework"]["back_and_forth"],
                  quick_runs=m["rework"]["quick_runs"], gap=m["rework"]["gap_minutes"],
                  quick_steps=sum(1 for r in m["rework"]["rows"] if r["quick_runs"]), has_active=m["has"]["active"])
    sections["rework"] = rw

    # 10. People and handovers (masked participants: never names)
    pp = gated("people")
    if not pp["available"] and not no_case and not m["people"]:
        pp["reason"] = pp["reason"] or "Not available: no user column was mapped."
    if pp["available"]:
        pp.update(people=m["people"][:int(rcfg["max_table_rows"])], total=len(m["people"]), handovers=m["handovers"],
                  total_handovers=m["total_handovers"], has_active=m["has"]["active"])
    sections["people"] = pp

    # 11. Applications and switching
    ap = gated("applications")
    sw_mining = (mining.get("app_switching") or {}).get("data")
    if ap["available"] and m["switching"].get("available"):
        sw = m["switching"]
        ap.update(sw=sw, by_step=sw["by_step"][:int(rcfg["top_n"])],
                  sentence=describe_switching(sw_mining) if sw_mining else "",
                  dot=to_app_dot(sw_mining["app_nodes"], sw_mining["app_edges"]) if sw_mining else None)
    elif ap["available"]:
        ap = {"available": False, "reason": "Not available: run the analysis on page 3 again to count application switches."}
    sections["applications"] = ap

    # 12. Trend per ISO week
    tr = gated("trend")
    if tr["available"]:
        full = [w for w in m["trend"] if not w["partial"]]
        line = ""
        if len(full) >= 2:
            busiest = max(full, key=lambda w: (w["cases_started"], w["week"]))
            quietest = min(full, key=lambda w: (w["cases_started"], w["week"]))
            line = (f"Among the {len(full)} full weeks, cases started range from {fmt.count(quietest['cases_started'])} "
                    f"({quietest['week']}) to {fmt.count(busiest['cases_started'])} ({busiest['week']}).")
        tr.update(weeks=m["trend"], line=line, has_active=m["has"]["active"])
    sections["trend"] = tr

    # 13. Variants (compact)
    va = gated("variants")
    if va["available"]:
        va.update(v=m["variants"], cases=t["cases"])
    sections["variants"] = va

    # 14. Appendix (checks are filled in below)
    sections["appendix"] = appendix_section(job_id, job, mapping, quality, mining, m, narrative, facts, folder)
    if sections["process"].get("business"):
        sections["appendix"]["definition"] = sections["process"]["business"]["appendix"]

    pm_facts = [f for f in facts if f.section == "pm4py"]
    if pm_facts and (mining.get("process_model") or {}).get("available"):
        sections["appendix"]["pm4py"] = pm4py_appendix(folder, pm_facts)
    checks = report_checks.run(folder, m, narrative.summary + narrative.findings + narrative.recommendations,
                               {f.id for f in facts}, ((mining.get("level4") or {}).get("data")),
                               pm_facts if sections["appendix"].get("pm4py") else None)
    sections["appendix"]["checks"] = checks
    title = rcfg["title"]
    if checks["failed"]:
        title = f"{report_checks.headline(checks)} · {title}"

    ordered = [{"key": k, "number": i, "title": tt, **sections[k]} for i, (k, tt) in enumerate(TITLES, 1)]
    return {
        "title": title, "job_id": job_id, "source": job.get("source_name"),
        "app_version": __version__,
        "case_label": case_label(mapping),
        "case_note": {"work_session": WORK_SESSION_NOTE, "time_window": TIME_WINDOW_NOTE}.get(case_kind(mapping), "")
                     + (" " + SCREEN_NOTE if mapping.get("derived_activity") else ""),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "header": {"period": _period(t), "case_column": mapping.get("case_id") or "–",
                   "activity_column": mapping.get("activity") or "–",
                   "order_badge": (m["order"]["badge"] if trusted else
                                   f"{m['order']['badge']} — not confirmed; steps sorted by cases reached"),
                   "checks": report_checks.headline(checks), "draft": bool(checks["failed"]),
                   "failed": [c for c in checks["checks"] if not c["passed"]]},
        "band": mining["band"], "band_label": mining.get("band_label") or "", "score": quality["feasibility"],
        "collapse_appendix": bool(rcfg.get("collapse_appendix", True)),
        **viz_js(),
        "sections": ordered,
        # Raw results for the Excel file (numbers as numbers).
        "raw": {"facts": [asdict(f) for f in facts], "quality": quality, "mining": mining, "metrics": m,
                "mapping": mapping, "job": job, "reference": ref, "checks": checks},
    }


def pm4py_appendix(folder: Path, pm_facts: list[Fact]) -> dict:
    """Appendix "Formal model (PM4Py)": only when a model was built for this job. Every number is a fact."""
    from app.core import pm4py_model
    conf = pm4py_model.load_json(folder, pm4py_model.CONFORMANCE_FILE) or {}
    chk = pm4py_model.load_json(folder, pm4py_model.DEFINITION_FILE) or {}
    row = lambda f: {"id": f.id, "label": f.label, "value": format_fact(f), "support": f.support}  # noqa: E731
    model = [row(f) for f in pm_facts if not f.type.startswith(("pm4py_def", "pm4py_order"))]
    order = [row(f) for f in pm_facts if f.type.startswith("pm4py_order")]
    oc = pm4py_model.load_json(folder, pm4py_model.ORDER_FILE) or {}
    approved = [row(f) for f in pm_facts if f.type.startswith("pm4py_def")]
    dev = conf.get("deviations") or {}
    return {"model": model, "approved": approved, "order": order, "order_badge": oc.get("order_badge"),
            "show_model": conf.get("show_model", True), "no_order": conf.get("structure_note"),
            "order_notes": oc.get("notes") if order else [], "cases_used": conf.get("cases_used"),
            "cases_total": conf.get("cases_total"), "sampled_cases": dev.get("sampled_cases"),
            "method": conf.get("method"), "noise_threshold": conf.get("noise_threshold"),
            "version": conf.get("pm4py_version"), "licence": conf.get("licence") or pm4py_model.LICENCE_NOTE,
            "approved_name": chk.get("definition") if approved else None, "approved_version": chk.get("version"),
            "approved_notes": chk.get("notes") if approved else [], "caveat": chk.get("caveat") if approved else None,
            "not_in_definition": chk.get("not_in_definition") if approved else []}


def _period(t: dict) -> str:
    if not t.get("first_event"):
        return "–"
    return f"{t['first_event'][:16].replace('T', ' ')} – {t['last_event'][:16].replace('T', ' ')} UTC"


def _other_line(others: list[dict], has_active: bool) -> str:
    if not others:
        return ""
    runs = sum(r["runs"] for r in others)
    text = f"Other activities without a stage: {', '.join(r['step'] for r in others)} — {fmt.count(runs)} runs"
    if has_active:
        text += f", {fmt.hours(sum(r['active_hours'] or 0 for r in others))}"
    return text + "."


def scope_section(sc: dict | None, job: dict) -> dict:
    """Rows and hours from the file to the analysis; the lines add up to the rows in the file."""
    if not sc:
        return {"available": False, "reason": "Not available: convert the file again on Import data to count where the "
                                              "rows go (scope.json)."}
    active = sc.get("has_active_time")
    h = lambda part: (sc[part]["active_seconds"] or 0) / 3600 if active else None  # noqa: E731
    f = sc.get("filter")
    lines = [("Rows in file", sc["rows_in_file"], (sc["active_seconds_in_file"] or 0) / 3600 if active else None, True)]
    labels = {"left_out_by_filter": (f"Left out by the row filter ({f['column']} in {', '.join(f['values'])})"
                                     if f else "Left out by the row filter (no filter)"),
              "without_case": "Without a case ID", "without_activity": "Without an activity",
              "without_time": "Without a usable time"}
    for part, label in labels.items():
        if part == "without_time" and not sc[part]["rows"]:
            continue
        lines.append((label, sc[part]["rows"], h(part), False))
    lines.append(("In scope (analysed)", sc["in_scope"]["rows"], h("in_scope"), True))
    rows = [{"label": a, "rows": b, "hours": c, "strong": d} for a, b, c, d in lines]
    missing = None
    if active:
        out = {k: h(k) for k in labels}
        missing = {"total": sum(out.values()), "parts": [(labels[k], v) for k, v in out.items() if v]}
    return {"available": True, "reason": None, "rows": rows, "has_active": active, "missing": missing}


def inside_blocks(folder: Path, facts: list[Fact], mining: dict, use_ai: bool) -> dict:
    """Per step (report order): patterns, time split, rework, apps, people flags, PM4Py and one short paragraph
    (the AI through the narrative checks with {{F}} placeholders only, else the template)."""
    from app.core import inside_step, privacy
    from app.core.narrative import ai_status, user_names
    data = inside_step.load(folder)
    if not data or not data.get("available"):
        return {}
    ok, why = ai_status() if use_ai else (False, "switched off for this run")
    if ok and not privacy.ai_allowed()[0]:
        ok, why = False, privacy.ai_allowed()[1]
    ai = {"ok": bool(ok), "reason": None if ok else f"AI not used: {why}"}
    local = [f for f in facts if f.section == "inside"]
    out = {}
    for l3, d in data["steps"].items():
        if not d.get("enough"):
            out[l3] = {"enough": False, "reason": d.get("reason")}
            continue
        para = inside_step.paragraph(d, local, ai, user_names(folder.name))
        pm = d.get("pm4py") or {}
        out[l3] = {"enough": True, "runs": d["runs"], "active_h": d["active_h"], "median_run_s": d["median_run_s"],
                   "text": para["text"], "provenance": provenance_text(para),
                   "patterns": d["patterns"][:5], "time_split": d["time_split"][:8], "rework": d["rework"],
                   "apps": d["apps"], "people_flagged": [p for p in d["people"] if p["flags"]][:5],
                   "variation_flag": d["variation_flag"], "automation": d["automation_candidates"],
                   "cv": d.get("cv_between_people"), "cv_label": d.get("cv_label"),
                   "pm": pm if pm.get("available") else None, "status": d["status"]}
    return out


def provenance_text(s: dict) -> str:
    from app.core.level4 import provenance
    return provenance(s)


def inside_section(mining: dict, m: dict, no_case: bool) -> dict:
    """Per step (in the report's order): its summary and its sub-steps with their share of the step's hours.
    A step whose taxonomy fell back to rules shows its top 5 screens and one line why."""
    from app.core import level4
    sec = mining.get("level4") or {}
    if no_case:
        return {"available": False, "reason": "Not available: no case"}
    if not sec.get("available"):
        return {"available": False, "reason": sec.get("reason") or "Not available: map an Action / UI event column "
                                                                    "on Import data to see the sub-steps of each step."}
    details = {d["l3"]: d for d in sec["data"]["l3s"]}
    banner = level4_banner(list(details.values()))
    order = [r["step"] for r in m["steps"] if r["step"] in details] + sorted(set(details) - {r["step"] for r in m["steps"]})
    blocks = []
    for l3 in order:
        d = details[l3]
        hours_based = any(n.get("active_share") is not None for n in d["nodes"])
        nodes = [n for n in d["nodes"] if n["name"] != level4.OTHER]
        ai_like = True              # rule-based labels read like business labels now; one banner says how many
        rows = [{"name": n["name"], "description": n.get("description", "") if ai_like else "",
                 "share": n.get("active_share") if hours_based else n["share"],
                 "active": fmt.duration_s(n.get("median_active_s"))} for n in nodes]
        blocks.append({"l3": l3, "summary": (d.get("summary") or {}).get("text", ""), "rows": rows,
                       "hours_based": hours_based, "fallback": None,
                       "coverage": d["coverage"], "source": level4.source_label(d), "confidence": d["confidence"]})
    return {"available": True, "reason": None, "blocks": blocks, "banner": banner}


def level4_banner(details: list[dict]) -> str | None:
    """One line for all steps: "Level 4 built without AI for N of M steps (reason). Labels are rule-based." """
    rules = [d for d in details if d.get("source") == "rules" and not d.get("confirmed")]
    if not rules:
        return None
    reasons = sorted({str(d.get("reason") or "AI not used").split(":")[0] for d in rules})
    return (f"Level 4 built without AI for {len(rules)} of {len(details)} steps ({'; '.join(reasons)[:160]}). "
            "Labels are rule-based.")


def appendix_section(job_id: str, job: dict, mapping: dict, quality: dict, mining: dict, m: dict, narrative,
                     facts: list[Fact], folder: Path) -> dict:
    scope = mapping.get("scope") or {}
    summary = job.get("summary") or {}
    q = settings()["quality"]["checks"]
    mcfg = settings()["mining"]
    rcfg = settings()["report"]
    l4 = (mining.get("level4") or {}).get("data") or {}
    ai_rows = [("Executive summary", "AI" if narrative.source == "ai" else f"fixed templates ({narrative.reason})")]
    for d in l4.get("l3s") or []:
        from app.core.level4 import provenance
        ai_rows.append((f"Level 4 summary: {d['l3']}", provenance(d.get("summary")) or "–"))
    ms = (mining.get("milestones") or {}).get("data")
    milestone = {}
    if ms:
        from app.core.milestones import sequence_lines, to_dot as milestone_dot
        milestone = {"milestone_lines": sequence_lines(ms), "milestone_dot": milestone_dot(ms),
                     "milestone_coverage": ms["coverage"]}
    return {
        "available": True, "reason": None,
        "source": job.get("source_name"), "format": job.get("format"), "job_id": job_id,
        "quality_checks": [{"title": c["title"], "status": c["status"], "shown": c["shown"]} for c in quality["checks"]],
        "quality_score": quality["feasibility"], "quality_band": quality["band"],
        "fixes": quality.get("fixes", [])[:5],
        "mapping": [{"role": ROLE_LABELS[r], "column": mapping.get(r) or "— none —"} for r in ROLE_LABELS],
        "settings": [
            ("Application version", __version__),
            ("Case definition", case_label(mapping)),
            ("Activity definition", "derived from application + window title (screens)"
             if mapping.get("derived_activity") else f"column {mapping.get('activity')}"),
            ("Timestamp format", mapping.get("timestamp_format") or "–"),
            ("Timezone", f"{mapping.get('timezone') or 'UTC'} (all times shown in UTC)"),
            ("Leave out rows without a case ID", "yes" if scope.get("drop_missing_case") else "no"),
            ("Leave out rows without an activity", "yes" if scope.get("drop_missing_activity") else "no"),
            ("Row filter", f"{scope['filter_column']} in {', '.join(map(str, scope.get('filter_values') or []))}"
             if scope.get("filter_column") else "none"),
            ("Rows in file", fmt.count(summary.get("rows_in_file", 0))),
            ("Rows left out", fmt.count(summary.get("rows_left_out", 0))),
            ("Columns left out", ", ".join(mapping.get("exclude_columns") or []) or "none"),
            ("Merge repeated steps", "yes" if mapping.get("merge_repeats") else "no"),
            ("Activity names merged", "; ".join(f"{a} → {b}" for a, b in sorted((mapping.get("activity_renames") or {})
                                                                            .items())) or "none"),
            ("Step order", m["order"]["badge"]),
        ],
        "thresholds": [(k.replace("_", " ").capitalize(), ", ".join(f"{a} {b}" for a, b in v.items()))
                       for k, v in q.items()]
        + [("Low support below", f"{mcfg['min_support_cases']} cases"),
           ("Facts hidden below", f"{settings()['facts']['hide_below_support']} cases"),
           ("Waiting pairs need", f"{rcfg['min_cases']} cases and a median of {rcfg['min_wait_minutes']} minutes"),
           ("Rework gap", f"{rcfg['rework_gap_minutes']} minutes"),
           ("High badge", ", ".join(f"{a} {b}" for a, b in settings()["facts"]["badges"]["high"].items())),
           ("Medium badge", ", ".join(f"{a} {b}" for a, b in settings()["facts"]["badges"]["medium"].items()))],
        "definitions": DEFINITIONS,
        # Only resolved names (real names or "Unnamed app N"): raw hashes never reach the report.
        "applications": ((mining.get("applications") or {}).get("data") or {}).get("applications", []),
        "unnamed_apps": ((mining.get("applications") or {}).get("data") or {}).get("unnamed_masked", 0),
        "ai_provenance": ai_rows,
        "narrative": [
            ("Summary written by", "AI" if narrative.source == "ai" else "fixed templates"),
            ("Why not AI", narrative.reason or "–"),
            ("AI attempts", str(narrative.attempts)),
            ("Facts sent to the AI", str(narrative.facts_sent)),
            ("Prompt version", narrative.prompt_version or "–"),
            ("AI provider and model", f"{narrative.provider} · {narrative.model}" if narrative.provider else "–"),
            *level4_appendix(mining),
            ("Checks failed along the way", str(sum(1 for e in narrative.errors if not e.startswith("fallback:")))),
            ("Provider fallbacks", str(sum(1 for e in narrative.errors if e.startswith("fallback:")))),
            ("Langfuse trace", narrative.trace_id or "–"),
            ("Saved as", "narrative.json (with every error and attempt)"),
        ],
        "facts": [{"id": f.id, "label": f.label, "value": format_fact(f), "support": f.support,
                   "ci": (f"{fmt.duration(f.ci_low)}–{fmt.duration(f.ci_high)}" if f.ci_low is not None else ""),
                   "badge": f.badge, "source": f.source} for f in facts],
        **milestone,
        "order_evidence": _load(folder / "order_evidence.json"),
        "level4_quality": _quality_rows(_load(folder / "level4_quality.json")),
    }


def _quality_rows(q: dict | None) -> list[dict]:
    return [{"step": l3, "named": v["named_events_share"], "other": v["other_hours_share"], "overlap": v["overlap_units"],
             "stability": v["stability"], "flags": "; ".join(f["measure"] for f in v["flags"]) or "–",
             "sub_steps": len([k for k in v["sub_steps"] if k != "Other"])} for l3, v in (q or {}).items()]


def level4_appendix(mining: dict) -> list[tuple[str, str]]:
    sec = mining.get("level4") or {}
    if not sec.get("available"):
        return []
    from app.core.level4 import source_label
    return [(f"Level 4 taxonomy: {d['l3']}", f"{source_label(d)}; prompt version {d.get('prompt_version') or '–'}")
            for d in sec["data"]["l3s"]]


def _level4_sheet(wb: Workbook, data: dict, sub: dict) -> None:
    """Sheets "Level 4" (one row per sub-step: hierarchy, description, rules, members, numbers) and "Summaries"
    (process, stage and step texts with their source and model)."""
    stage_of = {l3: st["stage"] for st in data.get("stages") or [] for l3 in st["l3s"]}
    tree = ((sub.get("data") or {}).get("hierarchy") or [])
    level1 = next((n["name"] for n in tree if n["level"] == "Process level 1"), "")
    rows = []
    for d in data["l3s"]:
        subs = {s["label"]: s for s in d.get("substeps") or []}
        for n in d["nodes"]:
            s = subs.get(n["name"], {})
            rules = "; ".join(f"{r.get('application') or '*'} | screen: {r.get('screen_regex') or '-'} | "
                              f"click: {r.get('click_regex') or '-'}" for r in s.get("rules") or [])
            rows.append([level1, stage_of.get(d["l3"], ""), d["l3"], n["name"], n.get("description", ""), rules,
                         ", ".join(s.get("members_shown") or []), n["events"], n["runs"], n["share"],
                         None if n.get("median_active_s") is None else round(n["median_active_s"] / 3600, 4),
                         n["repeat_share"], d.get("source"), "yes" if d.get("confirmed") else "no"])
    _sheet(wb, "Level 4", ["Level 1", "Stage", "Level 3", "Level 4", "Description", "Rules", "Assigned clicks",
                           "Events", "Runs", "Share of the step's work", "Median active time (h)", "Repeat share",
                           "Source", "Confirmed"], rows, {10: "0.0%", 12: "0.0%"})
    texts = [["Process", "", (data.get("overview") or {}).get("text", ""), (data.get("overview") or {}).get("source"),
              (data.get("overview") or {}).get("model")]]
    for st in data.get("stages") or []:
        s = st.get("summary") or {}
        texts.append(["Stage", st["stage"], s.get("text", ""), s.get("source"), s.get("model")])
    for d in data["l3s"]:
        s = d.get("summary") or {}
        texts.append(["Step", d["l3"], s.get("text", ""), s.get("source"), s.get("model")])
    _sheet(wb, "Summaries", ["Level", "Name", "Summary", "Written by", "Model"], texts)


def business_context(job: dict, folder: Path) -> dict | None:
    """The business process map block of section 4 and the definition appendix, or None when
    no definition was checked against this job (job.json -> definition, evidence.json)."""
    from app.core import definition as dmod
    from app.core.business_map import EVIDENCE_LEGEND, LEGEND as BLEGEND, title, to_business_dot
    from app.core.definition_ai import describe_op, load_suggestions
    from app.core.evidence import load_evidence, score_line, top_contradicted
    defn = dmod.for_job(job)
    evidence = load_evidence(folder, defn) if defn is not None else None
    if defn is None or evidence is None:
        return None
    dot = to_business_dot(defn, evidence["claims"], evidence.get("stats"), True,
                          step_stats=evidence.get("step_stats"))
    sugg = load_suggestions(folder)
    return {
        "approved": defn.status == "approved", "title": title(defn, evidence.get("stats")), "dot": dot,
        "legend": f"{BLEGEND} · {EVIDENCE_LEGEND}", "score_line": "Evidence: " + score_line(evidence),
        "top_contradicted": [{"id": c["id"], "text": c["text"], "statement": c["statement"]}
                             for c in top_contradicted(evidence, 5)],
        "appendix": {
            "name": defn.name, "version": defn.version, "status": defn.status,
            "drafted_from_template": str(defn.source or "").startswith("Template:"),
            "approved_by": defn.approved_by, "approved_at": defn.approved_at,
            "steps": [{"position": s["position"], "name": s["name"], "stage": defn.stage_name(s["stage_id"]),
                       "owner": s.get("owner_role") or "", "system": s.get("system") or "",
                       "mandatory": "yes" if s.get("mandatory", True) else "no",
                       "sla": "" if s.get("sla_hours") is None else f"{s['sla_hours']:g} h",
                       "rules": s.get("business_rules") or "",
                       "activities": ", ".join(s.get("mapped_activities") or [])} for s in defn.ordered_steps()],
            "claims": [{"id": c["id"], "statement": c["statement"], "verdict": c["verdict"], "text": c["text"],
                        "support": c.get("support") or 0, "badge": c.get("badge")} for c in evidence["claims"]],
            "score_line": score_line(evidence),
            "change_log": list(defn.change_log),
            "suggestions": [{"what": describe_op(x, defn), "reason": x["reason"], "status": x.get("status", "open"),
                             "claims": ", ".join(x.get("claims") or [])}
                            for x in (sugg.suggestions if sugg else [])],
            **knowledge_appendix(defn, folder),
        },
        **combined_block(defn, evidence, folder),
    }


def _short(quote: str) -> str:
    """A source quote as shown in the report: at most knowledge.max_quote_words words."""
    words = str(quote or "").split()
    n = int(settings()["knowledge"]["max_quote_words"])
    return " ".join(words[:n]) + (" …" if len(words) > n else "")


def combined_block(defn, evidence: dict, folder: Path) -> dict:
    """Section 4 additions when knowledge sources exist: the map coloured by combined status,
    the overall confidence with its breakdown, and the two key-finding lists."""
    from app.core import combined_confidence as cc
    from app.core.business_map import to_business_dot
    combined = cc.load(folder)
    if not combined or not combined.get("elements"):
        return {}
    return {
        "combined_line": cc.breakdown_line(combined["summary"]),
        "dot": to_business_dot(defn, evidence["claims"], evidence.get("stats"), True,
                               step_stats=evidence.get("step_stats"), combined=combined),
        "documented_not_actual": [{"label": e["label"], "data": e["tooltip"].split(" · Data: ")[-1]}
                                  for e in cc.findings(combined, cc.DOCUMENTED_NOT_ACTUAL)],
        "undocumented": [{"label": e["label"], "data": e["tooltip"].split(" · Data: ")[-1]}
                         for e in cc.findings(combined, cc.UNDOCUMENTED)],
        "combined_elements": combined["elements"],
    }


def knowledge_appendix(defn, folder: Path) -> dict:
    """Appendix: sources (titles, kinds, dates, reliability, statements used), conflicts and their
    resolutions, expert questions and answers, pain points. Quotes at most 25 words."""
    from app.core import knowledge
    sources = knowledge.list_sources(defn.name)
    if not sources:
        return {}
    statements = knowledge.all_statements(defn.name)
    merged = knowledge.merge(statements)
    qa = []
    for src in sources:
        if src["kind"] == "sme_answer":
            for q, a in re.findall(r"Question: (.*?)\nAnswer: (.*?)(?:\n\n|\Z)", knowledge.source_text(defn.name, src["id"]),
                                   re.S):
                qa.append({"question": _short(q), "answer": _short(a), "source": src["id"]})
    return {
        "sources": [{"id": s["id"], "title": s["title"], "kind": knowledge.KIND_LABELS[s["kind"]],
                     "date": s.get("recorded_on") or s["added_at"][:10], "reliability": s["effective_reliability"],
                     "stale": s["stale"], "status": s["status"],
                     "statements": sum(1 for x in statements if x["source_id"] == s["id"])} for s in sources],
        "conflicts": [{"id": c["id"], "sides": " / ".join(f"{x['label']} ({', '.join(sorted({y['source_id'] for y in x['sources']}))})"
                                                          for x in c["sides"]),
                       "status": c["status"], "resolution": knowledge.describe_key(c["resolution"])
                       if c.get("resolution") else "–"} for c in merged["conflicts"]],
        "qa": qa,
        "statements": [{"source": x["source_id"], "anchor": x["anchor"], "type": x["type"], "subject": x["subject"],
                        "object": x.get("object") or "", "value": x.get("value") or "", "quote": _short(x["quote"]),
                        "reliability": x["reliability"]} for x in statements],
        "pain_points": [{**p, "quote": _short(p["quote"]), "supported": supported_by_data(p["step"], defn, folder)}
                        for p in knowledge.pain_points(statements)],
    }


def supported_by_data(step: str, defn, folder: Path) -> bool:
    """A pain point is "supported by data" when the step has one of the slowest waits
    (bottlenecks) or the most application switching (Prompts 3 / 6)."""
    s = next((x for x in defn.steps if x["name"] == step), None)
    acts = set((s or {}).get("mapped_activities") or [step])
    m = _load(folder / "mining.json") or {}
    waits = ((m.get("bottlenecks") or {}).get("data") or {}).get("transitions") or []
    switching = ((m.get("app_switching") or {}).get("data") or {}).get("by_step") or []
    return any(t["dst"] in acts for t in waits[:5]) or any(x["activity"] in acts for x in switching[:5])


# --------------------------------------------------------------------------- #
# 2. HTML
# --------------------------------------------------------------------------- #
def viz_js() -> dict:
    """How report maps are drawn, from `report.viz_js_mode` (see config/settings.yaml).

    inline  the bundled Viz.js is embedded in the HTML: the report makes no network
            request. The file's SHA-256 is checked first; if it is missing or changed,
            maps fall back to "off" with a warning (never silently to the CDN).
    cdn     the HTML loads Viz.js from `report.viz_js_url`.
    off     no drawing; the tables under each map still show everything.
    Returns the template values: viz_mode, viz_inline (script text), viz_js_url.
    """
    cfg = settings()["report"]
    mode = str(cfg.get("viz_js_mode") or "inline").lower()
    if mode == "cdn" and cfg.get("viz_js_url"):
        return {"viz_mode": "cdn", "viz_inline": "", "viz_js_url": cfg["viz_js_url"]}
    if mode == "inline":
        path = PROJECT_ROOT / cfg.get("viz_js_file", "")
        try:
            data = path.read_bytes()
        except OSError:
            log.warning("Viz.js file %s is missing: report maps are switched off (tables only).", path)
            return {"viz_mode": "off", "viz_inline": "", "viz_js_url": ""}
        if hashlib.sha256(data).hexdigest() != str(cfg.get("viz_js_sha256", "")).lower():
            log.warning("Viz.js file %s does not match report.viz_js_sha256: report maps are switched off "
                        "(tables only).", path)
            return {"viz_mode": "off", "viz_inline": "", "viz_js_url": ""}
        # "</script" inside the code would end the <script> element early; the bundled file has none.
        return {"viz_mode": "inline", "viz_inline": data.decode("utf-8").replace("</script", "<\\/script"),
                "viz_js_url": ""}
    return {"viz_mode": "off", "viz_inline": "", "viz_js_url": ""}


def map_html(dot: str, title: str, notes: list[str] | None = None) -> str:
    """One self-contained HTML page with a map, drawn by Viz.js in `report.viz_js_mode`.

    Inline (default): the page makes no network request and opens offline. The DOT
    text is always included (and shown when drawing is off), and the page prints to
    PDF from any browser.
    """
    viz = viz_js()
    dot_js = json.dumps(dot).replace("</", "<\\/")          # safe inside a <script> element
    library = (f"<script>{viz['viz_inline']}</script>" if viz["viz_mode"] == "inline" else
               f'<script src="{html.escape(viz["viz_js_url"])}"></script>' if viz["viz_mode"] == "cdn" else "")
    draw = ("" if viz["viz_mode"] == "off" else
            "<script>(function(){if(!window.Viz||!Viz.instance)return;Viz.instance().then(function(v){"
            "var el=document.getElementById('map');el.innerHTML='';el.appendChild(v.renderSVGElement(DOT));"
            "}).catch(function(){});})();</script>")
    note_html = "".join(f"<li>{html.escape(n)}</li>" for n in notes or [])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #1f2933;
         margin: 24px; }}
  h1 {{ font-size: 20px; margin: 0 0 12px; }}
  #map svg {{ max-width: 100%; height: auto; }}
  pre {{ background: #f5f7fa; padding: 12px; font-size: 11px; white-space: pre-wrap; }}
  .caption {{ color: #616e7c; font-size: 13px; }}
  @media print {{ @page {{ size: A4 landscape; margin: 12mm; }} body {{ margin: 0; }} .no-print {{ display: none; }} }}
</style></head><body>
<h1>{html.escape(title)}</h1>
<div id="map"><pre>{html.escape(dot)}</pre></div>
<ul class="caption">{note_html}</ul>
<p class="caption no-print">For a PDF: use your browser's Print → Save as PDF.</p>
<script>var DOT = {dot_js};</script>
{library}
{draw}
</body></html>
"""


def render_html(context: dict) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html", "j2"]))
    env.globals.update(fd=fmt.duration, fds=fmt.duration_s, fh=fmt.hours, fp=fmt.pct, fc=fmt.count)
    return env.get_template("report.html.j2").render(**context)


# --------------------------------------------------------------------------- #
# 3. Excel
# --------------------------------------------------------------------------- #
def _sheet(wb: Workbook, title: str, header: list[str], rows: list[list], formats: dict | None = None,
           note: str | None = None) -> None:
    """One table per sheet: bold frozen header, numbers as numbers, readable widths."""
    ws = wb.create_sheet(title)
    if note is not None:
        header, rows, formats = ["Note"], [[note]], None
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    for r in rows:
        ws.append(r)
    for col, fmt in (formats or {}).items():
        for (cell,) in ws.iter_rows(min_row=2, min_col=col, max_col=col):
            cell.number_format = fmt
    for i, _ in enumerate(header, 1):
        longest = max((len(str(c.value)) for (c,) in ws.iter_rows(min_col=i, max_col=i) if c.value is not None),
                      default=8)
        ws.column_dimensions[get_column_letter(i)].width = min(max(10, longest + 2), 80)


def _app_switch_sheet(wb: Workbook, sw: dict) -> None:
    """Sheet "App switches": three tables one below the other (pairs, by step, steps × apps)."""
    ws = wb.create_sheet("App switches")
    apps = [n["application"] for n in sw["app_nodes"]]
    blocks = [
        ("Switch pairs", ["From", "To", "Switches", "Cases", "Median gap (h)"],
         [[p["from"], p["to"], p["count"], p["cases"], p["median_gap_h"]] for p in sw["pairs"]], {}),
        ("Switching by step", ["Step", "Switches into the step", "Times the step is done",
                               "Switches per occurrence", "Cases", "Top apps", "Low support"],
         [[s["activity"], s["switches"], s["occurrences"], s["per_occurrence"], s["cases"],
           ", ".join(s["top_apps"]), s["low_support"]] for s in sw["by_step"]], {}),
        ("Steps × applications (share of the step's events)", ["Step", "Apps used", *apps],
         [[r["activity"], r["apps_used"], *[r["shares"].get(a, 0.0) for a in apps]] for r in sw["steps_by_app"]],
         {i: "0.0%" for i in range(3, 3 + len(apps))}),
    ]
    widths: dict[int, int] = {}
    for title, header, rows, formats in blocks:
        ws.append([title])
        ws.cell(ws.max_row, 1).font = Font(bold=True, size=12)
        ws.append(header)
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
        for r in rows:
            ws.append(r)
            for col, fmt in formats.items():
                ws.cell(ws.max_row, col).number_format = fmt
        ws.append([])
        for i, h in enumerate(header, 1):
            widths[i] = max(widths.get(i, 10), min(len(str(h)) + 2, 40),
                            *(min(len(str(r[i - 1])) + 2, 60) for r in rows if i - 1 < len(r)))
    ws.freeze_panes = "A3"
    for i, w in widths.items():
        ws.column_dimensions[get_column_letter(i)].width = w


def write_excel(context: dict, path: Path) -> Path:
    """The report's tables, numbers stored as numbers (hours as hours, shares as 0-1 with a % format)."""
    raw = context["raw"]
    m, mn, q = raw["metrics"], raw["mining"], raw["quality"]
    sec = {s["key"]: s for s in context["sections"]}
    wb = Workbook()
    wb.remove(wb.active)
    pct, hrs = "0.0%", "#,##0.000"
    t = m["totals"]
    hdr = context["header"]
    _sheet(wb, "Summary", ["Item", "Value"],
           [["Report", context["title"]], ["Source", context["source"]], ["Job", context["job_id"]],
            ["Generated (UTC)", context["generated_at"]], ["Period", hdr["period"]],
            ["Case column", hdr["case_column"]], ["Activity column", hdr["activity_column"]],
            ["Step order", hdr["order_badge"]], ["Calculation checks", hdr["checks"]],
            ["Feasibility score", context["score"]], ["Quality band", context["band"]]]
           + [["Summary source", sec["summary"]["provenance"]]]
           + [[f"Summary {i}", x["text"]] for i, x in enumerate(sec["summary"]["summary"], 1)]
           + [[f"Finding {i}", x["text"]] for i, x in enumerate(sec["summary"]["findings"], 1)]
           + [[f"Recommendation {i}", x["text"]] for i, x in enumerate(sec["summary"]["recommendations"], 1)])
    _sheet(wb, "Key numbers", ["Measure", "Value", "Unit"],
           [["Cases", t["cases"], "cases"], ["Step runs", t["runs"], "runs"],
            ["Events before merging", t["events"], "events"], ["Participants", t["participants"], "users"],
            ["Applications", t["applications"], "applications"], ["Activities", t["activities"], "activities"],
            ["Active hours in scope", t["active_hours"], "hours"], ["Median case duration", t["median_case_h"], "hours"],
            ["90% of cases finish within", t["p90_case_h"], "hours"], ["Mean case duration", t["mean_case_h"], "hours"]])
    sc = sec["scope"]
    _sheet(wb, "Data scope", ["Line", "Rows", "Active hours"],
           [[r["label"], r["rows"], r["hours"]] for r in sc.get("rows") or []], {3: hrs},
           None if sc["available"] else sc["reason"])
    steps = m["steps"]
    _sheet(wb, "Steps", ["Step", "Stage", "Cases reached", "Share of all cases", "Runs", "Events", "Active hours",
                         "Share of active hours", "Hours per case reached", "Median active time per run (s)",
                         "Top application", "Top application hours", "Cases with rework", "Share of cases reaching (rework)",
                         "Waiting hours", "Waiting share (wait ÷ TAT)"],
           [[r["step"], r["stage"], r["cases"], r["cases_share"], r["runs"], r["events"], r["active_hours"],
             r["hours_share"], r["hours_per_case"], r["median_active_run_s"],
             (r["top_apps"][0]["application"] if r["top_apps"] else None),
             (r["top_apps"][0]["hours"] if r["top_apps"] else None), r["rework_cases"], r["rework_share"],
             r["waiting_hours"], r["waiting_share"]] for r in steps],
           {4: pct, 7: hrs, 8: pct, 9: hrs, 12: hrs, 14: pct, 15: hrs, 16: pct})
    _sheet(wb, "Stage hours", ["Stage", "Steps", "Cases", "Runs", "Active hours", "Share of active hours"],
           [[s["stage"] or "Other activities without a stage", ", ".join(s["steps"]), s["cases"], s["runs"],
             s["active_hours"], s["hours_share"]] for s in m["stages"]], {5: hrs, 6: pct})
    mx = m["matrix"]
    unit = "active hours" if mx["measure"] == "active_hours" else "events"
    _sheet(wb, "Step x application", [f"Step ({unit})", *mx["apps"], "Total"],
           [[r["step"], *r["values"], r["total"]] for r in mx["rows"]] + [["Total", *mx["column_totals"], mx["grand_total"]]],
           {i: hrs for i in range(2, len(mx["apps"]) + 3)} if mx["measure"] == "active_hours" else None)
    _sheet(wb, "Applications", ["Application", "Category", "Events", "Cases", "Share of events", "Active hours",
                                "Share of active hours"],
           [[a["application"], _category(a["application"]), a["events"], a["cases"],
             a["events"] / t["events"] if t["events"] else None, a["active_hours"], a["hours_share"]]
            for a in m["applications"]], {5: pct, 6: hrs, 7: pct})
    if (mn.get("level4") or {}).get("available"):
        _level4_sheet(wb, mn["level4"]["data"], mn.get("subprocess") or {})
    conf = m.get("conformance")
    if conf and m["order"]["trusted"]:
        _sheet(wb, "Conformance", ["Measure", "Value"],
               [["Cases in the intended order", conf["in_order_share"]], ["Step pairs in the intended order",
                                                                          conf["pair_order_share"]],
                [f"Cases reaching {conf['last_step']}", conf["reach_last_share"]]]
               + [[f"{e['step']} done before {e['before']} (cases)", e["cases"]] for e in conf["earlier"]]
               + [[f"Cases reaching {r['step']}", r["cases"]] for r in conf["reached"]])
    _sheet(wb, "Waiting", ["From", "To", "Transitions", "Cases", "Median wait (h)", "90% wait (h)", "Total waiting (h)",
                           "Shown in report"],
           [[x["from"], x["to"], x["count"], x["cases"], x["median_wait_h"], x["p90_wait_h"], x["total_wait_h"],
             x in m["waiting"]["rows"]] for x in m["transitions"]], {5: hrs, 6: hrs, 7: hrs})
    _sheet(wb, "Rework", ["Step", "Cases reaching", "Cases with rework", "Share of cases reaching", "Rework runs",
                          "Rework hours", "Cases with quick switching", "Quick switches"],
           [[r["step"], r["cases"], r["rework_cases"], r["rework_share"], r["rework_runs"], r["rework_hours"],
             r["quick_cases"], r["quick_runs"]] for r in m["rework"]["rows"]], {4: pct, 6: hrs})
    _sheet(wb, "People", ["Participant (masked)", "Cases", "Runs worked in", "Events", "Steps worked", "Active hours"],
           [[p["participant"], p["cases"], p["runs"], p.get("events"), p["steps"], p["active_hours"]]
            for p in m["people"]], {6: hrs})
    sw = m["switching"]
    if sw.get("available"):
        _sheet(wb, "Switching", ["From", "To", "Switches", "Cases"],
               [[p["from"], p["to"], p["count"], p["cases"]] for p in sw["pairs"]])
    if (mn.get("app_switching") or {}).get("available"):   # only when switches were counted
        _app_switch_sheet(wb, mn["app_switching"]["data"])
    _sheet(wb, "Trend", ["ISO week", "Week start", "Cases started", "Active hours", "Median case duration (h)",
                         "Partial week"],
           [[w["week"], w["week_start"], w["cases_started"], w["active_hours"], w["median_case_h"], w["partial"]]
            for w in m["trend"]], {4: hrs, 5: hrs})
    v = m["variants"]
    _sheet(wb, "Variants", ["Path", "Cases", "Share of cases", "Median case duration (h)"],
           [[" → ".join(x["path"]), x["cases"], x["share"], x["median_case_h"]] for x in v["top"]]
           + [["All variants", v["count"], None, None], ["Top 10 paths", v["top10_cases"], v["top10_share"], None]],
           {3: pct, 4: hrs})
    ms = (mn.get("milestones") or {}).get("data")
    if ms:
        rows = [["Catalogue", r["step"], r["label"], r["events"], r["cases"], r["is_milestone"]] for r in ms["catalogue"]]
        rows += [["Chain", " → ".join(c["steps"]), " → ".join(c["labels"]), None, c["cases"], None] for c in ms["chains"]]
        rows += [["Completion", c["step"], c["label"], c["occurrences"], None, c["share"]] for c in ms["completion"]]
        _sheet(wb, "Milestones", ["Kind", "Step", "Label / chain", "Events / occurrences", "Cases",
                                  "Selected / completion share"], rows)
    _sheet(wb, "Data quality", ["Check", "Status", "Result", "Score"],
           [[c["title"], c["status"], c["shown"], c["score"]] for c in q["checks"]])
    _sheet(wb, "Facts", ["ID", "Section", "Label", "Value", "Unit", "Support (cases)", "CI low", "CI high",
                         "Stability", "Badge", "Source"],
           [[f["id"], f["section"], f["label"], f["value"], f["unit"], f["support"], f["ci_low"], f["ci_high"],
             f["stability"], f["badge"], f["source"]] for f in raw["facts"]])
    _sheet(wb, "Checks", ["#", "Check", "Report value", "Recomputed", "Passed"],
           [[c["number"], c["check"], _cell(c["report"]), _cell(c["recomputed"]), c["passed"]]
            for c in raw["checks"]["checks"]])
    appendix = sec["appendix"]
    _sheet(wb, "Mapping", ["Setting", "Value"],
           [[r["role"], r["column"]] for r in appendix["mapping"]] + [list(s) for s in appendix["settings"]])
    definition = appendix.get("definition")
    if definition and definition.get("sources"):   # knowledge sources of the definition
        _sheet(wb, "Sources", ["Source", "Title", "Kind", "Date", "Reliability", "May be outdated", "Status",
                               "Statements"],
               [[x["id"], x["title"], x["kind"], x["date"], x["reliability"], "yes" if x["stale"] else "no", x["status"],
                 x["statements"]] for x in definition["sources"]])
        _sheet(wb, "Statements", ["Source", "Anchor", "Type", "Subject", "Object", "Value", "Quote", "Reliability"],
               [[x["source"], x["anchor"], x["type"], x["subject"], x["object"], x["value"], x["quote"], x["reliability"]]
                for x in definition.get("statements") or []])
    combined_rows = (sec["process"].get("business") or {}).get("combined_elements")
    if combined_rows:
        _sheet(wb, "Combined confidence", ["Element", "Kind", "Status", "Confidence", "Business belief",
                                           "Data verdict", "Support (cases)", "Sources"],
               [[e["label"], e["kind"], e["status"], e["confidence"], e["belief"], e["verdict"], e["support"],
                 ", ".join(e["sources"])] for e in combined_rows], {4: "0.0", 5: "0.00"})
    if definition:   # only when a business process definition was checked against this job
        _sheet(wb, "Definition", ["Position", "Step", "Stage", "Owner", "System", "Mandatory", "SLA", "Rules",
                                  "Mapped activities"],
               [[r["position"], r["name"], r["stage"], r["owner"], r["system"], r["mandatory"], r["sla"], r["rules"],
                 r["activities"]] for r in definition["steps"]])
        _sheet(wb, "Evidence", ["Claim", "Statement", "Verdict", "Evidence", "Support (cases)", "Badge"],
               [[c["id"], c["statement"], c["verdict"], c["text"], c["support"], c["badge"]]
                for c in definition["claims"]])
    wb.save(path)
    return path


def _cell(v):
    """A check value as one Excel cell (lists and dicts as text)."""
    return v if v is None or isinstance(v, (int, float, str, bool)) else json.dumps(v, default=str)[:30000]


def _category(app: str) -> str:
    from app.core.apps import category_of, load_map
    return category_of(app, load_map()) if app != metrics_mod.NO_APP else ""


# --------------------------------------------------------------------------- #
# 4. Everything for one job
# --------------------------------------------------------------------------- #
def generate_report(job_id: str, use_ai: bool = True, client=None,
                    order: "reference.Order | None" = None, inside_ai: bool | None = None) -> dict:
    """Build the step order (reference.json), the report numbers (metrics.json), the facts, the summary
    (narrative.json) and the calculation checks (checks.json), then report.html and report.xlsx.

    `use_ai=False` forces the fixed-template summary. `order` replaces the saved or
    suggested step order for this report (e.g. from the command line).
    """
    folder = jobs.job_folder(job_id)
    job = jobs.load_job(job_id)
    quality, mining = _load(folder / "quality.json"), _load(folder / "mining.json")
    if quality is None or mining is None:
        raise ValueError("Run the quality check and the analysis before generating the report.")
    con = connect()
    ref, built = None, False
    if mining["map"]["available"]:
        name = Path(job.get("source_name") or "Process").stem or "Process"
        # The same order as page 3 (reference.resolve): the user's pick, else the most trusted option.
        ref = reference.compute_for_job(con, folder / "events.parquet", job, name, order)
        built = True
        job["order_source"] = ref.get("source_badge")
    if ref is not None:
        with open(folder / "reference.json", "w", encoding="utf-8") as f:
            json.dump(ref, f, indent=2, default=str)
        ref = _load(folder / "reference.json")          # as stored (JSON types)
        if ref.get("flow"):
            job["flow_band"] = ref["flow"]["band"]       # shown on the jobs page; set before job.json is saved
    elif (folder / "reference.json").exists():
        (folder / "reference.json").unlink()
    m = metrics_mod.compute(connect(), folder, ref)
    metrics_mod.save(m, folder)
    facts = build_facts(con, folder / "events.parquet", quality, mining, job, reference=ref, log_built=built,
                        metrics=m)
    save_facts(facts, folder)
    # The claims of the definition checked against this job are facts too (type "claim").
    from app.core.evidence import claim_facts, load_evidence, run_check
    from app.core.definition import for_job
    definition = for_job(job)
    evidence = load_evidence(folder, definition) if definition else None
    if definition and evidence is None:
        evidence = run_check(job, definition)
    combined_facts = []
    if definition is not None and evidence is not None:
        from app.core import combined_confidence
        combined_facts = combined_confidence.facts(combined_confidence.run(job, definition))
    facts = load_facts(folder)
    # The optional sections (PM4Py, order evidence, inside each step) are numbered LAST, after the definition's
    # combined facts, so adding or removing them never moves an existing fact ID.
    LAST = ("pm4py", "evidence", "inside")
    base = [f for f in facts if f.type != "claim" and not f.type.startswith("combined_") and f.section not in LAST]
    tail = [f for f in facts if f.type != "claim" and not f.type.startswith("combined_") and f.section in LAST]
    last = max([int(f.id[1:]) for f in base if f.id[:1] == "F" and f.id[1:].isdigit()] or [0])
    for i, f in enumerate(combined_facts + tail, last + 1):
        f.id = f"F{i:03d}"
    with_claims = base + combined_facts + tail + claim_facts(evidence)
    if with_claims != facts:
        save_facts(with_claims, folder)
    context = build_context(job_id, use_ai=use_ai, client=client, inside_ai=inside_ai)
    html_path = folder / "report.html"
    html_path.write_text(render_html(context), encoding="utf-8")
    xlsx_path = write_excel(context, folder / "report.xlsx")
    job.update(status="reported", reported_at=context["generated_at"])
    jobs.save_job(job)
    return {"html": html_path, "excel": xlsx_path, "facts": folder / "facts.json",
            "narrative": folder / "narrative.json", "metrics": folder / metrics_mod.METRICS_FILE,
            "checks": folder / "checks.json"}
