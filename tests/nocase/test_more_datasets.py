"""Fixes found by running more no-case datasets (a desktop window tracker, the vendor-master layout)."""
import pandas as pd

from app.core.config import PROJECT_ROOT  # noqa: F401


def _tracker(tmp_path):
    rows = []
    t = pd.Timestamp("2026-03-02 08:00")
    titles = ["Inbox - Outlook", "Budget 2026.xlsx - Excel", "Team meeting notes - Word", "Search - Google Chrome"]
    for i in range(400):
        start = t + pd.Timedelta(minutes=3 * i)
        rows.append({"App": titles[i % 4].split(" - ")[-1], "Title": titles[i % 4],
                     "Begin": start.strftime("%d-%m-%Y %H:%M"), "End": (start + pd.Timedelta(minutes=2)).strftime("%d-%m-%Y %H:%M")})
    p = tmp_path / "awt.csv"
    pd.DataFrame(rows).to_csv(p, sep=";", index=False)
    return p


def test_window_tracker_without_clicks_or_active_time(tmp_path, monkeypatch):
    """No click column: nothing is a 'generic click'; no active time: end - start is the time basis."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    from app.core import jobs
    from app.nocase.canonical import NcMapping
    from app.nocase.engine import measures
    from app.nocase.pipeline import run
    m = NcMapping(columns={"ts": "Begin", "ts_end": "End", "app": "App", "text": "Title"},
                  ts_format="%d-%m-%Y %H:%M", dataset_class="public")
    r = run(_tracker(tmp_path), m)
    folder = jobs.job_folder(r["job"]["job_id"])
    ev = pd.read_parquet(folder / "nc_events_clean.parquet")
    assert not ev["is_generic"].any()
    scope = r["scope"] if "scope" in r else __import__("json").loads((folder / "nc_scope.json").read_text())
    assert abs(scope["active_hours_in_scope"] - 400 * 2 / 60) < 1e-6          # 400 events x 2 minutes
    mm = measures.build(folder, None)
    assert mm["basis"]["time"] == "start-end" and abs(mm["overall"]["hours"] - 400 * 2 / 60) < 1e-6


def test_a_window_title_column_is_never_a_case(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import duckdb
    from app.intake.mode_detect import detect
    rows = []
    for d in range(20):
        for k in range(30):
            rows.append({"timestamp": f"2026-08-{d + 1:02d} 09:{k:02d}:00", "user_key": f"u{k % 5}",
                         "app_name": "SAP GUI", "window": f"Vendor request {d}{k % 3} - ServiceNow",
                         "action": "Click Save"})
    p = tmp_path / "w.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    con = duckdb.connect()
    con.execute(f"CREATE VIEW src AS SELECT * FROM read_csv_auto('{p.as_posix()}')")
    d = detect(con)
    win = next(c for c in d["candidates"] if c["column"] == "window")
    assert not win["passed"] and "free text" in win["fail_reason"]


def test_column_suggestions_prefer_clear_names(tmp_path):
    import duckdb
    from app.core.ingest import profile
    from app.ui.nocase_mapping import suggestions
    pd.DataFrame({"row_no": range(50), "event_key": [f"e{i}" for i in range(50)],
                  "timestamp": pd.date_range("2026-08-03", periods=50, freq="min").astype(str),
                  "user_key": ["usr_a", "usr_b"] * 25, "app_name": ["SAP GUI", "Outlook"] * 25,
                  "exe": ["saplogon.exe", "OUTLOOK.EXE"] * 25, "window": ["XK01 Create Vendor"] * 50,
                  "action": ["Click Save"] * 50, "duration_s": [3.5] * 50,
                  "machine": ["WS-1", "WS-2"] * 25}).to_csv(tmp_path / "v.csv", index=False)
    con = duckdb.connect()
    con.execute(f"CREATE VIEW src AS SELECT * FROM read_csv_auto('{(tmp_path / 'v.csv').as_posix()}')")
    s = suggestions(con, profile(con))
    assert s["ts"] == "timestamp" and s["actor"] == "user_key" and s["app"] == "app_name"
    assert s["text"] == "window" and s["action"] == "action" and s["active_s"] == "duration_s"
