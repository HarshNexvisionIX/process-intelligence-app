"""The optional process model discovered with PM4Py (pm4py_model.py; internal use only, AGPL v3).

Jobs are written to a temporary data folder (DATA_DIR), never to data/. The rest of the suite runs with
PM_DISABLE_PM4PY=1 (conftest.py); these tests switch the model on.
"""
import json
import shutil

import pytest

from app.core import jobs, pm4py_model
from app.core.config import PROJECT_ROOT, settings
from test_golden import GOLDEN, MAPPING, A, B, C, D, make_job

pytest.importorskip("pm4py")


@pytest.fixture
def model_on(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PM_DISABLE_PM4PY", raising=False)
    monkeypatch.setitem(settings()["pm4py"], "enabled", True)
    monkeypatch.setitem(settings()["pm4py"], "build_on_run", True)      # these tests build with the analysis
    return tmp_path


def golden_job(tmp):
    src = tmp / "golden_events.csv"
    shutil.copy(GOLDEN / "golden_events.csv", src)
    job = make_job(tmp, src)
    return job, jobs.job_folder(job["job_id"])


def test_model_is_discovered_in_the_analysis(model_on):
    job, folder = golden_job(model_on)
    sec = json.loads((folder / "mining.json").read_text(encoding="utf-8"))["process_model"]
    assert sec["available"], sec["reason"]
    d = sec["data"]
    assert (d["cases_used"], d["cases_total"], d["runs_used"], d["steps"]) == (6, 6, 23, 4)
    assert 0 < d["fitness"]["log_fitness"] <= 1 and 0 <= d["fitness"]["fitting_cases_share"] <= 1
    assert d["dot"].startswith("digraph bpmn") and all(f'"{s}"' in d["dot"] for s in (A, B, C, D))
    bpmn = (folder / pm4py_model.BPMN_FILE).read_text(encoding="utf-8")
    assert "bpmn:definitions" in bpmn and A in bpmn
    for secret in ("u1", "u2", "Ayakha", "Razumovs", "Priya"):          # only step names leave the data
        assert secret not in d["dot"] and secret not in bpmn
    assert "internal use only" in d["licence"]


def test_switched_off_or_not_installed(model_on, monkeypatch):
    monkeypatch.setitem(settings()["pm4py"], "enabled", False)
    ok, why = pm4py_model.status()
    assert not ok and "switched off" in why
    monkeypatch.setitem(settings()["pm4py"], "enabled", True)
    monkeypatch.setattr(pm4py_model.importlib.util, "find_spec", lambda name: None)
    ok, why = pm4py_model.status()
    assert not ok and "pip install -r requirements-pm4py.txt" in why
    job, folder = golden_job(model_on)                                  # the analysis still works
    mining = json.loads((folder / "mining.json").read_text(encoding="utf-8"))
    assert mining["map"]["available"] and not mining["process_model"]["available"]


def test_a_pm4py_error_never_breaks_the_analysis(model_on, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(pm4py_model, "discover", broken)
    job, folder = golden_job(model_on)
    sec = json.loads((folder / "mining.json").read_text(encoding="utf-8"))["process_model"]
    assert not sec["available"] and "RuntimeError" in sec["reason"]


def test_case_sample_is_fixed(model_on, monkeypatch):
    monkeypatch.setitem(settings()["pm4py"], "max_cases", 3)
    job, folder = golden_job(model_on)
    from app.core.db import connect
    first, total = pm4py_model.run_log(connect(), folder / "events.parquet", 3)
    again, _ = pm4py_model.run_log(connect(), folder / "events.parquet", 3)
    assert total == 6 and first["case_id"].nunique() == 3 and first.equals(again)


def test_page3_shows_the_model(model_on):
    from streamlit.testing.v1 import AppTest
    job, folder = golden_job(model_on)
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.session_state["job"] = jobs.load_job(job["job_id"])
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    at.radio(key="map_view").set_value("Discovered model (PM4Py)").run()
    assert not at.exception, at.exception
    assert {"Cases used", "Fitness", "Cases that fit fully"} <= {mt.label for mt in at.metric}
    assert any("Inductive Miner" in c.value for c in at.caption)
