"""Deployment health checks for local and container environments.

Run ``python scripts/health.py --json`` for dependency, data-folder and disk checks. ``--check-ai``
opts into a tiny JSON request to configured providers allowed by the active profile/privacy policy.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def health(check_ai: bool = False) -> dict:
    from app.core.config import data_dir, settings, startup_status
    cfg = settings()
    folder = data_dir()
    checks = {}
    try:
        with tempfile.NamedTemporaryFile(prefix=".health-", dir=folder, delete=False) as f:
            probe = Path(f.name)
            f.write(b"ok")
        probe.unlink()
        checks["data_folder"] = {"ok": True, "path": str(folder), "read_write": True}
    except OSError as exc:
        checks["data_folder"] = {"ok": False, "path": str(folder), "error": type(exc).__name__}
    try:
        import duckdb
        checks["duckdb"] = {"ok": True, "version": duckdb.__version__}
    except Exception as exc:  # noqa: BLE001
        checks["duckdb"] = {"ok": False, "error": type(exc).__name__}
    disk = shutil.disk_usage(folder)
    checks["disk"] = {"ok": True, "free_bytes": disk.free, "total_bytes": disk.total}

    from app.core import ai_client
    deployment = startup_status()
    allowed = {row["provider"] for row in deployment["providers"]}
    providers = []
    for name, provider in ai_client.providers().items():
        private = bool(provider.get("private"))
        ok, why = ai_client.usable(provider)
        active = name in allowed and (deployment["profile"] != "aws" or private)
        record = {"provider": name, "private": private, "configured": bool(ok), "allowed": active,
                  "status": "not checked"}
        if check_ai and active and ok:
            try:
                response = ai_client.complete("summary", [{"role": "user", "content": "Return JSON: {\"ok\": true}"}],
                                               max_output_tokens=16, only=name)
                record.update(status="ok", model=response.model)
            except Exception as exc:  # noqa: BLE001 - report health rather than fail the script
                record.update(status="failed", error=type(exc).__name__)
        elif check_ai and active:
            record.update(status="skipped", reason=why or "provider is not configured")
        providers.append(record)
    checks["ai_providers"] = {"ok": all(p["status"] != "failed" for p in providers), "providers": providers}

    storage = cfg.get("storage") or {}
    s3_enabled = bool(storage.get("s3_backup"))
    if s3_enabled:
        bucket = os.getenv("S3_BUCKET")
        if not bucket:
            checks["s3"] = {"ok": False, "reason": "S3_BUCKET is not set"}
        else:
            try:
                from scripts.s3_sync import _client
                _client().head_bucket(Bucket=bucket)
                checks["s3"] = {"ok": True, "bucket": bucket}
            except Exception as exc:  # noqa: BLE001
                checks["s3"] = {"ok": False, "bucket": bucket, "error": type(exc).__name__}
    else:
        checks["s3"] = {"ok": True, "enabled": False}
    checks["profile"] = {"ok": True, **deployment}
    return {"ok": all(item.get("ok", False) for item in checks.values()), "checks": checks}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--check-ai", action="store_true", help="send a tiny test request to configured allowed providers")
    args = parser.parse_args(argv)
    result = health(check_ai=args.check_ai)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for name, value in result["checks"].items():
            print(f"{'OK' if value['ok'] else 'FAIL'} {name}: {json.dumps(value, default=str)}")
        print("Overall: " + ("healthy" if result["ok"] else "unhealthy"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
