"""The final business process map: the process as the business defines it, with the data's evidence.

to_business_dot   Graphviz DOT (drawn like the other maps; no Graphviz install needed):
                  swimlanes per stage (or owner role), start and end events, steps with
                  owner · system and the data's numbers, flows by kind, evidence colours.
business_html     one self-contained HTML file (DOT + the bundled Viz.js), prints to PDF.
to_bpmn           BPMN 2.0 XML with diagram layout (BPMN DI): opens in Camunda Modeler / bpmn.io.
"""
from __future__ import annotations

import html
import textwrap
import xml.etree.ElementTree as ET
from datetime import datetime

from app.core.definition import ProcessDefinition
from app.core.evidence import COLORS, CONTRADICTED, NO_DATA, PARTLY, SUPPORTED
from app.core.mining import _dot_text, format_hours

LEGEND = ("Rounded box = step (name · owner · system · cases, median time, SLA met) · solid border = mandatory · "
          "dashed border = optional · solid arrow = next step · curved dashed arrow = loop allowed · "
          "dotted arrow = optional skip · ○ start · ◉ end")
EVIDENCE_LEGEND = ("Colours: green = supported by the data · amber = partly supported · red = contradicted "
                   "(! with the claim id) · grey = no data")
LANE_FILLS = ("#f6f8fb", "#fdf6ec")
_RANK = {CONTRADICTED: 0, PARTLY: 1, SUPPORTED: 2, NO_DATA: 3}


def _month(m: str | None) -> str:
    return datetime.strptime(m, "%Y-%m").strftime("%b %Y") if m else "–"


def title(defn: ProcessDefinition, stats: dict | None) -> str:
    stats = stats or {}
    status = "approved" if defn.status == "approved" else "draft"
    parts = [f"{defn.name} — v{defn.version} {status}"]
    if stats.get("cases"):
        parts.append(f"{stats['cases']:,} cases")
    if stats.get("first_month"):
        parts.append(f"{_month(stats['first_month'])}–{_month(stats.get('last_month'))}")
    return " · ".join(parts)


def _step_verdict(claims: list[dict], step_id: str) -> tuple[str | None, list[str]]:
    """The worst verdict of the claims about one step, and the ids of its contradicted claims."""
    mine = [c for c in claims if (c.get("refs") or {}).get("step_id") == step_id and c["verdict"] in _RANK]
    if not mine:
        return None, []
    worst = min(mine, key=lambda c: _RANK[c["verdict"]])["verdict"]
    if worst == NO_DATA and any(c["verdict"] != NO_DATA for c in mine):
        worst = min((c for c in mine if c["verdict"] != NO_DATA), key=lambda c: _RANK[c["verdict"]])["verdict"]
    return worst, [c["id"] for c in mine if c["verdict"] == CONTRADICTED]


def _flow_claim(claims: list[dict], a: str, b: str, kinds=("order",)) -> dict | None:
    return next((c for c in claims if c["kind"] in kinds and (c.get("refs") or {}).get("from_step") == a
                 and (c.get("refs") or {}).get("to_step") == b), None)


def to_business_dot(defn: ProcessDefinition, claims: list[dict] | None, stats: dict | None,
                    show_evidence: bool = True, lane_by: str = "stage", step_stats: dict | None = None,
                    combined: dict | None = None) -> str:
    """Graphviz DOT of the business process map (see the module docstring).

    With `combined` (combined_confidence.combine), colours follow the combined status of each
    step and flow (business knowledge x data), and every box and arrow gets an id and a
    tooltip "Sources: S001 p.4, S003 [00:12:41] · Data: Supported (2,410 cases, High)".
    """
    from app.core.combined_confidence import (DOCUMENTED_NOT_ACTUAL, STATUS_COLORS, STATUS_NOTES, flow_status,
                                              step_status)
    claims = claims or []
    by_step = step_status(combined) if combined else {}
    by_flow = flow_status(combined) if combined else {}
    step_stats = step_stats or {}
    steps = defn.ordered_steps()
    ids = {s["id"]: f"n{i}" for i, s in enumerate(steps)}
    head = title(defn, stats)
    sub = " → ".join(x for x in (defn.start_trigger, defn.end_result) if x)
    label = head + (f"\n{sub}" if sub else "")
    lines = ["digraph business {", "  rankdir=LR;", "  newrank=true;", "  nodesep=0.35;", "  ranksep=0.5;",
             "  compound=true;",
             f'  label="{_dot_text(label)}"; labelloc=t; labeljust=c; fontsize=18; fontname="Helvetica"; '
             'fontcolor="#1f2933";',
             '  node [shape=box, style="rounded,filled", fillcolor="#ffffff", color="#4a6fa5", fontname="Helvetica", '
             'fontsize=12, fontcolor="#1f2933", margin="0.12,0.06"];',
             '  edge [color="#4a6fa5", arrowsize=0.8, penwidth=1.3, fontname="Helvetica", fontsize=10];']

    def box(s: dict) -> str:
        st = step_stats.get(s["id"]) or {}
        rows = ["\n".join(textwrap.wrap(s["name"], 26)) or s["name"]]
        who = " · ".join(x for x in (s.get("owner_role"), s.get("system")) if x)
        if who:
            rows.append(who)
        if s.get("milestone_labels"):
            rows.append("Milestones: " + " → ".join(s["milestone_labels"]))
        nums = []
        if st.get("cases"):
            nums.append(f"{st['cases']:,} cases ({100 * st.get('share', 0):.0f}%)")
        if st.get("median_h") is not None:
            nums.append(f"median {format_hours(st['median_h'])}")
        if st.get("sla_met") is not None:
            nums.append(f"SLA met {100 * st['sla_met']:.0f}%")
        if nums:
            rows.append(" · ".join(nums))
        if st.get("standard_share") is not None:
            rows.append(f"Standard way followed in {100 * st['standard_share']:.0f}% of runs")
        style = "rounded,filled" + ("" if s.get("mandatory", True) else ",dashed")
        color, extra = "#4a6fa5", ""
        el = by_step.get(s["id"])
        if el is not None:
            extra += f', id="step_{_dot_text(s["id"])}", tooltip="{_dot_text(el["tooltip"])}"'
        if show_evidence and el is not None:
            color = STATUS_COLORS[el["status"]]
            extra += ", penwidth=3" if el["status"] == DOCUMENTED_NOT_ACTUAL else ", penwidth=2.2"
            rows.append(f"{el['status']} · {el['confidence']:.0f}/100")
        elif show_evidence:
            verdict, bad = _step_verdict(claims, s["id"])
            if verdict:
                color = COLORS[verdict]
                extra = ", penwidth=2.2"
            if bad:
                rows.append("! " + ", ".join(bad))
        return (f'    {ids[s["id"]]} [label="{_dot_text(chr(10).join(rows))}", style="{style}", '
                f'color="{color}"{extra}];')

    # Lanes: one cluster per stage (or per owner role), in order of first use, left to right.
    lanes: dict[str, list[dict]] = {}
    for s in steps:
        key = defn.stage_name(s["stage_id"]) if lane_by == "stage" else (s.get("owner_role") or "(no owner)")
        lanes.setdefault(key, []).append(s)
    for i, (lane, members) in enumerate(lanes.items()):
        lines += [f"  subgraph cluster_{i} {{", f"    label=<<b>{html.escape(lane or '(no stage)')}</b>>;",
                  f'    fontsize=14; fontname="Helvetica"; style="rounded,filled"; fillcolor="{LANE_FILLS[i % 2]}"; '
                  'color="#d9e2ec"; margin=14;']
        lines += [box(s) for s in members]
        lines.append("  }")

    lines.append('  start [shape=circle, label="", width=0.3, style=filled, fillcolor="#ffffff", color="#2f9e44", '
                 'penwidth=1.5];')
    lines.append('  end [shape=doublecircle, label="", width=0.28, style=filled, fillcolor="#1f2933", '
                 'color="#1f2933", penwidth=2.5];')
    if steps:
        lines.append(f"  start -> {ids[steps[0]['id']]};")
        lines.append(f"  {ids[steps[-1]['id']]} -> end;")

    for f in defn.effective_flows():
        a, b = f["from_step"], f["to_step"]
        if a not in ids or b not in ids:
            continue
        attrs = []
        text = f.get("condition") or ""
        if f["kind"] == "allowed_loop":
            loop = _flow_claim(claims, a, b, ("loop",))
            rate = f" ({100 * loop['value']:.0f}% of cases)" if loop and loop.get("value") is not None else ""
            text = "loop allowed" + rate + (f"\n{text}" if text else "")
            attrs += ["style=dashed", "constraint=false", 'color="#4a6fa5"']
        elif f["kind"] == "optional_skip":
            attrs += ["style=dotted", "constraint=false"]
        el = by_flow.get((a, b))
        if el is not None:
            attrs += [f'id="flow_{_dot_text(a)}_{_dot_text(b)}"', f'tooltip="{_dot_text(el["tooltip"])}"']
        if show_evidence and el is not None:
            attrs.append(f'color="{STATUS_COLORS[el["status"]]}"')
        elif show_evidence and f["kind"] == "next":
            c = _flow_claim(claims, a, b)
            if c and c["verdict"] in COLORS:
                attrs.append(f'color="{COLORS[c["verdict"]]}"')
                if c["verdict"] == CONTRADICTED:
                    text = (text + "\n" if text else "") + f"! {c['id']}"
        if text:
            attrs.append(f'label="{_dot_text(text)}"')
        lines.append(f"  {ids[a]} -> {ids[b]}" + (f" [{', '.join(attrs)}]" if attrs else "") + ";")

    status_legend = "Status (business sources × data): " + " · ".join(f"{k} = {v}" for k, v in STATUS_NOTES.items())
    legend = LEGEND + ("\n" + (status_legend if combined else EVIDENCE_LEGEND) if show_evidence else "")
    lines.append(f'  legend [shape=plaintext, style="", label="{_dot_text(legend)}", fontsize=11, '
                 'fontcolor="#616e7c"];')
    lines.append("  end -> legend [style=invis];")
    lines.append("}")
    return "\n".join(lines)


def business_html(defn: ProcessDefinition, dot: str, evidence: dict | None = None) -> str:
    """One self-contained HTML file (pinned, bundled Viz.js by default) that prints to PDF."""
    from app.core.evidence import score_line
    from app.core.report import map_html
    notes = [LEGEND, EVIDENCE_LEGEND]
    if evidence:
        notes.insert(0, "Evidence: " + score_line(evidence))
    if defn.status != "approved":
        notes.insert(0, "DRAFT — not approved")
    return map_html(dot, title(defn, (evidence or {}).get("stats")), notes)


# --------------------------------------------------------------------------- #
# BPMN 2.0 with diagram layout
# --------------------------------------------------------------------------- #
NS = {"bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL", "bpmndi": "http://www.omg.org/spec/BPMN/20100524/DI",
      "dc": "http://www.omg.org/spec/DD/20100524/DC", "di": "http://www.omg.org/spec/DD/20100524/DI"}
TASK_W, TASK_H, COL_W, LANE_H, LEFT = 140, 80, 190, 160, 60


def _xml_id(text: str, prefix: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(text))
    return f"{prefix}_{clean}"


def to_bpmn(defn: ProcessDefinition) -> str:
    """BPMN 2.0 XML: a collaboration with one pool, a lane per stage, a task per step (with
    documentation: owner, system, SLA, rules), start and end events and sequence flows
    (conditions as names), plus BPMN DI shapes and edges on a simple grid: lanes stacked,
    steps left to right by position."""
    for p, uri in NS.items():
        ET.register_namespace(p, uri)
    b, dc, di, bdi = (f"{{{NS[k]}}}" for k in ("bpmn", "dc", "di", "bpmndi"))
    root = ET.Element(b + "definitions", {"id": "Definitions_1", "targetNamespace": "http://bpmn.io/schema/bpmn",
                                          "exporter": "process-mining-app", "exporterVersion": "1"})
    collab = ET.SubElement(root, b + "collaboration", {"id": "Collaboration_1"})
    proc_id = _xml_id(defn.name, "Process")
    ET.SubElement(collab, b + "participant", {"id": "Participant_1", "name": defn.name, "processRef": proc_id})
    proc = ET.SubElement(root, b + "process", {"id": proc_id, "name": defn.name, "isExecutable": "false"})
    if defn.description:
        ET.SubElement(proc, b + "documentation").text = defn.description
    steps = defn.ordered_steps()
    stages = [s for s in defn.stages if any(x["stage_id"] == s["id"] for x in steps)] or \
        [{"id": "ST01", "name": "Process"}]
    lane_of_step = {s["id"]: (s["stage_id"] if any(st["id"] == s["stage_id"] for st in stages) else stages[0]["id"])
                    for s in steps}
    task_id = {s["id"]: _xml_id(s["id"], "Task") for s in steps}
    lane_set = ET.SubElement(proc, b + "laneSet", {"id": "LaneSet_1"})
    lane_id = {st["id"]: _xml_id(st["id"], "Lane") for st in stages}
    for st in stages:
        lane = ET.SubElement(lane_set, b + "lane", {"id": lane_id[st["id"]], "name": st["name"]})
        refs = [task_id[s["id"]] for s in steps if lane_of_step[s["id"]] == st["id"]]
        if st["id"] == lane_of_step.get(steps[0]["id"] if steps else "", stages[0]["id"]):
            refs = ["StartEvent_1"] + refs
        if st["id"] == lane_of_step.get(steps[-1]["id"] if steps else "", stages[0]["id"]):
            refs = refs + ["EndEvent_1"]
        for r in refs:
            ET.SubElement(lane, b + "flowNodeRef").text = r
    ET.SubElement(proc, b + "startEvent", {"id": "StartEvent_1", "name": defn.start_trigger or "Start"})
    for s in steps:
        t = ET.SubElement(proc, b + "task", {"id": task_id[s["id"]], "name": s["name"]})
        doc = "; ".join(x for x in (
            f"Owner: {s['owner_role']}" if s.get("owner_role") else "",
            f"System: {s['system']}" if s.get("system") else "",
            f"SLA: {s['sla_hours']:g} h" if s.get("sla_hours") is not None else "",
            "Mandatory" if s.get("mandatory", True) else "Optional",
            f"Rules: {s['business_rules']}" if s.get("business_rules") else "",
            s.get("description") or "") if x)
        ET.SubElement(t, b + "documentation").text = doc
    ET.SubElement(proc, b + "endEvent", {"id": "EndEvent_1", "name": defn.end_result or "End"})
    seq = []
    if steps:
        seq.append(("StartEvent_1", task_id[steps[0]["id"]], ""))
    for f in defn.effective_flows():
        if f["from_step"] in task_id and f["to_step"] in task_id:
            name = f.get("condition") or ("loop allowed" if f["kind"] == "allowed_loop" else
                                          "optional skip" if f["kind"] == "optional_skip" else "")
            seq.append((task_id[f["from_step"]], task_id[f["to_step"]], name))
    if steps:
        seq.append((task_id[steps[-1]["id"]], "EndEvent_1", ""))
    flow_ids = []
    for i, (src, dst, name) in enumerate(seq, 1):
        fid = f"Flow_{i}"
        flow_ids.append((fid, src, dst))
        attrs = {"id": fid, "sourceRef": src, "targetRef": dst}
        if name:
            attrs["name"] = name
        ET.SubElement(proc, b + "sequenceFlow", attrs)

    # Diagram: lanes stacked top to bottom, one column per step position (start and end at the ends).
    col = {task_id[s["id"]]: i + 1 for i, s in enumerate(steps)}
    col["StartEvent_1"], col["EndEvent_1"] = 0, len(steps) + 1
    row = {task_id[s["id"]]: [st["id"] for st in stages].index(lane_of_step[s["id"]]) for s in steps}
    row["StartEvent_1"] = row.get(task_id[steps[0]["id"]], 0) if steps else 0
    row["EndEvent_1"] = row.get(task_id[steps[-1]["id"]], 0) if steps else 0
    bounds = {}
    for el, c in col.items():
        is_event = el in ("StartEvent_1", "EndEvent_1")
        w, h = (36, 36) if is_event else (TASK_W, TASK_H)
        x = LEFT + 40 + c * COL_W + (TASK_W - w) / 2
        y = row[el] * LANE_H + (LANE_H - h) / 2
        bounds[el] = (x, y, w, h)
    width = LEFT + 40 + (len(steps) + 2) * COL_W
    diagram = ET.SubElement(root, bdi + "BPMNDiagram", {"id": "BPMNDiagram_1"})
    plane = ET.SubElement(diagram, bdi + "BPMNPlane", {"id": "BPMNPlane_1", "bpmnElement": "Collaboration_1"})

    def shape(el: str, x, y, w, h, extra=None):
        s = ET.SubElement(plane, bdi + "BPMNShape", {"id": f"{el}_di", "bpmnElement": el, **(extra or {})})
        ET.SubElement(s, dc + "Bounds", {"x": f"{x:.0f}", "y": f"{y:.0f}", "width": f"{w:.0f}", "height": f"{h:.0f}"})

    shape("Participant_1", 0, 0, width, LANE_H * len(stages), {"isHorizontal": "true"})
    for i, st in enumerate(stages):
        shape(lane_id[st["id"]], 30, i * LANE_H, width - 30, LANE_H, {"isHorizontal": "true"})
    for el, (x, y, w, h) in bounds.items():
        shape(el, x, y, w, h)
    for fid, src, dst in flow_ids:
        sx, sy, sw, sh = bounds[src]
        tx, ty, tw, th = bounds[dst]
        e = ET.SubElement(plane, bdi + "BPMNEdge", {"id": f"{fid}_di", "bpmnElement": fid})
        if tx > sx:
            pts = [(sx + sw, sy + sh / 2), (tx, ty + th / 2)]
        else:   # a flow back (loop): over the top of the boxes
            top = min(sy, ty) - 20
            pts = [(sx + sw / 2, sy), (sx + sw / 2, top), (tx + tw / 2, top), (tx + tw / 2, ty)]
        for px, py in pts:
            ET.SubElement(e, di + "waypoint", {"x": f"{px:.0f}", "y": f"{py:.0f}"})
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def bpmn_problems(xml_text: str) -> list[str]:
    """Checks on a BPMN file: well-formed, every sequenceFlow references existing elements,
    every flow node has a DI shape. [] when fine."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError as err:
        return [f"not well-formed XML: {err}"]
    ids = {e.get("id") for e in root.iter() if e.get("id")}
    out = []
    for e in root.iter(f"{{{NS['bpmn']}}}sequenceFlow"):
        for ref in ("sourceRef", "targetRef"):
            if e.get(ref) not in ids:
                out.append(f"sequenceFlow {e.get('id')} refers to a missing element {e.get(ref)}")
    shaped = {e.get("bpmnElement") for e in root.iter(f"{{{NS['bpmndi']}}}BPMNShape")}
    for tag in ("task", "startEvent", "endEvent"):
        for e in root.iter(f"{{{NS['bpmn']}}}{tag}"):
            if e.get("id") not in shaped:
                out.append(f"{tag} {e.get('id')} has no diagram shape")
    return out
