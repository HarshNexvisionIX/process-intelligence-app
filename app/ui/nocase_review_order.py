"""Review and confirm the ordered steps and explicit order exceptions."""
from __future__ import annotations

import pandas as pd
import streamlit as st
import yaml
from datetime import datetime, timezone

from app.nocase.knowledge import library
from app.ui.nocase_review_common import page_context, reviewer_form

ctx = page_context("Review step order")
if ctx:
    process, pack = ctx["process"], ctx["pack"] or {}
    by, comment = reviewer_form("review_order")
    st.caption("Pack order is documented knowledge until an SME confirms it. Recorded event order does not invent cases.")
    rows = [{"Position": i + 1, "Stage": stage.get("name", ""), "Step": step.get("name", ""),
             "Exception": step.get("exception", "")}
            for i, stage in enumerate(pack.get("stages") or []) for step in stage.get("steps") or []]
    if rows:
        edited = st.data_editor(pd.DataFrame(rows), hide_index=True, width="stretch", key="review_order_table")
        if st.button("Save SME-confirmed order", key="review_order_save"):
            if not by.strip():
                st.error("Enter who confirmed the order.")
            else:
                edited = edited.sort_values("Position", kind="stable")
                stages = []
                for stage_name, group in edited.groupby("Stage", sort=False):
                    stages.append({"name": stage_name, "description": "", "steps": [
                        {"name": row["Step"], "description": "", **({"exception": row["Exception"]}
                         if row.get("Exception") else {})} for _, row in group.iterrows()]})
                value = {**pack, "stages": stages, "provenance": {"source": "SME review", "status": "sme_confirmed",
                         "confirmed": True, "by": by.strip()}}
                from app.nocase.knowledge.library import _atomic
                folder = library.process_dir(process)
                library._snapshot(folder, by.strip(), "Before changing business order")
                before = pack
                _atomic(folder / "pack.yaml", yaml.safe_dump(value, sort_keys=False, allow_unicode=True))
                library._log(folder, {"who": by.strip(), "when": datetime.now(timezone.utc).isoformat(),
                    "item": "process-order", "action": "approved",
                    "before": before, "after": value, "comment": comment})
                st.success("Business order saved as SME-confirmed.")
                st.rerun()
    else:
        st.info("This process pack has no ordered steps yet.")

    st.subheader("Order edges and exceptions")
    approved = library.load(process)["approved"].get("order.yaml") or {}
    edges = approved.get("edges", []) if isinstance(approved, dict) else []
    if edges:
        st.dataframe(pd.DataFrame(edges), hide_index=True, width="stretch")
    else:
        st.info("No separately recorded order edges yet. The pack's step order remains the displayed order.")
