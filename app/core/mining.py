"""Day 3: the mining engine. Everything the process map and the report need.

Gist
----
1. `build_log`   prepares three working tables from events.parquet:
                     mlog    the analysable events, each with its position `seq` in its case
                     mcases  one row per case (duration, first/last step, path fingerprint)
                     mtrans  one row per step-to-step transition inside a case
2. The analyses read those tables: `kpis`, `dfg` (the process map), `variants`,
   `bottlenecks`, `rework`, `resources`, `trends`.
3. `run_mining`  runs the analyses the quality band allows and says, for every
   section it does not show, why ("Not available: ...").
4. Map helpers (`simplify`, `to_dot`, `format_hours`) are plain Python, used by
   the map screen and the report.

All heavy work runs in DuckDB SQL, so it works on 10M+ events. All durations
are in HOURS (float, 2 decimals). Every result carries its support (number of
cases), and results with fewer than `mining.min_support_cases` cases are
flagged `low_support`.
"""
from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from app.core.config import settings
from app.core.db import quote_ident, sql_literal
from app.core.quality import FILE_COLUMN, analysable_where, utc_now

SECTIONS = ("kpis", "map", "variants", "bottlenecks", "rework", "resources", "applications", "app_switching",
            "trends")


def _h(value) -> float | None:
    """An hours value as a float at full precision (6 decimals = 4 ms); None stays None.
    Rounding for display happens only in fmt.py (a 2-decimal round turned 30 s into 0)."""
    return None if value is None else round(float(value), 6)


def _iso(value) -> str | None:
    return None if value is None else value.isoformat()


# --------------------------------------------------------------------------- #
# 1. Working tables
# --------------------------------------------------------------------------- #
def build_log(con: duckdb.DuckDBPyConnection, events_path: Path) -> dict:
    """Create the TEMP tables mlog, mcases and mtrans and return basic counts.

    mlog    the events the quality gate calls analysable (same filter, see
            quality.analysable_where), exact duplicates removed (ignoring the
            source file name, so overlapping exports count once), plus
            seq = position in the case (by time, then the row order in the file, so
            every run gives the same order).
    mcases  one row per case: n_events, start_ts, end_ts, duration_h,
            start_activity, end_activity and variant_key (a fingerprint of the
            path: same steps in the same order give the same number).
    mtrans  one row per transition: an event (src) and the next event of the
            same case (dst), the one after that (dst2, for ping-pong), both
            resources and the raw wait in hours (negative when steps overlap).
            Built once with window functions, so the analyses never join
            10M events with themselves again.
    """
    path = sql_literal(events_path.as_posix())
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({path})").fetchall()]
    attrs = "".join(f", {quote_ident(c)}" for c in cols if c.startswith("attr_") and c != FILE_COLUMN)
    # Files converted before "merge repeated steps" existed have no merged_events column.
    merged = "merged_events" if "merged_events" in cols else "1 AS merged_events"
    # Files converted before application mapping existed have no application column.
    application = "application" if "application" in cols else "CAST(NULL AS VARCHAR) AS application"
    stage = "stage" if "stage" in cols else "CAST(NULL AS VARCHAR) AS stage"   # older files have none
    base = f"case_id, activity, ts_start, ts_end, resource, {application}, {stage}, {merged}{attrs}"
    # Ties in time are broken by the row order in the file (src_row, written by ingest.normalise);
    # files converted before it existed fall back to the activity name.
    tie = "activity"
    if FILE_COLUMN in cols:
        # The source file is not part of the event: the same event in two overlapping
        # exports is kept once, with the first file name (alphabetically).
        row = ", MIN(src_row) AS src_row" if "src_row" in cols else ""
        tie = "src_row, activity" if row else tie
        unique = f"SELECT {base}, MIN({FILE_COLUMN}) AS {FILE_COLUMN}{row}"
        group = "GROUP BY ALL"
    else:
        unique, group = f"SELECT DISTINCT {base}", ""
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE mlog AS
        SELECT *, row_number() OVER (PARTITION BY case_id ORDER BY ts_start, {tie}) AS seq
        FROM ({unique}
              FROM read_parquet({path})
              WHERE {analysable_where(utc_now())}
              {group})
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE mcases AS
        SELECT case_id,
               COUNT(*)                                        AS n_events,
               MIN(ts_start)                                   AS start_ts,
               MAX(COALESCE(ts_end, ts_start))                 AS end_ts,
               (epoch(MAX(COALESCE(ts_end, ts_start))) - epoch(MIN(ts_start))) / 3600.0 AS duration_h,
               min_by(activity, seq)                           AS start_activity,
               max_by(activity, seq)                           AS end_activity,
               SUM(CAST(hash(activity, seq) AS HUGEINT))       AS variant_key
        FROM mlog GROUP BY case_id
    """)
    from app.core.stable import build_case_keys
    build_case_keys(con, "mlog")          # content keys for bootstrap samples and half-samples (stable.py)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE mtrans AS
        SELECT * FROM (
            SELECT case_id, seq,
                   activity                   AS src,
                   lead(activity) OVER w      AS dst,
                   lead(activity, 2) OVER w   AS dst2,
                   resource                   AS src_resource,
                   lead(resource) OVER w      AS dst_resource,
                   (epoch(lead(ts_start) OVER w) - epoch(COALESCE(ts_end, ts_start))) / 3600.0 AS raw_wait_h
            FROM mlog
            WINDOW w AS (PARTITION BY case_id ORDER BY seq)
        ) WHERE dst IS NOT NULL
    """)
    events, has_end = con.execute(
        "SELECT COUNT(*), COALESCE(bool_or(ts_end IS NOT NULL AND ts_end <> ts_start), false) FROM mlog"
    ).fetchone()
    cases, = con.execute("SELECT COUNT(*) FROM mcases").fetchone()
    transitions, = con.execute("SELECT COUNT(*) FROM mtrans").fetchone()
    return {"events": events, "cases": cases, "transitions": transitions, "has_end_times": bool(has_end)}


def _has_end_times(con) -> bool:
    return bool(con.execute(
        "SELECT COALESCE(bool_or(ts_end IS NOT NULL AND ts_end <> ts_start), false) FROM mlog"
    ).fetchone()[0])


# --------------------------------------------------------------------------- #
# 2. Analyses (call build_log first)
# --------------------------------------------------------------------------- #
def kpis(con: duckdb.DuckDBPyConnection) -> dict:
    """Headline numbers: volume, period and case duration (hours)."""
    events, activities, resources, first, last = con.execute("""
        SELECT COUNT(*), COUNT(DISTINCT activity), COUNT(DISTINCT resource),
               MIN(ts_start), MAX(ts_start)
        FROM mlog""").fetchone()
    cases, median_h, p90_h, mean_h, epc = con.execute("""
        SELECT COUNT(*), median(duration_h), quantile_cont(duration_h, 0.9), AVG(duration_h),
               median(n_events)
        FROM mcases""").fetchone()
    return {
        "cases": cases, "events": events, "activities": activities, "resources": resources,
        "first_event": _iso(first), "last_event": _iso(last),
        "median_h": _h(median_h), "p90_h": _h(p90_h), "mean_h": _h(mean_h),
        "events_per_case_median": None if epc is None else float(epc),
        "has_end_times": _has_end_times(con),
    }


def dfg(con: duckdb.DuckDBPyConnection) -> dict:
    """The directly-follows graph: boxes (activities) and arrows (one step to the next).

    An arrow A -> B counts how often B directly follows A in the same case.
    Its wait is B's start minus A's end (A's start when there is no end).
    Negative waits (overlapping steps) count as 0; how many there were is
    returned as `overlaps`. Artificial start and end nodes connect each case's
    first and last activity, so the map shows where cases begin and finish.
    """
    cfg = settings()["mining"]
    start, end = cfg["start_node"], cfg["end_node"]
    has_end = _has_end_times(con)
    n_cases, = con.execute("SELECT COUNT(*) FROM mcases").fetchone()
    processing = ("median((epoch(ts_end) - epoch(ts_start)) / 3600.0) FILTER (WHERE ts_end IS NOT NULL)"
                  if has_end else "NULL")
    nodes = [{"activity": a, "events": e, "cases": c, "median_processing_h": _h(p)}
             for a, e, c, p in con.execute(f"""
                 SELECT activity, COUNT(*), COUNT(DISTINCT case_id), {processing}
                 FROM mlog GROUP BY activity ORDER BY COUNT(*) DESC, activity""").fetchall()]
    special = {"events": n_cases, "cases": n_cases, "median_processing_h": None}
    nodes = [{"activity": start, **special}] + nodes + [{"activity": end, **special}]

    # overlap_share: how often B started before A ended (work in parallel); those waits count as 0.
    edges = [{"src": s, "dst": d, "count": n, "cases": c,
              "median_wait_h": _h(med), "p90_wait_h": _h(p90), "mean_wait_h": _h(mean),
              "overlap_share": None if ov is None else round(float(ov), 4)}
             for s, d, n, c, med, p90, mean, ov in con.execute("""
                 SELECT src, dst, COUNT(*), COUNT(DISTINCT case_id),
                        median(GREATEST(raw_wait_h, 0)), quantile_cont(GREATEST(raw_wait_h, 0), 0.9),
                        AVG(GREATEST(raw_wait_h, 0)), AVG(CASE WHEN raw_wait_h < 0 THEN 1.0 ELSE 0.0 END)
                 FROM mtrans GROUP BY src, dst ORDER BY COUNT(*) DESC, src, dst""").fetchall()]
    no_wait = {"median_wait_h": None, "p90_wait_h": None, "mean_wait_h": None}
    edges += [{"src": start, "dst": a, "count": n, "cases": n, **no_wait}
              for a, n in con.execute("""SELECT start_activity, COUNT(*) FROM mcases
                                         GROUP BY 1 ORDER BY 2 DESC, 1""").fetchall()]
    edges += [{"src": a, "dst": end, "count": n, "cases": n, **no_wait}
              for a, n in con.execute("""SELECT end_activity, COUNT(*) FROM mcases
                                         GROUP BY 1 ORDER BY 2 DESC, 1""").fetchall()]
    overlaps, = con.execute("SELECT COUNT(*) FILTER (WHERE raw_wait_h < 0) FROM mtrans").fetchone()
    return {"nodes": nodes, "edges": edges, "overlaps": overlaps, "cases": n_cases,
            "has_end_times": has_end, "start_node": start, "end_node": end}


def variants(con: duckdb.DuckDBPyConnection, top: int) -> dict:
    """The most frequent paths (variants), most frequent first.

    Cases are grouped by their path fingerprint. The readable path is read
    from ONE representative case per variant, so only `top` cases are turned
    into lists of activities, never all cases.
    """
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE mvariants AS
        SELECT * FROM (
            SELECT variant_key, COUNT(*) AS n, any_value(case_id) AS rep, median(duration_h) AS med,
                   row_number() OVER (ORDER BY COUNT(*) DESC, variant_key) AS rk
            FROM mcases GROUP BY variant_key)
    """)
    total_variants, total_cases, top10 = con.execute("""
        SELECT COUNT(*), COALESCE(SUM(n), 0), COALESCE(SUM(n) FILTER (WHERE rk <= 10), 0)
        FROM mvariants""").fetchone()
    rows = con.execute(f"""
        SELECT v.rk, v.n, v.med, list(m.activity ORDER BY m.seq)
        FROM mvariants v JOIN mlog m ON m.case_id = v.rep
        WHERE v.rk <= {int(top)}
        GROUP BY v.rk, v.n, v.med ORDER BY v.rk""").fetchall()
    out, cumulative = [], 0
    for rank, n, med, path in rows:
        cumulative += n
        out.append({"rank": rank, "cases": n, "share": round(n / total_cases, 4),
                    "cumulative_share": round(cumulative / total_cases, 4),
                    "path": list(path), "median_duration_h": _h(med)})
    return {"variants": out, "total_variants": total_variants, "cases": total_cases,
            "coverage_top10": round(top10 / total_cases, 4) if total_cases else 0.0}


def bottlenecks(dfg_result: dict, min_support: int, top: int) -> dict:
    """The slowest arrows (by median wait) and, when end times exist, the slowest steps.

    Only results backed by at least `min_support` cases are ranked, so a slow
    arrow seen in three cases cannot top the list. Start and end arrows have
    no wait and are skipped.
    """
    special = {dfg_result["start_node"], dfg_result["end_node"]}
    real = [e for e in dfg_result["edges"]
            if e["src"] not in special and e["dst"] not in special and e["median_wait_h"] is not None]
    supported = [e for e in real if e["cases"] >= min_support]
    transitions = [{**e, "low_support": False}
                   for e in sorted(supported, key=lambda e: (-e["median_wait_h"], e["src"], e["dst"]))[:top]]
    result = {"transitions": transitions, "skipped_low_support": len(real) - len(supported)}
    if dfg_result["has_end_times"]:
        acts = [n for n in dfg_result["nodes"] if n["activity"] not in special
                and n["median_processing_h"] is not None and n["cases"] >= min_support]
        result["activities"] = [{**n, "low_support": False} for n in
                                sorted(acts, key=lambda n: (-n["median_processing_h"], n["activity"]))[:top]]
        result["activities_reason"] = None
    else:
        result["activities"] = []
        result["activities_reason"] = ("Not available: no end times were recorded, so the time "
                                       "spent inside each step cannot be measured.")
    return result


def rework(con: duckdb.DuckDBPyConnection, min_support: int, top: int) -> dict:
    """Steps done more than once in a case, direct repeats, and back-and-forth (A -> B -> A)."""
    activities = [{"activity": a, "cases": rc, "share": round(rc / c, 4), "extra_occurrences": extra,
                   "of_cases": c, "low_support": rc < min_support}
                  for a, rc, c, extra in con.execute(f"""
                      WITH per AS (SELECT activity, case_id, COUNT(*) AS n FROM mlog GROUP BY 1, 2)
                      SELECT activity, COUNT(*) FILTER (WHERE n > 1), COUNT(*), SUM(n - 1)
                      FROM per GROUP BY activity
                      HAVING COUNT(*) FILTER (WHERE n > 1) > 0
                      ORDER BY 2 DESC, activity LIMIT {int(top)}""").fetchall()]
    self_loops, = con.execute("SELECT COUNT(*) FILTER (WHERE src = dst) FROM mtrans").fetchone()
    ping_pong = [{"a": a, "b": b, "count": n, "cases": c, "low_support": c < min_support}
                 for a, b, n, c in con.execute(f"""
                     SELECT src, dst, COUNT(*), COUNT(DISTINCT case_id) FROM mtrans
                     WHERE dst2 = src AND dst <> src
                     GROUP BY src, dst ORDER BY 3 DESC, src, dst LIMIT {int(top)}""").fetchall()]
    return {"activities": activities, "self_loops": self_loops, "ping_pong": ping_pong}


def _app_units(con: duckdb.DuckDBPyConnection, folder: Path | None) -> None:
    """TEMP view mapps(case_id, activity, application, n): events per case, step and application.

    After "Merge repeated steps" a merged row keeps only its first event's application, so the events of a
    run per application come from events_apps.parquet (ingest.merged_apps_sql); otherwise from mlog."""
    from app.core.ingest import APPS_FILE
    path = Path(folder) / APPS_FILE if folder else None
    if path is not None and path.exists():
        con.execute(f"""CREATE OR REPLACE TEMP VIEW mapps AS
                        SELECT case_id, activity, application, events AS n FROM read_parquet({sql_literal(path.as_posix())})
                        WHERE {analysable_where(utc_now())}""")
    else:
        con.execute("CREATE OR REPLACE TEMP VIEW mapps AS SELECT case_id, activity, application, merged_events AS n "
                    "FROM mlog")


def applications(con: duckdb.DuckDBPyConnection, folder: Path | None = None) -> dict:
    """Events and cases per application (names already resolved by the application map).

    Two recorded values mapped to the same real name are one application here.
    The category comes from the shared application map.
    """
    from app.core.apps import category_of, load_map  # local import avoids a cycle

    _app_units(con, folder)
    n_apps, total = con.execute("SELECT COUNT(DISTINCT application), SUM(n) FROM mapps").fetchone()
    if not n_apps:
        return {"available": False, "reason": "No application column was mapped."}
    the_map = load_map()
    label = settings()["apps"]["unnamed_label"]
    rows = [{"application": a, "category": category_of(a, the_map), "events": e, "cases": c,
             "share": round(e / total, 4) if total else 0.0}
            for a, e, c in con.execute("""
                SELECT application, SUM(n), COUNT(DISTINCT case_id) FROM mapps
                WHERE application IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1""").fetchall()]
    return {"available": True, "reason": None, "total_applications": n_apps, "applications": rows,
            "unnamed_masked": sum(1 for r in rows if r["application"].startswith(f"{label} ")
                                  or r["application"] == label)}


def app_switching(con: duckdb.DuckDBPyConnection, job_folder: Path) -> dict:
    """How often people switch application, between which, and in which steps.

    Switches come from app_switches.parquet (written by ingest.normalise on the rows
    in scope, BEFORE repeated steps are merged); steps and applications from mlog.
    Every count carries its support (cases). Needs build_log first.
    """
    from app.core.apps import category_of, load_map   # local import avoids a cycle
    from app.core.ingest import SWITCHES_FILE

    cfg, support = settings()["app_switching"], settings()["mining"]["min_support_cases"]
    path = Path(job_folder) / SWITCHES_FILE
    if not path.exists():
        has_apps = con.execute("SELECT COUNT(application) FROM mlog").fetchall()[0][0] > 0
        return {"available": False, "reason": (
            "Re-run 'Validate and convert' on Import data to count application switches." if has_apps
            else "No application column was mapped.")}
    con.execute(f"CREATE OR REPLACE TEMP VIEW msw AS SELECT * FROM read_parquet({sql_literal(path.as_posix())})")
    _app_units(con, job_folder)
    top = int(cfg["top_pairs"])
    gap = float(cfg["session_gap_minutes"]) * 60
    # Switches at the same time follow the file's row order (src_row); files written before it was kept fall
    # back to the switch itself. Never the (salted) case ID.
    has_row = any(r[0] == "src_row" for r in con.execute("DESCRIBE msw").fetchall())
    tie = "src_row" if has_row else "activity, from_app, to_app, gap_seconds"

    total, switch_cases = con.execute("SELECT COUNT(*), COUNT(DISTINCT case_id) FROM msw").fetchall()[0]
    n_cases, events_before_merge, n_apps = con.execute(
        "SELECT COUNT(DISTINCT case_id), SUM(merged_events), COUNT(DISTINCT application) FROM mlog").fetchall()[0]
    median, p90, mean = con.execute("""
        SELECT median(n), quantile_cont(n, 0.9), AVG(n) FROM (
            SELECT c.case_id, COUNT(s.case_id) AS n FROM mcases c LEFT JOIN msw s USING (case_id)
            GROUP BY c.case_id)""").fetchall()[0]
    kpis = {"total": total, "per_case_median": None if median is None else float(median),
            "per_case_p90": None if p90 is None else round(float(p90), 2),
            "per_case_mean": None if mean is None else round(float(mean), 2),
            "per_100_events": round(100 * total / events_before_merge, 2) if events_before_merge else None,
            "cases_with_switch": switch_cases, "cases": n_cases,
            "share_cases_with_switch": round(switch_cases / n_cases, 4) if n_cases else 0.0,
            "applications": n_apps, "session_gap_minutes": cfg["session_gap_minutes"]}

    pairs = [{"from": a, "to": b, "count": n, "cases": c, "median_gap_h": _h(g), "median_gap": format_hours(_h(g)),
              "low_support": c < support}
             for a, b, n, c, g in con.execute(f"""
                 SELECT from_app, to_app, COUNT(*), COUNT(DISTINCT case_id), median(gap_seconds) / 3600.0
                 FROM msw GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT {top}""").fetchall()]

    # A -> B followed by B -> A as the next switch of the same case and user, within the session gap.
    ping_pong = [{"a": a, "b": b, "count": n, "cases": c, "low_support": c < support}
                 for a, b, n, c in con.execute(f"""
                     SELECT from_app, to_app, COUNT(*), COUNT(DISTINCT case_id) FROM (
                         SELECT case_id, from_app, to_app, ts,
                                lead(from_app) OVER w AS next_from, lead(to_app) OVER w AS next_to,
                                lead(ts) OVER w AS next_ts
                         FROM msw WINDOW w AS (PARTITION BY case_id, resource ORDER BY ts, {tie}))
                     WHERE next_from = to_app AND next_to = from_app AND epoch(next_ts) - epoch(ts) <= {gap}
                     GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT {top}""").fetchall()]

    step_apps: dict[str, list[tuple[str, int]]] = {}
    for act, app, n in con.execute("""
            SELECT activity, application, SUM(n) FROM mapps WHERE application IS NOT NULL
            GROUP BY 1, 2 ORDER BY 1, 3 DESC, 2""").fetchall():
        step_apps.setdefault(act, []).append((app, n))
    by_step = []
    for act, switches, cases, occurrences in con.execute("""
            SELECT s.activity, s.switches, s.cases, o.n FROM
                (SELECT activity, COUNT(*) AS switches, COUNT(DISTINCT case_id) AS cases FROM msw GROUP BY 1) s
            JOIN (SELECT activity, COUNT(*) AS n FROM mlog GROUP BY 1) o USING (activity)""").fetchall():
        by_step.append({"activity": act, "switches": switches, "occurrences": occurrences,
                        "per_occurrence": round(switches / occurrences, 3) if occurrences else None,
                        "cases": cases, "top_apps": [a for a, _ in step_apps.get(act, [])[:2]],
                        "low_support": cases < support})
    by_step.sort(key=lambda r: (-(r["per_occurrence"] or 0), -r["switches"], r["activity"]))

    min_share = float(cfg["min_step_app_share"])
    steps_by_app = []
    for act, apps_n in step_apps.items():
        n = sum(c for _, c in apps_n)
        shares = {a: round(c / n, 4) for a, c in apps_n}
        steps_by_app.append({"activity": act, "events": n, "shares": shares,
                             "apps_used": sum(1 for s in shares.values() if s >= min_share)})
    steps_by_app.sort(key=lambda r: (-r["events"], r["activity"]))

    the_map = load_map()
    nodes = [{"application": a, "category": category_of(a, the_map), "events": n}
             for a, n in con.execute(f"""
                 SELECT application, SUM(n) FROM mapps WHERE application IS NOT NULL
                 GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {int(cfg['map_max_apps'])}""").fetchall()]
    shown = {x["application"] for x in nodes}
    edges = [{"src": a, "dst": b, "count": n}
             for a, b, n in con.execute("SELECT from_app, to_app, COUNT(*) FROM msw GROUP BY 1, 2 ORDER BY 3 DESC")
             .fetchall() if a in shown and b in shown]
    return {"available": True, "reason": None, "kpis": kpis, "pairs": pairs, "ping_pong": ping_pong,
            "by_step": by_step, "steps_by_app": steps_by_app, "app_nodes": nodes, "app_edges": edges}


def describe_switching(data: dict) -> str:
    """One plain sentence from fixed templates; every number comes from `data`."""
    k = data["kpis"]
    if not k["total"]:
        return "No application switches were found within a work session."
    median = k["per_case_median"] or 0
    parts = [f"People switch application about {median:g} times per case"]
    if data["pairs"]:
        p = data["pairs"][0]
        parts[0] += f"; the most common switch is {p['from']} → {p['to']} ({p['count']:,} times)."
    else:
        parts[0] += "."
    if data["by_step"]:
        s = data["by_step"][0]
        parts.append(f"{s['activity']} has the most switching: {s['per_occurrence']:.1f} switches each time "
                     "it is done.")
    return " ".join(parts)


def to_app_dot(nodes: list[dict], edges: list[dict]) -> str:
    """Graphviz DOT of the switch map: applications as boxes, switches as arrows (both ways drawn)."""
    max_count = max((e["count"] for e in edges), default=1)
    legend = "Box = application · arrow = switch from → to · number = times"
    lines = ["digraph switches {", "  rankdir=LR;",
             '  node [shape=box, style="rounded,filled", fillcolor="#eef3fb", color="#4a6fa5", '
             'fontname="Helvetica", fontsize=11];',
             '  edge [color="#666666", fontname="Helvetica", fontsize=9];',
             f'  label="{_dot_text(legend)}"; labelloc=b; fontname="Helvetica"; fontsize=10; fontcolor="#616e7c";']
    for n in nodes:
        detail = " · ".join(x for x in (n.get("category") or "", f"{n['events']:,} events") if x)
        lines.append(f'  "{_dot_text(n["application"])}" [label="{_dot_text(n["application"])}\\n{_dot_text(detail)}"];')
    for e in edges:
        width = 1 + 5 * (math.log(e["count"]) / math.log(max_count)) if max_count > 1 and e["count"] > 0 else 1
        lines.append(f'  "{_dot_text(e["src"])}" -> "{_dot_text(e["dst"])}" '
                     f'[label="{e["count"]:,}", penwidth={width:.2f}];')
    lines.append("}")
    return "\n".join(lines)


def resources(con: duckdb.DuckDBPyConnection, top: int) -> dict:
    """Who does the work, and how often work passes from one person/system to another."""
    n_resources, = con.execute("SELECT COUNT(DISTINCT resource) FROM mlog").fetchone()
    if not n_resources:
        return {"available": False, "reason": "No resource/user column was mapped."}
    processing = ("median((epoch(ts_end) - epoch(ts_start)) / 3600.0) FILTER (WHERE ts_end IS NOT NULL)"
                  if _has_end_times(con) else "NULL")
    per_resource = [{"resource": r, "events": e, "cases": c, "median_processing_h": _h(p)}
                    for r, e, c, p in con.execute(f"""
                        SELECT resource, COUNT(*), COUNT(DISTINCT case_id), {processing}
                        FROM mlog WHERE resource IS NOT NULL
                        GROUP BY resource ORDER BY 2 DESC, resource""").fetchall()]
    total_handovers, = con.execute(
        "SELECT COUNT(*) FROM mtrans WHERE src_resource <> dst_resource").fetchone()
    handovers = [{"from": a, "to": b, "count": n, "cases": c}
                 for a, b, n, c in con.execute(f"""
                     SELECT src_resource, dst_resource, COUNT(*), COUNT(DISTINCT case_id)
                     FROM mtrans WHERE src_resource <> dst_resource
                     GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT {int(top)}""").fetchall()]
    return {"available": True, "reason": None, "total_resources": n_resources,
            "resources": per_resource, "total_handovers": total_handovers, "handovers": handovers}


def trends(con: duckdb.DuckDBPyConnection, min_support: int | None = None) -> dict:
    """Cases started per calendar month and their median duration (hours)."""
    if min_support is None:
        min_support = settings()["mining"]["min_support_cases"]
    months = [{"month": m.strftime("%Y-%m"), "cases": n, "median_duration_h": _h(med),
               "low_support": n < min_support}
              for m, n, med in con.execute("""
                  SELECT date_trunc('month', start_ts) AS m, COUNT(*), median(duration_h)
                  FROM mcases GROUP BY m ORDER BY m""").fetchall()]
    return {"months": months}


# --------------------------------------------------------------------------- #
# 3. Everything the band allows
# --------------------------------------------------------------------------- #
def run_mining(con: duckdb.DuckDBPyConnection, events_path: Path, band: str, use_ai: bool = True,
               progress=None) -> dict:
    """Run the analyses the quality band allows; say why the others are missing.

    Every section is {"available", "reason", "data"}. For "Not feasible" nothing
    heavy is computed: only the number of rows in the file is read.
    """
    start = time.perf_counter()
    cfg = settings()["mining"]
    rules = cfg["band_rules"]
    if band not in rules:
        raise ValueError(f"Unknown quality band '{band}'. Expected one of: {', '.join(rules)}.")
    allowed = set(rules[band]["allowed"])
    blocked = f"Not available: data quality is {band}."
    sections = {k: {"available": False, "reason": blocked, "data": None} for k in SECTIONS}

    from app.core.derived import NO_CASE_REASON, case_kind, mapping_of   # local import avoids a cycle
    if case_kind(mapping_of(Path(events_path).parent)) == "overview":
        return _overview_mining(con, Path(events_path), band, rules[band]["label"], start, NO_CASE_REASON)

    if not allowed:
        rows, = con.execute(
            f"SELECT COUNT(*) FROM read_parquet({sql_literal(events_path.as_posix())})").fetchone()
        counts = {"rows_in_file": rows}
    else:
        counts = build_log(con, events_path)
        support = cfg["min_support_cases"]
        graph = dfg(con) if allowed & {"map", "bottlenecks"} else None
        compute = {
            "kpis": lambda: kpis(con),
            "map": lambda: graph,
            "variants": lambda: variants(con, cfg["top_variants"]),
            "bottlenecks": lambda: bottlenecks(graph, support, cfg["top_bottlenecks"]),
            "rework": lambda: rework(con, support, cfg["top_rework"]),
            "resources": lambda: resources(con, cfg["top_handovers"]),
            "applications": lambda: applications(con, Path(events_path).parent),
            "app_switching": lambda: app_switching(con, Path(events_path).parent),
            "trends": lambda: trends(con, support),
        }
        for key in SECTIONS:
            if key not in allowed:
                continue
            data = compute[key]()
            if key in ("resources", "applications", "app_switching") and not data["available"]:
                sections[key] = {"available": False, "reason": f"Not available: {data['reason']}",
                                 "data": None}
            else:
                sections[key] = {"available": True, "reason": None, "data": data}

    # Flow confidence (a separate score from data quality): is there a reliable order?
    flow = {"available": False, "reason": blocked, "data": None}
    sections["subprocess"] = {"available": False, "reason": blocked, "data": None}
    sections["level4"] = {"available": False, "reason": blocked, "data": None}
    sections["process_model"] = {"available": False, "reason": blocked, "data": None}
    sections["inside"] = {"available": False, "reason": blocked, "data": None}
    if "map" in allowed:
        from app.core.milestones import analyse, atomic_json
        from app.core.reference import order_key
        meta_path = events_path.parent / "job.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {"job_id": events_path.parent.name}
        milestone_data = analyse(con, events_path, order_key(meta))
        sections["milestones"] = {"available": milestone_data is not None, "reason": None,
                                  "data": milestone_data}
        atomic_json(events_path.parent / "milestones.json", milestone_data or {})
        from app.core.subprocess import analyse as analyse_steps
        sub = analyse_steps(con, Path(events_path), order_key(meta))
        sections["subprocess"] = {"available": sub["available"], "reason": sub["reason"],
                                  "data": sub if sub["available"] else {"hierarchy": sub["hierarchy"]}}
        from app.core.level4 import analyse as analyse_level4
        l4 = analyse_level4(con, Path(events_path), order_key(meta), use_ai=use_ai, progress=progress)
        sections["level4"] = {"available": l4["available"], "reason": l4["reason"],
                              "data": l4 if l4["available"] else None}
        sections["process_model"] = _process_model(con, Path(events_path), progress)
        from app.core import inside_step
        try:
            ins = inside_step.analyse(con, Path(events_path), order_key(meta), use_ai=use_ai, progress=progress)
        except Exception as err:  # noqa: BLE001 - an add-on: never fails the analysis
            logging.getLogger(__name__).exception("Inside each step failed")
            ins = {"available": False, "reason": f"Not available: inside-step analysis failed ({type(err).__name__})."}
        sections["inside"] = {"available": ins["available"], "reason": ins["reason"],
                              "data": {"steps": list((ins.get("steps") or {}))} if ins["available"] else None}
        flow = {"available": True, "reason": None, "data": _flow_for(con, Path(events_path))}

    return {
        "band": band,
        "band_label": rules[band]["label"],
        "counts": counts,
        **sections,
        "flow_confidence": flow,
        "seconds": round(time.perf_counter() - start, 2),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _process_model(con: duckdb.DuckDBPyConnection, events_path: Path, progress=None) -> dict:
    """The optional PM4Py model (pm4py_model.py). Any PM4Py problem leaves the rest of the analysis intact."""
    from app.core import pm4py_model
    if not pm4py_model.status()[0]:
        return {"available": False, "reason": pm4py_model.status()[1], "data": None}
    if not pm4py_model._cfg().get("build_on_run", True):
        return {"available": False, "data": None, "build_button": True,
                "reason": "Not built yet: click Build model (pm4py.build_on_run is false in config/settings.yaml)."}
    if progress:
        progress("Discovering the process model (PM4Py)")
    try:
        from app.core.db import connect
        model = pm4py_model.discover(connect(), events_path)       # own connection: its own working tables
    except Exception as err:  # noqa: BLE001 - optional add-on: report, never fail the analysis
        import logging
        logging.getLogger(__name__).exception("PM4Py could not build the model")
        return {"available": False, "reason": f"Not available: PM4Py could not build the model "
                                              f"({type(err).__name__}: {str(err)[:200]}).",
                "data": None}
    return {"available": model["available"], "reason": model["reason"], "data": model if model["available"] else None}


def activity_overview(con: duckdb.DuckDBPyConnection) -> dict:
    """What can be said without cases: activity counts, when work happens, and step runs.

    Used in "activity overview only" mode, where each user's timeline stands in for a
    case. A step run = consecutive events of the same activity by one user, with no
    pause longer than `derived_case.session_gap_minutes`. Needs build_log first.
    """
    gap = float(settings()["derived_case"]["session_gap_minutes"]) * 60
    activities = [{"activity": a, "events": n, "users": u} for a, n, u in con.execute("""
        SELECT activity, COUNT(*), COUNT(DISTINCT resource) FROM mlog GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 30""").fetchall()]
    by_hour = [{"hour": int(h), "events": n} for h, n in con.execute(
        "SELECT hour(ts_start), COUNT(*) FROM mlog GROUP BY 1 ORDER BY 1").fetchall()]
    by_weekday = [{"weekday": d, "events": n} for _, d, n in con.execute(
        "SELECT isodow(ts_start), dayname(ts_start), COUNT(*) FROM mlog GROUP BY 1, 2 ORDER BY 1").fetchall()]
    by_month = [{"month": m, "events": n} for m, n in con.execute(
        "SELECT strftime(date_trunc('month', ts_start), '%Y-%m'), COUNT(*) FROM mlog GROUP BY 1 ORDER BY 1").fetchall()]
    runs = [{"activity": a, "runs": r, "median_events": float(e or 0), "median_minutes": _h(m)}
            for a, r, e, m in con.execute(f"""
                WITH w AS (SELECT *, CASE WHEN lag(activity) OVER p IS DISTINCT FROM activity
                                            OR epoch(ts_start) - epoch(lag(COALESCE(ts_end, ts_start)) OVER p) > {gap}
                                          THEN 1 ELSE 0 END AS new_run
                           FROM mlog WINDOW p AS (PARTITION BY case_id ORDER BY seq)),
                     r AS (SELECT *, SUM(new_run) OVER (PARTITION BY case_id ORDER BY seq ROWS UNBOUNDED PRECEDING)
                                     AS run FROM w),
                     g AS (SELECT case_id, run, any_value(activity) AS activity, COUNT(*) AS n,
                                  (epoch(MAX(COALESCE(ts_end, ts_start))) - epoch(MIN(ts_start))) / 60.0 AS minutes
                           FROM r GROUP BY 1, 2)
                SELECT activity, COUNT(*), median(n), median(minutes) FROM g GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 30""")
            .fetchall()]
    events, users = con.execute("SELECT COUNT(*), COUNT(DISTINCT resource) FROM mlog").fetchall()[0]
    return {"events": events, "users": users, "activities": activities, "by_hour": by_hour,
            "by_weekday": by_weekday, "by_month": by_month, "step_runs": runs}


def _overview_mining(con: duckdb.DuckDBPyConnection, events_path: Path, band: str, label: str, start: float,
                     no_case: str) -> dict:
    """"Activity overview only" (no case): everything that needs a case says so; the rest is computed.

    Each user's timeline stands in for a case, so applications and application
    switches still work. These descriptive counts are shown whatever the quality band.
    """
    from app.core.flow_confidence import ALLOWED   # local import avoids a cycle
    counts = build_log(con, events_path)
    sections = {k: {"available": False, "reason": no_case, "data": None} for k in SECTIONS}
    for key, fn in (("resources", lambda: resources(con, settings()["mining"]["top_handovers"])),
                    ("applications", lambda: applications(con, events_path.parent)),
                    ("app_switching", lambda: app_switching(con, events_path.parent))):
        data = fn()
        sections[key] = ({"available": False, "reason": f"Not available: {data['reason']}", "data": None}
                         if not data.get("available", True) else {"available": True, "reason": None, "data": data})
    flow = {"score": 0, "band": "None", "source": "data", "reasons": ["no case: activity overview only"],
            "allowed": dict(ALLOWED["None"]), "source_badge": "No case"}
    return {"band": band, "band_label": label, "counts": counts, **sections,
            "overview": {"available": True, "reason": None, "data": activity_overview(con)},
            "flow_confidence": {"available": True, "reason": None, "data": flow},
            "seconds": round(time.perf_counter() - start, 2),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _flow_for(con: duckdb.DuckDBPyConnection, events_path: Path) -> dict:
    """The flow confidence for the order the job uses now (saved for its layout, or suggested).

    Uses the working tables already built on `con`. The job's layout signature and
    name come from job.json next to events.parquet, when there is one.
    """
    from app.core import reference   # local import avoids a cycle (reference uses this module)

    job: dict = {}
    try:
        with open(events_path.parent / "job.json", encoding="utf-8") as f:
            job = json.load(f)
    except (OSError, ValueError):
        pass
    name = Path(job.get("source_name") or "Process").stem or "Process"
    if not job.get("job_id"):
        job = {**job, "job_id": events_path.parent.name}
    ref = reference.compute_for_job(con, events_path, job, name, log_built=True)   # the order page 3 and the report use
    return {**ref["flow"], "source_badge": ref["source_badge"]}


def save_mining(result: dict, folder: Path) -> Path:
    path = folder / "mining.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    return path


# --------------------------------------------------------------------------- #
# 4. Map helpers (plain Python, no DuckDB)
# --------------------------------------------------------------------------- #
def _special_nodes() -> set[str]:
    cfg = settings()["mining"]
    return {cfg["start_node"], cfg["end_node"]}


def simplify(nodes: list[dict], edges: list[dict], activity_coverage: float,
             edge_coverage: float, max_nodes: int) -> tuple[list[dict], list[dict], dict]:
    """Make a readable map: keep the most frequent boxes and arrows.

    1. Keep the most frequent activities until they cover `activity_coverage`
       of all events (never more than `max_nodes`). Start and end always stay.
    2. Among arrows between kept boxes, keep the most frequent until they cover
       `edge_coverage` of those arrows' transitions.
    3. Repair: every kept activity keeps its most frequent incoming and outgoing
       arrow (if it has one), so no box is left floating.
    Returns (nodes, edges, hidden), where `hidden` says how much is not shown.
    """
    special = _special_nodes()
    acts = sorted((n for n in nodes if n["activity"] not in special),
                  key=lambda n: (-n["events"], n["activity"]))
    total_events = sum(n["events"] for n in acts)
    kept_acts, covered = [], 0
    for n in acts:
        if len(kept_acts) >= max_nodes or (total_events and covered / total_events >= activity_coverage - 1e-9):
            break
        kept_acts.append(n)
        covered += n["events"]
    kept_names = {n["activity"] for n in kept_acts} | special
    kept_nodes = [n for n in nodes if n["activity"] in kept_names]

    candidates = sorted((e for e in edges if e["src"] in kept_names and e["dst"] in kept_names),
                        key=lambda e: (-e["count"], e["src"], e["dst"]))
    total_candidate = sum(e["count"] for e in candidates)
    kept_edges, running = [], 0
    for e in candidates:
        if total_candidate and running / total_candidate >= edge_coverage - 1e-9:
            break
        kept_edges.append(e)
        running += e["count"]

    chosen = {(e["src"], e["dst"]) for e in kept_edges}
    for n in kept_acts:
        name = n["activity"]
        for side, other in (("dst", "src"), ("src", "dst")):  # incoming, then outgoing
            if any(e[side] == name for e in kept_edges):
                continue
            best = next((e for e in candidates if e[side] == name), None)  # candidates are sorted
            if best is not None and (best["src"], best["dst"]) not in chosen:
                kept_edges.append(best)
                chosen.add((best["src"], best["dst"]))

    total_transitions = sum(e["count"] for e in edges)
    hidden = {
        "hidden_activities": len(acts) - len(kept_acts),
        "hidden_edges": len(edges) - len(kept_edges),
        "events_shown_share": round(covered / total_events, 4) if total_events else 1.0,
        "transitions_shown_share": (round(sum(e["count"] for e in kept_edges) / total_transitions, 4)
                                    if total_transitions else 1.0),
    }
    return kept_nodes, kept_edges, hidden


def slowest_edges(edges: list[dict], share: float | None = None) -> set[tuple[str, str]]:
    """The arrows in the slowest `share` by median wait (default: mining.highlight_slowest_share)."""
    if share is None:
        share = settings()["mining"]["highlight_slowest_share"]
    timed = sorted((e for e in edges if e.get("median_wait_h") is not None),
                   key=lambda e: -e["median_wait_h"])
    k = math.ceil(len(timed) * share) if timed else 0
    return {(e["src"], e["dst"]) for e in timed[:k]}


def _dot_text(text: str) -> str:
    """Escape text for a quoted DOT string (backslashes first, then quotes and line breaks)."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def to_dot(nodes: list[dict], edges: list[dict], mode: str = "frequency",
           highlight: set[tuple[str, str]] | None = None) -> str:
    """A Graphviz DOT text of the map (Streamlit draws DOT in the browser; no install needed).

    mode "frequency": boxes show how many events, arrows how many transitions.
    mode "time": boxes show the median time inside the step (when known),
    arrows the median wait. The slowest arrows (see slowest_edges, or the
    `highlight` set of (src, dst) pairs if given) are drawn in a warm colour.
    Arrow thickness grows with the log of its count (1 to 6).
    """
    if mode not in ("frequency", "time"):
        raise ValueError("mode must be 'frequency' or 'time'")
    special = _special_nodes()
    if highlight is None:
        highlight = slowest_edges(edges) if mode == "time" else set()
    max_count = max((e["count"] for e in edges), default=1)

    lines = ["digraph process {",
             "  rankdir=TB;",
             '  node [shape=box, style="rounded,filled", fillcolor="#eef3fb", color="#4a6fa5", '
             'fontname="Helvetica", fontsize=11];',
             '  edge [color="#666666", fontname="Helvetica", fontsize=9];']
    for n in nodes:
        name = n["activity"]
        if name in special:
            lines.append(f'  "{_dot_text(name)}" [label="{_dot_text(name)}", shape=oval, '
                         'fillcolor="#dddddd", fontsize=9, width=0.3, height=0.3];')
            continue
        if mode == "time" and n.get("median_processing_h") is not None:
            detail = format_hours(n["median_processing_h"])
        else:
            detail = f"{n['events']:,}"
        lines.append(f'  "{_dot_text(name)}" [label="{_dot_text(name)}\\n{_dot_text(detail)}"];')
    for e in edges:
        if mode == "time":
            label = format_hours(e["median_wait_h"]) if e.get("median_wait_h") is not None else ""
        else:
            label = f"{e['count']:,}"
        width = 1 + 5 * (math.log(e["count"]) / math.log(max_count)) if max_count > 1 and e["count"] > 0 else 1
        attrs = [f'label="{_dot_text(label)}"', f"penwidth={width:.2f}"]
        if (e["src"], e["dst"]) in highlight:
            attrs += ['color="#d9480f"', 'fontcolor="#d9480f"']
        lines.append(f'  "{_dot_text(e["src"])}" -> "{_dot_text(e["dst"])}" [{", ".join(attrs)}];')
    lines.append("}")
    return "\n".join(lines)


def format_hours(h: float | None) -> str:
    """Hours as people say them: "38 s", "4.2 min", "6.5 h", "3.2 days", "5.1 weeks"; None -> "–".

    Rule (fmt.duration, the one formatter of the app): under 60 s -> seconds; under 60 min ->
    minutes with one decimal; under 48 hours -> hours; under 14 days -> days; otherwise weeks.
    Never "< 1 min".
    """
    from app.core.fmt import duration
    return duration(h)


def hours_unit(h: float) -> tuple[float, str]:
    """The unit format_hours uses for `h`: (factor, unit name). Seconds and minutes MULTIPLY the
    hours by the factor; hours, days and weeks DIVIDE by it.

    Shared so a range ("4.2 days (3.9–4.6)") can be written in one unit.
    """
    if h * 3600 < 60:
        return 3600, "s"
    if h < 1:
        return 60, "min"
    if h < 48:
        return 1, "h"
    if h < 14 * 24:
        return 24, "days"
    return 7 * 24, "weeks"
