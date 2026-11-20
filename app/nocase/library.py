"""Process packs: <data_dir>/library/<process>/pack.yaml (stages and steps in business order) + rules.yaml.

The app ships no sample processes. A process is created by the user on Business context (create_pack); the tests
seed theirs from PM_SAMPLE_PACKS_DIR / PM_PROCESS_TEMPLATES_DIR (tests/sample_processes/).
Process knowledge lives here and in settings, never in the engine code.

rules.yaml: a list of {id, step, stage, match: {app, screen, action}, status, version}. `screen` and `action` are
regular expressions (searched in the cleaned screen / click); `app` is the application name or "*". Only rules
with a status in nocase.assign.rule_statuses apply; the first match wins, in file order.
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.core.config import PROJECT_ROOT, data_dir, settings

SAMPLES = Path(os.environ.get("PM_SAMPLE_PACKS_DIR") or PROJECT_ROOT / "config" / "nocase_packs")
TEMPLATES = Path(os.environ.get("PM_PROCESS_TEMPLATES_DIR") or PROJECT_ROOT / "config" / "process_templates")


def process_key(name: str) -> str:
    """'Order Handling (UK)' -> 'order_handling_uk': the library folder name of a process."""
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def user_processes() -> list[str]:
    """Library processes the user created or approved (never sample, test or template packs)."""
    root = library_dir()
    if not root.exists():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if not (p / "pack.yaml").exists():
            continue
        pack = yaml.safe_load((p / "pack.yaml").read_text(encoding="utf-8")) or {}
        src = str((pack.get("provenance") or {}).get("source") or pack.get("source") or "")
        if src == "test pack" or src.startswith("config/"):
            continue
        out.append(p.name)
    return out


def display_name(process: str) -> str:
    """The process name as the user typed it; a pack that only carries its folder key reads as words."""
    try:
        name = (load_pack(process) or {}).get("name") or process
    except (ValueError, OSError):
        name = process
    if name == process_key(name):                      # "order_handling_uk" -> "Order handling uk"
        name = name.replace("_", " ").capitalize()
    return name


def create_pack(name: str, by: str = "user") -> str:
    """A new, empty process in the library (stages and steps come from documents or the reviewer). Returns its key."""
    key = process_key(name)
    if not key:
        raise ValueError("Enter a process name.")
    folder = library_dir() / key
    if not (folder / "pack.yaml").exists():
        folder.mkdir(parents=True, exist_ok=True)
        pack = {"name": name.strip(), "provenance": {"source": "user", "status": "draft", "confirmed": False,
                                                     "by": by, "created": datetime.now(timezone.utc).isoformat()},
                "stages": []}
        (folder / "pack.yaml").write_text(yaml.safe_dump(pack, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return key


def library_dir() -> Path:
    return data_dir() / "library"


def pack_dir(process: str) -> Path:
    """The process's library folder, seeded on first use; raises ValueError when nothing is known about it."""
    if not process or "/" in process or "\\" in process or process.startswith("."):
        raise ValueError(f"Not a valid process name: {process!r}")
    folder = library_dir() / process
    if (folder / "pack.yaml").exists():
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    sample = SAMPLES / process
    if (sample / "pack.yaml").exists():
        for f in sample.glob("*.yaml"):
            if not (folder / f.name).exists():
                shutil.copyfile(f, folder / f.name)
        return folder
    template = TEMPLATES / f"{process}.yaml"
    if template.exists():
        t = yaml.safe_load(template.read_text(encoding="utf-8")) or {}
        pack = {"name": t.get("name") or process, "source": f"config/process_templates/{process}.yaml",
                "stages": [{"name": s["name"], "description": "",
                            "steps": [{"name": st["name"],
                                       "description": ("Also called: " + ", ".join(st.get("synonyms") or [])
                                                       if st.get("synonyms") else "")}
                                      for st in s.get("steps") or []]}
                           for s in t.get("stages") or []]}
        (folder / "pack.yaml").write_text(yaml.safe_dump(pack, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return folder
    folder.rmdir()
    raise ValueError(f"No pack for process '{process}': add {library_dir() / process / 'pack.yaml'}, "
                     f"a sample in {SAMPLES / process}, or a template {template}")


def load_pack(process: str | None) -> dict | None:
    if not process:
        return None
    return yaml.safe_load((pack_dir(process) / "pack.yaml").read_text(encoding="utf-8")) or {}


def step_stage(pack: dict | None) -> dict[str, str]:
    """step name -> stage name, in business order."""
    return {st["name"]: s["name"] for s in (pack or {}).get("stages") or [] for st in s.get("steps") or []}


def step_order(pack: dict | None) -> list[str]:
    return list(step_stage(pack))


def load_rules(process: str | None) -> list[dict]:
    if not process:
        return []
    path = pack_dir(process) / "rules.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return list(data.get("rules", []) if isinstance(data, dict) else data)


def rule_problems(rules: list[dict], texts: list[str]) -> dict[str, str]:
    """{rule id: problem}: unknown status, a regex that does not compile, has a nested quantifier, is too long,
    or is too slow on these texts (level4's guard: a separate process with a time limit)."""
    from app.core.level4 import regex_problems, static_regex_problem
    allowed = set(settings()["nocase"]["assign"]["rule_statuses"])
    known = allowed | {"proposed", "documented", "rejected"}
    out: dict[str, str] = {}
    patterns: dict[str, list[str]] = {}
    for r in rules:
        rid = str(r.get("id") or "?")
        if r.get("status") not in known:
            out[rid] = f"unknown status {r.get('status')!r}"
            continue
        if not r.get("step"):
            out[rid] = "no step"
            continue
        for key in ("screen", "action"):
            p = (r.get("match") or {}).get(key)
            if p:
                why = static_regex_problem(str(p))
                if why:
                    out[rid] = f"the {key} regex {why}"
                else:
                    patterns.setdefault(str(p), []).append(rid)
    slow = regex_problems(list(patterns), texts) if patterns and texts else {}
    for p, why in slow.items():
        for rid in patterns[p]:
            out.setdefault(rid, f"a regex {why}")
    return out


def provenance(pack: dict | None) -> dict:
    """Where the pack's step order comes from. A pack order is always `documented` (never observed or
    sme_confirmed unless a reviewer confirms it in the review page), and only for its own process."""
    p = dict((pack or {}).get("provenance") or {})
    p.setdefault("source", (pack or {}).get("source") or "process pack")
    p["status"] = "sme_confirmed" if p.get("confirmed") is True and p.get("status") == "sme_confirmed" else "documented"
    p["confirmed"] = bool(p.get("confirmed")) and p["status"] == "sme_confirmed"
    p["label"] = (f"Documented ({p['source']}, not confirmed)" if not p["confirmed"]
                  else f"Confirmed by the business ({p['source']})")
    return p
