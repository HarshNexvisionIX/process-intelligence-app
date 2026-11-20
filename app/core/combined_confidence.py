"""Combined confidence: business knowledge (knowledge.py) x data evidence (evidence.py).

For every element of the definition (step, flow, owner, system, mandatory flag, SLA, plus
frequent activities the definition leaves out) the business belief and the data verdict
give a status and a number:

    belief >= threshold  + Supported     -> Confirmed
    belief >= threshold  + No data       -> Business only (the data cannot see it)
    belief >= threshold  + Partly        -> Partly confirmed
    belief >= threshold  + Contradicted  -> Documented ≠ actual (a finding, not an error)
    belief <  threshold  + Supported     -> Undocumented practice (a finding)
    open conflict on the element         -> Needs decision
    belief <  threshold  + other         -> Unverified

confidence = 100 x (0.5 x belief + 0.5 x data score); data score = Supported x badge factor,
Partly 0.5, Contradicted 0; with No data the belief alone counts, capped at 70. The
overall confidence is a weighted mean (steps x 2, flows x 1.5, others x 1) and is never
shown without the breakdown (elements per status).
"""
from __future__ import annotations

from app.core.config import settings
from app.core.definition import ProcessDefinition
from app.core.evidence import CONTRADICTED, NO_DATA, PARTLY, SUPPORTED

CONFIRMED, BUSINESS_ONLY, PARTLY_CONFIRMED = "Confirmed", "Business only", "Partly confirmed"
DOCUMENTED_NOT_ACTUAL, UNDOCUMENTED, NEEDS_DECISION, UNVERIFIED = (
    "Documented ≠ actual", "Undocumented practice", "Needs decision", "Unverified")
STATUSES = (CONFIRMED, BUSINESS_ONLY, PARTLY_CONFIRMED, DOCUMENTED_NOT_ACTUAL, UNDOCUMENTED, NEEDS_DECISION,
            UNVERIFIED)
STATUS_COLORS = {CONFIRMED: "#2f9e44", BUSINESS_ONLY: "#1c7ed6", PARTLY_CONFIRMED: "#e8a317",
                 DOCUMENTED_NOT_ACTUAL: "#d63a3a", UNDOCUMENTED: "#7048e8", NEEDS_DECISION: "#f76707",
                 UNVERIFIED: "#9aa5b1"}
STATUS_NOTES = {CONFIRMED: "green", BUSINESS_ONLY: "blue — the data cannot see it", PARTLY_CONFIRMED: "amber",
                DOCUMENTED_NOT_ACTUAL: "red outline — documented, but the data shows otherwise (a finding, not an error)",
                UNDOCUMENTED: "purple — done in practice, not documented (a finding)",
                NEEDS_DECISION: "orange — sources disagree", UNVERIFIED: "grey"}


def _cfg() -> dict:
    return settings()["combined"]


def status(belief: float, verdict: str, conflict_open: bool = False) -> str:
    """The status matrix (see the module docstring)."""
    if conflict_open:
        return NEEDS_DECISION
    if belief >= float(_cfg()["belief_threshold"]):
        return {SUPPORTED: CONFIRMED, NO_DATA: BUSINESS_ONLY, PARTLY: PARTLY_CONFIRMED,
                CONTRADICTED: DOCUMENTED_NOT_ACTUAL}.get(verdict, BUSINESS_ONLY)
    return UNDOCUMENTED if verdict == SUPPORTED else UNVERIFIED


def confidence(belief: float, verdict: str, badge: str | None = None) -> float:
    """0-100: 100 x (0.5 x belief + 0.5 x data score); with No data the belief alone, capped."""
    cfg = _cfg()
    if verdict not in (SUPPORTED, PARTLY, CONTRADICTED):
        return round(min(float(cfg["business_only_cap"]), 100 * belief), 1)
    data = {SUPPORTED: float(cfg["badge_factor"].get(badge or "Exploratory", 0.5)),
            PARTLY: float(cfg["partly_score"]), CONTRADICTED: 0.0}[verdict]
    return round(100 * (0.5 * belief + 0.5 * data), 1)


def overall(elements: list[dict]) -> dict:
    """{"score", "breakdown": {status: n}, "elements": n} — the weighted mean comes with the breakdown."""
    weights = _cfg()["weights"]
    total = sum(float(weights.get(e["kind"], 1.0)) for e in elements)
    score = (sum(float(weights.get(e["kind"], 1.0)) * e["confidence"] for e in elements) / total) if total else 0.0
    return {"score": round(score, 1), "breakdown": {s: sum(1 for e in elements if e["status"] == s) for s in STATUSES},
            "elements": len(elements)}


def breakdown_line(summary: dict) -> str:
    """"Map confidence 64 / 100 — 5 Confirmed · 2 Business only · ..." (never the number alone)."""
    parts = [f"{n} {s}" for s, n in summary["breakdown"].items() if n]
    return f"Map confidence {summary['score']:.0f} / 100 — " + (" · ".join(parts) or "no elements")


def _sources_text(statements: list[dict]) -> str:
    refs = sorted({f"{s['source_id']} {s['anchor'] if s['anchor'].startswith('p.') else '[' + s['anchor'] + ']'}"
                   for s in statements})
    return ", ".join(refs)


def combine(defn: ProcessDefinition, merged: dict | None, evidence: dict | None) -> dict:
    """Every element with its belief, data verdict, status, confidence and the tooltip text;
    plus the overall confidence and breakdown."""
    merged = merged or {"keys": {}, "conflicts": []}
    keys = merged["keys"]
    open_topics = {c["topic"] for c in merged["conflicts"] if c["status"] == "open"}
    claims = (evidence or {}).get("claims") or []
    by_name = {s["id"]: s["name"] for s in defn.steps}

    def claim(kind: str, **refs) -> dict | None:
        return next((c for c in claims if c["kind"] == kind
                     and all((c.get("refs") or {}).get(k) == v for k, v in refs.items())), None)

    def mentions(name: str) -> list[dict]:
        return [st for v in keys.values() for st in v["statements"]
                if st["subject"] == name or (st.get("object") == name and st["type"] in ("order", "loop_allowed"))]

    def belief_of(statements: list[dict]) -> float:
        from app.core.knowledge import belief
        per: dict[str, float] = {}
        for st in statements:
            per[st["source_id"]] = max(per.get(st["source_id"], 0.0), float(st.get("reliability") or 0))
        return belief(list(per.values())) if per else 0.0

    elements: list[dict] = []

    def add(kind: str, key: str, label: str, statements: list[dict], c: dict | None, topic: str | None, **ref) -> None:
        b = belief_of(statements)
        verdict = c["verdict"] if c and c["verdict"] in (SUPPORTED, PARTLY, CONTRADICTED) else NO_DATA
        badge = (c or {}).get("badge")
        st = status(b, verdict, topic in open_topics)
        data_text = (f"{verdict} ({(c or {}).get('support') or 0:,} cases, {badge})" if verdict != NO_DATA
                     else "No data")
        elements.append({"kind": kind, "key": key, "label": label, "belief": b, "verdict": verdict, "badge": badge,
                         "support": (c or {}).get("support") or 0, "claim": (c or {}).get("id"), "status": st,
                         "confidence": confidence(b, verdict, badge), "topic": topic,
                         "sources": sorted({s["source_id"] for s in statements}),
                         "tooltip": f"Sources: {_sources_text(statements) or 'none'} · Data: {data_text}", **ref})

    for s in defn.ordered_steps():
        n = s["name"]
        add("step", f"step:{n}", f"{n} is a step of the process", mentions(n), claim("step_exists", step_id=s["id"]),
            None, step_id=s["id"])
        if s.get("owner_role"):
            k = f"owner:{n}={s['owner_role']}"
            add("owner", k, f"{n} is done by {s['owner_role']}",
                [st for st in (keys.get(k) or {}).get("statements", [])], None, f"owner:{n}", step_id=s["id"])
        if s.get("system"):
            k = next((x for x in keys if x.lower() == f"system:{n}={s['system']}".lower()), f"system:{n}={s['system']}")
            add("system", k, f"{n} is done in {s['system']}", (keys.get(k) or {}).get("statements", []),
                claim("system", step_id=s["id"]), f"system:{n}", step_id=s["id"])
        m = "yes" if s.get("mandatory", True) else "no"
        k = f"mandatory:{n}={m}"
        add("mandatory", k, f"{n} is {'mandatory' if m == 'yes' else 'optional'}",
            (keys.get(k) or {}).get("statements", []), claim("mandatory", step_id=s["id"]) if m == "yes" else None,
            f"mandatory:{n}", step_id=s["id"])
        if s.get("sla_hours") is not None:
            k = next((x for x in keys if x.startswith(f"sla:{n}=") and abs(float(x.split("=", 1)[1]) - s["sla_hours"]) < 1e-6),
                     f"sla:{n}={s['sla_hours']:g}")
            add("sla", k, f"{n} within {s['sla_hours']:g} h", (keys.get(k) or {}).get("statements", []),
                claim("sla", step_id=s["id"]), f"sla:{n}", step_id=s["id"])
    for f in defn.effective_flows():
        a, b = by_name.get(f["from_step"]), by_name.get(f["to_step"])
        if not a or not b:
            continue
        if f["kind"] == "allowed_loop":
            k = f"loop_allowed:{a}>{b}"
            add("flow", k, f"{a} may go back to {b}", (keys.get(k) or {}).get("statements", []), None, None,
                from_step=f["from_step"], to_step=f["to_step"])
        else:
            k = f"order:{a}<{b}"
            add("flow", k, f"{a} before {b}", (keys.get(k) or {}).get("statements", []),
                claim("order", from_step=f["from_step"], to_step=f["to_step"]) if f["kind"] == "next" else None,
                "order:" + "|".join(sorted((a, b))), from_step=f["from_step"], to_step=f["to_step"])
    for c in claims:
        if c["kind"] == "uncovered_activity":
            act = (c.get("refs") or {}).get("activity")
            observed = {**c, "verdict": SUPPORTED}
            add("activity", f"activity:{act}", f"{act} is part of the work", mentions(act), observed, None,
                activity=act)
    return {"elements": elements, **{"summary": overall(elements)},
            "open_conflicts": len(open_topics)}


def findings(combined: dict, wanted: str, n: int = 5) -> list[dict]:
    """The top `n` elements with one status (most supporting cases first)."""
    return sorted((e for e in combined["elements"] if e["status"] == wanted),
                  key=lambda e: (-(e["support"] or 0), e["label"]))[:n]


def step_status(combined: dict | None) -> dict[str, dict]:
    return {e["step_id"]: e for e in (combined or {}).get("elements", []) if e["kind"] == "step"}


def flow_status(combined: dict | None) -> dict[tuple[str, str], dict]:
    return {(e["from_step"], e["to_step"]): e for e in (combined or {}).get("elements", []) if e["kind"] == "flow"}


def run(job: dict, defn: ProcessDefinition) -> dict:
    """Merge the definition's active sources, combine with the job's evidence, save combined.json."""
    from app.core import jobs, knowledge
    from app.core.evidence import load_evidence
    folder = jobs.job_folder(job["job_id"])
    merged = knowledge.merge(knowledge.all_statements(defn.name))
    result = combine(defn, merged, load_evidence(folder))
    result["conflicts"] = merged["conflicts"]
    knowledge.save_state(folder, "combined.json", result)
    return result


def load(folder) -> dict | None:
    from app.core.knowledge import load_state
    return load_state(folder, "combined.json")


def facts(combined: dict | None):
    """Combined statuses as facts: one per element, the counts per status and the overall
    confidence. Names, statuses and numbers only — never quotes from sources."""
    from app.core.facts import Fact, badge as badge_of
    if not combined:
        return []
    out = []
    s = combined["summary"]
    out.append(Fact(id="", type="combined_overall", label="Business process map confidence (business knowledge × data)",
                    value=s["score"], unit="score", support=None, section="map",
                    source="weighted mean over the elements of the definition; " + breakdown_line(s),
                    meta={"breakdown": s["breakdown"]}))
    for st, n in s["breakdown"].items():
        if n:
            out.append(Fact(id="", type="combined_count", label=f"Elements with status {st}", value=n, unit="count",
                            support=None, section="map", source="combined confidence", meta={"status": st}))
    for e in combined["elements"]:
        out.append(Fact(id="", type="combined_status", label=e["label"], value=None, unit="count",
                        support=e["support"] or None, section="map", text=f"{e['status']} ({e['confidence']:.0f} / 100)",
                        source="business sources × data evidence", meta={"status": e["status"], "kind": e["kind"]}))
    for f in out:
        f.badge = badge_of(f.value, f.support) if f.support else "Medium"
    return out
