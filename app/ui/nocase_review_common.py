"""Shared, privacy-aware job and process selection for no-case SME review pages."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from app.core import jobs
from app.nocase import library as packs
from app.nocase.knowledge import library


PURPOSE = {
    "Review steps": ("nc_steps", "Confirm or reject the rules that give clicks a business step, and check how each "
                     "step is done."),
    "Review activities": ("nc_activities", "Rename, merge or confirm the activities (Level 4) inside each step."),
    "Review step order": ("nc_order", "Confirm the order of the steps. Confirmed orders are reused for the next file."),
    "Open questions": ("nc_questions", "Questions the analysis could not decide on its own. Answers are saved to "
                       "the process library."),
}


def page_context(title: str):
    from app.ui import shell
    here, purpose = PURPOSE.get(title, (None, ""))
    shell.header(title, purpose, here)
    candidates = [j for j in jobs.list_jobs() if j.get("mode") == "nocase" and
                  (jobs.job_folder(j["job_id"]) / "nc_measures.json").exists()]
    if not candidates:
        st.info("Nothing to review yet: import a file without a case ID and run the analysis first.")
        return None
    job_id = shell.pick_job(candidates, title, "run the clickstream analysis first")
    if job_id is None:
        return None
    job = jobs.load_job(job_id)

    process = job.get("process")
    if not process or process not in packs.user_processes():
        st.warning("Choose the process of this analysis on **Business context** first: the review uses its steps, "
                   "rules and answers.")
        st.page_link(shell.st_page("context"), label="Go to Business context", icon=":material/arrow_forward:")
        return None
    st.caption(f"Process: **{packs.display_name(process)}** (change it on Business context)")
    try:
        folder = jobs.job_folder(job_id)
        pack = packs.load_pack(process) if process else None
    except (ValueError, OSError) as exc:
        st.warning(str(exc))
        return None
    return {"job": job, "folder": folder, "process": process, "pack": pack,
            "knowledge": library.load(process) if process else None}


def reviewer_form(prefix: str) -> tuple[str, str]:
    col1, col2 = st.columns([1, 2])
    with col1:
        by = st.text_input("Reviewed by", key=f"{prefix}_by")
    with col2:
        comment = st.text_input("Review note", key=f"{prefix}_comment")
    return by, comment



def all_questions(folder: Path, process: str | None) -> list[dict]:
    """Every question for this analysis with its saved answer (rules in app/nocase/questions.py)."""
    from app.nocase.questions import all_questions as _all
    return _all(folder, process)
