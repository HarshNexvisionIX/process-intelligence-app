"""calibrate: on a dataset with truth, set each evidence signal's weight to its measured accuracy against the true
step -> <job>/nc_evidence_weights.yaml (and the process library's copy). Evaluation only: the weights are then
used by app/nocase/engine/confidence.py on every later dataset; truth is never read at run time.

    screen / action / app_transition / time_pattern / completion_verb   events-weighted share of events whose
        value's majority true step is their true step (how well the signal alone tells the step)
    source_label                                                         l3 = true step on labelled events
    episode_sequence / co_occurrence                                     the next event in the episode has the
        same true step
    library_rule / rule-based and AI assignments                         assigned step = true step
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

from app.core.config import settings
from app.core.db import connect, sql_literal

KEYS = {"screen": "screen_clean", "action": "action_clean", "app_transition": "app"}


def _purity(df: pd.DataFrame, key: str, truth: str) -> float | None:
    d = df.dropna(subset=[key, truth])
    if d.empty:
        return None
    c = d.groupby([key, truth]).size()
    return float(c.groupby(level=0).max().sum() / len(d))


def calibrate(folder: Path, process: str | None = None) -> dict:
    from app.nocase.clean import CLEAN_FILE
    from app.nocase.engine.confidence import WEIGHTS_FILE
    from app.nocase.eval.evaluate import _pick, truth_frame
    truth = truth_frame(folder)
    col = _pick(truth, settings()["nocase"]["eval"]["step_truth"])
    if not col:
        raise ValueError("This job has no true step column (eval) to calibrate on.")
    ev = connect().execute(f"SELECT * FROM read_parquet({sql_literal((folder / CLEAN_FILE).as_posix())})").df()
    ev = ev.merge(truth[["eid", col]].rename(columns={col: "__truth"}), on="eid", how="inner")
    ev["hour"] = ev["ts"].dt.hour
    w: dict[str, float] = {}
    for sig, key in {**KEYS, "time_pattern": "hour"}.items():
        v = _purity(ev, key, "__truth")
        if v is not None:
            w[sig] = round(v, 4)
    if "l3" in ev.columns and ev["l3"].notna().any():
        d = ev.dropna(subset=["l3"])
        w["source_label"] = round(float((d["l3"].str.lower() == d["__truth"].astype(str).str.lower()).mean()), 4)
    if "episode_id" in ev.columns:
        d = ev.sort_values(["episode_id", "ts", "eid"])
        same = d["episode_id"].eq(d["episode_id"].shift())
        val = float((d["__truth"].eq(d["__truth"].shift()) & same).sum() / max(int(same.sum()), 1))
        w["episode_sequence"] = w["co_occurrence"] = round(val, 4)
    if "step_source" in ev.columns:
        d = ev[ev["step_source"] == "rule"]
        if len(d):
            w["library_rule"] = round(float((d["step"].str.lower() == d["__truth"].astype(str).str.lower()).mean()), 4)
    doc = {"weights": w, "truth_column": col, "events": int(len(ev)),
           "note": "weight = measured accuracy of the signal against the true step (calibrate)"}
    text = yaml.safe_dump(doc, sort_keys=True)
    (folder / WEIGHTS_FILE).write_text(text, encoding="utf-8")
    if process:
        from app.nocase import library
        (library.pack_dir(process) / WEIGHTS_FILE).write_text(text, encoding="utf-8")
    return doc
