"""Business process definition: enter business knowledge, test it, and approve it.

This page deliberately contains no verdict logic.  It only collects a definition and
uses the testable core modules for validation, evidence, AI assistance and exports.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from app.ui import shell

from app.core import definition as dmod
from app.core import jobs, reference

from app.core.business_map import bpmn_problems, business_html, to_bpmn, to_business_dot
from app.core.db import connect
from app.core.definition_ai import (apply_ops, available as ai_available, describe_op,
                                    draft_from_documents,
                                    extract_text, load_suggestions, record_decision, save_suggestions,
                                    suggest_corrections, validate_suggestion)
from app.core.evidence import load_evidence, run_check, score_line
from app.core.facts import load_facts


shell.header("Process definition", "Define the intended process, check every claim against the data, then approve it. "
             "Verdicts are computed from the data; AI suggestions are never applied automatically.", "definition")


def _activity_data(folder: Path) -> tuple[list[str], dict[str, int], list[str], dict[str, str], dict[str, float]]:
    """Names and small aggregates only; never load event rows into the browser."""
    path = (folder / "events.parquet").as_posix()
    con = connect()
    rows = con.execute(f"SELECT activity, COUNT(*) FROM read_parquet('{path}') GROUP BY 1 ORDER BY 2 DESC, 1").fetchall()
    apps = con.execute(f"SELECT DISTINCT application FROM read_parquet('{path}') "
                       "WHERE application IS NOT NULL AND trim(application) <> '' ORDER BY 1").fetchall()
    dominant_apps = dict(con.execute(f"""WITH ranked AS (
            SELECT activity, application, COUNT(*) AS n,
                   ROW_NUMBER() OVER (PARTITION BY activity ORDER BY COUNT(*) DESC, application) AS rn
            FROM read_parquet('{path}') WHERE application IS NOT NULL AND trim(application) <> ''
            GROUP BY 1, 2) SELECT activity, application FROM ranked WHERE rn = 1""").fetchall())
    medians = dict(con.execute(f"""SELECT activity,
            median(epoch(ts_end) - epoch(ts_start)) / 3600.0
            FROM read_parquet('{path}') WHERE ts_start IS NOT NULL AND ts_end IS NOT NULL AND ts_end >= ts_start
            GROUP BY 1""").fetchall())
    return [r[0] for r in rows], dict(rows), [r[0] for r in apps], dominant_apps, medians


def _get_draft(key: str) -> dmod.ProcessDefinition | None:
    raw = st.session_state.get(key)
    return dmod.ProcessDefinition.from_dict(raw) if raw else None


def _set_draft(key: str, definition: dmod.ProcessDefinition) -> None:
    st.session_state[key] = definition.to_dict()


TEMPLATE_BADGE = "drafted from template — please re-check"


def _template_badge(definition) -> None:
    if definition is not None and str(definition.source or "").startswith("Template:"):
        st.warning(f"**{TEMPLATE_BADGE}**: this definition was started from the built-in {definition.source[10:]} "
                   "template, which is not evidence about your process. Check its order against your documents "
                   "('Proposed order' below) before you rely on it.")


def _proposed_order_section(definition) -> None:
    """Proposed order (from your documents, checked against the data): order_evidence.compute, shown with the
    quote and the data verdict of every arrow; "Use as draft" puts it into the editor for approval."""
    from app.core import order_evidence
    st.subheader("Proposed order (from your documents, checked against the data)")
    st.caption("The Level 3 order comes from the documents you loaded as knowledge sources (order statements with "
               "their quotes). The data only says how far the fully observed cases support each arrow. Conflicts "
               "between documents are never decided here: they are questions for the experts (Conflicts tab).")
    if st.button("Propose the order from the documents and the data", key="evidence_order_run"):
        with st.spinner("Reading the statements and checking the cases..."):
            order_evidence.compute(job, definition)
        st.rerun()
    ev = order_evidence.load(folder)
    if not ev:
        st.caption("Not computed yet.")
        return
    st.markdown(f"**{ev['label']}** · {ev['cases_used']:,} fully observed cases of {ev['cases_total']:,} "
                f"(cases at least {ev['edge_days']} days inside the data).")
    st.dataframe(pd.DataFrame([{"#": s["position"], "Step": s["step"], "Position from": s["provenance"],
                                "Note": s["note"] or ""} for s in ev["steps"]]), hide_index=True, width="stretch")
    if ev.get("pairs"):
        st.markdown("**Arrows and what the data says**")
        for p in ev["pairs"]:
            share = "–" if p["share"] is None else f"{100 * p['share']:.0f}%"
            icon = {"supports": "✅", "contradicts": "❌", "unclear": "❔"}[p["verdict"]]
            head = f"{icon} {p['a']} → {p['b']}: {p['verdict']} ({share} of {p['cases']:,} cases do {p['a']} first)"
            if p["kind"] == "conflict":
                head += f" · conflict {p['conflict']} ({p['status']}) — decide it in the Conflicts tab"
            with st.expander(head):
                if p["quotes"]:
                    for q in p["quotes"]:
                        st.write(f"“{q['quote']}” — {q['source_id']} [{q['anchor']}]")
                else:
                    st.caption("No document states this arrow directly: the two steps are neighbours in the "
                               "documented order." if p["kind"] == "neighbour" else "See the Conflicts tab.")
    if ev.get("not_stated"):
        st.caption("Position not stated in any document (placed by the data): " + ", ".join(ev["not_stated"]))
    if ev.get("documented") and st.button("Use this order as the draft to edit and approve", key="evidence_order_use"):
        d = order_evidence.to_definition(ev, PROCESS_NAME, definition)
        d.version = dmod.next_version(d.name)
        d.change_log.append({"version": d.version, "at": dmod._now(), "by": "user",
                             "summary": "Proposed order from the documents, checked against the data"})
        _set_draft(draft_key, d)
        st.success("The proposed order is now the draft: review it in the Edit tab and approve it in the Approve tab.")
        st.rerun()


def _pm4py_check(definition) -> dict | None:
    """The saved PM4Py check of THIS approved definition version (pm4py_definition_check.json), or None."""
    from app.core import pm4py_model
    res = pm4py_model.load_json(folder, pm4py_model.DEFINITION_FILE)
    if not res or not res.get("available") or definition.status != "approved":
        return None
    return res if (res.get("definition"), res.get("version")) == (definition.name, definition.version) else None


def _pm4py_section(definition) -> None:
    """Check the approved process with PM4Py: fitness, precision, the most skipped / out-of-order steps."""
    from app.core import pm4py_model
    st.subheader("Check the approved process with PM4Py")
    ok, why = pm4py_model.status()
    if not ok:
        st.caption(why)
        return
    if definition.status != "approved":
        st.caption("Approve the definition first: PM4Py then checks every case against the approved process.")
        return
    if st.button("Check the approved process with PM4Py", key="pm4py_def_check"):
        with st.spinner("Replaying the cases on the approved process..."):
            res = pm4py_model.check_definition(job, definition)
        if not res.get("available"):
            st.warning(res.get("reason"))
    res = _pm4py_check(definition)
    if not res:
        st.caption("Not run yet for this version of the definition.")
        return
    a, b, c = st.columns(3)
    a.metric("Fitness against the approved process", f"{res['fitness']:.2f}",
             help="Token-based replay: 1.00 = every case follows the approved process.")
    b.metric("Precision", f"{res['precision']:.2f}" if res.get("precision") is not None else "–",
             help="1.0 = the approved process allows only what really happens; low = it allows paths nobody uses."
                  + (f" Not available: {res['precision_note']}." if res.get("precision_note") else ""))
    c.metric("Cases that follow it fully", f"{100 * res['fitting_cases_share']:.1f}%")
    for note in res.get("notes") or []:
        st.caption(note)
    left, right = st.columns(2)
    with left:
        st.markdown(f"**Most skipped steps** (of {res['sampled_cases']:,} sampled cases)")
        st.dataframe(pd.DataFrame([{"Step": x["step"], "Skipped %": round(100 * x["skipped_share"], 1)}
                                   for x in res["most_skipped"]], columns=["Step", "Skipped %"]), hide_index=True)
    with right:
        st.markdown("**Most out-of-order steps** (done at an unexpected point)")
        st.dataframe(pd.DataFrame([{"Step": x["step"], "Out of order %": round(100 * x["out_of_order_share"], 1)}
                                   for x in res["most_out_of_order"]], columns=["Step", "Out of order %"]),
                     hide_index=True)
    with st.expander("Every step"):
        st.dataframe(pd.DataFrame([{"Step": x["step"], "Skipped %": round(100 * x["skipped_share"], 1),
                                    "Out of order %": round(100 * x["out_of_order_share"], 1),
                                    "Extra (more than expected) %": round(100 * x["extra_share"], 1)}
                                   for x in res["steps"]]), hide_index=True, width="stretch")
    if res.get("not_in_definition"):
        st.markdown("**Not in the approved process** (steps in the data that no approved step covers)")
        st.dataframe(pd.DataFrame(res["not_in_definition"]).rename(
            columns={"activity": "Activity", "runs": "Runs", "cases": "Cases"}), hide_index=True)
    st.caption(res["caveat"] + " " + res["licence"])


def _rows(value, fields: tuple[str, ...]) -> list[dict]:
    """Normalise data_editor output without letting pandas NaN reach the model."""
    if isinstance(value, pd.DataFrame):
        value = value.to_dict("records")
    return [{f: r.get(f) for f in fields if f in r} for r in (value or []) if any(str(v) != "nan" for v in r.values())]


ready = [j for j in jobs.list_jobs() if jobs.reached(j, "mined")]
if not ready:
    st.info("Run the process analysis first (step 3).")
    st.stop()
job_id = shell.pick_job(ready, "definition", "run the process analysis first")
if job_id is None:
    st.info("Run the process analysis first.")
    shell.next_link(shell.current_job())
    st.stop()
job, folder = jobs.load_job(job_id), jobs.job_folder(job_id)
if job.get("mode") == "nocase":   # the clickstream workflow has its own pages
    st.info("The current analysis has no case ID: its pages are in the Clickstream workflow.")
    st.stop()
activities, activity_counts, applications, step_apps, median_hours = _activity_data(folder)
# The definition carries the process name from Business context, so the documents added there are its sources.
from app.nocase import library as _library  # noqa: E402
PROCESS_NAME = _library.display_name(job["process"]) if job.get("process") else (job.get("source_name") or "Process")
draft_key = f"business_draft_{job_id}"
definition = _get_draft(draft_key)
linked = dmod.for_job(job)
if definition is None and linked is not None:
    _set_draft(draft_key, linked)
    definition = linked

tab_start, tab_sources, tab_edit, tab_check, tab_conflicts, tab_approve, tab_final = st.tabs(
    ["Start the definition", "Knowledge sources", "Edit the definition", "Check against the data",
     "Conflicts & questions", "Approve", "Final map"])

with tab_start:
    st.write("Create a draft from the observed data, from process documents, or from a definition file.")
    if st.button("Start from the data", type="primary"):
        order = reference.load_order(reference.order_key(job))
        if order is None:
            order = reference.Order(PROCESS_NAME, [{"name": "Process", "steps": activities}])
        d = dmod.from_order(order, PROCESS_NAME, step_apps, median_hours)
        d.version = dmod.next_version(d.name)
        d.change_log.append({"version": d.version, "at": dmod._now(), "by": "user", "summary": "Started from the data"})
        _set_draft(draft_key, d)
        st.rerun()

    ok, why = ai_available()
    st.subheader("From a document")
    if not ok:
        st.info("Drafting from a document needs AI, which is not available now (" + (why or "switched off") + "). "
                "Turn it on in Settings, or upload a definition file below.")
    else:
        files = st.file_uploader("SOPs, decks or process descriptions (one or more)", type=["docx", "pdf", "txt", "md"],
                                 accept_multiple_files=True, key="business_doc")
        pasted = st.text_area("And / or paste document text", key="business_doc_text")
        st.caption("Long documents are cut into pieces that fit every AI provider in the route; a step found in "
                   "several pieces or documents becomes one step.")
        if st.button("Draft from documents", disabled=not (files or pasted.strip())):
            try:
                docs = [(f.name, extract_text(f.name, f.getvalue())) for f in files or []]
                if pasted.strip():
                    docs.append(("pasted-process-description.txt", pasted))
                with st.spinner(f"Drafting a definition from {len(docs)} document(s)..."):
                    result = draft_from_documents(job_id, docs, activities, applications=applications,
                                                  process_name=PROCESS_NAME)
                if result.definition is None:
                    st.error("No usable draft was returned. " + " ".join(result.errors[:3]))
                else:
                    result.definition.version = dmod.next_version(result.definition.name)
                    result.definition.change_log.append({"version": result.definition.version, "at": dmod._now(),
                        "by": "AI draft", "summary": result.definition.source})
                    _set_draft(draft_key, result.definition)
                    if result.dropped:
                        st.warning("Dropped because the source quote was not found: " + "; ".join(result.dropped))
                    st.rerun()
            except (ValueError, ImportError) as err:
                st.error(str(err))

    st.subheader("Upload a definition")
    uploaded = st.file_uploader("Excel, CSV, YAML, JSON, or BPMN", type=["xlsx", "csv", "yaml", "yml", "json", "bpmn", "xml"],
                                key="business_definition_upload")
    if uploaded and st.button("Import definition"):
        try:
            d, notes = dmod.load_file(uploaded.name, uploaded.getvalue(), default_name=Path(uploaded.name).stem)
            d.version = dmod.next_version(d.name)
            d.change_log.append({"version": d.version, "at": dmod._now(), "by": "user", "summary": f"Imported {uploaded.name}"})
            _set_draft(draft_key, d)
            for note in notes:
                st.info(note)
            st.rerun()
        except (ValueError, KeyError) as err:
            st.error(f"The definition could not be imported: {err}")

if definition is None:
    for tab in (tab_sources, tab_edit, tab_check, tab_conflicts, tab_approve, tab_final):
        with tab:
            st.info("Start or import a definition first.")
    st.stop()

from app.ui import knowledge_tabs

with tab_sources:
    knowledge_tabs.sources_tab(definition, activities, applications)

with tab_edit:
    st.caption(f"{definition.name} · v{definition.version} · {definition.status}. Save a draft before approval.")
    name = st.text_input("Name", definition.name)
    description = st.text_area("Description", definition.description)
    start_trigger, end_result = st.columns(2)
    start = start_trigger.text_input("Start trigger", definition.start_trigger)
    end = end_result.text_input("End result", definition.end_result)
    changed_by = st.text_input("Changed by", value="user")
    stages = st.data_editor(pd.DataFrame(definition.stages, columns=dmod.STAGE_FIELDS), num_rows="dynamic",
                            key=f"business_stages_{job_id}", width="stretch")
    step_frame = pd.DataFrame(definition.steps, columns=dmod.STEP_FIELDS)
    from app.core.subprocess import load_standards
    standards = load_standards(reference.order_key(job))
    linked = dmod.linked_standards(job, definition)
    step_frame["standard_mandatory_actions"] = [", ".join((linked.get(sid) or {}).get("mandatory_actions") or [])
                                                for sid in step_frame["id"]]
    from app.core.milestones import milestone_catalogue
    milestone_rows = milestone_catalogue(job)
    steps = st.data_editor(step_frame, num_rows="dynamic", key=f"business_steps_{job_id}", width="stretch",
        column_config={"mapped_activities": st.column_config.MultiselectColumn("Mapped activities", options=activities),
                       "mandatory": st.column_config.CheckboxColumn("Mandatory"),
                       "milestone_labels": st.column_config.ListColumn("Milestones (choose below)", disabled=True),
                       "standard_path_ref": st.column_config.SelectboxColumn(
                           "Step standard", options=sorted(set(standards) | set(activities)),
                           help="The saved standard way (Sub-processes tab in Process analysis); approving confirms it."),
                       "standard_mandatory_actions": st.column_config.TextColumn(
                           "Mandatory actions (standard)", disabled=True),
                       "sla_hours": st.column_config.NumberColumn("SLA hours", min_value=0.0)})
    if milestone_rows:
        with st.expander("Milestone labels per step"):
            for index, row in steps.iterrows():
                allowed_labels = [r["label"] for r in milestone_rows if r["is_milestone"]
                                  and r["step"] in (row.get("mapped_activities") or [])]
                selected = st.multiselect(str(row["name"]), allowed_labels,
                    default=[x for x in (row.get("milestone_labels") or []) if x in allowed_labels],
                    key=f"definition_milestones_{job_id}_{row['id']}")
                steps.at[index, "milestone_labels"] = selected
    flows = st.data_editor(pd.DataFrame(definition.flows, columns=dmod.FLOW_FIELDS), num_rows="dynamic",
                           key=f"business_flows_{job_id}", width="stretch",
                           column_config={"kind": st.column_config.SelectboxColumn("Kind", options=list(dmod.FLOW_KINDS))})
    edited = dmod.ProcessDefinition.from_dict({**definition.to_dict(), "name": name, "description": description,
        "start_trigger": start, "end_result": end, "stages": _rows(stages, dmod.STAGE_FIELDS),
        "steps": _rows(steps, dmod.STEP_FIELDS), "flows": _rows(flows, dmod.FLOW_FIELDS)})
    a, b, c = st.columns(3)
    if a.button("Validate"):
        errors, warnings = dmod.validate(edited, activities)
        for x in errors: st.error(x)
        for x in warnings: st.warning(x)
        if not errors: st.success("The definition structure is valid.")
    if b.button("Save draft", type="primary"):
        errors, warnings = dmod.validate(edited, activities)
        if errors:
            st.error("Fix validation errors before saving: " + " ".join(errors))
        else:
            if definition.status == "approved" or edited.name != definition.name:
                edited = dmod.new_draft(edited, changed_by, "Started a revised draft")
            edited.status, edited.approved_by, edited.approved_at = "draft", None, None
            edited.change_log.append({"version": edited.version, "at": dmod._now(), "by": changed_by or "unknown",
                                       "summary": "Saved draft"})
            dmod.save(edited); _set_draft(draft_key, edited)
            for x in warnings: st.warning(x)
            st.success(f"Saved {edited.name} v{edited.version} as a draft.")
            st.rerun()
    c.download_button("Download Excel", dmod.to_excel(edited), file_name=f"{dmod.slug(edited.name)}.xlsx")
    y, z = st.columns(2)
    y.download_button("Download YAML", dmod.to_yaml(edited), file_name=f"{dmod.slug(edited.name)}.yaml")
    xml = to_bpmn(edited)
    z.download_button("Download BPMN", xml, file_name=f"{dmod.slug(edited.name)}.bpmn", mime="application/xml",
                      disabled=bool(bpmn_problems(xml)))

with tab_check:
    _template_badge(definition)
    st.write("Every verdict below is calculated from the event data. Evidence is saved with this job and becomes report facts.")
    if st.button("Check definition against the data", type="primary"):
        errors, warnings = dmod.validate(definition, activities)
        if errors:
            st.error("Fix validation errors before checking: " + " ".join(errors))
        else:
            for x in warnings: st.warning(x)
            evidence = run_check(job, definition)
            st.success(score_line(evidence))
    evidence = load_evidence(folder, definition)
    if evidence:
        st.metric("Evidence score", score_line(evidence))
        wanted = st.multiselect("Verdicts", ["Supported", "Partly supported", "Contradicted", "No data", "Reported"],
                                default=["Supported", "Partly supported", "Contradicted", "No data", "Reported"])
        table = [c for c in evidence["claims"] if c["verdict"] in wanted]
        columns = ["id", "statement", "verdict", "text", "support", "badge", "note"]
        pm_check = _pm4py_check(definition)
        if pm_check:            # the verdicts stay as they are; PM4Py only adds one column
            skipped = {x["step"]: x["skipped_share"] for x in pm_check["steps"]}
            names = {st_["id"]: st_["name"] for st_ in definition.steps}
            col = "PM4Py: % of cases where this step is skipped"
            table = [{**c, col: (round(100 * skipped[names[c["refs"]["step_id"]]], 1)
                                 if names.get((c.get("refs") or {}).get("step_id")) in skipped else None)}
                     for c in table]
            columns.append(col)
        st.dataframe(pd.DataFrame(table, columns=columns), hide_index=True, width="stretch")
        if pm_check:
            st.caption(pm_check["caveat"])
        ok, why = ai_available()
        if ok and st.button("Ask AI to explain and suggest corrections"):
            with st.spinner("Reviewing evidence..."):
                result = suggest_corrections(job_id, definition, evidence, activities, facts=load_facts(folder))
                save_suggestions(result, folder)
            st.rerun()
        elif not ok:
            st.info("AI suggestions are hidden because AI is switched off. " + (why or ""))
        suggestions = load_suggestions(folder)
        if suggestions:
            for i, suggestion in enumerate(suggestions.suggestions):
                st.write(f"**{describe_op(suggestion, definition)}** — {suggestion['reason']} "
                         f"({', '.join(suggestion.get('claims') or [])}) · {suggestion.get('status', 'open')}")
                if suggestion.get("status", "open") == "open":
                    accept, reject = st.columns(2)
                    if accept.button("Accept", key=f"suggest_accept_{i}"):
                        updated = apply_ops(definition, [suggestion], "user")
                        dmod.save(updated)
                        _set_draft(draft_key, updated)
                        record_decision(folder, i, True)
                        run_check(job, updated)
                        st.rerun()
                    if reject.button("Reject", key=f"suggest_reject_{i}"):
                        record_decision(folder, i, False); st.rerun()
                    with st.expander("Edit then accept"):
                        amended = st.text_area("Suggestion JSON", value=json.dumps(
                            {k: suggestion[k] for k in ("op", "args", "reason", "claims")}, indent=2),
                            key=f"suggest_edit_{i}")
                        if st.button("Validate and accept edited suggestion", key=f"suggest_edit_accept_{i}"):
                            try:
                                proposal = json.loads(amended)
                                claim_map = {c["id"]: c for c in evidence["claims"]}
                                known_ids = set(claim_map) | {f.id for f in load_facts(folder)}
                                errors = validate_suggestion(proposal, definition, claim_map, activities,
                                                             known_ids, [], "edited suggestion")
                                if errors:
                                    st.error(" ".join(errors))
                                else:
                                    updated = apply_ops(definition, [proposal], "user")
                                    dmod.save(updated)
                                    _set_draft(draft_key, updated)
                                    record_decision(folder, i, True)
                                    run_check(job, updated)
                                    st.rerun()
                            except json.JSONDecodeError as err:
                                st.error(f"Suggestion JSON is invalid: {err.msg}")
    else:
        st.info("Run the evidence check to see claims and AI suggestions.")
    _proposed_order_section(definition)
    _pm4py_section(definition)
    st.subheader("Business knowledge × data")
    knowledge_tabs.combined_section(definition, job, evidence)

with tab_conflicts:
    knowledge_tabs.conflicts_tab(definition, job, folder, load_evidence(folder, definition), activity_counts, draft_key)

with tab_approve:
    _template_badge(definition)
    approved = dmod.load(definition.name, which="approved")
    st.subheader("Changes since the last approved version")
    st.write("\n".join("- " + x for x in dmod.diff(approved, definition)))
    evidence = load_evidence(folder, definition)
    if evidence:
        st.write(score_line(evidence))
        open_s = load_suggestions(folder)
        if open_s and any(s.get("status") == "open" for s in open_s.suggestions):
            st.warning("There are open AI suggestions. Review or reject them before approving.")
    else:
        st.warning("Check the definition against the data before approving it.")
    approver = st.text_input("Approved by")
    if st.button("Approve this version", type="primary", disabled=evidence is None or definition.status == "approved"):
        try:
            approved_d = dmod.approve(definition, approver)
            dmod.make_confirmed_order(job, approved_d)
            run_check(job, approved_d)
            _set_draft(draft_key, approved_d)
            st.success("Approved. This definition is now the confirmed order source for this job.")
        except ValueError as err:
            st.error(str(err))

with tab_final:
    evidence = load_evidence(folder, definition)
    if evidence is None:
        st.info("Run the evidence check to create the final map.")
    else:
        show = st.checkbox("Show evidence colours", value=True)
        lane = st.radio("Swimlanes", ["stage", "owner_role"], format_func=lambda x: "Stage" if x == "stage" else "Owner role", horizontal=True)
        from app.core import combined_confidence, knowledge
        combined = combined_confidence.load(folder) if knowledge.list_sources(definition.name) else None
        dot = to_business_dot(definition, evidence["claims"], evidence.get("stats"), show, lane,
                              evidence.get("step_stats"), combined)
        if combined:
            st.caption(combined_confidence.breakdown_line(combined["summary"]))
        st.graphviz_chart(dot, width="stretch")
        a, b, c = st.columns(3)
        a.download_button("Download DOT", dot, file_name=f"{dmod.slug(definition.name)}.dot")
        b.download_button("Download HTML", business_html(definition, dot, evidence), file_name=f"{dmod.slug(definition.name)}.html",
                          mime="text/html")
        c.download_button("Download BPMN", to_bpmn(definition), file_name=f"{dmod.slug(definition.name)}.bpmn",
                          mime="application/xml", key="business_final_bpmn")
