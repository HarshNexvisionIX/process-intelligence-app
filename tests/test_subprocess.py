"""Sub-processes: process levels, step runs, the standard way inside each step, and the
link to the business process definition.

Run with:  pytest -q tests/test_subprocess.py
"""
import json
import os
from pathlib import Path

import pandas as pd
import pytest

from app.core import jobs, narrative, reference, subprocess as sp
from app.core.config import PROJECT_ROOT
from app.core.db import connect
from app.core.facts import Fact
from app.core.ingest import normalise, open_source
from scripts.generate_synthetic import main as generate
from scripts.run_pipeline import main as pipeline

STANDARD = ["Open Create Request", "Enter details", "Check data", "Click Submit"]
PLANTED = {"Standard": 0.60, "Standard + repeats": 0.10, "Extra actions": 0.10, "Missing steps": 0.10,
           "Different order": 0.10}


@pytest.fixture(scope="module")
def sp_job(tmp_path_factory):
    """One pipeline run on the planted log (merge repeated steps on), shared by the tests."""
    tmp = tmp_path_factory.mktemp("subprocess")
    old = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(tmp / "data")
    src = tmp / "steps.parquet"
    generate(["--cases", "1500", "--start", "2024-01-01", "--months", "12", "--step-actions", "--out", str(src)])
    assert pipeline(["--input", str(src), "--auto-map", "--action", "activity_alias_name", "--merge-repeats",
                     "--no-ai"]) == 0
    job = jobs.list_jobs()[0]
    folder = jobs.job_folder(job["job_id"])
    mining = json.loads((folder / "mining.json").read_text(encoding="utf-8"))
    yield {"tmp": tmp, "src": src, "job": job, "folder": folder, "mining": mining}
    if old is None:
        os.environ.pop("DATA_DIR", None)
    else:
        os.environ["DATA_DIR"] = old


@pytest.fixture
def data_dir_of(sp_job, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(sp_job["tmp"] / "data"))
    return sp_job


def detail(sp_job, step: str) -> dict:
    return next(d for d in sp_job["mining"]["subprocess"]["data"]["steps"] if d["activity"] == step)


def test_mapping_roles_found(sp_job):
    mapping = json.loads((sp_job["folder"] / "mapping.json").read_text(encoding="utf-8"))
    assert mapping["level1"] == "level1_process_name" and mapping["stage"] == "level2_process_name"
    assert mapping["level3"] == "level3_process_name" and mapping["step_run"] == "abstraction_instance_id"
    assert mapping["active_time"] == "active_time"


def test_hierarchy_counts_add_up(sp_job):
    tree = sp_job["mining"]["subprocess"]["data"]["hierarchy"]
    assert [n["level"] for n in tree if n["depth"] == 1] == ["Process level 1"]
    assert {n["level"] for n in tree} == {"Process level 1", "Stage", "Process level 3", "Step"}
    top = next(n for n in tree if n["depth"] == 1)
    assert top["name"] == "Purchase to pay" and top["cases"] == 1500
    for parent in tree:
        children = [n for n in tree if n["depth"] == parent["depth"] + 1 and n["path"][:-1] == parent["path"]]
        if children:
            assert sum(c["events"] for c in children) == parent["events"], parent["path"]
            assert max(c["cases"] for c in children) <= parent["cases"]


def test_runs_from_run_id_equal_generator_runs(sp_job):
    con = connect()
    runs = con.execute(f"SELECT COUNT(DISTINCT (activity, run_id)) FROM "
                       f"read_parquet('{(sp_job['folder'] / sp.ACTIONS_FILE).as_posix()}')").fetchall()[0][0]
    truth = con.execute(f"SELECT COUNT(DISTINCT abstraction_instance_id) FROM "
                        f"read_parquet('{sp_job['src'].as_posix()}')").fetchall()[0][0]
    assert runs == truth


def test_runs_are_the_maps_runs(tmp_path):
    """A work sitting = a run of the map (consecutive events of the same step in a case; a step-run ID splits it
    further) split by person and by pauses over inside_step.gap_minutes. Events are unchanged."""
    t0 = pd.Timestamp("2024-03-01 09:00")
    rows = [("C1", "u1", "A", t0, "Open", "x1"), ("C1", "u1", "A", t0 + pd.Timedelta(seconds=60), "Save", "x1"),
            ("C1", "u1", "A", t0 + pd.Timedelta(hours=2), "Open", "x2"),                   # a pause: same run
            ("C1", "u1", "B", t0 + pd.Timedelta(hours=2, seconds=60), "Open", "x3"),      # another step
            ("C1", "u2", "A", t0 + pd.Timedelta(seconds=30), "Open", "x1"),               # another user: same run
            ("C2", "u1", "A", t0 + pd.Timedelta(seconds=90), "Open", "x4")]               # another case
    df = pd.DataFrame(rows, columns=["case_id", "resource", "activity", "ts_start", "__action", "__run"])
    df["application"], df["__active"] = None, None
    con = connect()
    con.register("tiny", df.astype({"application": object, "__run": object, "__active": float}))
    path = tmp_path / sp.ACTIONS_FILE

    def per_step(has_run_id):
        out = sp.capture_step_actions(con, "SELECT * FROM tiny", path, has_run_id=has_run_id)
        per = dict(con.execute(f"SELECT activity, COUNT(DISTINCT run_id) FROM read_parquet('{path.as_posix()}') "
                               "GROUP BY 1").fetchall())
        events = con.execute(f"SELECT SUM(repeats) FROM read_parquet('{path.as_posix()}')").fetchall()[0][0]
        return out["runs"], per, events
    # 01c: counted in WORK SITTINGS (a map run split by person and by pauses over 30 min), as Level 4 and
    # "Inside each step" count them. C1's A: u1 before / after the 2-hour pause and u2 = 3 sittings; C2 = 1.
    assert per_step(False) == (5, {"A": 4, "B": 1}, 6)
    assert per_step(True) == (5, {"A": 4, "B": 1}, 6)          # the step-run ID x2 split falls on the pause anyway


def test_same_step_actions_with_and_without_merging(sp_job, tmp_path, data_dir_of):
    mapping = json.loads((sp_job["folder"] / "mapping.json").read_text(encoding="utf-8"))
    assert mapping["merge_repeats"] is True
    con = connect()
    open_source(con, sp_job["src"], tmp_path)
    target = tmp_path / "unmerged"
    target.mkdir()
    normalise(con, {**mapping, "merge_repeats": False}, target / "events.parquet")
    q = "SELECT * FROM read_parquet('{}') ORDER BY ALL"
    before = con.execute(q.format((sp_job["folder"] / sp.ACTIONS_FILE).as_posix())).fetchall()
    after = con.execute(q.format((target / sp.ACTIONS_FILE).as_posix())).fetchall()
    assert before == after and len(before) > 1000


def test_standard_from_data_and_planted_class_shares(sp_job):
    d = detail(sp_job, "Create Request")
    assert d["standard"]["path"] == STANDARD and d["standard"]["source"] == sp.SOURCE_DATA
    shares = {c["class"]: c["share"] for c in d["comparison"]["classes"]}
    for cls, planted in PLANTED.items():
        assert abs(shares[cls] - planted) <= 0.03, (cls, shares[cls])
    assert d["comparison"]["top_missing"][0]["action"] == "Click Submit"
    assert d["comparison"]["top_extra"][0]["action"] == "Attach file"


def test_business_standard_wording(sp_job):
    runs = sp.runs_of(connect(), sp_job["folder"] / sp.ACTIONS_FILE, "Create Request")
    confirmed = sp.compare(runs, STANDARD, ["Click Submit"], confirmed=True)
    assert sp.MISSING_MANDATORY in [c["label"] for c in confirmed["classes"]]
    assert confirmed["wording"] == "Compared with the business standard"
    open_ = sp.compare(runs, STANDARD, ["Click Submit"], confirmed=False)
    assert "Missing steps" in [c["label"] for c in open_["classes"]]
    d = detail(sp_job, "Create Request")
    text = (json.dumps(open_, default=str) + json.dumps(d, default=str) + sp.describe_step(d)).lower()
    assert "incorrect" not in text and open_["wording"] == "Variation from the most common way"


def test_inside_step_order_strength_and_badges(sp_job):
    planted, shuffled = detail(sp_job, "Create Request"), detail(sp_job, "Pay Invoice")
    assert planted["order_strength"] >= 0.7 and planted["confidence"] == "High"
    assert shuffled["order_strength"] < 0.2 and shuffled["confidence"] == "Exploratory"


def test_rare_person_and_hex_actions_never_appear(sp_job):
    con = connect()
    path = sp_job["folder"] / sp.ACTIONS_FILE
    stored = {a for (a,) in con.execute(f"SELECT DISTINCT action FROM read_parquet('{path.as_posix()}')").fetchall()}
    # The file keeps every cleaned action (Level 4 groups them); the click view collapses rare ones.
    assert "Print preview" in stored and sp.OTHER_ACTION not in stored
    actions = {a for (a,) in con.execute(f"SELECT DISTINCT action FROM {sp.clicks(path)}").fetchall()}
    assert sp.OTHER_ACTION in actions and "Print preview" not in actions
    assert "Check data" in actions
    assert "#" not in json.dumps(sorted(stored)) and not any("Priya" in a for a in stored)
    everything = json.dumps(sorted(actions)) + json.dumps(sp_job["mining"]["subprocess"], default=str)
    assert "#" not in json.dumps(sorted(actions)) and "9f3c2a1b" not in everything and "Priya" not in everything


def test_standards_saved_by_signature_and_one_step_recomputed(sp_job, data_dir_of, monkeypatch):
    sig = reference.order_key(sp_job["job"])
    sp.save_standard(sig, "Create Request", STANDARD, ["Click Submit"], confirmed=False)
    sp.save_standard(sig, "Create Order", ["Open Create Order", "Enter details"], [], confirmed=True)
    saved = sp.load_standards(sig)
    assert saved["Create Request"]["standard_path"] == STANDARD and saved["Create Request"]["mandatory_actions"] == ["Click Submit"]
    assert saved["Create Order"]["confirmed_by_business"] is True
    assert sp.standards_path(sig).exists()

    from app.ui import subprocess_tab
    asked = []
    real = sp.runs_of
    monkeypatch.setattr(sp, "runs_of", lambda con, path, activity=None: asked.append(activity) or real(con, path, activity))
    actions = sp_job["folder"] / sp.ACTIONS_FILE
    comp = subprocess_tab._comparison(str(actions), actions.stat().st_mtime, "Create Order",
                                      json.dumps({"path": ["Open Create Order", "Enter details"], "confirmed": True}))
    assert asked == ["Create Order"] and comp["confirmed"]


def test_page3_subprocess_tab(sp_job, data_dir_of, tmp_path):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=180)
    at.session_state["job"] = sp_job["job"]
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    assert not at.exception, at.exception
    at.toggle(key="sp_show_clicks").set_value(True).run()       # Level 4 is the default view; clicks on request
    assert not at.exception, at.exception
    assert at.selectbox(key="sp_step").value
    at.selectbox(key="sp_stage").set_value("Invoicing").run()
    assert not at.exception, at.exception
    at.selectbox(key="sp_step").set_value("Pay Invoice").run()
    at.radio(key="sp_map_mode").set_value("Time").run()
    assert not at.exception, at.exception
    assert any("Variation from the most common way" in m.value or "Compared with" in m.value for m in at.markdown)

    # Without an action column the tab says why.
    plain = tmp_path / "plain.parquet"
    generate(["--cases", "300", "--start", "2024-01-01", "--months", "6", "--out", str(plain)])
    assert pipeline(["--input", str(plain), "--auto-map", "--no-ai"]) == 0
    job = jobs.list_jobs()[0]
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=180)
    at.session_state["job"] = job
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    assert not at.exception, at.exception
    assert any("No Action / UI event column was mapped" in i.value for i in at.info)


def test_report_excel_and_facts(sp_job):
    folder = sp_job["folder"]
    html = (folder / "report.html").read_text(encoding="utf-8")
    # The optimised report drops the "How each step is done" table (standard-way columns); the step table stays.
    assert "How each step is done" not in html and "Follow it" not in html
    assert '<table class="steps">' in html and "Create Request" in html
    facts = [f for f in json.loads((folder / "facts.json").read_text(encoding="utf-8")) if f["type"].startswith("step_")]
    assert {"step_runs", "step_standard_share", "step_standard_source", "step_order_strength"} <= {f["type"] for f in facts}
    text = json.dumps(facts)
    resources = {r for (r,) in connect().execute(
        f"SELECT DISTINCT resource FROM read_parquet('{sp_job['src'].as_posix()}')").fetchall()}
    assert not any(r in text for r in resources)
    assert "9f3c2a1b" not in text and "Priya" not in text
    shuffled = next(f for f in facts if f["type"] == "step_order_strength" and f["meta"]["activity"] == "Pay Invoice")
    assert shuffled["badge"] == "Exploratory"


def test_validator_rejects_user_names_and_incorrect():
    def step_fact(confirmed: bool) -> Fact:
        return Fact(id="F001", type="step_standard_share", label="Create Request: runs that follow the most common way",
                    value=0.6, unit="share", support=1500, badge="High", section="map",
                    meta={"activity": "Create Request", "confidence": "High", "standard_confirmed": confirmed})

    def answer(text: str) -> dict:
        return {"summary": [{"text": text, "facts": ["F001"]}], "findings": [], "recommendations": []}

    by_id = {"F001": step_fact(False)}
    bad = narrative.validate(answer("Runs of Create Request are done incorrectly in {{F001}}."), {"F001"}, by_id)
    assert any("incorrect" in e for e in bad)
    ok = narrative.validate(answer("{{F001}} of Create Request runs follow the most common way."), {"F001"}, by_id)
    assert ok == []
    named = narrative.validate(answer("clerk_7 follows the most common way in {{F001}}."), {"F001"}, by_id,
                               avoid_names=["clerk_7"])
    assert any("individual user" in e for e in named)
    confirmed = {"F001": step_fact(True)}
    assert narrative.validate(answer("Runs without the business standard are incorrect in {{F001}}."),
                              {"F001"}, confirmed) == []


def test_definition_standard_claims_and_map_line(sp_job, data_dir_of):
    from app.core import definition as dmod
    from app.core.business_map import to_business_dot
    from app.core.evidence import check_definition
    sig = reference.order_key(sp_job["job"])
    sp.save_standard(sig, "Create Request", STANDARD, ["Open Create Request"], confirmed=False)
    sp.save_standard(sig, "Pay Invoice", ["Open Pay Invoice", "Enter details", "Check data", "Click Submit"], [])
    defn = dmod.ProcessDefinition.from_dict({"name": "Sub-process check", "stages": [{"id": "A", "name": "All"}],
        "steps": [{"id": "S1", "name": "Create request", "stage_id": "A", "position": 1,
                   "mapped_activities": ["Create Request"], "standard_path_ref": "Create Request"},
                  {"id": "S2", "name": "Pay invoice", "stage_id": "A", "position": 2,
                   "mapped_activities": ["Pay Invoice"], "standard_path_ref": "Pay Invoice"}]})
    ev = check_definition(defn, sp_job["job"])
    std = {c["refs"]["step_id"]: c for c in ev["claims"] if c["kind"] == "standard"}
    assert std["S2"]["verdict"] == "Contradicted"                       # the shuffled step
    assert abs(std["S1"]["value"] - 0.6) <= 0.03
    action = next(c for c in ev["claims"] if c["kind"] == "mandatory_action")
    assert action["verdict"] == "Supported" and action["refs"]["action"] == "Open Create Request"
    dot = to_business_dot(defn, ev["claims"], ev["stats"], step_stats=ev["step_stats"])
    assert "Standard way followed in" in dot

    defn.status = "approved"
    assert dmod.confirm_linked_standards(sp_job["job"], defn) == ["Create Request", "Pay Invoice"]
    assert sp.load_standards(sig)["Create Request"]["confirmed_by_business"] is True
