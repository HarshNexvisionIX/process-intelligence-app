"""Page 3, tab "Sub-processes": each process level, and how the work is done inside each step.

Reads mining.json (section "subprocess") and the small step-standards file. Editing a
standard recomputes only that step's comparison, from step_actions.parquet, with a cached
query keyed by job + step + standard; the whole analysis is never re-run here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from app.core import reference, subprocess as sp
from app.core.config import settings
from app.core.db import connect
from app.core.mining import format_hours, simplify, to_dot

BADGE_COLORS = {"High": "green", "Medium": "blue", "Exploratory": "orange"}

SITTINGS = ('"Step runs" are consecutive events of one step in a case (the map); "work sittings" split them by person and by pauses over 30 minutes, so a step has at least as many sittings as step runs.')


@st.cache_data(show_spinner=False)
def _comparison(actions: str, modified: float, step: str, standard_json: str) -> dict:
    """The comparison for one step with a given standard (only this step is recomputed)."""
    std = json.loads(standard_json)
    runs = sp.runs_of(connect(), Path(actions), step)
    out = sp.compare(runs, std["path"], std.get("mandatory_actions"), bool(std.get("confirmed")))
    out.pop("per_run", None)
    return out


def _hierarchy(tree: list[dict]) -> None:
    if not tree:
        st.caption("No process levels found.")
        return
    depth = max(n["depth"] for n in tree)
    names = [next(n["level"] for n in tree if n["depth"] == d) for d in range(1, depth + 1)]
    rows = []
    for n in tree:
        row = {name: (n["path"][i] if i == n["depth"] - 1 else "") for i, name in enumerate(names)}
        row.update({"Cases": n["cases"], "Share of cases %": round(100 * n["share"], 1), "Events": n["events"]})
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def render(job: dict, folder: Path, m: dict) -> None:
    from app.core import level4
    section = m.get("subprocess") or {"available": False,
                                      "reason": "Not available: run the analysis again to see sub-processes."}
    data = section.get("data") or {}
    l4 = m.get("level4") or {}
    st.markdown("**Process levels**")
    tree = data.get("hierarchy") or []
    rows = level4.level_rows(tree, l4.get("data") if l4.get("available") else None)
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"Share of cases %": st.column_config.NumberColumn(format="%.1f"),
                                    "Share of the step's work %": st.column_config.NumberColumn(format="%.1f")})
    else:
        _hierarchy(tree)
    if not section["available"]:
        st.info(section["reason"])
        return
    show_clicks = st.toggle("Show clicks", key="sp_show_clicks",
                            help="The raw click names per task instead of the Level 4 sub-steps.")
    if show_clicks or not l4.get("available"):
        if not l4.get("available") and l4.get("reason"):
            st.caption(l4["reason"])
        _clicks_view(job, folder, data)
        return
    from app.ui.level4_view import render as render_level4
    render_level4(job, folder, tree, l4["data"])


def _clicks_view(job: dict, folder: Path, data: dict) -> None:
    """The click-level view (unchanged): each task's actions, standard way and comparison."""
    steps = data["steps"]
    if not steps:
        st.info("No step has recorded actions.")
        return
    stage_of = data.get("stage_of") or {}
    stages = sorted({stage_of.get(d["activity"]) or "(no stage)" for d in steps})
    c1, c2 = st.columns(2)
    stage = c1.selectbox("Stage", ["All stages"] + stages, key="sp_stage")
    shown = [d for d in steps if stage == "All stages" or (stage_of.get(d["activity"]) or "(no stage)") == stage]
    pick = c2.selectbox("Step", [d["activity"] for d in shown], key="sp_step")
    d = next(x for x in shown if x["activity"] == pick)
    signature = reference.order_key(job)
    saved = sp.load_standards(signature).get(pick)
    actions_path = folder / sp.ACTIONS_FILE

    # 2. Header
    st.markdown(f"### {pick}")
    levels = next((n["path"] for n in data.get("hierarchy") or [] if n["level"] == "Step" and n["path"][-1] == pick), [])
    if len(levels) > 1:
        st.caption(" › ".join(levels[:-1]))
    stability = "–" if d["stability"] is None else f"{d['stability']:.2f}"
    st.badge(f"Step confidence: {d['confidence']}", color=BADGE_COLORS.get(d["confidence"], "gray"),
             help=f"Order of actions inside the step: {d['order_strength']:.2f} ({d['order_strength_label']}); "
                  f"stability {stability}; {d['runs']:,} work sittings.")
    standard = d["standard"]
    comp = d["comparison"]
    if saved and saved.get("standard_path") and saved["standard_path"] != standard["path"]:
        standard = {"path": saved["standard_path"], "mandatory_actions": saved.get("mandatory_actions") or [],
                    "confirmed": bool(saved.get("confirmed_by_business")),
                    "source": sp.SOURCE_BUSINESS if saved.get("confirmed_by_business") else "Saved — not confirmed"}
        comp = _comparison(str(actions_path), actions_path.stat().st_mtime, pick, json.dumps(standard))
    st.write(sp.describe_step({**d, "comparison": comp}))

    # 3. KPIs
    k = st.columns(5)
    k[0].metric("Work sittings", f"{d['runs']:,}", help=SITTINGS)
    k[1].metric("Cases", f"{d['cases']:,}")
    k[2].metric("Median actions", f"{d['median_actions']:g}")
    k[3].metric("Median duration" if d["median_active_h"] is None else "Median duration / active",
                format_hours(d["median_duration_h"]) if d["median_active_h"] is None
                else f"{format_hours(d['median_duration_h'])} / {format_hours(d['median_active_h'])}")
    k[4].metric("Apps", ", ".join(a["application"] for a in d["apps"]) or "–")

    # 4. Action map
    m1, m2 = st.columns(2)
    mode = m1.radio("Show", ["Frequency", "Time"], horizontal=True, key="sp_map_mode")
    arrows = m2.slider("Arrows shown", 10, 100, round(100 * settings()["subprocess"]["map_edge_coverage"]),
                       format="%d%%", key="sp_arrows")
    nodes, edges, _ = simplify(d["nodes"], d["edges"], 1.0, arrows / 100, settings()["subprocess"]["max_actions_on_map"])
    dot = to_dot(nodes, edges, mode.lower())
    st.graphviz_chart(dot, width="stretch")

    # 5. Standard way
    with st.container(border=True):
        st.markdown("**Standard way**")
        st.markdown(" → ".join(f"`{a}`" for a in standard["path"]) or "–")
        st.badge(standard["source"], color="green" if standard.get("confirmed") else "orange")
        version = st.session_state.get(f"sp_std_version_{pick}", 0)
        options = [" → ".join(p["path"]) for p in d["paths"]]
        chosen = st.selectbox("Path from the table", range(len(options)), format_func=lambda i: options[i],
                              key=f"sp_path_pick_{pick}_{version}") if options else None
        b1, b2 = st.columns(2)

        def store(path: list[str], mandatory: list[str], confirmed: bool, message: str) -> None:
            try:
                sp.save_standard(signature, pick, path, mandatory, confirmed)
            except ValueError as err:
                st.error(str(err))
                return
            st.session_state[f"sp_std_version_{pick}"] = version + 1
            st.session_state["sp_flash"] = message
            st.rerun()

        if chosen is not None and b1.button("Set selected path as standard", key=f"sp_set_{pick}"):
            store(d["paths"][chosen]["path"], [], False, "Standard saved (not yet confirmed by the business).")
        if b2.button("Confirm as business standard", key=f"sp_confirm_{pick}"):
            store(standard["path"], standard.get("mandatory_actions") or [], True, "Confirmed as the business standard.")
        if st.session_state.get("sp_flash"):
            st.success(st.session_state.pop("sp_flash"))
        with st.expander("Edit standard"):
            edited = st.data_editor(
                pd.DataFrame([{"Order": i, "Action": a, "Mandatory": a in (standard.get("mandatory_actions") or [])}
                              for i, a in enumerate(standard["path"], 1)]),
                key=f"sp_editor_{pick}_{version}", num_rows="dynamic", hide_index=True, width="stretch")
            confirmed = st.checkbox("Confirmed by the business", value=bool(standard.get("confirmed")),
                                    key=f"sp_confirmed_{pick}_{version}")
            if st.button("Save", key=f"sp_save_{pick}", type="primary"):
                rows = sorted((r for r in edited.to_dict("records") if str(r.get("Action") or "").strip()),
                              key=lambda r: float(r.get("Order") or 0))
                store([str(r["Action"]).strip() for r in rows],
                      [str(r["Action"]).strip() for r in rows if r.get("Mandatory")], confirmed, "Standard saved.")

    # 6. Comparison
    st.markdown(f"**{comp['wording']}**")
    classes = pd.DataFrame([{"Class": c["label"], "Share of sittings %": round(100 * c["share"], 1), "Work sittings": c["runs"],
                             "Median duration": format_hours(c["median_duration_h"])} for c in comp["classes"]])
    st.bar_chart(classes.set_index("Class")[["Share of sittings %"]])
    st.dataframe(classes, hide_index=True, width="stretch")
    t1, t2 = st.columns(2)
    with t1:
        st.markdown("Top missing actions")
        st.dataframe(pd.DataFrame([{"Action": x["action"], "Share of sittings %": round(100 * x["share"], 1)}
                                   for x in comp["top_missing"]] or [{"Action": "none", "Share of sittings %": 0.0}]),
                     hide_index=True, width="stretch")
    with t2:
        st.markdown("Top extra actions")
        st.dataframe(pd.DataFrame([{"Action": x["action"], "Share of sittings %": round(100 * x["share"], 1)}
                                   for x in comp["top_extra"]] or [{"Action": "none", "Share of sittings %": 0.0}]),
                     hide_index=True, width="stretch")

    # 7. Top paths
    st.markdown("**Top paths**")
    paths = pd.DataFrame([{"Path": " → ".join(p["path"]), "Work sittings": p["runs"], "Share %": round(100 * p["share"], 1),
                           "Cumulative %": round(100 * p["cumulative_share"], 1),
                           "Median duration": format_hours(p["median_duration_h"])} for p in d["paths"]])
    st.dataframe(paths, hide_index=True, width="stretch")

    # 8. Apps and switches
    st.markdown("**Apps used in this step**")
    if d["apps"]:
        st.dataframe(pd.DataFrame([{"Application": a["application"], "Share of events %": round(100 * a["share"], 1)}
                                   for a in d["apps"]]), hide_index=True, width="stretch")
    else:
        st.caption("No application recorded.")
    if d.get("switches"):
        st.caption(f"Application switches inside work sittings: {d['switches']['total']:,} "
                   f"({d['switches']['per_run']:.2f} per sitting).")

    # 9. Variation by user
    with st.expander("Variation by user", expanded=False):
        st.caption(sp.USER_NOTE)
        if d["by_user"]:
            st.dataframe(pd.DataFrame([{"User": u["user"], "Work sittings": u["runs"],
                                        "Median duration": format_hours(u["median_duration_h"]),
                                        "Standard %": round(100 * u["standard_share"], 1)} for u in d["by_user"]]),
                         hide_index=True, width="stretch")
        else:
            st.caption(f"No user has {settings()['subprocess']['min_user_runs']} or more runs of this step.")

    # 10. Downloads
    c1, c2 = st.columns(2)
    c1.download_button("Download action map (DOT)", dot, file_name=f"action_map_{pick}.dot", mime="text/vnd.graphviz",
                       key="sp_dot_download")
    detail = pd.concat([classes.assign(Table="Comparison"), paths.assign(Table="Top paths")], ignore_index=True)
    c2.download_button("Download step detail (CSV)", detail.to_csv(index=False), file_name=f"step_detail_{pick}.csv",
                       mime="text/csv", key="sp_csv_download")
