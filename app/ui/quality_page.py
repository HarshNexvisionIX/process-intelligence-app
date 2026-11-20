"""Page 2: run the Data Quality and Feasibility Gate on a converted job.

Shows, top to bottom:
    A. Pick the job          (the one just converted is pre-selected)
    B. Feasibility Score     score, band and what the band allows
    C. Six dimensions        one bar each
    D. Data-fix list         what to fix first, ranked
    E. All checks            status, value, explanation and examples
    F. Better reading        other case / activity definitions, ranked by confidence
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from app.ui import shell

from app.core import jobs
from app.core.db import connect
from app.core.ingest import open_source
from app.core.quality import run_quality, save_report
from app.core.reading_finder import apply_reading, find_readings, load_readings, save_readings, warning_text

RECONVERT = "Run Validate and convert again on Import data to apply the merges."


def merge_names(job: dict, folder) -> None:
    """Under the near-identical-names check: tick groups to merge into one spelling, then save
    them for this file layout (applied at the next conversion)."""
    from app.core import activity_renames
    signature = job.get("layout_signature")
    groups = activity_renames.groups(folder / "events.parquet") if (folder / "events.parquet").exists() else []
    if not groups or not signature:
        return
    st.markdown("**Merge near-identical names**")
    renames: dict[str, str] = {}
    for i, group in enumerate(groups):
        st.dataframe(pd.DataFrame([{"Spelling": name, "Events": n} for name, n in group]),
                     hide_index=True, width="stretch")
        c1, c2 = st.columns([1, 2])
        tick = c1.checkbox("Merge into the first spelling", key=f"merge_{job['job_id']}_{i}")
        target = c2.text_input("Target name", value=group[0][0], key=f"merge_target_{job['job_id']}_{i}")
        if tick and target.strip():
            renames.update({name: target.strip() for name, _ in group if name != target.strip()})
    if st.button("Apply merges", disabled=not renames, key=f"merge_apply_{job['job_id']}"):
        try:
            activity_renames.save(signature, renames)
            st.success(f"{len(renames)} name(s) will be merged for this file layout. {RECONVERT}")
        except ValueError as err:
            st.error(str(err))
    saved = activity_renames.load(signature)
    if saved:
        st.caption("Saved merges: " + "; ".join(f"{a} → {b}" for a, b in sorted(saved.items())))

ICONS = {"pass": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}
BAND_TEXT = {
    "High": ("success", "Full report, process map, root-cause analysis and AI summary."),
    "Medium": ("warning", "Full report with caveats; the AI summary uses only well-supported findings."),
    "Low": ("warning", "Statistics and an exploratory map only, clearly labelled. No root causes or AI advice."),
    "Not feasible": ("error", "No report. Fix the data using the list below, then upload again."),
}
DIMENSION_NAMES = {
    "completeness": "Completeness", "timestamps": "Timestamp quality",
    "ordering": "Ordering reliability", "volume": "Volume", "pattern": "Pattern strength",
    "stability": "Stability",
}

shell.header("Data quality", "Checks whether this data can support a reliable process map, before any analysis runs.", "quality")

# --------------------------------------------------------------------------- #
# A. Pick the job
# --------------------------------------------------------------------------- #
# Every job that was converted, including ones analysed or reported later.
ready = [j for j in jobs.list_jobs() if jobs.reached(j, "normalised")]
if not ready:
    st.info("No converted data yet. Start with **Import data**.")
    st.stop()

job_id = shell.pick_job(ready, "quality", "it needs converted data")
if job_id is None:
    st.info("No converted data yet. Start with Import data.")
    shell.next_link(shell.current_job())
    st.stop()
job = jobs.load_job(job_id)
if job.get("mode") == "nocase":   # the clickstream workflow has its own pages
    st.info("The current analysis has no case ID: its pages are in the Clickstream workflow.")
    st.stop()
folder = jobs.job_folder(job_id)
saved = folder / "quality.json"

if st.button("Run quality checks", type="primary"):
    with st.spinner("Running 12 checks on all events..."):
        report = run_quality(connect(), folder / "events.parquet")
        save_report(report, folder)
    job.update(status="quality_checked", feasibility=report.feasibility, band=report.band)
    jobs.save_job(job)

if not saved.exists():
    st.stop()
with open(saved, encoding="utf-8") as f:
    r = json.load(f)

# --------------------------------------------------------------------------- #
# B. Feasibility Score
# --------------------------------------------------------------------------- #
st.divider()
left, right = st.columns([1, 2])
with left:
    st.metric("Feasibility Score", f"{r['feasibility']} / 100")
    st.markdown(f"### {r['band']}")
with right:
    kind, text = BAND_TEXT[r["band"]]
    getattr(st, kind)(f"**What this allows:** {text}")
    if r["capped"]:
        st.caption("The score was capped: a failed check limits it to 39, "
                   "and more than two warnings limit it to 79.")
    s = r["stats"]
    st.caption(f"{s['rows']:,} rows → {s['clean_rows']:,} analysable events in "
               f"{s['cases']:,} cases · checked {r['created_at'][:19]} UTC")
    # Older jobs have no rows_left_out in their summary.
    summary = job.get("summary") or {}
    if summary.get("rows_left_out", 0) > 0:
        st.caption(f"Scored on the rows you kept: {summary['rows_left_out']:,} of "
                   f"{summary['rows_in_file']:,} rows were left out on Import data as out of scope.")

# --------------------------------------------------------------------------- #
# C. Six dimensions
# --------------------------------------------------------------------------- #
st.subheader("Score by dimension")
for name, score in r["dimensions"].items():
    a, b = st.columns([1, 4])
    a.write(f"**{DIMENSION_NAMES.get(name, name)}**")
    b.progress(score / 100, text=f"{score}")

# --------------------------------------------------------------------------- #
# D. Data-fix list
# --------------------------------------------------------------------------- #
st.subheader("What to fix first")
if not r["fixes"]:
    st.success("Nothing to fix. The data is ready for analysis.")
else:
    for i, fx in enumerate(r["fixes"], 1):
        st.markdown(f"{i}. {ICONS[fx['status']]} **{fx['check']}**: {fx['problem']}  \n"
                    f"   → {fx['fix']} *(worth up to {fx['points']} points)*")

# --------------------------------------------------------------------------- #
# E. All checks
# --------------------------------------------------------------------------- #
st.subheader("All checks")
st.dataframe(pd.DataFrame([{
    "": ICONS[c["status"]],
    "Check": c["title"],
    "Result": c["shown"],
    "Score": c["score"],  # blank for info-only checks
} for c in r["checks"]]), hide_index=True, width="stretch")

for c in r["checks"]:
    with st.expander(f"{ICONS[c['status']]} {c['title']}"):
        st.write(f"**Result:** {c['shown']}")
        st.write(f"**Why it matters:** {c['explanation']}")
        if c.get("fix"):
            st.write(f"**What to do:** {c['fix']}")
        for ex in c.get("examples") or []:
            st.write(f"- {ex}")
        if c["key"] == "label_hygiene" and c["status"] != "pass":
            merge_names(job, folder)
# --------------------------------------------------------------------------- #
# F. Is there a better way to read this process?
# --------------------------------------------------------------------------- #
st.subheader("Reading of the data")
with st.expander("Is there a better way to read this process?"):
    st.caption("Clean data can still show no order when the case or the activity is defined the wrong way "
               "(a batch number instead of a request ID, a status instead of a step). This tries other "
               "case and activity definitions of the same rows, on a sample, and ranks them by confidence.")
    if st.button("Search", key="readings_search"):
        with open(folder / "mapping.json", encoding="utf-8") as f:
            job_mapping = json.load(f)
        search_con = connect()
        open_source(search_con, jobs.job_source(job), folder)
        with st.spinner("Trying other readings of the data..."):
            save_readings(find_readings(search_con, "src", job_mapping), folder)
    readings = load_readings(folder)
    if readings:
        st.dataframe(pd.DataFrame([{
            "": "current" if r.current else "",
            "Case definition": r.case_label, "Activity definition": r.activity,
            "Order strength": r.order_strength, "Stability": r.stability,
            "Multi-step %": round(100 * r.multi_step_share, 1), "Coverage %": round(100 * r.coverage, 1),
            "Confidence": r.confidence,
        } for r in readings]), hide_index=True, width="stretch")
        message = warning_text(readings)
        best = readings[0]
        if message:
            st.warning(message)
        elif best.current:
            st.success("The current reading is already the best one found.")
        else:
            st.write(f"Best reading: cases = **{best.case_label}**, activity = **{best.activity}** "
                     f"(confidence {best.confidence:.0f}).")
            if st.button("Use this reading", key="readings_use", type="primary",
                         help="Creates a NEW job with this case and activity definition; this job is kept."):
                with st.spinner("Converting the data with the new reading and checking its quality..."):
                    new_job = apply_reading(job_id, best)
                st.session_state["job"] = new_job
                st.success(f"New job {new_job['job_id']} created (quality {new_job['band']}). This job is kept.")
                st.page_link(shell.st_page("analysis"), label="Open the new analysis in Process analysis", icon=":material/account_tree:")
