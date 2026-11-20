"""Clickstream mapping (no case ID): scope, what the data supports, and the units (app, screen, click).

Every number is computed by app/nocase/; nothing here sends data anywhere.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.ui import shell

from app.core import fmt, jobs
from app.core.db import connect, sql_literal
from app.core.privacy import set_dataset_class
from app.nocase import profile
from app.nocase.ingest import load_scope
from app.nocase.units import UNITS_FILE
from app.nocase.strategies.base import load_decisions
from app.nocase.ai.evidence import PLAN_FILE, SCORES_FILE

shell.header("Overview and analysis", "What the data contains and what it supports. Run the analysis here: work "
             "sittings, steps, activities and measures are built from the clicks; no case is invented.", "nc_overview")

nc_jobs = [j for j in jobs.list_jobs() if j.get("mode") == "nocase"]
if not nc_jobs:
    st.info("No analysis without a case ID yet. On **Import data**, load a file: the app detects that it has no "
            "case ID and offers the clickstream mapping.")
    st.stop()

job_id = shell.pick_job(nc_jobs, "nc_overview", "import a file without a case ID first")
if job_id is None:
    st.stop()
job = jobs.load_job(job_id)
set_dataset_class(job.get("dataset_class"))
folder = jobs.job_folder(job_id)
_client = job.get("dataset_class", "client") == "client"
st.caption((":orange-badge[Client data] AI only on private providers" if _client
            else ":blue-badge[Public data] any allowed AI provider"))

scope = load_scope(folder)
if not scope:
    st.warning("This job was not converted yet.")
    st.stop()

# --------------------------------------------------------------------------- scope
st.subheader("Scope")
c = st.columns(5)
c[0].metric("Rows in file", f"{scope['rows_in_file']:,}")
c[1].metric("Left out by the filter", f"{scope['left_out_by_filter']:,}")
c[2].metric("Unreadable time", f"{scope['unreadable_time']:,}")
c[3].metric("In scope", f"{scope['in_scope']:,}")
c[4].metric("Active hours", fmt.hours(scope["active_hours_in_scope"]))
st.caption(f"{scope['actors']:,} actors · {scope['apps']:,} applications · "
           f"{scope.get('first_event') or '–'} to {scope.get('last_event') or '–'} (UTC)")

# --------------------------------------------------------------------------- run analysis
_lock = folder / ".nc_run.lock"


def _running() -> str | None:
    """When another run of this analysis is going on (a second tab or a double click), its start time."""
    import time as _t
    if _lock.exists() and _t.time() - _lock.stat().st_mtime < 3600:
        return _lock.read_text(encoding="utf-8").strip() or "earlier"
    return None


_busy = _running()
if _busy:
    st.info(f"The analysis of this file is already running (started {_busy} UTC). Wait for it to finish, then "
            "refresh this page. Two runs at once would overwrite each other's results.", icon=":material/hourglass_top:")
if st.button("Run analysis", key="nc_run_analysis", type="primary", disabled=bool(_busy),
             help="Every stage in order: profile, clean, units, episodes, record links, evidence, steps, activities, "
                  "measures, map, confidence, inside each step. With the AI on this takes a few minutes."):
    import time as _time
    from datetime import datetime as _dt, timezone as _tz
    from app.nocase.ai_gateway import gateway as _gateway
    from app.nocase.stages import run_all as _run_all
    start = _time.perf_counter()
    _lock.write_text(_dt.now(_tz.utc).strftime("%H:%M"), encoding="utf-8")
    try:
        with st.status("Running the analysis... (a few minutes when the AI is on; keep this tab open)",
                       expanded=True) as _status:
            try:
                _stages = _run_all(folder, process=job.get("process"),
                                   ai=_gateway(job.get("dataset_class"), "online"))
            except Exception as _exc:   # noqa: BLE001 - shown to the user, not as a traceback
                _status.update(label="The analysis stopped with an error", state="error")
                st.error(f"The analysis stopped: {type(_exc).__name__}: {_exc}")
                st.stop()
            for _name, _info in _stages.items():
                _msg = (_info.get("output") or {}).get("message") if isinstance(_info.get("output"), dict) else None
                st.write(f"{'✅' if _info['status'] == 'ran' else '⏭️'} {_name}: " + (_msg or _info.get("reason") or "done"))
            _status.update(label=f"Analysis done in {_time.perf_counter() - start:.1f} s", state="complete")
    finally:
        _lock.unlink(missing_ok=True)
    job["status"] = "nc_analysed"
    jobs.save_job(job)

# The last run, stage by stage (kept after the page reloads): what each stage did and why a layer did not apply
_dec = load_decisions(folder)
_order = ["episodes", "case_link", "evidence", "assign", "activities", "measures", "flow", "confidence", "inside_step"]
_done = [k for k in _order if isinstance(_dec.get(k), dict)]
if _done and not _busy:
    with st.expander("Last run: what each stage did"):
        for k in _done:
            d = _dec[k]
            st.markdown(f"**{k.replace('_', ' ').capitalize()}** · {d.get('message') or d.get('decision') or 'done'}")
            if k == "assign":
                for layer in d.get("layers") or []:
                    st.caption(f"{'applied' if layer.get('applies') else 'not applied'} · {layer.get('strategy')}: "
                               f"{layer.get('reason')}")
                if d.get("ai_note"):
                    st.caption(f"AI note: {d['ai_note']}")
                import json as _json
                _q = folder / "nc_assign_quality.json"
                _ai = (_json.loads(_q.read_text(encoding="utf-8")).get("ai") or {}) if _q.exists() else {}
                if _ai:
                    _rej = _ai.get("rejected") or []
                    st.caption(f"AI rules: {len(_ai.get('rules') or [])} proposed and applied · {len(_rej)} rejected · "
                               f"{len(_ai.get('cannot_assign') or [])} unit(s) the AI could not place")
                    _why: dict[str, int] = {}
                    for r in _rej:
                        k = str(r.get("reason", ""))[:90]
                        _why[k] = _why.get(k, 0) + 1
                    for k, n in sorted(_why.items(), key=lambda kv: -kv[1])[:5]:
                        st.caption(f"rejected {n}×: {k}")
            elif d.get("ai_note"):
                st.caption(f"AI note: {d['ai_note']}")

caps = profile.load(folder)
if not caps:
    if st.button("Measure what the data supports", type="primary"):
        caps = profile.build(folder)
        st.rerun()
    st.stop()

decisions = load_decisions(folder)
case_link = decisions.get("case_link") or {}
if case_link:
    st.info("**Case-link check:** " + str(case_link.get("message") or case_link.get("decision")
                                                   or "not available"), icon=":material/link_off:")
evidence_plan = folder / PLAN_FILE
evidence_scores = folder / SCORES_FILE
if evidence_plan.exists():
    import json
    plan = json.loads(evidence_plan.read_text(encoding="utf-8"))
    scores = json.loads(evidence_scores.read_text(encoding="utf-8")) if evidence_scores.exists() else {}
    with st.expander("Evidence plan and measured scores"):
        st.caption(f"Evidence plan source: {plan.get('source', 'not recorded')}")
        st.dataframe(pd.DataFrame([{"Use": p.get("use"), "Signal": p.get("signal"), "Why": p.get("why"),
                                   "Measured score": (scores.get("scores", {}).get(p.get("signal"), {}) or {}).get("score")}
                                  for p in plan.get("plan", [])]), hide_index=True, width="stretch")

# --------------------------------------------------------------------------- capabilities
st.subheader("What this data supports")
st.dataframe(pd.DataFrame([{"Analysis": k.replace("_", " ").capitalize(), "Possible": "Yes" if v["possible"] else "No",
                            "Why": v["reason"]} for k, v in caps["supports"].items()]),
             hide_index=True, width="stretch")
left, right = st.columns(2)
with left:
    st.markdown("**Fill rate of the standard columns**")
    st.dataframe(pd.DataFrame([{"Column": k, "Rows": v["rows"], "Filled %": round(100 * v["share"], 1)}
                               for k, v in caps["fill"].items() if v["rows"]]), hide_index=True, width="stretch")
    lab = caps["labels"]
    if lab["lowest_level"]:
        cons = "–" if lab.get("consistency") is None else f"{100 * lab['consistency']:.1f}%"
        st.caption(f"Step labels: lowest level {lab['lowest_level']} is {100 * lab['lowest_fill']:.1f}% filled; "
                   f"consistency per unit {cons}.")
with right:
    st.markdown("**ID shapes** (letters → A, digits → 9; values are never stored)")
    shapes = caps["id_shapes"]["overall"]
    if shapes:
        st.dataframe(pd.DataFrame([{"Shape": s["shape"], "Events": s["events"], "Events %": round(100 * s["share"], 1),
                                    "Distinct": s["distinct"]} for s in shapes]), hide_index=True, width="stretch")
    else:
        st.caption("No ID-like tokens (5+ characters with 3+ digits) in the text, screen or action.")
    if caps.get("case_id"):
        ci = caps["case_id"]
        st.markdown(f"**Mapped case_id: {ci['verdict'].replace('_', ' ')}** — " + "; ".join(ci["reasons"]))
    for cnt in caps.get("counters") or []:
        st.caption(f"`{cnt['column']}`: {cnt['reason']}")
t = caps["time"]
gen = caps["action"]["generic_share"]
st.caption(f"Active time: {t['active_time']} · end times: {'yes' if t['ts_end'] else 'no'} · median gap between "
           f"one person's events: {'–' if t['median_gap_per_actor_s'] is None else fmt.duration_s(t['median_gap_per_actor_s'])}"
           f" · generic clicks: {fmt.pct(gen)}")

# --------------------------------------------------------------------------- units
st.subheader("Units (application · screen · click)")
path = folder / UNITS_FILE
if not path.exists():
    st.info("Units are built by the conversion (or `python scripts/nocase.py units --job ...`).")
    st.stop()
units = connect().execute(f"SELECT * FROM read_parquet({sql_literal(path.as_posix())}) ORDER BY unit_id").df()
st.caption(f"{len(units):,} units cover {int(units['events'].sum()):,} events.")
show = units.head(200).rename(columns={"unit_id": "Unit", "app": "Application", "screen_clean": "Screen",
                                       "action_clean": "Click", "events": "Events", "actors": "Actors",
                                       "active_hours": "Active h", "share": "Share", "is_generic": "Generic",
                                       "top_l3": "Top step (l3)", "top_l3_share": "Top step share"})
st.dataframe(show[["Unit", "Application", "Screen", "Click", "Events", "Actors", "Active h", "Share", "Generic",
                   "Top step (l3)", "Top step share"]], hide_index=True, width="stretch")
