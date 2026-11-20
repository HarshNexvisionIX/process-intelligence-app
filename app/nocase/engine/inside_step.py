"""Inside each step: how the work is actually done -> nc_inside_step.json. Adds only; changes no other number.

Works on step runs (measures.py: consecutive events of one step by one actor within one episode), per step:
    sequence     the order of Level 4 activities inside each run (repeats merged); "B directly after A" shares
    patterns     the top nocase.inside.top_patterns sequences with their share of runs (each needs min_runs runs
                 and min_actors people), the rest "Other"
    start / end  the most common first and last activity; the end checked against completion verbs
    time split   share of the step's hours per activity, median time per activity per run
    work type    entry / lookup / review / communication / navigation from the words of the activity's label,
                 clicks and screens (word lists: settings inside_step.work_types); hours per type
    rework       the same activity coming back in a run after another one AND after a pause >= rework_gap_minutes
                 (as in the case-ID mode); quicker returns are back-and-forth, reported apart
    apps         apps per run, switches per run
    variation    actors' median run time (masked): coefficient of variation, slow / fast people, and actors whose
                 usual pattern others hardly use; flagged when CV > cv_flag or that share < pattern_others_min
    automation   high-volume, repetitive (one pattern in >= automation_pattern_share of its runs) entry activities:
                 facts for SME review, never recommendations
Every sequence and pattern has status inferred. Totals check: activity hours = step hours, pattern runs = runs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from app.core.config import settings
from app.nocase.engine.measures import UNASSIGNED, events_frame, runs_frame

INSIDE_FILE = "nc_inside_step.json"
OTHER = "Other"


def _cfg() -> dict:
    return settings()["nocase"]["inside"]


def work_type(texts: list[str]) -> str | None:
    """The work type whose words occur most in the texts (settings inside_step.work_types)."""
    types = settings()["inside_step"]["work_types"]
    counts = {t: 0 for t in types}
    for text in texts:
        words = set(re.findall(r"[a-z]+", str(text).lower()))
        for t, ws in types.items():
            counts[t] += len(words & {str(w).lower() for w in ws})
    best = max(counts, key=lambda t: (counts[t], -list(types).index(t)))
    return best if counts[best] else None


def _merge(seq: list[str]) -> list[str]:
    return [a for i, a in enumerate(seq) if i == 0 or a != seq[i - 1]]


def compact(seq: tuple[str, ...] | list[str], max_items: int = 5) -> tuple[str, ...]:
    """A run's pattern for grouping and display: activities in order of first appearance. When an activity comes
    back (A -> B -> A -> B), the items are joined with "⇄" instead of "→" by pattern_text(); at most max_items
    items, then "…"."""
    seen: list[str] = []
    for a in seq:
        if a not in seen:
            seen.append(a)
    back = len(seen) < len(list(seq))
    items = seen[:max_items] + (["…"] if len(seen) > max_items else [])
    return tuple(items) + (("⇄",) if back else ())


def pattern_text(p: tuple[str, ...]) -> str:
    if p and p[-1] == "⇄":
        return " ⇄ ".join(p[:-1])
    return " → ".join(p)


def step_detail(step: str, ev: pd.DataFrame, runs: pd.DataFrame, step_hours: float) -> dict:
    c = _cfg()
    d = ev[ev["step_label"] == step].sort_values(["run_no", "ts", "eid"])
    r = runs[runs["step"] == step]
    n_runs = int(len(r))
    base = {"step": step, "runs": n_runs, "status": "inferred"}
    if n_runs < int(c["min_runs"]):
        return {**base, "enough_runs": False,
                "message": f"not enough runs to describe how this step is done ({n_runs} run(s), at least "
                           f"{c['min_runs']} needed)"}
    d = d.assign(activity=d["activity"].fillna(OTHER))
    seqs = d.groupby("run_no")["activity"].agg(lambda s: tuple(_merge(list(s))))
    keys = seqs.map(lambda s: compact(s, int(c.get("pattern_max_items", 5))))
    actor_of = r["actor_mask"]
    pat = pd.DataFrame({"pattern": keys, "actor": actor_of.reindex(seqs.index)})
    counts = pat.groupby("pattern").agg(runs=("actor", "size"), actors=("actor", "nunique")).sort_values(
        ["runs"], ascending=False)
    ok = counts[(counts["runs"] >= int(c["min_runs"])) & (counts["actors"] >= int(c["min_actors"]))]
    top = ok.head(int(c["top_patterns"]))
    patterns = [{"pattern": pattern_text(p), "runs": int(x.runs), "share": round(float(x.runs) / n_runs, 6),
                 "actors": int(x.actors), "status": "inferred"} for p, x in top.iterrows()]
    rest = n_runs - int(top["runs"].sum())
    if rest:
        patterns.append({"pattern": OTHER, "runs": int(rest), "share": round(rest / n_runs, 6), "actors": None,
                         "status": "inferred"})
    # transitions inside runs
    pairs: dict[tuple[str, str], int] = {}
    for s in seqs:
        for a, b in set(zip(s, s[1:])):
            pairs[(a, b)] = pairs.get((a, b), 0) + 1
    transitions = sorted([{"from": a, "to": b, "runs": n, "share": round(n / n_runs, 6)} for (a, b), n in pairs.items()],
                         key=lambda x: (-x["runs"], x["from"], x["to"]))
    starts = seqs.map(lambda s: s[0]).value_counts()
    ends = seqs.map(lambda s: s[-1]).value_counts()
    verbs = {str(v).lower() for v in settings()["inside_step"]["completion_verbs"]}
    end_act = ends.index[0]
    end_words = [str(x).lower().split()[0] for x in [end_act] + list(d.loc[d["activity"] == end_act, "action_clean"]
                                                                       .dropna().unique()) if str(x).split()]
    end_is_completion = any(w in verbs for w in end_words)
    # time split and work type
    sec = d.groupby("activity")["sec"].sum()
    per_run = d.groupby(["activity", "run_no"])["sec"].sum().groupby(level=0).median()
    total = float(sec.sum())
    types, time_split = {}, []
    for a in sec.sort_values(ascending=False).index:
        rows = d[d["activity"] == a]
        wt = work_type([a] + list(rows["action_clean"].dropna().astype(str).unique()[:50])
                       + list(rows["screen_clean"].dropna().astype(str).unique()[:20])) or "other"
        types[wt] = types.get(wt, 0.0) + float(sec[a])
        time_split.append({"activity": a, "hours": round(float(sec[a]) / 3600, 6),
                           "share": round(float(sec[a]) / total, 6) if total else 0.0,
                           "median_per_run_s": round(float(per_run[a]), 3), "work_type": wt})
    # rework (as in the case-ID mode): an activity coming back in the run after another one AND after a pause of
    # at least rework_gap_minutes; a return within the gap is back-and-forth (normal switching), counted apart
    gap = float(c["rework_gap_minutes"]) * 60
    rework_runs = back_runs = back_switches = 0
    for _, g in d.groupby("run_no"):
        last: dict[str, pd.Timestamp] = {}
        prev = None
        hit = back = False
        for a, t in zip(g["activity"], g["ts"]):
            if a != prev and a in last:
                if (t - last[a]).total_seconds() >= gap:
                    hit = True
                else:
                    back = True
                    back_switches += 1
            last[a] = t
            prev = a
        rework_runs += hit
        back_runs += back
    # apps
    apps_per_run = d.groupby("run_no")["app"].nunique()
    # variation between people (masked)
    med = r.groupby("actor_mask")["sec"].median()
    overall_med = float(r["sec"].median())
    cv = float(med.std(ddof=0) / med.mean()) if len(med) > 1 and med.mean() else 0.0
    usual = pat.groupby("actor")["pattern"].agg(lambda s: s.value_counts().index[0])
    odd = []
    for actor, p in usual.items():
        others = pat[pat["actor"] != actor]
        if len(others) and (others["pattern"] == p).mean() < float(c["pattern_others_min"]):
            odd.append(actor)
    reasons = []
    if cv > float(c["cv_flag"]):
        slow = med.idxmax()
        reasons.append(f"{slow} takes {med.max() / overall_med:.1f}× the median run time" if overall_med else
                       f"run times vary between people (CV {cv:.2f})")
    for a in odd:
        reasons.append(f"{a} usually works in a pattern the others rarely use")
    # automation candidates (facts)
    auto = []
    entry = {x["activity"] for x in time_split if x["work_type"] == "entry"}
    for a in entry:
        runs_with = [s for s in seqs if a in s]
        if not runs_with:
            continue
        share = pd.Series(runs_with).value_counts().iloc[0] / len(runs_with)
        events = int((d["activity"] == a).sum())
        if events >= int(c["automation_min_events"]) and share >= float(c["automation_pattern_share"]):
            auto.append({"activity": a, "events": events, "pattern_share": round(float(share), 6)})
    out = {**base, "enough_runs": True, "patterns": patterns, "top_pattern_share": patterns[0]["share"] if patterns
           and patterns[0]["pattern"] != OTHER else 0.0, "transitions": transitions[:int(c["max_transitions"])],
           "start_activity": starts.index[0], "end_activity": end_act, "end_is_completion": end_is_completion,
           "time_split": time_split, "work_type_share": {k: round(v / total, 6) if total else 0.0 for k, v in types.items()},
           "rework_runs": int(rework_runs), "rework_share": round(rework_runs / n_runs, 6),
           "rework_rule": f"same activity again after another one and a pause of at least {c['rework_gap_minutes']} min",
           "back_and_forth_runs": int(back_runs), "back_and_forth_share": round(back_runs / n_runs, 6),
           "back_and_forth_per_run": round(back_switches / n_runs, 6),
           "median_apps_per_run": float(apps_per_run.median()),
           "switches_per_run": round(float(r["switches"].mean()), 6),
           "variation": {"cv": round(cv, 6), "flagged": bool(reasons), "reasons": reasons,
                         "median_run_s_by_actor": {k: round(float(v), 3) for k, v in med.items()}},
           "automation_candidates": sorted(auto, key=lambda x: -x["events"]),
           "checks": {"activity_hours_equal_step_hours": abs(total / 3600 - step_hours) <= float(c["hours_tolerance"]),
                      "pattern_runs_equal_runs": sum(p["runs"] for p in patterns) == n_runs}}
    out["dot"] = activity_dot(out)
    return out


def activity_dot(st: dict) -> str:
    from app.nocase.engine.flow import _q
    lines = ['digraph G {', 'rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#f2f7ee", '
             'fontname="Helvetica"]; edge [fontname="Helvetica", fontsize=10];']
    for t in st["time_split"]:
        label = t["activity"] + "\n" + f"{100 * t['share']:.0f}% of hours"
        lines.append(f'{_q(t["activity"])} [label={_q(label)}];')
    for x in st["transitions"]:
        share = f"{100 * x['share']:.0f}%"
        lines.append(f'{_q(x["from"])} -> {_q(x["to"])} [label={_q(share)}];')
    lines.append("}")
    return "\n".join(lines)


def build(folder: Path, process: str | None = None) -> dict:
    from app.nocase.engine.measures import load as load_measures
    ev, _basis = events_frame(folder)
    runs = runs_frame(ev)
    m = load_measures(folder)
    hours = {s["step"]: s["hours"] for s in m.get("steps") or []}
    steps = [step_detail(s, ev, runs, hours.get(s, 0.0)) for s in (m.get("step_order") or []) if s != UNASSIGNED]
    reviewed = _reviews(process)
    for s in steps:                       # the SME's last word on a step's usual pattern (review page, N5)
        r = reviewed.get(s["step"])
        if r and s.get("patterns") and s["patterns"][0]["pattern"] == r["pattern"]:
            s["patterns"][0]["status"] = r["status"]
    checks = {"passed": all(all(s["checks"].values()) for s in steps if s.get("enough_runs")),
              "failed_steps": [s["step"] for s in steps if s.get("enough_runs") and not all(s["checks"].values())]}
    out = {"steps": steps, "checks": checks}
    (folder / INSIDE_FILE).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def _reviews(process: str | None) -> dict[str, dict]:
    """step -> {pattern, status} from the library's decisions.jsonl ("inside:<step>" items), the latest wins."""
    if not process:
        return {}
    try:
        from app.nocase.knowledge import library as klib
        rows = klib._read_decisions(klib.process_dir(process))
    except Exception:  # noqa: BLE001 - no library or no decisions yet
        return {}
    out = {}
    for r in rows:
        item = str(r.get("item") or "")
        if item.startswith("inside:") and isinstance(r.get("after"), dict):
            out[item.split(":", 1)[1]] = {"pattern": r["after"].get("pattern"), "status": r["after"].get("status")}
    return out


def load(folder: Path) -> dict:
    p = folder / INSIDE_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def summary_lines(inside: dict) -> list[str]:
    """One plain line per step (the bench prints these)."""
    out = []
    for s in inside.get("steps") or []:
        if not s.get("enough_runs"):
            out.append(f"{s['step']}: {s['message']}")
            continue
        p = s["patterns"][0] if s["patterns"] else {"pattern": OTHER, "share": 0}
        usual = ("no repeated pattern" if p["pattern"] == OTHER
                 else f"usual pattern {p['pattern']} ({100 * p['share']:.0f}%)")
        out.append(f"{s['step']}: {s['runs']} runs; {usual}; "
                   f"rework {100 * s['rework_share']:.0f}%; {'varies between people' if s['variation']['flagged'] else 'similar between people'}")
    return out

