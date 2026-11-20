"""N2: work episodes, the case-link test, layered step assignment, evaluation, decisions in plain words.

Run with:  pytest -q tests/nocase/test_n2_strategies.py
"""
import json
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from app.core import jobs
from app.core.config import PROJECT_ROOT
from app.nocase.canonical import NcMapping
from app.nocase.pipeline import run
from app.nocase.stages import run_n2
from fixtures import SHAPES, icc_like_paused, load_mapping, make

PACKS = Path(__file__).parent / "packs"


def _library(tmp_path, name):
    shutil.copytree(PACKS / name, tmp_path / "data" / "library" / name)


def _job(src, mapping, process=None, forced=None):
    r = run(src, mapping)
    folder = jobs.job_folder(r["job"]["job_id"])
    return folder, run_n2(folder, forced, process)


def _decisions(folder):
    return json.loads((folder / "nc_decisions.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- 1. ICC-like
def test_icc_counter_is_never_a_case(tmp_path):
    folder, out = _job(make("icc_like", tmp_path / "src"), load_mapping("icc_like"))
    d = _decisions(folder)["case_link"]
    assert d["strategy"] == "NoCase" and d["decision"] == "no_case"
    assert any("event counter, not a case" in r for r in d["reasons"])
    tried = {t["strategy"]: t for t in d["tried"]}
    assert not tried["CaseColumn"]["applies"] and "not a case" in tried["CaseColumn"]["reason"]
    assert pd.read_parquet(folder / "nc_case_links.parquet").empty


def test_actor_app_context_is_purer_than_day_window(tmp_path):
    from app.nocase.eval.evaluate import evaluate
    folder, _ = _job(icc_like_paused(tmp_path / "src"), load_mapping("icc_like"),
                     forced={"episodes": "ActorAppContext"})
    e = evaluate(folder)
    eps = e["episodes"]
    assert eps["ActorAppContext"]["purity"] > eps["DayWindow"]["purity"]
    assert eps["ActorAppContext"]["purity"] >= eps["ActorGap"]["purity"]
    assert _decisions(folder)["episodes"]["strategy"] == "ActorAppContext"
    assert _decisions(folder)["episodes"]["tried"][0]["forced"]


# --------------------------------------------------------------------------- 2. AP-like
def test_ap_ids_in_titles_reach_case_mode_with_high_precision(tmp_path):
    from app.nocase.eval.evaluate import evaluate
    folder, _ = _job(make("ap_like", tmp_path / "src"), load_mapping("ap_like"))
    d = _decisions(folder)["case_link"]
    assert d["strategy"] == "IdPatternInText" and d["decision"] == "case", d
    assert d["quality"]["shape"] == "AAA-999999"
    e = evaluate(folder)
    assert e["case_link"]["precision"] >= 0.95
    links = pd.read_parquet(folder / "nc_case_links.parquet")
    assert links["used"].all() and not links["case_id"].str.contains("INV").any()      # hashed, never the value


# --------------------------------------------------------------------------- 3. HR-like
def test_hr_tickets_give_partial_mode_never_case(tmp_path):
    folder, _ = _job(make("hr_like", tmp_path / "src"), load_mapping("hr_like"))
    d = _decisions(folder)["case_link"]
    assert d["decision"] == "partial" and d["quality"]["partial_apps"] == ["ServiceNow"]
    assert "partial mode for ServiceNow" in d["message"]
    links = pd.read_parquet(folder / "nc_case_links.parquet")
    ev = pd.read_parquet(folder / "nc_events_clean.parquet")[["eid", "app"]]
    used = links[links["used"]].merge(ev, on="eid")
    assert set(used["app"]) == {"ServiceNow"}


# --------------------------------------------------------------------------- 4. collisions
def test_an_id_used_by_five_people_on_one_day_is_rejected(tmp_path):
    rows, t = [], datetime(2026, 3, 2, 9)
    for k in range(300):                                     # good IDs, one person each
        for j in range(3):
            rows.append({"ts": (t + timedelta(minutes=k * 10 + j)).isoformat(), "user": f"u{k % 4}", "app": "Desk",
                         "title": f"Request REQ{100000 + k} - Desk"})
    for p in range(5):                                       # one ID, five people, one day
        rows.append({"ts": (t + timedelta(minutes=p)).isoformat(), "user": f"p{p}", "app": "Desk",
                     "title": "Request REQ999999 - Desk"})
    src = tmp_path / "c.csv"
    pd.DataFrame(rows).to_csv(src, index=False)
    m = NcMapping(columns={"ts": "ts", "actor": "user", "app": "app", "text": "title"}, dataset_class="public")
    folder, _ = _job(src, m)
    q = _decisions(folder)["case_link"]["quality"]
    assert q["rejected_ids"] == 1 and q["conflict_share"] > 0
    ev = pd.read_parquet(folder / "nc_events.parquet")[["eid", "text"]]
    links = pd.read_parquet(folder / "nc_case_links.parquet").merge(ev, on="eid")
    assert not links["text"].str.contains("REQ999999").any()


# --------------------------------------------------------------------------- 5. assignment layers
def _label_data(tmp_path):
    rows, t = [], datetime(2026, 2, 2, 8)

    def add(screen, action, label, n):
        nonlocal t
        for _ in range(n):
            rows.append({"ts": t.isoformat(), "user": "a", "app": "Tool", "screen": screen, "action": action,
                         "l3": label})
            t += timedelta(seconds=30)
    add("Alpha Form", "Save Alpha", "Step One", 19)
    add("Alpha Form", "Save Alpha", "Step Two", 1)              # a labelled minority: keeps its own label
    add("Alpha Form", "Save Alpha", None, 5)                    # unlabelled: the unit majority
    add("Beta Form", "Save Beta", "Step One", 5)
    add("Beta Form", "Save Beta", "Step Two", 5)                # 50/50: a conflict
    add("Beta Form", "Save Beta", None, 3)
    src = tmp_path / "labels.csv"
    pd.DataFrame(rows).to_csv(src, index=False)
    return src, NcMapping(columns={"ts": "ts", "actor": "user", "app": "app", "screen": "screen", "action": "action",
                                   "l3": "l3"}, dataset_class="public")


def test_source_labels_are_kept_and_units_inherit_their_majority(tmp_path):
    src, m = _label_data(tmp_path)
    folder, _ = _job(src, m)
    ev = pd.read_parquet(folder / "nc_events_clean.parquet")
    alpha = ev[ev["screen_clean"] == "Alpha Form"]
    assert (alpha[alpha["l3"] == "Step Two"]["step"] == "Step Two").all()
    assert (alpha[alpha["l3"] == "Step Two"]["step_status"] == "observed").all()
    unl = alpha[alpha["l3"].isna()]
    assert (unl["step"] == "Step One").all() and (unl["step_status"] == "inferred_from_unit").all()
    a = pd.read_parquet(folder / "nc_assignments.parquet").set_index("unit_id")
    ua = alpha["unit_id"].iloc[0]
    assert a.at[ua, "confidence"] == pytest.approx(0.95)
    q = json.loads((folder / "nc_assign_quality.json").read_text(encoding="utf-8"))
    assert q["conflicts_count"] == 1 and {x["step"] for x in q["conflicts"][0]["labels"]} == {"Step One", "Step Two"}
    beta = ev[(ev["screen_clean"] == "Beta Form") & ev["l3"].isna()]
    assert (beta["step_status"] == "unassigned").all()
    assert q["by_source"]["source_label"]["events"] == int(ev["l3"].notna().sum())


def test_library_rules_fill_unlabelled_units_and_skip_proposed_and_unsafe(tmp_path):
    _library(tmp_path, "icc_like")
    folder, out = _job(make("icc_like", tmp_path / "src"), load_mapping("icc_like"), process="icc_like")
    ev = pd.read_parquet(folder / "nc_events_clean.parquet")
    sap = ev[ev["app"] == "SAP"]
    assert sap["step_status"].eq("rule").all()
    assert set(sap["step"]) == {"Invoice Check", "Credit Note"}
    assert (ev[ev["app"] == "Outlook"]["step_status"] == "unassigned").all()    # R3 is only proposed
    q = json.loads((folder / "nc_assign_quality.json").read_text(encoding="utf-8"))
    skipped = {r["rule_id"]: r["reason"] for r in q["rules_skipped"]}
    assert "nested quantifier" in skipped["R4"] and "proposed" in skipped["R3"]
    assert (folder / "nc_assignments.parquet").exists()
    stages = set(ev.loc[ev["step"].notna(), "stage"])
    assert stages == {"Checks"}


def test_a_pack_is_created_from_a_template_when_missing(tmp_path):
    from app.nocase import library
    pack = library.load_pack("purchase_to_pay")
    assert pack["stages"] and (tmp_path / "data" / "library" / "purchase_to_pay" / "pack.yaml").exists()
    hr = library.load_pack("hr_onboarding")
    assert library.step_order(hr)[0] == "Requisition Creation" and len(library.step_order(hr)) == 11
    assert len(hr["stages"]) == 4 and library.step_stage(hr)["Payroll Activation"] == "Hire Completion and Payroll Activation"
    with pytest.raises(ValueError):
        library.load_pack("no_such_process")


# --------------------------------------------------------------------------- 6. no eval in the engine
def test_engine_modules_do_not_read_eval_columns():
    for f in list((PROJECT_ROOT / "app" / "nocase" / "strategies").rglob("*.py")) + [
            PROJECT_ROOT / "app" / "nocase" / n for n in ("stages.py", "library.py", "pipeline.py", "bench.py")]:
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"eval_|EVAL_PREFIX|truth\.parquet", text), f.name


# --------------------------------------------------------------------------- 7. decisions in plain words
@pytest.mark.parametrize("shape", SHAPES)
def test_every_shape_runs_the_n2_stages_and_explains_each_choice(shape, tmp_path):
    folder, out = _job(make(shape, tmp_path / "src"), load_mapping(shape))
    for stage, info in out.items():
        assert info["status"] in ("ran", "skipped"), (stage, info)
        if info["status"] == "skipped":
            assert info["reason"]
    d = _decisions(folder)
    for stage in ("episodes", "case_link", "assign"):
        if out[stage]["status"] != "ran":
            continue
        assert len(d[stage]["message"]) > 20 and not re.search(r"PHR0\d|INV-\d{6}|REQ\d", d[stage]["message"])
        tried = d[stage].get("tried") or d[stage].get("layers")
        assert tried and all(t["reason"] for t in tried)
