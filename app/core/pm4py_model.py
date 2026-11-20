"""Optional: the formal process model and conformance with PM4Py (Inductive Miner, token replay, alignments).

LICENCE: PM4Py is AGPL v3. This app uses it for INTERNAL use only (decided by the owner). If the app is
ever sold to or hosted for clients, switch it off (`pm4py.enabled: false`) or buy a commercial PM4Py
licence. Everything else in the app works without PM4Py. It never changes an existing number: its results
are separate (mining.json "process_model", pm4py_conformance.json, pm4py_definition_check.json) and
clearly labelled.

Every PM4Py call runs in a separate process (pm4py_worker.py) and is stopped after `pm4py.timeout_s`.
Only hashed case IDs, step names and run start times go to PM4Py; nothing leaves this computer.

discover          (Run analysis, or "Build model" on page 3 with pm4py.build_on_run false) step runs of at
                  most pm4py.max_cases cases (a fixed hash sample) -> Inductive Miner -> BPMN model, DOT
                  text, process_model.bpmn; token replay: fitness, precision, per-case fitness histogram;
                  alignments on the first pm4py.alignment_max_cases of those cases: the most skipped and the
                  most extra steps (token-replay problems per step when alignments do not finish in time).
                  Written to pm4py_conformance.json.
check_order       (page 3) the same against the step order page 3 and the report use (e.g. the HR template).
                  Written to pm4py_order_check.json.
check_definition  (page 4, approved definition) the approved process as a model (definition_blocks):
                  fitness, precision, cases that fit fully, and per step the share of cases that skip it, do it
                  at an unexpected point, or do it more than expected. Written to pm4py_definition_check.json.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import duckdb

from app.core.config import PROJECT_ROOT, settings

BPMN_FILE = "process_model.bpmn"
PNML_FILE = "process_model.pnml"
CONFORMANCE_FILE = "pm4py_conformance.json"
DEFINITION_FILE = "pm4py_definition_check.json"
WORK_DIR = "_pm4py"
LICENCE_NOTE = ("Discovered with PM4Py (AGPL v3), for internal use only. Switch it off in config/settings.yaml "
                "(pm4py.enabled) before the app is sold to or hosted for clients.")
BUCKETS = ((0.0, 0.5, "0–0.5"), (0.5, 0.8, "0.5–0.8"), (0.8, 0.95, "0.8–0.95"), (0.95, 1.0000001, "0.95–1"))
log = logging.getLogger(__name__)


class PM4PyTimeout(Exception):
    """A PM4Py call did not finish within pm4py.timeout_s."""


def _cfg() -> dict:
    return settings().get("pm4py") or {}


def timeout_s() -> float:
    return float(_cfg().get("timeout_s") or 120)


def status() -> tuple[bool, str | None]:
    """(on, why not): on when pm4py.enabled and PM4Py is installed. PM_DISABLE_PM4PY=1 switches it off
    (the test suite does, to stay fast)."""
    if not _cfg().get("enabled") or os.getenv("PM_DISABLE_PM4PY") == "1":
        return False, "Not available: the PM4Py model is switched off (pm4py.enabled in config/settings.yaml)."
    if importlib.util.find_spec("pm4py") is None:
        return False, ("Not available: PM4Py is not installed. Install it with "
                       "pip install -r requirements-pm4py.txt, then run the analysis again.")
    return True, None


# --------------------------------------------------------------------------- #
# Running PM4Py in a separate process, with a time limit
# --------------------------------------------------------------------------- #
def call(task: dict, folder: Path, timeout: float | None = None) -> dict:
    """Run one worker task (pm4py_worker.py) and return its JSON result; PM4PyTimeout after `timeout` seconds."""
    timeout = timeout_s() if timeout is None else float(timeout)
    work = Path(folder) / WORK_DIR
    work.mkdir(exist_ok=True)
    task_path, result_path = work / f"{task['task']}_task.json", work / f"{task['task']}_result.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    result_path.unlink(missing_ok=True)
    try:
        done = subprocess.run([sys.executable, "-m", "app.core.pm4py_worker", str(task_path), str(result_path)],
                              cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as err:
        raise PM4PyTimeout(f"{task['task']} took longer than {timeout:g} s") from err
    if done.returncode != 0 or not result_path.exists():
        last = (done.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"PM4Py {task['task']} failed: {last[0][:200]}")
    return json.loads(result_path.read_text(encoding="utf-8"))


def _write_log(df, folder: Path, name: str) -> str:
    work = Path(folder) / WORK_DIR
    work.mkdir(exist_ok=True)
    path = work / name
    df.to_parquet(path, index=False)
    return str(path)


# --------------------------------------------------------------------------- #
# The log PM4Py sees: step runs of a fixed sample of cases
# --------------------------------------------------------------------------- #
def run_log(con: duckdb.DuckDBPyConnection, events_path: Path, max_cases: int):
    """The step runs of at most `max_cases` cases (hash sample) as a pandas table (case_id, activity, start_ts),
    and the number of cases in scope. The first k cases of the sample are the first k of any larger sample."""
    from app.core.metrics import build_tables
    build_tables(con, events_path)
    total = con.execute("SELECT COUNT(*) FROM xcase").fetchall()[0][0]
    from app.core.stable import case_key_sql
    # The sample follows the cases' content (stable.py), not their salted IDs: the same in every data folder.
    stable_keys = case_key_sql("xrun", "CAST(start_ts AS VARCHAR) || '~' || activity", "run_no, start_ts")
    df = con.execute(f"""
        SELECT r.case_id, r.activity, r.start_ts FROM xrun r
        JOIN (SELECT case_id, row_number() OVER (ORDER BY ckey, case_id) AS k
              FROM ({stable_keys}) kk) s USING (case_id)
        WHERE s.k <= {int(max_cases)}
        ORDER BY s.k, r.run_no""").df()
    return df, total


def _sample(df, n: int):
    """The first n cases of a run log (it is ordered by the fixed hash sample)."""
    keep = list(dict.fromkeys(df["case_id"]))[:int(n)]
    return df[df["case_id"].isin(set(keep))]


def histogram(per_case: list[float]) -> list[dict]:
    n = len(per_case) or 1
    out = []
    for lo, hi, label in BUCKETS:
        k = sum(1 for f in per_case if lo <= f < hi)
        out.append({"bucket": label, "cases": k, "share": k / n})
    return out


# --------------------------------------------------------------------------- #
# Section 1: the discovered model and its conformance
# --------------------------------------------------------------------------- #
def discover(con: duckdb.DuckDBPyConnection, events_path: Path) -> dict:
    """The `process_model` section of mining.json: {available, reason, ...}; also writes pm4py_conformance.json."""
    ok, why = status()
    if not ok:
        return {"available": False, "reason": why}
    cfg = _cfg()
    folder = Path(events_path).parent
    start = time.perf_counter()
    df, total = run_log(con, Path(events_path), int(cfg.get("max_cases", 20000)))
    if df["case_id"].nunique() < 2 or df["activity"].nunique() < 2:
        return {"available": False, "reason": "Not available: a process model needs at least 2 cases and 2 steps."}
    noise = float(cfg.get("noise_threshold", 0.2))
    try:
        model = call({"task": "discover", "log": _write_log(df, folder, "log.parquet"), "noise_threshold": noise,
                      "bpmn_out": str(folder / BPMN_FILE), "pnml_out": str(folder / PNML_FILE)}, folder)
    except PM4PyTimeout:
        secs = time.perf_counter() - start
        log.warning("PM4Py model not built in time (%.1f s, pm4py.timeout_s = %g)", secs, timeout_s())
        (folder / CONFORMANCE_FILE).unlink(missing_ok=True)
        return {"available": False, "reason": f"PM4Py model not built in time ({secs:.0f} s; pm4py.timeout_s = "
                                              f"{timeout_s():g}). Everything else is complete."}
    prec, prec_cases, prec_note = _precision(df, folder / PNML_FILE, folder, "precision_log.parquet")
    any_order = bool(model.get("any_order"))
    show = not any_order and (prec is None or prec >= float(cfg.get("min_precision_to_show", 0.4)))
    structure_note = None if show else no_order_message(prec)
    if any_order:
        # Every step in parallel, optional and repeatable: every case fits, so alignments find nothing (and are slow).
        dev, method = ({"sampled_cases": model["cases"], "skipped": [], "extra": [], "out_of_order": [],
                        "token_problems": model["token_problems"]},
                       "token replay (alignments not run: the model allows any order)")
    else:
        dev, method = _align(df, folder / PNML_FILE, folder, "align_log.parquet", model)
    seconds = round(time.perf_counter() - start, 2)
    log.info("PM4Py model built in %.1f s (%s cases)", seconds, df["case_id"].nunique())
    conformance = {"fitness": model["fitness"], "precision": prec, "precision_cases": prec_cases,
                   "precision_note": prec_note, "any_order": any_order, "show_model": show,
                   "structure_note": structure_note,
                   "fitting_cases_share": model["fitting_cases_share"], "cases_used": int(df["case_id"].nunique()),
                   "cases_total": int(total), "histogram": histogram(model["case_fitness"]), "method": method,
                   "deviations": dev, "noise_threshold": noise, "pm4py_version": model["pm4py_version"],
                   "seconds": seconds, "licence": LICENCE_NOTE}
    (folder / CONFORMANCE_FILE).write_text(json.dumps(conformance, indent=2), encoding="utf-8")
    return {"available": True, "reason": None, "dot": model["dot"], "bpmn_file": BPMN_FILE,
            "cases_used": conformance["cases_used"], "cases_total": int(total), "runs_used": len(df),
            "steps": int(df["activity"].nunique()), "noise_threshold": noise,
            "fitness": {"log_fitness": model["fitness"], "fitting_cases_share": model["fitting_cases_share"]},
            "precision": prec, "gateways": model["gateways"], "tree": model["tree"], "show_model": show,
            "structure_note": structure_note,
            "pm4py_version": model["pm4py_version"], "seconds": seconds, "licence": LICENCE_NOTE}


def _precision(df, pnml: Path, folder: Path, name: str) -> tuple[float | None, int, str | None]:
    """Token-replay precision on the first pm4py.precision_max_cases cases of the sample (it is by far the
    slowest PM4Py step); (None, n, why) when switched off or not done in time - the model is kept either way."""
    cfg = _cfg()
    if not cfg.get("precision", True):
        return None, 0, "switched off (pm4py.precision)"
    sample = _sample(df, int(cfg.get("precision_max_cases", 200)))
    n = int(sample["case_id"].nunique())
    try:
        res = call({"task": "precision", "log": _write_log(sample, folder, name), "pnml": str(pnml)}, folder)
    except PM4PyTimeout:
        log.warning("PM4Py precision not computed in time (pm4py.timeout_s = %g)", timeout_s())
        return None, n, f"not computed in time (pm4py.timeout_s = {timeout_s():g})"
    return res["precision"], n, None


def _align(df, pnml: Path, folder: Path, name: str, replay: dict) -> tuple[dict, str]:
    """Alignment deviations on the first pm4py.alignment_max_cases cases; token-replay problems per step when
    the alignments do not finish in time."""
    sample = _sample(df, int(_cfg().get("alignment_max_cases", 1000)))
    try:
        return (call({"task": "align", "log": _write_log(sample, folder, name), "pnml": str(pnml)}, folder),
                "alignments")
    except PM4PyTimeout:
        log.warning("PM4Py alignments not done in time (pm4py.timeout_s = %g): token replay used", timeout_s())
        return ({"sampled_cases": replay["cases"], "skipped": [], "extra": [], "out_of_order": [],
                 "token_problems": replay["token_problems"]}, "token replay (alignments did not finish in time)")


def no_order_message(precision: float | None) -> str:
    """What page 3 and the report say instead of an empty Level 3 model."""
    p = f" (precision {precision:.2f})" if precision is not None else ""
    return (f"No stable order found in the data at Level 3{p}. Cases show only parts of the process, so steps appear "
            "in almost any order. Use the confirmed order (page 4) and see 'Inside each step' for the detailed flow.")


def build(folder: Path) -> dict:
    """Page 3 "Build model" (pm4py.build_on_run false): discover and store the section in mining.json."""
    from app.core.db import connect
    folder = Path(folder)
    path = folder / "mining.json"
    mining = json.loads(path.read_text(encoding="utf-8"))
    try:
        model = discover(connect(), folder / "events.parquet")
    except Exception as err:  # noqa: BLE001 - optional add-on: report, never fail the page
        log.exception("PM4Py could not build the model")
        model = {"available": False, "reason": f"Not available: PM4Py could not build the model "
                                               f"({type(err).__name__}: {str(err)[:200]})."}
    mining["process_model"] = {"available": model["available"], "reason": model["reason"],
                               "data": model if model["available"] else None}
    path.write_text(json.dumps(mining, indent=2, default=str), encoding="utf-8")
    return mining["process_model"]


def load_json(folder: Path, name: str) -> dict | None:
    try:
        return json.loads((Path(folder) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Section 2: the APPROVED process as a model
# --------------------------------------------------------------------------- #
def definition_blocks(defn) -> tuple[list[dict], list[str]]:
    """The approved definition as model blocks, in the approved order, and notes on how it was read.

    - steps in their approved order make a sequence;
    - a step that is not mandatory, or that an "optional_skip" flow jumps over, may be skipped;
    - an "allowed_loop" flow from a step to itself lets it repeat;
    - steps with the SAME position may be done in any order (AND);
    - the definition has no way to mark alternatives (XOR), so none are built.
    """
    steps = defn.ordered_steps()
    pos = {s["id"]: i for i, s in enumerate(steps)}
    optional = {s["id"] for s in steps if not s.get("mandatory", True)}
    loops = set()
    for f in defn.flows:
        a, b = pos.get(f.get("from_step")), pos.get(f.get("to_step"))
        if a is None or b is None:
            continue
        if f.get("kind") == "optional_skip":
            optional |= {steps[i]["id"] for i in range(min(a, b) + 1, max(a, b))}
        if f.get("kind") == "allowed_loop" and a == b:
            loops.add(steps[a]["id"])
    blocks, notes = [], []
    groups: dict = {}
    for s in steps:
        groups.setdefault(s.get("position"), []).append(s)
    for _p, group in groups.items():
        items = [{"kind": "step", "name": s["name"], "optional": s["id"] in optional, "loop": s["id"] in loops}
                 for s in group]
        blocks.append(items[0] if len(items) == 1 else {"kind": "and", "steps": items})
    if any(b["kind"] == "and" for b in blocks):
        notes.append("Steps with the same position may be done in any order.")
    if optional:
        notes.append(f"Optional steps (may be skipped): {', '.join(s['name'] for s in steps if s['id'] in optional)}.")
    if loops:
        notes.append(f"Steps that may repeat: {', '.join(s['name'] for s in steps if s['id'] in loops)}.")
    if not optional and not loops and all(b["kind"] == "step" for b in blocks):
        notes.append("The definition is an ordered list of steps, so the model is a plain sequence.")
    notes.append("The definition marks no alternatives (either-or steps), so the model has none.")
    return blocks, notes


def definition_to_petri(defn):
    """(net, initial marking, final marking) of the approved definition (imports PM4Py in this process)."""
    from app.core.pm4py_worker import _block_tree, _pm4py
    blocks, _notes = definition_blocks(defn)
    return _pm4py().convert_to_petri_net(_block_tree(blocks))


def _relabel(df, defn):
    """The run log in the definition's step names; activities no step maps are left out and counted."""
    import pandas as pd
    a2s = defn.activity_to_step()
    names = {s["id"]: s["name"] for s in defn.steps}
    step = df["activity"].map(lambda a: names.get(a2s.get(a)))
    missing = df[step.isna()].groupby("activity").agg(runs=("case_id", "size"), cases=("case_id", "nunique"))
    out = df.assign(activity=step)[step.notna()]
    out = out[(out["case_id"] != out["case_id"].shift()) | (out["activity"] != out["activity"].shift())]
    return (pd.DataFrame(out), [{"activity": a, "runs": int(r.runs), "cases": int(r.cases)}
                                for a, r in missing.sort_values("runs", ascending=False).iterrows()])


def check_definition(job: dict, defn) -> dict:
    """Reality against the APPROVED process, on the same case sample as the discovered model. Saves and returns
    pm4py_definition_check.json: {available, reason, fitness, precision, fitting_cases_share, steps [...],
    not_in_definition [...], notes, sampled_cases, ...}."""
    from app.core import jobs
    from app.core.db import connect
    ok, why = status()
    folder = jobs.job_folder(job["job_id"])
    if not ok:
        return {"available": False, "reason": why}
    if defn is None or defn.status != "approved":
        return {"available": False, "reason": "Not available: approve the business definition on page 4 first."}
    df, total = run_log(connect(), folder / "events.parquet", int(_cfg().get("max_cases", 20000)))
    log_df, not_in = _relabel(df, defn)
    blocks, notes = definition_blocks(defn)
    out = _check(folder, log_df, total, blocks, notes, [s["name"] for s in defn.ordered_steps()], "def",
                 {"definition": defn.name, "version": defn.version, "not_in_definition": not_in})
    if out["available"]:
        (folder / DEFINITION_FILE).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


ORDER_FILE = "pm4py_order_check.json"


def order_blocks(order: dict) -> tuple[list[dict], list[str], list[str]]:
    """The step order page 3 and the report use (reference Order as dict) as model blocks: its drawn steps in
    order; a step not marked mandatory may be skipped. Returns (blocks, notes, steps)."""
    steps = [x for st in order.get("stages") or [] for x in st.get("steps") or []]
    mandatory = set(order.get("mandatory_steps") or [])
    blocks = [{"kind": "step", "name": x, "optional": x not in mandatory, "loop": False} for x in steps]
    notes = ["Steps are expected in this order; a step done before an earlier one counts as out of order, a repeat "
             "of a step as extra."]
    notes.append("No step is mandatory, so a step a case does not do is not a deviation." if not mandatory else
                 f"Mandatory steps: {', '.join(x for x in steps if x in mandatory)}; the others may be skipped.")
    return blocks, notes, steps


def check_order(job: dict) -> dict:
    """The cases against the step order page 3 and the report use (reference.resolve: confirmed, saved, template,
    AI, milestones or data), when an approved definition is not there yet. Saves pm4py_order_check.json."""
    from app.core import jobs, reference
    from app.core.db import connect
    ok, why = status()
    folder = jobs.job_folder(job["job_id"])
    if not ok:
        return {"available": False, "reason": why}
    name = Path(job.get("source_name") or "Process").stem or "Process"
    ref = reference.compute_for_job(connect(), folder / "events.parquet", job, name)
    blocks, notes, steps = order_blocks(ref["order"])
    if len(steps) < 2:
        return {"available": False, "reason": "Not available: the step order has fewer than 2 steps."}
    df, total = run_log(connect(), folder / "events.parquet", int(_cfg().get("max_cases", 20000)))
    inside = df["activity"].isin(set(steps))
    not_in = (df[~inside].groupby("activity").agg(runs=("case_id", "size"), cases=("case_id", "nunique"))
              .sort_values("runs", ascending=False))
    log_df = df[inside]
    log_df = log_df[(log_df["case_id"] != log_df["case_id"].shift()) | (log_df["activity"] != log_df["activity"].shift())]
    out = _check(folder, log_df, total, blocks, notes, steps, "order",
                 {"order_badge": ref.get("source_badge"), "order_choice": ref.get("choice"),
                  "not_in_order": [{"activity": a, "runs": int(r.runs), "cases": int(r.cases)}
                                   for a, r in not_in.iterrows()]})
    if out["available"]:
        (folder / ORDER_FILE).write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def _check(folder: Path, log_df, total: int, blocks: list[dict], notes: list[str], steps: list[str], prefix: str,
           extra: dict) -> dict:
    """Replay a run log on a model given as blocks: fitness and cases that fit (all cases), precision (sample),
    and per step the share of sampled cases that skip it, do it at an unexpected point, or do it as an extra."""
    start = time.perf_counter()
    pnml = Path(folder) / f"{prefix}_process.pnml"
    try:
        res = call({"task": "model", "blocks": blocks, "log": _write_log(log_df, folder, f"{prefix}_log.parquet"),
                    "pnml_out": str(pnml)}, folder)
    except PM4PyTimeout:
        return {"available": False, "reason": f"PM4Py check not finished in time (pm4py.timeout_s = "
                                              f"{timeout_s():g}). Everything else is complete."}
    prec, prec_cases, prec_note = _precision(log_df, pnml, folder, f"{prefix}_precision_log.parquet")
    dev, method = _align(log_df, pnml, folder, f"{prefix}_align_log.parquet", res)
    share = lambda key, name: next((x["share"] for x in dev.get(key) or [] if x["step"] == name), 0.0)  # noqa: E731
    rows = [{"step": x, "skipped_share": share("skipped", x), "out_of_order_share": share("out_of_order", x),
             "extra_share": share("extra", x)} for x in steps]
    if method != "alignments":
        notes = notes + ["Alignments did not finish in time: per step, the share of cases with token-replay problems "
                         "is shown as 'skipped'."]
        tokens = {x["step"]: x["share"] for x in dev.get("token_problems") or []}
        for r in rows:
            r["skipped_share"] = tokens.get(r["step"], 0.0)
    return {"available": True, "reason": None, **extra,
            "fitness": res["fitness"], "precision": prec, "fitting_cases_share": res["fitting_cases_share"],
            "cases_used": int(log_df["case_id"].nunique()), "cases_total": int(total),
            "sampled_cases": dev.get("sampled_cases", 0), "steps": rows, "method": method,
            "precision_cases": prec_cases, "precision_note": prec_note,
            "most_skipped": [x for x in sorted(rows, key=lambda x: -x["skipped_share"]) if x["skipped_share"]][:5],
            "most_out_of_order": [x for x in sorted(rows, key=lambda x: -x["out_of_order_share"])
                                  if x["out_of_order_share"]][:5],
            "most_extra": [x for x in sorted(rows, key=lambda x: -x["extra_share"]) if x["extra_share"]][:5],
            "notes": notes, "seconds": round(time.perf_counter() - start, 2), "licence": LICENCE_NOTE,
            "caveat": "This measures recorded clicks. A step done outside the recorded apps (phone, paper, email) "
                      "shows as skipped."}


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #
def _kind(node) -> str:
    return type(node).__name__


def count_gateways(bpmn) -> dict:
    out = {"exclusive": 0, "parallel": 0, "inclusive": 0}
    for n in bpmn.get_nodes():
        k = _kind(n)
        if "Exclusive" in k:
            out["exclusive"] += 1
        elif "Parallel" in k:
            out["parallel"] += 1
        elif "Inclusive" in k:
            out["inclusive"] += 1
    return out


def to_dot(bpmn) -> str:
    """Graphviz DOT of the BPMN model: tasks as boxes, × exclusive choice, + parallel, ○ inclusive gateways."""
    from app.core.mining import _dot_text
    ids, lines = {}, ["digraph bpmn {", "  rankdir=LR;",
                      '  node [fontname="Helvetica", fontsize=11];', '  edge [color="#666666", arrowsize=0.7];']
    for i, n in enumerate(bpmn.get_nodes()):
        ids[n] = f"n{i}"
        k = _kind(n)
        if "StartEvent" in k:
            attrs = 'shape=circle, label="", width=0.3, style=filled, fillcolor="#e3f4ea", color="#1f7a4d"'
        elif "EndEvent" in k:
            attrs = 'shape=doublecircle, label="", width=0.25, style=filled, fillcolor="#fbe1e1", color="#a61b1b"'
        elif "Gateway" in k:
            sign = "×" if "Exclusive" in k else "+" if "Parallel" in k else "○"
            attrs = f'shape=diamond, label="{sign}", width=0.4, height=0.4, fixedsize=true, fontsize=14'
        else:
            attrs = (f'shape=box, style="rounded,filled", fillcolor="#eef3fb", color="#4a6fa5", '
                     f'label="{_dot_text(n.get_name())}"')
        lines.append(f"  {ids[n]} [{attrs}];")
    for f in bpmn.get_flows():
        lines.append(f"  {ids[f.get_source()]} -> {ids[f.get_target()]};")
    lines.append("}")
    return "\n".join(lines)
