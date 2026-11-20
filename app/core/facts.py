"""Day 4: the fact store. Every number the report shows, with how far it can be trusted.

Gist
----
`build_facts` turns the quality and mining results into a list of `Fact`s. Each
fact has an ID ("F001"), a plain-English label, its value and unit, its
support (how many cases are behind it) and a badge:

    High         many cases, a narrow confidence interval, a stable ranking
    Medium       enough cases to report, with care
    Exploratory  too few cases to rely on (facts below `hide_below_support`
                 cases are dropped altogether, except facts about the data itself)

Confidence comes from two techniques, both computed from DuckDB data:
    bootstrap interval  for medians of durations and waits: resample the values
                        many times and see how much the median moves
    stability           for rankings (top variants, top transitions): rebuild the
                        ranking on random halves of the cases and see whether the
                        item stays in the top list

The report and the executive summary use ONLY these facts, so every number
they show can be traced back to its ID, support and source.
"""
from __future__ import annotations

import json
from app.core.stable import half as stable_half, seed as stable_seed
from dataclasses import asdict, dataclass, field
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.flow_confidence import cap_badge
from app.core.mining import build_log, format_hours, hours_unit

# Report sections, in report order; fact IDs follow this order.
# "hours" (metrics.py: active hours, shares, waiting, rework) comes last so older fact IDs keep their numbers.
# "pm4py" (the optional formal model, pm4py_model.py) comes after it, so no existing fact ID ever moves.
SECTION_ORDER = ("data", "overview", "variants", "map", "bottlenecks", "rework", "resources", "trends", "hours",
                 "pm4py", "evidence", "inside")      # evidence / inside (order evidence, inside each step) last
BADGE_ORDER = {"High": 0, "Medium": 1, "Exploratory": 2}
# Facts that describe a SEQUENCE (paths, waits between steps, order). Their badge is capped
# by the flow band (flow_confidence.ALLOWED), and with flow band Low/None the AI may not
# describe them as a sequence.
SEQUENCE_TYPES = {"variant_share", "variant_coverage_top10", "transition_count", "bottleneck_wait",
                  "in_order_share", "pair_order_share", "done_earlier", "waiting_total", "waiting_median"}


@dataclass
class Fact:
    id: str
    type: str
    label: str
    value: float | None
    unit: str                    # cases | events | share | hours | count | score | ratio (0-1)
    support: int | None          # cases behind the number
    ci_low: float | None = None
    ci_high: float | None = None
    stability: float | None = None
    badge: str = "Exploratory"
    section: str = "data"
    source: str = ""
    text: str | None = None      # for facts whose value is a word (the quality band)
    meta: dict = field(default_factory=dict)  # what the fact is about: rank, path, activity...


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #
def bootstrap_median_ci(values, resamples: int, seed: int = 0) -> tuple[float, float] | None:
    """95% interval of the median: the 2.5th and 97.5th percentiles of resampled medians."""
    arr = np.asarray(values, dtype=float)
    if len(arr) < 2:
        return None
    rng = np.random.default_rng(seed)
    medians = np.median(rng.choice(arr, size=(resamples, len(arr)), replace=True), axis=1)
    return round(float(np.percentile(medians, 2.5)), 6), round(float(np.percentile(medians, 97.5)), 6)


def ranking_stability(rows, k: int, samples: int) -> dict:
    """How often each item stays in the top `k` when ranked on random halves of the cases.

    `rows` are (bucket, item, count) with cases hashed into 64 buckets. Each half
    is 32 random buckets (seeded, so the result is the same on every run).
    Returns {item: share of halves where it is in the top k}.
    """
    totals: dict = {}
    for _, item, n in rows:
        totals[item] = totals.get(item, 0) + n
    hits = {item: 0 for item in totals}
    for i in range(samples):
        half = stable_half(i)
        counts: dict = {}
        for b, item, n in rows:
            if b in half:
                counts[item] = counts.get(item, 0) + n
        for item in sorted(counts, key=lambda x: (-counts[x], str(x)))[:k]:
            hits[item] += 1
    return {item: h / samples for item, h in hits.items()}


def badge(value, support, ci_low=None, ci_high=None, stability=None) -> str:
    """High / Medium / Exploratory, from support, interval width and stability (see settings)."""
    b = settings()["facts"]["badges"]
    hi = b["high"]
    support = support or 0
    narrow = True
    if ci_low is not None and ci_high is not None:
        width = ci_high - ci_low
        narrow = width == 0 or (bool(value) and width / abs(value) <= hi["max_relative_ci"])
    stable = stability is None or stability >= hi["min_stability"]
    if support >= hi["min_support"] and narrow and stable:
        return "High"
    if support >= b["medium"]["min_support"]:
        return "Medium"
    return "Exploratory"


# --------------------------------------------------------------------------- #
# Building the facts
# --------------------------------------------------------------------------- #
def _section(mining: dict, key: str):
    s = mining.get(key) or {}
    return s.get("data") if s.get("available") else None


def build_facts(con: duckdb.DuckDBPyConnection, events_path: Path, quality: dict, mining: dict,
                job: dict | None = None, reference: dict | None = None,
                log_built: bool = False, metrics: dict | None = None) -> list[Fact]:
    """All report numbers as facts (only for the sections `mining` makes available).

    `quality` is quality.json, `mining` is mining.json; `job` (job.json) adds the
    rows left out on page 1 when known. The mining working tables are rebuilt
    (mining.build_log) to fetch the values behind intervals and rankings.
    `metrics` (metrics.json) adds the work-hour facts of the report (section "hours" and the
    totals in "data"); they are sums over the data, so they are never hidden for low support.
    """
    cfg = settings()["facts"]
    flow = (reference or {}).get("flow")      # caps the badges of sequence facts (flow_confidence.py)
    resamples = cfg["bootstrap_resamples"]
    cap = int(cfg["bootstrap_max_values"])
    samples = cfg["stability_samples"]
    stats = quality.get("stats") or {}
    n_cases = stats.get("cases") or 0
    # With metrics.json (the report), the report's own numbers win: one definition per number (docs/METRICS.md).
    mt = (metrics or {}).get("totals") or {}
    if metrics:
        n_cases = mt["cases"]
    facts: list[Fact] = []

    def add(**kw) -> Fact:
        kw.setdefault("support", n_cases)
        f = Fact(id="", **kw)
        f.badge = badge(f.value, f.support, f.ci_low, f.ci_high, f.stability)
        if flow and f.type in SEQUENCE_TYPES:
            f.badge = cap_badge(f.badge, flow["allowed"]["sequence_badge_cap"])
        facts.append(f)
        return f

    # --- Data and quality (always kept) ---
    add(type="feasibility_score", label="Feasibility score", value=quality["feasibility"], unit="score",
        section="data", source=f"quality gate, {len(quality.get('checks') or [])} checks")
    add(type="quality_band", label="Quality band", value=quality["feasibility"], unit="score",
        text=quality["band"], section="data", source="band of the feasibility score")
    if metrics:
        add(type="events", label="Events in scope", value=mt["events"], unit="events", section="data",
            source="rows with case, activity and a usable time after the page-1 filters (metrics.py)")
    else:
        add(type="events", label="Analysable events", value=stats.get("clean_rows"), unit="events",
            section="data", source=f"rows with case, activity and a valid time, of {stats.get('rows', 0):,} rows")
    add(type="cases", label="Cases", value=n_cases, unit="cases", section="data",
        source="distinct case IDs among analysable events")
    kpi = _section(mining, "kpis")
    if kpi:
        add(type="activities", label="Activities", value=kpi["activities"], unit="count", section="data",
            source="distinct activity names")
    from app.core.derived import case_kind, case_label, mapping_of   # local import avoids a cycle
    job_mapping = mapping_of(Path(events_path).parent)
    if case_kind(job_mapping) != "column" or job_mapping.get("derived_activity"):
        add(type="case_definition", label="How cases are defined", value=None, unit="count", section="data",
            text=case_label(job_mapping) + ("; activities are screens (application + window title)"
                                            if job_mapping.get("derived_activity") else ""),
            source="derived on Import data because the export has no suitable column")
    summary = (job or {}).get("summary") or {}
    if "rows_left_out" in summary:
        add(type="rows_left_out", label="Rows left out on Import data", value=summary["rows_left_out"],
            unit="count", section="data",
            source=f"filters chosen on Import data, of {summary.get('rows_in_file', 0):,} rows in the file")

    if metrics:
        _metric_facts(add, metrics)

    needs_tables = any(_section(mining, k) for k in ("kpis", "variants", "map", "bottlenecks"))
    if needs_tables and not log_built:   # reference.compute may have built them already on `con`
        build_log(con, events_path)

    # --- Overview: case duration ---
    if kpi:
        durations = [r[0] for r in con.execute(
            f"SELECT duration_h FROM mcases JOIN case_key USING (case_id) ORDER BY ckey, duration_h "
            f"LIMIT {cap}").fetchall()]
        ci = bootstrap_median_ci(durations, resamples, stable_seed())
        dur = ({"median_h": mt["median_case_h"], "p90_h": mt["p90_case_h"], "mean_h": mt["mean_case_h"],
                "cases": mt["cases"]} if metrics else kpi)
        add(type="case_duration_median", label="Median case duration", value=dur["median_h"], unit="hours",
            ci_low=ci[0] if ci else None, ci_high=ci[1] if ci else None, section="overview",
            source=f"median of case durations, {dur['cases']:,} cases")
        add(type="case_duration_p90", label="90% of cases finish within", value=dur["p90_h"], unit="hours",
            section="overview", source=f"90th percentile of case durations, {dur['cases']:,} cases")
        add(type="case_duration_mean", label="Mean case duration", value=dur["mean_h"], unit="hours",
            section="overview", source=f"mean of case durations, {dur['cases']:,} cases")
        add(type="events_per_case", label="Median events per case", value=kpi["events_per_case_median"],
            unit="count", section="overview", source=f"median of events per case, {kpi['cases']:,} cases")

    # --- Variants: shares and how stable their ranking is ---
    var = _section(mining, "variants")
    if var and metrics:
        # Paths of step runs (metrics.py): the report's variants. No ranking stability for these.
        v = metrics["variants"]
        add(type="total_variants", label="Distinct paths (variants)", value=v["count"], unit="count",
            section="variants", source=f"distinct paths of step runs of {mt['cases']:,} cases")
        add(type="variant_coverage_top10", label="Share of cases covered by the 10 most common paths",
            value=v["top10_share"], unit="share", section="variants", source=f"top 10 paths of {v['count']:,}")
        for rank, x in enumerate(v["top"], 1):
            add(type="variant_share", label=" → ".join(x["path"]), value=x["share"], unit="share", support=x["cases"],
                section="variants", source=f"path rank {rank} (2+ steps): {x['cases']:,} of {mt['cases']:,} cases",
                meta={"rank": rank, "path": x["path"], "median_duration_h": x["median_case_h"]})
    elif var:
        top_k = cfg["top_variants"]
        add(type="total_variants", label="Distinct paths (variants)", value=var["total_variants"],
            unit="count", section="variants", source=f"distinct paths of {var['cases']:,} cases")
        add(type="variant_coverage_top10", label="Share of cases covered by the 10 most common paths",
            value=var["coverage_top10"], unit="share", section="variants",
            source=f"top 10 paths of {var['total_variants']:,}")
        rows = con.execute("SELECT bucket, variant_key, COUNT(*) FROM mcases JOIN case_key USING (case_id) "
                           "GROUP BY ALL").fetchall()
        keys = [r[0] for r in con.execute(
            f"SELECT variant_key FROM mcases GROUP BY 1 ORDER BY COUNT(*) DESC, variant_key LIMIT {int(top_k)}"
        ).fetchall()]
        stab = ranking_stability(rows, top_k, samples)
        for v, key in zip(var["variants"][:top_k], keys):  # same ranking rule as mining.variants
            add(type="variant_share", label=" → ".join(v["path"]), value=v["share"], unit="share",
                support=v["cases"], stability=round(stab.get(key, 0.0), 2), section="variants",
                source=f"variant rank {v['rank']}: {v['cases']:,} of {var['cases']:,} cases",
                meta={"rank": v["rank"], "path": v["path"], "median_duration_h": v["median_duration_h"]})

    # --- Process map: the most frequent transitions ---
    graph = _section(mining, "map")
    if graph:
        top_k = cfg["top_transitions"]
        special = {graph["start_node"], graph["end_node"]}
        real = [e for e in graph["edges"] if e["src"] not in special and e["dst"] not in special]
        real.sort(key=lambda e: (-e["count"], e["src"], e["dst"]))
        rows = con.execute(
            "SELECT bucket, src, dst, COUNT(*) FROM mtrans JOIN case_key USING (case_id) GROUP BY ALL").fetchall()
        stab = ranking_stability([(b, (s, d), n) for b, s, d, n in rows], top_k, samples)
        for rank, e in enumerate(real[:top_k], 1):
            add(type="transition_count", label=f"{e['src']} → {e['dst']}", value=e["count"], unit="count",
                support=e["cases"], stability=round(stab.get((e["src"], e["dst"]), 0.0), 2), section="map",
                source=f"times {e['dst']} directly follows {e['src']} in the same case",
                meta={"rank": rank, "src": e["src"], "dst": e["dst"], "median_wait_h": e["median_wait_h"]})

    # --- Process map: how closely cases follow the intended order (reference.py) ---
    if graph and reference:
        s = reference["stats"]
        n = s["cases"]
        if flow:
            from app.core.narrative import order_claim_allowed
            add(type="flow_confidence", label=f"Flow confidence ({flow['band']})", value=flow["score"], unit="score",
                support=n, section="map", source="; ".join(flow["reasons"]),
                meta={"band": flow["band"], "source": flow["source"],
                      "order_claim_allowed": order_claim_allowed(flow["source"] == "confirmed", s.get("order_strength"))})
            add(type="order_source", label="Where the step order comes from", value=None, unit="count",
                text=reference.get("source_badge") or flow["source"], support=n, section="map",
                source="confirmed by the business, a process template, an AI suggestion or the data",
                meta={"source": flow["source"]})
        add(type="order_strength", label=f"Order strength ({s['order_strength_label']})",
            value=s["order_strength"], unit="ratio", section="map",
            source="weighted one-sidedness of the order of every pair of steps (0 = none, 1 = fixed)",
            meta={"label": s["order_strength_label"]})
        if s["in_order_share"] is not None:
            add(type="in_order_share", label="Cases that follow the step order", value=s["in_order_share"],
                unit="share", support=s["in_order_support"], section="map",
                source=f"first occurrences in the intended order, of {s['in_order_support']:,} cases "
                       "with 2 or more drawn steps")
        if s["pair_order_share"] is not None:
            add(type="pair_order_share", label="Step pairs done in the intended order",
                value=s["pair_order_share"], unit="share", support=s["pair_support"], section="map",
                source="share of step pairs (first occurrences) in the intended order")
        if s["last_step"]:
            add(type="reach_last_share", label=f"Cases that reach {s['last_step']}", value=s["reach_last_share"],
                unit="share", support=n, section="map", source=f"of {n:,} cases",
                meta={"activity": s["last_step"]})
        for rank, step in enumerate(s["steps"] if not metrics else [], 1):   # with metrics: step_cases
            add(type="step_reach", label=f"Cases that reach {step['step']}", value=step["share_reached"],
                unit="share", support=step["cases_reached"], section="map",
                source=f"{step['cases_reached']:,} of {n:,} cases",
                meta={"rank": rank, "activity": step["step"], "stage": step["stage"]})
        for rank, p in enumerate(s["back_and_forth"], 1):
            add(type="back_and_forth", label=f"{p['a']} ⇄ {p['b']}", value=p["count"], unit="count",
                support=p["cases"], section="map",
                source=f"{p['n_ab']:,} times {p['a']} → {p['b']} and {p['n_ba']:,} times back, in {p['cases']:,} cases",
                meta={"rank": rank, "a": p["a"], "b": p["b"]})
        # Conformance with the chosen order ("How reality compares").
        for rank, e in enumerate(s.get("earlier_than_intended") or [], 1):
            add(type="done_earlier", label=f"{e['step']} done before {e['before']}", value=e["cases"],
                unit="cases", support=e["cases"], section="map",
                source=f"first occurrence of {e['step']} before {e['before']}, which the order puts earlier",
                meta={"rank": rank, "a": e["step"], "b": e["before"]})
        for rank, m in enumerate(s.get("missing_mandatory") or [], 1):
            if m["cases_missing"]:
                add(type="missing_mandatory", label=f"Cases without the mandatory step {m['step']}",
                    value=m["share_missing"], unit="share", support=n, section="map",
                    source=f"{m['cases_missing']:,} of {n:,} cases", meta={"rank": rank, "activity": m["step"]})

    # --- Bottlenecks: median waits with bootstrap intervals ---
    # With metrics, waits are between step RUNS (waiting_total / waiting_median) and rework is a return after
    # the gap (step_rework_share): the event-level bottleneck and repeat facts would be a second definition.
    bot = _section(mining, "bottlenecks") if not metrics else None
    if bot and bot["transitions"]:
        pairs = pd.DataFrame([(t["src"], t["dst"]) for t in bot["transitions"]], columns=["src", "dst"])
        con.register("fact_pairs", pairs)
        waits: dict = {}
        for s, d, w in con.execute(f"""
                SELECT src, dst, wait FROM (
                    SELECT t.src, t.dst, GREATEST(t.raw_wait_h, 0) AS wait,
                           row_number() OVER (PARTITION BY t.src, t.dst
                                              ORDER BY k.ckey, t.seq, t.raw_wait_h) AS rn    -- content order (stable.py)
                    FROM mtrans t JOIN fact_pairs p ON t.src = p.src AND t.dst = p.dst
                    JOIN case_key k ON k.case_id = t.case_id)
                WHERE rn <= {cap}""").fetchall():
            waits.setdefault((s, d), []).append(w)
        con.unregister("fact_pairs")
        for rank, t in enumerate(bot["transitions"], 1):
            ci = bootstrap_median_ci(waits.get((t["src"], t["dst"]), []), resamples, seed=stable_seed() + rank)
            add(type="bottleneck_wait", label=f"Wait from {t['src']} to {t['dst']}", value=t["median_wait_h"],
                unit="hours", support=t["cases"], ci_low=ci[0] if ci else None, ci_high=ci[1] if ci else None,
                section="bottlenecks", source=f"median wait over {t['count']:,} transitions in {t['cases']:,} cases",
                meta={"rank": rank, "src": t["src"], "dst": t["dst"], "p90_wait_h": t["p90_wait_h"]})

    # --- Rework ---
    rw = _section(mining, "rework") if not metrics else None
    if rw:
        for rank, a in enumerate(rw["activities"], 1):
            add(type="rework_share", label=f"{a['activity']} repeated within a case", value=a["share"],
                unit="share", support=a["cases"], section="rework",
                source=f"{a['cases']:,} of {a['of_cases']:,} cases with this activity",
                meta={"rank": rank, "activity": a["activity"], "extra_occurrences": a["extra_occurrences"]})
        for rank, p in enumerate(rw["ping_pong"], 1):
            add(type="ping_pong", label=f"{p['a']} → {p['b']} → {p['a']}", value=p["count"], unit="count",
                support=p["cases"], section="rework", source=f"back-and-forth in {p['cases']:,} cases",
                meta={"rank": rank, "a": p["a"], "b": p["b"]})

    # --- Resources ---
    res = _section(mining, "resources")
    if res:
        add(type="resources", label="Resources (people or systems)", value=res["total_resources"],
            unit="count", section="resources", source="distinct resource values")
        for rank, h in enumerate(res["handovers"] if not metrics else metrics["handovers"], 1):   # masked (P01)
            add(type="handover", label=f"{h['from']} → {h['to']}", value=h["count"], unit="count",
                support=h["cases"], section="resources", source=f"handovers in {h['cases']:,} cases",
                meta={"rank": rank, "from": h["from"], "to": h["to"]})

    # --- Application switches (readable names or "Unnamed app N"; never raw hashes) ---
    sw = _section(mining, "app_switching")
    if sw:
        k = sw["kpis"]
        gap = f"same case and user, at most {k['session_gap_minutes']} minutes apart, before merging repeats"
        add(type="app_switches_total", label="Application switches", value=k["total"], unit="count",
            support=k["cases"], section="resources", source=gap)
        if k["per_case_median"] is not None:
            add(type="app_switches_per_case", label="Median application switches per case",
                value=k["per_case_median"], unit="count", support=k["cases"], section="resources",
                source=f"median over {k['cases']:,} cases (90% have at most {k['per_case_p90']})")
        if k["per_100_events"] is not None:
            add(type="app_switches_per_100_events", label="Application switches per 100 events",
                value=k["per_100_events"], unit="count", support=k["cases"], section="resources",
                source="switches / events before merging repeated steps × 100")
        add(type="app_switch_case_share", label="Cases with at least one application switch",
            value=k["share_cases_with_switch"], unit="share", support=k["cases"], section="resources",
            source=f"{k['cases_with_switch']:,} of {k['cases']:,} cases")
        for rank, p in enumerate(sw["pairs"][:5], 1):
            add(type="app_switch_pair", label=f"Switch from {p['from']} to {p['to']}", value=p["count"],
                unit="count", support=p["cases"], section="resources",
                source=f"in {p['cases']:,} cases; median gap {p['median_gap']}",
                meta={"rank": rank, "from": p["from"], "to": p["to"]})
        if metrics and (metrics.get("switching") or {}).get("available"):
            for rank, s in enumerate(metrics["switching"]["by_step"][:5], 1):
                add(type="app_switches_per_step", label=f"Application switches per run of {s['step']}",
                    value=s["per_run"], unit="ratio", support=next((r["cases"] for r in metrics["steps"]
                                                                    if r["step"] == s["step"]), 0),
                    section="resources", source=f"{s['switches']:,} switches over {s['runs']:,} runs of the step",
                    meta={"rank": rank, "activity": s["step"]})
        for rank, s in enumerate(sw["by_step"][:5] if not metrics else [], 1):
            add(type="app_switches_per_step", label=f"Application switches each time {s['activity']} is done",
                value=s["per_occurrence"], unit="ratio", support=s["cases"], section="resources",
                source=f"{s['switches']:,} switches over {s['occurrences']:,} times the step is done",
                meta={"rank": rank, "activity": s["activity"]})

    ms = _section(mining, "milestones") if "milestones" in mining else None
    if ms:
        add(type="milestone_strength", label="Milestone order strength", value=ms["order_strength"], unit="ratio",
            support=ms["cases"], stability=ms["stability"], section="map", source="completion milestone transitions")
        add(type="milestone_coverage", label="Cases recording at least two milestones", value=ms["coverage"],
            unit="share", support=ms["total_cases"], section="map", source="all analysable cases")
        for c in ms["chains"]:
            f = add(type="milestone_chain", label=" → ".join(c["labels"]), value=c["cases"], unit="cases",
                    support=c["cases"], section="map", source="whole chain observed in case order",
                    meta={"chain_id": c["id"], "labels": c["labels"], "steps": c["steps"]})
            f.badge = cap_badge(f.badge, c["flow"]["allowed"]["sequence_badge_cap"])
        seen_steps = set()
        for c in ms["completion"]:
            if c["step"] not in seen_steps:
                seen_steps.add(c["step"])
                add(type="milestone_completion", label=f"{c['step']}: {c['label']}", value=c["share"], unit="share",
                    support=next((r["cases"] for r in ms["catalogue"] if r["label"] == c["label"]), 0),
                    section="map", source="step runs containing this milestone")

    # --- How each step is done (subprocess.py): badges capped by the step confidence ---
    sub = _section(mining, "subprocess") if "subprocess" in mining else None
    for d in (sub or {}).get("steps") or []:
        step, comp = d["activity"], d["comparison"]
        meta = {"activity": step, "confidence": d["confidence"], "standard_confirmed": bool(d["standard"]["confirmed"])}
        cap = "Exploratory" if d["confidence"] == "Exploratory" else d["confidence"]
        rows = [dict(type="step_runs", label=f"{step}: work sittings", value=d["runs"], unit="count",
                     source=f"work sittings (one person, no pause over 30 min) in {d['cases']:,} cases"),
                dict(type="step_median_actions", label=f"{step}: median actions per run", value=d["median_actions"],
                     unit="count", source=f"over {d['runs']:,} runs"),
                dict(type="step_median_duration", label=f"{step}: median run duration", value=d["median_duration_h"],
                     unit="hours", source="first to last action of a run"),
                dict(type="step_order_strength", label=f"{step}: order strength of the actions inside the step",
                     value=d["order_strength"], unit="ratio", source="action pairs inside runs"),
                dict(type="step_standard_share",
                     label=f"{step}: runs that follow the {'business standard' if meta['standard_confirmed'] else 'most common way'}",
                     value=comp["standard_share"], unit="share", source=f"standard: {d['standard']['source']}")]
        if comp["top_missing"]:
            m = comp["top_missing"][0]
            rows.append(dict(type="step_missing_share", label=f"{step}: runs without '{m['action']}'",
                             value=m["share"], unit="share", source="standard action missing in the run"))
        rows.append(dict(type="step_standard_source", label=f"{step}: where the standard way comes from", value=None,
                         unit="count", text=d["standard"]["source"], source="business standard or most common path"))
        for r in rows:
            f = add(**r, support=d["cases"], section="map", meta=dict(meta))
            f.badge = cap_badge(f.badge, cap)

    # --- Level 4 sub-steps inside each Level 3 step (level4.py): badges capped by the L3 confidence ---
    l4 = _section(mining, "level4") if "level4" in mining else None
    for d in (l4 or {}).get("l3s") or []:
        l3, cap = d["l3"], ("Exploratory" if d["confidence"] == "Exploratory" else d["confidence"])
        base = {"l3": l3, "confidence": d["confidence"], "source": d.get("source")}
        rows = [dict(label=f"{l3}: share of clicks in named sub-steps", value=d["coverage"], unit="share",
                     meta={**base, "kind": "coverage"}, source="Level 4 taxonomy coverage"),
                dict(label=f"{l3}: order strength of the sub-steps", value=d["order_strength"], unit="ratio",
                     meta={**base, "kind": "order_strength"}, source="sub-step pairs inside runs")]
        if d["paths"]:
            rows.append(dict(label=f"{l3}: runs that follow the most common sub-step path", value=d["paths"][0]["share"],
                             unit="share", meta={**base, "kind": "top_path"}, source=" → ".join(d["paths"][0]["path"])))
        if d["comparison"].get("classes"):
            rows.append(dict(label=f"{l3}: runs that follow the standard sub-step path",
                             value=d["comparison"]["standard_share"], unit="share", meta={**base, "kind": "standard"},
                             source=d["standard"]["source"]))
        for nd in d["nodes"]:
            rows.append(dict(label=f"{l3}: {nd['name']} — share of the step's clicks", value=nd["share"], unit="share",
                             meta={**base, "kind": "bucket_share", "bucket": nd["name"]}, source="clicks in this sub-step"))
            if nd.get("median_active_s") is not None:
                rows.append(dict(label=f"{l3}: {nd['name']} — median active time per run",
                                 value=nd["median_active_s"] / 3600, unit="hours",
                                 meta={**base, "kind": "bucket_active", "bucket": nd["name"]},
                                 source="active time of the sub-step in each run"))
        if d.get("median_active_s") is not None:
            rows.append(dict(label=f"{l3}: median active time per run", value=d["median_active_s"] / 3600, unit="hours",
                             meta={**base, "kind": "step_active"}, source="active time of all sub-steps in a run"))
        for r in rows:
            f = add(type="level4", support=d["cases"], section="map", **r)
            f.badge = cap_badge(f.badge, cap)
    for st in (l4 or {}).get("stages") or []:
        add(type="level4", label=f"{st['stage']}: steps with sub-steps", value=len(st["l3s"]), unit="count",
            section="map", source="Level 3 steps in this stage", meta={"stage": st["stage"], "kind": "stage_steps"})

    # --- Trends ---
    tr = _section(mining, "trends") if not metrics else None      # the report trends by ISO week (metrics.py)
    if tr:
        for m in tr["months"]:
            add(type="cases_started", label=f"Cases started in {m['month']}", value=m["cases"], unit="cases",
                support=m["cases"], section="trends", source="cases whose first event is in this month",
                meta={"month": m["month"], "median_duration_h": m["median_duration_h"]})

    # Drop facts with too little support (facts about the data itself always stay),
    # then number them in section order, so the same data always gives the same IDs.
    hide = cfg["hide_below_support"]
    if (mining.get("process_model") or {}).get("available"):
        _pm4py_facts(add, Path(events_path).parent)
    for r in evidence_rows(Path(events_path).parent):
        add(section="evidence", source="order_evidence.json (documents checked against the fully observed cases)", **r)
    if (mining.get("inside") or {}).get("available"):
        from app.core import inside_step
        for r in inside_step.fact_rows(inside_step.load(Path(events_path).parent)):
            add(section="inside", source="inside_steps.json (runs inside the step)", **r)
    kept = [f for f in facts if f.section in ("data", "hours", "pm4py", "evidence", "inside") or (f.support or 0) >= hide]
    kept.sort(key=lambda f: SECTION_ORDER.index(f.section))  # stable: keeps rank order inside a section
    for i, f in enumerate(kept, 1):
        f.id = f"F{i:03d}"
    return kept


def _metric_facts(add, m: dict) -> None:
    """Facts from metrics.json: totals (section "data") and hours, shares, rework and waiting ("hours")."""
    t = m["totals"]
    n = t["cases"]
    add(type="step_runs_total", label="Step runs (consecutive events of one step merged)", value=t["runs"],
        unit="count", support=n, section="data", source="runs of the same activity in a case, in time order")
    if t.get("participants"):
        add(type="participants", label="Participants", value=t["participants"], unit="count", support=n,
            section="data", source="distinct users (shown masked)")
    if t.get("applications"):
        add(type="applications", label="Applications", value=t["applications"], unit="count", support=n,
            section="data", source="distinct applications")
    if t.get("active_hours") is not None:
        add(type="active_hours_total", label="Active hours in scope", value=t["active_hours"], unit="work_hours",
            support=n, section="data", source="sum of the active-time column over the events in scope")
    for rank, r in enumerate(m["steps"], 1):
        meta = {"rank": rank, "activity": r["step"], "stage": r["stage"]}
        reached = r["cases"]
        add(type="step_cases", label=f"Cases that reach {r['step']}", value=r["cases_share"], unit="share",
            support=reached, section="hours", source=f"{reached:,} of {n:,} cases", meta=meta)
        if r["active_hours"] is not None:
            add(type="step_hours", label=f"Active hours in {r['step']}", value=r["active_hours"], unit="work_hours",
                support=reached, section="hours", source="sum of active time over the step's events", meta=meta)
            add(type="step_hours_share", label=f"{r['step']}: share of active hours", value=r["hours_share"],
                unit="share", support=reached, section="hours", source="step hours ÷ hours in scope", meta=meta)
        if r["rework_cases"]:
            add(type="step_rework_share", label=f"{r['step']}: cases with rework", value=r["rework_share"],
                unit="share", support=reached, section="hours",
                source=f"{r['rework_cases']:,} of {reached:,} cases reaching the step return to it after "
                       f"{m['settings']['rework_gap_minutes']} minutes or more", meta=meta)
        if r.get("waiting_share") is not None:
            add(type="step_waiting_share", label=f"{r['step']}: waiting share", value=r["waiting_share"], unit="share",
                support=reached, section="hours", source="wait time ÷ turnaround time of the step's events", meta=meta)
    for rank, a in enumerate(m["applications"], 1):
        if a["active_hours"] is not None and a["application"] != "(no application)":
            meta = {"rank": rank, "application": a["application"]}
            add(type="app_hours", label=f"Active hours in {a['application']}", value=a["active_hours"],
                unit="work_hours", support=a["cases"], section="hours", source="sum of active time", meta=meta)
            add(type="app_hours_share", label=f"{a['application']}: share of active hours", value=a["hours_share"],
                unit="share", support=a["cases"], section="hours", source="application hours ÷ hours in scope", meta=meta)
    for rank, st in enumerate(m["stages"], 1):
        if st["stage"] and st.get("hours_share") is not None:
            add(type="stage_hours_share", label=f"Stage {st['stage']}: share of active hours", value=st["hours_share"],
                unit="share", support=st["cases"], section="hours", source="stage hours ÷ hours in scope",
                meta={"rank": rank, "stage": st["stage"]})
    for rank, w in enumerate(m["waiting"]["rows"][:5], 1):
        meta = {"rank": rank, "src": w["from"], "dst": w["to"]}
        add(type="waiting_total", label=f"Total waiting from {w['from']} to {w['to']}", value=w["total_wait_h"],
            unit="work_hours", support=w["cases"], section="hours", source=f"sum over {w['count']:,} transitions",
            meta=meta)
        add(type="waiting_median", label=f"Median wait from {w['from']} to {w['to']}", value=w["median_wait_h"],
            unit="hours", support=w["cases"], section="hours", source="end of one step run to the start of the next",
            meta=meta)


def pm4py_rows(folder: Path) -> list[dict]:
    """The PM4Py numbers as fact rows, read from pm4py_conformance.json and pm4py_definition_check.json
    (report_checks.py recomputes the same list from the same files)."""
    from app.core import pm4py_model
    rows = []
    conf = pm4py_model.load_json(folder, pm4py_model.CONFORMANCE_FILE)
    if conf:
        n, dev = conf["cases_used"], conf.get("deviations") or {}
        rows += [dict(type="pm4py_fitness", label="Formal model: fitness (token replay)", value=conf["fitness"],
                      unit="ratio", support=n),
                 dict(type="pm4py_precision", label="Formal model: precision (token replay)", value=conf["precision"],
                      unit="ratio", support=conf.get("precision_cases") or n),
                 dict(type="pm4py_fitting_share", label="Formal model: cases that fit fully",
                      value=conf["fitting_cases_share"], unit="share", support=n)]
        m = dev.get("sampled_cases", 0)
        for kind, word in (("skipped", "skipped"), ("extra", "done as an extra step")):
            for rank, x in enumerate(dev.get(kind, [])[:5], 1):
                rows.append(dict(type=f"pm4py_{kind}", label=f"Formal model: {x['step']} {word}", value=x["share"],
                                 unit="share", support=m, meta={"rank": rank, "activity": x["step"]}))
    order = pm4py_model.load_json(folder, pm4py_model.ORDER_FILE)
    if order and order.get("available"):
        n, m = order["cases_used"], order["sampled_cases"]
        rows += [dict(type="pm4py_order_fitness", label="Step order: fitness (token replay)", value=order["fitness"],
                      unit="ratio", support=n),
                 dict(type="pm4py_order_precision", label="Step order: precision (token replay)",
                      value=order["precision"], unit="ratio", support=order.get("precision_cases") or n),
                 dict(type="pm4py_order_fitting_share", label="Step order: cases that follow it fully",
                      value=order["fitting_cases_share"], unit="share", support=n)]
        for rank, x in enumerate(order["most_out_of_order"], 1):
            rows.append(dict(type="pm4py_order_out_of_order", label=f"Step order: {x['step']} out of order",
                             value=x["out_of_order_share"], unit="share", support=m,
                             meta={"rank": rank, "activity": x["step"]}))
        for rank, x in enumerate(order.get("most_extra") or [], 1):
            rows.append(dict(type="pm4py_order_extra", label=f"Step order: {x['step']} repeated or extra",
                             value=x["extra_share"], unit="share", support=m, meta={"rank": rank, "activity": x["step"]}))
    chk = pm4py_model.load_json(folder, pm4py_model.DEFINITION_FILE)
    if chk and chk.get("available"):
        n, m = chk["cases_used"], chk["sampled_cases"]
        rows += [dict(type="pm4py_def_fitness", label="Approved process: fitness (token replay)", value=chk["fitness"],
                      unit="ratio", support=n),
                 dict(type="pm4py_def_precision", label="Approved process: precision (token replay)",
                      value=chk["precision"], unit="ratio", support=chk.get("precision_cases") or n),
                 dict(type="pm4py_def_fitting_share", label="Approved process: cases that follow it fully",
                      value=chk["fitting_cases_share"], unit="share", support=n)]
        for rank, x in enumerate(chk["most_skipped"], 1):
            rows.append(dict(type="pm4py_def_skipped", label=f"Approved process: {x['step']} skipped",
                             value=x["skipped_share"], unit="share", support=m, meta={"rank": rank, "activity": x["step"]}))
        for rank, x in enumerate(chk["most_out_of_order"], 1):
            rows.append(dict(type="pm4py_def_out_of_order", label=f"Approved process: {x['step']} at an unexpected point",
                             value=x["out_of_order_share"], unit="share", support=m,
                             meta={"rank": rank, "activity": x["step"]}))
    return rows


def evidence_rows(folder: Path) -> list[dict]:
    """The order-evidence numbers as fact rows (order_evidence.json; report_checks recomputes the shares)."""
    from app.core import order_evidence
    ev = order_evidence.load(folder)
    if not ev:
        return []
    rows = [dict(type="evidence_cases", label="Order evidence: fully observed cases used", value=ev["cases_used"],
                 unit="cases", support=ev["cases_used"])]
    for rank, p in enumerate(ev.get("pairs") or [], 1):
        if p["share"] is not None:
            rows.append(dict(type="evidence_pair", label=f"Order evidence: {p['a']} before {p['b']} ({p['verdict']})",
                             value=p["share"], unit="share", support=p["cases"],
                             meta={"rank": rank, "a": p["a"], "b": p["b"], "verdict": p["verdict"], "kind": p["kind"]}))
    return rows


def _pm4py_facts(add, folder: Path) -> None:
    for r in pm4py_rows(folder):
        add(section="pm4py", source="PM4Py (pm4py_conformance.json / pm4py_definition_check.json)", **r)


# --------------------------------------------------------------------------- #
# Formatting, saving, loading
# --------------------------------------------------------------------------- #
def _in_unit(h: float, factor: float, unit: str) -> str:
    if unit == "s":
        return f"{h * factor:.0f}"
    return f"{h * factor:.1f}" if unit == "min" else f"{h / factor:.1f}"


def format_fact(f: Fact) -> str:
    """The value as people read it: "4.2 days (3.9–4.6)", "45.2%", "10,907", "1,234.5 h" (work hours)."""
    from app.core import fmt
    if f.text is not None:
        return f"{f.text} ({f.value:.0f} / 100)" if f.value is not None else f.text
    if f.value is None:
        return "–"
    if f.unit == "work_hours":          # summed active time: always in hours
        return fmt.hours(f.value)
    if f.unit == "hours":
        out = format_hours(f.value)
        if f.ci_low is not None and f.ci_high is not None and f.value > 0:
            factor, unit = hours_unit(f.value)
            out += f" ({_in_unit(f.ci_low, factor, unit)}–{_in_unit(f.ci_high, factor, unit)})"
        return out
    if f.unit == "share":
        return f"{100 * f.value:.1f}%"
    if f.unit == "ratio":
        return f"{f.value:.2f}"
    if f.unit == "score":
        return f"{f.value:.0f} / 100"
    if float(f.value).is_integer():
        return f"{int(f.value):,}"
    return f"{f.value:,.1f}"


def trust_text(f: Fact) -> str:
    """Badge and support, shown next to every number: "High · 10,907 cases"."""
    return f"{f.badge} · {f.support or 0:,} cases"


def save_facts(facts: list[Fact], folder: Path) -> Path:
    path = folder / "facts.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([asdict(f) for f in facts], fh, indent=2, default=str)
    return path


def load_facts(folder: Path) -> list[Fact]:
    with open(folder / "facts.json", encoding="utf-8") as fh:
        return [Fact(**d) for d in json.load(fh)]
