"""Combined confidence (business knowledge x data), the coloured map, report and page.

Run with:  pytest -q tests/test_combined_confidence.py
"""
import json
import re

import pytest

from app.core import combined_confidence as cc
from app.core import definition as dmod, knowledge
from app.core.business_map import to_business_dot
from app.core.evidence import run_check
from test_narrative import FakeClient
from test_report import make_job

LONG = ("Pay Invoice is the final step and the treasury team checks every bank detail twice, compares the amount "
        "with the order, writes a note and only then releases the payment run for the week")
SOP = ("Create Request is always followed by Approve Request. Change Price is mandatory for every order. "
       f"{LONG}. Pay Invoice is slow because approvals queue up.")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


@pytest.mark.parametrize("belief, verdict, conflict, expected", [
    (0.9, "Supported", False, "Confirmed"),
    (0.9, "No data", False, "Business only"),
    (0.9, "Partly supported", False, "Partly confirmed"),
    (0.9, "Contradicted", False, "Documented ≠ actual"),
    (0.3, "Supported", False, "Undocumented practice"),
    (0.9, "Supported", True, "Needs decision"),
    (0.3, "No data", False, "Unverified"),
    (0.0, "Partly supported", False, "Unverified"),
])
def test_status_matrix(belief, verdict, conflict, expected):
    assert cc.status(belief, verdict, conflict) == expected


def test_confidence_formula_and_overall_with_breakdown():
    assert cc.confidence(0.9, "Supported", "High") == 95.0
    assert cc.confidence(0.9, "Supported", "Medium") == 85.0
    assert cc.confidence(0.9, "Supported", "Exploratory") == 70.0
    assert cc.confidence(0.9, "Partly supported") == 70.0
    assert cc.confidence(0.9, "Contradicted") == 45.0
    assert cc.confidence(0.9, "No data") == 70.0 and cc.confidence(0.5, "No data") == 50.0
    elements = [{"kind": "step", "confidence": 80, "status": "Confirmed"},
                {"kind": "flow", "confidence": 50, "status": "Partly confirmed"},
                {"kind": "owner", "confidence": 60, "status": "Business only"}]
    summary = cc.overall(elements)
    assert summary["score"] == pytest.approx(round((2 * 80 + 1.5 * 50 + 60) / 4.5, 1))
    assert summary["breakdown"]["Confirmed"] == 1 and summary["breakdown"]["Business only"] == 1
    line = cc.breakdown_line(summary)
    assert line.startswith("Map confidence 66 / 100 — ") and "1 Partly confirmed" in line


def tiny():
    d = dmod.ProcessDefinition.from_dict({"name": "Tiny", "stages": [{"id": "A", "name": "All"}],
        "steps": [{"id": "S1", "name": "Offer", "stage_id": "A", "position": 1, "owner_role": "Recruiter"},
                  {"id": "S2", "name": "Hire", "stage_id": "A", "position": 2}]})
    merged = knowledge.merge([
        {"type": "step_exists", "subject": "Offer", "object": "", "value": "", "quote": "q", "anchor": "p.4",
         "source_id": "S001", "reliability": 0.9},
        {"type": "order", "subject": "Offer", "object": "Hire", "value": "", "quote": "q", "anchor": "00:12:41",
         "source_id": "S003", "reliability": 0.8}])
    evidence = {"claims": [
        {"id": "C001", "kind": "step_exists", "verdict": "Supported", "support": 2410, "badge": "High",
         "refs": {"step_id": "S1"}},
        {"id": "C002", "kind": "order", "verdict": "Contradicted", "support": 900, "badge": "High",
         "refs": {"from_step": "S1", "to_step": "S2"}},
        {"id": "C003", "kind": "step_exists", "verdict": "Supported", "support": 1500, "badge": "High",
         "refs": {"step_id": "S2"}}]}
    return d, cc.combine(d, merged, evidence), evidence


def test_combine_elements_and_map_tooltips():
    d, combined, evidence = tiny()
    by = {e["key"]: e for e in combined["elements"]}
    assert by["step:Offer"]["status"] == "Confirmed" and by["step:Offer"]["confidence"] == 99.0   # belief 0.98 (two sources)
    assert by["order:Offer<Hire"]["status"] == "Documented ≠ actual"
    assert by["step:Hire"]["belief"] == pytest.approx(0.8) and by["step:Hire"]["status"] == "Confirmed"
    assert by["owner:Offer=Recruiter"]["status"] == "Unverified"
    dot = to_business_dot(d, evidence["claims"], None, True, combined=combined)
    assert 'tooltip="Sources: S001 p.4, S003 [00:12:41] · Data: Supported (2,410 cases, High)"' in dot
    assert 'id="step_S1"' in dot and 'id="flow_S1_S2"' in dot and "Data: Contradicted (900 cases, High)" in dot
    for status in cc.STATUSES:
        assert status in dot
    assert cc.STATUS_COLORS["Documented ≠ actual"] in dot


def p2p_definition() -> dmod.ProcessDefinition:
    names = ["Create Request", "Approve Request", "Create Order", "Change Price", "Pay Invoice"]
    return dmod.ProcessDefinition.from_dict({"name": "Purchase to pay", "stages": [{"id": "A", "name": "All"}],
        "steps": [{"id": f"S{i}", "name": n, "stage_id": "A", "position": i, "mapped_activities": [n]}
                  for i, n in enumerate(names, 1)]})


def with_sources(d) -> None:
    src, _ = knowledge.add_source(d.name, "sop", "purchasing_sop.txt", SOP.encode(), title="Purchasing SOP")
    for st in ({"type": "order", "subject": "Create Request", "object": "Approve Request",
                "quote": "Create Request is always followed by Approve Request", "anchor": "p.1"},
               {"type": "mandatory", "subject": "Change Price", "quote": "Change Price is mandatory for every order",
                "anchor": "p.1"},
               {"type": "pain_point", "subject": "Pay Invoice", "value": "approvals queue up",
                "quote": "Pay Invoice is slow because approvals queue up", "anchor": "p.1"}):
        knowledge.add_manual_statement(d.name, src["id"], st, d)


def test_report_lists_quotes_and_narrative_payload(tmp_path):
    from app.core.report import generate_report
    job = make_job(tmp_path, 400)
    d = p2p_definition()
    with_sources(d)
    run_check(job, d)

    def reply(messages):
        ids = re.findall(r"^(F\d{3}) \|", messages[-1]["content"], re.MULTILINE)
        return json.dumps({"summary": [{"text": f"The analysis covers {{{{{ids[0]}}}}}.", "facts": [ids[0]]}],
                           "findings": [], "recommendations": []})
    client = FakeClient(reply)
    paths = generate_report(job["job_id"], client=client)
    html = paths["html"].read_text(encoding="utf-8")
    gap = html.split("<h3>Documented ≠ actual</h3>")[1].split("<h3>")[0]
    assert "Change Price is mandatory" in gap
    undocumented = html.split("<h3>Undocumented practice</h3>")[1].split("<h3>")[0]
    assert "Create Order is a step of the process" in undocumented
    assert "Map confidence" in html and "Purchasing SOP" in html
    assert LONG not in html and " ".join(LONG.split()[:26]) not in html
    for quote in re.findall(r"“(.*?)”", html):
        assert len(quote.replace(" …", "").split()) <= 25
    sent = client.sent_text()
    assert client.calls and "always followed by" not in sent and "every order" not in sent and "approvals queue" not in sent
    import pandas as pd
    sheets = pd.ExcelFile(paths["excel"]).sheet_names
    assert {"Sources", "Statements", "Combined confidence"} <= set(sheets)
    facts = json.loads(paths["facts"].read_text(encoding="utf-8"))
    assert any(f["type"] == "combined_overall" for f in facts)
    assert any(f["type"] == "combined_status" and f["meta"]["status"] == "Documented ≠ actual" for f in facts)
    assert not any("every order" in json.dumps(f) for f in facts)


def test_narrative_rejects_blame_for_documented_gaps():
    from app.core import narrative
    from app.core.facts import Fact
    f = Fact(id="F001", type="combined_status", label="Change Price is mandatory", value=None, unit="count",
             support=400, badge="High", section="map", text="Documented ≠ actual (45 / 100)",
             meta={"status": "Documented ≠ actual", "kind": "mandatory"})
    answer = lambda t: {"summary": [{"text": t, "facts": ["F001"]}], "findings": [], "recommendations": []}  # noqa: E731
    bad = narrative.validate(answer("Skipping the step is an error by the team: {{F001}}."), {"F001"}, {"F001": f})
    assert any("gap between the documented and the observed process" in e for e in bad)
    assert narrative.validate(answer("The documented and the observed process differ here: {{F001}}."),
                              {"F001"}, {"F001": f}) == []


def test_business_page_knowledge_tabs_render(tmp_path):
    from streamlit.testing.v1 import AppTest
    from app.core.config import PROJECT_ROOT
    job = make_job(tmp_path, 300)
    d = p2p_definition()
    with_sources(d)
    run_check(job, d)
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.session_state["job"] = job
    at.run().switch_page("ui/business_page.py").run()
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs] == ["Start the definition", "Knowledge sources", "Edit the definition",
                                          "Check against the data", "Conflicts & questions", "Approve", "Final map"]
    infos = " ".join(i.value for i in at.info)
    assert "AI is switched off" in infos
    assert any("Map confidence" in m.value for m in at.markdown)
    assert any(t.label.startswith("The data shows") or "mandatory" in t.label for t in at.text_area)
