"""The report's numbers, computed in ONE place (metrics.json). Definitions: docs/METRICS.md.

Working tables (TEMP, on the connection given):
    xev    the events in scope: case, activity and a usable time (quality.analysable_where), NOT
           de-duplicated, with active / wait / turnaround seconds and src_row (row order in the file)
    xrun   step runs: consecutive events of the same activity in the same case, after sorting by
           time and then by row order. A run's active time = the SUM of its events' active time
           (never the duration of the run); start = first event, end = last event end (or start).
    xcase  one row per case: first and last event, duration (last - first, 0 for one event), path
    xtrans one row per pair of consecutive runs in a case: wait = start of the next run - end of
           the previous run (negative overlaps count as 0)

`compute` returns plain numbers (hours as floats at full precision; nothing rounded for display):
totals, scope (scope.json), steps (in the business order when one is confirmed / saved / a
template), stages, step x application matrix, applications, transitions, rework vs quick switching,
people (masked as P01, P02 ... - never names), handovers, switching, ISO-week trend, variants and
conformance (only with a trusted order). report_checks.py recomputes them independently.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from app.core.config import settings
from app.core.db import sql_literal

METRICS_FILE = "metrics.json"
NO_APP = "(no application)"
NO_STAGE_LABEL = "(no stage)"
TRUSTED_SOURCES = ("confirmed", "template")      # confirmed by the business, a template, or a saved (edited) order
# How participants are ranked for their masked labels P01, P02 ... (by active hours, then cases, then a hash).
PEOPLE_ORDER = "round(COALESCE(SUM(active_s), 0), 6) DESC, COUNT(DISTINCT case_id) DESC, hash(resource)"


def _cfg() -> dict:
    return settings()["report"]


def _f(x) -> float | None:
    return None if x is None else float(x)


# --------------------------------------------------------------------------- #
# Working tables
# --------------------------------------------------------------------------- #
def _has_col(con, path: Path, name: str) -> bool:
    return name in {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({sql_literal(path.as_posix())})")
                    .fetchall()}


def build_tables(con: duckdb.DuckDBPyConnection, events_path: Path) -> dict:
    """Create xev, xrun, xcase and xtrans; returns which time columns exist."""
    from app.core.quality import analysable_where, utc_now
    path = sql_literal(Path(events_path).as_posix())
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({path})").fetchall()}

    def col(name: str, kind: str = "VARCHAR") -> str:
        return name if name in cols else f"CAST(NULL AS {kind}) AS {name}"

    tie = "src_row" if "src_row" in cols else "activity"
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE xev AS
        SELECT case_id, activity, ts_start, COALESCE(ts_end, ts_start) AS ts_last,
               {col('resource')}, {col('application')}, {col('stage')},
               {col('active_s', 'DOUBLE')}, {col('wait_s', 'DOUBLE')}, {col('tat_s', 'DOUBLE')},
               {'merged_events' if 'merged_events' in cols else '1 AS merged_events'},
               {col('src_row', 'BIGINT')},
               row_number() OVER (PARTITION BY case_id ORDER BY ts_start, {tie}) AS pos
        FROM read_parquet({path})
        WHERE {analysable_where(utc_now())}""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE xevrun AS
        WITH n AS (SELECT *, CASE WHEN lag(activity) OVER (PARTITION BY case_id ORDER BY pos) = activity
                                  THEN 0 ELSE 1 END AS new_run FROM xev)
        SELECT *, SUM(new_run) OVER (PARTITION BY case_id ORDER BY pos ROWS UNBOUNDED PRECEDING) AS run_no FROM n""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE xrun AS
        WITH r AS (SELECT * FROM xevrun)
        SELECT case_id, run_no, any_value(activity) AS activity, MIN(ts_start) AS start_ts, MAX(ts_last) AS end_ts,
               SUM(merged_events) AS events, COUNT(*) AS rows, SUM(active_s) AS active_s, SUM(wait_s) AS wait_s,
               SUM(tat_s) AS tat_s, arg_min(resource, pos) AS resource
        FROM r GROUP BY case_id, run_no""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE xcase AS
        SELECT case_id, MIN(start_ts) AS start_ts, MAX(end_ts) AS end_ts,
               (epoch(MAX(end_ts)) - epoch(MIN(start_ts))) / 3600.0 AS duration_h, COUNT(*) AS runs,
               list(activity ORDER BY run_no) AS path
        FROM xrun GROUP BY case_id""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE xtrans AS
        SELECT * FROM (
            SELECT case_id, run_no, activity AS src, lead(activity) OVER w AS dst,
                   resource AS src_resource, lead(resource) OVER w AS dst_resource,
                   GREATEST(epoch(lead(start_ts) OVER w) - epoch(end_ts), 0) / 3600.0 AS wait_h
            FROM xrun WINDOW w AS (PARTITION BY case_id ORDER BY run_no))
        WHERE dst IS NOT NULL""")
    # Per-application and per-user numbers: a merged run keeps one application and one user, so its events and
    # active time per application and user come from events_apps.parquet (ingest.merged_apps_sql), joined to the
    # runs in scope. xapp: case_id, run_no, activity, application, resource, events, active_s.
    from app.core.ingest import APPS_FILE
    apps_path = Path(events_path).parent / APPS_FILE
    if apps_path.exists() and "src_row" in cols:
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE xapp AS
            SELECT a.case_id, e.run_no, e.activity, a.application, {'a.resource' if _has_col(con, apps_path, 'resource')
                                                                     else 'e.resource'} AS resource,
                   a.events, a.active_s
            FROM read_parquet({sql_literal(apps_path.as_posix())}) a
            JOIN xevrun e ON a.case_id = e.case_id AND a.src_row = e.src_row""")
    else:
        con.execute("""CREATE OR REPLACE TEMP TABLE xapp AS
                       SELECT case_id, run_no, activity, application, resource, merged_events AS events, active_s
                       FROM xevrun""")
    has = con.execute("SELECT COUNT(active_s) > 0, COUNT(wait_s) > 0, COUNT(tat_s) > 0, COUNT(resource) > 0, "
                      "COUNT(application) > 0 FROM xev").fetchall()[0]
    return dict(zip(("active", "wait", "tat", "resource", "application"), map(bool, has)))


# --------------------------------------------------------------------------- #
# The numbers
# --------------------------------------------------------------------------- #
def _order_info(ref: dict | None) -> dict:
    """The step order to present: its source, whether it is trusted, its steps and stages."""
    from app.core.flow_confidence import order_source_of
    if not ref or not ref.get("order"):
        return {"source": "none", "badge": "No step order", "trusted": False, "steps": [], "stage_of": {},
                "stages": []}
    order = ref["order"]
    source = order_source_of(order)
    from_template = order.get("source") == "template" or bool(order.get("template_name"))
    if from_template and not order.get("confirmed_by_business") and not settings()["templates"].get("use_for_order"):
        # A (saved) order from the built-in template is not evidence about the process: no conformance on it.
        return {"source": source, "badge": ref.get("source_badge") or source, "trusted": False,
                "steps": [step for s in order.get("stages") or [] for step in s.get("steps") or []], "stage_of": {},
                "stages": []}
    stages = [s for s in order.get("stages") or []]
    # A stage without a real name ("" or "(no stage)") is no stage: such steps are listed apart, never drawn.
    stage_of = {step: (s.get("name") if s.get("name") not in ("", NO_STAGE_LABEL) else None)
                for s in stages for step in s.get("steps") or []}
    return {"source": source, "badge": ref.get("source_badge") or source, "trusted": source in TRUSTED_SOURCES,
            "steps": [step for s in stages for step in s.get("steps") or []], "stage_of": stage_of,
            "stages": [s.get("name") or None for s in stages]}


def compute(con: duckdb.DuckDBPyConnection, folder: Path, ref: dict | None = None) -> dict:
    """Every number of the report for the job in `folder` (see the module docstring)."""
    from app.core.ingest import SCOPE_FILE, SWITCHES_FILE
    cfg = _cfg()
    folder = Path(folder)
    has = build_tables(con, folder / "events.parquet")
    order = _order_info(ref)
    one = lambda sql: con.execute(sql).fetchall()[0]  # noqa: E731

    rows, events, cases, activities, participants, active_s, first, last = one("""
        SELECT COUNT(*), SUM(merged_events), COUNT(DISTINCT case_id), COUNT(DISTINCT activity),
               COUNT(DISTINCT resource), SUM(active_s), MIN(ts_start), MAX(ts_last)
        FROM xev""")
    apps, participants = one("SELECT COUNT(DISTINCT application), COUNT(DISTINCT resource) FROM xapp")
    runs, = one("SELECT COUNT(*) FROM xrun")
    med, p90, mean, one_event = one("""
        SELECT median(duration_h), quantile_cont(duration_h, 0.9), SUM(duration_h ORDER BY duration_h) / COUNT(duration_h),   -- value order: the same in any data folder
               COUNT(*) FILTER (WHERE runs = 1 AND duration_h = 0) FROM xcase""")
    total_h = (active_s or 0) / 3600 if has["active"] else None
    totals = {"rows": rows, "events": int(events or 0), "runs": runs, "cases": cases, "activities": activities,
              "participants": participants, "applications": apps, "active_hours": total_h,
              "first_event": first.isoformat() if first else None, "last_event": last.isoformat() if last else None,
              "median_case_h": _f(med), "p90_case_h": _f(p90), "mean_case_h": _f(mean),
              "one_event_cases": one_event}

    # --- Steps ---
    gap_h = float(cfg["rework_gap_minutes"]) / 60
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE xrework AS
        SELECT r.*, (epoch(r.start_ts) - epoch(lag(r.end_ts) OVER w)) / 3600.0 AS since_prev_h,
               lag(r.run_no) OVER w IS NOT NULL AS is_return
        FROM xrun r WINDOW w AS (PARTITION BY case_id, activity ORDER BY run_no)""")
    con.execute(f"""CREATE OR REPLACE TEMP VIEW xrework_kind AS
        SELECT *, is_return AND since_prev_h >= {gap_h} AS rework, is_return AND since_prev_h < {gap_h} AS quick
        FROM xrework""")
    step_rows = con.execute("""
        SELECT activity, COUNT(DISTINCT case_id), COUNT(*), SUM(events), SUM(active_s), median(active_s),
               SUM(wait_s), SUM(tat_s), SUM(COALESCE(active_s, 0) + COALESCE(wait_s, 0)),
               COUNT(DISTINCT case_id) FILTER (WHERE rework), COUNT(*) FILTER (WHERE rework),
               SUM(active_s) FILTER (WHERE rework),
               COUNT(DISTINCT case_id) FILTER (WHERE quick), COUNT(*) FILTER (WHERE quick)
        FROM xrework_kind GROUP BY activity""").fetchall()
    stage_mode = dict(con.execute(f"""SELECT activity, mode(stage) FROM xev WHERE stage IS NOT NULL
                                      AND stage NOT IN ('', '{NO_STAGE_LABEL}') GROUP BY 1""").fetchall())
    app_of: dict[str, list] = {}
    for act, app, sec, n in con.execute("""
            SELECT activity, COALESCE(application, ?), SUM(active_s), SUM(events) FROM xapp
            GROUP BY 1, 2 ORDER BY 1, 3 DESC NULLS LAST, 4 DESC, 2""", [NO_APP]).fetchall():
        app_of.setdefault(act, []).append({"application": app, "hours": None if sec is None else sec / 3600,
                                           "events": int(n)})
    steps = []
    for (act, reached, n_runs, ev, act_s, med_s, wait_s, tat_s, act_wait_s, rw_cases, rw_runs, rw_s,
         q_cases, q_runs) in step_rows:
        h = None if not has["active"] else (act_s or 0) / 3600
        base_tat = tat_s if has["tat"] else act_wait_s
        stage = order["stage_of"].get(act) if order["trusted"] else stage_mode.get(act)
        steps.append({
            "step": act, "stage": stage or None, "cases": reached, "cases_share": reached / cases if cases else None,
            "runs": n_runs, "events": int(ev), "active_hours": h,
            "hours_share": (h / total_h) if h is not None and total_h else None,
            "hours_per_case": (h / reached) if h is not None and reached else None,
            "median_active_run_s": _f(med_s) if has["active"] else None,
            "top_apps": [a for a in app_of.get(act, []) if a["application"] != NO_APP][:2],
            "rework_cases": rw_cases, "rework_share": rw_cases / reached if reached else None,
            "rework_runs": rw_runs, "rework_hours": (rw_s or 0) / 3600 if has["active"] else None,
            "quick_cases": q_cases, "quick_runs": q_runs,
            "waiting_hours": (wait_s or 0) / 3600 if has["wait"] else None,
            "waiting_share": (wait_s / base_tat) if has["wait"] and base_tat else None})
    if order["trusted"]:
        pos = {s: i for i, s in enumerate(order["steps"])}
        steps.sort(key=lambda r: (pos.get(r["step"], len(pos)), -r["cases"], r["step"]))
    else:
        steps.sort(key=lambda r: (-r["cases"], r["step"]))
    staged = any(r["stage"] for r in steps)

    # --- Stages, ordered by the first step of each stage ---
    stages, seen = [], {}
    for r in steps:
        key = r["stage"] or NO_STAGE_LABEL
        if key not in seen:
            seen[key] = {"stage": r["stage"], "steps": [], "active_hours": 0.0 if has["active"] else None,
                         "runs": 0, "events": 0}
            stages.append(seen[key])
        s = seen[key]
        s["steps"].append(r["step"])
        s["runs"] += r["runs"]
        s["events"] += r["events"]
        if has["active"]:
            s["active_hours"] += r["active_hours"] or 0
    stage_cases = {}
    if stages:
        con.register("xstage_map", _frame([(r["step"], r["stage"] or NO_STAGE_LABEL) for r in steps], ["activity", "stage"]))
        stage_cases = dict(con.execute("""SELECT m.stage, COUNT(DISTINCT r.case_id) FROM xrun r JOIN xstage_map m
                                          USING (activity) GROUP BY 1""").fetchall())
        con.unregister("xstage_map")
    for s in stages:
        s["cases"] = stage_cases.get(s["stage"] or NO_STAGE_LABEL, 0)
        s["hours_share"] = (s["active_hours"] / total_h) if has["active"] and total_h else None

    # --- Step x application (active hours and events) ---
    app_rows = con.execute("""
        SELECT COALESCE(application, ?), SUM(active_s), SUM(events), COUNT(DISTINCT case_id) FROM xapp
        GROUP BY 1 ORDER BY 2 DESC NULLS LAST, 3 DESC, 1""", [NO_APP]).fetchall()
    applications = [{"application": a, "active_hours": (s or 0) / 3600 if has["active"] else None,
                     "hours_share": ((s or 0) / 3600 / total_h) if has["active"] and total_h else None,
                     "events": int(n), "cases": c} for a, s, n, c in app_rows]
    cells = {(act, a["application"]): a for act, lst in app_of.items() for a in lst}
    app_names = [a["application"] for a in applications]
    matrix = {"apps": app_names, "rows": [], "measure": "active_hours" if has["active"] else "events"}
    for r in steps:
        vals = [(cells.get((r["step"], a)) or {}).get("hours" if has["active"] else "events") or 0 for a in app_names]
        matrix["rows"].append({"step": r["step"], "values": vals, "total": sum(vals)})
    matrix["column_totals"] = [sum(row["values"][i] for row in matrix["rows"]) for i in range(len(app_names))]
    matrix["grand_total"] = sum(matrix["column_totals"])

    # --- Waiting between steps (consecutive runs) ---
    transitions = [{"from": s, "to": d, "count": n, "cases": c, "median_wait_h": _f(m), "p90_wait_h": _f(p),
                    "total_wait_h": _f(t)}
                   for s, d, n, c, m, p, t in con.execute("""
                       SELECT src, dst, COUNT(*), COUNT(DISTINCT case_id), median(wait_h), quantile_cont(wait_h, 0.9),
                              SUM(wait_h ORDER BY wait_h)   -- a fixed order: the same total on every run and data folder
                       FROM xtrans GROUP BY 1, 2 ORDER BY 7 DESC, 3 DESC, 1, 2""").fetchall()]
    min_cases, min_wait = int(cfg["min_cases"]), float(cfg["min_wait_minutes"]) / 60
    supported = [t for t in transitions if t["cases"] >= min_cases]
    waits = [t for t in supported if (t["median_wait_h"] or 0) >= min_wait]
    waiting = {"rows": waits[:int(cfg["top_n"])], "left_out_short": len(supported) - len(waits),
               "left_out_low_support": len(transitions) - len(supported), "transitions": sum(t["count"] for t in transitions)}

    # --- Rework (return after the gap) vs quick switching, and back-and-forth of the rework kind ---
    back = [{"a": a, "b": b, "count": n, "cases": c} for a, b, n, c in con.execute(f"""
        WITH t AS (SELECT case_id, run_no, activity, end_ts,
                          lead(activity) OVER w AS b, lead(activity, 2) OVER w AS a2, lead(start_ts, 2) OVER w AS a2_start
                   FROM xrun WINDOW w AS (PARTITION BY case_id ORDER BY run_no))
        SELECT activity, b, COUNT(*), COUNT(DISTINCT case_id) FROM t
        WHERE a2 = activity AND b <> activity AND (epoch(a2_start) - epoch(end_ts)) / 3600.0 >= {gap_h}
        GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT {int(cfg['top_n'])}""").fetchall()]
    rework = {"gap_minutes": cfg["rework_gap_minutes"],
              "rows": sorted([{k: r[k] for k in ("step", "cases", "rework_cases", "rework_share", "rework_runs",
                                                  "rework_hours", "quick_cases", "quick_runs")}
                              for r in steps if r["rework_runs"] or r["quick_runs"]],
                             key=lambda x: (-x["rework_cases"], -x["quick_cases"], x["step"])),
              "back_and_forth": back,
              "rework_runs": sum(r["rework_runs"] for r in steps), "quick_runs": sum(r["quick_runs"] for r in steps)}

    # --- People (masked) and handovers ---
    people, handovers, mask = [], [], {}
    if has["resource"]:
        # Per user from the per-run, per-user totals (xapp): a run worked by two users counts for both, each with
        # its own events and hours. Masked labels follow PEOPLE_ORDER (report_checks uses the same order).
        prow = con.execute(f"""
            SELECT resource, COUNT(DISTINCT case_id), SUM(active_s), COUNT(DISTINCT activity),
                   COUNT(DISTINCT (case_id, run_no)), SUM(events)
            FROM xapp WHERE resource IS NOT NULL GROUP BY resource
            ORDER BY {PEOPLE_ORDER}""").fetchall()
        width = max(2, len(str(len(prow))))
        for i, (res, c, s, nsteps, nruns, n) in enumerate(prow, 1):
            mask[res] = f"P{i:0{width}d}"
            people.append({"participant": mask[res], "cases": c, "runs": nruns, "steps": nsteps, "events": int(n),
                           "active_hours": (s or 0) / 3600 if has["active"] else None})
        hand = con.execute(f"""
            SELECT src_resource, dst_resource, COUNT(*), COUNT(DISTINCT case_id) FROM xtrans
            WHERE src_resource IS NOT NULL AND dst_resource IS NOT NULL AND src_resource <> dst_resource
            GROUP BY 1, 2 ORDER BY 4 DESC, 3 DESC, hash(src_resource), hash(dst_resource)
            LIMIT {int(cfg['top_n'])}""").fetchall()
        handovers = [{"from": mask.get(a, "P?"), "to": mask.get(b, "P?"), "count": n, "cases": c} for a, b, n, c in hand]
    total_handovers = one("""SELECT COUNT(*) FROM xtrans WHERE src_resource IS NOT NULL AND dst_resource IS NOT NULL
                             AND src_resource <> dst_resource""")[0]

    # --- Application switches (counted at conversion, before merging; see ingest.write_app_switches) ---
    switching = {"available": False}
    sw_path = folder / SWITCHES_FILE
    if sw_path.exists():
        sw = f"read_parquet({sql_literal(sw_path.as_posix())})"
        total_sw, sw_cases = one(f"SELECT COUNT(*), COUNT(DISTINCT case_id) FROM {sw}")
        per_med, per_mean = one(f"""SELECT median(n), AVG(n) FROM (SELECT c.case_id, COUNT(s.case_id) AS n FROM xcase c
                                    LEFT JOIN {sw} s USING (case_id) GROUP BY 1)""")
        pairs = [{"from": a, "to": b, "count": n, "cases": c} for a, b, n, c in con.execute(f"""
            SELECT from_app, to_app, COUNT(*), COUNT(DISTINCT case_id) FROM {sw} GROUP BY 1, 2
            ORDER BY 3 DESC, 1, 2 LIMIT {int(cfg['top_n'])}""").fetchall()]
        run_n = {r["step"]: r["runs"] for r in steps}
        by_step = sorted(({"step": a, "switches": n, "runs": run_n.get(a, 0),
                           "per_run": n / run_n[a] if run_n.get(a) else None}
                          for a, n in con.execute(f"SELECT activity, COUNT(*) FROM {sw} GROUP BY 1").fetchall()
                          if a is not None), key=lambda x: (-(x["per_run"] or 0), x["step"]))
        switching = {"available": True, "total": total_sw, "cases_with_switch": sw_cases,
                     "per_case_median": _f(per_med), "per_case_mean": _f(per_mean), "pairs": pairs,
                     "by_step": by_step, "gap_minutes": settings()["app_switching"]["session_gap_minutes"]}

    # --- Trend per ISO week ---
    weeks = []
    trend_rows = con.execute("""
        WITH c AS (SELECT date_trunc('week', start_ts) AS w, COUNT(*) AS n, median(duration_h) AS med FROM xcase GROUP BY 1),
             h AS (SELECT date_trunc('week', ts_start) AS w, SUM(active_s) AS s FROM xev GROUP BY 1)
        SELECT COALESCE(c.w, h.w) AS w, COALESCE(c.n, 0), c.med, h.s FROM c FULL JOIN h USING (w) ORDER BY 1""").fetchall()
    for i, (w, n, m, s) in enumerate(trend_rows):
        iso = w.isocalendar()
        partial = (i == 0 and first is not None and first > w) or (
            i == len(trend_rows) - 1 and last is not None and (last - w).total_seconds() < 7 * 86400 - 1)
        weeks.append({"week": f"{iso[0]}-W{iso[1]:02d}", "week_start": w.date().isoformat(), "cases_started": n,
                      "active_hours": (s or 0) / 3600 if has["active"] else None, "median_case_h": _f(m),
                      "partial": bool(partial)})

    # --- Variants (paths of step runs) ---
    n_var, = one("SELECT COUNT(DISTINCT path) FROM xcase")
    var_rows = con.execute("""SELECT path, COUNT(*) AS n, median(duration_h) FROM xcase GROUP BY path
                              ORDER BY n DESC, CAST(path AS VARCHAR)""").fetchall()
    top10 = sum(n for _, n, _ in var_rows[:10])
    multi = [(p, n, m) for p, n, m in var_rows if len(p) >= 2][:5]
    variants = {"count": n_var, "top10_cases": top10, "top10_share": top10 / cases if cases else None,
                "top": [{"path": list(p), "cases": n, "share": n / cases if cases else None, "median_case_h": _f(m)}
                        for p, n, m in multi]}

    result = {
        "version": 1, "has": has, "order": {k: order[k] for k in ("source", "badge", "trusted")},
        "totals": totals, "scope": _load_json(folder / SCOPE_FILE),
        "steps": steps, "staged": staged, "stages": stages, "matrix": matrix, "applications": applications,
        "transitions": transitions, "waiting": waiting, "rework": rework, "people": people,
        "handovers": handovers, "total_handovers": total_handovers, "switching": switching, "trend": weeks,
        "variants": variants, "conformance": conformance(con, order, cases) if order["trusted"] else None,
        "settings": {k: cfg[k] for k in ("min_cases", "min_wait_minutes", "rework_gap_minutes", "top_n")},
    }
    return result


def conformance(con: duckdb.DuckDBPyConnection, order: dict, cases: int) -> dict:
    """How cases follow a trusted order, on FIRST occurrences: the first event of each step (time, then
    row order) gives its place in the case. In order = the first occurrences of the ordered steps a
    case has come in the order's sequence (cases with 2+ of them); pair order = the same per pair."""
    steps = [s for s in order["steps"]]
    if len(steps) < 1:
        return None
    con.register("xorder", _frame([(s, i) for i, s in enumerate(steps)], ["activity", "opos"]))
    con.execute("""CREATE OR REPLACE TEMP TABLE xfirst AS
                   SELECT e.case_id, e.activity, MIN(e.pos) AS first_pos, o.opos FROM xev e JOIN xorder o USING (activity)
                   GROUP BY e.case_id, e.activity, o.opos""")
    con.unregister("xorder")
    ordered, ok = con.execute("""
        WITH w AS (SELECT case_id, opos, lag(opos) OVER (PARTITION BY case_id ORDER BY first_pos) AS prev,
                          COUNT(*) OVER (PARTITION BY case_id) AS n FROM xfirst),
             c AS (SELECT case_id, MAX(n) AS n, bool_and(prev IS NULL OR opos >= prev) AS ok FROM w GROUP BY 1)
        SELECT COUNT(*) FILTER (WHERE n >= 2), COUNT(*) FILTER (WHERE n >= 2 AND ok) FROM c""").fetchall()[0]
    pairs, pairs_ok = con.execute("""SELECT COUNT(*), COUNT(*) FILTER (WHERE a.first_pos < b.first_pos)
                                     FROM xfirst a JOIN xfirst b ON a.case_id = b.case_id AND a.opos < b.opos""").fetchall()[0]
    earlier = [{"step": done, "before": intended, "cases": n} for intended, done, n in con.execute("""
        SELECT a.activity, b.activity, COUNT(DISTINCT a.case_id) FROM xfirst a JOIN xfirst b
        ON a.case_id = b.case_id AND a.opos < b.opos AND b.first_pos < a.first_pos
        GROUP BY 1, 2 ORDER BY 3 DESC, 1, 2 LIMIT 5""").fetchall()]
    reached = dict(con.execute("SELECT activity, COUNT(DISTINCT case_id) FROM xfirst GROUP BY 1").fetchall())
    last = steps[-1]
    return {"in_order_share": ok / ordered if ordered else None, "in_order_cases": ok, "ordered_cases": ordered,
            "pair_order_share": pairs_ok / pairs if pairs else None, "pairs": pairs, "pairs_ok": pairs_ok,
            "earlier": earlier, "last_step": last, "reach_last": reached.get(last, 0),
            "reach_last_share": reached.get(last, 0) / cases if cases else None,
            "reached": [{"step": s, "cases": reached.get(s, 0), "share": reached.get(s, 0) / cases if cases else None}
                        for s in steps]}


def _frame(rows: list[tuple], columns: list[str]):
    import pandas as pd
    return pd.DataFrame(rows, columns=columns).astype({c: object for c in columns if c != "opos"})


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(result: dict, folder: Path) -> Path:
    path = Path(folder) / METRICS_FILE
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return path


def load(folder: Path) -> dict | None:
    return _load_json(Path(folder) / METRICS_FILE)
