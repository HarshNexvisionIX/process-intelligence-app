"""The no-case steps in order, for the command line, the bench and the overview page:
ingest -> profile -> clean -> units. Every stage either runs (and says what it produced) or is skipped with a
written reason; later prompts add their stages here."""
from __future__ import annotations

from pathlib import Path

from app.nocase import clean, profile, units
from app.nocase.canonical import NcMapping
from app.nocase.ingest import ingest

TEXT_SIGNALS = ("screen", "text", "action")


def run_stages(folder: Path) -> dict[str, dict]:
    """Profile, clean and units for a converted job: {stage: {"status": ran|skipped, "reason"?, "output"?}}."""
    stages: dict[str, dict] = {}
    caps = profile.build(folder)
    stages["profile"] = {"status": "ran", "output": caps}
    if not any(caps["fill"][c]["rows"] for c in TEXT_SIGNALS):
        why = "no screen, text or action column is mapped: nothing to clean"
        stages["clean"] = {"status": "skipped", "reason": why}
        stages["units"] = {"status": "skipped", "reason": "units need cleaned screens or clicks (" + why + ")"}
        return stages
    stages["clean"] = {"status": "ran", "output": clean.build(folder)}
    stages["units"] = {"status": "ran", "output": units.build(folder)}
    return stages


def after_ingest(folder: Path) -> dict:
    """Profile, clean and units for a converted job; returns what each step reported."""
    stages = run_stages(folder)
    return {"capabilities": stages["profile"]["output"], "stages": stages,
            "clean": stages["clean"].get("output"), "units": stages["units"].get("output")}


def run(source, mapping: NcMapping, job: dict | None = None, source_name: str | None = None) -> dict:
    from app.core import jobs
    job, scope = ingest(source, mapping, job=job, source_name=source_name)
    folder = jobs.job_folder(job["job_id"])
    out = after_ingest(folder)
    job.update(status="nc_units")
    jobs.save_job(job)
    return {"job": job, "scope": scope, **out}
