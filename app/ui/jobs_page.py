"""Analyses: every imported file, its mode and progress; open one (it becomes the current analysis) or move it
to the backup."""
import pandas as pd
import streamlit as st

from app.core.jobs import delete_job, job_files, list_jobs
from app.ui import shell

shell.header("Analyses", "Every file you imported, with its progress. Open one to continue where you left off.")
if st.session_state.get("jobs_flash"):
    st.success(st.session_state.pop("jobs_flash"))
found = list_jobs()
if not found:
    st.info("No analyses yet. Start with **Import data**.")
    st.page_link(shell.st_page("import"), label="Import data", icon=":material/upload_file:")
    st.stop()

cur = (shell.current_job() or {}).get("job_id")
rows = []
for j in found:
    mode = shell.mode_of(j)
    done = shell.step_status(j)
    flow = shell.WORKFLOWS[mode]
    nxt = shell.next_step(j)
    s = j.get("summary") or {}
    rows.append({
        "Current": "●" if j["job_id"] == cur else "",
        "File": j.get("source_name") or "upload",
        "Type": "Case ID" if mode == "case" else "Clickstream",
        "Data": "Client" if j.get("dataset_class", "client") == "client" else "Public",
        "Events": s.get("events") or s.get("rows_in_scope") or j.get("rows"),
        "Cases": f"{s.get('cases'):,}" if mode == "case" and s.get("cases") else "–",
        "Progress": f"{sum(bool(done.get(k)) for _, k in flow)} of {len(flow)} steps",
        "Next step": nxt[0] if nxt else "Done",
        "Imported": (j.get("created_at") or "")[:16].replace("T", " "),
        "ID": j["job_id"],
    })
st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
             column_config={"Events": st.column_config.NumberColumn(format="localized")})

labels = {j["job_id"]: f"{j.get('source_name') or 'upload'} · {(j.get('created_at') or j['job_id'])[:16].replace('T', ' ')}"
          for j in found}
c1, c2, c3 = st.columns([3, 1, 1], vertical_alignment="bottom")
pick = c1.selectbox("Analysis", list(labels), format_func=labels.get, key="job_delete_pick",
                    index=list(labels).index(cur) if cur in labels else 0)
if c2.button("Open", type="primary", key="jobs_open", width="stretch"):
    shell.set_current(pick)
    nxt = shell.next_step(st.session_state["job"])
    # the menu of the opened analysis' mode is built on the next run (app/main.py follows "goto")
    st.session_state["goto"] = nxt[1] if nxt else ("report" if shell.mode_of(st.session_state["job"]) == "case"
                                                   else "nc_report")
    st.rerun()
if c3.button("Move to backup…", key="job_delete", width="stretch"):
    st.session_state["job_delete_pending"] = pick


@st.dialog("Move this analysis to the backup?")
def confirm_delete(job_id: str) -> None:
    files = job_files(job_id)
    st.write(f"**{labels.get(job_id, job_id)}** and its {len(files)} file(s) move to "
             f"`data/_backup/<time>/jobs/{job_id}`. Nothing is deleted; move the folder back to restore it.")
    st.code("\n".join(files[:40]) + (f"\n… and {len(files) - 40} more" if len(files) > 40 else ""), language=None)
    st.caption("Shared settings stay: column mappings, application names, step orders, step standards, milestone "
               "catalogues, process definitions and the process library.")
    b1, b2 = st.columns(2)
    if b1.button("Move to backup", type="primary", key="job_delete_confirm"):
        st.session_state.pop("job_delete_pending", None)
        target = delete_job(job_id)
        if (st.session_state.get("job") or {}).get("job_id") == job_id:
            st.session_state.pop("job", None)
        st.session_state["jobs_flash"] = f"Analysis {job_id} moved to {target}."
        st.rerun()
    if b2.button("Cancel", key="job_delete_cancel"):
        st.session_state.pop("job_delete_pending", None)
        st.rerun()


pending = st.session_state.get("job_delete_pending")
if pending and pending in labels:
    confirm_delete(pending)
