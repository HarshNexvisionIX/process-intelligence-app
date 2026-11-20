"""Facts of the no-case report: F-numbered, verified numbers -> nc_facts.json.

Every fact records its value, unit, basis, confidence band and `ref`, the path of the number in nc_measures.json
(or nc_inside_step.json) it was taken from; the self-checks resolve every ref again, so a fact that does not
equal its source fails the report (DRAFT). Text never contains a number itself: summaries use {{F012}}
placeholders that are filled from these facts.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core import fmt

FACTS_FILE = "nc_facts.json"


def display(value, unit: str) -> str:
    if value is None:
        return fmt.DASH
    if unit == "hours":
        return fmt.hours(value)
    if unit == "share":
        return fmt.pct(value)
    if unit == "seconds":
        return fmt.duration_s(value)
    if unit == "count":
        return fmt.count(value)
    return fmt.number(value)


def resolve(root: dict, ref: str):
    """root = {"measures": ..., "inside": ...}; "measures.steps[step=Offer Processing].hours" -> the number."""
    cur = root
    for part in ref.split("."):
        if "[" in part:
            name, sel = part[:-1].split("[", 1)
            key, val = sel.split("=", 1)
            cur = next(x for x in cur[name] if str(x.get(key)) == val)
        else:
            cur = cur[part]
    return cur


def build(folder: Path) -> list[dict]:
    from app.nocase.engine.confidence import load as load_conf
    from app.nocase.engine.measures import UNASSIGNED, load as load_measures
    m, conf = load_measures(folder), load_conf(folder)
    basis = m["basis"]["time"]
    facts: list[dict] = []

    def add(label, ref, unit, band="Measured", basis_text=None, doc=m):
        value = resolve({"measures": doc}, ref)
        facts.append({"id": f"F{len(facts) + 1:03d}", "label": label, "value": value, "unit": unit, "ref": ref,
                      "basis": basis_text or (f"{basis} time" if unit == "hours" else "events in scope"),
                      "confidence": band, "display": display(value, unit)})
        return facts[-1]["id"]
    add("Events in scope", "measures.overall.events", "count")
    add("People (actors)", "measures.overall.actors", "count")
    add("Active hours", "measures.overall.hours", "hours")
    add("Step runs", "measures.overall.step_runs", "count", basis_text="consecutive events of one step by one actor in an episode")
    add("Share of hours without a step", "measures.overall.unassigned_share", "share")
    add("Hours in generic clicks", "measures.overall.generic_hours", "hours")
    add("Share of hours in the last days of the month", "measures.overall.month_end_share", "share")
    add("Application switches", "measures.overall.switches", "count")
    for s in m.get("steps") or []:
        if s["step"] == UNASSIGNED:
            continue
        b = (conf.get("steps") or {}).get(s["step"], {}).get("band", "Exploratory")
        key = f"measures.steps[step={s['step']}]"
        add(f"{s['step']}: active hours", f"{key}.hours", "hours", b)
        add(f"{s['step']}: share of hours", f"{key}.share_of_hours", "share", b)
        add(f"{s['step']}: people", f"{key}.actors", "count", b)
        add(f"{s['step']}: step runs", f"{key}.runs", "count", b)
        add(f"{s['step']}: median active time per run", f"{key}.median_run_s", "seconds", b, "median over step runs")
        add(f"{s['step']}: runs that repeat the step", f"{key}.repetition_share", "share", b,
            "runs of the step done again in the same episode")
    for st in m.get("stages") or []:
        add(f"{st['stage']}: active hours", f"measures.stages[stage={st['stage']}].hours", "hours")
    (folder / FACTS_FILE).write_text(json.dumps(facts, indent=2, default=str), encoding="utf-8")
    return facts


def add_inside(folder: Path, facts: list[dict]) -> list[dict]:
    """Facts from nc_inside_step.json (Phase 3) appended after the others, so no earlier id moves."""
    p = folder / "nc_inside_step.json"
    if not p.exists():
        return facts
    inside = json.loads(p.read_text(encoding="utf-8"))
    for st in inside.get("steps") or []:
        if not st.get("enough_runs"):
            continue
        key = f"inside.steps[step={st['step']}]"
        for label, ref, unit in ((f"{st['step']}: runs following the usual pattern", f"{key}.top_pattern_share", "share"),
                                 (f"{st['step']}: runs with rework inside the step", f"{key}.rework_share", "share"),
                                 (f"{st['step']}: median apps per run", f"{key}.median_apps_per_run", "number")):
            value = resolve({"inside": inside}, ref)
            facts.append({"id": f"F{len(facts) + 1:03d}", "label": label, "value": value, "unit": unit, "ref": ref,
                          "basis": "step runs", "confidence": "Exploratory", "display": display(value, unit)})
    (folder / FACTS_FILE).write_text(json.dumps(facts, indent=2, default=str), encoding="utf-8")
    return facts


def load(folder: Path) -> list[dict]:
    p = folder / FACTS_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
