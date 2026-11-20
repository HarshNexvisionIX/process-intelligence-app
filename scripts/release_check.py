"""Reproducible offline release checks; all generated inputs/results live outside data/.

Run with --work pointing to an empty temporary directory. --worker is internal.
Never starts a Streamlit server or contacts an AI service.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def write(path, value):
    path.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")


def read(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def isolated(work):
    os.environ.update(DATA_DIR=str(work / "scratch"), AI_ENABLED="false", ALLOW_EXTERNAL_AI="false",
                      ALLOW_EXTERNAL_LANGFUSE="false", LLM_BASE_URL="", LLM_MODEL="", LLM_API_KEY="",
                      LANGFUSE_PUBLIC_KEY="", LANGFUSE_SECRET_KEY="", LANGFUSE_BASE_URL="",
                      PYTHONIOENCODING="utf-8")


def peak_memory():
    if sys.platform == "win32":
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage")]
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        ctypes.windll.psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
        return round(counters.PeakWorkingSetSize / 1024**2, 1)
    return None


def worker(work, scenario):
    from app.core import ingest, quality, mining, report
    from scripts.run_pipeline import main
    timings = {}
    for module, name in ((ingest, "normalise"), (quality, "run_quality"), (mining, "run_mining"), (report, "generate_report")):
        original = getattr(module, name)
        def timed(*args, _fn=original, _name=name, **kwargs):
            t = time.perf_counter()
            try:
                return _fn(*args, **kwargs)
            finally:
                timings[_name] = round(time.perf_counter() - t, 3)
        setattr(module, name, timed)
    t = time.perf_counter()
    code = main(scenario["args"])
    data = Path(scenario["data_dir"])
    metas = sorted((data / "jobs").glob("*/job.json"))
    job = read(metas[-1], {}) if metas else {}
    folder = metas[-1].parent if metas else data
    ref = read(folder / "reference.json", {})
    result = {"name": scenario["name"], "exit": code, "seconds": round(time.perf_counter() - t, 3),
              "peak_rss_mb": peak_memory(), "timings": timings, "job": job,
              "flow": ref.get("flow"), "order": ref.get("order"),
              "strength": ref.get("stats", {}).get("order_strength"),
              "facts": len(read(folder / "facts.json", [])), "folder": str(folder),
              "html": str(folder / "report.html") if (folder / "report.html").exists() else None,
              "excel": str(folder / "report.xlsx") if (folder / "report.xlsx").exists() else None}
    write(work / (scenario["name"] + ".result.json"), result)
    return code


def prepare(work):
    import numpy as np
    import pandas as pd
    from scripts.generate_synthetic import main as generate, MASKED_APPS, app_hash, VARIANTS
    sources = work / "inputs"
    sources.mkdir(exist_ok=True)
    scenarios = []
    def add(name, source, options=()):
        data = work / name
        scenarios.append({"name": name, "data_dir": str(data), "args": ["--input", str(source),
            "--auto-map", "--no-ai", "--data-dir", str(data), *options]})
    def gen(name, cases=3000, flags=()):
        src = sources / (name + ".parquet")
        generate(["--cases", str(cases), "--start", "2024-01-01", "--months", "12", "--out", str(src), *flags])
        return src
    clean = gen("clean", flags=["--apps", "--stages"])
    appmap = sources / "app-map.csv"
    pd.DataFrame([{"raw_value": app_hash(a), "real_name": a} for a in sorted(MASKED_APPS)]).to_csv(appmap, index=False)
    acts = VARIANTS[0][1]
    order = {"process_name": "Purchasing", "stages": [{"name": "Process", "steps": acts}],
             "source": "edited", "confirmed_by_business": True}
    write(sources / "order.json", order)
    definition = {"name": "Purchasing", "stages": [{"id": "ST1", "name": "Process"}], "steps": [
        {"id": f"S{i}", "name": a, "stage_id": "ST1", "position": i, "mapped_activities": [a]}
        for i, a in enumerate(acts, 1)]}
    write(sources / "definition.json", definition)
    add("clean", clean, ["--stage", "phase", "--application", "Application_name", "--window-title", "simplified_title",
                         "--merge-repeats", "--app-map", str(appmap)])
    add("definition", clean, ["--stage", "phase", "--definition", str(sources / "definition.json"),
                               "--export-bpmn", str(work / "definition.bpmn"), "--order", str(sources / "order.json")])
    dirty = gen("dirty", flags=["--missing-ts", "0.015", "--duplicates", "0.02", "--typos", "0.02",
                                "--missing-case", "0.005"])
    df = pd.read_parquet(dirty)
    df["timestamp"] = pd.to_datetime(df.timestamp, utc=True).dt.tz_convert("Asia/Kolkata").astype(str)
    df.loc[df.timestamp == "NaT", "timestamp"] = None
    df.to_parquet(dirty, index=False)
    add("dirty", dirty)
    shuffled = gen("shuffled", flags=["--stages", "--shuffle-order"])
    add("shuffled", shuffled, ["--stage", "phase"])
    add("shuffled_confirmed", shuffled, ["--stage", "phase", "--order", str(sources / "order.json")])
    plain = gen("plain")
    folder = sources / "five_files"
    folder.mkdir(exist_ok=True)
    df = pd.read_parquet(plain)
    for i, indices in enumerate(np.array_split(np.arange(len(df)), 5)):
        part = df.iloc[indices].copy()
        if i < 2: part["region"] = "North"
        part.to_parquet(folder / f"part_{i}.parquet", index=False)
    add("folder", folder)
    add("tiny", gen("tiny", 60))
    bad = df.copy()
    bad["timestamp"] = "not a timestamp"
    bad.to_parquet(sources / "bad.parquet", index=False)
    add("bad_timestamp", sources / "bad.parquet", ["--timestamp", "timestamp"])
    add("missing_columns", plain)
    tickets = gen("tickets", flags=["--ids-in-title", "--drop-columns", "case"])
    add("derived_case", tickets, ["--activity", "activity", "--timestamp", "timestamp", "--resource", "resource",
                                 "--window-title", "window_title", "--derive-case", "text_pattern", "--pattern", "ticket"])
    sessions = pd.DataFrame([{"resource": f"user_{i % 6}", "activity": a,
        "timestamp": pd.Timestamp("2024-01-01") + pd.Timedelta(days=i // 6, hours=8, minutes=j*3)}
        for i in range(3000) for j, a in enumerate(["Open", "Check", "Approve", "Close"])])
    sessions.to_parquet(sources / "sessions.parquet", index=False)
    add("work_sessions", sources / "sessions.parquet", ["--activity", "activity", "--timestamp", "timestamp",
                                                        "--resource", "resource", "--derive-case", "work_session"])
    add("overview", sources / "sessions.parquet", ["--activity", "activity", "--timestamp", "timestamp",
                                                   "--resource", "resource", "--overview-only"])
    # v1.1: milestones, sub-processes (with and without business standards)
    actions = gen("milestones", flags=["--actions", "--shuffle-order", "--stages"])
    add("milestones", actions, ["--action", "activity_alias_name", "--stage", "phase", "--merge-repeats"])
    steps = gen("subprocess", 1500, flags=["--step-actions"])
    add("subprocess", steps, ["--action", "activity_alias_name", "--merge-repeats"])
    write(sources / "standards.json", {"Create Request": {
        "standard_path": ["Open Create Request", "Enter details", "Check data", "Click Submit"],
        "mandatory_actions": ["Click Submit"], "confirmed_by_business": True}})
    add("subprocess_standards", steps, ["--action", "activity_alias_name", "--standards",
                                        str(sources / "standards.json")])
    big = gen("big", 420000)
    df = pd.read_parquet(big)
    if len(df) < 2500000: raise ValueError("Large fixture has fewer than 2.5M rows")
    df.iloc[:2500000].to_parquet(big, index=False)
    add("large", big)
    write(work / "scenarios.json", scenarios)
    return scenarios


def tests(work, marker):
    args = [sys.executable, "-m", "pytest", "-q"] + (["-m", "slow"] if marker == "slow" else [])
    t = time.perf_counter()
    with (work / f"pytest_{marker}.log").open("w", encoding="utf-8") as stream:
        code = subprocess.call(args, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
    result = {"command": args, "exit": code, "seconds": round(time.perf_counter() - t, 2),
              "summary": (work / f"pytest_{marker}.log").read_text(encoding="utf-8")[-4000:]}
    write(work / f"pytest_{marker}.json", result)
    print(marker, code, result["seconds"], flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--worker")
    p.add_argument("--phase", choices=["prepare", "runs", "normal", "slow"], default="runs")
    p.add_argument("--only", nargs="*")
    args = p.parse_args()
    work = args.work.resolve()
    if work == ROOT or work.is_relative_to(ROOT / "data"):
        p.error("Use a temporary output directory outside the project's data directory")
    work.mkdir(parents=True, exist_ok=True)
    isolated(work)
    if args.worker:
        return worker(work, next(s for s in read(work / "scenarios.json") if s["name"] == args.worker))
    if args.phase in ("normal", "slow"):
        tests(work, args.phase)
    elif args.phase == "prepare":
        prepare(work)
    else:
        for scenario in read(work / "scenarios.json"):
            if args.only and scenario["name"] not in args.only: continue
            with (work / (scenario["name"] + ".console.log")).open("w", encoding="utf-8") as stream:
                code = subprocess.call([sys.executable, __file__, "--work", str(work), "--worker", scenario["name"]],
                                       cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
            print(scenario["name"], code, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
