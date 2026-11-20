# Milestones from actions — implementation and validation

Date: 2026-11-05. Scope: Step 6, the milestone feature for the planned v1.1 release.

Implemented optional action mapping, cleaned pre-merge capture, editable catalogues,
milestone analysis, order-source integration, reading-finder candidates, maps,
report/Excel exports, fact and AI rules, and business-definition integration.
No project `data/` contents were read or modified. No commit or Streamlit server
was started. Page checks used headless Streamlit AppTest with temporary jobs.

## Changed files and affected line ranges

Ranges identify the affected implementation areas, including their surrounding
functions; they are not a Git diff. The workspace has no Git repository.

| File | Lines | Change |
| --- | --- | --- |
| `app/core/mapping.py` | 32–49, 261–262, 305–315 | Optional action role, hints, fill and distinct-value checks. |
| `app/core/ingest.py` | 515–516, 564–571 | Exclude mapped actions from events; capture scoped, unmerged milestone candidates and summary counts. |
| `app/core/milestones.py` | 1–242 | New cleaning, atomic catalogue storage, SQL analysis, supported chains, completion rates, coverage and DOT map. |
| `app/core/capabilities.py` | 67–69 | Recognize a mapped action column in the capability matrix. |
| `app/core/mining.py` | 514–524 | Run milestone analysis when maps are allowed; persist milestone evidence. |
| `app/core/flow_confidence.py` | 18–34, 66–122 | Milestone order source and coverage-based Medium cap. |
| `app/core/reference.py` | 429–455, 811–844 | Milestone order selection, confidence, coverage badge, and fallback when no selected milestones remain. |
| `app/core/reading_finder.py` | 113–114, 259–279, 343–346 | Add both milestone readings with coverage; keep raw actions out of ordinary activity candidates. |
| `app/core/facts.py` | 359–376 | Strength, coverage, chain and completion facts with business labels and chain confidence. |
| `app/core/narrative.py` | 84, 231–244 | Include catalogue labels; validate the chain-only sequence-language exception. |
| `app/prompts/report_writer_system.txt` | 16 | Explain the verified-chain exception and coverage caveat to the AI. |
| `app/core/evidence.py` | 74–107, 257–285, 443–445 | Use newest flow evidence, expand multi-step chains, support forward claims, contradict reverse claims and invalidate stale evidence. |
| `app/core/business_map.py` | 84–85 | Display selected milestone labels in business-map boxes. |
| `app/ui/business_page.py` | 169–184 | Per-step milestone multiselect restricted to that step's mapped activities. |
| `app/ui/map_page.py` | 221–240, 381–399 | Coverage-aware source picker, milestone view, chain display and catalogue editor. |
| `app/ui/milestones_editor.py` | 1–48 | New filterable editor, confirmation, CSV import/export, JSON export and recomputation after save. |
| `app/core/report.py` | 164–168, 505–510 | Section-four milestone content and Excel Milestones sheet. |
| `app/templates/report.html.j2` | 158–171 | Chain text, coverage and milestone map using the existing bundled Viz.js. |
| `scripts/generate_synthetic.py` | 265–282, 311, 333–334, 367 | Action noise, planted chain in forty percent of cases, one-user action and action-column omission support. |
| `scripts/run_pipeline.py` | 31, 63–64, 335–344, 391–392 | Action/catalogue arguments, catalogue import and milestone summary/order source. |
| `config/settings.yaml` | 290–295 | Completion verbs, frequency, users, pair support and dependency thresholds. |
| `tests/test_milestones.py` | 1–206 | Six integration tests covering the requested scenarios, fake AI payloads and both relevant pages. |
| `README.md` | 965–1014 | Workflow, thresholds, interpretation, privacy, CLI examples and limitations. |
| `docs/MILESTONES_IMPLEMENTATION.md` | Entire file | This implementation and validation record. |

The upload page already builds its role selectors from `ALL_ROLES`, so the action
selector appears there without duplicating its mapping UI.

## Validation

Tests used `.release-venv\Scripts\python.exe`, the working environment established
during the v1.0 release check. No dependency changes were needed.

| Check | Result | Pytest time |
| --- | --- | --- |
| Baseline `pytest -q` | 177 passed, 2 slow tests deselected | 117.68 s |
| Final `pytest -q` | 183 passed, 2 slow tests deselected | 142.47 s |
| `pytest -q -m slow` | 2 passed, 183 deselected | 45.20 s |
| Focused milestone tests | 6 passed | 13.70 s |

The final normal run took 144.18 seconds including its harness; the slow run took
46.26 seconds including its harness. Warnings are Streamlit's existing pandas
`reset_index(inplace=...)` deprecation, also emitted by the new AppTest checks.

Covered checks include identical capture with/without merging, removal of noisy
and one-user actions, person-like filtering, whole-word verbs, exact chain support,
half-sample stability, coverage caps, catalogue persistence and deselection,
reading-finder candidates, per-run completion denominators, forward/reverse
business claims, business-map labels, report/Excel output, narrative validation,
fake AI payload privacy, the milestone editor/map and the business-page selector.

Final end-to-end runs both exited zero:

| Run | Rows | Cases | Quality | Step strength | Milestone strength | Coverage | Chains | Flow |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Shuffled actions, stages, merge repeats, reading finder | 21,786 | 3,000 | 96 High | 0.12 | 0.999 | 40% | 1 | 79 Medium |
| Same input, imported JSON catalogue with business labels | 21,786 | 3,000 | 96 High | 0.12 | 0.999 | 40% | 1 | 79 Medium |

There are 20,362 events after merging. The planted chain has 1,200 supporting
cases, forward link counts of 1,200 and 1,200, and zero reverse transitions.
Both milestone reading candidates report 40% coverage. Catalogue import changes
the labels to “Tax data created”, “Benefits created” and “Submitted”.

Final test log:
`C:\Users\hp\AppData\Local\Temp\pm-milestones-final-result\pytest_normal.log`.
The two successful final CLI jobs are under:

- `C:\Users\hp\AppData\Local\Temp\pm-milestones-verified\pipeline\jobs\20261105-122753-bff1f4`
- `C:\Users\hp\AppData\Local\Temp\pm-milestones-verified\imported\jobs\20261105-122824-c79c03`

Each contains `report.html`, `report.xlsx`, `milestones.json`, `facts.json` and
the job's other pipeline artifacts. These are synthetic outputs only.

## Assumptions and practical limits

- “Many more distinct values” means at least three times the activity column's
  distinct count for automatic suggestions. Explicit mapping is still allowed.
- Coverage requires at least two distinct selected milestones in a case. Equal
  timestamps do not establish directional links.
- Default multi-user filtering needs a resource column. Missing resources do not
  silently bypass that threshold.
- A reported chain must occur consecutively in the selected, repeat-merged
  milestone sequence of at least `min_pair` cases. Support is counted across the
  whole chain; it is not inferred from separate link totals.
- Branching-chain exploration is bounded at 10,000 visited paths. The displayed
  chain list explicitly reports truncation if the limit is reached.
- Order preselection requires a strictly higher confidence **band** than the
  ordinary data order. A catalogue confirmation does not approve a definition.
- Catalogue labels are the permitted business-facing action names. Their default
  is the cleaned “Step · Action”; editing them replaces that text in facts,
  reports and AI input. Raw action fields remain outside those outputs.
- This implements Step 6. It does not claim the remaining v1.1 sub-process and
  knowledge-source steps are complete or issue a new v1.1 release.

During validation, the new capture's DuckDB result was changed to be fully consumed
before unregistering its label table; the capability status was aligned with the
existing `available` enum; and evidence selection was corrected to use the newest
flow analysis after catalogue edits. The final test and CLI results above include
these corrections.
