"""AI readout = ProcessLens's "AI Analyst": the seven-section transformation readout, with ProcessLens's exact JSON.

    summary    the ProcessLens aggregate summary (ai_summary.safe_ai_payload, schema_version 1.1) of this analysis.
    evidence   ProcessLens's transformation context built from it (ai_summary.build_transformation_context): schema
               processlens-transformation-readout-v1, scope, evidence_catalog E01-E11 (id, topic, observed_fact,
               limitation), case_stage_summary, no_case_navigation_categories, rules. Without a case ID this app adds
               its own evidence in the same format (E12 ...: hours by approved step, work not linked to a step, steps
               not seen, transitions, back-and-forth, run-time variation, repetitive activities) and
               no_case_step_summary (the approved steps, measured).
    request    ProcessLens's system prompt and user line, then the context as compact JSON, exactly as ProcessLens
               sends it to Gemini. One AI request (task nc_readout) through the approval gate (ai_packages.py):
               prepared, shown as JSON with its SHA-256, sent only after a named reviewer approves it.
    answer     ProcessLens's TransformationReadoutResult keys: headline, transformation_objective, executive_message,
               findings, risks, interventions, orchestrator_role, agents, outcomes, decisions. Checked as ProcessLens
               checks it (every finding, risk and intervention cites one evidence_id of the catalog; sizes bounded)
               plus this app's rule that the free text contains no numbers (ProcessLens's prompt asks the same).
               One small retry with the problems, then the reason is shown.
The readout is saved as readout.json in the job folder, with the fingerprint, provider and model.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

TASK = "nc_readout"
FILE = "readout.json"
BOUNDS = {"findings": (2, 4), "risks": (1, 3), "interventions": (2, 5), "agents": (1, 3), "outcomes": (2, 3),
          "decisions": (1, 3)}

# ProcessLens v2.8 services/ai.py TRANSFORMATION_SYSTEM, unchanged ...
TRANSFORMATION_SYSTEM = """You are a senior operations transformation analyst authoring a SEVEN-SLIDE
business readout, using only a small reviewed, anonymous aggregate evidence catalog.
The required storyline is (1) Transformation objective and scope, (2) Where work is concentrated,
(3) As-Is business process (will be drawn from measured case stage flows),
(4) Where effort and handoffs concentrate, (5) Five targeted interventions,
(6) To-Be agent orchestration, (7) Outcomes and scale path.
Return concise structured JSON, not slide layouts or counts. The deterministic app adds visuals,
actual metrics and case transitions to the slide templates.
Create 3-4 findings, 2-3 risks, 3-5 specific interventions, three agent roles,
three outcomes, and three implementation decisions. Each finding, risk and intervention
MUST cite exactly one evidence_id present in evidence_catalog, without inventing other IDs.
The same evidence may be used for different insights, but avoid repetitive recommendations.
Propose potential causes and solutions as HYPOTHESES; do not assert an unobserved root cause.
You may suggest conditional workflow/orchestration agents but cannot claim the source
has an actual orchestrator, approval policies, system integrations, or SLA targets.
Make recommendations specific to the available signals, explaining the validation gate
and a measurable KPI. If case coverage is low, prioritize correlation/measurement.
No invented numbers, percentages, business systems, brand names, staffing, savings, SLAs,
costs, case meanings or customer identities. Do not put numeric facts in free-text prose;
all numeric figures are inserted from the approved evidence catalog by the application.
No-case user sessions are navigation only, NOT business transaction sequences.
For agents, each list of actions must have only two short, concrete actions; always
include human approval for consequential decisions. Three outcomes should cover
cycle time/control, productivity/throughput, and measured scale/adoption, with baseline
measurement and pilot validation. Keep each field to one short sentence, enough for a
board-ready graphical readout. Never imply effort hours are recoverable savings.
Return valid complete JSON. Do not include markdown."""

# ... and the TransformationReadoutResult schema, which ProcessLens passes to Gemini as response_schema. Other
# providers have no response_schema, so it is written out here.
OUTPUT_SCHEMA = """Return ONE JSON object with exactly these keys (TransformationReadoutResult):
{"headline": "...", "transformation_objective": "...", "executive_message": "...",
 "findings": [{"headline": "...", "interpretation": "...", "evidence_id": "E02"}],
 "risks": [{"headline": "...", "interpretation": "...", "evidence_id": "E08"}],
 "interventions": [{"title": "...", "evidence_id": "E05", "proposed_cause": "...", "proposed_change": "...",
                    "kpi_to_test": "...", "validation_gate": "...", "priority": "High|Medium|Low"}],
 "orchestrator_role": "...",
 "agents": [{"title": "...", "role": "...", "actions": ["...", "..."], "human_control": "..."}],
 "outcomes": [{"title": "...", "target_direction": "...", "kpi": "...", "how_to_measure": "...", "scale_gate": "..."}],
 "decisions": ["..."]}
No digits in any text field: the application shows the observed_fact of each cited evidence_id."""

SYSTEM = TRANSFORMATION_SYSTEM + "\n\n" + OUTPUT_SCHEMA
USER_LINE = "Use this evidence catalog to write a concise seven-slide transformation readout:"   # ProcessLens


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(folder: Path, name: str, default=None):
    p = folder / name
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except ValueError:
        return default


def _pct(x) -> str:
    return f"{100 * float(x or 0):.0f}%"


def _extra(folder: Path, job: dict) -> list[dict]:
    """This app's evidence that ProcessLens has no equivalent for (no case ID), in the catalog's format."""
    if job.get("mode") != "nocase":
        return []
    m = _load(folder, "nc_measures.json", {}) or {}
    flow = _load(folder, "nc_flow.json", {}) or {}
    ins = {s["step"]: s for s in (_load(folder, "nc_inside_step.json", {}) or {}).get("steps", [])}
    out = []
    steps = sorted([s for s in m.get("steps", []) if s.get("step") != "Unassigned"], key=lambda s: -float(s.get("hours") or 0))
    if steps:
        out.append({"topic": "Hours by approved process step",
                    "observed_fact": "; ".join(f"{s['step']}: {float(s.get('hours') or 0):,.1f}h" for s in steps[:6]),
                    "limitation": "Steps come from the approved process; screens are linked to them by approved rules."})
    o = m.get("overall") or {}
    if o.get("unassigned_share") is not None:
        out.append({"topic": "Work not linked to a step yet",
                    "observed_fact": f"{_pct(o.get('unassigned_share'))} of hours "
                                     f"({float(o.get('unassigned_hours') or 0):,.1f}h)",
                    "limitation": "Includes other work and screens not reviewed yet; shrinks as rules are accepted."})
    missing = [x.get("step") if isinstance(x, dict) else x for x in flow.get("missing", [])]
    if missing:
        out.append({"topic": "Approved steps not seen in the data", "observed_fact": ", ".join(missing),
                    "limitation": "They may happen outside the recorded applications, or no rule links their "
                                  "screens yet."})
    kept = sorted([e for e in flow.get("edges", []) if e.get("kept")], key=lambda e: -int(e.get("count") or 0))
    if kept:
        out.append({"topic": "Step transitions seen across people and weeks",
                    "observed_fact": "; ".join(f"{e['src']} -> {e['dst']}: {int(e.get('count') or 0):,} times"
                                               for e in kept[:6]),
                    "limitation": "Order of work inside one person's working episode, not a case hand-off."})
    bf = sorted([s for s in ins.values() if s.get("back_and_forth_share")],
                key=lambda s: -float(s.get("back_and_forth_share") or 0))
    if bf:
        out.append({"topic": "Back-and-forth between activities inside a step",
                    "observed_fact": "; ".join(f"{s['step']}: {_pct(s['back_and_forth_share'])} of runs" for s in bf[:4]),
                    "limitation": "Switching between two activities may be normal checking, not rework."})
    var = [s["step"] for s in ins.values() if (s.get("variation") or {}).get("flagged")]
    if var:
        out.append({"topic": "Steps whose run time differs a lot between people", "observed_fact": ", ".join(var),
                    "limitation": "Can reflect different case mixes; check with the SME before acting."})
    rep = [(s["step"], a["activity"]) for s in ins.values() for a in s.get("automation_candidates") or []]
    if rep:
        out.append({"topic": "Repetitive activities (same activity in most runs)",
                    "observed_fact": "; ".join(f"{a} in {s}" for s, a in rep[:6]),
                    "limitation": "Repetition is a signal for standardisation or automation, not proof."})
    return out


def evidence(folder: Path, job: dict) -> dict:
    """ProcessLens's transformation context for this analysis (+ this app's extra evidence). No raw data."""
    from app.api import ai_summary
    payload = ai_summary.safe_ai_payload(folder, job)
    ctx = ai_summary.build_transformation_context(payload, _extra(folder, job))
    steps = []
    if job.get("mode") == "nocase":
        try:                                  # names allowed in the answer (step and stage names may hold digits)
            from app.nocase import library as packs
            pack = packs.load_pack(job.get("process")) or {}
            steps = [{"step": sp.get("name"), "stage": st.get("name")} for st in pack.get("stages") or []
                     for sp in st.get("steps") or [] if sp.get("name")]
        except (ValueError, OSError):
            steps = []
    return {"context": ctx, "evidence_catalog": ctx["evidence_catalog"], "summary_payload": payload,
            "steps_in_business_order": steps, "mode": "no case ID" if job.get("mode") == "nocase" else "case ID"}


def messages(folder: Path, job: dict) -> list[dict]:
    """Exactly what ProcessLens sends: the system instruction, then its user line + the context as compact JSON."""
    from app.api import ai_summary
    ev = evidence(folder, job)
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER_LINE + "\n" + ai_summary.to_json(ev["context"])}]


def retry_messages(ev: dict, previous: str, problems: list[str]) -> list[dict]:
    """A small retry: the rules, the valid IDs, the previous answer and what to fix (not the whole evidence again),
    so it stays within the providers' per-minute token limits."""
    ids = "\n".join(f"{e['id']} | {e['topic']}" for e in ev["evidence_catalog"])
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Valid evidence IDs (id | topic):\n" + ids
                                        + "\n\nYour previous answer:\n" + str(previous)[:12000]
                                        + "\n\nIt broke these rules:\n- " + "\n- ".join(problems[:15])
                                        + "\nReturn the whole corrected JSON object only; keep what was right."}]


_ALLOWED = re.compile(r"\b(?:E\d{2}|Level\s?[1-5]|L[1-5]|S/4\s?HANA|Q[1-4]|24/7|B2B|P2P|O2C|R2R)\b", re.I)


def _names(ev: dict) -> list[str]:
    out = [s["step"] for s in ev.get("steps_in_business_order", [])] + \
          [s["stage"] for s in ev.get("steps_in_business_order", []) if s.get("stage")]
    for e in ev["evidence_catalog"]:
        out += re.split(r"[;:,]| -> ", str(e.get("topic", "")))
    return sorted({x.strip() for x in out if x and x.strip()}, key=len, reverse=True)


def check(report, ev: dict) -> list[str]:
    """Problems with an AI answer (empty = usable)."""
    if not isinstance(report, dict):
        return ["the answer is not one JSON object"]
    problems = []
    ids = {e["id"] for e in ev["evidence_catalog"]}
    for k in ("headline", "transformation_objective", "executive_message", "orchestrator_role"):
        if not str(report.get(k) or "").strip():
            problems.append(f"'{k}' is missing")
    for k, (lo, hi) in BOUNDS.items():
        v = report.get(k)
        if not isinstance(v, list) or len(v) < lo:
            problems.append(f"'{k}' needs at least {lo} item(s)")
        elif len(v) > hi:
            report[k] = v[:hi]
    for k in ("findings", "risks", "interventions"):
        for i, x in enumerate(report.get(k) or []):
            if not isinstance(x, dict) or x.get("evidence_id") not in ids:
                problems.append(f"{k}[{i}] cites an evidence_id that is not in the catalog")
    names = _names(ev)

    def scan(value, path):
        if isinstance(value, dict):
            for kk, vv in value.items():
                if kk != "evidence_id":
                    scan(vv, f"{path}.{kk}")
        elif isinstance(value, list):
            for j, vv in enumerate(value):
                scan(vv, f"{path}[{j}]")
        elif isinstance(value, str):
            t = value
            for n in names:
                t = t.replace(n, " ")
            t = _ALLOWED.sub(" ", t)
            if re.search(r"\d", t):
                problems.append(f"{path} contains a number: numbers come only from the cited evidence")
    scan({k: v for k, v in report.items()}, "answer")
    return problems[:30]


def load(folder: Path) -> dict | None:
    return _load(folder, FILE)


def save(folder: Path, report: dict, answer, fp: str) -> dict:
    doc = {"generated_at": _now(), "provider": answer.provider, "model": answer.model, "fingerprint": fp,
           "report": report}
    (folder / FILE).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc
