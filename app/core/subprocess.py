"""Sub-processes: each process level, and how the work is done inside each step.

Between steps there is often no fixed order, but INSIDE a step the UI actions are
usually far more consistent. This module looks inside the steps:

capture_step_actions  (at conversion, on the rows in scope BEFORE merging) writes
                      step_actions.parquet: one row per cleaned UI action of a step run.
hierarchy             level 1 -> stage (level 2) -> level 3 -> step, with cases and events.
step_detail           per step: KPIs, order strength inside the step and its stability (step
                      confidence badge), action map, top paths, the standard way and how
                      runs compare with it, app switches per run, variation by user.
Standards             <data_dir>/step_standards/<signature>.json: the business standard per
                      step (path, mandatory actions, confirmed or not), per file layout.

A step run = one execution of a step, as on the map: consecutive events of the same step in a case
(by time, then the row order in the file); a Step run ID, when mapped, only splits a run further.
Actions are cleaned like milestone actions (milestones.scrub_action); a click name that cannot be shown
(empty, a person's name, only an ID) is kept as "<other click>", so no event is ever dropped. Per step,
actions seen fewer than `subprocess.min_action_count` times become "Other action"; consecutive identical
actions in a run are merged (with a `repeats` count).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from app.core.config import data_dir, settings
from app.core.db import sql_literal

ACTIONS_FILE = "step_actions.parquet"
CATALOGUE_FILE = "action_catalogue.parquet"
OTHER_ACTION = "Other action"
CLASSES = ("Standard", "Standard + repeats", "Extra actions", "Different order", "Missing steps", "Other")
MISSING_MANDATORY = "Missing mandatory actions"
SOURCE_DATA, SOURCE_BUSINESS = "Most common — from data", "Confirmed by business"
USER_NOTE = ("Shows how the same step is done by different people; anonymised IDs stay anonymised. "
             "Use to find training or tooling needs, not to rate individuals.")


def _cfg() -> dict:
    return settings()["subprocess"]


# --------------------------------------------------------------------------- #
# 1. Capture (at conversion)
# --------------------------------------------------------------------------- #
def capture_step_actions(con: duckdb.DuckDBPyConnection, query: str, path: Path, has_run_id: bool) -> dict:
    """Write step_actions.parquet and action_catalogue.parquet from `query` (the rows in scope before
    merging, with the columns of the standard query plus __action, __run, __active and, when a window
    title is mapped, __title). Raw action text and raw titles are never written: only cleaned click
    names ("<typed text>" for typed content) and cleaned screen labels (screens.clean_screen; an empty
    screen is carried forward within the run). Returns {"rows", "runs", "actions"}."""
    from app.core.milestones import OTHER_CLICK, scrub_action
    from app.core.screens import TYPED, clean_screen, is_typed
    con.execute(f"CREATE OR REPLACE TEMP VIEW sp_capture AS {query}")
    cols = {r[0] for r in con.execute("DESCRIBE SELECT * FROM sp_capture").fetchall()}
    add = (([] if "level3" in cols else ["CAST(NULL AS VARCHAR) AS level3"])
           + ([] if "__title" in cols else ["CAST(NULL AS VARCHAR) AS __title"])
           + ([] if "src_row" in cols else ["CAST(NULL AS BIGINT) AS src_row"]))
    if add:
        con.execute(f"CREATE OR REPLACE TEMP VIEW sp_capture AS SELECT *, {', '.join(add)} FROM ({query})")
    names = []
    for (raw,) in con.execute("SELECT DISTINCT __action FROM sp_capture WHERE __action IS NOT NULL").fetchall():
        clean = scrub_action(raw)
        names.append((raw, TYPED if clean and is_typed(clean) else clean))
    screens = [(t, a, clean_screen(t, a)) for t, a in con.execute(
        "SELECT DISTINCT __title, application FROM sp_capture WHERE __title IS NOT NULL").fetchall()]
    con.register("sp_clean_names", pd.DataFrame(names, columns=["raw", "clean"]).astype(object))
    con.register("sp_clean_screens", pd.DataFrame(screens, columns=["raw", "app", "screen"]).astype(object))
    # WORK SITTINGS: a step run of the map (consecutive events of the same step in a case, by time and then the
    # row order in the file; a step-run ID, when mapped, splits it further), split again by person and by pauses
    # over inside_step.gap_minutes. Level 4, sub-processes and "Inside each step" all count these; they never
    # change a step's events, hours or cases.
    split = " OR __run IS DISTINCT FROM lag(__run) OVER w" if has_run_id else ""
    gap = float(settings()["inside_step"]["gap_minutes"]) * 60
    try:
        con.execute(f"""COPY (
            WITH c AS (
                -- Every row is kept: a click name that cannot be shown (empty, a person, only an ID) is OTHER_CLICK.
                SELECT ts_start AS ts, src_row, case_id, resource, activity, level3, c.application,
                       COALESCE(n.clean, '{OTHER_CLICK}') AS action, s.screen AS screen0, __run, __active
                FROM sp_capture c LEFT JOIN sp_clean_names n ON __action = n.raw
                LEFT JOIN sp_clean_screens s ON __title = s.raw AND c.application IS NOT DISTINCT FROM s.app
                WHERE case_id IS NOT NULL AND activity IS NOT NULL AND ts_start IS NOT NULL),
            g AS (
                SELECT *, CASE WHEN lag(activity) OVER w IS DISTINCT FROM activity{split} THEN 1 ELSE 0 END AS __new
                FROM c WINDOW w AS (PARTITION BY case_id ORDER BY ts, src_row, action)),
            n0 AS (
                SELECT *, SUM(__new) OVER (PARTITION BY case_id ORDER BY ts, src_row, action
                                          ROWS UNBOUNDED PRECEDING) AS __map_no FROM g),
            s1 AS (
                SELECT *, CASE WHEN lag(ts) OVER p IS NULL OR epoch(ts) - epoch(lag(ts) OVER p) > {gap} THEN 1 ELSE 0
                          END AS __sit FROM n0
                WINDOW p AS (PARTITION BY case_id, __map_no, COALESCE(resource, '') ORDER BY ts, src_row, action)),
            n AS (
                SELECT *, SUM(__sit) OVER (PARTITION BY case_id, __map_no, COALESCE(resource, '')
                                           ORDER BY ts, src_row, action ROWS UNBOUNDED PRECEDING) AS __sit_no FROM s1),
            f AS (
                SELECT *, COALESCE(screen0, last_value(screen0 IGNORE NULLS) OVER (
                           PARTITION BY case_id, __map_no, COALESCE(resource, ''), __sit_no ORDER BY ts, src_row, action
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) AS screen FROM n),
            k AS (
                SELECT 'R' || md5(concat_ws('|', case_id, activity, CAST(__map_no AS VARCHAR), COALESCE(resource, ''),
                                            CAST(__sit_no AS VARCHAR))) AS run_id,
                       case_id, resource, activity, level3, application, screen, ts, src_row, __active, action,
                       CAST(__run AS VARCHAR) AS step_run
                FROM f),
            m AS (
                SELECT *, CASE WHEN lag(action) OVER v IS NOT DISTINCT FROM action
                                AND lag(screen) OVER v IS NOT DISTINCT FROM screen
                                AND lag(application) OVER v IS NOT DISTINCT FROM application THEN 0 ELSE 1 END AS __first
                FROM k WINDOW v AS (PARTITION BY activity, run_id ORDER BY ts, src_row, action)),
            r AS (
                SELECT *, SUM(__first) OVER (PARTITION BY activity, run_id ORDER BY ts, src_row, action
                                             ROWS UNBOUNDED PRECEDING) AS __grp FROM m)
            SELECT run_id, any_value(case_id) AS case_id, any_value(resource) AS resource, activity,
                   any_value(level3) AS level3, any_value(screen) AS screen, any_value(action) AS action,
                   any_value(application) AS application, MIN(ts) AS ts, MIN(src_row) AS src_row,
                   any_value(step_run) AS step_run, SUM(__active) AS active_seconds, COUNT(*) AS repeats
            FROM r GROUP BY activity, run_id, __grp
        ) TO {sql_literal(path.as_posix())} (FORMAT parquet, COMPRESSION zstd)""")
        # The catalogue of distinct units per Level 3 step: the only input the Level 4 AI sees.
        catalogue = path.parent / CATALOGUE_FILE
        con.execute(f"""COPY (
            SELECT COALESCE(level3, activity) AS l3, activity, application, COALESCE(screen, '') AS screen, action,
                   SUM(repeats) AS events, COUNT(DISTINCT run_id) AS runs, COUNT(DISTINCT case_id) AS cases,
                   COUNT(DISTINCT resource) AS users, SUM(active_seconds) AS active_seconds
            FROM read_parquet({sql_literal(path.as_posix())}) GROUP BY ALL ORDER BY l3, events DESC, screen, action
        ) TO {sql_literal(catalogue.as_posix())} (FORMAT parquet, COMPRESSION zstd)""")
        rows, runs, actions = con.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT (activity, run_id)), COUNT(DISTINCT action) "
            f"FROM read_parquet({sql_literal(path.as_posix())})").fetchall()[0]
        return {"rows": rows, "runs": runs, "actions": actions}
    finally:
        con.unregister("sp_clean_names")
        con.unregister("sp_clean_screens")


# --------------------------------------------------------------------------- #
# 2. Hierarchy
# --------------------------------------------------------------------------- #
LEVELS = (("level1", "Process level 1"), ("stage", "Stage"), ("level3", "Process level 3"), ("activity", "Step"))


def hierarchy(con: duckdb.DuckDBPyConnection, events_path: Path) -> list[dict]:
    """Level 1 -> stage -> level 3 -> step, depth first. Levels that are missing (no column,
    or all empty) are skipped. Each node: depth, level, path, name, cases, events, share."""
    src = f"read_parquet({sql_literal(Path(events_path).as_posix())})"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    used = [(c, label) for c, label in LEVELS if c in cols and (
        c == "activity" or con.execute(f"SELECT COUNT({c}) FROM {src}").fetchall()[0][0] > 0)]
    total = con.execute(f"SELECT COUNT(DISTINCT case_id) FROM {src} WHERE case_id IS NOT NULL").fetchall()[0][0] or 1
    events = "SUM(COALESCE(merged_events, 1))" if "merged_events" in cols else "COUNT(*)"
    nodes = []
    for depth in range(1, len(used) + 1):
        keys = [f"COALESCE(CAST({c} AS VARCHAR), '(none)')" for c, _ in used[:depth]]
        for row in con.execute(f"""SELECT {', '.join(keys)}, COUNT(DISTINCT case_id), {events} FROM {src}
                                   WHERE case_id IS NOT NULL AND activity IS NOT NULL
                                   GROUP BY ALL""").fetchall():
            path = list(row[:depth])
            nodes.append({"depth": depth, "level": used[depth - 1][1], "path": path, "name": path[-1],
                          "cases": row[depth], "events": int(row[depth + 1]),
                          "share": round(row[depth] / total, 4)})
    nodes.sort(key=lambda n: n["path"])
    return nodes


# --------------------------------------------------------------------------- #
# 3. Runs, paths and the comparison with the standard way
# --------------------------------------------------------------------------- #
def clicks(actions_path: Path) -> str:
    """SQL relation of the click-level view: step_actions.parquet keeps every cleaned action; here, per
    step, actions seen fewer than `subprocess.min_action_count` times become "Other action" and the
    same action back to back in a run counts once (the same result as collapsing at capture)."""
    src = f"read_parquet({sql_literal(Path(actions_path).as_posix())})"
    cols = {r[0] for r in duckdb.connect().execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    level3 = "level3" if "level3" in cols else "CAST(NULL AS VARCHAR) AS level3"
    return f"""(
        WITH k AS (
            SELECT run_id, case_id, resource, activity, {level3}, application, ts, active_seconds, repeats,
                   CASE WHEN SUM(repeats) OVER (PARTITION BY activity, action) < {int(_cfg()['min_action_count'])}
                        THEN {sql_literal(OTHER_ACTION)} ELSE action END AS action
            FROM {src}),
        m AS (SELECT *, CASE WHEN lag(action) OVER (PARTITION BY activity, run_id ORDER BY ts, action)
                                  IS NOT DISTINCT FROM action THEN 0 ELSE 1 END AS __first FROM k),
        r AS (SELECT *, SUM(__first) OVER (PARTITION BY activity, run_id ORDER BY ts, action
                                           ROWS UNBOUNDED PRECEDING) AS __grp FROM m)
        SELECT run_id, any_value(case_id) AS case_id, any_value(resource) AS resource, activity,
               any_value(level3) AS level3, any_value(action) AS action, arg_min(application, ts) AS application,
               MIN(ts) AS ts, SUM(active_seconds) AS active_seconds, SUM(repeats) AS repeats
        FROM r GROUP BY activity, run_id, __grp)"""


def runs_of(con: duckdb.DuckDBPyConnection, actions_path: Path, activity: str | None = None) -> pd.DataFrame:
    """One row per run: activity, run_id, case_id, resource, path (list of actions), seconds,
    active, t0, t1."""
    where = f"WHERE activity = {sql_literal(activity)}" if activity is not None else ""
    return con.execute(f"""
        SELECT activity, run_id, any_value(case_id) AS case_id, any_value(resource) AS resource,
               list(action ORDER BY ts, action) AS path,
               epoch(MAX(ts)) - epoch(MIN(ts)) AS seconds, SUM(active_seconds) AS active,
               MIN(ts) AS t0, MAX(ts) AS t1
        FROM {clicks(actions_path)} {where}
        GROUP BY activity, run_id""").df()


def _subsequence(small: list[str], big: list[str]) -> bool:
    it = iter(big)
    return all(x in it for x in small)


def classify(path: list[str], standard: list[str]) -> tuple[str, list[str], list[str]]:
    """The class of one run against the standard way, its missing and its extra actions."""
    path, std = list(path), list(standard)
    missing = [a for a in std if a not in path]
    extra = sorted({a for a in path if a not in std})
    if path == std:
        return "Standard", [], []
    if set(path) == set(std) and len(path) > len(set(path)):
        return "Standard + repeats", [], []
    if not missing and extra and _subsequence(std, path):
        return "Extra actions", [], extra
    if set(path) == set(std):
        return "Different order", [], []
    if missing:
        return "Missing steps", missing, extra
    return "Other", [], extra


def _median(values) -> float | None:
    vals = sorted(v for v in values if v is not None and v == v)
    if not vals:
        return None
    n = len(vals)
    return float(vals[n // 2]) if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def compare(runs: pd.DataFrame, standard: list[str], mandatory: list[str] | None = None,
            confirmed: bool = False) -> dict:
    """How the runs compare with the standard way: per class runs, share and median duration;
    top missing and extra actions. With a confirmed standard, "Missing steps" for a mandatory
    action is labelled "Missing mandatory actions"."""
    n = len(runs)
    mandatory = list(mandatory or [])
    by_class: dict[str, list[float]] = {c: [] for c in CLASSES}
    missing_n: dict[str, int] = {}
    extra_n: dict[str, int] = {}
    mandatory_missing = 0
    per_run = []
    for path, seconds in zip(runs["path"], runs["seconds"]):
        cls, missing, extra = classify(list(path), standard)
        by_class[cls].append(seconds)
        per_run.append(cls)
        for a in missing:
            missing_n[a] = missing_n.get(a, 0) + 1
        for a in extra:
            extra_n[a] = extra_n.get(a, 0) + 1
        if confirmed and any(a in mandatory for a in missing):
            mandatory_missing += 1

    def label(c: str) -> str:
        return MISSING_MANDATORY if c == "Missing steps" and confirmed and mandatory else c

    classes = [{"class": c, "label": label(c), "runs": len(v), "share": round(len(v) / n, 4) if n else 0.0,
                "median_duration_h": None if not v else round(_median(v) / 3600, 6)} for c, v in by_class.items()]
    top = lambda d: [{"action": a, "runs": k, "share": round(k / n, 4) if n else 0.0}  # noqa: E731
                     for a, k in sorted(d.items(), key=lambda x: (-x[1], x[0]))[:3]]
    return {"classes": classes, "top_missing": top(missing_n), "top_extra": top(extra_n),
            "standard_share": classes[0]["share"], "runs": n, "confirmed": confirmed,
            "mandatory_missing_share": round(mandatory_missing / n, 4) if n else 0.0,
            "wording": "Compared with the business standard" if confirmed
            else "Variation from the most common way", "per_run": per_run}


def _order(con: duckdb.DuckDBPyConnection, runs: pd.DataFrame) -> tuple[float, str, float | None, list[dict]]:
    """Order strength of the actions inside the step (pairs with >= min_pair), its stability
    (half-samples of the runs), and the action transitions with their median wait."""
    from app.core.reference import _pairs, order_stability, order_strength
    rows, first = [], []
    for run_id, path, t0 in zip(runs["run_id"], runs["path"], runs["t0"]):
        for a, b in zip(path, path[1:]):
            rows.append((a, b))
        n = len(path)
        seen = {}
        for i, a in enumerate(path):
            seen.setdefault(a, i)
        first += [(run_id, a, i / max(n - 1, 1)) for a, i in seen.items()]
    counts: dict[tuple[str, str], int] = {}
    for a, b in rows:
        counts[(a, b)] = counts.get((a, b), 0) + 1
    edges = [{"src": a, "dst": b, "count": k} for (a, b), k in counts.items()]
    minimum = int(_cfg()["min_pair"])
    keep = {p for p, c in _pairs(edges).items() if sum(c) >= minimum}
    strong = [e for e in edges if tuple(sorted((e["src"], e["dst"]))) in keep]
    strength, label = order_strength(strong)
    con.register("sp_first", pd.DataFrame(first, columns=["case_id", "activity", "rel_pos"]).astype(
        {"case_id": object, "activity": object}))
    try:
        order = [a for (a,) in con.execute(
            "SELECT activity FROM sp_first GROUP BY 1 ORDER BY median(rel_pos), activity").fetchall()]
        stability = order_stability(con, order, "sp_first") if strong else None
    finally:
        con.unregister("sp_first")
    return strength, label, stability, edges


def confidence(strength: float, stability: float | None, runs: int) -> str:
    """Step confidence badge: the flow-confidence score of the inside-step order (strength x
    stability), High only with at least `high_min_runs` runs."""
    from app.core.flow_confidence import band_of, strength_score
    w = float(settings()["flow_confidence"]["stability_weight"])
    score = strength_score(strength) * (1 - w + w * (1.0 if stability is None else stability))
    band = band_of(score)
    if band == "High" and runs < int(_cfg()["high_min_runs"]):
        band = "Medium"
    return {"High": "High", "Medium": "Medium"}.get(band, "Exploratory")


def step_detail(con: duckdb.DuckDBPyConnection, actions_path: Path, activity: str, standard: dict | None = None,
                switches_path: Path | None = None, runs: pd.DataFrame | None = None) -> dict:
    """Everything the Sub-processes tab shows for one step (see the module docstring)."""
    cfg = _cfg()
    mcfg = settings()["mining"]
    runs = runs_of(con, actions_path, activity) if runs is None else runs
    n = len(runs)
    lengths = [len(p) for p in runs["path"]]
    strength, label, stability, edges = _order(con, runs)
    badge = confidence(strength, stability, n)
    src = clicks(actions_path)
    apps = [{"application": a, "events": e, "share": round(e / t, 4)} for a, e, t in con.execute(f"""
        SELECT application, SUM(repeats), SUM(SUM(repeats)) OVER () FROM {src}
        WHERE activity = ? GROUP BY 1 ORDER BY 2 DESC, 1""", [activity]).fetchall() if a is not None]
    apps = [a for a in apps if a["share"] >= cfg["min_app_share"]]
    # Paths
    keys = [" → ".join(p) for p in runs["path"]]
    counts: dict[str, list] = {}
    for k, p, s in zip(keys, runs["path"], runs["seconds"]):
        counts.setdefault(k, [list(p), []])[1].append(s)
    ranked = sorted(counts.items(), key=lambda x: (-len(x[1][1]), x[0]))
    cum, paths = 0.0, []
    for k, (p, secs) in ranked[:int(cfg["top_paths"])]:
        share = len(secs) / n if n else 0.0
        cum += share
        paths.append({"path": p, "runs": len(secs), "share": round(share, 4), "cumulative_share": round(cum, 4),
                      "median_duration_h": round(_median(secs) / 3600, 6)})
    # The standard way: the business standard when saved, else the most common path of 2+ actions.
    common = next((p for _, (p, _s) in ranked if len(p) >= 2), ranked[0][1][0] if ranked else [])
    if standard and standard.get("standard_path"):
        std, source = list(standard["standard_path"]), (SOURCE_BUSINESS if standard.get("confirmed_by_business")
                                                        else "Saved — not confirmed")
        mandatory, confirmed = list(standard.get("mandatory_actions") or []), bool(standard.get("confirmed_by_business"))
    else:
        std, source, mandatory, confirmed = common, SOURCE_DATA, [], False
    comp = compare(runs, std, mandatory, confirmed)
    per_run = comp.pop("per_run")
    # Action map: Start / End, counts and median waits between consecutive actions.
    start, end = mcfg["start_node"], mcfg["end_node"]
    node_n: dict[str, int] = {}
    for p in runs["path"]:
        for a in p:
            node_n[a] = node_n.get(a, 0) + 1
    waits = {}
    if n:
        for a, b, w in con.execute(f"""
                SELECT action, nxt, median(epoch(nts) - epoch(ts)) / 3600.0 FROM (
                    SELECT action, ts, lead(action) OVER w AS nxt, lead(ts) OVER w AS nts FROM {src}
                    WHERE activity = ? WINDOW w AS (PARTITION BY run_id ORDER BY ts, action))
                WHERE nxt IS NOT NULL GROUP BY 1, 2""", [activity]).fetchall():
            waits[(a, b)] = w
    nodes = ([{"activity": start, "events": n, "cases": n, "median_processing_h": None}]
             + [{"activity": a, "events": k, "cases": k, "median_processing_h": None}
                for a, k in sorted(node_n.items(), key=lambda x: (-x[1], x[0]))]
             + [{"activity": end, "events": n, "cases": n, "median_processing_h": None}])
    map_edges = [{"src": e["src"], "dst": e["dst"], "count": e["count"], "cases": e["count"],
                  "median_wait_h": waits.get((e["src"], e["dst"]))} for e in edges]
    for side, node in (("first", start), ("last", end)):
        tally: dict[str, int] = {}
        for p in runs["path"]:
            if len(p):
                key = p[0] if side == "first" else p[-1]
                tally[key] = tally.get(key, 0) + 1
        map_edges += [{"src": node, "dst": a, "count": k, "cases": k, "median_wait_h": None} if side == "first"
                      else {"src": a, "dst": node, "count": k, "cases": k, "median_wait_h": None}
                      for a, k in tally.items()]
    keep = {nd["activity"] for nd in nodes[1:-1][:int(cfg["max_actions_on_map"])]} | {start, end}
    nodes = [nd for nd in nodes if nd["activity"] in keep]
    map_edges = sorted((e for e in map_edges if e["src"] in keep and e["dst"] in keep),
                       key=lambda e: (-e["count"], e["src"], e["dst"]))
    # App switches inside runs
    switches = None
    if switches_path is not None and Path(switches_path).exists() and n:
        con.register("sp_runs", runs[["run_id", "case_id", "resource", "t0", "t1"]].astype(
            {"run_id": object, "case_id": object, "resource": object}))
        try:
            total = con.execute(f"""
                SELECT COUNT(*) FROM read_parquet({sql_literal(Path(switches_path).as_posix())}) s JOIN sp_runs r
                  ON s.case_id = r.case_id AND s.resource IS NOT DISTINCT FROM r.resource AND s.activity = ?
                 AND s.ts BETWEEN r.t0 AND r.t1""", [activity]).fetchall()[0][0]
        finally:
            con.unregister("sp_runs")
        switches = {"total": int(total), "per_run": round(total / n, 3)}
    # Variation by user (resource values as recorded)
    users = []
    frame = pd.DataFrame({"resource": runs["resource"], "seconds": runs["seconds"], "cls": per_run})
    for user, g in frame.groupby("resource", dropna=True):
        if len(g) >= int(cfg["min_user_runs"]):
            users.append({"user": user, "runs": len(g), "median_duration_h": round(_median(g["seconds"]) / 3600, 6),
                          "standard_share": round(float((g["cls"] == "Standard").mean()), 4)})
    users.sort(key=lambda u: (-u["runs"], str(u["user"])))
    run_cases = runs.groupby("case_id").size() if n else pd.Series(dtype=int)
    return {
        "activity": activity, "runs": n, "cases": int(runs["case_id"].nunique()) if n else 0,
        "users": int(runs["resource"].nunique()) if n else 0,
        "median_actions": _median(lengths), "median_duration_h": None if not n else round(_median(runs["seconds"]) / 3600, 6),
        "median_active_h": (None if not n or runs["active"].isna().all()
                            else round(_median(runs["active"].dropna()) / 3600, 6)),
        "runs_per_case": _median(list(run_cases)) if n else None, "apps": apps,
        "order_strength": strength, "order_strength_label": label, "stability": stability, "confidence": badge,
        "nodes": nodes, "edges": map_edges, "paths": paths,
        "standard": {"path": std, "source": source, "mandatory_actions": mandatory, "confirmed": confirmed},
        "comparison": comp, "switches": switches, "by_user": users[:50],
    }


def describe_step(d: dict) -> str:
    """One sentence from fixed templates (numbers from the step detail)."""
    from app.core.mining import format_hours
    out = (f"{d['activity']} is done {d['runs']:,} times in {d['cases']:,} cases; a run has a median of "
           f"{d['median_actions']:g} actions and takes {format_hours(d['median_duration_h'])}.")
    comp = d["comparison"]
    way = "the business standard" if comp["confirmed"] else "the most common way"
    out += f" {100 * comp['standard_share']:.0f}% of runs follow {way}"
    if comp["top_missing"]:
        m = comp["top_missing"][0]
        out += f"; the most common difference is a missing '{m['action']}' ({100 * m['share']:.0f}% of runs)"
    return out + "."


# --------------------------------------------------------------------------- #
# 4. Business standards per step (per file layout)
# --------------------------------------------------------------------------- #
def standards_path(signature: str) -> Path:
    import hashlib
    safe = signature if re.fullmatch(r"[\w-]+", signature or "") else hashlib.sha256(signature.encode()).hexdigest()
    return data_dir() / "step_standards" / f"{safe}.json"


def load_standards(signature: str) -> dict:
    """{step: {standard_path, mandatory_actions, confirmed_by_business, updated_at}}."""
    import json
    path = standards_path(signature)
    return json.loads(path.read_text(encoding="utf-8")).get("steps", {}) if path.exists() else {}


def save_standard(signature: str, step: str, standard_path: list[str], mandatory_actions: list[str] | None = None,
                  confirmed: bool = False) -> dict:
    """Save one step's standard (atomically); the other steps are kept."""
    from app.core.milestones import atomic_json
    from app.core.narrative import payload_mask
    path = [str(a).strip() for a in standard_path if str(a).strip()]
    if not path:
        raise ValueError("A standard way needs at least one action.")
    if any(payload_mask(a) != a for a in path):
        raise ValueError("Actions in a standard must not contain personal identifiers.")
    mandatory = [a for a in (mandatory_actions or []) if a in path]
    steps = load_standards(signature)
    steps[step] = {"standard_path": path, "mandatory_actions": mandatory, "confirmed_by_business": bool(confirmed),
                   "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    atomic_json(standards_path(signature), {"steps": steps})
    return steps[step]


def save_standards(signature: str, steps: dict) -> Path:
    """Replace every step's standard (e.g. from a --standards JSON file)."""
    for step, s in steps.items():
        save_standard(signature, step, s.get("standard_path") or [], s.get("mandatory_actions"),
                      bool(s.get("confirmed_by_business")))
    return standards_path(signature)


def confirm_standards(signature: str, steps: list[str]) -> None:
    """Mark these steps' saved standards as confirmed by the business (on approving a definition)."""
    saved = load_standards(signature)
    for step in steps:
        s = saved.get(step)
        if s:
            save_standard(signature, step, s["standard_path"], s.get("mandatory_actions"), True)


# --------------------------------------------------------------------------- #
# 5. The mining section
# --------------------------------------------------------------------------- #
def analyse(con: duckdb.DuckDBPyConnection, events_path: Path, signature: str) -> dict:
    """The `subprocess` section of mining.json: hierarchy plus a detail per step (most runs
    first, at most `subprocess.max_steps`). {"available": False, ...} without step actions."""
    folder = Path(events_path).parent
    tree = hierarchy(con, events_path)
    actions = folder / ACTIONS_FILE
    if not actions.exists():
        return {"available": False, "hierarchy": tree,
                "reason": "No Action / UI event column was mapped, so the work inside each step cannot be shown. "
                          "Map an action column on Import data to see sub-processes."}
    standards = load_standards(signature)
    all_runs = runs_of(con, actions)
    order = all_runs.groupby("activity").size().sort_values(ascending=False)
    steps = []
    for activity in list(order.index)[:int(_cfg()["max_steps"])]:
        steps.append(step_detail(con, actions, activity, standards.get(activity), folder / "app_switches.parquet",
                                 runs=all_runs[all_runs["activity"] == activity].reset_index(drop=True)))
    stage_depth = next((n["depth"] for n in tree if n["level"] == "Stage"), None)
    return {"available": True, "reason": None, "hierarchy": tree, "steps": steps,
            "stage_of": {n["path"][-1]: (n["path"][stage_depth - 1] if stage_depth else None)
                         for n in tree if n["level"] == "Step"}}


def step_table(section: dict) -> list[dict]:
    """One row per step for the report and Excel."""
    from app.core.mining import format_hours
    rows = []
    for d in section.get("steps") or []:
        comp = d["comparison"]
        top = comp["top_missing"][0] if comp["top_missing"] else (comp["top_extra"][0] if comp["top_extra"] else None)
        diff = (f"missing '{top['action']}' ({100 * top['share']:.0f}%)" if top and comp["top_missing"]
                else f"extra '{top['action']}' ({100 * top['share']:.0f}%)" if top else "–")
        rows.append({"step": d["activity"], "stage": (section.get("stage_of") or {}).get(d["activity"]) or "",
                     "runs": d["runs"], "median_actions": d["median_actions"],
                     "median_duration": format_hours(d["median_duration_h"]), "median_duration_h": d["median_duration_h"],
                     "confidence": d["confidence"], "order_strength": d["order_strength"],
                     "standard_source": d["standard"]["source"], "standard_share": comp["standard_share"],
                     "top_difference": diff})
    return rows
