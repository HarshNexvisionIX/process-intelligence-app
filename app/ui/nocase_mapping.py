"""Page 1, "No case ID (clickstream mapping)": map the standard columns and convert (app/nocase/).

Shown by upload_page.py when the Case ID choice is "No case ID". The case-ID form below it is not shown.
"""
from __future__ import annotations

import re

import streamlit as st

from app.core import jobs
from app.nocase.canonical import (COLUMN_HELP, DATASET_CLASSES, STANDARD_COLUMNS, TIME_FORMATS, NcMapping,
                                  load_mapping)

CLASS_HELP = {"client": "Client data: AI calls only go to providers marked private (config/ai_providers.yaml).",
              "public": "Public data: any AI provider allowed in the AI settings may be used."}
TIMEZONES = ["UTC", "Europe/London", "Europe/Berlin", "Asia/Kolkata", "Africa/Johannesburg", "America/New_York",
             "America/Chicago", "America/Los_Angeles", "Asia/Singapore", "Asia/Dubai", "Australia/Sydney"]


def _sync_class() -> None:
    """Keep the page's dataset class (top of page 1) the same as the one chosen here."""
    value = st.session_state.get("nc_dataset_class")
    if value in DATASET_CLASSES:
        st.session_state["dataset_class_choice"] = value


def _values(con, column: str) -> list[str]:
    from app.core.db import quote_ident
    q = quote_ident(column)
    return [str(v) for (v,) in con.execute(
        f"SELECT CAST({q} AS VARCHAR) v FROM src WHERE {q} IS NOT NULL GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 50"
    ).fetchall()]


# words in a column name that point to a standard column (lower case; a whole word or part of the name)
_HINTS = {"ts_end": ("end", "end_time", "ended", "finish", "stop"),
          "action": ("action", "click", "activity_alias", "ui_action", "event_type"),
          "screen": ("screen", "simplified_title", "page", "form"),
          "active_s": ("active_time", "active", "duration", "duration_s", "duration_sec", "seconds")}


_NAMED = {"ts": ("timestamp", "date_time", "datetime", "time", "start", "start_time", "begin", "ts"),
          "actor": ("user", "user_id", "user_key", "username", "participant_id", "employee", "agent", "actor",
                    "resource"),
          "app": ("application", "application_name", "app_name", "app", "program"),
          "text": ("window", "window_title", "title", "caption", "page_title")}


def suggestions(con, prof) -> dict[str, str]:
    """A first guess for a new layout: time, user, application and window title from the import checks, the
    rest from column names. Never a case ID, and every column at most once."""
    from app.intake.mode_detect import roles
    try:
        r = roles(con, prof)
    except Exception:  # noqa: BLE001 - a guess only
        r = {}
    out: dict[str, str] = {}
    by_name = {c.name.lower(): c.name for c in prof.columns}
    for std, words in _NAMED.items():           # a clear column name wins over a statistical guess
        hit = next((by_name[w] for w in words if w in by_name), None)
        if hit and hit not in out.values():
            out[std] = hit
    for std, key in (("ts", "ts"), ("actor", "actor"), ("app", "app")):
        if std not in out and r.get(key) and r[key] not in out.values():
            out[std] = r[key]
    if "text" not in out and r.get("text") and r["text"][0] not in out.values():
        out["text"] = r["text"][0]
    used = set(out.values())
    numeric = {c.name for c in prof.columns if c.dtype.startswith(("DOUBLE", "FLOAT", "DECIMAL", "INT", "BIGINT"))}
    for std, words in _HINTS.items():
        for c in prof.columns:
            n = c.name.lower()
            if c.name in used or (std == "active_s" and c.name not in numeric):
                continue
            if n in words or any(len(w) >= 5 and w in n for w in words) or n.split("_")[0] in words:
                if std == "ts_end" and not c.ts_format:
                    continue
                out[std] = c.name
                used.add(c.name)
                break
    return out


def form(job: dict, con, prof) -> NcMapping:
    """The mapping form; returns the mapping as chosen (not yet checked)."""
    names = [c.name for c in prof.columns]
    saved = load_mapping(names)
    guess: dict[str, str] = {}
    if saved:
        st.success("This file layout was mapped for clickstream mapping before; the saved mapping is pre-filled.")
    else:
        guess = suggestions(con, prof)
        if guess:
            st.info("Suggested from the column names and contents. Check each one before you convert.",
                    icon=":material/auto_awesome:")
    st.markdown("**Standard columns** (only *time* is required; map what the export has)")
    cols: dict[str, str | None] = {}
    grid = st.columns(4)
    for i, std in enumerate(STANDARD_COLUMNS):
        default = (saved.columns.get(std) if saved else guess.get(std))
        options = [None] + names
        with grid[i % 4]:
            cols[std] = st.selectbox(std, options, index=options.index(default) if default in options else 0,
                                     format_func=lambda v: "— none —" if v is None else v, key=f"nc_col_{std}",
                                     help=COLUMN_HELP[std])
    c1, c2 = st.columns(2)
    fmt_options = ["detect"] + list(TIME_FORMATS)
    fmt = c1.selectbox("Time format", fmt_options, key="nc_ts_format",
                       index=fmt_options.index(saved.ts_format) if saved and saved.ts_format in fmt_options else 0,
                       help="detect, or a strptime pattern typed in the mapping YAML (command line)")
    tz = c2.selectbox("Timezone of times without an offset", TIMEZONES, key="nc_tz",
                      index=TIMEZONES.index(saved.timezone) if saved and saved.timezone in TIMEZONES else 0)

    st.markdown("**Rows in scope**")
    k1, k2 = st.columns(2)
    keep: dict[str, list] = {}
    drop: dict[str, list] = {}
    with k1:
        kc = st.selectbox("Keep only rows where", [None] + names, key="nc_keep_col",
                          format_func=lambda v: "— all rows —" if v is None else v)
        if kc:
            keep[kc] = st.multiselect("... has one of these values", _values(con, kc), key="nc_keep_vals")
    with k2:
        dc = st.selectbox("Leave out rows where", [None] + names, key="nc_drop_col",
                          format_func=lambda v: "— none —" if v is None else v)
        if dc:
            drop[dc] = st.multiselect("... has one of these values", _values(con, dc), key="nc_drop_vals")

    st.markdown("**Eval columns** (kept ONLY to measure accuracy, e.g. a true case or step; never used to build "
                "anything)")
    ev_cols = st.multiselect("Eval columns", names, key="nc_eval",
                             default=[c for c in (saved.eval.values() if saved else []) if c in names])
    accuracy_cols = {re.sub(r"[^A-Za-z0-9_]", "_", c): c for c in ev_cols}

    st.markdown("**Dataset class** (required)")
    current = job.get("dataset_class") if job.get("dataset_class") in DATASET_CLASSES else None
    opts = list(DATASET_CLASSES)
    dclass = st.radio("Dataset class", opts, index=opts.index(current) if current in opts else None,
                      key="nc_dataset_class", horizontal=True,
                      format_func=lambda v: f"{v} — {CLASS_HELP[v]}", on_change=_sync_class,
                      help="Chosen when the file was loaded; change it here if needed. Client data is never sent to "
                           "a provider that is not marked private.")
    return NcMapping(columns={k: v for k, v in cols.items() if v}, dataset_class=dclass,
                     ts_format=None if fmt == "detect" else fmt, timezone=tz, keep_rows_where=keep,
                     drop_rows_where=drop, eval=accuracy_cols)


def render(job: dict, con, prof) -> None:
    """The whole no-case section of page 1 (form, problems, convert button, result)."""
    st.info("**No case ID**: events are mapped to standard columns and analysed as clickstream (episodes, "
            "switching, screens and clicks). No case is invented: a case only comes from a real ID that passes "
            "the case-link tests.")
    mapping = form(job, con, prof)
    problems = mapping.problems([c.name for c in prof.columns])
    for p in problems:
        st.error(p)
    if st.button("Convert for clickstream mapping", type="primary", disabled=bool(problems), key="nc_convert"):
        from app.nocase.pipeline import after_ingest
        from app.nocase.ingest import ingest
        with st.spinner("Converting, profiling, cleaning and building units..."):
            try:
                job, scope = ingest(jobs.job_source(job), mapping, job=job)
                after_ingest(jobs.job_folder(job["job_id"]))
            except ValueError as err:
                st.error(str(err))
                return
        job["status"] = "nc_units"
        jobs.save_job(job)
        st.session_state["job"] = job
        st.session_state["nc_scope"] = scope
        st.rerun()                        # the menu switches to the clickstream workflow
    scope = st.session_state.get("nc_scope")
    if scope and st.session_state.get("job", {}).get("mode") == "nocase":
        st.success(f"Converted: {scope['in_scope']:,} of {scope['rows_in_file']:,} rows in scope "
                   f"({scope['left_out_by_filter']:,} left out by the filter, {scope['unreadable_time']:,} with an "
                   "unreadable time).")
        from app.ui import shell
        shell.next_link(st.session_state["job"], after="import")
