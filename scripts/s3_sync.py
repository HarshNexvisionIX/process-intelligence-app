"""Copy approved job outputs, the knowledge library and mappings to or from S3.

Credentials always come from boto3's standard provider chain (EC2 instance role in AWS); this
script never opens credential files. Raw job uploads are excluded unless ``--include-raw`` is set.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MAPPINGS = ("app_identity_map.csv", "mapping_profiles", "nocase_mappings")


def _client():
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("S3 support needs boto3; install requirements.txt") from exc
    return boto3.client("s3")


def _key(prefix: str, relative: str) -> str:
    return "/".join(part for part in (prefix.strip("/"), relative.replace("\\", "/").lstrip("/")) if part)


def _files(data_root: Path, include_raw: bool):
    roots = [data_root / "jobs", data_root / "library"]
    for name in MAPPINGS:
        path = data_root / name
        if path.exists():
            roots.append(path)
    for base in roots:
        if not base.exists():
            continue
        paths = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for path in paths:
            relative = path.relative_to(data_root).as_posix()
            if not include_raw and "raw" in PurePosixPath(relative).parts:
                continue
            if base.is_file():
                relative = "mappings/" + base.name
            elif relative.startswith(("mapping_profiles/", "nocase_mappings/")):
                relative = "mappings/" + relative
            yield path, relative


def push(bucket: str, prefix: str = "", data_root: Path | None = None, include_raw: bool = False,
         s3=None) -> dict:
    """Upload supported state files; no raw uploads unless explicitly requested."""
    from app.core.config import data_dir
    root = (data_root or data_dir()).resolve()
    client = s3 or _client()
    count, size = 0, 0
    for path, relative in _files(root, include_raw):
        client.upload_file(str(path), bucket, _key(prefix, relative))
        count += 1
        size += path.stat().st_size
    return {"files": count, "bytes": size, "bucket": bucket, "prefix": prefix}


def _destination(key: str, prefix: str, root: Path, include_raw: bool = False) -> Path | None:
    normalized = key.lstrip("/")
    prefix = prefix.strip("/")
    if prefix:
        if not normalized.startswith(prefix + "/"):
            return None
        normalized = normalized[len(prefix) + 1:]
    parts = PurePosixPath(normalized).parts
    if not parts:
        return None
    if parts[0] == "mappings":
        parts = parts[1:]
        if not parts:
            return None
    elif parts[0] not in ("jobs", "library"):
        return None
    if "raw" in parts and not include_raw:
        return None
    target = (root / Path(*parts)).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"Refusing S3 key outside the data folder: {key}")
    return target


def pull(bucket: str, prefix: str = "", data_root: Path | None = None, include_raw: bool = False,
         s3=None) -> dict:
    """Restore backed-up state into the local data folder with traversal checks."""
    from app.core.config import data_dir
    root = (data_root or data_dir()).resolve()
    client = s3 or _client()
    paginator = client.get_paginator("list_objects_v2")
    count, size = 0, 0
    search_prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
    for page in paginator.paginate(Bucket=bucket, Prefix=search_prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            target = _destination(key, prefix, root, include_raw=include_raw)
            if target is None:
                continue
            if not include_raw and "raw" in PurePosixPath(key).parts:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            count += 1
            size += int(item.get("Size") or 0)
    return {"files": count, "bytes": size, "bucket": bucket, "prefix": prefix}


def maybe_push(data_root: Path | None = None) -> dict | None:
    """Best-effort automatic backup hook used only when storage.s3_backup is enabled."""
    from app.core.config import settings
    cfg = settings().get("storage") or {}
    if not cfg.get("s3_backup"):
        return None
    bucket = os.getenv("S3_BUCKET")
    if not bucket:
        raise RuntimeError("storage.s3_backup is enabled but S3_BUCKET is not set")
    return push(bucket, os.getenv("S3_PREFIX", ""), data_root,
                include_raw=bool(cfg.get("s3_include_raw", False)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("push", "pull"))
    parser.add_argument("--bucket", default=os.getenv("S3_BUCKET"))
    parser.add_argument("--prefix", default=os.getenv("S3_PREFIX", ""))
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--include-raw", action="store_true", help="also copy original uploaded files")
    args = parser.parse_args(argv)
    if not args.bucket:
        parser.error("--bucket or S3_BUCKET is required")
    result = (push if args.operation == "push" else pull)(args.bucket, args.prefix, args.data_dir,
                                                           include_raw=args.include_raw)
    print(f"{args.operation}: {result['files']} files, {result['bytes']} bytes, s3://{args.bucket}/"
          f"{args.prefix.strip('/')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
