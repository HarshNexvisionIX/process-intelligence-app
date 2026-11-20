"""Day 1 tests. Run from the project root with:  pytest -q"""
from pathlib import Path

import duckdb
import pytest

from app.core.db import connect
from app.core.ingest import normalise, open_source, profile
from app.core.mapping import suggest_mapping, validate_mapping
from scripts.generate_synthetic import main as generate


@pytest.fixture(scope="module")
def samples(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("samples")
    clean = d / "clean.parquet"
    dirty = d / "dirty.csv"
    generate(["--cases", "3000", "--out", str(clean)])
    generate(["--cases", "3000", "--out", str(dirty), "--ts-format", "%d/%m/%Y %H:%M:%S",
              "--missing-ts", "0.05", "--duplicates", "0.02", "--typos", "0.02"])
    return {"clean": clean, "dirty": dirty}


def _mapping(prof, sugg):
    m = {role: s.column for role, s in sugg.items()}
    m["timestamp_format"] = prof.by_name()[m["timestamp"]].ts_format
    m["end_timestamp_format"] = None
    m["timezone"] = "UTC"
    return m


@pytest.mark.parametrize("kind", ["clean", "dirty"])
def test_suggestions_find_the_right_columns(samples, tmp_path, kind):
    con = connect()
    open_source(con, samples[kind], tmp_path)
    prof = profile(con)
    sugg = suggest_mapping(prof)
    assert sugg["case_id"].column == "case_id"
    assert sugg["activity"].column == "activity"
    assert sugg["timestamp"].column == "timestamp"
    assert sugg["resource"].column == "resource"
    assert sugg["end_timestamp"].column is None


def test_text_dates_are_detected(samples, tmp_path):
    con = connect()
    open_source(con, samples["dirty"], tmp_path)
    prof = profile(con)
    assert prof.by_name()["timestamp"].ts_format == "%d/%m/%Y %H:%M:%S"


@pytest.mark.parametrize("kind", ["clean", "dirty"])
def test_normalise_keeps_every_row(samples, tmp_path, kind):
    con = connect()
    open_source(con, samples[kind], tmp_path)
    prof = profile(con)
    mapping = _mapping(prof, suggest_mapping(prof))
    assert not [c for c in validate_mapping(con, mapping, prof.rows) if not c.ok and c.severity == "error"]
    out = tmp_path / "events.parquet"
    result = normalise(con, mapping, out)
    assert result["events"] == prof.rows  # bad rows are kept for the quality gate
    cols = [r[0] for r in duckdb.execute(f"DESCRIBE SELECT * FROM '{out.as_posix()}'").fetchall()]
    assert cols[:6] == ["case_id", "activity", "ts_start", "ts_end", "ts_start_raw", "resource"]
    assert "attr_vendor" in cols


def test_unparsed_timestamps_are_counted(samples, tmp_path):
    con = connect()
    open_source(con, samples["dirty"], tmp_path)
    prof = profile(con)
    result = normalise(con, _mapping(prof, suggest_mapping(prof)), tmp_path / "e.parquet")
    assert result["unparsed_timestamps"] > 0


def test_timezone_conversion(tmp_path):
    csv = tmp_path / "tz.csv"
    csv.write_text("case,activity,time\nA,Start,2026-01-01 10:00:00\nA,End,2026-01-01 11:00:00\n")
    con = connect()
    open_source(con, csv, tmp_path)
    prof = profile(con)
    m = _mapping(prof, suggest_mapping(prof))
    m["timezone"] = "Asia/Kolkata"
    out = tmp_path / "e.parquet"
    normalise(con, m, out)
    first = duckdb.execute(f"SELECT MIN(ts_start) FROM '{out.as_posix()}'").fetchone()[0]
    assert (first.hour, first.minute) == (4, 30)  # 10:00 IST = 04:30 UTC


def test_event_id_is_not_suggested_and_is_flagged(tmp_path):
    rows = ["event_id,order_no,step,created"]
    for i in range(300):
        for j, step in enumerate(["Open", "Work", "Close"]):
            rows.append(f"E{i * 3 + j},O{i},{step},2026-01-{1 + j:02d} 0{j}:00:00")
    csv = tmp_path / "ids.csv"
    csv.write_text("\n".join(rows))
    con = connect()
    open_source(con, csv, tmp_path)
    prof = profile(con)
    sugg = suggest_mapping(prof)
    assert sugg["case_id"].column == "order_no"  # not the unique event_id
    m = _mapping(prof, sugg)
    m["case_id"] = "event_id"  # a user mistake
    checks = {c.name: c for c in validate_mapping(con, m, prof.rows)}
    assert not checks["Events per case"].ok
