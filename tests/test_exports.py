"""Exports: the business process map and the process map as HTML and PDF; the readouts, the analysis, the insights
and the case-ID results as HTML. HTML and PDF are built from the same element list, so they hold the same content.

Run with:  pytest -q tests/test_exports.py
"""
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "tests" / "nocase" / "packs"


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)


def _new_process(c, jobs, name, pack, mode="nocase"):
    from app.nocase import library
    job = jobs.create_job("x.csv", None, "public")
    job.update(mode=mode)
    jobs.save_job(job)
    key = c.post(f"/api/jobs/{job['job_id']}/process", json={"new_name": name}).json()["process"]
    (library.pack_dir(key) / "pack.yaml").write_text(yaml.safe_dump(pack), encoding="utf-8")
    return job, key


# --------------------------------------------------------------------------- the business process map, as documented
def test_business_map_as_documented_exports_to_html_and_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.api.server import app
    from app.core import jobs
    c = TestClient(app)
    pack = {"name": "Export test", "provenance": {"source": "SME review", "status": "sme_confirmed", "confirmed": True},
            "stages": [{"name": "Intake", "steps": [{"name": "Receive request"}, {"name": "Check <request> & data"}]},
                       {"name": "Delivery", "steps": [{"name": "Approve request"}, {"name": "Notify customer"}]}]}
    _job, key = _new_process(c, jobs, "Export test", pack)
    for direction in ("LR", "TB"):
        r = c.get(f"/api/processes/{key}/map.html?dir={direction}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert "attachment" in r.headers["content-disposition"] and ".html" in r.headers["content-disposition"]
        page = r.text
        assert "<svg" in page and page.count("<rect") >= 2 + 4                 # 2 stage blocks + 4 steps
        for s in ("Intake", "Delivery", "Receive request", "Approve request", "Notify customer"):
            assert s in page
        assert "Check &lt;request&gt; &amp; data" in page and "<request>" not in page     # escaped
        assert "BUSINESS PROCESS MAP · AS DOCUMENTED" in page and "stroke-dasharray" in page   # dashed order arrows
    r = c.get(f"/api/processes/{key}/map.pdf")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    text = _pdf_text(r.content)
    for s in ("Export test", "Receive request", "Notify customer", "Steps", "Transitions"):
        assert s in text, s
    assert c.get("/api/processes/unknown/map.pdf").status_code == 404


# --------------------------------------------------------------------------- a whole no-case analysis
def _nocase_job(tmp_path, c, jobs):
    """hr_like through the whole no-case pipeline, with its pack as a user process, and the report."""
    sys.path.insert(0, str(ROOT / "tests" / "nocase"))
    from fixtures import load_mapping, make
    from app.nocase import library
    from app.nocase.pipeline import run
    from app.nocase.report.report import build
    from app.nocase.stages import run_all
    lib = library.library_dir() / "hr_export"
    shutil.copytree(PACKS / "hr_like", lib)
    r = run(make("hr_like", tmp_path / "src"), load_mapping("hr_like"))
    job = r["job"]
    job["process"] = "hr_export"
    jobs.save_job(job)
    folder = jobs.job_folder(job["job_id"])
    run_all(folder, process="hr_export")
    build(folder, "hr_export")
    return job, folder


def test_nocase_maps_readouts_and_pages_export(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.api import pdf as pdf_mod
    from app.api import readout as ro
    from app.api.server import app
    from app.core import jobs
    c = TestClient(app)
    job, folder = _nocase_job(tmp_path, c, jobs)
    jid = job["job_id"]
    # the business process map as observed in this analysis
    m = c.get("/api/processes/hr_export/map").json()
    assert m["analyses"] and m["analyses"][0]["job_id"] == jid
    page = c.get(f"/api/processes/hr_export/map.html?view={jid}").text
    assert "AS OBSERVED · NO CASE ID" in page and "Offer Processing" in page and " h" in page
    assert c.get(f"/api/processes/hr_export/map.pdf?view={jid}").content[:4] == b"%PDF"
    assert c.get(f"/api/processes/hr_export/map.pdf?view=not-a-job").status_code == 404
    # the process map at each level
    for level in ("stage", "step", "activity"):
        h = c.get(f"/api/jobs/{jid}/map.html?level={level}")
        assert h.status_code == 200 and "<svg" in h.text, level
        p = c.get(f"/api/jobs/{jid}/map.pdf?level={level}")
        assert p.status_code == 200 and p.content[:4] == b"%PDF", level
    # the stakeholder readout: HTML holds what the PDF holds
    html = c.get(f"/api/jobs/{jid}/stakeholder.html").text
    pdf = _pdf_text(c.get(f"/api/jobs/{jid}/report.pdf").content)
    for heading in ("1 · Scope", "2 · Where the time goes", "4 · Checked facts", "5 · Checks and limits"):
        assert heading in html and heading in pdf, heading
    # the AI readout: HTML only once there is one, the same sections as the PDF
    assert c.get(f"/api/jobs/{jid}/readout.html").status_code == 404
    report = {"headline": "Most effort sits in offers", "transformation_objective": "Shorten it.",
              "executive_message": "Start with offers.", "findings": [{"headline": "h", "interpretation": "i",
                                                                       "evidence_id": "E02"}],
              "risks": [], "interventions": [], "orchestrator_role": "r", "agents": [], "outcomes": [],
              "decisions": ["Agree the pilot"]}
    ro.save(folder, report, type("A", (), {"provider": "fake", "model": "fake-1"})(), "f" * 64)
    html = c.get(f"/api/jobs/{jid}/readout.html").text
    pdf = _pdf_text(c.get(f"/api/jobs/{jid}/readout.pdf").content)
    for part in ("Most effort sits in offers", "01 · Objective and scope", "05 · Interventions", "Agree the pilot"):
        assert part in html and part in pdf, part
    assert pdf_mod.readout_html  # the same element list renders both
    # the analysis and insights pages
    a = c.get(f"/api/jobs/{jid}/analysis.html")
    assert a.status_code == 200 and "Steps" in a.text and "Offer Processing" in a.text and "<table>" in a.text
    i = c.get(f"/api/jobs/{jid}/insights.html")
    assert i.status_code == 200 and "<table>" in i.text and "P01" in i.text
    for body in (a.text, i.text, html):
        assert "PHR0" not in body and "Ayakha" not in body           # nothing raw: cleaned values and P01… only


# --------------------------------------------------------------------------- a case-ID analysis
def test_case_map_and_results_export(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    src = tmp_path / "case.parquet"
    subprocess.run([sys.executable, "scripts/generate_synthetic.py", "--cases", "200", "--apps", "--out", str(src)],
                   check=True, cwd=ROOT, capture_output=True)
    from app.api.server import app
    c = TestClient(app)
    with open(src, "rb") as f:
        d = c.post("/api/jobs", files=[("files", ("case.parquet", f))],
                   data={"dataset_class": "public", "new_process": "Export test"}).json()
    jid = d["job"]["job_id"]
    m = {k: v["column"] for k, v in d["profile"]["case"]["suggested"].items()}
    assert c.post(f"/api/jobs/{jid}/convert/case", json={"mapping": m}).status_code == 200
    c.post(f"/api/jobs/{jid}/context/confirm", json={"by": "T", "no_documents_reason": "test"})
    c.post(f"/api/jobs/{jid}/quality")
    assert c.post(f"/api/jobs/{jid}/analysis").status_code == 200
    h = c.get(f"/api/jobs/{jid}/map.html").text
    assert "PROCESS MAP · CASE ID" in h and "Create Request" in h and " events" in h
    assert c.get(f"/api/jobs/{jid}/map.pdf").content[:4] == b"%PDF"
    r = c.get(f"/api/jobs/{jid}/case-results.html").text
    assert "Kpis" in r and "Variants" in r and "<table>" in r
    assert c.get(f"/api/jobs/{jid}/analysis.html").status_code == 200
    assert "1 · Data quality" in c.get(f"/api/jobs/{jid}/stakeholder.html").text


# --------------------------------------------------------------------------- the converter
def test_elements_html_escapes_text_and_keeps_bold():
    from reportlab.platypus import Paragraph
    from app.api.exports import _styles, elements_html
    st = _styles()
    page = elements_html([Paragraph("<b>Step</b> & <i>more</i><br/>next", st["body"]),
                          Paragraph("A < B", st["small"])], "T")
    assert "<b>Step</b> &amp; <i>more</i><br/>next" in page and "A &lt; B" in page


def test_a_big_map_is_drawn_and_lists_every_transition():
    from app.api.exports import MAX_DRAWN_EDGES, layout, map_elements, pdf_bytes
    nodes = [{"id": f"s{i}", "label": f"Step {i}", "group": f"Stage {i // 4}", "hours": i, "share": 0.01} for i in range(23)]
    edges = [{"source": f"s{i}", "target": f"s{(i * 7) % 23}", "kind": "observed", "count": 100 - i} for i in range(60)]
    lay = layout(nodes, edges)
    assert len(lay["boxes"]) == 23 and lay["hidden_edges"] == 60 - MAX_DRAWN_EDGES
    data = pdf_bytes(map_elements("Big", "K", "sub", nodes, edges), "Big", landscape_page=True)
    assert data[:4] == b"%PDF" and "less frequent transition" in _pdf_text(data)
