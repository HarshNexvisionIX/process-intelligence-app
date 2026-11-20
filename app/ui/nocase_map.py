"""Clickstream map (no case ID): the candidate process map with its status legend, the step table, the step x
application matrix, Level 4/5 per step, evidence, and "inside each step" for a chosen step."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.ui import shell

from app.core import fmt, jobs

shell.header("Process map", "The steps in business order, with hours and volumes from the data. Arrows "
             "show the documented order and how often one step follows another within a work sitting.", "nc_map")
nc_jobs = [j for j in jobs.list_jobs() if j.get("mode") == "nocase"]
if not nc_jobs:
    st.info("No analysis without a case ID yet: import a file first.")
    st.stop()
job_id = shell.pick_job(nc_jobs, "nc_map", "run the clickstream analysis first")
if job_id is None:
    st.stop()
folder = jobs.job_folder(job_id)

from app.nocase.engine import activities as act  # noqa: E402
from app.nocase.engine.confidence import load as load_conf  # noqa: E402
from app.nocase.engine.flow import drawn_edges, load as load_flow  # noqa: E402
from app.nocase.engine.inside_step import load as load_inside  # noqa: E402
from app.nocase.engine.measures import load as load_measures  # noqa: E402

m, flow, conf = load_measures(folder), load_flow(folder), load_conf(folder)
if not m:
    st.info("This job has no measures yet: run the analysis on **Overview and analysis**.")
    st.stop()
job = jobs.load_job(job_id)
process = job.get("process")
from app.nocase import library as _lib  # noqa: E402
order = _lib.step_order(_lib.load_pack(process)) if process else []
steps_m = [s for s in m["steps"] if s["step"] != "Unassigned"]
unassigned = next((s for s in m["steps"] if s["step"] == "Unassigned"), None)
total_h = float(m["overall"]["hours"]) or 1.0
linked = sum(s["hours"] for s in steps_m)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Hours in scope", fmt.hours(m["overall"]["hours"]))
c2.metric("Linked to a step", f"{100 * linked / total_h:.0f}%")
c3.metric("Process steps seen", f"{sum(1 for x in steps_m if x['step'] in order)} of {len(order)}" if order
          else f"{len(steps_m)}", help="Steps of the process (Business context / Process library) that have hours "
          "in this data.")
c4.metric("People", fmt.count(m["overall"]["actors"]))
st.caption(f"Time basis: {m['basis']['time']} · {fmt.count(m['overall']['events'])} events"
           + (f" · process: {_lib.display_name(process)}" if process else ""))
if unassigned and unassigned["share_of_hours"] > 0.3:
    st.warning(f"**{100 * unassigned['share_of_hours']:.0f}% of the hours are not linked to a step yet**, so the map "
               "shows only part of the process. Link the clicks to steps on **Review steps** (accept the proposed "
               "rules, or assign the largest unassigned screens), then run the analysis again on **Overview and "
               "analysis**.", icon=":material/link_off:")
    st.page_link(shell.st_page("nc_steps"), label="Go to Review steps", icon=":material/arrow_forward:")

st.subheader("Map")
if flow:
    from app.nocase.engine.flow import dot as _dot  # noqa: E402
    _seen = {n["step"] for n in flow["nodes"]}
    _stage = _lib.step_stage(_lib.load_pack(process)) if process else {}
    _flow = dict(flow, missing=[{"step": s, "stage": _stage.get(s)} for s in order if s not in _seen])
    _n = len(flow["nodes"]) + len(_flow["missing"])
    st.graphviz_chart(_dot(_flow, order or None), use_container_width=False)   # natural size: never blown up
    st.caption("Boxes = steps in business order, grouped by stage, with their hours · grey dashed box = a documented "
               "step not seen in the data yet · arrow style: bold = order confirmed by the business, dotted = "
               "documented order not confirmed, dashed blue = one step followed by another inside a person's work "
               "sitting (with how often).")
    extra = drawn_edges(flow)
    if extra:
        with st.expander(f"Other transitions seen in the data ({len(extra)})"):
            st.dataframe(pd.DataFrame([{"From": e["src"], "To": e["dst"], "How": e["status"], "Times": e["count"],
                                        "People": e.get("actors")} for e in extra]), hide_index=True, width="stretch")

st.subheader("Steps")
st.caption("Hours and volumes per step. Unassigned = clicks not linked to any step yet.")
steps = conf.get("steps") or {}
st.dataframe(pd.DataFrame([{
    "Step": s["step"], "Hours": round(s["hours"], 1), "Share %": round(100 * s["share_of_hours"], 1), "People": s["actors"],
    "Step runs": s["runs"], "Median run": fmt.duration_s(s["median_run_s"]), "Repeated %": round(100 * s["repetition_share"], 1),
    "Top apps": ", ".join(a["app"] for a in s["top_apps"]), "Confidence": steps.get(s["step"], {}).get("band", "–")}
    for s in m["steps"]]), hide_index=True, width="stretch")

mx = m["matrix_step_app"]
with st.expander("Step × application hours"):
    t = pd.DataFrame(mx["cells"], index=mx["rows"], columns=mx["columns"]).round(2)
    t["Total"] = [round(x, 2) for x in mx["row_totals"]]
    st.dataframe(t, width="stretch")

st.subheader("Inside a step")
choices = [s["step"] for s in m["steps"] if s["step"] != "Unassigned"]
if choices:
    step = st.selectbox("Step", choices, key="ncmap_step")
    a = act.load(folder)
    if len(a):
        st.dataframe(a[a["step"] == step][["activity", "events", "hours", "actors", "source", "status"]],
                     hide_index=True, width="stretch")
    inside = next((x for x in (load_inside(folder).get("steps") or []) if x["step"] == step), None)
    if inside and not inside.get("enough_runs"):
        st.info(inside["message"])
    elif inside:
        st.graphviz_chart(inside["dot"], use_container_width=True)
        st.caption(f"{inside['runs']:,} step runs · usually starts with \"{inside['start_activity']}\" and ends with "
                   f"\"{inside['end_activity']}\" · rework inside the step (after a 30+ min pause) in {fmt.pct(inside['rework_share'])} of runs · back-and-forth in {fmt.pct(inside.get('back_and_forth_share') or 0)} of runs · "
                   "status: inferred")
        st.dataframe(pd.DataFrame(inside["patterns"]), hide_index=True, width="stretch")
        st.dataframe(pd.DataFrame(inside["time_split"]), hide_index=True, width="stretch")
        if inside["variation"]["flagged"]:
            st.warning("People do this step differently: " + "; ".join(inside["variation"]["reasons"]))
        if inside["automation_candidates"]:
            st.caption("Candidates for SME review (facts only): " + "; ".join(
                f"{x['activity']} ({x['events']:,} events, same pattern in {fmt.pct(x['pattern_share'])} of runs)"
                for x in inside["automation_candidates"]))
