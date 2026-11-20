"""Universality guards for the no-case mode (prompt N1b): it must work on ANY clickstream or UI log.

1. no process words in app/nocase/ (code and prompts): process knowledge lives in packs and settings only
2. every built stage runs on every synthetic shape, or skips with a written reason; never an exception
3. truth never leaks: not in nc_events.parquet, units, AI payloads (fake provider) or the bench report
4. shape-agnostic thresholds: no literal threshold in app/nocase/ code; they are in settings.yaml `nocase:`
5. smoke test: the first 5,000 rows of every present public dataset go through ingest -> units

Run with:  pytest -q tests/nocase/test_universal.py
"""
import ast
import json
import re
from pathlib import Path

import pandas as pd
import pytest
import yaml

from app.core.config import PROJECT_ROOT
from fixtures import SHAPES, load_mapping, make

NOCASE = PROJECT_ROOT / "app" / "nocase"
PROCESS_WORDS = ["ICC", "recharge", "intercompany", "PHR", "onboarding", "invoice", "Workday", "Exion",
                 "ServiceNow", "SAP"]


def _code_files():
    return [p for p in NOCASE.rglob("*") if p.is_file() and p.suffix in (".py", ".txt", ".md", ".yaml", ".j2")
            and "__pycache__" not in p.parts]


# --------------------------------------------------------------------------- 1
def test_no_process_words_in_the_engine():
    hits = []
    for f in _code_files():
        text = f.read_text(encoding="utf-8")
        for w in PROCESS_WORDS:
            flags = 0 if w.isupper() else re.I
            if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![a-z])", text, flags):
                hits.append(f"{f.relative_to(PROJECT_ROOT)}: {w}")
    assert not hits, hits


# --------------------------------------------------------------------------- 2
@pytest.mark.parametrize("shape", SHAPES)
def test_every_stage_runs_or_skips_with_a_reason(shape, tmp_path):
    from app.nocase.ingest import ingest
    from app.nocase.pipeline import run_stages
    from app.core import jobs
    src = make(shape, tmp_path / "src")
    job, scope = ingest(src, load_mapping(shape))
    assert scope["rows_in_file"] == scope["left_out_by_filter"] + scope["unreadable_time"] + scope["in_scope"]
    stages = run_stages(jobs.job_folder(job["job_id"]))
    assert set(stages) == {"profile", "clean", "units"}
    for name, s in stages.items():
        assert s["status"] in ("ran", "skipped"), (name, s)
        assert s.get("output") if s["status"] == "ran" else s.get("reason"), (name, s)


def test_a_stage_without_text_signals_is_skipped_with_a_reason(tmp_path):
    from app.core import jobs
    from app.nocase.canonical import NcMapping
    from app.nocase.ingest import ingest
    from app.nocase.pipeline import run_stages
    src = make("web_sessions", tmp_path / "src")
    job, _ = ingest(src, NcMapping(columns={"ts": "viewed_at", "actor": "visitor"}, dataset_class="public"))
    stages = run_stages(jobs.job_folder(job["job_id"]))
    assert stages["profile"]["status"] == "ran"
    assert stages["clean"]["status"] == "skipped" and "nothing to clean" in stages["clean"]["reason"]
    assert stages["units"]["status"] == "skipped" and stages["units"]["reason"]


# --------------------------------------------------------------------------- 3
TRUTH = {"hr_like": "case_id", "icc_like": "truth_case", "ap_like": "invoice_truth", "web_sessions": "session_truth",
         "rpa_ui": "routine_truth", "erp_docs": "order_truth"}


@pytest.mark.parametrize("shape", SHAPES)
def test_truth_never_leaks(shape, tmp_path):
    from app.core import jobs
    from app.nocase.ai_proxy import AIProxy
    from app.nocase.ingest import ingest
    from app.nocase.pipeline import run_stages
    src = make(shape, tmp_path / "src")
    raw = pd.read_parquet(src) if src.suffix == ".parquet" else pd.read_csv(src, dtype=str)
    values = sorted({str(v) for v in raw[TRUTH[shape]].dropna()}, key=len, reverse=True)[:40]
    mapping = load_mapping(shape)
    # A truth value may also be real input (an invoice number in the title): the raw events keep the input
    # as it is, so there only values that are NOT in the mapped input may not appear.
    inputs = raw[[c for c in mapping.mapped().values()]].astype(str).to_csv(index=False)
    foreign = [v for v in values if v not in inputs]
    job, _ = ingest(src, mapping)
    folder = jobs.job_folder(job["job_id"])
    run_stages(folder)
    for name in ("nc_events.parquet", "nc_events_clean.parquet", "nc_units.parquet"):
        if not (folder / name).exists():
            continue
        df = pd.read_parquet(folder / name)
        assert not [c for c in df.columns if c.startswith("eval_")], name
        if TRUTH[shape] == "case_id" and "case_id" in df.columns:
            assert df["case_id"].isna().all(), name                  # hr_like's case column is eval only
        text = df.astype(str).to_csv(index=False)
        check = foreign if name == "nc_events.parquet" else values
        leaked = [v for v in check if len(v) >= 5 and v in text]
        assert not leaked, (name, leaked[:3])
    for f in folder.glob("nc_*.json"):
        text = f.read_text(encoding="utf-8")
        assert not [v for v in values if len(v) >= 5 and v in text], f.name
    assert (folder / "eval" / "truth.parquet").exists()             # truth is kept apart, for evaluation
    proxy = AIProxy("fake", "public")
    proxy.complete("probe", [{"role": "user", "content": "units: " + ", ".join(
        pd.read_parquet(folder / "nc_units.parquet")["action_clean"].astype(str).head(20))}]) \
        if (folder / "nc_units.parquet").exists() else None
    assert not [v for v in values if len(v) >= 5 and v in proxy.payload_text()]


def test_truth_is_not_in_the_bench_report(tmp_path):
    from app.nocase.bench import load_specs, run_dataset, write_report
    root = tmp_path / "datasets"
    (root / "rpa").mkdir(parents=True)
    src = make("rpa_ui", root / "rpa")
    m = yaml.safe_load((Path(__file__).parent / "mappings" / "rpa_ui.yaml").read_text(encoding="utf-8"))
    spec = {"name": "rpa", "source_url": "synthetic", "licence": "test", "dataset_class": "public",
            "files": [{"path": src.name, "format": "parquet"}],
            "mapping": {"columns": m["columns"], "eval": m["eval"]}, "truth": {"columns": ["routine_truth"]},
            "sample": {"rows": None}, "expected": {"mode": "no_case"}}
    (root / "rpa" / "dataset.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    (spec, folder), = load_specs(root)
    row = run_dataset(spec, folder, tmp_path / "work", "fake")
    assert row["status"] == "ok", row
    j, md = write_report([row], tmp_path / "out")
    text = j.read_text(encoding="utf-8") + md.read_text(encoding="utf-8")
    assert "R0001" not in text and "R0259" not in text


def test_truth_columns_are_never_mapped_as_inputs():
    from app.nocase.bench import check_spec
    bad = {"name": "x", "dataset_class": "public", "mapping": {"columns": {"ts": "t", "case_id": "Case"}},
           "truth": {"columns": ["Case", "Activity"]}}
    assert any("truth column 'Case' is mapped as case_id" in p for p in check_spec(bad))
    for f in sorted((PROJECT_ROOT / "test_datasets").glob("*/dataset.yaml")):
        spec = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert check_spec(spec) == [], f


# --------------------------------------------------------------------------- 4
def test_no_literal_thresholds_in_the_engine():
    """Comparisons with a literal number other than 0 or 1, in code or in SQL text, are not allowed:
    thresholds live in settings.yaml under nocase:."""
    hits = []
    for f in NOCASE.rglob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for c in [node.left, *node.comparators]:
                    if (isinstance(c, ast.Constant) and isinstance(c.value, (int, float))
                            and not isinstance(c.value, bool) and c.value not in (0, 1)):
                        hits.append(f"{f.name}:{node.lineno} compares with {c.value}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for m in re.finditer(r"(?<![-=<>])(?:<=|>=|<|>)\s*(\d+(?:\.\d+)?)\b", node.value):
                    if float(m.group(1)) not in (0, 1):
                        hits.append(f"{f.name}:{node.lineno} SQL {m.group(0)}")
    assert not hits, hits


# --------------------------------------------------------------------------- 5
def _present_public():
    from app.nocase.bench import load_specs, presence
    return [(s, f) for s, f in load_specs() if presence(s, f)[0]]


@pytest.mark.parametrize("spec_folder", _present_public() or [pytest.param(None, marks=pytest.mark.skip(
    reason="no public dataset downloaded into test_datasets/"))])
def test_real_data_smoke(spec_folder, tmp_path):
    from app.nocase.bench import run_dataset
    spec, folder = spec_folder
    row = run_dataset(spec, folder, tmp_path / "work", "fake", max_rows=5000)
    assert row["status"] == "ok", row
    assert row["scope_adds_up"], row["scope"]
    assert all(s["status"] in ("ran", "skipped") for s in row["stages"].values()), row["stages"]
    json.dumps(row, default=str)
