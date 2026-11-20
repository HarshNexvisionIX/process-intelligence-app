"""Data Insights (adopted from ProcessLens): deterministic, no AI.

Computed from the converted events of an analysis:
    no case ID  nc_events_clean.parquet (after cleaning; actor is already a salted hash)
    case ID     events.parquet (resource is the mapped user column)
People are shown as P01, P02 ... (ordered by hours, then by the hash), never by name or ID. Devices are not a
standard column: when the export had none, the section says so instead of guessing.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

COPY_WORDS = ("copy", "paste", "ctrl+c", "ctrl+v", "clipboard")


def _q(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _cols(con, path: Path) -> set[str]:
    return {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')").fetchall()}


def build(folder: Path, mode: str, min_active_days: int = 5) -> dict:
    path = folder / ("nc_events_clean.parquet" if mode == "nocase" else "events.parquet")
    if not path.exists():
        return {"available": False, "reason": "Convert the data first (Import data)."}
    con = duckdb.connect()
    cols = _cols(con, path)
    if mode == "nocase":
        who, app, act, scr = "actor", "app", "action_clean", "screen_clean"
        secs = "COALESCE(active_s, 0)" if "active_s" in cols else "0"
    else:
        who = "resource" if "resource" in cols else None
        app = "application" if "application" in cols else None
        act, scr = "activity", None
        dur = "epoch(ts_end) - epoch(ts_start)" if "ts_end" in cols else "NULL"
        secs = f"COALESCE(active_s, {dur}, 0)" if "active_s" in cols else f"COALESCE({dur}, 0)"
    ts = "ts" if mode == "nocase" else ("ts_start" if "ts_start" in cols else "timestamp")
    who_sql = f"CAST({who} AS VARCHAR)" if who else "'(no user column)'"
    app_sql = f"COALESCE(CAST({app} AS VARCHAR), '(no application)')" if app else "'(no application column)'"
    con.execute(f"""CREATE VIEW ev AS SELECT {ts} AS ts, {who_sql} AS who, {app_sql} AS app,
                   CAST({act} AS VARCHAR) AS act, {secs} AS secs FROM read_parquet('{path.as_posix()}')
                   WHERE {ts} IS NOT NULL""")
    con.execute("""CREATE VIEW masks AS SELECT who, 'P' || lpad(CAST(row_number() OVER (ORDER BY SUM(secs) DESC,
                   hash(who)) AS VARCHAR), 2, '0') AS person FROM ev GROUP BY who""")
    con.execute("CREATE VIEW e AS SELECT ev.*, m.person, CAST(ts AS DATE) AS day FROM ev JOIN masks m USING (who)")

    o = _q(con, """SELECT COUNT(*) AS events, COUNT(DISTINCT person) AS participants, COUNT(DISTINCT app) AS apps,
                   COUNT(DISTINCT day) AS days, MIN(day) AS first_date, MAX(day) AS last_date,
                   SUM(secs) / 3600.0 AS hours FROM e""")[0]
    o["first_date"], o["last_date"] = str(o["first_date"]), str(o["last_date"])
    daily = _q(con, "SELECT CAST(day AS VARCHAR) AS date, COUNT(DISTINCT person) AS participants, COUNT(*) AS events, "
                    "SUM(secs)/3600.0 AS hours FROM e GROUP BY day ORDER BY day")
    people = _q(con, f"""SELECT person AS participant, COUNT(DISTINCT day) AS active_days, COUNT(*) AS events,
                        SUM(secs)/3600.0 AS hours, CAST(MIN(day) AS VARCHAR) AS first_seen,
                        CAST(MAX(day) AS VARCHAR) AS last_seen,
                        CASE WHEN COUNT(DISTINCT day) >= {int(min_active_days)} THEN 'Regular' ELSE 'Occasional' END
                        AS attendance FROM e GROUP BY person ORDER BY person""")
    attendance = _q(con, "SELECT DISTINCT person AS participant, CAST(day AS VARCHAR) AS date FROM e")
    apps = _q(con, "SELECT app AS application, COUNT(*) AS events, COUNT(DISTINCT person) AS participants, "
                   "SUM(secs)/3600.0 AS hours FROM e GROUP BY app ORDER BY hours DESC, events DESC")
    app_daily = _q(con, "SELECT CAST(day AS VARCHAR) AS date, app AS application, SUM(secs)/3600.0 AS hours "
                        "FROM e GROUP BY day, app ORDER BY day")
    hour = _q(con, "SELECT hour(ts) AS hour, COUNT(*) AS events, SUM(secs)/3600.0 AS hours FROM e GROUP BY 1 "
                   "ORDER BY 1")
    weekday = _q(con, "SELECT isodow(ts) AS weekday, COUNT(*) AS events, SUM(secs)/3600.0 AS hours FROM e "
                      "GROUP BY 1 ORDER BY 1")
    con.execute("""CREATE VIEW sw AS SELECT person, day, app, LAG(app) OVER (PARTITION BY person, day ORDER BY ts)
                   AS prev FROM e""")
    switches = _q(con, "SELECT COUNT(*) AS n FROM sw WHERE prev IS NOT NULL AND prev <> app")[0]["n"]
    switch_daily = _q(con, "SELECT CAST(day AS VARCHAR) AS date, COUNT(*) AS switches FROM sw WHERE prev IS NOT NULL "
                           "AND prev <> app GROUP BY day ORDER BY day")
    switch_flows = _q(con, "SELECT prev AS from_application, app AS to_application, COUNT(*) AS switches FROM sw "
                           "WHERE prev IS NOT NULL AND prev <> app GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 25")
    like = " OR ".join(f"lower(act) LIKE '%{w}%'" for w in COPY_WORDS)
    copy = _q(con, f"SELECT app AS application, act AS action, COUNT(*) AS events FROM e WHERE {like} "
                   "GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 25")
    top_actions = _q(con, "SELECT act AS action, COUNT(*) AS events FROM e WHERE act IS NOT NULL GROUP BY 1 "
                          "ORDER BY 2 DESC LIMIT 15")
    con.close()
    regular = sum(1 for p in people if p["attendance"] == "Regular")
    return {"available": True, "source": path.name, "overview": o,
            "participation": {"daily": daily, "people": people, "attendance": attendance,
                              "regular": regular, "occasional": len(people) - regular,
                              "min_active_days": min_active_days,
                              "devices": None, "devices_note": "No device column is part of the standard columns; "
                                                               "people are counted by the user column."},
            "applications": {"by_app": apps, "daily": app_daily},
            "time": {"hour_of_day": hour, "weekday": weekday},
            "interactions": {"switches": int(switches), "switch_daily": switch_daily, "switch_flows": switch_flows,
                             "copy_paste": copy, "copy_paste_events": int(sum(c["events"] for c in copy)),
                             "top_actions": top_actions},
            "notes": ["Hours are active time where the export has it; otherwise end minus start; events without "
                      "either count as 0 hours.",
                      "Application switches are consecutive events of one person on one day in different "
                      "applications: navigation, not case handoffs.",
                      "Copy / paste is a keyword match on the click name; it shows possible manual re-keying, "
                      "not proven waste."]}
