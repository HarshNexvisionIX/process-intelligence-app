"""The simple process map: intended order of steps, order strength and how cases relate.

Run with:  pytest -q tests/test_reference_map.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import streamlit

from app.core import jobs, reference
from app.core.config import PROJECT_ROOT
from app.core.db import connect
from app.core.mining import build_log, dfg
from scripts.generate_synthetic import main as generate
from test_quality import to_events

MAIN_PATH = ["Create Request", "Approve Request", "Create Order", "Receive Goods", "Receive Invoice", "Pay Invoice"]


@pytest.fixture(autouse=True)
def data_folder(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(folder))
    return folder


def log(tmp: Path, name: str, *extra: str, cases: int = 3000) -> Path:
    src = tmp / f"{name}.parquet"
    generate(["--cases", str(cases), "--start", "2024-01-01", "--months", "12", "--out", str(src), *extra])
    return to_events(src, tmp)


def computed(events: Path, **kw):
    con = connect()
    ref = reference.compute(con, events, f"test-{events.stem}", "Purchasing", **kw)
    return con, reference.Order.from_dict(ref["order"]), ref["stats"]


@pytest.fixture(scope="module")
def clean_and_shuffled(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ref")
    return log(tmp, "clean"), log(tmp, "shuffled", "--shuffle-order")


def test_order_strength(clean_and_shuffled):
    clean, shuffled = clean_and_shuffled
    for events, expected in ((clean, "Strong"), (shuffled, "Weak")):
        con = connect()
        build_log(con, events)
        score, label = reference.order_strength(dfg(con)["edges"])
        assert label == expected, (events.stem, score)
    assert reference.order_strength([{"src": "A", "dst": "B", "count": 50}, {"src": "B", "dst": "A", "count": 50}]) \
        == (0.0, "Weak")


def test_suggested_order_follows_the_main_path(clean_and_shuffled):
    _, order, _ = computed(clean_and_shuffled[0])
    drawn = order.drawn_steps()
    positions = [drawn.index(step) for step in MAIN_PATH]
    assert positions == sorted(positions)
    assert order.source == "suggested" and not order.confirmed_by_business


def test_rare_steps_go_to_other_steps(clean_and_shuffled):
    con, _, _ = computed(clean_and_shuffled[0])
    order = reference.suggest_order(con, min_case_share=0.06)     # 5% variants fall below
    assert {"Cancel Order", "Change Vendor Bank"} <= set(order.other_steps)
    assert not {"Cancel Order", "Change Vendor Bank"} & set(order.drawn_steps())
    stats = reference.reference_stats(con, order)
    dot = reference.to_reference_dot(order, stats)
    # Not drawn as step boxes: the names appear only inside the Other steps box.
    step_boxes = re.findall(r'\bs\d+ \[label="([^"]*)"', dot)
    assert not any("Cancel Order" in b or "Change Vendor Bank" in b for b in step_boxes)
    other = re.search(r"subgraph cluster_other \{(.*?)\n  \}", dot, re.S).group(1)
    assert f"{len(order.other_steps)} steps · " in other and "Cancel Order" in other


def test_stages_become_clusters_in_order(tmp_path):
    _, order, stats = computed(log(tmp_path, "staged", "--stages"))
    assert [s["name"] for s in order.stages] == ["Request", "Ordering", "Receiving", "Invoicing"]
    dot = reference.to_reference_dot(order, stats)
    clusters = re.findall(r"subgraph cluster_\d+ \{\s*label=<<b>([^<]+)</b>>;(.*?)\n  \}", dot, re.S)
    assert [name for name, _ in clusters] == ["Request", "Ordering", "Receiving", "Invoicing"]
    ids = {s: f"s{i}" for i, s in enumerate(order.drawn_steps())}
    for step, node in ids.items():
        assert sum(bool(re.search(rf"\b{node} \[", body)) for _, body in clusters) == 1, step


def test_in_order_share(tmp_path, clean_and_shuffled):
    rows = [(f"C{c}", step, pd.Timestamp("2024-01-01") + pd.Timedelta(hours=c * 10 + i))
            for c in range(120) for i, step in enumerate(["Open", "Check", "Close"])]
    src = tmp_path / "exact.parquet"
    pd.DataFrame(rows, columns=["case_id", "activity", "timestamp"]).to_parquet(src, index=False)
    _, order, stats = computed(to_events(src, tmp_path))
    assert order.drawn_steps() == ["Open", "Check", "Close"]
    assert stats["in_order_share"] == 1.0 and stats["pair_order_share"] == 1.0 and stats["reach_last_share"] == 1.0
    _, _, shuffled = computed(clean_and_shuffled[1])
    assert shuffled["in_order_share"] < 0.3


def test_save_and_load_order():
    order = reference.Order("Hiring", [{"name": "Start", "steps": ["A", "B"]}], ["C"], "edited", True)
    reference.save_order("layout-123", order)
    loaded = reference.load_order("layout-123")
    assert loaded.drawn_steps() == ["A", "B"] and loaded.other_steps == ["C"]
    assert loaded.confirmed_by_business is True and loaded.source == "edited" and loaded.updated_at
    assert reference.load_order("unknown-layout") is None
    # A saved order fits new data: missing steps dropped, new ones go to Other steps.
    fitted = reference.reconcile(loaded, ["A", "C", "D"])
    assert fitted.drawn_steps() == ["A"] and fitted.other_steps == ["C", "D"]


def test_describe_reference(clean_and_shuffled):
    _, order, stats = computed(clean_and_shuffled[1])
    text = " ".join(reference.describe_reference(order, stats))
    assert "not done in a fixed order" in text and "suggested from the data" in text
    assert "The most back-and-forth is between" in text
    order.confirmed_by_business = True
    assert "suggested from the data" not in " ".join(reference.describe_reference(order, stats))
    assert len(reference.describe_reference(order, stats)) <= 4


def test_dot_output():
    order = reference.Order("P", [{"name": "", "steps": ['Say "hi"', "Reply"]}], [])
    stats = {"steps": [{"step": 'Say "hi"', "cases_reached": 50, "share_reached": 0.5},
                       {"step": "Reply", "cases_reached": 40, "share_reached": 0.4}],
             "other_steps": {"steps": 0, "names": [], "cases": 0, "share": 0.0},
             "back_and_forth": [{"a": 'Say "hi"', "b": "Reply", "count": 12, "cases": 9}]}
    dot = reference.to_reference_dot(order, stats, show_back_and_forth=True)
    # Columns/rows are laid out top to bottom, with stage columns forced side by side.
    assert dot.startswith("digraph") and "rankdir=TB" in dot and reference.LEGEND in dot
    assert 'Say \\"hi\\"' in dot and "subgraph" not in dot          # a nameless stage draws no cluster
    assert "dir=both" in dot and "⇄ 12" in dot
    assert "dir=both" not in reference.to_reference_dot(order, stats, show_back_and_forth=False)


def _rows(dot: str) -> list[list[str]]:
    """The rank=same groups that contain only step boxes (the rows of the no-stage layout)."""
    groups = [g.split("; ") for g in re.findall(r"\{ rank=same; (.*?); \}", dot)]
    return [g for g in groups if all(re.fullmatch(r"s\d+", n) for n in g)]


def test_no_stage_layout_wraps_into_rows_of_five():
    steps = [f"Step {i:02d}" for i in range(12)]
    order = reference.Order("P", [{"name": "", "steps": steps}], [])
    stats = {"steps": [{"step": s, "cases_reached": 10, "share_reached": 0.1} for s in steps],
             "other_steps": {"steps": 0}, "back_and_forth": []}
    dot = reference.to_reference_dot(order, stats)
    rows = _rows(dot)
    assert all(len(r) <= 5 for r in rows) and len(rows) >= 3
    assert sorted({n for r in rows for n in r}, key=lambda n: int(n[1:])) == [f"s{i}" for i in range(12)]
    assert rows[1] == ["s9", "s8", "s7", "s6", "s5"]               # row 2 snakes back (right to left)
    for a, b in zip(range(11), range(1, 12)):                        # every intended arrow is still drawn
        assert re.search(rf"\bs{a} -> s{b}( \[constraint=false\])?;", dot), (a, b)


def test_back_and_forth_lines(clean_and_shuffled):
    _, order, stats = computed(clean_and_shuffled[1])
    assert stats["back_and_forth"]
    on = reference.to_reference_dot(order, stats, show_back_and_forth=True)
    lines = [l for l in on.splitlines() if "dir=both" in l]
    assert len(lines) == len(stats["back_and_forth"])
    assert all("constraint=false" in l and "#c0504d" in l and "style=dashed" in l for l in lines)
    assert "dir=both" not in reference.to_reference_dot(order, stats, show_back_and_forth=False)


def test_title_at_the_top(clean_and_shuffled):
    _, order, stats = computed(clean_and_shuffled[1])
    dot = reference.to_reference_dot(order, stats)
    title = re.search(r'\n  label="([^"]*)"; labelloc=t;', dot).group(1)
    assert title.startswith("Purchasing — 3,000 cases · January 2024 to ")
    assert "order: suggested" in title and "order strength: Weak" in title
    order.confirmed_by_business = True
    assert "order: confirmed" in reference.map_title(order, stats)


def test_other_steps_box_lists_names():
    names = [f"Rare {i}" for i in range(7)]
    order = reference.Order("P", [{"name": "", "steps": ["A", "B"]}], names)
    stats = {"steps": [{"step": "A", "cases_reached": 9, "share_reached": 0.9},
                       {"step": "B", "cases_reached": 8, "share_reached": 0.8}],
             "other_steps": {"steps": 7, "names": names, "cases": 3, "share": 0.3}, "back_and_forth": []}
    dot = reference.to_reference_dot(order, stats)
    other = re.search(r"subgraph cluster_other \{(.*?)\n  \}", dot, re.S).group(1)
    assert "7 steps · 30% of cases" in other and "Rare 3" in other and "Rare 4" not in other
    assert "+3 more" in other and "style=\"rounded,dashed\"" in other
    assert re.search(r"\{ rank=same; s0; s1; other; \}", dot)            # far right, beside the steps


def test_map_html_download_is_self_contained(clean_and_shuffled):
    from app.core.report import map_html
    _, order, stats = computed(clean_and_shuffled[0])
    dot = reference.to_reference_dot(order, stats)
    page = map_html(dot, reference.map_title(order, stats), reference.describe_reference(order, stats))
    assert page.startswith("<!doctype html>") and "Viz.js 3.30.0" in page and "renderSVGElement" in page
    assert not re.search(r"<script[^>]+src=", page, re.I)                 # inline: no network request
    assert json.loads(re.search(r"var DOT = (\".*?\");</script>", page, re.S).group(1)
                      .replace("<\\/", "</")) == dot                      # the exact DOT is embedded


def test_page3_simple_view(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from test_report import make_job
    job = make_job(tmp_path, 3000)
    original = streamlit.data_editor

    def swap_first_two(df, **kwargs):
        """Render the editor, then return it as if the user swapped the first two positions."""
        original(df, **kwargs)
        if not str(kwargs.get("key", "")).startswith("ref_editor"):
            return df
        edited = df.copy()
        edited.loc[0, "Position"], edited.loc[1, "Position"] = df.loc[1, "Position"], df.loc[0, "Position"]
        return edited

    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.session_state["job"] = job
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    assert not at.exception, at.exception
    assert at.radio(key="map_view").value == "Simple (intended order)"
    assert at.get("graphviz_chart")
    at.checkbox(key="ref_back_and_forth").uncheck().run()
    assert not at.exception, at.exception
    at.radio(key="map_view").set_value("Detailed (all transitions)").run()
    assert not at.exception and at.get("graphviz_chart")
    at.radio(key="map_view").set_value("Simple (intended order)").run()
    assert not at.exception, at.exception
    before = reference.Order.from_dict(json.loads(json.dumps(
        reference.compute(connect(), jobs.job_folder(job["job_id"]) / "events.parquet",
                          reference.order_key(job))["order"]))).drawn_steps()

    monkeypatch.setattr(streamlit, "data_editor", swap_first_two)
    next(b for b in at.button if b.label == "Save order").click()
    at.run()
    assert not at.exception, at.exception
    saved = reference.load_order(reference.order_key(job))
    # The synthetic log matches the purchase-to-pay template, which is preselected (more trusted
    # than the data); a saved template-based order keeps its source until it is confirmed.
    assert saved is not None and saved.source in ("template", "edited")
    assert saved.drawn_steps()[:2] == [before[1], before[0]]
    assert any("Order saved" in s.value for s in at.success)


def test_report_shows_the_simple_map(tmp_path):
    from openpyxl import load_workbook
    from test_report import make_job
    from app.core.report import generate_report
    job = make_job(tmp_path, 3000)
    paths = generate_report(job["job_id"])
    html = paths["html"].read_text(encoding="utf-8")
    section = html.split("<h2>4. Process map and steps</h2>")[1].split("</section>")[0]
    # The optimised report: the map plus one row per step, in the order page 3 uses. The built-in template is
    # not offered (01b), so the data's own order: steps sorted by cases reached; no "follow this order" sentence.
    assert reference.LEGEND in section and "sorted by cases reached" in section and "Template:" not in html
    assert "of cases follow this order" not in section
    ws = load_workbook(paths["excel"])["Steps"]
    assert ws["A2"].value == "Create Request" and isinstance(ws["C2"].value, int)
    facts = json.loads((jobs.job_folder(job["job_id"]) / "facts.json").read_text(encoding="utf-8"))
    types = {f["type"] for f in facts}
    assert {"order_strength", "in_order_share", "pair_order_share", "reach_last_share", "step_cases"} <= types


def test_pipeline_with_stage_and_order(tmp_path):
    src = tmp_path / "staged.parquet"
    generate(["--cases", "1500", "--start", "2024-01-01", "--months", "6", "--stages", "--out", str(src)])
    order = {"process_name": "Purchasing", "confirmed_by_business": True,
             "stages": [{"name": "Request", "steps": ["Create Request", "Approve Request"]},
                        {"name": "Ordering", "steps": ["Create Order"]},
                        {"name": "Invoicing", "steps": ["Receive Invoice", "Pay Invoice"]}],
             "other_steps": []}
    order_file = tmp_path / "order.json"
    order_file.write_text(json.dumps(order), encoding="utf-8")
    data = tmp_path / "cli_data"
    run = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "run_pipeline.py"), "--input", str(src),
                          "--auto-map", "--no-ai", "--stage", "phase", "--order", str(order_file), "--save-order",
                          "--data-dir", str(data)],
                         cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
    assert re.search(r"Order strength: \d\.\d\d \((Strong|Partial|Weak)\) · in order: [\d.]+% of cases · "
                     r"order: confirmed", run.stdout), run.stdout
    ref = json.loads(next((data / "jobs").glob("*/reference.json")).read_text(encoding="utf-8"))
    assert ref["order"]["source"] == "edited" and "Receive Goods" in ref["order"]["other_steps"]
    assert list((data / "process_orders").glob("*.json"))        # --save-order kept it for this layout


def test_move_step_earlier_and_later_across_stages():
    order = reference.Order("P", [{"name": "Request", "steps": ["A", "B"]}, {"name": "Delivery", "steps": ["C", "D"]}],
                            ["X"], "suggested", True)
    later = reference.move_step(order, "B", 1)                 # B passes C: joins Delivery, after C
    assert later.drawn_steps() == ["A", "C", "B", "D"] and later.stage_of()["B"] == "Delivery"
    assert later.other_steps == ["X"] and later.confirmed_by_business is False and later.source == "edited"
    earlier = reference.move_step(order, "C", -1)              # C passes B: joins Request, before B
    assert earlier.drawn_steps() == ["A", "C", "B", "D"] and earlier.stage_of()["C"] == "Request"
    assert reference.move_step(order, "A", -1).drawn_steps() == ["A", "B", "C", "D"]   # already first


def test_page3_correct_the_intended_flow(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from test_report import make_job
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    job = make_job(tmp_path, 300)
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.session_state["job"] = job
    at.run()
    at.switch_page("ui/map_page.py")
    at.run()
    assert not at.exception, at.exception
    assert any(e.label.startswith("Correct the intended flow") for e in at.expander)
    before = reference.load_order(reference.order_key(jobs.load_job(job["job_id"])))
    step = at.selectbox(key="ref_move_step_0").value
    at.button(key="ref_move_down").click().run()
    assert not at.exception, at.exception
    saved = reference.load_order(reference.order_key(jobs.load_job(job["job_id"])))
    assert before is None and saved is not None and saved.drawn_steps().index(step) == 1
    assert any("moved one place later" in s.value for s in at.success)
