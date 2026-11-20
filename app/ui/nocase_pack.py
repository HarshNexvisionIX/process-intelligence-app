"""Process library: the processes the app knows (stages, steps, rules, review history), independent of any analysis.

A process pack lives in <data>/library/<process>/ (pack.yaml, rules.yaml, decisions.jsonl). Packs proposed from
documents wait in pack.proposed.yaml until someone approves them here.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import yaml

from app.nocase import library as packs
from app.nocase.knowledge import library
from app.ui import shell
from app.ui.nocase_review_common import reviewer_form

shell.header("Process library", "The processes the app knows: their stages and steps, the rules that recognise "
             "each step, and who approved what. Every analysis of the same process reuses them.")

options = packs.user_processes()          # only processes you created: the app ships no samples or templates
if not options:
    st.info("The library is empty. A process is created on **Business context** (step 2 of an analysis).")
    st.page_link(shell.st_page("context"), label="Business context", icon=":material/menu_book:")
    st.stop()

cur = (shell.current_job() or {}).get("process")
process = st.selectbox("Process", options, index=options.index(cur) if cur in options else 0, key="lib_process",
                       format_func=packs.display_name)
try:
    folder = packs.pack_dir(process)
except ValueError as exc:
    st.warning(str(exc))
    st.stop()
current = library._read_yaml(folder / "pack.yaml", {})
rules = packs.load_rules(process)
prov = packs.provenance(current)
decisions = library._read_decisions(folder)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Stages", len(current.get("stages") or []))
c2.metric("Steps", sum(len(s.get("steps") or []) for s in current.get("stages") or []))
c3.metric("Step rules", len(rules))
c4.metric("Review decisions", len(decisions))
st.caption(f"Status: **{prov['label']}**" + (f" · source: {current.get('source')}" if current.get("source") else ""))

tab_steps, tab_rules, tab_proposed, tab_history = st.tabs(["Stages and steps", "Step rules", "Waiting for approval",
                                                           "History"])

with tab_steps:
    rows = [{"Stage": stage.get("name", ""), "Stage description": stage.get("description", ""),
             "Step": step.get("name", ""), "Step description": step.get("description", ""),
             "Terminology": ", ".join(step.get("terminology", []) or []),
             "Exceptions": ", ".join(step.get("exceptions", []) or [])}
            for stage in current.get("stages", []) or [] for step in stage.get("steps", []) or []]
    cols = ["Stage", "Stage description", "Step", "Step description", "Terminology", "Exceptions"]
    if not rows:
        st.info("No stages or steps yet. Propose them from the documents on Business context, or type them here "
                "from the SOP: one row per step, in business order.")
    st.caption("Edit in the table (add rows at the bottom), then save with your name. Every save keeps the "
               "previous version.")
    edited = pd.DataFrame(st.data_editor(pd.DataFrame(rows, columns=cols), hide_index=True, width="stretch",
                                         key="pack_edit_table", num_rows="dynamic")).fillna("")
    edited = edited[edited["Step"].astype(str).str.strip() != ""]
    by, comment = reviewer_form("review_pack")
    if st.button("Save changes", key="pack_save", type="primary"):
        if not by.strip():
            st.error("Enter your name in 'Reviewed by' to save.")
        else:
            stages = []
            for stage_name, group in edited.groupby("Stage", sort=False):
                first = group.iloc[0]
                stages.append({"name": stage_name, "description": first["Stage description"], "steps": [
                    {"name": row["Step"], "description": row["Step description"],
                     "terminology": [x.strip() for x in str(row["Terminology"]).split(",") if x.strip()],
                     "exceptions": [x.strip() for x in str(row["Exceptions"]).split(",") if x.strip()]}
                    for _, row in group.iterrows()]})
            value = {**current, "stages": stages}
            library._snapshot(folder, by.strip(), "Before editing approved process pack")
            library._atomic(folder / "pack.yaml", yaml.safe_dump(value, sort_keys=False, allow_unicode=True))
            library._log(folder, {"who": by.strip(), "when": datetime.now(timezone.utc).isoformat(),
                                  "item": "process-pack", "action": "edited", "before": current, "after": value,
                                  "comment": comment})
            st.success("Saved. The previous version is kept in the history.")
            st.rerun()

with tab_rules:
    if not rules:
        st.info("No step rules yet. They are proposed by an analysis and approved on Review steps.")
    else:
        st.dataframe(pd.DataFrame([{"Rule": r.get("id"), "Step": r.get("step"),
                                    "Application": (r.get("match") or {}).get("app"),
                                    "Screen": (r.get("match") or {}).get("screen"),
                                    "Action": (r.get("match") or {}).get("action"), "Status": r.get("status"),
                                    "Version": r.get("version")} for r in rules]),
                     hide_index=True, width="stretch")

with tab_proposed:
    proposed_path = folder / "pack.proposed.yaml"
    if not proposed_path.exists():
        st.info("Nothing is waiting. Packs proposed from documents appear here for approval.")
    else:
        proposed = library._read_yaml(proposed_path, {})
        st.dataframe(pd.DataFrame([{"Stage": s.get("name", ""), "Description": s.get("description", ""),
                                    "Steps": " · ".join(x.get("name", "") for x in s.get("steps", [])),
                                    "Exceptions": " · ".join(s.get("exceptions", []) or [])}
                                   for s in proposed.get("stages", [])]), hide_index=True, width="stretch")
        by2, comment2 = reviewer_form("approve_pack")
        if st.button("Approve this pack", key="pack_approve", type="primary"):
            if not by2.strip():
                st.error("Enter your name in 'Reviewed by' to approve.")
            else:
                proposed["name"] = current.get("name") or proposed.get("name") or process   # keep your process name
                proposed["provenance"] = {"source": "SME review", "status": "sme_confirmed", "confirmed": True,
                                          "by": by2.strip()}
                library._snapshot(folder, by2.strip(), "Before approving proposed process pack")
                library._atomic(folder / "pack.yaml", yaml.safe_dump(proposed, sort_keys=False, allow_unicode=True))
                library._log(folder, {"who": by2.strip(), "when": datetime.now(timezone.utc).isoformat(),
                                      "item": "process-pack", "action": "approved", "before": current,
                                      "after": proposed, "comment": comment2})
                proposed_path.unlink()
                st.success("Approved and versioned.")
                st.rerun()

with tab_history:
    if not decisions:
        st.info("No decisions recorded yet.")
    else:
        st.dataframe(pd.DataFrame([{"When": str(d.get("when", ""))[:19].replace("T", " "), "Who": d.get("who"),
                                    "Item": d.get("item"), "Action": d.get("action"), "Note": d.get("comment")}
                                   for d in reversed(decisions)]), hide_index=True, width="stretch")
