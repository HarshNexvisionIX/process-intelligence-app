"""The business process definition: the process as the business defines it.

A ProcessDefinition holds the stages, steps (owner, system, rules, SLA, the
activities in the data that belong to each step) and flows between steps. It is
entered on the "Business process" page (from the data, a template, a document or
an uploaded file), checked against the data by code (evidence.py), corrected,
approved by a person and versioned.

Storage: <data_dir>/process_definitions/<slug(name)>/v<version>.json (written
atomically) plus current.json = {"latest_approved": n | null, "latest_draft": n | null}.
A job links to the definition it was checked against: job.json -> definition.

Import / export: Excel (sheets Process, Stages, Steps, Flows), a CSV of steps,
YAML / JSON, and BPMN 2.0 XML import (tasks, sequence flows, lanes). The BPMN
export with diagram layout lives in business_map.py.
"""
from __future__ import annotations

import copy
import io
import json
import math
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from app.core.config import data_dir

FLOW_KINDS = ("next", "allowed_loop", "optional_skip")
STEP_FIELDS = ("id", "name", "stage_id", "position", "owner_role", "system", "description", "inputs", "outputs",
               "mandatory", "sla_hours", "business_rules", "mapped_activities", "milestone_labels",
               "standard_path_ref", "position_source")
STAGE_FIELDS = ("id", "name", "owner_role")
FLOW_FIELDS = ("from_step", "to_step", "kind", "condition")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 1. The model
# --------------------------------------------------------------------------- #
@dataclass
class ProcessDefinition:
    name: str
    description: str = ""
    version: int = 1
    status: str = "draft"                     # "draft" | "approved"
    approved_by: str | None = None
    approved_at: str | None = None
    start_trigger: str = ""
    end_result: str = ""
    stages: list[dict] = field(default_factory=list)       # [{id, name, owner_role}]
    steps: list[dict] = field(default_factory=list)        # see STEP_FIELDS
    flows: list[dict] = field(default_factory=list)        # [{from_step, to_step, kind, condition}]
    change_log: list[dict] = field(default_factory=list)   # [{version, at, by, summary}]
    source: str = ""                          # how the draft was started ("From the data", "Drafted by AI ...")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProcessDefinition":
        d = d or {}
        return cls(name=str(d.get("name") or "Process").strip() or "Process",
                   description=str(d.get("description") or ""), version=int(d.get("version") or 1),
                   status="approved" if d.get("status") == "approved" else "draft",
                   approved_by=d.get("approved_by"), approved_at=d.get("approved_at"),
                   start_trigger=str(d.get("start_trigger") or ""), end_result=str(d.get("end_result") or ""),
                   stages=[_stage(s) for s in d.get("stages") or []],
                   steps=[_step(s, i) for i, s in enumerate(d.get("steps") or [], 1)],
                   flows=[_flow(f) for f in d.get("flows") or []],
                   change_log=list(d.get("change_log") or []), source=str(d.get("source") or ""))

    # --- helpers ---
    def step(self, step_id: str) -> dict | None:
        return next((s for s in self.steps if s["id"] == step_id), None)

    def stage_name(self, stage_id: str) -> str:
        return next((s["name"] for s in self.stages if s["id"] == stage_id), stage_id or "")

    def ordered_steps(self) -> list[dict]:
        return sorted(self.steps, key=lambda s: (_num(s.get("position"), 1e9), s["id"]))

    def effective_flows(self) -> list[dict]:
        """The flows; when none are given, "next" flows follow the position order."""
        if self.flows:
            return list(self.flows)
        steps = self.ordered_steps()
        return [{"from_step": a["id"], "to_step": b["id"], "kind": "next", "condition": ""}
                for a, b in zip(steps, steps[1:])]

    def activity_to_step(self) -> dict[str, str]:
        """{activity name in the data: step id} (an activity belongs to its first step)."""
        out: dict[str, str] = {}
        for s in self.ordered_steps():
            for a in s.get("mapped_activities") or []:
                out.setdefault(a, s["id"])
        return out


def _num(v, default=None):
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def _text(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v).strip()


def _list(v) -> list[str]:
    """A list from a list, or from "a; b; c" text (Excel / CSV cells)."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).split(";") if x.strip()]


def _bool(v, default=True) -> bool:
    if isinstance(v, bool):
        return v
    t = _text(v).lower()
    if t in ("yes", "y", "true", "1", "x", "mandatory"):
        return True
    if t in ("no", "n", "false", "0", "optional"):
        return False
    return default


def _stage(s: dict) -> dict:
    return {"id": _text(s.get("id")) or _text(s.get("name")), "name": _text(s.get("name")) or _text(s.get("id")),
            "owner_role": _text(s.get("owner_role"))}


def _step(s: dict, i: int) -> dict:
    sla = _num(s.get("sla_hours"))
    return {"id": _text(s.get("id")) or f"S{i:02d}", "name": _text(s.get("name")),
            "stage_id": _text(s.get("stage_id")), "position": int(_num(s.get("position"), i)),
            "owner_role": _text(s.get("owner_role")), "system": _text(s.get("system")),
            "description": _text(s.get("description")), "inputs": _text(s.get("inputs")),
            "outputs": _text(s.get("outputs")), "mandatory": _bool(s.get("mandatory"), True),
            "sla_hours": sla, "business_rules": _text(s.get("business_rules")),
            "mapped_activities": _list(s.get("mapped_activities")),
            "milestone_labels": _list(s.get("milestone_labels")),
            "standard_path_ref": _text(s.get("standard_path_ref")) or None,
            # Where the step's POSITION comes from (order_evidence.py): document | milestones | data | sme.
            "position_source": _text(s.get("position_source")) or None}


def _flow(f: dict) -> dict:
    kind = _text(f.get("kind")) or "next"
    return {"from_step": _text(f.get("from_step")), "to_step": _text(f.get("to_step")),
            "kind": kind if kind in FLOW_KINDS else "next", "condition": _text(f.get("condition"))}


# --------------------------------------------------------------------------- #
# 2. Validation (code only)
# --------------------------------------------------------------------------- #
def validate(defn: ProcessDefinition, activities: list[str] | None = None) -> tuple[list[str], list[str]]:
    """(errors, warnings). Errors block saving as approved; warnings are shown.

    Errors: duplicate stage / step ids, duplicate positions, a step without a name or in an
    unknown stage, a flow to or from an unknown step, a cycle in the "next" flows.
    Warnings (with `activities`, the job's activity names): mapped activities not in the data.
    """
    errors, warnings = [], []
    stage_ids = [s["id"] for s in defn.stages]
    for dup in sorted({x for x in stage_ids if stage_ids.count(x) > 1}):
        errors.append(f"Stage id '{dup}' is used more than once.")
    step_ids = [s["id"] for s in defn.steps]
    for dup in sorted({x for x in step_ids if step_ids.count(x) > 1}):
        errors.append(f"Step id '{dup}' is used more than once.")
    positions = [s.get("position") for s in defn.steps]
    for dup in sorted({p for p in positions if positions.count(p) > 1}, key=str):
        names = ", ".join(s["name"] or s["id"] for s in defn.steps if s.get("position") == dup)
        errors.append(f"Position {dup} is used by more than one step ({names}).")
    for s in defn.steps:
        if not s["name"]:
            errors.append(f"Step '{s['id']}' has no name.")
        if defn.stages and s["stage_id"] not in stage_ids:
            errors.append(f"Step '{s['name'] or s['id']}' is in an unknown stage '{s['stage_id']}'.")
        if s.get("sla_hours") is not None and s["sla_hours"] <= 0:
            errors.append(f"Step '{s['name']}' has an SLA that is not a positive number of hours.")
    known = set(step_ids)
    for f in defn.flows:
        for end in ("from_step", "to_step"):
            if f[end] not in known:
                errors.append(f"A flow refers to an unknown step '{f[end]}' ({f['from_step']} → {f['to_step']}).")
    cycle = next_cycle(defn)
    if cycle:
        names = " → ".join((defn.step(x) or {}).get("name") or x for x in cycle)
        errors.append(f"The 'next' flows go round in a circle ({names}); mark the flow back as an allowed loop.")
    if activities is not None:
        present = set(activities)
        for s in defn.steps:
            missing = [a for a in s.get("mapped_activities") or [] if a not in present]
            if missing:
                warnings.append(f"Step '{s['name']}': mapped activities not in this data: {', '.join(missing)}.")
            if not s.get("mapped_activities"):
                warnings.append(f"Step '{s['name']}' is not mapped to any activity in the data.")
    return errors, warnings


def next_cycle(defn: ProcessDefinition) -> list[str] | None:
    """A cycle in the "next" flows (allowed loops excluded), as step ids, or None."""
    graph: dict[str, list[str]] = {}
    for f in defn.effective_flows():
        if f["kind"] == "next":
            graph.setdefault(f["from_step"], []).append(f["to_step"])
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                return path[path.index(nxt):] + [nxt]
            if state.get(nxt) is None:
                found = visit(nxt)
                if found:
                    return found
        state[node] = 2
        path.pop()
        return None

    for node in list(graph):
        if state.get(node) is None:
            found = visit(node)
            if found:
                return found
    return None


# --------------------------------------------------------------------------- #
# 3. Storage and versions
# --------------------------------------------------------------------------- #
def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "process").lower()).strip("-")
    return s or "process"


def definitions_dir() -> Path:
    d = data_dir() / "process_definitions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".def_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _folder(name: str) -> Path:
    folder = definitions_dir() / slug(name)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def versions(name: str) -> list[int]:
    folder = definitions_dir() / slug(name)
    if not folder.exists():
        return []
    return sorted(int(p.stem[1:]) for p in folder.glob("v*.json") if p.stem[1:].isdigit())


def current(name: str) -> dict:
    path = definitions_dir() / slug(name) / "current.json"
    if not path.exists():
        return {"latest_approved": None, "latest_draft": None}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(defn: ProcessDefinition) -> Path:
    """Write v<version>.json (atomically) and update current.json."""
    folder = _folder(defn.name)
    path = folder / f"v{defn.version}.json"
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous.get("status") == "approved" and previous != defn.to_dict():
            raise ValueError("An approved version is immutable; create a new draft version before editing.")
    _write_json(path, defn.to_dict())
    cur = current(defn.name)
    key = "latest_approved" if defn.status == "approved" else "latest_draft"
    if cur.get(key) is None or defn.version >= cur[key]:
        cur[key] = defn.version
    if defn.status == "approved" and cur.get("latest_draft") == defn.version:
        cur["latest_draft"] = None              # this version is no longer a draft
    _write_json(folder / "current.json", cur)
    return path


def load(name: str, version: int | None = None, which: str = "latest") -> ProcessDefinition | None:
    """A version; or `which` = "draft" / "approved" / "latest" (the highest version)."""
    if version is None:
        cur = current(name)
        if which == "draft":
            version = cur.get("latest_draft")
        elif which == "approved":
            version = cur.get("latest_approved")
        else:
            vs = versions(name)
            version = vs[-1] if vs else None
    if version is None:
        return None
    path = definitions_dir() / slug(name) / f"v{version}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return ProcessDefinition.from_dict(json.load(f))


def list_definitions() -> list[str]:
    """Names of every saved definition (from their latest version)."""
    out = []
    for folder in sorted(definitions_dir().iterdir()):
        if folder.is_dir():
            d = load(folder.name)
            if d is not None:
                out.append(d.name)
    return out


def next_version(name: str) -> int:
    vs = versions(name)
    return (vs[-1] + 1) if vs else 1


def new_draft(defn: ProcessDefinition, by: str, summary: str) -> ProcessDefinition:
    """A copy as the next draft version, with a change-log entry (not saved)."""
    d = ProcessDefinition.from_dict(copy.deepcopy(defn.to_dict()))
    d.version = next_version(d.name)
    d.status, d.approved_by, d.approved_at = "draft", None, None
    d.change_log.append({"version": d.version, "at": _now(), "by": by or "unknown", "summary": summary})
    return d


def approve(defn: ProcessDefinition, by: str) -> ProcessDefinition:
    """Mark this version approved by `by` (now) and save it. Needs a valid definition."""
    errors, _ = validate(defn)
    if errors:
        raise ValueError("The definition has errors: " + " ".join(errors))
    if not (by or "").strip():
        raise ValueError("Enter the name of the person who approves.")
    defn.status, defn.approved_by, defn.approved_at = "approved", by.strip(), _now()
    defn.change_log.append({"version": defn.version, "at": defn.approved_at, "by": by.strip(),
                            "summary": "Approved"})
    save(defn)
    return defn


def to_order(defn: ProcessDefinition):
    """The definition as a step order (reference.Order): stages in order, each step's mapped
    activities in position order. Confirmed by the business only when approved."""
    from app.core.reference import Order   # local import avoids a cycle
    stages: list[dict] = []
    for s in defn.ordered_steps():
        name = defn.stage_name(s["stage_id"])
        if not stages or stages[-1]["name"] != name:
            stages.append({"name": name, "steps": []})
        stages[-1]["steps"] += [a for a in s.get("mapped_activities") or [] if a not in
                                {x for st in stages for x in st["steps"]}]
    mandatory = [a for s in defn.steps if s.get("mandatory") for a in s.get("mapped_activities") or []]
    return Order(defn.name, [s for s in stages if s["steps"]], [], "edited", defn.status == "approved", _now(),
                 mandatory_steps=mandatory)


def link_job(job: dict, defn: ProcessDefinition) -> None:
    """job.json -> definition: {name, version, status}."""
    from app.core import jobs
    job["definition"] = {"name": defn.name, "version": defn.version, "status": defn.status}
    jobs.save_job(job)


def for_job(job: dict) -> ProcessDefinition | None:
    """The definition version the job was checked against (job.json -> definition)."""
    link = job.get("definition") or {}
    return load(link["name"], link.get("version")) if link.get("name") else None


def make_confirmed_order(job: dict, defn: ProcessDefinition) -> bool:
    """When `defn` is approved, save it as the confirmed step order for the job's layout
    (order source "confirmed"); a draft never changes the saved order. Returns True when saved."""
    if defn.status != "approved":
        return False
    from app.core import reference
    reference.save_order(reference.order_key(job), to_order(defn))
    confirm_linked_standards(job, defn)
    return True


def standard_key(step: dict) -> str | None:
    """The saved step standard a definition step links to (subprocess.py, keyed by activity):
    `standard_path_ref`, else the step's first mapped activity."""
    return step.get("standard_path_ref") or next(iter(step.get("mapped_activities") or []), None)


def linked_standards(job: dict, defn: ProcessDefinition) -> dict[str, dict]:
    """{step id: saved standard} for the steps whose linked standard exists for the job's layout."""
    from app.core.reference import order_key
    from app.core.subprocess import load_standards
    saved = load_standards(order_key(job))
    out = {}
    for s in defn.steps:
        key = standard_key(s)
        if key and key in saved:
            out[s["id"]] = {"key": key, **saved[key]}
    return out


def confirm_linked_standards(job: dict, defn: ProcessDefinition) -> list[str]:
    """Approving a definition marks the step standards it links to as confirmed by the business."""
    if defn.status != "approved":
        return []
    from app.core.reference import order_key
    from app.core.subprocess import confirm_standards
    keys = sorted({v["key"] for v in linked_standards(job, defn).values()})
    confirm_standards(order_key(job), keys)
    return keys


def diff(old: ProcessDefinition | None, new: ProcessDefinition) -> list[str]:
    """Plain lines: what changed from `old` (e.g. the last approved version) to `new`."""
    if old is None:
        return [f"First version: {len(new.stages)} stages, {len(new.steps)} steps, {len(new.effective_flows())} flows."]
    out = []
    for key in ("name", "description", "start_trigger", "end_result"):
        if getattr(old, key) != getattr(new, key):
            out.append(f"{key.replace('_', ' ').capitalize()} changed.")
    olds, news = {s["id"]: s for s in old.steps}, {s["id"]: s for s in new.steps}
    for sid in news.keys() - olds.keys():
        out.append(f"Step added: {news[sid]['name']}.")
    for sid in olds.keys() - news.keys():
        out.append(f"Step removed: {olds[sid]['name']}.")
    for sid in sorted(news.keys() & olds.keys()):
        changed = [k for k in STEP_FIELDS if olds[sid].get(k) != news[sid].get(k)]
        if changed:
            out.append(f"Step {news[sid]['name']}: {', '.join(c.replace('_', ' ') for c in changed)} changed.")
    oldf = {(f["from_step"], f["to_step"], f["kind"]) for f in old.effective_flows()}
    newf = {(f["from_step"], f["to_step"], f["kind"]) for f in new.effective_flows()}
    for f in sorted(newf - oldf):
        out.append(f"Flow added: {f[0]} → {f[1]} ({f[2]}).")
    for f in sorted(oldf - newf):
        out.append(f"Flow removed: {f[0]} → {f[1]} ({f[2]}).")
    return out or ["No changes."]


# --------------------------------------------------------------------------- #
# 4. Starting a definition: from the data, from a template
# --------------------------------------------------------------------------- #
def from_order(order, process_name: str, step_apps: dict[str, str] | None = None,
               median_hours: dict[str, float] | None = None, source: str = "From the data") -> ProcessDefinition:
    """A draft from a step order (reference.Order): one step per activity, stages in order.

    `step_apps` (most-used application per activity) prefills the system; `median_hours`
    goes into the description as a hint only (never as an SLA).
    """
    stages, steps = [], []
    for si, stage in enumerate(order.stages, 1):
        sid = f"ST{si:02d}"
        stages.append({"id": sid, "name": stage["name"] or "Process", "owner_role": ""})
        for a in stage["steps"]:
            hint = (median_hours or {}).get(a)
            steps.append({"id": f"S{len(steps) + 1:02d}", "name": a, "stage_id": sid, "position": len(steps) + 1,
                          "system": (step_apps or {}).get(a) or "", "mandatory": a in (order.mandatory_steps or [a]),
                          "description": f"Observed median time in the step: {hint:.1f} h (a hint, not an SLA)."
                          if hint is not None else "", "mapped_activities": [a]})
    d = ProcessDefinition.from_dict({"name": process_name, "stages": stages, "steps": steps, "source": source})
    return d


def from_template(template: dict, mapping: dict[str, str | None], process_name: str | None = None
                  ) -> ProcessDefinition:
    """A draft from a process template (templates.py): its stages and steps, each step mapped
    to the job's activities that matched it."""
    stages, steps = [], []
    for si, stage in enumerate(template["stages"], 1):
        sid = f"ST{si:02d}"
        stages.append({"id": sid, "name": stage["name"], "owner_role": ""})
        for t in stage["steps"]:
            steps.append({"id": f"S{len(steps) + 1:02d}", "name": t["name"], "stage_id": sid,
                          "position": len(steps) + 1, "mandatory": bool(t.get("mandatory")),
                          "mapped_activities": sorted(a for a, s in mapping.items() if s == t["name"])})
    return ProcessDefinition.from_dict({"name": process_name or template["name"], "stages": stages, "steps": steps,
                                        "source": f"Template: {template['name']}"})


# --------------------------------------------------------------------------- #
# 5. Import / export: Excel, CSV, YAML / JSON, BPMN
# --------------------------------------------------------------------------- #
def _rows(items: list[dict], fields: tuple) -> pd.DataFrame:
    return pd.DataFrame([{k: ("; ".join(v) if isinstance(v, list) else v) for k, v in
                          ((f, it.get(f)) for f in fields)} for it in items], columns=list(fields))


def to_excel(defn: ProcessDefinition) -> bytes:
    """The Excel template: sheets Process, Stages, Steps, Flows (lists as "a; b")."""
    buf = io.BytesIO()
    meta = pd.DataFrame([{"field": k, "value": getattr(defn, k)} for k in
                         ("name", "description", "version", "status", "start_trigger", "end_result")])
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        meta.to_excel(xw, sheet_name="Process", index=False)
        _rows(defn.stages, STAGE_FIELDS).to_excel(xw, sheet_name="Stages", index=False)
        _rows(defn.steps, STEP_FIELDS).to_excel(xw, sheet_name="Steps", index=False)
        _rows(defn.flows, FLOW_FIELDS).to_excel(xw, sheet_name="Flows", index=False)
    return buf.getvalue()


def _records(df: pd.DataFrame) -> list[dict]:
    df = df.rename(columns=lambda c: str(c).strip().lower().replace(" ", "_"))
    return [{k: v for k, v in r.items()} for r in df.to_dict("records")]


def from_excel(data: bytes, default_name: str = "Process") -> ProcessDefinition:
    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
    by = {k.strip().lower(): v for k, v in sheets.items()}
    if "steps" not in by:
        raise ValueError("The Excel file needs a sheet named Steps (and optionally Process, Stages, Flows).")
    d: dict = {"name": default_name}
    if "process" in by:
        for r in _records(by["process"]):
            if _text(r.get("field")):
                d[_text(r["field"])] = r.get("value")
    d.pop("status", None)
    d.pop("version", None)
    d["stages"] = _records(by["stages"]) if "stages" in by else []
    d["steps"] = _records(by["steps"])
    d["flows"] = _records(by["flows"]) if "flows" in by else []
    return _with_stages(ProcessDefinition.from_dict(d))


def from_csv(data: bytes, default_name: str = "Process") -> ProcessDefinition:
    """A CSV with the Steps columns (stage_id may be a stage name; stages are created from it)."""
    df = pd.read_csv(io.BytesIO(data), sep=None, engine="python", encoding="utf-8-sig", dtype=str,
                     keep_default_na=False)
    return _with_stages(ProcessDefinition.from_dict({"name": default_name, "steps": _records(df)}))


def _with_stages(d: ProcessDefinition) -> ProcessDefinition:
    """Stages named by steps but not listed are added (in order of first use)."""
    known = {s["id"] for s in d.stages}
    for s in d.ordered_steps():
        if s["stage_id"] and s["stage_id"] not in known:
            d.stages.append({"id": s["stage_id"], "name": s["stage_id"], "owner_role": ""})
            known.add(s["stage_id"])
    if not d.stages and d.steps:
        d.stages.append({"id": "ST01", "name": "Process", "owner_role": ""})
        for s in d.steps:
            s["stage_id"] = "ST01"
    return d


def to_yaml(defn: ProcessDefinition) -> str:
    return yaml.safe_dump(defn.to_dict(), sort_keys=False, allow_unicode=True)


def from_yaml(text: str | bytes) -> ProcessDefinition:
    data = yaml.safe_load(text.decode("utf-8-sig") if isinstance(text, bytes) else text)
    if not isinstance(data, dict):
        raise ValueError("The YAML / JSON file must hold one object with name, stages, steps and flows.")
    data.pop("status", None)
    data.pop("approved_by", None)
    data.pop("approved_at", None)
    return _with_stages(ProcessDefinition.from_dict(data))


_TASKS = {"task", "userTask", "serviceTask", "manualTask", "scriptTask", "sendTask", "receiveTask",
          "businessRuleTask", "callActivity", "subProcess"}
_GATEWAYS = {"exclusiveGateway", "parallelGateway", "inclusiveGateway", "eventBasedGateway", "complexGateway"}
_KNOWN = _TASKS | _GATEWAYS | {"startEvent", "endEvent", "sequenceFlow", "laneSet", "lane", "flowNodeRef",
                               "documentation", "process", "incoming", "outgoing", "conditionExpression",
                               "extensionElements", "childLaneSet"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def from_bpmn(data: bytes | str, default_name: str = "Process") -> tuple[ProcessDefinition, list[str]]:
    """A simple BPMN 2.0 process: tasks -> steps, lanes -> stages, sequence flows -> flows
    (through gateways: each task before a gateway connects to each task after it, with the
    flow's name as condition). Start / end event names become the start trigger / end result.
    Returns (definition, notes on what was ignored)."""
    try:
        root = ET.fromstring(data if isinstance(data, (bytes, str)) else bytes(data))
    except ET.ParseError as err:
        raise ValueError(f"The BPMN file is not well-formed XML: {err}") from err
    process = next((e for e in root.iter() if _local(e.tag) == "process"), None)
    if process is None:
        raise ValueError("No <process> element found in the BPMN file.")
    notes: list[str] = []
    ignored: dict[str, int] = {}
    for e in process.iter():
        name = _local(e.tag)
        if name not in _KNOWN and e is not process:
            ignored[name] = ignored.get(name, 0) + 1
    for name, n in sorted(ignored.items()):
        notes.append(f"Ignored {n} <{name}> element{'s' if n > 1 else ''} (not understood).")
    nodes: dict[str, dict] = {}
    for e in process.iter():
        name = _local(e.tag)
        if name in _TASKS | _GATEWAYS | {"startEvent", "endEvent"} and e.get("id"):
            doc = next((d.text or "" for d in e if _local(d.tag) == "documentation"), "")
            nodes[e.get("id")] = {"kind": name, "name": (e.get("name") or "").strip(), "doc": doc.strip()}
            if name == "subProcess":
                notes.append(f"Sub-process '{e.get('name') or e.get('id')}' was imported as one step.")
    flows = [(e.get("sourceRef"), e.get("targetRef"), (e.get("name") or "").strip())
             for e in process.iter() if _local(e.tag) == "sequenceFlow"]
    lanes = []
    for lane in (e for e in process.iter() if _local(e.tag) == "lane"):
        refs = [r.text.strip() for r in lane if _local(r.tag) == "flowNodeRef" and r.text]
        lanes.append((lane.get("id"), (lane.get("name") or lane.get("id") or "").strip(), refs))
    succ: dict[str, list[tuple[str, str]]] = {}
    for s, t, cond in flows:
        succ.setdefault(s, []).append((t, cond))
    tasks = [nid for nid, n in nodes.items() if n["kind"] in _TASKS]

    def task_successors(nid: str, cond: str = "", seen=None) -> list[tuple[str, str]]:
        seen = seen or set()
        out = []
        for t, c in succ.get(nid, []):
            if t in seen:
                continue
            kind = nodes.get(t, {}).get("kind")
            if kind in _TASKS or kind == "endEvent":
                out.append((t, c or cond))
            elif kind in _GATEWAYS or kind == "startEvent":
                out += task_successors(t, c or cond, seen | {t})
        return out

    # Positions: breadth-first from the start events, then any task not reached.
    starts = [nid for nid, n in nodes.items() if n["kind"] == "startEvent"]
    order: list[str] = []
    queue = [t for s in starts for t, _ in task_successors(s) if t in tasks]
    while queue:
        t = queue.pop(0)
        if t in order or t not in tasks:
            continue
        order.append(t)
        queue += [x for x, _ in task_successors(t) if x in tasks and x not in order]
    order += [t for t in tasks if t not in order]
    pos = {t: i for i, t in enumerate(order, 1)}
    lane_of = {ref: lid for lid, _, refs in lanes for ref in refs}
    stages = [{"id": lid, "name": name, "owner_role": ""} for lid, name, _ in lanes]
    steps = [{"id": t, "name": nodes[t]["name"] or t, "stage_id": lane_of.get(t, ""), "position": pos[t],
              "description": nodes[t]["doc"], "mapped_activities": []} for t in order]
    out_flows = []
    for t in order:
        for nxt, cond in task_successors(t):
            if nxt in pos:
                kind = "allowed_loop" if pos[nxt] <= pos[t] else "next"
                out_flows.append({"from_step": t, "to_step": nxt, "kind": kind, "condition": cond})
    start_names = [nodes[s]["name"] for s in starts if nodes[s]["name"]]
    end_names = [n["name"] for n in nodes.values() if n["kind"] == "endEvent" and n["name"]]
    d = ProcessDefinition.from_dict({"name": (process.get("name") or default_name), "stages": stages,
                                     "steps": steps, "flows": out_flows, "start_trigger": "; ".join(start_names),
                                     "end_result": "; ".join(end_names), "source": "BPMN import"})
    if any(not s["stage_id"] for s in d.steps) and d.stages:
        d.stages.append({"id": "ST_none", "name": "(no lane)", "owner_role": ""})
        for s in d.steps:
            s["stage_id"] = s["stage_id"] or "ST_none"
    return _with_stages(d), notes


def load_file(file_name: str, data: bytes, default_name: str = "Process") -> tuple[ProcessDefinition, list[str]]:
    """Any supported definition file, by extension: .xlsx, .csv, .yaml/.yml, .json, .bpmn/.xml."""
    ext = Path(file_name).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return from_excel(data, default_name), []
    if ext == ".csv":
        return from_csv(data, default_name), []
    if ext in (".yaml", ".yml", ".json"):
        return from_yaml(data), []
    if ext in (".bpmn", ".xml"):
        return from_bpmn(data, default_name)
    raise ValueError(f"Unsupported definition file type '{ext}' (use Excel, CSV, YAML, JSON or BPMN).")
