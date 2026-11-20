"""Self-checks of the no-case report -> nc_checks.json. Independent SQL on nc_events_clean / nc_scope.json (not the
pandas code of measures.py) recomputes the numbers; counts must be exact, hours within nocase.checks.hours_tolerance.

    scope adds up; step hours = total; app hours = total; actor hours = total; matrix row and column totals;
    activity totals = step totals; step runs and repetitions; top edge counts; every fact equals its source and
    every placeholder used in the text exists.
If any check fails, the report title gets "DRAFT — K checks failed".
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.config import settings
from app.core.db import connect, sql_literal

CHECKS_FILE = "nc_checks.json"


def _tol() -> float:
    return float(settings()["nocase"]["checks"]["hours_tolerance"])


def _sec_sql(ev: str, basis: str) -> str:
    """The seconds of work per event, the same rule as measures.events_frame, written in SQL."""
    if basis == "measured":
        return f"SELECT *, COALESCE(active_s, 0) AS sec FROM {ev}"
    if basis == "start-end":
        return f"SELECT *, COALESCE(GREATEST(0, epoch(ts_end) - epoch(ts)), 0) AS sec FROM {ev}"
    cap = float(settings()["nocase"]["measures"]["estimate_cap_seconds"])
    return f"""SELECT *, COALESCE(LEAST({cap}, GREATEST(0, epoch(lead(ts) OVER (PARTITION BY actor ORDER BY ts, eid))
                                                      - epoch(ts))), 0) AS sec FROM {ev}"""


def run(folder: Path, texts: dict[str, str] | None = None) -> dict:
    from app.nocase.clean import CLEAN_FILE
    from app.nocase.engine.activities import ACT_FILE
    from app.nocase.engine.flow import load as load_flow
    from app.nocase.engine.measures import UNASSIGNED, load as load_measures
    from app.nocase.ingest import load_scope
    from app.nocase.report.facts import load as load_facts, resolve
    con = connect()
    m, flow = load_measures(folder), load_flow(folder)
    tol = _tol()
    ev = f"read_parquet({sql_literal((folder / CLEAN_FILE).as_posix())})"
    con.execute(f"CREATE OR REPLACE TEMP TABLE __ck AS {_sec_sql(ev, m['basis']['time'])}")
    checks: list[dict] = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    s = load_scope(folder) or {}
    check("scope adds up", s and s["rows_in_file"] == s["left_out_by_filter"] + s["unreadable_time"] + s["in_scope"],
          f"{s.get('rows_in_file')} = {s.get('left_out_by_filter')} + {s.get('unreadable_time')} + {s.get('in_scope')}")
    events, total = con.execute("SELECT COUNT(*), SUM(sec) / 3600 FROM __ck").fetchone()
    check("events in scope", events == s.get("in_scope") == m["overall"]["events"],
          f"{events} events (scope {s.get('in_scope')}, measures {m['overall']['events']})")
    check("total hours", abs((total or 0) - m["overall"]["hours"]) <= tol, f"{total} vs {m['overall']['hours']}")
    step_sql = {r[0]: r[1] for r in con.execute(
        f"SELECT COALESCE(step, '{UNASSIGNED}'), SUM(sec) / 3600 FROM __ck GROUP BY 1").fetchall()}
    mm = {x["step"]: x["hours"] for x in m["steps"]}
    check("step hours = SQL per step", all(abs(step_sql.get(k, 0) - v) <= tol for k, v in mm.items())
          and set(step_sql) == set(mm), f"{len(mm)} steps")
    check("step hours add up to the total", abs(sum(mm.values()) - (total or 0)) <= tol)
    app_sql = {r[0]: r[1] for r in con.execute(
        "SELECT COALESCE(app, '(no app)'), SUM(sec) / 3600 FROM __ck GROUP BY 1").fetchall()}
    ma = {x["app_label"]: x["hours"] for x in m["apps"]}
    check("app hours = SQL per app", all(abs(app_sql.get(k, 0) - v) <= tol for k, v in ma.items())
          and set(app_sql) == set(ma))
    check("app hours add up to the total", abs(sum(ma.values()) - (total or 0)) <= tol)
    check("actor hours add up to the total", abs(sum(x["hours"] for x in m["actors"]) - (total or 0)) <= tol)
    mx = m["matrix_step_app"]
    rows_ok = all(abs(rt - mm.get(r, 0)) <= tol for r, rt in zip(mx["rows"], mx["row_totals"]))
    cols_ok = all(abs(ct - ma.get(c, 0)) <= tol for c, ct in zip(mx["columns"], mx["column_totals"]))
    cells_ok = all(abs(sum(r) - rt) <= tol for r, rt in zip(mx["cells"], mx["row_totals"]))
    check("step x app matrix: row totals = step hours", rows_ok and cells_ok)
    check("step x app matrix: column totals = app hours", cols_ok)
    check("step x app matrix: grand total", abs(mx["grand_total"] - (total or 0)) <= tol)
    if (folder / ACT_FILE).exists():
        acts = f"read_parquet({sql_literal((folder / ACT_FILE).as_posix())})"
        bad = con.execute(f"""
            WITH a AS (SELECT step, SUM(events) e FROM {acts} GROUP BY 1),
                 s AS (SELECT step, COUNT(*) e FROM __ck WHERE step IS NOT NULL GROUP BY 1)
            SELECT COUNT(*) FROM s FULL JOIN a USING (step) WHERE a.e IS DISTINCT FROM s.e""").fetchone()[0]
        act_m = {}
        for a in m["activities"]:
            act_m[a["step"]] = act_m.get(a["step"], 0) + a["hours"]
        hrs_ok = all(abs(act_m.get(k, 0) - v) <= tol for k, v in mm.items() if k != UNASSIGNED)
        check("activity totals = step totals", bad == 0 and hrs_ok, f"{bad} step(s) differ in events")
    runs, reps = con.execute(f"""
        WITH o AS (SELECT actor, episode_id, COALESCE(step, '{UNASSIGNED}') AS st,
                          lag(COALESCE(step, '{UNASSIGNED}')) OVER (PARTITION BY actor, episode_id ORDER BY ts, eid) AS prev
                   FROM __ck),
             r AS (SELECT actor, episode_id, st FROM o WHERE prev IS NULL OR prev <> st)
        SELECT COUNT(*), COUNT(*) - (SELECT COUNT(*) FROM (SELECT DISTINCT actor, episode_id, st FROM r)) FROM r""").fetchone()
    check("step runs", runs == m["overall"]["step_runs"], f"{runs} vs {m['overall']['step_runs']}")
    check("repetitions", reps == m["overall"]["repetitions"], f"{reps} vs {m['overall']['repetitions']}")
    if flow:
        pairs = {(a, b): n for a, b, n in con.execute(f"""
            WITH o AS (SELECT actor, episode_id, ts, eid, COALESCE(step, '{UNASSIGNED}') AS st,
                              lag(COALESCE(step, '{UNASSIGNED}')) OVER (PARTITION BY actor, episode_id ORDER BY ts, eid) AS prev
                       FROM __ck),
                 r AS (SELECT actor, episode_id, st, row_number() OVER (PARTITION BY actor, episode_id ORDER BY ts, eid) k
                       FROM o WHERE prev IS NULL OR prev <> st),
                 x AS (SELECT * FROM r WHERE st <> '{UNASSIGNED}'),
                 y AS (SELECT st, lead(st) OVER (PARTITION BY actor, episode_id ORDER BY k) AS nx FROM x)
            SELECT st, nx, COUNT(*) FROM y WHERE nx IS NOT NULL AND nx <> st GROUP BY 1, 2""").fetchall()}
        top = sorted(flow.get("edges") or [], key=lambda e: -e["count"])[:int(settings()["nocase"]["checks"]["top_edges"])]
        check("top edge counts", all(pairs.get((e["src"], e["dst"])) == e["count"] for e in top), f"{len(top)} edges")
    facts = load_facts(folder)
    root = {"measures": m}
    if (folder / "nc_inside_step.json").exists():
        root["inside"] = json.loads((folder / "nc_inside_step.json").read_text(encoding="utf-8"))
    wrong = []
    for f in facts:
        try:
            src = resolve(root, f["ref"])
        except (KeyError, StopIteration, TypeError):
            wrong.append(f["id"])
            continue
        if (src is None) != (f["value"] is None) or (src is not None and abs(float(src) - float(f["value"])) > float(
                settings()["nocase"]["checks"]["fact_tolerance"])):
            wrong.append(f["id"])
    check("every fact equals its source", not wrong, ", ".join(wrong))
    ids = {f["id"] for f in facts}
    used = set(re.findall(r"\{\{(F\d{3})\}\}", " ".join((texts or {}).values())))
    check("every fact used in the text exists", used <= ids, ", ".join(sorted(used - ids)))
    out = {"passed": sum(c["passed"] for c in checks), "failed": sum(not c["passed"] for c in checks), "checks": checks}
    (folder / CHECKS_FILE).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def load(folder: Path) -> dict:
    p = folder / CHECKS_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
