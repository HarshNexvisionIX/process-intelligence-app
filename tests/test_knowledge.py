"""Knowledge sources: extraction (local, mocked media tools), statements, belief, conflicts and
questions for the experts. No network and no model downloads.

Run with:  pytest -q tests/test_knowledge.py
"""
import json
from datetime import date, timedelta

import pytest

from app.core import definition as dmod, knowledge
from test_narrative import FakeClient

VTT = """WEBVTT

00:00:05.000 --> 00:00:09.000
<v Priya Sharma>First I open Offer Processing in Workday.

00:01:10.500 --> 00:01:14.000
<v John Doe>Then Priya checks the Vetting result.
"""
SRT = """1
00:00:02,000 --> 00:00:04,000
Priya Sharma: Offer Processing comes first.

2
00:12:41,000 --> 00:12:45,000
John Doe: After that we do Hire Processing.
"""
SOP = ("Offer Processing comes before Hire Processing. Hire Processing is done by the HR Partner. "
       "Payroll Activation is mandatory. Hire Processing must be finished within 48 hours.")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def defn() -> dmod.ProcessDefinition:
    return dmod.ProcessDefinition.from_dict({"name": "Onboarding", "stages": [{"id": "A", "name": "All"}],
        "steps": [{"id": "S1", "name": "Offer Processing", "stage_id": "A", "position": 1},
                  {"id": "S2", "name": "Vetting", "stage_id": "A", "position": 2},
                  {"id": "S3", "name": "Hire Processing", "stage_id": "A", "position": 3},
                  {"id": "S4", "name": "Payroll Activation", "stage_id": "A", "position": 4}]})


def stmt(type_, subject, obj="", value="", quote="x", anchor="p.1", source="S001", r=0.9, **kw):
    return {"type": type_, "subject": subject, "object": obj, "value": value, "quote": quote, "anchor": anchor,
            "source_id": source, "reliability": r, **kw}


def test_vtt_and_srt_keep_timestamps_and_mask_speakers():
    vtt = knowledge.transcript_text("meeting.vtt", VTT.encode())
    assert "[00:00:05] Speaker A: First I open Offer Processing in Workday." in vtt
    assert "[00:01:10] Speaker B: Then Speaker A checks the Vetting result." in vtt
    srt = knowledge.transcript_text("call.srt", SRT.encode(), roles=["HR Partner", "Recruiter"])
    assert "[00:12:41] Recruiter: After that we do Hire Processing." in srt
    for text in (vtt, srt):
        assert "Priya" not in text and "Sharma" not in text and "John" not in text
    assert {"00:00:02", "00:12:41"} <= knowledge.anchors_of(srt)


def test_video_with_mocked_tools_and_missing_tools(monkeypatch):
    monkeypatch.setattr(knowledge, "tools_status", lambda: {"faster-whisper": True, "ffmpeg": True, "tesseract": True})
    monkeypatch.setattr(knowledge, "transcribe_audio", lambda path: [(12.0, "We open Offer Processing")])
    monkeypatch.setattr(knowledge, "ocr_frames", lambda path: [(30.4, "Workday Home Hire")])
    src, messages = knowledge.add_source("Onboarding", "video", "walk.mp4", b"fake video", walkthrough=True)
    text = knowledge.source_text("Onboarding", src["id"])
    assert "[00:00:12] We open Offer Processing" in text and "[00:00:30] screen: Workday Home Hire" in text
    assert messages == [] and src["reliability"] == 0.8

    monkeypatch.setattr(knowledge, "tools_status", lambda: {"faster-whisper": False, "ffmpeg": False, "tesseract": False})
    src2, messages = knowledge.add_source("Onboarding", "video", "walk2.mp4", b"fake video")
    assert any("faster-whisper" in m and "pip install" in m for m in messages)
    assert any("ffmpeg" in m and "winget" in m for m in messages)
    assert knowledge.source_text("Onboarding", src2["id"]) == ""


def test_statement_extraction_validates_quotes_anchors_and_names():
    src, _ = knowledge.add_source("Onboarding", "sop", "sop.txt", SOP.encode())
    reply = json.dumps({"statements": [
        {"type": "order", "subject": "Offer Processing", "object": "Hire Processing",
         "quote": "Offer Processing comes before Hire Processing", "anchor": "p.1"},
        {"type": "order", "subject": "Offer Processing", "object": "Vetting",
         "quote": "Offer Processing always comes before Vetting", "anchor": "p.1"},            # quote not in text
        {"type": "mandatory", "subject": "Payroll Activation", "quote": "Payroll Activation is mandatory",
         "anchor": "p.9"},                                                                      # anchor missing
        {"type": "step_exists", "subject": "Background Check", "quote": "Hire Processing is done by the HR Partner",
         "anchor": "p.1"},                                                                      # new step: allowed
        {"type": "mandatory", "subject": "Background Check", "quote": "Payroll Activation is mandatory",
         "anchor": "p.1"},                                                                      # new name: not allowed
        {"type": "sla", "subject": "Hire Processing", "value": "48",
         "quote": "Hire Processing must be finished within 48 hours", "anchor": "p.1"},
        {"type": "sla", "subject": "Hire Processing", "value": "12",
         "quote": "Hire Processing must be finished within 48 hours", "anchor": "p.1"}]})     # number not in quote
    kept, dropped, _ = knowledge.extract_statements("Onboarding", src["id"], defn(), client=FakeClient(reply))
    assert [(s["type"], s["subject"]) for s in kept] == [("order", "Offer Processing"),
                                                         ("step_exists", "Background Check"), ("sla", "Hire Processing")]
    assert kept[1]["new_name"] is True and kept[2]["value"] == "48"
    assert len(dropped) == 4
    assert any("quote not found" in d for d in dropped) and any("anchor" in d for d in dropped)
    assert any("unknown step 'Background Check'" in d for d in dropped)
    assert knowledge.load_statements("Onboarding", src["id"]) == kept


def test_without_ai_policy_rules_and_manual_statements():
    csv = "Step,Responsible,SLA (hours)\nHire Processing,HR Partner,48\nOffer Processing,Recruiter,24\n"
    src, _ = knowledge.add_source("Onboarding", "policy", "raci.csv", csv.encode())
    kept, _, messages = knowledge.extract_statements("Onboarding", src["id"], defn())
    assert ("owner", "Hire Processing", "HR Partner") in {(s["type"], s["subject"], s["object"]) for s in kept}
    assert ("sla", "Offer Processing", "24") in {(s["type"], s["subject"], s["value"]) for s in kept}
    sop, _ = knowledge.add_source("Onboarding", "sop", "sop.txt", SOP.encode())
    _, _, messages = knowledge.extract_statements("Onboarding", sop["id"], defn())
    assert any("by hand" in m for m in messages)
    knowledge.add_manual_statement("Onboarding", sop["id"], {"type": "mandatory", "subject": "Payroll Activation",
                                                            "quote": "Payroll Activation is mandatory", "anchor": "p.1"},
                                   defn())
    with pytest.raises(ValueError, match="quote not found"):
        knowledge.add_manual_statement("Onboarding", sop["id"], {"type": "optional", "subject": "Vetting",
                                                                "quote": "Vetting is optional", "anchor": "p.1"}, defn())


def test_belief_from_two_sources_and_stale_down_weighting():
    merged = knowledge.merge([stmt("order", "Offer Processing", "Hire Processing", r=0.9, source="S001"),
                              stmt("order", "Offer Processing", "Hire Processing", r=0.6, source="S002"),
                              stmt("order", "Offer Processing", "Hire Processing", r=0.6, source="S002")])
    assert merged["keys"]["order:Offer Processing<Hire Processing"]["belief"] == pytest.approx(1 - 0.1 * 0.4)
    old = (date.today() - timedelta(days=800)).isoformat()
    src = knowledge.with_effective_reliability({"reliability": 0.9, "recorded_on": old})
    assert src["stale"] and src["effective_reliability"] == pytest.approx(0.72)
    assert knowledge.belief([0.99, 0.99, 0.99]) == 0.99


def test_opposite_orders_make_one_conflict_resolved_by_an_expert():
    src, _ = knowledge.add_source("Onboarding", "sop", "sop.txt", SOP.encode())
    other, _ = knowledge.add_source("Onboarding", "transcript", "call.srt", SRT.encode())
    knowledge.save_statements("Onboarding", src["id"], [stmt("order", "Offer Processing", "Vetting", source=src["id"])])
    knowledge.save_statements("Onboarding", other["id"], [stmt("order", "Vetting", "Offer Processing", source=other["id"],
                                                                anchor="00:12:41")])
    merged = knowledge.merge(knowledge.all_statements("Onboarding"))
    assert len(merged["conflicts"]) == 1 and merged["conflicts"][0]["status"] == "open"
    decision = knowledge.resolve_conflict("Onboarding", merged["conflicts"][0], "order:Offer Processing<Vetting")
    assert decision["kind"] == "sme_answer" and decision["reliability"] == 0.95
    merged = knowledge.merge(knowledge.all_statements("Onboarding"))
    assert merged["conflicts"][0]["status"] == "resolved"
    assert "order:Vetting<Offer Processing" not in merged["keys"]
    assert merged["keys"]["order:Offer Processing<Vetting"]["belief"] == 0.99      # 1 - 0.1 x 0.05, capped


def test_questions_for_conflict_contradiction_and_undocumented_activity():
    d = defn()
    merged = knowledge.merge([stmt("order", "Offer Processing", "Vetting", source="S002"),
                              stmt("order", "Vetting", "Offer Processing", source="S004"),
                              stmt("mandatory", "Payroll Activation", source="S001")])
    evidence = {"claims": [{"id": "C001", "kind": "mandatory", "verdict": "Contradicted", "value": 0.16,
                            "refs": {"step_id": "S4"}, "support": 500}]}
    qs = knowledge.questions(d, merged, evidence, None, {"Case Tracking": 0.64}, {"S001": "sop"})
    texts = [q["text"] for q in qs]
    assert any(t.startswith("Source S002 says Offer Processing before Vetting, S004 says after") or
               ("S002" in t and "S004" in t and "which is right" in t) for t in texts)
    assert "The SOP says Payroll Activation is mandatory, but the data shows it in 16% of cases — is it done " \
           "elsewhere or optional?" in texts
    assert "The data shows Case Tracking in 64% of cases but no source mentions it — what is it and where does it " \
           "belong?" in texts
    back = knowledge.answers_from_excel(_answered(knowledge.questions_excel(qs)))
    assert len(back) == len(qs) and back[0]["answer"] == "Answer 1"
    src = knowledge.add_answers("Onboarding", back)
    assert src["kind"] == "sme_answer" and "[q1] Question:" in knowledge.source_text("Onboarding", src["id"])


def _answered(xlsx: bytes) -> bytes:
    import io
    import pandas as pd
    df = pd.read_excel(io.BytesIO(xlsx), dtype=str)
    df["answer"] = [f"Answer {i}" for i in range(1, len(df) + 1)]
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()
