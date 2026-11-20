"""Evidence planner: which signals can tell the step, the activities, the order or the case -> nc_evidence_plan.json,
and how good each one is, measured by code -> nc_evidence_scores.json.

The AI (task nc_evidence_plan) sees only a profile: column names, types, fill rates, distinct counts, value SHAPES,
the top cleaned screens and clicks with counts, the capabilities and the pack's steps. It returns a plan of
signals from the 12 allowed ones; without the AI a default plan is made from the capabilities, so there is always
a plan. Code then scores every planned signal 0..1 with the reason: coverage, consistency, agreement with the l3
labels on the labelled events (when there are labels) and repeatability across actors and weeks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.core.config import settings
from app.core.db import connect, sql_literal

PLAN_FILE = "nc_evidence_plan.json"
SCORES_FILE = "nc_evidence_scores.json"
SIGNALS = ("source_label", "screen", "action", "completion_verb", "id_in_text", "episode_sequence", "co_occurrence",
           "app_transition", "time_pattern", "document", "library_rule", "sme_answer")
USES = ("step_assignment", "activities", "order", "case_link")


def _cfg() -> dict:
    return settings()["nocase"]["evidence"]


# --------------------------------------------------------------------------- plan
def default_plan(caps: dict, process: str | None) -> dict:
    """The plan without the AI, from the capabilities only."""
    fill = caps.get("fill") or {}
    has = {c: (fill.get(c) or {}).get("rows", 0) > 0 for c in fill}
    plan, skip = [], []

    def use(u, sig, col, why):
        plan.append({"use": u, "signal": sig, "column": col, "why": why, "expected": "measured by code"})
    if has.get("l3"):
        use("step_assignment", "source_label", "l3", "the export carries step labels")
    else:
        skip.append({"signal": "source_label", "reason": "no step labels in the export"})
    for sig, col in (("screen", "screen_clean"), ("action", "action_clean")):
        use("step_assignment", sig, col, f"cleaned {sig}s name the work being done")
        use("activities", sig, col, f"activities are groups of {sig}s inside a step")
    use("activities", "completion_verb", "action_clean", "completion clicks (submit, post, send) end activities")
    if (caps.get("id_shapes") or {}).get("overall"):
        use("case_link", "id_in_text", "text", "ID shapes were found in the text")
    else:
        skip.append({"signal": "id_in_text", "reason": "no ID shapes in the text"})
    use("order", "episode_sequence", "episode_id", "steps that follow each other inside work episodes")
    use("step_assignment", "co_occurrence", "episode_id", "units done in the same episodes belong together")
    if has.get("app"):
        use("step_assignment", "app_transition", "app", "the application narrows the step")
    use("step_assignment", "time_pattern", "ts", "some steps happen at fixed times (month end)")
    if process:
        use("step_assignment", "library_rule", "rules.yaml", "the process library's rules")
        use("step_assignment", "document", "docs", "the process documents describe the steps")
    skip.append({"signal": "sme_answer", "reason": "no SME answers yet (review page, N5)"})
    return {"plan": plan, "skip": skip, "questions_for_sme": [], "source": "default plan (no AI)"}


def profile_payload(folder: Path, caps: dict, pack: dict | None) -> str:
    """The profile the AI sees: names, types, fills, distincts, shapes, top cleaned screens / clicks; no values."""
    from app.nocase.clean import CLEAN_FILE
    from app.nocase.payloads import step_lines
    con = connect()
    ev = f"read_parquet({sql_literal((folder / CLEAN_FILE).as_posix())})"
    top = int(_cfg()["profile_top"])
    lines = ["Columns (fill %):"] + [f"- {c}: {100 * v['share']:.0f}%" for c, v in (caps.get("fill") or {}).items()
                                      if v["rows"]]
    shapes = [s["shape"].replace("9", "N") for s in (caps.get("id_shapes") or {}).get("overall", [])[:5]]
    lines += ["ID shapes (A = letter, N = digit): " + (", ".join(shapes) or "none")]
    for col in ("screen_clean", "action_clean"):
        rows = con.execute(f"SELECT {col}, COUNT(*) / SUM(COUNT(*)) OVER () FROM {ev} WHERE {col} IS NOT NULL "
                           f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {top}").fetchall()
        lines += [f"Top {col.split('_')[0]}s (share of events):"] + [f"- {v} ({100 * sh:.1f}%)" for v, sh in rows]
    if pack:
        lines += ["Process steps:"] + [f"- {x}" for x in step_lines(pack)]
    return "\n".join(lines)


def check_plan(data) -> list[str]:
    errs = []
    for i, p in enumerate(data.get("plan") or []):
        if not isinstance(p, dict):
            errs.append(f"plan[{i}] must be an object")
            continue
        if p.get("signal") not in SIGNALS:
            errs.append(f"plan[{i}].signal must be one of: {', '.join(SIGNALS)}")
        if p.get("use") not in USES:
            errs.append(f"plan[{i}].use must be one of: {', '.join(USES)}")
    if not (data.get("plan") or []):
        errs.append("plan must list at least one signal")
    return errs


def plan(folder: Path, caps: dict, pack: dict | None, process: str | None, gw=None) -> dict:
    base = default_plan(caps, process)
    if gw is not None:
        msgs = [{"role": "system", "content": _cfg()["plan_prompt"]},
                {"role": "user", "content": profile_payload(folder, caps, pack)}]
        a = gw.ask("nc_evidence_plan", msgs, check_plan)
        if a.ok:
            d = a.data
            out = {"plan": [p for p in d["plan"] if p.get("signal") in SIGNALS], "skip": d.get("skip") or [],
                   "questions_for_sme": d.get("questions_for_sme") or [], "source": f"ai:{a.model}",
                   "provider": a.provider}
            return out
        base["ai_note"] = a.reason
    return base


# --------------------------------------------------------------------------- scores
def _purity(df: pd.DataFrame, key: str) -> float | None:
    d = df.dropna(subset=[key, "l3"])
    if d.empty:
        return None
    counts = d.groupby([key, "l3"]).size()
    return float(counts.groupby(level=0).max().sum() / len(d))


def _repeat(df: pd.DataFrame, key: str, overall: float | None) -> float | None:
    """Share of actors and of weeks whose own agreement is within evidence.repeat_tolerance of the overall."""
    if overall is None:
        return None
    tol = float(_cfg()["repeat_tolerance"])
    shares = []
    for grp in ("actor", "week"):
        vals = [_purity(g, key) for _, g in df.groupby(grp) if len(g) >= int(_cfg()["repeat_min_events"])]
        vals = [v for v in vals if v is not None]
        if vals:
            shares.append(sum(v >= overall - tol for v in vals) / len(vals))
    return sum(shares) / len(shares) if shares else None


def score_signal(sig: str, ev: pd.DataFrame, ctx: dict) -> dict:
    """{score, coverage, consistency, agreement, repeatability, reason} for one signal."""
    n = len(ev) or 1
    labelled = ev["l3"].notna().any()
    comp: dict = {"coverage": None, "consistency": None, "agreement": None, "repeatability": None}
    why = ""
    if sig == "source_label":
        comp["coverage"] = float(ev["l3"].notna().mean())
        comp["consistency"] = ctx.get("label_consistency")
        comp["agreement"] = 1.0 if labelled else None
        why = f"labels on {100 * comp['coverage']:.1f}% of events"
    elif sig in ("screen", "action", "app_transition", "time_pattern", "completion_verb"):
        key = {"screen": "screen_clean", "action": "action_clean", "app_transition": "app", "time_pattern": "hour",
               "completion_verb": "completion"}[sig]
        comp["coverage"] = float(ev[key].notna().mean())
        comp["agreement"] = _purity(ev, key) if labelled else None
        comp["repeatability"] = _repeat(ev, key, comp["agreement"]) if labelled else None
        vc = ev[key].value_counts(normalize=True)
        comp["consistency"] = float(1 - vc.iloc[0]) if len(vc) > 1 else 0.0       # one value only says nothing
        why = (f"{key.replace('_', ' ')} on {100 * comp['coverage']:.1f}% of events"
               + (f"; it predicts the labelled step for {100 * comp['agreement']:.1f}% of labelled events"
                  if comp["agreement"] is not None else "; no labels to check it against"))
    elif sig == "id_in_text":
        cl = ctx.get("case_link") or {}
        comp["coverage"] = float(cl.get("coverage") or 0.0)
        comp["consistency"] = 1 - float(cl.get("conflict_share") or 0.0)
        why = f"IDs cover {100 * comp['coverage']:.1f}% of events ({cl.get('decision', 'not tested')})"
    elif sig in ("episode_sequence", "co_occurrence"):
        if "episode_id" not in ev.columns:
            return {"score": 0.0, **comp, "reason": "no episodes"}
        sizes = ev.groupby("episode_id").size()
        comp["coverage"] = float(sizes[sizes > 1].sum() / n)
        if labelled:
            d = ev.dropna(subset=["l3"]).sort_values(["episode_id", "ts", "eid"])
            same = d["episode_id"].eq(d["episode_id"].shift())
            comp["agreement"] = float((d["l3"].eq(d["l3"].shift()) & same).sum() / max(same.sum(), 1))
        why = (f"{100 * comp['coverage']:.1f}% of events share an episode with others"
               + (f"; the next labelled event in the episode has the same step {100 * comp['agreement']:.1f}% of the time"
                  if comp["agreement"] is not None else ""))
    elif sig == "library_rule":
        comp["coverage"] = ctx.get("rule_coverage")
        comp["agreement"] = ctx.get("rule_agreement")
        why = ("library rules cover " + ("–" if comp["coverage"] is None else f"{100 * comp['coverage']:.1f}%")
               + " of events")
    elif sig == "document":
        comp["coverage"] = 1.0 if ctx.get("documents") else 0.0
        why = f"{ctx.get('documents', 0)} process document(s) in the library"
    elif sig == "sme_answer":
        comp["coverage"] = float(ctx.get("sme_answer_coverage") or 0.0)
        why = (f"SME answers recorded for {ctx.get('sme_answers', 0)} question(s)"
               if ctx.get("sme_answers") else "no SME answers yet")
    # Signals that are judged against steps cannot be scored before any event has a step: "n/a", never 1.00
    if sig not in ("source_label", "id_in_text", "document", "library_rule", "sme_answer") \
            and int(ev["l3"].notna().sum()) < int(_cfg().get("min_labelled_events", 200)):
        return {"score": None, **{k: None if v is None else round(float(v), 4) for k, v in comp.items()},
                "reason": "n/a: no steps yet to score against (" + why + ")" if why else
                "n/a: no steps yet to score against"}
    w = _cfg()["component_weights"]
    others = {k: v for k, v in comp.items() if v is not None and k != "agreement"}
    total = sum(float(w[k]) for k in others)
    rest = sum(float(w[k]) * v for k, v in others.items()) / total if total else 0.0
    if comp["agreement"] is not None:            # with labels, agreement decides: a signal that disagrees is weak
        k = float(_cfg()["agreement_share"])
        score = comp["agreement"] * ((1 - k) + k * rest) if others else comp["agreement"]
    else:
        score = rest
    return {"score": round(score, 4), **{k: None if v is None else round(float(v), 4) for k, v in comp.items()},
            "reason": why}


def build(folder: Path, process: str | None = None, gw=None) -> dict:
    """Plan (AI or default) + code scores -> both files; returns {"plan", "scores"}."""
    from app.nocase import library, profile
    from app.nocase.clean import CLEAN_FILE
    from app.nocase.strategies.base import load_decisions
    caps = profile.load(folder) or {}
    pack = library.load_pack(process) if process else None
    p = plan(folder, caps, pack, process, gw)
    con = connect()
    ev = con.execute(f"SELECT * FROM read_parquet({sql_literal((folder / CLEAN_FILE).as_posix())})").df()
    ev["hour"] = ev["ts"].dt.hour.astype(str)
    ev["week"] = ev["ts"].dt.strftime("%G-%V")
    verbs = [str(v).lower() for v in settings()["milestones"]["verbs"]]
    ev["completion"] = ev["action_clean"].where(
        ev["action_clean"].fillna("").str.lower().str.split().str[0].isin(verbs))
    ctx = {"label_consistency": (caps.get("labels") or {}).get("consistency"),
           "case_link": (load_decisions(folder).get("case_link") or {}).get("quality") | {
               "decision": (load_decisions(folder).get("case_link") or {}).get("decision")}
           if load_decisions(folder).get("case_link") else {}}
    if process:
        docs = library.pack_dir(process) / "docs"
        ctx["documents"] = len([f for f in docs.glob("*") if f.is_file()]) if docs.exists() else 0
        if "step_source" in ev.columns:
            ctx["rule_coverage"] = float((ev["step_source"] == "rule").mean())
        from app.nocase.knowledge.library import load as load_knowledge
        knowledge = load_knowledge(process)
        answers = [row for row in knowledge["decisions"] if row.get("action") == "sme_answer"]
        ctx["sme_answers"] = len(answers)
        question_count = max(1, len(p.get("questions_for_sme") or []))
        ctx["sme_answer_coverage"] = min(1.0, len(answers) / question_count)
        if answers:
            p["plan"] = [item for item in p.get("plan", []) if item.get("signal") != "sme_answer"]
            p["plan"].append({"use": "step_assignment", "signal": "sme_answer", "column": "decisions.jsonl",
                              "why": f"{len(answers)} SME answer(s) are logged as review evidence",
                              "expected": "reviewed by a person"})
            p["skip"] = [item for item in p.get("skip", []) if item.get("signal") != "sme_answer"]
    scores = {}
    for sig in dict.fromkeys(x["signal"] for x in p["plan"]):
        scores[sig] = score_signal(sig, ev, ctx)
    (folder / PLAN_FILE).write_text(json.dumps(p, indent=2, default=str), encoding="utf-8")
    (folder / SCORES_FILE).write_text(json.dumps(scores, indent=2, default=str), encoding="utf-8")
    return {"plan": p, "scores": scores}


def load_scores(folder: Path) -> dict:
    p = folder / SCORES_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
