"""Install only the configured private provider endpoint, then exec the container command."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml


def _provider_config() -> None:
    profile = os.getenv("PM_PROFILE")
    providers = {}
    if profile == "aws":
        model = os.getenv("VLLM_MODEL", "Qwen/Qwen3.5-27B")
        providers["vllm_private"] = {"base_url": "http://vllm.internal:8000/v1", "model": model}
        os.environ["ALLOW_EXTERNAL_AI"] = "false"
    elif profile == "local" and os.getenv("OLLAMA_BASE_URL") and os.getenv("OLLAMA_MODEL"):
        providers["ollama_local"] = {"base_url": os.environ["OLLAMA_BASE_URL"],
                                      "model": os.environ["OLLAMA_MODEL"]}
    if not providers:
        return
    path = Path(__file__).resolve().parents[1] / "config" / "ai_providers.local.yaml"
    old = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    merged = (old or {}).get("providers", {})
    merged.update(providers)
    path.write_text(yaml.safe_dump({"providers": merged}, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    _provider_config()
    os.execvp(sys.argv[1], sys.argv[1:])
