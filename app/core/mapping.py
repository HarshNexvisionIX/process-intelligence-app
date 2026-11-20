"""Step 2: decide which column is the case ID, activity and timestamp.

Gist
----
- `detect_timestamp_format` finds how a column's dates are written.
- `suggest_mapping` scores every column for every role, using two signals:
      name signal  - does the column name look like "case id", "activity", ...?
      data signal  - do the values behave like one (e.g. a case ID repeats a few
                     times, an activity has few distinct values, a timestamp parses)?
  It returns the best column per role with a confidence and the reasons.
- `validate_mapping` re-checks the user's final choice on the FULL data.

The user always confirms the mapping. Suggestions are help, never a decision.
(On Day 5 an AI suggester can be added on top; this rule-based one stays as
the safety check.)
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

import duckdb

from app.core.config import data_dir, settings
from app.core.db import quote_ident, sql_literal

REQUIRED_ROLES = ("case_id", "activity", "timestamp")


def _distinct(col) -> str:
    """The distinct count as shown: "2,940", or "about 2,994" when it is an estimate (very large files)."""
    return f"{col.distinct:,}" if getattr(col, "distinct_exact", True) else f"about {col.distinct:,}"
# "window_title" only helps the user recognise applications on page 1: it is never
# written to events.parquet, reports, facts or the AI.
OPTIONAL_ROLES = ("end_timestamp", "resource", "application", "window_title", "stage", "action",
                  "level1", "level3", "step_run", "active_time", "wait_time", "tat_time")
ALL_ROLES = REQUIRED_ROLES + OPTIONAL_ROLES

ROLE_LABELS = {
    "action": "Action / UI event (optional)",
    "case_id": "Case ID",
    "activity": "Activity",
    "timestamp": "Timestamp (start or event time)",
    "end_timestamp": "End timestamp (optional)",
    "resource": "Resource / user (optional)",
    "application": "Application (optional)",
    "window_title": "Window title (optional, Import data only)",
    "stage": "Stage / group (optional)",
    "level1": "Process level 1 (optional)",
    "level3": "Process level 3 (optional)",
    "step_run": "Step run ID (optional)",
    "active_time": "Active time, seconds (optional)",
    "wait_time": "Wait time, seconds (optional)",
    "tat_time": "Turnaround time (active + wait), seconds (optional)",
}

# Words that commonly appear in each role's column name (lower-case, no spaces).
NAME_HINTS = {
    "action": ["activityaliasname", "alias", "action", "uiaction", "eventname", "click", "operation"],
    "case_id": ["caseid", "case", "caseconceptname", "ticket", "ticketid", "orderid", "order",
                "invoice", "invoiceid", "document", "documentno", "docno", "sessionid", "session",
                "claim", "claimid", "requestid", "request", "instance", "processid", "po", "poid",
                "trace", "traceid"],
    "activity": ["activity", "activityname", "conceptname", "event", "eventname", "eventtype",
                 "action", "actionname", "step", "stepname", "task", "taskname", "status",
                 "stage", "page", "pagename", "screen", "operation", "transaction", "tcode"],
    "timestamp": ["timestamp", "time", "date", "datetime", "ts", "starttime", "start",
                  "startdate", "created", "createdat", "eventtime", "timetimestamp", "eventdate",
                  "occurredat", "loggedat", "recordedat"],
    "end_timestamp": ["end", "endtime", "enddate", "complete", "completed", "completetime",
                      "finish", "finished", "finishtime", "closed", "closedat"],
    "resource": ["resource", "orgresource", "user", "username", "userid", "agent", "agentid",
                 "employee", "employeeid", "owner", "performer", "actor", "createdby", "changedby",
                 "team", "role", "assignee", "participant", "participantid", "operator", "analyst",
                 "worker", "login", "machine", "hostname", "bot"],
    "application": ["applicationname", "application", "appname", "app", "applicationid", "program", "exe"],
    "window_title": ["simplifiedtitle", "windowtitle", "title"],
    "stage": ["stage", "phase", "level2processname", "level2", "processgroup", "group", "subprocess"],
    "level1": ["level1processname", "level1", "process", "processname"],
    "level3": ["level3processname", "level3", "subprocess"],
    "step_run": ["abstractioninstanceid", "instanceid", "stepinstance", "taskinstance", "activityinstance"],
    "active_time": ["activetime", "handlingtime", "worktime", "duration"],
    "wait_time": ["waittime", "waitingtime"],
    "tat_time": ["tattime", "turnaroundtime", "tat"],
}

# Name endings that mean "this column holds a code for something", e.g. task_id
# or step_code. Such a column names the same thing as task_name, but in a form
# people cannot read, so it should not win the activity role.
CODE_SUFFIXES = ("id", "code", "key", "no", "num", "hash", "guid", "uuid")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def name_score(column: str, role: str) -> float:
    n = _norm(column)
    hints = NAME_HINTS[role]
    # task_id is the code of a task, not its name: let task_name win the activity role.
    if role == "activity" and n.endswith(CODE_SUFFIXES) and n not in hints:
        return 0.2
    if n in hints:
        return 1.0
    if any(h in n for h in hints if len(h) >= 4):
        return 0.6
    return 0.0


# --------------------------------------------------------------------------- #
# Timestamps
# --------------------------------------------------------------------------- #
# A time written with its own offset at the end: "Z", "+05:30", "-0400".
OFFSET_PATTERN = r"(Z|z|[+-][0-9]{2}:?[0-9]{2})$"


def _local_to_utc(expr: str, tz: str) -> str:
    """Read a time WITHOUT an offset as local time in `tz`, then convert it to UTC.

    When `tz` is empty or already UTC, the expression is returned unchanged.
    """
    if tz and tz != "UTC":
        return f"timezone('UTC', timezone({sql_literal(tz)}, {expr}))"
    return expr


def timestamp_sql(column: str, fmt: str, tz: str = "UTC") -> str:
    """SQL expression that turns a column into a UTC TIMESTAMP (NULL if it fails).

    Which timezone is used depends on what each value says about itself:
    - Times that carry their own offset ("10:00+05:30", "10:00Z") and columns
      whose type is already timezone-aware use THAT offset. The timezone chosen
      on screen is ignored for them, because the value itself is more reliable.
    - Epoch numbers are always UTC by definition.
    - Only times WITHOUT an offset are read in the timezone chosen on screen
      and then converted to UTC.
    """
    q = quote_ident(column)
    if fmt == "native_tz":
        # Timezone-aware column type: convert from its own offset to UTC.
        return f"CAST(TRY_CAST({q} AS TIMESTAMPTZ) AT TIME ZONE 'UTC' AS TIMESTAMP)"
    if fmt == "native":
        return _local_to_utc(f"TRY_CAST({q} AS TIMESTAMP)", tz)
    if fmt == "iso":
        text = f"trim(CAST({q} AS VARCHAR))"
        # Each value is checked on its own: one file can mix times with and without offsets.
        return (
            f"CASE WHEN regexp_matches({text}, {sql_literal(OFFSET_PATTERN)}) "
            f"THEN CAST(TRY_CAST({text} AS TIMESTAMPTZ) AT TIME ZONE 'UTC' AS TIMESTAMP) "
            f"ELSE {_local_to_utc(f'TRY_CAST({text} AS TIMESTAMP)', tz)} END"
        )
    if fmt == "epoch_s":
        return f"CAST(to_timestamp(TRY_CAST({q} AS DOUBLE)) AS TIMESTAMP)"  # epoch is already UTC
    if fmt == "epoch_ms":
        return f"CAST(to_timestamp(TRY_CAST({q} AS DOUBLE) / 1000) AS TIMESTAMP)"
    return _local_to_utc(f"try_strptime(CAST({q} AS VARCHAR), {sql_literal(fmt)})", tz)


def detect_timestamp_format(con: duckdb.DuckDBPyConnection, table: str, column: str,
                            dtype: str) -> tuple[str | None, float]:
    """Try every known format on the sample; return (best format, parse rate)."""
    q = quote_ident(column)
    non_null = con.execute(f"SELECT COUNT({q}) FROM {table}").fetchone()[0]
    if non_null == 0:
        return None, 0.0
    if "WITH TIME ZONE" in dtype:
        return "native_tz", 1.0  # the column type itself carries the offset
    if dtype.startswith(("TIMESTAMP", "DATE")):
        return "native", 1.0

    candidates: list[str] = []
    is_number = dtype.startswith(("BIGINT", "INTEGER", "HUGEINT", "DOUBLE", "FLOAT", "DECIMAL", "UBIGINT"))
    if is_number:
        # Only plausible epoch values (years ~2001-2033) count as timestamps.
        lo, hi = con.execute(f"SELECT MIN({q}), MAX({q}) FROM {table}").fetchone()
        if lo is not None and 1e9 <= lo and hi <= 2e9:
            candidates.append("epoch_s")
        elif lo is not None and 1e12 <= lo and hi <= 2e12:
            candidates.append("epoch_ms")
        else:
            return None, 0.0
    else:
        candidates = ["iso"] + settings()["mapping"]["timestamp_formats"]
        # Fast pre-screen on 2,000 values: trying 15 formats on 200k text values
        # that are clearly not dates (names, IDs) wastes ~1 second per column.
        probe = (f"(SELECT {q} FROM {table} WHERE {q} IS NOT NULL "
                 f"AND regexp_matches(CAST({q} AS VARCHAR), '\\d') LIMIT 2000)")
        probe_n = con.execute(f"SELECT COUNT(*) FROM {probe}").fetchone()[0]
        if probe_n == 0:
            return None, 0.0
        candidates = [
            fmt for fmt in candidates
            if con.execute(f"SELECT COUNT({timestamp_sql(column, fmt)}) FROM {probe}").fetchone()[0]
            >= 0.3 * probe_n
        ]
        if not candidates:
            return None, 0.0

    best, best_rate = None, 0.0
    for fmt in candidates:
        parsed = con.execute(
            f"SELECT COUNT({timestamp_sql(column, fmt)}) FROM {table}"
        ).fetchone()[0]
        rate = parsed / non_null
        if rate > best_rate + 1e-9:
            best, best_rate = fmt, rate
        if best_rate >= 0.999:
            break
    return (best, round(best_rate, 4)) if best_rate > 0 else (None, 0.0)


def ambiguous_day_month(con: duckdb.DuckDBPyConnection, table: str, column: str, fmt: str) -> bool:
    """True when dd/mm and mm/dd both parse every value (e.g. all days <= 12).

    Then 03/04 could be 3 April or 4 March, and the user must choose.
    """
    if not fmt or "%d" not in fmt or "%m" not in fmt:
        return False
    swapped = fmt.replace("%d", "§").replace("%m", "%d").replace("§", "%m")
    q = quote_ident(column)
    a, b, n = con.execute(
        f"SELECT COUNT({timestamp_sql(column, fmt)}), COUNT({timestamp_sql(column, swapped)}), "
        f"COUNT({q}) FROM {table}"
    ).fetchone()
    return n > 0 and a == n and b == n


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #
@dataclass
class Suggestion:
    role: str
    column: str | None
    confidence: float
    reasons: list[str]
    needs_review: bool


def _data_score(col, role: str, min_ts_rate: float) -> tuple[float, str]:
    """How well the VALUES fit the role. Returns (score 0-1, reason)."""
    is_time = col.ts_parse_rate >= min_ts_rate
    text_like = col.dtype.startswith(("VARCHAR", "STRING", "TEXT"))
    if role in ("timestamp", "end_timestamp"):
        if is_time:
            return 1.0, f"{col.ts_parse_rate:.0%} of values parse as dates ({col.ts_format})"
        return col.ts_parse_rate * 0.4, f"only {col.ts_parse_rate:.0%} parse as dates"
    if is_time:
        return 0.0, "values are dates"
    r = col.distinct_ratio
    if role == "case_id":
        if 0.0005 <= r <= 0.7:
            return 1.0, f"{_distinct(col)} distinct values, each repeated ~{1 / max(r, 1e-9):.1f} times"
        if r > 0.95:
            return 0.1, "almost every value is unique (looks like an event ID)"
        return 0.3, f"{_distinct(col)} distinct values"
    if role == "activity":
        # Hashes, UUIDs and long IDs are unreadable on a process map.
        if col.code_like >= settings()["mapping"]["code_like_share"]:
            example = col.samples[0][:20] if col.samples else ""
            return 0.2, f"values look like codes (e.g. {example}…)"
        if 2 <= col.distinct <= 300:
            return (1.0 if text_like else 0.6), f"{_distinct(col)} distinct values"
        if col.distinct <= 2000:
            return 0.4, f"{_distinct(col)} distinct values (many for activities)"
        return 0.05, f"{_distinct(col)} distinct values (too many for activities)"
    if role == "resource":
        if 2 <= col.distinct <= 5000 and r < 0.5:
            return (0.8 if text_like else 0.5), f"{_distinct(col)} distinct values"
        return 0.1, f"{_distinct(col)} distinct values"
    if role == "application":
        if 1 <= col.distinct <= 5000 and r < 0.5:
            return (0.8 if text_like else 0.4), f"{_distinct(col)} distinct values"
        return 0.1, f"{_distinct(col)} distinct values"
    if role == "window_title":
        return (0.8 if text_like else 0.1), "free text" if text_like else "not text"
    if role == "action":
        return (0.8 if text_like and col.null_pct <= 50 else 0.0), "UI action labels"
    if role == "level1":
        return (1.0 if 1 <= col.distinct <= 15 else 0.0), f"{_distinct(col)} distinct values"
    if role == "level3":
        return (1.0 if 2 <= col.distinct <= 300 else 0.0), f"{_distinct(col)} distinct values"
    if role == "step_run":
        return (0.8 if col.distinct >= 10 else 0.0), f"{_distinct(col)} distinct values"
    if role in ("active_time", "wait_time", "tat_time"):   # read as a number at conversion (TRY_CAST); text exports are common
        numeric = any(t in col.dtype.upper() for t in ("INT", "DOUBLE", "FLOAT", "DECIMAL", "REAL"))
        return (0.8 if numeric else 0.6), "numbers" if numeric else "text, read as numbers"
    if role == "stage":
        if 2 <= col.distinct <= 15:
            return 1.0, f"{_distinct(col)} distinct values"
        return 0.0, f"{_distinct(col)} distinct values (a stage column has 2-15)"
    return 0.0, ""


def score(col, role: str) -> tuple[float, list[str]]:
    cfg = settings()["mapping"]
    data, data_reason = _data_score(col, role, cfg["min_timestamp_parse_rate"])
    name = name_score(col.name, role)
    if role == "timestamp" and name_score(col.name, "end_timestamp") >= 0.6:
        name = min(name, 0.2)  # "end_time" should not win the START role
    if role == "end_timestamp" and name < 0.6:
        return 0.0, []  # an end column is only proposed when its name says so
    if role == "application" and name < 0.6:
        return 0.0, []  # an application column is only proposed when its name says so
    if role == "window_title" and name < 1.0:
        return 0.0, []  # exact names only: a wrong guess would leave a column out of the data
    if role == "stage" and name < 0.6:
        return 0.0, []  # a stage column is only proposed when its name says so
    if role in ("level1", "level3", "step_run", "active_time", "wait_time", "tat_time") and name < 1.0:
        return 0.0, []  # exact names only: these roles are optional extras
    weight_name = 0.35 if role in ("timestamp", "end_timestamp") else 0.55
    conf = round(weight_name * name + (1 - weight_name) * data, 2)
    if data == 0:
        conf = 0.0  # a perfect name cannot rescue values that do not fit
    reasons = []
    if name >= 1.0:
        reasons.append("column name matches")
    elif name > 0:
        reasons.append("column name partly matches")
    if data_reason:
        reasons.append(data_reason)
    return conf, reasons


def step_run_fits(con, run_col: str, activity_col: str | None, view: str = "src") -> bool:
    """A step run ID must stand for one execution of ONE step: most of its values (more than
    half) have rows of a single activity."""
    if not activity_col:
        return False
    single, total = con.execute(f"""
        SELECT COUNT(*) FILTER (WHERE n = 1), COUNT(*) FROM (
            SELECT COUNT(DISTINCT {quote_ident(activity_col)}) AS n FROM {view}
            WHERE {quote_ident(run_col)} IS NOT NULL GROUP BY {quote_ident(run_col)})""").fetchall()[0]
    return bool(total) and single / total > 0.5


def suggest_mapping(profile, skip_roles=()) -> dict[str, Suggestion]:
    """Pick the best column per role; a column is used for one role only."""
    threshold = settings()["mapping"]["min_suggestion_confidence"]
    used: set[str] = set()
    out: dict[str, Suggestion] = {}
    # "_ingest_file" (added by ingest.open_source) is the source file name, never a role.
    candidates = [c for c in profile.columns if c.name != "_ingest_file"]
    # A file that already carries the process hierarchy (Process Level 1 / Level 2 / Level 3, e.g. a task-mining
    # export): Level 3 is the step (activity), Level 2 the stage, Level 1 the process, and the click column
    # (activity alias / action) is Level 4. Without this the click column would win "activity" by its name.
    def _level(n: int):
        return next((c for c in candidates if re.search(rf"level{n}(processname|name)?$", _norm(c.name))
                     or _norm(c.name) in (f"processlevel{n}", f"level{n}processname", f"l{n}")), None)
    l1, l2, l3 = _level(1), _level(2), _level(3)
    # ... only when the file has no step column of its own ("activity", "task", ...); with one, that column is the
    # activity and Level 3 is the level above it (the usual rules below map it).
    own_step = any(name_score(c.name, "activity") == 1.0 and c not in (l1, l2, l3) for c in candidates)
    if l3 is not None and not own_step and "activity" not in skip_roles:
        reason = ["the file has process levels: Level 3 is the step"]
        out["activity"] = Suggestion("activity", l3.name, 0.95, reason, False)
        used.add(l3.name)
        for role, col, why in (("stage", l2, "Level 2 is the stage"), ("level1", l1, "Level 1 is the process")):
            if col is not None and role not in skip_roles:
                out[role] = Suggestion(role, col.name, 0.95, [why], False)
                used.add(col.name)
        clicks = sorted((c for c in candidates if c.name not in used and c.distinct > l3.distinct
                         and max(name_score(c.name, "action"), name_score(c.name, "activity")) >= 0.6
                         and name_score(c.name, "step_run") < 1.0 and c.null_pct <= 50),
                        key=lambda c: -max(name_score(c.name, "action"), name_score(c.name, "activity")))
        if clicks and "action" not in skip_roles:
            out["action"] = Suggestion("action", clicks[0].name, 0.9, ["clicks inside a Level 3 step (Level 4)"], False)
            used.add(clicks[0].name)
    for role in ("timestamp", "case_id", "activity", "end_timestamp", "resource", "application", "window_title",
                 "stage", "action", "level1", "level3", "step_run", "active_time", "wait_time", "tat_time"):
        if role in out:
            continue
        if role in skip_roles:
            out[role] = Suggestion(role, None, 0.0, ["derived rather than mapped"], False)
            continue
        activity = next((c for c in candidates if c.name == getattr(out.get("activity"), "column", None)), None)
        ranked = sorted(
            ((score(c, role), c.name) for c in candidates if c.name not in used
             and (role != "action" or (activity is not None and c.distinct >= 3 * max(activity.distinct, 1)
                                       and c.null_pct <= 50 and name_score(c.name, role) >= 0.6
                                       and name_score(c.name, "step_run") < 1.0))),
            key=lambda x: x[0][0], reverse=True,
        )
        if ranked and ranked[0][0][0] > 0.15:
            (conf, reasons), name = ranked[0]
            if role in OPTIONAL_ROLES and conf < threshold:
                out[role] = Suggestion(role, None, 0.0, ["no confident match; optional"], False)
                continue
            used.add(name)
            out[role] = Suggestion(role, name, conf, reasons, conf < threshold)
        else:
            out[role] = Suggestion(role, None, 0.0, ["no suitable column found"], role in REQUIRED_ROLES)
    return out


# --------------------------------------------------------------------------- #
# Validation of the user's confirmed mapping (full data)
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    name: str
    ok: bool
    severity: str  # "error" blocks conversion, "warning" is shown only, "info" is a plain fact
    message: str


def scope_where(mapping: dict) -> str:
    """SQL condition that keeps only the rows the user chose to keep.

    Real logs mix process events with unrelated ones (other apps, system noise,
    other processes). The user decides on page 1 what is in scope, using the
    options stored in mapping["scope"]. Nothing is dropped silently: the number
    of rows left out is always reported. Returns "TRUE" when nothing is filtered.
    """
    scope = mapping.get("scope") or {}
    parts: list[str] = []
    if scope.get("drop_missing_case") and mapping.get("case_id"):
        parts.append(f"{quote_ident(mapping['case_id'])} IS NOT NULL")
    if scope.get("drop_missing_activity") and mapping.get("activity"):
        parts.append(f"{quote_ident(mapping['activity'])} IS NOT NULL")
    if scope.get("filter_column") and scope.get("filter_values"):
        values = ", ".join(sql_literal(str(v)) for v in scope["filter_values"])
        parts.append(f"CAST({quote_ident(scope['filter_column'])} AS VARCHAR) IN ({values})")
    return " AND ".join(parts) if parts else "TRUE"


def validate_mapping(con: duckdb.DuckDBPyConnection, mapping: dict, rows: int,
                     app_map: dict | None = None) -> list[Check]:
    """Check the user's confirmed mapping on the FULL data (only the rows in scope).

    Principle: upload never blocks on messy data; it measures it.
    Only real blockers are errors and stop conversion:
        - a required role is not chosen,
        - one column is used for two roles,
        - no rows are left after the user's filters,
        - almost no timestamps parse (the date format is surely wrong).
    Everything else (missing values, odd counts) is a warning with a suggested
    fix, because the quality gate on page 2 decides how far the data can be trusted.
    With an Application column, the application mapping is checked too
    (`app_map` overrides the shared map, e.g. for a command-line run).
    """
    v = settings()["mapping"]["validation"]
    min_rate = settings()["mapping"]["min_timestamp_parse_rate"]

    # 1. Blocker: a required role is not chosen.
    missing = [ROLE_LABELS[r] for r in REQUIRED_ROLES if not mapping.get(r)]
    if missing:
        return [Check("Required fields", False, "error", "Please choose: " + ", ".join(missing))]

    # 2. Blocker: one column is used for two roles.
    chosen = [mapping[r] for r in ALL_ROLES if mapping.get(r)]
    if len(chosen) != len(set(chosen)):
        return [Check("Distinct columns", False, "error", "The same column is used for two roles.")]

    # 3. One pass over the rows in scope gives every count we need.
    where = scope_where(mapping)
    tz = mapping.get("timezone") or "UTC"
    case_q, act_q = quote_ident(mapping["case_id"]), quote_ident(mapping["activity"])
    ts_expr = timestamp_sql(mapping["timestamp"], mapping["timestamp_format"], tz)
    kept, case_nn, act_nn, ts_nn, ts_ok, n_cases, n_acts = con.execute(f"""
        SELECT COUNT(*), COUNT({case_q}), COUNT({act_q}), COUNT({quote_ident(mapping['timestamp'])}),
               COUNT({ts_expr}), COUNT(DISTINCT {case_q}), COUNT(DISTINCT {act_q})
        FROM src WHERE {where}
    """).fetchone()

    # 4. Blocker: the filters left nothing to convert.
    if kept == 0:
        return [Check("Rows in scope", False, "error", "No rows are left after your filters.")]

    # 5. Plain fact: how many rows the filters keep and leave out.
    left_out = max(rows - kept, 0)
    checks = [Check("Rows in scope", True, "info",
                    f"{kept:,} rows kept; {left_out:,} ({left_out / rows if rows else 0:.1%}) "
                    "left out by your filters.")]

    # 6. Timestamps: an error only when the format is clearly wrong.
    rate = ts_ok / ts_nn if ts_nn else 0
    blocked = rate < v["block_if_parse_rate_below"]
    checks.append(Check(
        "Timestamp parsing", rate >= min_rate, "error" if blocked else "warning",
        f"{rate:.1%} of timestamps parse with format '{mapping['timestamp_format']}'."
        + (" The date format is almost certainly wrong." if blocked else ""),
    ))
    if mapping.get("end_timestamp"):
        end_expr = timestamp_sql(mapping["end_timestamp"], mapping["end_timestamp_format"], tz)
        e_nn, e_ok = con.execute(
            f"SELECT COUNT({quote_ident(mapping['end_timestamp'])}), COUNT({end_expr}) "
            f"FROM src WHERE {where}"
        ).fetchone()
        e_rate = e_ok / e_nn if e_nn else 0
        checks.append(Check("End timestamp parsing", e_rate >= min_rate, "warning",
                            f"{e_rate:.1%} of end timestamps parse."))

    # 7. Missing case IDs / activities among the KEPT rows: always a warning,
    #    with the fix (leave them out if they are unrelated activity).
    for label, nn, tick in (("Case ID", case_nn, "Leave out rows without a case ID"),
                            ("Activity", act_nn, "Leave out rows without an activity")):
        miss = 1 - nn / kept
        ok = miss <= v["warn_if_missing_above"]
        message = f"{miss:.1%} of the rows kept have no {label.lower()}."
        if not ok:
            message += (f" If these rows are unrelated activity, tick '{tick}'. "
                        "Otherwise the quality check will count them as missing data.")
        checks.append(Check(f"{label} completeness", ok, "warning", message))

    # 8. About one event per case usually means an event ID was chosen as the case ID.
    min_per_case = v["min_events_per_case"]
    per_case = case_nn / n_cases if n_cases else 0
    checks.append(Check("Events per case", per_case >= min_per_case, "warning",
                        f"{n_cases:,} cases, {per_case:.1f} events per case on average."
                        + ("" if per_case >= min_per_case else
                           " Almost one event per case: is this really the case ID?")))

    # 9. Too few or too many activity names makes an unreadable map.
    max_acts = v["max_activities"]
    acts_ok = 2 <= n_acts <= max_acts
    checks.append(Check("Number of activities", acts_ok, "warning",
                        f"{n_acts:,} distinct activities."
                        + ("" if acts_ok else
                           " Is this the right column? Try a less detailed one (task, step, stage).")))
    if mapping.get("application"):
        checks += application_checks(con, mapping["application"], where, app_map)
    return checks


def application_checks(con: duckdb.DuckDBPyConnection, app_col: str, where: str = "TRUE",
                       app_map: dict | None = None) -> list[Check]:
    """Warnings about the application mapping (never blocking).

    - masked applications without a real name: they will show as "Unnamed app N";
    - two recorded values with the same real name: allowed, they become one application.
    """
    from app.core import apps  # local import avoids a cycle

    the_map = apps.load_map() if app_map is None else app_map
    counts = apps.app_counts(con, app_col, where)
    total = sum(n for _, n in counts) or 1
    unnamed = [(raw, n) for raw, n in counts
               if apps.is_masked(raw) and not (the_map.get(raw) or {}).get("real_name")]
    checks: list[Check] = []
    label = settings()["apps"]["unnamed_label"]
    if unnamed:
        share = sum(n for _, n in unnamed) / total
        shown = ", ".join(f"{raw[:8]}… ({n:,} events)" for raw, n in unnamed[:10])
        more = f" and {len(unnamed) - 10} more" if len(unnamed) > 10 else ""
        checks.append(Check(
            "Application names", False, "warning",
            f"{len(unnamed)} masked applications are not mapped ({share:.1%} of events). "
            f"They will appear as '{label} N': {shown}{more}."))
    else:
        checks.append(Check("Application names", True, "warning",
                            f"All {len(counts)} applications have a readable name."))
    by_name: dict[str, list[str]] = {}
    for raw, _ in counts:
        name = (the_map.get(raw) or {}).get("real_name")
        if name:
            by_name.setdefault(name, []).append(raw)
    for name, raws in by_name.items():
        if len(raws) > 1:
            checks.append(Check("Combined applications", True, "info",
                                f"{len(raws)} recorded values are mapped to '{name}': "
                                "these will be combined as one application."))
    return checks


# --------------------------------------------------------------------------- #
# Personal data detection
# --------------------------------------------------------------------------- #
# First words of action labels ("Select Vendor", "Open Ticket"). Such labels are
# two capitalised words, like a person's name, but they are not names.
_ACTION_WORDS = {
    "select", "click", "enter", "open", "close", "create", "update", "save", "submit", "edit",
    "view", "search", "approve", "reject", "send", "check", "add", "delete", "copy", "paste",
    "type", "press", "navigate", "go", "review", "receive", "pay", "change", "cancel", "start",
    "end", "assign", "upload", "download", "print", "login", "logout", "scroll", "switch",
}


def _name_tokens(column: str) -> list[str]:
    """Split a column name into lower-case words: "customerEmail_2" -> ["customer", "email", "2"]."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", column)  # split camelCase
    return [t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t]


def sensitive_columns(profile, mapped: set[str]) -> dict[str, str]:
    """Find columns that probably hold personal data. Returns {column: reason}.

    Mapped role columns are skipped (they are needed for the analysis). A column
    is flagged when its NAME suggests personal data, or when its VALUES look like
    email addresses or person names. Found columns are pre-selected on page 1 to
    be left out of the converted file; the user can change the selection.
    """
    cfg = settings()["mapping"]["sensitive"]
    share = cfg["value_share"]
    found: dict[str, str] = {}
    for col in profile.columns:
        if col.name in mapped:
            continue
        # 1. The name. Matching whole words avoids false hits such as "pan" inside "company".
        tokens = _name_tokens(col.name)
        joined = "".join(tokens)
        hint = next((h for h in cfg["name_hints"]
                     if h in tokens or h == joined or (len(h) >= 8 and joined.endswith(h))), None)
        if hint:
            found[col.name] = f"column name suggests personal data ('{hint}')"
            continue
        # 2. Values that look like email addresses.
        if col.email_like >= share:
            found[col.name] = f"{col.email_like:.0%} of values look like email addresses"
            continue
        # 3. Person names: only for columns with many distinct values,
        #    because app or team names also look like names but are few.
        if col.distinct >= cfg["person_name_min_distinct"]:
            if col.person_like >= share:
                found[col.name] = f"{col.person_like:.0%} of values look like person names"
            elif col.code_like >= 0.5 and col.person_in_readable >= share:
                # Mostly hashed, but some real names slipped through.
                found[col.name] = (f"mostly codes, but {col.person_in_readable:.0%} of the readable "
                                   "values look like person names (anonymisation looks incomplete)")
    return found


# --------------------------------------------------------------------------- #
# Saved mapping profiles (same file layout -> same mapping next time)
# --------------------------------------------------------------------------- #
def signature(column_names: list[str]) -> str:
    """Fingerprint of a file layout. "_ingest_file" (added on loading, not part of the
    file) is ignored, so layouts saved before it existed still match."""
    names = sorted(c for c in column_names if c != "_ingest_file")
    return hashlib.sha1("|".join(names).encode()).hexdigest()[:16]


def _profiles_dir():
    d = data_dir() / "mapping_profiles"
    d.mkdir(exist_ok=True)
    return d


def save_profile(column_names: list[str], mapping: dict) -> None:
    with open(_profiles_dir() / f"{signature(column_names)}.json", "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)


def load_profile(column_names: list[str]) -> dict | None:
    path = _profiles_dir() / f"{signature(column_names)}.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def suggestion_dict(s: Suggestion) -> dict:
    return asdict(s)
