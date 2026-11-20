"""Inspect release harness artifacts, capture fake AI payloads, and smoke-test pages."""
from __future__ import annotations
import ast
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys

from release_check import ROOT, isolated, read, write


def consistency(work):
    import yaml
    code = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8")
            for base in ("app", "scripts") for p in (ROOT / base).rglob("*.py")
            if not p.name.startswith("release_")}
    literals = set()
    for source in code.values():
        for n in ast.walk(ast.parse(source)):
            if isinstance(n, ast.Constant) and isinstance(n.value, str): literals.add(n.value)
    cfg = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8"))
    leaves = []
    def walk(d, path=""):
        for k, v in d.items():
            key = f"{path}.{k}".lstrip(".")
            if isinstance(v, dict): walk(v, key)
            else: leaves.append((key, k))
    walk(cfg)
    undocumented = []
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for key in re.findall(r"^([A-Z][A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M):
        if key not in readme: undocumented.append(key)
    installed = {d.metadata["Name"].lower().replace("_", "-"): d.version for d in importlib.metadata.distributions()}
    original = {d.metadata["Name"].lower().replace("_", "-"): d.version for d in
                importlib.metadata.distributions(path=[str(ROOT / ".venv/Lib/site-packages")]) if d.metadata["Name"]}
    comparisons = {}
    for name in ("requirements.txt", "requirements.lock.txt"):
        pins = re.findall(r"^([\w-]+)==([^\s#]+)", (ROOT / name).read_text(), re.M)
        comparisons[name] = {"pins": len(pins), "release_mismatches": [], "original_venv_mismatches": []}
        for package, version in pins:
            norm = package.lower().replace("_", "-")
            for label, inventory in (("release_mismatches", installed), ("original_venv_mismatches", original)):
                if inventory.get(norm) != version:
                    comparisons[name][label].append([package, version, inventory.get(norm)])
    write(work / "consistency.json", {"unreferenced_setting_leaf_candidates": [p for p, k in leaves if k not in literals],
          "env_undocumented": undocumented, "dependencies": comparisons, "python": sys.version})


def smoke_and_fake(work):
    from streamlit.testing.v1 import AppTest
    from app.core import jobs, definition as dmod
    from app.core.evidence import run_check
    from app.core.report import generate_report
    from app.core.definition_ai import draft_from_document
    from scripts.generate_synthetic import VARIANTS
    sys.path.insert(0, str(ROOT / "tests"))
    from test_narrative import FakeClient
    clean = read(work / "clean.result.json")
    os.environ["DATA_DIR"] = str(work / "clean")
    job = jobs.load_job(clean["job"]["job_id"])
    d = dmod.ProcessDefinition.from_dict(read(work / "inputs/definition.json"))
    d = dmod.new_draft(d, "Release reviewer", "Synthetic smoke test")
    run_check(job, d)
    outcomes = []
    for page in ("upload_page", "quality_page", "map_page", "business_page", "report_page", "jobs_page"):
        at = AppTest.from_file(str(ROOT / "app/main.py"), default_timeout=120)
        at.run().switch_page(f"ui/{page}.py").run()
        outcomes.append({"page": page, "errors": [e.message for e in at.exception],
                         "tabs": [t.label for t in at.tabs]})
        if page == "business_page" and not at.exception:
            next(x for x in at.multiselect if x.label == "Verdicts").set_value([]).run()
            outcomes.append({"action": "empty evidence filter", "errors": [e.message for e in at.exception]})
    def reply(messages):
        ids = re.findall(r"^(F\d{3}) \|", messages[-1]["content"], re.M)
        return json.dumps({"summary": [{"text": "The analysis covers {{" + ids[0] + "}}.", "facts": [ids[0]]}],
                           "findings": [], "recommendations": []})
    client = FakeClient(reply)
    generate_report(job["job_id"], client=client)
    (work / "clean.ai_payload.txt").write_text(client.sent_text(), encoding="utf-8")
    acts = VARIANTS[0][1]
    draft_client = FakeClient(json.dumps({"name": "Process", "stages": [{"id": "ST1", "name": "Process"}],
        "steps": [{"id": "S1", "name": acts[0], "stage_id": "ST1", "position": 1,
                   "mapped_activities": [acts[0]], "source_quote": "Create Request"}], "flows": []}))
    result = draft_from_document(job["job_id"], "Create Request", "release.txt", acts, client=draft_client)
    (work / "definition.ai_payload.txt").write_text(draft_client.sent_text(), encoding="utf-8")
    # v1.1: a knowledge source with statements, combined confidence in the report
    from app.core import knowledge
    from app.core.combined_confidence import load as load_combined
    sop = (f"{acts[0]} comes before {acts[1]}. {acts[1]} is mandatory for every request. "
           f"{acts[-1]} is slow because approvals queue up.")
    src, _ = knowledge.add_source(d.name, "sop", "release_sop.txt", sop.encode(), title="Release SOP")
    for st in ({"type": "order", "subject": acts[0], "object": acts[1], "quote": f"{acts[0]} comes before {acts[1]}",
                "anchor": "p.1"},
               {"type": "mandatory", "subject": acts[1], "quote": f"{acts[1]} is mandatory for every request",
                "anchor": "p.1"},
               {"type": "pain_point", "subject": acts[-1], "value": "approvals queue up",
                "quote": f"{acts[-1]} is slow because approvals queue up", "anchor": "p.1"}):
        knowledge.add_manual_statement(d.name, src["id"], st, d)
    knowledge_client = FakeClient(reply)
    paths = generate_report(job["job_id"], client=knowledge_client)
    html = paths["html"].read_text(encoding="utf-8")
    (work / "knowledge.ai_payload.txt").write_text(knowledge_client.sent_text(), encoding="utf-8")
    combined = load_combined(jobs.job_folder(job["job_id"])) or {}
    knowledge_result = {"map_confidence_in_report": "Map confidence" in html,
                        "documented_gap_list": "Documented ≠ actual" in html,
                        "undocumented_list": "Undocumented practice" in html,
                        "summary": combined.get("summary"),
                        "quotes_in_ai_payload": any(q in knowledge_client.sent_text()
                                                    for q in ("mandatory for every request", "approvals queue up"))}
    at = AppTest.from_file(str(ROOT / "app/main.py"), default_timeout=120)
    at.run().switch_page("ui/business_page.py").run()
    knowledge_result["business_page_errors"] = [e.message for e in at.exception]
    knowledge_result["business_page_tabs"] = [t.label for t in at.tabs]
    os.environ["DATA_DIR"] = str(work / "tiny")
    tiny = read(work / "tiny.result.json")
    never = FakeClient(reply)
    generate_report(tiny["job"]["job_id"], client=never)
    write(work / "smoke.json", {"pages": outcomes, "clean_ai_calls": len(client.calls),
          "draft_ai_calls": len(draft_client.calls), "draft_accepted": result.definition is not None,
          "tiny_ai_calls": len(never.calls), "knowledge": knowledge_result})


def leaks(work):
    import duckdb
    from openpyxl import load_workbook
    patterns = {"hex32": r"\b[0-9a-fA-F]{32,}\b", "email": r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
                "input_case_id": r"\bC[0-9]{8}\b", "input_ticket": r"\bINC[0-9]{6,}\b",
                "window_title": r"Approval_tracker_|Payments_Q[1-4]\.xlsx|SAP Easy Access - Order|Inbox - user",
                "person_name": r"\b(?:Asha Rao|John Smith|Maria Garcia|Louise Jones|Priya Sharma)\b",
                "raw_action_token": r"#9f3c2a1b#",
                "remote_script": r"<script[^>]+src=[\"']?https?://"}
    hits, scanned = [], []
    def scan(text, path, location):
        for category, pattern in patterns.items():
            matches = list(re.finditer(pattern, str(text), re.I if category == "remote_script" else 0))
            if matches:
                hits.append({"file": str(path), "location": location, "category": category,
                             "count": len(matches), "examples": [m.group()[:100] for m in matches[:3]]})
    def strings(value, path="$", out=None):
        if isinstance(value, dict):
            for k, v in value.items(): yield from strings(v, path + "." + str(k))
        elif isinstance(value, list):
            for i, v in enumerate(value): yield from strings(v, f"{path}[{i}]")
        elif isinstance(value, str): yield path, value
    for scenario in read(work / "scenarios.json"):
        folder = Path(scenario["data_dir"])
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix not in (".json", ".parquet", ".html", ".xlsx"): continue
            scanned.append(str(path))
            if path.suffix == ".json":
                for loc, value in strings(read(path)): scan(value, path, loc)
            elif path.suffix == ".html": scan(path.read_text(encoding="utf-8"), path, "HTML/script")
            elif path.suffix == ".xlsx":
                wb = load_workbook(path, read_only=True, data_only=False)
                for sheet in wb:
                    for row in sheet:
                        for cell in row:
                            if isinstance(cell.value, str): scan(cell.value, path, f"{sheet.title}!{cell.coordinate}")
                wb.close()
            else:
                con = duckdb.connect()
                cols = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
                for col, typ, *_ in cols:
                    if typ != "VARCHAR": continue
                    quoted = '"' + col.replace('"', '""') + '"'
                    for category, pattern in patterns.items():
                        if category == "remote_script": continue
                        count = con.execute(f"SELECT count(*) FROM read_parquet(?) WHERE regexp_matches({quoted}, ?)",
                                            [str(path), pattern]).fetchone()[0]
                        if count:
                            examples = con.execute(f"SELECT DISTINCT {quoted} FROM read_parquet(?) "
                                f"WHERE regexp_matches({quoted}, ?) LIMIT 3", [str(path), pattern]).fetchall()
                            hits.append({"file": str(path), "location": f"column {col}", "category": category,
                                         "count": count, "examples": [v[0] for v in examples]})
                con.close()
    for path in list(work.glob("*.console.log")) + list(work.glob("*.ai_payload.txt")):
        scanned.append(str(path))
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1): scan(line, path, f"line {i}")
    write(work / "leaks.json", {"files_scanned": scanned, "hits": hits})


if __name__ == "__main__":
    work = Path(sys.argv[1]).resolve()
    isolated(work)
    consistency(work)
    if "--no-smoke" not in sys.argv: smoke_and_fake(work)
    leaks(work)
