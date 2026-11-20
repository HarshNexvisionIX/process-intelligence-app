"""Source file -> the standard event table (nc_events.parquet) and the scope counts (nc_scope.json).

Rows are counted at every step and the counts must add up:
    rows in file = left out by keep_rows_where + left out by drop_rows_where + unreadable time + in scope

Conversion (all with try_ casts, never failing on a bad value):
    times      with an offset -> that offset; without -> mapping.timezone; ts_format via try_strptime
               (mapping.timestamp_sql, the same expression the case mode uses) -> UTC
    numbers    DOUBLE
    text       TRIM, empty -> NULL
    app        the application map (apps.name_table, as in the case mode)
    actor      the salted hash used for stored case IDs (derived.hash_case_sql); so is case_id
    eval_*     copied as text, for app/nocase/eval/ only
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb

from app.core.db import quote_ident, sql_literal
from app.core.ingest import SOURCE_VIEW
from app.nocase.canonical import (EVAL_PREFIX, HASHED, NUMBER_COLUMNS, STANDARD_COLUMNS, TEXT_COLUMNS,
                                  NcMapping)

EVENTS_FILE = "nc_events.parquet"
SCOPE_FILE = "nc_scope.json"
ID_COLUMNS_FILE = "nc_id_columns.json"
MAPPING_FILE = "nc_mapping.json"
SCOPED = "__nc_scoped"


def _text(src: str) -> str:
    return f"NULLIF(TRIM(CAST({quote_ident(src)} AS VARCHAR)), '')"


def _match(col: str, values: list) -> str:
    """SQL: the column's value is one of `values` (as text, or as a number for numeric values: 1 = 1.0)."""
    q = quote_ident(col)
    texts = ", ".join(sql_literal(str(v).strip()) for v in values)
    nums = []
    for v in values:
        try:
            nums.append(repr(float(v)))
        except (TypeError, ValueError):
            pass
    cond = f"TRIM(CAST({q} AS VARCHAR)) IN ({texts})"
    if nums:
        cond = f"({cond} OR TRY_CAST({q} AS DOUBLE) IN ({', '.join(nums)}))"
    return f"COALESCE({cond}, FALSE)"


def keep_sql(m: NcMapping) -> str:
    return " AND ".join(_match(c, v) for c, v in m.keep_rows_where.items()) or "TRUE"


def drop_sql(m: NcMapping) -> str:
    return " OR ".join(_match(c, v) for c, v in m.drop_rows_where.items()) or "FALSE"


def _dtypes(con) -> dict[str, str]:
    return {r[0]: str(r[1]) for r in con.execute(f"DESCRIBE {SOURCE_VIEW}").fetchall()}


def time_format(con, m: NcMapping, column: str) -> str:
    """The mapping's ts_format, else detected from the column (mapping.detect_timestamp_format)."""
    from app.core.mapping import detect_timestamp_format
    if m.ts_format and column in (m.columns.get("ts"), m.columns.get("ts_end")):   # start and end share a format
        return m.ts_format
    fmt, _rate = detect_timestamp_format(con, SOURCE_VIEW, column, _dtypes(con)[column])
    return fmt or "iso"


def standard_select(con, m: NcMapping, app_join: bool) -> list[str]:
    """The SELECT list of the standard table (over the rowed source)."""
    from app.core.derived import hash_case_sql
    from app.core.mapping import timestamp_sql
    cols = m.mapped()
    out = ["eid"]
    for t in ("ts", "ts_end"):
        src = cols.get(t)
        out.append(f"{timestamp_sql(src, time_format(con, m, src), m.timezone)} AS {t}" if src
                   else f"CAST(NULL AS TIMESTAMP) AS {t}")
    for c in STANDARD_COLUMNS:
        if c in ("ts", "ts_end"):
            continue
        src = cols.get(c)
        if c in NUMBER_COLUMNS:
            out.append(f"TRY_CAST({quote_ident(src)} AS DOUBLE) AS {c}" if src else f"CAST(NULL AS DOUBLE) AS {c}")
        elif c == "app" and src and app_join:
            out.append("CAST(__app_name AS VARCHAR) AS app")
        elif c in HASHED and src:
            out.append(f"{hash_case_sql(_text(src))} AS {c}")
        elif c in TEXT_COLUMNS:
            out.append(f"{_text(src)} AS {c}" if src else f"CAST(NULL AS VARCHAR) AS {c}")
    out.append(f"{sql_literal(m.dataset_class or 'client')} AS dataset_class")
    return out


def eval_select(m: NcMapping) -> list[str]:
    """The eval (truth) columns: carried through the scope, then written ONLY to <job>/eval/ (eval package)."""
    return [f"{_text(src)} AS {quote_ident(EVAL_PREFIX + name)}" for name, src in m.eval.items()]


def convert(con: duckdb.DuckDBPyConnection, m: NcMapping, folder: Path, app_map: dict | None = None) -> dict:
    """Write nc_events.parquet and nc_scope.json from the `src` view; returns the scope."""
    start = time.perf_counter()
    names = list(_dtypes(con))
    problems = m.problems(names)
    if problems:
        raise ValueError("The no-case mapping is not usable: " + " ".join(problems))
    folder.mkdir(parents=True, exist_ok=True)
    keep, drop = keep_sql(m), drop_sql(m)
    cols = m.mapped()
    join = ""
    if cols.get("app"):
        from app.core.apps import name_table
        con.register("__nc_app_names", name_table(con, cols["app"], f"({keep}) AND NOT ({drop})", app_map))
        join = f" LEFT JOIN __nc_app_names ON __app_raw = CAST(r.{quote_ident(cols['app'])} AS VARCHAR)"
    select = standard_select(con, m, bool(join))
    truth = eval_select(m)
    candidates = integer_columns(con, m)
    ints = "".join(f", TRY_CAST(TRIM(CAST(r.{quote_ident(c)} AS VARCHAR)) AS HUGEINT) AS __int_{i}"
                   for i, c in enumerate(candidates))
    rowed = f"(SELECT *, row_number() OVER () AS eid FROM {SOURCE_VIEW})"
    con.execute(f"""CREATE OR REPLACE TEMP VIEW {SCOPED} AS
        SELECT {', '.join(select + truth)}{ints}, ({keep}) AS __keep, ({drop}) AS __drop
        FROM {rowed} AS r{join}""")
    rows, left_keep, left_drop, unreadable, in_scope = con.execute(f"""
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE NOT __keep),
               COUNT(*) FILTER (WHERE __keep AND __drop),
               COUNT(*) FILTER (WHERE __keep AND NOT __drop AND ts IS NULL),
               COUNT(*) FILTER (WHERE __keep AND NOT __drop AND ts IS NOT NULL)
        FROM {SCOPED}""").fetchone()
    out_cols = [s.rsplit(" AS ", 1)[-1] if " AS " in s else s for s in select]
    target = folder / EVENTS_FILE
    con.execute(f"""COPY (SELECT {', '.join(out_cols)} FROM {SCOPED}
                          WHERE __keep AND NOT __drop AND ts IS NOT NULL ORDER BY eid)
                    TO {sql_literal(target.as_posix())} (FORMAT parquet, COMPRESSION zstd)""")
    from app.nocase.eval import write_truth
    write_truth(con, SCOPED, "__keep AND NOT __drop AND ts IS NOT NULL", folder, list(m.eval))
    active_s, first, last, actors, apps = con.execute(f"""
        SELECT COALESCE(SUM(active_s),          -- no active-time column: the start-to-end time of each event
                        SUM(CASE WHEN ts_end >= ts THEN epoch(ts_end) - epoch(ts) END)),
               MIN(ts), MAX(ts), COUNT(DISTINCT actor), COUNT(DISTINCT app)
        FROM read_parquet({sql_literal(target.as_posix())})""").fetchone()
    scope = {"rows_in_file": int(rows),
             "left_out": {"keep_rows_where": int(left_keep), "drop_rows_where": int(left_drop)},
             "left_out_by_filter": int(left_keep + left_drop),
             "unreadable_time": int(unreadable), "in_scope": int(in_scope),
             "active_hours_in_scope": None if active_s is None else round(float(active_s) / 3600, 6),
             "first_event": None if first is None else str(first), "last_event": None if last is None else str(last),
             "actors": int(actors), "apps": int(apps), "dataset_class": m.dataset_class,
             "filters": {"keep_rows_where": m.keep_rows_where, "drop_rows_where": m.drop_rows_where},
             "mapped": sorted(cols), "eval": sorted(m.eval), "seconds": round(time.perf_counter() - start, 2)}
    assert scope["rows_in_file"] == scope["left_out_by_filter"] + scope["unreadable_time"] + scope["in_scope"], scope
    (folder / SCOPE_FILE).write_text(json.dumps(scope, indent=2), encoding="utf-8")
    (folder / ID_COLUMNS_FILE).write_text(json.dumps(id_column_checks(con, m, candidates), indent=2), encoding="utf-8")
    (folder / MAPPING_FILE).write_text(json.dumps(m.to_dict(), indent=2), encoding="utf-8")
    return scope


def integer_columns(con, m: NcMapping) -> list[str]:
    """Source columns that hold whole numbers (integer type, or text that is 95%+ digits in a sample), except
    times, durations, eval and filter columns."""
    from app.core.config import settings
    icfg = settings()["nocase"]["integer_like"]
    skip = {m.columns.get(c) for c in ("ts", "ts_end", "active_s", "wait_s")} | set(m.eval.values()) \
        | set(m.keep_rows_where) | set(m.drop_rows_where) | {"_ingest_file"}
    out = []
    for col, dtype in _dtypes(con).items():
        if col in skip:
            continue
        up = dtype.upper()
        if up.startswith(("BIGINT", "INTEGER", "HUGEINT", "SMALLINT", "UBIGINT", "UINTEGER")):
            out.append(col)
        elif up.startswith("VARCHAR"):
            q = quote_ident(col)
            share = con.execute(f"""SELECT AVG(CASE WHEN regexp_full_match(TRIM(v), '[0-9]{{1,18}}') THEN 1.0 ELSE 0 END)
                                    FROM (SELECT CAST({q} AS VARCHAR) v FROM {SOURCE_VIEW} WHERE {q} IS NOT NULL
                                          LIMIT {int(icfg['sample_rows'])})""").fetchone()[0]
            if share is not None and share >= float(icfg["min_digit_share"]):
                out.append(col)
    return out


def id_column_checks(con, m: NcMapping, candidates: list[str]) -> list[dict]:
    """Integer-like source columns (in scope): how unique they are, and whether they rise with time across ALL
    actors in steps of 1 - an event counter, not a case. Aggregates only; no value is stored."""
    from app.core.config import settings
    cfg = settings()["nocase"]["counter"]
    roles = {v: k for k, v in m.mapped().items()}
    out = []
    for i, col in enumerate(candidates):
        n, distinct, inc, one = con.execute(f"""
            WITH x AS (SELECT __int_{i} AS v, ts, eid FROM {SCOPED}
                       WHERE __keep AND NOT __drop AND ts IS NOT NULL AND __int_{i} IS NOT NULL),
                 d AS (SELECT v, v - lag(v) OVER (ORDER BY ts, eid) AS step FROM x)
            SELECT COUNT(*), COUNT(DISTINCT v),
                   AVG(CASE WHEN step > 0 THEN 1.0 ELSE 0 END) FILTER (WHERE step IS NOT NULL),
                   AVG(CASE WHEN step = 1 THEN 1.0 ELSE 0 END) FILTER (WHERE step > 0)
            FROM d""").fetchone()
        if not n:
            continue
        unique = distinct / n
        counter = (unique >= cfg["min_unique_share"] and (inc or 0) >= cfg["min_increasing_share"]
                   and (one or 0) >= cfg["min_step_one_share"])
        out.append({"column": col, "role": roles.get(col), "values": int(n), "unique_share": round(unique, 4),
                    "increasing_share": None if inc is None else round(float(inc), 4),
                    "step_one_share": None if one is None else round(float(one), 4), "counter": bool(counter),
                    "reason": (f"an event counter, not a case: {100 * unique:.0f}% unique values that rise with time "
                               f"across all actors ({100 * (inc or 0):.0f}% of steps up, "
                               f"{100 * (one or 0):.0f}% by exactly 1)") if counter else None})
    return out


def ingest(source, m: NcMapping, job: dict | None = None, source_name: str | None = None,
           app_map: dict | None = None) -> tuple[dict, dict]:
    """Open `source`, convert it into a job folder (a new job when `job` is None); returns (job, scope).

    The job gets mode "nocase" and the mapping's dataset_class; the mapping is saved for this layout."""
    from app.core import jobs
    from app.core.db import connect
    from app.nocase.canonical import save_mapping
    from app.nocase.readers import open_nc_source
    if job is None:
        job = jobs.create_job(source_name or Path(str(source)).name, source, m.dataset_class or "client")
    folder = jobs.job_folder(job["job_id"])
    con = connect()
    fmt = open_nc_source(con, source if not isinstance(source, list) else source, folder / "raw")
    names = [r[0] for r in con.execute(f"DESCRIBE {SOURCE_VIEW}").fetchall()]
    scope = convert(con, m, folder, app_map=app_map)
    save_mapping(names, m)
    from app.nocase.canonical import layout_signature
    job.update(mode="nocase", format=fmt, status="nc_converted", dataset_class=m.dataset_class or "client",
               rows=scope["rows_in_file"], nc_layout_signature=layout_signature(names))
    jobs.save_job(job)
    from app.core.privacy import set_dataset_class
    set_dataset_class(job["dataset_class"])
    return job, scope


def events_path(folder: Path) -> Path:
    return folder / EVENTS_FILE


def load_scope(folder: Path) -> dict | None:
    p = folder / SCOPE_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
