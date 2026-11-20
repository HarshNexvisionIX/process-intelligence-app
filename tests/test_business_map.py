"""The final business map is a pure DOT/BPMN export, so its essentials stay easy to test."""
from app.core.business_map import to_business_dot
from app.core.definition import ProcessDefinition


def test_business_dot_has_lanes_events_optional_style_and_optional_evidence():
    d = ProcessDefinition.from_dict({"name": "Demo", "stages": [{"id": "A", "name": "One"}, {"id": "B", "name": "Two"}],
        "steps": [{"id": "S1", "name": "First", "stage_id": "A", "position": 1, "mandatory": True},
                  {"id": "S2", "name": "Second", "stage_id": "B", "position": 2, "mandatory": False}]})
    claims = [{"id": "C001", "kind": "step_exists", "verdict": "Contradicted", "refs": {"step_id": "S2"}}]
    shown = to_business_dot(d, claims, {"cases": 5}, show_evidence=True)
    hidden = to_business_dot(d, claims, {"cases": 5}, show_evidence=False)
    assert "cluster_0" in shown and "cluster_1" in shown and "start [shape=circle" in shown
    assert "style=\"rounded,filled,dashed\"" in shown and "! C001" in shown and "Colours:" in shown
    assert "! C001" not in hidden and "Colours:" not in hidden
