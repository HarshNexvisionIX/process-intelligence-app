import pytest
"""No-case profile (counters, ID shapes, no values stored), cleaning and units; the command line and page 1.

Run with:  pytest -q tests/nocase
"""
import re
from pathlib import Path

import pandas as pd

from app.core.config import PROJECT_ROOT
from conftest import read_json


# --------------------------------------------------------------------------- 5. profile
def test_icc_counter_is_not_a_case(run_fixture):
    _job, folder, _ = run_fixture("icc_like")
    caps = read_json(folder / "nc_capabilities.json")
    assert caps["case_id"]["verdict"] == "not_a_case"
    assert any("an event counter, not a case" in r for r in caps["case_id"]["reasons"])
    assert [c["column"] for c in caps["counters"]] == ["seq"]
    assert not caps["supports"]["case_analysis"]["possible"]
    assert not caps["supports"]["source_step_labels"]["possible"]               # icc has no labels
    assert caps["action"]["generic_share"] > 0.3


def test_id_shapes_found_and_no_values_stored(run_fixture, tmp_path):
    _j, hr, _ = run_fixture("hr_like", tmp_path / "a")
    _j, ap, _ = run_fixture("ap_like", tmp_path / "b")
    hr_caps, ap_caps = read_json(hr / "nc_capabilities.json"), read_json(ap / "nc_capabilities.json")
    top = hr_caps["id_shapes"]["overall"][0]
    assert top["shape"] == "AAA9999999" and 0.2 < top["share"] < 0.4                # ~30% of titles
    assert list(hr_caps["id_shapes"]["per_app"]) == ["ServiceNow"]                   # all in the ServiceNow app
    assert ap_caps["id_shapes"]["overall"][0]["shape"] == "AAA-999999"
    assert 0.6 < ap_caps["id_shapes"]["overall"][0]["share"] < 0.8                   # ~70% of titles
    for folder in (hr, ap):
        for f in folder.glob("nc_*.json"):
            text = f.read_text(encoding="utf-8")
            assert not re.search(r"PHR0\d{6}|INV-\d{6}|Ayakha|Ningiza|Priya", text), f.name


def test_labels_and_supports(run_fixture):
    _job, folder, _ = run_fixture("hr_like")
    caps = read_json(folder / "nc_capabilities.json")
    assert caps["labels"]["lowest_level"] == "l3" and 0.8 < caps["labels"]["lowest_fill"] < 0.9
    assert caps["labels"]["consistency"] == 1.0                    # each unit belongs to one l3 in hr_like
    assert caps["supports"]["source_step_labels"]["possible"] and caps["time"]["active_time"] == "measured"
    assert caps["supports"]["case_link_test"]["possible"]


# --------------------------------------------------------------------------- 6. clean
def test_cleaning_examples():
    from app.nocase.clean import action_label, screen_label
    assert screen_label("View Worker - Ayakha Ningiza - Workday", "Workday") == "View Worker"
    assert screen_label("PHR0263894 - Case - ServiceNow", "ServiceNow") == "Case"
    assert screen_label("Payment Run INV-500007 - Oracle", "Oracle") == "Payment Run"
    for kept in ("Core Activity", "Work Notes", "Worker Profile"):
        assert action_label(kept) == kept
    assert action_label("Hello") == "<typed text>"
    assert action_label("John Smith") == "<person>"
    assert action_label(None) == "<other click>"


def test_clean_keeps_every_row_and_raw_text_stays_behind(run_fixture):
    _job, folder, r = run_fixture("hr_like")
    ev = pd.read_parquet(folder / "nc_events.parquet")
    cl = pd.read_parquet(folder / "nc_events_clean.parquet")
    assert len(cl) == len(ev) == r["scope"]["in_scope"]
    assert "text" not in cl.columns and "screen" not in cl.columns
    assert cl["action_clean"].notna().all()
    assert not cl["screen_clean"].fillna("").str.contains(r"PHR\d|Ayakha").any()


def test_case_mode_screen_cleaning_unchanged():
    """The optional extra_vocabulary of screens.clean_screen is off by default (the case mode)."""
    from app.core.screens import clean_screen
    assert clean_screen("Credit Memo - SAP", "SAP") is None
    assert clean_screen("Credit Memo - SAP", "SAP", extra_vocabulary=["memo"]) == "Credit Memo"


# --------------------------------------------------------------------------- 7. units
def test_unit_ids_are_stable_and_events_add_up(run_fixture, tmp_path, monkeypatch):
    _job, a, r = run_fixture("hr_like", tmp_path / "s1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data2"))
    _job, b, _ = run_fixture("hr_like", tmp_path / "s2")
    ua, ub = pd.read_parquet(a / "nc_units.parquet"), pd.read_parquet(b / "nc_units.parquet")
    keys = ["unit_id", "app", "screen_clean", "action_clean", "events"]
    assert ua[keys].equals(ub[keys])
    assert ua["events"].sum() == r["scope"]["in_scope"] and ua["unit_id"].iloc[0] == "U00001"
    assert ua["events"].is_monotonic_decreasing
    cl = pd.read_parquet(a / "nc_events_clean.parquet")
    assert cl["unit_id"].notna().all() and set(cl["unit_id"]) == set(ua["unit_id"])
    assert cl.groupby("unit_id").size().sort_index().tolist() == ua.set_index("unit_id")["events"].sort_index().tolist()


# --------------------------------------------------------------------------- command line
def test_command_line_run_and_steps(tmp_path, capsys):
    from fixtures import make
    from scripts.nocase import main
    src = make("ap_like", tmp_path / "src")
    data = tmp_path / "cli"
    assert main(["run", "--input", str(src), "--mapping", str(Path(__file__).parent / "mappings" / "ap_like.yaml"),
                 "--data-dir", str(data)]) == 0
    out = capsys.readouterr().out
    assert "Rows in file:" in out and "AAA-999999" in out and "INV-5" not in out
    job_id = re.search(r"Job (\S+) \(no case ID, public data\)", out).group(1)
    for cmd in ("profile", "clean", "units"):
        assert main([cmd, "--job", job_id, "--data-dir", str(data)]) == 0
    assert main(["profile", "--data-dir", str(data)]) == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("columns: {actor: user}\n", encoding="utf-8")
    assert main(["ingest", "--input", str(src), "--mapping", str(bad), "--data-dir", str(data)]) == 2


def test_real_file_mapping_is_valid():
    from app.nocase.canonical import NcMapping
    m = NcMapping.from_yaml(PROJECT_ROOT / "config" / "nocase_mappings" / "hr_skan_nocase.yaml")
    assert m.problems() == [] and m.dataset_class == "client" and m.keep_rows_where == {"is_process": ["1"]}
    assert m.eval == {"case_id": "case_id"} and m.columns["l3"] == "level3_process_name"


# --------------------------------------------------------------------------- 2. page 1 and the overview page
def _load_page1(src):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.run()
    next(t for t in at.text_input if t.label == "File, folder or pattern").input(str(src))
    at.run()
    next(b for b in at.button if b.label == "Load from this computer").click()
    at.run()
    assert not at.exception, at.exception
    return at


def test_page1_existing_modes_unchanged(tmp_path):
    from fixtures import make
    at = _load_page1(make("ap_like", tmp_path / "src"))
    assert at.radio(key="case_mode").options == ["Use a column", "Derive it", "No case ID (clickstream mapping)"]
    assert at.radio(key="case_mode").value == "Use a column"
    labels = [b.label for b in at.button]
    assert "Validate and convert" in labels and "Convert for clickstream mapping" not in labels
    assert not [s for s in at.selectbox if str(s.key or "").startswith("nc_")]
    at.radio(key="case_mode").set_value("Derive it").run()
    assert any(s.label == "How to derive the case" for s in at.selectbox)
    assert not at.exception, at.exception        # fixed after N1: a hint instead of a crash
    at.selectbox(key="case_method").set_value("work_session").run()
    assert not at.exception, at.exception
    assert "Validate and convert" in [b.label for b in at.button]


def test_page1_nocase_form_requires_the_dataset_class(tmp_path):
    from app.core import jobs
    from fixtures import make
    at = _load_page1(make("ap_like", tmp_path / "src"))
    at.radio(key="case_mode").set_value("No case ID (clickstream mapping)").run()
    assert not at.exception, at.exception
    assert "Validate and convert" not in [b.label for b in at.button]         # the case-ID form is not shown
    for std, col in {"ts": "timestamp", "actor": "user", "app": "app", "text": "title", "action": "action"}.items():
        at.selectbox(key=f"nc_col_{std}").set_value(col).run()
    convert = next(b for b in at.button if b.label == "Convert for clickstream mapping")
    if at.radio(key="nc_dataset_class").value is None:        # no class chosen when the file was loaded
        assert convert.disabled and any("dataset class" in e.value for e in at.error)
    else:                                                       # the class chosen at load is pre-selected
        assert at.radio(key="nc_dataset_class").value == at.radio(key="dataset_class_choice").value
    at.radio(key="nc_dataset_class").set_value("public").run()
    assert at.radio(key="dataset_class_choice").value == "public"              # the page's class follows
    convert = next(b for b in at.button if b.label == "Convert for clickstream mapping")
    assert not convert.disabled
    convert.click()
    at.run()
    assert not at.exception, at.exception
    at.run()                                                   # convert reruns the page (the menu switches)
    assert any("Converted:" in s.value for s in at.success)
    job = next(j for j in jobs.list_jobs())
    assert job["mode"] == "nocase" and job["dataset_class"] == "public"
    folder = jobs.job_folder(job["job_id"])
    assert (folder / "nc_units.parquet").exists()
    # the converted job is the current analysis: the menu is now the clickstream workflow (no case-ID pages)
    at.run()
    at.switch_page("ui/nocase_overview.py").run()
    assert not at.exception, at.exception
    assert any(s.value == "What this data supports" for s in at.subheader)
    assert any(s.value.startswith("Units") for s in at.subheader)
    with pytest.raises(ValueError):
        at.switch_page("ui/quality_page.py")
