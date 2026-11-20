"""Check a job's report against its ORIGINAL input file, recomputed with pandas (no DuckDB).

Usage:
    python scripts/verify_report.py <job_id> [--data-dir DIR]

Reads the file the job was converted from, applies the saved mapping and filters (row filter, rows
without case / activity / usable time), rebuilds the step runs, and recomputes the key numbers and the
"Where the data comes from" lines of the report, the hours, cases and runs of every step and the hours
of every application. Prints report value, recomputed value and ✓ / ✗ side by side.
Exit code 0 when everything matches, 1 when something differs, 2 when the job cannot be checked.
Never prints case IDs, users or window titles: only counts, hours and step / application names.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOURS_TOL = 0.05


def read_input(job: dict) -> pd.DataFrame:
    """The original file(s) as one table of text columns (like the conversion reads CSV), in file order."""
    from app.core import jobs
    from app.core.ingest import resolve_sources
    fmt, files = resolve_sources(jobs.job_source(job))
    frames = []
    for f in files:
        if fmt == "parquet":
            frames.append(pd.read_parquet(f))
        elif fmt == "csv":
            frames.append(pd.read_csv(f, dtype=str, keep_default_na=True, sep=None, engine="python"))
        else:
            frames.append(pd.read_excel(f, dtype=object))
    return pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]


def parse_times(s: pd.Series, fmt: str | None, tz: str) -> pd.Series:
    """Times as naive UTC, the way mapping.timestamp_sql reads them (NaT when they do not parse)."""
    if fmt == "epoch_s":
        out = pd.to_datetime(pd.to_numeric(s, errors="coerce"), unit="s", errors="coerce")
    elif fmt == "epoch_ms":
        out = pd.to_datetime(pd.to_numeric(s, errors="coerce"), unit="ms", errors="coerce")
    elif fmt and fmt.startswith("%"):
        out = pd.to_datetime(s, format=fmt, errors="coerce")
    elif fmt == "iso":
        out = pd.to_datetime(s, format="ISO8601", errors="coerce")
    else:
        out = pd.to_datetime(s, errors="coerce")
    if getattr(out.dt, "tz", None) is not None:
        return out.dt.tz_convert("UTC").dt.tz_localize(None)
    if tz and tz != "UTC":
        return out.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC").dt.tz_localize(None)
    return out


def recompute(job: dict, mapping: dict) -> dict:
    """Everything verify compares, from the original file."""
    from app.core.apps import load_map, number_unnamed, resolve
    from app.core.config import settings
    df = read_input(job)
    n_file = len(df)
    df = df.reset_index(drop=True)
    df["__row"] = range(n_file)
    act_col, case_col, ts_col = mapping["activity"], mapping["case_id"], mapping["timestamp"]
    active_col = mapping.get("active_time")
    sec = pd.to_numeric(df[active_col], errors="coerce").fillna(0.0) if active_col else pd.Series(0.0, index=df.index)
    scope = mapping.get("scope") or {}
    if scope.get("filter_column") and scope.get("filter_values"):
        col = df[scope["filter_column"]]
        keep = col.notna() & col.map(_text).isin([str(v) for v in scope["filter_values"]])
    else:
        keep = pd.Series(True, index=df.index)
    has_case, has_act = df[case_col].notna(), df[act_col].notna()
    ts = parse_times(df[ts_col], mapping.get("timestamp_format"), mapping.get("timezone") or "UTC")
    earliest = int(settings()["quality"]["earliest_valid_year"])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ok_time = ts.notna() & (ts.dt.year >= earliest) & (ts <= now)
    parts = {"left_out_by_filter": ~keep, "without_case": keep & ~has_case,
             "without_activity": keep & has_case & ~has_act,
             "without_time": keep & has_case & has_act & ~ok_time,
             "in_scope": keep & has_case & has_act & ok_time}
    out = {"rows_in_file": n_file, "hours_in_file": sec.sum() / 3600 if active_col else None}
    for k, mask in parts.items():
        out[f"{k}_rows"] = int(mask.sum())
        out[f"{k}_hours"] = float(sec[mask].sum()) / 3600 if active_col else None

    e = df[parts["in_scope"]].copy()
    e["__ts"] = ts[parts["in_scope"]]
    end_col = mapping.get("end_timestamp")
    e["__end"] = (parse_times(e[end_col], mapping.get("end_timestamp_format"), mapping.get("timezone") or "UTC")
                  .fillna(e["__ts"]) if end_col else e["__ts"])
    renames = mapping.get("activity_renames") or {}
    e["__act"] = e[act_col].map(_text).map(lambda a: renames.get(a, a))
    e["__case"] = e[case_col].map(_text)
    e["__sec"] = sec[parts["in_scope"]]
    e = e.sort_values(["__case", "__ts", "__row"], kind="mergesort")
    new_run = (e["__case"] != e["__case"].shift()) | (e["__act"] != e["__act"].shift())
    e["__run"] = new_run.cumsum()
    runs = e.groupby("__run").agg(case=("__case", "first"), act=("__act", "first"))
    durations = e.groupby("__case").apply(lambda g: (g["__end"].max() - g["__ts"].min()).total_seconds() / 3600,
                                          include_groups=False)
    out.update(cases=int(e["__case"].nunique()), runs=len(runs), events=len(e),
               participants=int(e[mapping["resource"]].nunique()) if mapping.get("resource") else 0,
               active_hours=float(e["__sec"].sum()) / 3600 if active_col else None,
               median_case_h=float(durations.median()) if len(durations) else None,
               p90_case_h=float(durations.quantile(0.9)) if len(durations) else None)
    out["steps"] = {a: {"cases": int(g["__case"].nunique()), "hours": float(g["__sec"].sum()) / 3600}
                    for a, g in e.groupby("__act")}
    for a, n in runs.groupby("act").size().items():
        out["steps"][a]["runs"] = int(n)
    app_col = mapping.get("application")
    if app_col:
        # Shown names exactly as the conversion resolved them (application map; masked values -> "Unnamed app N").
        kept = df[keep & (has_case if scope.get("drop_missing_case") else True)
                  & (has_act if scope.get("drop_missing_activity") else True)]
        counts = kept[app_col].dropna().map(_text).value_counts()
        counts = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        the_map = load_map()
        numbers = number_unnamed(counts, the_map)
        names = e[app_col].map(lambda v: None if pd.isna(v) else resolve(_text(v), the_map, numbers))
        out["applications"] = int(names.dropna().nunique())
        out["app_hours"] = {k: float(v) / 3600 for k, v in e["__sec"].groupby(names.fillna("(no application)")).sum().items()}
    return out


def _text(v) -> str:
    """A value as the conversion reads it as text (whole floats without ".0")."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def compare(m: dict, r: dict) -> list[tuple[str, object, object, bool]]:
    rows = []

    def add(label, report, mine, kind="count"):
        if report is None and mine is None:
            ok = True
        elif report is None or mine is None:
            ok = False
        elif kind == "hours":
            ok = abs(float(report) - float(mine)) <= HOURS_TOL
        else:
            ok = float(report) == float(mine)
        rows.append((label, report, mine, ok))

    sc = m.get("scope") or {}
    if sc:
        add("Rows in file", sc["rows_in_file"], r["rows_in_file"])
        for k, label in (("left_out_by_filter", "Left out by the row filter"), ("without_case", "Without a case ID"),
                         ("without_activity", "Without an activity"), ("without_time", "Without a usable time"),
                         ("in_scope", "In scope")):
            add(f"{label}: rows", sc[k]["rows"], r[f"{k}_rows"])
            if sc.get("has_active_time"):
                add(f"{label}: active hours", sc[k]["active_seconds"] / 3600, r[f"{k}_hours"], "hours")
    t = m["totals"]
    add("Cases", t["cases"], r["cases"])
    add("Step runs", t["runs"], r["runs"])
    add("Events before merging", t["events"], r["events"])
    add("Participants", t["participants"], r["participants"])
    if "applications" in r:
        add("Applications", t["applications"], r["applications"])
    add("Active hours in scope", t["active_hours"], r["active_hours"], "hours")
    add("Median case duration (h)", t["median_case_h"], r["median_case_h"], "hours")
    add("90% of cases finish within (h)", t["p90_case_h"], r["p90_case_h"], "hours")
    for s in m["steps"]:
        mine = r["steps"].get(s["step"], {})
        add(f"{s['step']}: cases reached", s["cases"], mine.get("cases"))
        add(f"{s['step']}: runs", s["runs"], mine.get("runs"))
        if s["active_hours"] is not None:
            add(f"{s['step']}: active hours", s["active_hours"], mine.get("hours"), "hours")
    for a in m["applications"]:
        if a["active_hours"] is not None and "app_hours" in r:
            add(f"{a['application']}: active hours", a["active_hours"], r["app_hours"].get(a["application"]), "hours")
    return rows


def _show(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.3f}"
    return f"{int(v):,}" if isinstance(v, (int, float)) else str(v)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("job_id")
    p.add_argument("--data-dir", help="data folder (default: the app's data folder)")
    args = p.parse_args(argv)
    if args.data_dir:
        os.environ["DATA_DIR"] = str(Path(args.data_dir).resolve())
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    from app.core import jobs, metrics
    try:
        job = jobs.load_job(args.job_id)
    except (OSError, ValueError, KeyError):
        print(f"Job {args.job_id} not found.")
        return 2
    folder = jobs.job_folder(args.job_id)
    m = metrics.load(folder)
    mapping_path = folder / "mapping.json"
    if m is None or not mapping_path.exists():
        print("This job has no report numbers yet (metrics.json): generate the report on page 5 first.")
        return 2
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    if ((mapping.get("case_source") or {}).get("kind") or "column") != "column" or mapping.get("derived_activity"):
        print("This job derives its case or activity on page 1; verify works on mapped columns only.")
        return 2
    rows = compare(m, recompute(job, mapping))
    width = max(len(r[0]) for r in rows)
    print(f"{'Number'.ljust(width)}  {'Report':>14}  {'Recomputed':>14}")
    for label, report, mine, ok in rows:
        print(f"{label.ljust(width)}  {_show(report):>14}  {_show(mine):>14}  {'✓' if ok else '✗'}")
    bad = sum(1 for r in rows if not r[3])
    print(f"\n{len(rows) - bad} of {len(rows)} numbers match" + (f"; {bad} differ." if bad else "."))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
