"""Score the no-case stages against the known answers (truth) -> nc_eval.json. Evaluation only.

Truth comes from <job>/eval/ (eval columns of the mapping, or a bench truth file). Which column is the true case,
step or process: the first available name in nocase.eval.case_truth / step_truth / process_truth.

    episodes   per episode strategy that applies: purity (events-weighted share of an episode's events from its
               dominant true case) and fragmentation (median number of episodes per true case)
    case link  precision and recall of linked event pairs (counted exactly from group sizes, no sampling)
    steps      accuracy by events and by hours against the true step, the unassigned share, the top confusions
    process    with a true process: share of other work (not the target process) wrongly given a step
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.core.config import settings
from app.core.db import connect, sql_literal
from app.nocase.canonical import EVAL_PREFIX
from app.nocase.eval import EVAL_DIR, TRUTH_FILE

EVAL_FILE = "nc_eval.json"


def _cfg() -> dict:
    return settings()["nocase"]["eval"]


def truth_frame(folder: Path) -> pd.DataFrame:
    """eid + every truth column (names without the eval_ prefix)."""
    con = connect()
    frames = []
    p = folder / EVAL_DIR / TRUTH_FILE
    if p.exists():
        df = con.execute(f"SELECT * FROM read_parquet({sql_literal(p.as_posix())})").df()
        frames.append(df.rename(columns={c: c[len(EVAL_PREFIX):] for c in df.columns if c.startswith(EVAL_PREFIX)}))
    p = folder / EVAL_DIR / "truth_file.parquet"
    if p.exists():
        frames.append(con.execute(f"SELECT * FROM read_parquet({sql_literal(p.as_posix())})").df())
    if not frames:
        return pd.DataFrame({"eid": []})
    out = frames[0]
    for f in frames[1:]:
        out = out.merge(f, on="eid", how="outer", suffixes=("", "_file"))
    return out


def _pick(df: pd.DataFrame, names: list[str]) -> str | None:
    return next((n for n in names if n in df.columns and df[n].notna().any()), None)


def _pairs(groups: pd.Series) -> float:
    n = groups.astype(float)
    return float((n * (n - 1) / 2).sum())


def episode_scores(ev: pd.DataFrame, truth: str) -> dict:
    d = ev.dropna(subset=[truth])
    if d.empty:
        return {}
    counts = d.groupby(["episode_id", truth]).size()
    purity = counts.groupby(level=0).max().sum() / len(d)
    frag = d.groupby(truth)["episode_id"].nunique()
    return {"purity": round(float(purity), 4), "fragmentation_median": float(frag.median()),
            "episodes": int(d["episode_id"].nunique()), "true_cases": int(frag.size)}


def case_link_scores(links: pd.DataFrame, truth: pd.DataFrame, col: str) -> dict:
    t = truth.dropna(subset=[col])[["eid", col]]
    j = links.merge(t, on="eid")
    if j.empty:
        return {"linked_events_with_truth": 0}
    same_both = _pairs(j.groupby(["case_id", col]).size())
    pred = _pairs(j.groupby("case_id").size())
    true = _pairs(t.groupby(col).size())
    return {"precision": round(same_both / pred, 4) if pred else None,
            "recall": round(same_both / true, 4) if true else None,
            "linked_events_with_truth": int(len(j)), "events_with_truth": int(len(t))}


def step_scores(ev: pd.DataFrame, col: str) -> dict:
    d = ev.dropna(subset=[col]).copy()
    if d.empty:
        return {}
    d["ok"] = d["step"].fillna("").str.lower() == d[col].astype(str).str.lower()
    hours = d["active_s"].fillna(0)
    wrong = d[~d["ok"] & d["step"].notna()]
    conf = (wrong.groupby([col, "step"]).size().sort_values(ascending=False).head(int(_cfg()["confusion_top"])))
    per_step = d.groupby(col)["ok"].mean().round(4).to_dict()
    return {"accuracy_events": round(float(d["ok"].mean()), 4),
            "wrong_share": round(float((~d["ok"] & d["step"].notna()).mean()), 4),
            "accuracy_by_true_step": {str(k): float(v) for k, v in per_step.items()},
            "accuracy_hours": round(float(hours[d["ok"]].sum() / hours.sum()), 4) if hours.sum() else None,
            "unassigned_share": round(float(d["step"].isna().mean()), 4), "events_with_truth": int(len(d)),
            "confusion_top": [{"true": t, "assigned": s, "events": int(n)} for (t, s), n in conf.items()]}


def process_scores(ev: pd.DataFrame, col: str, target: str | None) -> dict:
    d = ev.dropna(subset=[col])
    if d.empty:
        return {}
    target = target or d[col].mode().iloc[0]
    other = d[d[col] != target]
    return {"target_process": target, "other_work_events": int(len(other)),
            "other_work_given_a_step_share": round(float(other["step"].notna().mean()), 4) if len(other) else 0.0}


def flow_scores(ev: pd.DataFrame, case_col: str, step_col: str, folder: Path) -> dict:
    """Share of the map's kept inferred edges A->B whose direction the truth agrees with: inside true cases,
    true step A is directly followed by true step B more often than the other way round."""
    p = folder / "nc_flow.json"
    if not p.exists():
        return {}
    flow = json.loads(p.read_text(encoding="utf-8"))
    kept = [e for e in flow.get("edges") or [] if e.get("kept")]
    d = ev.dropna(subset=[case_col, step_col]).sort_values([case_col, "ts", "eid"])
    d = d[d[step_col].ne(d.groupby(case_col)[step_col].shift())]
    nxt = d.groupby(case_col)[step_col].shift(-1)
    pairs = d.assign(nx=nxt).dropna(subset=["nx"]).groupby([step_col, "nx"]).size().to_dict()
    judged = [(e, pairs.get((e["src"], e["dst"]), 0), pairs.get((e["dst"], e["src"]), 0)) for e in kept]
    judged = [(e, f, r) for e, f, r in judged if f + r]
    if not judged:
        return {"edges_judged": 0, "kept_edges": len(kept)}
    right = sum(f > r for _, f, r in judged)
    return {"direction_right_share": round(right / len(judged), 4), "edges_judged": len(judged), "kept_edges": len(kept)}


def evaluate(folder: Path, target_process: str | None = None) -> dict:
    """Score everything that has truth; writes nc_eval.json; {} when the job has no truth."""
    from app.nocase import profile
    from app.nocase.clean import CLEAN_FILE
    from app.nocase.strategies.base import REGISTRY, Context
    truth = truth_frame(folder)
    if len(truth.columns) <= 1:
        return {}
    con = connect()
    ev = con.execute(f"SELECT * FROM read_parquet({sql_literal((folder / CLEAN_FILE).as_posix())})").df()
    ev = ev.merge(truth, on="eid", how="left", suffixes=("", "_truth"))
    case_col = _pick(truth, _cfg()["case_truth"])
    step_col = _pick(truth, _cfg()["step_truth"])
    proc_col = _pick(truth, _cfg()["process_truth"])
    case_col = case_col + "_truth" if case_col and case_col + "_truth" in ev.columns else case_col
    step_col = step_col + "_truth" if step_col and step_col + "_truth" in ev.columns else step_col
    out: dict = {"truth": {"case": case_col, "step": step_col, "process": proc_col}}
    if case_col:
        ctx = Context(folder, profile.load(folder) or {})
        per = {}
        for name, cls in REGISTRY["episodes"].items():
            s = cls()
            ok, why = s.applies(ctx.caps, ctx)
            if not ok:
                per[name] = {"applies": False, "reason": why}
                continue
            eps = s.compute(con, ctx)[["eid", "episode_id"]]
            per[name] = {"applies": True, **episode_scores(ev.drop(columns=["episode_id"], errors="ignore")
                                                           .merge(eps, on="eid"), case_col)}
        out["episodes"] = per
        links_path = folder / "nc_case_links.parquet"
        if links_path.exists():
            links = pd.read_parquet(links_path)
            t = ev[["eid", case_col]]
            out["case_link"] = case_link_scores(links[["eid", "case_id"]], t, case_col)
    if step_col and "step" in ev.columns:
        out["steps"] = step_scores(ev, step_col)
    if case_col and step_col:
        out["flow"] = flow_scores(ev, case_col, step_col, folder)
    if proc_col and "step" in ev.columns:
        out["process"] = process_scores(ev, proc_col, target_process)
        if step_col:                      # the same step scores on the target process only (other work has no step)
            tgt = out["process"].get("target_process")
            sub = ev[ev[proc_col] == tgt] if tgt is not None else ev.iloc[0:0]
            if len(sub):
                out["steps_target"] = {"target_process": tgt, **step_scores(sub, step_col)}
    (folder / EVAL_FILE).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def table(e: dict) -> str:
    """A short plain-text table of the scores."""
    lines = []
    for name, s in (e.get("episodes") or {}).items():
        lines.append(f"  episodes {name.ljust(16)} " + (f"purity {s['purity']:.3f}, {s['fragmentation_median']:g} "
                     f"episodes per true case" if s.get("applies") and "purity" in s else f"– ({s.get('reason')})"))
    c = e.get("case_link")
    if c:
        lines.append(f"  case link              precision {c.get('precision')}, recall {c.get('recall')} "
                     f"({c.get('linked_events_with_truth', 0):,} linked events with truth)")
    s = e.get("steps")
    if s:
        lines.append(f"  steps                  accuracy {s['accuracy_events']:.3f} by events, "
                     f"{s['accuracy_hours'] if s['accuracy_hours'] is not None else '–'} by hours; "
                     f"{100 * s['unassigned_share']:.1f}% unassigned")
    p = e.get("process")
    if p:
        lines.append(f"  process separation     {100 * p['other_work_given_a_step_share']:.1f}% of other work "
                     f"(not {p['target_process']}) was given a step")
    return "\n".join(lines) or "  no truth columns in this job"
