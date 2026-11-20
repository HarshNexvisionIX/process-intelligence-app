"""N3 (Big prompt B, phase 1): the AI layer of the no-case mode, with fake providers only.

Gateway privacy, payload masking, the evidence planner, AI-proposed step rules, activities, and the bench with a
scripted fake provider. No real AI is ever called.

Run with:  pytest -q tests/nocase/test_n3_ai.py
"""
import json
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
import pytest
import yaml

from app.core import ai_client, jobs
from app.core.config import PROJECT_ROOT, settings
from app.nocase.ai_gateway import Gateway
from app.nocase.canonical import NcMapping
from app.nocase.pipeline import run
from app.nocase.stages import run_all, run_n2
from fixtures import load_mapping, make
from test_ai_client import FakeAPI, SECRETS

PACKS = Path(__file__).parent / "packs"
NO_PRIVATE = "AI not available (client data: private providers only)"


# --------------------------------------------------------------------------- 1. gateway privacy
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("ALLOW_EXTERNAL_AI", "true")
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    local = tmp_path / "ai_providers.local.yaml"
    local.write_text("providers:\n  gemini: {model: gemini-2.5-flash}\n", encoding="utf-8")
    monkeypatch.setattr(ai_client, "LOCAL_FILE", local)
    monkeypatch.setattr(ai_client, "_sleep", lambda s: None)
    fake = FakeAPI()
    fake.local = local
    monkeypatch.setattr(ai_client, "http_client", httpx.Client(transport=httpx.MockTransport(fake)))
    for state in (ai_client._rejected, ai_client._windows, ai_client._last_call):
        state.clear()
    monkeypatch.setitem(settings()["ai"], "pace_seconds", {"default": 0})
    monkeypatch.setitem(settings()["ai"], "allow_external_for_client", False)
    return fake


MSG = [{"role": "user", "content": "Summarise the work in plain words."}]


def test_client_data_never_reaches_a_non_private_provider(api):
    a = Gateway("client", "online").ask("nc_summary", MSG, lambda d: [])
    assert not a.ok and a.reason == NO_PRIVATE
    assert api.called("groq") == 0 and api.called("gemini") == 0 and not api.requests


def test_client_data_uses_a_private_provider_when_one_is_configured(api):
    api.local.write_text("providers:\n  gemini: {model: gemini-2.5-flash}\n  ollama_local: {model: tiny}\n",
                         encoding="utf-8")
    a = Gateway("client", "online").ask("nc_summary", MSG, lambda d: [])
    assert a.ok and a.provider == "ollama_local"
    assert api.called("custom") == 1 and api.called("groq") == 0          # localhost answers as "custom" in FakeAPI


def test_public_data_may_use_the_online_providers(api):
    a = Gateway("public", "online").ask("nc_summary", MSG, lambda d: [])
    assert a.ok and a.provider == "groq"


def test_an_invalid_answer_is_retried_once_on_the_same_provider_then_falls_back(api):
    api.reply("groq", (200, "not json at all", {}))
    a = Gateway("public", "online").ask("nc_summary", MSG, lambda d: [])
    assert not a.ok and "invalid after one retry" in a.reason and api.called("groq") == 2


# --------------------------------------------------------------------------- 2. payload masking
def _library(tmp_path, name, target=None):
    shutil.copytree(PACKS / name, tmp_path / "data" / "library" / (target or name))


def test_no_raw_value_ever_reaches_the_ai(tmp_path):
    _library(tmp_path, "hr_like")
    gw = Gateway("public", "fake", lambda task, messages: "{}")
    r = run(make("hr_like", tmp_path / "src"), load_mapping("hr_like"))
    run_all(jobs.job_folder(r["job"]["job_id"]), process="hr_like", ai=gw)
    sent = gw.payload_text()
    assert gw.calls, "the AI layers were not called"
    for raw in ("PHR0", "Ayakha", "Ningiza", "Priya", "Sharma", "Hello team", "HRC0", "eval_", "P1 ", "participant"):
        assert raw not in sent, raw
    unescaped = "\n".join(m["content"] for c in gw.calls for m in c["messages"])     # the texts as sent
    assert not re.search(r"\d{4,}", re.sub(r"\b[UE]\d{5,6}\b", "", unescaped))


# --------------------------------------------------------------------------- 3. evidence planner
def _hr_job(tmp_path, gw=None):
    r = run(make("hr_like", tmp_path / "src"), load_mapping("hr_like"))
    folder = jobs.job_folder(r["job"]["job_id"])
    run_n2(folder, only=("episodes", "case_link"))
    return folder


def test_an_invalid_ai_plan_falls_back_to_the_default_plan(tmp_path):
    from app.nocase.ai.evidence import build, check_plan
    assert check_plan({"plan": [{"signal": "telepathy", "use": "order"}]})
    assert check_plan({"plan": [{"signal": "screen"}]})                        # no "use"
    assert check_plan({"plan": []})
    folder = _hr_job(tmp_path)
    gw = Gateway("public", "fake", lambda t, m: json.dumps({"plan": [{"signal": "telepathy", "use": "order"}]}))
    e = build(folder, None, gw)
    assert e["plan"]["source"] == "default plan (no AI)" and "invalid" in e["plan"]["ai_note"]
    assert "screen" in e["scores"] and "source_label" in e["scores"]


def test_a_valid_ai_plan_is_scored_and_saved(tmp_path):
    from app.nocase.ai.evidence import SCORES_FILE, build
    folder = _hr_job(tmp_path)
    plan = {"plan": [{"use": "step_assignment", "signal": "screen", "column": "screen_clean", "why": "names the work",
                      "expected": "high"},
                     {"use": "step_assignment", "signal": "time_pattern", "column": "ts", "why": "month end",
                      "expected": "low"}], "skip": [], "questions_for_sme": ["Is month end special?"]}
    e = build(folder, None, Gateway("public", "fake", lambda t, m: json.dumps(plan)))
    assert e["plan"]["source"] == "ai:fake-model" and set(e["scores"]) == {"screen", "time_pattern"}
    saved = json.loads((folder / SCORES_FILE).read_text(encoding="utf-8"))
    assert saved["screen"]["score"] > saved["time_pattern"]["score"]           # poor agreement scores lower
    assert "predicts the labelled step" in saved["time_pattern"]["reason"]


# --------------------------------------------------------------------------- 4. AiProposedRules
def _rules_data(tmp_path):
    rows, t = [], datetime(2026, 2, 2, 8)

    def add(screen, action, label, n, user="a"):
        nonlocal t
        for _ in range(n):
            rows.append({"ts": t.isoformat(), "user": user, "app": "Tool", "screen": screen, "action": action,
                         "l3": label})
            t += timedelta(seconds=30)
    add("Alpha Form", "Save Alpha", "Step One", 20)
    add("Beta Form", "Save Beta", None, 8)
    add("Gamma Form", "Save Gamma", None, 6)
    add("Delta Form", "Save Delta", None, 5, "b")
    src = tmp_path / "rules.csv"
    pd.DataFrame(rows).to_csv(src, index=False)
    lib = tmp_path / "data" / "library" / "toy"
    lib.mkdir(parents=True)
    (lib / "pack.yaml").write_text(yaml.safe_dump({"name": "Toy", "stages": [{"name": "Work", "steps": [
        {"name": "Step One"}, {"name": "Step Two"}, {"name": "Step Three"}]}]}), encoding="utf-8")
    (lib / "rules.yaml").write_text(yaml.safe_dump({"rules": [
        {"id": "L1", "step": "Step Three", "match": {"app": "*", "screen": "^Gamma Form$"}, "status": "sme_confirmed"},
        {"id": "X1", "step": "Step Two", "match": {"app": "*", "screen": "^Delta Form$"}, "status": "rejected"}]}),
        encoding="utf-8")
    return src, NcMapping(columns={"ts": "ts", "actor": "user", "app": "app", "screen": "screen", "action": "action",
                                   "l3": "l3"}, dataset_class="public")


def test_ai_rules_are_validated_one_by_one(tmp_path):
    src, m = _rules_data(tmp_path)
    answer = {"rules": [
        {"step": "Step Two", "match": {"app": "*", "screen": "^Beta Form$"}, "why": "beta work"},
        {"step": "Step Three", "match": {"app": "*", "screen": "Beta"}, "why": "overlaps the first"},
        {"step": "Step Two", "match": {"app": "*", "screen": "^Gamma Form$"}, "why": "against the library"},
        {"step": "Step One", "match": {"app": "*", "screen": "^Nothing Here$"}, "why": "matches nothing"},
        {"step": "Step Two", "match": {"app": "*", "screen": "^Delta Form$"}, "why": "rejected before"}],
        "cannot_assign": []}
    gw = Gateway("public", "fake", lambda t, msgs: json.dumps(answer) if t == "nc_assign_rules" else "{}")
    r = run(src, m)
    folder = jobs.job_folder(r["job"]["job_id"])
    run_n2(folder, process="toy", ai=gw)
    q = json.loads((folder / "nc_assign_quality.json").read_text(encoding="utf-8"))
    applied = {x["id"]: x for x in q["ai"]["rules"]}
    assert [x["step"] for x in applied.values()] == ["Step Two"] and all(x["status"] == "proposed" for x in applied.values())
    why = {x["rule"]: x["reason"] for x in q["ai"]["rejected"]}
    assert "more than one step" in why[1]
    assert "approved library rule" in why[2]
    assert "matches no unit" in why[3]
    assert "never proposed again" in why[4]
    ev = pd.read_parquet(folder / "nc_events_clean.parquet")
    beta = ev[ev["screen_clean"] == "Beta Form"]
    assert (beta["step"] == "Step Two").all() and (beta["step_status"] == "proposed").all()
    assert (ev[ev["screen_clean"] == "Alpha Form"]["step_status"] == "observed").all()   # labels untouched
    assert (ev[ev["screen_clean"] == "Gamma Form"]["step"] == "Step Three").all()         # library rule kept
    proposed = yaml.safe_load((tmp_path / "data" / "library" / "toy" / "rules.proposed.yaml").read_text(encoding="utf-8"))
    assert [x["status"] for x in proposed["rules"]] == ["proposed"]


# --------------------------------------------------------------------------- 5. activities
def test_activities_add_up_and_fall_back_without_the_ai(tmp_path):
    from app.nocase.engine.activities import build, check_groups, load
    folder = _hr_job(tmp_path)
    run_n2(folder, only=("assign",))
    out = build(folder, None, Gateway("public", "off"))
    assert out["checks"]["passed"]
    assert all("AI switched off" in v for v in out["fallback_steps"].values())
    acts = load(folder)
    ev = pd.read_parquet(folder / "nc_events_clean.parquet")
    per = ev[ev["step"].notna()].groupby("step").agg(n=("eid", "size"), h=("active_s", "sum"))
    for step, g in acts.groupby("step"):
        assert g["events"].sum() == per.at[step, "n"]
        assert abs(g["hours"].sum() - per.at[step, "h"] / 3600) < 1e-6
    units = pd.DataFrame({"app": ["X"], "screen_clean": ["Form"], "action_clean": ["Click"], "events": [10]},
                         index=["U00001"])
    bad = check_groups({"groups": [{"label": "Click", "rules": [{"screen": "Form"}]},
                                   {"label": "Update 2 records", "rules": [{"screen": "Form"}]},
                                   {"label": "Review offers", "rules": [{"screen": "Form"}]}]}, units, {"X"})
    assert any("click name" in e for e in bad) and any("has digits" in e for e in bad)


# --------------------------------------------------------------------------- 6. the bench with a scripted fake
ICC = PROJECT_ROOT / "test_datasets" / "icc_like_A"


@pytest.mark.skipif(not (ICC / "icc_like_clickstream_A.parquet").exists(), reason="icc_like_A is not in test_datasets/")
def test_bench_with_a_scripted_fake_assigns_steps(tmp_path):
    from app.nocase.bench import run_dataset
    truth = pd.read_parquet(ICC / "icc_like_clickstream_A_TRUTH.parquet")
    events = pd.read_parquet(ICC / "icc_like_clickstream_A.parquet", columns=["event_id", "Application_name"])
    steps = sorted(truth["true_step"].dropna().unique())
    by_app = events.merge(truth, on="event_id").groupby("Application_name")["true_step"].agg(lambda s: s.mode()[0])

    def fake(task, messages):                      # a scripted "AI": one rule per application, its main true step
        if task != "nc_assign_rules":
            return "{}"
        return json.dumps({"rules": [{"step": st, "match": {"app": app, "screen": ".", "action": None}, "why": "main step"}
                                     for app, st in by_app.items()], "cannot_assign": []})
    spec = yaml.safe_load((ICC / "dataset.yaml").read_text(encoding="utf-8"))
    spec["process"] = "icc_bench"
    lib = tmp_path / "work" / "data" / "library" / "icc_bench"
    lib.mkdir(parents=True)
    (lib / "pack.yaml").write_text(yaml.safe_dump({"name": "ICC", "stages": [{"name": "All", "steps": [
        {"name": s} for s in steps]}]}), encoding="utf-8")
    row = run_dataset(spec, ICC, tmp_path / "work", "fake", fake=fake)
    acc = row["accuracy"]
    assert row["status"] == "ok", row.get("status")
    assert acc["step_accuracy"] > 0 and acc["step_unassigned_share"] < 1.0


def test_ai_names_one_step_per_unit_and_each_becomes_an_exact_rule(tmp_path):
    """The default answer shape: {"assign": [{"unit": "S001", "step"}]}, one per application + screen; OTHER
    leaves the screen without a step."""
    import re as _re
    src, m = _rules_data(tmp_path)

    def fake(task, msgs):
        if task != "nc_assign_rules":
            return "{}"
        units = _re.findall(r"^(S\d{3}) \| [^|]* \| ([^|]*) \|", msgs[-1]["content"], _re.M)
        out = []
        for uid, screen in units:
            step = {"Beta Form": "Step Two", "Delta Form": "OTHER"}.get(screen.strip(), "Step One")
            out.append({"unit": uid, "step": step, "why": "test"})
        return json.dumps({"assign": out})
    r = run(src, m)
    folder = jobs.job_folder(r["job"]["job_id"])
    run_n2(folder, process="toy", ai=Gateway("public", "fake", fake))
    ev = pd.read_parquet(folder / "nc_events_clean.parquet")
    beta = ev[ev["screen_clean"] == "Beta Form"]
    assert (beta["step"] == "Step Two").all() and (beta["step_status"] == "proposed").all()
    assert ev[ev["screen_clean"] == "Delta Form"]["step"].isna().all()                   # OTHER: no step
    assert (ev[ev["screen_clean"] == "Gamma Form"]["step"] == "Step Three").all()         # library rule kept
    proposed = yaml.safe_load((tmp_path / "data" / "library" / "toy" / "rules.proposed.yaml").read_text(encoding="utf-8"))
    assert proposed["rules"] and all(x["match"]["screen"].startswith("^") for x in proposed["rules"])
