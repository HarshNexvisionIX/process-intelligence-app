"""Page 1: upload a file, check the suggested column mapping, convert.

Flow (each block below is one step on screen):
    A. Choose a file  -> creates a job and points DuckDB at the file
    B. Profile        -> rows, columns, missing values, date detection
    C. Map columns    -> suggestions with confidence; the user confirms
                         (with an Application column: name masked applications)
    D. Validate and convert -> checks on the full data, then events.parquet
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from app.ui import shell

from app.core import apps, jobs
from app.core.config import settings, data_dir
from app.core.db import connect, quote_ident
from app.core.ingest import (
    SOURCE_VIEW, SUPPORTED, dataset_overview, normalise, open_source, profile, resolve_sources,
)
from app.core.mapping import (
    ALL_ROLES, REQUIRED_ROLES, ROLE_LABELS, ambiguous_day_month, load_profile,
    Suggestion, save_profile, sensitive_columns, signature, step_run_fits, suggest_mapping, validate_mapping,
)
from app.core.capabilities import capabilities, missing_lines
from app.core.derived import (
    ACTIVITY_COL as DERIVED_ACTIVITY_COL, CASE_COL as DERIVED_CASE_COL, SCREEN_NOTE, TIME_WINDOW_NOTE,
    WORK_SESSION_NOTE, apply_derivations, case_label, find_id_patterns,
)

TIMEZONES = ["UTC", "Asia/Kolkata", "Europe/London", "Europe/Berlin", "Europe/Paris",
             "America/New_York", "America/Chicago", "America/Los_Angeles",
             "Asia/Singapore", "Asia/Dubai", "Australia/Sydney"]

shell.header("Import data", "Load an event log, check what is in it, tell the app how cases are identified and "
             "which columns mean what, then convert. Supported: " + ", ".join(sorted(SUPPORTED)) + ".", "import")
st.markdown("##### 1 · Choose a file")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def start_job(source_name: str, path=None, uploads=None) -> None:
    """Create a job, open the file(s) in DuckDB and profile them.

    `path` is a file, folder or glob pattern on this computer; `uploads` is a
    list of browser uploads (one file is saved as today, several into raw/).
    """
    job = jobs.create_job(source_name, path, st.session_state.get("dataset_class_choice", "client"))
    if uploads:
        if len(uploads) == 1:
            jobs.save_browser_upload(job, uploads[0].name, uploads[0])
        else:
            jobs.save_browser_uploads(job, uploads)
    con = connect()
    try:
        with st.spinner("Reading and profiling the data..."):
            fmt = open_source(con, jobs.job_source(job), jobs.job_folder(job["job_id"]))
            prof = profile(con)
            overview = dataset_overview(con)
    except ValueError as err:  # nothing found, mixed formats, column types that clash
        st.error(str(err))
        return
    job.update({"status": "profiled", "format": fmt, "rows": prof.rows})
    jobs.save_job(job)
    st.session_state.update(job=job, con=con, profile=prof, overview=overview, result=None,
                            upload_job_id=job["job_id"])


def get_con():
    """Reuse the DuckDB connection for this job (recreate it after a restart)."""
    job = st.session_state["job"]
    if st.session_state.get("con") is None:
        con = connect()
        open_source(con, jobs.job_source(job), jobs.job_folder(job["job_id"]))
        st.session_state["con"] = con
    return st.session_state["con"]


def source_label(items: list[str]) -> str:
    """Short name for a dataset of several files, e.g. "3 files: a.csv, b.csv, …"."""
    shown = ", ".join(items[:2]) + (", …" if len(items) > 2 else "")
    return f"{len(items)} files: {shown}"


# --------------------------------------------------------------------------- #
# A. Choose a file
# --------------------------------------------------------------------------- #
CLASS_LABELS = {"client": "Client data (AI only on private providers)", "public": "Public data (any allowed AI provider)"}
_cur = (st.session_state.get("job") or {}).get("dataset_class", "client")
dataset_class = st.radio("Dataset class", list(CLASS_LABELS), index=list(CLASS_LABELS).index(_cur),
                         format_func=CLASS_LABELS.get, horizontal=True, key="dataset_class_choice",
                         help="Client data is only sent to AI providers marked `private: true` in "
                              "config/ai_providers.yaml (unless ai.allow_external_for_client is true). "
                              "Public data may use any provider allowed in the AI settings.")
if st.session_state.get("job") and st.session_state["job"].get("dataset_class") != dataset_class:
    st.session_state["job"]["dataset_class"] = dataset_class
    jobs.save_job(st.session_state["job"])
from app.core.privacy import set_dataset_class   # noqa: E402
set_dataset_class(dataset_class if st.session_state.get("job") else None)

tab_upload, tab_path, tab_sample = st.tabs(["Upload file(s)", "Use files on this computer", "Try a sample"])

with tab_upload:
    ups = st.file_uploader("Event log (one file, or several files of the same format)",
                           type=[s.lstrip(".") for s in SUPPORTED], accept_multiple_files=True)
    if ups and st.button("Load uploaded file(s)", type="primary"):
        name = ups[0].name if len(ups) == 1 else source_label([u.name for u in ups])
        start_job(name, uploads=ups)

with tab_path:
    st.write("Best for files over ~1 GB: the data is read where it is and never copied or changed. "
             "Enter one file, a folder (all files in it are read as one log) or a pattern.")
    raw = st.text_input("File, folder or pattern", placeholder=r"C:\data\exports  or  C:\data\events_*.parquet")
    if raw and st.button("Load from this computer", type="primary"):
        source = raw.strip().strip('"')
        try:
            resolve_sources(source)  # checks before a job is created: found, one format, not too many
        except ValueError as err:
            st.error(str(err))
        else:
            start_job(Path(source).name or source, path=source)

with tab_sample:
    samples = sorted((data_dir() / "samples").glob("*.*"))
    samples = [s for s in samples if s.suffix.lower() in SUPPORTED]
    if not samples:
        st.info("No samples yet. Generate one with:\n\n"
                "`python scripts/generate_synthetic.py --cases 20000 --out data/samples/clean_small.parquet`")
    else:
        choice = st.selectbox("Sample file", samples, format_func=lambda p: p.name)
        if st.button("Load sample", type="primary"):
            start_job(choice.name, path=choice)

# The rest of the page is for the file loaded above in this session. The current analysis (sidebar) may be an
# older one: then only a pointer to where it continues is shown.
if (st.session_state.get("upload_job_id") is None or "profile" not in st.session_state
        or (st.session_state.get("job") or {}).get("job_id") != st.session_state.get("upload_job_id")):
    cur = shell.current_job()
    if cur and shell.step_status(cur).get("import"):
        st.caption(f"The current analysis ({cur.get('source_name')}) is already imported. Load a file above to "
                   "start a new analysis, or continue with the current one:")
        shell.next_link(cur, after="import")
    st.stop()

job = st.session_state["job"]
prof = st.session_state["profile"]
con = get_con()
if st.session_state.get("overview") is None:
    st.session_state["overview"] = dataset_overview(con)
overview = st.session_state["overview"]

# --------------------------------------------------------------------------- #
# B. Profile
# --------------------------------------------------------------------------- #
st.divider()
st.markdown(f"##### 2 · What is in the file: {job['source_name']}")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Rows", f"{prof.rows:,}")
c2.metric("Columns", len(prof.columns))
c3.metric("Format", job.get("format", "").upper())
c4.metric("Profiled in", f"{prof.seconds:.1f} s")

if overview["files"] > 1:
    st.markdown(f"**Dataset: {overview['files']:,} files, {overview['rows']:,} rows**")
    with st.expander("Files in this dataset"):
        st.dataframe(pd.DataFrame(overview["per_file"]).rename(columns={"file": "File", "rows": "Rows"}),
                     hide_index=True, width="stretch")
        missing = overview["columns_missing_in_files"]
        if missing:
            st.warning("Some columns are not in every file:\n\n" + "\n".join(
                f"- `{m['column']}` is empty or missing in {m['missing_in']:,} of {overview['files']:,} files "
                "— those rows will have no value"
                for m in missing))

st.dataframe(pd.DataFrame([{
    "Column": c.name,
    "Type": c.dtype,
    "Missing %": c.null_pct,
    "Distinct (approx.)": c.distinct,
    "Looks like a date": f"{c.ts_parse_rate:.0%} ({c.ts_format})" if c.ts_format else "",
    "Examples": ", ".join(c.samples[:3]),
} for c in prof.columns]), hide_index=True, width="stretch")

with st.expander("Preview first 20 rows"):
    st.dataframe(con.execute("SELECT * FROM src LIMIT 20").df(), width="stretch")

# Automatic mode detection (app/intake/): one banner, pre-filled once per job; off with intake.auto_detect.
from app.ui.nocase_intake import banner as mode_banner  # noqa: E402
mode_banner(job, con, prof)

# --------------------------------------------------------------------------- #
# C. Map columns
# --------------------------------------------------------------------------- #
st.divider()
names = [c.name for c in prof.columns]
by_name = prof.by_name()
suggestions = suggest_mapping(prof)
saved = load_profile(names)
st.markdown("##### 3 · How cases are identified")
# Case ID: a column, or derived when the export has no suitable column (derived.py).
saved_case = (saved or {}).get("case_source") or {}
NOCASE_MODE = "No case ID (clickstream mapping)"
case_mode = st.radio("Case ID", ["Use a column", "Derive it", NOCASE_MODE], horizontal=True, key="case_mode",
                     index=1 if saved_case.get("kind", "column") != "column" else 0,
                     help="Derive it when no column identifies the case: an ID found in text, work sessions, "
                          "time windows, a system-of-record export, or no case at all (activity overview).")
if case_mode == NOCASE_MODE:   # the no-case mode has its own form (app/ui/nocase_mapping.py); nothing below runs
    from app.ui.nocase_mapping import render as render_nocase
    render_nocase(job, con, prof)
    st.stop()

st.markdown("##### 4 · Map your columns")
if saved:
    st.success("This file layout was mapped before; the saved mapping is pre-filled.")
else:
    st.caption("Suggestions are pre-filled. Check each one: you are the final decision.")

if suggestions["step_run"].column and not step_run_fits(con, suggestions["step_run"].column,
                                                           suggestions["activity"].column, SOURCE_VIEW):
    suggestions["step_run"] = Suggestion("step_run", None, 0.0, ["values hold more than one step"], False)
mapping: dict = {}
per_row = 5
cols = [c for i in range(0, len(ALL_ROLES), per_row)
        for c in st.columns(per_row)][:len(ALL_ROLES)]
for col_ui, role in zip(cols, ALL_ROLES):
    s = suggestions[role]
    default = (saved or {}).get(role, s.column)
    options = [None] + names
    with col_ui:
        mapping[role] = st.selectbox(
            ROLE_LABELS[role], options,
            index=options.index(default) if default in options else 0,
            format_func=lambda v: "— none —" if v is None else v, key=f"map_{role}",
        )
        if s.column and mapping[role] == s.column and not saved:
            badge = "🟢" if not s.needs_review else "🟠 please check"
            st.caption(f"{badge} confidence {s.confidence:.0%}: " + "; ".join(s.reasons))
        elif role in REQUIRED_ROLES and not mapping[role]:
            st.caption("🔴 required")

if case_mode == "Derive it":
    methods = {"text_pattern": "ID found in a text column (ticket number, code, masked token)",
               "work_session": "Work sessions (user + pause)", "time_window": "Fixed time windows (day or hour)",
               "record_join": "Join with a system-of-record export", "overview": "Activity overview only (no case)"}
    method = st.selectbox("How to derive the case", list(methods), format_func=methods.get, key="case_method",
                          index=list(methods).index(saved_case["kind"]) if saved_case.get("kind") in methods else 0)
    case_source: dict = {"kind": method}
    if method == "text_pattern":
        text_cols = [c for c in [mapping.get("window_title")] if c]
        text_cols += [c.name for c in prof.columns if c.dtype.startswith(("VARCHAR", "STRING", "TEXT"))
                      and c.distinct > 60 and c.name not in text_cols and c.name != mapping.get("activity")]
        if st.button("Find IDs in text columns", key="find_ids"):
            with st.spinner("Looking for repeating IDs in the text columns..."):
                st.session_state["id_patterns"] = find_id_patterns(con, text_cols[:6], mapping.get("activity"))
        patterns = st.session_state.get("id_patterns") or []
        if patterns:
            st.dataframe(pd.DataFrame([{
                "Pattern": p["label"], "Column": p["column"], "Rows with an ID %": round(100 * p["match_share"], 1),
                "Distinct IDs": p["distinct"], "Median rows per ID": p["median_rows_per_value"],
                "IDs spanning 2+ steps %": None if p["span_share"] is None else round(100 * p["span_share"], 1),
                "Case candidate": "yes" if p["candidate"] else "no"} for p in patterns]),
                hide_index=True, width="stretch")
            pick = st.selectbox("Use this ID as the case", range(len(patterns)), key="id_pick",
                                format_func=lambda i: f"{patterns[i]['label']} in {patterns[i]['column']}")
            case_source.update(column=patterns[pick]["column"], pattern=patterns[pick]["pattern"])
            case_source["confirmed"] = st.checkbox(
                "The business confirms what this ID means", key="case_confirmed",
                help="Until then, flow confidence stays at most Medium. The ID is stored only as a salted hash.")
        else:
            st.info("Click **Find IDs in text columns** to look for ticket numbers, codes or masked tokens.")
    elif method == "work_session":
        case_source["gap_minutes"] = st.number_input("A new session starts after a pause of (minutes)", 5, 480,
                                                     int(settings()["derived_case"]["session_gap_minutes"]),
                                                     key="case_gap")
        case_source["split_at_midnight"] = st.checkbox("Also start a new session at midnight", key="case_midnight")
        st.warning(WORK_SESSION_NOTE)
        if not mapping.get("resource"):
            st.error("Work sessions need a user / resource column.")
    elif method == "time_window":
        case_source["window"] = st.radio("Window", ["day", "hour"], horizontal=True, key="case_window")
        case_source["group"] = st.selectbox("Per (user or machine, optional)", [None] + names, key="case_group",
                                            format_func=lambda v: "— all events —" if v is None else v)
        st.warning(TIME_WINDOW_NOTE + " Flow confidence is capped at Low.")
    elif method == "record_join":
        records = st.file_uploader("System-of-record export (CSV or Excel: record_id, user, start_ts, end_ts)",
                                   type=["csv", "xlsx"], key="case_records")
        if records is not None:
            target = jobs.job_folder(job["job_id"]) / "raw" / f"records{Path(records.name).suffix.lower()}"
            target.write_bytes(records.getvalue())
            case_source["file"] = str(target)
        st.caption("Events by the same user inside a record's time window get that record's ID (longest overlap "
                   "wins). Events outside every window are left out and counted.")
    else:
        st.info("No case: activity counts, when work happens, step runs, applications and application switches. "
                "The process map, variants, waiting times and conformance are not available.")
    mapping["case_source"] = case_source
    mapping["case_id"] = DERIVED_CASE_COL

# Activity: a column, or screens (application + window title) when there is no activity column.
if not mapping.get("activity") and mapping.get("window_title"):
    if st.checkbox("Derive the activity from application + window title (screens)", key="derive_activity",
                   value=bool((saved or {}).get("derived_activity"))):
        mapping["derived_activity"] = True
        mapping["activity"] = DERIVED_ACTIVITY_COL
        st.caption(SCREEN_NOTE)

# Timestamp formats and timezone
all_formats = ["native", "native_tz", "iso", "epoch_s", "epoch_ms"] + settings()["mapping"]["timestamp_formats"]
fmt_cols = st.columns(3)
for ui, role in zip(fmt_cols[:2], ("timestamp", "end_timestamp")):
    col = mapping.get(role)
    if col:
        detected = by_name[col].ts_format or "iso"
        default = (saved or {}).get(f"{role}_format", detected)
        with ui:
            mapping[f"{role}_format"] = st.selectbox(
                f"Date format of '{col}'", all_formats,
                index=all_formats.index(default) if default in all_formats else 0,
                key=f"fmt_{role}",
            )
            if ambiguous_day_month(con, "__sample", col, mapping[f"{role}_format"]):
                st.warning("Every value works as both day/month and month/day. "
                           "Confirm the format with someone who knows the source system.")
    else:
        mapping[f"{role}_format"] = None
with fmt_cols[2]:
    tz_default = (saved or {}).get("timezone", "UTC")
    mapping["timezone"] = st.selectbox(
        "Timezone the times were recorded in", TIMEZONES,
        index=TIMEZONES.index(tz_default) if tz_default in TIMEZONES else 0,
        help="Times without their own offset are read in this timezone and converted to UTC. "
             "Times with an offset (+05:30, Z) and epoch times keep their own offset.",
    )

# --------------------------------------------------------------------------- #
# Application mapping (only when an Application column is chosen)
# --------------------------------------------------------------------------- #
if mapping.get("application"):
    st.markdown("#### Application mapping")
    st.caption("Give each application its real name once. The names are kept in one shared map "
               "for all datasets and applied when you convert. Recorded (masked) values and window "
               "titles are only shown here; they never reach the converted data, reports or the AI. "
               "Save the mapping before converting.")
    if st.session_state.get("apps_flash"):
        st.success(st.session_state.pop("apps_flash"))
    version = st.session_state.get("apps_version", 0)
    inv_key = (job["job_id"], mapping["application"], mapping.get("window_title"), mapping.get("resource"), version)
    if st.session_state.get("apps_inv_key") != inv_key:
        st.session_state["apps_inv"] = apps.app_inventory(con, mapping["application"], mapping.get("window_title"),
                                                          mapping.get("resource"))
        st.session_state["apps_inv_key"] = inv_key
    inv = st.session_state["apps_inv"]
    s = apps.inventory_summary(inv)
    st.markdown(f"**{s['applications']} applications · {s['masked']} masked · {s['mapped']} of {s['masked']} "
                f"mapped ({s['unnamed_share']:.0%} of events still unnamed)**")
    only_open = st.toggle("Show only masked / unmapped", key="apps_only_open")
    view = inv[inv["masked"] & (inv["real_name"].fillna("").str.strip() == "")] if only_open else inv
    categories = list(settings()["apps"]["categories"])
    categories += sorted({c for c in inv["category"] if c and c not in categories})  # imported ones too
    editor_table = pd.DataFrame({
        "raw_value": view["raw_value"],
        "Application (as recorded)": [f"{v[:8]}…" if m else v for v, m in zip(view["raw_value"], view["masked"])],
        "Masked?": view["masked"], "Events": view["events"], "Users": view["users"],
        "Example window titles": view["examples"],
        "Real name": view["real_name"], "Category": view["category"].replace("", None), "Notes": view["notes"],
    })
    edited = st.data_editor(
        editor_table, key=f"apps_editor_{only_open}_{version}", hide_index=True, width="stretch",
        disabled=["Application (as recorded)", "Masked?", "Events", "Users", "Example window titles"],
        column_config={
            "raw_value": None,  # the full value: kept for saving, shown in the expander below
            "Real name": st.column_config.TextColumn("Real name ✏️"),
            "Category": st.column_config.SelectboxColumn("Category ✏️", options=categories),
            "Notes": st.column_config.TextColumn("Notes ✏️"),
        })
    with st.expander("Full recorded values (for copying)"):
        masked_values = view.loc[view["masked"], "raw_value"].tolist()
        st.code("\n".join(masked_values) if masked_values else "No masked values.", language=None)
    save_col, download_col = st.columns(2)
    if save_col.button("Save mapping", key="apps_save"):
        counts = apps.save_map([{"raw_value": r["raw_value"], "real_name": r["Real name"],
                                 "category": r["Category"], "notes": r["Notes"]}
                                for r in edited.to_dict("records")])
        st.session_state["apps_flash"] = (
            f"Mapping saved: {counts['added']} added, {counts['updated']} updated, {counts['removed']} removed "
            f"({counts['total']} applications in the shared map).")
        st.session_state["apps_version"] = version + 1
        st.rerun()
    download_col.download_button("Download mapping (CSV)", apps.export_map(), file_name=apps.MAP_FILE,
                                 mime="text/csv", key="apps_download")
    uploaded_map = st.file_uploader("Import mapping (CSV/Excel)", type=["csv", "xlsx"], key="apps_import")
    if uploaded_map is not None:
        preview = apps.import_map(uploaded_map.getvalue())
        st.info(f"This file would add {preview['added']}, update {preview['updated']} and leave "
                f"{preview['unchanged']} unchanged; {preview['ignored']} rows are ignored (no value or no name).")
        if st.button("Confirm import", key="apps_import_ok"):
            counts = apps.save_map(preview["rows"])
            st.session_state["apps_flash"] = (f"Import saved: {counts['added']} added, {counts['updated']} "
                                              f"updated ({counts['total']} applications in the shared map).")
            st.session_state["apps_version"] = version + 1
            st.rerun()

# --------------------------------------------------------------------------- #
# Rows and columns to keep
# --------------------------------------------------------------------------- #
st.markdown("##### 5 · Rows and columns to keep")
st.caption("Real logs mix process events with unrelated activity (other apps, system noise, "
           "other processes). You decide what is in scope; whatever is left out is counted "
           "and reported.")
saved_scope = (saved or {}).get("scope") or {}
role_cols = {mapping[r] for r in ALL_ROLES if mapping.get(r)}
scope_left, scope_right = st.columns(2)
with scope_left:
    drop_case = st.checkbox(
        "Leave out rows without a case ID", key="drop_case",
        value=bool(saved_scope.get("drop_missing_case", False)),
        help="Tick this when rows without a case are unrelated activity (other apps, system "
             "events), not lost data. Otherwise leave it, so the quality check counts them.",
    )
    if mapping.get("case_id") in by_name:
        st.caption(f"'{mapping['case_id']}' is missing in {by_name[mapping['case_id']].null_pct}% of rows.")
    drop_act = st.checkbox(
        "Leave out rows without an activity", key="drop_act",
        value=bool(saved_scope.get("drop_missing_activity", False)),
    )
    if mapping.get("activity") in by_name:
        st.caption(f"'{mapping['activity']}' is missing in {by_name[mapping['activity']].null_pct}% of rows.")
    mapping["merge_repeats"] = st.checkbox(
        "Merge repeated steps", key="merge",
        value=bool((saved or {}).get("merge_repeats", False)),
        help="When the same step is recorded several times in a row in a case, keep it as one "
             "step: it starts at the first event and ends at the last one. Useful for desktop, "
             "clickstream and status data. The quality check tells you when it is worth it.",
    )
with scope_right:
    # Good filter columns: not a role, not a date, and only a few distinct values.
    filter_options = [c.name for c in prof.columns
                      if c.name not in role_cols and not c.ts_format and 2 <= c.distinct <= 50]
    no_filter = "— no filter —"
    fcol_options = [no_filter] + filter_options
    fcol_default = saved_scope.get("filter_column")
    fcol = st.selectbox(
        "Only keep rows where this column…", fcol_options, key="fcol",
        index=fcol_options.index(fcol_default) if fcol_default in fcol_options else 0,
    )
    fvals: list[str] = []
    if fcol != no_filter:
        q = quote_ident(fcol)
        top_values = [r[0] for r in con.execute(
            f"SELECT CAST({q} AS VARCHAR) AS v, COUNT(*) AS n FROM src WHERE {q} IS NOT NULL "
            f"GROUP BY v ORDER BY n DESC LIMIT 50").fetchall()]
        saved_vals = [v for v in saved_scope.get("filter_values") or [] if v in top_values]
        fvals = st.multiselect("…has one of these values", top_values, default=saved_vals, key="fvals")
mapping["scope"] = {
    "drop_missing_case": drop_case,
    "drop_missing_activity": drop_act,
    "filter_column": fcol if fcol != no_filter and fvals else None,
    "filter_values": fvals,
}

# Personal data: suggested columns are pre-selected to be left out.
unmapped = [n for n in names if n not in role_cols]
suggested = sensitive_columns(prof, role_cols)
saved_exclude = (saved or {}).get("exclude_columns")
exclude_default = saved_exclude if saved_exclude is not None else list(suggested)
mapping["exclude_columns"] = st.multiselect(
    "Columns to leave out of the converted data (e.g. personal data)", unmapped,
    default=[c for c in exclude_default if c in unmapped], key="exclude",
    help="Left-out columns are never copied into the converted file, "
         "so they cannot reach reports or the AI.",
)
for col_name, reason in suggested.items():
    st.caption(f"🔒 Suggested: **{col_name}**: {reason}")

# --------------------------------------------------------------------------- #
# D. Validate and convert
# --------------------------------------------------------------------------- #
st.divider()
st.markdown("##### 6 · Validate and convert")
# What this data supports (and what each missing column costs)
caps = capabilities(mapping)
with st.expander("What this data supports", expanded=any(c["status"] != "available" for c in caps)):
    st.dataframe(pd.DataFrame([{"": c["icon"], "Feature": c["feature"], "Needs": c["needs"]} for c in caps]),
                 hide_index=True, width="stretch")
    for line in missing_lines(caps):
        st.caption(line)

if st.button("Validate and convert", type="primary"):
    derive_problem = None
    cs = mapping.get("case_source") or {}
    if cs.get("kind") == "text_pattern" and not cs.get("pattern"):
        derive_problem = "Find IDs in the text columns and choose one first."
    elif cs.get("kind") == "record_join" and not cs.get("file"):
        derive_problem = "Upload the system-of-record export first."
    try:
        convert_mapping = mapping if derive_problem else apply_derivations(con, mapping)
    except ValueError as err:
        derive_problem = str(err)
    if derive_problem:
        st.error(derive_problem)
        st.stop()
    mapping = convert_mapping
    with st.spinner("Checking the mapping on all rows..."):
        checks = validate_mapping(con, mapping, prof.rows)
    for c in checks:
        if c.severity == "info":
            icon = "ℹ️"
        else:
            icon = "✅" if c.ok else ("❌" if c.severity == "error" else "⚠️")
        st.write(f"{icon} **{c.name}**: {c.message}")
    if any(not c.ok and c.severity == "error" for c in checks):
        st.error("Fix the errors above, then try again.")
    else:
        folder = jobs.job_folder(job["job_id"])
        from app.core import activity_renames
        mapping["activity_renames"] = activity_renames.load(signature(names))   # merges saved on page 2
        with st.status("Writing the standard event table...", expanded=False) as status:
            result = normalise(con, mapping, folder / "events.parquet")
            status.update(label="Conversion complete", state="complete")
        with open(folder / "mapping.json", "w", encoding="utf-8") as f:
            json.dump(mapping, f, indent=2)
        save_profile(names, mapping)
        job.update(status="normalised", summary=result, warnings=[c.message for c in checks if not c.ok],
                   layout_signature=signature(names),   # page 3 reuses a saved step order per layout
                   case_source={**(mapping.get("case_source") or {"kind": "column", "column": mapping["case_id"]}),
                                "label": case_label(mapping)},
                   capabilities=capabilities(mapping))
        jobs.save_job(job)
        st.session_state["result"] = result

result = st.session_state.get("result")
if result:
    st.subheader("Converted")
    m = st.columns(5)
    m[0].metric("Events", f"{result['events']:,}")
    m[1].metric("Cases", f"{result['cases']:,}")
    m[2].metric("Activities", f"{result['activities']:,}")
    m[3].metric("Unparsed timestamps", f"{result['unparsed_timestamps']:,}")
    m[4].metric("Converted in", f"{result['seconds']:.1f} s")
    st.caption(f"Period: {result['first_event']} to {result['last_event']} (UTC) · "
               f"events.parquet is {result['size_mb']} MB · job {job['job_id']}")
    # What the scope options changed, so nothing is left out silently.
    scope_parts = []
    if result.get("rows_left_out"):
        scope_parts.append(f"{result['rows_left_out']:,} of {result['rows_in_file']:,} rows "
                           "left out by your filters")
    if result.get("events_before_merge", result["events"]) > result["events"]:
        scope_parts.append(f"{result['events_before_merge']:,} events merged into "
                           f"{result['events']:,} steps")
    if result.get("columns_left_out"):
        scope_parts.append("columns left out: " + ", ".join(result["columns_left_out"]))
    if scope_parts:
        st.caption("Scope: " + " · ".join(scope_parts))
    if result["unparsed_timestamps"]:
        st.info("Rows with problems are kept on purpose. The Data quality step (Day 2) "
                "counts them and decides whether the data can support a reliable report.")
