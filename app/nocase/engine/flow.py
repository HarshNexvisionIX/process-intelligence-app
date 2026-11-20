"""The candidate process map of the no-case mode -> nc_flow.json (nodes, edges, DOT).

    nodes      the steps, in the pack's business order (else by hours), with events, hours and actors
    inferred   "A followed by B in the same episode" (consecutive step runs of one actor): kept only when seen in
               at least nocase.flow.min_episodes episodes and by at least min_actors actors; with count,
               share_of_episodes, consistency across actors and across weeks
    observed   directly-follows inside linked cases (case mode only)
    documented the pack's business order (a spine of consecutive steps), with support = the share of inferred
               evidence in that direction; sme_confirmed when the order was confirmed in the review page
Never a case: episodes are one person's stretch of work, so an inferred edge is evidence of order, not a flow of
cases. The DOT draws stages as clusters, the steps in pack order, at most nocase.flow.max_edges edges plus the
spine, styled by status (solid observed, dashed inferred, dotted documented), with a legend.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.core.config import settings
from app.nocase import library
from app.nocase.engine.measures import UNASSIGNED, events_frame, runs_frame, step_order

FLOW_FILE = "nc_flow.json"
STYLE = {"observed": "solid", "inferred": "dashed", "documented": "dotted", "sme_confirmed": "bold"}


def _cfg() -> dict:
    return settings()["nocase"]["flow"]


def _edge_table(runs: pd.DataFrame) -> pd.DataFrame:
    r = runs[runs["step"] != UNASSIGNED].sort_values(["actor", "episode_id", "start"])
    nxt = r.groupby(["actor", "episode_id"], dropna=False)["step"].shift(-1)
    e = r.assign(dst=nxt).dropna(subset=["dst"])
    e = e[e["dst"] != e["step"]]
    e["week"] = e["start"].dt.strftime("%G-%V")
    return e.rename(columns={"step": "src"})


def _consistency(e: pd.DataFrame, a: str, b: str, by: str) -> float | None:
    """Share of actors (or weeks) with the pair whose A->B count is at least their B->A count."""
    fwd = e[(e["src"] == a) & (e["dst"] == b)].groupby(by).size()
    rev = e[(e["src"] == b) & (e["dst"] == a)].groupby(by).size()
    keys = fwd.index.union(rev.index)
    if not len(keys):
        return None
    return round(float(sum(fwd.get(k, 0) >= rev.get(k, 0) for k in keys) / len(keys)), 6)


def build(folder: Path, process: str | None = None) -> dict:
    from app.nocase.strategies.base import load_decisions
    c = _cfg()
    ev, _basis = events_frame(folder)
    runs = runs_frame(ev)
    order = [s for s in step_order(ev, process) if s != UNASSIGNED]
    pack = library.load_pack(process) if process else None
    stage_of = library.step_stage(pack)
    prov = library.provenance(pack) if pack else None
    nodes = []
    for s in order:
        d = ev[ev["step_label"] == s]
        nodes.append({"step": s, "stage": stage_of.get(s) or (d["stage"].dropna().iloc[0] if d["stage"].notna().any()
                                                               else None),
                      "events": int(len(d)), "hours": round(float(d["sec"].sum() / 3600), 6),
                      "actors": int(d["actor"].nunique())})
    e = _edge_table(runs)
    # episodes in which one person worked on at least two DIFFERENT steps (the only ones that can show an order)
    multi = int(runs[runs["step"] != UNASSIGNED].groupby(["actor", "episode_id"], dropna=False)["step"].nunique()
                .gt(1).sum())
    edges = []
    for (a, b), g in e.groupby(["src", "dst"]):
        episodes = g[["actor", "episode_id"]].drop_duplicates()
        actors = int(g["actor"].nunique())
        keep = len(episodes) >= int(c["min_episodes"]) and actors >= int(c["min_actors"])
        edges.append({"src": a, "dst": b, "status": "inferred", "count": int(len(g)), "episodes": int(len(episodes)),
                      "actors": actors, "share_of_episodes": round(len(episodes) / multi, 6) if multi else 0.0,
                      "consistency_across_actors": _consistency(e, a, b, "actor"),
                      "consistency_across_weeks": _consistency(e, a, b, "week"), "kept": bool(keep)})
    inferred = {(x["src"], x["dst"]): x for x in edges}
    # observed: directly-follows inside linked cases (case mode only)
    observed = []
    d = load_decisions(folder).get("case_link") or {}
    if d.get("decision") == "case" and (folder / "nc_case_links.parquet").exists():
        links = pd.read_parquet(folder / "nc_case_links.parquet")
        j = ev[ev["step"].notna()].merge(links[links["used"].astype(bool)][["eid", "case_id"]], on="eid")
        j = j.sort_values(["case_id", "ts", "eid"])
        seq = j[j["step_label"].ne(j.groupby("case_id")["step_label"].shift())]
        nxt = seq.groupby("case_id")["step_label"].shift(-1)
        pairs = seq.assign(dst=nxt).dropna(subset=["dst"]).groupby(["step_label", "dst"]).size()
        observed = [{"src": a, "dst": b, "status": "observed", "count": int(n)} for (a, b), n in pairs.items()]
    # documented: the pack's business order, with the inferred support in that direction
    confirmed = (prov or {}).get("status") == "sme_confirmed"
    approved = _approved_edges(process)
    spine = []
    for a, b in zip(order, order[1:]):
        if not pack or a not in stage_of or b not in stage_of:
            continue
        fwd = inferred.get((a, b), {}).get("count", 0)
        rev = inferred.get((b, a), {}).get("count", 0)
        status = "sme_confirmed" if confirmed or (a, b) in approved else "documented"
        spine.append({"src": a, "dst": b, "status": status, "label": (prov or {}).get("label"),
                      "support": round(fwd / (fwd + rev), 6) if fwd + rev else None, "count": int(fwd)})
    # documented steps the data does not show yet: drawn greyed, so the whole intended process is visible
    seen = {n["step"] for n in nodes}
    missing = [st for st in library.step_order(pack) if st not in seen] if pack else []
    out = {"nodes": nodes, "missing": [{"step": st, "stage": stage_of.get(st)} for st in missing],
           "edges": edges, "observed": observed, "spine": spine, "order_source": prov,
           "multi_step_episodes": multi, "min_episodes": c["min_episodes"], "min_actors": c["min_actors"]}
    out["dot"] = dot(out, library.step_order(pack) if pack else None)
    (folder / FLOW_FILE).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def _approved_edges(process: str | None) -> set[tuple[str, str]]:
    if not process:
        return set()
    try:
        from app.nocase.knowledge import library as klib
        doc = klib.load(process)["approved"].get("order.yaml") or {}
    except Exception:  # noqa: BLE001 - no approved order yet
        return set()
    edges = doc.get("edges", []) if isinstance(doc, dict) else []
    return {(str(x.get("from") or x.get("src")), str(x.get("to") or x.get("dst"))) for x in edges
            if x.get("status", "sme_confirmed") == "sme_confirmed"}


def drawn_edges(flow: dict) -> list[dict]:
    """The edges the map draws besides the spine: observed and kept inferred ones, most frequent first."""
    spine = {(x["src"], x["dst"]) for x in flow["spine"]}
    cand = [x for x in flow["observed"]] + [x for x in flow["edges"] if x["kept"]]
    cand = [x for x in cand if (x["src"], x["dst"]) not in spine]
    cand.sort(key=lambda x: (-x["count"], x["src"], x["dst"]))
    return cand[:int(_cfg()["max_edges"])]


def _q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def dot(flow: dict, business_order: list[str] | None = None) -> str:
    """The map: steps grouped by stage in business order, hours and share of hours on each step; documented steps
    not seen in the data greyed and dashed. Long processes run top to bottom so the labels stay readable. The
    legend is the caption under the chart (a legend drawn inside the graph took more room than the map)."""
    nodes = list(flow["nodes"])
    total = sum(n["hours"] for n in nodes) or 1.0
    missing = flow.get("missing") or []
    rank = {st: i for i, st in enumerate(business_order or [])}
    allnodes = sorted([dict(n, seen=True) for n in nodes] + [dict(m, seen=False, hours=0.0) for m in missing],
                      key=lambda n: rank.get(n["step"], len(rank)))
    tb = len(allnodes) > int(_cfg().get("top_down_after_steps", len(allnodes)))
    lines = ['digraph G {', f'rankdir={"TB" if tb else "LR"}; nodesep=0.35; ranksep=0.35; '
             'node [shape=box, style="rounded,filled", fillcolor="#e8f1f5", color="#0f5f7a", fontname="Helvetica", '
             'fontsize=11, margin="0.18,0.06"]; edge [fontname="Helvetica", fontsize=9, color="#5b6b75"];']
    stages: dict[str, list[dict]] = {}
    for n in allnodes:
        stages.setdefault(n.get("stage") or "", []).append(n)
    for i, (stage, group) in enumerate(stages.items()):
        if stage:
            lines.append(f'subgraph cluster_{i} {{ label={_q(stage)}; labeljust="l"; style="rounded"; color="#c9d3da"; '
                         'fontname="Helvetica"; fontsize=11; fontcolor="#14323f";')
        for n in group:
            if n["seen"]:
                label = f'{n["step"]}\n{n["hours"]:,.1f} h · {100 * n["hours"] / total:.0f}% of step hours'
                lines.append(f'{_q(n["step"])} [label={_q(label)}];')
            else:
                label = f'{n["step"]}\nnot seen in the data yet'
                lines.append(f'{_q(n["step"])} [label={_q(label)}, style="rounded,dashed", fillcolor="white", '
                             'color="#9aa7b0", fontcolor="#6b7a84"];')
        if stage:
            lines.append("}")
    # the business order as the spine (through steps not seen yet too), then the extra transitions seen
    order = [n["step"] for n in allnodes]
    spine = {(x["src"], x["dst"]): x for x in flow["spine"]}
    for a, b in zip(order, order[1:]):
        x = spine.get((a, b))
        if x:
            sup = "" if x["support"] is None else f'{100 * x["support"]:.0f}% this way'
            lines.append(f'{_q(a)} -> {_q(b)} [style={STYLE[x["status"]]}, label={_q(sup)}];')
        elif business_order:
            lines.append(f'{_q(a)} -> {_q(b)} [style=dotted, color="#b8c2c9"];')
    for x in drawn_edges(flow):
        lines.append(f'{_q(x["src"])} -> {_q(x["dst"])} [style={STYLE[x["status"]]}, color="#2a6fdb", '
                     f'label={_q(str(x["count"]))}, constraint=false];')
    lines.append("}")
    return "\n".join(lines)


def load(folder: Path) -> dict:
    p = folder / FLOW_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
