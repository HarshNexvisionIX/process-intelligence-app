"""The no-case report -> nc_report.html + nc_report.xlsx, built only from the job's computed files.

Sections: 1 key numbers, 2 where the data comes from, 3 what this data can support, 4 executive summary,
5 candidate process map, 6 step table, 7 hours by stage and step x application, 8 inside each step,
9 evidence, 10 questions for SMEs, 11 effort patterns, 12 record links (case or partial mode), 13 appendix.
In no_case mode the words for a case appear only in section 12 and the appendix (step names are shown as they
are). Durations as 38 s / 4.2 min / 5.3 h / 3.4 days. When a self-check fails the title starts with
"DRAFT — K checks failed".
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, select_autoescape

from app.core import fmt

HTML_FILE = "nc_report.html"
XLSX_FILE = "nc_report.xlsx"
SUMMARY_FILE = "nc_summary.json"
TEMPLATE = Path(__file__).parent / "nc_report.html.j2"


def _load(folder: Path, name: str, default=None):
    p = folder / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def mode_of(folder: Path) -> str:
    from app.nocase.strategies.base import load_decisions
    return (load_decisions(folder).get("case_link") or {}).get("decision") or "no_case"


def build(folder: Path, process: str | None = None, gw=None) -> dict:
    """Facts, summaries, checks, then the HTML and the Excel workbook; returns paths and the check result."""
    from app.core.report import viz_js
    from app.nocase import library, profile
    from app.nocase.engine import activities as act
    from app.nocase.engine.confidence import load as load_conf
    from app.nocase.engine.flow import drawn_edges, load as load_flow
    from app.nocase.engine.measures import load as load_measures
    from app.nocase.ingest import load_scope
    from app.nocase.report import checks as ck
    from app.nocase.report import facts as fx
    from app.nocase.report import narrative as nv
    from app.nocase.strategies.base import load_decisions
    mode = mode_of(folder)
    facts = fx.build(folder)
    facts = fx.add_inside(folder, facts)
    story = nv.write(facts, mode, gw)
    result = ck.run(folder, story["texts"])
    by_id = {f["id"]: f for f in facts}
    texts = {k: nv.fill(v, by_id) for k, v in story["texts"].items()}
    m, flow, conf = load_measures(folder), load_flow(folder), load_conf(folder)
    caps = profile.load(folder) or {}
    inside = _load(folder, "nc_inside_step.json", {})
    title = "Clickstream analysis"
    if result["failed"]:
        title = f"DRAFT — {result['failed']} checks failed · {title}"
    pack = library.load_pack(process) if process else None
    ctx = {
        "title": title, "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "mode": mode,
        "fmt": fmt, "m": m, "scope": load_scope(folder) or {}, "caps": caps, "texts": texts, "story": story,
        "flow": flow, "drawn": drawn_edges(flow) if flow else [], "conf": conf, "checks": result,
        "activities": act.load(folder).to_dict("records") if (folder / act.ACT_FILE).exists() else [],
        "evidence": _load(folder, "nc_evidence_scores.json", {}), "plan": _load(folder, "nc_evidence_plan.json", {}),
        "questions": _load(folder, "nc_sme_questions.json", []), "decisions": load_decisions(folder),
        "mapping": _load(folder, "nc_mapping.json", {}), "inside": inside, "facts": facts,
        "order_source": library.provenance(pack)["label"] if pack else "no process pack: steps by hours",
        "supports": _plain_supports(caps, mode), **viz_js()}
    env = Environment(autoescape=select_autoescape(["html"]))
    env.filters["dur"] = lambda s: fmt.duration_s(s)
    env.filters["hrs"] = lambda h: fmt.hours(h)
    env.filters["pct"] = lambda x: fmt.pct(x)
    env.filters["cnt"] = lambda n: fmt.count(n)
    html = env.from_string(TEMPLATE.read_text(encoding="utf-8")).render(**ctx)
    (folder / HTML_FILE).write_text(html, encoding="utf-8")
    (folder / SUMMARY_FILE).write_text(json.dumps({                 # what the summary rests on (shown by the web UI)
        "source": story["source"], "reason": story.get("reason"), "provider": story.get("provider"),
        "texts": texts, "generated": ctx["generated"], "checks": {k: result[k] for k in ("passed", "failed")}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    write_xlsx(folder / XLSX_FILE, ctx)
    return {"html": str(folder / HTML_FILE), "excel": str(folder / XLSX_FILE), "checks": result, "title": title,
            "summary_source": story["source"]}


def _plain_supports(caps: dict, mode: str) -> list[dict]:
    """Capabilities in plain words; in no_case mode the record-link rows go to section 12 / the appendix."""
    words = {"episodes": "Work episodes (one person's stretch of work)", "switching": "Application switching",
             "source_step_labels": "Step labels from the export", "level4_5": "Activities inside each step"}
    out = []
    for k, v in (caps.get("supports") or {}).items():
        if k in words:
            out.append({"what": words[k], "possible": v["possible"], "why": v["reason"]})
    return out


def write_xlsx(path: Path, ctx: dict) -> None:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Key numbers"
    ws.append(["Fact", "What", "Value", "Basis", "Confidence"])
    for f in ctx["facts"]:
        ws.append([f["id"], f["label"], f["value"], f["basis"], f["confidence"]])
    s = wb.create_sheet("Scope")
    for k in ("rows_in_file", "left_out_by_filter", "unreadable_time", "in_scope"):
        s.append([k.replace("_", " "), ctx["scope"].get(k)])
    s = wb.create_sheet("Steps")
    s.append(["Step", "Stage", "Hours", "Share of hours", "People", "Step runs", "Median run (s)", "Repetitions",
              "Top apps", "Confidence"])
    for x in ctx["m"].get("steps") or []:
        s.append([x["step"], x["stage"], x["hours"], x["share_of_hours"], x["actors"], x["runs"], x["median_run_s"],
                  x["repetitions"], ", ".join(a["app"] for a in x["top_apps"]),
                  (ctx["conf"].get("steps") or {}).get(x["step"], {}).get("band", "")])
    mx = ctx["m"].get("matrix_step_app") or {}
    s = wb.create_sheet("Step x app")
    s.append(["Step"] + mx.get("columns", []) + ["Total"])
    for r, cells, t in zip(mx.get("rows", []), mx.get("cells", []), mx.get("row_totals", [])):
        s.append([r] + cells + [t])
    s.append(["Total"] + mx.get("column_totals", []) + [mx.get("grand_total")])
    s = wb.create_sheet("Activities")
    s.append(["Step", "Activity", "Events", "Hours", "People", "Source", "Status"])
    for a in ctx["activities"]:
        s.append([a["step"], a["activity"], a["events"], a["hours"], a["actors"], a["source"], a["status"]])
    s = wb.create_sheet("Evidence")
    s.append(["Signal", "Score", "Why"])
    for k, v in ctx["evidence"].items():
        s.append([k, v["score"], v["reason"]])
    s = wb.create_sheet("Checks")
    s.append(["Check", "Passed", "Detail"])
    for c in ctx["checks"]["checks"]:
        s.append([c["check"], c["passed"], c["detail"]])
    wb.save(path)
