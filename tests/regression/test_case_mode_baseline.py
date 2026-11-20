"""The case-ID mode must not change: rerun the pipeline on the baseline datasets and compare exactly.

The baseline is in tests/regression/baseline/ (written by make_baseline.py). A difference here means case-ID
behaviour changed: undo the change, or (only for a deliberate, reported change) rewrite the baseline.

Run with:  pytest -q tests/regression/test_case_mode_baseline.py
"""
import json

import pytest

from make_baseline import BASELINE_DIR, DATASETS, compare, run_dataset


@pytest.mark.parametrize("name", list(DATASETS))
def test_case_mode_is_unchanged(name, tmp_path, monkeypatch):
    path = BASELINE_DIR / f"{name}.json"
    assert path.exists(), f"No baseline for {name}: run python tests/regression/make_baseline.py"
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PM_DISABLE_PM4PY", "1")
    new = json.loads(json.dumps(run_dataset(name, tmp_path), sort_keys=True, default=str))
    old = json.loads(path.read_text(encoding="utf-8"))
    diffs = compare(old, new)
    assert not diffs, f"{name}: {len(diffs)} differences, first ones:\n" + "\n".join(diffs[:25])
