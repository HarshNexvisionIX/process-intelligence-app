"""Everything recorded for one process, in one place: the process library (stages and steps, rules, documents,
versions, decisions) and every analysis linked to the process with its results, reports, readout, evidence and
the AI requests that were prepared, approved and sent.

Nothing is copied: each analysis keeps its own folder (data/jobs/<id>/), and this module reads them whenever the
process is opened, so the view is always current. `bundle()` writes one ZIP of all of it for keeping or sharing.
Event tables (*.parquet) and uploaded raw files are never listed or bundled: they hold the row-level data.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app.core import jobs

ALLOWED = {".json", ".jsonl", ".html", ".xlsx", ".csv", ".md", ".txt", ".yaml"}
NEVER = {"job.json"}                       # internal; its content is shown as the analysis summary instead

# name -> (category, label). Anything else allowed is listed as "Other results".
KNOWN = {
    # reports
    "nc_report.html": ("Reports", "Report (HTML)"), "nc_report.xlsx": ("Reports", "Report (Excel)"),
    "report.html": ("Reports", "Report (HTML)"), "report.xlsx": ("Reports", "Report (Excel)"),
    # AI answers
    "readout.json": ("AI answers", "AI Analyst readout"),
    "ai_process_map.json": ("AI answers", "AI-enhanced conceptual map"),
    "stage_explanations.json": ("AI answers", "Step explanations and Level 4 summaries"),
    "nc_summary.json": ("AI answers", "Report summary"), "summary.json": ("AI answers", "Report summary"),
    # what was sent to the AI
    "ai_packages.json": ("AI requests (JSON sent)", "Requests prepared, with the exact messages"),
    "ai_approvals.json": ("AI requests (JSON sent)", "Approvals: who approved which request"),
    "ai_audit.jsonl": ("AI requests (JSON sent)", "Audit log: every request sent"),
    # evidence (measured)
    "nc_facts.json": ("Evidence", "Facts used by the report"), "facts.json": ("Evidence", "Facts used by the report"),
    "nc_measures.json": ("Evidence", "Hours, people and runs per step"), "nc_flow.json": ("Evidence", "Process map"),
    "nc_inside_step.json": ("Evidence", "Inside each step (Level 4)"), "nc_confidence.json": ("Evidence", "Confidence"),
    "nc_checks.json": ("Evidence", "Report checks"), "nc_evidence_scores.json": ("Evidence", "Evidence scores"),
    "mining.json": ("Evidence", "Process mining results"), "quality.json": ("Evidence", "Data quality"),
    "reference.json": ("Evidence", "Intended order and how the cases follow it"),
    "metrics.json": ("Evidence", "Report numbers"), "checks.json": ("Evidence", "Report checks"),
    "narrative.json": ("AI answers", "Report summary"),
}
ORDER = ["Reports", "AI answers", "AI requests (JSON sent)", "Evidence", "Other results"]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def job_files(folder: Path) -> list[dict]:
    """The result files of one analysis that may be shown or downloaded (top level only, no event tables)."""
    out = []
    if not folder.exists():
        return out
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name in NEVER or p.suffix.lower() not in ALLOWED:
            continue
        cat, label = KNOWN.get(p.name, ("Other results", p.stem.replace("_", " ")))
        st = p.stat()
        out.append({"name": p.name, "category": cat, "label": label, "bytes": st.st_size, "modified": _iso(st.st_mtime)})
    return sorted(out, key=lambda f: (ORDER.index(f["category"]), f["label"]))


def safe_file(folder: Path, name: str) -> Path:
    """The file `name` of an analysis folder if it may be downloaded; ValueError otherwise."""
    p = (folder / name)
    if "/" in name or "\\" in name or name.startswith(".") or p.name in NEVER or p.suffix.lower() not in ALLOWED \
            or not p.is_file():
        raise ValueError("Not a downloadable result of this analysis.")
    return p


def exports(job: dict, files: set[str]) -> list[dict]:
    """Documents built on request (not stored as files): PDF and HTML exports of this analysis."""
    jid = job["job_id"]
    nocase = job.get("mode") == "nocase"
    out = []
    if (nocase and "nc_report.html" in files) or (not nocase and "report.html" in files):
        out += [{"label": "Stakeholder report (PDF)", "url": f"/api/jobs/{jid}/report.pdf"},
                {"label": "Stakeholder report (HTML)", "url": f"/api/jobs/{jid}/stakeholder.html"}]
    if "readout.json" in files:
        out += [{"label": "AI Analyst readout (PDF)", "url": f"/api/jobs/{jid}/readout.pdf"},
                {"label": "AI Analyst readout (HTML)", "url": f"/api/jobs/{jid}/readout.html"}]
    if ("nc_flow.json" if nocase else "mining.json") in files:
        out += [{"label": "Process map (PDF)", "url": f"/api/jobs/{jid}/map.pdf"},
                {"label": "Process map (HTML)", "url": f"/api/jobs/{jid}/map.html"}]
    return out


def library_files(lib_folder: Path) -> list[dict]:
    out = []
    if lib_folder.exists():
        for p in sorted(lib_folder.rglob("*")):
            if p.is_file():
                rel = p.relative_to(lib_folder).as_posix()
                kind = ("Documents" if rel.startswith("docs/") else "Versions" if rel.startswith("versions/")
                        else "Stages, steps and rules")
                out.append({"path": rel, "kind": kind, "bytes": p.stat().st_size, "modified": _iso(p.stat().st_mtime)})
    return out


def ai_counts(folder: Path) -> dict:
    """How many AI requests were prepared, approved and sent for this analysis."""
    def read(name, default):
        try:
            return json.loads((folder / name).read_text(encoding="utf-8")) if (folder / name).exists() else default
        except ValueError:
            return default
    prepared = sum(len((v or {}).get("packages") or []) for v in (read("ai_packages.json", {}) or {}).values())
    approvals = read("ai_approvals.json", {}) or {}
    approved = sum(len(v or {}) for v in approvals.values()) if isinstance(approvals, dict) else 0
    sent = 0
    if (folder / "ai_audit.jsonl").exists():
        sent = sum(1 for line in (folder / "ai_audit.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())
    return {"prepared": prepared, "approved": approved, "sent": sent}


def analyses_of(process: str) -> list[dict]:
    return [j for j in jobs.list_jobs() if j.get("process") == process]


def bundle(process: str, name: str, lib_folder: Path) -> bytes:
    """One ZIP: the process library folder and, per analysis, its results, reports, evidence and AI records."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        index = {"process": process, "name": name, "created_at": datetime.now(timezone.utc).isoformat(),
                 "note": "Results only: event tables (*.parquet) and uploaded raw files are not included.",
                 "analyses": []}
        for f in library_files(lib_folder):
            z.write(lib_folder / f["path"], f"process_library/{f['path']}")
        for j in analyses_of(process):
            folder = jobs.job_folder(j["job_id"])
            files = job_files(folder)
            for f in files:
                z.write(folder / f["name"], f"analyses/{j['job_id']}/{f['name']}")
            index["analyses"].append({"job_id": j["job_id"], "source_name": j.get("source_name"),
                                      "created_at": j.get("created_at"), "mode": j.get("mode"),
                                      "files": [f["name"] for f in files], "ai": ai_counts(folder)})
        z.writestr("index.json", json.dumps(index, ensure_ascii=False, indent=1))
    return buf.getvalue()
