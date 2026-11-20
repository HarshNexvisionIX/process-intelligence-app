"""A process pack proposed from documents -> <data_dir>/library/<process>/pack.proposed.yaml (never pack.yaml).

Reads .txt, .md, .docx and .pdf from <data_dir>/library/<process>/docs/ (text extraction is local, reused from
app.core.definition_ai), cuts the text into chunks that fit the smallest provider of the route, asks the AI
(task nc_pack_from_docs) for stages, steps, descriptions, order hints and exceptions per chunk, and merges the
chunks (stages and steps in first-seen order; descriptions joined). The user approves it later (review page).
"""
from __future__ import annotations


import yaml

from app.core.config import settings
from app.nocase import library

PROPOSED = "pack.proposed.yaml"
DOC_TYPES = (".txt", ".md", ".docx", ".pdf")


def _cfg() -> dict:
    return settings()["nocase"]["pack_from_docs"]


def check(data) -> list[str]:
    errs = []
    stages = data.get("stages")
    if not isinstance(stages, list) or not stages:
        return ['"stages" must be a non-empty list of {"name", "steps": [{"name", "description"}]}']
    for i, s in enumerate(stages):
        if not isinstance(s, dict) or not str(s.get("name") or "").strip():
            errs.append(f"stages[{i}] needs a name")
            continue
        for j, st in enumerate(s.get("steps") or []):
            if not isinstance(st, dict) or not str(st.get("name") or "").strip():
                errs.append(f"stages[{i}].steps[{j}] needs a name")
    return errs


def merge(parts: list[dict]) -> dict:
    """Stages and steps in first-seen order across chunks; a step seen twice keeps its first stage."""
    stages: dict[str, dict] = {}
    seen: set[str] = set()
    hints, exceptions = [], []
    for part in parts:
        for s in part.get("stages") or []:
            name = str(s["name"]).strip()
            st = stages.setdefault(name.lower(), {"name": name, "description": s.get("description") or "", "steps": []})
            for step in s.get("steps") or []:
                key = str(step["name"]).strip().lower()
                if key in seen:
                    for other in stages.values():
                        for x in other["steps"]:
                            if x["name"].lower() == key and step.get("description") and \
                                    step["description"] not in x["description"]:
                                x["description"] = (x["description"] + " " + step["description"]).strip()
                    continue
                seen.add(key)
                st["steps"].append({"name": str(step["name"]).strip(), "description": step.get("description") or ""})
        hints += [h for h in part.get("order_hints") or [] if h not in hints]
        exceptions += [e for e in part.get("exceptions") or [] if e not in exceptions]
    return {"stages": list(stages.values()), "order_hints": hints, "exceptions": exceptions}


def propose(process: str, gw) -> dict:
    """{"path", "chunks", "stages", "steps", "reason"?}; writes pack.proposed.yaml when the AI answered."""
    from app.core.ai_client import chunk_chars
    from app.core.definition_ai import chunks, extract_text
    folder = library.pack_dir(process)
    docs = sorted(f for f in (folder / "docs").glob("*") if f.suffix.lower() in DOC_TYPES) \
        if (folder / "docs").exists() else []
    if not docs:
        return {"path": None, "chunks": 0, "reason": f"no documents in {folder / 'docs'}"}
    from app.nocase.payloads import PayloadError, scrub_text
    # Names, e-mails, IDs and long numbers are masked before anything is sent (the payload guard still checks).
    # The document file names are not sent: they can carry a client or person name.
    text, masked = scrub_text("\n\n".join(f"# Document {i}\n" + extract_text(d.name, d.read_bytes())
                                           for i, d in enumerate(docs, 1)))
    size = chunk_chars("nc_pack_from_docs", int(_cfg()["chunk_chars"]))
    parts, notes = [], []
    pieces = chunks(text, size)
    for k, piece in enumerate(pieces, 1):
        msgs = [{"role": "system", "content": _cfg()["prompt"]},
                {"role": "user", "content": f"Part {k} of {len(pieces)}:\n{piece}"}]
        try:
            a = gw.ask("nc_pack_from_docs", msgs, check)
        except PayloadError as exc:       # something the masking missed: this part is not sent
            notes.append(f"part {k}: {exc}")
            continue
        if a.ok:
            parts.append(a.data)
        else:
            notes.append(f"part {k}: {a.reason}")
    if not parts:
        return {"path": None, "chunks": len(pieces), "reason": "; ".join(notes) or "no answer", "masked": masked}
    pack = {"name": library.display_name(process), "provenance": {"source": "documents (AI proposal)", "status": "proposed",
                                            "confirmed": False}, **merge(parts)}
    out = folder / PROPOSED
    out.write_text(yaml.safe_dump(pack, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return {"path": str(out), "chunks": len(pieces), "stages": len(pack["stages"]),
            "steps": sum(len(s["steps"]) for s in pack["stages"]), "notes": notes, "masked": masked}
