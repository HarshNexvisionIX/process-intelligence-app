"""Derived cases and activities: keep working when the export lacks a case ID or an activity.

Case ID stays required for a process analysis, but when no column holds it the user
can DERIVE it (page 1 "Case ID: derive it", run_pipeline --derive-case):

    text_pattern  an ID found in a text column (ticket number, code, masked #hex# token)
    work_session  user + work session (a new session after a pause)
    time_window   a calendar day or hour (per user/machine when there is such a column)
    record_join   the record whose time window (from a system-of-record export) contains the event
    overview      no case at all: "activity overview only" (no map, variants or waits)

Every derived case ID is a salted SHA-256 hash (salt: CASE_SALT in .env, or a random
salt kept in the data folder), so raw ticket or employee numbers never reach
events.parquet, reports or the AI. When there is no activity column, an activity can
be derived from the application and a cleaned window title ("screens").

Derivations are added as extra columns of the `src` view (__derived_case,
__derived_activity) just before `normalise`; the original view is kept as `src_raw`.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
from pathlib import Path

import duckdb
import pandas as pd

from app.core.config import data_dir, settings
from app.core.db import quote_ident, sql_literal

CASE_COL = "__derived_case"
ACTIVITY_COL = "__derived_activity"
KINDS = ("column", "text_pattern", "work_session", "time_window", "record_join", "overview")
WORK_SESSION_NOTE = ("Cases are WORK SESSIONS, not business cases: the map shows how people work in one sitting, "
                     "not the end-to-end business flow.")
TIME_WINDOW_NOTE = ("Cases are fixed TIME WINDOWS, not business cases: steps of different records done on the same "
                    "day are mixed. Use this only to get a first impression.")
SCREEN_NOTE = "Activities are screens, not business steps — map them to steps in the business definition."
NO_CASE_REASON = "Not available: no case"


def _cfg() -> dict:
    return settings()["derived_case"]


# --------------------------------------------------------------------------- #
# Labels and caps
# --------------------------------------------------------------------------- #
def case_kind(mapping: dict | None) -> str:
    return ((mapping or {}).get("case_source") or {}).get("kind") or "column"


NO_PATTERN_YET = "No ID pattern found yet — pick a column and click Find"


def case_label(mapping: dict | None) -> str:
    """How cases are defined, in words: "Case = work session (30-min gap)"."""
    cs = (mapping or {}).get("case_source") or {}
    kind = cs.get("kind") or "column"
    if kind == "column":
        return f"Case = column {(mapping or {}).get('case_id')}"
    if kind == "text_pattern":
        if not cs.get("pattern"):         # page 1 before "Find IDs in text columns" found one
            return NO_PATTERN_YET
        label = _cfg()["id_patterns"].get(cs.get("pattern"), {}).get("label", cs.get("pattern"))
        return f"Case = {str(label).lower()} found in {cs.get('column')}"
    if kind == "work_session":
        return f"Case = work session ({cs.get('gap_minutes', _cfg()['session_gap_minutes'])}-min gap)"
    if kind == "time_window":
        per = f" per {cs['group']}" if cs.get("group") else ""
        return f"Case = calendar {cs.get('window', 'day')}{per}"
    if kind == "record_join":
        return "Case = record from the system-of-record export (by user and time window)"
    return "No case: activity overview only"


def flow_caps(mapping: dict | None) -> list[tuple[str, str]]:
    """The highest flow band each derivation allows, with the reason: [(band, reason)]."""
    mapping = mapping or {}
    cs = mapping.get("case_source") or {}
    kind = cs.get("kind") or "column"
    caps: list[tuple[str, str]] = []
    if kind == "text_pattern" and not cs.get("confirmed"):
        caps.append(("Medium", "the case ID was found in text and its meaning is not yet confirmed by the business"))
    elif kind == "record_join":
        caps.append(("Medium", "cases come from joining a system-of-record export by user and time"))
    elif kind == "work_session":
        caps.append(("Medium", WORK_SESSION_NOTE))
    elif kind == "time_window":
        caps.append(("Low", TIME_WINDOW_NOTE))
    elif kind == "overview":
        caps.append(("None", "no case: activity overview only"))
    if mapping.get("derived_activity"):
        caps.append(("Low", SCREEN_NOTE))
    return caps


def mapping_of(folder: Path) -> dict:
    """The job's mapping.json (empty when there is none)."""
    try:
        with open(Path(folder) / "mapping.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# Salted hashing
# --------------------------------------------------------------------------- #
def case_salt() -> str:
    """CASE_SALT from .env, or a random salt created once and kept in the data folder."""
    salt = os.getenv("CASE_SALT")
    if salt:
        return salt
    path = data_dir() / ".case_salt"
    if not path.exists():
        path.write_text(secrets.token_hex(16), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def hash_case_sql(expr: str) -> str:
    """SQL: the salted case hash used for every stored case ID (mapped or derived)."""
    return _hashed(expr)


def _hashed(expr: str) -> str:
    """SQL: a salted SHA-256 of `expr` (NULL stays NULL); raw IDs never leave this expression."""
    n = int(_cfg()["hash_length"])
    return f"CASE WHEN ({expr}) IS NULL THEN NULL ELSE substr(sha256({sql_literal(case_salt())} || ({expr})), 1, {n}) END"


def _extract(column: str, pattern: str) -> str:
    """SQL: the ID matched by a configured pattern in a text column, or NULL."""
    p = _cfg()["id_patterns"][pattern]
    return (f"NULLIF(regexp_extract(CAST({quote_ident(column)} AS VARCHAR), {sql_literal(p['regex'])}, "
            f"{int(p.get('group', 0))}), '')")


# --------------------------------------------------------------------------- #
# 1. Finding IDs in text columns
# --------------------------------------------------------------------------- #
def find_id_patterns(con: duckdb.DuckDBPyConnection, text_columns: list[str], activity_col: str | None = None,
                     src_view: str = "src") -> list[dict]:
    """Look for repeating ID-like tokens in text columns (a sample of rows). Best first.

    For each pattern and column: share of rows with a match, distinct values, median
    rows per value, and the share of values that span 2+ activities (an ID that spans
    several activities is a case candidate).
    """
    cols = [c for c in dict.fromkeys(text_columns) if c]
    if not cols:
        return []
    keep = cols + ([activity_col] if activity_col and activity_col not in cols else [])
    n = int(_cfg()["sample_rows"])
    con.execute(f"CREATE OR REPLACE TEMP TABLE __idscan AS SELECT {', '.join(quote_ident(c) for c in keep)} "
                f"FROM {src_view} USING SAMPLE reservoir({n} ROWS) REPEATABLE (7)")
    act = f"CAST({quote_ident(activity_col)} AS VARCHAR)" if activity_col else "NULL"
    out = []
    names = list(_cfg()["id_patterns"])
    for col in cols:
        for i, name in enumerate(names):
            total, matched, distinct, median_rows, span = con.execute(f"""
                WITH m AS (SELECT {_extract(col, name)} AS v, {act} AS a FROM __idscan),
                     per AS (SELECT v, COUNT(*) AS n, COUNT(DISTINCT a) AS acts FROM m WHERE v IS NOT NULL GROUP BY v)
                SELECT (SELECT COUNT(*) FROM m), (SELECT COUNT(*) FROM m WHERE v IS NOT NULL), COUNT(*), median(n),
                       AVG(CASE WHEN acts >= 2 THEN 1.0 ELSE 0.0 END) FROM per""").fetchall()[0]
            if not matched:
                continue
            share = matched / total if total else 0.0
            span_share = float(span) if activity_col and span is not None else None
            candidate = distinct >= 2 and (median_rows or 0) >= 2 and (span_share is None or span_share > 0)
            out.append({"pattern": name, "label": _cfg()["id_patterns"][name]["label"], "column": col,
                        "match_share": round(share, 4), "distinct": int(distinct),
                        "median_rows_per_value": float(median_rows or 0),
                        "span_share": None if span_share is None else round(span_share, 4),
                        "candidate": candidate, "score": round(share * (span_share if span_share is not None else 1), 4),
                        "_order": i})
    con.execute("DROP TABLE IF EXISTS __idscan")
    out.sort(key=lambda r: (not r["candidate"], -r["score"], r["_order"], r["column"]))
    for r in out:
        r.pop("_order")
    return out


# --------------------------------------------------------------------------- #
# 2. Screen-level activities (no activity column)
# --------------------------------------------------------------------------- #
def _re2_words(words: list[str]) -> str:
    """An RE2 alternation of literal words ("View Worker|Profile"), punctuation escaped."""
    return "|".join(re.sub(r"([^A-Za-z0-9 ])", r"\\\1", w) for w in words)


def _screen_key_sql(title_col: str) -> str:
    """SQL: the window title without IDs, numbers, #hex# tokens or emails (names are removed in Python).

    "View Worker - <anything> - Workday": the part between a known person-screen prefix and an
    app suffix is always a record or person, so it becomes "<name>" (the app suffix is dropped:
    the app is shown in front of the screen name anyway).
    """
    cfg = settings()["derived_activity"]
    expr = f"CAST({quote_ident(title_col)} AS VARCHAR)"
    if cfg.get("name_screen_prefixes") and cfg.get("name_screen_apps"):
        pattern = (f"(?i)^\\s*({_re2_words(cfg['name_screen_prefixes'])})\\s+-\\s+.+?\\s+-\\s+"
                   f"({_re2_words(cfg['name_screen_apps'])})\\s*$")
        expr = f"regexp_replace({expr}, {sql_literal(pattern)}, {sql_literal(chr(92) + '1 - <name>')})"
    expr = f"regexp_replace({expr}, '#[0-9a-fA-F]+#', ' ', 'g')"
    expr = f"regexp_replace({expr}, '[^@\\s]+@[^@\\s]+\\.[A-Za-z]{{2,}}', ' ', 'g')"
    for p in _cfg()["id_patterns"].values():
        expr = f"regexp_replace({expr}, {sql_literal(p['regex'])}, ' ', 'g')"
    expr = f"regexp_replace({expr}, '[0-9]+', ' ', 'g')"
    return f"trim(regexp_replace({expr}, '[\\s|:·_\\-]+', ' ', 'g'))"


_PERSON_IN_TEXT = re.compile(r"\b[A-Z][a-z]+(?: [A-Z][a-zA-Z'-]+){1,2}\b")
NAME_TOKEN = "<name>"


def _phrase_is_person(phrase: str) -> bool:
    """The profile's person-name test (PERSON_RE, not starting with an action word), applied to
    one phrase; a phrase containing a known screen word ("Access", "Order") is screen text."""
    from app.core.ingest import PERSON_RE
    from app.core.mapping import _ACTION_WORDS
    words = phrase.lower().split()
    screen_words = {w.lower() for w in settings()["derived_activity"].get("screen_words", [])}
    return bool(PERSON_RE.match(phrase)) and words[0] not in _ACTION_WORDS \
        and not any(w in screen_words for w in words)


def _screen_label(app: str | None, key: str, kept: set[str]) -> str:
    """A readable screen name: capitalised phrases not in `kept` become "<name>", first
    `max_words` words, with the app."""
    text = _PERSON_IN_TEXT.sub(lambda m: m.group(0) if m.group(0) in kept else NAME_TOKEN, key or "")
    words: list[str] = []
    for w in text.split():
        if not (w == NAME_TOKEN and words and words[-1] == NAME_TOKEN):
            words.append(w)
    title = " ".join(words[: int(settings()["derived_activity"]["max_words"])])
    if app and title:
        return f"{app}: {title}"
    return app or title or ""


def sensitive_values(con: duckdb.DuckDBPyConnection, exclude: list[str], skip: set[str],
                     base: str = "src_raw") -> set[str]:
    """Lower-cased values (of two words or more) of the columns left out as sensitive on page 1
    and of every text column whose values look like person names (the profile's rule)."""
    from app.core.ingest import value_shapes
    cfg = settings()["mapping"]["sensitive"]
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {base}").fetchall()
            if r[0] not in skip and "VARCHAR" in str(r[1]).upper()]
    out: set[str] = set()
    for c in cols:
        vals = [r[0] for r in con.execute(
            f"SELECT DISTINCT CAST({quote_ident(c)} AS VARCHAR) FROM {base} "
            f"WHERE {quote_ident(c)} IS NOT NULL").fetchall()]
        flagged = c in (exclude or [])
        if not flagged and len(vals) >= cfg["person_name_min_distinct"]:
            shapes = value_shapes(vals[:5000])
            flagged = shapes["person_like"] >= cfg["value_share"] or (
                shapes["code_like"] >= 0.5 and shapes["person_in_readable"] >= cfg["value_share"])
        if flagged:
            out |= {v.strip().lower() for v in vals if " " in v.strip()}
    return out


def screen_activity_table(con: duckdb.DuckDBPyConnection, app_col: str | None, title_col: str,
                          base: str = "src_raw", user_col: str | None = None,
                          sensitive: set[str] | None = None) -> pd.DataFrame:
    """(app value, cleaned title) -> screen activity; rare screens become "Other screen".

    A capitalised 2-3 word phrase stays in the screen name only when it is not person-like,
    is used by at least `min_users_for_phrase` users on at least `min_count` events, and is not
    a value of a sensitive / person-like column; otherwise it becomes "<name>".
    """
    from app.core import apps
    the_map = apps.load_map()
    cfg = settings()["derived_activity"]
    app_sql = f"CAST({quote_ident(app_col)} AS VARCHAR)" if app_col else "NULL"
    user_sql = f"CAST({quote_ident(user_col)} AS VARCHAR)" if user_col else "NULL"
    by_user = con.execute(f"""
        SELECT {app_sql} AS app, {_screen_key_sql(title_col)} AS k, {user_sql} AS u, COUNT(*) AS n
        FROM {base} WHERE {quote_ident(title_col)} IS NOT NULL GROUP BY 1, 2, 3""").fetchall()
    counts: dict[tuple, int] = {}
    users: dict[tuple, set] = {}
    for a, k, u, n in by_user:
        counts[(a, k)] = counts.get((a, k), 0) + n
        if u is not None:
            users.setdefault((a, k), set()).add(u)
    rows = [(a, k, n) for (a, k), n in counts.items()]
    per_app: dict[str, int] = {}
    for a, _, n in rows:
        if a is not None:
            per_app[a] = per_app.get(a, 0) + n
    numbers = apps.number_unnamed(list(per_app.items()), the_map) if app_col else {}
    min_count = int(cfg["min_count"])
    min_users = int(cfg["min_users_for_phrase"])
    phrase_events: dict[str, int] = {}
    phrase_users: dict[str, set] = {}
    for (a, k), n in counts.items():
        for phrase in set(_PERSON_IN_TEXT.findall(k or "")):
            phrase_events[phrase] = phrase_events.get(phrase, 0) + n
            phrase_users.setdefault(phrase, set()).update(users.get((a, k), set()))
    sensitive = sensitive or set()
    kept = {p for p, n in phrase_events.items()
            if not _phrase_is_person(p) and n >= min_count and len(phrase_users[p]) >= min_users
            and p.lower() not in sensitive}
    labelled = [(a, k, _screen_label(apps.resolve(a, the_map, numbers) if a else None, k, kept), n)
                for a, k, n in rows]
    totals: dict[str, int] = {}
    for _, _, label, n in labelled:
        totals[label] = totals.get(label, 0) + n
    return pd.DataFrame({
        "__app": pd.Series([a if a is not None else "" for a, _, _, _ in labelled], dtype=object),
        "__key": pd.Series([k if k is not None else "" for _, k, _, _ in labelled], dtype=object),
        "__screen": pd.Series([lab if lab and totals[lab] >= min_count else "Other screen"
                               for _, _, lab, _ in labelled], dtype=object)})


# --------------------------------------------------------------------------- #
# 3. Applying derivations before normalise
# --------------------------------------------------------------------------- #
def _records(path: str) -> pd.DataFrame:
    """A system-of-record export: record_id, user, start_ts, end_ts (CSV or Excel)."""
    p = Path(path)
    df = pd.read_excel(p, dtype=str) if p.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(p, dtype=str)
    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = {"record_id", "user", "start_ts", "end_ts"} - set(df.columns)
    if missing:
        raise ValueError(f"The record file needs the columns record_id, user, start_ts, end_ts (missing: "
                         f"{', '.join(sorted(missing))}).")
    out = pd.DataFrame({"record_id": df["record_id"].astype(str), "user": df["user"].astype(str)})
    for c in ("start_ts", "end_ts"):
        out[c] = pd.to_datetime(df[c], utc=True, errors="coerce").dt.tz_localize(None)
    return out.dropna()


def apply_derivations(con: duckdb.DuckDBPyConnection, mapping: dict) -> dict:
    """Add the derived case / activity columns to the `src` view; return the mapping to normalise with.

    Safe to call again (the original view is kept as `src_raw`). With nothing to
    derive the view is left as it was and the mapping is returned unchanged.
    """
    from app.core.mapping import timestamp_sql   # local import avoids a cycle

    kind = case_kind(mapping)
    derive_act = bool(mapping.get("derived_activity"))
    views = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
    if kind == "column" and not derive_act:
        if "src_raw" in views:
            con.execute("CREATE OR REPLACE VIEW src AS SELECT * FROM src_raw")
        return mapping
    if "src_raw" not in views:
        con.execute("ALTER VIEW src RENAME TO src_raw")
    cs = mapping.get("case_source") or {}
    tz = mapping.get("timezone") or "UTC"
    ts = timestamp_sql(mapping["timestamp"], mapping["timestamp_format"], tz)
    user = mapping.get("resource")
    user_sql = f"CAST({quote_ident(user)} AS VARCHAR)" if user else "NULL"
    gap = float(cs.get("gap_minutes", _cfg()["session_gap_minutes"])) * 60
    extra: list[str] = ["__rowid", "__ts", "__new", "__session", "__id", "__prev_id", "__next_id"]
    layers = [f"SELECT *, row_number() OVER () AS __rowid, {ts} AS __ts FROM src_raw"]

    def session_layer(part: str) -> None:
        layers.append(f"SELECT *, CASE WHEN epoch(__ts) - epoch(lag(__ts) OVER (PARTITION BY {part} ORDER BY __ts)) "
                      f"> {gap} THEN 1 ELSE 0 END AS __new FROM ({{prev}})")
        layers.append(f"SELECT *, SUM(__new) OVER (PARTITION BY {part} ORDER BY __ts ROWS UNBOUNDED PRECEDING) "
                      f"AS __session FROM ({{prev}})")

    new = copy.deepcopy(mapping)
    case_sql = None
    if kind == "text_pattern":
        layers.append(f"SELECT *, {_extract(cs['column'], cs['pattern'])} AS __id FROM ({{prev}})")
        if _cfg()["fill_within_session"]:
            session_layer(user_sql if user else "1")
            part = f"{user_sql}, __session" if user else "__session"
            layers.append(
                f"SELECT *, last_value(__id IGNORE NULLS) OVER (PARTITION BY {part} ORDER BY __ts, __rowid "
                f"ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS __prev_id, "
                f"first_value(__id IGNORE NULLS) OVER (PARTITION BY {part} ORDER BY __ts, __rowid "
                f"ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS __next_id FROM ({{prev}})")
            case_sql = _hashed("COALESCE(__id, CASE WHEN __prev_id = __next_id THEN __prev_id END)")
        else:
            case_sql = _hashed("__id")
        new.setdefault("scope", {})["drop_missing_case"] = True        # rows without an ID are counted as left out
    elif kind == "work_session":
        if not user:
            raise ValueError("Work sessions need a user / resource column.")
        part = f"{user_sql}, CAST(__ts AS DATE)" if cs.get("split_at_midnight", _cfg()["split_sessions_at_midnight"]) \
            else user_sql
        session_layer(part)
        case_sql = _hashed(f"{user_sql} || '|' || CAST(__session AS VARCHAR)"
                           + (" || '|' || CAST(CAST(__ts AS DATE) AS VARCHAR)" if "DATE" in part else ""))
    elif kind == "time_window":
        window = "hour" if cs.get("window") == "hour" else "day"
        group = f"COALESCE(CAST({quote_ident(cs['group'])} AS VARCHAR), '')" if cs.get("group") else "'all'"
        case_sql = _hashed(f"{group} || '|' || CAST(date_trunc('{window}', __ts) AS VARCHAR)")
    elif kind == "record_join":
        if not user:
            raise ValueError("Joining a record export needs a user / resource column.")
        con.register("__records", _records(cs["file"]))
        # The rows are used twice below: materialise them once so every row keeps one row id.
        con.execute(f"CREATE OR REPLACE TEMP TABLE __derive_base AS {layers[0]}")
        layers[0] = "SELECT * FROM __derive_base"
        end = (timestamp_sql(mapping["end_timestamp"], mapping["end_timestamp_format"], tz)
               if mapping.get("end_timestamp") else "__ts")
        layers.append(f"""
            SELECT e.*, j.__id FROM ({{prev}}) e LEFT JOIN (
                SELECT e2.__rowid, arg_max(r.record_id, struct_pack(o := epoch(least(r.end_ts, COALESCE({end}, e2.__ts)))
                                                                         - epoch(greatest(r.start_ts, e2.__ts)),
                                                                    s := r.start_ts)) AS __id
                FROM ({{prev}}) e2 JOIN __records r
                  ON r.user = CAST(e2.{quote_ident(user)} AS VARCHAR) AND e2.__ts BETWEEN r.start_ts AND r.end_ts
                GROUP BY e2.__rowid) j USING (__rowid)""")
        case_sql = _hashed("__id")
        new.setdefault("scope", {})["drop_missing_case"] = True        # events outside every window are left out
    elif kind == "overview":
        case_sql = _hashed(f"COALESCE({user_sql}, 'all events')")        # one "case" per user timeline
    if case_sql is not None:
        new["case_id"] = CASE_COL
    act_sql = None
    if derive_act:
        title, app = mapping.get("window_title"), mapping.get("application")
        if not title:
            raise ValueError("Deriving activities needs a window title column.")
        skip = {title, app} - {None}
        con.register("__screens", screen_activity_table(
            con, app, title, user_col=user,
            sensitive=sensitive_values(con, mapping.get("exclude_columns") or [], skip)))
        app_sql = f"COALESCE(CAST({quote_ident(app)} AS VARCHAR), '')" if app else "''"
        layers.append(f"""SELECT q.*, COALESCE(s.__screen, 'Other screen') AS __act FROM ({{prev}}) q
                          LEFT JOIN __screens s ON s.__app = {app_sql} AND s.__key = {_screen_key_sql(title)}""")
        extra.append("__act")
        act_sql = "__act"
        new["activity"] = ACTIVITY_COL
    sql = layers[0]
    for layer in layers[1:]:
        sql = layer.replace("{prev}", sql)
    present = {c[0] for c in con.execute(f"DESCRIBE SELECT * FROM ({sql})").fetchall()}
    drop = [c for c in extra if c in present]
    cols = ", ".join(x for x in ((f"{case_sql} AS {quote_ident(CASE_COL)}" if case_sql else None),
                                 (f"{act_sql} AS {quote_ident(ACTIVITY_COL)}" if act_sql else None)) if x)
    con.execute(f"CREATE OR REPLACE VIEW src AS SELECT * EXCLUDE ({', '.join(drop)}), {cols} FROM ({sql})")
    return new
