"""Day 5 tests: the AI summary guardrails. No network: a fake client plays the AI.

Run with:  pytest -q tests/test_narrative.py
"""
import json
import re
from types import SimpleNamespace

import pytest

from app.core import observability
from app.core.facts import Fact, format_fact
from app.core.narrative import select_facts, validate, write_summary
from app.core.config import settings

MAX_RETRIES = settings()["ai"]["max_retries"]


class FakeClient:
    """Mimics client.chat.completions.create(...); replies are returned in order.

    A reply may be a string or a function of the messages (to answer with real fact IDs).
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        text = reply(kwargs["messages"]) if callable(reply) else reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    def sent_text(self) -> str:
        return "\n".join(m["content"] for call in self.calls for m in call["messages"])


def fact(id_, type_, label, value, unit, support, badge, section, **kw):
    return Fact(id=id_, type=type_, label=label, value=value, unit=unit, support=support, badge=badge,
                section=section, **kw)


@pytest.fixture
def facts():
    return [
        fact("F001", "feasibility_score", "Feasibility score", 86, "score", 3000, "High", "data"),
        fact("F002", "quality_band", "Quality band", 86, "score", 3000, "High", "data", text="High"),
        fact("F003", "cases", "Cases", 3000, "cases", 3000, "High", "data"),
        fact("F004", "case_duration_median", "Median case duration", 120.5, "hours", 3000, "High", "overview",
             ci_low=110.0, ci_high=130.0, meta={"case": "C00000042"}),
        fact("F005", "case_duration_p90", "90% of cases finish within", 400.0, "hours", 3000, "High", "overview"),
        fact("F006", "variant_share", "Create Request → Approve Request → Pay Invoice", 0.452, "share", 1356,
             "High", "variants", stability=1.0,
             meta={"rank": 1, "path": ["Create Request", "Approve Request", "Pay Invoice"]}),
        fact("F007", "bottleneck_wait", "Wait from Receive Invoice to Pay Invoice", 240.0, "hours", 120,
             "Medium", "bottlenecks", meta={"rank": 1, "src": "Receive Invoice", "dst": "Pay Invoice"}),
        fact("F008", "handover", "user_17 → user_42", 400, "count", 350, "High", "resources",
             meta={"from": "user_17", "to": "user_42"}),
        fact("F009", "rework_share", "Approve Request repeated within a case", 0.11, "share", 25,
             "Exploratory", "rework", meta={"activity": "Approve Request"}),
    ]


def answer(summary=None, findings=None, recommendations=None) -> str:
    return json.dumps({
        "summary": summary or [{"text": "The median case takes {{F004}}.", "facts": ["F004"]}],
        "findings": findings or [{"text": "The most common path covers {{F006}} of cases.", "facts": ["F006"]}],
        "recommendations": recommendations or [{"text": "Review the wait before Pay Invoice ({{F007}}).",
                                                "facts": ["F007"]}],
    })


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def test_valid_answer_is_accepted(facts):
    client = FakeClient(answer())
    r = write_summary("job-1", facts, "High", client=client)
    assert r.source == "ai" and r.attempts == 1 and len(client.calls) == 1
    by_id = {f.id: f for f in facts}
    assert r.summary[0]["text"] == f"The median case takes {format_fact(by_id['F004'])}."
    assert "[Medium]" in r.recommendations[0]["text"]          # Medium facts show their badge
    assert not any("{{" in x["text"] for x in r.summary + r.findings + r.recommendations)
    assert r.scores["fallback_used"] == 0 and r.scores["numbers_grounded"] == 1


def test_typed_digit_is_retried(facts):
    bad = answer(summary=[{"text": "A case costs 5 days ({{F004}}).", "facts": ["F004"]}])
    client = FakeClient(bad, answer())
    r = write_summary("job-1", facts, "High", client=client)
    assert r.source == "ai" and r.attempts == 2
    retry = client.calls[1]["messages"][-1]["content"]
    assert "broke these rules" in retry and "typed digit" in retry


def test_always_invalid_falls_back_to_templates(facts):
    unknown = answer(findings=[{"text": "Something about {{F099}}.", "facts": ["F099"]}])
    client = FakeClient(unknown)
    r = write_summary("job-1", facts, "High", client=client)
    assert r.source == "template" and r.attempts == MAX_RETRIES + 1 == len(client.calls)
    assert r.scores["fallback_used"] == 1 and r.scores["citations_valid"] == 0
    assert any("F099" in e for e in r.errors)
    assert r.summary and not r.recommendations


def test_causal_phrase_is_rejected(facts):
    by_id = {f.id: f for f in facts}
    out = json.loads(answer(findings=[{"text": "Long waits are caused by {{F007}}.", "facts": ["F007"]}]))
    errors = validate(out, set(by_id), by_id)
    assert any("caused by" in e and "is associated with" in e for e in errors)


def test_exploratory_fact_needs_a_hedge(facts):
    by_id = {f.id: f for f in facts}
    flat = json.loads(answer(findings=[{"text": "Approve Request is repeated in {{F009}} of its cases.",
                                        "facts": ["F009"]}]))
    assert any("Exploratory" in e and "hedge" in e for e in validate(flat, set(by_id), by_id))
    hedged = json.loads(answer(findings=[{"text": "Approve Request may be repeated in {{F009}} of its cases.",
                                          "facts": ["F009"]}]))
    assert validate(hedged, set(by_id), by_id) == []


def test_band_low_never_calls_the_ai(facts):
    client = FakeClient(answer())
    r = write_summary("job-1", facts, "Low", client=client)
    assert r.source == "template" and client.calls == [] and "Low" in r.reason


def test_band_medium_sends_only_high_facts(facts):
    client = FakeClient(answer(recommendations=[{"text": "Check the path covering {{F006}}.", "facts": ["F006"]}]))
    write_summary("job-1", facts, "Medium", client=client)
    sent_ids = set(re.findall(r"^(F\d{3}) \|", client.sent_text(), re.MULTILINE))
    by_id = {f.id: f for f in facts}
    assert sent_ids and all(by_id[i].badge == "High" for i in sent_ids)
    assert "F007" not in sent_ids and "F009" not in sent_ids


def test_no_case_or_resource_values_reach_the_ai(facts):
    client = FakeClient(answer())
    write_summary("job-1", facts, "High", client=client)
    text = client.sent_text()
    for secret in ("C00000042", "user_17", "user_42"):
        assert secret not in text
    assert "F008" not in {f.id for f in select_facts(facts, "High", 60)}
    assert "F002" not in {f.id for f in select_facts(facts, "High", 60)}   # internals are not findings


def test_ai_switched_off_uses_templates(facts):
    r = write_summary("job-1", facts, "High")          # no client, AI_ENABLED=false (conftest)
    assert r.source == "template" and "switched off" in r.reason and r.attempts == 0


def test_observability_is_a_safe_no_op():
    assert observability.enabled() is False
    with observability.trace("job-1", ["report"], {"a": 1}) as trace_id:
        assert trace_id is None
        observability.score("schema_valid", 1)
    observability.score("user_feedback", 1, trace_id="abc")
    observability.flush()
    messages, version = observability.get_prompt("pm-report-writer", "production", [{"role": "system",
                                                                                      "content": "x"}])
    assert version == "local" and messages[0]["content"] == "x"


def test_mask():
    masked = observability.mask("Mail louise.jones@example.com, call 0123456789, ask Louise Jones. "
                                "Approve Request stays.")
    assert "example.com" not in masked and "0123456789" not in masked and "Louise Jones" not in masked
    assert masked.count("[redacted]") == 3 and "Approve Request" in masked


def test_ids_in_activity_names_are_masked_before_the_ai(facts):
    facts.append(fact("F010", "rework_share", "PHR0243855 Case Tracking repeated within a case", 0.2, "share",
                      400, "High", "rework", meta={"rank": 1, "activity": "PHR0243855 Case Tracking"}))
    client = FakeClient(answer())
    r = write_summary("job-1", facts, "High", client=client)   # 1 of 5 activity names has an ID: allowed
    text = client.sent_text()
    assert r.source == "ai"
    assert "PHR[id] Case Tracking" in text and "0243855" not in text


def test_many_ids_in_activity_names_use_templates(facts):
    names = [f"Step {c}" for c in "ABCDEFG"] + ["Ticket 123456 Review", "Mail ops@example.com Check",
                                                "ID 9876543 Close"]
    many = [fact(f"F{i:03d}", "rework_share", f"{n} repeated within a case", 0.1, "share", 400, "High",
                 "rework", meta={"rank": i, "activity": n}) for i, n in enumerate(names, 20)]
    client = FakeClient(answer())
    r = write_summary("job-1", facts[:6] + many, "High", client=client)   # 3 of 10 names have IDs
    assert r.source == "template" and client.calls == []
    assert r.reason == "activity names contain IDs or contact details"


def test_job_activities_stay_readable_in_langfuse_masking():
    assert observability.mask("Block Invoice") == "[redacted]"                 # no allow-list: masked
    assert observability.mask("Block Invoice", {"Block Invoice"}) == "Block Invoice"
    assert observability.mask("Wait before Block Invoice by Louise Jones", {"Block Invoice"}) \
        == "Wait before Block Invoice by [redacted]"
    with observability.allowed_names({"Block Invoice"}):                        # what trace() sets up
        masked = observability._mask_data(data={"step": "Block Invoice", "mail": "a.b@example.com"})
    assert masked == {"step": "Block Invoice", "mail": "[redacted]"}
    assert observability._mask_data(data="Block Invoice") == "[redacted]"       # list reset afterwards


def test_report_uses_the_ai_summary(tmp_path):
    """End to end with a fake AI that cites real fact IDs from the table it receives."""
    from test_report import make_job
    from app.core import jobs
    from app.core.narrative import load_narrative
    from app.core.report import generate_report

    def reply(messages):
        ids = re.findall(r"^(F\d{3}) \|", messages[-1]["content"], re.MULTILINE)
        return json.dumps({"summary": [{"text": f"The analysis covers {{{{{ids[0]}}}}}.", "facts": [ids[0]]}],
                           # Each number says what it measures (B7: "Another fact is {{F002}}" is rejected).
                           "findings": [{"text": f"Another measure of the cases is {{{{{ids[1]}}}}}.", "facts": [ids[1]]}],
                           "recommendations": [{"text": f"Check the step that takes {{{{{ids[2]}}}}} with the team.",
                                                "facts": [ids[2]]}]})

    job = make_job(tmp_path, 3000)
    paths = generate_report(job["job_id"], client=FakeClient(reply))
    html = paths["html"].read_text(encoding="utf-8")
    assert "Written by AI from" in html and "<h3>Recommendations</h3>" in html and "{{F" not in html
    story = load_narrative(jobs.job_folder(job["job_id"]))
    assert story.source == "ai" and story.attempts == 1 and paths["narrative"].exists()

    # Forced templates: provenance says why, and no recommendations block.
    paths = generate_report(job["job_id"], use_ai=False)
    html = paths["html"].read_text(encoding="utf-8")
    assert "Generated from fixed templates (AI not used: switched off for this run)" in html
    # The template now writes up to 3 recommendations too (report section 3).
    assert "{{F" not in html
