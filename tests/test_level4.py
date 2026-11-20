"""Level 4 v2: screens and typed text, AI-written rules (fake client), validation and fallback, automatic
building in the analysis, summaries at every level, storyboard and flow maps, tables, page, report.

Run with:  pytest -q tests/test_level4.py
"""
import json
import os
import re
from pathlib import Path

import pandas as pd
import pytest

from app.core import jobs, level4, reference
from app.core.config import PROJECT_ROOT
from app.core.screens import TYPED, clean_screen
from scripts.run_pipeline import main as pipeline
from test_narrative import FakeClient

FAMILIES = [("S1", "Maintain tax data", "SAP", "maintain tax data", "Maintains the employee tax records in SAP."),
            ("S2", "Update bank details", "SAP", "bank details", "Checks and changes the bank details in SAP."),
            ("S3", "Review planned absences", "Workday", "absence overview", "Reviews planned absences in Workday."),
            ("S4", "Look up the worker profile", "Workday", "worker profile", "Opens the worker profile in Workday."),
            ("S5", "Email the candidate", "ServiceNow", "compose email", "Writes and sends the e-mail from ServiceNow.")]
UNIT = re.compile(r"^(U\d{3}) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|", re.M)


def good_answer(messages, **change):
    """Sub-steps with one screen rule per planted family; unmatched units with a family screen are assigned."""
    text = "\n".join(m["content"] for m in messages)
    subs = [{"id": i, "label": lab, "description": desc,
             "rules": [{"application": app, "screen_regex": f"(?i)^{scr}", "click_regex": None}],
             "examples": []} for i, lab, app, scr, desc in FAMILIES]
    out = {"substeps": subs, "assign_unmatched": {}}
    out.update(change)
    return json.dumps(out)


def fake(**change):
    client = FakeClient(lambda messages: good_answer(messages, **change))
    client.provider, client.model = "fake", "fake-model"
    return client


@pytest.fixture(scope="module")
def l4_job(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("level4v2")
    old = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(tmp / "data")
    from scripts.generate_synthetic import main as generate
    src = tmp / "clicks.parquet"
    generate(["--cases", "500", "--start", "2024-01-01", "--months", "6", "--level4", "--out", str(src)])
    assert pipeline(["--input", str(src), "--auto-map", "--merge-repeats", "--no-ai", "--level4", "off"]) == 0
    job = jobs.list_jobs()[0]
    yield {"tmp": tmp, "src": src, "job": job, "folder": jobs.job_folder(job["job_id"])}
    if old is None:
        os.environ.pop("DATA_DIR", None)
    else:
        os.environ["DATA_DIR"] = old


@pytest.fixture
def env(l4_job, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(l4_job["tmp"] / "data"))
    return l4_job


def all_text(folder: Path):
    import duckdb
    for p in folder.rglob("*"):
        if p.suffix == ".parquet":
            con = duckdb.connect()
            for col, typ, *_ in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(p)]).fetchall():
                if typ == "VARCHAR":
                    q = '"' + col + '"'
                    yield p.name, " ".join(str(v) for (v,) in con.execute(
                        f"SELECT DISTINCT {q} FROM read_parquet(?) WHERE {q} IS NOT NULL", [str(p)]).fetchall())
        elif p.suffix in (".json", ".html"):
            yield p.name, p.read_text(encoding="utf-8")
        elif p.suffix == ".xlsx":
            yield p.name, " ".join(df.astype(str).to_string() for df in
                                   pd.read_excel(p, sheet_name=None, header=None).values())


def run_analysis(env, use_ai=False):
    from app.core.db import connect
    from app.core.mining import run_mining, save_mining
    m = run_mining(connect(), env["folder"] / "events.parquet", "High", use_ai=use_ai)
    save_mining(m, env["folder"])
    return m


def test_typed_text_never_stored_or_sent(env):
    cat = level4.catalogue(env["folder"])
    assert TYPED in set(cat["action"])
    client = fake()
    level4.build_or_update(env["job"], "build", client=client, l3s=["Requisition"])
    from app.core.report import generate_report
    run_analysis(env)
    generate_report(env["job"]["job_id"], use_ai=False)
    texts = list(all_text(env["folder"])) + [("ai payload", client.sent_text())]
    for name, text in texts:
        for typed in ("Hello Team", "Kind Regards", "see attached", "Priya"):
            assert typed not in text, (name, typed)
    assert "<typed text>" in client.sent_text()


def test_screen_cleaning_and_carry_forward(env):
    assert clean_screen("Maintain Tax Data #9a8b7c6d# - SAP", "SAP") == "Maintain Tax Data"
    assert clean_screen("Bank Details 4471234 - SAP", "SAP") == "Bank Details"
    assert clean_screen("Worker Profile - Priya Sharma - Workday", "Workday") == "Worker Profile"   # person dropped
    assert clean_screen("Compose Email | ServiceNow", "ServiceNow") == "Compose Email"
    assert clean_screen("Priya Sharma - Workday", "Workday") is None
    import duckdb
    rows = duckdb.connect().execute(f"""SELECT screen FROM read_parquet('{(env['folder'] / 'step_actions.parquet').as_posix()}')
                                        WHERE action = '{TYPED}'""").fetchall()
    assert rows and all(s for (s,) in rows)                     # typed text got the screen of its run
    assert {s for (s,) in rows} <= {"Compose Email", "Maintain Tax Data", "Bank Details", "Absence Overview",
                                    "Worker Profile"}


def test_valid_rules_and_reuse_on_a_new_file(env, tmp_path):
    level4.build_or_update(env["job"], "build", client=fake())
    sig = reference.order_key(env["job"])
    tax = level4.load(sig)
    entry = tax["Requisition"]
    assert entry["source"] == "ai" and entry["model"] == "fake-model"
    # 01c: generic clicks ("Click Done", "Navigate to Details": level4.navigation_words) go to "Navigate and search"
    assert [s["label"] for s in entry["substeps"] if s["label"] != level4.NAVIGATE] == [f[1] for f in FAMILIES]
    assert all(s["rules"][0]["screen_regex"] for s in entry["substeps"] if s["label"] != level4.NAVIGATE)
    assert entry["coverage"] >= 0.8
    m = run_analysis(env)
    d = next(x for x in m["level4"]["data"]["l3s"] if x["l3"] == "Requisition")
    assert d["coverage"] >= 0.8 and d["source"] == "ai"
    assert {n["name"] for n in d["nodes"]} >= {f[1] for f in FAMILIES}
    assert all(n["description"] for n in d["nodes"])
    # A second file of the same layout with NEW click names: grouped by the saved regex rules.
    df = pd.read_parquet(env["src"])
    df["activity_alias_name"] = df["activity_alias_name"].str.replace("View Create Tax Data", "View Archive Tax Data")
    second = tmp_path / "second.parquet"
    df.to_parquet(second, index=False)
    assert pipeline(["--input", str(second), "--auto-map", "--merge-repeats", "--no-ai"]) == 0
    job2 = jobs.list_jobs()[0]
    cat2 = level4.catalogue(jobs.job_folder(job2["job_id"]))
    unit = cat2[(cat2["l3"] == "Requisition") & (cat2["action"] == "View Archive Tax Data")].iloc[0]
    label = level4.assigner(level4.load(sig)["Requisition"])(unit.to_dict())
    assert label == "Maintain tax data"


@pytest.mark.parametrize("bad", ["bad_regex", "nested", "overlap", "click_label", "generic_label", "digit",
                                 "coverage"])
def test_invalid_answers_retried_then_fallback(env, bad):
    def reply(messages):
        out = json.loads(good_answer(messages))
        s = out["substeps"]
        if bad == "bad_regex":
            s[0]["rules"][0]["screen_regex"] = "(?i)(maintain tax"
        elif bad == "nested":
            s[0]["rules"][0]["click_regex"] = "(a+)+b"
        elif bad == "overlap":
            s[1]["rules"][0]["screen_regex"] = "(?i)^(bank details|maintain tax data)"
        elif bad == "click_label":
            s[0]["label"] = "View Create Tax Data"
        elif bad == "generic_label":
            s[0]["label"] = "Review in SAP"
        elif bad == "digit":
            s[0]["description"] = "Maintains tax records in SAP within two days or 2 days."
        elif bad == "coverage":
            out["substeps"] = s[:3]
            for x in out["substeps"]:
                x["rules"][0]["screen_regex"] += "$x"
            out["substeps"][0]["rules"][0]["screen_regex"] = "(?i)^maintain tax data"
            out["substeps"][1]["rules"][0]["screen_regex"] = "(?i)^bank details"
            out["substeps"][2]["rules"][0]["screen_regex"] = "(?i)^absence overview"
        return json.dumps(out)
    client = FakeClient(reply)
    entry = level4.propose("job", "Payment", "Invoicing", ["Pay Invoice"], level4.catalogue(env["folder"]), client)
    assert entry["source"] == "rules" and entry["reason"].startswith("answer invalid after 3 tries:"), entry["reason"]
    assert len(client.calls) == 3
    # 01c: rule-based labels are verb + object (fallback_label) and pass the label rules
    label = entry["substeps"][0]["label"]
    assert not label.startswith("Work in ") and level4.label_problems(label, {"SAP", "Workday", "ServiceNow"}) == []


def test_regex_timeout_in_a_separate_process():
    problems = level4.regex_problems(["(?i)tax", "(x|xx)*y"], ["x" * 34, "Maintain Tax Data"])
    assert "(?i)tax" not in problems
    assert "timed out" in problems["(x|xx)*y"]
    assert "nested quantifier" in level4.regex_problems(["(a+)+"], ["a"])["(a+)+"]


def test_auto_build_in_run_mining_keeps_confirmed(env, monkeypatch):
    sig = reference.order_key(env["job"])
    tax = {"Payment": {**level4.rules_entry("Payment", level4.units_of(level4.catalogue(env["folder"]), "Payment")),
                       "source": "edited", "confirmed_by_business": True}}
    level4.save(sig, tax)
    client = fake()
    monkeypatch.setattr(level4, "_client", lambda job_id: (client, None))
    run_analysis(env, use_ai=True)
    after = level4.load(sig)
    assert after["Payment"]["confirmed_by_business"] is True and after["Payment"]["source"] == "edited"
    assert after["Requisition"]["source"] == "ai" and client.calls
    payment_calls = [c for c in client.calls if "Level 3 step: Payment" in c["messages"][-1]["content"]]
    assert not payment_calls


def test_summaries_everywhere_and_validator(env):
    m = run_analysis(env)["level4"]["data"]
    assert m["overview"]["text"].startswith("The process runs through")
    assert all(st["summary"]["text"] for st in m["stages"])
    assert all(d["summary"]["text"] and all(n["summary"] for n in d["nodes"]) for d in m["l3s"])
    facts = level4._facts([("Payment: Maintain tax data — share of the step's work", 0.4, "share", 400, {})])

    def answer(text):
        return FakeClient(json.dumps({"summary": [{"text": text, "facts": ["F001"]}], "findings": [],
                                      "recommendations": []}))
    ok = level4._write("step", "Payment", "", facts, "Template {{F001}}.", "High",
                       {"ok": True, "client": answer("Most of Payment is tax data ({{F001}}).")}, [])
    assert ok["source"] == "ai" and "40.0%" in ok["text"]
    for bad in ("About 40 percent is tax data {{F001}}.", "clerk_7 does tax data in {{F001}}."):
        res = level4._write("step", "Payment", "", facts, "Template {{F001}}.", "High",
                            {"ok": True, "client": answer(bad)}, ["clerk_7"])
        assert res["source"] == "template" and res["text"] == "Template 40.0%."


def test_storyboard_and_flow_maps(env):
    level4.build_or_update(env["job"], "build", client=fake())
    data = run_analysis(env)["level4"]["data"]
    dot = level4.storyboard_dot(data)
    assert dot.count("subgraph cluster_") == len(data["stages"]) == 4
    assert all("style=invis" in ln for ln in dot.splitlines() if "->" in ln)
    for d in data["l3s"]:
        assert d["l3"] in dot
    boxes = dot.split("<table")[1:]
    assert len(boxes) == len(data["l3s"])
    assert all(1 <= len(re.findall(r">\d+\. [^<]+ — \d+%<", box)) <= 6 for box in boxes)
    pay = next(d for d in data["l3s"] if d["l3"] == "Payment")
    counts = level4.edge_count(level4.flow_dot(pay))
    assert counts["inner"] <= 12 and counts["start"] <= 2 and counts["end"] <= 2 and counts["both"] >= 1
    assert level4.edge_count(level4.flow_dot(pay, show_all=True))["inner"] >= counts["inner"]


def test_tables_have_no_empty_cells(env):
    data = run_analysis(env)
    rows = level4.level_rows(data["subprocess"]["data"]["hierarchy"], data["level4"]["data"])
    assert rows and all(str(v).strip() for r in rows for v in r.values())
    assert {"Process level 1", "Stage", "Level 3", "Sub-step"} <= set(rows[0])
    d = data["level4"]["data"]["l3s"][0]
    assert all(c["share"] is not None and c["runs"] is not None for c in d["comparison"]["classes"])


def test_page_report_excel_and_pipeline(env, capsys):
    level4.build_or_update(env["job"], "build", client=fake())
    run_analysis(env)
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=180)
    at.session_state["job"] = env["job"]
    at.run().switch_page("ui/map_page.py").run()
    assert not at.exception, at.exception
    assert any(e.label == "Edit Level 4" for e in at.expander)
    assert any("Matches units" in df.value.columns for df in at.dataframe)
    assert any("The process at a glance" in m.value for m in at.markdown)
    at.selectbox(key="l4_l3").set_value("Payment").run()
    at.toggle(key="l4_all_Payment").set_value(True).run()
    assert not at.exception, at.exception
    at.toggle(key="sp_show_clicks").set_value(True).run()
    assert not at.exception, at.exception

    from app.core.report import generate_report
    paths = generate_report(env["job"]["job_id"], use_ai=False)
    html = paths["html"].read_text(encoding="utf-8")
    inside = html.split("<h2>6. Inside each step (Level 4)</h2>")[1].split("</section>")[0]
    assert "<h3>Payment</h3>" in inside and "Share of the step's clicks" in inside   # no active-time column here
    assert "Maintain tax data" in inside and "Maintains the employee tax records in SAP." in inside
    assert {"Level 4", "Summaries"} <= set(pd.ExcelFile(paths["excel"]).sheet_names)

    capsys.readouterr()
    assert pipeline(["--input", str(env["src"]), "--auto-map", "--merge-repeats", "--no-ai"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"  Payment: sub-steps \d+ · coverage \d+% · ", out)
