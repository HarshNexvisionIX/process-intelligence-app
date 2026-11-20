"""Upload the local report-writer prompt to Langfuse as a NEW VERSION (no label).

The two files in app/prompts/ (system and user message) become one chat prompt
named `ai.prompt_name` in config/settings.yaml. The new version gets NO label:
moving the "production" label to it is a human decision, made in the Langfuse
UI after scripts/evaluate_narrative.py shows the new version is at least as good.

    python scripts/push_prompts.py

Needs LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL in .env.
Exit codes: 0 uploaded, 1 Langfuse is not configured or the upload failed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.core import observability
    from app.core.config import settings
    from app.core.narrative import local_prompt

    name = settings()["ai"]["prompt_name"]
    if not observability.enabled():
        print("Langfuse is not configured (LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL).")
        return 1
    version = observability.push_prompt(name, local_prompt())
    from app.core import level4
    extra = {level4.BUCKETING_PROMPT: level4.local_prompts("bucketing"),
             level4.SUMMARY_PROMPT: level4.local_prompts("summary")}
    extra_versions = {n: observability.push_prompt(n, msgs) for n, msgs in extra.items()}
    observability.flush()
    if version is None or None in extra_versions.values():
        print("Upload failed; see the warning above.")
        return 1
    for n, v in extra_versions.items():
        print(f"Uploaded '{n}' as version {v} (no label).")
    print(f"Uploaded '{name}' as version {version} (no label). Evaluate it with:\n"
          f"    python scripts/evaluate_narrative.py --prompt-version {version}\n"
          "then move the 'production' label to it in the Langfuse UI if it is good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
