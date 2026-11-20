"""Big prompt B, phase 0: episode strategy picked by a data-only score; pack order is documented, never confirmed.

Run with:  pytest -q tests/nocase/test_bigB_phase0.py
"""
import json

import yaml

from app.core import jobs
from app.core.config import PROJECT_ROOT, settings
from app.nocase import library
from app.nocase.canonical import NcMapping
from app.nocase.pipeline import run
from app.nocase.stages import run_n2
from fixtures import icc_like_paused, load_mapping, make


def test_default_order_puts_actor_app_context_before_actor_gap():
    order = settings()["nocase"]["strategies"]["episodes"]
    assert order == ["SourceSession", "ActorAppContext", "ActorGap", "DayWindow"]
    assert settings()["nocase"]["episodes"]["auto_pick"] is True


def test_auto_pick_scores_every_strategy_that_applies(tmp_path):
    r = run(icc_like_paused(tmp_path / "src"), load_mapping("icc_like"))
    folder = jobs.job_folder(r["job"]["job_id"])
    run_n2(folder)
    d = json.loads((folder / "nc_decisions.json").read_text(encoding="utf-8"))["episodes"]
    assert set(d["scores"]) == {"ActorAppContext", "ActorGap", "DayWindow"}
    best = max(d["scores"], key=lambda k: d["scores"][k]["score"])
    assert d["strategy"] == best and "data-only score" in d["tried"][-1]["reason"]
    for s in d["scores"].values():
        assert set(s) == {"score", "single_event_share", "median_minutes", "unit_concentration", "episodes"}


def test_actor_app_context_applies_with_an_app_and_no_screen(tmp_path):
    src = make("rpa_ui", tmp_path / "src")
    m = NcMapping(columns={"ts": "timestamp", "actor": "user", "app": "app", "action": "event"}, dataset_class="public")
    r = run(src, m)
    folder = jobs.job_folder(r["job"]["job_id"])
    run_n2(folder)
    d = json.loads((folder / "nc_decisions.json").read_text(encoding="utf-8"))["episodes"]
    assert any(t["strategy"] == "ActorAppContext" and t["applies"] for t in d["tried"])


def test_pack_order_is_documented_test_pack_and_never_confirmed():
    pack = yaml.safe_load((PROJECT_ROOT / "tests" / "sample_processes" / "nocase_packs" / "hr_onboarding" / "pack.yaml").read_text(encoding="utf-8"))
    p = library.provenance(pack)
    assert p == {"source": "test pack", "status": "documented", "confirmed": False,
                 "label": "Documented (test pack, not confirmed)"}
    assert library.provenance({"provenance": {"status": "observed"}})["status"] == "documented"
    assert library.provenance({})["status"] == "documented"
