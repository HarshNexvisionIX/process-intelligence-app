"""Privacy lockdown: nothing leaves the computer unless the user deliberately allows it.

Run with:  pytest -q tests/test_privacy.py
"""
import re
import tomllib
from pathlib import Path

import pytest

from app.core import observability, privacy
from app.core.config import PROJECT_ROOT, settings
from app.core.narrative import write_summary
from test_narrative import FakeClient, answer, facts  # noqa: F401 (facts is a fixture)

EXTERNAL_LLM = "https://api.openai.com/v1"


@pytest.fixture(autouse=True)
def data_folder(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(folder))
    return folder


@pytest.mark.parametrize("url, kind", [
    ("http://localhost:11434/v1", "local"), ("http://127.0.0.1:3000", "local"), ("http://[::1]:8080", "local"),
    ("http://10.2.3.4:8000/v1", "private"), ("http://192.168.1.20:3000", "private"),
    ("http://172.20.0.5", "private"), ("https://langfuse.internal", "private"), ("http://ollama.lan:11434", "private"),
    ("https://api.openai.com/v1", "external"), ("https://cloud.langfuse.com", "external"),
    ("http://172.32.0.1", "external"), ("", "unset"), (None, "unset"),
])
def test_classify_endpoint(url, kind):
    assert privacy.classify_endpoint(url) == kind


def test_external_ai_is_blocked_by_default(facts, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", EXTERNAL_LLM)
    client = FakeClient(answer())
    r = write_summary("job-1", facts, "High", client=client)
    assert client.calls == [] and r.source == "template"
    assert r.reason == "AI endpoint is outside your network (set ALLOW_EXTERNAL_AI=true to allow)"
    # An empty LLM_BASE_URL means OpenAI's own service: also external when the AI is on.
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("AI_ENABLED", "true")
    assert privacy.ai_allowed() == (False, privacy.AI_BLOCKED_REASON)


def test_external_ai_allowed_by_flag(facts, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", EXTERNAL_LLM)
    monkeypatch.setenv("ALLOW_EXTERNAL_AI", "true")
    client = FakeClient(answer())
    r = write_summary("job-1", facts, "High", client=client)
    assert len(client.calls) == 1 and r.source == "ai"


def test_local_ai_needs_no_flag(facts, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
    client = FakeClient(answer())
    assert write_summary("job-1", facts, "High", client=client).source == "ai"


def test_external_langfuse_is_off_by_default(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
    assert observability.enabled() is False
    monkeypatch.setenv("ALLOW_EXTERNAL_LANGFUSE", "true")
    assert observability.enabled() is True             # no request is made by enabled()
    monkeypatch.setenv("ALLOW_EXTERNAL_LANGFUSE", "false")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:3000")
    assert observability.enabled() is True             # self-hosted on this computer: allowed


def test_streamlit_config_is_local_only():
    with open(PROJECT_ROOT / ".streamlit" / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    assert cfg["server"]["address"] == "127.0.0.1"
    assert cfg["browser"]["gatherUsageStats"] is False
    assert cfg["server"]["enableXsrfProtection"] is True and cfg["server"]["maxUploadSize"] >= 4096


def test_report_inline_viz_and_checksum_fallback(tmp_path, monkeypatch):
    from test_report import make_job
    from app.core.report import generate_report
    job = make_job(tmp_path, 3000)
    html = generate_report(job["job_id"])["html"].read_text(encoding="utf-8")
    assert not re.search(r"<script[^>]+src=[\"']?https?://", html, re.I)      # no external script
    assert "Viz.js 3.30.0" in html and "Viz.instance()" in html                # embedded and used
    assert "digraph reference" in html                                         # the map's DOT text

    monkeypatch.setitem(settings()["report"], "viz_js_sha256", "0" * 64)    # a changed file
    html = generate_report(job["job_id"])["html"].read_text(encoding="utf-8")
    assert "Viz.js 3.30.0" not in html and "Viz.instance()" not in html
    assert not re.search(r"<script[^>]+src=", html, re.I)                   # never silently the CDN
    assert "Map drawing is switched off" in html and '<table class="steps">' in html   # the step table stays


def test_sidebar_privacy_line():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    # the sidebar keeps a short status; the full privacy line is on Settings → System
    assert any("AI: off" in m.value for m in at.sidebar.markdown)
    at.switch_page("ui/ai_settings_page.py").run()
    assert not at.exception, at.exception
    line = next(c.value for c in at.caption if c.value.startswith("🔒 Data folder:"))
    assert "AI: off" in line and "Langfuse: off" in line and "Usage stats: off" in line


def test_status_line_shows_blocked_external_ai(monkeypatch):
    monkeypatch.setenv("AI_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", EXTERNAL_LLM)
    assert "AI: external (blocked)" in privacy.status_line()
    monkeypatch.setenv("LLM_BASE_URL", "http://192.168.1.5:11434/v1")
    assert "AI: private" in privacy.status_line()
