"""The Level 3 order from YOUR business documents, checked against the data (order_evidence.json).

Why: in case data where cases hold only part of the process, the order of steps across a case is close to a
coin toss, so the data alone cannot say what the order is - and a built-in template is not evidence about your
process. The order comes from the documents loaded on page 4 (transcripts, SOPs, deck, SME answers) and the
data only says, honestly, how far the cases support each part of it.

1. Documented order: the `order` statements of the knowledge sources (knowledge.merge; quotes and anchors kept).
   Statements in an OPEN conflict (A before B in one source, B before A in another) are never used and never
   resolved here: they are SME questions (knowledge.questions, page 4 "Conflicts"). The order is a topological
   sort of the remaining statements; steps no statement mentions are placed by the data (their median first
   position in the cases), marked "position not stated in any document".
2. Data check per pair: in the FULLY OBSERVED cases that contain both steps (first event at least
   order_evidence.edge_days after the start of the data, last event at least that many days before its end), the
   share where the first occurrence of A comes before the first occurrence of B. Verdict: supports (>= supports),
   contradicts (<= contradicts), unclear (in between, or fewer than min_cases cases). Every number has its cases.
3. Without documents: the milestone order or the data's order, labelled "No business documents loaded. Order
   taken from the data (weak)."
Code computes everything; the AI only extracted the statements earlier (knowledge.py). Quotes stay local.
"""
from __future__ import annotations

import heapq
import json
from pathlib import Path

import duckdb

from app.core.config import settings

FILE = "order_evidence.json"
NO_DOCS = "No business documents loaded. Order taken from the data (weak)."
NOT_STATED = "position not stated in any document"


def _cfg() -> dict:
    return settings()["order_evidence"]


def load(folder: Path) -> dict | None:
    try:
        return json.loads((Path(folder) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def verdict(share: float | None, cases: int) -> str:
    cfg = _cfg()
    if share is None or cases < int(cfg["min_cases"]):
        return "unclear"
    if share >= float(cfg["supports"]):
        return "supports"
    if share <= float(cfg["contradicts"]):
        return "contradicts"
    return "unclear"


# --------------------------------------------------------------------------- #
# The data side
# --------------------------------------------------------------------------- #
def first_positions(con: duckdb.DuckDBPyConnection, events_path: Path) -> tuple[dict, dict, int, int]:
    """({case: {activity: first position}} for the FULLY OBSERVED cases, {activity: median relative first
    position over all cases}, fully observed cases, all cases)."""
    from app.core.metrics import build_tables
    build_tables(con, events_path)
    days = int(_cfg()["edge_days"])
    rows = con.execute(f"""
        WITH b AS (SELECT MIN(ts_start) AS lo, MAX(ts_last) AS hi FROM xev),
             c AS (SELECT case_id FROM xcase, b
                   WHERE start_ts >= lo + INTERVAL {days} DAY AND end_ts <= hi - INTERVAL {days} DAY)
        SELECT e.case_id, e.activity, MIN(e.pos) FROM xev e JOIN c USING (case_id) GROUP BY 1, 2""").fetchall()
    first: dict[str, dict[str, int]] = {}
    for case, act, pos in rows:
        first.setdefault(case, {})[act] = int(pos)
    median = dict(con.execute("""
        WITH f AS (SELECT case_id, activity, MIN(pos) AS p FROM xev GROUP BY 1, 2),
             n AS (SELECT case_id, MAX(pos) AS n FROM xev GROUP BY 1)
        SELECT f.activity, median(CAST(f.p - 1 AS DOUBLE) / greatest(n.n - 1, 1)) FROM f JOIN n USING (case_id)
        GROUP BY 1""").fetchall())
    total = con.execute("SELECT COUNT(*) FROM xcase").fetchall()[0][0]
    return first, median, len(first), total


def precedence(first: dict, a: set[str], b: set[str]) -> tuple[float | None, int]:
    """In the cases that contain both (a step = one or more activities): the share where A's first occurrence
    comes before B's. (share, cases)."""
    both = before = 0
    for acts in first.values():
        pa = [acts[x] for x in a if x in acts]
        pb = [acts[x] for x in b if x in acts]
        if pa and pb:
            both += 1
            before += min(pa) < min(pb)
    return (before / both if both else None), both


# --------------------------------------------------------------------------- #
# The documents side
# --------------------------------------------------------------------------- #
def _activities_of(name: str, defn, activities: set[str]) -> list[str]:
    """A statement's step name as data activities: the definition step's mapped activities, or the activity."""
    if defn is not None:
        step = next((s for s in defn.steps if s["name"].lower() == name.lower()), None)
        if step and step.get("mapped_activities"):
            return [a for a in step["mapped_activities"] if a in activities]
    return [name] if name in activities else []


def documented(defn, activities: set[str]) -> tuple[list[dict], list[dict], dict]:
    """(order statements usable as edges, conflicts, source kinds). An edge: {a, b, acts_a, acts_b, quotes, by}."""
    from app.core import knowledge
    if defn is None:
        return [], [], {}
    kinds = {s["id"]: s["kind"] for s in knowledge.list_sources(defn.name)}
    merged = knowledge.merge(knowledge.all_statements(defn.name))
    open_topics = {c["topic"] for c in merged["conflicts"] if c["status"] == "open"}
    edges = []
    for k in merged["keys"].values():
        if k["type"] != "order" or k["topic"] in open_topics:
            continue
        acts_a, acts_b = _activities_of(k["subject"], defn, activities), _activities_of(k["object"], defn, activities)
        if not acts_a or not acts_b:
            continue
        sts = k["statements"]
        edges.append({"a": k["subject"], "b": k["object"], "acts_a": acts_a, "acts_b": acts_b,
                      "quotes": [{"source_id": s["source_id"], "quote": s["quote"], "anchor": s["anchor"]} for s in sts],
                      "by": "sme" if all(kinds.get(s["source_id"]) == "sme_answer" for s in sts) else "document"})
    conflicts = [{"id": c["id"], "topic": c["topic"], "status": c["status"],
                  "sides": [x["label"] for x in c["sides"]]} for c in merged["conflicts"] if c["topic"].startswith("order:")]
    return edges, conflicts, kinds


def topo_order(nodes: list[str], edges: list[tuple[str, str]], position: dict[str, float]) -> tuple[list[str], list[str]]:
    """Topological order (ties: the earlier median position in the data, then the name). Steps on a cycle of
    statements cannot be ordered by the documents: they are returned separately (and placed by the data)."""
    succ: dict[str, set[str]] = {n: set() for n in nodes}
    indeg = {n: 0 for n in nodes}
    for a, b in edges:
        if b not in succ[a]:
            succ[a].add(b)
            indeg[b] += 1
    key = lambda n: (position.get(n, 1.0), n)  # noqa: E731
    heap = [(key(n), n) for n in nodes if indeg[n] == 0]
    heapq.heapify(heap)
    out = []
    while heap:
        _k, n = heapq.heappop(heap)
        out.append(n)
        for m in sorted(succ[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                heapq.heappush(heap, (key(m), m))
    cycle = sorted((n for n in nodes if n not in out), key=key)
    return out, cycle


# --------------------------------------------------------------------------- #
# Everything for one job
# --------------------------------------------------------------------------- #
def compute(job: dict, defn=None) -> dict:
    """The proposed order with its evidence; saved as order_evidence.json in the job folder."""
    from app.core import jobs
    from app.core.db import connect
    folder = jobs.job_folder(job["job_id"])
    first, median, observed, total = first_positions(connect(), folder / "events.parquet")
    activities = set(median)
    edges, conflicts, _kinds = documented(defn, activities)
    pairs = []
    if edges:
        act_edges = [(x, y) for e in edges for x in e["acts_a"] for y in e["acts_b"] if x != y]
        order, cycle = topo_order(sorted(activities), act_edges, median)
        order = order + cycle            # steps on a cycle of statements: after the others, by the data
        in_docs = {x for e in edges for x in e["acts_a"] + e["acts_b"]}
        by_sme = {x for e in edges if e["by"] == "sme" for x in e["acts_a"] + e["acts_b"]}
        steps = [{"step": a, "position": i, "provenance": ("sme" if a in by_sme else "document") if a in in_docs
                  and a not in cycle else "data", "note": None if a in in_docs and a not in cycle else NOT_STATED}
                 for i, a in enumerate(order, 1)]
        for e in edges:
            share, n = precedence(first, set(e["acts_a"]), set(e["acts_b"]))
            pairs.append({"a": e["a"], "b": e["b"], "kind": "statement", "quotes": e["quotes"], "share": share,
                          "cases": n, "verdict": verdict(share, n)})
        seen = {(p["a"], p["b"]) for p in pairs}
        docs_in_order = [a for a in order if a in in_docs]
        for a, b in zip(docs_in_order, docs_in_order[1:]):
            if (a, b) not in seen:
                share, n = precedence(first, {a}, {b})
                pairs.append({"a": a, "b": b, "kind": "neighbour", "quotes": [], "share": share, "cases": n,
                              "verdict": verdict(share, n)})
                seen.add((a, b))
        label = "Proposed order (from your documents, checked against the data)"
        source = "documents"
    else:
        ms = _load_json(folder / "milestones.json") or {}
        mo = [a for a in (ms.get("step_order") or {}).get("order") or [] if a in activities]
        rest = sorted((a for a in activities if a not in mo), key=lambda a: (median.get(a, 1.0), a))
        order = mo + rest if mo else rest
        steps = [{"step": a, "position": i, "provenance": "milestones" if a in mo else "data", "note": NOT_STATED}
                 for i, a in enumerate(order, 1)]
        label, source, cycle = NO_DOCS, "milestones" if mo else "data", []
    for c in conflicts:                     # the data on both sides of a conflict: shown, never decided
        a, b = c["topic"].split(":", 1)[1].split("|")
        if a in activities and b in activities:
            share, n = precedence(first, {a}, {b})
            pairs.append({"a": a, "b": b, "kind": "conflict", "quotes": [], "share": share, "cases": n,
                          "verdict": verdict(share, n), "conflict": c["id"], "status": c["status"]})
    out = {"documented": bool(edges), "source": source, "label": label, "definition": defn.name if defn else None,
           "order": order, "steps": steps, "pairs": pairs, "conflicts": conflicts, "cycle": cycle,
           "not_stated": [s["step"] for s in steps if s["note"]], "cases_used": observed, "cases_total": total,
           "edge_days": int(_cfg()["edge_days"]), "thresholds": {k: _cfg()[k] for k in ("min_cases", "supports",
                                                                                     "contradicts")}}
    (folder / FILE).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def to_definition(evidence: dict, name: str, base=None):
    """The proposed order as a DRAFT definition (page 4): the base definition's steps re-positioned by the
    proposed order, or one step per activity; every step keeps where its position comes from."""
    from app.core import definition as dmod
    order = evidence["order"]
    prov = {s["step"]: s["provenance"] for s in evidence["steps"]}
    def idx(step: dict) -> int:
        places = [order.index(a) for a in step.get("mapped_activities") or [] if a in order]
        if not places and step["name"] in order:
            places = [order.index(step["name"])]
        return min(places) if places else len(order)

    if base is not None and base.steps:
        d = dmod.ProcessDefinition.from_dict(base.to_dict())
        for i, s in enumerate(sorted(d.steps, key=lambda s: (idx(s), s.get("position") or 0)), 1):
            s["position"] = i
            acts = [a for a in s.get("mapped_activities") or [] if a in prov] or [s["name"]]
            s["position_source"] = prov.get(acts[0], "data")
        d.status, d.approved_by, d.approved_at = "draft", None, None
    else:
        d = dmod.ProcessDefinition.from_dict({"name": name, "steps": [
            {"id": f"S{i:02d}", "name": a, "position": i, "mapped_activities": [a], "position_source": prov.get(a)}
            for i, a in enumerate(order, 1)]})
    d.source = evidence["label"]
    return d


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
