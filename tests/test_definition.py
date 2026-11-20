"""Business definition model, import/export and approval guardrails."""
import xml.etree.ElementTree as ET

from app.core import definition as dmod
from app.core.business_map import bpmn_problems, to_bpmn


def sample():
    return dmod.ProcessDefinition.from_dict({"name": "Purchasing", "stages": [{"id": "ST1", "name": "Buy"}],
        "steps": [{"id": "S1", "name": "Request", "stage_id": "ST1", "position": 1,
                   "mapped_activities": ["Request"]},
                  {"id": "S2", "name": "Approve", "stage_id": "ST1", "position": 2,
                   "mapped_activities": ["Approve"]}]})


def test_validation_detects_bad_references_positions_and_next_cycles():
    d = sample()
    d.steps[1]["position"] = 1
    d.flows = [{"from_step": "S1", "to_step": "missing", "kind": "next", "condition": ""}]
    errors, _ = dmod.validate(d)
    assert any("Position" in x for x in errors)
    assert any("unknown step" in x for x in errors)
    d.flows = [{"from_step": "S1", "to_step": "S2", "kind": "next", "condition": ""},
               {"from_step": "S2", "to_step": "S1", "kind": "next", "condition": ""}]
    assert any("round in a circle" in x for x in dmod.validate(d)[0])


def test_yaml_excel_and_bpmn_round_trip():
    d = sample()
    assert dmod.from_yaml(dmod.to_yaml(d)).steps[1]["name"] == "Approve"
    assert dmod.from_excel(dmod.to_excel(d)).steps[0]["mapped_activities"] == ["Request"]
    xml = to_bpmn(d)
    ET.fromstring(xml)
    assert bpmn_problems(xml) == []
    imported, notes = dmod.from_bpmn(xml)
    assert not notes and [s["name"] for s in imported.ordered_steps()] == ["Request", "Approve"]
