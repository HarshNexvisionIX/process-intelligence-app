"""Prompt 01b: the order from evidence (no template), sharper Level 4, inside each step, and the follow-up
(no empty Level 3 model). Jobs go to a temporary DATA_DIR; fake / no AI; PM4Py only where the test says.
"""
import json
import random
import shutil

import pandas as pd
import pytest

from app.core import definition as dmod
from app.core import inside_step, jobs, knowledge, level4, metrics, order_evidence, pm4py_model, reference
from app.core.config import PROJECT_ROOT, settings
from app.core.db import connect
from test_audit_fixes import HR, hr_job
from test_golden import GOLDEN, MAPPING, A, B, C, D, confirmed_order, make_job

SEQ = {**MAPPING, "stage": None, "wait_time": None, "tat_time": None, "window_title": None, "scope": {}}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def defn_for(job: dict, steps: list[str], source: str = "", status: str = "draft") -> dmod.ProcessDefinition:
    d = dmod.ProcessDefinition.from_dict({"name": f"Def {job['job_id']}", "status": status, "source": source, "steps": [
        {"id": f"S{i}", "name": a, "position": i, "mapped_activities": [a]} for i, a in enumerate(steps, 1)]})
    dmod.save(d)
    dmod.link_job(job, d)
    return d


def sop(name: str, text: str, kind: str = "sop", file: str = "sop.txt") -> dict:
    src, _ = knowledge.add_source(name, kind, file, text.encode())
    return src


def say(name: str, src: dict, defn, a: str, b: str, quote: str) -> None:
    knowledge.add_manual_statement(name, src["id"], {"type": "order", "subject": a, "object": b, "quote": quote,
                                                     "anchor": "p.1"}, defn)


# --------------------------------------------------------------------------- #
# 1. Template off
# --------------------------------------------------------------------------- #
def test_template_is_never_an_order_option_and_old_choices_fall_back(data_dir):
    from app.core.report import generate_report
    job = hr_job(data_dir)
    folder = jobs.job_folder(job["job_id"])
    generate_report(job["job_id"], use_ai=False)
    ref = json.loads((folder / "reference.json").read_text(encoding="utf-8"))
    step_events = {r["step"]: r["events"] for r in ref["stats"]["steps"] + ref["stats"]["other"]}
    opts = reference.order_options(reference.order_key(job), reference.Order.from_dict(ref["suggested"]), step_events,
                                   "HR")
    assert "template" not in {o["key"] for o in opts} and ref["choice"] != "template"
    job["order_choice"] = "template"
    jobs.save_job(job)
    assert reference.resolve(opts, job)["key"] == opts[0]["key"] != "template"
    assert reference.notice(opts, job) == reference.TEMPLATE_OFF_NOTICE
    # a saved order that came from the template: shown as such, never confirmed
    t = reference.Order("HR", [{"name": "", "steps": HR}], [], "template", True, template_name="HR onboarding")
    reference.save_order(reference.order_key(job), t)
    opts = reference.order_options(reference.order_key(job), reference.Order.from_dict(ref["suggested"]), step_events, "HR")
    assert opts[0]["key"] == "saved" and opts[0]["label"] == reference.TEMPLATE_SAVED
    assert not opts[0]["order"].confirmed_by_business


def test_template_drafted_definition_gets_the_badge_in_the_report(data_dir):
    from app.core.evidence import run_check
    from app.core.report import generate_report
    job = hr_job(data_dir)
    d = defn_for(job, HR, source="Template: HR onboarding", status="approved")
    run_check(job, d)
    html = generate_report(job["job_id"], use_ai=False)["html"].read_text(encoding="utf-8")
    assert "drafted from template — please re-check" in html


# --------------------------------------------------------------------------- #
# 2. Order from evidence
# --------------------------------------------------------------------------- #
def evidence_job(tmp, before_ab: float, before_bc: float, n: int = 200, edge_swaps: int = 0):
    """n cases A,B,C over 40 days (plus edge_swaps swapped cases in the first 3 days); shares of A<B and B<C."""
    rnd = random.Random(4)
    rows, c, planted = [], 0, {"ab": 0, "bc": 0}
    for i in range(n):
        t = pd.Timestamp("2026-03-01") + pd.Timedelta(days=8 + 24 * i / n, hours=9)
        ab = ["A", "B"] if rnd.random() < before_ab else ["B", "A"]
        path = ab + ["C"] if rnd.random() < before_bc else ["C"] + ab
        planted["ab"] += path.index("A") < path.index("B")
        planted["bc"] += path.index("B") < path.index("C")
        rows += [(f"E{c}", a, (t + pd.Timedelta(minutes=10 * k)).strftime("%Y-%m-%d %H:%M:%S")) for k, a in enumerate(path)]
        c += 1
    for i in range(edge_swaps):                                  # cut off at the data's start: B before A
        t = pd.Timestamp("2026-03-01") + pd.Timedelta(days=1, hours=i)
        rows += [(f"E{c}", a, (t + pd.Timedelta(minutes=10 * k)).strftime("%Y-%m-%d %H:%M:%S")) for k, a in enumerate("BAC")]
        c += 1
    rows += [("Z0", "A", "2026-04-20 09:00:00"), ("Z0", "B", "2026-04-20 09:10:00")]   # fixes the data's end
    src = tmp / "ev.csv"
    pd.DataFrame(rows, columns=["case_id", "activity", "timestamp"]).to_csv(src, index=False)
    m = {**SEQ, "resource": None, "application": None, "active_time": None, "action": None}
    job = make_job(tmp, src, m)
    return job, jobs.job_folder(job["job_id"]), {k: v / n for k, v in planted.items()}


def test_documented_order_checked_against_the_data(data_dir):
    job, folder, planted = evidence_job(data_dir, 0.8, 0.3, edge_swaps=40)
    d = defn_for(job, ["A", "B", "C"])
    s = sop(d.name, "Step A comes before step B. Then B comes before C in every case.")
    say(d.name, s, d, "A", "B", "Step A comes before step B")
    say(d.name, s, d, "B", "C", "B comes before C")
    ev = order_evidence.compute(job, d)
    assert ev["documented"] and ev["order"] == ["A", "B", "C"]
    assert [x["provenance"] for x in ev["steps"]] == ["document"] * 3 and not ev["not_stated"]
    by = {(p["a"], p["b"]): p for p in ev["pairs"]}
    assert by[("A", "B")]["verdict"] == "supports" and abs(by[("A", "B")]["share"] - planted["ab"]) <= 0.005
    assert by[("B", "C")]["verdict"] == "contradicts" and abs(by[("B", "C")]["share"] - planted["bc"]) <= 0.005
    assert ev["cases_used"] == 200 and by[("A", "B")]["cases"] == 200     # the 40 edge cases are left out
    assert by[("A", "B")]["quotes"][0]["quote"] == "Step A comes before step B"
    assert order_evidence.load(folder)["label"].startswith("Proposed order (from your documents")
    draft = order_evidence.to_definition(ev, "P", d)
    assert [x["name"] for x in draft.ordered_steps()] == ["A", "B", "C"] and draft.status == "draft"
    assert draft.steps[0]["position_source"] == "document"


def test_few_cases_are_unclear_and_no_documents_means_data(data_dir):
    job, folder, _ = evidence_job(data_dir, 0.9, 0.9, n=20)
    d = defn_for(job, ["A", "B", "C"])
    s = sop(d.name, "A comes before B.")
    say(d.name, s, d, "A", "B", "A comes before B")
    ev = order_evidence.compute(job, d)
    assert ev["pairs"][0]["verdict"] == "unclear" and ev["pairs"][0]["cases"] < 30
    ev2 = order_evidence.compute(job, None)
    assert not ev2["documented"] and ev2["label"] == order_evidence.NO_DOCS and ev2["source"] in ("data", "milestones")
    assert all(x["note"] == order_evidence.NOT_STATED for x in ev2["steps"])


def test_conflicting_statements_become_a_question_never_resolved(data_dir):
    job, folder, _ = evidence_job(data_dir, 0.8, 0.8)
    d = defn_for(job, ["A", "B", "C"])
    s1 = sop(d.name, "A comes before B.", file="one.txt")
    s2 = sop(d.name, "B comes before A.", file="two.txt")
    say(d.name, s1, d, "A", "B", "A comes before B")
    say(d.name, s2, d, "B", "A", "B comes before A")
    ev = order_evidence.compute(job, d)
    conflict = [p for p in ev["pairs"] if p["kind"] == "conflict"]
    assert conflict and conflict[0]["status"] == "open" and ev["conflicts"][0]["status"] == "open"
    assert not any(p["kind"] == "statement" for p in ev["pairs"])      # the conflicting pair is not an edge
    merged = knowledge.merge(knowledge.all_statements(d.name))
    qs = knowledge.questions(d, merged, None)
    assert any(q["kind"] == "conflict" for q in qs)


# --------------------------------------------------------------------------- #
# 3. Level 4
# --------------------------------------------------------------------------- #
def units(*rows):
    return [{"activity": "Step", "application": app, "screen": scr, "action": act, "events": n, "users": 3}
            for app, scr, act, n in rows]


def test_navigation_clicks_document_names_locks_and_stability():
    us = units(("Workday", "Offer screen", "Enter offer details", 100), ("Workday", "Offer screen", "Select Search", 30),
               ("Workday", "Home", "View List", 20), ("Workday", "Home", "Scroll", 10),
               ("Excel", "Tracker.xlsx", "Paste values", 40), ("Excel", "Tracker.xlsx", "Paste values again", 5))
    entry = {"substeps": [{"id": "S1", "label": "Enter the offer details", "description": "", "members": [],
                           "rules": [{"application": "Workday", "screen_regex": "Offer", "click_regex": "Enter"}]},
                          {"id": "S2", "label": "Paste values to tracker", "description": "", "members": [],
                           "rules": [{"application": "Excel", "click_regex": "^Paste values$"}]}]}
    docs = ["Enter offer details", "Update the tracker"]      # "Enter the offer details" fits the first (word overlap)
    out = level4.refine(entry, us, docs)
    labels = {s["label"]: s for s in out["substeps"]}
    lab = level4.assigner(out)
    assert lab(us[1]) == "Enter offer details"               # a navigation click on a specific screen
    assert lab(us[2]) == lab(us[3]) == level4.NAVIGATE        # generic navigation
    assert lab(us[5]) == lab(us[4])                           # the tail joins by word overlap
    assert labels["Enter offer details"]["named_from"] == "document"
    assert labels["Paste values to tracker"]["named_from"] == "ai"          # no document name fits it
    assert labels[level4.NAVIGATE]["named_from"] == "rule" and not out["review_queue"]
    assert sum(u["events"] for u in us if lab(u) != level4.OTHER) == sum(u["events"] for u in us)   # nothing dropped
    assert level4.assignment_hash(out, us) == level4.assignment_hash(level4.refine(entry, us, docs), us)
    locked = level4.sme_lock(out, "Enter offer details", True, "Ana")
    rebuilt = level4.keep_locked(locked, {"substeps": [{"id": "S1", "label": "Enter offer details", "rules": [],
                                                        "members": [], "description": "new"},
                                                       {"id": "S2", "label": "Other work", "rules": [], "members": []}]})
    kept = next(s for s in rebuilt["substeps"] if s["label"] == "Enter offer details")
    assert kept.get("locked") and kept["rules"] and locked["source"] == "sme" and locked["sme_by"] == "Ana"
    assert level4.is_navigation("Select Search") and not level4.is_navigation("Enter offer details")


def test_level4_totals_unchanged_and_quality_file(data_dir):
    src = data_dir / "g.csv"
    shutil.copy(GOLDEN / "golden_events.csv", src)
    job = make_job(data_dir, src)
    folder = jobs.job_folder(job["job_id"])
    q = json.loads((folder / level4.QUALITY_FILE).read_text(encoding="utf-8"))
    assert set(q) == {A, B, C, D} and all("flags" in v and "assignment_hash" in v for v in q.values())
    mining = json.loads((folder / "mining.json").read_text(encoding="utf-8"))
    m = metrics.load(folder) or metrics.compute(connect(), folder)
    by = {r["step"]: r for r in m["steps"]}
    for d in mining["level4"]["data"]["l3s"]:
        assert d["events"] == by[d["l3"]]["events"] and abs(d["active_h"] - by[d["l3"]]["active_hours"]) < 1e-6
    assert q[A]["stability"] == "first run"
    from app.core.mining import run_mining, save_mining
    save_mining(run_mining(connect(), folder / "events.parquet", "High", use_ai=False), folder)
    q2 = json.loads((folder / level4.QUALITY_FILE).read_text(encoding="utf-8"))
    assert q2[A]["stability"] == "stable" and q2[A]["assignment_hash"] == q[A]["assignment_hash"]


# --------------------------------------------------------------------------- #
# 4. Inside each step
# --------------------------------------------------------------------------- #
def step_rows(n: int = 100, slow_user: bool = True) -> pd.DataFrame:
    """Runs A→B→C (70%) and A→C (30%), one run with rework (A→B→A→C), u3 slow."""
    rnd = random.Random(9)
    rows, t = [], pd.Timestamp("2026-03-02 09:00")
    for i in range(n):
        user = ["u1", "u2", "u3"][i % 3]
        path = ["A", "B", "C"] if i < int(0.7 * n) else ["A", "C"]
        if i == 0:
            path = ["A", "B", "A", "C"]
        secs = 300 if user == "u3" and slow_user else 60
        for k, a in enumerate(path):
            rows.append((f"run{i}", f"K{i}", user, "Step", "Workday", "Screen", a, t + pd.Timedelta(hours=2 * i, minutes=k),
                         secs, 1))
    return pd.DataFrame(rows, columns=["run_id", "case_id", "resource", "activity", "application", "screen", "action",
                                       "ts", "active_seconds", "repeats"])


ENTRY = {"substeps": [{"id": f"S{i}", "label": lab, "description": "", "members": [],
                       "rules": [{"application": "*", "click_regex": f"^{a}$"}]}
                      for i, (a, lab) in enumerate((("A", "Enter data"), ("B", "Check data"), ("C", "Save record")), 1)]}


def test_inside_step_patterns_rework_slow_user_and_masking(monkeypatch):
    d = inside_step.analyse_step(step_rows(), ENTRY, "Step")
    assert d["enough"] and d["runs"] == 100 and d["users"] == 3
    pats = {" → ".join(p["path"]): p["share"] for p in d["patterns"]}
    assert abs(pats["Enter data → Check data → Save record"] - 0.69) <= 0.01    # 70 runs minus the rework run
    assert abs(pats["Enter data → Save record"] - 0.30) <= 0.01
    assert d["rework"]["runs"] == 1 and d["rework"]["by_sub_step"][0]["sub_step"] == "Enter data"
    slow = [p for p in d["people"] if "slower" in " ".join(p["flags"])]
    assert len(slow) == 1 and slow[0]["user"].startswith("P") and d["variation_flag"]
    assert not any(u in json.dumps(d) for u in ("u1", "u2", "u3"))
    assert d["start"]["sub_step"] == "Enter data" and d["end"]["sub_step"] == "Save record"
    assert {s["sub_step"]: s["work_type"] for s in d["time_split"]}["Enter data"] == "entry"
    assert d["automation_candidates"] and "runs" in d["automation_candidates"][0]
    small = inside_step.analyse_step(step_rows(6), ENTRY, "Step")
    assert not small["enough"] and "not enough runs" in small["reason"]


def inside_job(tmp, skip_b: float = 0.0, n_cases: int = 60):
    rnd = random.Random(3)
    rows, skipped = [], 0
    for c in range(n_cases):
        t = pd.Timestamp("2026-03-02 09:00") + pd.Timedelta(hours=12 * c)      # 30 days: fully observed cases exist
        clicks = ["Enter data", "Check data", "Save record"] if rnd.random() >= skip_b else ["Enter data", "Save record"]
        skipped += len(clicks) == 2
        for k, a in enumerate(clicks):
            rows.append((f"W{c}", "Work", (t + pd.Timedelta(minutes=k)).strftime("%Y-%m-%d %H:%M:%S"), f"u{c % 2}",
                         "Workday", 60, a))
        rows.append((f"W{c}", "Close", (t + pd.Timedelta(minutes=9)).strftime("%Y-%m-%d %H:%M:%S"), f"u{c % 2}",
                     "Workday", 30, "Close case"))
    src = tmp / "inside.csv"
    pd.DataFrame(rows, columns=["case_id", "activity", "timestamp", "user", "application", "active_time", "action"]).to_csv(src, index=False)
    job = make_job(tmp, src, SEQ)
    return job, jobs.job_folder(job["job_id"]), skipped / n_cases


NAMED = {"source": "edited", "substeps": [{"id": f"S{i}", "label": lab, "description": "", "members": [],
                                          "rules": [{"application": "*", "click_regex": f"^{lab}$"}]}
                                         for i, lab in enumerate(("Enter data", "Check data", "Save record"), 1)]}


@pytest.mark.skipif(pytest.importorskip("pm4py") is None, reason="PM4Py")
def test_inside_step_pm4py_model_timeout_and_documented_flow(data_dir, monkeypatch):
    monkeypatch.delenv("PM_DISABLE_PM4PY", raising=False)
    monkeypatch.setitem(settings()["pm4py"], "enabled", True)
    job, folder, planted_skip = inside_job(data_dir, skip_b=0.2)
    data = inside_step.load(folder)
    assert data["steps"]["Work"]["enough"]
    level4.save_entry(reference.order_key(job), "Work", NAMED)                            # named sub-steps
    monkeypatch.setattr(level4, "doc_names", lambda job_id, l3: ["Enter data", "Check data", "Save record"] if l3 == "Work" else [])
    from app.core.mining import run_mining, save_mining
    save_mining(run_mining(connect(), folder / "events.parquet", "High", use_ai=False), folder)
    res = inside_step.build_models(jobs.load_job(job["job_id"]), ["Work"])
    pm = res["steps"]["Work"]["pm4py"]
    assert pm["available"] and pm["precision"] >= 0.8 and pm["fitness"] >= 0.9
    doc = pm["documented"]
    skipped = {s["step"]: s["skipped_share"] for s in doc["steps"]}
    assert doc["status"] == "documented" and abs(skipped["Check data"] - planted_skip) <= 0.01
    monkeypatch.setitem(settings()["pm4py"], "timeout_s", 0.01)
    res = inside_step.build_models(jobs.load_job(job["job_id"]), ["Work"])
    assert not res["steps"]["Work"]["pm4py"]["available"] and "not built in time" in res["steps"]["Work"]["pm4py"]["reason"]


# --------------------------------------------------------------------------- #
# 5. Report, checks, unchanged outputs
# --------------------------------------------------------------------------- #
def _snapshot(folder):
    mining = json.loads((folder / "mining.json").read_text(encoding="utf-8"))
    for k in ("inside", "process_model", "created_at", "seconds"):
        mining.pop(k, None)
    facts = [(f["id"], f["type"], f["label"], f["value"]) for f in json.loads((folder / "facts.json").read_text(encoding="utf-8"))
             if f["section"] not in ("evidence", "inside", "pm4py")]
    checks = [(c["number"], c["check"], c["report"], c["passed"]) for c in json.loads(
        (folder / "checks.json").read_text(encoding="utf-8"))["checks"] if c["number"] not in (15, 16, 17)]
    return mining, metrics.load(folder), facts, checks


def test_report_sections_checks_and_unchanged_outputs(data_dir, monkeypatch):
    from app.core.mining import run_mining, save_mining
    from app.core.report import generate_report
    job, folder, _ = inside_job(data_dir)
    level4.save_entry(reference.order_key(job), "Work", NAMED)
    save_mining(run_mining(connect(), folder / "events.parquet", "High", use_ai=False), folder)
    d = defn_for(job, ["Work", "Close"])
    s = sop(d.name, "Work comes before Close.")
    say(d.name, s, d, "Work", "Close", "Work comes before Close")
    paths = generate_report(job["job_id"], use_ai=False, order=reference.Order("P", [{"name": "", "steps": ["Work", "Close"]}],
                                                                                [], "edited", True))
    before = _snapshot(folder)
    order_evidence.compute(job, d)
    paths = generate_report(job["job_id"], use_ai=False, order=reference.Order("P", [{"name": "", "steps": ["Work", "Close"]}],
                                                                                [], "edited", True))
    assert _snapshot(folder) == before                                   # nothing existing changed
    html = paths["html"].read_text(encoding="utf-8")
    for text in ("Order evidence", "Level 4 quality", "Usual pattern inside the step", "Enter data → Check data → Save record",
                 "Work before Close"):
        assert text in html, text
    checks = json.loads(paths["checks"].read_text(encoding="utf-8"))
    assert {16, 17} <= {c["number"] for c in checks["checks"]} and checks["failed"] == 0
    facts = json.loads((folder / "facts.json").read_text(encoding="utf-8"))
    assert {"evidence_pair", "inside_top_pattern", "inside_rework"} <= {f["type"] for f in facts}
    numbered = [f for f in facts if f["id"].startswith("F")]                  # claims (C001 ...) number apart
    ids = [int(f["id"][1:]) for f in numbered]
    assert ids == sorted(ids) and numbered[-1]["section"] == "inside"          # the new sections come last
    real = inside_step.load

    def broken(folder_):
        data = real(folder_)
        data["steps"]["Work"]["patterns"][0]["runs"] += 1
        return data
    monkeypatch.setattr(inside_step, "load", broken)
    html = generate_report(job["job_id"], use_ai=False)["html"].read_text(encoding="utf-8")
    assert html.split("<h1>")[1].startswith("DRAFT")


# --------------------------------------------------------------------------- #
# Follow-up: no empty Level 3 model
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(pytest.importorskip("pm4py") is None, reason="PM4Py")
def test_empty_level3_model_is_not_drawn_by_default(data_dir, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.delenv("PM_DISABLE_PM4PY", raising=False)
    monkeypatch.setitem(settings()["pm4py"], "enabled", True)
    assert settings()["pm4py"]["build_on_run"] is False                 # the default: build only with "Build model"
    monkeypatch.setitem(settings()["pm4py"], "build_on_run", False)     # this test checks the deferred-build path
    rnd = random.Random(2)
    rows = []
    for c in range(200):
        steps = list("ABCDEFG")
        rnd.shuffle(steps)
        t = pd.Timestamp("2026-03-02") + pd.Timedelta(hours=c)
        rows += [(f"R{c}", a, (t + pd.Timedelta(minutes=k)).strftime("%Y-%m-%d %H:%M:%S")) for k, a in enumerate(steps[:rnd.randint(3, 7)])]
    src = data_dir / "random.csv"
    pd.DataFrame(rows, columns=["case_id", "activity", "timestamp"]).to_csv(src, index=False)
    job = make_job(data_dir, src, {**SEQ, "resource": None, "application": None, "active_time": None, "action": None})
    folder = jobs.job_folder(job["job_id"])
    assert not json.loads((folder / "mining.json").read_text(encoding="utf-8"))["process_model"]["available"]
    sec = pm4py_model.build(folder)
    assert sec["available"] and sec["data"]["show_model"] is False
    assert sec["data"]["structure_note"].startswith("No stable order found in the data at Level 3 (precision ")
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=180)
    at.session_state["job"] = jobs.load_job(job["job_id"])
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    at.radio(key="map_view").set_value("Discovered model (PM4Py)").run()
    assert not at.exception, at.exception
    assert any("No stable order found" in i.value for i in at.info)
    assert len(at.get("graphviz_chart")) == 0                            # the model is not drawn ...
    at.toggle(key="pm4py_show_anyway").set_value(True).run()
    assert not at.exception, at.exception
    assert len(at.get("graphviz_chart")) == 1                            # ... until asked for
    assert any(b.label.startswith("Download model") for b in at.get("download_button"))
    # a sequential log still draws the model
    rows = [(f"S{c}", a, (pd.Timestamp("2026-03-02") + pd.Timedelta(hours=c, minutes=k)).strftime("%Y-%m-%d %H:%M:%S"))
            for c in range(100) for k, a in enumerate("ABC")]
    src2 = data_dir / "seq.csv"
    pd.DataFrame(rows, columns=["case_id", "activity", "timestamp"]).to_csv(src2, index=False)
    job2 = make_job(data_dir, src2, {**SEQ, "resource": None, "application": None, "active_time": None, "action": None})
    sec2 = pm4py_model.build(jobs.job_folder(job2["job_id"]))
    assert sec2["data"]["show_model"] is True and sec2["data"]["precision"] >= 0.4


def test_saved_template_order_never_drives_conformance(data_dir):
    from app.core.report import generate_report
    job = hr_job(data_dir)
    t = reference.Order("HR", [{"name": "", "steps": HR}], [], "template", False, template_name="HR onboarding")
    reference.save_order(reference.order_key(job), t)
    html = generate_report(job["job_id"], use_ai=False)["html"].read_text(encoding="utf-8")
    m = metrics.load(jobs.job_folder(job["job_id"]))
    assert m["order"]["trusted"] is False and m["conformance"] is None
    assert "Step order not confirmed" in html.split("<h2>7. ")[1].split("</section>")[0]


# --------------------------------------------------------------------------- #
# Business context for Level 4, plain descriptions
# --------------------------------------------------------------------------- #
def test_level4_gets_business_context_before_any_check(data_dir):
    job, folder, _ = inside_job(data_dir)
    d = dmod.ProcessDefinition.from_dict({"name": "Onboarding", "steps": [
        {"id": "S1", "name": "Do the work", "position": 1, "mapped_activities": ["Work"],
         "description": "Prepare and save the new hire record. [Source: “some quote”]",
         "business_rules": "The record needs a signed contract."}, {"id": "S2", "name": "Close", "position": 2,
                                                                     "mapped_activities": ["Close"]}]})
    dmod.save(d)                                   # NOT linked to the job: no check has been run on page 4
    assert dmod.for_job(jobs.load_job(job["job_id"])) is None
    src = sop("Onboarding", "Do the work: check data before saving the record. Exceptions go to HR.")
    knowledge.add_manual_statement("Onboarding", src["id"], {"type": "rule", "subject": "Do the work",
                                   "value": "check data before saving the record",
                                   "quote": "check data before saving the record", "anchor": "p.1"}, d)
    assert level4.business_definition(job["job_id"]).name == "Onboarding"
    ctx = level4.doc_context(job["job_id"], "Work")
    assert "description: Prepare and save the new hire record." in ctx              # quote part removed
    assert "business rules: The record needs a signed contract." in ctx
    assert any(x.startswith("rule: Do the work — check data before saving the record") for x in ctx)
    assert not any("some quote" in x for x in ctx)


def test_descriptions_must_be_plain_business_words():
    us = [{"id": f"U{i:03d}", "activity": "Step", "application": "Workday", "screen": "Offer", "action": a,
           "events": 50, "share": 0.5, "shown": a, "shown_screen": "Offer"} for i, a in enumerate(["Enter offer", "Save offer"], 1)]
    out = {"substeps": [{"id": "S1", "label": "Prepare the offer", "description": "Clicks on the offer screen in Workday.",
                         "rules": [{"application": "Workday", "click_regex": "(?i)offer"}]}], "assign_unmatched": {}}
    problems, _ = level4.validate(out, us, us, {"Workday"})
    assert any("plain business words" in p for p in problems)
    out["substeps"][0]["description"] = "Prepare the offer so the candidate can accept it."
    problems, _ = level4.validate(out, us, us, {"Workday"})
    assert not any("plain business words" in p for p in problems)
    text = level4.substep_summary({"description": "Prepare the offer.", "share": 0.4, "active_share": 0.55,
                                   "median_active_s": 300, "apps": ["Workday"], "repeat_share": 0.2})
    assert text == "Prepare the offer. (55% of the step's time, about 5.0 min per run)."
