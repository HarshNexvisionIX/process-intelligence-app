"""SME review of proposed step rules and unassigned no-case units."""
from __future__ import annotations

import json
import re

import pandas as pd
import streamlit as st

from app.nocase.knowledge import library
from app.ui.nocase_review_common import page_context, reviewer_form

ctx = page_context("Review steps")
if ctx:
    folder, process, pack = ctx["folder"], ctx["process"], ctx["pack"]
    quality_path = folder / "nc_assign_quality.json"
    if quality_path.exists():
        quality = json.loads(quality_path.read_text(encoding="utf-8"))
        reused = quality.get("by_source", {}).get("rule", {})
        assignments_path = folder / "nc_assignments.parquet"
        reused_units = 0
        if assignments_path.exists():
            reused_units = int(pd.read_parquet(assignments_path)["source"].eq("rule").sum())
        st.caption(f"Approved library reuse: {reused_units:,} units · {reused.get('events', 0):,} events · "
                   + (f"{reused['hours']:.1f} active hours" if reused.get("hours") is not None
                      else "active hours unavailable"))
    by, comment = reviewer_form("review_steps")
    # The analysis writes its proposed rules to the process library (library/<process>/rules.proposed.yaml);
    # older runs wrote them to the job folder.
    proposals = library.load(process)["proposed"].get("rules.proposed.yaml") if process else None
    if not proposals and (folder / "rules.proposed.yaml").exists():
        proposals = library._read_yaml(folder / "rules.proposed.yaml", {})
    rules = list((proposals or {}).get("rules", []) if isinstance(proposals, dict) else (proposals or []))
    st.subheader("Proposed rules")
    if rules:
        from app.nocase.units import UNITS_FILE
        units_path = folder / UNITS_FILE
        units = pd.read_parquet(units_path) if units_path.exists() else pd.DataFrame()
        rows = []
        for rule in rules:
            matches = pd.Series([False] * len(units), index=units.index)
            if len(units):
                m = rule.get("match") or {}
                matches = units["app"].fillna("").astype(str).str.fullmatch(re.escape(str(m.get("app") or "*")), case=False) \
                    if m.get("app") and m.get("app") != "*" else matches
                matches = pd.Series(True, index=units.index) if not m.get("app") or m.get("app") == "*" else matches
                for col, key in (("screen_clean", "screen"), ("action_clean", "action")):
                    if m.get(key):
                        matches &= units[col].fillna("").astype(str).str.contains(str(m[key]), case=False, regex=True)
            hit = units.loc[matches] if len(units) else units
            rows.append({"ID": rule.get("id"), "Step": rule.get("step"), "Application": (rule.get("match") or {}).get("app"),
                         "Screen regex": (rule.get("match") or {}).get("screen"),
                         "Action regex": (rule.get("match") or {}).get("action"),
                         "Matched units": len(hit), "Events": int(hit["events"].sum()) if len(hit) else 0,
                         "Hours": round(float(hit.get("active_hours", pd.Series(dtype=float)).fillna(0).sum()), 2),
                         "Evidence reason": rule.get("why", "") or "AI or heuristic proposal",
                         "Confidence": rule.get("confidence", "")})
        st.caption(f"The AI linked {len(rules)} screen(s) to a step. They are already used on the map as *proposed*. "
                   "Check the list, change a step in the table where it is wrong, then accept: every later analysis of "
                   "this process reuses them as confirmed rules.")
        def _plain(rx):
            """^XK01\\ Create$ -> XK01 Create (an exact-match rule shown as the screen or click it matches)."""
            if not rx:
                return ""
            t = str(rx)
            if t.startswith("^") and t.endswith("$"):
                body = t[1:-1]
                plain = re.sub(r"\\(.)", r"\1", body)
                if re.escape(plain) == body:
                    return plain
            return f"pattern: {t}"
        table = pd.DataFrame(rows)
        table.insert(3, "Screen", table["Screen regex"].map(_plain))
        table.insert(4, "Click", table["Action regex"].map(_plain).replace("", "any"))
        step_names = [x["name"] for stg in (pack or {}).get("stages", []) for x in stg.get("steps", [])]
        edited = st.data_editor(
            table, hide_index=True, width="stretch", key="review_step_rules",
            column_order=["ID", "Step", "Application", "Screen", "Click", "Matched units", "Events", "Hours",
                          "Evidence reason"],
            disabled=["ID", "Application", "Screen", "Click", "Matched units", "Events", "Hours", "Evidence reason"],
            column_config={"Step": st.column_config.SelectboxColumn("Step", options=step_names or None,
                                                                    help="Change it if the AI chose the wrong step")})

        def _changed(ids_: list[str]) -> list[dict]:
            out = []
            for row in edited.to_dict("records"):
                if str(row["ID"]) in ids_:
                    out.append({"id": str(row["ID"]), "step": row["Step"], "match": {
                        "app": row.get("Application") or "*", "screen": row.get("Screen regex") or None,
                        "action": row.get("Action regex") or None},
                        "why": row.get("Evidence reason") or "SME reviewed", "confidence": 1.0})
            return out
        ids = edited["ID"].astype(str).tolist()
        if st.button(f"Accept all {len(rules)} rule(s), with your changes", key="review_step_accept_all",
                     type="primary"):
            if not by.strip():
                st.error("Enter your name in 'Reviewed by' to accept.")
            else:
                library.propose(process, "rules", _changed(ids), source="sme review")
                st.success(library.approve(ids, by, comment or "Accepted on Review steps", process=process))
                st.rerun()
        selected = st.multiselect("Or select some rules", ids, key="review_step_ids")
        col_a, col_b = st.columns(2)
        if col_a.button("Accept selected", key="review_step_accept"):
            if not by.strip():
                st.error("Enter who reviewed these rules.")
            elif selected:
                library.propose(process, "rules", _changed(selected), source="sme edit")
                st.success(library.approve(selected, by, comment, process=process))
                st.rerun()
            else:
                st.info("Select at least one proposal.")
        if col_b.button("Reject selected", key="review_step_reject"):
            if not by.strip() or not comment.strip():
                st.error("Enter the reviewer and a rejection reason in Review note.")
            elif selected:
                st.success(library.reject(selected, by, comment, process=process))
                st.rerun()
        # Preview regexes against cleaned units before saving an edit.
        adv = st.expander("Advanced: test a rule's screen or click pattern")
        selected_id = adv.selectbox("Rule", ids, key="review_step_preview_id")
        current = next(r for r in rows if str(r["ID"]) == selected_id)
        new_screen = adv.text_input("Screen pattern (regex)", str(current.get("Screen regex") or ""),
                                    key="review_step_screen")
        new_action = adv.text_input("Click pattern (regex)", str(current.get("Action regex") or ""),
                                    key="review_step_action")
        try:
            screen_re, action_re = re.compile(new_screen, re.I), re.compile(new_action, re.I)
            preview = units[units["screen_clean"].fillna("").astype(str).str.contains(screen_re) &
                            units["action_clean"].fillna("").astype(str).str.contains(action_re)] if len(units) else units
            adv.caption(f"Matches {len(preview)} units / {int(preview['events'].sum()) if len(preview) else 0} events.")
        except re.error as exc:
            adv.error(f"Invalid regex: {exc}")
    else:
        st.info("No step rules are waiting for review.")

    st.subheader("Unassigned units")
    unit_path = folder / "nc_units.parquet"
    assigned_path = folder / "nc_assignments.parquet"
    if unit_path.exists():
        units = pd.read_parquet(unit_path)
        assigned = pd.read_parquet(assigned_path) if assigned_path.exists() else pd.DataFrame()
        if len(assigned):
            units = units.merge(assigned[["unit_id", "step", "status"]], on="unit_id", how="left")
            units = units[units["status"].eq("unassigned") | units["step"].isna()]
        else:
            units = units.iloc[0:0]
        steps = [x for stg in (pack or {}).get("stages", []) for x in stg.get("steps", [])]
        step_names = [x["name"] for x in steps]
        stage_of = {x.get("name"): stg["name"] for stg in (pack or {}).get("stages", []) for x in stg.get("steps", [])}
        if not len(units):
            st.success("Every unit has a step.")
        elif not step_names:
            st.info("The process has no steps yet: add them in Process library first.")
        else:
            units = units.sort_values(["active_hours" if "active_hours" in units else "events", "unit_id"],
                                      ascending=[False, True])
            st.caption(f"{len(units)} unit(s) (application · screen · click) have no step yet, largest first. Pick the "
                       "step for each one you recognise, leave the rest empty (other work), enter your name above "
                       "and save. Each choice becomes an approved rule that every later analysis reuses.")
            table = pd.DataFrame({
                "Unit": units["unit_id"].astype(str), "Application": units["app"], "Screen": units["screen_clean"],
                "Click": units["action_clean"], "Events": units["events"],
                "Hours": units["active_hours"].round(2) if "active_hours" in units else None,
                "Step": [None] * len(units)})
            edited = st.data_editor(
                table, hide_index=True, width="stretch", key="review_assign_table",
                disabled=["Unit", "Application", "Screen", "Click", "Events", "Hours"],
                column_config={"Step": st.column_config.SelectboxColumn("Step", options=step_names, width="large",
                                                                         help="The business step this work belongs to")})
            picked = edited[edited["Step"].notna() & (edited["Step"].astype(str) != "")]
            if st.button(f"Save {len(picked)} assignment(s)", key="review_assign_save", type="primary",
                         disabled=picked.empty):
                if not by.strip():
                    st.error("Enter your name in 'Reviewed by' at the top to save.")
                else:
                    items, ids = [], []
                    for r in picked.itertuples():
                        row = units[units["unit_id"].astype(str) == r.Unit].iloc[0]
                        rid = f"SME-{r.Unit}"
                        items.append({"id": rid, "step": r.Step, "stage": stage_of.get(r.Step),
                                      "match": {"app": row.get("app") or "*",
                                                "screen": "^" + re.escape(str(row["screen_clean"])) + "$"
                                                if row.get("screen_clean") else None,
                                                "action": "^" + re.escape(str(row["action_clean"])) + "$"
                                                if row.get("action_clean") else None},
                                      "source": "sme", "status": "proposed", "confidence": 1.0})
                        ids.append(rid)
                    library.propose(process, "rules", items, source="sme assignment")
                    library.approve(ids, by, comment or f"Assigned {len(ids)} unit(s) on Review steps", process=process)
                    st.session_state["review_assign_flash"] = len(ids)
                    st.rerun()
        if st.session_state.get("review_assign_flash"):
            st.success(f"Saved {st.session_state.pop('review_assign_flash')} rule(s). Run the analysis again on "
                       "**Overview and analysis** to apply them to the map and the measures.")
            from app.ui import shell as _shell
            st.page_link(_shell.st_page("nc_overview"), label="Go to Overview and analysis",
                         icon=":material/arrow_forward:")

    # ---------------------------------------------------------------- how is this step done? (N4b)
    import json as _json
    inside_path = ctx["folder"] / "nc_inside_step.json"
    if inside_path.exists():
        st.subheader("How is this step done?")
        st.caption("The usual way each step is done, inferred from step runs. Confirm it or mark it wrong.")
        for _st in _json.loads(inside_path.read_text(encoding="utf-8")).get("steps") or []:
            if not _st.get("enough_runs") or not _st.get("patterns"):
                continue
            _p = _st["patterns"][0]
            st.markdown(f"**{_st['step']}** — usual pattern: {_p['pattern']} ({100 * _p['share']:.0f}% of "
                        f"{_st['runs']:,} runs) · status: {_p['status']}")
            _c1, _c2 = st.columns(2)
            for _col, _label, _action in ((_c1, "Confirm usual pattern", "sme_confirmed"),
                                          (_c2, "Mark it wrong", "rejected")):
                if _col.button(_label, key=f"inside_{_action}_{_st['step']}"):
                    if not by.strip():
                        st.error("Enter who reviewed this step.")
                    else:
                        from datetime import datetime, timezone
                        library._log(library.process_dir(process), {
                            "who": by.strip(), "when": datetime.now(timezone.utc).isoformat(),
                            "item": f"inside:{_st['step']}", "action": _action,
                            "before": {"pattern": _p["pattern"], "status": _p["status"]},
                            "after": {"pattern": _p["pattern"], "status": _action}, "comment": comment})
                        st.success("Saved: the next analysis shows it as " + _action.replace("_", " ") + ".")
