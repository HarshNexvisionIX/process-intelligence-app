"""Page 4: generate, preview and download the report.

Shows, top to bottom:
    A. Pick the job          (analysed on page 3)
    B. Trust banner          same rules as page 3
    C. Generate              facts, report.html and report.xlsx in the job folder
    D. Preview and download  HTML, Excel; PDF via the browser's Print -> Save as PDF
"""
from __future__ import annotations

import streamlit as st

from app.ui import shell
import streamlit.components.v1 as components

from app.core import jobs
from app.core.config import settings
from app.core.narrative import load_narrative, record_feedback
from app.core.report import generate_report

CFG = settings()["mining"]

shell.header("Report", "A report built only from checked facts: every number shows how far it can be trusted.", "report")

# --------------------------------------------------------------------------- #
# A. Pick the job
# --------------------------------------------------------------------------- #
ready = [j for j in jobs.list_jobs() if jobs.reached(j, "mined")]
if not ready:
    st.info("Run the process analysis first (step 3).")
    st.stop()

job_id = shell.pick_job(ready, "report", "run the process analysis first")
if job_id is None:
    st.info("Run the process analysis first.")
    shell.next_link(shell.current_job())
    st.stop()
job = jobs.load_job(job_id)
if job.get("mode") == "nocase":   # the clickstream workflow has its own pages
    st.info("The current analysis has no case ID: its pages are in the Clickstream workflow.")
    st.stop()
folder = jobs.job_folder(job_id)
band = job.get("band")

# --------------------------------------------------------------------------- #
# B. Trust banner (the report itself still documents why, for every band)
# --------------------------------------------------------------------------- #
if band == "Not feasible":
    st.error("This data did not pass the quality gate. The report shows only the data confidence "
             "section and what to fix.")
elif band in CFG["band_rules"]:
    label = CFG["band_rules"][band]["label"]
    if band == "Low":
        st.warning(label)
    elif band == "Medium":
        st.info(label)
    else:
        st.caption(f":green-badge[:material/check: Data quality: {band}]")

# The analysis must be newer than the last quality check (page 3 re-runs it otherwise).
if not jobs.is_newer(job_id, "mining.json", "quality.json"):
    st.warning("The quality check ran again after the analysis. Run the process analysis again first.")
    st.stop()

# --------------------------------------------------------------------------- #
# C. Generate
# --------------------------------------------------------------------------- #
context_ok = shell.require_context(job, "the report")    # mandatory step 2
if context_ok and st.button("Generate report", type="primary"):
    with st.spinner("Building facts and writing the report..."):
        generate_report(job_id)

html_path, xlsx_path = folder / "report.html", folder / "report.xlsx"
if not html_path.exists():
    if context_ok:
        st.info("Click **Generate report** to create it.")
    st.stop()
if not jobs.is_newer(job_id, "report.html", "mining.json"):
    st.warning("The analysis ran again after this report was made. Click **Generate report** to update it.")
    st.stop()

# --------------------------------------------------------------------------- #
# D. Preview and download
# --------------------------------------------------------------------------- #
html = html_path.read_text(encoding="utf-8")
a, b = st.columns(2)
a.download_button("Download HTML", html, file_name=f"report_{job_id}.html", mime="text/html")
if xlsx_path.exists():
    b.download_button("Download Excel", xlsx_path.read_bytes(), file_name=f"report_{job_id}.xlsx",
                      mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
st.caption("For PDF: open the HTML and use Print → Save as PDF.")

# Feedback on the AI-written summary: kept in narrative.json and sent to Langfuse when it is on.
story = load_narrative(folder)
if story is not None and story.source == "ai":
    st.write("Was the AI-written summary useful?")
    up, down, _ = st.columns([1, 1, 8])
    if up.button("👍", key="feedback_up"):
        record_feedback(job_id, 1)
        st.success("Thanks — feedback recorded.")
    if down.button("👎", key="feedback_down"):
        record_feedback(job_id, 0)
        st.success("Thanks — feedback recorded.")

components.html(html, height=900, scrolling=True)
