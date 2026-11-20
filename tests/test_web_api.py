"""The web API (app/api): the whole no-case flow, and approve-before-send for AI requests.

The AI is a stand-in (Gateway._call patched): nothing leaves the machine in tests."""
import json
import re
import yaml
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "test_datasets" / "icc_like_A" / "icc_like_clickstream_A.parquet"
PACK = Path(__file__).resolve().parent / "nocase" / "packs" / "icc_sme" / "pack.yaml"


_C = {"headline": "h", "interpretation": "i", "evidence_id": "E02"}
READOUT = {"headline": "Most effort sits in calculation and checking", "transformation_objective": "Shorten the close.",
           "executive_message": "Standardise the heaviest steps first.",
           "findings": [_C, _C], "risks": [_C], "orchestrator_role": "Coordinates the agents.",
           "interventions": [{"title": "t", "evidence_id": "E02", "proposed_cause": "c", "proposed_change": "x",
                              "kpi_to_test": "k", "validation_gate": "g", "priority": "High"}] * 2,
           "agents": [{"title": "a", "role": "r", "actions": ["x", "y"], "human_control": "h"}],
           "outcomes": [{"title": "o", "target_direction": "down", "kpi": "k", "how_to_measure": "m",
                         "scale_gate": "s"}] * 2, "decisions": ["d"]}


AIMAP = {"headline": "Prepare, calculate, post", "explanation": "A short hypothesis.", "evidence": ["e"],
         "limitations": ["l"], "proposed_steps": [{"id": "S1", "label": "Prepare", "explanation": "x"},
                                                  {"id": "S2", "label": "Calculate", "explanation": "y"}],
         "proposed_edges": [{"source": "S1", "target": "S2", "rationale": "r"}], "confidence": "Low"}


STAGE_AI = {"stage_business_summary": "People prepare and check the model.",
            "key_work_patterns": [{"title": "Model work", "business_meaning": "m", "application_roles": "a", "evidence_patterns": "e"}],
            "proposed_level4_steps": [{"name": "Prepare", "rationale": "r", "supporting_evidence": "s"}],
            "possible_friction_points": ["f"], "questions_for_sme": ["q"], "confidence": "Low",
            "evidence_limitations": "Aggregates only."}


def _wait(c, jid):
    for _ in range(240):
        r = c.get(f"/api/jobs/{jid}/run").json()
        if not r.get("running"):
            return r
        time.sleep(0.5)
    raise AssertionError("run did not finish")


@pytest.mark.skipif(not DATA.exists(), reason="test dataset not present")
def test_web_flow_with_approval_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.api.server import app
    from app.nocase import ai_gateway, library
    sent = []
    sent_readout = []

    def fake_call(self, task, messages, prefer):
        sent.append(task)
        if task == "nc_assign_rules":
            ids = sorted(set(re.findall(r"\bS\d{3}\b", messages[-1]["content"])))
            first = yaml.safe_load(PACK.read_text(encoding="utf-8"))["stages"][0]["steps"][0]["name"]
            return json.dumps({"assign": [{"unit": i, "step": first if n % 2 else "OTHER", "why": "test"}
                                          for n, i in enumerate(ids)]}), "fake", "fake-1"
        if task == "nc_readout":
            sent_readout.append(messages)
            return json.dumps(READOUT), "fake", "fake-1"
        if task == "nc_process_map":
            return json.dumps(AIMAP), "fake", "fake-1"
        if task == "nc_stage_explain":
            u = messages[-1]["content"]
            acts = [a["activity"] for a in json.loads(u.split("\n", 1)[1]).get("level4_activities", [])]
            return json.dumps({**STAGE_AI, "level4_summaries": [{"activity": a, "summary": "They do it.",
                                                                  "evidence_patterns": "p"} for a in acts]}), "fake", "fake-1"
        return "{}", "fake", "fake-1"
    monkeypatch.setattr(ai_gateway.Gateway, "why_not", lambda self, task: None)
    monkeypatch.setattr(ai_gateway.Gateway, "_call", fake_call)

    c = TestClient(app)
    with open(DATA, "rb") as f:
        r = c.post("/api/jobs", files=[("files", ("a.parquet", f))], data={"dataset_class": "public"})
    assert r.status_code == 200, r.text
    d = r.json()
    jid = d["job"]["job_id"]
    assert d["profile"]["detection"]["workflow"] == "nocase"
    r = c.post(f"/api/jobs/{jid}/convert/nocase", json={"columns": d["profile"]["nocase"]["suggested"],
                                                       "dataset_class": "public"})
    assert r.status_code == 200, r.text
    assert c.post(f"/api/jobs/{jid}/run", json={}).status_code == 400            # no process yet
    key = c.post(f"/api/jobs/{jid}/process", json={"new_name": "ICC test"}).json()["process"]
    text = PACK.read_text(encoding="utf-8").replace('source: "test pack"', 'source: "SME review"')
    (library.pack_dir(key) / "pack.yaml").write_text(text, encoding="utf-8")
    assert c.post(f"/api/jobs/{jid}/report/generate").status_code == 400         # business context is required
    assert c.post(f"/api/jobs/{jid}/context/confirm", json={"by": "T", "no_documents_reason": "test"}).status_code == 200

    # 1. without AI: nothing is sent, the requests are kept as packages
    assert c.post(f"/api/jobs/{jid}/run", json={"use_ai": False}).status_code == 200
    assert _wait(c, jid)["ok"]
    assert sent == []
    pk = c.get(f"/api/jobs/{jid}/ai/analysis").json()
    tasks = {t["task"]: t for t in pk["tasks"]}
    assert "nc_assign_rules" in tasks and not tasks["nc_assign_rules"]["approved"]
    p0 = next(p for p in pk["packages"] if p["task"] == "nc_assign_rules")
    assert len(p0["fingerprint"]) == 64 and p0["guard"] == "passed"
    assert c.post(f"/api/jobs/{jid}/run", json={"use_ai": True}).status_code == 400   # nothing approved

    # 2. approval needs the exact current packages
    bad = c.post(f"/api/jobs/{jid}/ai/analysis/approve", json={"task": "nc_assign_rules", "task_fingerprint": "x" * 64,
                                                              "by": "T"})
    assert bad.status_code == 400
    ok = c.post(f"/api/jobs/{jid}/ai/analysis/approve", json={
        "task": "nc_assign_rules", "task_fingerprint": tasks["nc_assign_rules"]["task_fingerprint"], "by": "Tester"})
    assert ok.status_code == 200

    # 3. with AI: only the approved task is sent, and every request sent is logged
    assert c.post(f"/api/jobs/{jid}/run", json={"use_ai": True}).status_code == 200
    assert _wait(c, jid)["ok"]
    assert sent and set(sent) == {"nc_assign_rules"}
    audit = c.get(f"/api/jobs/{jid}/ai/analysis").json()["audit"]
    assert audit and audit[0]["approved_by"] == "Tester" and audit[0]["matched"].startswith("exact")

    # the rest of the pages answer
    m = c.get(f"/api/jobs/{jid}/map?level=step").json()
    assert m["nodes"] and all(isinstance(x, str) for x in m["missing"])
    assert c.get(f"/api/jobs/{jid}/map?level=stage").status_code == 200
    assert c.get(f"/api/jobs/{jid}/map?level=activity").status_code == 200
    assert c.get(f"/api/jobs/{jid}/insights").json()["available"]
    assert c.get(f"/api/jobs/{jid}/review").status_code == 200
    sm = c.get(f"/api/system-map?job_id={jid}").json()
    assert {n["id"] for n in sm["nodes"]} >= {"N1", "E4", "G1", "R4"}
    assert c.post(f"/api/jobs/{jid}/report/generate").status_code == 200
    assert c.get(f"/api/jobs/{jid}/report.pdf").content[:4] == b"%PDF"

    # AI readout: prepared (nothing sent), approved, written, checked, PDF
    sent.clear()
    assert c.post(f"/api/jobs/{jid}/readout/generate").status_code == 400          # not approved yet
    assert c.post(f"/api/jobs/{jid}/readout/prepare").json()["prepared"] == 1 and sent == []
    t = {x["task"]: x for x in c.get(f"/api/jobs/{jid}/ai/readout").json()["tasks"]}["nc_readout"]
    assert c.post(f"/api/jobs/{jid}/ai/readout/approve", json={"task": "nc_readout", "task_fingerprint":
                                                                t["task_fingerprint"], "by": "Tester"}).status_code == 200
    r = c.post(f"/api/jobs/{jid}/readout/generate")
    assert r.status_code == 200, r.text
    assert sent == ["nc_readout"] and r.json()["report"]["headline"]
    got = c.get(f"/api/jobs/{jid}/readout").json()
    assert got["readout"] and not got["stale"]
    assert c.get(f"/api/jobs/{jid}/readout.pdf").content[:4] == b"%PDF"
    # the request is ProcessLens's: its user line, then the transformation context as compact JSON (E01 ...)
    user = sent_readout[0][-1]["content"]
    assert user.startswith("Use this evidence catalog to write a concise seven-slide transformation readout:\n")
    ctx = json.loads(user.split("\n", 1)[1])
    assert ctx["schema"] == "processlens-transformation-readout-v1"
    assert set(ctx) >= {"evidence_status", "scope", "evidence_catalog", "case_stage_summary",
                        "no_case_navigation_categories", "rules"}
    cat = {e["id"]: e for e in ctx["evidence_catalog"]}
    assert {"E01", "E02", "E05"} <= set(cat) and set(cat["E01"]) == {"id", "topic", "observed_fact", "limitation"}
    summary = got["evidence"]["summary_payload"]
    assert summary["schema_version"] == "1.1" and summary["event_count"] > 0
    assert set(summary) >= {"evidence_scope", "days_in_dataset", "participant_count", "case_coverage_pct",
                            "recorded_or_estimated_hours", "application_type_totals", "application_class_totals",
                            "activity_class_totals", "case_flow", "no_case_navigation", "no_case_semantic_navigation",
                            "title_categories", "data_limitations", "operational_signals"}
    raw_apps = {a["app_label"] for a in json.loads((tmp_path / "data" / "jobs" / jid / "nc_measures.json")
                                                   .read_text(encoding="utf-8"))["apps"]}
    assert not any(f'"{a}"' in user for a in raw_apps)          # application names leave only as generic classes

    # AI-enhanced conceptual map (ProcessLens): summary shown, approved, generated
    am = c.get(f"/api/jobs/{jid}/ai-map?scope=navigation&budget=compact").json()
    assert am["ready"] and am["payload"]["purpose"] == "process_map_hypothesis_not_observed_case_truth"
    assert "case_flow" not in am["payload"] and "nodes" in am["payload"]["no_case_navigation"]
    assert c.post(f"/api/jobs/{jid}/ai-map/generate", json={"scope": "navigation"}).status_code == 400
    assert c.post(f"/api/jobs/{jid}/ai-map/prepare", json={"scope": "navigation"}).json()["prepared"] == 1
    t = {x["task"]: x for x in c.get(f"/api/jobs/{jid}/ai/map").json()["tasks"]}["nc_process_map"]
    assert c.post(f"/api/jobs/{jid}/ai/map/approve", json={"task": "nc_process_map", "task_fingerprint":
                                                            t["task_fingerprint"], "by": "Tester"}).status_code == 200
    assert c.post(f"/api/jobs/{jid}/ai-map/generate", json={"scope": "combined"}).status_code == 400  # other summary
    r = c.post(f"/api/jobs/{jid}/ai-map/generate", json={"scope": "navigation"})
    assert r.status_code == 200, r.text
    assert r.json()["report"]["proposed_steps"] and c.get(f"/api/jobs/{jid}/ai-map").json()["saved"]

    # selecting a step: measured Level 4 summaries, then "Explain this step with AI" (ProcessLens), approved first
    step = max((n for n in m["nodes"] if not n.get("missing")), key=lambda n: n.get("hours") or 0)["id"]
    st = c.get(f"/api/jobs/{jid}/stage", params={"step": step}).json()
    assert st["kind"] == "step" and st["explanation"] is None
    l4 = st["level4"]["activities"]
    assert l4 and all(a["summary"].startswith(a["activity"] + ":") for a in l4)
    sa = c.get(f"/api/jobs/{jid}/stage-ai", params={"step": step}).json()
    p = sa["payload"]
    assert p["schema_version"] == "1.0" and p["stage"] == step and len(sa["fingerprint"]) == 64
    assert [a["activity"] for a in p["level4_activities"]] == [a["activity"] for a in l4][:len(p["level4_activities"])]
    sent.clear()
    assert c.post(f"/api/jobs/{jid}/stage-ai/generate", json={"step": step}).status_code == 400
    assert c.post(f"/api/jobs/{jid}/stage-ai/prepare", json={"step": step}).json()["prepared"] == 1 and sent == []
    t = {x["task"]: x for x in c.get(f"/api/jobs/{jid}/ai/stage").json()["tasks"]}["nc_stage_explain"]
    assert c.post(f"/api/jobs/{jid}/ai/stage/approve", json={"task": "nc_stage_explain", "task_fingerprint":
                                                              t["task_fingerprint"], "by": "Tester"}).status_code == 200
    assert c.post(f"/api/jobs/{jid}/stage-ai/generate", json={"step": step, "budget": "detailed"}).status_code == 400
    r = c.post(f"/api/jobs/{jid}/stage-ai/generate", json={"step": step})
    assert r.status_code == 200, r.text
    assert sent == ["nc_stage_explain"] and r.json()["report"]["stage_business_summary"]
    ex = c.get(f"/api/jobs/{jid}/stage", params={"step": step}).json()["explanation"]["report"]
    assert {x["activity"] for x in ex["level4_summaries"]} == {a["activity"] for a in p["level4_activities"]}


def test_api_never_takes_keys_or_models():
    """The web API has no route or field for an AI key or a model choice."""
    src = (Path(__file__).resolve().parents[1] / "app" / "api" / "server.py").read_text(encoding="utf-8")
    assert "api_key" not in src and "BaseModel):\n    model" not in src and "model: str" not in src
    web = Path(__file__).resolve().parents[1] / "web" / "src"
    if web.exists():
        for f in web.rglob("*.js*"):
            t = f.read_text(encoding="utf-8")
            assert "api_key" not in t and "apiKey" not in t, f


def test_names_written_with_a_role_are_masked():
    from app.nocase.payloads import problems, scrub_text
    assert scrub_text("owner: Sophie Turner (ICC Lead)")[0] == "owner: <name> (ICC Lead)"
    assert scrub_text("Raj Patel (Finance Manager) approves")[0].startswith("<name>")
    assert problems("Prepared by Mark O'Neil") == ["a person's name"]
    for ok in ("Send to Group Finance", "Check the Recharge Input Check (filters, sorting) workbook",
               "Accounts Payable handles supplier queries"):
        assert scrub_text(ok)[0] == ok and not problems(ok)


def test_readout_answers_are_checked():
    from app.api.readout import check
    ev = {"steps_in_business_order": [{"step": "Validate inputs 2", "stage": "Stage A"}],
          "evidence_catalog": [{"id": "E02", "topic": "Work duration", "observed_fact": "143.1 hours.",
                                "limitation": "l"}]}
    assert check(json.loads(json.dumps(READOUT)), ev) == []
    bad = json.loads(json.dumps(READOUT))
    bad["findings"][0]["interpretation"] = "It takes 143 hours"
    bad["risks"][0]["evidence_id"] = "E99"
    problems = check(bad, ev)
    assert any("number" in p for p in problems) and any("evidence_id" in p for p in problems)
    ok_name = json.loads(json.dumps(READOUT))
    ok_name["findings"][0]["headline"] = "Validate inputs 2 is the heaviest step"     # digits inside a name are fine
    assert check(ok_name, ev) == []


def test_case_flow_summary_gate_and_results(tmp_path, monkeypatch):
    """Case ID: convert, quality, analysis, results, map; the report summary request is prepared (not sent),
    sent only after approval, logged, and the template summary is kept when the AI answer fails the checks."""
    import subprocess
    import sys
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    src = tmp_path / "case.parquet"
    subprocess.run([sys.executable, "scripts/generate_synthetic.py", "--cases", "300", "--apps", "--out", str(src)],
                   check=True, cwd=Path(__file__).resolve().parents[1], capture_output=True)
    from app.api.server import app
    from app.core import ai_client
    calls = []
    monkeypatch.setattr(ai_client.Session, "call", lambda self, messages, json_expected=True, max_output_tokens=None:
                        calls.append(1) or "not json")
    c = TestClient(app)
    with open(src, "rb") as f:
        d = c.post("/api/jobs", files=[("files", ("case.parquet", f))], data={"dataset_class": "public"}).json()
    jid = d["job"]["job_id"]
    assert d["profile"]["detection"]["workflow"] == "case"
    m = {k: v["column"] for k, v in d["profile"]["case"]["suggested"].items()}
    assert c.post(f"/api/jobs/{jid}/convert/case", json={"mapping": m}).status_code == 200
    assert c.post(f"/api/jobs/{jid}/quality").json()["band"] in ("High", "Medium")
    assert c.post(f"/api/jobs/{jid}/analysis").status_code == 400          # Business context first
    c.post(f"/api/jobs/{jid}/process", json={"new_name": "P2P test"})
    c.post(f"/api/jobs/{jid}/context/confirm", json={"by": "T", "no_documents_reason": "test"})
    assert c.post(f"/api/jobs/{jid}/analysis").status_code == 200
    res = c.get(f"/api/jobs/{jid}/case-results").json()["sections"]
    assert res["kpis"]["available"] and res["variants"]["data"]["variants"]
    mp = c.get(f"/api/jobs/{jid}/map").json()
    assert len(mp["nodes"]) > 3 and mp["edges"] and all(e["source"] != e["target"] for e in mp["edges"])
    assert c.get(f"/api/jobs/{jid}/insights").json()["available"]
    r = c.post(f"/api/jobs/{jid}/report/generate").json()
    assert r["prepared"] == 1 and calls == []                                   # prepared, nothing sent
    s = c.get(f"/api/jobs/{jid}/summary").json()
    assert s["available"] and not s["ai"] and s["summary"] and "waiting for your approval" in s["reason"]
    t = {x["task"]: x for x in c.get(f"/api/jobs/{jid}/ai/report").json()["tasks"]}["summary"]
    c.post(f"/api/jobs/{jid}/ai/report/approve", json={"task": "summary", "task_fingerprint": t["task_fingerprint"],
                                                       "by": "Tester"})
    assert c.post(f"/api/jobs/{jid}/report/generate?use_ai=true").status_code == 200
    assert calls                                                                # sent after approval
    audit = c.get(f"/api/jobs/{jid}/ai/report").json()["audit"]
    assert audit[0]["matched"].startswith("exact") and audit[0]["approved_by"] == "Tester"
    assert not c.get(f"/api/jobs/{jid}/summary").json()["ai"]                   # bad answer -> template kept
    assert c.get(f"/api/jobs/{jid}/facts.json").status_code == 200
    # ProcessLens summary for case data: opaque case steps, case metrics, E06/E07, no activity names sent
    am = c.get(f"/api/jobs/{jid}/ai-map?scope=case").json()
    flow = am["payload"]["case_flow"]
    assert flow["nodes"] and flow["nodes"][0]["id"] == "CaseStep_01" and flow["case_metrics"]["count"] > 0
    assert c.post(f"/api/jobs/{jid}/ai-map/prepare", json={"scope": "case"}).json()["prepared"] == 1
    ro = c.get(f"/api/jobs/{jid}/readout").json()["evidence"]
    ids = {e["id"] for e in ro["evidence_catalog"]}
    assert {"E01", "E06", "E07"} <= ids and ro["context"]["case_stage_summary"]["nodes"]
    # selecting a box of the case map: the activity, its Level 4 summaries and the step-explanation request
    box = next(n["id"] for n in mp["nodes"] if not n.get("boundary"))
    st = c.get(f"/api/jobs/{jid}/stage", params={"step": box}).json()
    assert st["kind"] == "case_step" and st["step"]["cases"] > 0 and "activities" in st["level4"]
    assert all(a["summary"] for a in st["level4"]["activities"])
    sa = c.get(f"/api/jobs/{jid}/stage-ai", params={"step": box}).json()
    assert sa["payload"]["stage"] == box and sa["payload"]["stage_summary"]["cases"] > 0
    assert c.post(f"/api/jobs/{jid}/stage-ai/prepare", json={"step": box}).json()["prepared"] == 1
    assert c.get(f"/api/jobs/{jid}/stage", params={"step": "no such step"}).status_code == 404
    acts = {n["label"] for n in mp["nodes"]}
    sent_text = json.dumps(ro["context"])
    assert not any(f'"{a}"' in sent_text for a in acts if len(a) > 3)


def test_order_and_activities_review(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import yaml
    from app.api.server import app
    from app.core import jobs
    from app.nocase import library
    c = TestClient(app)
    job = jobs.create_job("x.csv", None, "public")
    job.update(mode="nocase")
    jobs.save_job(job)
    jid = job["job_id"]
    key = c.post(f"/api/jobs/{jid}/process", json={"new_name": "Order test"}).json()["process"]
    pack = {"name": "Order test", "stages": [{"name": "A", "steps": [{"name": "s1"}, {"name": "s2"}]}]}
    (library.pack_dir(key) / "pack.yaml").write_text(yaml.safe_dump(pack), encoding="utf-8")
    rows = c.get(f"/api/jobs/{jid}/order").json()["rows"]
    assert [r["step"] for r in rows] == ["s1", "s2"]
    rows = [{**rows[1], "position": 1}, {**rows[0], "position": 2, "exception": "only when needed"}]
    assert c.put(f"/api/jobs/{jid}/order", json={"rows": rows, "by": "T"}).status_code == 200
    new = c.get(f"/api/jobs/{jid}/order").json()
    assert [r["step"] for r in new["rows"]] == ["s2", "s1"] and new["rows"][1]["exception"] == "only when needed"
    assert new["provenance"]["confirmed"]
    (library.pack_dir(key) / "activities.proposed.yaml").write_text(yaml.safe_dump(
        {"steps": {"s1": [{"label": "Open", "description": "d", "rules": []},
                          {"label": "Close", "description": "", "rules": []}]}}), encoding="utf-8")
    a = c.get(f"/api/jobs/{jid}/activities").json()
    assert a["proposed"][0]["id"] == "activity:s1:0"
    ok = c.post(f"/api/jobs/{jid}/activities/accept", json={"items": [{**a["proposed"][0], "activity": "Open file"}],
                                                            "by": "T"})
    assert ok.status_code == 200
    after = c.get(f"/api/jobs/{jid}/activities").json()
    assert any(g["activity"] == "Open file" for g in after["approved"])
    assert [g["activity"] for g in after["proposed"]] == ["Close"]          # the unselected group still waits


def test_business_process_map_without_and_with_analysis(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import yaml
    from app.api.server import app
    from app.core import jobs
    from app.nocase import library
    c = TestClient(app)
    job = jobs.create_job("x.csv", None, "public")
    job.update(mode="nocase")
    jobs.save_job(job)
    key = c.post(f"/api/jobs/{job['job_id']}/process", json={"new_name": "Map test"}).json()["process"]
    pack = {"name": "Map test", "stages": [{"name": "A", "steps": [{"name": "s1", "description": "d1"}]},
                                           {"name": "B", "steps": [{"name": "s2"}]}]}
    (library.pack_dir(key) / "pack.yaml").write_text(yaml.safe_dump(pack), encoding="utf-8")
    m = c.get(f"/api/processes/{key}/map").json()
    assert [n["id"] for n in m["nodes"]] == ["s1", "s2"] and m["nodes"][0]["group"] == "A"
    assert m["edges"] == [{"source": "s1", "target": "s2", "kind": "order"}] and m["analyses"] == []
    (jobs.job_folder(job["job_id"]) / "nc_flow.json").write_text("{}", encoding="utf-8")
    assert c.get(f"/api/processes/{key}/map").json()["analyses"][0]["job_id"] == job["job_id"]


def test_stages_and_steps_must_be_approved_before_the_analysis(tmp_path, monkeypatch):
    """No case ID: the run is refused and Business context is not done until stages and steps are approved; they
    can be approved (and edited) on Business context, with or without an AI proposal."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import yaml
    from app.api.server import app
    from app.core import jobs
    from app.nocase import library
    c = TestClient(app)
    job = jobs.create_job("x.csv", None, "public")
    job.update(mode="nocase")
    jobs.save_job(job)
    jid = job["job_id"]
    key = c.post(f"/api/jobs/{jid}/process", json={"new_name": "Gate test"}).json()["process"]
    assert c.post(f"/api/jobs/{jid}/context/confirm", json={"by": "T", "no_documents_reason": "none"}).status_code == 200
    ctx = c.get(f"/api/jobs/{jid}/context").json()
    assert ctx["pack"]["approved"] in (None, {}, []) or not ctx["pack"]["approved"].get("stages")
    steps = {s["key"]: s for s in c.get(f"/api/jobs/{jid}").json()["steps"]}
    assert not steps["context"]["done"]
    from app.api import server
    (server._folder(jid) / "nc_events.parquet").write_bytes(b"")       # imported (the gate is checked first)
    r = c.post(f"/api/jobs/{jid}/run", json={})
    assert r.status_code == 400 and "stages and steps" in r.json()["detail"]

    # nothing proposed and nothing typed: refused
    assert c.post(f"/api/processes/{key}/pack/approve", json={"by": "T"}).status_code == 400
    # typed by hand: duplicate names refused, then approved
    dup = [{"name": "A", "steps": [{"name": "s1"}, {"name": "s1"}]}]
    assert c.post(f"/api/processes/{key}/pack/approve", json={"by": "T", "stages": dup}).status_code == 400
    assert c.post(f"/api/processes/{key}/pack/approve", json={"by": "", "stages": dup}).status_code == 400
    ok = [{"name": "A", "steps": [{"name": "s1", "description": "first"}, {"name": "s2"}, {"name": ""}]},
          {"name": "Empty", "steps": []}]
    r = c.post(f"/api/processes/{key}/pack/approve", json={"by": "T", "stages": ok})
    assert r.status_code == 200 and r.json()["steps"] == 2
    pack = library.load_pack(key)
    assert [st["name"] for st in pack["stages"]] == ["A"] and pack["provenance"]["confirmed"]
    steps = {s["key"]: s for s in c.get(f"/api/jobs/{jid}").json()["steps"]}
    assert steps["context"]["done"]
    ids = [s["key"] for s in c.get(f"/api/jobs/{jid}").json()["steps"]]
    assert ids.index("nc_steps") < ids.index("nc_map") < ids.index("nc_report")   # review steps before the map

    # an AI proposal, edited before approval: the edit wins and the proposal is cleared
    prop = {"name": "Gate test", "stages": [{"name": "B", "steps": [{"name": "x1"}]}]}
    (library.pack_dir(key) / "pack.proposed.yaml").write_text(yaml.safe_dump(prop), encoding="utf-8")
    assert c.get(f"/api/jobs/{jid}/context").json()["pack"]["proposed"]
    edited = [{"name": "B", "steps": [{"name": "x1 renamed"}]}]
    assert c.post(f"/api/processes/{key}/pack/approve", json={"by": "T", "stages": edited}).status_code == 200
    assert [sp["name"] for sp in library.load_pack(key)["stages"][0]["steps"]] == ["x1 renamed"]
    assert not (library.pack_dir(key) / "pack.proposed.yaml").exists()


def test_readout_retry_is_small_and_gateway_uses_it():
    from app.api import readout as ro
    from app.nocase import ai_gateway
    ev = {"mode": "nocase", "steps_in_business_order": [{"step": "Validate inputs", "stage": "Stage A"}],
          "evidence_catalog": [{"id": f"E{i:02d}", "topic": f"Topic {i}", "observed_fact": "x" * 300,
                                "limitation": "l"} for i in range(1, 30)]}
    msgs = ro.retry_messages(ev, "{" + "y" * 50000 + "}", ["p1"] * 40)
    text = msgs[-1]["content"]
    assert "E29" in text and "x" * 300 not in text and len(text) < 20000 and text.count("- p1") == 15
    # allowed tokens with digits pass the check
    ok = json.loads(json.dumps(READOUT))
    ok["findings"][0]["headline"] = "Move to S/4HANA and Level 4 detail in Q3, see E02"
    assert ro.check(ok, ev) == []

    seen = []

    def fake(self, task, messages, prefer):
        seen.append(messages)
        return (json.dumps({"bad": 1}) if len(seen) == 1 else json.dumps({"good": 1})), "fake", "m"
    g = ai_gateway.Gateway(dataset_class="public")
    g.why_not = lambda task: None
    g._call = fake.__get__(g)
    ans = g.ask("nc_readout", [{"role": "user", "content": "big"}], lambda d: [] if d.get("good") else ["wrong"],
                retry=lambda prev, errs: [{"role": "user", "content": f"small retry: {errs[0]}"}])
    assert ans.data == {"good": 1} and seen[1] == [{"role": "user", "content": "small retry: wrong"}]


def test_stage_explanation_answers_are_checked():
    from app.api.stage_explain import check
    p = {"level4_activities": [{"activity": "Save model"}, {"activity": "Check totals"}]}
    good = {**STAGE_AI, "level4_summaries": [{"activity": "Save model", "summary": "s"},
                                             {"activity": "Check totals", "summary": "s"}]}
    assert check(good, p) == []
    bad = {**good, "level4_summaries": good["level4_summaries"][:1],
           "proposed_level4_steps": STAGE_AI["proposed_level4_steps"] * 5}
    problems = check(bad, p)
    assert any("Check totals" in x for x in problems) and any("four" in x for x in problems)
    assert check("text", p)


def test_case_steps_are_checked_against_the_documents_and_the_intended_flow_is_confirmed(tmp_path, monkeypatch):
    """Case ID: the data's own steps (Level 3, with their Level 2 stages) are the process as recorded; the documented
    steps check them (same / similar / not in the documents). The intended flow is prefilled from the documented
    order and the data, corrected and confirmed on Review steps; the map, conformance and report then use it."""
    import pandas as pd
    import yaml
    from app.core import jobs
    from app.nocase import library
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    rows = []
    for c in range(1100):
        seq = [("Intake", "Receive Request"), ("Intake", "Check Request"), ("Delivery", "Build Item"),
               ("Delivery", "Ship Item")] + ([("Delivery", "Chase Customer")] if c % 3 == 0 else [])
        for i, (stage, step) in enumerate(seq):
            for k, click in enumerate(("Open", "Save")):
                t = pd.Timestamp("2026-01-01") + pd.Timedelta(hours=7 * c + 2 * i, minutes=5 * k)
                rows.append({"Case ID": f"C{c}", "Process Level 1": "Order to cash", "Process Level 2": stage,
                             "Process Level 3": step, "Activity Alias": f"{click} {step}", "Start time": t,
                             "End time": t + pd.Timedelta(minutes=4), "User": f"u{c % 3}"})
    src = tmp_path / "levels.parquet"
    pd.DataFrame(rows).to_parquet(src, index=False)
    from app.api.server import app
    c = TestClient(app)
    d = c.post("/api/jobs/from-path", json={"path": str(src), "dataset_class": "public",
                                            "new_process": "Order to cash"}).json()
    jid, key = d["job"]["job_id"], d["job"]["process"]
    sug = {k: v["column"] for k, v in d["profile"]["case"]["suggested"].items() if v.get("column")}
    # the levels are recognised: Level 3 is the activity, Level 2 the stage, the click column is Level 4
    assert (sug["activity"], sug["stage"], sug["level1"], sug["action"]) == \
        ("Process Level 3", "Process Level 2", "Process Level 1", "Activity Alias")
    assert c.post(f"/api/jobs/{jid}/convert/case", json={"mapping": sug, "merge_repeats": True}).status_code == 200
    # documented steps: one the same, one similar, one the data does not have; "Ship Item" is not documented
    stages = [{"name": "Start", "steps": [{"name": "Receive request"}, {"name": "Check the request"}]},
              {"name": "Finish", "steps": [{"name": "Build item"}, {"name": "Archive the file"}]}]
    (library.pack_dir(key) / "pack.yaml").write_text(yaml.safe_dump({"name": "Order to cash", "stages": stages}),
                                                     encoding="utf-8")
    c.post(f"/api/jobs/{jid}/context/confirm", json={"by": "T", "no_documents_reason": "test"})
    q = c.post(f"/api/jobs/{jid}/quality").json()
    assert c.post(f"/api/jobs/{jid}/analysis").status_code == 200
    mi = json.loads((jobs.job_folder(jid) / "mining.json").read_text(encoding="utf-8"))
    assert mi["map"]["available"], [x["key"] for x in q.get("checks", []) if x.get("status") == "fail"]

    v = c.get(f"/api/jobs/{jid}/case-steps").json()
    assert v["level1"] == ["Order to cash"] and v["has_stages"]
    chk = {a["activity"]: (a["step"], a["verdict"], a["stage"]) for a in v["activities"]}
    assert chk["Receive Request"] == ("Receive request", "same", "Intake")
    assert chk["Check Request"] == ("Check the request", "similar", "Intake")
    assert chk["Ship Item"] == ("", "none", "Delivery")
    assert [m["step"] for m in v["missing"]] == ["Archive the file"]
    # prefilled flow: every data step, data stages, documented order, the undocumented step at its usual place
    assert v["flow_source"] == "prefilled"
    assert [(r["stage"], r["step"]) for r in v["flow"]] == [
        ("Intake", "Receive Request"), ("Intake", "Check Request"), ("Delivery", "Build Item"),
        ("Delivery", "Ship Item"), ("Delivery", "Chase Customer")]
    assert v["reference"]["choice"] == "approved_process"
    steps = {s["key"]: s["done"] for s in c.get(f"/api/jobs/{jid}").json()["steps"]}
    assert not steps["definition"] and not steps["case_map"]

    mp = c.get(f"/api/jobs/{jid}/map").json()
    assert [n["id"] for n in mp["nodes"]][:2] == ["Receive Request", "Check Request"]
    assert mp["nodes"][0]["group"] == "Intake" and mp["documented_missing"] == ["Archive the file"]
    assert not any(n.get("missing") for n in mp["nodes"]) and not mp["confirmed"]
    assert [n["id"] for n in c.get(f"/api/jobs/{jid}/map", params={"level": "stage"}).json()["nodes"]] == \
        ["Intake", "Delivery"]
    assert c.get(f"/api/jobs/{jid}/map", params={"level": "observed"}).json()["level"] == "observed"
    assert all("overlap_share" in e for e in mp["edges"] if e["kind"] == "observed")

    # confirm the comparison (a name is required; only documented steps can be chosen)
    links = {a["activity"]: a["step"] for a in v["activities"]}
    assert c.put(f"/api/jobs/{jid}/case-steps", json={"links": links, "by": " "}).status_code == 400
    assert c.put(f"/api/jobs/{jid}/case-steps", json={"links": {"Ship Item": "Invented"}, "by": "T"}).status_code == 400
    assert c.put(f"/api/jobs/{jid}/case-steps", json={"links": links, "by": "Harsh"}).json()["reviewed"]
    # confirm the intended flow: Chase Customer out of the flow, Ship Item renamed into its own stage
    rows = [{"stage": r["stage"], "step": r["step"], "in_flow": r["step"] != "Chase Customer"} for r in v["flow"]]
    rows[3]["stage"] = "Shipping"
    assert c.put(f"/api/jobs/{jid}/case-flow", json={"rows": rows, "by": ""}).status_code == 400
    assert c.put(f"/api/jobs/{jid}/case-flow", json={"rows": rows[:1], "by": "T"}).status_code == 400
    v = c.put(f"/api/jobs/{jid}/case-flow", json={"rows": rows, "by": "Harsh"}).json()
    assert v["flow_source"] == "confirmed" and v["flow_by"] == "Harsh"
    assert v["reference"]["choice"] == "confirmed_process"
    assert "Chase Customer" in v["reference"]["other_steps"]["names"]
    assert v["reference"]["in_order_share"] == 1.0
    steps = {s["key"]: s["done"] for s in c.get(f"/api/jobs/{jid}").json()["steps"]}
    assert steps["definition"] and steps["case_map"]
    assert [n["id"] for n in c.get(f"/api/jobs/{jid}/map", params={"level": "stage"}).json()["nodes"]] == \
        ["Intake", "Delivery", "Shipping"]
    mi = json.loads((jobs.job_folder(jid) / "mining.json").read_text(encoding="utf-8"))
    assert mi["flow_confidence"]["data"]["source"] == "confirmed"
    # the flow belongs to the process (a second file of it reuses it) and the report uses it
    from app.core import case_steps
    assert case_steps.load_flow(key)["by"] == "Harsh"
    c.post(f"/api/jobs/{jid}/report/generate")
    ref = json.loads((jobs.job_folder(jid) / "reference.json").read_text(encoding="utf-8"))
    assert ref["choice"] == "confirmed_process"
