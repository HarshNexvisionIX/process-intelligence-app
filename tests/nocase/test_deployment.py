"""Phase N6: deployment overlays, S3 state transfer and container guardrails."""
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

from app.core import config
from app.core.config import PROJECT_ROOT


def test_no_profile_settings_are_an_exact_copy_of_the_yaml(monkeypatch):
    monkeypatch.delenv("PM_PROFILE", raising=False)
    config.settings.cache_clear()
    try:
        expected = yaml.safe_load((PROJECT_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
        assert config.settings() == expected
    finally:
        config.settings.cache_clear()


def test_local_profile_works_and_aws_profile_routes_only_private_ai(monkeypatch):
    monkeypatch.setenv("PM_PROFILE", "local")
    monkeypatch.setenv("DATA_DIR", "data")
    monkeypatch.setenv("ALLOW_EXTERNAL_AI", "true")
    config.settings.cache_clear()
    try:
        assert config.settings()["deployment"]["profile"] == "local"
        monkeypatch.setenv("PM_PROFILE", "aws")
        config.settings.cache_clear()
        aws = config.settings()
        assert aws["storage"]["data_dir"] == "/data"
        assert aws["ai"]["allow_external_for_client"] is False
        assert all(route == ["vllm_private"] for route in aws["ai"]["routes"].values())
        assert __import__("os").environ["ALLOW_EXTERNAL_AI"] == "false"
    finally:
        config.settings.cache_clear()


def test_aws_profile_refuses_a_non_private_route():
    base = yaml.safe_load((PROJECT_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    base["ai"]["routes"]["summary"] = ["groq"]
    with pytest.raises(ValueError, match="non-private"):
        config.validate_profile("aws", base)


@mock_aws
def test_s3_push_pull_round_trip_excludes_raw_by_default(tmp_path):
    from scripts.s3_sync import pull, push
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="process-state")
    source = tmp_path / "source"
    (source / "jobs" / "job-1" / "raw").mkdir(parents=True)
    (source / "library" / "process").mkdir(parents=True)
    (source / "mapping_profiles").mkdir(parents=True)
    (source / "jobs" / "job-1" / "job.json").write_text('{"status":"mined"}', encoding="utf-8")
    (source / "jobs" / "job-1" / "raw" / "upload.csv").write_text("secret source", encoding="utf-8")
    (source / "library" / "process" / "rules.yaml").write_text("rules: []", encoding="utf-8")
    (source / "mapping_profiles" / "layout.json").write_text("{}", encoding="utf-8")
    (source / "app_identity_map.csv").write_text("raw_value,real_name\n", encoding="utf-8")

    result = push("process-state", "tenant/a", source, s3=client)
    assert result["files"] == 4
    keys = [obj["Key"] for obj in client.list_objects_v2(Bucket="process-state", Prefix="tenant/a")["Contents"]]
    assert all("raw/upload.csv" not in key for key in keys)

    restored = tmp_path / "restored"
    restored_result = pull("process-state", "tenant/a", restored, s3=client)
    assert restored_result["files"] == 4
    assert (restored / "jobs" / "job-1" / "job.json").read_text() == '{"status":"mined"}'
    assert (restored / "library" / "process" / "rules.yaml").exists()
    assert (restored / "mapping_profiles" / "layout.json").exists()
    assert not (restored / "jobs" / "job-1" / "raw" / "upload.csv").exists()

    push("process-state", "tenant/a", source, include_raw=True, s3=client)
    pull("process-state", "tenant/a", restored, include_raw=True, s3=client)
    assert (restored / "jobs" / "job-1" / "raw" / "upload.csv").exists()
    from scripts.s3_sync import _destination
    with pytest.raises(ValueError, match="outside the data folder"):
        _destination("jobs/../../outside.txt", "", restored)


def test_compose_and_dockerignore_are_private_and_exclude_local_data():
    root = PROJECT_ROOT
    local = yaml.safe_load((root / "docker" / "compose.local.yaml").read_text(encoding="utf-8"))
    aws = yaml.safe_load((root / "docker" / "compose.aws.yaml").read_text(encoding="utf-8"))
    assert local["services"]["app"]["ports"] == ["127.0.0.1:8501:8501"]
    assert local["services"]["ollama"]["profiles"] == ["local-llm"]
    assert aws["services"]["app"]["ports"] == ["127.0.0.1:8501:8501"]
    assert "ports" not in aws["services"]["vllm"]
    for compose in (local, aws):
        for service in compose["services"].values():
            for port in service.get("ports", []):
                assert str(port).startswith("127.0.0.1:")
    dockerfile = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "USER appuser" in dockerfile and '"0.0.0.0"' in dockerfile
    ignore = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore and "data/" in ignore and ".venv/" in ignore and "tests/" in ignore
    assert "0.0.0.0:" not in "\n".join(ignore)


def test_completed_job_auto_backup_is_opt_in(tmp_path, monkeypatch):
    from app.core import jobs
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(jobs, "settings", lambda: {"storage": {"s3_backup": True}})
    calls = []
    monkeypatch.setattr("scripts.s3_sync.maybe_push", lambda path: calls.append(path) or {"files": 1})
    job = jobs.create_job("tiny.csv")
    assert calls == []
    job["status"] = "mined"
    jobs.save_job(job)
    assert len(calls) == 1 and job.get("s3_backup_at")
