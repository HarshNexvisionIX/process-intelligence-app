"""Regression checks discovered by the v1.0 release audit (temporary synthetic data only)."""
import json

import pytest

from app.core import definition as dmod, jobs
from app.core.evidence import run_check
from test_definition import sample
from test_report import make_job


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def test_approved_version_cannot_be_overwritten_as_draft():
    d = dmod.approve(sample(), "Reviewer")
    edited = dmod.ProcessDefinition.from_dict(d.to_dict())
    edited.status = "draft"
    edited.steps[0]["name"] = "Changed"
    with pytest.raises(ValueError, match="approved"):
        dmod.save(edited)
    assert dmod.load(d.name, d.version).status == "approved"


def test_evidence_check_persists_definition_it_links(tmp_path):
    job = make_job(tmp_path, 300)
    d = sample()
    run_check(job, d)
    assert dmod.for_job(jobs.load_job(job["job_id"])) is not None


def test_business_page_checked_definition_and_empty_verdict_filter(tmp_path):
    from streamlit.testing.v1 import AppTest
    from app.core.config import PROJECT_ROOT
    job = make_job(tmp_path, 300)
    d = sample()
    dmod.save(d)
    run_check(job, d)
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.run().switch_page("ui/business_page.py").run()
    assert not at.exception, at.exception
    assert len(at.tabs) == 7      # + "Knowledge sources" and "Conflicts & questions" (Prompt 8)
    next(x for x in at.multiselect if x.label == "Verdicts").set_value([]).run()
    assert not at.exception, at.exception
    next(x for x in at.text_input if x.label == "Start trigger").set_value("A changed trigger")
    next(x for x in at.button if x.label == "Save draft").click().run()
    assert not at.exception, at.exception
    assert next(x for x in at.button if x.label == "Approve this version").disabled


def test_definition_cli_completes(tmp_path):
    from test_report import run_cli
    from scripts.generate_synthetic import main as generate
    src = tmp_path / "input.parquet"
    generate(["--cases", "300", "--start", "2024-01-01", "--out", str(src)])
    path = tmp_path / "definition.json"
    path.write_text(json.dumps(sample().to_dict()), encoding="utf-8")
    result = run_cli("--input", str(src), "--auto-map", "--no-ai", "--definition", str(path),
                     "--data-dir", str(tmp_path / "cli"), "--export-bpmn", str(tmp_path / "out.bpmn"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Claims:" in result.stdout
    html = next((tmp_path / "cli/jobs").glob("*/report.html")).read_text(encoding="utf-8")
    assert "ProcessLens v1.1.0" in html and "Application version" in html
    assert "DRAFT — not approved" in html and "Evidence:" in html


def test_derived_case_does_not_steal_activity_or_resource_columns(tmp_path):
    import pandas as pd
    from app.core.db import connect
    from app.core.ingest import open_source, profile
    from scripts.run_pipeline import parse_args, build_mapping
    src = tmp_path / "sessions.parquet"
    pd.DataFrame([{"resource": f"user_{i % 6}", "activity": a,
                   "timestamp": pd.Timestamp("2024-01-01") + pd.Timedelta(days=i, minutes=j)}
                  for i in range(30) for j, a in enumerate(["Open", "Check", "Close"])]).to_parquet(src)
    con = connect()
    open_source(con, src, tmp_path)
    args = parse_args(["--input", str(src), "--auto-map", "--derive-case", "work_session"])
    mapped = build_mapping(args, profile(con))
    assert mapped["activity"] == "activity" and mapped["resource"] == "resource"
