"""Calculation checks: the report proves its own numbers (checks.json, appendix table).

Each check recomputes a number INDEPENDENTLY of metrics.py: one plain SQL query reads the events in
scope from events.parquet, and everything else (runs, paths, waits, rework, conformance) is redone
here in plain Python. The result is compared with the value the report shows. Tolerance: counts
exact; hours within report.check_hours_tolerance; shares within report.check_share_tolerance_pp
percentage points. Any failure makes the report a DRAFT ("DRAFT — K checks failed").
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import duckdb

from app.core.config import settings
from app.core.db import sql_literal

CHECKS_FILE = "checks.json"


def _quantile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (the same rule as DuckDB quantile_cont)."""
    if not values:
        return None
    v = sorted(values)
    pos = (len(v) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


class _Book:
    def __init__(self):
        cfg = settings()["report"]
        self.h_tol = float(cfg.get("check_hours_tolerance", 0.05))
        self.s_tol = float(cfg.get("check_share_tolerance_pp", 0.05)) / 100
        self.rows: list[dict] = []

    def add(self, number: int, check: str, report, recomputed, kind: str = "count") -> None:
        if report is None and recomputed is None:
            ok = True
        elif report is None or recomputed is None:
            ok = False
        elif kind == "count":
            ok = int(round(float(report))) == int(round(float(recomputed))) and float(report) == float(recomputed)
        elif kind == "hours":
            ok = abs(float(report) - float(recomputed)) <= self.h_tol
        elif kind == "share":
            ok = abs(float(report) - float(recomputed)) <= self.s_tol
        else:                                         # exact equality of anything else (lists, text)
            ok = report == recomputed
        self.rows.append({"number": number, "check": check, "report": report, "recomputed": recomputed,
                          "kind": kind, "passed": bool(ok)})


def _events(events_path: Path) -> list[tuple]:
    """The events in scope, sorted per case by time and then file row order (plain SQL, no metrics code)."""
    from app.core.quality import utc_now
    con = duckdb.connect()
    path = sql_literal(Path(events_path).as_posix())
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({path})").fetchall()}
    pick = lambda c, kind="VARCHAR": c if c in cols else f"CAST(NULL AS {kind})"  # noqa: E731
    earliest = int(settings()["quality"]["earliest_valid_year"])
    return con.execute(f"""
        SELECT case_id, activity, epoch(ts_start), epoch(COALESCE(ts_end, ts_start)),
               {pick('merged_events', 'BIGINT')}, {pick('active_s', 'DOUBLE')}, {pick('application')},
               {pick('resource')}
        FROM read_parquet({path})
        WHERE case_id IS NOT NULL AND activity IS NOT NULL AND ts_start IS NOT NULL
          AND year(ts_start) >= {earliest} AND ts_start <= TIMESTAMP {sql_literal(utc_now())}
        ORDER BY case_id, ts_start, {pick('src_row', 'BIGINT') if 'src_row' in cols else 'activity'}""").fetchall()


def _raw_source(folder: Path, role: str):
    """(connection with the ORIGINAL file as view src, mapping), for a check on the raw input rows; None when it
    cannot be done (role not mapped, a derived case or activity, or the original file is gone)."""
    from app.core import jobs
    from app.core.ingest import open_source
    try:
        mapping = json.loads((folder / "mapping.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    derived = ((mapping.get("case_source") or {}).get("kind") or "column") != "column" or mapping.get("derived_activity")
    if not mapping.get(role) or derived:
        return None
    try:
        con = duckdb.connect()
        open_source(con, jobs.job_source(jobs.load_job(folder.name)), folder)
    except Exception:  # noqa: BLE001 - the file was moved or cannot be read: the check is skipped
        return None
    return con, mapping


def _seconds(mapping: dict) -> str:
    from app.core.db import quote_ident
    active = mapping.get("active_time")
    return f"COALESCE(TRY_CAST({quote_ident(active)} AS DOUBLE), 0)" if active else "0"


def _raw_apps(folder: Path) -> dict | None:
    """Per application, from the RAW input rows in scope (the original file, read again with the job's mapping,
    not events.parquet): {application: (active hours, events, cases)}."""
    from app.core.apps import name_table
    from app.core.db import quote_ident
    from app.core.ingest import SOURCE_VIEW, scope_conditions
    from app.core.mapping import scope_where
    src = _raw_source(folder, "application")
    if src is None:
        return None
    con, mapping = src
    app_col = mapping["application"]
    con.register("rc_names", name_table(con, app_col, scope_where(mapping)))
    rows = con.execute(f"""
        SELECT COALESCE(n.__app_name, '(no application)'), SUM({_seconds(mapping)}) / 3600.0, COUNT(*),
               COUNT(DISTINCT CAST({quote_ident(mapping['case_id'])} AS VARCHAR))
        FROM {SOURCE_VIEW} LEFT JOIN rc_names n ON n.__app_raw = CAST({quote_ident(app_col)} AS VARCHAR)
        WHERE {scope_conditions(mapping)['in_scope']} GROUP BY 1""").fetchall()
    return {a: (h, int(e), int(c)) for a, h, e, c in rows}


def _raw_people(folder: Path) -> dict | None:
    """Per participant, from the RAW input rows in scope: {masked label: (active hours, events, cases)}. The
    labels P01, P02 ... follow metrics.PEOPLE_ORDER, so they match the report's without any name leaving here."""
    from app.core.db import quote_ident
    from app.core.ingest import SOURCE_VIEW, scope_conditions
    from app.core.metrics import PEOPLE_ORDER
    src = _raw_source(folder, "resource")
    if src is None:
        return None
    con, mapping = src
    rows = con.execute(f"""
        SELECT SUM(active_s) / 3600.0, COUNT(*), COUNT(DISTINCT case_id) FROM (
            SELECT CAST({quote_ident(mapping['resource'])} AS VARCHAR) AS resource,
                   CAST({quote_ident(mapping['case_id'])} AS VARCHAR) AS case_id, {_seconds(mapping)} AS active_s
            FROM {SOURCE_VIEW}
            WHERE {scope_conditions(mapping)['in_scope']} AND {quote_ident(mapping['resource'])} IS NOT NULL)
        GROUP BY resource ORDER BY {PEOPLE_ORDER}""").fetchall()
    width = max(2, len(str(len(rows))))
    return {f"P{i:0{width}d}": (h, int(e), int(c)) for i, (h, e, c) in enumerate(rows, 1)}


def _apps_from_companion(folder: Path, ev: list[tuple]) -> dict[str, float]:
    """Active hours per application when the original file cannot be read: from events_apps.parquet (per run
    and application, written at conversion) for the cases in scope, else from the event rows."""
    from app.core.ingest import APPS_FILE
    path = folder / APPS_FILE
    out: dict[str, float] = defaultdict(float)
    if path.exists():
        cases = {e[0] for e in ev}
        for case, app, sec in duckdb.connect().execute(
                f"SELECT case_id, application, active_s FROM read_parquet({sql_literal(path.as_posix())})").fetchall():
            if case in cases:
                out[app if app is not None else "(no application)"] += (sec or 0) / 3600
        return out
    for e in ev:
        out[e[6] if e[6] is not None else "(no application)"] += (e[5] or 0) / 3600
    return out


def run(folder: Path, m: dict, summary_items: list[dict] | None = None, fact_ids: set | None = None,
        level4: dict | None = None, pm4py_facts: list | None = None) -> dict:
    """Recompute and compare; returns {"checks": [...], "total", "failed", "passed"} and saves checks.json.
    `level4` (mining.json level4 data) adds check 13: Level 4 per step equals the step table."""
    folder = Path(folder)
    b = _Book()
    t = m["totals"]
    ev = _events(folder / "events.parquet")
    has_active = m["has"]["active"]
    hours = lambda s: s / 3600  # noqa: E731

    # 1. Rows in file add up (rows and active hours); the scope matches the events analysed.
    sc = m.get("scope")
    if sc:
        parts = ("left_out_by_filter", "without_case", "without_activity", "without_time", "in_scope")
        b.add(1, "Rows in file = left out by filter + without case + without activity + without time + in scope",
              sc["rows_in_file"], sum(sc[p]["rows"] for p in parts))
        if sc.get("has_active_time"):
            b.add(1, "Active hours in file = the same split, in hours", hours(sc["active_seconds_in_file"]),
                  hours(sum(sc[p]["active_seconds"] for p in parts)), "hours")
        # With "Merge repeated steps" one row of events.parquet stands for merged_events input rows.
        b.add(1, "Rows in scope (scope.json) = events analysed (sum of merged events)", sc["in_scope"]["rows"],
              sum(int(e[4] or 1) for e in ev))
        if sc.get("has_active_time") and has_active:
            b.add(1, "Active hours in scope (scope.json) = report total", hours(sc["in_scope"]["active_seconds"]),
                  t["active_hours"], "hours")

    # 2. Events
    events = sum(int(e[4] or 1) for e in ev)
    b.add(2, "Events in scope", t["events"], events)
    b.add(2, "Events: sum over steps = total", sum(r["events"] for r in m["steps"]), t["events"])
    b.add(2, "Events: sum over applications = total", sum(a["events"] for a in m["applications"]), t["events"])

    # Per application from the RAW input rows in scope (used by check 3 and 3b): a merged row keeps only its
    # run's first application, so events.parquet cannot give the hours per application.
    raw = _raw_apps(folder) if m["has"].get("application") else None

    # 3. Hours reconcile everywhere
    if has_active:
        total = hours(sum(e[5] or 0 for e in ev))
        b.add(3, "Active hours in scope", t["active_hours"], total, "hours")
        b.add(3, "Hours: sum over steps = total", sum(r["active_hours"] or 0 for r in m["steps"]), total, "hours")
        b.add(3, "Hours: sum over applications = total", sum(a["active_hours"] or 0 for a in m["applications"]),
              total, "hours")
        b.add(3, "Hours: step × application grand total = total", m["matrix"]["grand_total"], total, "hours")
        by_step = defaultdict(float)          # a run has one step: the merged rows are right per step
        for e in ev:
            by_step[e[1]] += (e[5] or 0) / 3600
        by_app = defaultdict(float, {a: v[0] for a, v in raw.items()} if raw is not None
                             else _apps_from_companion(folder, ev))
        rows_ok = all(abs(r["total"] - by_step[r["step"]]) <= b.h_tol for r in m["matrix"]["rows"])
        cols_ok = all(abs(v - by_app[a]) <= b.h_tol for a, v in zip(m["matrix"]["apps"], m["matrix"]["column_totals"]))
        b.add(3, "Matrix row totals = step hours", rows_ok, True, "exact")
        b.add(3, "Matrix column totals = application hours", cols_ok, True, "exact")
        stage_sum = sum(s["active_hours"] or 0 for s in m["stages"])
        b.add(3, "Hours: sum over stages = total", stage_sum, total, "hours")

    # 3b. Per application (hours, events, cases), from the RAW input rows in scope: a merged run keeps one
    #     application, so only the original rows can prove the per-application numbers.
    if raw is not None:
        shown = {a["application"]: a for a in m["applications"]}
        b.add(3, "Applications: the same names in the input rows and in the report", sorted(shown), sorted(raw),
              "exact")
        for name, (h, e, c) in sorted(raw.items()):
            a = shown.get(name) or {}
            if has_active:
                b.add(3, f"{name}: active hours (input rows)", a.get("active_hours"), h, "hours")
            b.add(3, f"{name}: events (input rows)", a.get("events"), e)
            b.add(3, f"{name}: cases (input rows)", a.get("cases"), c)

    # 14. Per participant (events, hours, cases), from the RAW input rows in scope: a merged run keeps one
    #      user, so only the original rows can prove the per-participant numbers.
    people = _raw_people(folder) if m["has"].get("resource") else None
    if people is not None:
        shown = {p["participant"]: p for p in m["people"]}
        b.add(14, "Participants in the input rows = in the report", len(shown), len(people))
        for label, (h, e, c) in people.items():
            p = shown.get(label) or {}
            if has_active:
                b.add(14, f"{label}: active hours (input rows)", p.get("active_hours"), h, "hours")
            b.add(14, f"{label}: events (input rows)", p.get("events"), e)
            b.add(14, f"{label}: cases (input rows)", p.get("cases"), c)

    # Runs, cases and paths, rebuilt in Python
    runs: list[dict] = []
    cases: dict[str, list[dict]] = defaultdict(list)
    for case, act, t0, t1, n, a, app, res in ev:
        lst = cases[case]
        if lst and lst[-1]["activity"] == act:
            r = lst[-1]
            r["end"] = max(r["end"], t1)
            r["events"] += int(n or 1)
            r["active"] += a or 0
        else:
            r = {"case": case, "activity": act, "start": t0, "end": t1, "events": int(n or 1), "active": a or 0,
                 "resource": res}
            lst.append(r)
            runs.append(r)

    # 4. Runs
    b.add(4, "Step runs", t["runs"], len(runs))
    b.add(4, "Runs: sum over steps = total", sum(r["runs"] for r in m["steps"]), t["runs"])

    # 5. Cases and cases reached
    n_cases = len(cases)
    b.add(5, "Cases", t["cases"], n_cases)
    reached = Counter(act for lst in cases.values() for act in {r["activity"] for r in lst})
    b.add(5, "Cases reached per step", {r["step"]: r["cases"] for r in m["steps"]}, dict(reached), "exact")
    b.add(5, "Share of cases reaching each step", all(
        abs((r["cases_share"] or 0) - reached[r["step"]] / n_cases) <= b.s_tol for r in m["steps"]) if n_cases else True,
        True, "exact")

    # 6. Case durations
    durations = [(max(r["end"] for r in lst) - lst[0]["start"]) / 3600 for lst in cases.values()]
    b.add(6, "Median case duration (h)", t["median_case_h"], statistics.median(durations) if durations else None, "hours")
    b.add(6, "90% of cases finish within (h)", t["p90_case_h"], _quantile(durations, 0.9), "hours")
    b.add(6, "Mean case duration (h)", t["mean_case_h"], statistics.fmean(durations) if durations else None, "hours")

    # 7. Transitions and the waits shown
    pairs: dict[tuple, list[float]] = defaultdict(list)
    for lst in cases.values():
        for x, y in zip(lst, lst[1:]):
            pairs[(x["activity"], y["activity"])].append(max(y["start"] - x["end"], 0) / 3600)
    b.add(7, "Transitions = sum over cases of (runs − 1)", sum(x["count"] for x in m["transitions"]),
          sum(len(lst) - 1 for lst in cases.values()))
    for row in m["waiting"]["rows"]:
        w = pairs.get((row["from"], row["to"]), [])
        b.add(7, f"Median wait {row['from']} → {row['to']}", row["median_wait_h"],
              statistics.median(w) if w else None, "hours")
        b.add(7, f"Total wait {row['from']} → {row['to']}", row["total_wait_h"], sum(w), "hours")

    # 8. Rework and quick switching
    gap = float(m["settings"]["rework_gap_minutes"]) * 60
    rework, quick = Counter(), Counter()
    for lst in cases.values():
        last_end: dict[str, float] = {}
        for r in lst:
            if r["activity"] in last_end:
                (rework if r["start"] - last_end[r["activity"]] >= gap else quick)[r["activity"]] += 1
            last_end[r["activity"]] = r["end"]
    for row in m["rework"]["rows"]:
        b.add(8, f"Rework runs in {row['step']}", row["rework_runs"], rework[row["step"]])
        b.add(8, f"Quick switches in {row['step']}", row["quick_runs"], quick[row["step"]])
    b.add(8, "Rework runs in total", m["rework"]["rework_runs"], sum(rework.values()))
    b.add(8, "Quick switches in total", m["rework"]["quick_runs"], sum(quick.values()))

    # 9. Application switches (from app_switches.parquet, the switches counted at conversion)
    sw = m.get("switching") or {}
    path = folder / "app_switches.parquet"
    if sw.get("available") and path.exists():
        rows = duckdb.connect().execute(
            f"SELECT from_app, to_app, case_id FROM read_parquet({sql_literal(path.as_posix())})").fetchall()
        b.add(9, "Application switches", sw["total"], len(rows))
        counts = Counter((a, c) for a, c, _ in rows)
        b.add(9, "Top switch pairs", [(p["from"], p["to"], p["count"]) for p in sw["pairs"]],
              [(a, c, n) for (a, c), n in sorted(counts.items(), key=lambda x: (-x[1], x[0][0], x[0][1]))
               [:len(sw["pairs"])]], "exact")

    # 10. Variants
    paths = Counter(tuple(r["activity"] for r in lst) for lst in cases.values())
    v = m["variants"]
    b.add(10, "Variants", v["count"], len(paths))
    top10 = sum(n for _, n in paths.most_common(10))
    b.add(10, "Share of cases in the 10 most common paths", v["top10_share"], top10 / n_cases if n_cases else None,
          "share")
    for row in v["top"]:
        b.add(10, f"Share of path {' → '.join(row['path'])}", row["share"],
              paths[tuple(row["path"])] / n_cases if n_cases else None, "share")

    # 11. Conformance (only shown with a trusted order)
    conf = m.get("conformance")
    if conf:
        steps = [x["step"] for x in conf["reached"]]
        opos = {s: i for i, s in enumerate(steps)}
        ordered = ok = pairs_n = pairs_ok = 0
        for lst in cases.values():
            first: dict[str, int] = {}
            for i, r in enumerate(lst):
                if r["activity"] in opos and r["activity"] not in first:
                    first[r["activity"]] = i
            seq = [opos[a] for a, _ in sorted(first.items(), key=lambda x: x[1])]
            if len(seq) >= 2:
                ordered += 1
                ok += seq == sorted(seq)
            present = sorted(first, key=lambda a: opos[a])
            for i, a in enumerate(present):
                for c in present[i + 1:]:
                    pairs_n += 1
                    pairs_ok += first[a] < first[c]
        b.add(11, "Cases in the intended order", conf["in_order_share"], ok / ordered if ordered else None, "share")
        b.add(11, "Step pairs in the intended order", conf["pair_order_share"], pairs_ok / pairs_n if pairs_n else None,
              "share")
        b.add(11, f"Cases reaching {conf['last_step']}", conf["reach_last"], reached.get(conf["last_step"], 0))

    # 13. Level 4 sees every event of a step: its events, hours and cases equal the step table.
    steps = {r["step"]: r for r in m["steps"]}
    for d in (level4 or {}).get("l3s") or []:
        r = steps.get(d["l3"])
        if r is None:
            continue                       # a Level 3 that groups several steps: no single row to compare
        b.add(13, f"Level 4 {d['l3']}: events = step table", d.get("events"), r["events"])
        b.add(13, f"Level 4 {d['l3']}: cases = step table", d.get("cases"), r["cases"])
        if has_active:
            b.add(13, f"Level 4 {d['l3']}: active hours = step table", d.get("active_h") or 0.0,
                  r["active_hours"] or 0.0, "hours")

    # 15. The formal model (PM4Py) appendix: every fact equals its value in the PM4Py JSON files.
    if pm4py_facts is not None:
        from app.core.facts import pm4py_rows
        want = {(r["type"], r["label"]): r["value"] for r in pm4py_rows(folder)}
        b.add(15, "Formal model (PM4Py): facts = the PM4Py result files", len(pm4py_facts), len(want))
        for f in pm4py_facts:
            b.add(15, f.label, f.value, want.get((f.type, f.label)), "exact")

    # 16. Inside each step: the patterns add up to the runs, the time split to the step's hours, and the step's
    #     hours to the step table (the runs differ only in how they are split: by person and pause).
    from app.core import inside_step, order_evidence
    ins = inside_step.load(folder)
    steps_by_name = {r["step"]: r for r in m["steps"]}
    for l3, d in ((ins or {}).get("steps") or {}).items():
        if not d.get("enough"):
            continue
        b.add(16, f"Inside {l3}: pattern runs = runs", sum(p["runs"] for p in d["patterns"]), d["runs"])
        if has_active:
            b.add(16, f"Inside {l3}: time split = step hours", sum(t["hours"] for t in d["time_split"]), d["active_h"], "hours")
            if l3 in steps_by_name:
                b.add(16, f"Inside {l3}: hours = step table", d["active_h"], steps_by_name[l3]["active_hours"], "hours")
        if l3 in steps_by_name:
            b.add(16, f"Inside {l3}: events = step table", d["events"], steps_by_name[l3]["events"])

    # 18. One named measure, one value: Level 4 and "Inside each step" both count WORK SITTINGS per step.
    l4_runs = {d["l3"]: d["runs"] for d in (level4 or {}).get("l3s") or []}
    for l3, d in ((ins or {}).get("steps") or {}).items():
        if l3 in l4_runs:
            b.add(18, f"{l3}: work sittings (Level 4) = work sittings (inside the step)", l4_runs[l3], d["runs"])

    # 17. Order evidence: the precedence shares recomputed from the events (fully observed cases only).
    ev_doc = order_evidence.load(folder)
    if ev_doc and ev_doc.get("pairs"):
        days = int(ev_doc["edge_days"]) * 86400
        lo = min(e[2] for e in ev) if ev else 0
        hi = max(e[3] for e in ev) if ev else 0
        first: dict = {}
        span: dict = {}
        for case, act, t0, t1, *_ in ev:
            f = first.setdefault(case, {})
            f.setdefault(act, len(f))
            s0, s1 = span.get(case, (t0, t1))
            span[case] = (min(s0, t0), max(s1, t1))
        full = {c for c, (s0, s1) in span.items() if s0 >= lo + days and s1 <= hi - days}
        b.add(17, "Order evidence: fully observed cases", ev_doc["cases_used"], len(full))
        for p in ev_doc["pairs"]:
            if p["share"] is None or p["a"] not in reached or p["b"] not in reached:
                continue
            both = [first[c] for c in full if p["a"] in first[c] and p["b"] in first[c]]
            share = sum(1 for f in both if f[p["a"]] < f[p["b"]]) / len(both) if both else None
            b.add(17, f"Order evidence: {p['a']} before {p['b']}", p["share"], share, "share")
            b.add(17, f"Order evidence: cases with {p['a']} and {p['b']}", p["cases"], len(both))

    # 12. Every number in the summary text comes from the fact store
    if summary_items is not None and fact_ids is not None:
        from app.core.summary import PLACEHOLDER
        used = {fid for x in summary_items for fid in PLACEHOLDER.findall(x.get("text", ""))}
        b.add(12, "Every number in the summary text is a fact in the fact store", sorted(used - fact_ids), [], "exact")

    failed = [r for r in b.rows if not r["passed"]]
    out = {"checks": b.rows, "total": len(b.rows), "failed": len(failed), "passed": len(b.rows) - len(failed)}
    (folder / CHECKS_FILE).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def headline(result: dict) -> str:
    if result["failed"]:
        return f"DRAFT — {result['failed']} checks failed"
    return f"All {result['total']} calculation checks passed"
