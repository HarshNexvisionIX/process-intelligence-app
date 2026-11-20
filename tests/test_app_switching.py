"""Application switches: how often people switch application, where, and between which.

Run with:  pytest -q tests/test_app_switching.py
"""
import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from app.core import jobs
from app.core.config import PROJECT_ROOT, settings
from app.core.db import connect
from app.core.ingest import SWITCHES_FILE, normalise, open_source, profile
from app.core.mining import run_mining, to_app_dot
from scripts.generate_synthetic import main as generate
from test_app_mapping import HEX32, make_job, text_values

GAP_S = settings()["app_switching"]["session_gap_minutes"] * 60
T0 = pd.Timestamp("2024-03-01 09:00")


@pytest.fixture(autouse=True)
def data_folder(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(folder))
    return folder


def convert(src: Path, folder: Path, merge: bool = False, **roles) -> Path:
    """Page 1 for a test file: explicit roles (case_id, activity, timestamp, resource, application)."""
    folder.mkdir(parents=True, exist_ok=True)
    con = connect()
    open_source(con, src, folder)
    prof = profile(con)
    m = {"case_id": "case_id", "activity": "activity", "timestamp": "timestamp", "end_timestamp": None,
         "end_timestamp_format": None, "timezone": "UTC", "merge_repeats": merge, **roles}
    m["timestamp_format"] = prof.by_name()[m["timestamp"]].ts_format
    normalise(con, m, folder / "events.parquet")
    return folder / "events.parquet"


def tiny(tmp: Path, rows: list[tuple], name: str = "tiny", merge: bool = False) -> Path:
    """A hand-made log: rows of (case, activity, minutes after T0, user, app)."""
    df = pd.DataFrame([{"case_id": c, "activity": a, "timestamp": T0 + pd.Timedelta(minutes=m), "user": u, "app": x}
                       for c, a, m, u, x in rows])
    src = tmp / f"{name}.parquet"
    df.to_parquet(src, index=False)
    return convert(src, tmp / name, merge, resource="user", application="app")


def switching(events: Path, band: str = "High") -> dict:
    return run_mining(connect(), events, band)["app_switching"]


def switches(events: Path) -> pd.DataFrame:
    return pd.read_parquet(events.parent / SWITCHES_FILE)


def apps_log(tmp: Path) -> Path:
    src = tmp / "apps.parquet"
    generate(["--cases", "2000", "--apps", "--start", "2024-01-01", "--months", "12", "--out", str(src)])
    return src


APP_ROLES = {"resource": "resource", "application": "Application_name"}


def test_count_matches_pandas(tmp_path):
    events = convert(apps_log(tmp_path), tmp_path / "job", **APP_ROLES)
    df = pd.read_parquet(events, columns=["case_id", "resource", "activity", "ts_start", "application"])
    df = df.dropna(subset=["case_id", "ts_start"]).sort_values(
        ["case_id", "resource", "ts_start", "activity", "application"], na_position="first")
    grouped = df.groupby(["case_id", "resource"], dropna=False)
    df["prev_app"], df["prev_ts"] = grouped["application"].shift(), grouped["ts_start"].shift()
    gap = (df["ts_start"] - df["prev_ts"]).dt.total_seconds()
    expected = (df["prev_app"].notna() & df["application"].notna() & (df["prev_app"] != df["application"])
                & (gap <= GAP_S)).sum()
    assert expected > 0 and len(switches(events)) == expected


def test_long_gap_is_not_a_switch(tmp_path):
    events = tiny(tmp_path, [("C1", "Open", 0, "u1", "Mail"), ("C1", "Check", 120, "u1", "ERP"),
                             ("C1", "Close", 125, "u1", "Mail"), ("C2", "Open", 0, "u2", "Mail")])
    sw = switches(events)
    assert len(sw) == 1 and (sw["from_app"].iloc[0], sw["to_app"].iloc[0]) == ("ERP", "Mail")
    assert sw["gap_seconds"].iloc[0] == 300


def test_null_applications_are_not_counted(tmp_path):
    events = tiny(tmp_path, [("C1", "Open", 0, "u1", "Mail"), ("C1", "Check", 1, "u1", None),
                             ("C1", "Close", 2, "u1", "ERP"), ("C2", "Open", 0, "u2", "Mail")])
    assert len(switches(events)) == 0          # Mail -> (unknown) -> ERP: no switch is known


def test_same_result_with_and_without_merging(tmp_path):
    src = apps_log(tmp_path)
    plain = switches(convert(src, tmp_path / "plain", merge=False, **APP_ROLES))
    merged_events = convert(src, tmp_path / "merged", merge=True, **APP_ROLES)
    merged = switches(merged_events)
    cols = ["case_id", "ts", "from_app", "to_app"]
    assert len(plain) == len(merged) > 0
    assert plain.sort_values(cols)[cols].reset_index(drop=True).equals(
        merged.sort_values(cols)[cols].reset_index(drop=True))
    assert switching(merged_events)["data"]["kpis"]["total"] == len(plain)


SWITCHY = [("C1", "Hire", 0, "u1", "Workday"), ("C1", "Hire", 1, "u1", "Excel"), ("C1", "Hire", 2, "u1", "Workday"),
           ("C1", "Close", 3, "u1", "Workday"), ("C2", "Hire", 0, "u2", "Workday"), ("C2", "Close", 1, "u2", "Mail")]


def test_by_step_and_switches_per_occurrence(tmp_path):
    data = switching(tiny(tmp_path, SWITCHY))["data"]
    first = data["by_step"][0]
    assert first["activity"] == "Hire" and first["switches"] == 2 and first["occurrences"] == 4
    assert first["per_occurrence"] == 0.5
    close = next(s for s in data["by_step"] if s["activity"] == "Close")
    assert close["switches"] == 1 and close["occurrences"] == 2
    gen = switching(convert(apps_log(tmp_path), tmp_path / "gen", **APP_ROLES))["data"]
    assert gen["by_step"][0]["activity"] == "Approve Request"     # the generator's known switching step


def test_steps_by_app_shares_sum_to_one(tmp_path):
    data = switching(convert(apps_log(tmp_path), tmp_path / "gen", **APP_ROLES))["data"]
    for row in data["steps_by_app"]:
        assert abs(sum(row["shares"].values()) - 1) < 0.01, row["activity"]
    approve = next(r for r in data["steps_by_app"] if r["activity"] == "Approve Request")
    assert approve["apps_used"] == 2 and "Excel" in approve["shares"]


def test_ping_pong(tmp_path):
    data = switching(tiny(tmp_path, SWITCHY))["data"]
    assert data["ping_pong"] == [{"a": "Workday", "b": "Excel", "count": 1, "cases": 1, "low_support": True}]
    dot = to_app_dot(data["app_nodes"], data["app_edges"])
    assert dot.startswith("digraph") and "rankdir=LR" in dot
    assert '"Workday" -> "Excel"' in dot and '"Excel" -> "Workday"' in dot     # both directions drawn


def test_no_raw_hashes_anywhere(tmp_path):
    from app.core.report import generate_report
    job = make_job(tmp_path)           # no app map: masked apps become "Unnamed app N"
    folder = jobs.job_folder(job["job_id"])
    paths = generate_report(job["job_id"])
    outputs = {SWITCHES_FILE: text_values(folder / SWITCHES_FILE),
               "mining.json": (folder / "mining.json").read_text(encoding="utf-8"),
               "facts.json": (folder / "facts.json").read_text(encoding="utf-8"),
               "report.html": paths["html"].read_text(encoding="utf-8")}
    assert "Unnamed app" in outputs[SWITCHES_FILE]
    for name, text in outputs.items():
        assert not HEX32.search(text), name
    facts = json.loads(outputs["facts.json"])
    assert {"app_switches_total", "app_switch_pair", "app_switches_per_step"} <= {f["type"] for f in facts}


def test_band_and_missing_column(tmp_path):
    events = convert(apps_log(tmp_path), tmp_path / "gen", **APP_ROLES)
    low = switching(events, "Low")
    assert low["available"] is False and low["reason"] == "Not available: data quality is Low."
    src = tmp_path / "plain.parquet"
    generate(["--cases", "300", "--start", "2024-01-01", "--months", "6", "--out", str(src)])
    none = switching(convert(src, tmp_path / "plain", resource="resource"))
    assert none["available"] is False and none["reason"] == "Not available: No application column was mapped."
    assert not (tmp_path / "plain" / SWITCHES_FILE).exists()


def test_page3_applications_tab(tmp_path):
    from streamlit.testing.v1 import AppTest
    job = make_job(tmp_path)
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.session_state["job"] = job
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    assert not at.exception, at.exception
    labels = {m.label for m in at.metric}
    assert {"App switches", "Per case (median)", "Per 100 events", "Cases with a switch"} <= labels
    assert len(at.get("graphviz_chart")) >= 2        # the process map and the switch map
    assert any("switch application about" in md.value for md in at.markdown)


def test_report_and_excel(tmp_path):
    from openpyxl import load_workbook
    from app.core.report import generate_report
    job = make_job(tmp_path)
    paths = generate_report(job["job_id"])
    # The optimised report: section 11 "Applications and switching" (switches per run of each step).
    section = paths["html"].read_text(encoding="utf-8").split("<h2>11. Applications and switching</h2>")[1] \
        .split("</section>")[0]
    assert "switch application about" in section and "Switches per run" in section
    assert "Switching by step" in section and "Approve Request" in section
    wb = load_workbook(paths["excel"])
    assert "App switches" in wb.sheetnames and wb["App switches"]["A1"].value == "Switch pairs"
