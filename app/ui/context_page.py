"""Step 2 of both workflows: Business context (mandatory before the report).

The reviewer names the process this file belongs to (a process in the library, or a new one) and adds what the
business says about it: SOPs, transcripts, decks, walkthrough recordings, notes, process models, policy tables.
Nothing is preloaded: the app ships no process templates or sample processes, so every step name and step order
comes from these documents or from the data, never from a built-in template.

The step is complete when a process is chosen AND documents were added, or the reviewer recorded (with a reason)
that there are none. The report then says which of the two it rests on.

Where the documents are used:
    * case-ID files: Process definition turns them into statements and checks each one against the data
    * clickstream files: they are also kept in the process library, where the AI (when allowed) proposes the
      stages and steps; a reviewer approves them in Process library
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from app.core import jobs, knowledge
from app.core.definition_ai import available as ai_available
from app.nocase import library
from app.ui import shell

DOC_TYPES = (".txt", ".md", ".docx", ".pdf")    # what pack_from_docs can read

shell.header("Business context", "Which process is this, and what does the business say about it? Add the SOPs, "
             "transcripts, decks or walkthrough recordings. Required before the report: every claim in them is "
             "checked against the data, and nothing is taken from built-in templates.", "context")

job = shell.current_job()
if not job:
    st.info("Import a file first.")
    st.page_link(shell.st_page("import"), label="Import data", icon=":material/upload_file:")
    st.stop()
if not shell.step_status(job).get("import"):
    st.info("Finish **Import data** for the current analysis first (Validate and convert).")
    st.page_link(shell.st_page("import"), label="Import data", icon=":material/upload_file:")
    st.stop()
job = jobs.load_job(job["job_id"])
mode = shell.mode_of(job)


def _save(changes: dict) -> None:
    job.update(changes)
    for k, v in changes.items():
        if v is None:
            job.pop(k, None)
    jobs.save_job(job)
    st.session_state["job"] = job


# --------------------------------------------------------------------------- 1. process
st.markdown("##### 1 · Which process is this?")
mine = library.user_processes()
current = job.get("process")
if current and current not in mine:
    st.warning(f"This analysis was linked to **{current}**, a built-in sample that the app no longer offers. "
               "Choose your own process or create a new one.")
    current = None
NEW = "__new__"
options = mine + [NEW]
if "ctx_set_to" in st.session_state:                 # after "Use this process": show it as the selected one
    st.session_state["ctx_process"] = st.session_state.pop("ctx_set_to")
c1, c2 = st.columns([3, 2], vertical_alignment="bottom")
pick = c1.selectbox("Process", options, index=options.index(current) if current in options else len(options) - 1,
                    format_func=lambda k: "New process…" if k == NEW else library.display_name(k), key="ctx_process",
                    help="Analyses of the same process share its documents, steps, rules and review decisions.")
name = ""
if pick == NEW:
    name = c1.text_input("Process name", key="ctx_new_name", placeholder="e.g. HR onboarding UK")
if pick != current or pick == NEW:
    if c2.button("Use this process", type="primary", key="ctx_use", disabled=pick == NEW and not name.strip(),
                 width="stretch"):
        try:
            key = library.create_pack(name) if pick == NEW else pick
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        _save({"process": key, "business_context": None})       # a new process needs its own documents
        st.session_state["ctx_set_to"] = key
        st.rerun()
if not current:
    st.caption("Choose or create the process to continue.")
    st.stop()

process_name = library.display_name(current)
st.caption(f"This analysis belongs to **{process_name}**.")

# --------------------------------------------------------------------------- 2. documents
st.markdown("##### 2 · Business documents")
sources = knowledge.list_sources(process_name)
with st.container(border=True):
    kind = st.selectbox("Kind of document", knowledge.KINDS[:-1], format_func=knowledge.KIND_LABELS.get,
                        key="ctx_kind")
    uploads = st.file_uploader("Files (one or more)", type=[e.lstrip(".") for e in knowledge.EXTENSIONS[kind]],
                               accept_multiple_files=True,
                               key=f"ctx_files_{kind}_{st.session_state.get('ctx_upload_round', 0)}") or []
    a, b, c = st.columns(3)
    recorded = a.date_input("Dated / recorded on", value=None, key="ctx_date")
    walk = b.checkbox("Shows the work being done (walkthrough)", key="ctx_walk",
                      disabled=kind not in ("transcript", "video"))
    roles = c.text_input("Speaker roles, in order (optional)", key="ctx_roles",
                         disabled=kind not in ("transcript", "video"), placeholder="e.g. HR advisor, SME")
    if st.button("Add documents" if len(uploads) > 1 else "Add document", key="ctx_add", disabled=not uploads):
        docs_dir = library.pack_dir(current) / "docs"
        for up in uploads:
            try:
                src, messages = knowledge.add_source(
                    process_name, kind, up.name, up.getvalue(), None, "user",
                    recorded.isoformat() if recorded else None,
                    [r.strip() for r in roles.split(",") if r.strip()], walkthrough=walk)
                for m in messages:
                    st.warning(f"{up.name}: {m}")
                if mode == "nocase" and up.name.lower().endswith(DOC_TYPES):
                    docs_dir.mkdir(parents=True, exist_ok=True)       # read by "Propose stages and steps"
                    (docs_dir / up.name).write_bytes(up.getvalue())
            except ValueError as err:
                st.error(f"{up.name}: {err}")
        st.session_state["ctx_upload_round"] = st.session_state.get("ctx_upload_round", 0) + 1   # empty the box
        st.rerun()

if sources:
    st.dataframe(pd.DataFrame([{"ID": s["id"], "Document": s["title"], "Kind": knowledge.KIND_LABELS[s["kind"]],
                                "Dated": s.get("recorded_on") or s["added_at"][:10],
                                "Status": "in use" if s["status"] == "active" else "excluded",
                                "Note": "may be outdated" if s["stale"] else ""} for s in sources]),
                 hide_index=True, width="stretch")
else:
    st.caption("No documents yet for this process.")
active = [s for s in sources if s["status"] == "active"]

# --------------------------------------------------------------------------- 3. how they are used
if active:
    ok, why = ai_available()
    if mode == "case":
        st.caption("On **Process definition** each document is turned into statements (steps, order, owners, "
                   "rules) and every statement is checked against the data.")
    else:
        st.markdown("##### 3 · Stages and steps from the documents")
        if ok:
            st.caption("The AI reads the documents (text only, never the event data) and proposes the stages and "
                       "steps. Nothing is used until a reviewer approves it in Process library.")
            if st.button("Propose stages and steps", key="ctx_propose"):
                from app.nocase.ai.pack_from_docs import propose
                from app.nocase.ai_gateway import gateway
                with st.spinner("Reading the documents..."):
                    try:
                        r = propose(current, gateway(job.get("dataset_class"), "online"))
                    except Exception as exc:     # never a traceback on screen: say what happened
                        r = {"path": None, "reason": str(exc)}
                if r.get("masked"):
                    st.caption("Masked before sending: " + ", ".join(f"{n} {what}" for what, n in r["masked"].items())
                               + ". The documents themselves are unchanged.")
                if r.get("path"):
                    st.success(f"Proposed {r.get('stages')} stages and {r.get('steps')} steps: approve them in "
                               "Process library → Waiting for approval.")
                else:
                    st.warning(f"No proposal: {r.get('reason')}")
        else:
            st.caption(f"The AI is not available ({why or 'switched off'}): enter the stages and steps from the "
                       "documents by hand in Process library.")
        st.page_link(shell.st_page("library"), label="Open Process library", icon=":material/library_books:")

# --------------------------------------------------------------------------- confirm
st.markdown("##### " + ("4" if mode == "nocase" and active else "3") + " · Confirm")
ctx = job.get("business_context") or {}
if shell.context_done(job):
    if ctx["status"] == "documents":
        st.success(f"Business context confirmed by {ctx.get('by')} on {str(ctx.get('at'))[:10]}: "
                   f"{ctx.get('documents')} document(s) for {process_name}.", icon=":material/check_circle:")
    else:
        st.warning(f"Recorded by {ctx.get('by')} on {str(ctx.get('at'))[:10]}: no business documents are available "
                   f"({ctx.get('reason')}). The report will say that the process is described from the data only.",
                   icon=":material/info:")
    if st.button("Change", key="ctx_reopen"):
        _save({"business_context": None})
        st.rerun()
    shell.next_link(job, after="context")
    st.stop()

by = st.text_input("Reviewed by", key="ctx_by")
now = datetime.now(timezone.utc).isoformat()
if active:
    if st.button("Confirm business context", type="primary", key="ctx_confirm", disabled=not by.strip()):
        _save({"business_context": {"status": "documents", "documents": len(active), "by": by.strip(), "at": now}})
        st.rerun()
else:
    st.caption("Add at least one document, or record below that there are none.")
with st.expander("No documents are available for this process"):
    reason = st.text_input("Why not?", key="ctx_reason", placeholder="e.g. no SOP exists; SME interview planned")
    if st.button("Record: no documents", key="ctx_none", disabled=not (by.strip() and reason.strip())):
        _save({"business_context": {"status": "no_documents", "reason": reason.strip(), "by": by.strip(),
                                    "at": now}})
        st.rerun()
    st.caption("Enter your name above and a reason. The report states that no business documents were used.")
