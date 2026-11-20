# Release audit — 1.0.0

Date: **2026-11-05 (Asia/Calcutta)**. Scope: Step 5, after business process definition; not the v1.1 release.

**Decision: hold release approval.** The implemented synthetic workflows run, but the requested zero-input-ID criterion fails for ordinary mapped case IDs retained in local Parquet files. The original `.venv` is also unusable and has one dependency mismatch. These are explicit findings, not waived checks. Milestone/sub-process checks belong to v1.1 and cannot pass in this version.

## Environment and boundaries

- Windows; isolated `.release-venv`, Python 3.14.0. All 85 lock-file pins and 14 direct dependency pins match this audit environment; `pip check` reports no broken requirements.
- Original `.venv` points to a missing Python 3.11.9 executable. Its installed metadata has `pypdf==6.21.4`, whereas both requirements files pin `6.6.2`. The original environment was preserved.
- All generators and pipelines used this temporary root, abbreviated **T** below:
  `C:/Users/hp/AppData/Local/Temp/pm-release-35939a6909ee49329b8eac4d28595d67`
- No Streamlit server was started, no commit was made, and no real HR file was opened. AppTest runs scripts headlessly.
- An early AppTest exposed a hard-coded sample-directory glob that enumerated `data/samples` despite temporary `DATA_DIR`. This was corrected; no sample contents were loaded and no project data was modified. Subsequent smoke tests use the temporary sample directory.
- AI and Langfuse were disabled. Fake clients captured report and document-draft payloads without network calls.

## Tests

| Command | Final result | Pytest elapsed | Including interpreter startup |
| --- | --- | ---: | ---: |
| `python -m pytest -q` | **177 passed**, 2 slow deselected, 36 upstream warnings | **147.18 s** | 148.71 s |
| `python -m pytest -q -m slow` | **2 passed**, 177 deselected | **49.82 s** | 50.88 s |

Both ran with `.release-venv/Scripts/python.exe`. Initial regression runs failed on the real defects listed below, then passed after fixes. Existing tests were retained. Full final summaries are saved in the accompanying evidence JSON.

The suite includes report rendering, Excel sheets, missing-column fallbacks, privacy guards and AppTest. The new release regressions cover the definition CLI, checked-page rendering and empty filters, approved-version preservation, persistence of linked definitions, stale evidence, and derived-case mapping.

## Synthetic pipeline results

Times below are pipeline wall times excluding generator and interpreter startup. Facts are the counts recorded immediately after each pipeline, before the subsequent clean-job smoke/fake-AI exercise. Every successful run wrote both report formats. `—` means not computed, not a successful flow verdict.

| Run | Exit | Input rows | Cases | Quality | Flow band / source | Facts | Seconds |
| --- | ---: | ---: | ---: | --- | --- | ---: | ---: |
| Clean, apps, stages, merge repeats, app map | 0 | 24,288 | 3,000 | 100 High | High / data | 105 | 2.356 |
| Definition + BPMN + confirmed order | 0 | 24,288 | 3,000 | 100 High | High / confirmed | 122 | 2.193 |
| Dirty: missing values, duplicates, offsets, typos | 0 | 18,534 | 3,000 | 79 Medium | High / data | 98 | 1.486 |
| Shuffled with stages | 0 | 18,186 | 3,000 | 100 High | None / data | 79 | 1.313 |
| Shuffled with confirmed order | 0 | 18,186 | 3,000 | 100 High | Medium / confirmed | 73 | 1.294 |
| Five Parquet files, extra column in two | 0 | 18,186 | 3,000 | 100 High | High / data | 98 | 1.431 |
| Tiny | 0 | 355 | 60 | 39 Not feasible | — | 5 | 0.617 |
| Invalid timestamp | 2 | 18,186 | — | — | — | 0 | 0.404 |
| No stage/levels/app/action | 0 | 18,186 | 3,000 | 100 High | High / data | 98 | 1.265 |
| Case derived from ticket in title | 0 | 18,186 | 3,000 | 100 High | Medium / data | 99 | 1.701 |
| User work sessions | 0 | 12,000 | 3,000 | 100 High | Medium / data | 48 | 1.234 |
| Overview only | 0 | 12,000 | 6 user timelines* | 39 Not feasible | None; no process order | 6 | 0.852 |
| Large | 0 | 2,500,000 | 412,820 | 100 High | High / data | 98 | 23.561 |

*Overview's internal `summary.cases` is six user timelines, not six business cases. The report identifies it as an activity overview and omits process paths. The capability matrix and case source are retained in the evidence ledger's job metadata.

Clean order strength was **0.941 (Strong)**; shuffled strength was **0.040**. The shuffled narrative says “The data does not show a reliable order of steps”; its flow score was 8/100. The dirty report includes its fix list. The tiny report renders and the injected fake client received **zero calls**.

The folder overview lists five files with 3,638 / 3,637 / 3,637 / 3,637 / 3,637 rows. `region` is missing in exactly three files. This exercises the same overview used by the upload page.

Large-run timings: conversion **1.902 s**, quality **3.402 s**, mining **8.485 s**, report **7.762 s**; total **23.561 s**. Windows `GetProcessMemoryInfo` measured peak working set **1,369.9 MiB** for the pipeline process. Generator memory and other processes are excluded. The input is an exact 2.5M-row slice of the generated fixture; the final case may be truncated.

The generator/CLI have no action, run-ID, hierarchy-level, active-time or planted-milestone-chain options in this version. These flags were not invented. Milestone chain detection and v1.1 inside-step metrics are **not tested/not available**.

### Output locations

For every row below the HTML and Excel paths are respectively `T/<folder>/report.html` and `T/<folder>/report.xlsx`. The evidence JSON also records full absolute paths, commands and per-stage timings.

| Run | Folder below T |
| --- | --- |
| Clean | `clean/jobs/20261105-114820-7013d8` |
| Definition | `definition/jobs/20261105-115828-bfd49f` |
| Dirty | `dirty/jobs/20261105-114827-2056b8` |
| Shuffled | `shuffled/jobs/20261105-114830-c05c6e` |
| Shuffled confirmed | `shuffled_confirmed/jobs/20261105-114833-52b9dd` |
| Folder | `folder/jobs/20261105-114835-ab706e` |
| Tiny | `tiny/jobs/20261105-114838-67b5d5` |
| Missing columns | `missing_columns/jobs/20261105-114842-95f95d` |
| Derived case | `derived_case/jobs/20261105-115117-96d327` |
| Sessions | `work_sessions/jobs/20261105-115120-70e363` |
| Overview | `overview/jobs/20261105-115123-0ddd25` |
| Large | `large/jobs/20261105-114850-be367b` |

Bad timestamp intentionally produced no report; its job folder is `T/bad_timestamp/jobs/20261105-114840-6376ef`. BPMN is `T/definition.bpmn`. The clean job was subsequently given a synthetic definition for all-tab smoke tests and fake-AI capture, so its current report has additional definition evidence.

## Leak audit

**Strict requested result: FAIL, not zero hits.** Scanned 173 artifacts, including all output JSON, all string columns in output Parquet (not a sample), HTML, every Excel sheet/cell, console output, and captured fake-AI payloads. Earlier failed/retried pipeline artifacts were included as well.

- **15 file/column groups contain original case IDs** such as `C00000001`, with eight input-derived digits. These are in `events.parquet.case_id` and `app_switches.parquet.case_id`. The largest group has 2,500,000 rows. Normal mapping intentionally retains original case IDs; derived cases use salted hashes. Changing ordinary case-ID representation would change application behavior and is not silently done during this release check.
- **58 long-hex match locations**: 56 are the audit's generated temporary-folder identifier in source paths/console output; two are application-generated definition fingerprints in `evidence.json`. None is a raw application hash from the input. They remain listed because the requested regex would match them.
- **Zero matches** for email addresses, input ticket IDs, tested window-title patterns, person-name sentinels, or HTTP(S) script sources in reports. No original case IDs reached the captured AI payloads or the reports.
- The current generator does not generate raw action text or real person names. Those categories are not claimed as meaningfully exercised by the synthetic pipeline runs; existing privacy tests cover person-name handling separately.

Every match is retained in **[RELEASE_1.0.0_EVIDENCE.json](RELEASE_1.0.0_EVIDENCE.json)** under `leaks.hits`, with file, JSON path/column/cell/line, occurrence count and example. Parquet repeated matches are grouped by file/column with exact counts instead of listing millions of identical ID locations. The complete scanned-file list is also saved. This is the authoritative ledger for all hits, including benign metadata matches.

## Consistency and fixes

All settings leaf names have a string-literal reference in application/scripts source; no unused-leaf candidates were found. This static check does not prove that every possible configuration branch executes. The README now documents the previously absent `DATA_DIR` and `DUCKDB_THREADS` variables; every `.env.example` variable is present.

README coverage: ingest/mapping and quality (sections 1–7), mining/simple map/layout (8–9 and later map sections), facts/report (10–11), AI/Langfuse and application mapping (later dedicated sections), privacy, application switching, flow confidence/reading finder, missing columns (22), and business definitions (23). The stale Day-1-only table and CDN-default map description were corrected. `CHANGELOG.md` describes v1.0 features and labels v1.1 features as planned.

**13 README command checks passed**: four generator examples (including the 1,650,000-case example) and nine pipeline command families: simple, filtered/merged, application map, ticket derivation, sessions, time windows, overview, record join and derived activity. Placeholder filenames/column names were replaced by equivalent synthetic fixtures, outputs redirected to T, and AI disabled. Setup/shell activation, Git commit, server startup and external AI/Langfuse operations were not executed because they conflict with this audit's boundaries. Exact commands, outcomes and times are in `readme_examples` in the evidence ledger.

All six pages rendered with AI off, including all seven Process map tabs and five Business process tabs. Emptying the evidence filter also renders. Fake report and document clients each received one call; the draft was accepted. No real AI service was contacted.

Each bug fix is listed separately:

| Failing evidence | Cause | Change |
| --- | --- | --- |
| Definition CLI exited 1 with `NameError: connect is not defined` | Helper used an import local to another function | Import `connect` inside `check_definition_file`; rerun exits 0 and writes BPMN/report |
| Checked Business page raised `StreamlitDuplicateElementId` | Two identical BPMN download buttons | Give final-map download a distinct key |
| Empty verdict selection creates a columnless frame; selecting columns raises `KeyError` | DataFrame construction inferred columns from empty rows | Supply explicit evidence-table columns; AppTest exercises empty selection |
| Approved-version regression did not raise and overwrote v1 | Save had no immutable-approved guard; UI reused approved version number | Reject changed approved content; create the next draft when editing approved content |
| After checking an unsaved definition, `for_job(...)` returned None | Evidence linked a version not persisted | Save the checked definition before writing its job link |
| Derived-case/work-session auto-map initially exited 2 | Mapping reserved an input column for a case role that would later be derived, taking activity/resource columns | Skip roles supplied by derivation during automatic mapping; README auto-map examples now pass |
| Edited saved draft still had an enabled approval button using old evidence | Evidence was loaded solely by filename | Fingerprint definition content and check input/analysis freshness; disable approval/map evidence until rechecked; regenerate stale evidence during report generation and refresh changed claim facts |
| AppTest source showed sample lookup fixed to `PROJECT_ROOT/data/samples` | Sample picker ignored configured data directory | Resolve samples under `data_dir()` so temporary environments stay isolated |

Release metadata changes: `app.__version__ = "1.0.0"`; version is beside sidebar privacy status and in report footer/Appendix. These are requested metadata additions, not verdict changes. No thresholds were relaxed to make tests pass.

## Known limitations and release conditions

- Resolve the zero-original-ID requirement for local normalized Parquet before approving release. It differs from the existing design, which preserves mapped case IDs locally while excluding them from AI.
- Recreate or repair the original `.venv` before relying on it; it cannot execute on this machine. The audited `.release-venv` is usable and matches all checked pins.
- No fixed business order can be inferred from shuffled desktop behavior without supporting business knowledge. Confirmed order expresses business intent, not proof of observed sequencing.
- Milestones, sub-process standards, hierarchy-aware analysis and knowledge sources are not v1.0 features. Compatibility hooks do not mean milestone detection is implemented.
- The inherited `evaluate_narrative.py --task definition` is only a marker and still runs narrative evaluation; it is not a full definition-prompt comparison harness. The Step-4 acceptance matrix is not fully covered by dedicated evidence/definition-AI tests.
- Object-centric process mining is not supported; video processing requires separate local tools. No video or real HR files were examined.
- AppTest validates widget execution, not final visual appearance in a browser or third-party BPMN modeler. BPMN XML/layout references are tested, but opening Camunda/bpmn.io remains manual.
- Upstream Streamlit/Pandas deprecation warnings remain; they did not cause failures.

## Manual checklist — real HR file (user only)

The following figures are **user-supplied expectations, not measured by this audit**.

- [ ] Open the real HR file in the app and map case `case_id`, activity `task_name`, timestamp `date_time`, resource `participant_id`, application `Application_name`, window title `simplified_title`, and stage `level2_process_name`.
- [ ] Leave out rows without case and activity; enable merge repeated steps; exclude `case_id_value`.
- [ ] Name application `56b82a29…` as **Exion**.
- [ ] Verify **693,648 rows kept**, **117,187 steps after merging**, **10,907 cases**.
- [ ] Verify data-quality score **99 High** and order strength approximately **0.09**; without business order the flow band should be **None**.
- [ ] Verify **96,977 application switches**, median **3 per case**, top switch **ServiceNow → Exion**, and step with most switches **Case Tracking**.
- [ ] Define/edit/check the business process, review contradicted claims, approve with the process owner's name, and inspect the final map with evidence colours. Generate the report and check section 4 and the Definition/Evidence Excel sheets.
- [ ] Capture page 2 score, page 3 simple map, Applications tab, Business process final map with evidence colours, and report section 4.
- [ ] For the future **v1.1** release only: map action `activity_alias_name`, step-run ID `abstraction_instance_id`, level 1 `level1_process_name`, level 3 `level3_process_name`, active time `active_time`.
- [ ] Future v1.1 expectations: milestone order strength approximately **0.47** on approximately **34% of cases**; chain **Employee Profile · Create Monitoring Of Tasks → Payroll · Create National Ins → Create Tax Data → Create General Benefits → Click Submit**; inside-step strength **0.49–0.95**, Payroll Activation approximately **0.84**, Background Checks approximately **0.95**. These are not expected to be available in v1.0.

## Reproduce

Use `.release-venv/Scripts/python.exe` with `scripts/release_check.py --work <temporary-folder> --phase prepare`, then `--phase runs`, `--phase normal` and `--phase slow`. `scripts/release_examples.py <temporary-folder>` exercises README command families; `scripts/release_inspect.py <temporary-folder>` performs consistency, AppTest/fake-AI and leak inspections. Each pipeline receives `--data-dir` inside the temporary folder.
