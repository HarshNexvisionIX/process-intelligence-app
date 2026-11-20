# Changelog

## 1.1.0 — 2026-11-05

- Milestones from UI actions: cleaned completion actions captured before merging, a labelled catalogue,
  confirmed one-way sequences and a milestone map.
- Sub-processes: process levels 1–3, step runs (run ID or pause rule), the standard way inside each step and
  how runs compare with it, inside-step order strength and step confidence, variation by user, step standards
  per file layout, report table and Excel sheet.
- Knowledge sources: SOPs, transcripts, local video/audio transcription and screen OCR (optional tools),
  notes and e-mails, process models and policy tables become validated statements with quotes and anchors;
  business belief, conflicts decided by people, questions for the experts and "Update draft from knowledge".
- Combined confidence of business knowledge and data per element of the business process map, with status
  colours, tooltips, overall confidence with breakdown, and "Documented ≠ actual" / "Undocumented practice"
  findings in the report.
- Screen-level activities never keep person names (frequent or not); a run-ID column is no longer taken as
  the action column.
- Case IDs are stored as salted hashes (the same hash as derived cases) in every job file: events, app
  switches, step actions and milestone candidates. Older jobs show "Re-run Validate and convert to protect
  case IDs".
- Milestone chain search no longer depends on SQL row order (stable results).

## 1.0.0 — 2026-11-05

- Import CSV, Parquet and Excel logs, including folders with differing columns; map and validate columns.
- Measure data quality, explain issues and gate analyses by confidence.
- Mine process flows, variants, waiting times, rework, resources and trends using DuckDB.
- Produce grounded facts and HTML/Excel reports, with optional AI summaries and masked Langfuse tracing.
- Name masked applications and measure application switches, including switches inside merged steps.
- Draw a simple business-order map with stage groups, wrapped layouts and detailed maps.
- Keep AI and tracing private by default and render maps with pinned, bundled Viz.js.
- Measure flow confidence independently of data quality and find alternative readings of a log.
- Handle missing columns with derived case IDs, work sessions and activity-overview mode.
- Define business processes, check evidence, review AI suggestions and export BPMN diagrams.
- Display the release version in the sidebar and report; see docs/RELEASE_1.0.0.md for validation status.

Milestones, sub-process standards and knowledge sources are planned for 1.1.0 and are not released here.
