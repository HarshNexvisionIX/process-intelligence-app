"""PM4Py part 2: precision, deviations, the approved process, timeouts and the report appendix.

The rest of the suite runs with PM_DISABLE_PM4PY=1 (conftest.py); these tests switch the model on.
Jobs are written to a temporary data folder (DATA_DIR), never to data/.
"""
import json
import random

import pandas as pd
import pytest

from app.core import jobs, metrics, pm4py_model
from app.core.config import PROJECT_ROOT, settings
from test_golden import MAPPING, confirmed_order, make_job
from test_pm4py_model import golden_job

pytest.importorskip("pm4py")

PLAIN = {**MAPPING, "resource": None, "application": None, "stage": None, "active_time": None, "wait_time": None,
         "tat_time": None, "action": None, "window_title": None, "scope": {}}


@pytest.fixture
def model_on(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PM_DISABLE_PM4PY", raising=False)
    monkeypatch.setitem(settings()["pm4py"], "enabled", True)
    monkeypatch.setitem(settings()["pm4py"], "build_on_run", True)      # these tests build with the analysis
    return tmp_path


def log_job(tmp, paths: list[list[str]], name: str):
    rows = []
    for c, p in enumerate(paths):
        t = pd.Timestamp("2026-01-05 08:00") + pd.Timedelta(hours=c)
        rows += [(f"K{c:04d}", a, (t + pd.Timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S"))
                 for i, a in enumerate(p)]
    src = tmp / f"{name}.csv"
    pd.DataFrame(rows, columns=["case_id", "activity", "timestamp"]).to_csv(src, index=False)
    job = make_job(tmp, src, PLAIN)
    return job, jobs.job_folder(job["job_id"])


def test_xor_found_with_high_fitness_precision_and_the_skipped_step(model_on, monkeypatch):
    monkeypatch.setitem(settings()["pm4py"], "alignment_max_cases", 1000)     # every case is aligned
    rnd = random.Random(7)
    paths = [["A", "B", rnd.choice("CD"), "E"] for _ in range(400)]
    paths += [["A", rnd.choice("CD"), "E"] for _ in range(100)]                  # B skipped in 20% of cases
    rnd.shuffle(paths)
    _job, folder = log_job(model_on, paths, "xor_skip")
    d = json.loads((folder / "mining.json").read_text(encoding="utf-8"))["process_model"]["data"]
    tree = d["tree"].replace("X( 'D', 'C' )", "X( 'C', 'D' )")
    assert "X( 'C', 'D' )" in tree and d["gateways"]["exclusive"] >= 2           # the XOR is found
    conf = pm4py_model.load_json(folder, pm4py_model.CONFORMANCE_FILE)
    assert conf["fitness"] >= 0.95 and conf["precision"] >= 0.8
    assert sum(h["cases"] for h in conf["histogram"]) == 500
    top = conf["deviations"]["skipped"][0]
    assert top["step"] == "B" and abs(top["share"] - 0.2) <= 0.02 and conf["method"] == "alignments"
    assert conf["deviations"]["sampled_cases"] == 500


def test_approved_sequence_finds_out_of_order_cases(model_on, monkeypatch):
    from app.core.definition import ProcessDefinition
    monkeypatch.setenv("PM_DISABLE_PM4PY", "1")                     # build the job without the discovered model
    rnd = random.Random(11)
    paths = [["A", "B", "D", "C", "E"] if rnd.random() < 0.3 else ["A", "B", "C", "D", "E"] for _ in range(400)]
    paths += [["A", "B", "C", "D", "E", "Z"]] * 20                 # Z: a step the definition does not have
    job, folder = log_job(model_on, paths, "seq")
    monkeypatch.delenv("PM_DISABLE_PM4PY")
    monkeypatch.setitem(settings()["pm4py"], "alignment_max_cases", 1000)    # align every case ...
    monkeypatch.setitem(settings()["pm4py"], "timeout_s", 600)               # ... even on a busy machine
    defn = ProcessDefinition.from_dict({"name": "Seq", "status": "approved", "steps": [
        {"id": f"S{i}", "name": f"Step {a}", "position": i, "mandatory": True, "mapped_activities": [a]}
        for i, a in enumerate("ABCDE", 1)]})
    res = pm4py_model.check_definition(job, defn)
    assert res["available"], res.get("reason")
    out_of_order = {x["step"]: x["out_of_order_share"] for x in res["steps"]}
    swapped = sum(1 for p in paths if p[2] == "D") / len(paths)
    assert abs(max(out_of_order["Step C"], out_of_order["Step D"]) - swapped) <= 0.03
    assert abs(res["fitting_cases_share"] - (1 - swapped)) <= 0.03 and res["fitness"] < 1
    assert any("plain sequence" in n for n in res["notes"])
    assert res["not_in_definition"] == [{"activity": "Z", "runs": 20, "cases": 20}]
    assert (folder / pm4py_model.DEFINITION_FILE).exists()
    net, _im, _fm = pm4py_model.definition_to_petri(defn)
    assert {t.label for t in net.transitions if t.label} == {f"Step {a}" for a in "ABCDE"}


def test_definition_blocks_read_optional_parallel_and_loops():
    from app.core.definition import ProcessDefinition
    defn = ProcessDefinition.from_dict({"name": "P", "steps": [
        {"id": "a", "name": "A", "position": 1}, {"id": "b", "name": "B", "position": 2, "mandatory": False},
        {"id": "c", "name": "C", "position": 3}, {"id": "d", "name": "D", "position": 3},
        {"id": "e", "name": "E", "position": 4}],
        "flows": [{"from_step": "e", "to_step": "e", "kind": "allowed_loop"}]})
    blocks, notes = pm4py_model.definition_blocks(defn)
    assert blocks[1] == {"kind": "step", "name": "B", "optional": True, "loop": False}
    assert blocks[2]["kind"] == "and" and [s["name"] for s in blocks[2]["steps"]] == ["C", "D"]
    assert blocks[3]["loop"] is True and any("any order" in n for n in notes)


def test_timeout_keeps_page_and_report_working(model_on, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from app.core.report import generate_report
    monkeypatch.setitem(settings()["pm4py"], "timeout_s", 0.01)
    job, folder = golden_job(model_on)
    sec = json.loads((folder / "mining.json").read_text(encoding="utf-8"))["process_model"]
    assert not sec["available"] and sec["reason"].startswith("PM4Py model not built in time")
    html = generate_report(job["job_id"], use_ai=False)["html"].read_text(encoding="utf-8")
    assert "Formal model (PM4Py)" not in html and "14. Appendix" in html
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.session_state["job"] = jobs.load_job(job["job_id"])
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    at.radio(key="map_view").set_value("Discovered model (PM4Py)").run()
    assert not at.exception, at.exception
    assert any("not built in time" in i.value for i in at.info)


def test_build_on_run_false_and_build_button(model_on, monkeypatch):
    monkeypatch.setitem(settings()["pm4py"], "build_on_run", False)
    job, folder = golden_job(model_on)
    sec = json.loads((folder / "mining.json").read_text(encoding="utf-8"))["process_model"]
    assert not sec["available"] and "Build model" in sec["reason"]
    assert pm4py_model.build(folder)["available"]
    assert json.loads((folder / "mining.json").read_text(encoding="utf-8"))["process_model"]["available"]


def _snapshot(folder):
    mining = json.loads((folder / "mining.json").read_text(encoding="utf-8"))
    for k in ("process_model", "created_at", "seconds"):
        mining.pop(k, None)
    facts = [(f["id"], f["type"], f["label"], f["value"]) for f in json.loads((folder / "facts.json").read_text(
        encoding="utf-8")) if f["section"] != "pm4py"]
    checks = [(c["number"], c["check"], c["report"], c["passed"]) for c in json.loads(
        (folder / "checks.json").read_text(encoding="utf-8"))["checks"] if c["number"] != 15]
    return mining, metrics.load(folder), facts, checks


def test_existing_numbers_identical_with_and_without_pm4py_and_the_appendix(model_on, monkeypatch):
    from app.core.db import connect
    from app.core.mining import run_mining, save_mining
    from app.core.report import generate_report
    monkeypatch.setenv("PM_DISABLE_PM4PY", "1")
    job, folder = golden_job(model_on)
    html = generate_report(job["job_id"], use_ai=False, order=confirmed_order())["html"].read_text(encoding="utf-8")
    assert "Formal model (PM4Py)" not in html                                  # no model, no appendix
    before = _snapshot(folder)
    monkeypatch.delenv("PM_DISABLE_PM4PY")
    save_mining(run_mining(connect(), folder / "events.parquet", "High", use_ai=False), folder)
    paths = generate_report(job["job_id"], use_ai=False, order=confirmed_order())
    after = _snapshot(folder)
    for got, want, what in zip(after, before, ("mining.json", "metrics.json", "facts", "checks")):
        assert got == want, what
    html = paths["html"].read_text(encoding="utf-8")
    assert "Formal model (PM4Py)" in html and "Formal model: fitness (token replay)" in html
    checks = json.loads(paths["checks"].read_text(encoding="utf-8"))
    pm = [c for c in checks["checks"] if c["number"] == 15]
    assert pm and all(c["passed"] for c in pm) and checks["failed"] == 0
    facts = [f for f in json.loads((folder / "facts.json").read_text(encoding="utf-8")) if f["section"] == "pm4py"]
    assert {"pm4py_fitness", "pm4py_precision", "pm4py_fitting_share"} <= {f["type"] for f in facts}
    narrative = json.loads((folder / "narrative.json").read_text(encoding="utf-8"))
    pm_ids = {f["id"] for f in facts}
    assert not any(fid in pm_ids for x in narrative["summary"] + narrative["recommendations"] for fid in x["facts"])


def test_slow_precision_or_alignment_never_loses_the_model(model_on, monkeypatch):
    real = pm4py_model.call

    def slow(task, folder, timeout=None):
        if task["task"] in ("precision", "align"):
            raise pm4py_model.PM4PyTimeout(f"{task['task']} took too long")
        return real(task, folder, timeout)
    monkeypatch.setattr(pm4py_model, "call", slow)
    _job, folder = golden_job(model_on)
    sec = json.loads((folder / "mining.json").read_text(encoding="utf-8"))["process_model"]
    assert sec["available"] and sec["data"]["precision"] is None                  # the model and fitness stay
    conf = pm4py_model.load_json(folder, pm4py_model.CONFORMANCE_FILE)
    assert conf["fitness"] > 0 and conf["precision_note"].startswith("not computed in time (pm4py.timeout_s = 120")
    assert conf["method"].startswith("token replay") and "token_problems" in conf["deviations"]


def test_no_fixed_order_is_said_plainly_and_skips_alignments(model_on):
    rnd = random.Random(2)
    steps = list("ABCDEFG")
    paths = []
    for _ in range(300):
        p = steps[:]
        rnd.shuffle(p)
        paths.append(p[:rnd.randint(3, 7)])
    _job, folder = log_job(model_on, paths, "any_order")
    conf = pm4py_model.load_json(folder, pm4py_model.CONFORMANCE_FILE)
    assert conf["any_order"] is True and conf["method"].startswith("token replay")  # 01c
    assert "No stable order found" in conf["structure_note"] and conf["deviations"]["sampled_cases"] > 0
    assert "token_problems" in conf["deviations"]            # 01c: token replay listed per step
    assert conf["show_model"] is False


def test_check_against_the_step_order_page_3_uses(model_on, monkeypatch):
    from app.core import reference
    monkeypatch.setenv("PM_DISABLE_PM4PY", "1")
    job, folder = golden_job(model_on)
    monkeypatch.delenv("PM_DISABLE_PM4PY")
    reference.save_order(reference.order_key(job), confirmed_order())        # confirmed: the order everything uses
    res = pm4py_model.check_order(jobs.load_job(job["job_id"]))
    assert res["available"], res.get("reason")
    assert res["order_badge"] == "Confirmed by business" and res["cases_used"] == 6
    assert [x["step"] for x in res["steps"]] == ["Receive Request", "Check Request", "Approve", "Notify"]
    assert res["fitting_cases_share"] < 1                                    # C5 does Approve before Check Request
    late = {x["step"]: x["out_of_order_share"] for x in res["steps"]}
    assert max(late["Check Request"], late["Approve"]) > 0
    assert (folder / pm4py_model.ORDER_FILE).exists()
    assert pm4py_model.build(folder)["available"]               # the appendix appears only with a built model
    from app.core.report import generate_report
    html = generate_report(job["job_id"], use_ai=False)["html"].read_text(encoding="utf-8")
    assert "Cases checked against the step order with PM4Py" in html
    checks = json.loads((folder / "checks.json").read_text(encoding="utf-8"))
    assert checks["failed"] == 0 and any(c["check"].startswith("Step order: fitness") for c in checks["checks"])
