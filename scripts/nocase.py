"""No case ID mode ("clickstream mapping") from the command line. run_pipeline.py (case-ID mode) is untouched.

Commands:
    ingest   read the file(s) with a mapping YAML -> nc_events.parquet, nc_scope.json (a new job, or --job)
    profile  measure what the data supports -> nc_capabilities.json            (--job)
    clean    clean and mask screen / action -> nc_events_clean.parquet          (--job)
    units    one row per (app, screen, action) -> nc_units.parquet              (--job)
    episodes work episodes (sittings) -> nc_episodes.parquet                    (--job)
    caselink is there a real case ID? -> nc_case_links.parquet + decision       (--job)
    assign   a business step for every click -> nc_assignments.parquet          (--job [--process])
    evaluate score against the truth columns -> nc_eval.json                    (--job)
    evidence which signals can tell the step, scored by code -> nc_evidence_*.json  (--job [--process])
    activities  Level 4/5 activities inside each step -> nc_activities.parquet  (--job [--process])
    pack-propose  a process pack from <library>/<process>/docs -> pack.proposed.yaml (--process)
    ai-check providers: private or not, key present, one tiny test call
    report   measures, map, confidence, inside each step, then nc_report.html / .xlsx with self-checks (--job)
    calibrate  evidence weights from the truth columns -> nc_evidence_weights.yaml (--job, evaluation only)
    run      ingest + profile + clean + units + episodes + caselink + evidence + assign + activities
             + measures + flow + confidence + inside each step

Examples (from the project root):
    python scripts/nocase.py run --input "D:/exports/events.parquet" --mapping config/nocase_mappings/hr_skan_nocase.yaml
    python scripts/nocase.py profile --job 20261113-101500-ab12cd
    python scripts/nocase.py run --input ... --mapping ... --process hr_onboarding --strategy episodes=ActorAppContext
    python scripts/nocase.py evaluate --job 20261113-101500-ab12cd

Every decision (which strategy, what was tried, why) is saved in the job's nc_decisions.json.

Exit codes: 0 success, 2 the input or mapping is not usable, 1 an unexpected error.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["ingest", "profile", "clean", "units", "episodes", "caselink", "assign",
                                        "evaluate", "evidence", "activities", "pack-propose", "ai-check", "report",
                                        "calibrate", "run"])
    p.add_argument("--input", help="a file, a folder or a glob pattern (quote it); .jsonl and .xes too")
    p.add_argument("--mapping", help="the no-case mapping (YAML: columns, dataset_class, keep_rows_where, eval ...)")
    p.add_argument("--job", help="an existing job ID (default for ingest / run: a new job)")
    p.add_argument("--data-dir", help="data folder for this run (jobs are written under <data-dir>/jobs)")
    p.add_argument("--strategy", action="append", default=[], metavar="STAGE=NAME",
                   help="force a strategy, e.g. episodes=ActorAppContext (repeatable)")
    p.add_argument("--process", help="the process pack to use (<data dir>/library/<process>/)")
    p.add_argument("--records", help="a system-of-record export (CSV) for the RecordJoin case link")
    p.add_argument("--records-map", default="",
                   help="its columns: id=...,actor=...,start=...,end=... (default record_id, user, start_ts, end_ts)")
    p.add_argument("--target-process", help="evaluate: the true process to separate from other work")
    p.add_argument("--ai", choices=["online", "off"], default="online",
                   help="online: the configured providers (client data: private ones only); off: rules only")
    return p.parse_args(argv)


def _num(x, digits: int = 1) -> str:
    return "–" if x is None else f"{x:,.{digits}f}"


def print_scope(scope: dict) -> None:
    print(f"Rows in file:           {scope['rows_in_file']:,}")
    print(f"Left out by the filter: {scope['left_out_by_filter']:,}")
    print(f"Unreadable time:        {scope['unreadable_time']:,}")
    print(f"In scope:               {scope['in_scope']:,}")
    print(f"Active hours:           {_num(scope['active_hours_in_scope'])}")
    print(f"Actors: {scope['actors']:,} · apps: {scope['apps']:,} · dataset class: {scope['dataset_class']}")


def print_caps(caps: dict) -> None:
    for lvl, f in caps["labels"]["fill"].items():
        if f["rows"]:
            print(f"{lvl} fill:                {100 * f['share']:.1f}% ({f['rows']:,} rows)")
    shapes = caps["id_shapes"]["overall"]
    if shapes:
        print("ID shapes (no values kept):")
        for s in shapes[:5]:
            print(f"  {s['shape']:<16} {s['events']:>10,} events ({100 * s['share']:.1f}%), {s['distinct']:,} distinct")
    if caps.get("case_id"):
        c = caps["case_id"]
        print(f"case_id: {c['verdict']} ({'; '.join(c['reasons'])})")
    for c in caps.get("counters") or []:
        print(f"counter: {c['column']}: {c['reason']}")
    print("What this data supports:")
    for name, s in caps["supports"].items():
        print(f"  {'✅' if s['possible'] else '❌'} {name}: {s['reason']}")


def run(args) -> int:
    from app.core import jobs
    from app.core.privacy import set_dataset_class, status_line
    from app.nocase import clean, profile, units
    from app.nocase.canonical import NcMapping
    from app.nocase.ingest import ingest, load_scope

    print(status_line())
    if args.command == "ai-check":
        return ai_check()
    if args.command == "pack-propose":
        if not args.process:
            print("pack-propose needs --process.", file=sys.stderr)
            return 2
        from app.nocase.ai.pack_from_docs import propose
        from app.nocase.ai_gateway import gateway
        r = propose(args.process, gateway("client", args.ai))
        print(f"Proposed pack written to {r['path']} ({r.get('stages')} stages, {r.get('steps')} steps from "
              f"{r['chunks']} part(s))" if r["path"] else f"No pack proposed: {r['reason']}")
        return 0
    if args.command in ("ingest", "run"):
        if not args.input or not args.mapping:
            print("ingest and run need --input and --mapping.", file=sys.stderr)
            return 2
        mp = Path(args.mapping)
        if not mp.exists():
            print(f"Mapping not found: {mp}", file=sys.stderr)
            return 2
        mapping = NcMapping.from_yaml(mp)
        problems = mapping.problems()
        if problems:
            print("The mapping is not usable:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 2
        job = jobs.load_job(args.job) if args.job else None
        try:
            job, scope = ingest(args.input, mapping, job=job)
        except ValueError as err:
            print(f"Cannot continue: {err}", file=sys.stderr)
            return 2
        print(f"Job {job['job_id']} (no case ID, {job['dataset_class']} data)")
        print_scope(scope)
        if args.command == "ingest":
            return 0
    else:
        if not args.job:
            print(f"{args.command} needs --job.", file=sys.stderr)
            return 2
        job = jobs.load_job(args.job)
        if job.get("mode") != "nocase":
            print(f"Job {args.job} is not a no-case job.", file=sys.stderr)
            return 2
        set_dataset_class(job.get("dataset_class"))
    folder = jobs.job_folder(job["job_id"])
    if args.command in ("profile", "run"):
        caps = profile.build(folder)
        if args.command == "profile":
            print_scope(load_scope(folder) or {})
    if args.command in ("clean", "run"):
        c = clean.build(folder)
        print(f"Cleaned {c['rows']:,} rows: {c['screens_carried_forward']:,} screens carried forward, "
              f"{100 * c['generic_share']:.1f}% generic clicks")
    if args.command in ("units", "run"):
        u = units.build(folder)
        cons = "–" if u["label_consistency"] is None else f"{100 * u['label_consistency']:.1f}%"
        print(f"Units: {u['units']:,} (events {u['events']:,}); l3 consistency per unit {cons}")
    if args.command in ("profile", "run"):
        print_caps(profile.load(folder) or caps)
    from app.nocase.ai_gateway import gateway
    ai = gateway(job.get("dataset_class"), args.ai)
    if args.command in ("run",) and ai.why_not("nc_assign_rules"):
        print(f"AI layers: rules only ({ai.why_not('nc_assign_rules')})")
    n2 = {"episodes": ("episodes",), "caselink": ("case_link",), "assign": ("assign",),
          "run": ("episodes", "case_link", "evidence", "assign", "activities")}.get(args.command)
    if args.command in ("evidence", "activities"):
        n2 = (args.command,)
    if n2:
        from app.nocase.stages import run_activities, run_evidence, run_n2
        forced = dict(x.split("=", 1) for x in args.strategy)
        records = None
        if args.records:
            cols = dict(x.split("=", 1) for x in args.records_map.split(",") if "=" in x)
            records = {"file": str(Path(args.records).resolve()), "columns": cols}
        try:
            done = run_n2(folder, forced, args.process, records, only=tuple(x for x in n2 if x in
                                                                          ("episodes", "case_link")), ai=ai)
            if "evidence" in n2:
                done["evidence"] = {"status": "ran", "output": run_evidence(folder, args.process, ai)}
            if "assign" in n2:
                done.update(run_n2(folder, forced, args.process, records, only=("assign",), ai=ai))
            if "activities" in n2:
                done["activities"] = {"status": "ran", "output": run_activities(folder, args.process, ai)}
        except ValueError as err:
            print(f"Cannot continue: {err}", file=sys.stderr)
            return 2
        for stage, info in done.items():
            print(info["output"]["message"] if info["status"] == "ran" else f"{stage}: skipped ({info['reason']})")
        if done.get("assign", {}).get("status") == "ran":
            import json as _json
            q = _json.loads((folder / "nc_assign_quality.json").read_text(encoding="utf-8"))
            for src, v in q["by_source"].items():
                hrs = "–" if v["hours"] is None else f"{v['hours']:,.1f} h"
                print(f"  {src:<20} {v['events']:>10,} events ({100 * v['share']:.1f}%), {hrs}")
            rows = [r for r in q["hours_by_step"] if r["source"] == "source_label"]
            if rows:
                print("  Hours by step (source_label events only):")
                for r in rows:
                    hrs = "–" if r["hours"] is None else f"{r['hours']:,.1f}"
                    print(f"    {r['step']:<45} {hrs}")
    if args.process:
        job["process"] = args.process
    if args.command in ("run", "report"):
        from app.nocase.stages import run_n4
        for stage, info in run_n4(folder, args.process or job.get("process")).items():
            print(info["output"]["message"] if info["status"] == "ran" else f"{stage}: skipped ({info['reason']})")
            for line in (info.get("output") or {}).get("lines") or []:
                print(f"  {line}")
    if args.command == "report":
        from app.nocase.report.report import build as build_report
        r = build_report(folder, args.process or job.get("process"), ai)
        print(f"{r['title']}: {r['checks']['passed']} checks passed, {r['checks']['failed']} failed "
              f"(summary: {r['summary_source']})")
        for c in r["checks"]["checks"]:
            if not c["passed"]:
                print(f"  FAILED {c['check']}: {c['detail']}")
        print(f"Report (HTML):  {r['html']}\nReport (Excel): {r['excel']}")
    if args.command == "calibrate":
        from app.nocase.eval.calibrate import calibrate
        try:
            w = calibrate(folder, args.process or job.get("process"))
        except ValueError as err:
            print(f"Cannot calibrate: {err}", file=sys.stderr)
            return 2
        print("Evidence weights (measured accuracy against the true step): "
              + ", ".join(f"{k} {v:.2f}" for k, v in sorted(w["weights"].items(), key=lambda kv: -kv[1])))
    if args.command == "evaluate":
        from app.nocase.eval.evaluate import evaluate, table
        print("Accuracy against the truth columns (evaluation only):")
        print(table(evaluate(folder, args.target_process)))
    job.update(status={"profile": "nc_profiled", "clean": "nc_cleaned", "units": "nc_units"}.get(
        args.command, "nc_units" if args.command in ("ingest",) else "nc_assigned" if args.command in ("assign", "run")
        else job.get("status")))
    jobs.save_job(job)
    return 0


def ai_check() -> int:
    """Every provider: private or not, key present, model, usable; one tiny test call for the usable ones."""
    from app.core import ai_client
    from app.core.narrative import ai_status
    ai_on, ai_why = ai_status()            # no test call unless the AI is switched on (AI_ENABLED)
    print("Providers (private = may receive CLIENT data):")
    for key, p in ai_client.providers().items():
        ok, why = ai_client.usable(p)
        mark = lambda b: "✓" if b else "✗"  # noqa: E731
        line = (f"  {key:<14} private {mark(p.get('private'))}  key {mark(ai_client.has_key(p))}  "
                f"model {p.get('model') or '–'}")
        if ok and not ai_on:
            line += f"  usable (no test call: {ai_why})"
        elif ok:
            try:
                r = ai_client.complete("nc_summary", [{"role": "user", "content": 'Reply with {"ok": true}'}],
                                       only=key, max_output_tokens=20)
                line += f"  test call ✓ ({r.latency_s:.1f} s)"
            except ai_client.AIUnavailable as err:
                line += f"  test call ✗ ({err.short()})"
        else:
            line += f"  not usable ({why})"
        print(line)
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.data_dir:
        os.environ["DATA_DIR"] = str(Path(args.data_dir).resolve())
    try:
        return run(args)
    except Exception as err:  # noqa: BLE001
        print(f"\nUnexpected error: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
