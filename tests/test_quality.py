"""Day 2 tests: the quality gate must catch every injected defect.

Each test creates a log with KNOWN problems and checks that the gate reports
them and puts the data in the right band.  Run with:  pytest -q
"""
from pathlib import Path

import pandas as pd
import pytest

from app.core.db import connect
from app.core.ingest import normalise, open_source, profile
from app.core.mapping import suggest_mapping
from app.core.quality import grade, run_quality
from scripts.generate_synthetic import main as generate


def to_events(src: Path, tmp: Path) -> Path:
    """Day 1 pipeline: file -> events.parquet, using the suggested mapping."""
    con = connect()
    open_source(con, src, tmp)
    prof = profile(con)
    m = {role: s.column for role, s in suggest_mapping(prof).items()}
    m.update(timestamp_format=prof.by_name()[m["timestamp"]].ts_format,
             end_timestamp_format=None, timezone="UTC")
    out = tmp / f"{src.stem}_events.parquet"
    normalise(con, m, out)
    return out


def quality_of(tmp: Path, *args) -> dict:
    src = tmp / "log.csv"
    generate(["--cases", "3000", "--start", "2024-01-01", "--months", "12",
              "--ts-format", "%Y-%m-%d %H:%M:%S", "--out", str(src), *args])
    report = run_quality(connect(), to_events(src, tmp))
    return {"report": report, "checks": {c.key: c for c in report.checks}}


def test_grade():
    assert grade(0.0, 0.1, 0.3) == ("pass", 100)
    assert grade(0.5, 0.1, 0.3) == ("fail", 20)
    status, score = grade(0.2, 0.1, 0.3)
    assert status == "warn" and 50 <= score <= 85
    assert grade(5000, 1000, 100, higher_is_worse=False) == ("pass", 100)


def test_clean_log_is_high(tmp_path):
    q = quality_of(tmp_path)
    assert q["report"].band == "High"
    assert all(c.status in ("pass", "info") for c in q["report"].checks)


@pytest.mark.parametrize("flag, value, check_key", [
    ("--missing-ts", "0.03", "required_fields"),
    ("--placeholder", "0.01", "timestamp_validity"),
    ("--date-only", "0.5", "timestamp_granularity"),
    ("--ties", "0.2", "ordering_ties"),
    ("--duplicates", "0.03", "duplicates"),
    ("--typos", "0.02", "label_hygiene"),
])
def test_each_defect_is_detected(tmp_path, flag, value, check_key):
    q = quality_of(tmp_path, flag, value)
    assert q["checks"][check_key].status in ("warn", "fail"), q["checks"][check_key].shown


def test_badly_broken_log_is_not_feasible(tmp_path):
    q = quality_of(tmp_path, "--missing-ts", "0.25", "--ties", "0.4", "--date-only", "0.9")
    assert q["report"].band == "Not feasible"
    assert q["report"].fixes, "a not-feasible result must come with a fix list"


def test_too_few_cases_fails(tmp_path):
    src = tmp_path / "tiny.parquet"
    generate(["--cases", "50", "--start", "2024-01-01", "--months", "6", "--out", str(src)])
    report = run_quality(connect(), to_events(src, tmp_path))
    assert {c.key: c for c in report.checks}["volume"].status == "fail"
    assert report.feasibility <= 39


def test_random_paths_are_not_a_process(tmp_path):
    """Every case takes its own random path: there is no process to show."""
    import numpy as np
    rng = np.random.default_rng(0)
    rows, t0 = [], pd.Timestamp("2024-01-01")
    for c in range(2000):
        t = t0 + pd.Timedelta(minutes=int(rng.integers(0, 200_000)))
        for _ in range(int(rng.integers(4, 9))):
            t += pd.Timedelta(seconds=int(rng.integers(5, 600)))
            rows.append((f"S{c}", f"Page {rng.integers(0, 40)}", t))
    src = tmp_path / "random.parquet"
    pd.DataFrame(rows, columns=["session_id", "page", "event_time"]).to_parquet(src, index=False)
    report = run_quality(connect(), to_events(src, tmp_path))
    checks = {c.key: c for c in report.checks}
    assert checks["pattern_strength"].status == "fail"
    assert checks["label_hygiene"].status == "pass"  # "Page 1" vs "Page 10" are not typos
    assert report.band == "Not feasible"
