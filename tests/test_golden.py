"""Golden dataset: the whole pipeline on a small hand-written log, compared with hand-computed answers.

tests/golden/golden_events.csv (29 rows) and tests/golden/expected.yaml (every value worked out by
hand, never by the app). Any mismatch is a bug in the code (or a wrong definition in docs/METRICS.md).
Also the known risks of the audit (Part A3) and the report tests of Part B11.
Jobs are written to a temporary data folder (DATA_DIR), never to data/. Fake AI only (use_ai=False).
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest
import yaml
from openpyxl import load_workbook

from app.core import fmt, jobs, metrics, reference
from app.core.config import PROJECT_ROOT, settings
from app.core.db import connect
from app.core.ingest import normalise, open_source
from app.core.mining import run_mining, save_mining
from app.core.quality import run_quality, save_report

GOLDEN = Path(__file__).parent / "golden"
EXP = yaml.safe_load((GOLDEN / "expected.yaml").read_text(encoding="utf-8"))
A, B, C, D = "Receive Request", "Check Request", "Approve", "Notify"
TOL = 0.001
MAPPING = {"case_id": "case_id", "activity": "activity", "timestamp": "timestamp",
           "timestamp_format": "%Y-%m-%d %H:%M:%S", "end_timestamp": None, "end_timestamp_format": None,
           "timezone": "UTC", "resource": "user", "application": "application", "stage": "stage",
           "active_time": "active_time", "wait_time": "wait_time", "tat_time": "tat_time", "action": "action",
           "window_title": "window_title", "exclude_columns": [], "merge_repeats": False,
           "scope": {"drop_missing_case": True, "drop_missing_activity": True, "filter_column": "is_process",
                     "filter_values": ["1"]}}


def confirmed_order() -> reference.Order:
    return reference.Order("Golden", [{"name": "Intake", "steps": [A, B]}, {"name": "Delivery", "steps": [C, D]}],
                           [], "edited", True)


def make_job(tmp: Path, src: Path, mapping: dict = MAPPING) -> dict:
    job = jobs.create_job(src.name, src)
    folder = jobs.job_folder(job["job_id"])
    con = connect()
    open_source(con, src, folder)
    summary = normalise(con, mapping, folder / "events.parquet")
    (folder / "mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
    report = run_quality(connect(), folder / "events.parquet")
    save_report(report, folder)
    save_mining(run_mining(connect(), folder / "events.parquet", "High", use_ai=False), folder)
    job.update(status="mined", summary=summary, feasibility=report.feasibility, band="High", format="csv")
    jobs.save_job(job)
    return job


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    """The golden job through pages 1-5, with the business order confirmed."""
    tmp = tmp_path_factory.mktemp("golden")
    old = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(tmp / "data")
    try:
        src = tmp / "golden_events.csv"
        shutil.copy(GOLDEN / "golden_events.csv", src)
        job = make_job(tmp, src)
        paths = __import__("app.core.report", fromlist=["generate_report"]).generate_report(
            job["job_id"], use_ai=False, order=confirmed_order())
        folder = jobs.job_folder(job["job_id"])
        yield {"job": job, "folder": folder, "paths": paths, "tmp": tmp, "src": src,
               "m": metrics.load(folder), "html": paths["html"].read_text(encoding="utf-8")}
    finally:
        if old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = old


def h(seconds: float) -> float:
    return seconds / 3600


def close(a, b, tol=TOL) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


# --------------------------------------------------------------------------- #
# A2. Every metric against the hand-computed answers
# --------------------------------------------------------------------------- #
def test_scope_rows_and_hours_add_up(golden):
    sc, exp = golden["m"]["scope"], EXP["scope"]
    assert sc["rows_in_file"] == exp["rows_in_file"]
    assert close(sc["active_seconds_in_file"], exp["active_seconds_in_file"])
    for part in ("left_out_by_filter", "without_case", "without_activity", "without_time", "in_scope"):
        assert sc[part]["rows"] == exp[part]["rows"], part
        assert close(sc[part]["active_seconds"], exp[part]["active_seconds"]), part
    parts = [sc[p]["rows"] for p in ("left_out_by_filter", "without_case", "without_activity", "without_time", "in_scope")]
    assert sum(parts) == sc["rows_in_file"]                                    # A3.7: reasons add up


def test_totals_and_case_durations(golden):
    t, exp = golden["m"]["totals"], EXP["totals"]
    for k in ("rows", "events", "runs", "cases", "activities", "participants", "applications", "one_event_cases"):
        assert t[k] == exp[k], k
    for k in ("active_hours", "median_case_h", "p90_case_h", "mean_case_h"):
        assert close(t[k], exp[k]), (k, t[k])


def test_steps_in_business_order(golden):
    m = golden["m"]
    assert [r["step"] for r in m["steps"]] == [A, B, C, D]
    for r in m["steps"]:
        e = EXP["steps"][r["step"]]
        assert (r["stage"], r["cases"], r["runs"], r["events"]) == (e["stage"], e["cases"], e["runs"], e["events"])
        assert close(r["active_hours"], h(e["active_seconds"]))
        assert close(r["hours_share"], e["hours_share"]) and close(r["hours_per_case"], e["hours_per_case"])
        assert close(r["median_active_run_s"], e["median_active_run_s"])
        assert (r["rework_cases"], r["rework_runs"], r["quick_cases"], r["quick_runs"]) == \
               (e["rework_cases"], e["rework_runs"], e["quick_cases"], e["quick_runs"])
        assert close(r["rework_hours"], h(e["rework_seconds"]))
        assert close(r["waiting_hours"], h(e["wait_seconds"])) and close(r["waiting_share"], e["waiting_share"])
        assert close(r["cases_share"], e["cases"] / EXP["totals"]["cases"])     # A3.4: ÷ cases in scope


def test_stages_applications_and_matrix(golden):
    m = golden["m"]
    stages = {s["stage"]: s for s in m["stages"]}
    assert [s["stage"] for s in m["stages"]] == ["Intake", "Delivery"]        # B4: by the first step of each stage
    for name, e in EXP["stages"].items():
        assert close(stages[name]["active_hours"], h(e["active_seconds"])) and stages[name]["cases"] == e["cases"]
    apps = {a["application"]: a for a in m["applications"]}
    assert set(apps) == set(EXP["applications"])
    for name, e in EXP["applications"].items():
        assert close(apps[name]["active_hours"], h(e["active_seconds"]))
        assert (apps[name]["events"], apps[name]["cases"]) == (e["events"], e["cases"])
    mx = m["matrix"]
    for row in mx["rows"]:
        for app, v in zip(mx["apps"], row["values"]):
            assert close(v, h(EXP["matrix"][row["step"]][app])), (row["step"], app)
        assert close(row["total"], h(EXP["steps"][row["step"]]["active_seconds"]))
    for app, v in zip(mx["apps"], mx["column_totals"]):
        assert close(v, h(EXP["applications"][app]["active_seconds"]))
    assert close(mx["grand_total"], EXP["totals"]["active_hours"])


def test_transitions_and_waits(golden):
    m = golden["m"]
    assert sum(t["count"] for t in m["transitions"]) == EXP["transitions"]["total"]
    got = {(t["from"], t["to"]): t for t in m["transitions"]}
    assert len(got) == len(EXP["transitions"]["pairs"])
    for e in EXP["transitions"]["pairs"]:
        t = got[(e["from"], e["to"])]
        assert (t["count"], t["cases"]) == (e["count"], e["cases"])
        assert close(t["median_wait_h"], e["median_h"]) and close(t["total_wait_h"], e["total_h"])
        if "p90_h" in e:
            assert close(t["p90_wait_h"], e["p90_h"])


def test_rework_quick_switching_and_back_and_forth(golden):
    rw = golden["m"]["rework"]
    assert rw["rework_runs"] == EXP["rework"]["rework_runs"] and rw["quick_runs"] == EXP["rework"]["quick_runs"]
    assert [{k: b[k] for k in ("a", "b", "count", "cases")} for b in rw["back_and_forth"]] == EXP["rework"]["back_and_forth"]


def test_switches(golden):
    sw, e = golden["m"]["switching"], EXP["switching"]
    assert (sw["total"], sw["cases_with_switch"]) == (e["total"], e["cases_with_switch"])
    assert close(sw["per_case_median"], e["per_case_median"]) and close(sw["per_case_mean"], e["per_case_mean"])
    assert sw["pairs"] == e["pairs"]
    assert {x["step"]: {"switches": x["switches"], "runs": x["runs"]} for x in sw["by_step"]} == e["by_step"]


def test_variants_conformance_people_trend(golden):
    m = golden["m"]
    v = m["variants"]
    assert v["count"] == EXP["variants"]["count"] and close(v["top10_share"], EXP["variants"]["top10_share"])
    assert [(x["path"], x["cases"]) for x in v["top"]] == [(x["path"], x["cases"]) for x in EXP["variants"]["top"]]
    assert all(close(a["share"], b["share"]) for a, b in zip(v["top"], EXP["variants"]["top"]))
    c, e = m["conformance"], EXP["conformance"]
    for k in ("ordered_cases", "in_order_cases", "pairs", "pairs_ok"):
        assert c[k] == e[k], k
    assert close(c["in_order_share"], e["in_order_share"]) and close(c["pair_order_share"], e["pair_order_share"])
    assert c["earlier"] == e["earlier"] and {r["step"]: r["cases"] for r in c["reached"]} == e["reached"]
    assert close(c["reach_last_share"], e["reach_last_share"])
    people = [{**{k: p[k] for k in ("participant", "cases", "runs", "steps")}, "active_seconds": round(p["active_hours"] * 3600)}
              for p in m["people"]]
    assert people == EXP["people"]
    assert [{k: x[k] for k in ("from", "to", "count", "cases")} for x in m["handovers"]] == EXP["handovers"]
    assert m["total_handovers"] == EXP["total_handovers"]
    (w,) = m["trend"]
    e = EXP["trend"][0]
    assert (w["week"], w["cases_started"], w["partial"]) == (e["week"], e["cases_started"], e["partial"])
    assert close(w["active_hours"], h(e["active_seconds"])) and close(w["median_case_h"], e["median_case_h"])


def test_quality_counts(golden):
    q = json.loads((golden["folder"] / "quality.json").read_text(encoding="utf-8"))["stats"]
    assert (q["rows"], q["clean_rows"], q["cases"]) == (EXP["quality"]["rows"], EXP["quality"]["clean_rows"],
                                                        EXP["quality"]["cases"])


def test_level4_shares_for_a_given_taxonomy(golden):
    from app.core import level4
    e = EXP["level4"]
    entry = {"l3": e["step"], "source": "edited", "confirmed_by_business": True,
             "substeps": [{"id": f"S{i}", "label": t["label"], "description": "", "members": [],
                           "rules": [{"application": "*", "screen_regex": None, "click_regex": t["click_regex"]}]}
                          for i, t in enumerate(e["taxonomy"], 1)]}
    d = level4.detail(connect(), golden["folder"], e["step"], entry, None)
    shares = {n["name"]: n for n in d["nodes"]}
    for label, want in e["shares"].items():
        assert close(shares[label]["share"], want["events_share"]) and close(shares[label]["active_share"],
                                                                            want["active_share"]), label
    assert d["coverage"] == 1.0


def test_facts_equal_the_hand_computed_numbers(golden):
    facts = json.loads((golden["folder"] / "facts.json").read_text(encoding="utf-8"))
    by = {(f["type"], (f.get("meta") or {}).get("activity") or (f.get("meta") or {}).get("application")): f for f in facts}
    assert close(by[("active_hours_total", None)]["value"], EXP["totals"]["active_hours"])
    assert by[("step_runs_total", None)]["value"] == EXP["totals"]["runs"]
    for step, e in EXP["steps"].items():
        assert close(by[("step_hours", step)]["value"], h(e["active_seconds"]))
        assert close(by[("step_hours_share", step)]["value"], e["hours_share"])
    for app, e in EXP["applications"].items():
        assert close(by[("app_hours", app)]["value"], h(e["active_seconds"]))


# --------------------------------------------------------------------------- #
# B8. The report proves its own numbers
# --------------------------------------------------------------------------- #
def test_all_calculation_checks_pass(golden):
    checks = json.loads(golden["paths"]["checks"].read_text(encoding="utf-8"))
    assert checks["failed"] == 0, [c for c in checks["checks"] if not c["passed"]]
    assert {c["number"] for c in checks["checks"]} == set(range(1, 15)) | {18}  # 18 (01c): work sittings, Level 4 = inside the step; 13: Level 4 = step table; 14: people
    assert f"All {checks['total']} calculation checks passed" in golden["html"]
    assert not golden["html"].split("<h1>")[1].startswith("DRAFT")


def test_corrupted_number_makes_a_draft(golden, monkeypatch):
    from app.core import report
    real = metrics.compute

    def corrupt(*a, **k):
        m = real(*a, **k)
        m["variants"]["count"] += 1          # one number the report shows, checked by exactly one check
        return m
    monkeypatch.setattr(metrics, "compute", corrupt)
    html = report.generate_report(golden["job"]["job_id"], use_ai=False, order=confirmed_order())["html"] \
        .read_text(encoding="utf-8")
    assert html.split("<h1>")[1].startswith("DRAFT — 1 checks failed")
    monkeypatch.undo()
    report.generate_report(golden["job"]["job_id"], use_ai=False, order=confirmed_order())   # restore


def test_verify_script_exits_0(golden):
    out = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "verify_report.py"), golden["job"]["job_id"],
                          "--data-dir", str(golden["tmp"] / "data")], cwd=PROJECT_ROOT, capture_output=True,
                         text=True, encoding="utf-8", timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "✗" not in out.stdout and "numbers match" in out.stdout
    for secret in ("C1", "u1", "Ayakha", "Workday - "):
        assert secret not in out.stdout


# --------------------------------------------------------------------------- #
# B2-B6. What the report shows
# --------------------------------------------------------------------------- #
def test_formatting_helper():
    assert fmt.duration(38 / 3600) == "38 s"
    assert fmt.duration(4.2 / 60) == "4.2 min"
    assert fmt.duration(5.3) == "5.3 h"
    assert fmt.duration(3.4 * 24) == "3.4 days"
    assert fmt.duration(15 * 24) == "2.1 weeks"
    assert fmt.duration(0) == "0 s" and fmt.duration(None) == "–"
    assert fmt.hours(1234.56) == "1,234.6 h" and fmt.pct(0.4521) == "45.2%" and fmt.count(10907) == "10,907"
    assert fmt.pct(0) == "0.0%" and fmt.pct(None) == "–" and fmt.hours(0, zero_dash=True) == "–"


def test_report_sections_and_removed_filler(golden):
    from app.core.report import TITLES
    html = golden["html"]
    for number, (_, title) in enumerate(TITLES, 1):
        assert f"<h2>{number}. {title}</h2>" in html
    for gone in ("Slowest activities", "Root-cause", "Follow it", "Top difference", "needs the Graphviz viewer script",
                 "&lt; 1 min", "< 1 min", "as indicated by"):
        assert gone not in html, gone
    assert "Confirmed sequences" not in html.split("<h2>14. Appendix</h2>")[0]


def test_steps_without_a_stage_are_listed_not_drawn(golden, tmp_path, monkeypatch):
    from app.core.report import generate_report
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    df = pd.read_csv(GOLDEN / "golden_events.csv", dtype=str)
    df.loc[df["activity"] == D, "stage"] = None                     # Notify has no stage now
    src = tmp_path / "nostage.csv"
    df.to_csv(src, index=False)
    job = make_job(tmp_path, src)
    order = reference.Order("Golden", [{"name": "Intake", "steps": [A, B]}, {"name": "Delivery", "steps": [C]},
                                       {"name": "(no stage)", "steps": [D]}], [], "edited", True)
    html = generate_report(job["job_id"], use_ai=False, order=order)["html"].read_text(encoding="utf-8")
    process = html.split("<h2>4. ")[1].split("</section>")[0]
    assert "Other activities without a stage: Notify — 4 runs" in process
    table = process.split('<table class="steps">')[1].split("</table>")[0]
    assert ">Notify<" not in table and "(no stage)" not in table
    m = metrics.load(jobs.job_folder(job["job_id"]))
    dot = reference.to_reference_dot(*[__import__("app.core.report", fromlist=["x"])._drawn_order(
        json.loads((jobs.job_folder(job["job_id"]) / "reference.json").read_text(encoding="utf-8")))[0]],
        json.loads((jobs.job_folder(job["job_id"]) / "reference.json").read_text(encoding="utf-8"))["stats"])
    assert "(no stage)" not in dot and '"Notify"' not in dot
    assert [s["stage"] for s in m["stages"]][:2] == ["Intake", "Delivery"]


def test_conformance_only_with_a_trusted_order(golden, tmp_path, monkeypatch):
    from app.core.report import generate_report
    html = golden["html"].split("<h2>7. ")[1].split("</section>")[0]
    assert "80.0%" in html and "96.3%" in html and "Approve done before Check Request" in html
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    src = tmp_path / "g.csv"
    shutil.copy(GOLDEN / "golden_events.csv", src)
    job = make_job(tmp_path, src)
    html = generate_report(job["job_id"], use_ai=False)["html"].read_text(encoding="utf-8")
    sec = html.split("<h2>7. ")[1].split("</section>")[0]
    assert "Step order not confirmed — enter it on page 4 to see conformance." in sec and "%" not in sec
    assert "steps sorted by cases reached" in html
    m = metrics.load(jobs.job_folder(job["job_id"]))
    assert [r["cases"] for r in m["steps"]] == sorted((r["cases"] for r in m["steps"]), reverse=True)


def test_waiting_table_filters_and_ranks(golden, monkeypatch):
    m = golden["m"]
    assert m["waiting"]["rows"] == [] and m["waiting"]["left_out_low_support"] == 7     # every pair < 30 cases
    assert "No waiting longer than 1 hour between steps" in golden["html"]
    cfg = settings()["report"]
    monkeypatch.setitem(cfg, "min_cases", 1)
    m2 = metrics.compute(connect(), golden["folder"], json.loads((golden["folder"] / "reference.json").read_text(
        encoding="utf-8")))
    rows = m2["waiting"]["rows"]
    assert all(r["median_wait_h"] >= 1.0 for r in rows)                     # median under 60 min left out
    assert [(r["from"], r["to"]) for r in rows] == [(A, C), (C, B), (B, D)]   # by total waiting hours, then count
    assert m2["waiting"]["left_out_short"] == 4


def test_privacy_no_names_anywhere(golden):
    folder = golden["folder"]
    texts = {"report.html": golden["html"]}
    wb = load_workbook(golden["paths"]["excel"])
    texts["report.xlsx"] = " ".join(str(c.value) for ws in wb for row in ws.iter_rows() for c in row if c.value)
    for p in folder.rglob("*.json"):
        texts[p.name] = p.read_text(encoding="utf-8")
    for p in folder.glob("*.parquet"):
        if p.name in ("step_actions.parquet", "action_catalogue.parquet"):
            texts[p.name] = pd.read_parquet(p).astype(str).to_string()
    for name, text in texts.items():
        for secret in EXP["names"]:
            assert secret not in text, (name, secret)
    assert "u1" not in golden["html"].split("<h2>10. ")[1].split("</section>")[0]       # people are masked


@pytest.mark.parametrize("title, app, want", [
    ("View Job Application - Ayakha Ningiza - Workday", "Workday", "View Job Application"),
    ("View Worker - Razumovs - Workday", "Workday", "View Worker"),
    ("View Worker - (Terminated) - Workday", "Workday", "View Worker (Terminated)"),
    ("Casual Leave balance -06-17 20_10 IST.xlsx", "Excel", "Casual Leave balance.xlsx"),
    ("Prod Tracker 2026.xlsx", "Excel", "Prod Tracker.xlsx"),
    ("Search Results - P50052761 - Workday", "Workday", "Search Results"),
    ("Inbox - jane.doe@example.com - Outlook", "Outlook", "Inbox"),
])
def test_screen_labels_drop_people(title, app, want):
    from app.core.screens import clean_screen
    assert clean_screen(title, app) == want


# --------------------------------------------------------------------------- #
# A3. Known risks
# --------------------------------------------------------------------------- #
def test_idle_never_added_and_waiting_share_is_wait_over_tat(golden):
    by = {r["step"]: r for r in golden["m"]["steps"]}
    # Receive Request: wait 75 s, TAT 730 s; adding idle (30 s) would give 105 s.
    assert close(by[A]["waiting_share"], 75 / 730, 1e-9) and close(by[A]["waiting_hours"], h(75), 1e-9)


def test_run_active_time_is_the_sum_of_its_events_not_its_duration(golden):
    # C1's first Receive Request run = rows at 09:00 (120 s) and 09:02 (60 s): 180 s active, 2 min long.
    con = duckdb.connect()
    metrics.build_tables(con, golden["folder"] / "events.parquet")
    run = con.execute(f"SELECT active_s, epoch(end_ts) - epoch(start_ts), events FROM xrun "
                      f"WHERE activity = '{A}' AND events = 2").fetchall()
    assert run == [(180.0, 120.0, 2)]


def test_one_event_case_counts_with_duration_0(golden):
    assert golden["m"]["totals"]["one_event_cases"] == 1
    assert golden["m"]["totals"]["cases"] == 6


def test_hashed_case_ids_keep_the_distinct_count(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    raw = ["C1", "c1", "a1b2c3d4e5f60718", "A1B2C3D4E5F60718", "0000000000000001", "1", "01", "C1 "]
    src = tmp_path / "mixed.csv"
    pd.DataFrame({"case_id": raw, "activity": "Step", "timestamp": "2026-03-02 09:00:00"}).to_csv(src, index=False)
    m = {**MAPPING, "resource": None, "application": None, "stage": None, "active_time": None, "wait_time": None,
         "tat_time": None, "action": None, "window_title": None, "scope": {}}
    con = connect()
    open_source(con, src, tmp_path)
    s = normalise(con, m, tmp_path / "events.parquet")
    ev = pd.read_parquet(tmp_path / "events.parquet")
    assert s["cases"] == len(set(raw)) == ev["case_id"].nunique()
    assert not set(raw) & set(ev["case_id"])                                      # all hashed


def test_times_are_utc_and_date_filters_include_the_last_day(tmp_path):
    from app.core.mapping import timestamp_sql
    con = duckdb.connect()
    con.execute("CREATE VIEW src AS SELECT * FROM (VALUES ('2026-03-31 23:30:00'), ('2026-04-01 05:00:00')) t(ts)")
    got = [r[0] for r in con.execute(f"SELECT {timestamp_sql('ts', '%Y-%m-%d %H:%M:%S', 'Asia/Kolkata')} FROM src").fetchall()]
    assert [str(t) for t in got] == ["2026-03-31 18:00:00", "2026-03-31 23:30:00"]     # IST -> UTC
    last = pd.Timestamp("2026-03-31")
    inside = [t for t in got if pd.Timestamp(t).normalize() <= last]   # a period ending 31 March includes all of it
    assert len(inside) == 2


def test_short_words_match_whole_words_only():
    import re as _re
    from app.core.level4 import _compiled
    from app.core.screens import is_typed, word_pattern
    for text in ("Navigate to Process", "Mandatory", "History", "Font Size"):
        assert not is_typed(text), text
        assert not _re.search(word_pattern("hi|email"), text), text
    assert is_typed("Hello Team") and is_typed("Kind Regards, see attached") and is_typed("Hi")
    entry = {"substeps": [{"label": "Greeting", "rules": [{"application": "*", "click_regex": "hi|to"}]}]}
    rules, _ = _compiled(entry)
    _, _, _, click = rules[0]
    assert click.search("say hi") and not click.search("History") and not click.search("Mandatory")


def test_waits_use_run_ends_not_event_to_event(golden):
    # C1: Receive Request run ends 09:02, Check Request starts 09:10 -> 8 min (event-to-event would be 10 min).
    con = duckdb.connect()
    metrics.build_tables(con, golden["folder"] / "events.parquet")
    waits = sorted(round(w * 60, 6) for (w,) in con.execute(
        f"SELECT wait_h FROM xtrans WHERE src = '{A}' AND dst = '{B}'").fetchall())
    assert 8.0 in waits and 10.0 not in waits


def test_switches_need_same_case_same_user_and_30_minutes(golden):
    sw = pd.read_parquet(golden["folder"] / "app_switches.parquet")
    assert len(sw) == 4 and (sw["gap_seconds"] <= 1800).all()
    # C3: Excel -> Workday after 54 min is not a switch; C1: u1 Excel then u2 Workday is not a switch.
    assert set(zip(sw["from_app"], sw["to_app"])) == {("Workday", "Excel"), ("Workday", "Outlook")}


def test_conformance_ties_use_row_order(golden):
    # C4 has Check Request and Approve at the same time; the file lists Check Request first, so C4 is in order
    # (by name, Approve would come first and C4 would be out of order: 3 of 5 instead of 4 of 5).
    assert golden["m"]["conformance"]["in_order_cases"] == 4


def test_rounding_only_when_displayed(golden):
    t = golden["m"]["totals"]
    assert t["active_hours"] == pytest.approx(2245 / 3600, abs=1e-12)        # full precision stored
    assert fmt.hours(t["active_hours"]) == "0.6 h"                             # rounded for display only
    mining = json.loads((golden["folder"] / "mining.json").read_text(encoding="utf-8"))
    assert mining["kpis"]["data"]["median_h"] == pytest.approx(1.666667, abs=1e-6)   # not 1.67


def test_validator_rejects_filler_and_unmeasured_numbers():
    from app.core.facts import Fact
    from app.core.narrative import validate
    f = Fact(id="F001", type="step_hours", label="Active hours in Approve", value=12.5, unit="work_hours",
             support=500, badge="High", section="hours")

    def answer(text):
        return {"summary": [{"text": text, "facts": ["F001"]}], "findings": [], "recommendations": []}
    for bad in ("Approve is slow, as indicated by {{F001}}.", "Another fact is {{F001}}.",
                "Approve takes < 1 min of work ({{F001}})."):
        assert validate(answer(bad), {"F001"}, {"F001": f}), bad
    assert validate(answer("Approve takes {{F001}} of active work."), {"F001"}, {"F001": f}) == []
