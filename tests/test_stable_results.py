"""Results do not depend on the case-hash salt (app/core/stable.py), and page 1 "Derive it" before an ID pattern.

Run with:  pytest -q tests/test_stable_results.py
"""
import json

import pandas as pd

from app.core import jobs
from app.core.config import PROJECT_ROOT
from app.core.derived import NO_PATTERN_YET, case_label


def _run(src, data, monkeypatch, salt):
    from scripts.run_pipeline import main as pipeline
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("CASE_SALT", salt)
    assert pipeline(["--input", str(src), "--auto-map", "--merge-repeats", "--no-ai"]) == 0
    return jobs.job_folder(jobs.list_jobs()[0]["job_id"])


def test_same_file_two_salts_gives_identical_results(tmp_path, monkeypatch):
    from scripts.generate_synthetic import main as generate
    src = tmp_path / "log.parquet"
    assert generate(["--cases", "400", "--seed", "11", "--months", "6", "--apps", "--stages", "--actions",
                     "--step-actions", "--out", str(src)]) == 0
    a = _run(src, tmp_path / "data_a", monkeypatch, "salt-one")
    b = _run(src, tmp_path / "data_b", monkeypatch, "salt-two")
    ids_a = set(pd.read_parquet(a / "events.parquet")["case_id"])
    ids_b = set(pd.read_parquet(b / "events.parquet")["case_id"])
    assert not ids_a & ids_b                                       # the stored case IDs really differ
    for name in ("metrics.json", "quality.json", "facts.json"):
        ja, jb = (json.loads((f / name).read_text(encoding="utf-8")) for f in (a, b))
        for d in (ja, jb):
            if isinstance(d, dict):                      # run times and clock stamps are not results
                for k in ("created_at", "seconds"):
                    d.pop(k, None)
                if isinstance(d.get("stats"), dict):
                    d["stats"].pop("now", None)
        assert ja == jb, name


def test_case_label_before_an_id_pattern_is_found():
    mapping = {"case_id": "__case", "case_source": {"kind": "text_pattern"}}
    assert case_label(mapping) == NO_PATTERN_YET
    assert case_label({"case_source": {"kind": "text_pattern", "pattern": "ticket", "column": "t"}}).startswith("Case = ")


def test_page1_derive_default_method_shows_the_hint(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from scripts.generate_synthetic import main as generate
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    src = tmp_path / "log.csv"
    assert generate(["--cases", "200", "--ids-in-title", "--drop-columns", "case", "--format", "csv",
                     "--out", str(src)]) == 0
    at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
    at.run()
    next(t for t in at.text_input if t.label == "File, folder or pattern").input(str(src))
    at.run()
    next(b for b in at.button if b.label == "Load from this computer").click()
    at.run()
    at.radio(key="case_mode").set_value("Derive it").run()
    assert not at.exception, at.exception
    shown = " ".join(str(getattr(e, "value", "")) for e in list(at.markdown) + list(at.caption) + list(at.info)
                     + list(at.warning))
    assert NO_PATTERN_YET in shown or NO_PATTERN_YET in str(at.dataframe and [d.value.to_dict() for d in at.dataframe])
