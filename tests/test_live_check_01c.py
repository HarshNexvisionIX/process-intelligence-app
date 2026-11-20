"""Fixes from the live check of 01b (prompt 01c).

Run with:  pytest -q tests/test_live_check_01c.py
"""
import json

import httpx
import pandas as pd
import pytest

from app.core import ai_client, level4, privacy
from app.core.config import settings
from app.core.flow_confidence import flow_confidence
from app.core.narrative import check, order_claim_allowed
from test_ai_client import FakeAPI, SECRETS


# --------------------------------------------------------------------------- 1. flow confidence
def test_weak_data_order_caps_flow_at_low():
    """The live case: milestone order strong on its own, but all data strength 0.07 and 12.8% of cases in order."""
    flow = flow_confidence(0.8, "data (milestones)", 0.5, None, 1.0, coverage=0.18,
                           data_strength=0.07, data_in_order=0.128)
    assert (flow["band"], flow["score"]) == ("Low", 50)        # N1: score and band agree (Low = 40-59)
    assert "no fixed order in the data" in flow["reasons"]
    assert "0.07" in flow["explain"] and "12.8%" in flow["explain"]


def test_strong_order_but_few_cases_in_order_is_low():
    flow = flow_confidence(0.9, "data", 0.128, None, 1.0)
    assert (flow["band"], flow["score"]) == ("Low", 50)
    assert any("12.8% of cases follow the order" in r for r in flow["reasons"])


# --------------------------------------------------------------------------- 2. Level 4 fallback
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("ALLOW_EXTERNAL_AI", "true")
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    local = tmp_path / "ai_providers.local.yaml"
    local.write_text("providers:\n  gemini: {model: gemini-2.5-flash}\n", encoding="utf-8")
    monkeypatch.setattr(ai_client, "LOCAL_FILE", local)
    slept = []
    monkeypatch.setattr(ai_client, "_sleep", slept.append)
    fake = FakeAPI()
    fake.slept = slept
    monkeypatch.setattr(ai_client, "http_client", httpx.Client(transport=httpx.MockTransport(fake)))
    for state in (ai_client._rejected, ai_client._windows, ai_client._last_call):
        state.clear()
    monkeypatch.setitem(settings()["ai"], "pace_seconds", {"default": 0})
    return fake


def test_429_is_retried_with_backoff_and_succeeds(api):
    api.reply("groq", (429, None, {}), (429, None, {}), (200, '{"ok": true}', {}))
    r = ai_client.complete("summary", [{"role": "user", "content": "hi"}])
    assert r.provider == "groq" and api.called("groq") == 3
    assert [s for s in api.slept if s >= 2] == [2.0, 4.0]         # 2, 4 s backoff (no Retry-After)


def test_calls_to_one_provider_are_paced(api, monkeypatch):
    monkeypatch.setitem(settings()["ai"], "pace_seconds", {"default": 0, "groq": 3})
    clock = [1000.0]
    monkeypatch.setattr(ai_client, "_now", lambda: clock[0])
    msg = [{"role": "user", "content": "hi"}]
    ai_client.complete("summary", msg)
    ai_client.complete("summary", msg)
    assert 3.0 in api.slept


UNITS = [{"id": f"U{i:03d}", "activity": "Pay", "application": "Workday", "screen": s, "action": a, "events": 25}
         for i, (s, a) in enumerate([("Worker Profile", "Update Address"), ("Worker Profile", "Update Phone"),
                                     ("Payroll Inbox", "Approve Payment"), ("Payroll Inbox", "Reject Payment")])]


def _answer(*subs):
    return {"substeps": [{"id": f"S{i + 1}", "label": lab, "description": "The person keeps the record right.",
                          "rules": [{"application": "*", "screen_regex": None, "click_regex": rx}]}
                         for i, (lab, rx) in enumerate(subs)], "assign_unmatched": {}}


def test_an_answer_covering_75_percent_is_kept(monkeypatch):
    monkeypatch.setitem(settings()["level4"], "min_coverage", 0.8)
    monkeypatch.setitem(settings()["level4"], "max_other_share", 0.2)
    monkeypatch.setitem(settings()["level4"], "min_buckets", 2)
    out = _answer(("Update worker address", "^Update Address$"), ("Update worker phone", "^Update Phone$"),
                  ("Approve payroll payment", "^Approve Payment$"))
    errors, result = level4.validate(out, UNITS, UNITS, {"Workday"})
    assert len(errors) == 1 and "cover only 75%" in errors[0]
    assert result["partial_ok"] and result["coverage"] == 0.75

    class Client:                                # always the same 75% answer
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    class R:
                        choices = [type("C", (), {"message": type("M", (), {"content": json.dumps(out)})})]
                    return R
    kept, notes, res = level4._ask(Client, [{"role": "user", "content": "x"}],
                                   lambda o: level4.validate(o, UNITS, UNITS, {"Workday"}))
    assert kept == out and res["coverage"] == 0.75
    assert any("kept the AI answer covering 75%" in n for n in notes)


def test_below_60_percent_is_not_kept(monkeypatch):
    monkeypatch.setitem(settings()["level4"], "min_coverage", 0.8)
    monkeypatch.setitem(settings()["level4"], "min_buckets", 2)
    out = _answer(("Update worker address", "^Update Address$"), ("Update worker phone", "^Update Phone$"))
    errors, result = level4.validate(out, UNITS, UNITS, {"Workday"})
    assert errors and result == {}


@pytest.mark.parametrize("screen, actions, want", [
    ("View Worker", ["View Worker"], "View worker record"),
    ("Offers_Team_Web_Tool_Testing-v1.xlsx", ["Update Offer"], "Update offers tool"),
    ("Search Saved Categories", ["Select Search"], "Search saved categories"),
])
def test_fallback_labels_are_verb_and_object(screen, actions, want):
    label = level4.fallback_label(screen, actions)
    assert label == want
    assert level4.label_problems(label, {"Workday", "Excel"}) == []


def test_rule_based_build_puts_navigation_clicks_in_navigate_and_search():
    units = [{"id": f"U{i}", "activity": "Pay", "application": "Workday", "screen": s, "action": a, "events": n}
             for i, (s, a, n) in enumerate([("Worker Profile", "Update Address", 60), ("Worker Profile", "Click", 30),
                                            ("Worker Profile", "Select Search", 20),
                                            ("Payroll Inbox", "Approve Payment", 40)])]
    entry = level4.rules_entry("Pay", units, "AI not available: rate limited")
    labels = [s["label"] for s in entry["substeps"]]
    assert level4.NAVIGATE in labels
    for s in entry["substeps"]:
        assert s["label"] == level4.NAVIGATE or level4.label_problems(s["label"], {"Workday"}) == []
    assign = level4.assigner(entry)
    assert assign(units[1]) == level4.NAVIGATE and assign(units[2]) == level4.NAVIGATE
    assert assign(units[0]) != level4.NAVIGATE and entry["reason"].startswith("AI not available")


def test_one_banner_lists_the_fallback_steps():
    from app.core.report import level4_banner
    details = [{"l3": "Pay", "source": "rules", "reason": "AI not available: rate limited"}, {"l3": "Hire", "source": "ai"},
               {"l3": "Move", "source": "rules", "reason": "AI not available: rate limited"}]
    text = level4_banner(details)
    assert text.startswith("Level 4 built without AI for 2 of 3 steps") and "Labels are rule-based." in text


# --------------------------------------------------------------------------- 4. order claims
def _summary(text):
    return {"summary": [{"text": text, "facts": ["F001"]}], "findings": [], "recommendations": []}


def test_order_claims_rejected_unless_confirmed_or_strong():
    from test_narrative import fact
    facts = {"F001": fact("F001", "cases", "Cases", 3000, "cases", 3000, "High", "data")}
    assert not order_claim_allowed(False, 0.07) and order_claim_allowed(True, 0.07) and order_claim_allowed(False, 0.6)
    for text in ("Review is the final step for {{F001}} cases.", "The {{F001}} cases follow a fixed order."):
        bad = check(_summary(text), {"F001"}, facts, order_claims=False)
        assert any(bad.values()), text
        ok = check(_summary(text), {"F001"}, facts, order_claims=True)
        assert not ok.get("sequence_language_ok"), text


# --------------------------------------------------------------------------- 5. variation label
def test_variation_label_thresholds():
    from app.core import inside_step
    import inspect
    src = inspect.getsource(inside_step)
    assert '"low" if cv < 0.3 else "moderate" if cv <= float(cfg["cv_flag"]) else "high"' in src


# --------------------------------------------------------------------------- 6. token replay per step
def test_token_replay_lists_done_but_not_expected_and_expected_but_not_done():
    pm4py = pytest.importorskip("pm4py")
    from app.core.pm4py_worker import _replay
    t0 = pd.Timestamp("2026-01-05 08:00")

    def log(paths):
        return pd.DataFrame([(f"K{c}", a, t0 + pd.Timedelta(minutes=c * 10 + i)) for c, p in enumerate(paths)
                             for i, a in enumerate(p)], columns=["case_id", "activity", "start_ts"])
    net, im, fm = pm4py.discover_petri_net_inductive(log([["A", "B", "C"]] * 20), activity_key="activity",
                                                      case_id_key="case_id", timestamp_key="start_ts")
    clean = _replay(pm4py, log([["A", "B", "C"]] * 5), net, im, fm)
    assert clean["token_problems"] == []
    out = _replay(pm4py, log([["A", "B", "C"]] * 8 + [["A", "C"]] * 2), net, im, fm)
    rows = {r["step"]: r for r in out["token_problems"]}
    assert rows["C"]["done_not_expected"] == pytest.approx(0.2)
    assert rows["B"]["expected_not_done"] == pytest.approx(0.2)


# --------------------------------------------------------------------------- 9. exact distinct counts
def test_distinct_counts_are_exact_below_the_limit(tmp_path, monkeypatch):
    from app.core.db import connect
    from app.core.ingest import open_source, profile
    from app.core.mapping import _distinct
    src = tmp_path / "ids.csv"
    pd.DataFrame({"case_id": [f"C{i:05d}" for i in range(2940)] * 2,
                  "activity": ["A"] * 5880}).to_csv(src, index=False)
    con = connect()
    open_source(con, src, tmp_path)
    col = profile(con).by_name()["case_id"]
    assert col.distinct == 2940 and col.distinct_exact and _distinct(col) == "2,940"
    monkeypatch.setitem(settings()["ingest"], "exact_distinct_max_rows", 100)
    col = profile(con).by_name()["case_id"]
    assert not col.distinct_exact and _distinct(col).startswith("about ")


# --------------------------------------------------------------------------- 10. dataset class
def test_client_jobs_use_only_private_providers(api, monkeypatch, tmp_path):
    from app.core import jobs
    monkeypatch.setitem(settings()["ai"], "pace_seconds", {"default": 0})
    msg = [{"role": "user", "content": "hi"}]
    job = jobs.create_job("x.csv")
    assert job["dataset_class"] == "client" and privacy.dataset_class() == "client"
    with pytest.raises(ai_client.AIUnavailable) as err:
        ai_client.complete("summary", msg)
    assert "not marked private" in str(err.value) and api.called("groq") == 0

    monkeypatch.setitem(settings()["ai"], "allow_external_for_client", True)   # the override (sidebar warning)
    assert ai_client.complete("summary", msg).provider == "groq"
    monkeypatch.setitem(settings()["ai"], "allow_external_for_client", False)

    job["dataset_class"] = "public"
    jobs.save_job(job)
    assert jobs.load_job(job["job_id"])["dataset_class"] == "public"
    assert ai_client.complete("summary", msg).provider == "groq"

    old = {k: v for k, v in job.items() if k != "dataset_class"}               # a job saved before 01c
    (jobs.job_folder(job["job_id"]) / "job.json").write_text(json.dumps(old), encoding="utf-8")
    assert jobs.load_job(job["job_id"])["dataset_class"] == "client"
    assert not ai_client.usable(ai_client.providers()["groq"])[0]


def test_a_private_provider_serves_client_jobs(api, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "local-model")
    privacy.set_dataset_class("client")
    assert ai_client.usable(ai_client.providers()["custom"])[0]
    assert not ai_client.usable(ai_client.providers()["groq"])[0]


# --------------------------------------------------------------------------- 7. the two variant counts are named
def test_variant_counts_say_what_they_count():
    from app.core.config import PROJECT_ROOT
    tpl = (PROJECT_ROOT / "app" / "templates" / "report.html.j2").read_text(encoding="utf-8")
    assert "distinct paths of step runs" in tpl and '"unique paths of events"' in tpl
    from app.core import quality
    import inspect
    assert "unique paths of events" in inspect.getsource(quality)


# --------------------------------------------------------------------------- 8. no template in page 1 texts
def test_capabilities_do_not_offer_a_template_when_it_is_off(monkeypatch):
    from app.core.capabilities import capabilities
    mapping = {"case_id": "c", "activity": "a", "timestamp": "t"}
    monkeypatch.setitem(settings()["templates"], "use_for_order", False)
    text = " ".join(r["fallback"] for r in capabilities(mapping))
    assert "process template" not in text and "a template," not in text and "your documents" in text
    monkeypatch.setitem(settings()["templates"], "use_for_order", True)
    assert "a template," in " ".join(r["fallback"] for r in capabilities(mapping))
