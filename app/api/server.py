"""Web API for the new front end (web/): the same engine, files and rules as the Streamlit app.

Start:  python -m app.api            (http://127.0.0.1:8008, serves web/dist when it is built)
Dev:    cd web && npm run dev        (http://127.0.0.1:5173, /api is proxied to :8008)

Nothing here holds AI keys or picks a model: providers, routes and keys stay in Settings / .env, and every AI
request goes through the approval gate (app/api/ai_packages.py) before it is sent.
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.api import ai_packages as aip
from app.api import insights as insights_mod
from app.api import pdf as pdf_mod
from app.api import system_map
from app.core import jobs, knowledge
from app.core.config import PROJECT_ROOT, settings
from app.core.workflow import STEPS, approved_steps, context_done, mode_of, pending_rules, step_status
from app.nocase import library as packs
from app.nocase.knowledge import library as lib

app = FastAPI(title="ProcessLens API", version="3.0")
WEB_DIST = PROJECT_ROOT / "web" / "dist"
_RUNS: dict[str, dict] = {}            # job_id -> run state (one run per analysis at a time)
_RUN_GUARD = threading.Lock()


# --------------------------------------------------------------------------- helpers
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job(job_id: str) -> dict:
    try:
        return jobs.load_job(job_id)
    except (FileNotFoundError, ValueError, OSError):
        raise HTTPException(404, f"Analysis {job_id} not found.")


def _folder(job_id: str) -> Path:
    return jobs.job_folder(job_id)


def _read(folder: Path, name: str, default=None):
    p = folder / name
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except ValueError:
        return default


def _clean(x):
    """JSON-safe: NaN -> None, numpy / pandas scalars -> Python."""
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, float) and x != x:
        return None
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):
        try:
            return _clean(x.item())
        except (ValueError, AttributeError):
            return str(x)
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.isoformat()
    return x


def _summary(job: dict) -> dict:
    status = step_status(job)
    m = mode_of(job)
    if m == "nocase" and status.get("context") and not approved_steps(job.get("process")):
        status["context"] = False              # no case ID: the stages and steps must be approved first
    return {"job_id": job["job_id"], "source_name": job.get("source_name"), "created_at": job.get("created_at"),
            "mode": m if (job.get("mode") == "nocase" or status.get("import")) else None,
            "detected_mode": job.get("detected_mode"), "dataset_class": job.get("dataset_class", "client"),
            "rows": job.get("rows"), "process": job.get("process"),
            "process_name": packs.display_name(job["process"]) if job.get("process") else None,
            "business_context": job.get("business_context"), "status": job.get("status"),
            "steps": [{"key": k, "label": label, "done": bool(status.get(k))} for k, label in STEPS[m]]}


def _ai_status() -> dict:
    """Whether AI can be used, and through which providers (names only, no keys, no model choice)."""
    try:
        from app.core import ai_client
        from app.core.narrative import ai_status
        ok, why = ai_status()
        known = ai_client.providers()
        usable = [n for n, p in known.items() if ai_client.usable(p)[0]]
        private = [n for n in usable if known[n].get("private") and known[n].get("base_url")]
        return {"available": bool(ok), "reason": why, "providers": usable, "private_providers": private,
                "enabled_for_nocase": bool(settings()["nocase"].get("ai_enabled", True)),
                "where": "Settings / .env (keys are never entered in this app's pages)"}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}", "providers": [],
                "private_providers": []}


# --------------------------------------------------------------------------- health
@app.get("/api/health")
def health():
    return {"ok": True, "version": app.version, "data_dir": str(jobs.jobs_dir().parent), "ai": _ai_status()}


# --------------------------------------------------------------------------- analyses
@app.get("/api/jobs")
def list_jobs():
    out = []
    for j in jobs.list_jobs():
        try:
            out.append(_summary(j))
        except Exception:  # noqa: BLE001 - one broken job must not hide the others
            out.append({"job_id": j.get("job_id"), "source_name": j.get("source_name"), "broken": True})
    return out


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _job(job_id)
    s = _summary(job)
    s["run"] = _run_state(job_id)
    return _clean(s)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    _job(job_id)
    target = jobs.delete_job(job_id)
    return {"moved_to": str(target)}


# --------------------------------------------------------------------------- import
_CONS: dict[str, object] = {}


def _con(job: dict):
    """A DuckDB connection with the job's source opened as view `src` (kept per job)."""
    from app.core.db import connect
    from app.core.ingest import open_source
    con = _CONS.get(job["job_id"])
    if con is None:
        con = connect()
        open_source(con, jobs.job_source(job), jobs.job_folder(job["job_id"]))
        _CONS[job["job_id"]] = con
    return con


def _profile_payload(job: dict) -> dict:
    from app.core.ingest import profile
    from app.core.mapping import ALL_ROLES, REQUIRED_ROLES, ROLE_LABELS, suggest_mapping
    from app.intake import mode_detect
    from app.nocase.canonical import COLUMN_HELP, STANDARD_COLUMNS, TIME_FORMATS
    con = _con(job)
    prof = profile(con)
    folder = jobs.job_folder(job["job_id"])
    try:
        d = mode_detect.detect_and_save(con, folder, prof)
    except Exception as exc:  # noqa: BLE001
        d = {"decision": None, "error": str(exc)}
    try:
        from app.ui.nocase_mapping import suggestions as nc_suggestions
        nc = nc_suggestions(con, prof)
    except Exception:  # noqa: BLE001
        nc = {}
    cs = suggest_mapping(prof)
    preview = con.execute("SELECT * FROM src LIMIT 12").df()
    job["detected_mode"] = d.get("decision")
    jobs.save_job(job)
    return _clean({
        "rows": prof.rows, "seconds": prof.seconds,
        "columns": [{"name": c.name, "type": c.dtype, "missing_pct": c.null_pct, "distinct": c.distinct,
                     "date_format": c.ts_format, "examples": c.samples[:3]} for c in prof.columns],
        "preview": preview.astype(str).to_dict("records"),
        "detection": {"mode": d.get("decision"), "workflow": "nocase" if d.get("decision") in ("no_case", "partial")
                      else "case", "message": mode_detect.message(d) if d.get("decision") else d.get("error"),
                      "column": d.get("column"), "candidates": [{k: c.get(k) for k in ("column", "passed", "fail_reason")}
                                                          for c in d.get("candidates", [])][:8]},
        "nocase": {"standard_columns": list(STANDARD_COLUMNS), "help": COLUMN_HELP, "suggested": nc,
                   "time_formats": ["detect", *TIME_FORMATS]},
        "case": {"roles": list(ALL_ROLES), "required": list(REQUIRED_ROLES), "labels": ROLE_LABELS,
                 "suggested": {r: {"column": s.column, "confidence": s.confidence, "reasons": s.reasons}
                               for r, s in cs.items()}}})


@app.post("/api/jobs")
async def create_job(files: list[UploadFile] = File(...), dataset_class: str = Form("client"),
                     process: str = Form(""), new_process: str = Form("")):
    if dataset_class not in ("client", "public"):
        raise HTTPException(400, "Dataset class must be client or public.")
    _check_process_choice(process, new_process)
    from app.core.ingest import SUPPORTED
    truth = [f.filename for f in files if Path(f.filename or "").name.lower().endswith("_truth.parquet")]
    if truth:
        raise HTTPException(400, f"Answer-key file(s) are never used as input: {', '.join(truth)}.")
    bad = [f.filename for f in files if Path(f.filename or "").suffix.lower() not in SUPPORTED]
    if bad:
        raise HTTPException(400, f"Not a supported file: {', '.join(bad)}. Use {', '.join(sorted(SUPPORTED))}.")
    name = files[0].filename if len(files) == 1 else f"{len(files)} files: " + ", ".join(
        f.filename for f in files[:2]) + (", …" if len(files) > 2 else "")
    job = jobs.create_job(name, None, dataset_class)
    _link_process(job, process, new_process)

    class _Up:                          # jobs.save_browser_uploads expects objects with .name and .read()
        def __init__(self, f):
            self.name, self._f = f.filename, f.file

        def read(self, *a):
            return self._f.read(*a)

        def getbuffer(self):
            return memoryview(self._f.read())

    if len(files) == 1:
        jobs.save_browser_upload(job, files[0].filename, files[0].file)
    else:
        jobs.save_browser_uploads(job, [_Up(f) for f in files])
    job = jobs.load_job(job["job_id"])
    try:
        from app.core.ingest import open_source
        con = _con(job)
        fmt = open_source(con, jobs.job_source(job), jobs.job_folder(job["job_id"]))
        job.update(status="profiled", format=fmt)
        jobs.save_job(job)
        payload = _profile_payload(job)
    except ValueError as err:
        raise HTTPException(400, str(err))
    job = jobs.load_job(job["job_id"])
    job["rows"] = payload["rows"]
    jobs.save_job(job)
    return {"job": _summary(job), "profile": payload}


class FromPath(BaseModel):
    path: str
    dataset_class: str = "client"
    process: str = ""
    new_process: str = ""


def _check_process_choice(key: str, new_name: str) -> None:
    """Refuse an unknown process or a duplicate new name BEFORE anything is created."""
    if new_name and new_name.strip():
        k = packs.process_key(new_name)
        if not k:
            raise HTTPException(400, "Enter a process name with letters or digits.")
        if (packs.library_dir() / k / "pack.yaml").exists():
            raise HTTPException(400, f"A process named \"{packs.display_name(k)}\" already exists. "
                                     "Choose it from the list, or enter a different name.")
    elif key and key not in packs.user_processes():
        raise HTTPException(400, "Unknown process.")


def _link_process(job: dict, key: str, new_name: str) -> None:
    proc = _resolve_process(key, new_name)
    if proc:
        job["process"] = proc
        jobs.save_job(job)


@app.post("/api/jobs/from-path")
def create_job_from_path(body: FromPath):
    """A file, folder or pattern on this computer, read where it is (never copied): for files over ~1 GB."""
    from app.core.ingest import open_source, resolve_sources
    if body.dataset_class not in ("client", "public"):
        raise HTTPException(400, "Dataset class must be client or public.")
    source = body.path.strip().strip('"')
    if Path(source).name.lower().endswith("_truth.parquet"):
        raise HTTPException(400, "This is an answer-key file (_TRUTH): it is never used as input.")
    _check_process_choice(body.process, body.new_process)
    try:
        resolve_sources(source)            # found, one format, not too many files
    except ValueError as err:
        raise HTTPException(400, str(err))
    job = jobs.create_job(Path(source).name or source, source, body.dataset_class)
    _link_process(job, body.process, body.new_process)
    try:
        con = _con(job)
        fmt = open_source(con, jobs.job_source(job), jobs.job_folder(job["job_id"]))
        job.update(status="profiled", format=fmt)
        jobs.save_job(job)
        payload = _profile_payload(job)
    except ValueError as err:
        raise HTTPException(400, str(err))
    job = jobs.load_job(job["job_id"])
    job["rows"] = payload["rows"]
    jobs.save_job(job)
    return {"job": _summary(job), "profile": payload}


@app.get("/api/jobs/{job_id}/profile")
def get_profile(job_id: str):
    job = _job(job_id)
    try:
        return _profile_payload(job)
    except ValueError as err:
        raise HTTPException(400, str(err))


# ----------------------------------------------------------------------------- application names (masked values)
@app.get("/api/jobs/{job_id}/apps")
def job_apps(job_id: str, column: str, title: str | None = None, user: str | None = None):
    """The applications of the chosen column: which values are masked (hashes, IDs, numbers) and the real names
    already saved in the shared map. The page asks for names only when something is masked."""
    from app.core import apps
    job = _job(job_id)
    con = _con(job)
    names = {r[0] for r in con.execute("DESCRIBE src").fetchall()}
    if column not in names or (title and title not in names) or (user and user not in names):
        raise HTTPException(400, "Unknown column.")
    inv = apps.app_inventory(con, column, title or None, user or None)
    summ = apps.inventory_summary(inv)
    masked = inv[inv["masked"]] if len(inv) else inv
    return _clean({"summary": {k: v for k, v in summ.items() if k != "unnamed"},
                   "categories": list(settings()["apps"]["categories"]),
                   "masked": masked.to_dict("records")})


class AppNames(BaseModel):
    rows: list[dict]


@app.put("/api/apps/map")
def save_app_names(body: AppNames):
    from app.core import apps
    rows = [{k: r.get(k) for k in ("raw_value", "real_name", "category", "notes")} for r in body.rows]
    return apps.save_map(rows)


@app.get("/api/apps/map.csv")
def export_app_names():
    from app.core import apps
    return Response(apps.export_map(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{apps.MAP_FILE}"'})


@app.post("/api/apps/map/import")
async def import_app_names(file: UploadFile = File(...), confirm: bool = Form(False)):
    from app.core import apps
    try:
        preview = apps.import_map(await file.read())
    except Exception as err:  # noqa: BLE001 - any unreadable file is the user's input error
        raise HTTPException(400, f"Could not read this mapping file: {err}")
    out = {k: preview[k] for k in ("added", "updated", "unchanged", "ignored")}
    if confirm:
        out["saved"] = apps.save_map(preview["rows"])
    return out


class NcConvert(BaseModel):
    columns: dict[str, str | None]
    ts_format: str | None = None
    timezone: str = "UTC"
    keep_rows_where: dict[str, list] = {}
    drop_rows_where: dict[str, list] = {}
    dataset_class: str


@app.post("/api/jobs/{job_id}/convert/nocase")
def convert_nocase(job_id: str, body: NcConvert):
    from app.nocase.canonical import NcMapping
    from app.nocase.ingest import ingest
    from app.nocase.pipeline import after_ingest
    job = _job(job_id)
    m = NcMapping(columns={k: v for k, v in body.columns.items() if v}, dataset_class=body.dataset_class,
                  ts_format=None if body.ts_format in (None, "", "detect") else body.ts_format,
                  timezone=body.timezone, keep_rows_where=body.keep_rows_where, drop_rows_where=body.drop_rows_where)
    con = _con(job)
    names = [r[0] for r in con.execute("DESCRIBE src").fetchall()]
    problems = m.problems(names)
    if problems:
        raise HTTPException(400, " ".join(problems))
    try:
        job, scope = ingest(jobs.job_source(job), m, job=job)
        after_ingest(jobs.job_folder(job_id))
    except ValueError as err:
        raise HTTPException(400, str(err))
    job["status"] = "nc_units"
    jobs.save_job(job)
    return _clean({"job": _summary(job), "scope": scope})


class CaseConvert(BaseModel):
    mapping: dict[str, str | None]
    timestamp_format: str | None = None
    end_timestamp_format: str | None = None
    timezone: str = "UTC"
    drop_missing_case: bool = False
    drop_missing_activity: bool = False
    merge_repeats: bool = False
    exclude_columns: list[str] = []


@app.post("/api/jobs/{job_id}/convert/case")
def convert_case(job_id: str, body: CaseConvert):
    from app.core import activity_renames
    from app.core.capabilities import capabilities
    from app.core.derived import apply_derivations, case_label
    from app.core.ingest import normalise, profile
    from app.core.mapping import ALL_ROLES, save_profile, signature, validate_mapping
    job = _job(job_id)
    con = _con(job)
    prof = profile(con)
    names = [c.name for c in prof.columns]
    mapping: dict = {r: body.mapping.get(r) for r in ALL_ROLES}
    by = prof.by_name()
    for role, fmt in (("timestamp", body.timestamp_format), ("end_timestamp", body.end_timestamp_format)):
        col = mapping.get(role)
        mapping[f"{role}_format"] = (fmt or (by[col].ts_format if col in by else None) or "iso") if col else None
    mapping.update(timezone=body.timezone, merge_repeats=body.merge_repeats, exclude_columns=body.exclude_columns,
                   scope={"drop_missing_case": body.drop_missing_case,
                          "drop_missing_activity": body.drop_missing_activity,
                          "filter_column": None, "filter_values": []})
    missing = [r for r in ("case_id", "activity", "timestamp") if not mapping.get(r)]
    if missing:
        raise HTTPException(400, "Map these columns first: " + ", ".join(missing) + ".")
    try:
        mapping = apply_derivations(con, mapping)
    except ValueError as err:
        raise HTTPException(400, str(err))
    checks = validate_mapping(con, mapping, prof.rows)
    errors = [c.message for c in checks if not c.ok and c.severity == "error"]
    if errors:
        raise HTTPException(400, " ".join(errors))
    folder = jobs.job_folder(job_id)
    mapping["activity_renames"] = activity_renames.load(signature(names))
    result = normalise(con, mapping, folder / "events.parquet")
    (folder / "mapping.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    save_profile(names, mapping)
    job.update(mode="case", status="normalised", summary=result, warnings=[c.message for c in checks if not c.ok],
               layout_signature=signature(names),
               case_source={**(mapping.get("case_source") or {"kind": "column", "column": mapping["case_id"]}),
                            "label": case_label(mapping)}, capabilities=capabilities(mapping))
    jobs.save_job(job)
    return _clean({"job": _summary(job), "result": result,
                   "checks": [{"name": c.name, "ok": c.ok, "severity": c.severity, "message": c.message}
                              for c in checks]})


# --------------------------------------------------------------------------- business context
@app.get("/api/processes")
def processes():
    out = []
    for key in packs.user_processes():
        pack = packs.load_pack(key) or {}
        out.append({"key": key, "name": packs.display_name(key),
                    "stages": len(pack.get("stages") or []),
                    "steps": sum(len(s.get("steps") or []) for s in pack.get("stages") or []),
                    "rules": len(packs.load_rules(key)), "status": packs.provenance(pack)["label"]})
    return out


class ProcessPick(BaseModel):
    key: str | None = None
    new_name: str | None = None


def _resolve_process(key: str | None, new_name: str | None) -> str | None:
    """The process for an analysis: an existing one (key) or a new one (new_name, which must be unique: names that
    differ only in case, spaces or punctuation count as the same process)."""
    if new_name and new_name.strip():
        k = packs.process_key(new_name)
        if not k:
            raise HTTPException(400, "Enter a process name with letters or digits.")
        if (packs.library_dir() / k / "pack.yaml").exists():
            raise HTTPException(400, f"A process named \"{packs.display_name(k)}\" already exists. "
                                     "Choose it from the list, or enter a different name.")
        return packs.create_pack(new_name)
    if key:
        if key not in packs.user_processes():
            raise HTTPException(400, "Unknown process.")
        return key
    return None


@app.post("/api/jobs/{job_id}/process")
def set_process(job_id: str, body: ProcessPick):
    job = _job(job_id)
    try:
        key = _resolve_process(body.key, body.new_name)
    except ValueError as err:
        raise HTTPException(400, str(err))
    if not key:
        raise HTTPException(400, "Choose a process or enter a new process name.")
    if key != job.get("process"):
        job["process"] = key
        job.pop("business_context", None)
        jobs.save_job(job)
    return _summary(job)


def _pack_state(proc: str) -> dict:
    folder = packs.pack_dir(proc)
    pack = lib._read_yaml(folder / "pack.yaml", {}) or {}
    proposed = lib._read_yaml(folder / "pack.proposed.yaml", None) if (folder / "pack.proposed.yaml").exists() else None
    flat = lambda p: [{"stage": st.get("name", ""), "step": sp.get("name", ""), "description": sp.get("description", "")}
                      for st in (p or {}).get("stages") or [] for sp in st.get("steps") or []]
    return {"approved": flat(pack), "proposed": flat(proposed) if proposed else None,
            "provenance": packs.provenance(pack)}


@app.get("/api/jobs/{job_id}/context")
def get_context(job_id: str):
    job = _job(job_id)
    proc = job.get("process")
    sources = knowledge.list_sources(packs.display_name(proc)) if proc else []
    return _clean({"process": proc, "process_name": packs.display_name(proc) if proc else None,
                   "business_context": job.get("business_context"), "done": context_done(job),
                   "kinds": [{"key": k, "label": knowledge.KIND_LABELS[k], "extensions": knowledge.EXTENSIONS[k]}
                             for k in knowledge.KINDS[:-1]],
                   "documents": [{"id": s["id"], "title": s["title"], "kind": knowledge.KIND_LABELS[s["kind"]],
                                  "dated": s.get("recorded_on") or s["added_at"][:10],
                                  "status": "in use" if s["status"] == "active" else "excluded",
                                  "stale": s["stale"]} for s in sources],
                   "mode": mode_of(job), "pack": _pack_state(proc) if proc else None})


@app.post("/api/jobs/{job_id}/documents")
async def add_documents(job_id: str, files: list[UploadFile] = File(...), kind: str = Form("sop"),
                        recorded_on: str | None = Form(None), roles: str = Form(""),
                        walkthrough: bool = Form(False)):
    job = _job(job_id)
    proc = job.get("process")
    if not proc:
        raise HTTPException(400, "Choose the process first.")
    name = packs.display_name(proc)
    added, messages = [], []
    have = {s["title"].lower() for s in knowledge.list_sources(name) if s["status"] == "active"}
    for up in files:
        data = await up.read()
        if Path(up.filename).stem.lower() in have:
            messages.append(f"{up.filename}: already added for this process (exclude the old one first to replace it)")
            continue
        try:
            src, msgs = knowledge.add_source(name, kind, up.filename, data, None, "user", recorded_on or None,
                                             [r.strip() for r in roles.split(",") if r.strip()],
                                             walkthrough=walkthrough)
            messages += [f"{up.filename}: {m}" for m in msgs]
            added.append(up.filename)
            if up.filename.lower().endswith((".txt", ".md", ".docx", ".pdf")):
                d = packs.pack_dir(proc) / "docs"
                d.mkdir(parents=True, exist_ok=True)
                (d / up.filename).write_bytes(data)
        except ValueError as err:
            messages.append(f"{up.filename}: {err}")
    return {"added": added, "messages": messages}


class ContextConfirm(BaseModel):
    by: str
    no_documents_reason: str | None = None


@app.post("/api/jobs/{job_id}/context/confirm")
def confirm_context(job_id: str, body: ContextConfirm):
    job = _job(job_id)
    if not job.get("process"):
        raise HTTPException(400, "Choose the process first.")
    if not body.by.strip():
        raise HTTPException(400, "Enter your name.")
    active = [s for s in knowledge.list_sources(packs.display_name(job["process"])) if s["status"] == "active"]
    if body.no_documents_reason:
        job["business_context"] = {"status": "no_documents", "reason": body.no_documents_reason.strip(),
                                   "by": body.by.strip(), "at": _now()}
    elif active:
        job["business_context"] = {"status": "documents", "documents": len(active), "by": body.by.strip(),
                                   "at": _now()}
    else:
        raise HTTPException(400, "Add at least one document, or record why there are none.")
    jobs.save_job(job)
    return _summary(job)


@app.post("/api/jobs/{job_id}/context/reopen")
def reopen_context(job_id: str):
    job = _job(job_id)
    job.pop("business_context", None)
    jobs.save_job(job)
    return _summary(job)


# --------------------------------------------------------------------------- AI packages (approve before send)
def _gw_class(job: dict) -> str:
    return job.get("dataset_class") or "client"


@app.get("/api/jobs/{job_id}/ai/{kind}")
def ai_packages(job_id: str, kind: str):
    folder = _folder(job_id)
    pk = (aip.load_packages(folder).get(kind) or {})
    return _clean({"kind": kind, "prepared_at": pk.get("prepared_at"), "tasks": aip.summary(folder, kind),
                   "packages": pk.get("packages", []), "audit": aip.audit(folder)[-50:], "ai": _ai_status()})


class Approve(BaseModel):
    task: str
    task_fingerprint: str
    by: str


@app.post("/api/jobs/{job_id}/ai/{kind}/approve")
def ai_approve(job_id: str, kind: str, body: Approve):
    try:
        return aip.approve(_folder(job_id), kind, body.task, body.task_fingerprint, body.by)
    except ValueError as err:
        raise HTTPException(400, str(err))


@app.post("/api/jobs/{job_id}/ai/{kind}/revoke/{task}")
def ai_revoke(job_id: str, kind: str, task: str):
    aip.revoke(_folder(job_id), kind, task)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/ai/docs/prepare")
def docs_prepare(job_id: str):
    """Build the stages-and-steps request from the documents and keep it for approval (nothing is sent)."""
    from app.nocase.ai.pack_from_docs import propose
    job = _job(job_id)
    if not job.get("process"):
        raise HTTPException(400, "Choose the process first.")
    gw = aip.PreviewGateway(_gw_class(job))
    try:
        r = propose(job["process"], gw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not read the documents: {exc}")
    aip.save_packages(_folder(job_id), gw.packages, "docs")
    return _clean({"prepared": len(gw.packages), "masked": r.get("masked"), "blocked": gw.blocked,
                   "reason": None if gw.packages else r.get("reason")})


@app.post("/api/jobs/{job_id}/ai/docs/send")
def docs_send(job_id: str):
    from app.nocase.ai.pack_from_docs import propose
    job = _job(job_id)
    folder = _folder(job_id)
    approved = aip.approved_tasks(folder, "docs")
    if "nc_pack_from_docs" not in approved:
        raise HTTPException(400, "Approve the package first.")
    gw = aip.ApprovedGateway(_gw_class(job), approved, folder)
    try:
        r = propose(job["process"], gw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    return _clean({"ok": bool(r.get("path")), "stages": r.get("stages"), "steps": r.get("steps"),
                   "reason": r.get("reason")})


# --------------------------------------------------------------------------- process library
@app.get("/api/processes/{key}")
def get_process(key: str):
    if key not in packs.user_processes():
        raise HTTPException(404, "Unknown process.")
    folder = packs.pack_dir(key)
    pack = lib._read_yaml(folder / "pack.yaml", {}) or {}
    proposed = lib._read_yaml(folder / "pack.proposed.yaml", None) if (folder / "pack.proposed.yaml").exists() \
        else None
    return _clean({"key": key, "name": packs.display_name(key), "pack": pack, "proposed_pack": proposed,
                   "provenance": packs.provenance(pack), "rules": packs.load_rules(key),
                   "proposed_rules": pending_rules(key),
                   "decisions": list(reversed(lib._read_decisions(folder)))[:200]})


@app.get("/api/processes/{key}/records")
def process_records_view(key: str):
    """Everything recorded for one process: its library files and every linked analysis with its results,
    reports, readout, evidence and AI requests (read live from the analysis folders)."""
    from app.api import process_records as pr
    if key not in packs.user_processes():
        raise HTTPException(404, "Unknown process.")
    out = []
    for j in pr.analyses_of(key):
        folder = _folder(j["job_id"])
        files = pr.job_files(folder)
        try:
            summ = _summary(j)
        except Exception:  # noqa: BLE001 - one broken analysis must not hide the others
            summ = {"job_id": j["job_id"], "source_name": j.get("source_name"), "broken": True}
        out.append({**summ, "files": files, "exports": pr.exports(j, {f["name"] for f in files}),
                    "ai": pr.ai_counts(folder)})
    return _clean({"key": key, "name": packs.display_name(key), "library": pr.library_files(packs.pack_dir(key)),
                   "analyses": out})


@app.get("/api/processes/{key}/bundle.zip")
def process_bundle(key: str):
    from app.api import process_records as pr
    if key not in packs.user_processes():
        raise HTTPException(404, "Unknown process.")
    data = pr.bundle(key, packs.display_name(key), packs.pack_dir(key))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="process_{key}_{stamp}.zip"'})


@app.get("/api/processes/{key}/library-file")
def process_library_file(key: str, path: str):
    if key not in packs.user_processes():
        raise HTTPException(404, "Unknown process.")
    root = packs.pack_dir(key).resolve()
    p = (root / path).resolve()
    if root not in p.parents or not p.is_file():
        raise HTTPException(404, "Not a file of this process.")
    return FileResponse(p, filename=p.name)


@app.get("/api/jobs/{job_id}/files/{name}")
def job_file(job_id: str, name: str):
    from app.api import process_records as pr
    _job(job_id)
    try:
        p = pr.safe_file(_folder(job_id), name)
    except ValueError as err:
        raise HTTPException(404, str(err))
    return FileResponse(p, filename=p.name)


@app.get("/api/processes/{key}/map")
def process_business_map(key: str):
    """The business process map of a process: the approved stages and steps in business order, and the latest
    analysis of this process (if any) whose measured map can be laid over it."""
    if key not in packs.user_processes():
        raise HTTPException(404, "Unknown process.")
    pack = packs.load_pack(key) or {}
    nodes, order = [], []
    for stage in pack.get("stages") or []:
        for step in stage.get("steps") or []:
            nodes.append({"id": step.get("name"), "label": step.get("name"), "group": stage.get("name"),
                          "description": step.get("description"), "exception": step.get("exception"),
                          "documented": True, "apps": []})
            order.append(step.get("name"))
    edges = [{"source": a, "target": b, "kind": "order"} for a, b in zip(order, order[1:])]
    analyses = []
    for j in jobs.list_jobs():
        if j.get("process") == key and (jobs.job_folder(j["job_id"]) / ("nc_flow.json" if j.get("mode") == "nocase"
                                                                         else "mining.json")).exists():
            analyses.append({"job_id": j["job_id"], "source_name": j.get("source_name"), "mode": mode_of(j),
                             "created_at": j.get("created_at")})
    analyses.sort(key=lambda a: str(a.get("created_at") or a["job_id"]), reverse=True)
    return _clean({"key": key, "name": packs.display_name(key), "provenance": packs.provenance(pack),
                   "nodes": nodes, "edges": edges, "analyses": analyses})


class PackSave(BaseModel):
    stages: list[dict]
    by: str
    comment: str = ""


@app.put("/api/processes/{key}/pack")
def save_pack(key: str, body: PackSave):
    if not body.by.strip():
        raise HTTPException(400, "Enter your name to save.")
    folder = packs.pack_dir(key)
    current = lib._read_yaml(folder / "pack.yaml", {}) or {}
    value = {**current, "stages": body.stages}
    lib._snapshot(folder, body.by.strip(), "Before editing approved process pack")
    lib._atomic(folder / "pack.yaml", yaml.safe_dump(value, sort_keys=False, allow_unicode=True))
    lib._log(folder, {"who": body.by.strip(), "when": _now(), "item": "process-pack", "action": "edited",
                      "before": current, "after": value, "comment": body.comment})
    return {"ok": True}


class Reviewer(BaseModel):
    by: str
    comment: str = ""
    stages: list[dict] | None = None   # the stages and steps as edited by the reviewer (else the proposal as is)


@app.post("/api/processes/{key}/pack/approve")
def approve_pack(key: str, body: Reviewer):
    if not body.by.strip():
        raise HTTPException(400, "Enter your name to approve.")
    folder = packs.pack_dir(key)
    path = folder / "pack.proposed.yaml"
    if not path.exists() and not body.stages:
        raise HTTPException(400, "Nothing is waiting for approval.")
    current = lib._read_yaml(folder / "pack.yaml", {}) or {}
    proposed = (lib._read_yaml(path, {}) or {}) if path.exists() else dict(current)
    if body.stages is not None:
        stages = [{"name": str(st.get("name") or "").strip(), "description": st.get("description", ""),
                   "steps": [{"name": str(sp.get("name")).strip(), "description": sp.get("description", "")}
                             for sp in st.get("steps") or [] if str(sp.get("name") or "").strip()]}
                  for st in body.stages]
        stages = [st for st in stages if st["steps"]]
        if not stages:
            raise HTTPException(400, "Add at least one step.")
        names = [sp["name"] for st in stages for sp in st["steps"]]
        if len(names) != len(set(names)):
            raise HTTPException(400, "Each step name must be unique.")
        proposed["stages"] = stages
    proposed["name"] = current.get("name") or proposed.get("name") or key
    proposed["provenance"] = {"source": "SME review", "status": "sme_confirmed", "confirmed": True,
                              "by": body.by.strip()}
    lib._snapshot(folder, body.by.strip(), "Before approving proposed process pack")
    lib._atomic(folder / "pack.yaml", yaml.safe_dump(proposed, sort_keys=False, allow_unicode=True))
    lib._log(folder, {"who": body.by.strip(), "when": _now(), "item": "process-pack", "action": "approved",
                      "before": current, "after": proposed, "comment": body.comment})
    path.unlink(missing_ok=True)
    return {"ok": True, "steps": sum(len(st.get("steps") or []) for st in proposed.get("stages") or [])}


# --------------------------------------------------------------------------- no-case analysis run
def _lock(folder: Path) -> Path:
    return folder / ".nc_run.lock"


def _run_state(job_id: str) -> dict:
    st = dict(_RUNS.get(job_id) or {})
    lock = _lock(_folder(job_id))
    if not st.get("running") and lock.exists() and time.time() - lock.stat().st_mtime < 3600:
        st.update(running=True, started=lock.read_text(encoding="utf-8").strip(), elsewhere=True)
    return st


def _stage_rows(stages: dict) -> list[dict]:
    rows = []
    for name, r in (stages or {}).items():
        out = (r or {}).get("output") or {}
        rows.append({"stage": name, "status": (r or {}).get("status"),
                     "message": out.get("message") or (r or {}).get("reason") or (r or {}).get("error"),
                     "ai_note": out.get("ai_note"),
                     "layers": [{"strategy": x.get("strategy"), "applies": x.get("applies"), "reason": x.get("reason")}
                                for x in out.get("layers", [])] if isinstance(out.get("layers"), list) else None})
    return rows


def _run_thread(job_id: str, use_ai: bool):
    from app.nocase.stages import run_all, run_n4
    folder = _folder(job_id)
    state = _RUNS[job_id]
    try:
        job = jobs.load_job(job_id)
        if use_ai:
            approved = aip.approved_tasks(folder, "analysis")
            gw = aip.ApprovedGateway(_gw_class(job), approved, folder)
        else:
            gw = aip.PreviewGateway(_gw_class(job))
        state["message"] = "Running the analysis engine…"
        stages = run_all(folder, process=job.get("process"), ai=gw)
        run_n4(folder, job.get("process"))
        if not use_ai:
            aip.save_packages(folder, gw.packages, "analysis")
        (folder / "nc_last_run.json").write_text(json.dumps(_clean({
            "at": _now(), "with_ai": use_ai, "stages": _stage_rows(stages),
            "blocked": gw.blocked, "prepared": len(getattr(gw, "packages", []))}), indent=1), encoding="utf-8")
        job = jobs.load_job(job_id)
        job["status"] = "nc_analysed"
        jobs.save_job(job)
        state.update(running=False, finished=_now(), ok=True, message="Analysis complete.")
    except Exception as exc:  # noqa: BLE001
        state.update(running=False, finished=_now(), ok=False, message=f"{type(exc).__name__}: {exc}",
                     trace=traceback.format_exc()[-3000:])
    finally:
        _lock(folder).unlink(missing_ok=True)


class RunBody(BaseModel):
    use_ai: bool = False


@app.post("/api/jobs/{job_id}/run")
def start_run(job_id: str, body: RunBody):
    job = _job(job_id)
    if mode_of(job) != "nocase":
        raise HTTPException(400, "This run is for files without a case ID.")
    folder = _folder(job_id)
    if not (folder / "nc_events.parquet").exists():
        raise HTTPException(400, "Import and convert the file first.")
    if not job.get("process"):
        raise HTTPException(400, "Choose the process on Business context first: the analysis uses its steps.")
    if not approved_steps(job.get("process")):
        raise HTTPException(400, "Approve the stages and steps of the process on Business context first: the "
                                 "analysis links every screen to one of them.")
    with _RUN_GUARD:
        if _run_state(job_id).get("running"):
            raise HTTPException(409, "An analysis is already running for this file.")
        if body.use_ai and not aip.approved_tasks(folder, "analysis"):
            raise HTTPException(400, "No AI package is approved. Approve at least one task, or run without AI.")
        _lock(folder).write_text(datetime.now(timezone.utc).strftime("%H:%M"), encoding="utf-8")
        _RUNS[job_id] = {"running": True, "started": _now(), "with_ai": body.use_ai, "message": "Starting…"}
    threading.Thread(target=_run_thread, args=(job_id, body.use_ai), daemon=True).start()
    return _RUNS[job_id]


@app.get("/api/jobs/{job_id}/run")
def run_status(job_id: str):
    _job(job_id)
    return _clean({**_run_state(job_id), "last": _read(_folder(job_id), "nc_last_run.json")})


# --------------------------------------------------------------------------- overview, insights, map, stage panel
@app.get("/api/jobs/{job_id}/overview")
def overview(job_id: str):
    job = _job(job_id)
    folder = _folder(job_id)
    if mode_of(job) == "nocase":
        m = _read(folder, "nc_measures.json") or {}
        return _clean({"mode": "nocase", "scope": _read(folder, "nc_scope.json"), "overall": m.get("overall"),
                       "basis": m.get("basis"), "stages": m.get("stages"), "steps": m.get("steps"),
                       "apps": m.get("apps"), "assign_quality": _read(folder, "nc_assign_quality.json"),
                       "capabilities": _read(folder, "nc_capabilities.json"),
                       "evidence_plan": _read(folder, "nc_evidence_plan.json"),
                       "evidence_scores": _read(folder, "nc_evidence_scores.json"),
                       "units": _units_table(folder), "time_of_day": m.get("time_of_day"),
                       "day_of_week": m.get("day_of_week"), "activities": m.get("activities"),
                       "last_run": _read(folder, "nc_last_run.json")})
    return _clean({"mode": "case", "summary": job.get("summary"), "quality": _read(folder, "quality.json"),
                   "kpis": (_read(folder, "mining.json") or {}).get("kpis")})


def _data_quality(job: dict, folder: Path) -> dict:
    """The Data quality tab (adopted from ProcessLens): what was kept, what was left out and why, the time basis."""
    if mode_of(job) == "nocase":
        sc = _read(folder, "nc_scope.json") or {}
        m = _read(folder, "nc_measures.json") or {}
        ck = _read(folder, "nc_checks.json") or {}
        o = m.get("overall") or {}
        return {"mode": "nocase", "rows_in_file": sc.get("rows_in_file"), "in_scope": sc.get("in_scope"),
                "left_out_by_filter": sc.get("left_out_by_filter"), "unreadable_time": sc.get("unreadable_time"),
                "first_event": sc.get("first_event"), "last_event": sc.get("last_event"),
                "mapped": sc.get("mapped"), "time_basis": (m.get("basis") or {}).get("time"),
                "generic_hours": o.get("generic_hours"), "unassigned_share": o.get("unassigned_share"),
                "checks_passed": ck.get("passed"), "checks_failed": ck.get("failed"),
                "checks": ck.get("checks", []), "capabilities": _read(folder, "nc_capabilities.json")}
    q = _read(folder, "quality.json") or {}
    return {"mode": "case", "summary": job.get("summary"), "feasibility": q.get("feasibility"), "band": q.get("band"),
            "dimensions": q.get("dimensions"), "checks": q.get("checks", []), "fixes": q.get("fixes", [])}


def _units_table(folder: Path, top: int = 300) -> list[dict]:
    up, ap = folder / "nc_units.parquet", folder / "nc_assignments.parquet"
    if not up.exists():
        return []
    u = pd.read_parquet(up)
    if ap.exists():
        u = u.merge(pd.read_parquet(ap)[["unit_id", "step", "source", "status"]], on="unit_id", how="left")
    cols = [c for c in ("unit_id", "app", "screen_clean", "action_clean", "events", "actors", "active_hours",
                        "is_generic", "step", "source", "status") if c in u.columns]
    u = u.sort_values("active_hours" if "active_hours" in u else "events", ascending=False).head(top)[cols]
    return u.rename(columns={"screen_clean": "screen", "action_clean": "click", "active_hours": "hours"}) \
        .to_dict("records")


@app.get("/api/jobs/{job_id}/insights")
def insights(job_id: str, min_active_days: int = 5):
    job = _job(job_id)
    try:
        out = insights_mod.build(_folder(job_id), mode_of(job), min_active_days)
        out["quality"] = _data_quality(job, _folder(job_id))
        return _clean(out)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Insights could not be computed: {exc}")


def _step_apps(m: dict) -> dict[str, list[dict]]:
    mx = m.get("matrix_step_app") or {}
    out = {}
    for i, step in enumerate(mx.get("rows", [])):
        cells = mx.get("cells", [])[i] if i < len(mx.get("cells", [])) else []
        apps = [{"app": a, "hours": float(h or 0)} for a, h in zip(mx.get("columns", []), cells) if h]
        out[step] = sorted(apps, key=lambda x: -x["hours"])
    return out


@app.get("/api/jobs/{job_id}/map")
def process_map(job_id: str, level: str = "step", step: str | None = None):
    """level: stage (Level 2) | step (Level 3) | activity (Level 4, inside one step)."""
    job = _job(job_id)
    folder = _folder(job_id)
    if mode_of(job) == "case":
        return _case_map(folder, level if level in ("step", "stage", "observed") else "step", job)
    flow = _read(folder, "nc_flow.json")
    m = _read(folder, "nc_measures.json") or {}
    if not flow:
        raise HTTPException(400, "Run the analysis first.")
    apps = _step_apps(m)
    steps = {s["step"]: s for s in m.get("steps", [])}
    total = sum(float(s.get("hours") or 0) for s in m.get("steps", [])) or 1.0
    unassigned = steps.get("Unassigned", {})
    meta = {"unassigned_hours": unassigned.get("hours"), "unassigned_share": unassigned.get("share_of_hours"),
            "order_source": flow.get("order_source"), "multi_step_episodes": flow.get("multi_step_episodes"),
            "min_episodes": flow.get("min_episodes"), "min_actors": flow.get("min_actors"),
            "levels": [{"value": "stage", "label": "Level 2 · Stages"}, {"value": "step", "label": "Level 3 · Steps"},
                       {"value": "activity", "label": "Level 4 · Activities in a step"}],
            "steps": [n["step"] for n in flow.get("nodes", [])]}
    if level == "activity":
        ins = {s["step"]: s for s in (_read(folder, "nc_inside_step.json") or {}).get("steps", [])}
        pick = step or next(iter(ins), None)
        s = ins.get(pick) or {}
        nodes = [{"id": a["activity"], "label": a["activity"], "hours": a.get("hours"), "share": a.get("share"),
                  "group": pick, "kind": a.get("work_type"), "apps": []} for a in s.get("time_split", [])]
        edges = [{"source": t["from"], "target": t["to"], "count": t.get("runs"), "share": t.get("share"),
                  "kind": "observed"} for t in s.get("transitions", [])]
        return _clean({"level": "activity", "step": pick, "nodes": nodes, "edges": edges, "missing": [], **meta,
                       "note": "Order of activities inside one run of the step (share of runs)."})
    nodes, edges = [], []
    if level == "stage":
        stage_of = {n["step"]: n.get("stage") or "(no stage)" for n in flow.get("nodes", [])}
        for st in m.get("stages", []):
            if not st.get("stage"):
                continue
            st_apps: dict[str, float] = {}
            for stp, stg in stage_of.items():
                if stg == st["stage"]:
                    for a in apps.get(stp, []):
                        st_apps[a["app"]] = st_apps.get(a["app"], 0) + a["hours"]
            nodes.append({"id": st["stage"], "label": st["stage"], "hours": st.get("hours"),
                          "share": float(st.get("hours") or 0) / total, "events": st.get("events"),
                          "actors": st.get("actors"),
                          "apps": [{"app": k, "hours": v} for k, v in sorted(st_apps.items(), key=lambda x: -x[1])]})
        agg: dict[tuple, dict] = {}
        for e in flow.get("spine", []) + [x for x in flow.get("edges", []) if x.get("kept")]:
            a, b = stage_of.get(e["src"]), stage_of.get(e["dst"])
            if not a or not b or a == b:
                continue
            kind = "order" if e in flow.get("spine", []) else "observed"
            k = (a, b, kind)
            agg.setdefault(k, {"source": a, "target": b, "kind": kind, "count": 0})
            agg[k]["count"] += int(e.get("count") or 0)
        edges = list(agg.values())
        return _clean({"level": "stage", "nodes": nodes, "edges": edges, "missing": [], **meta})
    for n in flow.get("nodes", []):
        s = steps.get(n["step"], {})
        nodes.append({"id": n["step"], "label": n["step"], "group": n.get("stage"), "hours": n.get("hours"),
                      "share": float(n.get("hours") or 0) / total, "events": n.get("events"),
                      "actors": n.get("actors"), "runs": s.get("runs"), "median_run_s": s.get("median_run_s"),
                      "apps": apps.get(n["step"], [])})
    missing = [x if isinstance(x, dict) else {"step": x} for x in flow.get("missing", [])]
    for x in missing:
        nodes.append({"id": x["step"], "label": x["step"], "group": x.get("stage"), "missing": True, "hours": 0,
                      "apps": []})
    # business order: the approved step order, through every step on the map (also the ones not seen yet)
    on_map = {n["id"] for n in nodes}
    try:
        order = [x for x in packs.step_order(packs.load_pack(job.get("process"))) if x in on_map]
    except (ValueError, OSError):
        order = []
    if len(order) < 2:
        order_pairs = [(e["src"], e["dst"]) for e in flow.get("spine", [])]
    else:
        order_pairs = list(zip(order, order[1:]))
    edges = [{"source": a, "target": b, "kind": "order"} for a, b in order_pairs]
    edges += [{"source": e["src"], "target": e["dst"], "kind": "observed", "count": e.get("count"),
               "episodes": e.get("episodes"), "status": e.get("status")}
              for e in flow.get("edges", []) if e.get("kept")]
    others = [{"source": e["src"], "target": e["dst"], "count": e.get("count"), "episodes": e.get("episodes"),
               "why": "below the thresholds"} for e in flow.get("edges", []) if not e.get("kept")]
    mx = m.get("matrix_step_app") or {}
    meta["matrix"] = {"rows": mx.get("rows", []), "columns": mx.get("columns", []), "cells": mx.get("cells", [])}
    return _clean({"level": "step", "nodes": nodes, "edges": edges, "missing": [x["step"] for x in missing],
                   "other_transitions": others, **meta})


def _case_apps(folder: Path) -> dict[str, list[dict]]:
    """activity -> [{app, hours}] from the event log (end minus start of each event; none without end times)."""
    from app.core.db import connect
    p = (folder / "events.parquet").as_posix()
    try:
        rows = connect().execute(f"""SELECT activity, application,
                 SUM(GREATEST(epoch(ts_end) - epoch(ts_start), 0)) / 3600.0
               FROM read_parquet('{p}') WHERE application IS NOT NULL AND ts_end IS NOT NULL
               GROUP BY 1, 2 ORDER BY 1, 3 DESC""").fetchall()
    except Exception:  # noqa: BLE001 - no application or end column: boxes without applications
        return {}
    out: dict[str, list[dict]] = {}
    for act, app_name, h in rows:
        out.setdefault(act, []).append({"app": app_name, "hours": round(float(h or 0), 3)})
    return out


CASE_LEVELS = [{"value": "step", "label": "Level 3 · Steps (intended flow)"},
               {"value": "stage", "label": "Level 2 · Stages"},
               {"value": "observed", "label": "All steps as observed"}]


def _case_map(folder: Path, level: str = "step", job: dict | None = None) -> dict:
    """Case ID map. step / stage: the INTENDED flow (reference.json: the approved process with the activities
    linked to its steps, else the order found in the data) with the cases that reach each step, the business order
    (grey) and the transitions seen in the cases (blue). observed: every activity, directly-follows."""
    from app.core import case_steps as cs
    mi = _read(folder, "mining.json")
    if not mi:
        raise HTTPException(400, "Run the process analysis first.")
    mp = mi.get("map") or {}
    if not mp.get("available"):
        raise HTTPException(400, mp.get("reason") or "The map is not available for this data.")
    d = mp.get("data") or {}
    boundary = {d.get("start_node"), d.get("end_node")}
    apps = _case_apps(folder)
    raw_nodes = {n["activity"]: n for n in d.get("nodes", [])}
    raw_edges = [e for e in d.get("edges", []) if e.get("src") != e.get("dst")]
    loops = [{"activity": e["src"], "count": e.get("count"), "cases": e.get("cases")}
             for e in d.get("edges", []) if e.get("src") == e.get("dst")]
    ref = _read(folder, "reference.json")
    summ = _reference_summary(folder) if ref else None
    base = {"mode": "case", "levels": CASE_LEVELS, "steps": [], "self_loops": loops,
            "order_source": {"label": (ref or {}).get("source_badge")} if ref else None,
            "confirmed": str((ref or {}).get("choice", "")).startswith("confirmed"),
            "conformance": (summ or {}).get("conformance") or [], "in_order_share": (summ or {}).get("in_order_share"),
            "flow": (ref or {}).get("flow")}
    if level == "observed" or not ref:
        nodes = [{"id": n["activity"], "label": n["activity"], "events": n.get("events"), "cases": n.get("cases"),
                  "hours": None, "share": None, "apps": apps.get(n["activity"], []),
                  "boundary": n["activity"] in boundary or n["activity"].startswith(("\u25b6", "\u25a0"))}
                 for n in d.get("nodes", [])]
        edges = [{"source": e["src"], "target": e["dst"], "count": e.get("count"), "cases": e.get("cases"),
                  "median_wait_h": e.get("median_wait_h"), "overlap_share": e.get("overlap_share"),
                  "kind": "observed"} for e in raw_edges]
        note = None if ref else "Run the analysis again to see the approved process on the map."
        return _clean({**base, "level": "observed", "nodes": nodes, "edges": edges, "missing": [], "note": note})
    order = ref["order"]
    stats = {s["step"]: s for s in ref["stats"].get("steps", [])}
    drawn = [x for st in order.get("stages", []) for x in st.get("steps", [])]
    stage_of = {x: st.get("name") for st in order.get("stages", []) for x in st.get("steps", [])}
    no_stage = {None, "", "Steps", "(no stage)"}
    job = job or {}
    acts = list(raw_nodes)
    linked = {r["activity"]: r["step"] for r in cs.links(job.get("process"), acts)} if job.get("process") else {}
    missing = [m["step"] for m in cs.missing_steps(job.get("process"), acts)] if job.get("process") and \
        str(ref.get("choice", "")).endswith("_process") else []
    if level == "stage" and all(st.get("name") in no_stage for st in order.get("stages", [])):
        level = "step"                    # an order without stages (found in the data): show the steps
    if level == "stage":
        nodes, agg = [], {}
        n_cases = ref["stats"].get("cases") or 0
        for stg in ref["stats"].get("stages", []):
            st_apps: dict[str, float] = {}
            for x in drawn:
                if stage_of.get(x) == stg["name"]:
                    for a in apps.get(x, []):
                        st_apps[a["app"]] = st_apps.get(a["app"], 0) + a["hours"]
            nodes.append({"id": stg["name"], "label": stg["name"], "cases": stg.get("cases"), "share": None,
                          "hours": None, "events": sum(int((raw_nodes.get(x) or {}).get("events") or 0)
                                                      for x in drawn if stage_of.get(x) == stg["name"]),
                          "apps": [{"app": k, "hours": v} for k, v in sorted(st_apps.items(), key=lambda t: -t[1])]})
        names = [n["id"] for n in nodes]
        edges = [{"source": a, "target": b, "kind": "order", "count": None} for a, b in zip(names, names[1:])]
        for e in raw_edges:
            a, b = stage_of.get(e["src"]), stage_of.get(e["dst"])
            if a and b and a != b:
                k = (a, b)
                agg.setdefault(k, {"source": a, "target": b, "kind": "observed", "count": 0, "cases": 0})
                agg[k]["count"] += int(e.get("count") or 0)
        return _clean({**base, "level": "stage", "nodes": nodes, "edges": edges + list(agg.values()), "missing": [],
                       "cases": n_cases})
    nodes = []
    for x in drawn:
        rn, st = raw_nodes.get(x) or {}, stats.get(x) or {}
        step = linked.get(x)
        nodes.append({"id": x, "label": x, "group": None if stage_of.get(x) in no_stage else stage_of.get(x),
                      "events": rn.get("events") or st.get("events"), "cases": st.get("cases_reached"),
                      "case_share": st.get("share_reached"), "hours": None, "share": None,
                      "approved_step": step if step and step != x else None, "apps": apps.get(x, [])})
    # documented steps not found in the data are listed under the map (not drawn: they have no place in the data)
    edges = [{"source": a, "target": b, "kind": "order", "count": None} for a, b in zip(drawn, drawn[1:])]
    on = set(drawn)
    edges += [{"source": e["src"], "target": e["dst"], "count": e.get("count"), "cases": e.get("cases"),
               "median_wait_h": e.get("median_wait_h"), "overlap_share": e.get("overlap_share"), "kind": "observed"}
              for e in raw_edges if e["src"] in on and e["dst"] in on]
    other = (ref["stats"].get("other_steps") or {}).get("names") or []
    return _clean({**base, "level": "step", "nodes": nodes, "edges": edges, "missing": [],
                   "documented_missing": missing, "other_steps": other, "note": None})


@app.get("/api/jobs/{job_id}/case-results")
def case_results(job_id: str):
    """Every section of the case-ID analysis (mining.json), each with available / reason, as the classic app."""
    _job(job_id)
    folder = _folder(job_id)
    mi = _read(folder, "mining.json")
    if not mi:
        raise HTTPException(404, "Run the process analysis first.")
    keys = ("kpis", "variants", "bottlenecks", "rework", "resources", "applications", "app_switching", "trends",
            "flow_confidence", "subprocess", "level4", "process_model", "inside")
    out = {k: {"available": (mi.get(k) or {}).get("available"), "reason": (mi.get(k) or {}).get("reason"),
               "data": (mi.get(k) or {}).get("data")} for k in keys}
    for k in ("level4", "process_model", "inside", "subprocess"):
        out[k]["data"] = None                  # large; shown in the classic app
    return _clean({"band": mi.get("band"), "band_label": mi.get("band_label"), "created_at": mi.get("created_at"),
                   "sections": out})


@app.get("/api/jobs/{job_id}/stage")
def stage_panel(job_id: str, step: str):
    """Everything about one step: applications and hours, Level 4 activities, patterns, screens, confidence."""
    from app.api import stage_explain as sx
    job = _job(job_id)
    folder = _folder(job_id)
    if mode_of(job) == "case":                 # a box of the case map: the activity, its Level 4 and summaries
        cs = sx.case_step(folder, step)
        if not cs:
            raise HTTPException(404, "Unknown step.")
        return _clean({"kind": "case_step", "step": cs, "level4": sx.level4_case(folder, step),
                       "explanation": sx.load(folder).get(step)})
    m = _read(folder, "nc_measures.json") or {}
    s = next((x for x in m.get("steps", []) if x["step"] == step), None)
    st_stage = next((x for x in m.get("stages", []) if x.get("stage") == step), None)
    if not s and not st_stage:
        raise HTTPException(404, "Unknown step.")
    if st_stage and not s:
        steps = [x for x in m.get("steps", []) if x.get("stage") == step]
        return _clean({"kind": "stage", "stage": st_stage, "steps": steps})
    ins = next((x for x in (_read(folder, "nc_inside_step.json") or {}).get("steps", []) if x["step"] == step), {})
    ins = {k: v for k, v in ins.items() if k != "dot"}
    acts = [a for a in m.get("activities", []) if a.get("step") == step]
    conf = ((_read(folder, "nc_confidence.json") or {}).get("steps") or {}).get(step)
    screens = []
    units_p, asg_p = folder / "nc_units.parquet", folder / "nc_assignments.parquet"
    if units_p.exists() and asg_p.exists():
        u = pd.read_parquet(units_p).merge(pd.read_parquet(asg_p)[["unit_id", "step", "source", "status"]],
                                           on="unit_id", how="left")
        u = u[u["step"] == step].sort_values("active_hours", ascending=False).head(25)
        screens = u[["app", "screen_clean", "action_clean", "events", "active_hours", "source", "status"]] \
            .rename(columns={"screen_clean": "screen", "action_clean": "click", "active_hours": "hours"}) \
            .to_dict("records")
    return _clean({"kind": "step", "step": s, "apps": _step_apps(m).get(step, []), "activities": acts,
                   "inside": ins, "confidence": conf, "screens": screens, "level4": sx.level4_nocase(folder, step),
                   "explanation": sx.load(folder).get(step)})


# --------------------------------------------------------------------------- AI step explanation (ProcessLens)
class StageAIBody(BaseModel):
    step: str
    budget: str = "compact"


def _stage_payload(job: dict, folder: Path, step: str, budget: str) -> dict:
    from app.api import stage_explain as sx
    done = (folder / "nc_measures.json").exists() if mode_of(job) == "nocase" else (folder / "mining.json").exists()
    if not done:
        raise HTTPException(400, "Run the analysis first: the explanation is built from its results.")
    try:
        return sx.payload(folder, job, step, budget)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/jobs/{job_id}/stage-ai")
def get_stage_ai(job_id: str, step: str, budget: str = "compact"):
    from app.api import ai_summary, stage_explain as sx
    job = _job(job_id)
    folder = _folder(job_id)
    p = _stage_payload(job, folder, step, budget)
    return _clean({"step": step, "budget": budget, "payload": p, "fingerprint": aip.fingerprint(sx.messages(p)),
                   "approx_input_tokens": ai_summary.estimate_tokens(p), "saved": sx.load(folder).get(step),
                   "ai": _ai_status()})


@app.post("/api/jobs/{job_id}/stage-ai/prepare")
def stage_ai_prepare(job_id: str, body: StageAIBody):
    from app.api import stage_explain as sx
    job = _job(job_id)
    folder = _folder(job_id)
    p = _stage_payload(job, folder, body.step, body.budget)
    gw = aip.PreviewGateway(_gw_class(job))
    gw.ask(sx.TASK, sx.messages(p), lambda d: sx.check(d, p))
    aip.save_packages(folder, gw.packages, "stage")
    return _clean({"prepared": len(gw.packages), "blocked": gw.blocked, "step": body.step, "budget": body.budget})


@app.post("/api/jobs/{job_id}/stage-ai/generate")
def stage_ai_generate(job_id: str, body: StageAIBody):
    from app.api import stage_explain as sx
    job = _job(job_id)
    folder = _folder(job_id)
    approved = aip.approved_tasks(folder, "stage")
    if sx.TASK not in approved:
        raise HTTPException(400, "Prepare and approve the explanation request for this step first.")
    p = _stage_payload(job, folder, body.step, body.budget)
    msgs = sx.messages(p)
    if aip.fingerprint(msgs) not in (approved[sx.TASK].get("fingerprints") or []):
        raise HTTPException(400, "The approved request is for another step or profile, or the data changed. "
                                 "Prepare and approve this one.")
    gw = aip.ApprovedGateway(_gw_class(job), approved, folder)
    answer = gw.ask(sx.TASK, msgs, lambda d: sx.check(d, p), retry=lambda text, errs: sx.retry_messages(p, text, errs))
    if not answer.ok:
        why = str(answer.reason)
        if answer.notes:
            why += " | AI answer problems: " + " / ".join(answer.notes)
        raise HTTPException(400, f"No explanation: {why}")
    return _clean(sx.save(folder, body.step, answer.data, answer, aip.fingerprint(msgs), body.budget))


# --------------------------------------------------------------------------- review steps & questions
@app.get("/api/jobs/{job_id}/review")
def review(job_id: str):
    from app.nocase.questions import all_questions
    job = _job(job_id)
    folder = _folder(job_id)
    proc = job.get("process")
    if not proc:
        raise HTTPException(400, "Choose the process on Business context first.")
    pack = packs.load_pack(proc) or {}
    step_names = [x["name"] for stg in pack.get("stages", []) for x in stg.get("steps", [])]
    units = pd.read_parquet(folder / "nc_units.parquet") if (folder / "nc_units.parquet").exists() else pd.DataFrame()
    proposals = []
    for r in pending_rules(proc):
        mt = r.get("match") or {}
        hit = units
        if len(units):
            mask = pd.Series(True, index=units.index)
            if mt.get("app") and mt["app"] != "*":
                mask &= units["app"].fillna("").astype(str).str.fullmatch(re.escape(str(mt["app"])), case=False)
            for col, key in (("screen_clean", "screen"), ("action_clean", "action")):
                if mt.get(key):
                    try:
                        mask &= units[col].fillna("").astype(str).str.contains(str(mt[key]), case=False, regex=True)
                    except re.error:
                        mask &= False
            hit = units[mask]
        proposals.append({"id": r.get("id"), "step": r.get("step"), "app": mt.get("app"),
                          "screen": _plain(mt.get("screen")), "click": _plain(mt.get("action")) or "any",
                          "screen_regex": mt.get("screen"), "action_regex": mt.get("action"),
                          "units": int(len(hit)), "events": int(hit["events"].sum()) if len(hit) else 0,
                          "hours": round(float(hit["active_hours"].fillna(0).sum()), 2) if len(hit) else 0,
                          "why": r.get("why") or "AI or heuristic proposal", "confidence": r.get("confidence")})
    unassigned = []
    if len(units) and (folder / "nc_assignments.parquet").exists():
        a = pd.read_parquet(folder / "nc_assignments.parquet")
        u = units.merge(a[["unit_id", "step", "status"]], on="unit_id", how="left")
        u = u[u["status"].eq("unassigned") | u["step"].isna()].sort_values(["active_hours", "unit_id"],
                                                                             ascending=[False, True])
        unassigned = u[["unit_id", "app", "screen_clean", "action_clean", "events", "active_hours"]].rename(
            columns={"screen_clean": "screen", "action_clean": "click", "active_hours": "hours"}).head(300) \
            .to_dict("records")
    return _clean({"process": proc, "steps": step_names, "proposed": proposals, "unassigned": unassigned,
                   "confirmed_rules": len([r for r in packs.load_rules(proc)
                                           if r.get("status") in ("sme_confirmed", "rule")]),
                   "questions": all_questions(folder, proc)})


def _plain(rx):
    if not rx:
        return ""
    t = str(rx)
    if t.startswith("^") and t.endswith("$"):
        body = t[1:-1]
        plain = re.sub(r"\\(.)", r"\1", body)
        if re.escape(plain) == body:
            return plain
    return f"pattern: {t}"


class AcceptRules(BaseModel):
    rules: list[dict]          # [{id, step}] the step possibly changed by the reviewer
    by: str
    comment: str = ""


@app.post("/api/jobs/{job_id}/review/accept")
def accept_rules(job_id: str, body: AcceptRules):
    job = _job(job_id)
    proc = job.get("process")
    if not body.by.strip():
        raise HTTPException(400, "Enter your name to accept.")
    pending = {str(r.get("id")): r for r in pending_rules(proc)}
    items = []
    for x in body.rules:
        r = pending.get(str(x.get("id")))
        if not r:
            continue
        items.append({"id": str(r["id"]), "step": x.get("step") or r.get("step"), "match": r.get("match"),
                      "why": r.get("why") or "SME reviewed", "confidence": 1.0})
    if not items:
        raise HTTPException(400, "None of these rules is waiting.")
    lib.propose(proc, "rules", items, source="sme review")
    res = lib.approve([i["id"] for i in items], body.by.strip(), body.comment or "Accepted on Review steps",
                      process=proc)
    return _clean({"ok": True, "result": res})


class RejectRules(BaseModel):
    ids: list[str]
    by: str
    reason: str


@app.post("/api/jobs/{job_id}/review/reject")
def reject_rules(job_id: str, body: RejectRules):
    job = _job(job_id)
    if not (body.by.strip() and body.reason.strip()):
        raise HTTPException(400, "Enter your name and a reason.")
    return _clean(lib.reject(body.ids, body.by.strip(), body.reason.strip(), process=job.get("process")))


class Assign(BaseModel):
    items: list[dict]          # [{unit_id, step}]
    by: str
    comment: str = ""


@app.post("/api/jobs/{job_id}/review/assign")
def assign_units(job_id: str, body: Assign):
    job = _job(job_id)
    proc = job.get("process")
    if not body.by.strip():
        raise HTTPException(400, "Enter your name to save.")
    pack = packs.load_pack(proc) or {}
    stage_of = {x.get("name"): stg["name"] for stg in pack.get("stages", []) for x in stg.get("steps", [])}
    units = pd.read_parquet(_folder(job_id) / "nc_units.parquet").set_index("unit_id")
    items, ids = [], []
    for x in body.items:
        if not x.get("step") or str(x.get("unit_id")) not in units.index:
            continue
        row = units.loc[str(x["unit_id"])]
        rid = f"SME-{x['unit_id']}"
        items.append({"id": rid, "step": x["step"], "stage": stage_of.get(x["step"]),
                      "match": {"app": row.get("app") or "*",
                                "screen": "^" + re.escape(str(row["screen_clean"])) + "$"
                                if row.get("screen_clean") else None,
                                "action": "^" + re.escape(str(row["action_clean"])) + "$"
                                if row.get("action_clean") else None},
                      "source": "sme", "status": "proposed", "confidence": 1.0})
        ids.append(rid)
    if not items:
        raise HTTPException(400, "Pick a step for at least one unit.")
    lib.propose(proc, "rules", items, source="sme assignment")
    lib.approve(ids, body.by.strip(), body.comment or f"Assigned {len(ids)} unit(s) on Review steps", process=proc)
    return {"saved": len(ids)}


class Answer(BaseModel):
    key: str
    question: str
    answer: str
    by: str


@app.post("/api/jobs/{job_id}/questions/answer")
def answer_question(job_id: str, body: Answer):
    from app.nocase.questions import save_answer
    job = _job(job_id)
    if not (body.by.strip() and body.answer.strip()):
        raise HTTPException(400, "Enter your name and the answer.")
    save_answer(job["process"], body.key, body.question, body.answer.strip(), body.by.strip())
    return {"ok": True}


class NewQuestion(BaseModel):
    question: str


@app.post("/api/jobs/{job_id}/questions")
def new_question(job_id: str, body: NewQuestion):
    from app.nocase.questions import add_question
    _job(job_id)
    if not body.question.strip():
        raise HTTPException(400, "Type the question.")
    return add_question(_folder(job_id), body.question)


# --------------------------------------------------------------------------- review activities (Level 4) and order
def _activity_proposals(proc: str, folder: Path) -> dict:
    """Proposed Level 4 groups: written to the process library by the engine (older runs: the job folder)."""
    for p in (packs.pack_dir(proc) / "activities.proposed.yaml", folder / "activities.proposed.yaml"):
        if p.exists():
            doc = lib._read_yaml(p, {}) or {}
            if doc.get("steps"):
                return doc["steps"]
    return {}


def _approved_activities(proc: str) -> list[dict]:
    """activities.yaml as the library writes it: {"steps": {step: [group, ...]}} (older files: {"groups": [...]})."""
    doc = lib.load(proc)["approved"].get("activities.yaml") or {}
    if isinstance(doc, list):
        return doc
    out = [{**g, "step": g.get("step") or step} for step, gs in (doc.get("steps") or {}).items() for g in gs or []]
    return out + list(doc.get("groups") or [])


@app.get("/api/jobs/{job_id}/activities")
def review_activities(job_id: str):
    job = _job(job_id)
    proc = job.get("process")
    if not proc:
        raise HTTPException(400, "Choose the process on Business context first.")
    groups = _activity_proposals(proc, _folder(job_id))
    proposed = [{"id": str(g.get("id") or f"activity:{step}:{i}"), "step": step, "activity": g.get("label", ""),
                 "description": g.get("description", ""), "rules": g.get("rules") or []}
                for step, gs in groups.items() for i, g in enumerate(gs or [])]
    approved = _approved_activities(proc)
    acts = pd.read_parquet(_folder(job_id) / "nc_activities.parquet") \
        if (_folder(job_id) / "nc_activities.parquet").exists() else pd.DataFrame()
    return _clean({"proposed": proposed,
                   "approved": [{"step": g.get("step"), "activity": g.get("label") or g.get("name"),
                                 "status": g.get("status"), "id": g.get("id")} for g in approved if isinstance(g, dict)],
                   "in_use": acts.to_dict("records") if len(acts) else []})


class ActivityAccept(BaseModel):
    items: list[dict]          # [{id, activity, description}] for the selected groups
    by: str
    comment: str = ""


@app.post("/api/jobs/{job_id}/activities/accept")
def accept_activities(job_id: str, body: ActivityAccept):
    job = _job(job_id)
    proc = job.get("process")
    if not body.by.strip():
        raise HTTPException(400, "Enter your name.")
    groups = _activity_proposals(proc, _folder(job_id))
    edits = {str(x.get("id")): x for x in body.items}
    data: dict = {"steps": {}}                 # every waiting group stays waiting; the selected ones carry the edits
    ids = []
    for step, gs in groups.items():
        for i, g in enumerate(gs or []):
            gid = str(g.get("id") or f"activity:{step}:{i}")
            x = edits.get(gid)
            row = {**g, "id": gid}
            if x:
                row.update(label=str(x.get("activity") or g.get("label", "")).strip(),
                           description=str(x.get("description") or g.get("description", "")).strip())
                ids.append(gid)
            data["steps"].setdefault(step, []).append(row)
    if not ids:
        raise HTTPException(400, "Select at least one group that is waiting.")
    lib.propose(proc, "activities", data, source="sme edit")
    return _clean(lib.approve(ids, body.by.strip(), body.comment, process=proc))


@app.post("/api/jobs/{job_id}/activities/reject")
def reject_activities(job_id: str, body: RejectRules):
    job = _job(job_id)
    if not (body.by.strip() and body.reason.strip()):
        raise HTTPException(400, "Enter your name and a reason.")
    return _clean(lib.reject(body.ids, body.by.strip(), body.reason.strip(), process=job.get("process")))


@app.get("/api/jobs/{job_id}/order")
def review_order(job_id: str):
    job = _job(job_id)
    proc = job.get("process")
    if not proc:
        raise HTTPException(400, "Choose the process on Business context first.")
    pack = packs.load_pack(proc) or {}
    rows = [{"position": i + 1, "stage": stage.get("name", ""), "step": step.get("name", ""),
             "exception": step.get("exception", "")}
            for i, (stage, step) in enumerate((st, sp) for st in pack.get("stages") or [] for sp in st.get("steps") or [])]
    approved = lib.load(proc)["approved"].get("order.yaml") or {}
    return _clean({"rows": rows, "provenance": packs.provenance(pack),
                   "edges": approved.get("edges", []) if isinstance(approved, dict) else []})


class OrderSave(BaseModel):
    rows: list[dict]           # [{position, stage, step, exception}]
    by: str
    comment: str = ""


@app.put("/api/jobs/{job_id}/order")
def save_order(job_id: str, body: OrderSave):
    job = _job(job_id)
    proc = job.get("process")
    if not body.by.strip():
        raise HTTPException(400, "Enter who confirmed the order.")
    pack = packs.load_pack(proc) or {}
    old = {sp.get("name"): sp for st in pack.get("stages") or [] for sp in st.get("steps") or []}
    stages: list[dict] = []
    for r in sorted(body.rows, key=lambda r: float(r.get("position") or 0)):
        if not str(r.get("step") or "").strip():
            continue
        if not stages or stages[-1]["name"] != r.get("stage"):
            stages.append({"name": r.get("stage") or "", "description": "", "steps": []})
        step = {**old.get(r["step"], {}), "name": r["step"]}
        step.pop("exception", None)
        if str(r.get("exception") or "").strip():
            step["exception"] = r["exception"].strip()
        stages[-1]["steps"].append(step)
    value = {**pack, "stages": stages, "provenance": {"source": "SME review", "status": "sme_confirmed",
                                                      "confirmed": True, "by": body.by.strip()}}
    folder = packs.pack_dir(proc)
    lib._snapshot(folder, body.by.strip(), "Before changing business order")
    lib._atomic(folder / "pack.yaml", yaml.safe_dump(value, sort_keys=False, allow_unicode=True))
    lib._log(folder, {"who": body.by.strip(), "when": _now(), "item": "process-order", "action": "approved",
                      "before": pack, "after": value, "comment": body.comment})
    if mode_of(job) == "case" and (_folder(job_id) / "mining.json").exists():
        _case_reference(job)              # the map, conformance and report use the new order now
    return {"ok": True}


# --------------------------------------------------------------------------- case ID: activities -> approved steps
def _case_reference(job: dict) -> dict:
    """Recompute the intended order (reference.json) for a case-ID analysis after links or order changed, and keep
    mining.json's flow confidence in step with it (no full re-run of the analysis needed)."""
    from app.core import reference
    from app.core.db import connect
    folder = _folder(job["job_id"])
    name = Path(job.get("source_name") or "Process").stem or "Process"
    job.pop("order_choice", None)                        # the approved process is the default from now on
    ref = reference.compute_for_job(connect(), folder / "events.parquet", job, name)
    (folder / "reference.json").write_text(json.dumps(ref, indent=2, default=str), encoding="utf-8")
    job["order_source"] = ref.get("source_badge")
    job["flow_band"] = (ref.get("flow") or {}).get("band")
    jobs.save_job(job)
    mi = _read(folder, "mining.json")
    if mi and isinstance(mi.get("flow_confidence"), dict):
        mi["flow_confidence"]["data"] = {**(ref.get("flow") or {}), "source_badge": ref.get("source_badge")}
        mi["flow_confidence"]["available"] = True
        (folder / "mining.json").write_text(json.dumps(mi, indent=2, default=str), encoding="utf-8")
    return ref


def _reference_summary(folder: Path) -> dict | None:
    from app.core import reference
    ref = _read(folder, "reference.json")
    if not ref:
        return None
    order = reference.Order.from_dict(ref["order"])
    st = ref["stats"]
    return {"choice": ref.get("choice"), "source_badge": ref.get("source_badge"), "flow": ref.get("flow"),
            "cases": st.get("cases"), "in_order_share": st.get("in_order_share"),
            "pair_order_share": st.get("pair_order_share"), "reach_last_share": st.get("reach_last_share"),
            "last_step": st.get("last_step"), "earlier_than_intended": st.get("earlier_than_intended") or [],
            "back_and_forth": st.get("back_and_forth") or [], "steps": st.get("steps") or [],
            "stages": st.get("stages") or [], "other_steps": st.get("other_steps") or {},
            "order": ref["order"],
            "describe": reference.describe_reference(order, st),
            "conformance": reference.describe_conformance(order, st, ref.get("flow"))}


@app.get("/api/jobs/{job_id}/case-steps")
def case_steps_view(job_id: str):
    """Case ID, Review steps: every step of the data (Level 3, with its Level 2 stage) checked against the
    documented steps (same / similar / not in the documents), the intended flow (confirmed, else prefilled) and how
    the cases follow it."""
    from app.core import case_steps as cs
    job = _job(job_id)
    folder = _folder(job_id)
    if mode_of(job) != "case":
        raise HTTPException(400, "Only for analyses with a case ID.")
    if not (folder / "events.parquet").exists():
        raise HTTPException(400, "Import the data first.")
    proc = job.get("process")
    facts = cs.data_facts(folder / "events.parquet")
    names = [f["activity"] for f in facts]
    link = {r["activity"]: r for r in cs.links(proc, names)}
    pack = packs.load_pack(proc) if proc else None
    saved, flow = cs.load(proc), cs.load_flow(proc)
    rows, flow_source = cs.flow_rows(job, folder, facts)
    fact = {f["activity"]: f for f in facts}
    for r in rows:
        f = fact.get(r["step"]) or {}
        r.update(cases=f.get("cases"), events=f.get("events"), data_stage=f.get("stage"),
                 documented=(link.get(r["step"]) or {}).get("step") or "")
    level1 = sorted({f["level1"] for f in facts if f.get("level1")})
    return _clean({"process": proc, "process_name": packs.display_name(proc) if proc else None,
                   "level1": level1, "has_stages": any(f.get("stage") for f in facts),
                   "steps": cs.approved_steps(proc), "provenance": packs.provenance(pack) if pack else None,
                   "activities": [{**f, "step": link[f["activity"]]["step"], "verdict": link[f["activity"]]["verdict"],
                                   "source": link[f["activity"]]["source"],
                                   "suggested": link[f["activity"]]["suggested"]} for f in facts],
                   "reviewed": cs.reviewed(proc, names), "missing": cs.missing_steps(proc, names),
                   "analysed": (folder / "mining.json").exists(),
                   "links_by": saved.get("by"), "links_at": saved.get("at"),
                   "flow": rows, "flow_source": flow_source, "flow_by": flow.get("by"), "flow_at": flow.get("at"),
                   "reference": _reference_summary(folder)})


class CaseLinks(BaseModel):
    links: dict[str, str]          # data step -> documented step ("" = not in the documents)
    by: str


@app.put("/api/jobs/{job_id}/case-steps")
def case_steps_save(job_id: str, body: CaseLinks):
    from app.core import case_steps as cs
    job = _job(job_id)
    if mode_of(job) != "case":
        raise HTTPException(400, "Only for analyses with a case ID.")
    if not job.get("process"):
        raise HTTPException(400, "Choose the process on Business context first.")
    if not body.by.strip():
        raise HTTPException(400, "Enter your name to confirm the comparison.")
    try:
        cs.save(job["process"], body.links, body.by.strip())
    except ValueError as err:
        raise HTTPException(400, str(err))
    if (_folder(job_id) / "mining.json").exists():
        _case_reference(job)
    return case_steps_view(job_id)


class CaseFlow(BaseModel):
    rows: list[dict]               # [{stage, step, in_flow}] in the intended order
    by: str


@app.put("/api/jobs/{job_id}/case-flow")
def case_flow_save(job_id: str, body: CaseFlow):
    """Confirm the intended flow (order of the data's steps, their stages, which are part of the flow)."""
    from app.core import case_steps as cs
    job = _job(job_id)
    if mode_of(job) != "case":
        raise HTTPException(400, "Only for analyses with a case ID.")
    if not job.get("process"):
        raise HTTPException(400, "The analysis has no process.")
    if not body.by.strip():
        raise HTTPException(400, "Enter your name to confirm the intended flow.")
    known = {f["activity"] for f in cs.data_facts(_folder(job_id) / "events.parquet")}
    rows = [{"stage": str(r.get("stage") or "").strip(), "step": str(r.get("step")), "in_flow": bool(r.get("in_flow", True))}
            for r in body.rows if str(r.get("step")) in known]
    if len({r["step"] for r in rows}) != len(rows):
        raise HTTPException(400, "A step is listed twice.")
    try:
        cs.save_flow(job["process"], rows, body.by.strip(), packs.display_name(job["process"]))
    except ValueError as err:
        raise HTTPException(400, str(err))
    if (_folder(job_id) / "mining.json").exists():
        _case_reference(job)
    return case_steps_view(job_id)


# --------------------------------------------------------------------------- report
@app.post("/api/jobs/{job_id}/report/prepare")
def report_prepare(job_id: str):
    """No case ID: build the report with the summary request kept for approval (fact-only summary meanwhile)."""
    from app.nocase.report.report import build
    job = _job(job_id)
    if not context_done(job):
        raise HTTPException(400, "Business context is required before the report.")
    folder = _folder(job_id)
    if mode_of(job) != "nocase":
        raise HTTPException(400, "Use Generate for files with a case ID.")
    gw = aip.PreviewGateway(_gw_class(job))
    r = build(folder, job.get("process"), gw)
    aip.save_packages(folder, gw.packages, "report")
    return _clean({"checks": r.get("checks"), "prepared": len(gw.packages), "ai_used": False})


@app.post("/api/jobs/{job_id}/report/generate")
def report_generate(job_id: str, use_ai: bool = False):
    job = _job(job_id)
    if not context_done(job):
        raise HTTPException(400, "Business context is required before the report.")
    folder = _folder(job_id)
    if mode_of(job) == "nocase":
        from app.nocase.report.report import build
        if use_ai:
            approved = aip.approved_tasks(folder, "report")
            if not approved:
                raise HTTPException(400, "Approve the summary package first, or generate without AI.")
            gw = aip.ApprovedGateway(_gw_class(job), approved, folder)
        else:
            gw = aip.PreviewGateway(_gw_class(job))
        r = build(folder, job.get("process"), gw)
        if not use_ai:
            aip.save_packages(folder, gw.packages, "report")
        return _clean({"checks": r.get("checks"), "ai_used": use_ai})
    from app.core.report import generate_report
    if not (folder / "mining.json").exists():
        raise HTTPException(400, "Run the process analysis first.")
    if use_ai:
        approved = aip.approved_tasks(folder, "report")
        if "summary" not in approved:
            raise HTTPException(400, "Approve the summary request first, or generate without AI.")
        client = aip.ApprovedClient(_gw_class(job), approved, folder)
    else:
        client = aip.RecordingClient(_gw_class(job))
    try:
        generate_report(job_id, use_ai=True, client=client, inside_ai=False)
    except ValueError as err:
        raise HTTPException(400, str(err))
    if not use_ai:
        aip.save_packages(folder, client.packages, "report")
    return {"ok": True, "ai_used": use_ai, "prepared": len(client.packages)}


def _why_no_ai(reason):
    if reason and "prepared for approval" in str(reason):
        return "the AI request is prepared and waiting for your approval (below)"
    return reason


@app.get("/api/jobs/{job_id}/summary")
def report_summary(job_id: str):
    """The report's summary and what it rests on: checked facts only (no AI), or worded by the AI."""
    job = _job(job_id)
    folder = _folder(job_id)
    if mode_of(job) == "nocase":
        s = _read(folder, "nc_summary.json")
        if not s:
            return {"available": False}
        src = s.get("source") or "templates"
        texts = s.get("texts") or {}
        return _clean({"available": True, "mode": "nocase", "ai": src.startswith("ai:"),
                       "source": "worded by the AI (" + src[3:] + ")" if src.startswith("ai:")
                       else "written from the checked facts (no AI)", "reason": _why_no_ai(s.get("reason")),
                       "generated": s.get("generated"), "checks": s.get("checks"),
                       "overview": texts.get("overview"),
                       "steps": [{"step": k.split(":", 1)[1], "text": v} for k, v in texts.items()
                                 if k.startswith("step:")],
                       "inside": [{"step": k.split(":", 1)[1], "text": v} for k, v in texts.items()
                                  if k.startswith("inside:")]})
    n = _read(folder, "narrative.json")
    if not n:
        return {"available": False}
    ai = n.get("source") == "ai"
    return _clean({"available": True, "mode": "case", "ai": ai,
                   "source": f"worded by the AI ({n.get('model') or n.get('provider')})" if ai
                   else "written from fixed templates and the checked facts (no AI)", "reason": _why_no_ai(n.get("reason")),
                   "summary": [x.get("text") for x in n.get("summary", [])],
                   "findings": [x.get("text") for x in n.get("findings", [])],
                   "recommendations": [x.get("text") for x in n.get("recommendations", [])],
                   "facts_sent": n.get("facts_sent")})


@app.get("/api/jobs/{job_id}/facts.json")
def facts_json(job_id: str):
    job = _job(job_id)
    p = _folder(job_id) / ("nc_facts.json" if mode_of(job) == "nocase" else "facts.json")
    if not p.exists():
        raise HTTPException(404, "No facts yet: generate the report.")
    return FileResponse(p, filename=f"facts_{job_id}.json")


@app.get("/api/jobs/{job_id}/report.html", response_class=HTMLResponse)
def report_html(job_id: str):
    job = _job(job_id)
    p = _folder(job_id) / ("nc_report.html" if mode_of(job) == "nocase" else "report.html")
    if not p.exists():
        raise HTTPException(404, "No report yet.")
    return HTMLResponse(p.read_text(encoding="utf-8"))


@app.get("/api/jobs/{job_id}/report.xlsx")
def report_xlsx(job_id: str):
    job = _job(job_id)
    p = _folder(job_id) / ("nc_report.xlsx" if mode_of(job) == "nocase" else "report.xlsx")
    if not p.exists():
        raise HTTPException(404, "No Excel report yet.")
    return FileResponse(p, filename=f"report_{job_id}.xlsx")


@app.get("/api/jobs/{job_id}/report.pdf")
def report_pdf(job_id: str):
    job = _job(job_id)
    folder = _folder(job_id)
    name = packs.display_name(job["process"]) if job.get("process") else (job.get("source_name") or "Process")
    try:
        data = pdf_mod.nocase_pdf(folder, job, name) if mode_of(job) == "nocase" else \
            pdf_mod.case_pdf(folder, job, name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"The PDF could not be made: {exc}")
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="readout_{job_id}.pdf"'})


# --------------------------------------------------------------------------- AI readout (seven sections)
def _readout_ready(job: dict, folder: Path) -> None:
    if not context_done(job):
        raise HTTPException(400, "Business context is required before the readout.")
    if not (folder / ("nc_facts.json" if mode_of(job) == "nocase" else "facts.json")).exists():
        raise HTTPException(400, "Generate the report first: the readout is written from its checked facts.")


@app.get("/api/jobs/{job_id}/readout")
def get_readout(job_id: str):
    from app.api import readout as ro
    job = _job(job_id)
    folder = _folder(job_id)
    ready = context_done(job) and (folder / ("nc_facts.json" if mode_of(job) == "nocase" else "facts.json")).exists()
    ev = ro.evidence(folder, job) if ready else None
    saved = ro.load(folder)
    current = aip.fingerprint(ro.messages(folder, job)) if ready else None
    return _clean({"ready": ready, "evidence": ev, "readout": saved,
                   "stale": bool(saved and current and saved.get("fingerprint") != current), "ai": _ai_status()})


@app.post("/api/jobs/{job_id}/readout/prepare")
def readout_prepare(job_id: str):
    from app.api import readout as ro
    job = _job(job_id)
    folder = _folder(job_id)
    _readout_ready(job, folder)
    gw = aip.PreviewGateway(_gw_class(job))
    ev = ro.evidence(folder, job)
    gw.ask(ro.TASK, ro.messages(folder, job), lambda d: ro.check(d, ev))
    aip.save_packages(folder, gw.packages, "readout")
    return _clean({"prepared": len(gw.packages), "blocked": gw.blocked})


@app.post("/api/jobs/{job_id}/readout/generate")
def readout_generate(job_id: str):
    from app.api import readout as ro
    job = _job(job_id)
    folder = _folder(job_id)
    _readout_ready(job, folder)
    approved = aip.approved_tasks(folder, "readout")
    if ro.TASK not in approved:
        raise HTTPException(400, "Approve the readout request first (it must match the current evidence).")
    ev = ro.evidence(folder, job)
    msgs = ro.messages(folder, job)
    gw = aip.ApprovedGateway(_gw_class(job), approved, folder)
    answer = gw.ask(ro.TASK, msgs, lambda d: ro.check(d, ev), retry=lambda text, errs: ro.retry_messages(ev, text, errs))
    if not answer.ok:
        why = str(answer.reason)
        if "per minute" in why or "tokens per" in why or "429" in why or "rate" in why.lower():
            why += (" — the provider's per-minute limit was reached. Wait one minute and click Write the readout "
                    "again (the approval stays valid).")
        if answer.notes:
            why += " | AI answer problems: " + " / ".join(answer.notes)
        raise HTTPException(400, f"No readout: {why}")
    return _clean(ro.save(folder, answer.data, answer, aip.fingerprint(msgs)))


@app.get("/api/jobs/{job_id}/readout.pdf")
def readout_pdf(job_id: str):
    from app.api import readout as ro
    job = _job(job_id)
    folder = _folder(job_id)
    saved = ro.load(folder)
    if not saved:
        raise HTTPException(404, "No readout yet.")
    name = packs.display_name(job["process"]) if job.get("process") else (job.get("source_name") or "Process")
    data = pdf_mod.readout_pdf(saved, ro.evidence(folder, job), job, name)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="ai_readout_{job_id}.pdf"'})


# --------------------------------------------------------------------------- AI-enhanced conceptual map (ProcessLens)
class MapAIBody(BaseModel):
    scope: str | None = None
    budget: str = "compact"


def _map_ai_ready(job: dict, folder: Path) -> None:
    done = (folder / "nc_flow.json").exists() if mode_of(job) == "nocase" else (folder / "mining.json").exists()
    if not done:
        raise HTTPException(400, "Run the analysis first: the summary is built from its results.")


@app.get("/api/jobs/{job_id}/ai-map")
def get_ai_map(job_id: str, scope: str | None = None, budget: str = "compact"):
    from app.api import ai_map, ai_summary
    job = _job(job_id)
    folder = _folder(job_id)
    ready = (folder / ("nc_flow.json" if mode_of(job) == "nocase" else "mining.json")).exists()
    scope = scope or ai_map.default_scope(job)
    out = {"ready": ready, "scope": scope, "budget": budget, "saved": ai_map.load(folder), "ai": _ai_status()}
    if ready:
        try:
            payload = ai_map.summary(folder, job, scope, budget)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        out.update({"payload": payload, "fingerprint": aip.fingerprint(ai_map.messages(payload)),
                    "approx_input_tokens": ai_summary.estimate_tokens(payload)})
    return _clean(out)


@app.post("/api/jobs/{job_id}/ai-map/prepare")
def ai_map_prepare(job_id: str, body: MapAIBody):
    from app.api import ai_map
    job = _job(job_id)
    folder = _folder(job_id)
    _map_ai_ready(job, folder)
    scope = body.scope or ai_map.default_scope(job)
    try:
        payload = ai_map.summary(folder, job, scope, body.budget)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    gw = aip.PreviewGateway(_gw_class(job))
    gw.ask(ai_map.TASK, ai_map.messages(payload), ai_map.check)
    aip.save_packages(folder, gw.packages, "map")
    return _clean({"prepared": len(gw.packages), "blocked": gw.blocked, "scope": scope, "budget": body.budget})


@app.post("/api/jobs/{job_id}/ai-map/generate")
def ai_map_generate(job_id: str, body: MapAIBody):
    from app.api import ai_map
    job = _job(job_id)
    folder = _folder(job_id)
    _map_ai_ready(job, folder)
    scope = body.scope or ai_map.default_scope(job)
    approved = aip.approved_tasks(folder, "map")
    if ai_map.TASK not in approved:
        raise HTTPException(400, "Prepare and approve this exact summary first (scope and profile must match).")
    payload = ai_map.summary(folder, job, scope, body.budget)
    msgs = ai_map.messages(payload)
    if aip.fingerprint(msgs) not in (approved[ai_map.TASK].get("fingerprints") or []):
        raise HTTPException(400, "The approved summary is for another scope or profile, or the data changed. "
                                 "Prepare and approve this one.")
    gw = aip.ApprovedGateway(_gw_class(job), approved, folder)
    answer = gw.ask(ai_map.TASK, msgs, ai_map.check)
    if not answer.ok:
        why = str(answer.reason)
        if answer.notes:
            why += " | AI answer problems: " + " / ".join(answer.notes)
        raise HTTPException(400, f"No conceptual map: {why}")
    return _clean(ai_map.save(folder, answer.data, answer, aip.fingerprint(msgs), scope, body.budget))


# --------------------------------------------------------------------------- case ID: quality and analysis
@app.post("/api/jobs/{job_id}/quality")
def run_quality(job_id: str):
    from app.core.db import connect
    from app.core.quality import run_quality as rq, save_report
    job = _job(job_id)
    folder = _folder(job_id)
    if not (folder / "events.parquet").exists():
        raise HTTPException(400, "Import and convert the file first.")
    rep = rq(connect(), folder / "events.parquet")
    save_report(rep, folder)
    job.update(status="quality_checked", feasibility=rep.feasibility, band=rep.band)
    jobs.save_job(job)
    return _clean(_read(folder, "quality.json"))


@app.get("/api/jobs/{job_id}/quality")
def get_quality(job_id: str):
    _job(job_id)
    q = _read(_folder(job_id), "quality.json")
    if not q:
        raise HTTPException(404, "Run the quality checks first.")
    return _clean(q)


@app.post("/api/jobs/{job_id}/analysis")
def run_analysis(job_id: str):
    from app.core.db import connect
    from app.core.mining import run_mining, save_mining
    job = _job(job_id)
    folder = _folder(job_id)
    q = _read(folder, "quality.json")
    if not q:
        raise HTTPException(400, "Run the quality checks first.")
    if not context_done(job):
        raise HTTPException(400, "Confirm the Business context first: its approved steps are the intended flow "
                                 "the analysis compares the cases with.")
    result = run_mining(connect(), folder / "events.parquet", q["band"], use_ai=False)
    save_mining(result, folder)
    job.update(status="mined", mined_at=result["created_at"],
               flow_band=((result.get("flow_confidence") or {}).get("data") or {}).get("band"))
    jobs.save_job(job)
    if (result.get("map") or {}).get("available"):
        _case_reference(job)              # the intended order (approved process) for the map and Review steps
    return {"ok": True}


# --------------------------------------------------------------------------- workflow map
@app.get("/api/system-map")
def system_map_view(mode: str = "nocase", job_id: str | None = None):
    job = _job(job_id) if job_id else None
    if job:
        mode = mode_of(job) if job.get("mode") == "nocase" or step_status(job).get("import") else mode
    folder = _folder(job_id) if job_id else None
    lib_dir = None
    if job and job.get("process"):
        try:
            lib_dir = packs.pack_dir(job["process"])
        except ValueError:
            lib_dir = None
    nodes = system_map.with_status(mode, folder, job, lib_dir)
    return _clean({"mode": mode, "lanes": [{"id": a, "label": b} for a, b in system_map.LANES], "nodes": nodes,
                   "edges": system_map.edges(mode), "ai": _ai_status()})


# --------------------------------------------------------------------------- exports (HTML and PDF)
def _attach(body, name: str, media: str) -> Response:
    """A file download; HTML exports are self-contained (they open offline and print to PDF)."""
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(text or "process")).strip("_").lower() or "process"


def _business_map_doc(key: str, view: str | None, direction: str) -> tuple[str, list]:
    """The business process map as documented (view empty or "documented") or as observed in one analysis."""
    from app.api.exports import map_elements
    d = process_business_map(key)
    if not view or view == "documented":
        return d["name"], map_elements(
            f"{d['name']} · business process map", "BUSINESS PROCESS MAP · AS DOCUMENTED",
            f"Stages and steps in business order. {d['provenance'].get('label', '')}", d["nodes"], d["edges"],
            direction)
    analysis = next((a for a in d["analyses"] if a["job_id"] == view), None)
    if analysis is None:
        raise HTTPException(404, "This analysis is not an analysis of this process.")
    obs = process_map(view, "step")
    mode = "NO CASE ID" if analysis.get("mode") == "nocase" else "CASE ID"
    notes = []
    if obs.get("unassigned_share") is not None:
        notes.append(f"{100 * (1 - float(obs['unassigned_share'])):.0f}% of the hours are linked to a step.")
    return d["name"], map_elements(
        f"{d['name']} · business process map", f"BUSINESS PROCESS MAP · AS OBSERVED · {mode}",
        f"As observed in {analysis.get('source_name')}: hours and applications per step, the business order and "
        "the transitions seen in the data", obs["nodes"], obs["edges"], direction, notes)


@app.get("/api/processes/{key}/map.html")
def business_map_html(key: str, view: str | None = None, dir: str = "LR"):
    from app.api.exports import elements_html
    name, el = _business_map_doc(key, view, dir)
    return _attach(elements_html(el, f"{name} · business process map"), f"business_map_{_slug(name)}.html",
                   "text/html; charset=utf-8")


@app.get("/api/processes/{key}/map.pdf")
def business_map_pdf(key: str, view: str | None = None, dir: str = "LR"):
    from app.api.exports import pdf_bytes
    name, el = _business_map_doc(key, view, dir)
    return _attach(pdf_bytes(el, f"{name} · business process map", landscape_page=True),
                   f"business_map_{_slug(name)}.pdf", "application/pdf")


def _job_map_doc(job_id: str, level: str, step: str | None, direction: str) -> tuple[str, list]:
    from app.api.exports import map_elements
    job = _job(job_id)
    d = process_map(job_id, level, step)
    name = packs.display_name(job["process"]) if job.get("process") else (job.get("source_name") or "Process")
    what = {"stage": "Level 2 · stages", "step": "Level 3 · steps",
            "activity": f"Level 4 · activities in {d.get('step')}" if mode_of(job) == "nocase" else "activities"}
    notes = [d["note"]] if d.get("note") else []
    if d.get("self_loops"):
        notes.append("Repeated activities (self-loops): " + ", ".join(
            f"{x['activity']} {int(x.get('count') or 0):,}×" for x in d["self_loops"][:10]))
    return name, map_elements(
        f"{name} · process map", f"PROCESS MAP · {'NO CASE ID' if mode_of(job) == 'nocase' else 'CASE ID'} · "
        f"{what.get(d.get('level'), d.get('level', '')).upper()}", f"Source: {job.get('source_name', '')}",
        d["nodes"], d["edges"], direction, notes)


@app.get("/api/jobs/{job_id}/map.html")
def job_map_html(job_id: str, level: str = "step", step: str | None = None, dir: str = "LR"):
    from app.api.exports import elements_html
    name, el = _job_map_doc(job_id, level, step, dir)
    return _attach(elements_html(el, f"{name} · process map"), f"process_map_{job_id}_{level}.html",
                   "text/html; charset=utf-8")


@app.get("/api/jobs/{job_id}/map.pdf")
def job_map_pdf(job_id: str, level: str = "step", step: str | None = None, dir: str = "LR"):
    from app.api.exports import pdf_bytes
    name, el = _job_map_doc(job_id, level, step, dir)
    return _attach(pdf_bytes(el, f"{name} · process map", landscape_page=True), f"process_map_{job_id}_{level}.pdf",
                   "application/pdf")


def _process_name(job: dict) -> str:
    return packs.display_name(job["process"]) if job.get("process") else (job.get("source_name") or "Process")


@app.get("/api/jobs/{job_id}/readout.html")
def readout_html(job_id: str):
    """The AI readout (seven sections) as HTML: the same content as readout.pdf."""
    from app.api import readout as ro
    job = _job(job_id)
    folder = _folder(job_id)
    saved = ro.load(folder)
    if not saved:
        raise HTTPException(404, "No readout yet.")
    return _attach(pdf_mod.readout_html(saved, ro.evidence(folder, job), job, _process_name(job)),
                   f"ai_readout_{job_id}.html", "text/html; charset=utf-8")


@app.get("/api/jobs/{job_id}/stakeholder.html")
def stakeholder_html(job_id: str):
    """The stakeholder readout (from checked facts, no AI) as HTML: the same content as report.pdf."""
    job = _job(job_id)
    folder = _folder(job_id)
    try:
        body = pdf_mod.nocase_html(folder, job, _process_name(job)) if mode_of(job) == "nocase" else \
            pdf_mod.case_html(folder, job, _process_name(job))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"The readout could not be made: {exc}")
    return _attach(body, f"readout_{job_id}.html", "text/html; charset=utf-8")


@app.get("/api/jobs/{job_id}/analysis.html")
def analysis_html(job_id: str):
    """The analysis overview (the Analysis page's data) as one HTML file."""
    from app.api.exports import data_html
    job = _job(job_id)
    o = overview(job_id)
    if o.get("mode") == "nocase":
        sections = [("Scope", o.get("scope")), ("Overall", o.get("overall")), ("Time basis", o.get("basis")),
                    ("Stages", o.get("stages")), ("Steps", o.get("steps")), ("Applications", o.get("apps")),
                    ("Activities", o.get("activities")), ("How steps were assigned", (o.get("assign_quality") or {})
                                                          .get("by_source")),
                    ("Evidence scores", [{"signal": k, **v} for k, v in (o.get("evidence_scores") or {}).items()]),
                    ("Hours by hour of day", o.get("time_of_day")), ("Hours by weekday", o.get("day_of_week")),
                    ("Units of work (app · screen · click), largest first", o.get("units"))]
    else:
        sections = [("Summary", o.get("summary")), ("Key numbers", o.get("kpis")),
                    ("Data quality", {k: v for k, v in (o.get("quality") or {}).items() if k in
                                      ("feasibility", "band", "dimensions", "checks", "fixes")})]
    return _attach(data_html(f"{_process_name(job)} · analysis", "ANALYSIS", f"Source: {job.get('source_name', '')}",
                             sections), f"analysis_{job_id}.html", "text/html; charset=utf-8")


@app.get("/api/jobs/{job_id}/insights.html")
def insights_html(job_id: str, min_active_days: int = 5):
    """People, applications, time and data quality (the Insights page's data) as one HTML file."""
    from app.api.exports import data_html
    job = _job(job_id)
    d = insights(job_id, min_active_days)
    sections = [(k.replace("_", " ").capitalize(), v) for k, v in d.items() if v not in (None, {}, [])]
    return _attach(data_html(f"{_process_name(job)} · insights", "INSIGHTS",
                             f"Source: {job.get('source_name', '')} · people shown as P01, P02… (never names)",
                             sections), f"insights_{job_id}.html", "text/html; charset=utf-8")


@app.get("/api/jobs/{job_id}/case-results.html")
def case_results_html(job_id: str):
    """Every section of the case-ID analysis as one HTML file."""
    from app.api.exports import data_html
    job = _job(job_id)
    d = case_results(job_id)
    sections = [(k.replace("_", " ").capitalize(),
                 v["data"] if v.get("available") else f"Not available: {v.get('reason') or 'not computed'}")
                for k, v in (d.get("sections") or {}).items() if v.get("available") or v.get("reason")]
    return _attach(data_html(f"{_process_name(job)} · process mining results", "CASE ID · ANALYSIS RESULTS",
                             f"Source: {job.get('source_name', '')} · data quality band {d.get('band', '–')}",
                             sections), f"results_{job_id}.html", "text/html; charset=utf-8")


# --------------------------------------------------------------------------- the web page
if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    index = WEB_DIST / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>The web UI is not built yet.</h3><p>Run <code>cd web && npm install && npm run "
                            "build</code>, or use <code>npm run dev</code> on port 5173.</p>")
    target = WEB_DIST / path
    if path and target.is_file():
        return FileResponse(target)
    return FileResponse(index)
