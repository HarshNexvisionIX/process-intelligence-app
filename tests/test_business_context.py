"""Business context: a mandatory step 2 in both workflows; the app ships no process templates or sample packs."""
from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT

MAIN = str(PROJECT_ROOT / "app" / "main.py")


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def test_no_templates_or_sample_processes_ship_with_the_app():
    assert not (PROJECT_ROOT / "config" / "process_templates").exists()
    assert not (PROJECT_ROOT / "config" / "nocase_packs").exists()


def test_workflow_has_business_context_as_step_two():
    from app.ui.shell import WORKFLOWS
    assert WORKFLOWS["case"][1] == ("Business context", "context")
    assert WORKFLOWS["nocase"][1] == ("Business context", "context")


def test_library_lists_only_user_processes(data_dir):
    from app.nocase import library
    import yaml
    key = library.create_pack("Order Handling UK")
    assert key == "order_handling_uk"
    sample = data_dir / "library" / "seeded_sample"           # a copy seeded from a test pack earlier
    sample.mkdir(parents=True)
    (sample / "pack.yaml").write_text(yaml.safe_dump({"name": "x", "provenance": {"source": "test pack"}}))
    tmpl = data_dir / "library" / "from_template"
    tmpl.mkdir(parents=True)
    (tmpl / "pack.yaml").write_text(yaml.safe_dump({"name": "y", "source": "config/process_templates/y.yaml"}))
    assert library.user_processes() == ["order_handling_uk"]
    assert library.display_name(key) == "Order Handling UK"
    assert library.load_pack(key)["stages"] == []


def test_context_done_needs_a_process_and_documents_or_a_reason():
    from app.ui.shell import context_done
    assert not context_done({"process": "p"})
    assert not context_done({"business_context": {"status": "documents"}})
    assert context_done({"process": "p", "business_context": {"status": "documents"}})
    assert context_done({"process": "p", "business_context": {"status": "no_documents", "reason": "none yet"}})


def test_context_page_records_no_documents_and_unlocks_the_step(data_dir):
    from streamlit.testing.v1 import AppTest
    from app.core import jobs
    job = jobs.create_job("a.csv")
    (jobs.job_folder(job["job_id"]) / "events.parquet").write_bytes(b"x")
    job["status"] = "normalised"
    jobs.save_job(job)
    at = AppTest.from_file(MAIN, default_timeout=120)
    at.run().switch_page("ui/context_page.py").run()
    assert not at.exception, at.exception
    at.text_input(key="ctx_new_name").set_value("Order Handling UK").run()
    at.button(key="ctx_use").click().run()
    assert jobs.load_job(job["job_id"])["process"] == "order_handling_uk"
    at.text_input(key="ctx_by").set_value("Harsh").run()
    at.text_input(key="ctx_reason").set_value("No SOP exists yet").run()
    at.button(key="ctx_none").click().run()
    assert not at.exception, at.exception
    ctx = jobs.load_job(job["job_id"])["business_context"]
    assert ctx["status"] == "no_documents" and ctx["reason"] == "No SOP exists yet" and ctx["by"] == "Harsh"


def test_report_is_blocked_without_business_context(data_dir):
    from streamlit.testing.v1 import AppTest
    from app.ui import shell
    import streamlit as st  # noqa: F401
    # require_context renders the warning and returns False for an incomplete job
    at = AppTest.from_string(
        "from app.ui import shell\n"
        "ok = shell.require_context({'process': 'p'}, 'the report')\n"
        "import streamlit as st\nst.write(str(ok))\n", default_timeout=60)
    at.run()
    assert any("Business context is required before the report" in w.value for w in at.warning)
    assert any(m.value == "False" for m in at.markdown)


def test_documents_are_masked_before_the_ai_sees_them():
    from app.nocase.payloads import problems, scrub_text
    text = ("Step 3: Priya Sharma approves invoice INV-2026-00123 in SAP.\n"
            "Contact john.smith@capita.com. Raised by David.\nThen Mark the batch as posted.")
    safe, masked = scrub_text(text)
    assert problems(safe) == []
    assert "Priya" not in safe and "David" not in safe and "@" not in safe and "00123" not in safe
    assert "Mark the batch as posted" in safe and "approves invoice" in safe
    assert masked["person names"] == 2 and masked["e-mail addresses"] == 1


def test_propose_masks_names_instead_of_failing(data_dir):
    from app.nocase import library
    from app.nocase.ai.pack_from_docs import propose
    key = library.create_pack("Order Handling UK")
    docs = library.pack_dir(key) / "docs"
    docs.mkdir()
    (docs / "sop by Priya Sharma.txt").write_text("1. Priya Sharma raises the order.\n2. David approves it.",
                                                  encoding="utf-8")
    sent = []

    class Gate:                     # a gateway that guards like the real one and records what would be sent
        def ask(self, task, msgs, check):
            from app.nocase.payloads import guard
            guard(msgs)
            sent.extend(m["content"] for m in msgs)

            class A:
                ok, reason = False, "no AI in tests"
            return A()
    r = propose(key, Gate())
    assert sent and not any("Priya" in m or "David" in m for m in sent)
    assert r["masked"]["person names"] >= 2 and r["path"] is None
