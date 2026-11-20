"""Questions for the SME on a no-case analysis, with their saved answers (shared by both front ends).

Sources: the analysis' own questions (nc_sme_questions.json), the evidence plan's questions_for_sme, and questions a
reviewer added. Answers are kept in the process library's decision log (action "sme_answer", item "question:<key>").
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.nocase.knowledge import library


def all_questions(folder: Path, process: str | None) -> list[dict]:
    out: list[dict] = []
    q = folder / "nc_sme_questions.json"
    if q.exists():
        try:
            for x in json.loads(q.read_text(encoding="utf-8")) or []:
                if isinstance(x, dict) and x.get("question"):
                    out.append({"key": f"{x.get('kind', 'q')}:{x.get('item', x['question'])}",
                                "question": x["question"], "kind": x.get("kind") or "question"})
        except (OSError, ValueError):
            pass
    plan = folder / "nc_evidence_plan.json"
    if plan.exists():
        try:
            for x in json.loads(plan.read_text(encoding="utf-8")).get("questions_for_sme") or []:
                out.append({"key": f"plan:{x}", "question": str(x), "kind": "evidence plan"})
        except (OSError, ValueError):
            pass
    answers = {}
    if process:
        for d in library._read_decisions(library.process_dir(process)):
            if d.get("action") == "sme_answer":
                answers[str(d.get("item"))] = d
    for x in out:
        a = answers.get(f"question:{x['key']}")
        x["answer"], x["by"] = (a.get("after"), a.get("who")) if a else (None, None)
    return out


def save_answer(process: str, key: str, question: str, answer: str, by: str) -> None:
    library._log(library.process_dir(process), {
        "who": by, "when": datetime.now(timezone.utc).isoformat(), "item": f"question:{key}",
        "action": "sme_answer", "before": question, "after": answer, "comment": ""})


def add_question(folder: Path, question: str, kind: str = "reviewer") -> dict:
    path = folder / "nc_sme_questions.json"
    items = []
    if path.exists():
        try:
            items = json.loads(path.read_text(encoding="utf-8")) or []
        except ValueError:
            items = []
    item = {"kind": kind, "item": f"added-{len(items) + 1}", "question": question.strip()}
    items.append(item)
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    return item
