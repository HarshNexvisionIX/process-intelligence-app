"""The two workflows (case ID and no case ID): their steps and when each step is done.

Shared by the Streamlit pages (app/ui/shell.py) and the web API (app/api/server.py), so both front ends show the
same progress for the same analysis.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from app.core import jobs

STEPS = {
    "case": [("import", "Import data"), ("context", "Business context"), ("quality", "Data quality"),
             ("analysis", "Process analysis"), ("definition", "Review steps"), ("case_map", "Process Map"),
             ("report", "Report")],
    # Web UI order: the process (stages and steps) is approved on Business context BEFORE the analysis, and the
    # screen-to-step rules are reviewed BEFORE the map is read, so the map always shows the approved process.
    "nocase": [("import", "Import data"), ("context", "Business context"), ("nc_overview", "Overview and analysis"),
               ("nc_steps", "Review steps"), ("nc_map", "Process Map"), ("nc_report", "Report")],
}


def mode_of(job: dict | None) -> str:
    return "nocase" if (job or {}).get("mode") == "nocase" else "case"


def _exists(folder: Path, *names: str) -> bool:
    return any((folder / n).exists() for n in names)


def context_done(job: dict | None) -> bool:
    """Business context is complete: a process is chosen and documents were added, or the reviewer recorded
    that there are none (with a reason)."""
    ctx = (job or {}).get("business_context") or {}
    return bool((job or {}).get("process")) and ctx.get("status") in ("documents", "no_documents")


def approved_steps(process: str | None) -> list[str]:
    """Names of the approved steps of the process (empty: nothing approved yet)."""
    if not process:
        return []
    from app.nocase import library as packs
    try:
        pack = packs.load_pack(process) or {}
    except (ValueError, OSError):
        return []
    return [sp.get("name") for st in pack.get("stages") or [] for sp in st.get("steps") or [] if sp.get("name")]


def pending_rules(process: str | None) -> list[dict]:
    if not process:
        return []
    from app.nocase import library as packs
    waiting = packs.pack_dir(process) / "rules.proposed.yaml"
    doc = (yaml.safe_load(waiting.read_text(encoding="utf-8")) if waiting.exists() else None) or {}
    return list(doc.get("rules", []) if isinstance(doc, dict) else doc)


def steps_reviewed(job: dict, folder: Path) -> bool:
    """No-case: the process has confirmed step rules, nothing proposed is waiting, no question is open."""
    try:
        from app.nocase import library as packs
        from app.nocase.questions import all_questions
        proc = job.get("process")
        if not proc:
            return False
        open_q = [x for x in all_questions(folder, proc) if not x["answer"]]
        confirmed = [r for r in packs.load_rules(proc) if r.get("status") in ("sme_confirmed", "rule")]
        return bool(confirmed) and not pending_rules(proc) and not open_q
    except (OSError, ValueError):
        return False


def step_status(job: dict | None) -> dict[str, bool]:
    """step key -> done, for the workflow of the job's mode."""
    if not job:
        return {}
    folder = jobs.job_folder(job["job_id"])
    if mode_of(job) == "nocase":
        return {"import": _exists(folder, "nc_events.parquet"),
                "context": context_done(job),
                "nc_overview": _exists(folder, "nc_measures.json"),
                "nc_map": _exists(folder, "nc_flow.json"),
                "nc_steps": steps_reviewed(job, folder),
                "nc_report": _exists(folder, "nc_report.html")}
    defined = False
    try:
        ref = json.loads((folder / "reference.json").read_text(encoding="utf-8")) \
            if (folder / "reference.json").exists() else {}
        defined = str(ref.get("choice", "")).startswith("confirmed")
    except (OSError, ValueError):
        defined = False
    return {"import": jobs.reached(job, "normalised"),
            "context": context_done(job),
            "quality": _exists(folder, "quality.json"),
            "analysis": jobs.reached(job, "mined"),
            "definition": defined,
            "case_map": defined and _exists(folder, "mining.json"),
            "report": _exists(folder, "report.html")}
