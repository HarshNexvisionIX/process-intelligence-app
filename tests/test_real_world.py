"""Real-world messiness tests.

These do NOT use any client file. They rebuild, with made-up data, situations
that real recording tools produce: unrelated noise rows, ID columns next to
name columns, half-anonymised personal data, repeated clicks, timezone offsets.
The app must handle all of them without being tuned to one tool's format.
"""
import hashlib
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from app.core.db import connect
from app.core.ingest import normalise, open_source, profile
from app.core.mapping import sensitive_columns, suggest_mapping, validate_mapping
from app.core.quality import _typo_pairs, run_quality

STEPS = ["Receive Request", "Check Details", "Approve Request", "Update Record", "Close Request"]
FIRST = ["Asha", "John", "Maria", "Wei", "Omar", "Lena", "Ravi", "Sara", "Tom", "Priya"]
LAST = ["Rao", "Smith", "Garcia", "Chen", "Khan", "Fischer", "Iyer", "Brown", "Lopez", "Nair"]


def h(x) -> str:
    return hashlib.md5(str(x).encode()).hexdigest()


@pytest.fixture(scope="module")
def desktop_log(tmp_path_factory) -> Path:
    """Desktop-capture style log: 50% noise rows, repeated clicks, code columns, a name leak."""
    rng = np.random.default_rng(1)
    rows, t0 = [], pd.Timestamp("2025-03-01 09:00")
    for c in range(1500):
        t = t0 + pd.Timedelta(minutes=int(rng.integers(0, 60 * 24 * 60)))
        # Each case is one person. 95% are anonymised (hashed); 5% leaked as real names.
        person = f"{FIRST[(c // 20) % 10]} {LAST[(c // 7) % 10]}"
        case_value = person if c % 20 == 0 else h(("person", c))
        for step in STEPS[: rng.integers(3, 6)]:
            for _ in range(int(rng.integers(1, 6))):  # 1-5 clicks per step = repeats
                t += pd.Timedelta(seconds=int(rng.integers(2, 90)))
                rows.append({"event_id": h((c, t)), "case_key": h(c), "case_label": case_value,
                             "task_id": h(step), "task_name": step, "participant_id": h(c % 7),
                             "app": "Ticket System", "is_process": 1, "event_time": t})
                if rng.random() < 1.0:  # one unrelated noise event per process event
                    t += pd.Timedelta(seconds=int(rng.integers(1, 30)))
                    rows.append({"event_id": h((c, t, "n")), "case_key": None, "case_label": None,
                                 "task_id": None, "task_name": None, "participant_id": h(c % 7),
                                 "app": str(rng.choice(["Chat App", "File Browser", "Mail"])),
                                 "is_process": 0, "event_time": t})
    path = tmp_path_factory.mktemp("desktop") / "desktop.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def setup(path: Path, tmp: Path):
    con = connect()
    open_source(con, path, tmp)
    prof = profile(con)
    m = {r: s.column for r, s in suggest_mapping(prof).items()}
    m.update(timestamp_format=prof.by_name()[m["timestamp"]].ts_format,
             end_timestamp_format=None, timezone="UTC")
    return con, prof, m


def test_readable_names_beat_codes(desktop_log, tmp_path):
    _, _, m = setup(desktop_log, tmp_path)
    assert m["activity"] == "task_name"       # not task_id (a hash with the same 5 values)
    assert m["resource"] == "participant_id"  # recognised as the user column
    assert m["case_id"] == "case_key"


def test_noise_rows_warn_but_never_block(desktop_log, tmp_path):
    con, prof, m = setup(desktop_log, tmp_path)
    checks = {c.name: c for c in validate_mapping(con, m, prof.rows)}
    assert all(c.severity != "error" for c in checks.values())
    assert not checks["Case ID completeness"].ok  # ~50% noise is reported...
    assert "Leave out rows" in checks["Case ID completeness"].message  # ...with the fix


def test_scope_filter_removes_noise_and_reports_it(desktop_log, tmp_path):
    con, prof, m = setup(desktop_log, tmp_path)
    m["scope"] = {"drop_missing_case": True}
    checks = {c.name: c for c in validate_mapping(con, m, prof.rows)}
    assert checks["Case ID completeness"].ok
    result = normalise(con, m, tmp_path / "e.parquet")
    assert result["rows_left_out"] > 0
    assert result["events"] + result["rows_left_out"] == prof.rows  # nothing lost silently


def test_value_filter(desktop_log, tmp_path):
    con, prof, m = setup(desktop_log, tmp_path)
    m["scope"] = {"filter_column": "is_process", "filter_values": ["1"]}
    result = normalise(con, m, tmp_path / "e.parquet")
    noise = duckdb.execute(f"SELECT COUNT(*) FROM '{desktop_log.as_posix()}' WHERE is_process = 0").fetchone()[0]
    assert result["rows_left_out"] == noise


def test_half_anonymised_names_are_flagged(desktop_log, tmp_path):
    _, prof, m = setup(desktop_log, tmp_path)
    flagged = sensitive_columns(prof, {v for k, v in m.items() if k in ("case_id", "activity", "timestamp", "resource") and v})
    assert "case_label" in flagged
    assert "app" not in flagged and "task_name" not in flagged


def test_excluded_columns_are_not_copied(desktop_log, tmp_path):
    con, _, m = setup(desktop_log, tmp_path)
    m["exclude_columns"] = ["case_label"]
    normalise(con, m, tmp_path / "e.parquet")
    cols = [r[0] for r in duckdb.execute(f"DESCRIBE SELECT * FROM '{(tmp_path / 'e.parquet').as_posix()}'").fetchall()]
    assert "attr_case_label" not in cols


def test_merge_repeats(desktop_log, tmp_path):
    con, prof, m = setup(desktop_log, tmp_path)
    m["scope"] = {"drop_missing_case": True}
    raw = normalise(con, m, tmp_path / "raw.parquet")
    m["merge_repeats"] = True
    merged = normalise(con, m, tmp_path / "merged.parquet")
    assert merged["events"] < raw["events"]
    assert merged["events_before_merge"] == raw["events"]  # every click accounted for
    # A merged step starts at its first click and ends at its last one.
    bad = duckdb.execute(f"SELECT COUNT(*) FROM '{(tmp_path / 'merged.parquet').as_posix()}' "
                         "WHERE ts_end < ts_start").fetchone()[0]
    assert bad == 0
    # No two consecutive steps in a case are the same activity any more.
    q = run_quality(connect(), tmp_path / "merged.parquet")
    assert {c.key: c for c in q.checks}["repeated_steps"].value == 0


def test_repeats_produce_a_tip(desktop_log, tmp_path):
    con, prof, m = setup(desktop_log, tmp_path)
    m["scope"] = {"drop_missing_case": True}
    normalise(con, m, tmp_path / "e.parquet")
    q = run_quality(connect(), tmp_path / "e.parquet")
    assert any("Merge repeated steps" in (f["fix"] or "") for f in q.fixes)


@pytest.mark.parametrize("text, chosen_tz, expected_utc", [
    ("2026-06-15 10:00:00+05:30", "UTC", "2026-06-15 04:30:00"),            # offset wins
    ("2026-06-15T10:00:00Z", "Asia/Kolkata", "2026-06-15 10:00:00"),       # Z wins over the choice
    ("2026-06-15 10:00:00", "Asia/Kolkata", "2026-06-15 04:30:00"),        # no offset: use the choice
    ("2026-06-15 10:00:00.123-04:00", "UTC", "2026-06-15 14:00:00.123"),
])
def test_timezone_offsets(tmp_path, text, chosen_tz, expected_utc):
    csv = tmp_path / "tz.csv"
    csv.write_text(f"case,activity,time\nA,Start,{text}\nA,End,{text}\n")
    con, _, m = setup(csv, tmp_path)
    m["timezone"] = chosen_tz
    normalise(con, m, tmp_path / "e.parquet")
    got = duckdb.execute(f"SELECT CAST(MIN(ts_start) AS VARCHAR) FROM '{(tmp_path / 'e.parquet').as_posix()}'").fetchone()[0]
    assert got == expected_utc


def test_typo_search_is_fast_and_careful():
    labels = [f"Screen action number {i} for module {i % 97}" for i in range(20000)]
    keys = [l.lower().replace(" ", "") for l in labels] + ["approverequest", "aproverequest"]
    start = time.perf_counter()
    pairs = _typo_pairs(keys)
    assert time.perf_counter() - start < 10
    assert ("approverequest", "aproverequest") in pairs or ("aproverequest", "approverequest") in pairs
    assert not any("screenaction" in a for a, _ in pairs)  # differ only by numbers: not typos
