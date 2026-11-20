"""Application mapping: masked application values get their real names, everywhere.

The shared map lives in the data folder, so every test uses a temporary DATA_DIR.
Run with:  pytest -q tests/test_app_mapping.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest
import streamlit

from app.core import apps, jobs
from app.core.config import PROJECT_ROOT
from app.core.db import connect
from app.core.ingest import normalise, open_source, profile
from app.core.mapping import suggest_mapping, validate_mapping
from app.core.mining import run_mining, save_mining
from app.core.quality import run_quality, save_report
from scripts.generate_synthetic import main as generate

HEX32 = re.compile(r"\b[0-9a-fA-F]{32}\b")
TITLE_BITS = ("Approvals", "Easy Access", "Receipts", "Inbox", "Payments_Q")   # from simplified_title


@pytest.fixture(autouse=True)
def data_folder(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(folder))
    return folder


def apps_log(tmp: Path, cases: int = 3000) -> tuple[Path, dict]:
    src = tmp / "apps.parquet"
    generate(["--cases", str(cases), "--apps", "--start", "2024-01-01", "--months", "12", "--out", str(src)])
    truth = json.loads(Path(str(src) + ".truth.json").read_text(encoding="utf-8"))
    return src, {name: raw for raw, name in truth["applications"].items()}   # real name -> hash


def make_job(tmp: Path, the_map: dict | None = None, cases: int = 3000) -> dict:
    """A job taken through pages 1-3 on a log with an application column."""
    src, _ = apps_log(tmp, cases)
    job = jobs.create_job(src.name, src)
    folder = jobs.job_folder(job["job_id"])
    con = connect()
    open_source(con, src, folder)
    prof = profile(con)
    m = {role: s.column for role, s in suggest_mapping(prof).items()}
    m.update(timestamp_format=prof.by_name()[m["timestamp"]].ts_format, end_timestamp_format=None,
             timezone="UTC", scope={}, exclude_columns=[], merge_repeats=False)
    assert m["application"] == "Application_name" and m["window_title"] == "simplified_title"
    summary = normalise(con, m, folder / "events.parquet", app_map=the_map)
    (folder / "mapping.json").write_text(json.dumps(m), encoding="utf-8")
    report = run_quality(connect(), folder / "events.parquet")
    save_report(report, folder)
    mining = run_mining(connect(), folder / "events.parquet", "High")
    save_mining(mining, folder)
    job.update(status="mined", summary=summary, feasibility=report.feasibility, band="High", format="parquet")
    jobs.save_job(job)
    return job


def text_values(parquet: Path) -> str:
    """Every text value in a Parquet file, joined (compressed files cannot be searched as bytes)."""
    df = pd.read_parquet(parquet)
    return "\n".join(df[c].dropna().astype(str).str.cat(sep="\n") for c in df.columns if df[c].dtype != float)


def test_is_masked():
    assert apps.is_masked("8bea6433fd6ef458baacbeb02165d7ac")
    assert apps.is_masked("123e4567-e89b-12d3-a456-426614174000")
    assert apps.is_masked("4711")
    assert not apps.is_masked("ServiceNow") and not apps.is_masked("SAP Logon")
    assert not apps.is_masked("") and not apps.is_masked(None)


def test_save_and_load_keep_other_apps():
    apps.save_map([{"raw_value": "aaa", "real_name": "Workday", "category": "HR system", "notes": ""},
                   {"raw_value": "bbb", "real_name": "Coupa", "category": "ERP", "notes": "from IT"}])
    counts = apps.save_map([{"raw_value": "aaa", "real_name": "Workday HCM", "category": "HR system",
                             "notes": ""}])            # a later dataset only has "aaa"
    assert counts == {"added": 0, "updated": 1, "removed": 0, "total": 2}
    loaded = apps.load_map()
    assert loaded["aaa"]["real_name"] == "Workday HCM"
    assert loaded["bbb"] == {"real_name": "Coupa", "category": "ERP", "notes": "from IT"}
    assert b"raw_value,real_name,category,notes,updated_at" in apps.export_map()
    assert not list(apps.map_path().parent.glob(".app_map_*"))   # the temporary file is gone


def test_import_two_column_csv():
    apps.save_map([{"raw_value": "aaa", "real_name": "Old name"}])
    preview = apps.import_map(b"raw_value,real_name\n aaa , New name \nccc,Outlook\nddd,\n,Orphan\n")
    assert (preview["added"], preview["updated"], preview["ignored"]) == (1, 1, 2)
    assert {r["raw_value"]: r["real_name"] for r in preview["rows"]} == {"aaa": "New name", "ccc": "Outlook"}
    assert apps.load_map()["aaa"]["real_name"] == "Old name"     # nothing saved until confirmed
    headerless = apps.import_map(b"eee;Teams\nfff;Excel\n")
    assert headerless["added"] == 2


def test_normalise_applies_the_map(tmp_path):
    src, hashes = apps_log(tmp_path)
    con = connect()
    open_source(con, src, tmp_path)
    prof = profile(con)
    m = {role: s.column for role, s in suggest_mapping(prof).items()}
    m.update(timestamp_format=prof.by_name()[m["timestamp"]].ts_format, end_timestamp_format=None, timezone="UTC")
    the_map = {hashes["Workday"]: {"real_name": "Workday", "category": "HR system", "notes": ""}}
    checks = validate_mapping(con, m, prof.rows, app_map=the_map)
    assert any("2 masked applications are not mapped" in c.message for c in checks)
    normalise(con, m, tmp_path / "events.parquet", app_map=the_map)
    counts = dict(duckdb.connect().execute(f"SELECT application, COUNT(*) FROM '{(tmp_path / 'events.parquet').as_posix()}' "
                             "GROUP BY 1").fetchall())
    raw = dict(con.execute("SELECT Application_name, COUNT(*) FROM src GROUP BY 1").fetchall())
    assert counts["Workday"] == raw[hashes["Workday"]]
    first, second = sorted((hashes["Coupa"], hashes["Outlook"]), key=lambda h: -raw[h])
    assert counts["Unnamed app 1"] == raw[first] and counts["Unnamed app 2"] == raw[second]
    assert counts["ServiceNow"] == raw["ServiceNow"]               # readable values stay as they are
    assert not HEX32.search(text_values(tmp_path / "events.parquet"))


def test_two_values_with_one_name_are_one_application(tmp_path):
    _, hashes = apps_log(tmp_path / "probe", 50)
    office = {"real_name": "Office Suite", "category": "Email", "notes": ""}
    job = make_job(tmp_path, {hashes["Coupa"]: office, hashes["Outlook"]: office})
    mining = json.loads((jobs.job_folder(job["job_id"]) / "mining.json").read_text(encoding="utf-8"))
    rows = mining["applications"]["data"]["applications"]
    names = [r["application"] for r in rows]
    assert names.count("Office Suite") == 1 and len(names) == 5
    events = duckdb.connect().execute(f"SELECT COUNT(*) FROM '{(jobs.job_folder(job['job_id']) / 'events.parquet').as_posix()}' "
                            "WHERE application = 'Office Suite'").fetchone()[0]
    assert next(r for r in rows if r["application"] == "Office Suite")["events"] == events


def test_titles_and_hashes_never_leave_the_mapping_screen(tmp_path):
    from test_narrative import FakeClient
    from app.core.report import generate_report

    def reply(messages):
        ids = re.findall(r"^(F\d{3}) \|", messages[-1]["content"], re.MULTILINE)
        return json.dumps({"summary": [{"text": f"The analysis covers {{{{{ids[0]}}}}}.", "facts": [ids[0]]}],
                           "findings": [], "recommendations": []})

    job = make_job(tmp_path)          # no map: masked apps become "Unnamed app N"
    client = FakeClient(reply)
    paths = generate_report(job["job_id"], client=client)
    folder = jobs.job_folder(job["job_id"])
    outputs = {"events.parquet": text_values(folder / "events.parquet"),
               "report.html": paths["html"].read_text(encoding="utf-8"),
               "facts.json": (folder / "facts.json").read_text(encoding="utf-8"),
               "AI payload": client.sent_text()}
    assert client.calls, "the fake AI should have been asked"
    for name, text in outputs.items():
        assert not HEX32.search(text), name
        assert not any(bit in text for bit in TITLE_BITS), name
    assert "Unnamed app 1" in outputs["report.html"] and "Application mapping used" in outputs["report.html"]
    from openpyxl import load_workbook
    assert "Applications" in load_workbook(paths["excel"]).sheetnames


def test_page1_mapping_table_saves_the_csv(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    src, hashes = apps_log(tmp_path, 200)

    original = streamlit.data_editor

    def user_edits(df, **kwargs):
        """Render the editor as usual, then return it as if the user typed one real name."""
        original(df, **kwargs)
        edited = df.copy()
        row = edited.index[edited["raw_value"] == hashes["Workday"]][0]
        edited.loc[row, "Real name"] = "Workday"
        edited.loc[row, "Category"] = "HR system"
        return edited

    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.run()
    next(t for t in at.text_input if t.label == "File, folder or pattern").input(str(src))
    at.run()                                      # the load button appears once a path is entered
    next(b for b in at.button if b.label == "Load from this computer").click()
    at.run()
    assert not at.exception, at.exception
    assert any("6 applications · 3 masked · 0 of 3 mapped" in md.value for md in at.markdown)
    assert at.get("arrow_data_frame") or at.dataframe        # the mapping table is shown

    monkeypatch.setattr(streamlit, "data_editor", user_edits)
    next(b for b in at.button if b.label == "Save mapping").click()
    at.run()
    assert not at.exception, at.exception
    saved = apps.load_map()
    assert saved[hashes["Workday"]] == {"real_name": "Workday", "category": "HR system", "notes": ""}
    assert any("Mapping saved: 1 added" in s.value for s in at.success)
    assert any("1 of 3 mapped" in md.value for md in at.markdown)


def test_pipeline_with_app_map(tmp_path):
    src, hashes = apps_log(tmp_path, 500)
    map_file = tmp_path / "names.csv"
    map_file.write_text(f"raw_value,real_name\n{hashes['Coupa']},Coupa\n{hashes['Outlook']},Outlook\n",
                        encoding="utf-8")
    data = tmp_path / "cli_data"
    run = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "run_pipeline.py"), "--input", str(src),
                          "--auto-map", "--no-ai", "--application", "Application_name",
                          "--app-map", str(map_file), "--data-dir", str(data)],
                         cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "Applications: 6 (3 masked, 2 mapped)" in run.stdout
    events = next((data / "jobs").glob("*/events.parquet"))
    names = {r[0] for r in duckdb.connect().execute(f"SELECT DISTINCT application FROM '{events.as_posix()}'").fetchall()}
    assert {"Coupa", "Outlook", "Unnamed app 1"} <= names
    assert not (data / apps.MAP_FILE).exists()          # merged for this run only (no --save-app-map)
