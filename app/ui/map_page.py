"""Page 3: the process map and all analyses, with how far they can be trusted.

Shows, top to bottom:
    A. Pick the job          (the one in progress is pre-selected)
    B. Trust banner          what the quality band allows
    C. Run or reuse          runs the mining engine once; mining.json is reused after that
    D. Headline numbers      cases, events, activities, case duration
    E. Tabs                  map, variants, bottlenecks, rework, resources, trends

Nothing heavy is computed here: the page reads mining.json and only calls the
plain helpers (simplify, to_dot, format_hours) to draw the map.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from app.ui import shell

from app.core import derived, fmt, jobs, privacy, reference
from app.core.narrative import ai_status, suggest_order_ai
from app.core.report import map_html
from app.core.config import settings
from app.core.db import connect
from app.core.mining import (
    describe_switching, format_hours, run_mining, save_mining, simplify, slowest_edges, to_app_dot, to_dot,
)

CFG = settings()["mining"]

shell.header("Process analysis", "How cases really flow through the process, where they wait and where work is repeated.", "analysis")


@st.cache_data(show_spinner=False)
def load_json(path: str, modified: float) -> dict:
    """Read a result file once per version (`modified` changes when the file is rewritten)."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read(path: Path) -> dict:
    return load_json(str(path), path.stat().st_mtime)


def table(rows: list[dict], empty: str = "None found.") -> None:
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption(empty)


def shown(section: dict) -> bool:
    """True when the section can be shown; otherwise say why and show nothing else."""
    if not section["available"]:
        st.info(section["reason"])
        return False
    return True


def support_mark(item: dict) -> str:
    return "low support" if item.get("low_support") else ""


# --------------------------------------------------------------------------- #
# A. Pick the job
# --------------------------------------------------------------------------- #
ready = [j for j in jobs.list_jobs() if jobs.reached(j, "quality_checked")]
if not ready:
    st.info("Run the data quality check first (step 2).")
    st.stop()

job_id = shell.pick_job(ready, "analysis", "run the data quality check first")
if job_id is None:
    st.info("Run the data quality check first.")
    shell.next_link(shell.current_job())
    st.stop()
job = jobs.load_job(job_id)
if job.get("mode") == "nocase":   # the clickstream workflow has its own pages
    st.info("The current analysis has no case ID: its pages are in the Clickstream workflow.")
    st.stop()
folder = jobs.job_folder(job_id)
band = job.get("band")
quality_path = folder / "quality.json"
from app.core.ingest import PROTECT_CASE_IDS, case_ids_protected   # noqa: E402
if (folder / "events.parquet").exists() and not case_ids_protected(folder / "events.parquet"):
    st.warning(PROTECT_CASE_IDS + ": this job was converted before case IDs were stored as salted hashes, so its "
               "files still hold the original case IDs. Convert it again on Import data; old files are not rewritten.")

# --------------------------------------------------------------------------- #
# B. Trust banner
# --------------------------------------------------------------------------- #
if band not in CFG["band_rules"]:
    st.warning("This job has no quality result. Run the data quality check again.")
    st.stop()
if band == "Not feasible":
    st.error("This data did not pass the quality gate, so no process map is drawn.")
    if quality_path.exists():
        fixes = read(quality_path).get("fixes") or []
        if fixes:
            st.markdown("**Fix these first** (from Data quality):")
            for i, fx in enumerate(fixes[:3], 1):
                st.markdown(f"{i}. **{fx['check']}**: {fx['problem']}  \n   → {fx['fix']}")
    st.stop()
label = CFG["band_rules"][band]["label"]
if band == "Low":
    st.warning(label)
elif band == "Medium":
    st.info(label)
else:
    st.caption(f":green-badge[:material/check: Data quality: {band}]")

# --------------------------------------------------------------------------- #
# C. Run the analysis, or reuse the saved one
# --------------------------------------------------------------------------- #
saved = folder / "mining.json"
if st.button("Run analysis", type="primary"):
    with st.status("Analysing all events...") as status:
        result = run_mining(connect(), folder / "events.parquet", band,
                            progress=lambda msg: status.update(label=msg))   # Level 4 may call the AI per step
        status.update(label="Analysis complete", state="complete")
        save_mining(result, folder)
    job.update(status="mined", mined_at=result["created_at"],
               flow_band=(result["flow_confidence"]["data"] or {}).get("band"))
    jobs.save_job(job)

if not saved.exists():
    st.info("Click **Run analysis** to build the process map.")
    st.stop()
m = read(saved)

# A saved analysis is out of date when the quality check ran again after it
# (file times are used: they are far more precise than the saved timestamps).
if m["band"] != band or not jobs.is_newer(job_id, "mining.json", "quality.json"):
    st.warning("The quality check ran again after this analysis"
               + (f" (quality is now {band}, was {m['band']})" if m["band"] != band else "")
               + ". Click **Run analysis** to update the results.")
    st.stop()

events = m["counts"].get("events", 0)
st.caption(f"Last run {m['created_at'][:16].replace('T', ' ')} UTC · {events:,} events in {m['seconds']} s")
job_mapping = derived.mapping_of(folder)
if derived.case_kind(job_mapping) != "column" or job_mapping.get("derived_activity"):
    st.info(" ".join(x for x in (
        derived.case_label(job_mapping) + ".",
        {"work_session": derived.WORK_SESSION_NOTE, "time_window": derived.TIME_WINDOW_NOTE}.get(
            derived.case_kind(job_mapping), ""),
        derived.SCREEN_NOTE if job_mapping.get("derived_activity") else "") if x))

# --------------------------------------------------------------------------- #
# D. Headline numbers
# --------------------------------------------------------------------------- #
if shown(m["kpis"]):
    k = m["kpis"]["data"]
    c = st.columns(5)
    c[0].metric("Cases", f"{k['cases']:,}")
    c[1].metric("Events", f"{k['events']:,}")
    c[2].metric("Activities", f"{k['activities']:,}")
    c[3].metric("Median case duration", format_hours(k["median_h"]))
    c[4].metric("90% of cases finish within", format_hours(k["p90_h"]))

# --------------------------------------------------------------------------- #
# E. Tabs
# --------------------------------------------------------------------------- #
tab_map, tab_sub, tab_inside, tab_var, tab_bot, tab_rew, tab_res, tab_app, tab_trend = st.tabs(
    ["Process map", "Sub-processes", "Inside each step", "Variants", "Bottlenecks", "Rework", "Resources",
     "Applications", "Trends"])

def detailed_map(g: dict) -> None:
    """The detailed map: every transition, simplified with the sliders (unchanged from before)."""
    c1, c2, c3 = st.columns(3)
    mode = c1.radio("Show", ["Frequency", "Time"], horizontal=True, key="map_mode",
                    help="Frequency: how often. Time: how long cases wait between steps.")
    acts = c2.slider("Activities shown", 10, 100, round(100 * CFG["default_activity_coverage"]),
                     format="%d%%", key="map_acts",
                     help="Keeps the most frequent activities until they cover this share of events.")
    arrows = c3.slider("Arrows shown", 10, 100, round(100 * CFG["default_edge_coverage"]),
                       format="%d%%", key="map_arrows",
                       help="Keeps the most frequent arrows until they cover this share of transitions.")
    nodes, edges, hidden = simplify(g["nodes"], g["edges"], acts / 100, arrows / 100, CFG["max_nodes"])
    slow = slowest_edges(edges) if mode == "Time" else set()
    dot = to_dot(nodes, edges, mode.lower(), slow)
    st.graphviz_chart(dot, width="stretch")

    special = {g["start_node"], g["end_node"]}
    n_total = sum(1 for n in g["nodes"] if n["activity"] not in special)
    n_shown = sum(1 for n in nodes if n["activity"] not in special)
    note = (f"Showing {n_shown} of {n_total} activities ({hidden['events_shown_share']:.0%} of events) "
            f"and {len(edges)} of {len(g['edges'])} arrows "
            f"({hidden['transitions_shown_share']:.0%} of transitions).")
    if band == "Low":
        note += " Exploratory map — do not use for decisions."
    st.caption(note)
    if g.get("overlaps"):
        st.caption(f"{g['overlaps']:,} transitions overlap (the next step started before the "
                   "previous one ended); their wait counts as 0.")
    if mode == "Time":
        slow_edges = sorted((e for e in edges if (e["src"], e["dst"]) in slow),
                            key=lambda e: -e["median_wait_h"])
        if slow_edges:
            st.markdown("**Slowest arrows on this map** (highlighted):")
            for e in slow_edges:
                st.markdown(f"- {e['src']} → {e['dst']}: median wait {format_hours(e['median_wait_h'])} "
                            f"({e['cases']:,} cases)")
    st.download_button("Download map (DOT)", dot, file_name=f"process_map_{job_id}.dot",
                       mime="text/vnd.graphviz")


SIMPLE, DETAILED = "Simple (intended order)", "Detailed (all transitions)"
SITTING_NOTE = ('"Step runs" are consecutive events of one step in a case (the map); "work sittings" split them by person and by pauses over 30 minutes, so a step has at least as many sittings as step runs.')
MODEL = "Discovered model (PM4Py)"


@st.cache_data(show_spinner=False)
def reference_for(events: str, modified: float, key: str, order_json: str | None, use_saved: bool,
                  name: str) -> dict:
    """The step order and its numbers, cached per job file and order (the analysis is not re-run)."""
    order = reference.Order.from_dict(json.loads(order_json)) if order_json else None
    return reference.compute(connect(), Path(events), key, name, order, use_saved=use_saved)


def simple_map() -> None:
    """The simple map: the steps, how many cases reach each, and how far an order can be trusted.

    The order comes from the most trusted source available (confirmed > template > AI >
    data), shown with a badge; the flow confidence decides the title and whether
    arrows are drawn. The order can be edited, uploaded, confirmed and saved per layout.
    """
    key = reference.order_key(job)
    events = folder / "events.parquet"
    name = Path(job.get("source_name") or "Process").stem or "Process"
    mtime = max(events.stat().st_mtime, (folder / "milestones.json").stat().st_mtime if (folder / "milestones.json").exists() else 0)
    with st.spinner("Comparing the cases with the step order..."):
        base = reference_for(str(events), mtime, key, None, True, name)
    step_events = {r["step"]: r["events"] for r in base["stats"]["steps"] + base["stats"]["other"]}
    ms = (m.get("milestones") or {}).get("data")
    from app.core import order_evidence
    options = reference.order_options(key, reference.Order.from_dict(base["suggested"]), step_events, name, ms,
                                      order_evidence.load(folder))
    note = reference.notice(options, job)
    if note:
        st.info(note)

    # 0. Where the order comes from
    pick_key = f"order_source_{job_id}"
    if st.session_state.get(pick_key, 0) >= len(options):
        st.session_state.pop(pick_key)
    # One rule for the whole app (reference.resolve): the pick saved in job.json, else the most trusted option.
    default = options.index(reference.resolve(options, job))
    pick = st.selectbox("Order source", range(len(options)), key=pick_key, index=default,
                        format_func=lambda i: options[i]["label"],
                        help="Most trusted first: confirmed by the business, a saved order, your documents (evidence), an "
                             "AI suggestion (step names only), milestones, or the order found in the data. The "
                             "report uses the same choice.")
    chosen = options[pick]
    # Remember a pick that differs from the default rule, so the report and the facts use it too.
    want = None if chosen["key"] == options[0]["key"] else chosen["key"]
    if job.get("order_choice") != want or job.get("order_source") != chosen["label"]:
        job.pop("order_choice", None)
        if want:
            job["order_choice"] = want
        job["order_source"] = chosen["label"]
        jobs.save_job(job)
    with st.spinner("Comparing the cases with the step order..."):
        ref = reference_for(str(events), mtime, key, json.dumps(chosen["order"].to_dict()), False, name)
    order, stats, flow = reference.Order.from_dict(ref["order"]), ref["stats"], ref["flow"]
    if job.get("flow_band") != flow["band"]:
        job["flow_band"] = flow["band"]
        jobs.save_job(job)

    # 1. What this is, and how far the order can be trusted
    months = " to ".join(m for m in (stats["first_month"], stats["last_month"]) if m)
    st.markdown(f"**{order.process_name}** · {stats['cases']:,} cases · {months}")
    label = stats["order_strength_label"]
    badges = [f":{chosen['color']}-badge[{chosen['label']}]",
              f":{ {'High': 'green', 'Medium': 'blue', 'Low': 'orange'}.get(flow['band'], 'red')}-badge"
              f"[Flow confidence: {flow['score']} {flow['band']}]",
              f":{ {'Strong': 'green', 'Partial': 'orange'}.get(label, 'red')}-badge[Order strength: {label}]"]
    if order.template_name and not derived.mapping_of(folder).get("stage"):
        badges.append(f":blue-badge[Stages from template: {order.template_name}]")
    st.markdown(" ".join(badges), help="; ".join(flow["reasons"]).capitalize() + ". Separate from the data-quality "
                f"score. Order strength {stats['order_strength']:.2f} (0 = no fixed order, 1 = always the same order).")
    if flow.get("explain"):
        st.caption(flow["explain"])
    if not order.confirmed_by_business:
        st.caption("Wrong order? Correct it in **Correct the intended flow** below the map, or build it from your "
                   "documents on Process definition (Proposed order).")
    if reference_ai_available() and not any(o["key"] == "ai" for o in options):
        if st.button("Ask the AI to suggest an order", key="order_ai",
                     help="Sends only the step and stage names (never counts or data)."):
            with st.spinner("Asking the AI (step names only)..."):
                stages = [s["name"] for s in reference.Order.from_dict(base["suggested"]).stages if s["name"]]
                ai_order = suggest_order_ai(job_id, list(step_events), stages, name)
            if ai_order is None:
                st.info("The AI did not give a usable order; nothing changed.")
            else:
                reference.save_order(reference.ai_order_key(key), ai_order)
                st.session_state.pop(pick_key, None)
                st.rerun()

    # 2. The map
    show_bf = st.checkbox("Show back-and-forth", value=True, key="ref_back_and_forth")
    dot = reference.to_reference_dot(order, stats, show_bf, flow)
    st.graphviz_chart(dot, width="stretch")

    # 3. How to read it, and how reality compares with the order
    st.caption(reference.LEGEND)
    if stats["other_steps"]["names"]:
        st.caption("Other steps (not drawn): " + ", ".join(stats["other_steps"]["names"]) + ".")
    for line in reference.describe_reference(order, stats):
        st.markdown(f"- {line}")
    with st.container(border=True):
        st.markdown("**How reality compares**")
        for line in reference.describe_conformance(order, stats, flow):
            st.markdown(f"- {line}")
    if band == "Low":
        st.caption("Exploratory map — do not use for decisions.")

    # 4. Correct the intended flow: move steps, rename stages, hide steps, confirm and save (never re-runs the
    #    analysis). Open while the order is not confirmed, so it is easy to find.
    with st.expander("Correct the intended flow (step order)", expanded=not order.confirmed_by_business):
        drawn = order.drawn_steps()
        if drawn:
            m1, m2, m3, m4 = st.columns([4, 1, 1, 3])
            mv = m1.selectbox("Move a step", drawn, key=f"ref_move_step_{st.session_state.get('ref_version', 0)}")
            if m2.button("▲ Earlier", key="ref_move_up", disabled=drawn.index(mv) == 0):
                reference.save_order(key, reference.move_step(order, mv, -1))
                job.pop("order_choice", None)
                jobs.save_job(job)
                st.session_state["ref_flash"] = f"{mv} moved one place earlier (saved, not yet confirmed)."
                st.session_state["ref_version"] = st.session_state.get("ref_version", 0) + 1
                st.rerun()
            if m3.button("▼ Later", key="ref_move_down", disabled=drawn.index(mv) == len(drawn) - 1):
                reference.save_order(key, reference.move_step(order, mv, 1))
                job.pop("order_choice", None)
                jobs.save_job(job)
                st.session_state["ref_flash"] = f"{mv} moved one place later (saved, not yet confirmed)."
                st.session_state["ref_version"] = st.session_state.get("ref_version", 0) + 1
                st.rerun()
            m4.caption("Or edit the table below: type positions, stage names, untick 'Show on map'; tick "
                       "'confirmed by the business' and Save. The report uses the saved order.")
        if st.session_state.get("ref_flash"):
            st.success(st.session_state.pop("ref_flash"))
        version = st.session_state.get("ref_version", 0)
        new_name = st.text_input("Process name", value=order.process_name, key=f"ref_name_{version}")
        edited = st.data_editor(
            pd.DataFrame(reference.order_rows(order)), key=f"ref_editor_{version}", hide_index=True,
            width="stretch", disabled=["Step"],
            column_config={"Position": st.column_config.NumberColumn("Position", step=1),
                           "Show on map": st.column_config.CheckboxColumn("Show on map")})
        st.caption("Steps with 'Show on map' unticked go into Other steps. Steps with the same stage are "
                   "drawn together.")
        confirmed = st.checkbox("This order is confirmed by the business", value=order.confirmed_by_business,
                                key=f"ref_confirmed_{version}")

        def store(new_order, message: str) -> None:
            reference.save_order(key, new_order)
            st.session_state.pop(pick_key, None)          # the saved order is now the first option
            job.pop("order_choice", None)                 # ... and the one the report uses
            jobs.save_job(job)
            st.session_state["ref_flash"] = message
            st.session_state["ref_version"] = version + 1
            st.rerun()

        c1, c2, _ = st.columns([2, 2, 5])
        if c1.button("Use suggested order", key="ref_use_suggested"):
            st.session_state[pick_key] = next(i for i, o in enumerate(options) if o["key"] == "data")
            job["order_choice"] = "data"
            jobs.save_job(job)
            st.session_state["ref_version"] = version + 1
            st.rerun()
        if c2.button("Save order", type="primary", key="ref_save"):
            rows = edited.to_dict("records")
            errors = reference.validate_rows(rows)
            for e in errors:
                st.error(e)
            if not errors:
                store(reference.order_from_rows(rows, new_name, confirmed, base=order),
                      "Order saved. It is used for every file with this layout.")
        st.download_button("Download order (CSV)", reference.order_to_csv(order), file_name=f"order_{job_id}.csv",
                           mime="text/csv", key="order_csv_download",
                           help="position, stage, step: send it to the process owner to check or correct.")
        uploaded = st.file_uploader("Upload order (CSV: position, stage, step)", type=["csv"],
                                    key=f"order_csv_{version}")
        if uploaded is not None:
            up_order, problems = reference.order_from_csv(uploaded.getvalue(), list(step_events), new_name,
                                                          confirmed, base=order)
            for problem in problems:
                if up_order is None:
                    st.error(problem)
                else:
                    st.warning(problem)
            if up_order is not None and st.button("Save uploaded order", key="order_csv_save"):
                store(up_order, "Uploaded order saved. It is used for every file with this layout.")

    # 5. Downloads
    d1, d2, d3 = st.columns(3)
    d1.download_button("Download map (DOT)", dot, file_name=f"simple_map_{job_id}.dot",
                       mime="text/vnd.graphviz", key="ref_dot_download")
    d2.download_button("Download map (HTML)",
                       map_html(dot, reference.map_title(order, stats, flow),
                                [reference.LEGEND, *reference.describe_reference(order, stats),
                                 *reference.describe_conformance(order, stats, flow)]),
                       file_name=f"simple_map_{job_id}.html", mime="text/html", key="ref_html_download",
                       help="One file that opens in any browser, offline, and prints to PDF.")
    d3.download_button("Download step table (CSV)", reference.step_table(order, stats).to_csv(index=False),
                       file_name=f"process_steps_{job_id}.csv", mime="text/csv", key="ref_csv_download")


def process_model_view() -> None:
    """The BPMN model PM4Py discovered from the step runs, its fit, precision and deviations, and a download."""
    from app.core import pm4py_model
    sec = m.get("process_model") or {"available": False,
                                     "reason": "Not available: run the analysis again to discover the model."}
    if not sec["available"]:
        st.info(sec["reason"])
        if pm4py_model.status()[0] and st.button("Build model", type="primary", key="pm4py_build"):
            with st.spinner("Building the process model with PM4Py..."):
                pm4py_model.build(folder)
            st.rerun()
        return
    d = sec["data"]
    conf = pm4py_model.load_json(folder, pm4py_model.CONFORMANCE_FILE) or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cases used", f"{d['cases_used']:,}", help=f"of {d['cases_total']:,} cases (a fixed sample above "
                                                          "pm4py.max_cases)")
    if d.get("fitness"):
        c2.metric("Fitness", f"{d['fitness']['log_fitness']:.2f}",
                  help="Token-based replay: 1.00 = the model replays every case. Lower = the model is simpler than "
                       "what people really do (noise filtered out).")
        c4.metric("Cases that fit fully", f"{100 * d['fitness']['fitting_cases_share']:.1f}%")
    c3.metric("Precision", f"{d['precision']:.2f}" if d.get("precision") is not None else "–",
              help="1.0 = the model allows only what really happens; low = the model allows many paths nobody uses. "
                   f"Measured on {conf.get('precision_cases', 0):,} sampled cases."
                   + (f" Not available: {conf['precision_note']}." if conf.get("precision_note") else ""))
    show = d.get("show_model", True)
    if not show:
        st.info(d.get("structure_note") or pm4py_model.no_order_message(d.get("precision")))
        show = st.toggle("Show the model anyway", key="pm4py_show_anyway")
    if show:
        st.graphviz_chart(d["dot"], width="stretch")
    g = d.get("gateways") or {}
    st.caption(f"× = one of the paths (exclusive choice, {g.get('exclusive', 0)}) · + = paths done in any order or "
               f"in parallel ({g.get('parallel', 0)}) · built from {d['runs_used']:,} step runs with the Inductive "
               f"Miner, noise threshold {d['noise_threshold']:g} (pm4py.noise_threshold: higher = simpler model). "
               f"PM4Py {d['pm4py_version']} · {d.get('seconds', 0):.0f} s.")
    st.caption("Precision: 1.0 = the model allows only what really happens; low = the model allows many paths "
               "nobody uses.")
    if conf.get("histogram"):
        st.markdown("**How well each case fits** (token-based replay fitness per case)")
        st.bar_chart(pd.DataFrame([{"Fitness": h["bucket"], "Share of cases %": round(100 * h["share"], 1)}
                                   for h in conf["histogram"]]).set_index("Fitness"))
    dev = conf.get("deviations") or {}
    if dev:
        st.markdown(f"**Top deviations** — based on {dev.get('sampled_cases', 0):,} cases ({conf.get('method', 'alignments')})")
        if conf.get("method", "alignments") != "alignments":
            rows = [{"Step": x["step"],
                     "Done but not expected (% of cases)": round(100 * x.get("done_not_expected", x["share"]), 1),
                     "Expected but not done (% of cases)": round(100 * x.get("expected_not_done", 0.0), 1)}
                    for x in (dev.get("token_problems") or [])[:10]]
            if rows:
                st.caption("Token replay: per step, the cases where the step was done when the model did not expect it, "
                           "and the cases that ended while the model still expected it.")
                table(rows)
            else:
                st.info("No deviations found by token replay.")
        else:
            left, right = st.columns(2)
            with left:
                st.markdown("Skipped steps (the model expected the step, the case did not do it)")
                table([{"Step": x["step"], "Share of sampled cases %": round(100 * x["share"], 1)}
                       for x in dev.get("skipped", [])[:5]], "None.")
            with right:
                st.markdown("Extra steps (the case did a step the model did not expect)")
                table([{"Step": x["step"], "Share of sampled cases %": round(100 * x["share"], 1)}
                       for x in dev.get("extra", [])[:5]], "None.")
    order_check_view()
    st.caption(d.get("licence", ""))
    bpmn = folder / d["bpmn_file"]
    if bpmn.exists():
        st.download_button("Download model (BPMN 2.0)", bpmn.read_bytes(), file_name=f"process_model_{job_id}.bpmn",
                           mime="application/xml", key="bpmn_download",
                           help="Opens in Signavio, Camunda Modeler, bpmn.io or Visio (BPMN import).")


def order_check_view() -> None:
    """PM4Py: every case replayed on the step order page 3 and the report use (e.g. the HR template)."""
    from app.core import pm4py_model
    st.markdown("**Check the cases against the step order**")
    st.caption("Replays every case on the step order chosen above (the same order the report uses): how well the "
               "cases follow it, and per step how often it is skipped, done out of order or repeated.")
    if st.button("Check the cases against the step order", key="pm4py_order_check"):
        with st.spinner("Replaying the cases on the step order with PM4Py (up to a few minutes)..."):
            res = pm4py_model.check_order(jobs.load_job(job_id))
        if not res.get("available"):
            st.warning(res.get("reason"))
    res = pm4py_model.load_json(folder, pm4py_model.ORDER_FILE)
    if not res:
        st.caption("Not run yet.")
        return
    st.caption(f"Order: {res.get('order_badge') or '–'} · {res['cases_used']:,} cases · {res['seconds']:.0f} s")
    a, b, c = st.columns(3)
    a.metric("Fitness against the order", f"{res['fitness']:.2f}",
             help="Token-based replay: 1.00 = every case follows the order.")
    b.metric("Precision", f"{res['precision']:.2f}" if res.get("precision") is not None else "–",
             help="1.0 = the order allows only what really happens; low = it allows paths nobody uses.")
    c.metric("Cases that follow it fully", f"{100 * res['fitting_cases_share']:.1f}%")
    for note in res.get("notes") or []:
        st.caption(note)
    st.caption(f"Per step, based on {res['sampled_cases']:,} sampled cases ({res['method']}).")
    table([{"Step": x["step"], "Skipped %": round(100 * x["skipped_share"], 1),
            "Out of order %": round(100 * x["out_of_order_share"], 1),
            "Repeated / extra %": round(100 * x["extra_share"], 1)} for x in res["steps"]])
    if res.get("not_in_order"):
        st.caption("Not in the order: " + ", ".join(f"{x['activity']} ({x['cases']:,} cases)"
                                                     for x in res["not_in_order"]))
    st.caption(res["caveat"])


def reference_ai_available() -> bool:
    """Whether the AI may be asked for an order (switched on, configured, and allowed by the privacy guard)."""
    return ai_status()[0] and privacy.ai_allowed()[0]


with tab_map:
    overview = m.get("overview") or {}
    if not m["map"]["available"] and overview.get("available"):
        o = overview["data"]
        st.info("No case: activity overview only. The process map, variants, waiting times and conformance "
                "need a case.")
        st.markdown(f"**{o['events']:,} events** by **{o['users']:,} users**")
        table([{"Activity": a["activity"], "Events": a["events"], "Users": a["users"]} for a in o["activities"]])
        if o["by_hour"]:
            st.markdown("**Events by hour of day**")
            st.bar_chart(pd.DataFrame(o["by_hour"]).set_index("hour"))
        st.markdown("**Step runs** (consecutive events of one step by one user)")
        table([{"Activity": r["activity"], "Runs": r["runs"], "Median events per run": r["median_events"],
                "Median minutes": r["median_minutes"]} for r in o["step_runs"]])
    elif shown(m["map"]):
        ms = (m.get("milestones") or {}).get("data")
        view = st.radio("View", [SIMPLE, DETAILED] + (["Milestone map"] if ms else []) + [MODEL], horizontal=True,
                        key="map_view",
                        help="Simple: the intended order of steps, and how many cases reach each. "
                             "Detailed: every transition between steps. Discovered model: a BPMN model mined "
                             "from the data with PM4Py's Inductive Miner.")
        if view == MODEL:
            process_model_view()
        elif view == "Milestone map":
            from app.core.milestones import to_dot as milestone_dot
            st.graphviz_chart(milestone_dot(ms))
            st.caption(f"Based on {ms['coverage']:.0%} of cases that record milestone actions.")
        elif view == DETAILED:
            detailed_map(m["map"]["data"])
        else:
            simple_map()
            if ms:
                from app.core.milestones import sequence_lines
                st.markdown("**Confirmed sequences found in the data**")
                for line in sequence_lines(ms):
                    st.write(line)
                from app.ui.milestones_editor import render as milestone_editor
                milestone_editor(job)

with tab_sub:
    from app.ui.subprocess_tab import render as render_subprocesses
    render_subprocesses(job, folder, m)

with tab_var:
    if shown(m["variants"]):
        v = m["variants"]["data"]
        a, b = st.columns(2)
        a.metric("Total variants", f"{v['total_variants']:,}")
        b.metric("Top 10 cover", f"{v['coverage_top10']:.0%}")
        rows = [{"Rank": x["rank"], "Cases": x["cases"], "Share %": round(100 * x["share"], 1),
                 "Cumulative %": round(100 * x["cumulative_share"], 1),
                 "Path": " → ".join(x["path"]), "Median duration": format_hours(x["median_duration_h"])}
                for x in v["variants"]]
        table(rows[:20])
        if len(rows) > 20:
            with st.expander(f"Variants 21 to {len(rows)}"):
                table(rows[20:])
        if v["total_variants"] > len(rows):
            st.caption(f"Showing the {len(rows)} most frequent of {v['total_variants']:,} variants.")
        if rows:
            st.markdown("**Coverage curve**: share of cases covered by the most frequent variants")
            st.line_chart(pd.DataFrame({"Rank": [r["Rank"] for r in rows],
                                        "Cumulative % of cases": [r["Cumulative %"] for r in rows]}
                                       ).set_index("Rank"))

with tab_bot:
    if shown(m["bottlenecks"]):
        bt = m["bottlenecks"]["data"]
        st.markdown("**Slowest transitions**")
        table([{"From": t["src"], "To": t["dst"], "Cases": t["cases"],
                "Median wait": format_hours(t["median_wait_h"]), "90% wait": format_hours(t["p90_wait_h"])}
               for t in bt["transitions"]])
        if bt.get("skipped_low_support"):
            st.caption(f"{bt['skipped_low_support']:,} transitions seen in fewer than "
                       f"{CFG['min_support_cases']} cases are not ranked (low support).")
        if bt["activities"]:
            st.markdown("**Slowest activities** (time spent inside the step)")
            table([{"Activity": a["activity"], "Cases": a["cases"],
                    "Median processing": format_hours(a["median_processing_h"])} for a in bt["activities"]])
        elif bt.get("activities_reason"):
            st.caption(bt["activities_reason"])
        st.caption("Waiting time = time between the end of one step and the start of the next in the same case.")

with tab_rew:
    if shown(m["rework"]):
        rw = m["rework"]["data"]
        st.markdown("**Activities repeated within a case**")
        table([{"Activity": a["activity"], "Cases with repeats": a["cases"],
                "Share of its cases %": round(100 * a["share"], 1),
                "Extra occurrences": a["extra_occurrences"], "Support": support_mark(a)}
               for a in rw["activities"]], "No activity is repeated within a case.")
        st.metric("Self-loops (A → A)", f"{rw['self_loops']:,}")
        st.markdown("**Back and forth (A → B → A)**")
        table([{"Pattern": f"{p['a']} → {p['b']} → {p['a']}", "Count": p["count"], "Cases": p["cases"],
                "Support": support_mark(p)} for p in rw["ping_pong"]], "No back-and-forth found.")

with tab_res:
    if shown(m["resources"]):
        rs = m["resources"]["data"]
        st.markdown(f"**Resources** ({rs['total_resources']:,})")
        table([{"Resource": r["resource"], "Events": r["events"], "Cases": r["cases"],
                "Median processing": format_hours(r["median_processing_h"])} for r in rs["resources"]])
        st.markdown(f"**Handovers** ({rs['total_handovers']:,} in total; most frequent first)")
        table([{"From": h["from"], "To": h["to"], "Count": h["count"], "Cases": h["cases"]}
               for h in rs["handovers"]], "No handovers between different resources.")
        st.caption("Resource values are shown as recorded; anonymised IDs stay anonymised.")

with tab_app:
    # Analyses made before application mapping existed have no such section.
    section = m.get("applications") or {"available": False,
                                        "reason": "Not available: run the analysis again to see applications."}
    if shown(section):
        ap = section["data"]
        table([{"Application": a["application"], "Category": a["category"], "Events": a["events"],
                "Cases": a["cases"], "Share of events %": round(100 * a["share"], 1)}
               for a in ap["applications"]])
        if ap["unnamed_masked"]:
            st.caption(f"{ap['unnamed_masked']} masked applications have no real name yet "
                       "(name them under Application mapping on Import data).")

    st.markdown("#### Switching")
    section = m.get("app_switching") or {"available": False,
                                         "reason": "Not available: run the analysis again to count switches."}
    if shown(section):
        sw = section["data"]
        k = sw["kpis"]
        c = st.columns(4)
        c[0].metric("App switches", f"{k['total']:,}")
        c[1].metric("Per case (median)", f"{k['per_case_median'] or 0:g}")
        c[2].metric("Per 100 events", "–" if k["per_100_events"] is None else f"{k['per_100_events']:.1f}")
        c[3].metric("Cases with a switch", f"{k['share_cases_with_switch']:.0%}")
        st.write(describe_switching(sw))
        app_dot = to_app_dot(sw["app_nodes"], sw["app_edges"])
        st.graphviz_chart(app_dot, width="stretch")
        st.caption("Box = application · arrow = switch from → to · number = times")
        st.download_button("Download switch map (DOT)", app_dot, file_name=f"app_switches_{job_id}.dot",
                           mime="text/vnd.graphviz", key="switch_dot_download")
        st.markdown("**Most frequent switches**")
        table([{"From": p["from"], "To": p["to"], "Switches": p["count"], "Cases": p["cases"],
                "Median gap": p["median_gap"], "Support": support_mark(p)} for p in sw["pairs"]],
              "No switches.")
        st.markdown("**Switching by step** (most switching per time the step is done first)")
        table([{"Step": s["activity"], "Switches per time done": s["per_occurrence"], "Switches": s["switches"],
                "Times done": s["occurrences"], "Cases": s["cases"], "Main apps": ", ".join(s["top_apps"]),
                "Support": support_mark(s)} for s in sw["by_step"]], "No switches.")
        st.markdown("**Steps × applications** (share of each step's events done in each application)")
        apps_shown = [n["application"] for n in sw["app_nodes"]]
        if sw["steps_by_app"]:
            st.dataframe(
                pd.DataFrame([{"Step": r["activity"], "Apps used": r["apps_used"],
                               **{a: 100 * r["shares"].get(a, 0.0) for a in apps_shown}}
                              for r in sw["steps_by_app"]]),
                hide_index=True, width="stretch",
                column_config={a: st.column_config.ProgressColumn(a, min_value=0, max_value=100, format="%.0f%%")
                               for a in apps_shown})
        st.markdown("**Back and forth between applications (A → B → A)**")
        table([{"Pattern": f"{p['a']} → {p['b']} → {p['a']}", "Count": p["count"], "Cases": p["cases"],
                "Support": support_mark(p)} for p in sw["ping_pong"]], "No back-and-forth between applications.")
        st.caption(f"An application switch = two consecutive events of the same case and the same user (the same "
                   f"case only, when no user column is mapped), ordered by time, with two different, known "
                   f"applications, at most {k['session_gap_minutes']} minutes apart (a longer pause starts a new "
                   "work session). Counted before repeated steps are merged. Rows left out on Import data are not counted.")

with tab_trend:
    if shown(m["trends"]):
        months = m["trends"]["data"]["months"]
        if not months:
            st.caption("No cases to show.")
        else:
            df = pd.DataFrame(months).set_index("month")
            st.markdown("**Cases started per month**")
            st.bar_chart(df[["cases"]].rename(columns={"cases": "Cases started"}))
            st.markdown("**Median case duration per month** (hours)")
            st.line_chart(df[["median_duration_h"]].rename(columns={"median_duration_h": "Median duration (h)"}))
            low = [x["month"] for x in months if x["low_support"]]
            if low:
                st.caption(f"Low support (fewer than {CFG['min_support_cases']} cases): {', '.join(low)}.")


def inside_step_tab() -> None:
    """One step at a time: its Level 4 flow, usual patterns, time split, work types, rework, applications,
    variation between people, automation candidates and the PM4Py Level 4 model (built on request)."""
    from app.core import inside_step, pm4py_model
    sec = m.get("inside") or {"available": False, "reason": "Not available: run the analysis again."}
    data = inside_step.load(folder) if sec.get("available") else None
    if not data or not data.get("available"):
        st.info(sec.get("reason") or (data or {}).get("reason") or "Not available: run the analysis again.")
        return
    steps = data["steps"]
    l3 = st.selectbox("Step", list(steps), key="inside_l3")
    d = steps[l3]
    st.caption(f"Counted in work sittings: consecutive clicks of one case and this step by one person, no pause over "
               f"{data['settings']['gap_minutes']} min. {SITTING_NOTE} Status: {d['status']}.")
    if not d.get("enough"):
        st.info(d.get("reason", "not enough runs"))
        return
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Work sittings", f"{d['runs']:,}")
    c2.metric("Active hours", f"{d['active_h']:,.1f} h")
    c3.metric("Median active time per sitting", fmt.duration_s(d["median_run_s"]))
    c4.metric("Sittings with rework inside", f"{100 * d['rework']['share']:.1f}%")
    c5.metric("App switches per sitting", f"{d['apps']['switches_per_run']:.2f}")
    st.markdown("**Level 4 flow** (directly-follows counts inside runs)")
    st.graphviz_chart(d["flow"]["dot"], width="stretch")
    left, right = st.columns(2)
    with left:
        st.markdown(f"**Usual patterns** ({d['distinct_patterns']:,} distinct)")
        table([{"Pattern": " → ".join(p["path"]), "Runs": p["runs"], "Share %": round(100 * p["share"], 1)}
               for p in d["patterns"]])
        st.caption(f"Most runs start with {d['start']['sub_step']} ({100 * d['start']['share']:.0f}%) and end with "
                   f"{d['end']['sub_step']} ({100 * d['end']['share']:.0f}%); {100 * d['ends_with_completion_share']:.0f}% "
                   "end with a completion action (submit, post, save, send ...).")
    with right:
        st.markdown("**Where the time goes**")
        table([{"Sub-step": s["sub_step"], "Hours": round(s["hours"], 1), "Share %": round(100 * (s["share_of_hours"] or 0), 1),
                "Median per run": fmt.duration_s(s["median_run_s"]), "Work type": s["work_type"]} for s in d["time_split"]])
    left, right = st.columns(2)
    with left:
        st.markdown("**Rework inside the step** (a sub-step done again after another one, within a run)")
        table([{"Sub-step": r["sub_step"], "Runs": r["runs"], "Share %": round(100 * r["share"], 1)}
               for r in d["rework"]["by_sub_step"]], "No rework inside the step.")
        st.markdown("**Applications**")
        table([{"Application": a["application"], "Runs": a["runs"], "Share %": round(100 * a["share"], 1)} for a in d["apps"]["top"]])
    with right:
        st.markdown("**Variation between people** (masked)")
        if d.get("cv_between_people") is not None:
            st.caption(f"Spread of the users' median time per sitting: {d['cv_between_people']:.2f} — "
                       f"{d.get('cv_label') or '–'} (low < 0.3, moderate 0.3–{data['settings']['cv_flag']:g}, "
                       f"high above: flagged)")
        table([{"User": p["user"], "Runs": p["runs"], "Median per run": fmt.duration_s(p["median_run_s"]),
                "Main pattern": " → ".join(p["main_pattern"]), "Flags": ", ".join(p["flags"])} for p in d["people"]])
    if d.get("automation_candidates"):
        st.markdown("**Candidates for SME review** (facts only)")
        for h in d["automation_candidates"]:
            st.write(f"- {h}")
    st.markdown("**Level 4 model (PM4Py)**")
    pm = d.get("pm4py")
    if st.button("Build the Level 4 model for this step (PM4Py)", key="inside_build_pm"):
        with st.spinner("Discovering the Level 4 model and replaying the runs (up to a few minutes)..."):
            inside_step.build_models(jobs.load_job(job_id), [l3])
        st.rerun()
    if not pm:
        st.caption(pm4py_model.status()[1] or "Not built yet.")
    elif not pm.get("available"):
        st.info(pm["reason"])
    else:
        a, b, c = st.columns(3)
        a.metric("Fitness", f"{pm['fitness']:.2f}", help=f"token replay on {pm['runs_used']:,} of {pm['runs_total']:,} runs")
        b.metric("Precision", f"{pm['precision']:.2f}" if pm.get("precision") is not None else "–",
                 help="1.0 = the model allows only what really happens" + (f"; {pm['precision_note']}" if pm.get("precision_note") else ""))
        c.metric("Runs that fit fully", f"{100 * pm['fitting_runs_share']:.1f}%")
        st.graphviz_chart(pm["dot"], width="stretch")
        doc = pm.get("documented")
        if doc and doc.get("available"):
            st.markdown(f"**Documented vs actual** (sub-steps your documents name, status: {doc['status']})")
            x, y, z = st.columns(3)
            x.metric("Runs that follow the documented flow", f"{100 * doc['fitting_cases_share']:.1f}%")
            y.metric("Fitness", f"{doc['fitness']:.2f}")
            z.metric("Precision", f"{doc['precision']:.2f}" if doc.get("precision") is not None else "–")
            table([{"Sub-step": s["step"], "Skipped %": round(100 * s["skipped_share"], 1),
                    "Out of order %": round(100 * s["out_of_order_share"], 1), "Extra %": round(100 * s["extra_share"], 1)}
                   for s in doc["steps"]])
        elif d.get("doc_substeps"):
            st.caption("Your documents name sub-steps for this step: " + ", ".join(d["doc_substeps"]))
    st.caption(pm4py_model.LICENCE_NOTE)


with tab_inside:
    inside_step_tab()
