"""N4b (Big prompt B, phase 3): inside each step — patterns, rework, variation between people, totals.

Run with:  pytest -q tests/nocase/test_n4b_inside.py
"""
import json
import re
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.core import jobs
from app.nocase.canonical import NcMapping
from app.nocase.engine import inside_step
from app.nocase.pipeline import run
from app.nocase.stages import run_all


def step_log(tmp_path, runs: list[tuple[str, list[str], int]], name="inside.csv"):
    """One step "Work"; each run = (actor, screens in order, seconds per event); runs one hour apart, so each run
    is its own work episode. Screen Alpha / Beta / Gamma become the activities (screen-based groups)."""
    rows, t = [], datetime(2026, 4, 6, 8)
    for actor, screens, sec in runs:
        for k, sc in enumerate(screens):
            rows.append({"ts": (t + timedelta(seconds=sec * k)).isoformat(), "who": actor, "app": "Tool",
                         "screen": f"{sc} Form", "click": f"Save {sc}", "step": "Work", "active": sec})
        t += timedelta(seconds=sec * len(screens)) + timedelta(hours=1)
    src = tmp_path / name
    pd.DataFrame(rows).to_csv(src, index=False)
    m = NcMapping(columns={"ts": "ts", "actor": "who", "app": "app", "screen": "screen", "action": "click",
                           "l3": "step", "active_s": "active"}, dataset_class="public")
    r = run(src, m)
    folder = jobs.job_folder(r["job"]["job_id"])
    run_all(folder)
    return folder, json.loads((folder / inside_step.INSIDE_FILE).read_text(encoding="utf-8"))


def _work(inside):
    return next(s for s in inside["steps"] if s["step"] == "Work")


def test_a_known_pattern_is_found_with_its_shares(tmp_path):
    runs = [(f"u{i % 4}", ["Alpha", "Beta", "Gamma"], 30) for i in range(70)] + \
           [(f"u{i % 4}", ["Alpha", "Gamma"], 30) for i in range(30)]
    _f, inside = step_log(tmp_path, runs)
    w = _work(inside)
    shares = {p["pattern"]: p["share"] for p in w["patterns"]}
    abc = next(k for k in shares if k.count("→") == 2)
    ac = next(k for k in shares if k.count("→") == 1)
    abc_l, ac_l = abc.lower(), ac.lower()             # labels are verb + object now ("Save alpha form")
    assert "alpha" in abc_l and "beta" in abc_l and "gamma" in abc_l and "beta" not in ac_l
    assert abs(shares[abc] - 0.70) <= 0.01 and abs(shares[ac] - 0.30) <= 0.01
    assert w["runs"] == 100 and all(p["status"] == "inferred" for p in w["patterns"])
    assert w["checks"] == {"activity_hours_equal_step_hours": True, "pattern_runs_equal_runs": True}
    assert "alpha" in w["start_activity"].lower() and "gamma" in w["end_activity"].lower()


def test_quick_returns_are_back_and_forth_not_rework(tmp_path):
    """A -> B -> A within minutes is normal switching (copy and paste between two screens), not rework."""
    runs = [(f"u{i % 3}", ["Alpha", "Beta", "Alpha"], 60) for i in range(25)] + \
           [(f"u{i % 3}", ["Alpha", "Beta"], 60) for i in range(75)]
    _f, inside = step_log(tmp_path, runs)
    w = _work(inside)
    assert w["rework_share"] == pytest.approx(0.0)
    assert w["back_and_forth_share"] == pytest.approx(0.25)
    assert any("⇄" in p["pattern"] for p in w["patterns"])          # A → B → A shown as "A ⇄ B"


def test_a_known_rework_share_is_found(tmp_path):
    """Rework as in the case-ID mode: the same activity again after another one AND after a pause of 30+ min."""
    long_b = ["Alpha"] + ["Beta"] * 40 + ["Alpha"]                   # 40 min of Beta (one click a minute)
    runs = [(f"u{i % 3}", long_b, 60) for i in range(25)] + [(f"u{i % 3}", ["Alpha", "Beta"], 60) for i in range(75)]
    _f, inside = step_log(tmp_path, runs)
    assert _work(inside)["rework_share"] == pytest.approx(0.25)


def test_a_slow_person_is_flagged_and_people_stay_masked(tmp_path):
    runs = [(f"u{i % 4}", ["Alpha", "Beta"], 30) for i in range(60)] + \
           [("slowpoke", ["Alpha", "Beta"], 400) for _ in range(15)]
    folder, inside = step_log(tmp_path, runs)
    v = _work(inside)["variation"]
    assert v["flagged"] and any("× the median run time" in r for r in v["reasons"])
    text = json.dumps(json.loads((folder / inside_step.INSIDE_FILE).read_text(encoding="utf-8")), ensure_ascii=False)
    assert "slowpoke" not in text and not re.search(r"\bu\d\b", text) and "P01" in text


def test_too_few_runs_say_so(tmp_path):
    _f, inside = step_log(tmp_path, [("u1", ["Alpha", "Beta"], 30) for _ in range(4)])
    w = _work(inside)
    assert not w["enough_runs"] and w["message"].startswith("not enough runs to describe how this step is done")


def test_a_broken_total_makes_the_report_draft(tmp_path):
    from app.nocase.engine.activities import ACT_FILE
    from app.nocase.report.report import build
    runs = [(f"u{i % 4}", ["Alpha", "Beta", "Gamma"], 30) for i in range(40)]
    folder, _ = step_log(tmp_path, runs)
    ok = build(folder)
    assert not ok["checks"]["failed"], [c for c in ok["checks"]["checks"] if not c["passed"]]
    acts = pd.read_parquet(folder / ACT_FILE)
    acts.loc[0, "events"] = acts.loc[0, "events"] + 1                  # break one activity total
    acts.to_parquet(folder / ACT_FILE, index=False)
    bad = build(folder)
    assert bad["title"].startswith("DRAFT")
    assert any(c["check"] == "activity totals = step totals" and not c["passed"] for c in bad["checks"]["checks"])


def test_inside_step_changes_no_earlier_output(tmp_path):
    from app.nocase.report import checks
    runs = [(f"u{i % 4}", ["Alpha", "Beta", "Gamma"], 30) for i in range(40)]
    folder, _ = step_log(tmp_path, runs)
    before = {n: (folder / n).read_text(encoding="utf-8") for n in ("nc_measures.json", "nc_flow.json")}
    (folder / inside_step.INSIDE_FILE).unlink()
    first = checks.run(folder)
    inside_step.build(folder)
    second = checks.run(folder)
    assert {n: (folder / n).read_text(encoding="utf-8") for n in before} == before
    assert [(c["check"], c["passed"]) for c in first["checks"]] == [(c["check"], c["passed"]) for c in second["checks"]]
