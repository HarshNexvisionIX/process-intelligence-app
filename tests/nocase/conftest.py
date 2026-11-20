"""No-case tests: every job is written to a temporary data folder (never data/); AI off (tests/conftest.py)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def nc_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def run_fixture(tmp_path):
    """run_fixture("hr_like") -> (job, folder, result of app.nocase.pipeline.run)."""
    from app.core import jobs
    from app.nocase.pipeline import run
    from fixtures import load_mapping, make

    def go(name: str, src_dir: Path | None = None):
        src = make(name, src_dir or tmp_path / "src")
        result = run(src, load_mapping(name))
        job = result["job"]
        return job, jobs.job_folder(job["job_id"]), result
    return go


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
