"""Calibrate the episode auto-pick weights (nocase.episodes.score_weights) once, on the bench datasets with truth.

At run time the episode strategy is still picked from the data only (app/nocase/strategies/episodes.data_score);
truth is used HERE only, to choose the weights that make that data-only pick agree best with the purest strategy.

    python scripts/calibrate_episodes.py [--root test_datasets] [--step 0.05]

For every present dataset with a true case / batch column: the data-score components of every strategy that
applies and its purity against the truth. Then a grid of weights (single, length, concentration; summing to 1)
is searched: the smallest worst-case pick regret (best purity minus the chosen one), then the smallest mean
regret, then the weights closest to the current ones. Prints the result and the YAML line to paste.
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def components(root: Path) -> dict[str, dict[str, dict]]:
    """{dataset: {strategy: {single_event_share, median_minutes, unit_concentration, purity}}}."""
    from app.core import jobs
    from app.core.db import connect
    from app.nocase import profile
    from app.nocase.bench import attach_truth_file, load_specs, prepare_input, presence
    from app.nocase.canonical import NcMapping
    from app.nocase.eval.evaluate import _pick, episode_scores, truth_frame
    from app.nocase.pipeline import run
    from app.core.config import settings
    from app.nocase.strategies.base import REGISTRY, Context
    from app.nocase.strategies.episodes import data_score
    out = {}
    for spec, folder in load_specs(root):
        if not presence(spec, folder)[0]:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DATA_DIR"] = str(Path(tmp) / "data")
            src, _ = prepare_input(spec, folder, Path(tmp) / "work", None)
            r = run(src, NcMapping.from_dict({**spec["mapping"], "dataset_class": spec.get("dataset_class")}))
            f = jobs.job_folder(r["job"]["job_id"])
            attach_truth_file(spec, folder, f)
            t = truth_frame(f)
            col = _pick(t, settings()["nocase"]["eval"]["case_truth"])
            if not col:
                continue
            ctx = Context(f, profile.load(f) or {})
            con = connect()
            rows = {}
            for name, cls in REGISTRY["episodes"].items():
                s = cls()
                if not s.applies(ctx.caps, ctx)[0]:
                    continue
                ev = s.compute(con, ctx)
                ds = data_score(ev)
                pur = episode_scores(ev.merge(t[["eid", col]], on="eid"), col).get("purity")
                rows[name] = {**ds, "purity": pur}
            out[spec["name"]] = rows
    return out


def score(c: dict, w: dict, good: tuple[float, float]) -> float:
    lo, hi = good
    med = c["median_minutes"]
    length = 1.0 if lo <= med <= hi else (med / lo if med < lo else hi / med)
    return w["single"] * (1 - c["single_event_share"]) + w["length"] * length + w["concentration"] * c["unit_concentration"]


def search(data: dict, current: dict, good, step: float) -> tuple[dict, dict]:
    best = None
    n = round(1 / step)
    for a, b in itertools.product(range(n + 1), repeat=2):
        if a + b > n:
            continue
        w = {"single": a / n, "length": b / n, "concentration": (n - a - b) / n}
        regrets = {}
        for ds, rows in data.items():
            chosen = max(rows, key=lambda k: score(rows[k], w, good))
            regrets[ds] = max(r["purity"] for r in rows.values()) - rows[chosen]["purity"]
        key = (round(max(regrets.values()), 6), round(sum(regrets.values()) / len(regrets), 6),
               round(sum(abs(w[k] - current[k]) for k in w), 6))
        if best is None or key < best[0]:
            best = (key, w, regrets)
    return best[1], best[2]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=str(ROOT / "test_datasets"))
    p.add_argument("--step", type=float, default=0.05)
    args = p.parse_args(argv)
    from app.core.config import settings
    data = components(Path(args.root))
    if not data:
        print("No dataset with a true case / batch column is present.")
        return 0
    c = settings()["nocase"]["episodes"]
    w, regrets = search(data, c["score_weights"], tuple(c["good_minutes"]), args.step)
    for ds, rows in data.items():
        print(ds + ": " + ", ".join(f"{k} purity {v['purity']:.3f}" for k, v in rows.items()))
    print("Calibrated weights:", w, "· pick regret per dataset:", {k: round(v, 4) for k, v in regrets.items()})
    print(f"score_weights: {{single: {w['single']:g}, length: {w['length']:g}, concentration: {w['concentration']:g}}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
