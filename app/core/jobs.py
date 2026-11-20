"""Job folders: every upload becomes a job with its own folder.

data/jobs/<job_id>/
    job.json            metadata: file name, status, row counts, timings
    raw/<file(s)>       the uploaded file or files (browser uploads only)
    mapping.json        the confirmed column mapping
    events.parquet      the normalised event table every later step reads
"""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from app.core.config import data_dir, settings

log = logging.getLogger(__name__)


# Job statuses are stages, in this order. A job at a later stage has also
# passed every earlier one (a "reported" job is also "mined", and so on).
STAGES = ("created", "profiled", "normalised", "quality_checked", "mined", "reported")


def reached(job: dict, stage: str) -> bool:
    """True when the job's status is `stage` or a later stage."""
    status = job.get("status")
    return status in STAGES and STAGES.index(status) >= STAGES.index(stage)


def is_newer(job_id: str, file_name: str, than: str) -> bool:
    """True when `file_name` exists in the job folder and was written after `than`.

    Used to tell whether a result is still current: e.g. mining.json is out of
    date once quality.json is rewritten. A missing `than` file counts as older.
    """
    folder = job_folder(job_id)
    path, other = folder / file_name, folder / than
    if not path.exists():
        return False
    return not other.exists() or path.stat().st_mtime > other.stat().st_mtime


def jobs_dir() -> Path:
    path = data_dir() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def create_job(source_name: str, source_path=None, dataset_class: str = "client") -> dict:
    """Create the job folder and job.json.

    `source_path` is one file, folder or glob pattern (stored as the text
    `source_path`), or a list of files (stored as `source_paths`).
    """
    job_id = new_job_id()
    folder = jobs_dir() / job_id
    (folder / "raw").mkdir(parents=True)
    job = {
        "job_id": job_id,
        "source_name": source_name,
        # A local path is read in place and never modified or copied.
        "source_path": None,
        "status": "created",
        "created_at": datetime.now(timezone.utc).isoformat(),
        # "client" (default): only providers marked `private: true` get AI calls; "public": any allowed provider.
        "dataset_class": dataset_class if dataset_class in ("client", "public") else "client",
    }
    from app.core.privacy import set_dataset_class
    set_dataset_class(job["dataset_class"])
    if isinstance(source_path, (list, tuple)):
        job["source_paths"] = [str(p) for p in source_path]
    elif source_path:
        job["source_path"] = str(source_path)
    save_job(job)
    return job


def job_source(job: dict):
    """What `ingest.open_source` should read for this job: a list of files or one path/pattern."""
    return job.get("source_paths") or job.get("source_path")


def job_folder(job_id: str) -> Path:
    return jobs_dir() / job_id


def save_job(job: dict, *, s3_backup: bool | None = None) -> None:
    """Persist metadata and optionally back up once after analysis completes.

    The new option is inert by default; automatic backup requires ``storage.s3_backup``.
    """
    path = job_folder(job["job_id"]) / "job.json"

    def write():
        with open(path, "w", encoding="utf-8") as f:
            json.dump(job, f, indent=2, default=str)

    write()
    enabled = (settings().get("storage") or {}).get("s3_backup", False) if s3_backup is None else s3_backup
    finished = job.get("status") in {"mined", "reported", "nc_complete", "nc_activities"}
    if not enabled or not finished or job.get("s3_backup_at"):
        return
    try:
        from scripts.s3_sync import maybe_push
        result = maybe_push(data_dir())
        if result:
            job["s3_backup_at"] = datetime.now(timezone.utc).isoformat()
            write()
    except Exception as exc:  # noqa: BLE001 - remote backup failures must not fail completed analysis
        log.warning("Automatic S3 backup did not complete for job %s (%s)", job.get("job_id"), type(exc).__name__)


def load_job(job_id: str) -> dict:
    """job.json; a job saved before dataset classes existed counts as "client". Loading a job also sets the
    dataset class for the AI calls that follow (privacy.set_dataset_class)."""
    from app.core.privacy import set_dataset_class
    with open(job_folder(job_id) / "job.json", encoding="utf-8") as f:
        job = json.load(f)
    job.setdefault("dataset_class", "client")
    set_dataset_class(job["dataset_class"])
    return job


def _copy_upload(target: Path, stream: BinaryIO) -> None:
    """Copy an uploaded file to disk in 16 MB chunks (never all in memory)."""
    with open(target, "wb") as out:
        shutil.copyfileobj(stream, out, length=16 * 1024 * 1024)


def save_browser_upload(job: dict, file_name: str, stream: BinaryIO) -> Path:
    """Save one uploaded file into raw/ and point the job at it."""
    target = job_folder(job["job_id"]) / "raw" / Path(file_name).name
    _copy_upload(target, stream)
    job["source_path"] = str(target)
    save_job(job)
    return target


def save_browser_uploads(job: dict, files) -> Path:
    """Save several uploaded files into raw/ and point the job at that folder.

    `files` are file-like objects with a `.name` (e.g. Streamlit uploads).
    Two uploads with the same name are kept apart with a number ("a (2).csv").
    """
    raw = job_folder(job["job_id"]) / "raw"
    for upload in files:
        name = Path(upload.name)
        target, n = raw / name.name, 2
        while target.exists():
            target = raw / f"{name.stem} ({n}){name.suffix}"
            n += 1
        _copy_upload(target, upload)
    job["source_path"] = str(raw)
    job.pop("source_paths", None)
    save_job(job)
    return raw


def list_jobs() -> list[dict]:
    jobs = []
    for folder in sorted(jobs_dir().iterdir(), reverse=True):
        meta = folder / "job.json"
        if meta.exists():
            with open(meta, encoding="utf-8") as f:
                jobs.append(json.load(f))
    return jobs


def backup_dir() -> Path:
    return data_dir() / "_backup"


def job_files(job_id: str) -> list[str]:
    """The files a job folder holds (relative paths), for the confirmation before moving it."""
    folder = job_folder(job_id)
    return sorted(str(p.relative_to(folder)).replace("\\", "/") for p in folder.rglob("*") if p.is_file())


def delete_job(job_id: str) -> Path:
    """"Delete" a job: MOVE its folder to <data_dir>/_backup/<timestamp>/jobs/<job_id> (never a hard
    delete). Shared stores (mappings, application names, orders, standards, definitions) stay."""
    folder = job_folder(job_id)
    if not (folder / "job.json").exists():
        raise ValueError(f"Job {job_id} does not exist.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir() / stamp / "jobs" / job_id
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder), str(target))
    return target
