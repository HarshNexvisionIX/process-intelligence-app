"""Measures of the no-case mode -> nc_measures.json. Every number comes from nc_events_clean (the raw rows in scope,
one per event), never from merged rows; each measure says its basis.

    time        active_s when the export measures it (profile: active time measured), else end - start when most events
                have an end time ("start-end"), else estimated per event as
                the gap to the actor's next event, capped at nocase.measures.estimate_cap_seconds ("estimated")
    step run    consecutive events of one step by one actor within one episode
    repetition  a run of a step that the same actor already did earlier in the same episode (after another step)
    switch      an actor's two consecutive events in different applications at most switch_gap_seconds apart
Per step, Level 4 activity, application, actor (masked P01, P02 ...), stage and overall; time-of-day / weekday /
day-of-month distributions with the month-end share; the step x application and activity x application hours
matrices with totals; unassigned and generic effort apart; case measures only in case or partial mode.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.config import settings
from app.core.db import connect, sql_literal

MEASURES_FILE = "nc_measures.json"
UNASSIGNED = "Unassigned"
NO_APP = "(no app)"


def _cfg() -> dict:
    return settings()["nocase"]["measures"]


def events_frame(folder: Path) -> tuple[pd.DataFrame, str]:
    """The events with sec (seconds of work), step label, masked actor; and the time basis."""
    from app.nocase import profile
    from app.nocase.clean import CLEAN_FILE
    con = connect()
    path = (folder / CLEAN_FILE).as_posix()
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({sql_literal(path)})").fetchall()]
    want = [c for c in ("eid", "ts", "ts_end", "actor", "app", "step", "stage", "activity", "activity_l5", "active_s",
                        "is_generic", "episode_id", "step_source", "unit_id", "screen_clean", "action_clean")
            if c in cols]
    ev = con.execute(f"SELECT {', '.join(want)} FROM read_parquet({sql_literal(path)}) ORDER BY eid").df()
    for c in ("step", "stage", "activity", "episode_id", "step_source", "activity_l5"):
        if c not in ev.columns:
            ev[c] = None
    caps = profile.load(folder) or {}
    measured = (caps.get("time") or {}).get("active_time") == "measured"
    ev = ev.sort_values(["actor", "ts", "eid"], na_position="last", kind="stable").reset_index(drop=True)
    span = ((ev["ts_end"] - ev["ts"]).dt.total_seconds().clip(lower=0) if "ts_end" in ev.columns
            and ev["ts_end"].notna().mean() >= float(_cfg()["end_time_min_share"]) else None)
    if measured:
        ev["sec"] = ev["active_s"].fillna(0.0).astype(float)
    elif span is not None:            # an end time per event (window trackers): the time from start to end
        ev["sec"] = span.fillna(0.0)
        measured = "start-end"
    else:
        nxt = ev.groupby(ev["actor"].fillna(""), sort=False)["ts"].shift(-1)
        gap = (nxt - ev["ts"]).dt.total_seconds()
        ev["sec"] = gap.clip(lower=0, upper=float(_cfg()["estimate_cap_seconds"])).fillna(0.0)
    ev["step_label"] = ev["step"].fillna(UNASSIGNED)
    ev["app_label"] = ev["app"].fillna(NO_APP)
    hrs = ev.groupby(ev["actor"].fillna(""))["sec"].sum()
    order = sorted(hrs.index, key=lambda a: (-hrs[a], a))
    width = max(2, len(str(len(order))))
    mask = {a: f"P{i + 1:0{width}d}" for i, a in enumerate(order)}
    ev["actor_mask"] = ev["actor"].fillna("").map(mask)
    return ev, ("start-end" if measured == "start-end" else "measured" if measured else "estimated")


def runs_frame(ev: pd.DataFrame) -> pd.DataFrame:
    """Step runs: actor, episode, step, events, sec, apps, switches inside, is_repeat; adds run_no to ev."""
    o = ev.sort_values(["actor", "episode_id", "ts", "eid"], na_position="last", kind="stable")
    key = o["actor"].fillna("") + "|" + o["episode_id"].fillna("")
    new = (key != key.shift()) | (o["step_label"] != o["step_label"].shift())
    ev["run_no"] = new.cumsum().reindex(ev.index)          # aligned on the index: ev keeps its own row order
    ev["switch"] = switches_mask(ev)
    g = ev.groupby("run_no", sort=True)
    runs = pd.DataFrame({"actor": g["actor"].first(), "actor_mask": g["actor_mask"].first(),
                         "episode_id": g["episode_id"].first(), "step": g["step_label"].first(),
                         "start": g["ts"].min(), "events": g.size(), "sec": g["sec"].sum(),
                         "apps": g["app"].nunique(), "switches": g["switch"].sum()})
    runs["seq"] = runs.groupby(["actor", "episode_id", "step"], dropna=False).cumcount()
    runs["is_repeat"] = runs["seq"] > 0
    return runs


def switches_mask(ev: pd.DataFrame) -> pd.Series:
    """True for an event that switches application from the same actor's previous event (within the gap)."""
    o = ev.sort_values(["actor", "ts", "eid"], na_position="last", kind="stable")
    same = o["actor"].eq(o["actor"].shift()) & o["actor"].notna()
    gap = (o["ts"] - o["ts"].shift()).dt.total_seconds()
    sw = same & o["app"].notna() & o["app"].shift().notna() & (o["app"] != o["app"].shift()) \
        & (gap <= float(_cfg()["switch_gap_seconds"]))
    return sw.reindex(ev.index).fillna(False)


def _group(ev: pd.DataFrame, by: list[str], total_sec: float) -> list[dict]:
    g = ev.groupby(by, dropna=False, sort=False)
    out = []
    for key, d in g:
        key = key if isinstance(key, tuple) else (key,)
        h = d["sec"].sum() / 3600
        out.append({**dict(zip(by, key)), "events": int(len(d)), "actors": int(d["actor"].nunique()),
                    "hours": round(float(h), 6), "share_of_hours": round(float(d["sec"].sum() / total_sec), 6)
                    if total_sec else 0.0})
    return out


def _matrix(ev: pd.DataFrame, rows: list[str], row_key: str) -> dict:
    t = ev.pivot_table(index=row_key, columns="app_label", values="sec", aggfunc="sum", fill_value=0.0) / 3600
    t = t.reindex([r for r in rows if r in t.index])
    apps = list(t.sum().sort_values(ascending=False).index)
    t = t[apps]
    return {"rows": list(t.index), "columns": apps,
            "cells": [[round(float(v), 6) for v in r] for r in t.values],
            "row_totals": [round(float(v), 6) for v in t.sum(axis=1)],
            "column_totals": [round(float(v), 6) for v in t.sum(axis=0)],
            "grand_total": round(float(t.values.sum()), 6)}


def _dist(values: pd.Series, sec: pd.Series, index: list) -> list[dict]:
    s = sec.groupby(values).sum()
    tot = float(sec.sum()) or 1.0
    return [{"value": int(i), "hours": round(float(s.get(i, 0.0)) / 3600, 6), "share": round(float(s.get(i, 0.0)) / tot, 6)}
            for i in index]


def step_order(ev: pd.DataFrame, process: str | None) -> list[str]:
    from app.nocase import library
    pack_order = library.step_order(library.load_pack(process)) if process else []
    present = list(ev["step_label"].unique())
    rest = sorted([s for s in present if s not in pack_order and s != UNASSIGNED],
                  key=lambda s: -ev.loc[ev["step_label"] == s, "sec"].sum())
    return [s for s in pack_order if s in present] + rest + ([UNASSIGNED] if UNASSIGNED in present else [])


def build(folder: Path, process: str | None = None) -> dict:
    ev, basis = events_frame(folder)
    runs = runs_frame(ev)
    total_sec = float(ev["sec"].sum())
    order = step_order(ev, process)
    c = _cfg()
    # per step
    steps = []
    for s in order:
        d = ev[ev["step_label"] == s]
        r = runs[runs["step"] == s]
        src = d.groupby(d["step_source"].fillna("unassigned"))["sec"].sum() / 3600
        apps = d.groupby("app_label")["sec"].sum().sort_values(ascending=False)
        steps.append({"step": s, "stage": d["stage"].dropna().iloc[0] if d["stage"].notna().any() else None,
                      "events": int(len(d)), "actors": int(d["actor"].nunique()),
                      "hours": round(float(d["sec"].sum() / 3600), 6),
                      "share_of_hours": round(float(d["sec"].sum() / total_sec), 6) if total_sec else 0.0,
                      "runs": int(len(r)), "median_run_s": None if r.empty else round(float(r["sec"].median()), 3),
                      "repetitions": int(r["is_repeat"].sum()),
                      "repetition_share": round(float(r["is_repeat"].mean()), 6) if len(r) else 0.0,
                      "switches": int(r["switches"].sum()),
                      "switches_per_run": round(float(r["switches"].mean()), 6) if len(r) else 0.0,
                      "top_apps": [{"app": a, "hours": round(float(h / 3600), 6)} for a, h in apps.head(2).items()],
                      "hours_by_source": {k: round(float(v), 6) for k, v in src.items()}})
    acts = _group(ev[ev["step"].notna()], ["step_label", "activity"], total_sec)
    for a in acts:
        a["step"] = a.pop("step_label")
    apps_ = sorted(_group(ev, ["app_label"], total_sec), key=lambda x: (-x["hours"], x["app_label"]))
    actors = sorted(_group(ev, ["actor_mask"], total_sec), key=lambda x: x["actor_mask"])
    stages = _group(ev[ev["stage"].notna()], ["stage"], total_sec)
    month_end_days = int(c["month_end_days"])
    dim = ev["ts"].dt.days_in_month
    month_end = ev["ts"].dt.day > (dim - month_end_days)
    out = {
        "basis": {"time": basis, "events": "one row per event in scope (nc_events_clean)",
                  "step_runs": "consecutive events of one step by one actor within one episode",
                  "switches": f"consecutive events of one actor in different apps within {c['switch_gap_seconds']} s",
                  "estimate_cap_seconds": c["estimate_cap_seconds"] if basis == "estimated" else None},
        "overall": {"events": int(len(ev)), "actors": int(ev["actor"].nunique()), "apps": int(ev["app"].nunique()),
                    "hours": round(total_sec / 3600, 6), "episodes": int(ev["episode_id"].nunique()),
                    "step_runs": int(len(runs)), "repetitions": int(runs["is_repeat"].sum()),
                    "switches": int(ev["switch"].sum()),
                    "unassigned_hours": round(float(ev.loc[ev["step"].isna(), "sec"].sum() / 3600), 6),
                    "unassigned_share": round(float(ev.loc[ev["step"].isna(), "sec"].sum() / total_sec), 6) if total_sec else 0.0,
                    "generic_hours": round(float(ev.loc[ev["is_generic"].fillna(False).astype(bool), "sec"].sum() / 3600), 6)
                    if "is_generic" in ev.columns else None,
                    "month_end_share": round(float(ev.loc[month_end, "sec"].sum() / total_sec), 6) if total_sec else 0.0},
        "steps": steps, "activities": acts, "apps": apps_, "actors": actors, "stages": stages,
        "time_of_day": _dist(ev["ts"].dt.hour, ev["sec"], list(range(24))),
        "day_of_week": _dist(ev["ts"].dt.day_of_week, ev["sec"], list(range(7))),
        "day_of_month": _dist(ev["ts"].dt.day, ev["sec"], list(range(1, 32))),
        "switching": {"per_actor": sorted([{"actor_mask": a, "switches": int(n)} for a, n in
                                           ev.groupby("actor_mask")["switch"].sum().items()], key=lambda x: x["actor_mask"])},
        "matrix_step_app": _matrix(ev, order, "step_label"),
        "matrix_activity_app": _matrix(ev[ev["step"].notna()].assign(
            act_key=lambda d: d["step_label"] + " | " + d["activity"].fillna("Other")),
            sorted((ev[ev["step"].notna()]["step_label"] + " | " + ev[ev["step"].notna()]["activity"].fillna("Other")).unique(),
                   key=lambda k: (order.index(k.split(" | ")[0]), k)), "act_key"),
        "step_order": order,
    }
    out["case"] = case_measures(folder, ev)
    (folder / MEASURES_FILE).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def case_measures(folder: Path, ev: pd.DataFrame) -> dict | None:
    """Case or partial mode only: per linked case its duration and step-run path (the case-ID rules)."""
    from app.nocase.strategies.base import load_decisions
    from app.nocase.strategies.case_link import LINKS_FILE
    d = load_decisions(folder).get("case_link") or {}
    if d.get("decision") not in ("case", "partial") or not (folder / LINKS_FILE).exists():
        return None
    links = pd.read_parquet(folder / LINKS_FILE)
    links = links[links["used"].astype(bool)]
    j = ev.merge(links[["eid", "case_id"]], on="eid")
    if j.empty:
        return None
    j = j.sort_values(["case_id", "ts", "eid"])
    dur = j.groupby("case_id")["ts"].agg(lambda t: (t.max() - t.min()).total_seconds() / 3600)
    path = j.groupby("case_id")["step_label"].agg(lambda s: tuple(s[s.ne(s.shift())]))
    return {"mode": d.get("decision"), "cases": int(len(dur)), "median_cycle_h": round(float(dur.median()), 6),
            "p90_cycle_h": round(float(np.percentile(dur, int(_cfg()["cycle_percentile"]))), 6),
            "variants": int(path.nunique())}


def load(folder: Path) -> dict:
    p = folder / MEASURES_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
