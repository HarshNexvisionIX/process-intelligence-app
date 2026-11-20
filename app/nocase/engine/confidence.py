"""Combined confidence for every step assignment and every edge -> nc_confidence.json; conflicts become SME
questions -> nc_sme_questions.json.

    confidence = weighted average of the evidence scores (nc_evidence_scores.json) of the signals that SUPPORT the
                 item, minus the share of evidence that CONTRADICTS it; weights from nc_evidence_weights.yaml (the
                 job's, else the process library's, else nocase.confidence.default_weight for every signal);
                 `calibrate` (app/nocase/eval/calibrate.py) writes them from a dataset with truth
    bands      High >= nocase.confidence.bands.high, Medium >= medium, Low >= low, else Exploratory
Each item keeps its top 3 reasons in plain words.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from app.core.config import settings

CONF_FILE = "nc_confidence.json"
WEIGHTS_FILE = "nc_evidence_weights.yaml"
QUESTIONS_FILE = "nc_sme_questions.json"
# Which signals support an assignment, by the source that made it
SUPPORT = {"source_label": ["source_label"], "inferred_from_unit": ["source_label", "screen", "action"],
           "rule": ["library_rule", "screen", "action"], "episode_neighbours": ["episode_sequence", "co_occurrence"]}
AI_SUPPORT = ["screen", "action", "co_occurrence"]


def _cfg() -> dict:
    return settings()["nocase"]["confidence"]


def band(x: float | None) -> str:
    b = _cfg()["bands"]
    if x is None:
        return "Exploratory"
    return "High" if x >= float(b["high"]) else "Medium" if x >= float(b["medium"]) else "Low" if x >= float(b["low"]) \
        else "Exploratory"


def weights(folder: Path, process: str | None = None) -> dict:
    for p in [folder / WEIGHTS_FILE] + ([_library_weights(process)] if process else []):
        if p is not None and p.exists():
            return {k: float(v) for k, v in (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("weights", {}).items()}
    return {}


def _library_weights(process: str) -> Path | None:
    from app.nocase import library
    try:
        return library.pack_dir(process) / WEIGHTS_FILE
    except ValueError:
        return None


def support(signals: list[str], scores: dict, w: dict) -> tuple[float | None, list[str]]:
    used = [(s, scores[s]["score"], w.get(s, float(_cfg()["default_weight"]))) for s in signals
            if s in scores and scores[s].get("score") is not None]
    used = [x for x in used if x[2] > 0]
    if not used:
        return None, []
    val = sum(sc * wt for _, sc, wt in used) / sum(wt for *_, wt in used)
    return val, [f"{s.replace('_', ' ')} scores {sc:.2f}" for s, sc, _ in sorted(used, key=lambda x: -x[1] * x[2])]


def build(folder: Path, process: str | None = None) -> dict:
    from app.nocase.ai.evidence import load_scores
    from app.nocase.engine.flow import load as load_flow
    from app.nocase.engine.measures import UNASSIGNED, load as load_measures
    scores, w = load_scores(folder), weights(folder, process)
    m, flow = load_measures(folder), load_flow(folder)
    quality = json.loads((folder / "nc_assign_quality.json").read_text(encoding="utf-8")) \
        if (folder / "nc_assign_quality.json").exists() else {}
    conflicts = quality.get("conflicts") or []
    conflict_units = {c["unit_id"] for c in conflicts}
    unit_events = _unit_events(folder)
    steps = {}
    for s in m.get("steps") or []:
        if s["step"] == UNASSIGNED:
            continue
        by_src = {k: v for k, v in (s.get("hours_by_source") or {}).items() if k != "unassigned"}
        total = sum(by_src.values()) or 1.0
        val, reasons = 0.0, []
        known = 0.0
        for src, h in sorted(by_src.items(), key=lambda kv: -kv[1]):
            sigs = AI_SUPPORT if src.startswith("ai:") else SUPPORT.get(src, [])
            sv, why = support(sigs, scores, w)
            if sv is None:
                continue
            val += sv * h
            known += h
            reasons.append(f"{100 * h / total:.0f}% of hours from {src.replace('_', ' ')}: " + (why[0] if why else ""))
        sup = val / known if known else None
        events = unit_events.get(s["step"], {})
        contra = (sum(n for u, n in events.items() if u in conflict_units) / s["events"]) if s["events"] else 0.0
        if contra:
            reasons.append(f"{100 * contra:.1f}% of its events are in units with conflicting labels")
        conf = None if sup is None else max(0.0, sup - contra)
        steps[s["step"]] = {"confidence": None if conf is None else round(conf, 4), "band": band(conf),
                            "support": None if sup is None else round(sup, 4), "contradiction": round(contra, 4),
                            "reasons": reasons[:3] or ["no evidence scores for its sources"]}
    edges = []
    inferred = {(e["src"], e["dst"]): e for e in flow.get("edges") or []}
    es = scores.get("episode_sequence")
    es = es if es and es.get("score") is not None else None
    for e in [x for x in flow.get("edges") or [] if x["kept"]] + (flow.get("spine") or []):
        fwd = inferred.get((e["src"], e["dst"]), {}).get("count", 0)
        rev = inferred.get((e["dst"], e["src"]), {}).get("count", 0)
        parts = [x for x in (es["score"] if es else None, inferred.get((e["src"], e["dst"]), {}).get("consistency_across_actors"),
                             inferred.get((e["src"], e["dst"]), {}).get("consistency_across_weeks")) if x is not None]
        sup = (sum(parts) / len(parts)) if parts and fwd else None
        contra = rev / (fwd + rev) if fwd + rev else 0.0
        conf = None if sup is None else max(0.0, sup - contra)
        reasons = ([f"seen {fwd} time(s) in this direction, {rev} the other way"] if fwd + rev else
                   ["documented order only: no evidence in the episodes"])
        if es:
            reasons.append(f"episode sequence scores {es['score']:.2f}")
        edges.append({"src": e["src"], "dst": e["dst"], "status": e["status"],
                      "confidence": None if conf is None else round(conf, 4), "band": band(conf),
                      "reasons": reasons[:3]})
    out = {"weights": w or {"default": float(_cfg()["default_weight"])}, "steps": steps, "edges": edges}
    (folder / CONF_FILE).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    questions = sme_questions(conflicts, flow)
    (folder / QUESTIONS_FILE).write_text(json.dumps(questions, indent=2, default=str), encoding="utf-8")
    return out


def _unit_events(folder: Path) -> dict[str, dict[str, int]]:
    from app.core.db import connect, sql_literal
    from app.nocase.clean import CLEAN_FILE
    rows = connect().execute(f"""SELECT step, unit_id, COUNT(*) FROM read_parquet(
        {sql_literal((folder / CLEAN_FILE).as_posix())}) WHERE step IS NOT NULL GROUP BY 1, 2""").fetchall()
    out: dict[str, dict[str, int]] = {}
    for step, unit, n in rows:
        out.setdefault(step, {})[unit] = int(n)
    return out


def sme_questions(conflicts: list[dict], flow: dict) -> list[dict]:
    """Two strong signals that disagree: units whose labels split, and pairs of steps seen both ways round."""
    out = []
    for c in conflicts[:int(_cfg()["max_questions"])]:
        labels = ", ".join(f"{x['step']} {100 * x['share']:.0f}%" for x in c["labels"])
        out.append({"kind": "unit_step", "item": c["unit_id"],
                    "question": f"Which step is the work in unit {c['unit_id']} part of? Its labels disagree: {labels}."})
    seen = set()
    inferred = {(e["src"], e["dst"]): e for e in flow.get("edges") or []}
    for (a, b), e in inferred.items():
        r = inferred.get((b, a))
        if r and e["kept"] and r["kept"] and (b, a) not in seen:
            seen.add((a, b))
            out.append({"kind": "order", "item": f"{a} / {b}",
                        "question": f"Does {a} come before {b}? The episodes show both orders "
                                    f"({e['count']} and {r['count']} times)."})
    return out


def load(folder: Path) -> dict:
    p = folder / CONF_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
