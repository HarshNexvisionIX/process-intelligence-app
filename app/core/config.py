"""Loads settings.yaml and .env once, so every module reads the same values."""
from __future__ import annotations

import os
import copy
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _merge(base: dict, overlay: dict) -> dict:
    """Deep-merge a profile without mutating the base settings object."""
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def validate_profile(name: str | None, config: dict) -> None:
    """Reject unsafe AWS profiles when even one task route contains a public provider."""
    if name != "aws":
        return
    providers_path = PROJECT_ROOT / "config" / "ai_providers.yaml"
    with providers_path.open(encoding="utf-8") as f:
        providers = (yaml.safe_load(f) or {}).get("providers") or {}
    invalid = []
    routes = ((config.get("ai") or {}).get("routes") or {})
    for task, route in routes.items():
        for provider in route if isinstance(route, list) else [route]:
            if not (providers.get(provider) or {}).get("private", False):
                invalid.append(f"{task}:{provider}")
    if invalid:
        raise ValueError("AWS profile refuses non-private AI routes: " + ", ".join(invalid))


def apply_profile(base: dict, name: str | None, profiles_dir: Path | None = None) -> dict:
    """Apply one optional YAML overlay. No profile returns an exact deep copy of today's settings."""
    out = copy.deepcopy(base)
    if not name:
        return out
    if name not in {"local", "aws"}:
        raise ValueError(f"Unknown PM_PROFILE '{name}'. Choose local or aws.")
    path = (profiles_dir or PROJECT_ROOT / "config" / "profiles") / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"Profile file does not exist: {path}")
    with path.open(encoding="utf-8") as f:
        overlay = yaml.safe_load(f) or {}
    out = _merge(out, overlay)
    validate_profile(name, out)
    return out


@lru_cache(maxsize=1)
def settings() -> dict:
    with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        base = yaml.safe_load(f)
    profile = os.getenv("PM_PROFILE")
    configured = apply_profile(base, profile)
    if profile == "aws":
        # Make the privacy boundary effective even when an inherited .env sets it true.
        os.environ["ALLOW_EXTERNAL_AI"] = "false"
        os.environ["DATA_DIR"] = str((configured.get("storage") or {}).get("data_dir", "/data"))
        # The AWS profile owns routes; a custom LLM_* endpoint must not precede them.
        for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "AI_FORCE_PROVIDER"):
            os.environ[key] = ""
    return configured


def startup_status() -> dict:
    """Runtime deployment/privacy summary for stdout and the Streamlit sidebar."""
    cfg = settings()
    profile = os.getenv("PM_PROFILE") or "default"
    configured_dir = os.getenv("DATA_DIR", "data")
    folder = Path(configured_dir)
    if not folder.is_absolute():
        folder = PROJECT_ROOT / folder
    try:
        from app.core.ai_client import providers
        known = providers()
    except Exception:  # noqa: BLE001 - startup diagnostics must not block local use
        known = {}
    routes = (cfg.get("ai") or {}).get("routes") or {}
    rows, invalid = [], []
    for task, route in routes.items():
        names = route if isinstance(route, list) else [route]
        for name in names:
            is_private = bool((known.get(name) or {}).get("private"))
            rows.append({"task": task, "provider": name, "private": is_private})
            if profile == "aws" and not is_private:
                invalid.append(f"{task}:{name}")
    if invalid:
        raise ValueError("AWS profile refuses non-private AI routes: " + ", ".join(invalid))
    return {"profile": profile, "data_dir": str(folder), "providers": rows,
            "private_only": profile == "aws"}


def data_dir() -> Path:
    if os.getenv("PM_PROFILE"):
        settings()  # activate profile-specific environment paths before resolving the data folder
    path = Path(os.getenv("DATA_DIR", "data"))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def duckdb_limits() -> dict:
    return {
        "memory_limit": os.getenv("DUCKDB_MEMORY_LIMIT", "4GB"),
        "threads": int(os.getenv("DUCKDB_THREADS", "4")),
    }
