"""N2b: automatic mode detection at upload (app/intake/mode_detect.py) and its page-1 banner.

Run with:  pytest -q tests/nocase/test_intake_mode_detect.py
"""
import json
import random
import time
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.core import jobs
from app.core.config import PROJECT_ROOT, settings
from app.core.db import connect
from app.core.ingest import open_source
from app.intake.mode_detect import DECISION_FILE, detect, detect_and_save, unattended
from fixtures import make


def _detect(path, tmp_path, folder=None):
    con = connect()
    open_source(con, path, tmp_path / "w")
    return detect_and_save(con, folder, None) if folder is not None else detect(con)


def _strip(path, tmp_path, drop):
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path, dtype=str)
    out = tmp_path / f"{path.stem}_in.csv"
    df.drop(columns=drop).to_csv(out, index=False)
    return out


def hr_case_file(tmp_path, process_cover=0.75, name="hr_case.csv"):
    """case_id filled on ~40% of all rows and `process_cover` of the process rows (is_process = 1)."""
    rnd = random.Random(5)
    rows, t = [], datetime(2026, 3, 2, 8)
    acts = ["Open Case", "Update Case", "Approve", "Close Case"]
    for c in range(250):
        user = f"user{rnd.randint(1, 12):02d}"
        for k in range(4):
            rows.append({"case_id": f"CASE{c:05d}" if rnd.random() < process_cover else "", "activity": acts[k],
                         "timestamp": (t + timedelta(hours=c, minutes=10 * k)).strftime("%Y-%m-%d %H:%M:%S"),
                         "user": user, "is_process": "1"})
    n_proc = len(rows)
    for i in range(int(n_proc * 1.5)):                              # other desktop work: rarely a case ID
        rows.append({"case_id": f"CASE{rnd.randint(0, 249):05d}" if rnd.random() < 0.1666 else "",
                     "activity": "Browse", "user": f"user{rnd.randint(1, 12):02d}", "is_process": "0",
                     "timestamp": (t + timedelta(minutes=7 * i)).strftime("%Y-%m-%d %H:%M:%S")})
    path = tmp_path / name
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


# --------------------------------------------------------------------------- 1
def test_hr_like_case_column_on_process_rows(tmp_path, monkeypatch):
    path = hr_case_file(tmp_path)
    df = pd.read_csv(path, dtype=str)
    assert 0.35 < df["case_id"].notna().mean() < 0.45                      # ~40% of all rows
    d = _detect(path, tmp_path)
    assert d["decision"] == "case" and d["column"] == "case_id", d["message"]
    assert "process rows" in d["scope_base"] and d["prefill"] == {"case_mode": "Use a column", "column": "case_id"}
    # the existing pipeline with the detected column gives exactly what a manual run with that column gives
    from scripts.run_pipeline import main as pipeline
    args = ["--input", str(path), "--case", "case_id", "--activity", "activity", "--timestamp", "timestamp",
            "--resource", "user", "--drop-missing-case", "--filter-column", "is_process", "--filter-values", "1",
            "--no-ai"]
    outs = []
    for run in ("manual", "detected"):
        monkeypatch.setenv("DATA_DIR", str(tmp_path / run))
        cols = list(args)
        if run == "detected":
            cols[cols.index("--case") + 1] = d["column"]
        assert pipeline(cols) == 0
        f = jobs.job_folder(jobs.list_jobs()[0]["job_id"])
        m = json.loads((f / "metrics.json").read_text(encoding="utf-8"))
        m.pop("created_at", None)
        outs.append(m)
    assert outs[0] == outs[1]


# --------------------------------------------------------------------------- 2
def test_icc_like_counter_gives_no_case(tmp_path):
    d = _detect(_strip(make("icc_like", tmp_path / "src"), tmp_path, ["truth_case", "truth_l3"]), tmp_path)
    assert d["decision"] == "no_case"
    assert any("seq: an event counter, not a case" in r for r in d["reasons"])
    assert unattended(d) == "no_case"


# --------------------------------------------------------------------------- 3
def test_ap_like_invoice_numbers_in_titles_give_case_via_derive(tmp_path):
    d = _detect(_strip(make("ap_like", tmp_path / "src"), tmp_path, ["invoice_truth"]), tmp_path)
    assert d["decision"] in ("case", "case_ready")
    if d["decision"] == "case":
        assert d["prefill"]["case_mode"] == "Derive it" and d["prefill"]["column"] == "title"
        assert d["prefill"]["pattern"] in settings()["derived_case"]["id_patterns"]


# --------------------------------------------------------------------------- 4
def test_a_user_column_named_case_id_is_rejected(tmp_path):
    rows, t = [], datetime(2026, 1, 5, 8)
    for i in range(3000):
        u = f"emp{i % 15:03d}"
        rows.append({"case_id": u, "user": u, "activity": random.Random(i).choice(["Open", "Save", "Close"]),
                     "ts": (t + timedelta(minutes=5 * i)).isoformat()})
    path = tmp_path / "users.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    d = _detect(path, tmp_path)
    assert d["decision"] == "no_case"
    cand = {c["column"]: c for c in d["candidates"]}
    assert cand["case_id"]["fail_reason"] == "matches the user column"


# --------------------------------------------------------------------------- 5
def test_borderline_coverage_asks_to_confirm_and_unattended_is_no_case(tmp_path, capsys):
    path = hr_case_file(tmp_path, process_cover=0.62, name="borderline.csv")
    d = _detect(path, tmp_path)
    assert d["decision"] == "confirm" and d["column"] == "case_id"
    assert unattended(d) == "no_case"
    from scripts.detect_mode import main
    assert main(["--input", str(path)]) == 0
    assert "unattended run: no_case" in capsys.readouterr().out


# --------------------------------------------------------------------------- 6
def _page1(src):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=180)
    at.run()
    next(t for t in at.text_input if t.label == "File, folder or pattern").input(str(src))
    at.run()
    next(b for b in at.button if b.label == "Load from this computer").click()
    at.run()
    assert not at.exception, at.exception
    return at


def test_page1_banner_prefill_and_override(tmp_path):
    src = _strip(make("icc_like", tmp_path / "src"), tmp_path, ["truth_case", "truth_l3"])
    at = _page1(src)
    assert any("no case ID" in i.value for i in at.info)
    assert at.radio(key="case_mode").value == "No case ID (clickstream mapping)"
    assert at.selectbox(key="nc_col_ts").value == "time"
    job_id = jobs.list_jobs()[0]["job_id"]
    assert jobs.load_job(job_id)["mode_source"] == "auto"
    assert (jobs.job_folder(job_id) / DECISION_FILE).exists()
    next(b for b in at.button if b.label == "Use the other mode").click()
    at.run()
    assert not at.exception, at.exception
    assert at.radio(key="case_mode").value == "Use a column"
    assert jobs.load_job(job_id)["mode_source"] == "user_override"


def test_page1_case_file_is_prefilled_with_the_column(tmp_path):
    at = _page1(hr_case_file(tmp_path))
    assert any("case ID mode" in s.value for s in at.success)
    assert at.radio(key="case_mode").value == "Use a column"
    assert at.selectbox(key="map_case_id").value == "case_id"
    assert "Validate and convert" in [b.label for b in at.button]


def test_auto_detect_off_leaves_page1_as_before(tmp_path, monkeypatch):
    monkeypatch.setitem(settings()["intake"], "auto_detect", False)
    src = _strip(make("icc_like", tmp_path / "src"), tmp_path, ["truth_case", "truth_l3"])
    at = _page1(src)
    assert at.radio(key="case_mode").value == "Use a column"
    assert "Use the other mode" not in [b.label for b in at.button]
    assert not [x for x in list(at.info) + list(at.success) + list(at.warning) if "Detected" in x.value]
    job_id = jobs.list_jobs()[0]["job_id"]
    assert "mode_source" not in jobs.load_job(job_id)
    assert not (jobs.job_folder(job_id) / DECISION_FILE).exists()


# --------------------------------------------------------------------------- 7
def test_the_decision_file_holds_no_values(tmp_path):
    for name, drop, values in (("hr", None, ["CASE0", "user0"]), ("ap", ["invoice_truth"], ["INV-5", "U1", "U2"])):
        path = hr_case_file(tmp_path) if name == "hr" else _strip(make("ap_like", tmp_path / "src"), tmp_path, drop)
        folder = tmp_path / f"job_{name}"
        folder.mkdir()
        detect_and_save(_con(path, tmp_path), folder)
        text = (folder / DECISION_FILE).read_text(encoding="utf-8")
        for v in values:
            assert v not in text, (name, v)


def _con(path, tmp_path):
    con = connect()
    open_source(con, path, tmp_path / "w2")
    return con


def test_the_layout_is_remembered_and_a_change_is_warned(tmp_path):
    p1 = hr_case_file(tmp_path, name="a.csv")
    d1 = detect_and_save(_con(p1, tmp_path), None)
    assert not d1["remembered_before"] and d1["decision"] == "case"
    p2 = hr_case_file(tmp_path, process_cover=0.3, name="b.csv")
    d2 = detect_and_save(_con(p2, tmp_path), None)
    assert d2["remembered_before"] and d2["decision"] != "case" and "before" in d2["warning"]


# --------------------------------------------------------------------------- 8
def test_two_million_rows_decide_quickly(tmp_path):
    path = tmp_path / "big.parquet"
    connect().execute(f"""COPY (
        SELECT 'C' || CAST(i // 20 AS VARCHAR) AS case_id, 'u' || CAST(i // 20 % 40 AS VARCHAR) AS user_name,
               TIMESTAMP '2026-01-01' + INTERVAL (i) SECOND AS ts,
               ['Open', 'Edit', 'Check', 'Approve', 'Close'][1 + i % 5] AS activity,
               'Screen ' || CAST(i % 7 AS VARCHAR) || ' - App' AS title
        FROM range(2000000) t(i)) TO '{path.as_posix()}' (FORMAT parquet)""")
    start = time.perf_counter()
    d = _detect(path, tmp_path)
    took = time.perf_counter() - start
    assert d["decision"] == "case" and d["column"] == "case_id"
    assert took < 30, took                                       # the prompt's limit for 2.5M rows


def test_a_duration_column_is_not_a_case(tmp_path):
    """Found on the ICC-like B file: an active-time column repeats and covers every row, but is a measure."""
    rnd = random.Random(3)
    rows, t = [], datetime(2026, 7, 1, 8)
    for i in range(4000):
        rows.append({"ts": (t + timedelta(seconds=37 * i)).isoformat(), "user": f"p{i % 6}",
                     "active_time": f"{rnd.choice([1.5, 2.0, 3.5, 6.1, 9.9, 12.4]) + rnd.randint(0, 40)}",
                     "click": rnd.choice(["Open", "Type", "Scroll", "Select Cell"])})
    path = tmp_path / "dur.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    d = _detect(path, tmp_path)
    assert d["decision"] == "no_case"
    assert {c["column"]: c for c in d["candidates"]}["active_time"]["fail_reason"].startswith("a number with decimals")
