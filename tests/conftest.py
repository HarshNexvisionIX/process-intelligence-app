import os
import sys
from pathlib import Path

import pytest

# Let tests import `app...` and `scripts...` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The optional PM4Py model is off in tests (importing PM4Py takes seconds per analysis); the tests of the
# model switch it on themselves. Set here, so command-line subprocesses inherit it too.
os.environ["PM_DISABLE_PM4PY"] = "1"

# The app ships no process templates or sample packs; the tests use their own copies (subprocesses inherit these).
_SAMPLES = Path(__file__).resolve().parent / "sample_processes"
os.environ["PM_PROCESS_TEMPLATES_DIR"] = str(_SAMPLES / "process_templates")
os.environ["PM_SAMPLE_PACKS_DIR"] = str(_SAMPLES / "nocase_packs")


@pytest.fixture(autouse=True)
def no_network_ai(monkeypatch):
    """Tests never call a real AI model or Langfuse, whatever .env says.

    Variables are set to empty (not deleted) so that subprocesses, which load
    .env again, cannot fill them back in (load_dotenv never overrides).
    """
    monkeypatch.setenv("AI_ENABLED", "false")
    monkeypatch.setenv("ALLOW_EXTERNAL_AI", "false")
    monkeypatch.setenv("ALLOW_EXTERNAL_LANGFUSE", "false")
    for key in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "CEREBRAS_API_KEY",
                "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.setenv(key, "")


@pytest.fixture(autouse=True)
def no_dataset_class():
    """Each test starts without a job's dataset class (loading or creating a job sets it for the AI calls)."""
    from app.core import privacy
    privacy.set_dataset_class(None)
    yield
    privacy.set_dataset_class(None)
