import json
from pathlib import Path

import pandas as pd
import pytest

from app.core import jobs, milestones, reference
from app.core.config import settings
from app.core.db import connect
from app.core.ingest import normalise, open_source, profile
from app.core.mapping import suggest_mapping
from app.core.mining import run_mining, save_mining
from scripts.generate_synthetic import main as generate


@pytest.fixture
def milestone_job(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "isolated"))
    src = tmp_path / "actions.parquet"
    generate(["--cases", "3000", "--actions", "--shuffle-order", "--stages", "--out", str(src)])
    job = jobs.create_job(src.name, src)
    folder = jobs.job_folder(job["job_id"])
    con = connect()
    open_source(con, src, folder)
    prof = profile(con)
    mapping = {role:s.column for role,s in suggest_mapping(prof).items()}
    assert mapping["action"] == "activity_alias_name"
    from app.core.capabilities import capabilities
    assert next(r for r in capabilities(mapping) if r["feature"] == "Milestones")["status"] == "available"
    mapping.update(timestamp_format="native", timezone="UTC", merge_repeats=True)
    summary = normalise(con, mapping, folder / "events.parquet")
    (folder / "mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
    job.update(status="mined", summary=summary, band="High")
    jobs.save_job(job)
    result = run_mining(con, folder / "events.parquet", "High")
    save_mining(result, folder)
    yield job, folder, con, mapping, result
    con.close()


def test_capture_premerge_cleaning_and_chain(milestone_job, tmp_path):
    job, folder, con, mapping, result = milestone_job
    ms = result["milestones"]["data"]
    assert ms["actions"] == 3
    assert ms["coverage"] == pytest.approx(.4)
    assert ms["order_strength"] > .95
    assert ms["flow"]["band"] == "Medium"
    assert any("40%" in s for s in ms["flow"]["reasons"])
    assert len(ms["chains"]) == 1
    chain = ms["chains"][0]
    assert chain["labels"] == ["Create Request · Create Tax Data", "Approve Request · Create Benefits", "Approve Request · Click Submit"]
    assert chain["cases"] == 1200 and chain["link_counts"] == [1200,1200] and chain["back_counts"] == [0,0]
    cols = con.execute(f"DESCRIBE SELECT * FROM '{(folder / 'events.parquet').as_posix()}'").fetchall()
    assert not any("action" in r[0] or "alias" in r[0] for r in cols)
    before = con.execute(f"SELECT * FROM '{(folder / 'milestone_candidates.parquet').as_posix()}' ORDER BY ALL").fetchall()
    target = tmp_path / "unmerged"
    target.mkdir()
    normalise(con, {**mapping, "merge_repeats": False}, target / "events.parquet")
    after = con.execute(f"SELECT * FROM '{(target / 'milestone_candidates.parquet').as_posix()}' ORDER BY ALL").fetchall()
    assert before == after
    assert milestones.clean_action("Save Form #abcdef# john@example.com 12345678") == "Save Form"
    assert milestones.clean_action("Asha Rao") is None
    assert milestones.clean_action("Recreate field") is None
    assert milestones.clean_action("View panel") is None
    ref = reference.compute(con, folder / "events.parquet", reference.order_key(job))
    assert ref["stats"]["order_strength"] < ms["order_strength"]
    assert ref["flow"]["source"] == "data (milestones)"
    dot = milestones.to_dot(ms)
    assert "cluster_0" in dot and "cluster_1" in dot and "rankdir=LR" in dot


def test_catalogue_edit_disable_and_readings(milestone_job):
    job, folder, con, mapping, result = milestone_job
    rows = milestones.milestone_catalogue(job)
    for i,r in enumerate(rows):
        r["label"] = ["Benefits created", "Submitted", "Tax data created"][i]
    milestones.save_catalogue(reference.order_key(job), rows, True)
    assert milestones.load_catalogue(reference.order_key(job))["confirmed_by_business"]
    updated = run_mining(con, folder / "events.parquet", "High")["milestones"]["data"]
    assert "Create Tax Data" not in json.dumps(updated)
    assert "Tax data created" in json.dumps(updated)
    from app.core.reading_finder import find_readings
    readings = find_readings(con, "src", mapping, max_candidates=1)
    candidates = [r for r in readings if r.case_spec["kind"] == "milestones"]
    assert len(candidates) == 2 and all(r.coverage == pytest.approx(.4) for r in candidates)
    for r in rows:
        r["is_milestone"] = False
    milestones.save_catalogue(reference.order_key(job), rows)
    empty = run_mining(con, folder / "events.parquet", "High")["milestones"]["data"]
    assert empty["actions"] == 0 and empty["chains"] == [] and empty["coverage"] == 0


def test_chain_narrative_guardrail():
    from app.core.facts import Fact
    from app.core.narrative import validate
    f = Fact("F001", "milestone_chain", "Tax data created → Benefits created", 400, "cases", 400,
             badge="Medium", section="map", meta={"labels": ["Tax data created", "Benefits created"]})
    output = {"summary": [{"text": "Tax data created is usually followed by Benefits created ({{F001}}).", "facts": ["F001"]}],
              "findings": [], "recommendations": []}
    assert validate(output, {f.id}, {f.id:f}, "None") == []
    f.type = "transition_count"
    assert validate(output, {f.id}, {f.id:f}, "None")


def test_definition_chain_and_business_map(milestone_job):
    from app.core.definition import ProcessDefinition
    from app.core.evidence import check_definition
    from app.core.business_map import to_business_dot
    job, folder, con, mapping, result = milestone_job
    (folder / "reference.json").write_text(json.dumps({"flow": {"band": "None"}}), encoding="utf-8")
    definition = ProcessDefinition.from_dict({"name": "Milestone process", "stages": [{"id":"s", "name":"Work"}],
        "steps": [{"id":"a", "name":"Prepare", "stage_id":"s", "position":1, "mapped_activities":["Create Request"],
                   "milestone_labels":["Tax data created"]},
                  {"id":"b", "name":"Approve", "stage_id":"s", "position":2, "mapped_activities":["Approve Request"]}]})
    checked = check_definition(definition, job, con)
    claim = next(c for c in checked["claims"] if c["kind"] == "order")
    assert claim["verdict"] == "Supported" and "M001" in json.dumps(claim)
    assert "Milestones: Tax data created" in to_business_dot(definition, checked["claims"], checked["stats"])
    definition.steps[0]["position"], definition.steps[1]["position"] = 2,1
    reverse = check_definition(definition, job, con)
    claim = next(c for c in reverse["claims"] if c["kind"] == "order")
    assert claim["verdict"] == "Contradicted" and "M001" in json.dumps(claim)
    from app.core.definition import save, link_job
    from app.core.config import PROJECT_ROOT
    from streamlit.testing.v1 import AppTest
    save(definition)
    link_job(job, definition)
    at = AppTest.from_file(str(PROJECT_ROOT / "app/main.py"), default_timeout=120)
    at.session_state["job"] = jobs.load_job(job["job_id"])
    at.run().switch_page("ui/business_page.py").run()
    assert not at.exception
    assert any(e.label == "Milestone labels per step" for e in at.expander)
    chosen = next(w for w in at.multiselect if w.label == "Prepare")
    assert chosen.options == ["Create Request · Create Tax Data"]


def test_report_and_page(milestone_job):
    from app.core.quality import run_quality, save_report
    from app.core.report import generate_report
    from openpyxl import load_workbook
    from streamlit.testing.v1 import AppTest
    from app.core.config import PROJECT_ROOT
    job, folder, con, mapping, result = milestone_job
    quality = run_quality(con, folder / "events.parquet")
    save_report(quality, folder)
    result = run_mining(con, folder / "events.parquet", quality.band)
    save_mining(result, folder)
    job.update(band=quality.band, mined_at=result["created_at"], feasibility=quality.feasibility)
    jobs.save_job(job)
    rows = milestones.milestone_catalogue(job)
    for i,r in enumerate(rows):
        r["label"] = ["Benefits created", "Submitted", "Tax data created"][i]
    milestones.save_catalogue(reference.order_key(job), rows)
    save_mining(run_mining(con, folder / "events.parquet", quality.band), folder)
    generate_report(job["job_id"], use_ai=False)
    html = (folder / "report.html").read_text(encoding="utf-8")
    assert "Confirmed sequences found in the data" in html and "milestone-svg" in html
    assert "Create Tax Data" not in html and "Tax data created" in html
    facts = (folder / "facts.json").read_text(encoding="utf-8")
    assert "Create Tax Data" not in facts and 'milestone_chain' in facts
    from app.core.facts import load_facts
    from app.core.narrative import write_summary
    from test_narrative import FakeClient
    stored = load_facts(folder)
    f = next(f for f in stored if f.type == "milestone_chain")
    client = FakeClient(json.dumps({"summary": [{"text": "Tax data created is usually followed by Benefits created ({{" + f.id + "}}).",
                                                "facts": [f.id]}], "findings": [], "recommendations": []}))
    write_summary(job["job_id"], stored, "High", client=client)
    payload = client.sent_text()
    assert payload and "Tax data created" in payload and "Create Tax Data" not in payload
    assert "activity_alias_name" not in payload and "C00000001" not in payload and "clerk_" not in payload
    wb = load_workbook(folder / "report.xlsx", read_only=True)
    assert "Milestones" in wb.sheetnames
    wb.close()
    at = AppTest.from_file(str(PROJECT_ROOT / "app/main.py"), default_timeout=120)
    at.session_state["job"] = job
    at.run().switch_page("ui/map_page.py").run()
    assert not at.exception
    assert any(e.label == "Milestones (from actions)" for e in at.expander)
    at.radio(key="map_view").set_value("Milestone map").run()
    assert not at.exception
    assert at.get("graphviz_chart")


def test_completion_is_per_run_and_capture_respects_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "isolated"))
    monkeypatch.setitem(settings()["milestones"], "min_count", 2)
    monkeypatch.setitem(settings()["milestones"], "min_users", 2)
    monkeypatch.setitem(settings()["milestones"], "min_pair", 2)
    records = []
    for case in ("one", "two"):
        for i,(step,action) in enumerate([("A","Create Tax Data"), ("A","Create Tax Data"),
                                          ("A","Save Form"), ("B","Click Submit"), ("A","View field"),
                                          ("A","John Submit")]):
            records.append(dict(case=case, step=step, time=pd.Timestamp("2024-01-01") + pd.Timedelta(minutes=i),
                                user=case, action=action))
    with connect() as con:
        con.register("src", pd.DataFrame(records))
        normalise(con, {"case_id":"case", "activity":"step", "timestamp":"time", "resource":"user",
                        "action":"action", "timestamp_format":"native", "merge_repeats":True}, tmp_path / "events.parquet")
        result = run_mining(con, tmp_path / "events.parquet", "High")["milestones"]["data"]
    assert result["actions"] == 3
    completion = {c["label"]: c for c in result["completion"]}
    assert completion["A · Create Tax Data"]["share"] == .5
    assert completion["A · Create Tax Data"]["occurrences"] == 2
    assert completion["B · Click Submit"]["share"] == 1
