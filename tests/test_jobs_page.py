"""Previous jobs: "Delete" moves a job to data/_backup (never a hard delete).

Run with:  pytest -q tests/test_jobs_page.py
"""
import pytest

from app.core import jobs
from app.core.config import PROJECT_ROOT


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def two_jobs():
    a = jobs.create_job("a.csv")
    b = jobs.create_job("b.csv")
    (jobs.job_folder(a["job_id"]) / "events.parquet").write_bytes(b"x")
    return a, b


def test_delete_moves_job_to_backup_and_keeps_shared_stores(tmp_path):
    a, b = two_jobs()
    shared = jobs.data_dir() / "process_orders" / "layout.json"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text("{}", encoding="utf-8")
    assert jobs.job_files(a["job_id"]) == ["events.parquet", "job.json"]
    target = jobs.delete_job(a["job_id"])
    assert not jobs.job_folder(a["job_id"]).exists()
    assert target.parent.name == "jobs" and target.parents[2] == jobs.backup_dir()
    assert (target / "job.json").exists() and (target / "events.parquet").read_bytes() == b"x"
    assert [j["job_id"] for j in jobs.list_jobs()] == [b["job_id"]]
    assert shared.exists()
    with pytest.raises(ValueError):
        jobs.delete_job(a["job_id"])


def test_page_confirmation_moves_the_job():
    from streamlit.testing.v1 import AppTest
    a, b = two_jobs()
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.run().switch_page("ui/jobs_page.py").run()
    assert not at.exception, at.exception
    at.selectbox(key="job_delete_pick").set_value(a["job_id"]).run()
    at.button(key="job_delete").click().run()
    assert not at.exception, at.exception
    assert any("events.parquet" in c.value for c in at.code)          # the dialog lists what moves
    assert jobs.job_folder(a["job_id"]).exists()                      # nothing moved before confirming
    at.button(key="job_delete_confirm").click().run()
    assert not at.exception, at.exception
    assert not jobs.job_folder(a["job_id"]).exists()
    assert list(jobs.backup_dir().glob(f"*/jobs/{a['job_id']}/job.json"))
    assert any(a["job_id"] in s.value and "moved" in s.value for s in at.success)
