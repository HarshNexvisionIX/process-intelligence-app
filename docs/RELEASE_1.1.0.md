# Release audit — 1.1.0

Date: **2026-11-05 (Asia/Calcutta)**. Scope: Step 5 repeated for version 1.1.0, after milestones (Step 6),
sub-processes (Step 7) and knowledge sources with combined confidence (Step 8).

**Decision: approve release 1.1.0.** Everything implemented for 1.1 runs on synthetic data, all tests pass,
and no person name, e-mail address, raw action token, window title, ticket ID or **input case ID** was
found in any job file, report, Excel file or AI payload.

The blocking finding carried over from 1.0 is **closed**. Mapped case IDs are now stored as the same salted
hash used for derived cases (`derived_case.hash_length` = 16, salt `CASE_SALT` or `data/.case_salt`). The hash
is written in all four job files: `events.parquet`, `app_switches.parquet`, `step_actions.parquet` and
`milestone_candidates.parquet`. Jobs converted before this change are not rewritten: page 3 and the pipeline
show "Re-run Validate and convert to protect case IDs". All numbers below are from the re-run after the fix;
the first run is mentioned only where it differed.

## Environment and boundaries

- Windows, project `.venv` (Python 3.11). Release harness: `scripts/release_check.py` (phases prepare, runs,
  normal, slow) and `scripts/release_inspect.py` (consistency, page smoke tests, fake-AI payloads, leak scan).
- All inputs and outputs were written to a temporary folder outside the project, abbreviated **T**:
  `C:/Users/hp/AppData/Local/Temp/claude/d--process-mining-app/<session>/scratchpad/release-1.1.0`.
- No Streamlit server was started for the audit, nothing was committed, and nothing in `data/` was read.
  AppTest runs pages headlessly. AI and Langfuse were off; fake clients captured the payloads.
- Dependencies: `pip check` reports no broken requirements, and every pin in both requirement files matches
  the installed packages. The exceptions are the two optional media packages, which are not installed by
  design. In the first audit run `.venv` had `pypdf 6.21.4` against the `6.6.2` pin; it was reinstalled at
  `6.6.2`. `faster-whisper==1.2.1` and `pytesseract==0.3.13` are pinned as **optional**: they are
  not installed in the audit environment, and the app and tests run without them (video sources then report
  which tool is missing). ffmpeg is not installed; Tesseract is present on this machine.

## Tests

| Command | Result | Seconds |
| --- | --- | ---: |
| `python -m pytest -q` | **220 passed**, 2 slow deselected | 155.2 |
| `python -m pytest -q -m slow` | **2 passed**, 220 deselected | 41.9 |

New in 1.1: `tests/test_milestones.py`, `tests/test_subprocess.py` (14), `tests/test_knowledge.py` (7) and
`tests/test_combined_confidence.py` (13) and `tests/test_case_hashing.py` (3: no input case ID in any job
output, identical cases / variants / switches / milestones / sub-process runs before and after hashing,
derived cases hashed once, old-job message). Updated tests: the reading-finder session case is now checked as
a salted hash, the business page now has 7 tabs (was 5), and the
report shows `v1.1.0`.

## Synthetic pipeline results

| Run | Exit | Input rows | Cases | Quality | Flow band | Facts | Seconds | Peak memory (MiB) |
| --- | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: |
| Clean, apps, stages, merge repeats, app map | 0 | 24,288 | 3,000 | 100 High | High | 105 | 1.7 | 178 |
| Definition + BPMN + confirmed order | 0 | 24,288 | 3,000 | 100 High | High | 148 | 2.0 | 176 |
| Dirty: missing values, duplicates, offsets, typos | 0 | 18,534 | 3,000 | 79 Medium | High | 98 | 1.6 | 165 |
| Shuffled with stages | 0 | 18,186 | 3,000 | 100 High | None | 79 | 1.4 | 162 |
| Shuffled with confirmed order | 0 | 18,186 | 3,000 | 100 High | Medium | 73 | 1.3 | 158 |
| Five Parquet files, extra column in two | 0 | 18,186 | 3,000 | 100 High | High | 98 | 1.4 | 167 |
| Tiny | 0 | 355 | 60 | 39 Not feasible | — | 5 | 0.6 | 142 |
| Invalid timestamp (expected refusal) | 2 | — | — | — | — | 0 | 0.4 | 123 |
| No stage / levels / app / action | 0 | 18,186 | 3,000 | 100 High | High | 98 | 1.3 | 164 |
| Case derived from ticket in title | 0 | 18,186 | 3,000 | 100 High | Medium | 99 | 1.4 | 168 |
| User work sessions | 0 | 12,000 | 3,000 | 100 High | Medium | 48 | 1.0 | 157 |
| Overview only | 0 | 12,000 | 6 timelines | 39 Not feasible | — | 6 | 0.6 | 146 |
| **Milestones** (actions, shuffled, stages, merge) | 0 | 21,786 | 3,000 | 96 High | Medium | 159 | 2.6 | 196 |
| **Sub-processes** (step actions, levels, run ID) | 0 | 38,373 | 1,500 | 100 High | High | 187 | 3.3 | 184 |
| **Sub-processes + business standards file** | 0 | 38,373 | 1,500 | 100 High | High | 192 | 3.8 | 191 |
| Large | 0 | 2,500,000 | 412,820 | 100 High | High | 98 | 22.8 | 1,473 |

v1.1 specifics:

- **Milestones:** 15 completion actions, order strength 0.965, 6 chains found.
- **Sub-processes:**
  - The run ID, process levels 1–3 and active-time columns were found by auto-map.
  - Planted steps reach inside-step order strength 0.86–0.89 (confidence High) with about 60% of runs on
    the standard way.
  - The shuffled step "Pay Invoice" scores 0.03 (Exploratory).
  - The `--standards` file was saved for the layout and used.

## Pages, AI payloads and knowledge sources (`release_inspect.py`)

- **Pages:** all six pages render without exceptions.
  - The map page now has the tab "Sub-processes".
  - The business page has "Knowledge sources" and "Conflicts & questions".
  - The empty evidence filter still works.
- **AI calls:** the fake AI was called once for the clean report and once for a document draft. The tiny
  (Not feasible) report made **zero** calls.
- **Knowledge smoke test:** an SOP source with three validated statements was added to the clean job's
  definition, and the report was regenerated.
  - The report shows "Map confidence 60 / 100" with its breakdown (5 Confirmed, 17 Undocumented practice,
    1 Unverified), and both key-finding lists.
  - **No source quote reached the narrative AI payload.**
  - The business page rendered with the knowledge tabs and no exception.

## Leak scan

- 200 files (JSON, Parquet, HTML, Excel, console logs, AI payloads) were scanned.
- **Zero matches** in any of them for:
  - e-mail addresses, input ticket IDs, the tested window-title patterns and the person-name sentinels
    (now including "Priya Sharma", which the sub-process generator plants as an action);
  - the planted raw action token `#9f3c2a1b#`;
  - remote script sources in HTML.
- **Original case IDs** (`C` + 8 digits): **zero matches** in all 200 files, including every Parquet
  `case_id` column (first audit run: 18 groups in four local Parquet files; closed by hashing).
- **Hex hits:** two, both the SHA-256 `definition_fingerprint` in `evidence.json`. This is an internal
  hash of the definition, not input data.

## Consistency

- Every settings key is referenced in code.
- Every `.env.example` variable is documented in the README.
- The only pin mismatches are the optional media packages, which are not installed by design.

## Fixes made during this audit

1. **Case IDs stored as salted hashes** in every job file (closes the 1.0 / first-run blocker). Analyses
   that join on `case_id` (mining, reference map, milestones, sub-processes, evidence, reading finder) work
   unchanged because all four files use the same hash. Results on the clean synthetic log are identical
   before and after hashing (tested).
2. **Milestone chain search made deterministic.** The search stops after 10,000 paths, and it explored
   links in SQL row order, which is not stable. So the chains found could change from run to run. The
   links are now sorted first.

## Remaining (not blocking)

- Optional: install and smoke-test faster-whisper and ffmpeg on a target machine with a short real
  recording. The code path is covered only with mocked transcription and OCR.
