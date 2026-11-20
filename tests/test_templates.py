"""Process templates and the AI order suggestion (business knowledge for the order).

Run with:  pytest -q tests/test_templates.py
"""
import json
import re

import pytest

from app.core.narrative import suggest_order_ai, validate_order_suggestion
from app.core.templates import load_templates, match_template, template_order
from test_narrative import FakeClient

HR_NAMES = ["Candidate Identification and ID Creation", "Case Tracking", "Hire Processing", "Offer Processing",
            "Employee Profile Creation", "Case Assignment", "Payroll Activation", "Requisition Creation",
            "Onboarding Task", "Vetting Status Monitoring", "Background Checks Initiation"]


@pytest.fixture(autouse=True)
def data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def test_four_templates_ship():
    templates = load_templates()
    assert set(templates) == {"hr_onboarding", "purchase_to_pay", "order_to_cash", "ticket_handling"}
    for t in templates.values():
        assert t["name"] and t["stages"]
        assert all(step["name"] and "mandatory" in step for stage in t["stages"] for step in stage["steps"])


def test_hr_export_matches_hr_onboarding():
    template, mapping, share = match_template(HR_NAMES)
    assert template["key"] == "hr_onboarding" and share >= 0.9
    order = template_order(template, mapping, share, "Onboarding")
    assert order.drawn_steps()[0] == "Requisition Creation"
    assert "Onboarding Task" in order.stages[-1]["steps"]
    assert order.source == "template" and order.template_name == "HR onboarding"
    # The business order (HR onboarding deck), and no mandatory step unless the business marks one.
    assert order.drawn_steps() == [
        "Requisition Creation", "Candidate Identification and ID Creation", "Case Assignment", "Offer Processing",
        "Case Tracking", "Background Checks Initiation", "Vetting Status Monitoring", "Hire Processing",
        "Employee Profile Creation", "Onboarding Task", "Payroll Activation"]
    assert order.mandatory_steps == []


def test_unmatched_steps_go_to_other_and_weak_matches_are_not_suggested():
    events = {name: 100 for name in HR_NAMES} | {"Coffee Break": 50}
    template, mapping, share = match_template(events)
    order = template_order(template, mapping, share, "Onboarding")
    assert "Coffee Break" in order.other_steps and mapping["Coffee Break"] is None
    template, mapping, share = match_template(["Alpha Step", "Beta Step", "Gamma Step", "Case Tracking"])
    assert template is None and mapping == {} and share < 0.6


VALID = {"stages": [{"name": "Start", "steps": ["Open Ticket", "Assign Ticket"]},
                    {"name": "End", "steps": ["Resolve Ticket", "Close Ticket"]}]}
STEPS = ["Close Ticket", "Open Ticket", "Resolve Ticket", "Assign Ticket"]


def test_ai_order_suggestion_valid_answer():
    client = FakeClient(json.dumps(VALID))
    order = suggest_order_ai("job-1", STEPS, ["Intake"], "Tickets", client=client)
    assert order is not None and order.source == "ai" and not order.confirmed_by_business
    assert order.drawn_steps() == ["Open Ticket", "Assign Ticket", "Resolve Ticket", "Close Ticket"]
    sent = "\n".join(m["content"] for m in client.calls[0]["messages"] if m["role"] == "user")
    assert all(s in sent for s in STEPS) and "Intake" in sent
    assert not re.search(r"\d", sent.split("Return exactly")[0])      # names only: no counts or data


def test_ai_order_suggestion_rejects_invented_or_missing_steps():
    invented = {"stages": [{"name": "All", "steps": STEPS + ["Escalate To Manager"]}]}
    missing = {"stages": [{"name": "All", "steps": STEPS[:3]}]}
    assert any("not in the list" in e for e in validate_order_suggestion(invented, STEPS))
    assert any("missing" in e for e in validate_order_suggestion(missing, STEPS))
    assert validate_order_suggestion(VALID, STEPS) == []
    client = FakeClient(json.dumps(invented))               # always wrong: retried, then given up silently
    assert suggest_order_ai("job-1", STEPS, [], "Tickets", client=client) is None
    assert len(client.calls) > 1
