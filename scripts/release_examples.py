"""Run README command families against disposable synthetic fixtures."""
from pathlib import Path
import subprocess
import sys
import time

from release_check import ROOT, isolated, write


def main(work):
    isolated(work)
    import pandas as pd
    from app.core.db import connect
    from app.core.ingest import open_source, dataset_overview
    outcomes = []
    destination = work / "readme"
    destination.mkdir(exist_ok=True)
    def command(label, script, args):
        cmd = [sys.executable, str(ROOT / "scripts" / script), *map(str, args)]
        t = time.perf_counter()
        with (destination / f"{label}.log").open("w", encoding="utf-8") as stream:
            code = subprocess.call(cmd, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        outcomes.append({"label": label, "exit": code, "seconds": round(time.perf_counter()-t, 2), "command": cmd})
        print(label, code, flush=True)
    command("generate_clean", "generate_synthetic.py", ["--cases", "20000", "--out", destination / "clean.parquet"])
    command("generate_dirty", "generate_synthetic.py", ["--cases", "20000", "--missing-ts", "0.03", "--ties", "0.15",
        "--duplicates", "0.02", "--typos", "0.02", "--date-only", "0.4", "--placeholder", "0.01", "--missing-case", "0.01",
        "--out", destination / "dirty.csv", "--ts-format", "%d/%m/%Y %H:%M:%S"])
    command("generate_daily", "generate_synthetic.py", ["--cases", "20000", "--split-by", "day", "--out", destination / "daily"])
    command("generate_large", "generate_synthetic.py", ["--cases", "1650000", "--out", destination / "ten_million.parquet"])
    source = work / "inputs"
    def pipeline(label, src, flags=()):
        command(label, "run_pipeline.py", ["--input", src, "--auto-map", "--no-ai", "--data-dir", destination / label, *flags])
    pipeline("simple", destination / "clean.parquet")
    pipeline("filtered", source / "clean.parquet", ["--drop-missing-case", "--merge-repeats", "--filter-column",
                                                     "Application_name", "--filter-values", "SAP Logon", "Excel"])
    pipeline("apps", source / "clean.parquet", ["--application", "Application_name", "--window-title", "simplified_title",
                                                "--app-map", source / "app-map.csv"])
    pipeline("tickets", source / "tickets.parquet", ["--activity", "activity", "--timestamp", "timestamp", "--resource", "resource",
        "--window-title", "window_title", "--derive-case", "text_pattern", "--pattern", "ticket"])
    pipeline("sessions", source / "sessions.parquet", ["--derive-case", "work_session", "--session-gap", "30"])
    pipeline("time_windows", source / "sessions.parquet", ["--derive-case", "time_window", "--window", "day"])
    pipeline("overview", source / "sessions.parquet", ["--overview-only"])
    df = pd.read_parquet(source / "sessions.parquet")
    records = df.groupby(["resource", df.timestamp.dt.date]).agg(start_ts=("timestamp", "min"), end_ts=("timestamp", "max")).reset_index()
    records["record_id"] = [f"R{i}" for i in range(len(records))]
    records.rename(columns={"resource": "user"}).to_csv(destination / "records.csv", index=False)
    pipeline("record_join", source / "sessions.parquet", ["--resource", "resource", "--record-file", destination / "records.csv"])
    df = pd.read_parquet(source / "clean.parquet").drop(columns="activity")
    df.to_parquet(destination / "no_activity.parquet", index=False)
    pipeline("derive_activity", destination / "no_activity.parquet", ["--derive-activity"])
    con = connect()
    open_source(con, source / "five_files", destination)
    overview = dataset_overview(con)
    write(work / "folder_overview.json", overview)
    write(work / "readme_examples.json", outcomes)


if __name__ == "__main__": main(Path(sys.argv[1]).resolve())
