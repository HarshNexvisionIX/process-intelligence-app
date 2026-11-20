"""Safety net for the case-ID mode: run the existing pipeline on fixed datasets and keep what it computes.

Every number the case-ID pipeline produces (scope, quality, mining, map, variants, metrics, checks, facts, the
step order and its flow confidence) is stored per dataset in tests/regression/baseline/<name>.json. The test
(test_case_mode_baseline.py) reruns the pipeline and compares exactly (floats to 1e-9). If it changes, the
case-ID behaviour changed.

Only volatile values are left out: job IDs, creation times, computing durations, paths.

Write (or rewrite, only after a deliberate change) the baseline:
    python tests/regression/make_baseline.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASELINE_DIR = Path(__file__).parent / "baseline"
GOLDEN = ROOT / "tests" / "golden" / "golden_events.csv"

# The job outputs that hold computed results (report.html / .xlsx are built from these).
FILES = ("scope.json", "quality.json", "mining.json", "metrics.json", "facts.json", "checks.json",
         "reference.json", "mapping.json", "narrative.json")
JOB_KEYS = ("status", "rows", "format", "summary", "feasibility", "band", "flow_band", "case_source",
            "capabilities", "warnings", "dataset_class")
VOLATILE = {"job_id", "now", "seconds", "elapsed_s", "latency", "trace_id", "source_path", "source_paths", "path",
            "html", "excel", "pdf"}

GOLDEN_ARGS = ["--case", "case_id", "--activity", "activity", "--timestamp", "timestamp",
               "--timestamp-format", "%Y-%m-%d %H:%M:%S", "--resource", "user", "--application", "application",
               "--stage", "stage", "--active-time", "active_time", "--wait-time", "wait_time",
               "--tat-time", "tat_time", "--action", "action", "--window-title", "window_title",
               "--drop-missing-case", "--drop-missing-activity", "--filter-column", "is_process",
               "--filter-values", "1"]

# AI and the optional PM4Py model off, as in the tests. No CASE_SALT and no thread pin: the results do not depend
# on the case-hash salt or on DuckDB's threads (app/core/stable.py), so each run uses a fresh random salt.
FIXED_ENV = {"PM_DISABLE_PM4PY": "1", "AI_ENABLED": "false"}

# name -> (generator arguments or None for the golden file, pipeline arguments)
DATASETS = {
    "golden": (None, GOLDEN_ARGS),
    "synthetic_rich": (["--cases", "400", "--seed", "11", "--months", "6", "--apps", "--stages", "--actions",
                        "--step-actions"], ["--auto-map", "--merge-repeats"]),
    "synthetic_dirty": (["--cases", "400", "--seed", "23", "--months", "6", "--missing-ts", "0.03", "--ties", "0.1",
                         "--duplicates", "0.02", "--typos", "0.02", "--format", "csv"],
                        ["--auto-map", "--drop-missing-case"]),
}


def _clean(obj, job_id: str, folder: str):
    """Drop volatile keys (and every *_at time stamp); replace the job ID and folder inside text."""
    if isinstance(obj, dict):
        return {k: _clean(v, job_id, folder) for k, v in sorted(obj.items())
                if k not in VOLATILE and not k.endswith("_at")}
    if isinstance(obj, list):
        return [_clean(x, job_id, folder) for x in obj]
    if isinstance(obj, str):
        return obj.replace(job_id, "<job>").replace(folder, "<folder>").replace(folder.replace("\\", "/"), "<folder>")
    return obj


def run_dataset(name: str, work: Path) -> dict:
    """Run the case-ID pipeline (AI off, PM4Py off as in the tests) on one dataset in `work`; return the snapshot.

    DATA_DIR must already point into `work` (the caller sets it). CASE_SALT is removed for the run, so the data
    folder's own random salt is used; the environment is restored afterwards."""
    saved = {k: os.environ.get(k) for k in [*FIXED_ENV, "CASE_SALT"]}
    os.environ.pop("CASE_SALT", None)
    os.environ.update(FIXED_ENV)
    try:
        return _run(name, work)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run(name: str, work: Path) -> dict:
    from scripts import generate_synthetic, run_pipeline
    from app.core import jobs

    gen, args = DATASETS[name]
    if gen is None:
        src = GOLDEN
    else:
        ext = "csv" if "csv" in gen else "parquet"
        src = work / f"{name}.{ext}"
        if not src.exists():
            assert generate_synthetic.main(gen + ["--out", str(src)]) == 0
    before = {j["job_id"] for j in jobs.list_jobs()}
    code = run_pipeline.main(["--input", str(src), "--no-ai"] + args)
    assert code == 0, f"{name}: the pipeline ended with code {code}"
    job = next(j for j in jobs.list_jobs() if j["job_id"] not in before)
    folder = jobs.job_folder(job["job_id"])
    out = {"job": {k: job.get(k) for k in JOB_KEYS if k in job}}
    for fn in FILES:
        p = folder / fn
        if p.exists():
            out[fn] = json.loads(p.read_text(encoding="utf-8"))
    return _clean(out, job["job_id"], str(folder))


def compare(old, new, path: str = "") -> list[str]:
    """Every difference, as plain lines; floats equal within 1e-9."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for k in sorted(set(old) | set(new)):
            if k not in new:
                out.append(f"{path}.{k}: missing now")
            elif k not in old:
                out.append(f"{path}.{k}: new key")
            else:
                out += compare(old[k], new[k], f"{path}.{k}")
        return out
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            return [f"{path}: {len(old)} items before, {len(new)} now"]
        out = []
        for i, (a, b) in enumerate(zip(old, new)):
            out += compare(a, b, f"{path}[{i}]")
        return out
    if isinstance(old, (int, float)) and isinstance(new, (int, float)) and not isinstance(old, bool) \
            and not isinstance(new, bool):
        return [] if abs(float(old) - float(new)) <= 1e-9 else [f"{path}: {old!r} before, {new!r} now"]
    return [] if old == new else [f"{path}: {str(old)[:120]!r} before, {str(new)[:120]!r} now"]


def main() -> int:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    for name in DATASETS:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DATA_DIR"] = str(Path(tmp) / "data")
            snap = run_dataset(name, Path(tmp))
        (BASELINE_DIR / f"{name}.json").write_text(json.dumps(snap, indent=1, sort_keys=True, default=str),
                                                  encoding="utf-8")
        print(f"baseline written: {name}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("AI_ENABLED", "false")
    sys.exit(main())
