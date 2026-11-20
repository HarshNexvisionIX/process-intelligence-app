"""Merging near-identical activity names (quality check 6 -> page 2 -> conversion).

Run with:  pytest -q tests/test_activity_renames.py
"""
import json

import pandas as pd
import pytest

from app.core import activity_renames, jobs
from app.core.db import connect
from app.core.quality import run_quality
from scripts.run_pipeline import main as pipeline


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def typo_log(tmp):
    """400 cases; 'Approve Request' is written 'Aprove Request' in 40 cases and 'approve request ' in 20."""
    rows = []
    for i in range(400):
        approve = "Aprove Request" if i % 10 == 0 else ("approve request " if i % 20 == 5 else "Approve Request")
        for j, act in enumerate(["Create Request", approve, "Create Order"]):
            rows.append({"case_id": f"K{i:04d}", "activity": act, "resource": f"u{i % 7}",
                         "timestamp": pd.Timestamp("2024-01-01") + pd.Timedelta(days=i % 200, hours=j * 5)})
    src = tmp / "typos.parquet"
    pd.DataFrame(rows).to_parquet(src, index=False)
    return src


def run(*args):
    before = {j["job_id"] for j in jobs.list_jobs()}
    assert pipeline([*args, "--no-ai"]) == 0
    job = next(j for j in jobs.list_jobs() if j["job_id"] not in before)
    return job, jobs.job_folder(job["job_id"])


def counts(folder):
    df = pd.read_parquet(folder / "events.parquet", columns=["activity"])
    return df["activity"].value_counts().to_dict()


def label_check(folder):
    return next(c for c in run_quality(connect(), folder / "events.parquet").checks if c.key == "label_hygiene")


def test_merging_two_typo_spellings(tmp_path):
    src = typo_log(tmp_path)
    job, folder = run("--input", str(src), "--auto-map")
    before = counts(folder)
    groups = activity_renames.groups(folder / "events.parquet")
    assert len(groups) == 1 and groups[0][0] == ("Approve Request", 340)
    assert {n for n, _ in groups[0]} == {"Approve Request", "Aprove Request", "approve request "}
    assert label_check(folder).status == "warn"

    activity_renames.save(job["layout_signature"], {"Aprove Request": "Approve Request",
                                                    "approve request ": "Approve Request"})
    _, folder2 = run("--input", str(src), "--auto-map")            # converted again: merges applied
    after = counts(folder2)
    assert after["Approve Request"] == 400 == before["Approve Request"] + before["Aprove Request"] \
        + before["approve request "]
    assert "Aprove Request" not in after and sum(after.values()) == sum(before.values())
    check = label_check(folder2)
    assert check.status == "pass" and check.value == 0
    mapping = json.loads((folder2 / "mapping.json").read_text(encoding="utf-8"))
    assert mapping["activity_renames"]["Aprove Request"] == "Approve Request"
    assert "Aprove Request → Approve Request" in (folder2 / "report.html").read_text(encoding="utf-8")


def test_pipeline_renames_file(tmp_path):
    src = typo_log(tmp_path)
    renames = tmp_path / "renames.json"
    renames.write_text(json.dumps({"renames": {"Aprove Request": "Approve Request",
                                               "approve request ": "Approve Request"}}), encoding="utf-8")
    job, folder = run("--input", str(src), "--auto-map", "--activity-renames", str(renames))
    assert counts(folder)["Approve Request"] == 400
    assert activity_renames.load(job["layout_signature"])["Aprove Request"] == "Approve Request"


def test_invalid_renames_rejected():
    with pytest.raises(ValueError, match="itself renamed"):
        activity_renames.validate({"A": "B", "B": "C"})
    assert activity_renames.validate({"A": " A ", "x": "y", " ": "z"}) == {"x": "y"}


def test_page2_merge_controls(tmp_path):
    from streamlit.testing.v1 import AppTest
    from app.core.config import PROJECT_ROOT
    src = typo_log(tmp_path)
    job, folder = run("--input", str(src), "--auto-map")
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.session_state["job"] = job
    at.run().switch_page("ui/quality_page.py").run()
    assert not at.exception, at.exception
    next(c for c in at.checkbox if c.label == "Merge into the first spelling").check().run()
    next(b for b in at.button if b.label == "Apply merges").click().run()
    assert not at.exception, at.exception
    assert any("Run Validate and convert again on Import data" in s.value for s in at.success)
    assert activity_renames.load(job["layout_signature"]) == {"Aprove Request": "Approve Request",
                                                              "approve request ": "Approve Request"}
