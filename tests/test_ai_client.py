"""AI providers: routing, limits, errors, cleaning and privacy — fake HTTP only (httpx.MockTransport).

Run with:  pytest -q tests/test_ai_client.py
"""
import json
import re
from pathlib import Path

import httpx
import pytest

from app.core import ai_client
from app.core.narrative import write_summary
from test_narrative import fact

SECRETS = {"GROQ_API_KEY": "gsk_test_SECRET_groq_123", "GEMINI_API_KEY": "AIza_test_SECRET_gemini_456",
           "OPENROUTER_API_KEY": "sk-or-test-SECRET-789"}
HOSTS = {"api.groq.com": "groq", "generativelanguage.googleapis.com": "gemini", "openrouter.ai": "openrouter",
         "localhost": "custom", "api.cerebras.ai": "cerebras"}


class FakeAPI:
    """Answers per provider from a queue of (status, body or text, headers); records every request."""

    def __init__(self):
        self.queues: dict[str, list] = {}
        self.requests: list[tuple[str, str, dict]] = []

    def reply(self, provider, *items):
        self.queues.setdefault(provider, []).extend(items)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        provider = HOSTS[request.url.host]
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        self.requests.append((provider, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b:free"}]})
        queue = self.queues.get(provider) or [(200, None, {})]
        status, content, headers = queue.pop(0) if len(queue) > 1 else queue[0]
        if status != 200:
            return httpx.Response(status, json=content or {"error": {"message": "error"}}, headers=headers)
        text = content(body) if callable(content) else (content if content is not None else '{"ok": true}')
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": text}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    def called(self, provider) -> int:
        return sum(1 for p, path, _ in self.requests if p == provider and path.endswith("/chat/completions"))


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("ALLOW_EXTERNAL_AI", "true")
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    local = tmp_path / "ai_providers.local.yaml"
    local.write_text("providers:\n  gemini: {model: gemini-2.5-flash}\n  openrouter: {model: some/model:free}\n",
                     encoding="utf-8")
    monkeypatch.setattr(ai_client, "LOCAL_FILE", local)
    slept = []
    monkeypatch.setattr(ai_client, "_sleep", slept.append)
    fake = FakeAPI()
    fake.slept = slept
    monkeypatch.setattr(ai_client, "http_client", httpx.Client(transport=httpx.MockTransport(fake)))
    for state in (ai_client._rejected, ai_client._windows, ai_client._last_call):
        state.clear()
    # Fake hosted providers: no pacing pauses in `slept`, and the jobs these tests create (client datasets by
    # default) may use them; test_live_check_01c checks the client-dataset rule itself.
    monkeypatch.setitem(ai_client.settings()["ai"], "pace_seconds", {"default": 0})
    monkeypatch.setitem(ai_client.settings()["ai"], "allow_external_for_client", True)
    return fake


def facts():
    return [fact("F001", "cases", "Cases", 3000, "cases", 3000, "High", "data"),
            fact("F002", "case_duration_median", "Median case duration", 30.0, "hours", 3000, "High", "overview")]


def good_answer(body):
    ids = re.findall(r"^(F\d{3}) \|", body["messages"][-1]["content"], re.M)
    return json.dumps({"summary": [{"text": f"The analysis covers {{{{{ids[0]}}}}}.", "facts": [ids[0]]}],
                       "findings": [], "recommendations": []})


def test_summary_routes_to_groq_then_gemini_on_429(api):
    api.reply("groq", (429, None, {"retry-after": "1"}), (429, None, {"retry-after": "1"}))
    api.reply("gemini", (200, good_answer, {}))
    result = write_summary("job-1", facts(), "High")
    assert result.source == "ai" and result.provider == "gemini" and result.model == "gemini-2.5-flash"
    assert api.called("groq") == 3 and api.slept == [1.0, 1.0]     # 01c: two 429 retries (ai.rate_limit_retries)
    assert result.to_dict()["provider"] == "gemini"          # what narrative.json stores


def test_big_document_skips_groq_by_tokens_per_minute(api):
    messages = [{"role": "user", "content": "x" * 120000}]          # about 30,000 tokens
    r = ai_client.complete("summary", messages)
    assert r.provider == "gemini" and api.called("groq") == 0
    assert any("over tokens per minute" in e and "groq" in e for e in r.errors)


def test_daily_requests_limit_and_new_day(api, monkeypatch):
    ai_client.save_local("groq", limits={"rpd": 1})
    msg = [{"role": "user", "content": "hi"}]
    assert ai_client.complete("summary", msg).provider == "groq"
    second = ai_client.complete("summary", msg)
    assert second.provider == "gemini" and any("over requests per day" in e for e in second.errors)
    now = ai_client._now()
    monkeypatch.setattr(ai_client, "_now", lambda: now + 86400)
    assert ai_client.complete("summary", msg).provider == "groq"


def test_retry_after_honoured_once_or_next_provider(api):
    msg = [{"role": "user", "content": "hi"}]
    api.reply("groq", (429, None, {"retry-after": "5"}), (200, '{"ok": true}', {}))
    assert ai_client.complete("summary", msg).provider == "groq" and api.slept == [5.0]
    api.queues["groq"] = [(429, None, {"retry-after": "30"})]
    r = ai_client.complete("summary", msg)
    assert r.provider == "gemini" and api.slept == [5.0]


def test_401_rejects_key_and_unknown_model_asks_to_choose(api):
    msg = [{"role": "user", "content": "hi"}]
    api.reply("groq", (401, None, {}))
    api.reply("gemini", (404, {"error": {"message": "model not found"}}, {}))
    r = ai_client.complete("summary", msg)
    assert r.provider == "openrouter"
    assert any("key rejected by groq" in e for e in r.errors)
    assert any("choose another in AI settings" in e for e in r.errors)
    before = api.called("groq")
    ai_client.complete("summary", msg)
    assert api.called("groq") == before                   # rejected for the session


def test_cleaning_and_json_mode(api):
    api.reply("groq", (200, '<think>let me think</think>\n```json\n{"a": 1}\n```', {}))
    r = ai_client.complete("summary", [{"role": "user", "content": "hi"}])
    assert json.loads(r.text) == {"a": 1}
    groq_body = next(b for p, _, b in api.requests if p == "groq")
    assert groq_body["response_format"] == {"type": "json_object"} and groq_body["reasoning_effort"] == "low"
    ai_client.complete("summary", [{"role": "user", "content": "hi"}], only="openrouter")
    router_body = next(b for p, _, b in api.requests if p == "openrouter")
    assert "response_format" not in router_body


def test_all_fail_gives_template_with_reason(api):
    for p in ("groq", "gemini", "openrouter"):
        api.reply(p, (500, None, {}))
    result = write_summary("job-2", facts(), "High")
    assert result.source == "template" and result.reason.startswith("AI not available:")


def test_external_providers_never_called_without_permission(api, monkeypatch):
    monkeypatch.setenv("ALLOW_EXTERNAL_AI", "false")
    result = write_summary("job-3", facts(), "High")
    assert result.source == "template" and api.requests == []
    with pytest.raises(ai_client.AIUnavailable, match="ALLOW_EXTERNAL_AI"):
        ai_client.complete("summary", [{"role": "user", "content": "hi"}])


def test_custom_endpoint_first(api, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "llama3.1:8b")
    assert ai_client.route("summary")[0] == "custom"
    api.reply("custom", (200, good_answer, {}))
    result = write_summary("job-4", facts(), "High")
    assert result.provider == "custom" and result.model == "llama3.1:8b" and api.called("groq") == 0


def test_no_key_in_logs_usage_or_job_outputs(api, tmp_path, caplog):
    import logging
    from app.core.report import generate_report
    from test_report import make_job
    caplog.set_level(logging.DEBUG)
    job = make_job(tmp_path, 300)
    api.reply("groq", (429, None, {"retry-after": "1"}), (200, good_answer, {}))
    generate_report(job["job_id"])
    from app.core import jobs
    folder = jobs.job_folder(job["job_id"])
    narrative = json.loads((folder / "narrative.json").read_text(encoding="utf-8"))
    assert narrative["provider"] == "groq"
    html = (folder / "report.html").read_text(encoding="utf-8")
    assert "via groq" in html
    texts = [caplog.text, ai_client.usage_path().read_text(encoding="utf-8"), html]
    texts += [p.read_text(encoding="utf-8", errors="ignore") for p in folder.rglob("*.json")]
    import pandas as pd
    texts += [df.to_string() for df in pd.read_excel(folder / "report.xlsx", sheet_name=None, header=None).values()]
    for secret in SECRETS.values():
        for t in texts:
            assert secret not in t and secret[-6:] not in t


def test_model_list_shapes():
    assert ai_client.parse_models({"data": [{"id": "a"}, {"id": "b:free"}]}, "openrouter") == ["b:free"]
    assert ai_client.parse_models({"models": [{"name": "models/gemini-2.5-flash"}, {"name": "models/embedding-1"}]},
                                  "gemini") == ["gemini-2.5-flash"]
    assert ai_client.parse_models(["openai/gpt-oss-120b", "whisper-large-v3"], "groq") == ["openai/gpt-oss-120b"]


def test_list_models_through_http(api):
    assert ai_client.list_models("openrouter") == ["model-b:free"]
    assert any(path.endswith("/models") for _, path, _ in api.requests)


def test_ai_settings_page_without_keys(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    from app.core.config import PROJECT_ROOT
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(ai_client, "LOCAL_FILE", tmp_path / "none.yaml")
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.run().switch_page("ui/ai_settings_page.py").run()
    assert not at.exception, at.exception
    page = " ".join(m.value for m in at.markdown)
    assert page.count("key ✗") == 4 and "✓" not in page


def test_provider_test_uses_the_configured_answer_limit(api):
    from app.core.config import settings
    r = ai_client.test_provider("groq")
    assert r["ok"] and r["model"] == "openai/gpt-oss-120b"
    body = next(b for p, _, b in api.requests if p == "groq")
    assert body["max_tokens"] == int(settings()["ai"]["max_output_tokens"] or 500) and body["max_tokens"] > 50


def test_fallback_reasons_recorded_answer_unchanged(api):
    api.reply("groq", (503, None, {}), (503, None, {}))
    api.reply("gemini", (200, good_answer, {}))
    result = write_summary("job-5", facts(), "High")
    assert result.source == "ai" and result.provider == "gemini"
    assert result.summary[0]["text"].startswith("The analysis covers")          # the answer itself is unchanged
    assert "fallback: groq: 503" in result.errors
    assert not any("no key" in e or "no model" in e for e in result.errors)       # configuration skips are not noise


def test_fallback_reasons_in_drafts_and_statements(api, tmp_path):
    from app.core import definition as dmod, knowledge
    from app.core.definition_ai import draft_from_document
    doc = "Create Request comes first. Approve Request follows the request."
    draft = json.dumps({"name": "P", "stages": [{"id": "ST01", "name": "All"}], "flows": [],
                        "steps": [{"id": "S01", "name": "Create Request", "stage_id": "ST01", "position": 1,
                                   "mapped_activities": ["Create Request"], "source_quote": "Create Request comes first"}]})
    api.reply("gemini", (503, None, {}), (503, None, {}))
    api.reply("groq", (503, None, {}), (503, None, {}))          # groq is in the drafting route too (after gemini)
    api.reply("openrouter", (200, draft, {}))
    result = draft_from_document("job-6", doc, "sop.txt", ["Create Request", "Approve Request"])
    assert result.definition is not None and result.provider == "openrouter"
    assert "fallback: gemini: 503" in result.errors

    api.queues["gemini"] = [(500, None, {}), (500, None, {})]
    api.queues["groq"] = [(500, None, {}), (500, None, {})]
    api.queues["openrouter"] = [(200, json.dumps({"statements": [
        {"type": "order", "subject": "Create Request", "object": "Approve Request",
         "quote": "Approve Request follows the request", "anchor": "p.1"}]}), {})]
    d = dmod.ProcessDefinition.from_dict({"name": "P", "stages": [{"id": "A", "name": "All"}], "steps": [
        {"id": "S1", "name": "Create Request", "stage_id": "A", "position": 1},
        {"id": "S2", "name": "Approve Request", "stage_id": "A", "position": 2}]})
    src, _ = knowledge.add_source("P", "sop", "sop.txt", doc.encode())
    kept, _, _ = knowledge.extract_statements("P", src["id"], d)
    assert kept and kept[0]["provider"] == "openrouter" and "fallback: gemini: 500" in kept[0]["fallback"]


def test_report_counts_fallbacks_apart_from_failed_checks(api, tmp_path):
    from app.core import jobs
    from app.core.report import build_context
    from test_report import make_job
    job = make_job(tmp_path, 300)
    api.reply("groq", (503, None, {}), (503, None, {}))
    def any_answer(body):           # the retry's last message is the rule list, so search every message
        text = "\n".join(m["content"] for m in body["messages"])
        fid = re.findall(r"^(F\d{3}) \|", text, re.M)[0]
        return json.dumps({"summary": [{"text": f"The analysis covers {{{{{fid}}}}}.", "facts": [fid]}],
                           "findings": [], "recommendations": []})
    api.reply("gemini", (200, "not json at all", {}), (200, any_answer, {}))      # one broken answer, then valid
    from app.core.report import generate_report
    generate_report(job["job_id"])
    narrative = json.loads((jobs.job_folder(job["job_id"]) / "narrative.json").read_text(encoding="utf-8"))
    assert narrative["source"] == "ai" and "fallback: groq: 503" in narrative["errors"]
    html = (jobs.job_folder(job["job_id"]) / "report.html").read_text(encoding="utf-8")
    row = lambda label: re.search(rf"<td>{label}</td><td>(\d+)</td>", html).group(1)  # noqa: E731
    failed = sum(1 for e in narrative["errors"] if not e.startswith("fallback:"))
    fallbacks = sum(1 for e in narrative["errors"] if e.startswith("fallback:"))
    assert failed >= 1 and fallbacks >= 1
    assert row("Checks failed along the way") == str(failed) and row("Provider fallbacks") == str(fallbacks)
    rows = dict(build_context(job["job_id"])["sections"][-1]["narrative"])       # the same split in the context
    assert {"Checks failed along the way", "Provider fallbacks"} <= set(rows)


def test_chunks_fit_the_smallest_provider_of_the_route(monkeypatch):
    """Gemini (huge context) rate-limited -> the next provider must still take the chunk: the size comes from the
    SMALLEST usable provider of the route (Cerebras with 8,192 tokens), not the first."""
    from app.core.config import settings
    known = {"gemini": {"context_tokens": 1_000_000, "limits": {}}, "cerebras": {"context_tokens": 8192, "limits": {}},
             "groq": {"context_tokens": 131072, "limits": {"tpm": 8000}}}
    monkeypatch.setattr(ai_client, "providers", lambda: known)
    monkeypatch.setattr(ai_client, "route", lambda task: ["gemini", "groq", "cerebras"])
    monkeypatch.setattr(ai_client, "usable", lambda p: (True, None))
    share, answer = float(settings()["ai"]["chunk_share"]), int(settings()["ai"]["max_output_tokens"])
    want = (int(min(8192, 8000) * share) - answer) * 4
    assert ai_client.chunk_chars("definition_draft", 60000) == want
    tokens = want / 4 + answer
    assert tokens <= 0.61 * 8000                     # a chunk + the answer fits Groq's per-minute budget


def test_draft_from_several_documents_merges_steps(api, monkeypatch):
    """Several documents (and long ones, cut into chunks): one step per name, quotes checked in their own document."""
    from app.core import definition_ai
    monkeypatch.setattr(definition_ai.ai_client, "chunk_chars", lambda task, default: 60)   # force several chunks
    sop = "Create Request comes first.\nApprove Request follows the request."
    deck = "Approve Request follows the request.\nPay Invoice ends the process."

    def answer(steps):
        return json.dumps({"name": "P", "stages": [{"id": "ST01", "name": "All"}], "flows": [],
                           "steps": [{"id": f"S{i:02d}", "name": n, "stage_id": "ST01", "position": i,
                                      "mapped_activities": acts, "source_quote": q}
                                     for i, (n, acts, q) in enumerate(steps, 1)]})
    api.reply("gemini",
              (200, answer([("Create Request", ["Create Request"], "Create Request comes first")]), {}),
              (200, answer([("Approve Request", ["Approve Request"], "Approve Request follows the request")]), {}),
              (200, answer([("Approve request", ["Approve Request"], "Approve Request follows the request")]), {}),
              (200, answer([("Pay Invoice", ["Pay Invoice"], "Pay Invoice ends the process"),
                            ("Invented Step", [], "this quote is in no document")]), {}))
    result = definition_ai.draft_from_documents("job-7", [("sop.txt", sop), ("deck.txt", deck)],
                                                ["Create Request", "Approve Request", "Pay Invoice"])
    names = [s["name"] for s in result.definition.ordered_steps()]
    assert names == ["Create Request", "Approve Request", "Pay Invoice"]          # "Approve request" merged
    assert any("Invented Step" in d and "deck.txt" in d for d in result.dropped)
    assert "2 documents (sop.txt, deck.txt)" in result.definition.source
    assert "— deck.txt]" in next(s for s in result.definition.steps if s["name"] == "Pay Invoice")["description"]
