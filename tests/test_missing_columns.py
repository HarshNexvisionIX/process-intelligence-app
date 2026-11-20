"""Works without a case ID, without process levels, and with only the minimum columns.

Run with:  pytest -q tests/test_missing_columns.py
"""
import json
import re
from pathlib import Path

import pandas as pd
import pytest

from app.core import jobs
from app.core.capabilities import capabilities
from app.core.config import PROJECT_ROOT
from app.core.db import connect
from app.core.derived import (
    SCREEN_NOTE, TIME_WINDOW_NOTE, WORK_SESSION_NOTE, apply_derivations, find_id_patterns,
)
from app.core.ingest import normalise, open_source, profile
from scripts.generate_synthetic import main as generate
from scripts.run_pipeline import main as pipeline

BANDS = ("High", "Medium", "Low", "None")


@pytest.fixture(autouse=True)
def data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def log(tmp: Path, name: str, *extra: str, cases: int = 1500) -> Path:
    src = tmp / f"{name}.parquet"
    generate(["--cases", str(cases), "--start", "2024-01-01", "--months", "12", "--out", str(src), *extra])
    return src


def run(*args: str) -> tuple[int, dict, Path]:
    """The whole pipeline in this process (DATA_DIR is the test's folder). Returns code, job, folder."""
    before = {j["job_id"] for j in jobs.list_jobs()}
    code = pipeline([*args, "--no-ai"])
    job = next(j for j in jobs.list_jobs() if j["job_id"] not in before)
    return code, job, jobs.job_folder(job["job_id"])


def flow_band(folder: Path) -> str:
    """The job's flow band; "None" when no flow was computed (e.g. the quality gate blocked the analysis)."""
    flow = json.loads((folder / "mining.json").read_text(encoding="utf-8"))["flow_confidence"]
    return (flow.get("data") or {}).get("band", "None")


def at_most(band: str, cap: str) -> bool:
    return BANDS.index(band) >= BANDS.index(cap)


def test_no_stage_levels_app_or_action(tmp_path):
    src = log(tmp_path, "plain", "--apps", "--stages", "--drop-columns", "stage,levels,app,action")
    code, job, folder = run("--input", str(src), "--auto-map")
    assert code == 0
    caps = {c["feature"]: c for c in job["capabilities"]}
    assert caps["Applications"]["status"] == "missing" and "No application column" in caps["Applications"]["fallback"]
    assert caps["Stages on the map"]["status"] == "fallback"
    assert caps["Stages on the map"]["fallback"].startswith("No stage column → steps are grouped by your business definition")  # 01c: template off
    assert caps["Sub-process hierarchy"]["status"] == "missing" and caps["Process map"]["status"] == "available"
    ref = json.loads((folder / "reference.json").read_text(encoding="utf-8"))
    from app.core import reference
    # The order is the one page 3 uses (reference.resolve): here the matching process template, whose stages
    # group the steps; without a template the data's order has no stage boxes.
    data_order = reference.Order.from_dict(ref["suggested"])
    assert all(s["name"] == "" for s in data_order.stages)
    dot = reference.to_reference_dot(data_order, ref["stats"], True, ref["flow"])
    assert not re.search(r"subgraph cluster_\d", dot)                      # no stage boxes: snake layout
    assert ref["choice"] != "template"                  # the built-in template is never the order (01b)

    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.session_state["job"] = job
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    assert not at.exception, at.exception
    page = " ".join(md.value for md in at.markdown)
    assert "Stages from template" not in page and "Found in data" in page   # no template as the order (01b)


def opened(src: Path, tmp: Path, **roles) -> tuple:
    con = connect()
    open_source(con, src, tmp)
    prof = profile(con)
    mapping = {"case_id": None, "activity": "activity", "timestamp": "timestamp", "resource": "resource",
               "timestamp_format": prof.by_name()["timestamp"].ts_format, "end_timestamp": None,
               "end_timestamp_format": None, "timezone": "UTC", "scope": {}, "exclude_columns": [], **roles}
    return con, mapping


def test_case_from_ticket_numbers(tmp_path):
    src = log(tmp_path, "tickets", "--ids-in-title")              # the case column stays only to check against
    con, mapping = opened(src, tmp_path, window_title="window_title")
    found = find_id_patterns(con, ["window_title"], "activity")
    assert found[0]["pattern"] == "ticket" and found[0]["candidate"]
    mapping["case_source"] = {"kind": "text_pattern", "column": "window_title", "pattern": "ticket"}
    normalise(con, apply_derivations(con, mapping), tmp_path / "events.parquet")
    ev = pd.read_parquet(tmp_path / "events.parquet", columns=["case_id", "attr_case_id"])
    majority = ev.groupby("case_id")["attr_case_id"].agg(lambda s: s.value_counts().iloc[0]).sum()
    assert majority / len(ev) >= 0.95                                # derived cases match the true cases
    assert ev["case_id"].str.fullmatch(r"[0-9a-f]{16}").all()        # salted hashes, not ticket numbers


def test_no_raw_ticket_numbers_anywhere(tmp_path):
    from test_narrative import FakeClient
    from app.core.report import generate_report
    src = log(tmp_path, "nocase", "--ids-in-title", "--drop-columns", "case")
    code, job, folder = run("--input", str(src), "--activity", "activity", "--timestamp", "timestamp",
                            "--resource", "resource", "--window-title", "window_title",
                            "--derive-case", "text_pattern", "--pattern", "ticket")
    assert code == 0 and job["case_source"]["kind"] == "text_pattern"
    assert at_most(flow_band(folder), "Medium")

    def reply(messages):
        ids = re.findall(r"^(F\d{3}) \|", messages[-1]["content"], re.MULTILINE)
        return json.dumps({"summary": [{"text": f"The analysis covers {{{{{ids[0]}}}}}.", "facts": [ids[0]]}],
                           "findings": [], "recommendations": []})
    client = FakeClient(reply)
    html = generate_report(job["job_id"], client=client)["html"].read_text(encoding="utf-8")
    texts = {"events": pd.read_parquet(folder / "events.parquet").astype(str).to_string(),
             "facts": (folder / "facts.json").read_text(encoding="utf-8"), "report": html,
             "ai": client.sent_text()}
    assert client.calls
    for name, text in texts.items():
        assert not re.search(r"INC0\d{5,}", text), name
    assert "ticket / incident number found in window_title" in html


def test_work_sessions(tmp_path):
    rows = [("u1", "Open", 0), ("u1", "Check", 10), ("u1", "Close", 20), ("u1", "Open", 100), ("u1", "Close", 110),
            ("u2", "Open", 5), ("u2", "Close", 15)]
    t0 = pd.Timestamp("2024-03-01 09:00")
    src = tmp_path / "sessions.parquet"
    pd.DataFrame([{"user": u, "activity": a, "timestamp": t0 + pd.Timedelta(minutes=m)} for u, a, m in rows]
                 ).to_parquet(src, index=False)
    con, mapping = opened(src, tmp_path, resource="user", case_source={"kind": "work_session", "gap_minutes": 30})
    summary = normalise(con, apply_derivations(con, mapping), tmp_path / "events.parquet")
    assert summary["cases"] == 3                                     # u1 split at the 80-minute pause, u2 one session

    # Desktop-style work: each user does 4 steps a few minutes apart, in sessions on different days.
    sessions = []
    for i in range(1200):
        start = pd.Timestamp("2024-01-01 08:00") + pd.Timedelta(days=i // 6, hours=(i % 6))
        for j, step in enumerate(["Open Form", "Check Data", "Update Record", "Close Form"]):
            sessions.append({"user": f"user{i % 40}", "activity": step,
                             "timestamp": start + pd.Timedelta(minutes=3 * j)})
    big = tmp_path / "desk.parquet"
    pd.DataFrame(sessions).to_parquet(big, index=False)
    code, job, folder = run("--input", str(big), "--activity", "activity", "--timestamp", "timestamp",
                            "--resource", "user", "--derive-case", "work_session")
    assert code == 0 and flow_band(folder) == "Medium"              # a clear order, capped: cases are sessions
    html = (folder / "report.html").read_text(encoding="utf-8")
    assert WORK_SESSION_NOTE in html and "Case = work session (30-min gap)" in html


def test_time_windows(tmp_path):
    from app.core.derived import flow_caps
    from app.core.flow_confidence import flow_confidence
    capped = flow_confidence(0.95, "data", 0.9, 0.95, 1.0, flow_caps({"case_source": {"kind": "time_window"}}))
    assert capped["band"] == "Low" and capped["score"] <= 59 and TIME_WINDOW_NOTE in capped["reasons"]
    code, job, folder = run("--input", str(log(tmp_path, "days")), "--auto-map", "--derive-case", "time_window")
    assert code == 0 and at_most(flow_band(folder), "Low")
    assert TIME_WINDOW_NOTE in (folder / "report.html").read_text(encoding="utf-8")


def test_record_join(tmp_path):
    t0 = pd.Timestamp("2024-03-01 09:00")
    src = tmp_path / "events.parquet"
    pd.DataFrame([{"user": "u1", "activity": a, "timestamp": t0 + pd.Timedelta(minutes=m)}
                  for a, m in (("Open", 0), ("Check", 30), ("Close", 180))]).to_parquet(src, index=False)
    records = tmp_path / "records.csv"
    records.write_text("record_id,user,start_ts,end_ts\nREC-1,u1,2024-03-01 08:00:00,2024-03-01 10:00:00\n",
                       encoding="utf-8")
    con, mapping = opened(src, tmp_path, resource="user", case_source={"kind": "record_join", "file": str(records)})
    summary = normalise(con, apply_derivations(con, mapping), tmp_path / "out.parquet")
    assert summary["events"] == 2 and summary["rows_left_out"] == 1  # the 12:00 event is outside every window
    ev = pd.read_parquet(tmp_path / "out.parquet")
    assert ev["case_id"].nunique() == 1 and "REC-1" not in ev.astype(str).to_string()


def test_overview_only(tmp_path):
    code, job, folder = run("--input", str(log(tmp_path, "overview", "--apps")), "--auto-map", "--overview-only")
    assert code == 0 and flow_band(folder) == "None"
    html = (folder / "report.html").read_text(encoding="utf-8")
    for number in (4, 5, 6):
        assert "Not available: no case" in html.split(f"<h2>{number}. ")[1].split("</section>")[0]
    assert "activity overview only" in html.lower() and "Step runs" in html


def test_no_activity_column(tmp_path):
    src = log(tmp_path, "screens", "--apps", "--drop-columns", "activity", cases=2500)
    code, job, folder = run("--input", str(src), "--case", "case_id", "--timestamp", "timestamp",
                            "--resource", "resource", "--application", "Application_name",
                            "--window-title", "simplified_title", "--derive-activity")
    assert code == 0 and at_most(flow_band(folder), "Low")
    acts = pd.read_parquet(folder / "events.parquet")["activity"].unique().tolist()
    assert len(acts) >= 3 and any(":" in a for a in acts)
    for a in acts:
        assert not re.search(r"\d{4,}|@|#", a), a                    # IDs, emails and #hex# stripped
    assert SCREEN_NOTE in (folder / "report.html").read_text(encoding="utf-8")


def test_page1_derive_case(tmp_path):
    from streamlit.testing.v1 import AppTest
    src = log(tmp_path, "nocase", "--ids-in-title", "--drop-columns", "case", cases=400)
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.run()
    next(t for t in at.text_input if t.label == "File, folder or pattern").input(str(src))
    at.run()
    next(b for b in at.button if b.label == "Load from this computer").click()
    at.run()
    assert not at.exception, at.exception
    at.selectbox(key="map_case_id").set_value(None).run()
    at.radio(key="case_mode").set_value("Derive it").run()
    at.selectbox(key="map_window_title").set_value("window_title").run()
    next(b for b in at.button if b.label == "Find IDs in text columns").click()
    at.run()
    assert not at.exception, at.exception
    assert at.dataframe                                              # the pattern table
    assert any(c["status"] != "available" for c in capabilities({"case_id": None, "activity": "a", "timestamp": "t"}))
    next(b for b in at.button if b.label == "Validate and convert").click()
    at.run()
    assert not at.exception, at.exception
    assert not any("Please choose" in e.value for e in at.error)    # a derived case satisfies Case ID
    assert any(s.value == "Converted" for s in at.subheader)


def screen_log(tmp: Path) -> Path:
    """A screen log without an activity column: a person-name screen on 5,000 events by 20 users,
    "SAP Easy Access Order" used by 20 users, and a frequent phrase used by only 2 users."""
    rows, t0 = [], pd.Timestamp("2024-01-01 08:00")
    for i in range(1250):
        user = f"u{i % 20:02d}"
        start = t0 + pd.Timedelta(hours=6 * i)
        steps = [("SAP", "SAP Easy Access Order", user)]
        steps += [("Workday", "View Worker - Priya Sharma - Workday", user)] * 4
        if i % 5 == 0:
            steps += [("Reports", "Special Access Report", f"u{i % 2:02d}")] * 4
        for j, (app, title, u) in enumerate(steps):
            rows.append({"case_id": f"C{i:05d}", "timestamp": start + pd.Timedelta(minutes=5 * j),
                         "user": u, "app": app, "title": title})
    src = tmp / "screens.parquet"
    pd.DataFrame(rows).to_parquet(src, index=False)
    return src


def test_frequent_person_name_never_reaches_activities(tmp_path):
    from test_narrative import FakeClient
    from app.core.report import generate_report
    src = screen_log(tmp_path)
    code, job, folder = run("--input", str(src), "--case", "case_id", "--timestamp", "timestamp",
                            "--resource", "user", "--application", "app", "--window-title", "title",
                            "--derive-activity")
    assert code == 0
    acts = set(pd.read_parquet(folder / "events.parquet")["activity"].unique())
    assert "Workday: View Worker <name>" in acts
    assert "SAP: SAP Easy Access Order" in acts                       # used by 20 users: kept
    assert "Reports: <name>" in acts                                  # frequent, but only 2 users

    def reply(messages):
        ids = re.findall(r"^(F\d{3}) \|", messages[-1]["content"], re.MULTILINE)
        return json.dumps({"summary": [{"text": f"The analysis covers {{{{{ids[0]}}}}}.", "facts": [ids[0]]}],
                           "findings": [], "recommendations": []})
    client = FakeClient(reply)
    html = generate_report(job["job_id"], client=client)["html"].read_text(encoding="utf-8")
    excel = pd.read_excel(folder / "report.xlsx", sheet_name=None, header=None)
    texts = {"events": pd.read_parquet(folder / "events.parquet").astype(str).to_string(),
             "facts": (folder / "facts.json").read_text(encoding="utf-8"), "report.html": html,
             "report.xlsx": " ".join(df.astype(str).to_string() for df in excel.values()),
             "ai": client.sent_text()}
    assert client.calls
    for name, text in texts.items():
        assert "Priya" not in text and "Sharma" not in text, name
        assert "Special Access Report" not in text, name


def test_phrase_rules_unit():
    from app.core.derived import screen_activity_table
    con = connect()
    rows = [("SAP", "SAP Easy Access Order", f"u{i % 20}") for i in range(400)]
    rows += [("SAP", "Special Access Report", f"u{i % 2}") for i in range(400)]
    rows += [("SAP", "Monthly Close Board", f"u{i % 20}") for i in range(400)]
    con.register("t", pd.DataFrame(rows, columns=["app", "title", "user"]))
    table = screen_activity_table(con, "app", "title", base="t", user_col="user",
                                  sensitive={"monthly close board"})
    screens = dict(zip(table["__key"], table["__screen"]))
    assert screens["SAP Easy Access Order"] == "SAP: SAP Easy Access Order"
    assert screens["Special Access Report"] == "SAP: <name>"          # only 2 users
    assert screens["Monthly Close Board"] == "SAP: <name>"            # a value of a sensitive column
