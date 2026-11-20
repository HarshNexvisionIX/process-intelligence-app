"""The catalogue editor keeps excluded rows when a step filter is active."""
import io
import json

import pandas as pd
import streamlit as st

from app.core import jobs, milestones, reference
from app.core.db import connect
from app.core.mining import run_mining, save_mining


def render(job):
    with st.expander("Milestones (from actions)"):
        rows = milestones.milestone_catalogue(job)
        st.caption("Only frequent completion actions used by several people are listed. Labels are used in reports and AI summaries.")
        step = st.selectbox("Filter milestones by step", ["All steps"] + sorted({r["step"] for r in rows}))
        visible = [r for r in rows if step == "All steps" or r["step"] == step]
        frame = pd.DataFrame(visible, columns=["step", "action", "events", "cases", "users", "is_milestone", "label"])
        edited = st.data_editor(frame, hide_index=True, disabled=["step", "action", "events", "cases", "users"],
            key=f"milestones_editor_{job['job_id']}_{step}", column_config={
                "step": "Step", "action": "Action", "events": "Events", "cases": "Cases", "users": "Users",
                "is_milestone": st.column_config.CheckboxColumn("Milestone?"), "label": st.column_config.TextColumn("Label")})
        confirmed = st.checkbox("Milestone labels confirmed by the business",
                                value=milestones.load_catalogue(reference.order_key(job))["confirmed_by_business"])
        uploaded = st.file_uploader("Upload milestone catalogue CSV", type=["csv"])
        st.download_button("Download milestone catalogue CSV", pd.DataFrame(rows).to_csv(index=False), "milestones.csv", "text/csv")
        st.download_button("Download milestone catalogue JSON", json.dumps({"rows": rows, "confirmed_by_business": confirmed}),
                           "milestones.json", "application/json")
        if st.button("Save milestones"):
            try:
                changed = pd.read_csv(io.BytesIO(uploaded.getvalue())).to_dict("records") if uploaded else edited.to_dict("records")
                allowed = {(r["step"], r["action"]) for r in rows}
                if any((r["step"], r["action"]) not in allowed for r in changed):
                    raise ValueError("Uploaded catalogue contains actions not in this job.")
                updates = {(r["step"], r["action"]): r for r in changed}
                combined = [{**r, **updates.get((r["step"], r["action"]), {})} for r in rows]
                milestones.save_catalogue(reference.order_key(job), combined, confirmed)
                folder = jobs.job_folder(job["job_id"])
                with connect() as con:
                    result = run_mining(con, folder / "events.parquet", job["band"])
                save_mining(result, folder)
                job.update(mined_at=result["created_at"], flow_band=result["flow_confidence"]["data"]["band"])
                jobs.save_job(job)
                st.cache_data.clear()
                st.rerun()
            except (ValueError, KeyError) as exc:
                st.error(str(exc))
