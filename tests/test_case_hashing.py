"""Case IDs are stored as salted hashes in every job file; analysis results do not change.

Run with:  pytest -q tests/test_case_hashing.py
"""
import hashlib
import json
import re
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from app.core import derived, jobs
from app.core.ingest import PROTECT_CASE_IDS, case_ids_protected
from scripts.generate_synthetic import main as generate
from scripts.run_pipeline import main as pipeline

FILES = ("events.parquet", "app_switches.parquet", "step_actions.parquet", "milestone_candidates.parquet")
INPUT_ID = re.compile(r"\bC\d{8}\b")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def log(tmp: Path) -> Path:
    src = tmp / "clean.parquet"
    generate(["--cases", "800", "--start", "2024-01-01", "--months", "12", "--apps", "--step-actions", "--out", str(src)])
    return src


def run(src: Path) -> Path:
    before = {j["job_id"] for j in jobs.list_jobs()}
    assert pipeline(["--input", str(src), "--auto-map", "--application", "Application_name",
                     "--action", "activity_alias_name", "--no-ai"]) == 0
    job = next(j for j in jobs.list_jobs() if j["job_id"] not in before)
    return jobs.job_folder(job["job_id"])


def texts(folder: Path):
    """Every string in every job output: Parquet text columns, JSON, HTML and Excel cells."""
    for path in folder.rglob("*"):
        if path.suffix == ".parquet":
            con = duckdb.connect()
            for col, typ, *_ in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall():
                if typ == "VARCHAR":
                    q = '"' + col.replace('"', '""') + '"'
                    yield path.name, col, [v for (v,) in con.execute(
                        f"SELECT DISTINCT {q} FROM read_parquet(?) WHERE {q} IS NOT NULL", [str(path)]).fetchall()]
        elif path.suffix in (".json", ".html"):
            yield path.name, "text", [path.read_text(encoding="utf-8")]
        elif path.suffix == ".xlsx":
            sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str)
            yield path.name, "cells", [df.fillna("").to_string() for df in sheets.values()]


def summary(folder: Path) -> dict:
    m = json.loads((folder / "mining.json").read_text(encoding="utf-8"))
    sub = m["subprocess"]["data"]
    return {
        "cases": m["kpis"]["data"]["cases"],
        "variants": [(v["path"], v["cases"]) for v in m["variants"]["data"]["variants"]],
        "switches": (m["app_switching"]["data"]["kpis"]["total"],
                     [(p["from"], p["to"], p["count"], p["cases"]) for p in m["app_switching"]["data"]["pairs"]]),
        "milestones": [(c["labels"], c["cases"]) for c in m["milestones"]["data"]["chains"]],
        "subprocess": [(d["activity"], d["runs"], d["cases"], [c["runs"] for c in d["comparison"]["classes"]])
                       for d in sub["steps"]],
    }


def test_no_input_case_id_in_any_job_file_and_results_unchanged(tmp_path, monkeypatch):
    src = log(tmp_path)
    hashed = run(src)
    for name in FILES:
        ids = pd.read_parquet(hashed / name, columns=["case_id"])["case_id"].dropna()
        assert len(ids) and ids.str.fullmatch(r"[0-9a-f]{16}").all(), name
    for name, where, values in texts(hashed):
        for v in values:
            assert not INPUT_ID.search(str(v)), (name, where, INPUT_ID.search(str(v)).group(0))
    assert case_ids_protected(hashed / "events.parquet")

    monkeypatch.setattr(derived, "hash_case_sql", lambda expr: expr)       # the old behaviour
    plain = run(src)
    assert INPUT_ID.fullmatch(pd.read_parquet(plain / "events.parquet", columns=["case_id"])["case_id"].iloc[0])
    assert not case_ids_protected(plain / "events.parquet")
    assert summary(hashed) == summary(plain)


def test_derived_cases_are_hashed_once(tmp_path):
    src = tmp_path / "tickets.parquet"
    generate(["--cases", "300", "--start", "2024-01-01", "--months", "6", "--ids-in-title", "--drop-columns", "case",
              "--out", str(src)])
    before = {j["job_id"] for j in jobs.list_jobs()}
    assert pipeline(["--input", str(src), "--activity", "activity", "--timestamp", "timestamp", "--resource", "resource",
                     "--window-title", "window_title", "--derive-case", "text_pattern", "--pattern", "ticket",
                     "--no-ai"]) == 0
    folder = jobs.job_folder(next(j for j in jobs.list_jobs() if j["job_id"] not in before)["job_id"])
    ids = set(pd.read_parquet(folder / "events.parquet", columns=["case_id"])["case_id"].dropna())
    salt, n = derived.case_salt(), 16
    once = lambda v: hashlib.sha256((salt + v).encode()).hexdigest()[:n]  # noqa: E731
    first = once("INC00000001")
    assert first in ids                                   # one salted hash of the ticket
    assert once(first) not in ids                         # never a hash of the hash


def test_old_jobs_get_the_reconvert_message(tmp_path, monkeypatch, capsys):
    src = tmp_path / "plain.parquet"
    generate(["--cases", "300", "--start", "2024-01-01", "--months", "6", "--out", str(src)])
    monkeypatch.setattr(derived, "hash_case_sql", lambda expr: expr)       # convert like an older version
    before = {j["job_id"] for j in jobs.list_jobs()}
    assert pipeline(["--input", str(src), "--auto-map", "--no-ai"]) == 0
    assert PROTECT_CASE_IDS in capsys.readouterr().out
    job = next(j for j in jobs.list_jobs() if j["job_id"] not in before)
    monkeypatch.undo()
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    events = jobs.job_folder(job["job_id"]) / "events.parquet"
    stamp = events.stat().st_mtime

    from streamlit.testing.v1 import AppTest
    from app.core.config import PROJECT_ROOT
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.session_state["job"] = job
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    assert not at.exception, at.exception
    assert any(PROTECT_CASE_IDS in w.value for w in at.warning)
    assert events.stat().st_mtime == stamp                # old files are not rewritten
