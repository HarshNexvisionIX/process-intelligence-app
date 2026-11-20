"""Clickstream report (no case ID): generate the self-checked report and download it as HTML or Excel."""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from app.ui import shell

from app.core import jobs

shell.header("Report", "The self-checked report for this analysis: every number is recomputed before it "
             "is shown, and every statement carries its source.", "nc_report")
nc_jobs = [j for j in jobs.list_jobs() if j.get("mode") == "nocase"]
if not nc_jobs:
    st.info("No analysis without a case ID yet: import a file first.")
    st.stop()
job_id = shell.pick_job(nc_jobs, "nc_report", "run the clickstream analysis first")
if job_id is None:
    st.stop()
job = jobs.load_job(job_id)
folder = jobs.job_folder(job_id)
from app.nocase.engine.measures import MEASURES_FILE  # noqa: E402
from app.nocase.report.report import HTML_FILE, XLSX_FILE  # noqa: E402

if not (folder / MEASURES_FILE).exists():
    st.info("Run the analysis first (step 2, Overview and analysis).")
    shell.next_link(shell.current_job())
    st.stop()
if shell.require_context(job, "the report") and st.button("Generate report", type="primary", key="ncrep_go"):
    from app.nocase.ai_gateway import gateway
    from app.nocase.report.report import build
    with st.spinner("Writing the report and checking every number..."):
        r = build(folder, job.get("process"), gateway(job.get("dataset_class"), "online"))
    (st.error if r["checks"]["failed"] else st.success)(f"{r['title']} — {r['checks']['passed']} checks passed, "
                                                      f"{r['checks']['failed']} failed. " + ("Summary written from the checked facts (no AI)."
                                                      if r['summary_source'] == "templates" else
                                                      f"Summary worded by the AI ({r['summary_source'][3:]}) from the "
                                                      "checked facts."))
for name, mime, label in ((HTML_FILE, "text/html", "Download HTML"),
                          (XLSX_FILE, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Download Excel")):
    p: Path = folder / name
    if p.exists():
        st.download_button(label, p.read_bytes(), file_name=name, mime=mime, key=f"ncrep_{name}")
