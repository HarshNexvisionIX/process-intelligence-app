"""The app shell: menu per mode, page files, unchanged url paths; renamed / removed files.

Run with:  pytest -q tests/test_pages.py
"""
import subprocess
import sys

from app.core.config import PROJECT_ROOT


def test_menu_per_mode_and_url_paths():
    from app.ui.shell import PAGES, WORKFLOWS
    assert [PAGES[k].title for _, k in WORKFLOWS["case"]] == [
        "Import data", "Business context", "Data quality", "Process analysis", "Process definition", "Report"]
    assert [PAGES[k].title for _, k in WORKFLOWS["nocase"]] == [
        "Import data", "Business context", "Overview and analysis", "Process map", "Review steps", "Report"]
    # url paths of existing pages are unchanged, so bookmarks keep working
    assert {k: PAGES[k].url for k in ("quality", "analysis", "definition", "report", "nc_overview", "nc_map",
                                       "nc_report", "nc_steps")} == {
        "quality": "quality", "analysis": "process-map", "definition": "business-process", "report": "report",
        "nc_overview": "clickstream", "nc_map": "clickstream-map", "nc_report": "clickstream-report",
        "nc_steps": "review-steps"}
    for p in PAGES.values():
        assert (PROJECT_ROOT / "app" / p.file).exists(), p.file
    assert not (PROJECT_ROOT / "app" / "ui" / "coming_soon.py").exists()


def test_xes_script_renamed_with_shim():
    assert (PROJECT_ROOT / "scripts" / "xes_to_parquet.py").exists()
    old = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "xes_to_paraquet.py")],
                         capture_output=True, text=True)
    assert old.returncode != 0 and "xes_to_parquet.py" in old.stderr
