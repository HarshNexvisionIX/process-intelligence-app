"""The PM4Py calls, run in a SEPARATE PROCESS so a slow call can be stopped (pm4py.timeout_s).

    python -m app.core.pm4py_worker <task.json> <result.json>

task.json: {"task": "discover" | "model" | "precision" | "align", "log": <parquet: case_id, activity, start_ts>, ...}.
Only step names, hashed case IDs and times are in the log. The result is plain JSON. Used by pm4py_model.py
only (PM4Py is AGPL v3: internal use only, see README section 5).

discover  Inductive Miner -> BPMN (DOT text + .bpmn file) and a Petri net (.pnml, reused below); ONE
          token replay: fitness, per-case fitness, cases that fit fully, and per step the share of cases with
          replay problems (the fallback when alignments time out).
model     the APPROVED process (built from the definition's blocks) as a Petri net (.pnml), with the same replay.
precision token-replay precision of a saved net on a small sample (the slowest step, so it runs alone).
align     alignments of the sampled cases on a saved net: per step the share of cases where the model expected
          the step but the case did not do it (skipped, "model move"), where the case did a step the model did
          not expect (extra, "log move"), or both (done at an unexpected point, "out of order").
Each task is its own process with its own time limit: a slow precision or alignment never loses the model.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from collections import Counter

KEYS = {"activity_key": "activity", "case_id_key": "case_id", "timestamp_key": "start_ts"}


def _pm4py():
    with contextlib.redirect_stdout(io.StringIO()):
        import pm4py
    return pm4py


def _log(path: str):
    import pandas as pd
    df = pd.read_parquet(path)
    df["start_ts"] = pd.to_datetime(df["start_ts"])
    return df


def _replay(pm4py, df, net, im, fm) -> dict:
    """ONE token replay: per-case fitness, the log fitness (PM4Py's formula, from the same replay) and per step
    the share of cases with replay problems."""
    diag = pm4py.conformance_diagnostics_token_based_replay(df, net, im, fm, **KEYS)
    per_case = [float(d["trace_fitness"]) for d in diag]
    tot = {k: sum(float(d.get(k) or 0) for d in diag)
           for k in ("missing_tokens", "consumed_tokens", "remaining_tokens", "produced_tokens")}
    fitness = (0.5 * (1 - tot["missing_tokens"] / tot["consumed_tokens"] if tot["consumed_tokens"] else 0.5)
               + 0.5 * (1 - tot["remaining_tokens"] / tot["produced_tokens"] if tot["produced_tokens"] else 0.5))
    problems: Counter = Counter()       # done but not expected: the step fired with missing tokens
    expected: Counter = Counter()       # expected but not done: the step was still enabled when the case ended
    for d in diag:
        for t in {getattr(t, "label", None) for t in d.get("transitions_with_problems") or []}:
            if t:
                problems[t] += 1
        if not d.get("trace_is_fit"):
            for t in {getattr(t, "label", None) for t in d.get("enabled_transitions_in_marking") or []}:
                if t:
                    expected[t] += 1
    n = len(diag) or 1
    steps = sorted(set(problems) | set(expected), key=lambda s: (-(problems[s] + expected[s]), s))
    return {"fitness": fitness, "fitting_cases_share": sum(1 for d in diag if d.get("trace_is_fit")) / n,
            "case_fitness": per_case, "cases": len(diag),
            "token_problems": [{"step": s, "share": max(problems[s], expected[s]) / n,
                                "done_not_expected": problems[s] / n, "expected_not_done": expected[s] / n}
                               for s in steps]}


def _deviations(pm4py, df, net, im, fm) -> dict:
    """Per step: shares of sampled cases with a model move (skipped), a log move (extra), or both (out of order)."""
    aligned = pm4py.conformance_diagnostics_alignments(df, net, im, fm, **KEYS)
    skipped, extra, both = Counter(), Counter(), Counter()
    for a in aligned:
        model_moves = {m for (log, m) in a["alignment"] if log == ">>" and m not in (None, ">>")}
        log_moves = {log for (log, m) in a["alignment"] if m == ">>" and log not in (None, ">>")}
        synced = {log for (log, m) in a["alignment"] if log == m and log not in (None, ">>")}
        # skipped: expected, never done; out of order: done, but never where the model expected it (with optional
        # steps a late step is a free skip plus an unexpected move); extra: also done where expected = a repeat.
        for s in model_moves - log_moves - synced:
            skipped[s] += 1
        for s in log_moves - synced:
            both[s] += 1
        for s in log_moves & synced:
            extra[s] += 1
    n = len(aligned) or 1
    rows = lambda c: [{"step": s, "share": k / n, "cases": k} for s, k in sorted(c.items(), key=lambda x: (-x[1], x[0]))]  # noqa: E731
    return {"sampled_cases": len(aligned), "skipped": rows(skipped), "extra": rows(extra), "out_of_order": rows(both),
            "fitting_cases_share": sum(1 for a in aligned if a.get("fitness", 0) >= 1 - 1e-9) / n}


def discover(task: dict) -> dict:
    pm4py = _pm4py()
    from pm4py.objects.bpmn.exporter import exporter
    from pm4py.objects.petri_net.exporter import exporter as pnml_exporter
    from app.core.pm4py_model import count_gateways, to_dot
    df = _log(task["log"])
    tree = pm4py.discover_process_tree_inductive(df, noise_threshold=float(task["noise_threshold"]), **KEYS)
    bpmn = pm4py.convert_to_bpmn(tree)
    import shutil
    if shutil.which("dot"):                 # with the Graphviz program, PM4Py adds a diagram layout
        pm4py.write_bpmn(bpmn, task["bpmn_out"])
    else:                                   # without it: no coordinates (BPMN tools lay it out on import)
        exporter.apply(bpmn, task["bpmn_out"])
    net, im, fm = pm4py.convert_to_petri_net(tree)
    pnml_exporter.apply(net, im, task["pnml_out"], final_marking=fm)
    out = {"tree": str(tree), "dot": to_dot(bpmn), "gateways": count_gateways(bpmn), "pm4py_version": pm4py.__version__,
           "any_order": _any_order(tree, df["activity"].nunique())}
    out.update(_replay(pm4py, df, net, im, fm))
    return out


def _any_order(tree, n_steps: int) -> bool:
    """True when the model's top level puts (nearly) every step in parallel: the data shows no order at all."""
    from pm4py.objects.process_tree.obj import Operator
    return tree.operator == Operator.PARALLEL and len(tree.children) >= max(3, 0.8 * n_steps)


def precision(task: dict) -> dict:
    """Token-replay precision of the saved Petri net on a (small) sample: the slowest PM4Py step, so on its own."""
    pm4py = _pm4py()
    net, im, fm = pm4py.read_pnml(task["pnml"])
    df = _log(task["log"])
    return {"precision": float(pm4py.precision_token_based_replay(df, net, im, fm, **KEYS)),
            "cases": int(df["case_id"].nunique())}


def align(task: dict) -> dict:
    pm4py = _pm4py()
    net, im, fm = pm4py.read_pnml(task["pnml"])
    return _deviations(pm4py, _log(task["log"]), net, im, fm)


def _block_tree(blocks: list[dict]):
    """The approved process as a process tree: a sequence of blocks; a block is a step (optional = XOR with a
    silent skip; loop = may repeat), an AND of steps (any order) or an XOR of steps (alternatives)."""
    from pm4py.objects.process_tree.obj import Operator, ProcessTree

    def node(op, children):
        t = ProcessTree(operator=op)
        for c in children:
            c.parent = t
            t.children.append(c)
        return t

    def step(b):
        t = ProcessTree(label=b["name"])
        if b.get("loop"):
            t = node(Operator.LOOP, [t, ProcessTree()])
        if b.get("optional"):
            t = node(Operator.XOR, [t, ProcessTree()])
        return t

    parts = []
    for b in blocks:
        if b["kind"] == "step":
            parts.append(step(b))
        else:
            op = Operator.PARALLEL if b["kind"] == "and" else Operator.XOR
            parts.append(node(op, [step(s) for s in b["steps"]]))
    return parts[0] if len(parts) == 1 else node(Operator.SEQUENCE, parts)


def model(task: dict) -> dict:
    """The approved process (blocks) as a Petri net, saved for "precision" and "align", with its token replay."""
    pm4py = _pm4py()
    from pm4py.objects.petri_net.exporter import exporter as pnml_exporter
    net, im, fm = pm4py.convert_to_petri_net(_block_tree(task["blocks"]))
    pnml_exporter.apply(net, im, task["pnml_out"], final_marking=fm)
    out = _replay(pm4py, _log(task["log"]), net, im, fm)
    out.pop("case_fitness")
    return out


def main(argv: list[str]) -> int:
    task = json.loads(open(argv[0], encoding="utf-8").read())
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        result = {"discover": discover, "precision": precision, "align": align, "model": model}[task["task"]](task)
    with open(argv[1], "w", encoding="utf-8") as f:
        json.dump(result, f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
