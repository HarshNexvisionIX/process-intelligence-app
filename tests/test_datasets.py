"""Multi-file datasets: a folder, list of files or glob pattern read as ONE log.

Run with:  pytest -q tests/test_datasets.py
"""
import json
import shutil
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from app.core.db import connect
from app.core.ingest import (
    dataset_overview, normalise, open_source, profile, resolve_sources,
)
from app.core.mapping import signature, suggest_mapping
from app.core.mining import run_mining
from app.core.quality import run_quality
from scripts.generate_synthetic import main as generate

BANDS = {"High", "Medium", "Low", "Not feasible"}


def events(day: int, n: int = 4, **extra) -> pd.DataFrame:
    """A tiny log of n events in 2 cases on one day of January 2025."""
    df = pd.DataFrame({
        "case_id": [f"C{day}-{i // 2}" for i in range(n)],
        "activity": ["Start", "End"] * (n // 2),
        "timestamp": pd.date_range(f"2025-01-{day:02d} 08:00", periods=n, freq="h"),
        "resource": [f"user_{i}" for i in range(n)],
    })
    for k, v in extra.items():
        df[k] = v
    return df


def rows(con) -> int:
    return con.execute("SELECT COUNT(*) FROM src").fetchone()[0]


def test_folder_of_parquet_files(tmp_path):
    folder = tmp_path / "exports"
    folder.mkdir()
    sizes = [4, 6, 2]
    for day, n in zip((1, 2, 3), sizes):
        events(day, n).to_parquet(folder / f"day_{day}.parquet", index=False)
    con = connect()
    assert open_source(con, folder, tmp_path) == "parquet"
    assert rows(con) == sum(sizes)
    names = sorted(r[0] for r in con.execute("SELECT DISTINCT _ingest_file FROM src").fetchall())
    assert names == ["day_1.parquet", "day_2.parquet", "day_3.parquet"]  # base names only
    ov = dataset_overview(con)
    assert ov["files"] == 3 and ov["rows"] == sum(sizes)
    assert [f["rows"] for f in ov["per_file"]] == sizes


def test_hive_folders_with_repeated_file_names(tmp_path):
    root = tmp_path / "export"
    for month in ("01", "02"):
        folder = root / "year=2025" / f"month={month}"
        folder.mkdir(parents=True)
        events(int(month)).to_parquet(folder / "data.parquet", index=False)
    con = connect()
    open_source(con, root, tmp_path)
    got = con.execute("SELECT DISTINCT year, month, _ingest_file FROM src ORDER BY month").fetchall()
    # Partition keys become columns; the label is the path below the deepest
    # folder all files share (here year=2025/), never the full path.
    assert [(str(y), str(m)) for y, m, _ in got] == [("2025", "01"), ("2025", "02")]
    assert [g[2] for g in got] == ["month=01/data.parquet", "month=02/data.parquet"]
    assert dataset_overview(con)["files"] == 2


def test_files_with_different_columns_are_combined(tmp_path):
    events(1).to_parquet(tmp_path / "a.parquet", index=False)
    events(2).drop(columns="resource").to_parquet(tmp_path / "b.parquet", index=False)
    events(3, region="North").to_parquet(tmp_path / "c.parquet", index=False)
    con = connect()
    open_source(con, tmp_path, tmp_path)
    assert rows(con) == 12
    missing = {m["column"]: m["missing_in"] for m in dataset_overview(con)["columns_missing_in_files"]}
    assert missing == {"resource": 1, "region": 2}


def test_glob_selects_only_matching_files(tmp_path):
    events(1).to_parquet(tmp_path / "events_2025_01.parquet", index=False)
    events(2).to_parquet(tmp_path / "events_2025_02.parquet", index=False)
    events(3).to_parquet(tmp_path / "other_2025_03.parquet", index=False)
    fmt, files = resolve_sources(str(tmp_path / "events_*.parquet"))
    assert fmt == "parquet" and [f.name for f in files] == ["events_2025_01.parquet", "events_2025_02.parquet"]
    con = connect()
    open_source(con, (tmp_path / "events_*.parquet").as_posix(), tmp_path)
    assert rows(con) == 8


def test_mixed_formats_are_refused(tmp_path):
    events(1).to_parquet(tmp_path / "a.parquet", index=False)
    events(2).to_csv(tmp_path / "b.csv", index=False)
    events(3).to_csv(tmp_path / "c.csv", index=False)
    with pytest.raises(ValueError) as err:
        resolve_sources(tmp_path)
    assert "CSV" in str(err.value) and "Parquet" in str(err.value)
    assert "2 CSV files" in str(err.value)


def test_empty_folder_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "notes.json").write_text("{}")  # unsupported files do not count
    with pytest.raises(ValueError, match="No supported files"):
        open_source(connect(), tmp_path / "empty", tmp_path)


def test_folder_of_csv_with_day_month_dates(tmp_path):
    folder = tmp_path / "csv"
    folder.mkdir()
    for day in (13, 20, 27):  # days above 12: the format cannot be month/day
        df = events(day)
        df["timestamp"] = df["timestamp"].dt.strftime("%d/%m/%Y %H:%M:%S")
        df.to_csv(folder / f"log_{day}.csv", index=False)
    con = connect()
    assert open_source(con, folder, tmp_path) == "csv"
    prof = profile(con)
    assert prof.by_name()["timestamp"].ts_format == "%d/%m/%Y %H:%M:%S"
    m = {role: s.column for role, s in suggest_mapping(prof).items()}
    m.update(timestamp_format="%d/%m/%Y %H:%M:%S", end_timestamp_format=None, timezone="UTC")
    result = normalise(con, m, tmp_path / "events.parquet")
    assert result["events"] == prof.rows == 12
    assert result["unparsed_timestamps"] == 0


def test_existing_filename_column_survives(tmp_path):
    events(1, filename="scan_001.pdf").to_parquet(tmp_path / "a.parquet", index=False)
    events(2, filename="scan_002.pdf").to_parquet(tmp_path / "b.parquet", index=False)
    con = connect()
    open_source(con, tmp_path, tmp_path)
    got = con.execute("SELECT DISTINCT filename, _ingest_file FROM src ORDER BY 1").fetchall()
    assert got == [("scan_001.pdf", "a.parquet"), ("scan_002.pdf", "b.parquet")]


def test_single_file_also_gets_ingest_file(tmp_path):
    events(1).to_parquet(tmp_path / "one.parquet", index=False)
    con = connect()
    open_source(con, tmp_path / "one.parquet", tmp_path)
    assert con.execute("SELECT DISTINCT _ingest_file FROM src").fetchall() == [("one.parquet",)]


def test_incompatible_types_name_the_column(tmp_path):
    pd.DataFrame({"case_id": ["A"], "detail": [1]}).to_parquet(tmp_path / "a.parquet", index=False)
    pd.DataFrame({"case_id": ["B"], "detail": [[1, 2]]}).to_parquet(tmp_path / "b.parquet", index=False)
    with pytest.raises(ValueError, match='"detail"'):
        open_source(connect(), tmp_path, tmp_path)


def to_events(src: Path, tmp: Path) -> Path:
    """Day 1 pipeline: file(s) -> events.parquet, using the suggested mapping."""
    con = connect()
    open_source(con, src, tmp)
    prof = profile(con)
    m = {role: s.column for role, s in suggest_mapping(prof).items()}
    m.update(timestamp_format=prof.by_name()[m["timestamp"]].ts_format,
             end_timestamp_format=None, timezone="UTC")
    out = tmp / f"{src.stem}_events.parquet"
    normalise(con, m, out)
    return out


@pytest.fixture
def overlapping_exports(tmp_path) -> tuple[Path, Path]:
    """One log, and a folder holding two identical copies of it (overlapping exports)."""
    single = tmp_path / "single.parquet"
    generate(["--cases", "400", "--start", "2024-01-01", "--months", "6", "--out", str(single)])
    folder = tmp_path / "copied"
    folder.mkdir()
    for name in ("export_a.parquet", "export_b.parquet"):
        shutil.copy(single, folder / name)
    return to_events(single, tmp_path), to_events(folder, tmp_path)


def test_duplicates_across_files_are_found(overlapping_exports):
    _, copied = overlapping_exports
    check = {c.key: c for c in run_quality(connect(), copied).checks}["duplicates"]
    assert check.status in ("warn", "fail"), check.shown
    assert any("more than one file" in e for e in check.examples), check.examples


def test_mining_counts_overlapping_exports_once(overlapping_exports):
    single, copied = overlapping_exports
    one = run_mining(connect(), single, "High")["kpis"]["data"]
    two = run_mining(connect(), copied, "High")["kpis"]["data"]
    assert (two["cases"], two["events"]) == (one["cases"], one["events"])


def test_saved_mappings_ignore_ingest_file():
    assert signature(["case", "activity", "time", "_ingest_file"]) == signature(["case", "activity", "time"])


def test_generator_split_by_day(tmp_path):
    out = tmp_path / "daily"
    generate(["--cases", "200", "--start", "2024-01-01", "--months", "1",
              "--split-by", "day", "--out", str(out)])
    files = list(out.glob("events_*.parquet"))
    assert len(files) > 5 and (out / "truth.json").exists()
    truth = json.loads((out / "truth.json").read_text(encoding="utf-8"))
    con = connect()
    open_source(con, out, tmp_path)
    assert rows(con) == truth["rows_written"]
    assert dataset_overview(con)["files"] == len(files) == truth["files_written"]


def test_dataset_end_to_end(tmp_path):
    out = tmp_path / "monthly"
    generate(["--cases", "3000", "--start", "2024-01-01", "--months", "6",
              "--split-by", "month", "--format", "csv",
              "--ts-format", "%Y-%m-%d %H:%M:%S", "--out", str(out)])
    con = connect()
    open_source(con, out, tmp_path)
    prof = profile(con)
    suggestions = suggest_mapping(prof)
    assert "_ingest_file" not in {s.column for s in suggestions.values()}
    m = {role: s.column for role, s in suggestions.items()}
    m.update(timestamp_format=prof.by_name()[m["timestamp"]].ts_format,
             end_timestamp_format=None, timezone="UTC")
    ev = tmp_path / "events.parquet"
    normalise(con, m, ev)
    cols = [r[0] for r in duckdb.execute(f"DESCRIBE SELECT * FROM '{ev.as_posix()}'").fetchall()]
    assert "attr_ingest_file" in cols
    report = run_quality(connect(), ev)
    assert report.band in BANDS
    # _ingest_file is offered as a row filter by page 1's rule (2-50 values, not a date).
    f = prof.by_name()["_ingest_file"]
    assert not f.ts_format and 2 <= f.distinct <= 50
