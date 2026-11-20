"""Day 4 tests: facts, confidence badges, the report and the one-command pipeline.

Jobs are written to a temporary data folder (DATA_DIR), never to data/.
Run with:  pytest -q tests/test_report.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

from app.core import jobs
from app.core.config import PROJECT_ROOT
from app.core.db import connect
from app.core.facts import build_facts, load_facts
from app.core.ingest import normalise, open_source, profile
from app.core.mapping import suggest_mapping
from app.core.mining import run_mining, save_mining
from app.core.quality import run_quality, save_report
from app.core.report import TITLES, generate_report
from app.core.summary import PLACEHOLDER, render, template_summary
from scripts.generate_synthetic import main as generate

# The optimised report's sheets (no active time, stage, Level 4 or definition in this synthetic log).
# No trusted order (the built-in template is not used, 01b): no "Conformance" sheet.
SHEETS = ["Summary", "Key numbers", "Data scope", "Steps", "Stage hours", "Step x application", "Applications",
          "Waiting", "Rework", "People", "Trend", "Variants", "Data quality", "Facts", "Checks", "Mapping"]


@pytest.fixture
def data_folder(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(folder))
    return folder


def make_job(tmp: Path, cases: int, band: str | None = None) -> dict:
    """A job through pages 1-3. `band` overrides the quality band used for the analysis."""
    src = tmp / f"log_{cases}.parquet"
    generate(["--cases", str(cases), "--start", "2024-01-01", "--months", "12", "--out", str(src)])
    job = jobs.create_job(src.name, src)
    folder = jobs.job_folder(job["job_id"])
    con = connect()
    open_source(con, src, folder)
    prof = profile(con)
    m = {role: s.column for role, s in suggest_mapping(prof).items()}
    m.update(timestamp_format=prof.by_name()[m["timestamp"]].ts_format, end_timestamp_format=None,
             timezone="UTC", scope={}, exclude_columns=[], merge_repeats=False)
    summary = normalise(con, m, folder / "events.parquet")
    (folder / "mapping.json").write_text(json.dumps(m), encoding="utf-8")
    report = run_quality(connect(), folder / "events.parquet")
    save_report(report, folder)
    band = band or report.band
    mining = run_mining(connect(), folder / "events.parquet", band)
    save_mining(mining, folder)
    job.update(status="mined", summary=summary, feasibility=report.feasibility, band=band,
               format="parquet")
    jobs.save_job(job)
    return job


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def section_html(html: str, number: int) -> str:
    return html.split(f'<h2>{number}. ')[1].split("</section>")[0]


def test_facts_are_stable_and_confident(data_folder, tmp_path):
    job = make_job(tmp_path, 3000)
    folder = jobs.job_folder(job["job_id"])
    quality, mining = read_json(folder / "quality.json"), read_json(folder / "mining.json")
    first = build_facts(connect(), folder / "events.parquet", quality, mining, job)
    second = build_facts(connect(), folder / "events.parquet", quality, mining, job)
    ids = [f.id for f in first]
    assert len(ids) == len(set(ids))
    assert [(f.id, f.label, f.value) for f in first] == [(f.id, f.label, f.value) for f in second]
    median = next(f for f in first if f.type == "case_duration_median")
    assert median.ci_low is not None and median.ci_low <= median.value <= median.ci_high
    top = next(f for f in first if f.type == "variant_share" and f.meta["rank"] == 1)
    assert top.badge == "High" and top.stability == 1.0


def test_small_log_is_mostly_exploratory(data_folder, tmp_path):
    job = make_job(tmp_path, 60, band="High")   # forced, so every section is analysed
    paths = generate_report(job["job_id"])
    facts = load_facts(jobs.job_folder(job["job_id"]))
    # Hours facts (section "hours") are sums over the data, not estimates: they are kept at any support.
    analysis = [f for f in facts if f.section not in ("data", "hours")]
    assert all(f.badge != "High" for f in analysis)
    assert all((f.support or 0) >= 30 for f in analysis)   # the rest is hidden
    assert paths["html"].exists() and "14. Appendix" in paths["html"].read_text(encoding="utf-8")


def test_band_low_hides_sections(data_folder, tmp_path):
    job = make_job(tmp_path, 3000, band="Low")
    paths = generate_report(job["job_id"])
    html = paths["html"].read_text(encoding="utf-8")
    for number in (8, 9, 10, 12):          # waiting, rework, people, trend: blocked by the Low band
        assert "Not available" in section_html(html, number)
    facts = load_facts(jobs.job_folder(job["job_id"]))
    assert not [f for f in facts if f.section == "bottlenecks"]
    summary = template_summary(facts, "Low")
    assert len(summary) == 1 and "exploratory" in summary[0]["text"]


def test_summary_and_report_files(data_folder, tmp_path):
    job = make_job(tmp_path, 3000)
    paths = generate_report(job["job_id"])
    folder = jobs.job_folder(job["job_id"])
    facts = load_facts(folder)
    by_id = {f.id: f for f in facts}
    findings = template_summary(facts, "High")
    assert 3 <= len(findings) <= 5
    for x in findings:
        assert x["facts"] and all(by_id[i].badge in ("High", "Medium") for i in x["facts"])
        assert set(PLACEHOLDER.findall(x["text"])) == set(x["facts"])
        assert "{{F" not in render(x["text"], facts)
    medium_only = template_summary(facts, "Medium")
    assert all(by_id[i].badge == "High" for x in medium_only for i in x["facts"])

    html = paths["html"].read_text(encoding="utf-8")
    for number, (_, title) in enumerate(TITLES, 1):
        assert f"<h2>{number}. {title}</h2>" in html
    assert "{{F" not in html and "Root-cause" not in html and "Slowest activities" not in html
    assert "calculation checks passed" in html
    assert load_workbook(paths["excel"]).sheetnames == SHEETS
    ws = load_workbook(paths["excel"])["Variants"]
    assert isinstance(ws["B2"].value, int) and isinstance(ws["C2"].value, float)  # numbers, not text
    assert jobs.load_job(job["job_id"])["status"] == "reported"


def run_cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "run_pipeline.py"), *args],
                          cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=300)


def test_pipeline_end_to_end(tmp_path):
    dataset = tmp_path / "daily"
    generate(["--cases", "1500", "--start", "2024-01-01", "--months", "3", "--missing-case", "0.01",
              "--split-by", "month", "--out", str(dataset)])
    data = tmp_path / "cli_data"
    ok = run_cli("--input", str(dataset), "--auto-map", "--drop-missing-case", "--merge-repeats",
                 "--data-dir", str(data))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    reports = list((data / "jobs").glob("*/report.html"))
    assert len(reports) == 1 and (reports[0].parent / "report.xlsx").exists()
    assert "Quality:" in ok.stdout and "Report (HTML):" in ok.stdout
    assert re.search(r"Rows left out:\s+[1-9]", ok.stdout)   # the rows without a case ID

    bad = run_cli("--input", str(dataset), "--auto-map", "--timestamp", "vendor", "--data-dir", str(data))
    assert bad.returncode == 2, bad.stdout + bad.stderr
    assert "Timestamp parsing" in bad.stdout


def test_reported_job_is_listed_on_pages_2_3_4(data_folder, tmp_path):
    from streamlit.testing.v1 import AppTest
    job = make_job(tmp_path, 300)
    job.update(status="reported")
    jobs.save_job(job)
    for page in ("ui/quality_page.py", "ui/map_page.py", "ui/business_page.py", "ui/report_page.py"):
        at = AppTest.from_file(str(PROJECT_ROOT / "app" / "main.py"), default_timeout=120)
        at.run()
        at.switch_page(page)
        at.run()
        assert not at.exception, (page, at.exception)
        # the job is the current analysis (sidebar), so the page uses it without asking
        assert at.session_state["job"]["job_id"] == job["job_id"], page
        assert not any("not ready for this page" in i.value for i in at.info), page


def test_conformance_section_with_a_template_order(data_folder, tmp_path, monkeypatch):
    from app.core import reference
    from app.core.config import settings
    from app.core.templates import match_template, template_order
    job = make_job(tmp_path, 1500)
    folder = jobs.job_folder(job["job_id"])
    events = read_json(folder / "mining.json")["map"]["data"]["nodes"]
    template, mapping, share = match_template({n["activity"]: n["events"] for n in events
                                               if not n["activity"].startswith(("▶", "■"))})
    assert template is not None
    order = template_order(template, mapping, share, "Purchasing")
    # Template off (the default, 01b): a template order is not evidence, so no conformance on it.
    paths = generate_report(job["job_id"], use_ai=False, order=order)
    assert "Step order not confirmed" in section_html(paths["html"].read_text(encoding="utf-8"), 7)
    # Template switched on: conformance against it, as before.
    monkeypatch.setitem(settings()["templates"], "use_for_order", True)
    paths = generate_report(job["job_id"], use_ai=False, order=order)
    full = paths["html"].read_text(encoding="utf-8")
    html = section_html(full, 7)
    ref = read_json(folder / "reference.json")
    s = ref["stats"]
    assert "Not available" not in html and "Template: Purchase to pay" in full
    assert "Step order not confirmed" not in html
    # The report's conformance (metrics.py, first occurrences) equals the simple map's numbers.
    assert f"{100 * s['in_order_share']:.1f}%" in html and f"{100 * s['pair_order_share']:.1f}%" in html
    assert f"{100 * s['reach_last_share']:.1f}%" in html
    for e in s["earlier_than_intended"][:5]:
        assert f"{e['step']} done before {e['before']}" in html


def test_conformance_says_when_the_order_is_only_from_the_data(data_folder, tmp_path):
    job = make_job(tmp_path, 600)
    job["order_choice"] = "data"          # picked on page 3: the order found in the data, not the template
    jobs.save_job(job)
    full = generate_report(job["job_id"], use_ai=False)["html"].read_text(encoding="utf-8")
    html = section_html(full, 7)
    # A guessed order is not used for conformance (B2.7): one line instead.
    assert "Step order not confirmed — enter it on page 4 to see conformance." in html
    assert "Found in data — not confirmed; steps sorted by cases reached" in full
