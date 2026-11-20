"""Search for a better reading of the same data: other case and activity definitions.

A log can look clean and still show no order because the CASE or the ACTIVITY was
defined the wrong way (a batch number instead of a request ID, a status instead of
a step). `find_readings` tries other definitions of the same rows and ranks them by
a confidence (0-100):

    case       the mapped case column; other ID-like columns (2-200 rows per value,
               at least half filled; never time, application, window title, user or
               personal-data columns); each with the user; each with user + work
               session; and user work sessions alone
    activity   the mapped activity, the stage column, and other short text columns
               (3-60 values, at least half filled)

Each pair is measured on a seeded sample of cases, after merging repeated steps:
order strength, stability of the suggested order, share of cases with 2+ steps,
rows covered and median steps per case. All work runs in DuckDB.

`apply_reading` turns a chosen reading into a NEW job (the old job is kept).
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb

from app.core import jobs
from app.core.config import settings
from app.core.db import connect, quote_ident
from app.core.ingest import normalise, open_source
from app.core.mapping import _name_tokens, scope_where, timestamp_sql

READINGS_FILE = "readings.json"
DERIVED_CASE = "__reading_case"
_NOT_ID_TYPES = ("DOUBLE", "FLOAT", "DECIMAL", "REAL", "TIMESTAMP", "DATE", "TIME", "BOOLEAN", "INTERVAL")


@dataclass
class Reading:
    case_label: str            # how cases are defined, in words ("request_id + user + work session")
    case_spec: dict            # {"kind": column | column_user | column_user_session | user_session, ...}
    activity: str              # the activity column
    order_strength: float
    stability: float | None
    multi_step_share: float
    coverage: float
    median_steps: float | None
    cases: int                 # cases in the sample
    confidence: float
    current: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _cfg() -> dict:
    return settings()["reading_finder"]


def _personal(column: str) -> bool:
    """Column names that suggest personal data (the same hints as page 1)."""
    hints = settings()["mapping"]["sensitive"]["name_hints"]
    words = _name_tokens(column)
    joined = "".join(words)
    return any(h in words or h == joined or (len(h) >= 8 and joined.endswith(h)) for h in hints)


def _case_key(spec: dict) -> str:
    """SQL for the case key of a definition (over columns c, u and the session number __session)."""
    if spec["kind"] == "text_pattern":
        from app.core.derived import _extract   # local import avoids a cycle
        return _extract(spec["column"], spec["pattern"])
    col = f"CAST({quote_ident(spec['column'])} AS VARCHAR)" if spec.get("column") else None
    user = f"CAST({quote_ident(spec['user'])} AS VARCHAR)" if spec.get("user") else None
    kind = spec["kind"]
    if kind == "column":
        return col
    if kind == "column_user":
        return f"{col} || '|' || {user}"
    if kind == "column_user_session":
        return f"{col} || '|' || {user} || '|' || CAST(__session AS VARCHAR)"
    return f"{user} || '|' || CAST(__session AS VARCHAR)"           # user_session


def _session_sql(spec: dict, ts: str, base: str) -> str:
    """`base` rows plus __session: a new work session after a pause longer than the gap."""
    gap = float(_cfg()["session_gap_minutes"]) * 60
    part = ", ".join(quote_ident(c) for c in (spec.get("column"), spec.get("user")) if c)
    return f"""
        SELECT *, SUM(__new) OVER (PARTITION BY {part} ORDER BY __ts ROWS UNBOUNDED PRECEDING) AS __session
        FROM (SELECT *, CASE WHEN epoch(__ts) - epoch(lag(__ts) OVER (PARTITION BY {part} ORDER BY __ts)) > {gap}
                             THEN 1 ELSE 0 END AS __new
              FROM (SELECT *, {ts} AS __ts FROM {base}))"""


def _label(spec: dict) -> str:
    if spec["kind"] == "text_pattern":
        label = settings()["derived_case"]["id_patterns"][spec["pattern"]]["label"]
        return f"{label} found in {spec['column']}"
    return {"column": "{c}", "column_user": "{c} + user", "column_user_session": "{c} + user + work session",
            "user_session": "user work session"}[spec["kind"]].format(c=spec.get("column"))


def candidates(con: duckdb.DuckDBPyConnection, src_view: str, mapping: dict) -> tuple[list[tuple[dict, str]], dict]:
    """The (case definition, activity column) pairs to try, most promising first, and column stats."""
    cfg = _cfg()
    where = scope_where(mapping)
    cols = {c[0]: str(c[1]).upper() for c in con.execute(f"DESCRIBE {src_view}").fetchall()}
    user = mapping.get("resource")
    never = {mapping.get(r) for r in ("timestamp", "end_timestamp", "application", "window_title", "action")} | {user}
    never |= set(mapping.get("exclude_columns") or [])
    names = [c for c in cols if c not in never and not c.startswith("_") and not _personal(c)]
    parts = ", ".join(f"COUNT({quote_ident(c)}), approx_count_distinct({quote_ident(c)})" for c in names)
    row = con.execute(f"SELECT COUNT(*){', ' + parts if parts else ''} FROM {src_view} WHERE {where}").fetchall()[0]
    total = row[0] or 1
    stats = {c: {"fill": row[1 + 2 * i] / total, "distinct": row[2 + 2 * i] or 0, "filled": row[1 + 2 * i]}
             for i, c in enumerate(names)}
    lo, hi = cfg["id_rows_per_value"]["min"], cfg["id_rows_per_value"]["max"]
    case_cols = [mapping["case_id"]] if mapping.get("case_id") else []
    for c in names:
        s = stats[c]
        if c in case_cols or cols[c].startswith(_NOT_ID_TYPES) or s["fill"] < cfg["min_fill"] or not s["distinct"]:
            continue
        if lo <= s["filled"] / s["distinct"] <= hi:
            case_cols.append(c)
    act_cols = [mapping["activity"]] if mapping.get("activity") else []
    if mapping.get("stage") and mapping["stage"] not in act_cols:
        act_cols.append(mapping["stage"])
    amin, amax = cfg["activity_distinct"]["min"], cfg["activity_distinct"]["max"]
    for c in names:
        s = stats[c]
        if c in act_cols or c in case_cols or not cols[c].startswith(("VARCHAR", "STRING", "TEXT")):
            continue
        if s["fill"] >= cfg["min_fill"] and amin <= s["distinct"] <= amax:
            act_cols.append(c)
    case_defs: list[dict] = []
    for c in case_cols:
        case_defs.append({"kind": "column", "column": c})
        if user:
            case_defs += [{"kind": "column_user", "column": c, "user": user},
                          {"kind": "column_user_session", "column": c, "user": user}]
    if user:
        case_defs.append({"kind": "user_session", "user": user})
    # Derived cases: IDs found in the window title and other free-text columns (derived.find_id_patterns).
    text_cols = [mapping["window_title"]] if mapping.get("window_title") else []
    text_cols += [c for c in names if cols[c].startswith(("VARCHAR", "STRING", "TEXT")) and c not in case_cols
                  and c != mapping.get("activity") and stats[c]["distinct"] > amax and stats[c]["fill"] >= cfg["min_fill"]]
    if text_cols:
        from app.core.derived import find_id_patterns   # local import avoids a cycle
        found = [p for p in find_id_patterns(con, text_cols[:5], mapping.get("activity"), src_view) if p["candidate"]]
        case_defs += [{"kind": "text_pattern", "column": p["column"], "pattern": p["pattern"],
                       "distinct": p["distinct"]} for p in found[:2]]

    def fill(spec: dict) -> float:
        parts = [stats.get(spec.get("column"), {"fill": 1.0})["fill"]] if spec.get("column") else [1.0]
        return parts[0]

    current = {"kind": "column", "column": mapping.get("case_id")}
    pairs = [(cd, a) for cd in case_defs for a in act_cols]
    pairs.sort(key=lambda p: (not (p[0] == current and p[1] == mapping.get("activity")),
                              -(fill(p[0]) * stats.get(p[1], {"fill": 1.0})["fill"])))
    return pairs[:int(cfg["max_candidates"])], {"total_rows": total, "columns": stats}


def evaluate(con: duckdb.DuckDBPyConnection, src_view: str, mapping: dict, spec: dict, activity: str,
             sample_cases: int, total_rows: int, distinct: dict) -> Reading:
    """Measure one reading on a seeded sample of its cases (after merging repeated steps)."""
    from app.core.reference import _pairs, order_stability, order_strength   # local import avoids a cycle

    cfg = _cfg()
    tz = mapping.get("timezone") or "UTC"
    ts = timestamp_sql(mapping["timestamp"], mapping["timestamp_format"], tz)
    where = scope_where(mapping)
    act = quote_ident(activity)
    # Sample whole cases: by the case column (or the found ID), or by the user for user sessions (seeded hash).
    if spec["kind"] == "text_pattern":
        by_sql, n_values = _case_key(spec), max(int(spec.get("distinct") or 1), 1)
    else:
        by = spec.get("column") or spec["user"]
        by_sql, n_values = f"CAST({quote_ident(by)} AS VARCHAR)", max(distinct.get(by, 0), 1)
    keep = min(1.0, sample_cases / n_values)
    sample = (f"(hash({by_sql} || 'reading-sample') % 1000000) < {int(keep * 1000000)}" if keep < 1 else "TRUE")
    base = f"(SELECT * FROM {src_view} WHERE ({where}) AND {by_sql} IS NOT NULL AND {sample})"
    rows = _session_sql(spec, ts, base) if spec["kind"] in ("column_user_session", "user_session") \
        else f"SELECT *, {ts} AS __ts FROM {base}"
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE rf_events AS
        SELECT k AS case_id, a AS activity, __ts AS ts FROM (
            SELECT {_case_key(spec)} AS k, CAST({act} AS VARCHAR) AS a, __ts FROM ({rows}))
        WHERE k IS NOT NULL AND a IS NOT NULL AND __ts IS NOT NULL""")
    # Merge runs of the same step, then number the steps of each case.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE rf_steps AS
        SELECT case_id, activity, ts, row_number() OVER (PARTITION BY case_id ORDER BY ts, activity) AS seq FROM (
            SELECT *, lag(activity) OVER (PARTITION BY case_id ORDER BY ts, activity) AS prev FROM rf_events)
        WHERE prev IS NULL OR prev <> activity""")
    edges = [{"src": s, "dst": d, "count": n} for s, d, n in con.execute("""
        SELECT activity, nxt, COUNT(*) FROM (
            SELECT activity, lead(activity) OVER (PARTITION BY case_id ORDER BY seq) AS nxt FROM rf_steps)
        WHERE nxt IS NOT NULL GROUP BY 1, 2""").fetchall()]
    strength, _ = order_strength(edges) if _pairs(edges) else (0.0, "Weak")
    cases, multi, median_steps = con.execute("""
        SELECT COUNT(*), COUNT(*) FILTER (WHERE d >= 2), median(n) FROM (
            SELECT case_id, COUNT(DISTINCT activity) AS d, COUNT(*) AS n FROM rf_steps GROUP BY case_id)""").fetchall()[0]
    con.execute("""
        CREATE OR REPLACE TEMP TABLE rf_first AS
        SELECT f.case_id, f.activity, CAST(f.first_seq - 1 AS DOUBLE) / greatest(c.n - 1, 1) AS rel_pos
        FROM (SELECT case_id, activity, MIN(seq) AS first_seq FROM rf_steps GROUP BY 1, 2) f
        JOIN (SELECT case_id, COUNT(*) AS n FROM rf_steps GROUP BY 1) c USING (case_id)""")
    min_share = settings()["reference_map"]["min_case_share"]
    steps = [a for a, c in con.execute("SELECT activity, COUNT(*) FROM rf_first GROUP BY 1").fetchall()
             if cases and c / cases >= min_share]
    stability = order_stability(con, steps, "rf_first")
    # Coverage on ALL rows in scope: rows that this reading can place (case, activity and time known).
    needed = " AND ".join([f"{quote_ident(c)} IS NOT NULL" for c in (spec.get("column"), spec.get("user"), activity) if c]
                          + ([f"{_case_key(spec)} IS NOT NULL"] if spec["kind"] == "text_pattern" else []))
    covered = con.execute(f"SELECT COUNT(*) FROM {src_view} WHERE ({where}) AND {needed} AND {ts} IS NOT NULL"
                          ).fetchall()[0][0]
    coverage = covered / total_rows if total_rows else 0.0
    multi_share = multi / cases if cases else 0.0
    w = cfg["weights"]
    strong = settings()["reference_map"]["order_strength"]["strong"]
    # Order strength and stability rest on transitions: with few multi-step cases they are scaled down.
    # A stable ranking of an order that barely exists is no evidence of an order, so stability
    # counts in proportion to the (scaled) order strength.
    evidence = min(1.0, multi_share / float(cfg["min_multi_step_share"]))
    strength_scaled = min(1.0, strength / strong) * evidence
    confidence = 100 * (w["order_strength"] * strength_scaled
                        + w["stability"] * (stability or 0.0) * strength_scaled
                        + w["multi_step"] * multi_share + w["coverage"] * coverage)
    return Reading(_label(spec), spec, activity, round(strength, 3), stability, round(multi_share, 4),
                   round(coverage, 4), None if median_steps is None else float(median_steps), cases,
                   round(confidence, 1))


def find_readings(con: duckdb.DuckDBPyConnection, src_view: str, mapping: dict, max_candidates: int | None = None,
                  sample_cases: int | None = None) -> list[Reading]:
    """Try other case/activity definitions of the same rows; best confidence first."""
    cfg = _cfg()
    pairs, info = candidates(con, src_view, mapping)
    if max_candidates is not None:
        pairs = pairs[:max_candidates]
    sample = int(sample_cases or cfg["sample_cases"])
    distinct = {c: s["distinct"] for c, s in info["columns"].items()}
    if mapping.get("case_id") and mapping["case_id"] not in distinct:
        distinct[mapping["case_id"]] = con.execute(
            f"SELECT approx_count_distinct({quote_ident(mapping['case_id'])}) FROM {src_view}").fetchall()[0][0]
    if mapping.get("resource"):
        distinct[mapping["resource"]] = con.execute(
            f"SELECT approx_count_distinct({quote_ident(mapping['resource'])}) FROM {src_view}").fetchall()[0][0]
    out = []
    for spec, activity in pairs:
        r = evaluate(con, src_view, mapping, spec, activity, sample, info["total_rows"], distinct)
        r.current = spec == {"kind": "column", "column": mapping.get("case_id")} and activity == mapping.get("activity")
        out.append(r)
    for t in ("rf_events", "rf_steps", "rf_first"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    if mapping.get("action") and mapping.get("case_id"):
        import tempfile
        from app.core.milestones import analyse
        from app.core.mining import build_log
        from app.core.config import data_dir
        with tempfile.TemporaryDirectory(dir=data_dir(), prefix="milestone-reading-") as tmp:
            events = Path(tmp) / "events.parquet"
            normalise(con, mapping, events)
            build_log(con, events)
            ms = analyse(con, events, "reading-preview")
            if ms:
                for mode, data in (("step + milestone action", ms), ("step order from first milestones", ms["step_order"])):
                    out.append(Reading(_label({"kind": "column", "column": mapping["case_id"]}),
                        {"kind": "milestones", "column": mapping["case_id"], "mode": mode}, mode,
                        data["order_strength"], data["stability"], ms["coverage"], ms["coverage"], None,
                        ms["cases"], data["flow"]["score"]))
    return sorted(out, key=lambda r: (-r.confidence, -r.order_strength, not r.current, r.case_label, r.activity))


def warning_text(readings: list[Reading]) -> str | None:
    """The message to show when no reading reaches the Low flow-confidence threshold."""
    if not readings:
        return None
    low = settings()["flow_confidence"]["bands"]["low"]
    best = readings[0]
    if best.confidence >= low:
        return None
    return (f"No reading of this data shows a reliable order (best: {best.confidence:.0f} of {len(readings)} tried). "
            "The order must come from the business — see 'Process order' on page 3.")


def save_readings(readings: list[Reading], folder: Path) -> Path:
    path = Path(folder) / READINGS_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"readings": [r.to_dict() for r in readings], "warning": warning_text(readings)}, f, indent=2)
    return path


def load_readings(folder: Path) -> list[Reading]:
    path = Path(folder) / READINGS_FILE
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [Reading(**r) for r in json.load(f)["readings"]]


# --------------------------------------------------------------------------- #
# Using a reading: a new job with the changed mapping
# --------------------------------------------------------------------------- #
def derive_case_column(con: duckdb.DuckDBPyConnection, spec: dict, mapping: dict) -> str:
    """Add the reading's case key to the `src` view as column __reading_case (for normalise).

    The original view is kept as `src_raw`. Returns the new column's name.
    """
    tz = mapping.get("timezone") or "UTC"
    ts = timestamp_sql(mapping["timestamp"], mapping["timestamp_format"], tz)
    con.execute("ALTER VIEW src RENAME TO src_raw")
    rows = _session_sql(spec, ts, "src_raw") if "session" in spec["kind"] else "SELECT * FROM src_raw"
    con.execute(f"""CREATE VIEW src AS
        SELECT * EXCLUDE (__ts, __new, __session), {_case_key(spec)} AS {quote_ident(DERIVED_CASE)} FROM ({rows})"""
                if "session" in spec["kind"] else
                f"CREATE VIEW src AS SELECT *, {_case_key(spec)} AS {quote_ident(DERIVED_CASE)} FROM src_raw")
    return DERIVED_CASE


def apply_reading(job_id: str, reading: Reading) -> dict:
    """Create a NEW job that reads the same data with this reading's case and activity.

    The old job is kept unchanged. The new job is converted and quality checked
    (status quality_checked); job.json records where it came from under "reading".
    """
    from app.core.quality import run_quality, save_report   # local import avoids a cycle

    old = jobs.load_job(job_id)
    with open(jobs.job_folder(job_id) / "mapping.json", encoding="utf-8") as f:
        mapping = json.load(f)
    source = jobs.job_source(old)
    new = jobs.create_job(f"{old.get('source_name')} · {reading.case_label} / {reading.activity}", source)
    folder = jobs.job_folder(new["job_id"])
    con = connect()
    open_source(con, source, folder)
    new_mapping = copy.deepcopy(mapping)
    new_mapping["activity"] = mapping["activity"] if reading.case_spec["kind"] == "milestones" else reading.activity
    spec = reading.case_spec
    if spec["kind"] in ("column", "milestones"):
        new_mapping["case_id"] = spec["column"]
    elif spec["kind"] == "text_pattern":
        from app.core.derived import apply_derivations   # local import avoids a cycle
        new_mapping["case_source"] = {"kind": "text_pattern", "column": spec["column"], "pattern": spec["pattern"]}
        new_mapping = apply_derivations(con, new_mapping)
    else:
        new_mapping["case_id"] = derive_case_column(con, spec, mapping)
        new_mapping["derived_case"] = spec
    if new_mapping.get("stage") == reading.activity:
        new_mapping["stage"] = None                      # one column cannot be both activity and stage
    summary = normalise(con, new_mapping, folder / "events.parquet")
    with open(folder / "mapping.json", "w", encoding="utf-8") as f:
        json.dump(new_mapping, f, indent=2)
    new.update(status="normalised", summary=summary, format=old.get("format"),
               layout_signature=old.get("layout_signature"),
               reading={"from_job": job_id, "case_definition": reading.case_label, "case_spec": spec,
                        "activity": reading.activity, "confidence": reading.confidence,
                        "note": (f"case_id = {' | '.join(x for x in (spec.get('column'), spec.get('user')) if x)}"
                                 + (" | work session number" if "session" in spec["kind"] else ""))})
    new["case_source"] = new_mapping.get("case_source") or {"kind": "column", "column": new_mapping["case_id"]}
    jobs.save_job(new)
    report = run_quality(connect(), folder / "events.parquet")
    save_report(report, folder)
    new.update(status="quality_checked", feasibility=report.feasibility, band=report.band)
    jobs.save_job(new)
    return new
