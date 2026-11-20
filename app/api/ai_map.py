"""AI-enhanced conceptual map, as in ProcessLens v2.8 ("Enhance the map with AI").

    summary  ai_summary.compact_map_payload of the aggregate summary: Evidence scope (case / navigation / combined)
             x Summary profile (compact / detailed). The "EXACT OUTGOING SUMMARY" shown before anything is sent.
    request  ProcessLens's MAP_SYSTEM and user line + the summary as compact JSON (task nc_process_map), through the
             approval gate: prepared, shown with its SHA-256, sent only after a named reviewer approves it.
    answer   ProcessLens's ProcessMapResult: headline, explanation, evidence, limitations, proposed_steps (id, label,
             explanation), proposed_edges (source, target, rationale), confidence. A HYPOTHESIS drawn beside the
             measured map, never a change to it.
Saved as ai_process_map.json in the job folder.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

TASK = "nc_process_map"
FILE = "ai_process_map.json"
SCOPES = ("case", "navigation", "combined")
BUDGETS = ("compact", "detailed")

# ProcessLens v2.8 services/ai.py MAP_SYSTEM, unchanged ...
MAP_SYSTEM = """You are a process discovery analyst asked to enhance a BASIC map with a
small conceptual hypothesis. You receive a SMALL, PREVIEWED aggregate JSON with opaque
step aliases, fixed taxonomy hints, bounded transition frequencies, and no event records.
Return 3-7 broadly described business stages with concise names and arrows among them.
Return credible evidence references (aggregate keys/counts), limitations and validation needs.
Never pretend that opaque identifiers have semantic meanings beyond their allowed generic hints.
If evidence is insufficient, say so and provide the least specific plausible flow. No-case
navigation is not proof of case process. Do not invent event counts, durations, business rules,
approval thresholds, actors, costs or case correlation. Your output is a separate HYPOTHESIS,
never a modification of the observed Python-computed map. Keep output compact.
Use at most 5 steps, 5 edges, 3 evidence bullets and 3 limitations; write one sentence per explanation.
Return complete JSON rather than a lengthy narrative."""

# ... and its ProcessMapResult schema (ProcessLens passes it to Gemini as response_schema).
OUTPUT_SCHEMA = """Return ONE JSON object with exactly these keys (ProcessMapResult):
{"headline": "...", "explanation": "...", "evidence": ["..."], "limitations": ["..."],
 "proposed_steps": [{"id": "S1", "label": "...", "explanation": "..."}],
 "proposed_edges": [{"source": "S1", "target": "S2", "rationale": "..."}],
 "confidence": "Low|Medium|High"}"""

SYSTEM = MAP_SYSTEM + "\n\n" + OUTPUT_SCHEMA
USER_LINE = "Use only this approved compact aggregate to propose a short conceptual process map:"   # ProcessLens


def default_scope(job: dict) -> str:
    return "navigation" if job.get("mode") == "nocase" else "case"


def summary(folder: Path, job: dict, scope: str, budget: str) -> dict:
    from app.api import ai_summary
    if scope not in SCOPES or budget not in BUDGETS:
        raise ValueError("Choose an evidence scope (case, navigation, combined) and a profile (compact, detailed).")
    return ai_summary.compact_map_payload(ai_summary.safe_ai_payload(folder, job), budget, scope)


def messages(payload: dict) -> list[dict]:
    from app.api import ai_summary
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER_LINE + "\n" + ai_summary.to_json(payload)}]


def check(report) -> list[str]:
    if not isinstance(report, dict):
        return ["the answer is not one JSON object"]
    problems = []
    for k in ("headline", "explanation", "confidence"):
        if not str(report.get(k) or "").strip():
            problems.append(f"'{k}' is missing")
    for k in ("evidence", "limitations"):
        if not isinstance(report.get(k), list):
            problems.append(f"'{k}' must be a list of short sentences")
    steps = report.get("proposed_steps")
    if not isinstance(steps, list) or not steps:
        problems.append("'proposed_steps' needs at least one step")
        steps = []
    ids = set()
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or not str(s.get("id") or "").strip() or not str(s.get("label") or "").strip():
            problems.append(f"proposed_steps[{i}] needs an id and a label")
        else:
            ids.add(str(s["id"]))
    for i, e in enumerate(report.get("proposed_edges") or []):
        if not isinstance(e, dict) or str(e.get("source")) not in ids or str(e.get("target")) not in ids:
            problems.append(f"proposed_edges[{i}] must join two ids of proposed_steps")
    return problems[:20]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(folder: Path) -> dict | None:
    p = folder / FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except ValueError:
        return None


def save(folder: Path, report: dict, answer, fp: str, scope: str, budget: str) -> dict:
    report = dict(report)
    report["proposed_steps"] = (report.get("proposed_steps") or [])[:7]
    report["proposed_edges"] = (report.get("proposed_edges") or [])[:10]
    doc = {"generated_at": _now(), "provider": answer.provider, "model": answer.model, "fingerprint": fp,
           "scope": scope, "budget": budget, "report": report}
    (folder / FILE).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc
