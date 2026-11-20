"""The workflow map: every service, agent and human gate of the app, with its inputs and outputs.

One definition, used by the web UI's "Workflow map" page and by the workflow document. For an analysis, each node
gets a live status from the files it writes (done / waiting / not used), so the map doubles as a control tower.

kind     service = deterministic code (no AI) · agent = code that may ask the AI (always behind the approval gate,
         always with a rules fallback) · human = a person decides · store = where results live
lane     the phase of the workflow (left to right on the map)
writes   files in the job folder; "library:" files live in data/library/<process>/
"""
from __future__ import annotations

from pathlib import Path

LANES = [("intake", "1 · Intake"), ("context", "2 · Business context"), ("engine", "3 · Analysis engine"),
         ("review", "4 · Human review"), ("report", "5 · Report")]

NOCASE = [
    {"id": "N1", "name": "Intake & mode detection", "kind": "service", "lane": "intake",
     "module": "app/intake/mode_detect.py · app/core/ingest.py",
     "does": "Reads the uploaded file(s), profiles every column and tests whether a real case ID exists. Picks the "
             "workflow: case ID or no case ID.",
     "inputs": ["Uploaded Parquet / CSV / Excel file(s)", "Dataset class (public / client)"],
     "outputs": ["Column profile (types, missing %, examples)", "mode_decision.json (tests passed / failed)"],
     "writes": ["job.json", "raw/"], "next": ["N2"]},
    {"id": "N2", "name": "Clickstream converter", "kind": "service", "lane": "intake",
     "module": "app/nocase/ingest.py", "does": "Maps the source columns to the standard columns (time, user, "
     "application, screen, click, text, active time...), applies the row filters, hashes users and case IDs.",
     "inputs": ["Source file", "Column mapping (suggested, confirmed by you)", "Row filters", "Timezone"],
     "outputs": ["Standard events", "Scope: rows kept / left out / unreadable time"],
     "writes": ["nc_events.parquet", "nc_scope.json", "nc_mapping.json"], "next": ["N3"]},
    {"id": "N3", "name": "Profile, clean & units", "kind": "service", "lane": "intake",
     "module": "app/nocase/profile.py · clean.py · units.py",
     "does": "Finds what the data supports, cleans screen names (removes record numbers, names), marks generic "
             "clicks, and groups events into units = application + screen + click.",
     "inputs": ["nc_events.parquet"], "outputs": ["Capabilities", "Clean events", "Units with events and hours"],
     "writes": ["nc_capabilities.json", "nc_events_clean.parquet", "nc_units.parquet"], "next": ["E1"]},
    {"id": "C1", "name": "Business context", "kind": "human", "lane": "context",
     "module": "Business context page · app/core/knowledge.py",
     "does": "You name the process and add the SOPs, transcripts, decks or recordings, or record that there are "
             "none. Mandatory before the analysis.",
     "inputs": ["Process name", "Business documents"], "outputs": ["Process in the library", "Document sources"],
     "writes": ["job.json (process, business_context)", "library: docs/"], "next": ["C2"]},
    {"id": "C2", "name": "Stages & steps from documents", "kind": "agent", "lane": "context", "task": "nc_pack_from_docs",
     "module": "app/nocase/ai/pack_from_docs.py",
     "does": "Reads the masked document text and proposes stages and steps with descriptions.",
     "inputs": ["Document text (names, e-mails, IDs, long numbers masked)"],
     "outputs": ["Proposed process pack"], "writes": ["library: pack.proposed.yaml"], "next": ["H1"]},
    {"id": "H1", "name": "Approve the process", "kind": "human", "lane": "context",
     "module": "Business context page · section 3", "does": "You check and edit the proposed stages and steps (or type "
     "them from the SOP) and approve them. Required before the analysis can run; every later analysis of this "
     "process reuses them.",
     "inputs": ["pack.proposed.yaml"], "outputs": ["Approved stages and steps (versioned)"],
     "writes": ["library: pack.yaml", "library: decisions.jsonl", "library: versions/"], "next": ["E4"]},
    {"id": "E1", "name": "Episodes", "kind": "service", "lane": "engine", "module": "app/nocase/strategies/episodes.py",
     "does": "Cuts each person's events into episodes of continuous work (split at long pauses).",
     "inputs": ["Clean events"], "outputs": ["Episodes per person"], "writes": ["nc_episodes.parquet"],
     "next": ["E2"]},
    {"id": "E2", "name": "Case link", "kind": "service", "lane": "engine", "module": "app/nocase/strategies/case_link.py",
     "does": "Looks for a real ID (in a column or inside the text) that passes the case-link tests. No case is "
             "ever invented.", "inputs": ["Episodes", "Text columns"], "outputs": ["Case links (or none)"],
     "writes": ["nc_case_links.parquet"], "next": ["E3"]},
    {"id": "E3", "name": "Evidence plan", "kind": "agent", "lane": "engine", "task": "nc_evidence_plan",
     "module": "app/nocase/ai/evidence.py",
     "does": "Decides which signals separate the steps best and writes questions for the SME.",
     "inputs": ["Approved steps", "Application and screen-group counts"],
     "outputs": ["Evidence weights", "Questions for the SME"],
     "writes": ["nc_evidence_plan.json", "nc_evidence_scores.json"], "next": ["E4"]},
    {"id": "E4", "name": "Step assignment", "kind": "agent", "lane": "engine", "task": "nc_assign_rules",
     "module": "app/nocase/strategies/assign.py",
     "does": "Gives every unit a step, in layers: labels in the export → approved library rules → AI links each "
             "application + screen group to a step (or 'other work') → unassigned.",
     "inputs": ["Units", "Approved steps", "Approved rules"],
     "outputs": ["Step per unit with source and confidence", "Proposed rules for review"],
     "writes": ["nc_assignments.parquet", "nc_assign_quality.json", "library: rules.proposed.yaml"],
     "next": ["E5", "E6", "H2"]},
    {"id": "E5", "name": "Level 4 activities", "kind": "agent", "lane": "engine", "task": "nc_activities",
     "module": "app/nocase/engine/activities.py",
     "does": "Groups the clicks inside each step into a few named activities (Level 4).",
     "inputs": ["Units of one step"], "outputs": ["Activities per step"],
     "writes": ["nc_activities.parquet", "nc_activities_check.json", "library: activities.proposed.yaml"],
     "next": ["E6"]},
    {"id": "E6", "name": "Measures", "kind": "service", "lane": "engine", "module": "app/nocase/engine/measures.py",
     "does": "Hours, events, people, runs, repetitions and switches per step, stage, application and activity. "
             "Time basis: measured active time, else end − start, else an estimate (said so).",
     "inputs": ["Clean events with steps"], "outputs": ["All numbers of the map and report"],
     "writes": ["nc_measures.json"], "next": ["E7"]},
    {"id": "E7", "name": "Flow (process map)", "kind": "service", "lane": "engine", "module": "app/nocase/engine/flow.py",
     "does": "Order of steps: the approved business order, plus the transitions really seen inside episodes, kept "
             "only when they hold across people and weeks.", "inputs": ["Steps per episode", "Approved order"],
     "outputs": ["Nodes, edges, missing steps"], "writes": ["nc_flow.json"], "next": ["E8"]},
    {"id": "E8", "name": "Confidence & questions", "kind": "service", "lane": "engine",
     "module": "app/nocase/engine/confidence.py",
     "does": "Confidence per step and transition; questions for the SME where the evidence is weak.",
     "inputs": ["Assignments", "Flow"], "outputs": ["Confidence", "SME questions"],
     "writes": ["nc_confidence.json", "nc_sme_questions.json"], "next": ["E9"]},
    {"id": "E9", "name": "Inside-step patterns", "kind": "service", "lane": "engine",
     "module": "app/nocase/engine/inside_step.py",
     "does": "The usual sequence of activities inside each step and how often it is followed.",
     "inputs": ["Activities per run"], "outputs": ["Patterns per step"], "writes": ["nc_inside_step.json"],
     "next": ["H2", "R1"]},
    {"id": "G1", "name": "AI approval gate", "kind": "human", "lane": "engine", "module": "app/api/ai_packages.py",
     "does": "Every AI request is first built as a JSON package and shown: purpose, contents, size, SHA-256. Only "
             "approved tasks are sent; every request sent is logged.",
     "inputs": ["Packages prepared by C2, E3, E4, E5, R3, S2, S3, S4"], "outputs": ["Approvals", "Audit log"],
     "writes": ["ai_packages.json", "ai_approvals.json", "ai_audit.jsonl"], "next": []},
    {"id": "H2", "name": "Review steps", "kind": "human", "lane": "review", "module": "Review steps page",
     "does": "You accept or correct the proposed rules, give a step to the largest unassigned units, and answer "
             "the SME questions, then run the analysis again so the map and report use the accepted rules. Accepted "
             "rules are reused by every later analysis.",
     "inputs": ["Proposed rules", "Unassigned units", "Questions"], "outputs": ["Confirmed rules", "Answers"],
     "writes": ["library: rules.yaml", "library: decisions.jsonl"], "next": ["E4", "R1"]},
    {"id": "R1", "name": "Checks", "kind": "service", "lane": "report", "module": "app/nocase/report/checks.py",
     "does": "Recomputes every headline number a second way and compares them; a failed check blocks that number.",
     "inputs": ["Measures", "Clean events"], "outputs": ["Check results"], "writes": ["nc_checks.json"],
     "next": ["R2"]},
    {"id": "R2", "name": "Facts", "kind": "service", "lane": "report", "module": "app/nocase/report/facts.py",
     "does": "Turns the checked numbers into numbered facts (F001...). The report can only state facts.",
     "inputs": ["Measures", "Checks"], "outputs": ["Fact list"], "writes": ["nc_facts.json"], "next": ["R3"]},
    {"id": "R3", "name": "Summary wording", "kind": "agent", "lane": "report", "task": "nc_summary",
     "module": "app/nocase/report/narrative.py",
     "does": "Words the summary using fact IDs only; the app fills in the numbers. Any problem → the fact-only "
             "summary is used.", "inputs": ["Fact IDs and labels"], "outputs": ["Summary text"],
     "writes": [], "next": ["R4"]},
    {"id": "R4", "name": "Report builder", "kind": "service", "lane": "report", "module": "app/nocase/report/report.py "
     "· app/api/pdf.py", "does": "Builds the HTML and Excel report and the stakeholder PDF from checked facts.",
     "inputs": ["Facts", "Measures", "Flow", "Business context"], "outputs": ["Report HTML / Excel / PDF"],
     "writes": ["nc_report.html", "nc_report.xlsx"], "next": ["S1"]},
    {"id": "S1", "name": "AI summary JSON (ProcessLens)", "kind": "service", "lane": "report",
     "module": "app/api/ai_summary.py",
     "does": "Builds ProcessLens's aggregate summary (schema 1.1) from the results, then the two JSONs sent to the "
             "AI: the compact map summary and the evidence catalog E01-E11 (+ this app's step evidence E12 ...). "
             "No rows, titles, people, IDs or application names.",
     "inputs": ["Measures", "Flow", "Clean events (counted locally)"],
     "outputs": ["Aggregate summary", "Compact map summary", "Transformation context"], "writes": [],
     "next": ["S2", "S3", "S4"]},
    {"id": "S2", "name": "AI Analyst readout", "kind": "agent", "lane": "report", "task": "nc_readout",
     "module": "app/api/readout.py",
     "does": "Seven-section transformation readout. Every claim cites one evidence ID; the app prints the numbers.",
     "inputs": ["Transformation context"], "outputs": ["Readout page and PDF"], "writes": ["readout.json"],
     "next": []},
    {"id": "S4", "name": "Explain this step (Level 4)", "kind": "agent", "lane": "report", "task": "nc_stage_explain",
     "module": "app/api/stage_explain.py",
     "does": "On a click of a step on the Process Map: ProcessLens's stage explanation plus one summary for each "
             "measured Level 4 activity. A hypothesis beside the measured numbers.",
     "inputs": ["One step: applications, click + screen patterns, Level 4 activities"],
     "outputs": ["Step explanation and Level 4 summaries"], "writes": ["stage_explanations.json"], "next": []},
    {"id": "S3", "name": "AI-enhanced conceptual map", "kind": "agent", "lane": "report", "task": "nc_process_map",
     "module": "app/api/ai_map.py",
     "does": "A short conceptual map (3-7 stages) as a hypothesis beside the measured map.",
     "inputs": ["Compact map summary (scope, profile)"], "outputs": ["Conceptual map"],
     "writes": ["ai_process_map.json"], "next": []},
]

CASE = [
    {"id": "K1", "name": "Intake & mode detection", "kind": "service", "lane": "intake",
     "module": "app/intake/mode_detect.py · app/core/ingest.py",
     "does": "Reads and profiles the file(s) and confirms that a case ID column passes the tests.",
     "inputs": ["Uploaded file(s)", "Dataset class"], "outputs": ["Column profile", "Mode decision"],
     "writes": ["job.json", "raw/"], "next": ["K2"]},
    {"id": "K2", "name": "Event log converter", "kind": "service", "lane": "intake",
     "module": "app/core/mapping.py · ingest.normalise",
     "does": "Maps case ID, activity, timestamp (+ optional user, application, end time...) and writes the standard "
             "event log. Case IDs are stored as salted hashes.",
     "inputs": ["Source file", "Column mapping (suggested, confirmed by you)"],
     "outputs": ["Standard event log", "Scope"], "writes": ["events.parquet", "mapping.json", "scope.json"],
     "next": ["K3"]},
    {"id": "C1", "name": "Business context", "kind": "human", "lane": "context",
     "module": "Business context page · app/core/knowledge.py",
     "does": "You name the process and add its documents; the AI (after approval) proposes its stages and steps "
             "and you approve them. The approved steps are the intended flow every case is compared with.",
     "inputs": ["Process name", "Business documents"], "outputs": ["Document sources"],
     "writes": ["job.json (process, business_context)", "library: docs/"], "next": ["KC2"]},
    {"id": "KC2", "name": "Stages & steps from documents", "kind": "agent", "lane": "context",
     "task": "nc_pack_from_docs", "module": "app/nocase/ai/pack_from_docs.py",
     "does": "Reads the masked document text and proposes stages and steps with descriptions.",
     "inputs": ["Document text (names, e-mails, IDs, long numbers masked)"],
     "outputs": ["Proposed process pack"], "writes": ["library: pack.proposed.yaml"], "next": ["KH1"]},
    {"id": "KH1", "name": "Approve the process", "kind": "human", "lane": "context",
     "module": "Business context page · section 3",
     "does": "You check and edit the proposed stages and steps (or type them from the SOP) and approve them. They "
             "are the intended flow every case is compared with; every later analysis of this process reuses them.",
     "inputs": ["pack.proposed.yaml"], "outputs": ["Approved stages and steps (versioned)"],
     "writes": ["library: pack.yaml", "library: decisions.jsonl", "library: versions/"], "next": ["K6"]},
    {"id": "K3", "name": "Data quality", "kind": "service", "lane": "engine", "module": "app/core/quality.py",
     "does": "12 checks (timestamps, duplicates, case completeness, label hygiene, stability...) → a feasibility "
             "score and band that limits what the report may claim.", "inputs": ["events.parquet"],
     "outputs": ["Feasibility score, band, fixes"], "writes": ["quality.json"], "next": ["K4"]},
    {"id": "K4", "name": "Process mining", "kind": "service", "lane": "engine", "module": "app/core/mining.py",
     "does": "Directly-follows map, variants, waiting times, rework, people, applications, switching.",
     "inputs": ["events.parquet", "Quality band"], "outputs": ["Map, KPIs, variants, bottlenecks"],
     "writes": ["mining.json"], "next": ["K5", "K6"]},
    {"id": "K5", "name": "Level 4 inside each step", "kind": "agent", "lane": "engine", "task": "level4",
     "module": "app/core/level4.py", "does": "Groups the actions inside each step into named sub-steps.",
     "inputs": ["Actions per step"], "outputs": ["Sub-steps"], "writes": ["mining.json (level4)"], "next": ["K6"]},
    {"id": "K6", "name": "Review steps", "kind": "human", "lane": "review",
     "module": "Review steps page · app/core/case_steps.py · reference.py",
     "does": "You link each activity of the data to an approved step (same or very similar names are suggested), "
             "correct the order of the steps and confirm with your name. Every case is then compared with this "
             "intended flow: cases in order, steps done earlier than intended, back and forth, steps not seen.",
     "inputs": ["Approved steps", "Activities of the data", "Mining results"],
     "outputs": ["Confirmed intended order", "Conformance"],
     "writes": ["reference.json"], "next": ["K9", "K7"]},
    {"id": "K9", "name": "Process Map", "kind": "service", "lane": "review",
     "module": "Process Map page · server._case_map",
     "does": "The approved stages and steps with the cases that reach each step, the intended order (grey) and "
             "the transitions seen in the cases (blue); every activity as observed one click away.",
     "inputs": ["reference.json", "mining.json"], "outputs": ["Map (step, stage, observed)"],
     "writes": ["reference.json"], "next": ["KS1"]},
    {"id": "KS1", "name": "AI summary JSON (ProcessLens)", "kind": "service", "lane": "report",
     "module": "app/api/ai_summary.py",
     "does": "Builds ProcessLens's aggregate summary from the case data, then the JSONs sent to the AI: the compact "
             "map summary (case flow with opaque step aliases) and the evidence catalog E01-E11. No rows, case IDs, "
             "people or application names.",
     "inputs": ["events.parquet (counted locally)", "Mining", "Approved steps"],
     "outputs": ["Aggregate summary", "Compact map summary", "Transformation context"], "writes": [],
     "next": ["KS2", "KS3", "KS4"]},
    {"id": "KS2", "name": "AI Analyst readout", "kind": "agent", "lane": "report", "task": "nc_readout",
     "module": "app/api/readout.py",
     "does": "Seven-section transformation readout. Every claim cites one evidence ID; the app prints the numbers.",
     "inputs": ["Transformation context"], "outputs": ["Readout page and PDF"], "writes": ["readout.json"],
     "next": []},
    {"id": "KS4", "name": "Explain this step (Level 4)", "kind": "agent", "lane": "report", "task": "nc_stage_explain",
     "module": "app/api/stage_explain.py",
     "does": "On a click of a box on the Process Map: ProcessLens's stage explanation plus one summary for each "
             "Level 4 activity (sub-steps or clicks). A hypothesis beside the measured numbers.",
     "inputs": ["One activity: cases, waits, applications, Level 4"],
     "outputs": ["Step explanation and Level 4 summaries"], "writes": ["stage_explanations.json"], "next": []},
    {"id": "KS3", "name": "AI-enhanced conceptual map", "kind": "agent", "lane": "report", "task": "nc_process_map",
     "module": "app/api/ai_map.py",
     "does": "A short conceptual map (3-7 stages) as a hypothesis beside the measured map.",
     "inputs": ["Compact map summary (case scope)"], "outputs": ["Conceptual map"],
     "writes": ["ai_process_map.json"], "next": []},
    {"id": "K7", "name": "Facts & checks", "kind": "service", "lane": "report", "module": "app/core/facts.py · "
     "report_checks.py", "does": "Every number in the report becomes a checked fact.",
     "inputs": ["Mining", "Metrics", "Reference"], "outputs": ["Facts"], "writes": ["facts.json", "metrics.json"],
     "next": ["K8"]},
    {"id": "K8", "name": "Report builder", "kind": "agent", "lane": "report", "task": "narrative",
     "module": "app/core/report.py · narrative.py",
     "does": "HTML and Excel report; the AI may word the summary from facts only.",
     "inputs": ["Facts", "Mining", "Business context"], "outputs": ["Report HTML / Excel / PDF"],
     "writes": ["report.html", "report.xlsx"], "next": []},
]


def nodes(mode: str) -> list[dict]:
    return NOCASE if mode == "nocase" else CASE


def edges(mode: str) -> list[dict]:
    return [{"source": n["id"], "target": t} for n in nodes(mode) for t in n.get("next", [])]


def with_status(mode: str, folder: Path | None, job: dict | None, library_dir: Path | None) -> list[dict]:
    """Each node + status: done (its files exist), waiting (an upstream node is not done), ready, n/a."""
    out = []
    for n in nodes(mode):
        files = [w.split(" ")[0] for w in n.get("writes", [])]
        present, missing = [], []
        for w in files:
            if w == "library:":
                continue
            if w.startswith("library:"):
                continue
            path = (folder / w.rstrip("/")) if folder else None
            (present if path and path.exists() else missing).append(w)
        lib_files = [w.split("library: ")[1] for w in n.get("writes", []) if w.startswith("library: ")]
        lib_present = [w for w in lib_files if library_dir and (library_dir / w.rstrip("/")).exists()]
        if n["id"] == "C1":
            status = "done" if (job or {}).get("business_context", {}) and (job or {}).get("process") else "todo"
        elif n["kind"] == "human" and n["id"] in ("H1",):
            status = "done" if "pack.yaml" in lib_present else "todo"
        elif n["kind"] == "human" and n["id"] == "H2":
            status = "done" if "rules.yaml" in lib_present else "todo"
        elif n["id"] == "G1":
            status = "done" if "ai_audit.jsonl" in present else ("ready" if "ai_packages.json" in present else "todo")
        elif files and not missing and present:
            status = "done"
        elif present or lib_present:
            status = "partial" if missing else "done"
        else:
            status = "todo"
        out.append({**n, "status": status, "present": present + [f"library: {x}" for x in lib_present]})
    return out
