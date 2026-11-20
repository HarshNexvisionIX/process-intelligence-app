"""Page 3 tests: the process map page runs headless with Streamlit's AppTest.

AppTest runs the page scripts in this process without starting a server.
Jobs are written to a temporary data folder: config.data_dir() reads the
DATA_DIR environment variable on every call, and an absolute DATA_DIR is used
as is, so nothing touches the project's own data/ folder.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.core import jobs
from app.core.config import PROJECT_ROOT, data_dir
from app.core.db import connect
from app.core.ingest import normalise, open_source, profile
from app.core.mapping import suggest_mapping
from app.core.quality import run_quality, save_report
from scripts.generate_synthetic import main as generate

MAIN = str(PROJECT_ROOT / "app" / "main.py")


@pytest.fixture
def data_folder(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(folder))
    return folder


def make_job(tmp: Path, cases: int) -> dict:
    """A job taken through pages 1 and 2: converted and quality checked."""
    src = tmp / f"log_{cases}.parquet"
    generate(["--cases", str(cases), "--start", "2024-01-01", "--months", "12", "--out", str(src)])
    job = jobs.create_job(src.name, src)
    folder = jobs.job_folder(job["job_id"])
    con = connect()
    open_source(con, src, folder)
    prof = profile(con)
    m = {role: s.column for role, s in suggest_mapping(prof).items()}
    m.update(timestamp_format=prof.by_name()[m["timestamp"]].ts_format,
             end_timestamp_format=None, timezone="UTC")
    summary = normalise(con, m, folder / "events.parquet")
    report = run_quality(connect(), folder / "events.parquet")
    save_report(report, folder)
    job.update(status="quality_checked", summary=summary, feasibility=report.feasibility, band=report.band)
    jobs.save_job(job)
    return job


def open_map_page(job: dict) -> AppTest:
    at = AppTest.from_file(MAIN, default_timeout=120)
    at.session_state["job"] = job
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    assert not at.exception, at.exception
    return at


def click(at: AppTest, label: str) -> None:
    next(b for b in at.button if b.label == label).click()
    at.run()
    assert not at.exception, at.exception


def test_absolute_data_dir_is_used(data_folder):
    assert data_dir() == data_folder and data_folder.exists()


def test_run_analysis_draws_the_map(data_folder, tmp_path):
    job = make_job(tmp_path, 3000)
    assert job["band"] == "High"
    at = open_map_page(job)
    assert not at.get("graphviz_chart")          # nothing until the analysis has run
    click(at, "Run analysis")
    cases = next(m for m in at.metric if m.label == "Cases")
    assert cases.value == "3,000"
    assert at.get("graphviz_chart")
    saved = jobs.load_job(job["job_id"])
    assert saved["status"] == "mined" and saved["mined_at"]
    assert (jobs.job_folder(job["job_id"]) / "mining.json").exists()

    # Fewer arrows still draws a map; the saved analysis is reused, not re-run.
    # The sliders belong to the detailed map; the simple map (intended order) is the default view.
    at.radio(key="map_view").set_value("Detailed (all transitions)").run()
    assert not at.exception, at.exception
    at.slider(key="map_arrows").set_value(10).run()
    assert not at.exception, at.exception
    assert at.get("graphviz_chart")
    at.radio(key="map_mode").set_value("Time").run()
    assert not at.exception, at.exception
    assert at.get("graphviz_chart")

    # Quality checked again after the analysis: old results are not shown as current.
    folder = jobs.job_folder(job["job_id"])
    save_report(run_quality(connect(), folder / "events.parquet"), folder)
    at.run()
    assert any("quality check ran again" in w.value for w in at.warning)
    assert not at.get("graphviz_chart")


def test_not_feasible_shows_no_map(data_folder, tmp_path):
    job = make_job(tmp_path, 50)
    assert job["band"] == "Not feasible"
    at = open_map_page(job)
    assert any("did not pass the quality gate" in e.value for e in at.error)
    assert not at.get("graphviz_chart")
    assert not [b for b in at.button if b.label == "Run analysis"]


def test_no_checked_jobs_yet(data_folder):
    at = open_map_page({})
    assert any("Run the data quality check first" in i.value for i in at.info)
