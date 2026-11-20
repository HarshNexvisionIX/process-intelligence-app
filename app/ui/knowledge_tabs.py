"""Business process page: "Knowledge sources", "Conflicts & questions" and the combined
status shown under "Check against the data". No verdict logic here: knowledge.py and
combined_confidence.py decide everything."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from app.core import combined_confidence as cc
from app.core import definition as dmod
from app.core import knowledge
from app.core.definition_ai import apply_ops, available as ai_available, describe_op


def sources_tab(definition: dmod.ProcessDefinition, activities: list[str], applications: list[str]) -> None:
    name = definition.name
    ok, why = ai_available()
    tools = knowledge.tools_status()
    st.caption("Everything the team knows about the process. Text is extracted locally; nothing is sent anywhere "
               "unless the AI is switched on (then only masked text and names).")
    with st.expander("Add a source", expanded=not knowledge.list_sources(name)):
        kind = st.selectbox("Kind", knowledge.KINDS[:-1], format_func=knowledge.KIND_LABELS.get, key="ks_kind")
        uploads = st.file_uploader("Files (one or more; each becomes its own source)",
                                   type=[e.lstrip(".") for e in knowledge.EXTENSIONS[kind]], key=f"ks_file_{kind}",
                                   accept_multiple_files=True) or []
        c1, c2, c3 = st.columns(3)
        title = c1.text_input("Title (one file only; otherwise the file names)", key="ks_title")
        recorded = c2.date_input("Recorded / dated on", value=None, key="ks_date")
        walk = c3.checkbox("Shows the work being done (walkthrough)", key="ks_walk",
                           disabled=kind not in ("transcript", "video"))
        roles = st.text_input("Speaker roles in order of first appearance (optional, comma-separated)", key="ks_roles",
                              disabled=kind not in ("transcript", "video"))
        if kind == "video":
            missing = [t for t, have in tools.items() if not have]
            if missing:
                st.warning("Not installed: " + "; ".join(f"{t} — {knowledge.INSTALL[t]}" for t in missing))
        if st.button("Add sources" if len(uploads) > 1 else "Add source", type="primary", disabled=not uploads,
                     key="ks_add"):
            for upload in uploads:
                try:
                    src, messages = knowledge.add_source(
                        name, kind, upload.name, upload.getvalue(), (title or None) if len(uploads) == 1 else None,
                        "user", recorded.isoformat() if recorded else None,
                        [r.strip() for r in roles.split(",") if r.strip()], walkthrough=walk)
                    for m in messages:
                        st.warning(f"{upload.name}: {m}")
                    st.success(f"Added {src['id']}: {src['title']}.")
                except ValueError as err:
                    st.error(f"{upload.name}: {err}")
    sources = knowledge.list_sources(name)
    if not sources:
        st.info("No sources yet.")
        return
    st.dataframe(pd.DataFrame([{"Source": s["id"], "Title": s["title"], "Kind": knowledge.KIND_LABELS[s["kind"]],
                                "Date": s.get("recorded_on") or s["added_at"][:10],
                                "Reliability": s["effective_reliability"],
                                "Note": "may be outdated" if s["stale"] else "", "Status": s["status"],
                                "Statements": len(knowledge.load_statements(name, s["id"]))} for s in sources]),
                 hide_index=True, width="stretch")
    pick = st.selectbox("Source", [s["id"] for s in sources], key="ks_pick",
                        format_func=lambda i: f"{i} · {next(s['title'] for s in sources if s['id'] == i)}")
    src = next(s for s in sources if s["id"] == pick)
    a, b, c = st.columns(3)
    rel = a.number_input("Reliability", 0.0, 1.0, float(src["reliability"]), 0.05, key=f"ks_rel_{pick}")
    if a.button("Save reliability", key=f"ks_rel_save_{pick}"):
        knowledge.update_source(name, pick, reliability=rel)
        st.rerun()
    if b.button("Exclude" if src["status"] == "active" else "Re-include", key=f"ks_status_{pick}"):
        knowledge.update_source(name, pick, status="excluded" if src["status"] == "active" else "active")
        st.rerun()
    can_extract = ok or src["kind"] in ("policy", "model")
    if c.button("Extract statements", key=f"ks_extract_{pick}", disabled=not can_extract,
                help=None if can_extract else "AI is switched off: add statements by hand below."):
        kept, dropped, messages = knowledge.extract_statements(name, pick, definition, activities, None, applications)
        for m in messages:
            st.info(m)
        if dropped:
            st.warning("Dropped: " + "; ".join(dropped[:10]))
    if not ok:
        st.info("AI is switched off: statements come from fixed rules (policy and model sources) or are added by hand.")
    statements = knowledge.load_statements(name, pick)
    if statements:
        frame = pd.DataFrame([{"Anchor": s["anchor"], "Type": s["type"], "Subject": s["subject"], "Object": s["object"],
                               "Value": s["value"], "Quote": s["quote"], "By": s.get("added_by", "")} for s in statements])
        st.dataframe(frame, hide_index=True, width="stretch")
        if src["kind"] in ("video", "transcript"):
            st.markdown("**Timeline** (copy a timestamp to find the moment in the recording)")
            for s in sorted(statements, key=lambda x: x["anchor"]):
                st.code(s["anchor"], language=None)
                st.caption(f"{s['type'].replace('_', ' ')}: {s['subject']} {s['object']} — “{s['quote']}”")
    with st.expander("Add a statement by hand"):
        f1, f2, f3 = st.columns(3)
        typ = f1.selectbox("Type", knowledge.TYPES, key=f"ks_m_type_{pick}")
        subject = f2.text_input("Subject (step)", key=f"ks_m_subject_{pick}")
        obj = f3.text_input("Object (step, role or application)", key=f"ks_m_object_{pick}")
        g1, g2, g3 = st.columns(3)
        value = g1.text_input("Value (hours for an SLA, text for a rule)", key=f"ks_m_value_{pick}")
        anchor = g2.selectbox("Anchor", sorted(knowledge.anchors_of(knowledge.source_text(name, pick))),
                              key=f"ks_m_anchor_{pick}")
        quote = g3.text_input("Quote (exact words, at most 25)", key=f"ks_m_quote_{pick}")
        if st.button("Add statement", key=f"ks_m_add_{pick}"):
            try:
                knowledge.add_manual_statement(name, pick, {"type": typ, "subject": subject, "object": obj,
                                                            "value": value, "quote": quote, "anchor": anchor or ""},
                                               definition, activities, None, applications)
                st.rerun()
            except ValueError as err:
                st.error(str(err))
    pains = knowledge.pain_points(knowledge.all_statements(name))
    if pains:
        st.markdown("**Pain points and workarounds per step**")
        st.dataframe(pd.DataFrame(pains), hide_index=True, width="stretch")


def conflicts_tab(definition: dmod.ProcessDefinition, job: dict, folder: Path, evidence: dict | None,
                  activity_counts: dict[str, int], draft_key: str) -> None:
    name = definition.name
    statements = knowledge.all_statements(name)
    merged = knowledge.merge(statements)
    st.markdown("**Conflicts between sources**")
    if not merged["conflicts"]:
        st.caption("No conflicts.")
    for c in merged["conflicts"]:
        with st.container(border=True):
            st.markdown(f"**{c['id']}** · {c['status']}")
            for side in c["sides"]:
                st.markdown(f"- {side['label']} (belief {100 * side['belief']:.0f}%): "
                            + "; ".join(f"{x['source_id']} [{x['anchor']}] “{x['quote']}”" for x in side["sources"]))
            if c["status"] == "open":
                options = [s["key"] for s in c["sides"]] + ["variant"]
                choice = st.radio("Decision", options, key=f"kc_choice_{c['id']}",
                                  format_func=lambda k: "Both are true: a variant" if k == "variant"
                                  else knowledge.describe_key(k))
                if st.button("Save decision", key=f"kc_save_{c['id']}"):
                    knowledge.resolve_conflict(name, c, choice)
                    st.rerun()
            else:
                st.caption("Decision: " + knowledge.describe_key(c["resolution"]))
    combined = cc.combine(definition, merged, evidence)
    shares = {}
    for c in (evidence or {}).get("claims") or []:
        if c["kind"] == "uncovered_activity":
            shares[(c.get("refs") or {}).get("activity")] = c.get("value") or 0.0
    kinds = {s["id"]: s["kind"] for s in knowledge.list_sources(name)}
    qs = knowledge.questions(definition, merged, evidence, combined, shares, kinds)
    st.markdown("**Ask the experts**")
    if not qs:
        st.caption("No open questions.")
    answers = []
    for q in qs:
        ans = st.text_area(q["text"], key=f"kq_{q['id']}_{abs(hash(q['text'])) % 10**8}")
        if ans.strip():
            answers.append({"question": q["text"], "answer": ans.strip()})
    c1, c2 = st.columns(2)
    if c1.button("Save answers", disabled=not answers, key="kq_save"):
        knowledge.add_answers(name, answers)
        st.success(f"{len(answers)} answers saved as an expert-answer source.")
    if qs:
        c2.download_button("Download questions (Excel)", knowledge.questions_excel(qs),
                           file_name=f"questions_{dmod.slug(name)}.xlsx", key="kq_download")
    up = st.file_uploader("Upload answers (Excel)", type=["xlsx"], key="kq_upload")
    if up is not None and st.button("Import answers", key="kq_import"):
        try:
            got = knowledge.answers_from_excel(up.getvalue())
            knowledge.add_answers(name, got)
            st.success(f"{len(got)} answers imported.")
        except ValueError as err:
            st.error(str(err))
    st.markdown("**Update draft from knowledge**")
    props = knowledge.proposals(definition, merged)
    state = knowledge.load_state(folder, "knowledge_proposals.json", {})
    if not props:
        st.caption(f"Nothing to propose (statements need a business belief of at least "
                   f"{100 * float(knowledge._cfg()['min_belief']):.0f}%).")
    for i, p in enumerate(props):
        decided = state.get(p["key"])
        st.write(f"**{describe_op(p, definition)}** — {p['reason']}" + (f" · {decided}" if decided else ""))
        if decided:
            continue
        a, b = st.columns(2)
        if a.button("Accept", key=f"kp_accept_{i}"):
            updated = apply_ops(definition, [p], "knowledge")
            dmod.save(updated)
            st.session_state[draft_key] = updated.to_dict()
            state[p["key"]] = "accepted"
            knowledge.save_state(folder, "knowledge_proposals.json", state)
            st.rerun()
        if b.button("Reject", key=f"kp_reject_{i}"):
            state[p["key"]] = "rejected"
            knowledge.save_state(folder, "knowledge_proposals.json", state)
            st.rerun()


def combined_section(definition: dmod.ProcessDefinition, job: dict, evidence: dict | None) -> dict | None:
    """Combined status per element, with filters, and the overall confidence with its breakdown."""
    if not knowledge.list_sources(definition.name):
        st.caption("Add knowledge sources to combine business knowledge with the data.")
        return None
    combined = cc.run(job, definition)
    s = combined["summary"]
    st.markdown(f"**{cc.breakdown_line(s)}**")
    wanted = st.multiselect("Statuses", list(cc.STATUSES), default=list(cc.STATUSES), key="cc_filter")
    rows = [{"Element": e["label"], "Kind": e["kind"], "Status": e["status"], "Confidence": e["confidence"],
             "Business belief": e["belief"], "Data": e["verdict"], "Sources": ", ".join(e["sources"])}
            for e in combined["elements"] if e["status"] in wanted]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    return combined
