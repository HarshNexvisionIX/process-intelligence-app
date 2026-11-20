"""Page 1: the automatic mode decision (app/intake/mode_detect.py) as one banner, a "Why?" table and a one-click
override. It pre-fills page 1 once per job (the Case ID radio, the case column or the no-case form) BEFORE those
widgets are drawn; everything after that works exactly as without it. Off with settings intake.auto_detect.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core import jobs
from app.core.config import settings

NOCASE_MODE = "No case ID (clickstream mapping)"
LABELS = {"case": "case ID mode", "case_ready": "a case ID inside text (case-ready)", "confirm": "maybe a case ID",
          "partial": "no case ID (partly linkable)", "no_case": "no case ID"}


def _key(job: dict) -> str:
    return f"mode_detect_{job['job_id']}"


def _record(job: dict, source: str, mode: str) -> None:
    job.update(mode_source=source, detected_mode=mode)
    jobs.save_job(job)


def _prefill(d: dict, con, prof=None) -> None:
    p = d.get("prefill") or {}
    if d["decision"] in ("case", ) and p.get("case_mode") == "Use a column":
        st.session_state["case_mode"] = "Use a column"
        st.session_state["map_case_id"] = p["column"]
    elif d["decision"] == "case" and p.get("case_mode") == "Derive it":
        from app.core.derived import find_id_patterns
        st.session_state["case_mode"] = "Derive it"
        st.session_state["case_method"] = "text_pattern"
        found = find_id_patterns(con, [p["column"]], (d.get("roles") or {}).get("activity"))
        st.session_state["id_patterns"] = found
        idx = next((i for i, x in enumerate(found) if x["pattern"] == p["pattern"] and x["column"] == p["column"]), None)
        if idx is not None:
            st.session_state["id_pick"] = idx
    else:
        st.session_state["case_mode"] = NOCASE_MODE
        from app.ui.nocase_mapping import suggestions     # names first, then the import checks' roles
        for std, col in (suggestions(con, prof) if prof is not None else {}).items():
            st.session_state[f"nc_col_{std}"] = col


def _switch(job: dict) -> None:
    """"Use the other mode": case <-> no case; recorded as user_override."""
    now = st.session_state.get("case_mode")
    other = "Use a column" if now == NOCASE_MODE else NOCASE_MODE
    st.session_state["case_mode"] = other
    _record(job, "user_override", "no_case" if other == NOCASE_MODE else "case")


def _use_column(job: dict, column: str) -> None:
    st.session_state["case_mode"] = "Use a column"
    st.session_state["map_case_id"] = column
    _record(job, "user_override", "case")


def banner(job: dict, con, prof) -> None:
    """Detect once per job, pre-fill, and show the decision with its reasons and the override buttons."""
    if not (settings().get("intake") or {}).get("auto_detect", False):
        return
    from app.intake.mode_detect import detect_and_save
    key = _key(job)
    if key not in st.session_state:
        with st.spinner("Checking whether this file has a case ID..."):
            d = detect_and_save(con, jobs.job_folder(job["job_id"]), prof)
        st.session_state[key] = d
        _record(job, "auto", d["decision"])
        _prefill(d, con, prof)
    d = st.session_state[key]
    reasons = [r.rstrip(". ") + "." for r in d["reasons"][:3]]
    text = f"Detected: **{LABELS[d['decision']]}**. " + " ".join(reasons)
    if d["decision"] == "case":
        st.success(text)
    elif d["decision"] == "confirm":
        col = d.get("column") or (d.get("text_ids") or {}).get("column")
        st.warning(f"This file may have a case ID ({col}). " + " ".join(d["reasons"]) + " Use it?")
        if d.get("column"):
            st.button(f"Use {d['column']} as the case", key="intake_use_case", on_click=_use_column,
                      args=(job, d["column"]))
    else:
        st.info(text)
    if d.get("warning"):
        st.warning(d["warning"])
    with st.expander("Why?"):
        st.caption(f"Measured on {d['scope_base']} ({d['scope_rows']:,} of {d['rows']:,} rows), in "
                   f"{d['seconds']:.1f} s. Column names and statistics only; no value is stored.")
        st.dataframe(pd.DataFrame([{
            "Column": c["column"], "Passes": "yes" if c["passed"] else "no", "Coverage %": round(100 * c["coverage"], 1),
            "Distinct": c["distinct"], "Median events": c["median_events"], "Name hint": "yes" if c["name_hint"] else "",
            "Why not": c["fail_reason"] or ""} for c in d["candidates"]]), hide_index=True, width="stretch")
        t = d.get("text_ids")
        if t:
            st.caption(f"IDs in text: shape {t['shape']} in {t['column']} covers {100 * t['coverage']:.1f}% "
                       f"({100 * t['conflict_share']:.1f}% conflicts).")
    st.button("Use the other mode", key="intake_switch", on_click=_switch, args=(job,))
