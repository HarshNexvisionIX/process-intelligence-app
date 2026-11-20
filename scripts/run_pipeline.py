"""Run the whole app for one file or dataset, without the user interface.

Steps: create a job -> read the file(s) -> profile -> map columns -> validate ->
convert -> quality check -> analysis (limited by the quality band) -> report.

Examples (run from the project root):
    python scripts/run_pipeline.py --input data/samples/clean_small.parquet --auto-map
    python scripts/run_pipeline.py --input "C:/exports/daily" --auto-map --drop-missing-case \
        --merge-repeats --filter-column app --filter-values SAP Outlook

Explicit columns (--case, --activity, ...) override the automatic suggestions.
Columns that look like personal data are left out unless --keep-sensitive is given.

Exit codes: 0 success, 2 the input or mapping is not usable (every check is
printed), 1 an unexpected error (the message is printed).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ROLE_ARGS = {"case_id": "case", "activity": "activity", "timestamp": "timestamp",
             "end_timestamp": "end_timestamp", "resource": "resource",
             "application": "application", "window_title": "window_title", "stage": "stage", "action": "action",
             "level1": "level1", "level3": "level3", "step_run": "step_run", "active_time": "active_time",
             "wait_time": "wait_time", "tat_time": "tat_time"}


class InputProblem(Exception):
    """The input or the mapping cannot be used (exit code 2)."""


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="a file, a folder or a glob pattern (quote it)")
    p.add_argument("--auto-map", action="store_true", help="use the suggested column for every role not given")
    p.add_argument("--case", help="case ID column")
    p.add_argument("--activity", help="activity column")
    p.add_argument("--timestamp", help="timestamp (start) column")
    p.add_argument("--timestamp-format", help="e.g. '%%d/%%m/%%Y %%H:%%M:%%S' (default: detected)")
    p.add_argument("--end-timestamp", help="end timestamp column")
    p.add_argument("--resource", help="resource / user column")
    p.add_argument("--timezone", default="UTC", help="timezone of times without an offset (default UTC)")
    p.add_argument("--drop-missing-case", action="store_true", help="leave out rows without a case ID")
    p.add_argument("--drop-missing-activity", action="store_true", help="leave out rows without an activity")
    p.add_argument("--filter-column", help="keep only rows where this column ...")
    p.add_argument("--filter-values", nargs="+", default=[], help="... has one of these values")
    p.add_argument("--merge-repeats", action="store_true", help="merge runs of the same step in a case")
    p.add_argument("--exclude", nargs="+", default=[], help="columns to leave out of the converted data")
    p.add_argument("--keep-sensitive", action="store_true", help="do not leave out suspected personal data")
    p.add_argument("--data-dir", help="data folder for this run (jobs are written under <data-dir>/jobs)")
    p.add_argument("--no-ai", action="store_true", help="write the summary from fixed templates, never the AI")
    p.add_argument("--application", help="application column (masked values get names from the application map)")
    p.add_argument("--window-title", help="window title column (accepted for symmetry; never written)")
    p.add_argument("--app-map", help="CSV/Excel of application names, merged into the shared map for this run")
    p.add_argument("--save-app-map", action="store_true", help="also save the merged map (with --app-map)")
    p.add_argument("--stage", help="stage / group column (stages become boxes around steps on the simple map)")
    p.add_argument("--action", help="UI action column; only cleaned completion milestones are retained")
    p.add_argument("--milestones", help="JSON milestone catalogue (rows and confirmed_by_business)")
    p.add_argument("--order", help="step order for the simple map (an Order JSON file, as saved by page 3)")
    p.add_argument("--derive-case", choices=["text_pattern", "work_session", "time_window"],
                   help="no case column: derive the case from an ID in text, work sessions or time windows")
    p.add_argument("--pattern", help="with --derive-case text_pattern: the ID pattern (ticket, code, masked, digits); "
                                     "default: the best one found")
    p.add_argument("--text-column", help="with --derive-case text_pattern: the text column (default: window title)")
    p.add_argument("--case-id-confirmed", action="store_true",
                   help="the business confirms what the found ID means (lifts the flow-confidence cap)")
    p.add_argument("--session-gap", type=float, default=None, help="with work_session: pause in minutes (default 30)")
    p.add_argument("--window", choices=["day", "hour"], default="day", help="with time_window: the window")
    p.add_argument("--window-group", help="with time_window: per this column (user or machine)")
    p.add_argument("--record-file", help="derive the case by joining a system-of-record export "
                                         "(CSV/Excel: record_id, user, start_ts, end_ts)")
    p.add_argument("--overview-only", action="store_true", help="no case at all: activity overview only")
    p.add_argument("--derive-activity", action="store_true",
                   help="no activity column: activity = application + cleaned window title (screens)")
    p.add_argument("--find-readings", action="store_true",
                   help="also try other case/activity definitions of the data and print them, best first")
    p.add_argument("--save-order", action="store_true",
                   help="also save --order for this file layout, so page 3 and later runs reuse it")
    p.add_argument("--level1", help="process level 1 column (level 2 is --stage)")
    p.add_argument("--level3", help="process level 3 column")
    p.add_argument("--step-run", help="step run ID column (one execution of a step)")
    p.add_argument("--active-time", help="active time column, in seconds (effort per step run)")
    p.add_argument("--wait-time", help="wait time column, in seconds (waiting share = wait / turnaround time)")
    p.add_argument("--tat-time", help="turnaround time column (active + wait), in seconds")
    p.add_argument("--standards", help="JSON of business standards per step: {step: {standard_path, "
                   "mandatory_actions, confirmed_by_business}} (or {\"steps\": {...}}), saved for this layout")
    p.add_argument("--activity-renames", help="JSON {old name: new name} (or {\"renames\": {...}}): merge "
                   "near-identical activity names; saved for this file layout and applied at conversion")
    p.add_argument("--level4", choices=["build", "reuse", "off"], default="reuse",
                   help="Level 4 sub-steps (needs an action column): reuse the saved taxonomy and build missing Level 3 "
                        "steps (default), build = rebuild every unconfirmed Level 3, off = do not build")
    p.add_argument("--definition", help="a business process definition (Excel, CSV, YAML, JSON or BPMN): saved as a "
                   "draft, checked against the data (evidence), and shown in the report")
    p.add_argument("--export-bpmn", help="with --definition: also write the definition as BPMN 2.0 XML to this path")
    return p.parse_args(argv)


def application_map(args) -> dict:
    """The shared application map, with --app-map merged in (saved only with --save-app-map)."""
    from app.core import apps

    the_map = apps.load_map()
    if not args.app_map:
        return the_map
    path = Path(args.app_map)
    if not path.exists():
        raise InputProblem(f"Application map not found: {path}")
    preview = apps.import_map(path.read_bytes())
    print(f"Application map {path.name}: {preview['added']} added, {preview['updated']} updated, "
          f"{preview['ignored']} rows ignored")
    for row in preview["rows"]:
        the_map[row["raw_value"]] = {k: row[k] for k in ("real_name", "category", "notes")}
    if args.save_app_map:
        counts = apps.save_map(preview["rows"])
        print(f"  saved to the shared map ({counts['total']} applications)")
    return the_map


def derivation_mapping(args, con, mapping: dict) -> dict:
    """Apply --derive-case / --record-file / --overview-only / --derive-activity to the mapping."""
    from app.core.derived import ACTIVITY_COL, CASE_COL, find_id_patterns

    cs = None
    if args.overview_only:
        cs = {"kind": "overview"}
    elif args.record_file:
        path = Path(args.record_file)
        if not path.exists():
            raise InputProblem(f"Record file not found: {path}")
        cs = {"kind": "record_join", "file": str(path.resolve())}
    elif args.derive_case == "text_pattern":
        column = args.text_column or mapping.get("window_title")
        if not column:
            raise InputProblem("--derive-case text_pattern needs --text-column (or a window title column).")
        found = [p for p in find_id_patterns(con, [column], mapping.get("activity"))
                 if p["candidate"] and (not args.pattern or p["pattern"] == args.pattern)]
        if not found:
            raise InputProblem(f"No repeating ID{' of pattern ' + args.pattern if args.pattern else ''} "
                               f"found in {column}.")
        best = found[0]
        print(f"ID found: {best['label']} in {column} ({100 * best['match_share']:.1f}% of rows, "
              f"{best['distinct']:,} distinct)")
        cs = {"kind": "text_pattern", "column": column, "pattern": best["pattern"],
              "confirmed": bool(args.case_id_confirmed)}
    elif args.derive_case == "work_session":
        cs = {"kind": "work_session"}
        if args.session_gap:
            cs["gap_minutes"] = args.session_gap
    elif args.derive_case == "time_window":
        cs = {"kind": "time_window", "window": args.window, "group": args.window_group}
    if cs:
        mapping["case_source"] = cs
        mapping["case_id"] = CASE_COL
    if args.derive_activity:
        mapping["derived_activity"] = True
        mapping["activity"] = ACTIVITY_COL
    return mapping


def check_definition_file(args, job: dict, folder: Path) -> None:
    """--definition: read, validate, save as the next draft, check against the data, print the claims."""
    from app.core import definition as dmod
    from app.core.db import connect
    from app.core.business_map import bpmn_problems, to_bpmn
    from app.core.evidence import run_check, summary_line, top_contradicted

    path = Path(args.definition)
    if not path.exists():
        raise InputProblem(f"Definition file not found: {path}")
    try:
        defn, notes = dmod.load_file(path.name, path.read_bytes(), default_name=path.stem)
    except (ValueError, KeyError) as err:
        raise InputProblem(f"The definition could not be read: {err}") from err
    for note in notes:
        print(f"  note: {note}")
    activities = [a for (a,) in connect().execute(
        f"SELECT DISTINCT activity FROM read_parquet('{(folder / 'events.parquet').as_posix()}')").fetchall()]
    errors, warnings = dmod.validate(defn, activities)
    for w in warnings:
        print(f"  [warn] {w}")
    if errors:
        raise InputProblem("The definition is not valid: " + " ".join(errors))
    defn = dmod.new_draft(defn, "command line", f"Imported from {path.name}")
    dmod.save(defn)
    evidence = run_check(job, defn)
    print(f"Definition: {defn.name} v{defn.version} (draft)")
    print(summary_line(evidence))
    for c in top_contradicted(evidence, 5):
        print(f"  {c['id']} {c['statement']} — {c['text']}")
    if args.export_bpmn:
        xml = to_bpmn(defn)
        problems = bpmn_problems(xml)
        if problems:
            raise InputProblem("The BPMN export failed its checks: " + "; ".join(problems))
        Path(args.export_bpmn).write_text(xml, encoding="utf-8")
        print(f"BPMN written to {args.export_bpmn}")


def build_mapping(args, prof) -> dict:
    """Explicit columns first, then (with --auto-map) the suggestions; formats detected."""
    from app.core.mapping import ALL_ROLES, sensitive_columns, suggest_mapping

    names = prof.by_name()
    given = {role: getattr(args, arg) for role, arg in ROLE_ARGS.items() if getattr(args, arg)}
    unknown = [c for c in given.values() if c not in names]
    if args.filter_column and args.filter_column not in names:
        unknown.append(args.filter_column)
    if unknown:
        raise InputProblem(f"Column(s) not found: {', '.join(unknown)}. "
                           f"Available: {', '.join(names)}")
    skipped = set()
    if args.derive_case or args.record_file or args.overview_only:
        skipped.add("case_id")
    if args.derive_activity:
        skipped.add("activity")
    suggestions = suggest_mapping(prof, skip_roles=skipped) if args.auto_map else {}
    mapping: dict = {}
    for role in ALL_ROLES:
        suggested = suggestions[role].column if role in suggestions else None
        # A column given explicitly for one role is never also suggested for another.
        if suggested in given.values() and given.get(role) != suggested:
            suggested = None
        mapping[role] = given.get(role) or suggested

    def detected(col):
        return (names[col].ts_format or "iso") if col else None

    mapping["timestamp_format"] = args.timestamp_format or detected(mapping.get("timestamp"))
    mapping["end_timestamp_format"] = detected(mapping.get("end_timestamp"))
    mapping["timezone"] = args.timezone
    mapping["scope"] = {
        "drop_missing_case": args.drop_missing_case,
        "drop_missing_activity": args.drop_missing_activity,
        "filter_column": args.filter_column if args.filter_values else None,
        "filter_values": args.filter_values,
    }
    mapping["merge_repeats"] = args.merge_repeats
    roles = {mapping[r] for r in ALL_ROLES if mapping.get(r)}
    exclude = list(args.exclude)
    if not args.keep_sensitive:
        for col, reason in sensitive_columns(prof, roles).items():
            if col not in exclude:
                exclude.append(col)
                print(f"  leaving out '{col}' (personal data? {reason}); use --keep-sensitive to keep it")
    mapping["exclude_columns"] = exclude
    return mapping


def run(args) -> int:
    from app.core import jobs, observability
    from app.core.db import connect
    from app.core.facts import format_fact, load_facts, trust_text
    from app.core.ingest import normalise, open_source, profile, resolve_sources
    from app.core import reference
    from app.core.mapping import signature, validate_mapping
    from app.core.mining import run_mining, save_mining
    from app.core.narrative import load_narrative
    from app.core.quality import run_quality, save_report
    from app.core.report import generate_report

    from app.core.privacy import status_line
    print(status_line())
    source = args.input
    try:
        _, files = resolve_sources(source)
    except ValueError as err:
        raise InputProblem(str(err)) from err
    name = files[0].name if len(files) == 1 else f"{Path(source).name or source} ({len(files)} files)"
    job = jobs.create_job(name, source)
    folder = jobs.job_folder(job["job_id"])
    print(f"Job {job['job_id']}: {name}")

    con = connect()
    try:
        fmt = open_source(con, source, folder)
    except ValueError as err:
        raise InputProblem(str(err)) from err
    prof = profile(con)
    job.update(status="profiled", format=fmt, rows=prof.rows)
    jobs.save_job(job)
    print(f"Read {prof.rows:,} rows, {len(prof.columns)} columns ({fmt}) in {prof.seconds:.1f} s")

    mapping = build_mapping(args, prof)
    mapping = derivation_mapping(args, con, mapping)
    if mapping.get("step_run") and not args.step_run:
        from app.core.mapping import step_run_fits
        if not step_run_fits(con, mapping["step_run"], mapping.get("activity")):
            mapping["step_run"] = None     # a suggested run ID must hold one step per value
    print("Mapping: " + ", ".join(f"{r}={mapping.get(r)}" for r in ROLE_ARGS)
          + f", timestamp format {mapping['timestamp_format']}")
    from app.core.capabilities import as_text, capabilities
    from app.core.derived import apply_derivations, case_label
    caps = capabilities(mapping)
    print(case_label(mapping))
    print("What this data supports:")
    print(as_text(caps))
    try:
        mapping = apply_derivations(con, mapping)
    except ValueError as err:
        raise InputProblem(str(err)) from err
    app_map = application_map(args)
    if mapping.get("application"):
        from app.core import apps
        counts = apps.app_counts(con, mapping["application"])
        masked = [raw for raw, _ in counts if apps.is_masked(raw)]
        named = sum(1 for raw in masked if (app_map.get(raw) or {}).get("real_name"))
        print(f"Applications: {len(counts)} ({len(masked)} masked, {named} mapped)")
    checks = validate_mapping(con, mapping, prof.rows, app_map=app_map)
    for c in checks:
        mark = "info" if c.severity == "info" else ("ok" if c.ok else c.severity.upper())
        print(f"  [{mark}] {c.name}: {c.message}")
    if any(not c.ok and c.severity == "error" for c in checks):
        raise InputProblem("The mapping is not usable; see the checks above.")

    if args.find_readings:
        from app.core.reading_finder import find_readings, save_readings, warning_text
        readings = find_readings(con, "src", mapping)
        save_readings(readings, folder)
        print()
        print("Other readings of the data (best first):")
        print(f"  {'case definition':<40} {'activity':<20} {'strength':>8} {'stability':>9} "
              f"{'multi%':>6} {'cover%':>6} {'conf':>5}")
        for r in readings:
            print(f"  {r.case_label[:40]:<40} {r.activity[:20]:<20} {r.order_strength:>8.2f} "
                  f"{'-' if r.stability is None else f'{r.stability:.2f}':>9} {100 * r.multi_step_share:>6.1f} "
                  f"{100 * r.coverage:>6.1f} {r.confidence:>5.0f}{'  (current)' if r.current else ''}")
        message = warning_text(readings)
        if message:
            print(f"  {message}")
        print()
    from app.core import activity_renames
    layout = signature([c.name for c in prof.columns])
    if args.activity_renames:
        path = Path(args.activity_renames)
        if not path.exists():
            raise InputProblem(f"Activity renames file not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        try:
            saved = activity_renames.save(layout, data.get("renames", data))
        except (ValueError, AttributeError) as err:
            raise InputProblem(f"The activity renames file is not valid: {err}") from err
        print(f"Activity renames saved for this file layout ({len(saved)} names).")
    mapping["activity_renames"] = activity_renames.load(layout)
    if mapping["activity_renames"]:
        print(f"Merging {len(mapping['activity_renames'])} near-identical activity name(s).")
    summary = normalise(con, mapping, folder / "events.parquet", app_map=app_map)
    from app.core.ingest import PROTECT_CASE_IDS, case_ids_protected
    if not case_ids_protected(folder / "events.parquet"):
        print(f"  [warn] {PROTECT_CASE_IDS}")
    with open(folder / "mapping.json", "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)
    job.update(status="normalised", summary=summary, warnings=[c.message for c in checks if not c.ok],
               layout_signature=signature([c.name for c in prof.columns]),
               case_source={**(mapping.get("case_source") or {"kind": "column", "column": mapping["case_id"]}),
                            "label": case_label(mapping)},
               capabilities=caps)
    jobs.save_job(job)

    report = run_quality(connect(), folder / "events.parquet")
    save_report(report, folder)
    job.update(status="quality_checked", feasibility=report.feasibility, band=report.band)
    jobs.save_job(job)

    if args.milestones:
        from app.core.milestones import save_catalogue
        from app.core.reference import order_key
        catalogue = json.loads(Path(args.milestones).read_text(encoding="utf-8"))
        save_catalogue(order_key(job), catalogue["rows"], catalogue.get("confirmed_by_business", False))
    if args.standards:
        path = Path(args.standards)
        if not path.exists():
            raise InputProblem(f"Standards file not found: {path}")
        from app.core.subprocess import save_standards
        data = json.loads(path.read_text(encoding="utf-8"))
        try:
            save_standards(job["layout_signature"], data.get("steps", data))
        except (ValueError, AttributeError, TypeError) as err:
            raise InputProblem(f"The standards file is not valid: {err}") from err
        print(f"Step standards saved for this file layout ({len(data.get('steps', data))} steps).")
    from app.core.subprocess import ACTIONS_FILE
    use_ai_l4 = args.level4 != "off" and not args.no_ai
    if args.level4 == "build" and (folder / ACTIONS_FILE).exists():
        from app.core import level4
        built = level4.build_or_update(job, "build", use_ai=use_ai_l4, progress=print)
        print(f"Level 4 taxonomy (build): " + ", ".join(f"{k}: {v}" for k, v in built.items()))
    mining = run_mining(connect(), folder / "events.parquet", report.band, use_ai=use_ai_l4,
                        progress=print if use_ai_l4 else None)
    ms = (mining.get("milestones") or {}).get("data")
    if ms:
        print(f"Milestones: {ms['actions']} actions, order strength {ms['order_strength']:.3f} "
              f"(cases {ms['coverage']:.0%}), chains: {len(ms['chains'])}")
    save_mining(mining, folder)
    job.update(status="mined", mined_at=mining["created_at"],
               flow_band=(mining["flow_confidence"]["data"] or {}).get("band"))
    jobs.save_job(job)

    if args.definition:
        check_definition_file(args, job, folder)

    order = None
    if args.order:
        path = Path(args.order)
        if not path.exists():
            raise InputProblem(f"Order file not found: {path}")
        try:
            order = reference.Order.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError) as err:
            raise InputProblem(f"The order file is not a valid order: {err}") from err
        order.source = "edited"
        if args.save_order:
            reference.save_order(reference.order_key(job), order)
            print("Step order saved for this file layout.")
    paths = generate_report(job["job_id"], use_ai=not args.no_ai, order=order)
    observability.flush()   # send the trace before the script ends (no-op without Langfuse)
    facts = load_facts(folder)
    story = load_narrative(folder)

    merged = summary["events_before_merge"] - summary["events"]
    print("\n=== Summary ===")
    print(f"Rows in file:      {summary['rows_in_file']:,}")
    print(f"Rows left out:     {summary['rows_left_out']:,}")
    print(f"Events:            {summary['events']:,}" + (f" ({merged:,} repeats merged)" if merged else ""))
    print(f"Cases:             {summary['cases']:,}")
    print(f"Quality:           {report.feasibility} / 100 · {report.band}")
    if report.fixes:
        print("What to fix first:")
        for i, fx in enumerate(report.fixes, 1):
            print(f"  {i}. [{fx['status']}] {fx['check']}: {fx['problem']} -> {fx['fix']}")
    top = sorted((f for f in facts if f.section != "data"), key=lambda f: ({"High": 0, "Medium": 1}.get(f.badge, 2), f.id))[:5]
    if top:
        print("Top facts:")
        for f in top:
            print(f"  {f.id} {f.label}: {format_fact(f)} ({trust_text(f)})")
    ref_path = folder / "reference.json"
    if ref_path.exists():
        ref = json.loads(ref_path.read_text(encoding="utf-8"))
        s = ref["stats"]
        status = "data (milestones)" if ref["order"]["source"] == "data (milestones)" else "confirmed" if ref["order"]["confirmed_by_business"] else (
            "edited" if ref["order"]["source"] == "edited" else "suggested")
        in_order = "–" if s["in_order_share"] is None else f"{100 * s['in_order_share']:.1f}%"
        print(f"Order strength: {s['order_strength']:.2f} ({s['order_strength_label']}) · "
              f"in order: {in_order} of cases · order: {status}")
        if ref.get("flow"):
            from app.core.flow_confidence import describe as describe_flow
            print(f"Data quality: {report.feasibility} {report.band} · {describe_flow(ref['flow'])}")
    if story is not None and story.source == "ai":
        print(f"Summary written by: AI (prompt version {story.prompt_version}, "
              f"{story.attempts} attempt{'s' if story.attempts != 1 else ''}, {story.facts_sent} facts)")
    elif story is not None:
        print(f"Summary written by: fixed templates (AI not used: {story.reason})")
    sw = mining.get("app_switching") or {}
    if sw.get("available"):
        k, pairs = sw["data"]["kpis"], sw["data"]["pairs"]
        top = f"{pairs[0]['from']} → {pairs[0]['to']}" if pairs else "none"
        print(f"App switches: {k['total']:,} total · median {k['per_case_median'] or 0:g} per case · top: {top}")
    elif mapping.get("application"):
        print(f"App switches: {sw.get('reason') or 'not available'}")
    l4 = mining.get("level4") or {}
    if l4.get("available"):
        from app.core.level4 import source_label
        print("Level 4 (Level 3 step: sub-steps · coverage · source):")
        for d in l4["data"]["l3s"]:
            named = sum(1 for n in d["nodes"] if n["name"] != "Other")
            print(f"  {d['l3']}: sub-steps {named} · coverage {100 * d['coverage']:.0f}% · {source_label(d)}")
    sub = mining.get("subprocess") or {}
    if sub.get("available"):
        print("Sub-processes (step: runs, median actions, inside order strength, % standard):")
        for d in sub["data"]["steps"]:
            print(f"  {d['activity']}: {d['runs']:,} runs, {d['median_actions']:g} actions, "
                  f"order {d['order_strength']:.2f} ({d['confidence']}), "
                  f"{100 * d['comparison']['standard_share']:.0f}% standard")
    print(f"Report (HTML):     {paths['html']}")
    print(f"Report (Excel):    {paths['excel']}")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # activity names may hold any character ("→" too)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.data_dir:  # must be set before app modules read it
        os.environ["DATA_DIR"] = str(Path(args.data_dir).resolve())
    try:
        return run(args)
    except InputProblem as err:
        print(f"\nCannot continue: {err}", file=sys.stderr)
        return 2
    except Exception as err:  # noqa: BLE001 - report every unexpected error plainly
        print(f"\nUnexpected error: {type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
