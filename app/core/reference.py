"""The simple process map: the intended order of steps, and how cases relate to it.

Why
---
On desktop / task-mining data, steps are often not done in a fixed order: for
most pairs of steps, A -> B and B -> A happen about equally often. A map of every
transition then becomes "spaghetti", and a map that shows one sequence would be
invented. Instead this module draws a REFERENCE map: the business's intended
order of steps (grouped in stages), left to right, and uses the data to show

    - how many cases reach each step,
    - how often cases actually follow that order (`in_order_share`),
    - where people go back and forth between two steps.

Until the business confirms an order, one is suggested from the data
(`suggest_order`) and labelled as suggested. Orders are saved per file layout
in <data_dir>/process_orders/<layout signature>.json, like column mappings.

All counting runs in DuckDB on the mining working tables (mining.build_log).
"""
from __future__ import annotations

import html
import json
import math
import os
import tempfile
import textwrap
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from app.core.config import data_dir, settings
from app.core.flow_confidence import SOURCE_LABELS, flow_confidence, order_source_of
from app.core.mining import _dot_text, build_log

LEGEND = ("Box = step · % = share of cases that reach it · darker = more cases · "
          "arrow = intended order · dashed ⇄ = people go back and forth")
OTHER = "Other steps"
NO_STAGE = ""        # one stage without a name: no cluster is drawn


def _cfg() -> dict:
    return settings()["reference_map"]


# --------------------------------------------------------------------------- #
# The order
# --------------------------------------------------------------------------- #
@dataclass
class Order:
    process_name: str
    stages: list[dict]                      # [{"name": str, "steps": [str, ...]}], in order
    other_steps: list[str] = field(default_factory=list)
    source: str = "suggested"               # "suggested" (data) | "template" | "ai" | "edited"
    confirmed_by_business: bool = False
    updated_at: str = ""
    mandatory_steps: list[str] = field(default_factory=list)   # from a template or the business
    template_name: str | None = None        # the matched template (source "template")
    template_match_share: float | None = None   # share of events whose step matched the template

    def drawn_steps(self) -> list[str]:
        return [s for stage in self.stages for s in stage["steps"]]

    def stage_of(self) -> dict[str, str]:
        return {s: stage["name"] for stage in self.stages for s in stage["steps"]}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        return cls(process_name=d.get("process_name") or "Process",
                   stages=[{"name": s.get("name", NO_STAGE), "steps": list(s.get("steps") or [])}
                           for s in d.get("stages") or []],
                   other_steps=list(d.get("other_steps") or []), source=d.get("source", "edited"),
                   confirmed_by_business=bool(d.get("confirmed_by_business")), updated_at=d.get("updated_at", ""),
                   mandatory_steps=list(d.get("mandatory_steps") or []), template_name=d.get("template_name"),
                   template_match_share=d.get("template_match_share"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def order_key(job: dict) -> str:
    """Where a job's order is saved: its file layout signature (older jobs: the job itself)."""
    return job.get("layout_signature") or f"job-{job['job_id']}"


def _orders_dir() -> Path:
    d = data_dir() / "process_orders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_order(key: str) -> Order | None:
    path = _orders_dir() / f"{key}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return Order.from_dict(json.load(f))


def save_order(key: str, order: Order) -> Path:
    """Save the order for this layout (temporary file, then rename: never half-written)."""
    folder = _orders_dir()
    path = folder / f"{key}.json"
    order.updated_at = _now()
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".order_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(order.to_dict(), f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def reconcile(order: Order, activities: list[str]) -> Order:
    """Fit a saved order to this data: unknown steps are dropped, new activities go to Other steps."""
    present = set(activities)
    stages = [{"name": s["name"], "steps": [x for x in s["steps"] if x in present]} for s in order.stages]
    stages = [s for s in stages if s["steps"]]
    placed = {x for s in stages for x in s["steps"]}
    other = [x for x in order.other_steps if x in present and x not in placed]
    other += [a for a in activities if a not in placed and a not in other]
    return replace(order, stages=stages, other_steps=other,
                   mandatory_steps=[s for s in order.mandatory_steps if s in present])


# --------------------------------------------------------------------------- #
# Measures
# --------------------------------------------------------------------------- #
def _pairs(edges: list[dict]) -> dict[tuple[str, str], list[int]]:
    """{(a, b) with a < b: [n_ab, n_ba]} from directly-follows counts (self-loops skipped)."""
    out: dict[tuple[str, str], list[int]] = {}
    for e in edges:
        s, d = e["src"], e["dst"]
        if s == d:
            continue
        key = (s, d) if s < d else (d, s)
        counts = out.setdefault(key, [0, 0])
        counts[0 if s < d else 1] += e["count"]
    return out


def dependency(n_ab: int, n_ba: int) -> float:
    """+1: always A before B; -1: always B before A; 0: both ways equally often."""
    return (n_ab - n_ba) / (n_ab + n_ba + 1)


def strength_label(score: float) -> str:
    t = _cfg()["order_strength"]
    return "Strong" if score >= t["strong"] else "Partial" if score >= t["partial"] else "Weak"


def order_strength(edges: list[dict]) -> tuple[float, str]:
    """How fixed the order of steps is (0-1), and its label (Strong / Partial / Weak).

    For every pair of steps, |dependency| says how one-sided their order is;
    the score is the mean over pairs, weighted by how often the pair occurs.
    Start/end arrows of the process map are left out.
    """
    m = settings()["mining"]
    special = {m["start_node"], m["end_node"]}
    pairs = _pairs([e for e in edges if e["src"] not in special and e["dst"] not in special])
    total = sum(a + b for a, b in pairs.values())
    if not total:
        return 0.0, strength_label(0.0)
    score = sum(abs(dependency(a, b)) * (a + b) for a, b in pairs.values()) / total
    return round(score, 3), strength_label(score)


def first_positions(con: duckdb.DuckDBPyConnection) -> str:
    """TEMP table rfirst: per case and activity, its FIRST position and how often it occurs.

    Columns: case_id, activity, first_seq, times, rel_pos = (first_seq - 1) / (events in
    case - 1), i.e. 0 = first step of the case, 1 = last. Needs mining.build_log first.
    """
    con.execute("""
        CREATE OR REPLACE TEMP TABLE rfirst AS
        SELECT f.case_id, f.activity, f.first_seq, f.times,
               CAST(f.first_seq - 1 AS DOUBLE) / greatest(c.n_events - 1, 1) AS rel_pos
        FROM (SELECT case_id, activity, MIN(seq) AS first_seq, COUNT(*) AS times
              FROM mlog GROUP BY case_id, activity) f
        JOIN mcases c USING (case_id)
    """)
    return "rfirst"


def suggest_order(con: duckdb.DuckDBPyConnection, min_case_share: float | None = None,
                  process_name: str = "Process") -> Order:
    """An order suggested from the data: steps by their median first position in a case.

    Steps reached by fewer than `min_case_share` of cases go into "Other steps".
    With a stage column, stages are ordered by the median first position of their
    steps, each step belongs to its most frequent stage, and steps are ordered
    within their stage. Needs first_positions first.
    """
    share = _cfg()["min_case_share"] if min_case_share is None else min_case_share
    n_cases = con.execute("SELECT COUNT(*) FROM mcases").fetchall()[0][0] or 1
    steps = con.execute("""
        SELECT activity, COUNT(*) AS cases, median(rel_pos) AS pos FROM rfirst
        GROUP BY activity ORDER BY pos, cases DESC, activity""").fetchall()
    drawn = [(a, c, p) for a, c, p in steps if c / n_cases >= share]
    other = [a for a, c, _ in sorted(steps, key=lambda x: (-x[1], x[0])) if c / n_cases < share]
    has_stage = con.execute("SELECT COUNT(stage) FROM mlog").fetchall()[0][0] > 0
    if not has_stage:
        return Order(process_name, [{"name": NO_STAGE, "steps": [a for a, _, _ in drawn]}] if drawn else [],
                     other, "suggested", False, "")
    stage_of = dict(con.execute(
        "SELECT activity, mode(stage) FROM mlog WHERE stage IS NOT NULL GROUP BY activity").fetchall())
    names = pd.DataFrame({"activity": [a for a, _, _ in drawn],
                          "stage": [stage_of.get(a) or "(no stage)" for a, _, _ in drawn]})
    con.register("ref_step_stage", names)
    stage_order = [s for s, _ in con.execute("""
        SELECT s.stage, median(r.rel_pos) AS pos FROM rfirst r JOIN ref_step_stage s USING (activity)
        GROUP BY s.stage ORDER BY pos, s.stage""").fetchall()]
    con.unregister("ref_step_stage")
    stages = [{"name": st, "steps": [a for a, _, _ in drawn if (stage_of.get(a) or "(no stage)") == st]}
              for st in stage_order]
    return Order(process_name, [s for s in stages if s["steps"]], other, "suggested", False, "")


def _register(con, name: str, df: pd.DataFrame) -> None:
    con.register(name, df)


def reference_stats(con: duckdb.DuckDBPyConnection, order: Order) -> dict:
    """Everything the simple map shows, each number with its support (cases).

    steps            per drawn step: cases_reached, share_reached, events, median_times_per_case
    other            the same for each step in "Other steps"
    stages           per stage: cases reached and share
    in_order_share   share of cases (with 2+ drawn steps) whose first occurrences follow the order
    pair_order_share share of step pairs (first occurrences) in the intended order: a softer measure
    back_and_forth   the most frequent pairs of drawn steps done both ways about equally
    reach_last_share share of cases that reach the last drawn step
    order_strength   score and label (see order_strength)
    Needs mining.build_log and first_positions first.
    """
    cfg = _cfg()
    n_cases, first, last = con.execute("SELECT COUNT(*), MIN(start_ts), MAX(start_ts) FROM mcases").fetchall()[0]
    n_cases = n_cases or 0
    drawn = order.drawn_steps()
    stage_of = order.stage_of()
    _register(con, "ref_steps", pd.DataFrame({"activity": pd.Series(drawn, dtype=object),
                                              "pos": pd.Series(range(len(drawn)), dtype="int64"),
                                              "stage": pd.Series([stage_of[s] for s in drawn], dtype=object)}))

    per_step = {a: (c, e, t) for a, c, e, t in con.execute("""
        SELECT activity, COUNT(*), SUM(times), median(times) FROM rfirst GROUP BY activity""").fetchall()}

    def step_row(name: str, position: int | None) -> dict:
        c, e, t = per_step.get(name, (0, 0, None))
        return {"step": name, "stage": stage_of.get(name, OTHER), "position": position, "cases_reached": c,
                "share_reached": round(c / n_cases, 4) if n_cases else 0.0, "events": int(e or 0),
                "median_times_per_case": None if t is None else float(t)}

    steps = [step_row(s, i + 1) for i, s in enumerate(drawn)]
    other_rows = [step_row(s, None) for s in order.other_steps]
    stages = [{"name": n, "cases": c, "share": round(c / n_cases, 4) if n_cases else 0.0}
              for n, c in con.execute("""
                  SELECT s.stage, COUNT(DISTINCT r.case_id) FROM rfirst r JOIN ref_steps s USING (activity)
                  GROUP BY s.stage""").fetchall()]
    stages.sort(key=lambda x: [s["name"] for s in order.stages].index(x["name"]))

    # Do the first occurrences of the drawn steps come in the intended order?
    ordered_cases, in_order = con.execute("""
        WITH d AS (SELECT r.case_id, r.first_seq, s.pos FROM rfirst r JOIN ref_steps s USING (activity)),
             w AS (SELECT case_id, pos, lag(pos) OVER (PARTITION BY case_id ORDER BY first_seq) AS prev,
                          COUNT(*) OVER (PARTITION BY case_id) AS n FROM d),
             c AS (SELECT case_id, MAX(n) AS n, bool_and(prev IS NULL OR pos >= prev) AS ok FROM w GROUP BY case_id)
        SELECT COUNT(*) FILTER (WHERE n >= 2), COUNT(*) FILTER (WHERE n >= 2 AND ok) FROM c""").fetchall()[0]
    pairs_total, pairs_ok = con.execute("""
        WITH d AS (SELECT r.case_id, r.first_seq, s.pos FROM rfirst r JOIN ref_steps s USING (activity))
        SELECT COUNT(*), COUNT(*) FILTER (WHERE a.first_seq < b.first_seq)
        FROM d a JOIN d b ON a.case_id = b.case_id AND a.pos < b.pos""").fetchall()[0]
    # Conformance: which step is most often done before a step intended to come earlier.
    earlier = [{"step": done, "before": intended, "cases": n} for intended, done, n in con.execute("""
        WITH d AS (SELECT r.case_id, r.activity, r.first_seq, s.pos FROM rfirst r JOIN ref_steps s USING (activity))
        SELECT a.activity, b.activity, COUNT(DISTINCT a.case_id) AS n
        FROM d a JOIN d b ON a.case_id = b.case_id AND a.pos < b.pos AND b.first_seq < a.first_seq
        GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT 5""").fetchall()]

    other_cases = 0
    if order.other_steps:
        _register(con, "ref_other", pd.DataFrame({"activity": pd.Series(order.other_steps, dtype=object)}))
        other_cases = con.execute(
            "SELECT COUNT(DISTINCT case_id) FROM rfirst WHERE activity IN (SELECT activity FROM ref_other)"
        ).fetchall()[0][0]
        con.unregister("ref_other")
    con.unregister("ref_steps")

    # Order strength and back-and-forth come from the directly-follows counts.
    edges, pair_cases = [], {}
    for s, d, n in con.execute("SELECT src, dst, COUNT(*) FROM mtrans GROUP BY src, dst").fetchall():
        edges.append({"src": s, "dst": d, "count": n})
    for a, b, c in con.execute("""
            SELECT least(src, dst), greatest(src, dst), COUNT(DISTINCT case_id) FROM mtrans
            WHERE src <> dst GROUP BY 1, 2""").fetchall():
        pair_cases[(a, b)] = c
    score, label = order_strength(edges)
    in_drawn = set(drawn)
    candidates = [((a, b), n_ab, n_ba) for (a, b), (n_ab, n_ba) in _pairs(edges).items()
                  if a in in_drawn and b in in_drawn
                  and abs(dependency(n_ab, n_ba)) < cfg["back_and_forth_max_dependency"]]
    candidates.sort(key=lambda x: (-(x[1] + x[2]), x[0]))
    back = [{"a": a, "b": b, "count": n_ab + n_ba, "n_ab": n_ab, "n_ba": n_ba, "cases": pair_cases.get((a, b), 0)}
            for (a, b), n_ab, n_ba in candidates[:cfg["back_and_forth_pairs"]]]

    last_step = drawn[-1] if drawn else None
    reach_last = per_step.get(last_step, (0, 0, 0))[0] if last_step else 0
    return {
        "cases": n_cases,
        "first_month": first.strftime("%Y-%m") if first else None,
        "last_month": last.strftime("%Y-%m") if last else None,
        "steps": steps, "other": other_rows, "stages": stages,
        "other_steps": {"steps": len(order.other_steps), "names": list(order.other_steps), "cases": other_cases,
                        "share": round(other_cases / n_cases, 4) if n_cases else 0.0},
        "in_order_share": round(in_order / ordered_cases, 4) if ordered_cases else None,
        "in_order_support": ordered_cases,
        "pair_order_share": round(pairs_ok / pairs_total, 4) if pairs_total else None,
        "pair_support": ordered_cases,
        "back_and_forth": back,
        "last_step": last_step,
        "reach_last_share": round(reach_last / n_cases, 4) if n_cases else 0.0,
        "order_strength": score, "order_strength_label": label,
        "earlier_than_intended": earlier,
        "missing_mandatory": sorted(
            ({"step": s, "cases_missing": n_cases - per_step.get(s, (0, 0, 0))[0],
              "share_missing": round((n_cases - per_step.get(s, (0, 0, 0))[0]) / n_cases, 4) if n_cases else 0.0}
             for s in order.mandatory_steps), key=lambda x: (-x["cases_missing"], x["step"]))[:5],
        "stability": order_stability(con, [s for s in drawn]),
    }


def _kendall_tau(a: dict[str, float], b: dict[str, float], min_gap: float = 0.0) -> float | None:
    """Kendall tau between two orderings given as {step: position value} (common steps only).

    Pairs whose positions in `a` differ by less than `min_gap` are ties (no evidence of an
    order) and are left out; None when no pair is left.
    """
    keys = [k for k in a if k in b]
    concordant = discordant = 0
    for i, x in enumerate(keys):
        for y in keys[i + 1:]:
            if abs(a[x] - a[y]) < min_gap:
                continue
            s = (a[x] - a[y]) * (b[x] - b[y])
            concordant += s > 0
            discordant += s < 0
    total = concordant + discordant
    return None if total == 0 else (concordant - discordant) / total


def order_stability(con: duckdb.DuckDBPyConnection, steps: list[str], table: str = "rfirst") -> float | None:
    """How stable the data-suggested order is: the share of random half-samples of the cases
    whose order of these steps agrees with the full data (Kendall tau >= flow_confidence.stability_min_tau).

    Cases are hashed into 64 buckets (as in quality.check_model_stability); each half is
    32 seeded buckets. A step's position is its mean relative first position; steps closer
    than `stability_min_gap` count as a tie (near-equal positions are noise, not an order).
    A half without any clear pair does not agree. Needs `table`
    with columns case_id, activity, rel_pos (rfirst, from first_positions, by default).
    """
    cfg = settings()["flow_confidence"]
    wanted = set(steps)
    if len(wanted) < 2:
        return None
    from app.core.stable import CASE_KEY, case_key_sql, has_table, half
    # Buckets by case content, not the salted ID (stable.py): the log's case_key when it covers this table's
    # cases, else a key from the table itself (the case's steps and their positions).
    covered = has_table(con, CASE_KEY) and not con.execute(
        f"SELECT COUNT(*) FROM {table} t ANTI JOIN {CASE_KEY} k USING (case_id)").fetchone()[0]
    keys = CASE_KEY if covered else (
        "(" + case_key_sql(table, "activity || '@' || CAST(rel_pos AS VARCHAR)", "rel_pos, activity") + ")")
    rows = [(b, a, s, n) for b, a, s, n in con.execute(
        f"SELECT k.bucket, t.activity, SUM(t.rel_pos), COUNT(*) FROM {table} t JOIN {keys} k USING (case_id) "
        "GROUP BY 1, 2").fetchall()
        if a in wanted]

    def means(buckets: set[int] | None) -> dict[str, float]:
        tot: dict[str, list[float]] = {}
        for b, a, s, n in rows:
            if buckets is None or b in buckets:
                t = tot.setdefault(a, [0.0, 0])
                t[0] += s
                t[1] += n
        return {a: t[0] / t[1] for a, t in tot.items() if t[1]}

    full = means(None)
    samples, min_tau = int(cfg["stability_samples"]), float(cfg["stability_min_tau"])
    min_gap = float(cfg["stability_min_gap"])
    agree = 0
    for i in range(samples):
        tau = _kendall_tau(full, means(half(i)), min_gap)
        agree += tau is not None and tau >= min_tau
    return round(agree / samples, 3)


def compute(con: duckdb.DuckDBPyConnection, events_path: Path, key: str, process_name: str = "Process",
            order: Order | None = None, use_saved: bool = True, log_built: bool = False) -> dict:
    """Build the working tables, then the order to show (given, saved or suggested), its stats
    and the flow confidence.

    `order` wins; otherwise the order saved for `key` (unless `use_saved` is False);
    otherwise the suggestion. Returns {"order", "suggested", "stats", "flow"} as plain data.
    The mining working tables stay on `con` for later use (e.g. facts); `log_built` skips
    rebuilding them when the caller already did.
    """
    if not log_built:
        build_log(con, events_path)
    first_positions(con)
    suggested = suggest_order(con, process_name=process_name)
    chosen = order or (load_order(key) if use_saved else None)
    if chosen is None:
        chosen = suggested
    else:
        activities = [a for (a,) in con.execute(
            "SELECT activity FROM rfirst GROUP BY activity ORDER BY COUNT(*) DESC, activity").fetchall()]
        chosen = reconcile(chosen, activities)
    stats = reference_stats(con, chosen)
    if chosen.source == "suggested" and not chosen.confirmed_by_business:
        stability = stats["stability"]
    else:   # the stability of the DATA's own suggestion (the evidence for an order in the data)
        stability = order_stability(con, suggested.drawn_steps())
    from app.core.derived import flow_caps, mapping_of   # local import avoids a cycle
    flow = flow_confidence(stats["order_strength"], order_source_of(chosen.to_dict()), stats["in_order_share"],
                           stats["pair_order_share"], stability, flow_caps(mapping_of(Path(events_path).parent)))
    milestones_path = Path(events_path).parent / "milestones.json"
    ms = json.loads(milestones_path.read_text(encoding="utf-8")) if milestones_path.exists() else {}
    milestone_order = ms.get("step_order", {})
    if chosen.source == "data (milestones)" and not milestone_order.get("order"):
        chosen = suggested
        stats = reference_stats(con, chosen)
        flow = flow_confidence(stats["order_strength"], "data", stats["in_order_share"], stats["pair_order_share"],
                               stats["stability"], flow_caps(mapping_of(Path(events_path).parent)))
    if milestone_order.get("order"):
        # The milestone order is found on a subset of the cases: the evidence for it is still the order strength
        # of ALL the data and how many cases follow it (no "clear order" from 18% of the cases).
        ms_stats = reference_stats(con, Order(process_name, [{"name": "Milestone order",
                                                               "steps": milestone_order["order"]}], [],
                                              "data (milestones)"))
        mf = flow_confidence(milestone_order["order_strength"], "data (milestones)", None, None,
                             milestone_order["stability"], flow_caps(mapping_of(Path(events_path).parent)),
                             coverage=ms["coverage"], data_strength=stats["order_strength"],
                             data_in_order=ms_stats["in_order_share"])
        from app.core.flow_confidence import BANDS
        auto = order is None and chosen.source == "suggested" and not chosen.confirmed_by_business
        if chosen.source == "data (milestones)" or (auto and BANDS.index(mf["band"]) < BANDS.index(flow["band"])):
            if auto:
                drawn = milestone_order["order"]
                chosen = Order(process_name, [{"name": "Milestone order", "steps": drawn}],
                               [a for a in suggested.drawn_steps() + suggested.other_steps if a not in drawn],
                               "data (milestones)")
                stats = reference_stats(con, chosen)
            flow = mf
    badge = source_badge(chosen)
    if flow["source"] == "data (milestones)" and ms:
        badge = f"Found in data (milestones, {ms['coverage']:.0%} of cases)"
    return {"order": chosen.to_dict(), "suggested": suggested.to_dict(), "stats": stats, "flow": flow,
            "source_badge": badge}


# --------------------------------------------------------------------------- #
# Editing (the page's step table)
# --------------------------------------------------------------------------- #
def order_rows(order: Order) -> list[dict]:
    """The order as rows for the edit table: drawn steps first (positions 1..n), then Other steps."""
    rows = [{"Stage": stage["name"], "Step": s, "Position": i, "Show on map": True}
            for i, (stage, s) in enumerate(((st, s) for st in order.stages for s in st["steps"]), 1)]
    n = len(rows)
    rows += [{"Stage": "", "Step": s, "Position": n + i, "Show on map": False}
             for i, s in enumerate(order.other_steps, 1)]
    return rows


def move_step(order: Order, step: str, delta: int) -> Order:
    """The order with `step` moved `delta` places (-1 = earlier, +1 = later) among the drawn steps. A step that
    moves past a stage boundary joins the stage of the step it passes. The result is an edited order, not yet
    confirmed (confirming is a separate, explicit choice)."""
    rows = [r for r in order_rows(order) if r["Show on map"]]
    names = [r["Step"] for r in rows]
    if step not in names:
        return order
    i = names.index(step)
    j = max(0, min(len(rows) - 1, i + int(delta)))
    if i == j:
        return order
    moved = rows.pop(i)
    moved["Stage"] = rows[j - 1]["Stage"] if j > i else rows[j]["Stage"]   # the stage of the step it passes
    rows.insert(j, moved)
    for k, r in enumerate(rows, 1):
        r["Position"] = k
    others = [{"Stage": "", "Step": s, "Position": len(rows) + k, "Show on map": False}
              for k, s in enumerate(order.other_steps, 1)]
    return order_from_rows(rows + others, order.process_name, False, base=order)


def validate_rows(rows: list[dict]) -> list[str]:
    """Problems in an edited step table: positions must be unique numbers, each step once."""
    errors = []
    steps = [str(r.get("Step") or "").strip() for r in rows]
    if any(not s for s in steps):
        errors.append("Every row needs a step name.")
    dupes = sorted({s for s in steps if s and steps.count(s) > 1})
    if dupes:
        errors.append("These steps appear more than once: " + ", ".join(dupes) + ".")
    positions = []
    for r in rows:
        p = r.get("Position")
        try:
            p = float(p)
            if math.isnan(p):
                raise ValueError
            positions.append(p)
        except (TypeError, ValueError):
            errors.append(f"'{r.get('Step')}' has no valid position number.")
    if len(positions) == len(rows) and len(set(positions)) != len(positions):
        errors.append("Positions must be unique: two steps have the same number.")
    return errors


def order_from_rows(rows: list[dict], process_name: str, confirmed: bool, base: Order | None = None) -> Order:
    """An edited order: ticked steps by position, grouped into stages in order of first use.

    `base` (the order being edited) passes on its mandatory steps and template details.
    """
    shown = sorted((r for r in rows if bool(r.get("Show on map"))), key=lambda r: float(r["Position"]))
    stages: list[dict] = []
    for r in shown:
        name = str(r.get("Stage") or "").strip()
        stage = next((s for s in stages if s["name"] == name), None)
        if stage is None:
            stage = {"name": name, "steps": []}
            stages.append(stage)
        stage["steps"].append(str(r["Step"]).strip())
    other = [str(r["Step"]).strip() for r in sorted(rows, key=lambda r: float(r["Position"]))
             if not bool(r.get("Show on map"))]
    # A template or AI order keeps its source when saved (edited or not); anything else is "edited".
    source = base.source if base and base.source in ("template", "ai") else "edited"
    return Order(process_name.strip() or "Process", stages, other, source, bool(confirmed), _now(),
                 mandatory_steps=list(base.mandatory_steps) if base else [],
                 template_name=base.template_name if base else None,
                 template_match_share=base.template_match_share if base else None)


def order_to_csv(order: Order) -> str:
    """The order as CSV (position, stage, step), for the business to check, edit and send back."""
    rows = [{"position": i, "stage": stage["name"], "step": s}
            for i, (stage, s) in enumerate(((st, s) for st in order.stages for s in st["steps"]), 1)]
    return pd.DataFrame(rows, columns=["position", "stage", "step"]).to_csv(index=False)


def order_from_csv(data: bytes, activities: list[str], process_name: str, confirmed: bool,
                   base: Order | None = None) -> tuple[Order | None, list[str]]:
    """Read an uploaded order (CSV with position, stage, step). Returns (order, problems).

    Steps not in the data are reported and left out; activities missing from the file
    go to Other steps. Positions must be unique numbers.
    """
    import io
    try:
        df = pd.read_csv(io.BytesIO(data), dtype=str, keep_default_na=False, sep=None, engine="python",
                         encoding="utf-8-sig")
    except Exception as err:  # noqa: BLE001 - any unreadable file is a plain problem for the user
        return None, [f"The file could not be read as CSV: {err}"]
    df.columns = [str(c).strip().lower() for c in df.columns]
    if not {"position", "step"} <= set(df.columns):
        return None, ["The file needs the columns position, stage and step."]
    known = set(activities)
    unknown = sorted({s.strip() for s in df["step"] if s.strip() and s.strip() not in known})
    rows = [{"Stage": (r.get("stage") or "").strip(), "Step": r["step"].strip(), "Position": r["position"],
             "Show on map": True} for r in df.to_dict("records") if r["step"].strip() in known]
    problems = validate_rows(rows)
    if unknown:
        problems.append("These steps are not in this data and were left out: " + ", ".join(unknown) + ".")
    if any(p for p in problems if not p.startswith("These steps are not in")):
        return None, problems
    listed = {r["Step"] for r in rows}
    n = len(rows)
    rows += [{"Stage": "", "Step": a, "Position": n + i, "Show on map": False}
             for i, a in enumerate((a for a in activities if a not in listed), 1)]
    return order_from_rows(rows, process_name, confirmed, base), problems


def describe_conformance(order: Order, stats: dict, flow: dict | None = None) -> list[str]:
    """"How reality compares" with the chosen order, in fixed sentences (numbers from stats)."""
    intended = "the order suggested by the data" if order_source_of(order.to_dict()) == "data" else "the intended order"
    out = []
    if stats.get("in_order_share") is not None:
        out.append(f"{_pct(stats['in_order_share'])} of cases follow {intended} exactly; "
                   f"{_pct(stats.get('pair_order_share'))} of step pairs are done in that order.")
    for e in (stats.get("earlier_than_intended") or [])[:3]:
        out.append(f"{e['step']} is done before {e['before']} in {e['cases']:,} cases.")
    for m in (stats.get("missing_mandatory") or [])[:3]:
        if m["cases_missing"]:
            out.append(f"Mandatory step {m['step']} is missing in {_pct(m['share_missing'])} of cases.")
    if stats.get("last_step"):
        out.append(f"{_pct(stats.get('reach_last_share'))} of cases reach {stats['last_step']}.")
    if flow is not None and flow["band"] == "None":
        out.append("The data does not show a fixed order. The map shows which steps occur and how often, "
                   "grouped by stage.")
    return out


def step_table(order: Order, stats: dict) -> pd.DataFrame:
    """The steps as a table (for the CSV download and the Excel sheet)."""
    rows = [{"Stage": r["stage"], "Step": r["step"], "Position": r["position"], "Cases reached": r["cases_reached"],
             "Share of cases": r["share_reached"], "Median times per case": r["median_times_per_case"],
             "In other steps": "no"} for r in stats["steps"]]
    rows += [{"Stage": "", "Step": r["step"], "Position": None, "Cases reached": r["cases_reached"],
              "Share of cases": r["share_reached"], "Median times per case": r["median_times_per_case"],
              "In other steps": "yes"} for r in stats["other"]]
    return pd.DataFrame(rows, columns=["Stage", "Step", "Position", "Cases reached", "Share of cases",
                                       "Median times per case", "In other steps"])


# --------------------------------------------------------------------------- #
# The map and its plain-language summary
# --------------------------------------------------------------------------- #
def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _fill(share: float) -> str:
    """Box colour: from very light blue (few cases) to a mid blue (all cases); text stays readable."""
    light, dark = (0xEE, 0xF3, 0xFB), (0x8F, 0xAE, 0xD8)
    t = max(0.0, min(1.0, share))
    return "#" + "".join(f"{round(a + (b - a) * t):02x}" for a, b in zip(light, dark))


def source_badge(order: Order) -> str:
    """Where the order comes from, as shown on badges: "Template: HR onboarding (87% of events matched)"."""
    source = order_source_of(order.to_dict())
    if source == "template" and order.template_name:
        share = f" ({100 * order.template_match_share:.0f}% of events matched)" if order.template_match_share else ""
        return f"Template: {order.template_name}{share}"
    if source == "template" and order.source == "edited":
        return "Edited order — needs confirmation"
    return SOURCE_LABELS[source]


def map_title(order: Order, stats: dict, flow: dict | None = None) -> str:
    """The title drawn at the top of the map, so it explains itself full screen or downloaded.

    With `flow` (flow_confidence), the title starts with what the map may claim:
    "Process flow", "Intended order", "Intended order (weak evidence)" or
    "Step overview — no fixed order found".
    """
    if flow is not None:
        source = source_badge(order)
    elif order.confirmed_by_business:
        source = "confirmed"
    elif order.source == "edited":
        source = "edited, not yet confirmed"
    else:
        source = "suggested"
    head = f"{order.process_name} — {stats.get('cases', 0):,} cases"
    parts = [f"{flow['allowed']['map_title']}: {head}" if flow is not None else head]
    if stats.get("first_month"):
        parts.append(f"{_month(stats['first_month'])} to {_month(stats.get('last_month'))}")
    parts.append(f"order: {source}")
    if flow is not None:
        parts.append(f"flow confidence: {flow['score']} ({flow['band']})")
    elif stats.get("order_strength_label"):
        parts.append(f"order strength: {stats['order_strength_label']}")
    return " · ".join(parts)


def overview_order(order: Order, stats: dict) -> Order:
    """For a map without a fixed order: the same steps and stages, sorted by cases reached."""
    reached = {r["step"]: r["cases_reached"] for r in stats.get("steps") or []}
    return replace(order, stages=[{"name": s["name"], "steps": sorted(s["steps"], key=lambda x: (-reached.get(x, 0), x))}
                                  for s in order.stages])


def to_reference_dot(order: Order, stats: dict, show_back_and_forth: bool = True,
                     flow: dict | None = None) -> str:
    """Graphviz DOT of the simple map, laid out to be read on one screen.

    With stages: each stage is a column (a cluster), columns left to right in stage
    order, and the steps of a stage run top to bottom in their intended order.
    Without stages: the steps wrap into rows of `reference_map.max_steps_per_row`,
    snaking (row 1 left to right, row 2 right to left, ...) so each arrow stays short.
    Solid arrows = the intended next step. Dashed red ⇄ lines = back and forth.
    "Other steps" is a small grey dashed box at the far right. The title is drawn at
    the top and the legend at the bottom. Works in Streamlit and in Viz.js (reports).

    With `flow` (flow_confidence) the title says what the map may claim, and when the
    flow band does not allow arrows ("None") no arrows are drawn: the boxes are grouped
    by stage and sorted by cases reached (a step overview, not a sequence).
    """
    cfg = _cfg()
    arrows = flow is None or flow["allowed"]["arrows"]
    title = map_title(order, stats, flow)
    if not arrows:
        order = overview_order(order, stats)
    wrap, per_row = int(cfg["label_wrap"]), max(1, int(cfg["max_steps_per_row"]))
    by_step = {r["step"]: r for r in stats.get("steps") or []}
    drawn = order.drawn_steps()
    ids = {s: f"s{i}" for i, s in enumerate(drawn)}
    lines = ["digraph reference {", "  rankdir=TB;", "  newrank=true;", "  nodesep=0.28;", "  ranksep=0.42;",
             f'  label="{_dot_text(title)}"; labelloc=t; labeljust=c; fontsize=18; '
             'fontname="Helvetica"; fontcolor="#1f2933";',
             '  node [shape=box, style="rounded,filled", color="#4a6fa5", fontname="Helvetica", fontsize=14, '
             'fontcolor="#1f2933", width=2.3, margin="0.1,0.06"];',
             '  edge [color="#4a6fa5", arrowsize=0.8, penwidth=1.3];']

    def box(step: str, indent: str = "    ") -> str:
        r = by_step.get(step, {"cases_reached": 0, "share_reached": 0.0})
        name = "\n".join(textwrap.wrap(step, wrap)) or step
        label = f"{name}\n{r['cases_reached']:,} cases · {100 * r['share_reached']:.0f}%"
        return f'{indent}{ids[step]} [label="{_dot_text(label)}", fillcolor="{_fill(r["share_reached"])}"];'

    staged = any(stage["name"] for stage in order.stages)
    anchors: list[str] = []          # one node per column (staged) or row 1 (no stages), left to right
    if staged:
        fills = ("#f6f8fb", "#fdf6ec")
        stage_of = order.stage_of()
        for i, stage in enumerate(order.stages):
            name = html.escape(stage["name"] or "(no stage)")    # an HTML-like label (for bold text)
            lines += [f"  subgraph cluster_{i} {{", f"    label=<<b>{name}</b>>;",
                      f'    fontsize=16; fontname="Helvetica"; style="rounded,filled"; fillcolor="{fills[i % 2]}"; '
                      'color="#d9e2ec"; margin=14;']
            lines += [box(s) for s in stage["steps"]]
            lines.append("  }")
            anchors.append(ids[stage["steps"][0]])
        for a, b in zip(drawn, drawn[1:]):
            # Inside a stage the arrow stacks the steps; across stages it must not push
            # the next column down, so it is drawn without affecting the layout.
            same = stage_of[a] == stage_of[b]
            if same:   # without arrows the step still stacks under the one before (invisibly)
                lines.append(f"  {ids[a]} -> {ids[b]};" if arrows else f"  {ids[a]} -> {ids[b]} [style=invis];")
            elif arrows:
                lines.append(f"  {ids[a]} -> {ids[b]} [constraint=false];")
    else:
        lines += [box(s, "  ") for s in drawn]
        rows = [drawn[i:i + per_row] for i in range(0, len(drawn), per_row)]
        for r, row in enumerate(rows):
            visual = row if r % 2 == 0 else list(reversed(row))          # snake: every other row reversed
            lines.append("  { rank=same; " + "; ".join(ids[s] for s in visual) + "; }")
            for a, b in zip(visual, visual[1:]):
                lines.append(f"  {ids[a]} -> {ids[b]} [style=invis];")   # keeps the row in visual order
            if r == 0:
                anchors = [ids[s] for s in visual]
        for a, b in zip(drawn, drawn[1:]):
            # Row ends connect straight down (they are above each other); arrows inside a
            # row do not move anything (the invisible edges above set the order).
            row_end = drawn.index(b) % per_row == 0
            if row_end:
                lines.append(f"  {ids[a]} -> {ids[b]};" if arrows else f"  {ids[a]} -> {ids[b]} [style=invis];")
            elif arrows:
                lines.append(f"  {ids[a]} -> {ids[b]} [constraint=false];")

    other = stats.get("other_steps") or {}
    if other.get("steps"):
        names = list(other.get("names") or [])
        shown = [textwrap.shorten(n, 26, placeholder="…") for n in names[:4]]
        shown += [f"+{len(names) - 4} more"] if len(names) > 4 else []
        label = "\n".join([f"{other['steps']} steps · {100 * other.get('share', 0.0):.0f}% of cases", *shown])
        lines += ["  subgraph cluster_other {", f'    label="{OTHER}";',
                  '    fontsize=14; fontname="Helvetica"; fontcolor="#52606d"; style="rounded,dashed"; '
                  'color="#9aa5b1";',
                  f'    other [label="{_dot_text(label)}", style="rounded,dashed,filled", fillcolor="#f0f0f0", '
                  'color="#9aa5b1", fontcolor="#52606d", fontsize=12];',
                  "  }"]
        anchors.append("other")
    if len(anchors) > 1:
        # Columns (or row 1 and Other steps) side by side, in order, starting at the top.
        lines.append("  { rank=same; " + "; ".join(anchors) + "; }")
        for a, b in zip(anchors, anchors[1:]):
            lines.append(f"  {a} -> {b} [style=invis];")

    if show_back_and_forth:
        for p in stats.get("back_and_forth") or []:
            if p["a"] in ids and p["b"] in ids:
                lines.append(f'  {ids[p["a"]]} -> {ids[p["b"]]} [dir=both, style=dashed, color="#c0504d", '
                             f'penwidth=1.6, constraint=false, fontsize=12, fontcolor="#c0504d", '
                             f'label="⇄ {p["count"]:,}"];')
    # The legend sits under the map: tied (invisibly) below the last step of every column
    # or of the last row; a node without edges would be placed beside the map instead.
    lines.append(f'  legend [shape=plaintext, style="", width=0, label="{_dot_text(LEGEND)}", fontsize=12, '
                 'fontcolor="#616e7c"];')
    if staged:
        bottoms = [ids[stage["steps"][-1]] for stage in order.stages]
    else:
        bottoms = [ids[s] for s in drawn[(max(len(drawn) - 1, 0) // per_row) * per_row:]]
    bottoms += ["other"] if other.get("steps") else []
    lines += [f"  {b} -> legend [style=invis];" for b in bottoms]
    lines.append("}")
    return "\n".join(lines)


def _month(m: str | None) -> str:
    return datetime.strptime(m, "%Y-%m").strftime("%B %Y") if m else "–"


def describe_reference(order: Order, stats: dict) -> list[str]:
    """At most 4 sentences (plus the "suggested" note), from fixed templates; numbers come from stats."""
    out = [f"{order.process_name}: {stats['cases']:,} cases from {_month(stats['first_month'])} "
           f"to {_month(stats['last_month'])}."]
    share, label = _pct(stats["in_order_share"]), stats["order_strength_label"]
    if label == "Strong":
        out.append(f"{share} of cases follow this order.")
    elif label == "Partial":
        out.append(f"{share} of cases follow this order exactly; most others do the same steps in a "
                   "different order.")
    else:
        out.append(f"These steps are not done in a fixed order: only {share} of cases follow it. Read the map "
                   "as the intended order, with how many cases reach each step.")
    if stats["back_and_forth"]:
        top = stats["back_and_forth"][0]
        out.append(f"The most back-and-forth is between {top['a']} and {top['b']} ({top['count']:,} times).")
    if stats["last_step"]:
        out.append(f"{_pct(stats['reach_last_share'])} of cases reach {stats['last_step']}.")
    if not order.confirmed_by_business:
        out.append(f"The order shown {source_phrase(order)} and has not yet been confirmed by the business.")
    return out


def status_line(order: Order, stats: dict) -> str:
    """One line: whose order it is, and how strongly the data follows an order."""
    who = "confirmed by business" if order.confirmed_by_business else (
        "edited, not yet confirmed by the business" if order.source == "edited" else
        f"{order.template_name} template, not yet confirmed" if order.template_name else "suggested from data")
    return f"Order: {who} · order strength {stats['order_strength']:.2f} ({stats['order_strength_label']})"


# --------------------------------------------------------------------------- #
# Where the order comes from (page 3 "Order source")
# --------------------------------------------------------------------------- #
SOURCE_COLORS = {"confirmed": "green", "saved": "orange", "template": "blue", "evidence": "blue", "ai": "violet",
                 "data": "orange"}
TEMPLATE_SAVED = "Saved (originally from the template, not evidence)"
TEMPLATE_OFF_NOTICE = ("The built-in template is no longer used as the step order (templates.use_for_order is off): "
                       "the next most trusted order is used instead.")


def ai_order_key(key: str) -> str:
    """Where an AI order suggestion for a layout is kept (next to the saved order)."""
    return f"{key}.ai"


def order_options(key: str, suggested: Order, step_events: dict[str, int], process_name: str,
                  milestones: dict | None = None, evidence: dict | None = None,
                  approved: Order | None = None) -> list[dict]:
    """The orders available for this layout, most trusted first.

    confirmed  the saved order, confirmed by the business
    saved      the saved order, not yet confirmed (edited or AI; one that came from a template is labelled so and
               never counts as confirmed)
    evidence   the order from your documents, checked against the data (order_evidence.py, order_evidence.json)
    template   the best matching built-in process template - only with templates.use_for_order (default off: a
               template is not evidence about your process)
    ai         an AI suggestion asked for earlier (names only)
    data (milestones), data   the order found in the data
    Each item: {"key", "label" (badge text), "color", "order"}.
    """
    from app.core.config import settings
    from app.core.templates import match_template, template_order   # local import avoids a cycle

    options = []
    if approved is not None:
        # The approved process (Business context) with this file's activities linked to its steps (case_steps.py).
        # The intended flow of the process (case_steps.py): the one confirmed on Review steps, else prefilled
        # from the data's own levels and the documented order.
        if approved.confirmed_by_business:
            options.append({"key": "confirmed_process", "label": "Intended flow, confirmed by the business",
                             "color": "green", "order": approved})
        else:
            options.append({"key": "approved_process", "label": "Intended flow prefilled from your documents and "
                            "the data — confirm it on Review steps", "color": "blue", "order": approved})
    saved = load_order(key)
    if saved is not None:
        if saved.source == "template" or saved.template_name:
            # Saved from the built-in template: shown as such, never treated as confirmed.
            saved = Order.from_dict({**saved.to_dict(), "confirmed_by_business": False})
            options.append({"key": "saved", "label": TEMPLATE_SAVED, "color": "orange", "order": saved})
        else:
            k = "confirmed" if saved.confirmed_by_business else "saved"
            label = source_badge(saved) if k == "confirmed" else f"Saved: {source_badge(saved)}"
            options.append({"key": k, "label": label, "color": SOURCE_COLORS[k], "order": saved})
    if evidence and evidence.get("documented") and evidence.get("order"):
        drawn = [s for s in evidence["order"] if s in step_events]
        if len(drawn) >= 2:
            ev = Order(process_name, [{"name": NO_STAGE, "steps": drawn}], [a for a in step_events if a not in drawn],
                       "evidence")
            options.append({"key": "evidence", "label": SOURCE_LABELS["evidence"], "color": "blue", "order": ev})
    template, mapping, share = (match_template(step_events) if settings()["templates"].get("use_for_order")
                                else (None, None, None))
    if template is not None:
        t_order = template_order(template, mapping, share, process_name)
        options.append({"key": "template", "label": source_badge(t_order), "color": "blue", "order": t_order})
    ai = load_order(ai_order_key(key))
    if ai is not None:
        options.append({"key": "ai", "label": SOURCE_LABELS["ai"], "color": "violet", "order": ai})
    if milestones and milestones.get("step_order", {}).get("order"):
        drawn = milestones["step_order"]["order"]
        mo = Order(process_name, [{"name": "Milestone order", "steps": drawn}],
                   [a for a in step_events if a not in drawn], "data (milestones)")
        options.append({"key": "data (milestones)", "label": f"Found in data (milestones, {milestones['coverage']:.0%} of cases)",
                        "color": "orange", "order": mo})
    options.append({"key": "data", "label": SOURCE_LABELS["data"], "color": "orange", "order": suggested})
    return options


def resolve(options: list[dict], job: dict | None = None) -> dict:
    """THE order a job uses: page 3, the report, the facts, conformance and flow confidence all call this.

    The user's pick on page 3 (job.json "order_choice") while it is still offered; otherwise the most
    trusted option: confirmed > saved > template > AI > milestones > data (the order of order_options).
    """
    want = (job or {}).get("order_choice")
    return next((o for o in options if o["key"] == want), options[0])


def notice(options: list[dict], job: dict | None) -> str | None:
    """One line when the page-3 pick is no longer offered (e.g. the template, switched off)."""
    want = (job or {}).get("order_choice")
    if want == "template" and not any(o["key"] == "template" for o in options):
        return TEMPLATE_OFF_NOTICE
    return None


def compute_for_job(con: duckdb.DuckDBPyConnection, events_path: Path, job: dict, process_name: str = "Process",
                    order: Order | None = None, log_built: bool = False) -> dict:
    """reference.compute for the order `resolve` picks (or `order`, e.g. from the command line).

    The result's source_badge is the label page 3 shows for that order, and "choice" its option key."""
    key = order_key(job)
    if order is not None:
        ref = compute(con, events_path, key, process_name, order, log_built=log_built)
        ref["choice"] = "given"
        return ref
    base = compute(con, events_path, key, process_name, None, use_saved=True, log_built=log_built)
    step_events = {r["step"]: r["events"] for r in base["stats"]["steps"] + base["stats"]["other"]}
    ms_path = Path(events_path).parent / "milestones.json"
    ms = json.loads(ms_path.read_text(encoding="utf-8")) if ms_path.exists() else None
    ev_path = Path(events_path).parent / "order_evidence.json"
    ev = json.loads(ev_path.read_text(encoding="utf-8")) if ev_path.exists() else None
    approved = None
    if job.get("process"):
        from app.core import case_steps   # local import avoids a cycle
        approved = case_steps.approved_order(job, list(step_events), Path(events_path))
    options = order_options(key, Order.from_dict(base["suggested"]), step_events, process_name, ms or None, ev,
                            approved)
    chosen = resolve(options, job)
    ref = compute(con, events_path, key, process_name, chosen["order"], use_saved=False, log_built=True)
    ref["source_badge"], ref["choice"], ref["notice"] = chosen["label"], chosen["key"], notice(options, job)
    return ref


def source_phrase(order: Order) -> str:
    """Where the order comes from, inside a sentence: "comes from the HR onboarding template"."""
    source = order_source_of(order.to_dict())
    if order.confirmed_by_business:
        return "is confirmed by the business"
    if source == "template" and order.template_name:
        return f"comes from the {order.template_name} template"
    if order.source == "edited":
        return "was edited and saved by hand"
    return {"ai": "is an AI suggestion", "data (milestones)": "is found in the data (milestone actions)",
            "evidence": "comes from your documents, checked against the data"}.get(
        source, "is suggested from the data")
