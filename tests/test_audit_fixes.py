"""Regression tests for the six problems found in the live audit (job 20261111-142633-a10124).

Fix 1  per-application numbers after "Merge repeated steps" (a merged run keeps ONE application)
Fix 2  Level 4 / sub-process keep every event (click names like "Core Activity" were dropped as people)
Fix 3  the scope check compares rows with merged EVENTS, not merged rows (false DRAFT)
Fix 4  one order source for page 3, the report, the facts, conformance and flow confidence
Fix 5  the HR onboarding template follows the business order, nothing mandatory (test_templates.py)
Fix 6  page-1 capability messages follow the mapping
Jobs are written to a temporary data folder (DATA_DIR), never to data/. Fake AI only.
"""
import json
import shutil

import duckdb
import pandas as pd
import pytest

from app.core import jobs, metrics, reference
from app.core.config import PROJECT_ROOT
from app.core.db import connect
from test_golden import EXP, GOLDEN, MAPPING, A, B, C, D, close, confirmed_order, h, make_job


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def golden_job(tmp, merge: bool):
    src = tmp / "golden_events.csv"
    shutil.copy(GOLDEN / "golden_events.csv", src)
    job = make_job(tmp, src, {**MAPPING, "merge_repeats": merge})
    from app.core.report import generate_report
    paths = generate_report(job["job_id"], use_ai=False, order=confirmed_order())
    folder = jobs.job_folder(job["job_id"])
    return job, folder, paths


# --------------------------------------------------------------------------- #
# Fix 1 + Fix 3: merging on gives the same numbers as merging off
# --------------------------------------------------------------------------- #
def test_merged_run_in_two_applications_books_each_application(data_dir):
    rows = [("X1", "Step A", "2026-03-02 09:00:00", "u1", "Workday", 100),
            ("X1", "Step A", "2026-03-02 09:01:00", "u1", "Excel", 50),       # same run, another application
            ("X1", "Step B", "2026-03-02 09:05:00", "u1", "Workday", 30)]
    src = data_dir / "two_apps.csv"
    pd.DataFrame(rows, columns=["case_id", "activity", "timestamp", "user", "application", "active_time"]) \
        .to_csv(src, index=False)
    m = {**MAPPING, "stage": None, "wait_time": None, "tat_time": None, "action": None, "window_title": None,
         "scope": {}, "merge_repeats": True}
    job = make_job(data_dir, src, m)
    folder = jobs.job_folder(job["job_id"])
    assert len(pd.read_parquet(folder / "events.parquet")) == 2                 # Step A merged into one row
    res = metrics.compute(connect(), folder)
    apps = {a["application"]: a for a in res["applications"]}
    assert (apps["Workday"]["events"], apps["Workday"]["cases"]) == (2, 1) and close(apps["Workday"]["active_hours"], h(130))
    assert (apps["Excel"]["events"], apps["Excel"]["cases"]) == (1, 1) and close(apps["Excel"]["active_hours"], h(50))
    row = next(r for r in res["matrix"]["rows"] if r["step"] == "Step A")
    cells = dict(zip(res["matrix"]["apps"], row["values"]))
    assert close(cells["Workday"], h(100)) and close(cells["Excel"], h(50))
    top = {a["application"]: a["hours"] for a in next(r for r in res["steps"] if r["step"] == "Step A")["top_apps"]}
    assert close(top["Workday"], h(100)) and close(top["Excel"], h(50))
    mining = json.loads((folder / "mining.json").read_text(encoding="utf-8"))
    page3 = {a["application"]: a for a in mining["applications"]["data"]["applications"]}
    assert (page3["Workday"]["events"], page3["Excel"]["events"], page3["Excel"]["cases"]) == (2, 1, 1)


@pytest.mark.parametrize("merge", [False, True])
def test_golden_numbers_and_checks_with_and_without_merging(data_dir, merge):
    _job, folder, paths = golden_job(data_dir, merge)
    m = metrics.load(folder)
    t = m["totals"]
    assert (t["events"], t["runs"], t["cases"]) == (24, 23, 6)
    for a in m["applications"]:
        e = EXP["applications"][a["application"]]
        assert (a["events"], a["cases"]) == (e["events"], e["cases"]) and close(a["active_hours"], h(e["active_seconds"]))
    checks = json.loads(paths["checks"].read_text(encoding="utf-8"))
    assert checks["failed"] == 0, [c for c in checks["checks"] if not c["passed"]]
    scope = next(c for c in checks["checks"] if c["check"].startswith("Rows in scope"))
    assert scope["report"] == scope["recomputed"] == 24                         # rows vs SUM(merged_events)
    raw = [c for c in checks["checks"] if c["number"] == 3 and "(input rows)" in c["check"]]
    assert len(raw) == 9                                                         # 3 applications x hours/events/cases
    people = [c for c in checks["checks"] if c["number"] == 14]
    assert len(people) == 10 and all(c["passed"] for c in people)               # count + 3 users x hours/events/cases
    assert "DRAFT" not in paths["html"].read_text(encoding="utf-8").split("<h1>")[1].split("</h1>")[0]


def test_raw_application_check_catches_a_wrong_application_total(data_dir, monkeypatch):
    from app.core.report import generate_report
    job, folder, _ = golden_job(data_dir, True)
    real = metrics.compute

    def first_app_only(*a, **k):                     # the old bug: every event of a run booked to its first app
        m = real(*a, **k)
        wd = next(x for x in m["applications"] if x["application"] == "Workday")
        wd["events"] += 1
        return m
    monkeypatch.setattr(metrics, "compute", first_app_only)
    html = generate_report(job["job_id"], use_ai=False, order=confirmed_order())["html"].read_text(encoding="utf-8")
    assert "Workday: events (input rows)" in html and html.split("<h1>")[1].startswith("DRAFT")


# --------------------------------------------------------------------------- #
# Fix 2: no event is lost in step_actions; UI click names are not people
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("label, kept", [("Core Activity", True), ("Work Notes", True), ("Worker Profile", True),
                                         ("Explore Grid", True), ("Identification Filter", True),
                                         ("Candidates List", True), ("Team Action", True), ("On Hold Reason", True),
                                         ("Published Canvas", True), ("John Smith", False)])
def test_click_names_are_people_only_when_they_are_names(label, kept):
    from app.core.milestones import scrub_action
    assert (scrub_action(label) == label) is kept


def test_no_events_lost_between_events_and_step_actions(tmp_path):
    from app.core import subprocess as sp
    from app.core.milestones import OTHER_CLICK
    t0 = pd.Timestamp("2026-03-02 09:00")
    actions = ["Core Activity", "Work Notes", "Worker Profile", "John Smith", None, "Core Activity"]
    df = pd.DataFrame({"case_id": "C1", "resource": "u1", "activity": "Case Tracking",
                       "ts_start": [t0 + pd.Timedelta(seconds=10 * i) for i in range(6)], "__action": actions,
                       "__run": None, "application": "Exion", "__active": 10.0, "src_row": range(6)})
    con = connect()
    con.register("tiny", df.astype({"__run": object, "__action": object}))
    path = tmp_path / sp.ACTIONS_FILE
    sp.capture_step_actions(con, "SELECT * FROM tiny", path, has_run_id=False)
    out = duckdb.connect().execute(f"SELECT action, SUM(repeats), SUM(active_seconds) FROM read_parquet('{path.as_posix()}') "
                                   "GROUP BY 1").fetchall()
    got = {a: (int(n), float(s)) for a, n, s in out}
    assert sum(n for n, _ in got.values()) == 6 and sum(s for _, s in got.values()) == 60.0   # nothing dropped
    assert got["Core Activity"][0] == 2 and "Work Notes" in got and "Worker Profile" in got
    assert got[OTHER_CLICK][0] == 2 and "John Smith" not in got                # the name and the empty click


def test_level4_equals_the_step_table(data_dir):
    _job, folder, paths = golden_job(data_dir, True)
    checks = json.loads(paths["checks"].read_text(encoding="utf-8"))
    l4 = [c for c in checks["checks"] if c["number"] == 13]
    assert len(l4) == 12 and all(c["passed"] for c in l4)                     # 4 steps x events / cases / hours
    acts = pd.read_parquet(folder / "step_actions.parquet")
    assert acts["repeats"].sum() == 24 and acts["case_id"].nunique() == 6


# --------------------------------------------------------------------------- #
# Fix 4: one order source everywhere
# --------------------------------------------------------------------------- #
HR = ["Requisition Creation", "Candidate Identification and ID Creation", "Case Assignment", "Offer Processing",
      "Case Tracking", "Background Checks Initiation", "Vetting Status Monitoring", "Hire Processing",
      "Employee Profile Creation", "Onboarding Task", "Payroll Activation"]


def hr_job(tmp):
    rows = []
    for c in range(60):
        t = pd.Timestamp("2026-03-02 09:00") + pd.Timedelta(hours=c)
        path = HR if c % 3 else [HR[0], HR[1], HR[3], HR[2]] + HR[4:]     # a third swap two steps
        for i, step in enumerate(path):
            rows.append((f"H{c}", step, (t + pd.Timedelta(minutes=20 * i)).strftime("%Y-%m-%d %H:%M:%S"),
                         f"u{c % 4}", "Workday", 60))
    src = tmp / "hr.csv"
    pd.DataFrame(rows, columns=["case_id", "activity", "timestamp", "user", "application", "active_time"]) \
        .to_csv(src, index=False)
    m = {**MAPPING, "stage": None, "wait_time": None, "tat_time": None, "action": None, "window_title": None,
         "scope": {}}
    return make_job(tmp, src, m)


def test_template_order_is_the_same_on_page_3_in_the_report_and_the_facts(data_dir, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from app.core.config import settings
    from app.core.report import generate_report
    # The template is off by default (prompt 01b: not evidence); switched on here as a convenient trusted source.
    monkeypatch.setitem(settings()["templates"], "use_for_order", True)
    job = hr_job(data_dir)
    folder = jobs.job_folder(job["job_id"])
    html = generate_report(job["job_id"], use_ai=False)["html"].read_text(encoding="utf-8")
    ref = json.loads((folder / "reference.json").read_text(encoding="utf-8"))
    badge = ref["source_badge"]
    assert ref["choice"] == "template" and badge.startswith("Template: HR onboarding")
    assert badge in html.split("</header>")[0] and "steps sorted by cases reached" not in html
    facts = {f["type"]: f for f in json.loads((folder / "facts.json").read_text(encoding="utf-8"))}
    assert facts["order_source"]["text"] == badge
    assert facts["flow_confidence"]["value"] == ref["flow"]["score"] and facts["flow_confidence"]["meta"]["source"] == "template"
    conf = metrics.load(folder)["conformance"]
    assert close(conf["in_order_share"], ref["stats"]["in_order_share"])            # page 3's number
    assert close(conf["pair_order_share"], ref["stats"]["pair_order_share"])
    assert jobs.load_job(job["job_id"])["order_source"] == badge
    mining = json.loads((folder / "mining.json").read_text(encoding="utf-8"))
    assert mining["flow_confidence"]["data"]["source_badge"] == badge
    assert mining["flow_confidence"]["data"]["score"] == ref["flow"]["score"]
    sentences = reference.describe_reference(reference.Order.from_dict(ref["order"]), ref["stats"])
    assert any("comes from the HR onboarding template" in s for s in sentences)
    assert not any("suggested from the data" in s for s in sentences)

    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.session_state["job"] = jobs.load_job(job["job_id"])
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    assert not at.exception, at.exception
    pick = at.selectbox(key=f"order_source_{job['job_id']}")
    assert pick.format_func(pick.value) == badge                                 # page 3 shows the same source

    # A different pick on page 3 is what the report uses next.
    choice = jobs.load_job(job["job_id"])
    choice["order_choice"] = "data"
    jobs.save_job(choice)
    generate_report(job["job_id"], use_ai=False)
    ref = json.loads((folder / "reference.json").read_text(encoding="utf-8"))
    assert ref["choice"] == "data" and metrics.load(folder)["order"]["trusted"] is False


def test_resolve_priority():
    opts = [{"key": k, "label": k} for k in ("saved", "template", "ai", "data (milestones)", "data")]
    assert reference.resolve(opts)["key"] == "saved"
    assert reference.resolve(opts, {"order_choice": "ai"})["key"] == "ai"
    assert reference.resolve(opts[1:], {"order_choice": "saved"})["key"] == "template"   # no longer offered


# --------------------------------------------------------------------------- #
# Fix 6: capability messages follow the mapping
# --------------------------------------------------------------------------- #
def test_capabilities_follow_the_mapping():
    from app.core.capabilities import capabilities
    base = {"case_id": "case_id", "activity": "activity", "timestamp": "timestamp"}
    rows = {r["feature"]: r for r in capabilities(base)}
    assert rows["Step runs"]["status"] == "fallback" and "No step-run ID" in rows["Step runs"]["fallback"]
    assert rows["Active time and effort"]["status"] == "fallback" and "estimated" in rows["Active time and effort"]["fallback"]
    rows = {r["feature"]: r for r in capabilities({**base, "step_run": "abstraction_instance_id",
                                                   "active_time": "active_time", "wait_time": "wait_time",
                                                   "tat_time": "tat_time", "level3": "level3_process_name"})}
    assert rows["Step runs"]["status"] == "available" and "No step-run ID" not in rows["Step runs"]["fallback"]
    assert "abstraction_instance_id" in rows["Step runs"]["fallback"]
    eff = rows["Active time and effort"]
    assert eff["status"] == "available" and "estimated" not in eff["fallback"] and "active_time" in eff["fallback"]
    assert rows["Sub-process hierarchy"]["status"] == "available"


# --------------------------------------------------------------------------- #
# Follow-up A: the matrix check uses the per-application hours of the raw rows, not the merged rows
# --------------------------------------------------------------------------- #
def test_matrix_check_with_a_merged_run_in_two_applications(data_dir):
    from app.core.report import generate_report
    df = pd.read_csv(GOLDEN / "golden_events.csv", dtype=str)
    df.loc[1, "active_time"] = "1800"        # C1's Receive Request run: Workday 120 s + Excel 1800 s (0.5 h)
    src = data_dir / "golden_big_excel.csv"
    df.to_csv(src, index=False)
    job = make_job(data_dir, src, {**MAPPING, "merge_repeats": True})
    paths = generate_report(job["job_id"], use_ai=False, order=confirmed_order())
    m = metrics.load(jobs.job_folder(job["job_id"]))
    apps = {a["application"]: a["active_hours"] for a in m["applications"]}
    for app, total in zip(m["matrix"]["apps"], m["matrix"]["column_totals"]):
        assert close(total, apps[app]), app
    assert close(apps["Excel"], h(640 - 60 + 1800))
    checks = json.loads(paths["checks"].read_text(encoding="utf-8"))
    col = next(c for c in checks["checks"] if c["check"] == "Matrix column totals = application hours")
    assert col["passed"] and checks["failed"] == 0, [c for c in checks["checks"] if not c["passed"]]
    assert not paths["html"].read_text(encoding="utf-8").split("<h1>")[1].startswith("DRAFT")


# --------------------------------------------------------------------------- #
# Follow-up B: a merged run worked by two users credits each user
# --------------------------------------------------------------------------- #
def test_merged_run_by_two_users_credits_each_user(data_dir):
    from app.core.report import generate_report
    rows = [("X1", "Step A", "2026-03-02 09:00:00", "u1", "Workday", 100),
            ("X1", "Step A", "2026-03-02 09:01:00", "u2", "Workday", 300),     # same run, another user
            ("X1", "Step B", "2026-03-02 09:05:00", "u1", "Workday", 30),
            ("X2", "Step A", "2026-03-03 09:00:00", "u1", "Workday", 50)]
    src = data_dir / "two_users.csv"
    pd.DataFrame(rows, columns=["case_id", "activity", "timestamp", "user", "application", "active_time"]) \
        .to_csv(src, index=False)
    m = {**MAPPING, "stage": None, "wait_time": None, "tat_time": None, "action": None, "window_title": None,
         "scope": {}, "merge_repeats": True}
    job = make_job(data_dir, src, m)
    folder = jobs.job_folder(job["job_id"])
    assert len(pd.read_parquet(folder / "events.parquet")) == 3                # X1's Step A merged into one row
    paths = generate_report(job["job_id"], use_ai=False)
    people = {p["participant"]: p for p in metrics.load(folder)["people"]}
    u1, u2 = people["P01"], people["P02"]                                      # u1 180 s, u2 300 s ... by hours
    u1, u2 = (u1, u2) if u1["events"] == 3 else (u2, u1)
    assert (u1["events"], u1["cases"], u1["steps"], u1["runs"]) == (3, 2, 2, 3) and close(u1["active_hours"], h(180))
    assert (u2["events"], u2["cases"], u2["steps"], u2["runs"]) == (1, 1, 1, 1) and close(u2["active_hours"], h(300))
    assert people["P01"]["active_hours"] > people["P02"]["active_hours"]       # labels by active hours
    checks = json.loads(paths["checks"].read_text(encoding="utf-8"))
    raw = [c for c in checks["checks"] if c["number"] == 14]
    assert len(raw) == 7 and all(c["passed"] for c in raw)                    # count + 2 users x hours/events/cases
    assert checks["failed"] == 0
