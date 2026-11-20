"""Application names in the web UI: asked only when the chosen column has masked values; saved names are applied
at conversion, so the event log, map and AI only ever see the real names."""
import hashlib

import pandas as pd
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _file(tmp_path, masked: bool):
    def app(v):
        return hashlib.md5(v.encode()).hexdigest() if masked and v != "Outlook" else v
    rows = []
    for c in range(6):
        for i, (act, a) in enumerate([("Open ticket", "ServiceNow"), ("Email new joiner", "Outlook"),
                                       ("Create worker", "Workday")]):
            rows.append({"Ticket": f"T{c}", "Activity": act, "Time": f"2026-01-0{c + 1} 09:0{i}:00",
                         "User": f"u{c % 2}", "Application": app(a)})
    p = tmp_path / ("masked.csv" if masked else "plain.csv")
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


def test_masked_application_names_are_asked_saved_and_applied(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.api.server import app
    from app.core import jobs
    c = TestClient(app)
    plain = c.post("/api/jobs/from-path", json={"path": str(_file(tmp_path, False)), "dataset_class": "public",
                                                "new_process": "Plain"}).json()["job"]["job_id"]
    assert c.get(f"/api/jobs/{plain}/apps", params={"column": "Application"}).json()["masked"] == []

    jid = c.post("/api/jobs/from-path", json={"path": str(_file(tmp_path, True)), "dataset_class": "public",
                                              "new_process": "Masked"}).json()["job"]["job_id"]
    d = c.get(f"/api/jobs/{jid}/apps", params={"column": "Application", "title": "Activity", "user": "User"}).json()
    assert d["summary"]["masked"] == 2 and d["summary"]["mapped"] == 0 and d["summary"]["applications"] == 3
    hints = {r["raw_value"]: r["examples"] for r in d["masked"]}
    sn = hashlib.md5(b"ServiceNow").hexdigest()
    assert hints[sn] == "Open ticket" and "Outlook" not in hints        # readable values are not asked
    assert c.get(f"/api/jobs/{jid}/apps", params={"column": "nope"}).status_code == 400

    names = {sn: "ServiceNow", hashlib.md5(b"Workday").hexdigest(): "Workday"}
    r = c.put("/api/apps/map", json={"rows": [{"raw_value": k, "real_name": v, "category": "HR system"}
                                              for k, v in names.items()]}).json()
    assert r["added"] == 2
    assert c.get(f"/api/jobs/{jid}/apps", params={"column": "Application"}).json()["summary"]["mapped"] == 2
    assert "ServiceNow" in c.get("/api/apps/map.csv").text

    res = c.post(f"/api/jobs/{jid}/convert/case", json={"mapping": {"case_id": "Ticket", "activity": "Activity",
                 "timestamp": "Time", "resource": "User", "application": "Application"}})
    assert res.status_code == 200, res.text
    ev = pd.read_parquet(jobs.job_folder(jid) / "events.parquet")
    assert set(ev["application"]) == {"ServiceNow", "Outlook", "Workday"}

    # import: preview first, save only on confirm
    csv = "raw_value,real_name\n" + hashlib.md5(b"Teams").hexdigest() + ",Microsoft Teams\n"
    pv = c.post("/api/apps/map/import", files={"file": ("m.csv", csv.encode())}, data={"confirm": "false"}).json()
    assert pv["added"] == 1 and "saved" not in pv
    assert c.post("/api/apps/map/import", files={"file": ("m.csv", csv.encode())},
                  data={"confirm": "true"}).json()["saved"]["total"] == 3
