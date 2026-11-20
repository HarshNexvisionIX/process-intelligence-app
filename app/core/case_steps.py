"""Case ID mode: the data's own process levels, checked against your documents, and the intended flow.

A case-ID export usually carries its own hierarchy: Level 1 (the process), Level 2 (the stage) and Level 3 (the
step = the activity), sometimes Level 4 (clicks). Those steps ARE the process as recorded. The documents (the
approved stages and steps of Business context) are used to CHECK them, step by step:

    same      the data step has the documented name (ignoring case and punctuation)          ✓
    similar   a very similar name or mostly the same words - please check                    ?
    none      no documented step looks like it ("not in the documents")                      ✗
    you       you linked it yourself (Review steps), or confirmed the suggestion

The INTENDED FLOW is the order the cases should follow. Until you confirm one it is prefilled: every data step
(none dropped), each under its Level 2 stage from the data, in the documented order where it matches a documented
step and at its usual place in the cases otherwise. On Review steps you correct it (move steps, take a step out of
the flow) and confirm it with your name; it is saved with the process (library/<process>/case_flow.json) and
reference.compute_for_job uses it first, so the map, the comparison, the report and the AI all use the same flow.

Files in the process library folder:
    case_links.json   {"links": {data step: documented step or ""}, "by", "at"}    the comparison you confirmed
    case_flow.json    {"order": reference.Order, "by", "at"}                        the intended flow you confirmed
"""
from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

LINKS_FILE = "case_links.json"
FLOW_FILE = "case_flow.json"
FILE = LINKS_FILE                    # kept for callers of the first version
OTHER = ""                           # "not in the documents"
SIMILAR = 0.8
WORDS = 0.5
_STOP = {"the", "a", "an", "of", "to", "for", "and", "in", "on", "new", "s"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _words(text: str) -> set[str]:
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in _norm(text).split()} - _STOP


def _pack(process: str | None) -> dict:
    from app.nocase import library as packs
    try:
        return (packs.load_pack(process) or {}) if process else {}
    except (OSError, ValueError):
        return {}


def approved_steps(process: str | None) -> list[dict]:
    """[{position, stage, step}] of the documented (approved) process, in its order."""
    out = []
    for st in _pack(process).get("stages") or []:
        for sp in st.get("steps") or []:
            if sp.get("name"):
                out.append({"position": len(out) + 1, "stage": st.get("name") or "", "step": sp["name"]})
    return out


def match(activities: list[str], steps: list[str]) -> dict[str, tuple[str, str]]:
    """data step -> (documented step or "", verdict same | similar | none). One documented step is suggested for
    at most one data step (exact names first, then the most similar). Never a guess below the thresholds."""
    out = {a: (OTHER, "none") for a in activities}
    taken: set[str] = set()
    by_norm = {_norm(s): s for s in steps}
    for a in activities:
        s = by_norm.get(_norm(a))
        if s and s not in taken:
            out[a] = (s, "same")
            taken.add(s)
    cands = []
    for a in activities:
        if out[a][1] == "same":
            continue
        wa = _words(a)
        for s in steps:
            if s in taken:
                continue
            ratio = difflib.SequenceMatcher(None, _norm(a), _norm(s)).ratio()
            ws = _words(s)
            jac = len(wa & ws) / len(wa | ws) if wa | ws else 0.0
            if ratio >= SIMILAR or jac >= WORDS:
                cands.append((max(ratio, jac), a, s))
    for _, a, s in sorted(cands, reverse=True):
        if out[a][1] == "none" and s not in taken:
            out[a] = (s, "similar")
            taken.add(s)
    return out


def auto_links(activities: list[str], steps: list[str]) -> dict[str, str]:
    return {a: m[0] for a, m in match(activities, steps).items()}


def _path(process: str, name: str = LINKS_FILE) -> Path:
    from app.nocase import library as packs
    return packs.pack_dir(process) / name


def _read(process: str | None, name: str) -> dict:
    if not process:
        return {}
    p = _path(process, name)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def load(process: str | None) -> dict:
    return _read(process, LINKS_FILE)


def load_flow(process: str | None) -> dict:
    return _read(process, FLOW_FILE)


def links(process: str | None, activities: list[str]) -> list[dict]:
    """One row per data step: {activity, step, verdict, source}. source: saved (confirmed by you) or suggested."""
    steps = [s["step"] for s in approved_steps(process)]
    saved = load(process).get("links") or {}
    auto = match(activities, steps)
    rows = []
    for a in activities:
        sug, verdict = auto.get(a, (OTHER, "none"))
        if a in saved and (saved[a] == OTHER or saved[a] in steps):
            v = verdict if saved[a] == sug else ("none" if saved[a] == OTHER else "you")
            rows.append({"activity": a, "step": saved[a], "verdict": v, "source": "saved", "suggested": sug})
        else:
            rows.append({"activity": a, "step": sug, "verdict": verdict, "source": "suggested", "suggested": sug})
    return rows


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save(process: str, new_links: dict[str, str], by: str) -> dict:
    """Keep the reviewer's comparison (data step -> documented step, "" = not in the documents)."""
    from app.nocase.knowledge import library as lib
    steps = {s["step"] for s in approved_steps(process)}
    bad = [s for s in new_links.values() if s and s not in steps]
    if bad:
        raise ValueError(f"Not an approved step: {', '.join(sorted(set(bad)))}")
    cur = load(process)
    merged = {**(cur.get("links") or {}), **{str(k): str(v or "") for k, v in new_links.items()}}
    doc = {"links": merged, "by": by, "at": _now()}
    folder = _path(process).parent
    lib._atomic(folder / LINKS_FILE, json.dumps(doc, ensure_ascii=False, indent=1))
    lib._log(folder, {"who": by, "when": doc["at"], "item": "case-activity-links", "action": "approved",
                      "before": cur.get("links") or {}, "after": merged})
    return doc


def reviewed(process: str | None, activities: list[str]) -> bool:
    saved = load(process).get("links") or {}
    return bool(activities) and all(a in saved for a in activities)


# --------------------------------------------------------------------------- data facts
def data_facts(events_path: Path) -> list[dict]:
    """[{activity, events, cases, stage, level1, position}] of the event log: the most frequent Level 2 (stage) and
    Level 1 of each step and its usual place in a case (median relative position of its first occurrence, 0..1)."""
    from app.core.db import connect
    p = events_path.as_posix()
    con = connect()
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p}')").fetchall()}
    stage = "mode(stage)" if "stage" in cols else "NULL"
    level1 = "mode(level1)" if "level1" in cols else "NULL"
    rows = con.execute(f"""
        WITH f AS (SELECT case_id, activity, MIN(ts_start) AS t FROM read_parquet('{p}')
                   WHERE activity IS NOT NULL AND case_id IS NOT NULL GROUP BY 1, 2),
             r AS (SELECT activity, (rank() OVER (PARTITION BY case_id ORDER BY t, activity) - 1)
                          / GREATEST(COUNT(*) OVER (PARTITION BY case_id) - 1, 1) AS rel FROM f),
             pos AS (SELECT activity, median(rel) AS position FROM r GROUP BY 1),
             e AS (SELECT activity, COUNT(*) AS events, COUNT(DISTINCT case_id) AS cases, {stage} AS stage,
                          {level1} AS level1 FROM read_parquet('{p}') WHERE activity IS NOT NULL GROUP BY 1)
        SELECT e.activity, e.events, e.cases, e.stage, e.level1, pos.position
        FROM e LEFT JOIN pos USING (activity) ORDER BY pos.position NULLS LAST, e.events DESC""").fetchall()
    return [{"activity": a, "events": int(n), "cases": int(c), "stage": st, "level1": l1,
             "position": None if pos is None else round(float(pos), 4)} for a, n, c, st, l1, pos in rows]


def activities_of(events_path: Path) -> list[dict]:
    return [{k: f[k] for k in ("activity", "events", "cases")} for f in data_facts(events_path)]


# --------------------------------------------------------------------------- intended flow
def prefilled_rows(process: str | None, facts: list[dict]) -> list[dict]:
    """The intended flow before you confirm one: EVERY data step, under its data stage (Level 2; else the
    documented stage of its match), in documented order where it matches a documented step, otherwise at its usual
    place in the cases. Steps that occur in under 5% of cases are left out of the flow (still shown)."""
    doc = approved_steps(process)
    doc_pos = {s["step"]: s["position"] for s in doc}
    doc_stage = {s["step"]: s["stage"] for s in doc}
    link = {r["activity"]: r["step"] for r in links(process, [f["activity"] for f in facts])} if doc else {}
    n_cases = max((f["cases"] for f in facts), default=0) or 1
    matched = sorted((f for f in facts if link.get(f["activity"])), key=lambda f: doc_pos[link[f["activity"]]])
    order = list(matched)
    for f in sorted((f for f in facts if not link.get(f["activity"])), key=lambda f: (f["position"] is None,
                                                                                         f["position"] or 0)):
        # after the last matched step that usually comes earlier in the cases
        at = 0
        for i, g in enumerate(order):
            if (g["position"] or 0) <= (f["position"] or 0):
                at = i + 1
        order.insert(at, f)
    rows, last_stage = [], ""
    for f in order:
        st = f.get("stage") or doc_stage.get(link.get(f["activity"]) or "", "") or last_stage
        last_stage = st
        rows.append({"stage": st or "", "step": f["activity"], "in_flow": f["cases"] / n_cases >= 0.05,
                     "documented": link.get(f["activity"]) or ""})
    return rows


def order_from_rows(process_name: str, rows: list[dict], source: str, confirmed: bool):
    """reference.Order from flow rows [{stage, step, in_flow}] (consecutive steps of one stage form one block)."""
    from app.core import reference
    stages: list[dict] = []
    for r in rows:
        if not r.get("in_flow", True):
            continue
        name = r.get("stage") or reference.NO_STAGE
        if not stages or stages[-1]["name"] != name:
            stages.append({"name": name, "steps": []})
        stages[-1]["steps"].append(r["step"])
    other = [r["step"] for r in rows if not r.get("in_flow", True)]
    return reference.Order(process_name=process_name, stages=stages, other_steps=other, source=source,
                           confirmed_by_business=confirmed)


def save_flow(process: str, rows: list[dict], by: str, process_name: str) -> dict:
    from app.nocase.knowledge import library as lib
    if sum(1 for r in rows if r.get("in_flow", True)) < 2:
        raise ValueError("The intended flow needs at least two steps.")
    order = order_from_rows(process_name, rows, "edited", True)
    cur = load_flow(process)
    doc = {"order": order.to_dict(), "rows": rows, "by": by, "at": _now()}
    folder = _path(process).parent
    lib._atomic(folder / FLOW_FILE, json.dumps(doc, ensure_ascii=False, indent=1))
    lib._log(folder, {"who": by, "when": doc["at"], "item": "case-intended-flow", "action": "approved",
                      "before": (cur.get("order") or {}).get("stages"), "after": order.to_dict()["stages"]})
    return doc


def approved_order(job: dict, activities: list[str], events_path: Path | None = None):
    """The intended flow for this file (reference.Order): the one you confirmed for the process, else the
    prefilled one when the process has documented steps; None otherwise (the order found in the data is used)."""
    from app.core import reference
    from app.nocase import library as packs
    process = job.get("process")
    if not process:
        return None
    name = packs.display_name(process)
    saved = load_flow(process)
    if saved.get("order"):
        order = reference.Order.from_dict({**saved["order"], "process_name": name})
        present = set(activities)
        drawn = [s for s in order.drawn_steps() if s in present]
        if len(drawn) >= 2:
            new = [a for a in activities if a not in set(order.drawn_steps()) and a not in set(order.other_steps)]
            return reference.Order(process_name=name,
                                   stages=[{"name": st["name"], "steps": [s for s in st["steps"] if s in present]}
                                           for st in order.stages if any(s in present for s in st["steps"])],
                                   other_steps=[s for s in order.other_steps if s in present] + new,
                                   source="edited", confirmed_by_business=True)
    if not approved_steps(process) or events_path is None:
        return None
    rows = prefilled_rows(process, data_facts(events_path))
    if sum(1 for r in rows if r["in_flow"]) < 2:
        return None
    return order_from_rows(name, rows, "evidence", False)


def flow_rows(job: dict, folder: Path, facts: list[dict]) -> tuple[list[dict], str]:
    """Rows of the intended flow as shown on Review steps, and where they come from (confirmed | prefilled | data)."""
    process = job.get("process")
    saved = load_flow(process)
    present = {f["activity"] for f in facts}
    if saved.get("rows"):
        rows = [r for r in saved["rows"] if r["step"] in present]
        known = {r["step"] for r in rows}
        stage_of = {f["activity"]: f.get("stage") or "" for f in facts}
        rows += [{"stage": stage_of[a], "step": a, "in_flow": False, "new": True} for a in
                 (f["activity"] for f in facts) if a not in known]
        return rows, "confirmed"
    if approved_steps(process):
        return prefilled_rows(process, facts), "prefilled"
    ref = None
    try:
        ref = json.loads((folder / "reference.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if ref:
        rows = [{"stage": st["name"], "step": s, "in_flow": True} for st in ref["order"]["stages"] for s in st["steps"]]
        rows += [{"stage": "", "step": s, "in_flow": False} for s in ref["order"].get("other_steps") or []]
        return rows, "data"
    return [{"stage": f.get("stage") or "", "step": f["activity"], "in_flow": True} for f in facts], "data"


def missing_steps(process: str | None, activities: list[str]) -> list[dict]:
    """Documented steps that no data step is linked to."""
    linked = {r["step"] for r in links(process, activities) if r["step"]}
    return [s for s in approved_steps(process) if s["step"] not in linked]
