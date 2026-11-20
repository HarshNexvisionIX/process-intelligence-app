"""SME approval lifecycle for process knowledge stored below ``data/library``.

The format deliberately shares the existing pack directory with ``app.nocase.library``:
the no-case engine continues to consume the same ``pack.yaml``, ``rules.yaml`` and
``activities.yaml`` files, while this module adds proposal review, decisions and snapshots.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.core.config import data_dir

FILES = ("pack.yaml", "rules.yaml", "activities.yaml", "order.yaml")
PROPOSED = ("pack.proposed.yaml", "rules.proposed.yaml", "activities.proposed.yaml", "order.proposed.yaml")


class LibraryView(dict):
    """Loaded process library with a convenient process-bound review API."""
    def propose(self, kind: str, items: list[dict] | dict, source: str = "sme") -> Path:
        return propose(self["process"], kind, items, source)

    def approve(self, ids: list[str], by: str, comment: str = "") -> dict:
        return approve(ids, by, comment, process=self["process"])

    def reject(self, ids: list[str], by: str, reason: str) -> dict:
        return reject(ids, by, reason, process=self["process"])

    def edit_rule(self, rule_id: str, new_match: dict, by: str) -> dict:
        return edit_rule(rule_id, new_match, by, process=self["process"])

    def history(self, item_id: str) -> list[dict]:
        return history(item_id, process=self["process"])


def _name(process: str) -> str:
    value = str(process or "").strip()
    if not value or value in {".", ".."} or not re.fullmatch(r"[\w .-]{1,100}", value, re.UNICODE):
        raise ValueError("Process name must be a plain folder name (letters, numbers, spaces, dots, _ or -).")
    return value


def process_dir(process: str) -> Path:
    return data_dir() / "library" / _name(process)


def _atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def _read_yaml(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return yaml.safe_load(path.read_text(encoding="utf-8")) or default


def _rules(doc: Any) -> list[dict]:
    if isinstance(doc, dict):
        return list(doc.get("rules") or [])
    return list(doc or []) if isinstance(doc, list) else []


def _all_items(folder: Path) -> dict[str, dict]:
    items: dict[str, dict] = {}
    for filename in PROPOSED:
        doc = _read_yaml(folder / filename, {})
        if filename.startswith("rules"):
            for item in _rules(doc):
                if item.get("id"):
                    items[str(item["id"])] = {**item, "_file": filename}
        elif filename.startswith("activities"):
            groups = doc.get("steps", doc) if isinstance(doc, dict) else {}
            for step, values in (groups or {}).items():
                for index, value in enumerate(values or []):
                    item = {"id": str(value.get("id") or f"activity:{step}:{index}"), "step": step,
                            **value, "_file": filename}
                    items[item["id"]] = item
        elif filename.startswith("pack"):
            for stage_i, stage in enumerate((doc or {}).get("stages") or []):
                for step_i, step in enumerate(stage.get("steps") or []):
                    item = {"id": str(step.get("id") or f"step:{stage_i}:{step_i}"), "stage": stage.get("name"),
                            **step, "_file": filename}
                    items[item["id"]] = item
        else:
            for index, item in enumerate((doc or {}).get("edges") or []):
                items[str(item.get("id") or f"edge:{index}")] = {**item, "_file": filename}
    return items


def load(process: str) -> dict:
    """Load approved/proposed knowledge, rejected fingerprints, and review history."""
    folder = process_dir(process)
    approved = {name: _read_yaml(folder / name, {} if name != "rules.yaml" else []) for name in FILES}
    proposed = {name: _read_yaml(folder / name, {} if name != "rules.proposed.yaml" else [])
                for name in PROPOSED}
    return LibraryView({"process": _name(process), "path": folder, "approved": approved, "proposed": proposed,
                        "items": _all_items(folder), "decisions": _read_decisions(folder)})


def _read_decisions(folder: Path) -> list[dict]:
    path = folder / "decisions.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _log(folder: Path, decision: dict) -> None:
    path = folder / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(decision, ensure_ascii=False, default=str) + "\n")


def _snapshot(folder: Path, by: str, summary: str) -> Path:
    versions = folder / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = versions / f"{stamp}-{re.sub(r'[^A-Za-z0-9_-]+', '-', by).strip('-') or 'reviewer'}"
    target.mkdir()
    for filename in FILES:
        source = folder / filename
        if source.exists():
            shutil.copyfile(source, target / filename)
    (target / "snapshot.json").write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                                       "by": by, "summary": summary}, indent=2), encoding="utf-8")
    return target


def propose(process: str, kind: str, items: list[dict] | dict, source: str = "sme") -> Path:
    """Write validated-by-caller proposal records; does not approve or apply them."""
    filename = kind if kind.endswith(".proposed.yaml") else f"{kind}.proposed.yaml"
    if filename not in PROPOSED:
        raise ValueError(f"Unknown knowledge type: {kind}")
    folder = process_dir(process)
    folder.mkdir(parents=True, exist_ok=True)
    if filename.startswith("rules"):
        incoming = _rules(items)
        pending = _rules(_read_yaml(folder / filename, []))
        by_id = {str(x.get("id")): x for x in pending if x.get("id")}
        by_id.update({str(x.get("id")): x for x in incoming if x.get("id")})
        rows = list(by_id.values())
        approved = _rules(_read_yaml(folder / "rules.yaml", []))
        rejected = {json.dumps(x.get("match") or {}, sort_keys=True) for x in approved if x.get("status") == "rejected"}
        rows = [dict(x, status="proposed", source=source) for x in rows
                if json.dumps(x.get("match") or {}, sort_keys=True) not in rejected]
        value: Any = {"rules": rows}
    elif isinstance(items, dict):
        value = items
    else:
        value = {"items": list(items)}
    _atomic(folder / filename, yaml.safe_dump(value, sort_keys=False, allow_unicode=True))
    return folder / filename


def _items_by_id(doc: Any, filename: str) -> dict[str, dict]:
    out = {}
    if filename.startswith("rules"):
        out.update({str(x.get("id")): x for x in _rules(doc) if x.get("id")})
    elif filename.startswith("activities"):
        for step, groups in ((doc or {}).get("steps") or {}).items():
            for i, group in enumerate(groups or []):
                out[str(group.get("id") or f"activity:{step}:{i}")] = {**group, "step": step}
    elif filename.startswith("pack"):
        for si, stage in enumerate((doc or {}).get("stages") or []):
            for ti, step in enumerate(stage.get("steps") or []):
                out[str(step.get("id") or f"step:{si}:{ti}")] = {**step, "stage": stage.get("name")}
    elif filename.startswith("order"):
        for i, edge in enumerate((doc or {}).get("edges") or []):
            out[str(edge.get("id") or f"edge:{i}")] = edge
    return out


def _store_item(folder: Path, filename: str, item: dict, item_id: str) -> None:
    target = folder / filename
    doc = _read_yaml(target, [] if filename == "rules.yaml" else {})
    if filename == "rules.yaml":
        rows = _rules(doc)
        rows = [row for row in rows if str(row.get("id")) != item_id]
        rows.append(item)
        _atomic(target, yaml.safe_dump({"rules": rows}, sort_keys=False, allow_unicode=True))
        return
    if filename == "activities.yaml":
        doc.setdefault("steps", {}).setdefault(str(item.get("step") or "Unassigned"), []).append(item)
    elif filename == "pack.yaml":
        stage_name = str(item.get("stage") or "Unassigned")
        stages = doc.setdefault("stages", [])
        stage = next((x for x in stages if x.get("name") == stage_name), None)
        if stage is None:
            stage = {"name": stage_name, "steps": []}
            stages.append(stage)
        stage.setdefault("steps", []).append({k: v for k, v in item.items() if k not in ("id", "stage", "_file")})
    elif filename == "order.yaml":
        doc.setdefault("edges", []).append(item)
    _atomic(target, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))


def _remove_from_proposed(folder: Path, filename: str, item_id: str) -> None:
    """Remove an item after an explicit decision so a pending card cannot be approved twice."""
    path = folder / filename
    doc = _read_yaml(path, [] if filename == "rules.proposed.yaml" else {})
    if filename.startswith("rules"):
        rows = [r for r in _rules(doc) if str(r.get("id")) != item_id]
        value: Any = {"rules": rows}
    elif filename.startswith("activities"):
        value = doc or {}
        for step, rows in list((value.get("steps") or {}).items()):
            value["steps"][step] = [r for i, r in enumerate(rows or [])
                                    if str(r.get("id") or f"activity:{step}:{i}") != item_id]
    elif filename.startswith("pack"):
        value = doc or {}
        for si, stage in enumerate(value.get("stages") or []):
            stage["steps"] = [r for ti, r in enumerate(stage.get("steps") or [])
                               if str(r.get("id") or f"step:{si}:{ti}") != item_id]
    else:
        value = doc or {}
        value["edges"] = [r for i, r in enumerate(value.get("edges") or [])
                           if str(r.get("id") or f"edge:{i}") != item_id]
    _atomic(path, yaml.safe_dump(value, sort_keys=False, allow_unicode=True))


def approve(ids: list[str], by: str, comment: str = "", process: str | None = None) -> dict:
    """Approve selected proposal IDs in a batch, snapshot approved files, and log each decision."""
    if not str(by or "").strip():
        raise ValueError("A reviewer name is required.")
    if not process:
        raise ValueError("Process is required.")
    folder = process_dir(process)
    all_items = _all_items(folder)
    selected = [all_items[str(item_id)] for item_id in ids if str(item_id) in all_items]
    if len(selected) != len(set(map(str, ids))):
        raise ValueError("One or more proposal IDs are unknown or no longer pending.")
    if not selected:
        raise ValueError("Select at least one proposal.")
    before = {}
    for row in selected:
        item_id, filename = str(row["id"]), str(row["_file"])
        base = filename.replace(".proposed", "")
        before[item_id] = row.copy()
        item = {k: v for k, v in row.items() if k != "_file"}
        item.update(status="sme_confirmed", source="sme", confidence=float(item.get("confidence", 1.0)))
        _store_item(folder, base, item, item_id)
        _remove_from_proposed(folder, filename, item_id)
        _log(folder, {"who": str(by).strip(), "when": datetime.now(timezone.utc).isoformat(), "item": item_id,
                      "action": "approved", "before": before[item_id], "after": item, "comment": comment})
    _snapshot(folder, str(by).strip(), f"Approved {len(selected)} item(s): {comment}".strip())
    return {"approved": [str(x["id"]) for x in selected], "count": len(selected)}


def reject(ids: list[str], by: str, reason: str, process: str | None = None) -> dict:
    """Retain rejected rules as do-not-propose fingerprints; log other rejected knowledge too."""
    if not str(by or "").strip() or not str(reason or "").strip():
        raise ValueError("Reviewer and rejection reason are required.")
    if not process:
        raise ValueError("Process is required.")
    folder = process_dir(process)
    items = _all_items(folder)
    selected = [items[str(i)] for i in ids if str(i) in items]
    if len(selected) != len(set(map(str, ids))):
        raise ValueError("One or more proposal IDs are unknown or no longer pending.")
    for row in selected:
        item_id, filename = str(row["id"]), str(row["_file"])
        item = {k: v for k, v in row.items() if k != "_file"}
        if filename.startswith("rules"):
            item.update(status="rejected", source="sme", rejection_reason=reason)
            _store_item(folder, "rules.yaml", item, item_id)
        _remove_from_proposed(folder, filename, item_id)
        _log(folder, {"who": str(by).strip(), "when": datetime.now(timezone.utc).isoformat(), "item": item_id,
                      "action": "rejected", "before": row, "after": {**item, "status": "rejected"},
                      "comment": reason})
    return {"rejected": [str(x["id"]) for x in selected], "count": len(selected)}


def edit_rule(rule_id: str, new_match: dict, by: str, process: str | None = None) -> dict:
    """Edit an approved rule's match while preserving history and versioning."""
    if not process or not str(by or "").strip():
        raise ValueError("Process and reviewer are required.")
    from app.core.level4 import static_regex_problem
    for key in ("screen", "action"):
        if new_match.get(key):
            issue = static_regex_problem(str(new_match[key]))
            if issue:
                raise ValueError(f"The {key} regex {issue}.")
    folder = process_dir(process)
    rows = _rules(_read_yaml(folder / "rules.yaml", []))
    current = next((r for r in rows if str(r.get("id")) == str(rule_id)), None)
    if current is None:
        raise ValueError(f"Unknown rule {rule_id}.")
    before = dict(current)
    current.update(match=dict(new_match), version=int(current.get("version", 1)) + 1,
                    status="sme_confirmed", source="sme")
    rows = [current if str(r.get("id")) == str(rule_id) else r for r in rows]
    _snapshot(folder, str(by).strip(), f"Before editing rule {rule_id}")
    _atomic(folder / "rules.yaml", yaml.safe_dump({"rules": rows}, sort_keys=False, allow_unicode=True))
    _log(folder, {"who": str(by).strip(), "when": datetime.now(timezone.utc).isoformat(), "item": str(rule_id),
                  "action": "edited", "before": before, "after": current, "comment": "Rule match edited"})
    return current


def history(item_id: str, process: str | None = None) -> list[dict]:
    if not process:
        raise ValueError("Process is required.")
    return [x for x in _read_decisions(process_dir(process)) if str(x.get("item")) == str(item_id)]

