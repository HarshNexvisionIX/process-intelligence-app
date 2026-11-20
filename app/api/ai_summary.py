"""The AI summary JSON, built exactly as ProcessLens v2.8 builds it (same keys, same evidence IDs, same prompts).

ProcessLens sends the AI one privacy-controlled aggregate summary and two smaller JSONs made from it:

    safe_ai_payload               the aggregate summary (schema_version 1.1): counts, hours, application classes,
                                  activity classes, case flow / no-case navigation with opaque step IDs, operational
                                  signals, title categories, data limitations. No event rows, titles, people or IDs.
    compact_map_payload           the "EXACT OUTGOING SUMMARY" for the AI-enhanced conceptual map (compact / detailed,
                                  case / navigation / combined).
    build_transformation_context  the evidence catalog E01-E11 for the seven-section AI Analyst readout
                                  (schema processlens-transformation-readout-v1).

Here the same JSONs are filled from this app's engine instead of ProcessLens's pandas session code:
  * hours are this app's measured hours (the time basis of the analysis), not ProcessLens's capped gaps;
  * without a case ID the navigation nodes are the APPROVED steps of the process (NavStep_01 ... with the step name
    as semantic_hint), and the edges are the step transitions seen inside working episodes;
  * application names leave only as ProcessLens's generic classes (Spreadsheet, Email client, ...);
  * activity and title text never leave: only ProcessLens's fixed categories, computed here.
Extra evidence this app has and ProcessLens does not (steps not seen, back-and-forth, run-time variation, repetitive
activities, work not linked to a step) is appended to the catalog as E12-E16, in the same format.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

SCHEMA_VERSION = "1.1"
READOUT_SCHEMA = "processlens-transformation-readout-v1"

# ----------------------------------------------------------------------------- ProcessLens fixed vocabularies
STAGE_RULES = [
    ("Intake / Request", r"\b(intake|request|inbox|incoming|rfq|enquiry|inquiry|requisition|quote|quotation)\b"),
    ("Validate / Review", r"\b(validat\w*|check\w*|review\w*|verif\w*|reconcil\w*|match\w*|audit\w*|complet\w*)\b"),
    ("Create / Update", r"\b(create|created|new order|edit\w*|update\w*|entry|register|draft|enter|maintain)\b"),
    ("Approve / Decision", r"\b(approv\w*|authoriz\w*|reject\w*|sign.?off|decision)\b"),
    ("Send / Close", r"\b(send|sent|dispatch\w*|issue\w*|confirm\w*|close\w*|finish\w*|submit\w*|release)\b"),
]


def semantic_stage(text) -> str:
    val = str(text) if text is not None else ""
    for label, expression in STAGE_RULES:
        if re.search(expression, val, flags=re.I):
            return label
    return "Other / Unclassified"


def title_category(text, app, activity="") -> str:
    combined = str(text or "") + " " + str(activity or "")
    if re.search(r"\b(rfq|quotation|quote|vendor offer|sourcing)\b", combined, re.I):
        return "RFQ / Quote"
    if re.search(r"\b(validat\w*|review\w*|check\w*|match\w*|reconcil\w*)\b", combined, re.I):
        return "Validation / Review"
    if re.search(r"\b(purchase order|\bpo\b|order entry|purchase requisition)\b", combined, re.I):
        return "PO / Order work"
    if re.search(r"\b(approv\w*|reject\w*|authoriz\w*)\b", combined, re.I):
        return "Approval"
    if re.search(r"\b(report|dashboard|tracker|summary|pivot)\b", combined, re.I):
        return "Reporting / Tracking"
    if re.search(r"\b(mail|inbox|outlook|reply|forward|email)\b", combined, re.I):
        return "Email / Coordination"
    if re.search(r"\b(create|edit|update|save|entry)\b", combined, re.I):
        return "Create / Update"
    return "Other / Unknown"


def generic_app_bucket(name) -> str:
    name = str(name).lower()
    if any(term in name for term in ["outlook", "gmail", "mail", "thunderbird"]):
        return "Email client"
    if any(term in name for term in ["excel", "sheets", "spreadsheet", "calc"]):
        return "Spreadsheet"
    if any(term in name for term in ["sap", "fiori", "oracle", "erp", "netsuite"]):
        return "ERP / business system"
    if any(term in name for term in ["teams", "slack", "zoom", "chat"]):
        return "Collaboration"
    if any(term in name for term in ["chrome", "edge", "firefox", "browser"]):
        return "Web browser"
    if any(term in name for term in ["word", "docs", "document", "pdf"]):
        return "Document editor / reader"
    return "Other application"


CLIPBOARD = r"copy|paste|clipboard|ctrl\s*\+\s*[cv]"


def _short_number(v) -> float:
    return round(float(v or 0), 3)


def _rows_as_dicts(df: pd.DataFrame, fields: list[str], limit: int = 25) -> list[dict]:
    if df is None or df.empty:
        return []
    out = []
    for item in df.head(limit).to_dict("records"):
        row = {}
        for f in fields:
            v = item.get(f)
            if isinstance(v, bool) or not isinstance(v, (float, int)) and not hasattr(v, "dtype"):
                row[f] = v
            elif float(v).is_integer() and f in ("events", "cases", "sessions"):
                row[f] = int(v)
            else:
                row[f] = _short_number(v)
        out.append(row)
    return out


def _load(folder: Path, name: str, default=None):
    p = folder / name
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except ValueError:
        return default


def _empty_payload() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_scope": "Aggregated event telemetry; not transaction-system ground truth",
        "days_in_dataset": 0, "event_count": 0, "participant_count": 0, "case_coverage_pct": 0.0,
        "observed_case_count": 0, "recorded_or_estimated_hours": 0.0,
        "duration_source": "user-selected duration convention; may be inferred",
        "application_type_totals": [], "application_class_totals": [], "activity_class_totals": [],
        "case_flow": {"nodes": [], "edges": [], "start_edges": [], "end_edges": [], "case_metrics": {}},
        "no_case_navigation": {"nodes": [], "edges": [], "session_metrics": {}},
        "no_case_semantic_navigation": {"scope": "Navigation-only evidence, not a confirmed case process", "edges": []},
        "title_categories": [],
        "data_limitations": [
            "A sequence without case IDs is navigation/session evidence, NOT a true process instance.",
            "Timestamps and durations may not represent user effort or transaction throughput.",
            "Text and identities are deliberately excluded; step meanings are heuristic.",
        ],
    }


def _common(payload: dict, ev: pd.DataFrame) -> None:
    """The parts computed the same way in both modes. ev: participant, application, date, hours, activity, title,
    timestamp, app_type."""
    total_hours = float(ev["hours"].sum())
    payload["days_in_dataset"] = int(ev["date"].nunique())
    payload["event_count"] = int(len(ev))
    payload["participant_count"] = int(ev["participant"].dropna().nunique())
    payload["recorded_or_estimated_hours"] = _short_number(total_hours)
    for app_type, group in ev.groupby("app_type"):
        payload["application_type_totals"].append({"type": str(app_type), "events": int(len(group)),
                                                   "hours": _short_number(group["hours"].sum())})
    buckets = ev["application"].fillna("").map(generic_app_bucket)
    grouped = (ev.assign(app_class=buckets).groupby("app_class").agg(events=("hours", "size"), hours=("hours", "sum"))
               .reset_index())
    payload["application_class_totals"] = _rows_as_dicts(grouped.sort_values("events", ascending=False),
                                                         ["app_class", "events", "hours"], 12)
    ordered = ev.sort_values(["participant", "timestamp"], kind="stable")
    prev_app = ordered.groupby("participant")["application"].shift()
    prev_day = ordered.groupby("participant")["date"].shift()
    switch = prev_app.notna() & ordered["date"].eq(prev_day) & ordered["application"].ne(prev_app)
    clip = ev["activity"].fillna("").astype(str).str.contains(CLIPBOARD, case=False, regex=True)
    days = ev.dropna(subset=["participant"]).groupby("participant")["date"].nunique()
    payload["operational_signals"] = {
        "application_switches": int(switch.sum()),
        "clipboard_keyword_matches": int(clip.sum()),
        "median_active_days_per_known_participant": _short_number(days.median()) if len(days) else 0,
        "max_active_days_per_known_participant": int(days.max()) if len(days) else 0,
        "evidence_warning": "These are event signals, not proof of friction, rework, or automatable work.",
    }
    # categories on DISTINCT text combinations, then counted back: no text leaves, only the fixed labels
    combo = (ev.assign(activity=ev["activity"].fillna(""), title=ev["title"].fillna(""),
                       application=ev["application"].fillna(""))
             .groupby(["application", "activity", "title"], as_index=False)
             .agg(events=("hours", "size"), hours=("hours", "sum")))
    combo["semantic_category"] = (combo["activity"] + " " + combo["title"]).map(semantic_stage)
    act = combo.groupby("semantic_category")["events"].sum().sort_values(ascending=False)
    payload["activity_class_totals"] = [{"semantic_category": str(k), "events": int(v)} for k, v in act.items()]
    combo["category"] = [title_category(t, a, x) for t, a, x in zip(combo["title"], combo["application"],
                                                                     combo["activity"])]
    combo["app_class"] = combo["application"].map(generic_app_bucket)
    titles = combo.groupby(["app_class", "category"], as_index=False).agg(events=("events", "sum"),
                                                                         hours=("hours", "sum"))
    payload["title_categories"] = _rows_as_dicts(titles.sort_values("events", ascending=False),
                                                 ["app_class", "category", "events", "hours"], 25)


# ----------------------------------------------------------------------------- no case ID
def _nocase(folder: Path, job: dict) -> dict:
    from app.nocase.engine.measures import UNASSIGNED, events_frame
    payload = _empty_payload()
    ev, basis = events_frame(folder)
    linked = ev["step"].notna() & ev["step"].astype(str).ne("") & ev["step_label"].ne(UNASSIGNED)
    frame = pd.DataFrame({
        "participant": ev["actor"], "application": ev["app"], "timestamp": ev["ts"],
        "date": ev["ts"].dt.date, "hours": ev["sec"].astype(float) / 3600.0,
        "activity": ev.get("action_clean"), "title": ev.get("screen_clean"),
        # ProcessLens: Process / Non-process from the user's list of process applications. Here: the event is
        # linked to an approved step of the process (Process) or not linked to any step (Non-process).
        "app_type": ["Process" if a else "Non-process" for a in linked],
    })
    _common(payload, frame)
    payload["duration_source"] = {"measured": "active time measured by the source (active_s)",
                                  "start-end": "end time minus start time of each event",
                                  "estimated": "gap to the next event of the same person, capped"}.get(basis, basis)
    payload["case_coverage_pct"] = 0.0           # no case ID: this app found no column that links events into cases
    payload["observed_case_count"] = 0
    # navigation: the approved steps (business names, approved by the SME) and their observed transitions
    m = _load(folder, "nc_measures.json", {}) or {}
    flow = _load(folder, "nc_flow.json", {}) or {}
    steps = [s for s in m.get("steps", []) if s.get("step") != UNASSIGNED]
    by_step = {s["step"]: s for s in steps}
    order = [n["step"] for n in flow.get("nodes", []) if n.get("step") in by_step] or [s["step"] for s in steps]
    order = sorted(order, key=lambda s: -int(by_step[s].get("events") or 0))[:20]
    aliases = {name: f"NavStep_{i + 1:02d}" for i, name in enumerate(order)}
    payload["no_case_navigation"]["nodes"] = [
        {"id": aliases[s], "semantic_hint": s, "events": int(by_step[s].get("events") or 0),
         "sessions": int(by_step[s].get("runs") or 0)} for s in order]
    edges = sorted(flow.get("edges", []), key=lambda e: -int(e.get("count") or 0))
    payload["no_case_navigation"]["edges"] = [
        {"source": aliases[e["src"]], "target": aliases[e["dst"]], "transitions": int(e.get("count") or 0)}
        for e in edges if e.get("src") in aliases and e.get("dst") in aliases][:40]
    o = m.get("overall") or {}
    epi = ev.groupby("episode_id").size() if "episode_id" in ev else pd.Series(dtype=int)
    payload["no_case_navigation"]["session_metrics"] = {
        "sessions": int(o.get("episodes") or len(epi)),
        "median_events_per_session": _short_number(epi.median()) if len(epi) else 0}
    # ProcessLens's fixed-taxonomy navigation: category changes inside a working episode
    ordered = frame.assign(episode=ev["episode_id"].values,
                           safe_stage=(frame["activity"].fillna("") + " " + frame["title"].fillna("")).map(semantic_stage))
    ordered = ordered.sort_values(["participant", "timestamp"], kind="stable")
    old = ordered.groupby(["participant", "episode"], dropna=False)["safe_stage"].shift()
    changes = old.notna() & ordered["safe_stage"].ne(old)
    pairs = pd.DataFrame({"source": old[changes].values, "safe_stage": ordered.loc[changes, "safe_stage"].values})
    if not pairs.empty:
        freq = pairs.groupby(["source", "safe_stage"], as_index=False).size().sort_values("size", ascending=False)
        payload["no_case_semantic_navigation"]["edges"] = [
            {"from_category": str(r["source"]), "to_category": str(r["safe_stage"]),
             "observed_transitions": int(r["size"])} for _, r in freq.head(25).iterrows()]
    return payload


# ----------------------------------------------------------------------------- case ID
def _case(folder: Path, job: dict) -> dict:
    import duckdb
    from app.core.db import sql_literal
    payload = _empty_payload()
    path = folder / "events.parquet"
    if not path.exists():
        return payload
    con = duckdb.connect()
    p = sql_literal(path.as_posix())
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({p})").fetchall()]
    app = "application" if "application" in cols else "NULL"
    end = "ts_end" if "ts_end" in cols else "NULL"
    df = con.execute(f"""SELECT CAST(case_id AS VARCHAR) AS case_id, CAST(activity AS VARCHAR) AS activity,
                                ts_start, {end} AS ts_end, CAST(resource AS VARCHAR) AS resource,
                                CAST({app} AS VARCHAR) AS application
                         FROM read_parquet({p}) WHERE ts_start IS NOT NULL""").df()
    hours = ((pd.to_datetime(df["ts_end"]) - pd.to_datetime(df["ts_start"])).dt.total_seconds().clip(lower=0)
             .fillna(0) / 3600.0)
    # Process / Non-process, as in the no-case mode: the activity is linked to an approved step of the process
    # (Review steps) or not. Without an approved process every event stays Unclassified.
    app_type: object = "Unclassified"
    if job.get("process"):
        from app.core import case_steps
        acts = sorted(df["activity"].dropna().unique().tolist())
        if case_steps.approved_steps(job["process"]):
            linked = {r["activity"] for r in case_steps.links(job["process"], acts) if r["step"]}
            app_type = ["Process" if a in linked else "Non-process" for a in df["activity"]]
    frame = pd.DataFrame({"participant": df["resource"], "application": df["application"],
                          "timestamp": pd.to_datetime(df["ts_start"]), "date": pd.to_datetime(df["ts_start"]).dt.date,
                          "hours": hours, "activity": df["activity"], "title": None,
                          "app_type": app_type})
    _common(payload, frame)
    payload["duration_source"] = ("end time minus start time of each event" if float(hours.sum()) > 0
                                  else "no event durations in the source")
    has = df["case_id"].notna() & df["case_id"].ne("")
    payload["case_coverage_pct"] = round(100 * float(has.mean()), 1) if len(df) else 0.0
    payload["observed_case_count"] = int(df.loc[has, "case_id"].nunique())
    # observed case flow (direct-follows), with opaque step aliases and ProcessLens's semantic hints
    d = df[has].sort_values(["case_id", "ts_start"], kind="stable")
    nodes = (d.groupby("activity").agg(events=("case_id", "size"), cases=("case_id", "nunique"))
             .sort_values("events", ascending=False).head(24))
    aliases = {str(a): f"CaseStep_{i + 1:02d}" for i, a in enumerate(nodes.index)}
    payload["case_flow"]["nodes"] = [{"id": aliases[str(a)], "semantic_hint": semantic_stage(a),
                                      "events": int(r.events), "case_count": int(r.cases)}
                                     for a, r in nodes.iterrows()]
    nxt = d.groupby("case_id")["activity"].shift(-1)
    gap = (pd.to_datetime(d.groupby("case_id")["ts_start"].shift(-1)) - pd.to_datetime(d["ts_end"].fillna(d["ts_start"]))
           ).dt.total_seconds() / 60.0
    t = pd.DataFrame({"source": d["activity"], "target": nxt, "gap": gap}).dropna(subset=["target"])
    e = (t.groupby(["source", "target"]).agg(count=("gap", "size"), median_wait_minutes=("gap", "median"))
         .reset_index().sort_values("count", ascending=False))
    payload["case_flow"]["edges"] = [
        {"source": aliases[s], "target": aliases[g], "transitions": int(c), "median_gap_minutes": _short_number(w)}
        for s, g, c, w in e[["source", "target", "count", "median_wait_minutes"]].itertuples(index=False)
        if s in aliases and g in aliases][:40]
    first = d.groupby("case_id")["activity"].first().value_counts()
    last = d.groupby("case_id")["activity"].last().value_counts()
    payload["case_flow"]["start_edges"] = [{"step_id": aliases[a], "cases": int(n)} for a, n in first.items()
                                           if a in aliases]
    payload["case_flow"]["end_edges"] = [{"step_id": aliases[a], "cases": int(n)} for a, n in last.items()
                                         if a in aliases]
    g = d.groupby("case_id")
    elapsed = (pd.to_datetime(g["ts_start"].max()) - pd.to_datetime(g["ts_start"].min())).dt.total_seconds() / 3600
    handoffs = g["resource"].apply(lambda s: int((s.ne(s.shift()) & s.shift().notna()).sum()))
    revisits = g["activity"].apply(lambda s: int(len(s) - s.nunique()))
    payload["case_flow"]["case_metrics"] = {
        "count": int(len(elapsed)), "median_elapsed_hours": _short_number(elapsed.median()) if len(elapsed) else 0,
        "mean_handoffs": _short_number(handoffs.mean()) if len(handoffs) else 0,
        "cases_with_revisits_pct": _short_number(revisits.gt(0).mean() * 100) if len(revisits) else 0}
    return payload


def safe_ai_payload(folder: Path, job: dict) -> dict:
    """The ProcessLens aggregate summary (schema_version 1.1) for this analysis."""
    return _nocase(folder, job) if job.get("mode") == "nocase" else _case(folder, job)


# ----------------------------------------------------------------------------- ProcessLens: compact map summary
def compact_map_payload(base_payload: dict, budget: str = "compact", scope: str = "combined") -> dict:
    """Deterministically shrink already-safe aggregates for a SINGLE low-token map call (ProcessLens, unchanged)."""
    if budget not in ("compact", "detailed"):
        raise ValueError("Map budget must be compact or detailed")
    if scope not in ("case", "navigation", "combined"):
        raise ValueError("Map scope must be case, navigation or combined")
    node_cap, edge_cap = (8, 12) if budget == "compact" else (16, 25)
    case = base_payload.get("case_flow", {})
    nav = base_payload.get("no_case_navigation", {})

    def reduce_flow(flow, node_limit, edge_limit):
        nodes = sorted(flow.get("nodes", []), key=lambda v: v.get("events", 0), reverse=True)[:node_limit]
        ids = {n["id"] for n in nodes}
        edges = sorted(flow.get("edges", []), key=lambda v: v.get("transitions", 0), reverse=True)
        return {"nodes": nodes, "edges": [e for e in edges if e["source"] in ids and e["target"] in ids][:edge_limit]}

    c = reduce_flow(case, node_cap, edge_cap)
    c["case_metrics"] = case.get("case_metrics", {})
    n = reduce_flow(nav, node_cap, edge_cap)
    n["session_metrics"] = nav.get("session_metrics", {})
    semantic = base_payload.get("no_case_semantic_navigation", {})
    payload = {
        "purpose": "process_map_hypothesis_not_observed_case_truth",
        "budget": budget, "scope": scope,
        "evidence_scope": base_payload.get("evidence_scope"),
        "event_count": base_payload.get("event_count"),
        "case_coverage_pct": base_payload.get("case_coverage_pct"),
        "observed_case_count": base_payload.get("observed_case_count"),
        "case_flow": c,
        "no_case_navigation": n,
        "no_case_semantic_edges": semantic.get("edges", [])[:edge_cap],
        "top_application_classes": sorted(base_payload.get("application_class_totals", []),
                                          key=lambda v: v.get("events", 0), reverse=True)[:5 if budget == "compact" else 8],
        "top_activity_classes": sorted(base_payload.get("activity_class_totals", []),
                                       key=lambda v: v.get("events", 0), reverse=True)[:6 if budget == "compact" else 9],
        "limitations": base_payload.get("data_limitations", [])[:3] + [
            f"Only the top {node_cap} aggregate nodes and {edge_cap} transitions per flow are included; rare paths "
            "may be excluded.",
            "All original titles, activity text, persons, and case IDs are withheld.",
        ],
    }
    if scope == "case":
        payload.pop("no_case_navigation")
        payload.pop("no_case_semantic_edges")
    elif scope == "navigation":
        payload.pop("case_flow")
    return payload


def estimate_tokens(payload: dict) -> int:
    """Very rough character-based estimate (ProcessLens)."""
    return max(1, (len(json.dumps(payload, separators=(",", ":"), ensure_ascii=True)) + 3) // 4)


# ----------------------------------------------------------------------------- ProcessLens: evidence catalog E01-E11
def _num(value, precision=1):
    return round(float(value or 0), precision)


def build_transformation_context(payload: dict, extra: list[dict] | None = None) -> dict:
    """Return exactly the reviewed, compact, organization-agnostic AI input (ProcessLens), plus this app's extra
    evidence (E12 ...) when given."""
    total_hours = _num(payload.get("recorded_or_estimated_hours"))
    events = int(payload.get("event_count") or 0)
    participants = int(payload.get("participant_count") or 0)
    cases = int(payload.get("observed_case_count") or 0)
    coverage = _num(payload.get("case_coverage_pct"))
    apps = payload.get("application_class_totals") or []
    grouped = {x.get("app_class"): _num(x.get("hours")) for x in apps}
    combined = _num(grouped.get("Spreadsheet", 0) + grouped.get("Email client", 0))
    app_type = {x.get("type"): x for x in payload.get("application_type_totals") or []}
    process_hours = _num(app_type.get("Process", {}).get("hours"))
    nonprocess_hours = _num(app_type.get("Non-process", {}).get("hours"))
    case_metric = payload.get("case_flow", {}).get("case_metrics") or {}
    signals = payload.get("operational_signals") or {}
    nav = payload.get("no_case_navigation", {}).get("session_metrics") or {}
    titles = payload.get("title_categories") or []
    evidence: list[dict] = []

    def add(code, topic, fact, condition=True, caveat=""):
        if condition:
            evidence.append({"id": code, "topic": topic, "observed_fact": fact, "limitation": caveat})

    add("E01", "Data footprint", f'{events:,} analyzed events across {participants:,} participants and '
        f'{int(payload.get("days_in_dataset") or 0)} observed dates.', events > 0,
        "Observed dates are not proof of full-time attendance.")
    add("E02", "Work duration", f"{total_hours:,.1f} source-recorded or gap-estimated hours.", total_hours > 0,
        "Duration definitions and overlap must be validated; these are not recoverable hours.")
    add("E03", "Application classification", f"{process_hours:,.1f} process hours; {nonprocess_hours:,.1f} "
        "non-process hours.", bool(app_type), "Source flags or manual classification may be incomplete.")
    add("E04", "Email and spreadsheet footprint", f"{combined:,.1f} attributed hours in generic Email and "
        "Spreadsheet classes.", combined > 0,
        "Application usage does not establish manual waste or a specific business task.")
    add("E05", "Application concentration", "; ".join(f'{x.get("app_class")}: {_num(x.get("hours")):,.1f}h'
                                                       for x in apps[:3]), bool(apps),
        "Generic categories may combine distinct source applications.")
    add("E06", "Case traceability", f"{coverage:.1f}% of observed events carry case IDs; {cases:,} distinct "
        "observed case IDs.", events > 0, "Event-level case coverage is not end-to-end business case completeness.")
    add("E07", "Observed case loops and handoffs",
        f'{_num(case_metric.get("cases_with_revisits_pct")):.1f}% of observed cases revisit a stage; '
        f'{_num(case_metric.get("mean_handoffs")):.1f} average participant changes per case.',
        cases > 0, "A revisit is not necessarily rework; a participant change is not necessarily delay.")
    add("E08", "Application navigation", f'{int(signals.get("application_switches") or 0):,} same-day application '
        "changes.", int(signals.get("application_switches") or 0) > 0,
        "Changes measure navigation, not proven duplicate work.")
    add("E09", "Clipboard signal", f'{int(signals.get("clipboard_keyword_matches") or 0):,} activity aliases match '
        "clipboard keywords.", int(signals.get("clipboard_keyword_matches") or 0) > 0,
        "Keyword matches do not establish copied data or destinations.")
    add("E10", "No-case activity", f'{int(nav.get("sessions") or 0):,} heuristic user sessions among events '
        "without case IDs.", int(nav.get("sessions") or 0) > 0, "Navigation sessions are not reconstructed transactions.")
    add("E11", "Content category mix", "; ".join(f'{x.get("category")} ({x.get("app_class")}): '
                                                  f'{int(x.get("events") or 0):,} events' for x in titles[:5]),
        bool(titles), "Title categories are keyword heuristics, not validated document semantics.")
    for i, x in enumerate(extra or []):                   # this app's extra evidence, same format
        evidence.append({"id": f"E{12 + i:02d}", "topic": x["topic"], "observed_fact": x["observed_fact"],
                         "limitation": x.get("limitation", "")})

    flow = payload.get("case_flow") or {}
    nav_flow = payload.get("no_case_semantic_navigation") or {}
    nav_nodes = payload.get("no_case_navigation") or {}
    context = {
        "schema": READOUT_SCHEMA,
        "evidence_status": "Aggregate and partly heuristic; model may propose hypotheses, never measured savings",
        "scope": {"events": events, "participants": participants, "cases": cases,
                  "days": int(payload.get("days_in_dataset") or 0), "case_event_coverage_pct": coverage},
        "evidence_catalog": evidence,
        "case_stage_summary": {"nodes": [
            {"id": x["id"], "hint": x.get("semantic_hint", "Other / Unclassified"),
             "events": int(x.get("events") or 0)} for x in flow.get("nodes", [])[:8]],
            "transitions": [{"source": x["source"], "target": x["target"], "count": int(x.get("transitions") or 0)}
                            for x in flow.get("edges", [])[:12]]},
        "no_case_navigation_categories": (nav_flow.get("edges") or [])[:8],
        "rules": ["No business-specific system, actor, SLA, policy or cause is established by these aggregates.",
                  "Do not sum different opportunity pools or call them savings.",
                  "Use evidence IDs only; describe causes as hypotheses pending validation.",
                  "No-case navigation cannot establish a transaction flow."],
    }
    if nav_nodes.get("nodes"):                            # no case ID: the approved steps, measured
        context["no_case_step_summary"] = {
            "nodes": [{"id": x["id"], "hint": x.get("semantic_hint"), "events": int(x.get("events") or 0)}
                      for x in nav_nodes["nodes"][:12]],
            "transitions": [{"source": x["source"], "target": x["target"], "count": int(x.get("transitions") or 0)}
                            for x in nav_nodes.get("edges", [])[:12]]}
    return context


def validate_transformation_readout(report: dict, context: dict) -> dict:
    """ProcessLens's check: cited evidence must exist; sizes bounded. Raises ValueError."""
    allowed = {x["id"] for x in context.get("evidence_catalog", [])}
    for key in ("findings", "risks", "interventions"):
        for item in report.get(key, []):
            if item.get("evidence_id") not in allowed:
                raise ValueError(f"AI {key} references missing evidence; regenerate after reviewing data quality.")
    bounds = {"findings": 4, "risks": 3, "interventions": 5, "agents": 3, "outcomes": 3, "decisions": 3}
    report = dict(report)
    for key, count in bounds.items():
        report[key] = report.get(key, [])[:count]
    if not report["interventions"] or not report["outcomes"]:
        raise ValueError("The AI returned an incomplete transformation readout; nothing was saved.")
    return report


def safe_evidence_lookup(context: dict) -> dict:
    return {r["id"]: r for r in context.get("evidence_catalog", [])}


def to_json(payload: dict) -> str:
    """The exact text sent (ProcessLens: compact separators, no ASCII escaping)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
