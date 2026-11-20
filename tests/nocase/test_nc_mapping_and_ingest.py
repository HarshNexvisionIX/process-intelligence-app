"""No-case mapping (problems, layout signature) and ingest (scope counts, times, eval columns kept apart).

Run with:  pytest -q tests/nocase
"""
import re
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from app.core.config import PROJECT_ROOT
from app.nocase.canonical import NcMapping, layout_signature, load_mapping, save_mapping
from conftest import read_json
from fixtures import load_mapping as fixture_mapping
from fixtures import make


# --------------------------------------------------------------------------- 3. mapping
def test_mapping_problems_are_complete():
    m = NcMapping(columns={"actor": "user", "colour": "x", "app": "user"}, dataset_class=None, ts_format="weird",
                  timezone="Mars/Olympus", keep_rows_where={"flag": []}, eval={"bad name": "c", "case_id": "user"})
    text = " ".join(m.problems(["user", "flag"]))
    for part in ("Unknown standard column(s): colour", "'ts' (when the event happened (required)) is not mapped",
                 "Choose the dataset class", "Time format 'weird' is not known", "Timezone 'Mars/Olympus'",
                 "mapped to both 'actor' and 'app'", "Eval name 'bad name'", "Eval column 'user' is also mapped",
                 "keep_rows_where: no values given for 'flag'", "column 'c' is not in the file"):
        assert part in text, part
    ok = fixture_mapping("hr_like")
    assert ok.problems() == []


def test_layout_signature_ignores_order_and_case(tmp_path):
    assert layout_signature(["Date_Time", "user", "App"]) == layout_signature(["app", "USER", "date_time"])
    assert layout_signature(["a", "b"]) != layout_signature(["a", "c"])
    m = fixture_mapping("ap_like")
    save_mapping(["timestamp", "user", "app", "title", "action"], m)
    assert load_mapping(["ACTION", "Title", "App", "User", "Timestamp"]) == m
    assert (Path(tmp_path) / "data" / "nocase_mappings").is_dir()


def test_dataset_class_is_required():
    m = fixture_mapping("hr_like")
    m.dataset_class = None
    assert any("dataset class" in p for p in m.problems())


# --------------------------------------------------------------------------- 4. ingest
def test_counts_add_up_and_unreadable_times_are_counted(run_fixture):
    job, folder, r = run_fixture("hr_like")
    s = read_json(folder / "nc_scope.json")
    assert s["rows_in_file"] == s["left_out_by_filter"] + s["unreadable_time"] + s["in_scope"]
    assert s["unreadable_time"] == 2 and s["left_out"]["keep_rows_where"] == s["left_out_by_filter"] > 0
    assert job["mode"] == "nocase" and job["dataset_class"] == "client"
    ev = pd.read_parquet(folder / "nc_events.parquet")
    assert len(ev) == s["in_scope"] and ev["ts"].notna().all()
    assert s["active_hours_in_scope"] == pytest.approx(ev["active_s"].sum() / 3600, abs=1e-6)
    assert not ev["actor"].isin([f"P{i}" for i in range(1, 9)]).any()          # actors are hashed
    assert list(ev["eid"]) == sorted(ev["eid"])


def test_naive_time_in_kolkata_becomes_utc(run_fixture, tmp_path):
    job, folder, _ = run_fixture("icc_like")
    raw = pd.read_csv(tmp_path / "src" / "icc_like.csv")
    ev = pd.read_parquet(folder / "nc_events.parquet")
    first = pd.to_datetime(raw["time"].iloc[0], format="%d/%m/%Y %H:%M:%S")
    assert ev["ts"].iloc[0] == first - pd.Timedelta(hours=5, minutes=30)        # IST = UTC+5:30


def test_offsets_and_formats(tmp_path):
    from app.core.db import connect
    from app.core.ingest import open_source
    from app.nocase.ingest import convert
    src = tmp_path / "t.csv"
    pd.DataFrame({"t": ["2026-03-01T10:00:00+02:00", "2026-03-01 10:00:00", "", "garbage"],
                  "u": ["a", "b", "c", "d"], "n": ["1.5", "x", " 2 ", None]}).to_csv(src, index=False)
    con = connect()
    open_source(con, src, tmp_path)
    m = NcMapping(columns={"ts": "t", "actor": "u", "active_s": "n"}, dataset_class="public", timezone="Europe/Berlin")
    s = convert(con, m, tmp_path / "job")
    ev = pd.read_parquet(tmp_path / "job" / "nc_events.parquet")
    assert s["unreadable_time"] == 2 and s["in_scope"] == 2
    assert list(ev["ts"]) == [pd.Timestamp("2026-03-01 08:00"), pd.Timestamp("2026-03-01 09:00")]
    assert ev["active_s"].tolist()[0] == 1.5 and pd.isna(ev["active_s"].tolist()[1])


def test_jsonl_and_xes_are_read(tmp_path):
    from app.core.db import connect
    from app.nocase.readers import open_nc_source
    rows = pd.DataFrame({"ts": ["2026-01-01 08:00:00", "2026-01-01 08:01:00"], "user": ["a", "b"]})
    jl = tmp_path / "e.jsonl"
    jl.write_text("\n".join(rows.to_json(orient="records", lines=True).splitlines()), encoding="utf-8")
    con = connect()
    assert open_nc_source(con, jl, tmp_path / "w") == "jsonl"
    assert con.execute("SELECT COUNT(*) FROM src").fetchone()[0] == 2
    xes = tmp_path / "e.xes"
    xes.write_text('<?xml version="1.0"?><log><trace><string key="concept:name" value="T1"/>'
                   '<event><string key="concept:name" value="A"/><date key="time:timestamp" '
                   'value="2026-01-01T08:00:00+00:00"/></event></trace></log>', encoding="utf-8")
    assert open_nc_source(con, xes, tmp_path / "w2") == "xes"
    assert con.execute("SELECT concept_name FROM src").fetchone()[0] == "A"


def test_eval_columns_are_read_only_by_the_eval_package(run_fixture):
    """Static check: outside app/nocase/eval/, only canonical.py (the prefix) and ingest.py (copying them in)
    name eval columns; and no later file carries them."""
    allowed = {"canonical.py", "ingest.py"}
    files = list((PROJECT_ROOT / "app" / "nocase").rglob("*.py")) + list((PROJECT_ROOT / "app" / "ui").glob("nocase_*.py"))
    for f in files:
        if "eval" in f.parts[-2:-1] or f.name in allowed:
            continue
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"eval_|EVAL_PREFIX", text), f"{f.name} names eval columns"
    ingest_text = (PROJECT_ROOT / "app" / "nocase" / "ingest.py").read_text(encoding="utf-8")
    uses = [line for line in ingest_text.splitlines() if "EVAL_PREFIX" in line and "import" not in line
            and not line.strip().startswith(("from", "(", "EVAL"))]
    assert len(uses) == 1 and "AS" in uses[0]                       # the one place they are written
    _job, folder, _ = run_fixture("hr_like")
    for name in ("nc_events_clean.parquet", "nc_units.parquet"):
        cols = duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{(folder / name).as_posix()}')").fetchall()
        assert not [c[0] for c in cols if c[0].startswith("eval_")], name
    from app.nocase.eval import eval_columns, truth
    assert eval_columns(folder) == ["case_id"]
    assert truth(folder, "case_id")["truth"].str.startswith("HRC").all()
