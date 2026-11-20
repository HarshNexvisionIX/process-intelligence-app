"""Inside each step: the detailed flow and measures of one Level 3 step (inside_steps.json, page 3 tab).

Why: across a case the Level 3 order is close to a coin toss in the HR data, but INSIDE one step one person in
one sitting follows real patterns. So each step gets its own flow, patterns, time split, work types, rework,
applications, variation between people and PM4Py model.

Run (here): consecutive clicks of ONE case and ONE Level 3 step by ONE person, with no gap longer than
inside_step.gap_minutes. The app's step runs (map, report, Level 4) are consecutive events of one step in a
case, whatever the person and the pauses; a step run is split further here when the person changes or a pause
is longer than the gap. Totals (events, hours) are the same either way.

Everything is computed by code from step_actions.parquet and the Level 4 taxonomy (level4.assigner). Users are
masked (P01, P02 ... by hours inside the step); no raw rows, names or IDs leave this computer. The PM4Py model
per step (pm4py_model.call, with the time limit) runs only on request ("Build Level 4 models" on page 3).
Statuses: patterns are `observed`; a documented Level 4 flow (sub-step names from your documents) is
`documented`; a step whose taxonomy is confirmed by the business is `sme_confirmed`.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from pathlib import Path

import duckdb
import pandas as pd

from app.core.config import settings
from app.core.db import sql_literal

FILE = "inside_steps.json"
OTHER_PATTERN = "other patterns"


def _cfg() -> dict:
    return settings()["inside_step"]


def load(folder: Path) -> dict | None:
    try:
        return json.loads((Path(folder) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(folder: Path, data: dict) -> None:
    from app.core.milestones import atomic_json
    atomic_json(Path(folder) / FILE, data)


# --------------------------------------------------------------------------- #
# Rows and runs
# --------------------------------------------------------------------------- #
def rows_of(con: duckdb.DuckDBPyConnection, folder: Path, l3: str) -> pd.DataFrame:
    """The click rows of one Level 3 (step_actions.parquet), ordered by case, user, time."""
    from app.core.subprocess import ACTIONS_FILE
    src = f"read_parquet({sql_literal((Path(folder) / ACTIONS_FILE).as_posix())})"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    l3col = "COALESCE(level3, activity)" if "level3" in cols else "activity"
    order = "ts, src_row, action" if "src_row" in cols else "ts, action"
    return con.execute(f"""
        SELECT run_id, case_id, COALESCE(resource, '') AS resource, activity, COALESCE(application, '') AS application,
               COALESCE(screen, '') AS screen, action, ts, COALESCE(active_seconds, 0) AS active_seconds, repeats
        FROM {src} WHERE {l3col} = ? ORDER BY case_id, resource, {order}""", [l3]).df()


def runs_of(rows: pd.DataFrame, label_of) -> list[dict]:
    """Runs (see the module docstring) with their Level 4 path (consecutive repeats merged), hours, apps, switches."""
    gap = float(_cfg()["gap_minutes"]) * 60
    runs: list[dict] = []
    cur = None
    for r in rows.itertuples(index=False):
        l4 = label_of({"activity": r.activity, "application": r.application, "screen": r.screen, "action": r.action})
        new = (cur is None or r.case_id != cur["case_id"] or r.resource != cur["resource"] or r.run_id != cur["run_id"]
               or (r.ts - cur["t1"]).total_seconds() > gap)
        if new:
            cur = {"run_id": r.run_id, "case_id": r.case_id, "resource": r.resource, "t0": r.ts, "t1": r.ts,
                   "path": [], "seconds": {}, "events": 0, "active": 0.0, "apps": [], "switches": 0, "last_app": None}
            runs.append(cur)
        cur["t1"] = r.ts
        cur["events"] += int(r.repeats)
        cur["active"] += float(r.active_seconds)
        cur["seconds"][l4] = cur["seconds"].get(l4, 0.0) + float(r.active_seconds)
        if not cur["path"] or cur["path"][-1] != l4:
            cur["path"].append(l4)
        if r.application and r.application != cur["last_app"]:
            if cur["last_app"] is not None:
                cur["switches"] += 1
            cur["last_app"] = r.application
            if r.application not in cur["apps"]:
                cur["apps"].append(r.application)
    for x in runs:
        x["duration_s"] = (x["t1"] - x["t0"]).total_seconds()
        x["rework"] = sorted({a for i, a in enumerate(x["path"]) if a in x["path"][:i]})
        x.pop("last_app", None)
    return runs


# --------------------------------------------------------------------------- #
# Measures
# --------------------------------------------------------------------------- #
def _median(values) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return statistics.median(vals) if vals else None


def work_type(label: str, rows: pd.DataFrame) -> str:
    """entry / lookup / review / communication / navigation / other, from the words of the sub-step's clicks and
    screens (inside_step.work_types; the type with most events wins, ties by the order in settings)."""
    types = _cfg()["work_types"]
    counts = {t: 0 for t in types}
    label_words = set(re.findall(r"[a-z]+", str(label).lower()))
    for r in rows.itertuples(index=False):
        words = label_words | set(re.findall(r"[a-z]+", f"{r.action} {r.screen}".lower()))
        for t, ws in types.items():
            if words & {str(w).lower() for w in ws}:
                counts[t] += int(r.repeats)
    best = max(types, key=lambda t: counts[t])
    return best if counts[best] else "other"


def mask_users(rows: pd.DataFrame) -> dict[str, str]:
    """resource -> P01, P02 ... by active seconds inside the step (then events, then a stable hash)."""
    import hashlib
    g = rows.groupby("resource").agg(s=("active_seconds", "sum"), n=("repeats", "sum"))
    order = sorted(g.index, key=lambda u: (-round(float(g.loc[u, "s"]), 3), -int(g.loc[u, "n"]),
                                           hashlib.sha256(str(u).encode()).hexdigest()))
    width = max(2, len(str(len(order))))
    return {u: f"P{i:0{width}d}" for i, u in enumerate(order, 1)}


def analyse_step(rows: pd.DataFrame, entry: dict, l3: str) -> dict:
    """Everything shown for one step (code only)."""
    from app.core.level4 import OTHER, assigner
    from app.core.mining import _dot_text
    cfg = _cfg()
    label_of = assigner(entry)
    runs = runs_of(rows, label_of)
    n = len(runs)
    users = rows["resource"].replace("", pd.NA).dropna().nunique()
    out = {"l3": l3, "runs": n, "events": int(rows["repeats"].sum()), "active_h": float(rows["active_seconds"].sum()) / 3600,
           "cases": int(rows["case_id"].nunique()), "users": int(users),
           "status": "sme_confirmed" if entry.get("confirmed_by_business") else "observed",
           "enough": n >= int(cfg["min_runs"]) and users >= int(cfg["min_users"])}
    if not out["enough"]:
        out["reason"] = f"not enough runs ({n} runs by {users} users; needs {cfg['min_runs']} runs and {cfg['min_users']} users)"
        return out
    # Level 4 flow: directly-follows counts inside runs
    edges: Counter = Counter()
    starts: Counter = Counter()
    ends: Counter = Counter()
    for r in runs:
        p = r["path"]
        starts[p[0]] += 1
        ends[p[-1]] += 1
        for a, b in zip(p, p[1:]):
            edges[(a, b)] += 1
    labels = [s["label"] for s in entry.get("substeps") or []] + [OTHER]
    names = [x for x in labels if any(x in r["path"] for r in runs)]
    # Time split
    split = []
    for name in names:
        secs = [r["seconds"].get(name, 0.0) for r in runs if name in r["seconds"]]
        mine = rows[[label_of({"activity": a, "application": ap, "screen": sc, "action": ac}) == name
                     for a, ap, sc, ac in zip(rows["activity"], rows["application"], rows["screen"], rows["action"])]]
        split.append({"sub_step": name, "hours": sum(secs) / 3600, "runs": len(secs), "median_run_s": _median(secs),
                      "share_of_hours": (sum(secs) / 3600 / out["active_h"]) if out["active_h"] else None,
                      "work_type": work_type(name, mine), "events": int(mine["repeats"].sum())})
    # Patterns
    pats = Counter(tuple(r["path"]) for r in runs)
    top = pats.most_common(5)
    patterns = [{"path": list(p), "runs": k, "share": k / n, "status": "observed"} for p, k in top]
    rest = n - sum(k for _, k in top)
    if rest:
        patterns.append({"path": [OTHER_PATTERN], "runs": rest, "share": rest / n, "status": "observed",
                         "distinct": len(pats) - len(top)})
    verbs = {str(v).lower() for v in cfg["completion_verbs"]}
    first, last = starts.most_common(1)[0], ends.most_common(1)[0]
    ends_with_completion = sum(k for name, k in ends.items() if set(re.findall(r"[a-z]+", name.lower())) & verbs) / n
    # Rework inside the step
    rework_runs = [r for r in runs if r["rework"]]
    rework_by = Counter(a for r in rework_runs for a in r["rework"])
    # Applications
    apps = Counter(a for r in runs for a in r["apps"])
    # Variation between people (masked)
    mask = mask_users(rows)
    by_user: dict[str, list[dict]] = {}
    for r in runs:
        by_user.setdefault(r["resource"], []).append(r)
    step_median = _median([r["active"] for r in runs]) or 0.0
    people = []
    for u, rs in by_user.items():
        main = Counter(tuple(r["path"]) for r in rs).most_common(1)[0][0]
        others = [r for v, rs2 in by_user.items() if v != u for r in rs2]
        used_by_others = sum(1 for r in others if tuple(r["path"]) == main) / len(others) if others else 1.0
        med = _median([r["active"] for r in rs]) or 0.0
        flags = []
        if step_median and med > step_median * (1 + float(cfg["cv_flag"])):
            flags.append("slower than the step's median")
        if len(rs) >= 3 and used_by_others < float(cfg["other_pattern_share"]):
            flags.append("main pattern rarely used by others")
        people.append({"user": mask.get(u, "P?"), "runs": len(rs), "median_run_s": med, "main_pattern": list(main),
                       "main_pattern_share_others": used_by_others, "flags": flags})
    people.sort(key=lambda p: -p["runs"])
    medians = [p["median_run_s"] for p in people if p["runs"] >= 3]
    cv = (statistics.pstdev(medians) / statistics.mean(medians)) if len(medians) >= 2 and statistics.mean(medians) else None
    cv_label = (None if cv is None else "low" if cv < 0.3 else "moderate" if cv <= float(cfg["cv_flag"]) else "high")
    # Automation hints: facts only, for the SME to review
    entry_share = sum(s["hours"] for s in split if s["work_type"] == "entry") / out["active_h"] if out["active_h"] else 0
    hints = []
    if n >= int(cfg["automation_min_runs"]) and patterns and patterns[0]["share"] >= float(cfg["automation_pattern_share"]):
        hints.append(f"{n:,} runs, {100 * patterns[0]['share']:.0f}% follow one pattern"
                     + (f", {100 * entry_share:.0f}% of the time is data entry" if entry_share >= 0.5 else ""))
    # DOT
    lines = ["digraph inside {", "  rankdir=LR;",
             '  node [shape=box, style="rounded,filled", fillcolor="#eef3fb", color="#4a6fa5", fontname="Helvetica", fontsize=11];',
             '  edge [color="#666666", fontname="Helvetica", fontsize=9];',
             '  start [shape=circle, label="", width=0.25, fillcolor="#e3f4ea", color="#1f7a4d"];',
             '  end [shape=doublecircle, label="", width=0.2, fillcolor="#fbe1e1", color="#a61b1b"];']
    ids = {name: f"n{i}" for i, name in enumerate(names)}
    for name in names:
        s = next(x for x in split if x["sub_step"] == name)
        lines.append(f'  {ids[name]} [label="{_dot_text(name)}\\n{s["runs"]:,} runs · {s["hours"]:.1f} h"];')
    keep = max(1, int(0.05 * n))
    for (a, b), k in edges.most_common():
        if k >= keep:
            lines.append(f'  {ids[a]} -> {ids[b]} [label="{k:,}"];')
    for name, k in starts.most_common(2):
        lines.append(f'  start -> {ids[name]} [label="{k:,}"];')
    for name, k in ends.most_common(2):
        lines.append(f'  {ids[name]} -> end [label="{k:,}"];')
    lines.append("}")
    out.update({
        "flow": {"nodes": names, "edges": [{"from": a, "to": b, "runs": k} for (a, b), k in edges.most_common()],
                 "dot": "\n".join(lines)},
        "patterns": patterns, "distinct_patterns": len(pats),
        "start": {"sub_step": first[0], "share": first[1] / n}, "end": {"sub_step": last[0], "share": last[1] / n},
        "ends_with_completion_share": ends_with_completion,
        "time_split": sorted(split, key=lambda s: -s["hours"]),
        "rework": {"runs": len(rework_runs), "share": len(rework_runs) / n,
                   "by_sub_step": [{"sub_step": a, "runs": k, "share": k / n} for a, k in rework_by.most_common(5)]},
        "apps": {"per_run_median": _median([len(r["apps"]) for r in runs]),
                 "switches_per_run": sum(r["switches"] for r in runs) / n,
                 "top": [{"application": a, "runs": k, "share": k / n} for a, k in apps.most_common(5)]},
        "median_run_s": step_median, "median_duration_s": _median([r["duration_s"] for r in runs]),
        "people": people[:20], "cv_between_people": cv, "cv_label": cv_label,
        "variation_flag": bool(cv is not None and cv > float(cfg["cv_flag"])) or any(p["flags"] for p in people),
        "automation_candidates": hints,
        "entry_share": entry_share,
    })
    return out


# --------------------------------------------------------------------------- #
# The whole job (code only; PM4Py on request)
# --------------------------------------------------------------------------- #
def analyse(con: duckdb.DuckDBPyConnection, events_path: Path, signature: str, use_ai: bool = False,
            progress=None) -> dict:
    """inside_steps.json: {available, steps: {l3: ...}, order}. The PM4Py part of an earlier run is kept."""
    from app.core import level4
    from app.core.subprocess import ACTIONS_FILE
    folder = Path(events_path).parent
    if not (folder / ACTIONS_FILE).exists():
        return {"available": False, "reason": "Not available: map an Action / UI event column on Import data to see the "
                                              "inside of each step."}
    cat = level4.catalogue(folder)
    if cat.empty:
        return {"available": False, "reason": "Not available: convert the file again to build the action catalogue."}
    tax = level4.load(signature)
    previous = load(folder) or {}
    steps = {}
    for l3 in sorted(cat["l3"].dropna().unique()):
        if progress:
            progress(f"Inside each step: {l3}")
        entry = tax.get(l3) or level4.rules_entry(l3, level4.units_of(cat, l3), "no taxonomy yet")
        rows = rows_of(con, folder, l3)
        d = analyse_step(rows, entry, l3)
        old = (previous.get("steps") or {}).get(l3) or {}
        if old.get("pm4py") and old.get("taxonomy_hash") == level4.entry_hash(entry):
            d["pm4py"] = old["pm4py"]            # the model of this taxonomy, built earlier on request
        d["taxonomy_hash"] = level4.entry_hash(entry)
        d["doc_substeps"] = level4.doc_names(folder.name, l3)
        steps[l3] = d
    out = {"available": True, "reason": None, "steps": steps, "settings": {k: _cfg()[k] for k in
           ("gap_minutes", "min_runs", "min_users", "cv_flag", "max_runs")}}
    _save(folder, out)
    return out


def _run_log(rows: pd.DataFrame, entry: dict, max_runs: int) -> pd.DataFrame:
    """The runs of a step as a PM4Py log (case_id = run, activity = Level 4, start_ts), a fixed sample."""
    import hashlib
    from app.core.level4 import assigner
    runs = runs_of(rows, assigner(entry))
    runs.sort(key=lambda r: hashlib.sha256(f"{r['case_id']}|{r['resource']}|{r['t0']}".encode()).hexdigest())
    recs = []
    for i, r in enumerate(runs[:int(max_runs)]):
        for j, l4 in enumerate(r["path"]):
            recs.append((f"R{i}", l4, r["t0"] + pd.Timedelta(seconds=j)))
    return pd.DataFrame(recs, columns=["case_id", "activity", "start_ts"]), len(runs)


def build_models(job: dict, l3s: list[str] | None = None) -> dict:
    """PM4Py per step, on request: a Level 4 model (Inductive Miner) on at most inside_step.max_runs runs, its
    fitness and precision, and - when your documents name sub-steps for the step - conformance against that
    documented flow (same rules as the approved-process check). Saved into inside_steps.json."""
    from app.core import jobs, level4, pm4py_model, reference
    from app.core.db import connect
    folder = jobs.job_folder(job["job_id"])
    data = load(folder)
    if not data or not data.get("available"):
        return {"available": False, "reason": "Run the analysis first."}
    ok, why = pm4py_model.status()
    if not ok:
        return {"available": False, "reason": why}
    tax = level4.load(reference.order_key(job))
    cat = level4.catalogue(folder)
    con = connect()
    for l3, d in data["steps"].items():
        if l3s and l3 not in l3s or not d.get("enough"):
            continue
        entry = tax.get(l3) or level4.rules_entry(l3, level4.units_of(cat, l3), "no taxonomy yet")
        log_df, total = _run_log(rows_of(con, folder, l3), entry, int(_cfg()["max_runs"]))
        prefix = "inside_" + re.sub(r"[^A-Za-z0-9]+", "_", l3)[:40]
        res = {"runs_used": int(log_df["case_id"].nunique()), "runs_total": total}
        try:
            model = pm4py_model.call({"task": "discover", "log": pm4py_model._write_log(log_df, folder, f"{prefix}.parquet"),
                                      "noise_threshold": float(settings()["pm4py"].get("noise_threshold", 0.2)),
                                      "bpmn_out": str(folder / pm4py_model.WORK_DIR / f"{prefix}.bpmn"),
                                      "pnml_out": str(folder / pm4py_model.WORK_DIR / f"{prefix}.pnml")}, folder)
        except pm4py_model.PM4PyTimeout:
            res.update(available=False, reason=f"PM4Py model not built in time (pm4py.timeout_s = {pm4py_model.timeout_s():g}).")
            d["pm4py"] = res
            continue
        except Exception as err:  # noqa: BLE001
            res.update(available=False, reason=f"PM4Py could not build the model ({type(err).__name__}).")
            d["pm4py"] = res
            continue
        prec, prec_cases, prec_note = pm4py_model._precision(log_df, folder / pm4py_model.WORK_DIR / f"{prefix}.pnml",
                                                             folder, f"{prefix}_precision.parquet")
        res.update(available=True, reason=None, fitness=model["fitness"], precision=prec, precision_runs=prec_cases,
                   precision_note=prec_note, fitting_runs_share=model["fitting_cases_share"], dot=model["dot"],
                   tree=model["tree"], gateways=model["gateways"], any_order=bool(model.get("any_order")))
        docs = d.get("doc_substeps") or []
        if len(docs) >= 2:
            blocks = [{"kind": "step", "name": x, "optional": False, "loop": False} for x in docs]
            inside = log_df[log_df["activity"].isin(set(docs))]
            if len(inside):
                chk = pm4py_model._check(folder, inside, total, blocks, ["Documented sub-steps in the order your "
                                         "documents give them."], docs, prefix + "_doc", {})
                chk["status"] = "documented"
                res["documented"] = chk
        d["pm4py"] = res
    _save(folder, data)
    return data


# --------------------------------------------------------------------------- #
# Facts and the AI paragraph
# --------------------------------------------------------------------------- #
def fact_rows(data: dict | None) -> list[dict]:
    """The numbers of the inside-step blocks as fact rows (section "inside", placed last)."""
    rows = []
    for l3, d in ((data or {}).get("steps") or {}).items():
        if not d.get("enough"):
            continue
        meta = {"activity": l3}
        top = d["patterns"][0] if d.get("patterns") else None
        if top:
            rows.append(dict(type="inside_top_pattern", label=f"{l3}: work sittings that follow the most common pattern",
                             value=top["share"], unit="share", support=d["runs"], meta={**meta, "path": top["path"]}))
        rows.append(dict(type="inside_median_run", label=f"{l3}: median active time per work sitting", value=d["median_run_s"] / 3600,
                         unit="hours", support=d["runs"], meta=meta))
        rows.append(dict(type="inside_rework", label=f"{l3}: work sittings with rework inside the step", value=d["rework"]["share"],
                         unit="share", support=d["runs"], meta=meta))
        rows.append(dict(type="inside_switches", label=f"{l3}: application switches per work sitting",
                         value=d["apps"]["switches_per_run"], unit="ratio", support=d["runs"], meta=meta))
        if d.get("time_split"):
            s = d["time_split"][0]
            rows.append(dict(type="inside_top_hours", label=f"{l3}: {s['sub_step']} — share of the step's hours",
                             value=s["share_of_hours"], unit="share", support=d["runs"], meta={**meta, "bucket": s["sub_step"]}))
        pm = d.get("pm4py") or {}
        if pm.get("available"):
            rows.append(dict(type="inside_pm_fitness", label=f"{l3}: Level 4 model fitness (PM4Py)", value=pm["fitness"],
                             unit="ratio", support=pm["runs_used"], meta=meta))
            if pm.get("precision") is not None:
                rows.append(dict(type="inside_pm_precision", label=f"{l3}: Level 4 model precision (PM4Py)",
                                 value=pm["precision"], unit="ratio", support=pm["precision_runs"], meta=meta))
    return rows


def paragraph(d: dict, facts: list, ai: dict, avoid: list[str]) -> dict:
    """At most one short paragraph per step: the AI through the narrative checks with {{F}} placeholders only,
    else the template."""
    from app.core.level4 import _write
    ids = {f.type: f.id for f in facts if f.meta.get("activity") == d["l3"]}
    tpl = f"{d['l3']} is done in {d['runs']:,} work sittings."
    if "inside_top_pattern" in ids and d.get("patterns"):
        tpl += f" The most common pattern, {' → '.join(d['patterns'][0]['path'])}, covers {{{{{ids['inside_top_pattern']}}}}} of sittings."
    if "inside_median_run" in ids:
        tpl += f" A sitting takes {{{{{ids['inside_median_run']}}}}} of active time."
    if "inside_rework" in ids and d["rework"]["share"]:
        tpl += f" Work inside the step is redone in {{{{{ids['inside_rework']}}}}} of sittings."
    local = [f for f in facts if f.meta.get("activity") == d["l3"]]
    context = "Sub-steps: " + ", ".join(s["sub_step"] for s in d.get("time_split") or [])
    return _write("inside", d["l3"], context, local, tpl, None, ai, avoid)
