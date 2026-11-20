"""The N2 stages after units: episodes -> case_link -> assign. Each runs or is skipped with a written reason, and
its decision (strategy, what was tried and why, quality, a plain message) is saved in nc_decisions.json.

run_all(folder) = pipeline.run_stages (profile, clean, units) + these stages.
"""
from __future__ import annotations

from pathlib import Path

from app.core.db import connect
from app.nocase import profile
from app.nocase.clean import CLEAN_FILE
from app.nocase.strategies import Context
from app.nocase.strategies.base import choose, save_decision
from app.nocase.units import UNITS_FILE

STAGES = ("episodes", "case_link", "assign")


def context(folder: Path, forced: dict | None = None, process: str | None = None, records: dict | None = None,
            ai=None) -> Context:
    return Context(folder=folder, caps=profile.load(folder) or {}, forced=dict(forced or {}), process=process,
                   records=records, ai=ai)


def _missing(ctx: Context) -> str | None:
    if not ctx.path(CLEAN_FILE).exists() or not ctx.path(UNITS_FILE).exists():
        return "needs cleaned events and units (clean / units did not run)"
    return None


def run_episodes(ctx: Context) -> dict:
    from app.core.config import settings
    from app.nocase.strategies.base import REGISTRY, order
    from app.nocase.strategies.episodes import data_score
    con = connect()
    scores = {}
    if settings()["nocase"]["episodes"].get("auto_pick") and "episodes" not in ctx.forced:
        tried = []
        for name in order("episodes"):
            cand = REGISTRY["episodes"][name]()
            ok, why = cand.applies(ctx.caps, ctx)
            tried.append({"strategy": name, "applies": bool(ok), "reason": why, "forced": False})
            if ok:
                scores[name] = data_score(cand.compute(con, ctx))
        # a last-resort strategy (DayWindow: one episode per person per day) is only picked when nothing else
        # applies: a day is never a better "sitting" than real gaps, whatever the score says
        last_resort = set(settings()["nocase"]["episodes"].get("last_resort") or [])
        pool = {k: v for k, v in scores.items() if k not in last_resort} or scores
        best = max(pool, key=lambda k: (pool[k]["score"], -order("episodes").index(k)))
        s = REGISTRY["episodes"][best]()
        tried.append({"strategy": best, "applies": True, "forced": False,
                      "reason": f"picked by the data-only score {scores[best]['score']:.2f} (of {len(scores)} that apply)"})
    else:
        s, tried = choose("episodes", ctx)
    s.run(con, ctx)
    q = s.quality(con, ctx)
    d = {"stage": "episodes", "strategy": s.name, "tried": tried, "quality": q, "scores": scores,
         "message": (f"Episodes: {s.name} — {q['episodes']:,} work episodes, median {q['median_events']:g} events "
                     f"and {q['median_duration_s'] / 60:.1f} minutes; {100 * q['single_event_share']:.1f}% have "
                     f"one event. ({tried[-1]['reason']})")}
    save_decision(ctx.folder, "episodes", d)
    return d


def run_case_link(ctx: Context) -> dict:
    from app.nocase.strategies.case_link import message
    con = connect()
    s, tried = choose("case_link", ctx)
    s.run(con, ctx)
    q = s.quality(con, ctx)
    d = {"stage": "case_link", "strategy": s.name, "tried": tried, "decision": q["decision"],
         "reasons": q["reasons"], "quality": {k: v for k, v in q.items() if k not in ("decision", "reasons")},
         "message": message(q)}
    save_decision(ctx.folder, "case_link", d)
    return d


def run_assign(ctx: Context) -> dict:
    from app.nocase.strategies.assign import run_assign as assign
    return assign(connect(), ctx)


def run_n2(folder: Path, forced: dict | None = None, process: str | None = None, records: dict | None = None,
           only: tuple[str, ...] = STAGES, ai=None) -> dict[str, dict]:
    """The N2 stages: {stage: {"status": ran|skipped, "reason"?, "output"?}}."""
    out: dict[str, dict] = {}
    ctx = context(folder, forced, process, records, ai)
    why = _missing(ctx)
    for stage, fn in (("episodes", run_episodes), ("case_link", run_case_link), ("assign", run_assign)):
        if stage not in only:
            continue
        if why:
            out[stage] = {"status": "skipped", "reason": why}
            continue
        out[stage] = {"status": "ran", "output": fn(ctx)}
    return out


def run_evidence(folder: Path, process: str | None = None, ai=None) -> dict:
    from app.nocase.ai.evidence import build
    e = build(folder, process, ai)
    top = sorted(e["scores"].items(), key=lambda kv: -(kv[1]["score"] if kv[1]["score"] is not None else -1))
    d = {"stage": "evidence", "source": e["plan"]["source"], "ai_note": e["plan"].get("ai_note"),
         "scores": {k: v["score"] for k, v in e["scores"].items()},
         "message": f"Evidence ({e['plan']['source']}): " + ", ".join(f"{k} " + ("n/a" if v["score"] is None else f"{v['score']:.2f}") for k, v in top[:6])
         + (f" (AI: {e['plan']['ai_note']})" if e["plan"].get("ai_note") else "")}
    save_decision(folder, "evidence", d)
    return d


def run_activities(folder: Path, process: str | None = None, ai=None) -> dict:
    from app.nocase.engine.activities import build
    a = build(folder, process, ai)
    d = {"stage": "activities", **{k: a[k] for k in ("steps", "activities", "checks")},
         "fallback_steps": a["fallback_steps"],
         "message": (f"Activities: {a['activities']} in {a['steps']} step(s); totals check "
                     f"{'passed' if a['checks']['passed'] else 'FAILED for ' + ', '.join(a['checks']['failed_steps'])}"
                     + (f"; screen-based for {len(a['fallback_steps'])} step(s)" if a["fallback_steps"] else ""))}
    save_decision(folder, "activities", d)
    return d


def run_all(folder: Path, forced: dict | None = None, process: str | None = None, records: dict | None = None,
            ai=None) -> dict[str, dict]:
    """Every built stage, in order: profile, clean, units, episodes, case_link, evidence, assign, activities."""
    from app.nocase.pipeline import run_stages
    stages = run_stages(folder)
    stages.update(run_n2(folder, forced, process, records, only=("episodes", "case_link"), ai=ai))
    ready = all(stages[s]["status"] == "ran" for s in ("units", "episodes"))
    stages["evidence"] = ({"status": "ran", "output": run_evidence(folder, process, ai)} if ready
                          else {"status": "skipped", "reason": "needs units and episodes"})
    stages.update(run_n2(folder, forced, process, records, only=("assign",), ai=ai))
    stages["activities"] = ({"status": "ran", "output": run_activities(folder, process, ai)}
                            if stages["assign"]["status"] == "ran" else
                            {"status": "skipped", "reason": "needs the step assignment"})
    stages.update(run_n4(folder, process, ready=stages["activities"]["status"] == "ran"))
    return stages


def run_n4(folder: Path, process: str | None = None, ready: bool = True) -> dict[str, dict]:
    """Measures, flow, confidence (N4) and inside each step (N4b); each saved in nc_decisions.json."""
    from app.nocase.engine import confidence, flow, inside_step, measures
    out: dict[str, dict] = {}
    if not ready:
        for st in ("measures", "flow", "confidence", "inside_step"):
            out[st] = {"status": "skipped", "reason": "needs the step assignment and activities"}
        return out
    m = measures.build(folder, process)
    o = m["overall"]
    d = {"stage": "measures", "message": f"Measures ({m['basis']['time']} time): {o['events']:,} events, "
         f"{o['hours']:,.1f} h, {o['step_runs']:,} step runs, {100 * o['unassigned_share']:.1f}% of hours without a step"}
    save_decision(folder, "measures", d)
    out["measures"] = {"status": "ran", "output": d}
    f = flow.build(folder, process)
    kept = [e for e in f["edges"] if e["kept"]]
    d = {"stage": "flow", "message": f"Candidate map: {len(f['nodes'])} steps, {len(kept)} inferred edge(s) kept "
         f"(of {len(f['edges'])}), {len(f['spine'])} documented-order edge(s)"}
    save_decision(folder, "flow", d)
    out["flow"] = {"status": "ran", "output": d}
    c = confidence.build(folder, process)
    bands = [v["band"] for v in c["steps"].values()]
    d = {"stage": "confidence", "message": "Confidence by step: " + (", ".join(
        f"{b} {bands.count(b)}" for b in ("High", "Medium", "Low", "Exploratory") if bands.count(b)) or "no steps")}
    save_decision(folder, "confidence", d)
    out["confidence"] = {"status": "ran", "output": d}
    i = inside_step.build(folder, process)
    described = sum(1 for s in i["steps"] if s.get("enough_runs"))
    d = {"stage": "inside_step", "message": f"Inside each step: {described} of {len(i['steps'])} step(s) described; "
         f"totals check {'passed' if i['checks']['passed'] else 'FAILED'}", "lines": inside_step.summary_lines(i)}
    save_decision(folder, "inside_step", d)
    out["inside_step"] = {"status": "ran", "output": d}
    return out
