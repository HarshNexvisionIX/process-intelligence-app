"""SME review of Level 4/5 activity group proposals."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.nocase.knowledge import library
from app.ui.nocase_review_common import page_context, reviewer_form

ctx = page_context("Review activities")
if ctx:
    process = ctx["process"]
    by, comment = reviewer_form("review_activities")
    # The engine writes proposals to the process library (library/<process>/); older runs wrote them to the job folder.
    from app.nocase import library as _packs
    proposal_path = _packs.pack_dir(process) / "activities.proposed.yaml"
    if not proposal_path.exists():
        proposal_path = ctx["folder"] / "activities.proposed.yaml"
    doc = library._read_yaml(proposal_path, {}) if proposal_path.exists() else {}
    groups_by_step = (doc or {}).get("steps", {})
    rows = [{"ID": f"activity:{step}:{i}", "Step": step, "Activity": group.get("label", ""),
             "Description": group.get("description", ""), "Rules": str(group.get("rules") or [])}
            for step, groups in groups_by_step.items() for i, group in enumerate(groups or [])]
    st.subheader("Proposed activity groups")
    if rows:
        edited = st.data_editor(pd.DataFrame(rows), hide_index=True, width="stretch", key="review_activities_table",
                                disabled=["ID", "Step", "Rules"])
        selected = st.multiselect("Select activity groups", edited["ID"].astype(str).tolist(),
                                  key="review_activities_ids")
        left, right = st.columns(2)
        if left.button("Accept selected groups", key="review_activities_accept"):
            if not by.strip():
                st.error("Enter who reviewed these groups.")
            elif selected:
                data = {"steps": {}}
                for row in edited.to_dict("records"):
                    original_id = str(row["ID"])
                    step = str(row["Step"])
                    index = int(original_id.rsplit(":", 1)[1])
                    group = groups_by_step[step][index]
                    data["steps"].setdefault(step, []).append({**group, "id": original_id,
                        "label": str(row["Activity"]).strip(), "description": str(row["Description"]).strip()})
                library.propose(process, "activities", data, source="sme edit")
                st.success(library.approve(selected, by, comment, process=process))
                st.rerun()
            else:
                st.info("Select at least one group.")
        if right.button("Reject selected groups", key="review_activities_reject"):
            if not by.strip() or not comment.strip():
                st.error("Enter the reviewer and rejection reason.")
            elif selected:
                st.success(library.reject(selected, by, comment, process=process))
                st.rerun()
    else:
        st.info("No activity group proposals are waiting for review.")

    st.subheader("Approved groups")
    confirmed = library.load(process)["approved"].get("activities.yaml") or {}
    # the library writes {"steps": {step: [group, ...]}}; older files had {"groups": [...]}
    groups = ([{**g, "step": g.get("step") or s} for s, gs in (confirmed.get("steps") or {}).items() for g in gs or []]
              + list(confirmed.get("groups") or [])) if isinstance(confirmed, dict) else (confirmed or [])
    if not groups:
        st.caption("None approved yet.")
    else:
        st.dataframe(pd.DataFrame([{"Step": g.get("step"), "Activity": g.get("label") or g.get("name"),
                                    "Status": g.get("status"), "ID": g.get("id")} for g in groups
                                   if isinstance(g, dict)]), hide_index=True, width="stretch")
