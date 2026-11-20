"""Search for a better reading: other case and activity definitions of the same rows.

Run with:  pytest -q tests/test_reading_finder.py      (slow test: pytest -m slow)
"""
import json
import time
from pathlib import Path

import pandas as pd
import pytest

from app.core import jobs
from app.core.db import connect
from app.core.ingest import normalise, open_source, profile
from app.core.quality import run_quality, save_report
from app.core.reading_finder import Reading, apply_reading, find_readings, warning_text
from scripts.generate_synthetic import main as generate


@pytest.fixture(autouse=True)
def data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def opened(src: Path, tmp: Path, case: str) -> tuple:
    con = connect()
    open_source(con, src, tmp)
    prof = profile(con)
    mapping = {"case_id": case, "activity": "activity", "timestamp": "timestamp", "resource": "resource",
               "timestamp_format": prof.by_name()["timestamp"].ts_format, "end_timestamp": None,
               "end_timestamp_format": None, "timezone": "UTC", "scope": {}, "exclude_columns": [],
               "merge_repeats": False}
    return con, mapping


def batch_log(tmp: Path, cases: int = 3000) -> Path:
    src = tmp / "batches.parquet"
    generate(["--cases", str(cases), "--start", "2024-01-01", "--months", "12", "--case-column-name", "request_id",
              "--batch-days", "7", "--out", str(src)])
    return src


def test_correct_case_column_ranks_first(tmp_path):
    con, mapping = opened(batch_log(tmp_path), tmp_path, "batch")      # the wrong case column
    readings = find_readings(con, "src", mapping)
    best, current = readings[0], next(r for r in readings if r.current)
    assert best.case_spec == {"kind": "column", "column": "request_id"} and best.activity == "activity"
    assert best.confidence > current.confidence and current.case_label == "batch"
    assert warning_text(readings) is None
    assert all(r.case_spec.get("column") != "amount" for r in readings)   # numbers are not IDs


def test_shuffled_log_has_no_good_reading(tmp_path):
    src = tmp_path / "shuffled.parquet"
    generate(["--cases", "3000", "--start", "2024-01-01", "--months", "12", "--shuffle-order", "--out", str(src)])
    con, mapping = opened(src, tmp_path, "case_id")
    readings = find_readings(con, "src", mapping)
    assert readings[0].confidence < 40
    message = warning_text(readings)
    assert message.startswith("No reading of this data shows a reliable order (best: ")
    assert f"of {len(readings)} tried). The order must come from the business" in message


def make_job(src: Path, case: str) -> dict:
    job = jobs.create_job(src.name, src)
    folder = jobs.job_folder(job["job_id"])
    con, mapping = opened(src, folder, case)
    summary = normalise(con, mapping, folder / "events.parquet")
    (folder / "mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
    report = run_quality(connect(), folder / "events.parquet")
    save_report(report, folder)
    job.update(status="quality_checked", summary=summary, band=report.band, feasibility=report.feasibility,
               format="parquet", layout_signature="sig-test")
    jobs.save_job(job)
    return job


def test_use_this_reading_creates_a_new_job(tmp_path):
    src = batch_log(tmp_path, 1500)
    old = make_job(src, "batch")
    con, mapping = opened(src, tmp_path, "batch")
    best = find_readings(con, "src", mapping)[0]
    new = apply_reading(old["job_id"], best)
    assert new["job_id"] != old["job_id"] and new["status"] == "quality_checked"
    assert new["reading"]["from_job"] == old["job_id"] and new["reading"]["case_definition"] == "request_id"
    assert json.loads((jobs.job_folder(new["job_id"]) / "mapping.json").read_text())["case_id"] == "request_id"
    kept = jobs.load_job(old["job_id"])                                  # the old job is untouched
    assert kept["status"] == "quality_checked"
    assert json.loads((jobs.job_folder(old["job_id"]) / "mapping.json").read_text())["case_id"] == "batch"
    assert {j["job_id"] for j in jobs.list_jobs()} >= {old["job_id"], new["job_id"]}


def test_session_reading_adds_a_derived_case_column(tmp_path):
    src = batch_log(tmp_path, 400)
    old = make_job(src, "batch")
    reading = Reading("batch + user + work session", {"kind": "column_user_session", "column": "batch",
                                                      "user": "resource"}, "activity", 0, None, 0, 1, None, 0, 0)
    new = apply_reading(old["job_id"], reading)
    cases = pd.read_parquet(jobs.job_folder(new["job_id"]) / "events.parquet", columns=["case_id"])["case_id"]
    assert cases.str.fullmatch(r"[0-9a-f]{16}").all()     # batch | user | session number, stored as a salted hash
    assert cases.nunique() > pd.read_parquet(src, columns=["batch"])["batch"].nunique()
    assert new["reading"]["note"] == "case_id = batch | resource | work session number"


def test_page2_search_and_use_reading(tmp_path):
    from streamlit.testing.v1 import AppTest
    from app.core.config import PROJECT_ROOT
    old = make_job(batch_log(tmp_path, 1500), "batch")
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.session_state["job"] = old
    at.run()
    at.switch_page("ui/quality_page.py")
    at.run()
    next(b for b in at.button if b.label == "Search").click()
    at.run()
    assert not at.exception, at.exception
    assert (jobs.job_folder(old["job_id"]) / "readings.json").exists()
    assert any("Best reading: cases = **request_id**" in md.value for md in at.markdown)
    next(b for b in at.button if b.label == "Use this reading").click()
    at.run()
    assert not at.exception, at.exception
    assert any("created" in s.value and "This job is kept" in s.value for s in at.success)
    assert len(jobs.list_jobs()) == 2


@pytest.mark.slow
def test_find_readings_on_large_data_is_fast(tmp_path):
    src = tmp_path / "large.parquet"
    generate(["--cases", "420000", "--start", "2024-01-01", "--months", "12", "--case-column-name", "request_id",
              "--batch-days", "7", "--out", str(src)])
    con, mapping = opened(src, tmp_path, "batch")
    assert con.execute("SELECT COUNT(*) FROM src").fetchall()[0][0] >= 2_500_000
    start = time.perf_counter()
    readings = find_readings(con, "src", mapping)
    seconds = time.perf_counter() - start
    assert readings[0].case_spec.get("column") == "request_id"
    assert seconds < 60, f"find_readings took {seconds:.1f} s"
