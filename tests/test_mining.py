"""Day 3 tests: the mining engine must reproduce the KNOWN process of the generator.

The synthetic log has 7 known variants (see VARIANTS in scripts/generate_synthetic.py),
so every count can be checked against the truth.  Run with:  pytest -q
Performance test (about a minute):  pytest -m slow
"""
import time
from pathlib import Path

import pandas as pd
import pytest

from app.core.db import connect
from app.core.ingest import normalise, open_source, profile
from app.core.mapping import suggest_mapping
from app.core.mining import (
    build_log, dfg, format_hours, rework, run_mining, simplify, to_dot, variants,
)
from app.core.config import settings
from scripts.generate_synthetic import main as generate

CFG = settings()["mining"]


def to_events(src: Path, tmp: Path) -> Path:
    """Day 1 pipeline: file -> events.parquet, using the suggested mapping."""
    con = connect()
    open_source(con, src, tmp)
    prof = profile(con)
    m = {role: s.column for role, s in suggest_mapping(prof).items()}
    m.update(timestamp_format=prof.by_name()[m["timestamp"]].ts_format,
             end_timestamp_format=None, timezone="UTC")
    out = tmp / f"{src.stem}_events.parquet"
    normalise(con, m, out)
    return out


@pytest.fixture(scope="module")
def clean_events(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("mining")
    src = tmp / "clean.parquet"
    generate(["--cases", "3000", "--start", "2024-01-01", "--months", "12", "--out", str(src)])
    return to_events(src, tmp)


@pytest.fixture(scope="module")
def mined(clean_events) -> dict:
    return run_mining(connect(), clean_events, "High")


def real_edges(graph: dict) -> list[dict]:
    special = {graph["start_node"], graph["end_node"]}
    return [e for e in graph["edges"] if e["src"] not in special and e["dst"] not in special]


def test_kpis_and_variants(mined):
    assert mined["kpis"]["available"] and mined["kpis"]["data"]["cases"] == 3000
    v = mined["variants"]["data"]
    assert v["total_variants"] == 7
    top = v["variants"][0]
    assert 0.40 <= top["share"] <= 0.50
    assert top["path"] == ["Create Request", "Approve Request", "Create Order", "Receive Goods",
                           "Receive Invoice", "Pay Invoice"]
    assert v["variants"][-1]["cumulative_share"] == 1.0


def test_dfg_counts_are_exact(clean_events, mined):
    df = pd.read_parquet(clean_events, columns=["case_id", "activity", "ts_start"]).drop_duplicates()
    df = df.sort_values(["case_id", "ts_start", "activity"])
    df["next"] = df.groupby("case_id")["activity"].shift(-1)
    expected = df.dropna(subset=["next"]).groupby(["activity", "next"]).size().to_dict()
    got = {(e["src"], e["dst"]): e["count"] for e in real_edges(mined["map"]["data"])}
    assert got == expected


def test_start_edges(mined):
    graph = mined["map"]["data"]
    starts = [e for e in graph["edges"] if e["src"] == graph["start_node"]]
    assert sum(e["count"] for e in starts) == 3000
    assert {e["dst"] for e in starts} == {"Create Request"}


def test_rework_approve_request(mined):
    items = {a["activity"]: a for a in mined["rework"]["data"]["activities"]}
    assert 0.05 <= items["Approve Request"]["share"] <= 0.15
    assert mined["rework"]["data"]["self_loops"] == 0


def test_slowest_transition(mined):
    slowest = mined["bottlenecks"]["data"]["transitions"][0]
    assert slowest["dst"] in ("Unblock Invoice", "Pay Invoice")
    assert slowest["low_support"] is False and slowest["cases"] >= CFG["min_support_cases"]


def test_simplify_keeps_boxes_connected(mined):
    graph = mined["map"]["data"]
    nodes, edges, hidden = simplify(graph["nodes"], graph["edges"], 1.0, 0.5, CFG["max_nodes"])
    assert len(edges) < len(graph["edges"]) and hidden["hidden_edges"] > 0
    special = {graph["start_node"], graph["end_node"]}
    for n in nodes:
        if n["activity"] in special:
            continue
        assert any(e["dst"] == n["activity"] for e in edges), n["activity"]
        assert any(e["src"] == n["activity"] for e in edges), n["activity"]


def test_simplify_caps_activities(mined):
    graph = mined["map"]["data"]
    nodes, edges, hidden = simplify(graph["nodes"], graph["edges"], 1.0, 1.0, 3)
    assert len(nodes) == 3 + 2                     # 3 activities + start and end
    assert hidden["hidden_activities"] == 12 - 3   # the generator has 12 activities
    kept = {n["activity"] for n in nodes}
    assert all(e["src"] in kept and e["dst"] in kept for e in edges)
    assert hidden["events_shown_share"] < 1.0


def test_to_dot(mined):
    graph = mined["map"]["data"]
    nodes, edges, _ = simplify(graph["nodes"], graph["edges"], 1.0, 0.9, CFG["max_nodes"])
    dot = to_dot(nodes, edges)
    assert dot.startswith("digraph")
    for n in nodes:
        assert f'"{n["activity"]}"' in dot
    timed = to_dot(nodes, edges, mode="time")
    assert "#d9480f" in timed  # the slowest arrows are highlighted
    tricky = [{"activity": 'Say "hi"\\now', "events": 3, "cases": 3, "median_processing_h": None}]
    out = to_dot(tricky, [])
    assert '"Say \\"hi\\"\\\\now"' in out


def test_band_gating(clean_events):
    low = run_mining(connect(), clean_events, "Low")
    assert low["map"]["available"] and low["kpis"]["available"]
    assert low["bottlenecks"]["available"] is False
    assert low["bottlenecks"]["reason"] == "Not available: data quality is Low."
    assert low["band_label"].startswith("Exploratory only")
    nf = run_mining(connect(), clean_events, "Not feasible")
    for key in ("kpis", "map", "variants", "bottlenecks", "rework", "resources", "trends"):
        assert nf[key]["available"] is False and nf[key]["reason"]
        assert nf[key]["data"] is None


def test_resources_need_a_resource_column(tmp_path):
    rows = [(f"C{c}", act, f"2024-03-{1 + c % 28:02d} {h:02d}:00:00")
            for c in range(40) for h, act in enumerate(["Open", "Work", "Close"], start=8)]
    src = tmp_path / "no_user.csv"
    pd.DataFrame(rows, columns=["case_id", "activity", "timestamp"]).to_csv(src, index=False)
    result = run_mining(connect(), to_events(src, tmp_path), "High")
    assert result["resources"]["available"] is False
    assert "No resource/user column was mapped." in result["resources"]["reason"]
    assert result["map"]["available"]


def test_trends_and_save(mined, tmp_path):
    months = mined["trends"]["data"]["months"]
    assert sum(m["cases"] for m in months) == 3000
    assert all(m["low_support"] == (m["cases"] < CFG["min_support_cases"]) for m in months)
    from app.core.mining import save_mining
    assert save_mining(mined, tmp_path).exists()


def test_functions_on_their_own(clean_events):
    con = connect()
    counts = build_log(con, clean_events)
    assert counts["cases"] == 3000 and counts["has_end_times"] is False
    assert dfg(con)["overlaps"] == 0
    assert len(variants(con, 3)["variants"]) == 3
    assert rework(con, 30, 2)["activities"][0]["cases"] > 0


def test_format_hours():
    # One formatter for the whole app (fmt.duration): minutes keep one decimal, short times their seconds.
    assert format_hours(0.5) == "30.0 min"
    assert format_hours(0.75) == "45.0 min"
    assert format_hours(38 / 3600) == "38 s" and format_hours(0) == "0 s"
    assert format_hours(6.5) == "6.5 h"
    assert format_hours(30) == "30.0 h"
    assert format_hours(3.2 * 24) == "3.2 days"
    assert format_hours(5.1 * 168) == "5.1 weeks"
    assert format_hours(None) == "–"


def test_dfg_matches_pm4py(clean_events, mined):
    pm4py = pytest.importorskip("pm4py")
    df = pd.read_parquet(clean_events, columns=["case_id", "activity", "ts_start"]).drop_duplicates()
    df = df.sort_values(["case_id", "ts_start", "activity"])
    graph, starts, ends = pm4py.discover_dfg(df, case_id_key="case_id", activity_key="activity",
                                             timestamp_key="ts_start")
    got = {(e["src"], e["dst"]): e["count"] for e in real_edges(mined["map"]["data"])}
    assert got == dict(graph)


@pytest.mark.slow
def test_performance_300k_cases(tmp_path):
    src = tmp_path / "big.parquet"
    generate(["--cases", "300000", "--start", "2024-01-01", "--months", "12", "--out", str(src)])
    events = to_events(src, tmp_path)
    start = time.perf_counter()
    result = run_mining(connect(), events, "High")
    seconds = time.perf_counter() - start
    assert result["kpis"]["data"]["cases"] == 300000
    assert seconds < 60, f"run_mining took {seconds:.1f} s"
