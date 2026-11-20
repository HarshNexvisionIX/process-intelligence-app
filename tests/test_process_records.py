"""One process ties everything together: chosen at import, unique by name, and the Process library shows (and
bundles) every analysis of it with its results, AI requests and evidence. Event tables are never exposed."""
import io
import json
import zipfile

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _csv():
    return ("ts,user,app,window\n2026-01-05 09:00:00,a,Excel,Model\n2026-01-05 09:01:00,a,SAP,FB50\n").encode()


def test_process_is_chosen_at_import_and_names_are_unique(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.api.server import app
    c = TestClient(app)
    r = c.post("/api/jobs", files=[("files", ("a.csv", _csv()))],
               data={"dataset_class": "public", "new_process": "ICC Month-end Recharge"})
    assert r.status_code == 200, r.text
    first = r.json()["job"]
    assert first["process"] == "icc_month_end_recharge" and first["process_name"] == "ICC Month-end Recharge"
    # the same name again (other case / punctuation) is refused before anything is created
    n_jobs = len(c.get("/api/jobs").json())
    r = c.post("/api/jobs", files=[("files", ("b.csv", _csv()))],
               data={"dataset_class": "public", "new_process": "icc month-end  recharge!"})
    assert r.status_code == 400 and "already exists" in r.json()["detail"]
    assert len(c.get("/api/jobs").json()) == n_jobs
    # an existing process is chosen by key; an unknown key is refused
    r = c.post("/api/jobs", files=[("files", ("b.csv", _csv()))],
               data={"dataset_class": "public", "process": "icc_month_end_recharge"})
    assert r.status_code == 200 and r.json()["job"]["process"] == "icc_month_end_recharge"
    assert c.post("/api/jobs", files=[("files", ("c.csv", _csv()))],
                  data={"dataset_class": "public", "process": "nope"}).status_code == 400
    # Business context cannot create a duplicate either
    jid = r.json()["job"]["job_id"]
    assert c.post(f"/api/jobs/{jid}/process", json={"new_name": "ICC month end recharge"}).status_code == 400
    assert c.post(f"/api/jobs/{jid}/process", json={"new_name": "AP invoices"}).json()["process"] == "ap_invoices"
    assert {p["key"] for p in c.get("/api/processes").json()} == {"icc_month_end_recharge", "ap_invoices"}


def test_process_records_list_and_bundle_everything_but_event_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.api.server import app
    from app.core import jobs
    c = TestClient(app)
    job = c.post("/api/jobs", files=[("files", ("a.csv", _csv()))],
                 data={"dataset_class": "public", "new_process": "HR onboarding"}).json()["job"]
    folder = jobs.job_folder(job["job_id"])
    (folder / "readout.json").write_text(json.dumps({"report": {"headline": "h"}}), encoding="utf-8")
    (folder / "nc_report.html").write_text("<html>report</html>", encoding="utf-8")
    (folder / "ai_packages.json").write_text(json.dumps({"readout": {"packages": [{"task": "nc_readout"}]}}),
                                             encoding="utf-8")
    (folder / "ai_audit.jsonl").write_text('{"task": "nc_readout"}\n', encoding="utf-8")
    (folder / "nc_events.parquet").write_bytes(b"PAR1")
    other = c.post("/api/jobs", files=[("files", ("z.csv", _csv()))],
                   data={"dataset_class": "public", "new_process": "Other process"}).json()["job"]

    d = c.get("/api/processes/hr_onboarding/records").json()
    assert [a["job_id"] for a in d["analyses"]] == [job["job_id"]]           # only this process's analyses
    a = d["analyses"][0]
    names = {f["name"]: f["category"] for f in a["files"]}
    assert names["readout.json"] == "AI answers" and names["ai_packages.json"] == "AI requests (JSON sent)"
    assert names["nc_report.html"] == "Reports" and "nc_events.parquet" not in names and "job.json" not in names
    assert a["ai"] == {"prepared": 1, "approved": 0, "sent": 1}
    assert any(x["url"].endswith("/readout.pdf") for x in a["exports"])
    assert any(f["path"] == "pack.yaml" for f in d["library"])

    # downloads: results yes, event tables, job.json and paths outside the folder no
    jid = job["job_id"]
    assert c.get(f"/api/jobs/{jid}/files/readout.json").json()["report"]["headline"] == "h"
    for bad in ("nc_events.parquet", "job.json", "..%2Fjob.json"):
        assert c.get(f"/api/jobs/{jid}/files/{bad}").status_code == 404
    assert c.get("/api/processes/hr_onboarding/library-file", params={"path": "pack.yaml"}).status_code == 200
    assert c.get("/api/processes/hr_onboarding/library-file", params={"path": "../icc/pack.yaml"}).status_code == 404

    z = zipfile.ZipFile(io.BytesIO(c.get("/api/processes/hr_onboarding/bundle.zip").content))
    names = set(z.namelist())
    assert "index.json" in names and "process_library/pack.yaml" in names
    assert f"analyses/{jid}/readout.json" in names and f"analyses/{jid}/ai_audit.jsonl" in names
    assert not any(n.endswith(".parquet") or "/raw/" in n for n in names)
    assert not any(other["job_id"] in n for n in names)
    assert json.loads(z.read("index.json"))["analyses"][0]["job_id"] == jid
