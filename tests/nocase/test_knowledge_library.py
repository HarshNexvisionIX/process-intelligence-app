"""Phase N5: the SME library is append-logged, versioned and reused by the no-case engine."""
import json
import shutil

import pytest
import yaml

from app.core import jobs
from app.nocase import library as packs
from app.nocase.knowledge import library
from app.nocase.pipeline import run
from app.nocase.stages import run_n2
from fixtures import load_mapping, make


def _seed_pack():
    source = __import__("pathlib").Path(__file__).parent / "packs" / "icc_like"
    target = library.process_dir("icc_like")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
    return target


def test_approval_batch_writes_rules_decisions_and_snapshot(tmp_path):
    _seed_pack()
    view = library.load("icc_like")
    proposals = [{"id": f"SME-{i}", "step": "Invoice Check", "match": {"app": "SAP", "screen": f"screen-{i}"}}
                 for i in range(3)]
    view.propose("rules", proposals)
    out = view.approve([p["id"] for p in proposals], "Reviewer", "Confirmed after review")
    assert out["count"] == 3
    saved = yaml.safe_load((view["path"] / "rules.yaml").read_text(encoding="utf-8"))["rules"]
    mine = [r for r in saved if r["id"].startswith("SME-")]
    assert len(mine) == 3 and all(r["status"] == "sme_confirmed" for r in mine)
    decisions = (view["path"] / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(decisions) == 3
    assert len(list((view["path"] / "versions").iterdir())) == 1
    assert not library.load("icc_like")["items"]


def test_rejected_rule_fingerprint_is_not_proposed_again(tmp_path):
    _seed_pack()
    view = library.load("icc_like")
    rule = {"id": "AI-1", "step": "Invoice Check", "match": {"app": "SAP", "screen": "^Invoice$"}}
    view.propose("rules", [rule])
    view.reject(["AI-1"], "Reviewer", "Does not identify that step")
    view.propose("rules", [{**rule, "id": "AI-2"}])
    pending = yaml.safe_load((view["path"] / "rules.proposed.yaml").read_text(encoding="utf-8"))
    assert pending["rules"] == []
    approved = yaml.safe_load((view["path"] / "rules.yaml").read_text(encoding="utf-8"))["rules"]
    assert next(r for r in approved if r["id"] == "AI-1")["status"] == "rejected"


def test_approved_rules_are_reused_on_a_second_file(tmp_path):
    _seed_pack()
    view = library.load("icc_like")
    fixture_rules = yaml.safe_load((__import__("pathlib").Path(__file__).parent / "packs" / "icc_like" /
                                    "rules.yaml").read_text(encoding="utf-8"))
    view.propose("rules", fixture_rules)
    fixture_rules = fixture_rules["rules"]
    view.approve([r["id"] for r in fixture_rules], "Reviewer", "Validated on first fixture")
    src_one = make("icc_like", tmp_path / "first")
    first = run(src_one, load_mapping("icc_like"))
    run_n2(jobs.job_folder(first["job"]["job_id"]), process="icc_like")
    src_two = make("icc_like", tmp_path / "second")
    second = run(src_two, load_mapping("icc_like"))
    out = run_n2(jobs.job_folder(second["job"]["job_id"]), process="icc_like")
    assert out["assign"]["status"] == "ran"
    quality = json.loads((jobs.job_folder(second["job"]["job_id"]) / "nc_assign_quality.json").read_text())
    assert quality["by_source"].get("rule", {}).get("events", 0) > 0


def test_editing_a_rule_versions_it_and_is_auditable(tmp_path):
    _seed_pack()
    view = library.load("icc_like")
    edited = view.edit_rule("R1", {"app": "SAP", "screen": "^Invoice Display$"}, "Reviewer")
    assert edited["version"] == 2 and edited["status"] == "sme_confirmed"
    assert view.history("R1")[-1]["action"] == "edited"
    with pytest.raises(ValueError, match="regex"):
        view.edit_rule("R1", {"screen": "(a+)+"}, "Reviewer")


@pytest.mark.parametrize("page", ["nocase_review_steps.py", "nocase_review_activities.py", "nocase_review_order.py",
                                  "nocase_questions.py", "nocase_pack.py"])
def test_sme_pages_render_without_a_job(page):
    from streamlit.testing.v1 import AppTest
    from app.core.config import PROJECT_ROOT
    result = AppTest.from_file(str(PROJECT_ROOT / "app" / "ui" / page), default_timeout=30).run()
    assert not result.exception, result.exception


@pytest.mark.parametrize("page", ["nocase_review_steps.py", "nocase_review_activities.py", "nocase_review_order.py",
                                  "nocase_questions.py", "nocase_pack.py"])
def test_sme_pages_render_with_a_finished_job(page, tmp_path):
    from streamlit.testing.v1 import AppTest
    from app.core.config import PROJECT_ROOT
    _seed_pack()
    result = run(make("icc_like", tmp_path / "input"), load_mapping("icc_like"))
    job = result["job"]
    job["process"] = "icc_like"
    jobs.save_job(job)
    run_n2(jobs.job_folder(job["job_id"]), process="icc_like")
    app = AppTest.from_file(str(PROJECT_ROOT / "app" / "ui" / page), default_timeout=30).run()
    assert not app.exception, app.exception
