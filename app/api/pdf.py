"""Stakeholder PDF readout (adopted from ProcessLens), made only from checked numbers: no AI call.

No case ID: nc_facts.json (checked facts), nc_measures.json, nc_flow.json, nc_checks.json.
Case ID:    quality.json and mining.json (the full report stays the HTML / Excel report).
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BLUE = colors.HexColor("#155eef")
INK = colors.HexColor("#182f50")
LINE = colors.HexColor("#dce7f7")
SOFT = colors.HexColor("#f3f7fe")


def _load(folder: Path, name: str, default=None):
    p = folder / name
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except ValueError:
        return default


def _styles():
    s = getSampleStyleSheet()
    return {"title": ParagraphStyle("t", parent=s["Title"], textColor=INK, fontSize=20, leading=24, alignment=0),
            "kicker": ParagraphStyle("k", parent=s["Normal"], textColor=BLUE, fontSize=8, leading=10,
                                     fontName="Helvetica-Bold"),
            "h2": ParagraphStyle("h2", parent=s["Heading2"], textColor=INK, fontSize=13, spaceBefore=10),
            "body": ParagraphStyle("b", parent=s["Normal"], textColor=INK, fontSize=9.5, leading=13),
            "small": ParagraphStyle("s", parent=s["Normal"], textColor=colors.HexColor("#5b6f8c"), fontSize=8,
                                    leading=10)}


def _table(rows: list[list], widths=None, header=True) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("FONT", (0, 0), (-1, -1), "Helvetica", 8.5), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
             ("GRID", (0, 0), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), SOFT), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5)]
    t.setStyle(TableStyle(style))
    return t


def _h(x) -> str:
    return "–" if x is None else f"{float(x):,.1f} h"


def nocase_elements(folder: Path, job: dict, process_name: str) -> list:
    st = _styles()
    facts = _load(folder, "nc_facts.json", []) or []
    m = _load(folder, "nc_measures.json", {}) or {}
    flow = _load(folder, "nc_flow.json", {}) or {}
    checks = _load(folder, "nc_checks.json", {}) or {}
    ctx = job.get("business_context") or {}
    o = m.get("overall") or {}
    el = [Paragraph("PROCESS READOUT · NO CASE ID · FROM CHECKED FACTS (NO AI)", st["kicker"]),
          Paragraph(process_name, st["title"]),
          Paragraph(f"Source: {job.get('source_name', '')} · generated "
                    f"{datetime.now(timezone.utc).strftime('%d %b %Y %H:%M')} UTC", st["small"]), Spacer(1, 6)]
    basis = (m.get("basis") or {}).get("time", "measured")
    el.append(Paragraph(
        "Business context: " + (f"{ctx.get('documents')} document(s), confirmed by {ctx.get('by')}"
                                if ctx.get("status") == "documents" else
                                f"no documents ({ctx.get('reason', 'not recorded')}): the process is described "
                                "from the data only") + f". Time basis: {basis}.", st["body"]))
    el.append(Paragraph("1 · Scope", st["h2"]))
    el.append(_table([["People", "Applications", "Events", "Active hours", "Linked to a step"],
                      [f"{o.get('actors', 0):,}", f"{o.get('apps', 0):,}", f"{o.get('events', 0):,}",
                       _h(o.get("hours")), f"{100 * (1 - float(o.get('unassigned_share') or 0)):.0f}%"]],
                     widths=[34 * mm] * 5))
    el.append(Paragraph("2 · Where the time goes (by stage and step)", st["h2"]))
    rows = [["Stage", "Step", "Hours", "Share", "People", "Runs", "Median run"]]
    for s in m.get("steps", []):
        rows.append([Paragraph(str(s.get("stage") or "—"), st["small"]), Paragraph(str(s["step"]), st["small"]),
                     _h(s.get("hours")), f"{100 * float(s.get('share_of_hours') or 0):.0f}%", s.get("actors", ""),
                     s.get("runs", ""), f"{float(s.get('median_run_s') or 0) / 60:.1f} min"])
    el.append(_table(rows, widths=[38 * mm, 50 * mm, 18 * mm, 15 * mm, 15 * mm, 15 * mm, 22 * mm]))
    el.append(Paragraph("3 · Order of work (transitions kept on the map)", st["h2"]))
    kept = [e for e in flow.get("edges", []) if e.get("kept")]
    kept.sort(key=lambda e: -int(e.get("count") or 0))
    rows = [["From", "To", "Times", "Episodes", "Status"]]
    rows += [[Paragraph(str(e["src"]), st["small"]), Paragraph(str(e["dst"]), st["small"]), e.get("count"),
              e.get("episodes"), e.get("status")] for e in kept[:15]]
    el.append(_table(rows, widths=[55 * mm, 55 * mm, 18 * mm, 20 * mm, 25 * mm]) if len(rows) > 1 else
              Paragraph("No transition passed the tests (too few episodes or people).", st["body"]))
    if flow.get("missing"):
        el.append(Paragraph("Steps of the process not seen in the data: " + ", ".join(m.get("step", "") if isinstance(m, dict) else str(m)
                                                                        for m in flow["missing"]),
                            st["body"]))
    el.append(PageBreak())
    el.append(Paragraph("4 · Checked facts", st["h2"]))
    rows = [["ID", "Fact", "Value", "Confidence"]]
    rows += [[f["id"], Paragraph(str(f.get("label")), st["small"]), str(f.get("display", f.get("value"))),
              f.get("confidence", "")] for f in facts[:45]]
    el.append(_table(rows, widths=[14 * mm, 100 * mm, 32 * mm, 28 * mm]))
    el.append(Paragraph("5 · Checks and limits", st["h2"]))
    el.append(Paragraph(f"{checks.get('passed', 0)} checks passed, {checks.get('failed', 0)} failed. Every number "
                        "above was computed twice in different ways; a failed check removes the number.", st["body"]))
    el.append(Paragraph("No case ID: arrows are the order in which people moved between steps inside a working "
                        "episode, not proven hand-offs of one transaction. Steps come from approved rules or AI "
                        "proposals that a reviewer accepts on Review steps.", st["body"]))
    return el


def case_elements(folder: Path, job: dict, process_name: str) -> list:
    st = _styles()
    q = _load(folder, "quality.json", {}) or {}
    mi = _load(folder, "mining.json", {}) or {}
    k = mi.get("kpis") or {}
    el = [Paragraph("PROCESS READOUT · CASE ID · FROM CHECKED NUMBERS (NO AI)", st["kicker"]),
          Paragraph(process_name, st["title"]),
          Paragraph(f"Source: {job.get('source_name', '')}", st["small"]), Spacer(1, 6),
          Paragraph("1 · Data quality", st["h2"]),
          Paragraph(f"Feasibility {q.get('feasibility', '–')} / 100 · band {q.get('band', '–')}.", st["body"]),
          Paragraph("2 · Key numbers", st["h2"])]
    rows = [["Measure", "Value"]] + [[str(a).replace("_", " "), str(b)] for a, b in list(k.items())[:20]
                                     if not isinstance(b, (dict, list))]
    el.append(_table(rows, widths=[90 * mm, 70 * mm]))
    nodes = (mi.get("map") or {}).get("nodes") or []
    if nodes:
        el.append(Paragraph("3 · Activities", st["h2"]))
        rows = [["Activity", "Events"]] + [[Paragraph(str(n.get("id") or n.get("activity")), st["small"]),
                                            n.get("count") or n.get("events")] for n in nodes[:30]]
        el.append(_table(rows, widths=[120 * mm, 40 * mm]))
    el.append(Paragraph("The full report (HTML / Excel) has the map, variants, waiting times and the evidence for "
                        "every statement.", st["small"]))
    return el


def readout_elements(saved: dict, ev: dict, job: dict, process_name: str) -> list:
    """The seven-section AI readout. Every number printed comes from the cited evidence, never from the AI."""
    st = _styles()
    r = saved.get("report") or {}
    lookup = {e["id"]: e for e in ev.get("evidence_catalog", [])}

    def cite(eid):
        e = lookup.get(eid) or {}
        return Paragraph(f"<b>{eid}</b> {e.get('topic', '')}: {e.get('observed_fact', '')}", st["small"])

    el = [Paragraph("AI READOUT · HYPOTHESES FROM CHECKED FACTS · VALIDATE WITH THE SME", st["kicker"]),
          Paragraph(r.get("headline", process_name), st["title"]),
          Paragraph(f"{process_name} · {job.get('source_name', '')} · written by {saved.get('provider')} / "
                    f"{saved.get('model')} on {str(saved.get('generated_at'))[:16].replace('T', ' ')} UTC", st["small"]),
          Spacer(1, 6), Paragraph("01 · Objective and scope", st["h2"]),
          Paragraph(r.get("transformation_objective", ""), st["body"]),
          Paragraph(r.get("executive_message", ""), st["body"])]
    scope = [e for e in ev.get("evidence_catalog", []) if e["id"] in ("E01", "E02", "E03", "E06")]
    if scope:
        el.append(_table([[e["topic"] for e in scope], [Paragraph(str(e["observed_fact"]), st["small"]) for e in scope]], widths=[42 * mm] * len(scope)))
    el.append(Paragraph("02 · Where work is concentrated", st["h2"]))
    for f in r.get("findings", []):
        el += [Paragraph(f"<b>{f.get('headline', '')}</b> {f.get('interpretation', '')}", st["body"]),
               cite(f.get("evidence_id"))]
    el.append(Paragraph("03 · As-is process", st["h2"]))
    order = " → ".join(s["step"] for s in ev.get("steps_in_business_order") or [])
    if order:
        el.append(Paragraph(f"Approved business order: {order}", st["body"]))
    for e in ev.get("evidence_catalog", []):
        if "transitions" in str(e.get("topic", "")).lower() or "not seen" in str(e.get("topic", "")).lower():
            el.append(cite(e["id"]))
    el.append(Paragraph("04 · Effort, repetition and hand-offs", st["h2"]))
    for f in r.get("risks", []):
        el += [Paragraph(f"<b>{f.get('headline', '')}</b> {f.get('interpretation', '')}", st["body"]),
               cite(f.get("evidence_id"))]
    el.append(Paragraph("05 · Interventions (hypotheses to validate)", st["h2"]))
    rows = [["Priority", "Intervention", "Change", "KPI to test", "Validation gate"]]
    for x in r.get("interventions", []):
        rows.append([x.get("priority", ""), Paragraph(f"<b>{x.get('title', '')}</b><br/>{x.get('evidence_id')}",
                                                       st["small"]),
                     Paragraph(x.get("proposed_change", ""), st["small"]), Paragraph(x.get("kpi_to_test", ""), st["small"]),
                     Paragraph(x.get("validation_gate", ""), st["small"])])
    el.append(_table(rows, widths=[16 * mm, 40 * mm, 46 * mm, 38 * mm, 38 * mm]))
    el += [Paragraph("06 · To-be workflow with agents", st["h2"]), Paragraph(r.get("orchestrator_role", ""), st["body"])]
    rows = [["Agent", "Role", "Actions", "Human control"]]
    for a in r.get("agents", []):
        rows.append([Paragraph(f"<b>{a.get('title', '')}</b>", st["small"]), Paragraph(a.get("role", ""), st["small"]),
                     Paragraph("<br/>".join(a.get("actions", [])), st["small"]),
                     Paragraph(a.get("human_control", ""), st["small"])])
    el.append(_table(rows, widths=[34 * mm, 48 * mm, 52 * mm, 44 * mm]))
    el.append(Paragraph("07 · Outcomes and scale path", st["h2"]))
    rows = [["Outcome", "KPI", "How to measure", "Scale gate"]]
    for o in r.get("outcomes", []):
        rows.append([Paragraph(f"<b>{o.get('title', '')}</b> ({o.get('target_direction', '')})", st["small"]),
                     Paragraph(o.get("kpi", ""), st["small"]), Paragraph(o.get("how_to_measure", ""), st["small"]),
                     Paragraph(o.get("scale_gate", ""), st["small"])])
    el.append(_table(rows, widths=[44 * mm, 44 * mm, 46 * mm, 44 * mm]))
    if r.get("decisions"):
        el.append(Paragraph("Decisions needed", st["h2"]))
        el += [Paragraph(f"• {d}", st["body"]) for d in r["decisions"]]
    el.append(Spacer(1, 8))
    el.append(Paragraph("Causes and changes are hypotheses from aggregate evidence; hours are observed effort, not "
                        "savings. Validate with the SME and a measured pilot before acting.", st["small"]))
    return el


# --------------------------------------------------------------------------- PDF and HTML from the same elements
def _build(el: list, title: str) -> bytes:
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm,
                      bottomMargin=16 * mm, title=title).build(el)
    return buf.getvalue()


def nocase_pdf(folder: Path, job: dict, process_name: str) -> bytes:
    return _build(nocase_elements(folder, job, process_name), f"{process_name} – process readout")


def case_pdf(folder: Path, job: dict, process_name: str) -> bytes:
    return _build(case_elements(folder, job, process_name), f"{process_name} – process readout")


def readout_pdf(saved: dict, ev: dict, job: dict, process_name: str) -> bytes:
    return _build(readout_elements(saved, ev, job, process_name), f"{process_name} – AI readout")


def nocase_html(folder: Path, job: dict, process_name: str) -> str:
    from app.api.exports import elements_html
    return elements_html(nocase_elements(folder, job, process_name), f"{process_name} – process readout")


def case_html(folder: Path, job: dict, process_name: str) -> str:
    from app.api.exports import elements_html
    return elements_html(case_elements(folder, job, process_name), f"{process_name} – process readout")


def readout_html(saved: dict, ev: dict, job: dict, process_name: str) -> str:
    from app.api.exports import elements_html
    return elements_html(readout_elements(saved, ev, job, process_name), f"{process_name} – AI readout")
