"""N4 (Big prompt B, phase 2): measures, candidate map, confidence, report and self-checks, with every expected
number worked out BY HAND below (never by the app).

The golden log: 2 people (a, b), 3 steps, 3 apps, 3 work episodes (a on 2 March, a on 3 March, b on 2 March).

     #  who  time          step         app  active_s
     1  a    02-03 09:00   Prepare Data X    60
     2  a          09:01   Prepare Data X    30
     3  a          09:02   Prepare Data Y    30     <- one step run of Prepare Data spans X and Y
     4  a          09:03   Check Data   Y   120
     5  a          09:05   Check Data   Y    60
     6  a          09:06   Prepare Data X    30     <- Prepare Data again in the same episode: a repetition
     7  a          09:07   Send Data    Z    90
     8  a          09:09   Send Data    Z    30
     9  a    03-03 09:00   Check Data   Y    60
    10  a          09:01   Check Data   Z    60
    11  a          09:02   Send Data    Z   120
    12  b    02-03 10:00   Prepare Data X    30
    13  b          10:01   Prepare Data X    30
    14  b          10:02   Check Data   Y    60
    15  b          10:03   Send Data    Z    60
    16  b          10:04   Send Data    X    30
    17  b          10:05   Check Data   Y    30     <- Check Data again: a repetition

Measured time (seconds): Prepare 60+30+30+30+30+30 = 210; Check 120+60+60+60+60+30 = 390; Send 90+30+120+60+30 = 330;
total 930. Apps: X 60+30+30+30+30+30 = 210, Y 30+120+60+60+60+30 = 360, Z 90+30+60+120+60 = 360. People: a 690, b 240.
Step runs: a 2 March: Prepare(1-3) Check(4-5) Prepare(6) Send(7-8); a 3 March: Check(9-10) Send(11);
b: Prepare(12-13) Check(14) Send(15-16) Check(17) -> 10 runs, 2 repetitions.
Run seconds: Prepare 120, 30, 60 (median 60); Check 180, 120, 60, 30 (median 90); Send 120, 120, 90 (median 120).
Switches (same person, other app, within 30 min): 2-3, 5-6, 6-7, 9-10, 13-14, 14-15, 15-16, 16-17 = 8
(8 -> 9 is the next day). By step of the switching event: Prepare 2 (3, 6), Check 3 (10, 14, 17), Send 3 (7, 15, 16).
Matrix (s): Prepare X 180 Y 30; Check Y 330 Z 60; Send Z 300 X 30.
Estimated time (no active time): the gap to the person's next event, at most 300 s, the last one 0:
a 60 60 60 120 60 60 120 300(next day, capped) 60 60 0; b 60 60 60 60 60 0 ->
Prepare 360, Check 360, Send 540, total 1,260.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.core import jobs
from app.core.config import settings
from app.nocase.canonical import NcMapping
from app.nocase.pipeline import run
from app.nocase.stages import run_all

ROWS = [("a", "2026-03-02 09:00", "Prepare Data", "X", 60), ("a", "2026-03-02 09:01", "Prepare Data", "X", 30),
        ("a", "2026-03-02 09:02", "Prepare Data", "Y", 30), ("a", "2026-03-02 09:03", "Check Data", "Y", 120),
        ("a", "2026-03-02 09:05", "Check Data", "Y", 60), ("a", "2026-03-02 09:06", "Prepare Data", "X", 30),
        ("a", "2026-03-02 09:07", "Send Data", "Z", 90), ("a", "2026-03-02 09:09", "Send Data", "Z", 30),
        ("a", "2026-03-03 09:00", "Check Data", "Y", 60), ("a", "2026-03-03 09:01", "Check Data", "Z", 60),
        ("a", "2026-03-03 09:02", "Send Data", "Z", 120), ("b", "2026-03-02 10:00", "Prepare Data", "X", 30),
        ("b", "2026-03-02 10:01", "Prepare Data", "X", 30), ("b", "2026-03-02 10:02", "Check Data", "Y", 60),
        ("b", "2026-03-02 10:03", "Send Data", "Z", 60), ("b", "2026-03-02 10:04", "Send Data", "X", 30),
        ("b", "2026-03-02 10:05", "Check Data", "Y", 30)]
H = 3600


def ap(x):
    """Hours and shares are stored to 6 decimals (4 ms of an hour): compare within that."""
    return pytest.approx(x, abs=1e-5)


def golden(tmp_path, measured=True):
    df = pd.DataFrame(ROWS, columns=["who", "ts", "step", "app", "active"])
    df["screen"] = df["step"].str.replace(" Data", " Form")
    df["click"] = "Save Item"
    src = tmp_path / ("golden_m.csv" if measured else "golden_e.csv")
    df.to_csv(src, index=False)
    cols = {"ts": "ts", "actor": "who", "app": "app", "screen": "screen", "action": "click", "l3": "step"}
    if measured:
        cols["active_s"] = "active"
    r = run(src, NcMapping(columns=cols, dataset_class="public"))
    folder = jobs.job_folder(r["job"]["job_id"])
    run_all(folder)
    return folder


def _m(folder):
    return json.loads((folder / "nc_measures.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- 1. golden numbers
def test_golden_measures_measured_time(tmp_path):
    m = _m(golden(tmp_path))
    o = m["overall"]
    assert m["basis"]["time"] == "measured"
    assert (o["events"], o["actors"], o["apps"], o["episodes"]) == (17, 2, 3, 3)
    assert o["hours"] == ap(930 / H) and o["step_runs"] == 10 and o["repetitions"] == 2
    assert o["switches"] == 8 and o["unassigned_hours"] == 0 and o["month_end_share"] == 0
    steps = {s["step"]: s for s in m["steps"]}
    exp = {"Prepare Data": (210, 6, 3, 60, 1, 2), "Check Data": (390, 6, 4, 90, 1, 3), "Send Data": (330, 5, 3, 120, 0, 3)}
    for name, (sec, events, runs, med, reps, sw) in exp.items():
        s = steps[name]
        assert s["hours"] == ap(sec / H) and s["events"] == events and s["runs"] == runs, name
        assert s["median_run_s"] == med and s["repetitions"] == reps and s["switches"] == sw, name
        assert s["share_of_hours"] == ap(sec / 930) and s["actors"] == 2
    assert steps["Prepare Data"]["repetition_share"] == ap(1 / 3)
    assert steps["Check Data"]["repetition_share"] == ap(0.25)
    apps = {a["app_label"]: a["hours"] for a in m["apps"]}
    assert apps == ap({"X": 210 / H, "Y": 360 / H, "Z": 360 / H})
    actors = {a["actor_mask"]: a["hours"] for a in m["actors"]}
    assert actors == ap({"P01": 690 / H, "P02": 240 / H})               # masked, most hours first
    hod = {h["value"]: h["hours"] for h in m["time_of_day"] if h["hours"]}
    assert hod == ap({9: 690 / H, 10: 240 / H})
    dow = {h["value"]: h["hours"] for h in m["day_of_week"] if h["hours"]}
    assert dow == ap({0: 690 / H, 1: 240 / H})                          # Monday 2, Tuesday 3 March 2026
    mx = m["matrix_step_app"]
    cell = {(r, c): v for r, row in zip(mx["rows"], mx["cells"]) for c, v in zip(mx["columns"], row)}
    assert cell[("Prepare Data", "X")] == ap(180 / H) and cell[("Prepare Data", "Y")] == ap(30 / H)
    assert cell[("Check Data", "Y")] == ap(330 / H) and cell[("Check Data", "Z")] == ap(60 / H)
    assert cell[("Send Data", "Z")] == ap(300 / H) and cell[("Send Data", "X")] == ap(30 / H)
    assert mx["grand_total"] == ap(930 / H)


def test_golden_measures_estimated_time(tmp_path):
    m = _m(golden(tmp_path, measured=False))
    assert m["basis"]["time"] == "estimated" and m["basis"]["estimate_cap_seconds"] == 300
    steps = {s["step"]: s["hours"] for s in m["steps"]}
    assert steps == ap({"Prepare Data": 360 / H, "Check Data": 360 / H, "Send Data": 540 / H})
    assert m["overall"]["hours"] == ap(1260 / H)


# --------------------------------------------------------------------------- 2. merged-run trap
def test_a_step_run_over_two_apps_gives_each_app_its_own_hours(tmp_path):
    m = _m(golden(tmp_path))
    mx = m["matrix_step_app"]
    row = dict(zip(mx["columns"], mx["cells"][mx["rows"].index("Prepare Data")]))
    assert row["X"] == ap(180 / H) and row["Y"] == ap(30 / H)    # run 1-3: X 90 s, Y 30 s


# --------------------------------------------------------------------------- 3. map
def test_inferred_edges_need_two_people_and_the_dot_is_styled(tmp_path, monkeypatch):
    from app.nocase.engine import flow
    folder = golden(tmp_path)
    monkeypatch.setitem(settings()["nocase"]["flow"], "min_episodes", 2)
    f = flow.build(folder)
    kept = {(e["src"], e["dst"]) for e in f["edges"] if e["kept"]}
    assert kept == {("Prepare Data", "Check Data"), ("Check Data", "Send Data")}     # 2 episodes, 2 people each
    rest = {(e["src"], e["dst"]): e for e in f["edges"]}
    assert rest[("Check Data", "Prepare Data")]["actors"] == 1 and not rest[("Check Data", "Prepare Data")]["kept"]
    assert "style=dashed" in f["dot"] and "cluster_legend" not in f["dot"]   # the legend is the caption now
    many = {"nodes": f["nodes"], "spine": [], "observed": [],
            "edges": [{"src": f"S{i}", "dst": f"T{i}", "count": 30 - i, "kept": True, "status": "inferred"}
                      for i in range(20)]}
    assert len(flow.drawn_edges(many)) == 12


def test_documented_order_is_dotted_and_never_observed(tmp_path):
    import shutil
    import yaml
    lib = tmp_path / "data" / "library" / "golden"
    lib.mkdir(parents=True)
    (lib / "pack.yaml").write_text(yaml.safe_dump({"name": "Golden", "stages": [{"name": "All", "steps": [
        {"name": "Prepare Data"}, {"name": "Check Data"}, {"name": "Send Data"}]}]}), encoding="utf-8")
    from app.nocase.engine import flow
    folder = golden(tmp_path)
    f = flow.build(folder, "golden")
    assert [(e["src"], e["dst"], e["status"]) for e in f["spine"]] == [
        ("Prepare Data", "Check Data", "documented"), ("Check Data", "Send Data", "documented")]
    assert f["spine"][0]["support"] == ap(2 / 3)       # Prepare->Check twice, Check->Prepare once
    assert "style=dotted" in f["dot"]
    shutil.rmtree(lib)


# --------------------------------------------------------------------------- 4. the word "case"
def test_a_no_case_report_says_case_only_in_section_12_and_the_appendix(tmp_path):
    from app.nocase.report.report import build
    folder = golden(tmp_path)
    r = build(folder)
    html = Path(r["html"]).read_text(encoding="utf-8")
    assert not r["checks"]["failed"], [c for c in r["checks"]["checks"] if not c["passed"]]
    assert not r["title"].startswith("DRAFT")
    for sid in range(1, 12):
        sec = re.search(rf'<section id="s{sid}">(.*?)</section>', html, re.S).group(1)
        text = re.sub(r"<script.*?</script>", "", sec, flags=re.S)
        assert not re.search(r"\bcases?\b", re.sub(r"<[^>]+>", " ", text), re.I), f"section {sid}"
    assert Path(r["excel"]).exists()


# --------------------------------------------------------------------------- 5. a corrupted fact
def test_a_corrupted_fact_makes_the_report_draft(tmp_path, monkeypatch):
    from app.nocase.report import facts as fx
    from app.nocase.report.report import build
    folder = golden(tmp_path)
    real = fx.build

    def corrupt(f):
        out = real(f)
        out[2]["value"] = out[2]["value"] * 2                       # "Active hours" doubled
        (f / fx.FACTS_FILE).write_text(json.dumps(out), encoding="utf-8")
        return out
    monkeypatch.setattr(fx, "build", corrupt)
    r = build(folder)
    assert r["title"].startswith("DRAFT — 1 checks failed")
    bad = [c for c in r["checks"]["checks"] if not c["passed"]]
    assert bad[0]["check"] == "every fact equals its source" and "F003" in bad[0]["detail"]


# --------------------------------------------------------------------------- 6. calibrate
def test_calibrate_weights_the_more_accurate_signal_higher(tmp_path):
    from app.nocase.eval.calibrate import calibrate
    from fixtures import load_mapping, make
    r = run(make("icc_like", tmp_path / "src"), load_mapping("icc_like"))
    folder = jobs.job_folder(r["job"]["job_id"])
    run_all(folder)
    w = calibrate(folder)["weights"]
    assert w["screen"] > w["app_transition"]          # a screen names the step; SAP serves both steps
    assert (folder / "nc_evidence_weights.yaml").exists()
    from app.nocase.engine.confidence import weights
    assert weights(folder)["screen"] == w["screen"]
