"""Page 3, Sub-processes tab, Level 4 view.

Top: the process overview and the stage storyboard (stages as columns, steps as boxes listing their
sub-steps), with a summary per stage. Then one step: its summary, the filtered Level 4 flow, the sub-step
table with descriptions, and the editor for labels, descriptions and RULES (with a live preview). Saving
recomputes only that step and writes it back into mining.json; building with the AI runs in the analysis
(page 3 "Run analysis") or on "Rebuild with AI".
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from app.core import level4, reference
from app.core.db import connect
from app.core.mining import format_hours

BADGE_COLORS = {"High": "green", "Medium": "blue", "Exploratory": "orange"}
PCT = st.column_config.NumberColumn(format="%.1f")


@st.cache_data(show_spinner=False)
def _detail(folder: str, modified: float, l3: str, entry_json: str, standard_json: str) -> dict:
    """One Level 3 at Level 4 (recomputed only when the job file, the step or its taxonomy changes)."""
    return level4.detail(connect(), Path(folder), l3, json.loads(entry_json), json.loads(standard_json) or None)


def _entry(job: dict, folder: Path, l3: str, fallback_reason: str | None) -> dict:
    saved = level4.load(reference.order_key(job)).get(l3)
    if saved:
        return saved
    return level4.rules_entry(l3, level4.units_of(level4.catalogue(folder), l3), fallback_reason or "no taxonomy yet")


def _fallback_banner(job: dict, folder: Path, data: dict) -> None:
    """One banner for all steps built without AI, and a button that retries the AI for just those steps."""
    from app.core.report import level4_banner
    text = level4_banner(data.get("l3s") or [])
    if not text:
        return
    st.warning(text)
    fell_back = [d["l3"] for d in data["l3s"] if d.get("source") == "rules" and not d.get("confirmed")]
    if st.button(f"Retry AI for the {len(fell_back)} steps that fell back", key="l4_retry_fallback"):
        from app.core.subprocess import load_standards
        sig = reference.order_key(job)
        with st.spinner("Retrying the AI one step at a time (paced to stay under the providers' limits)..."):
            done = level4.build_or_update(job, "reuse", l3s=fell_back)
            standards = load_standards(sig)
            for l3 in fell_back:
                entry = level4.load(sig).get(l3)
                if entry:
                    level4.update_mining(folder, level4.detail(connect(), folder, l3, entry, standards.get(f"L4:{l3}")))
        ai = sum(1 for v in done.values() if "(ai)" in v)
        st.session_state["l4_flash"] = (f"AI built {ai} of {len(fell_back)} steps; "
                                        f"{len(fell_back) - ai} still use rule-based labels.")
        st.rerun()


def render(job: dict, folder: Path, tree: list[dict], data: dict) -> None:
    from app.core.subprocess import ACTIONS_FILE, load_standards
    _fallback_banner(job, folder, data)
    if data.get("overview"):
        st.markdown("**The process at a glance**")
        st.write(data["overview"]["text"])
        st.caption(level4.provenance(data["overview"]))
    st.graphviz_chart(level4.storyboard_dot(data), width="stretch")
    for stg in data.get("stages") or []:
        with st.expander(f"Stage: {stg['stage']}"):
            st.write((stg.get("summary") or {}).get("text", ""))
            st.caption(level4.provenance(stg.get("summary")))

    stages = [s["stage"] for s in data.get("stages") or []]
    l3_of = {s["stage"]: s["l3s"] for s in data.get("stages") or []}
    c1, c2 = st.columns(2)
    stage = c1.selectbox("Stage", ["All stages"] + stages, key="l4_stage")
    shown = [d["l3"] for d in data["l3s"]] if stage == "All stages" else l3_of.get(stage, [])
    if not shown:
        st.info("No Level 3 step in this stage.")
        return
    l3 = c2.selectbox("Level 3 step", shown, key="l4_l3")
    saved_d = next(d for d in data["l3s"] if d["l3"] == l3)
    sig = reference.order_key(job)
    entry = _entry(job, folder, l3, saved_d.get("reason"))
    standard = load_standards(sig).get(f"L4:{l3}") or {}
    actions = folder / ACTIONS_FILE
    d = _detail(str(folder), actions.stat().st_mtime, l3, json.dumps(entry, sort_keys=True, default=str),
                json.dumps(standard, sort_keys=True))

    st.markdown(f"### {l3}")
    b1, b2, b3 = st.columns(3)
    with b1:
        st.badge(f"Confidence: {d['confidence']}", color=BADGE_COLORS.get(d["confidence"], "gray"),
                 help=f"Order of the sub-steps: {d['order_strength']:.2f} ({d['order_strength_label']}); {d['runs']:,} work sittings.")
    with b2:
        st.badge(f"{100 * d['coverage']:.0f}% of events in named sub-steps", color="blue")
    with b3:
        st.badge(level4.source_label(d), color="green" if d["confirmed"] else ("violet" if d["source"] == "ai" else "gray"))
    summary = saved_d.get("summary") if saved_d.get("taxonomy_hash") == d["taxonomy_hash"] else None
    if summary:
        st.write(summary["text"])
        st.caption(level4.provenance(summary))
    else:
        st.caption("The step summary is refreshed with the next analysis (Run analysis above).")

    show_all = st.toggle("Show all arrows", key=f"l4_all_{l3}")
    st.graphviz_chart(level4.flow_dot(d, show_all), width="stretch")
    st.dataframe(pd.DataFrame([{
        "Sub-step": n["name"], "What is done": n.get("description", ""), "Apps": ", ".join(n["apps"]),
        "Share of the step's work %": round(100 * n["share"], 1), "Work sittings": n["runs"],
        "Median active time": format_hours(n["median_active_s"] / 3600) if n.get("median_active_s") is not None else "–",
        "Repeated in sittings %": round(100 * n["repeat_share"], 1)} for n in d["nodes"]]),
        hide_index=True, width="stretch",
        column_config={"Share of the step's work %": PCT, "Repeated in sittings %": PCT})
    for n in d["nodes"]:
        with st.expander(f"{n['name']}"):
            st.write(level4.substep_summary(n))
            # The rules that select the clicks are technical: they are shown and edited in "Edit Level 4" only.
    if d["unmatched"]["units"]:
        st.caption(f"{d['unmatched']['units']} click names match no sub-step (in Other); the largest: "
                   + "; ".join(d["unmatched"]["top"][:5]) + ". Use **Update with new actions** to assign them.")

    _editor(job, folder, l3, entry, sig)
    _quality_panel(job, folder, l3, entry, sig)
    _sme_controls(job, folder, l3, entry, sig)

    with st.container(border=True):
        st.markdown("**Standard way (Level 4)**")
        st.markdown(" → ".join(f"`{x}`" for x in d["standard"]["path"]) or "–")
        st.caption(d["standard"]["source"])
        comp = d["comparison"]
        if comp.get("classes"):
            st.markdown(f"**{comp.get('wording', 'How work sittings compare')}**")
            st.dataframe(pd.DataFrame([{"Class": c["label"], "Share of sittings %": round(100 * c["share"], 1),
                                        "Work sittings": c["runs"]} for c in comp["classes"]]),
                         hide_index=True, width="stretch", column_config={"Share of sittings %": PCT})
    if d["paths"]:
        st.markdown("**Top sub-step paths**")
        st.dataframe(pd.DataFrame([{"Path": " → ".join(p["path"]), "Work sittings": p["runs"], "Share of sittings %": round(100 * p["share"], 1),
                                    "Cumulative %": round(100 * p["cumulative_share"], 1)} for p in d["paths"]]),
                     hide_index=True, width="stretch", column_config={"Share of sittings %": PCT, "Cumulative %": PCT})
    st.download_button("Download Level 4 flow (DOT)", level4.flow_dot(d, show_all), file_name=f"level4_{l3}.dot",
                       mime="text/vnd.graphviz", key="l4_dot_download")


def _editor(job: dict, folder: Path, l3: str, entry: dict, sig: str) -> None:
    with st.expander("Edit Level 4"):
        if st.session_state.get("l4_flash"):
            st.success(st.session_state.pop("l4_flash"))
        version = st.session_state.get(f"l4_version_{l3}", 0)
        units = level4.units_of(level4.catalogue(folder), l3)
        subs = entry.get("substeps") or []
        st.markdown("Sub-steps (rename, describe, add a row to create one, delete a row to remove one)")
        table = st.data_editor(pd.DataFrame([{"Id": s["id"], "Label": s["label"], "Description": s.get("description", "")}
                                             for s in subs], columns=["Id", "Label", "Description"]),
                               key=f"l4_subs_{l3}_{version}", num_rows="dynamic", hide_index=True, width="stretch")
        labels = [str(x).strip() for x in table["Label"].tolist() if str(x).strip() and str(x) != "nan"]
        st.markdown("Rules (first match wins, in this order)")
        rules = st.data_editor(pd.DataFrame([{"Sub-step": s["label"], "Application": r.get("application") or "*",
                                              "Screen regex": r.get("screen_regex") or "",
                                              "Click regex": r.get("click_regex") or ""}
                                             for s in subs for r in s.get("rules") or []],
                                            columns=["Sub-step", "Application", "Screen regex", "Click regex"]),
                               key=f"l4_rules_{l3}_{version}", num_rows="dynamic", hide_index=True, width="stretch",
                               column_config={"Sub-step": st.column_config.SelectboxColumn("Sub-step", options=labels)})
        preview, problems = [], []
        for i, r in enumerate(rules.to_dict("records"), 1):
            rule = {"application": _cell(r.get("Application")) or "*", "screen_regex": _cell(r.get("Screen regex")) or None,
                    "click_regex": _cell(r.get("Click regex")) or None}
            p = level4.rule_preview(rule, units)
            preview.append({"Rule": i, "Sub-step": _cell(r.get("Sub-step")), "Matches units": p["units"],
                            "Matches events": p["events"], "Problem": p["error"] or ""})
            if p["error"]:
                problems.append(f"Rule {i}: {p['error']}")
        st.dataframe(pd.DataFrame(preview, columns=["Rule", "Sub-step", "Matches units", "Matches events", "Problem"]),
                     hide_index=True, width="stretch")

        def build(confirmed: bool) -> dict | None:
            if problems:
                for p in problems:
                    st.error(p)
                return None
            out = []
            for i, r in enumerate(table.to_dict("records"), 1):
                label = _cell(r.get("Label"))
                if label:
                    old = next((s for s in subs if s["label"] == label), {})
                    out.append({"id": _cell(r.get("Id")) or f"S{i}", "label": label,
                                "description": _cell(r.get("Description")), "rules": [],
                                "members": old.get("members") or []})
            by = {s["label"]: s for s in out}
            for r in rules.to_dict("records"):
                target = by.get(_cell(r.get("Sub-step")))
                if target:
                    target["rules"].append({"application": _cell(r.get("Application")) or "*",
                                            "screen_regex": _cell(r.get("Screen regex")) or None,
                                            "click_regex": _cell(r.get("Click regex")) or None})
            return {**entry, "source": "edited", "substeps": out, "confirmed_by_business": confirmed}

        def store(new_entry: dict, message: str) -> None:
            from app.core.subprocess import load_standards
            saved = level4.save_entry(sig, l3, new_entry)
            level4.update_mining(folder, level4.detail(connect(), folder, l3, saved, load_standards(sig).get(f"L4:{l3}")))
            st.session_state[f"l4_version_{l3}"] = version + 1
            st.session_state["l4_flash"] = message
            st.rerun()

        c1, c2, c3, c4 = st.columns(4)
        if c1.button("Save", type="primary", key=f"l4_save_{l3}"):
            new = build(False)
            if new:
                store(new, "Level 4 saved; this step was recomputed.")
        if c2.button("Confirm as business view", key=f"l4_confirm_{l3}"):
            new = build(True)
            if new:
                store(new, "Confirmed as the business view: the AI will not overwrite this step.")
        if c3.button("Rebuild with AI", key=f"l4_rebuild_{l3}"):
            st.session_state["l4_rebuild_pending"] = l3
        if c4.button("Update with new actions", key=f"l4_update_{l3}"):
            level4.build_or_update(job, "update", l3s=[l3])
            store(level4.load(sig).get(l3) or entry, "Click names without a sub-step were assigned where possible.")
        if st.session_state.get("l4_rebuild_pending") == l3:
            _rebuild_dialog(job, l3, sig, entry, store)


def _store_sme(job: dict, folder: Path, l3: str, sig: str, new_entry: dict, message: str) -> None:
    from app.core.subprocess import load_standards
    saved = level4.save_entry(sig, l3, new_entry)
    level4.update_mining(folder, level4.detail(connect(), folder, l3, saved, load_standards(sig).get(f"L4:{l3}")))
    st.session_state[f"l4_version_{l3}"] = st.session_state.get(f"l4_version_{l3}", 0) + 1
    st.session_state["l4_flash"] = message
    st.rerun()


def _quality_panel(job: dict, folder: Path, l3: str, entry: dict, sig: str) -> None:
    """Quality measures of the step's sub-steps (level4_quality.json): targets, flags with the units behind them,
    split proposals (applied only when accepted) and the review queue of unassigned clicks."""
    import json as _json
    try:
        q = _json.loads((folder / level4.QUALITY_FILE).read_text(encoding="utf-8")).get(l3)
    except (OSError, ValueError):
        q = None
    with st.expander("Level 4 quality", expanded=bool(q and q.get("flags"))):
        if not q:
            st.caption("Run the analysis to see the quality measures.")
            return
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Events in named sub-steps", f"{100 * q['named_events_share']:.1f}%", help="target ≥ 90%")
        c2.metric("Other's share of " + ("hours" if q.get("hours_based") else "clicks"), f"{100 * q['other_hours_share']:.1f}%",
                  help="target ≤ 10%")
        c3.metric("Units matching 2+ rules", f"{q['overlap_units']:,}", help="target 0")
        c4.metric("Stability", q["stability"], help="the same input gives the same assignments (hash compared on rerun)")
        if q["flags"]:
            for f in q["flags"]:
                st.warning(f"**{f['measure']}:** {f['reason']}")
                if f.get("units"):
                    with st.expander(f"Units behind '{f['measure']}'"):
                        st.write("\n".join(f"- {u}" for u in f["units"][:10]))
        else:
            st.success("All targets met.")
        rows = [{"Sub-step": k, "Share %": round(100 * v["share"], 1), "Units": v["units"], "Users": v.get("users"),
                 "App purity %": None if v.get("app_purity") is None else round(100 * v["app_purity"], 1),
                 "Label": ", ".join(v["label_problems"]) or "ok"} for k, v in q["sub_steps"].items()]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        units = level4.units_of(level4.catalogue(folder), l3)
        for i, sp in enumerate(q.get("split_proposals") or []):
            st.info(f"Split proposal: **{sp['sub_step']}** holds {100 * sp['share']:.0f}% of the step with {sp['units']} units. "
                    f"Screens: {', '.join(sp['screens'][:6])}.")
            screens = st.multiselect("Screens to move into a new sub-step", sp["screens"], key=f"l4_split_scr_{l3}_{i}")
            new = st.text_input("Name of the new sub-step", key=f"l4_split_name_{l3}_{i}")
            if st.button("Accept split", key=f"l4_split_{l3}_{i}") and screens and new.strip():
                _store_sme(job, folder, l3, sig, level4.sme_split(entry, sp["sub_step"], screens, new, units,
                                                                 st.session_state.get("l4_sme_name", "")),
                           f"Split {sp['sub_step']}: {new.strip()} created.")
        if q.get("review_queue"):
            st.markdown("**Review queue** (clicks no sub-step covers yet)")
            st.dataframe(pd.DataFrame(q["review_queue"]), hide_index=True, width="stretch")


def _sme_controls(job: dict, folder: Path, l3: str, entry: dict, sig: str) -> None:
    """Rename, merge, split (above), move a unit, lock. Saved with source "sme", your name and the date; locked
    sub-steps are never changed by an AI rebuild."""
    labels = [s["label"] for s in entry.get("substeps") or []]
    units = level4.units_of(level4.catalogue(folder), l3)
    with st.expander("SME edits (rename, merge, move, lock)"):
        by = st.text_input("Your name (saved with the edit)", key="l4_sme_name")
        a, b = st.columns(2)
        with a:
            st.markdown("**Rename**")
            old = st.selectbox("Sub-step", labels, key=f"l4_ren_old_{l3}")
            new = st.text_input("New name", key=f"l4_ren_new_{l3}")
            if st.button("Rename", key=f"l4_ren_{l3}") and new.strip():
                _store_sme(job, folder, l3, sig, level4.sme_rename(entry, old, new, by), f"Renamed {old} to {new.strip()}.")
            st.markdown("**Lock**")
            lk = st.selectbox("Sub-step to lock / unlock", labels, key=f"l4_lock_sel_{l3}")
            locked = next((s.get("locked") for s in entry.get("substeps") or [] if s["label"] == lk), False)
            if st.button("Unlock" if locked else "Lock (the AI never changes it)", key=f"l4_lock_{l3}"):
                _store_sme(job, folder, l3, sig, level4.sme_lock(entry, lk, not locked, by),
                           f"{'Unlocked' if locked else 'Locked'} {lk}.")
        with b:
            st.markdown("**Merge**")
            many = st.multiselect("Sub-steps to merge", labels, key=f"l4_merge_sel_{l3}")
            mname = st.text_input("Name of the merged sub-step", key=f"l4_merge_name_{l3}")
            if st.button("Merge", key=f"l4_merge_{l3}") and len(many) >= 2 and mname.strip():
                _store_sme(job, folder, l3, sig, level4.sme_merge(entry, many, mname, units, by),
                           f"Merged {len(many)} sub-steps into {mname.strip()}.")
            st.markdown("**Move a unit**")
            label_of = level4.assigner(entry)
            opts = sorted(units, key=lambda u: -int(u.get("events", 0)))[:300]
            pick = st.selectbox("Unit (application | screen | click)", range(len(opts)), key=f"l4_move_u_{l3}",
                                format_func=lambda i: f"{opts[i]['application'] or '-'} | {opts[i].get('screen') or '-'} | "
                                                      f"{opts[i]['action']} ({label_of(opts[i])})") if opts else None
            target = st.selectbox("To sub-step", labels, key=f"l4_move_t_{l3}")
            if pick is not None and st.button("Move", key=f"l4_move_{l3}"):
                _store_sme(job, folder, l3, sig, level4.sme_move(entry, opts[pick], target, by), f"Moved to {target}.")


def _cell(v) -> str:
    return "" if v is None or (isinstance(v, float) and v != v) else str(v).strip()


@st.dialog("Rebuild with AI?")
def _rebuild_dialog(job: dict, l3: str, sig: str, entry: dict, store) -> None:
    if entry.get("confirmed_by_business"):
        st.warning("This step is confirmed by the business: Rebuild keeps it unchanged.")
    else:
        st.write("The unconfirmed sub-steps of this step are replaced by a new AI proposal (only the action catalogue "
                 "is sent: names and shares, never rows or users).")
    a, b = st.columns(2)
    if a.button("Rebuild", type="primary", key="l4_rebuild_confirm"):
        st.session_state.pop("l4_rebuild_pending", None)
        level4.build_or_update(job, "build", l3s=[l3])
        store(level4.load(sig).get(l3) or entry, "Rebuilt.")
    if b.button("Cancel", key="l4_rebuild_cancel"):
        st.session_state.pop("l4_rebuild_pending", None)
        st.rerun()
