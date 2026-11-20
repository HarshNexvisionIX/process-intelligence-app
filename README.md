# ProcessLens

Upload an event log, check whether the data can be trusted, and get a process map
and full report. Version **1.1.0** includes the features below (1.1 adds milestones,
sub-processes and knowledge sources with combined confidence). Release validation and
outstanding limitations are recorded in `docs/RELEASE_1.1.0.md` (1.0: `docs/RELEASE_1.0.0.md`).

## Web UI (ProcessLens, v3)

A React front end with the ProcessLens look, on the same engine, files and rules as the Streamlit app.

    Start_Web_UI.bat          (Windows: installs fastapi/reportlab once, opens http://127.0.0.1:8008)
    python -m app.api         (any OS)

- Workflows: Import -> Business context -> Overview and analysis -> Process map -> Review steps -> Report
  (no case ID); Import -> Business context -> Data quality -> Process analysis -> Process definition -> Report (case ID).
- AI is approve-before-send: every request is built, privacy-checked and shown as JSON with a SHA-256 first;
  only approved tasks are sent, and every request sent is logged (`ai_audit.jsonl` in the job folder).
- No page asks for an AI key or a model: providers, routes and keys stay in Settings / `.env`.
- Workflow map: every service, AI agent and human gate with its inputs, outputs and files, live per analysis.
- Exports: the business process map and the process map download as **HTML and PDF** (the view, level and
  direction shown); the AI readout, the stakeholder readout, the analysis overview, Data Insights and the case-ID
  results download as **HTML** (readouts also as PDF). HTML files are self-contained: they open offline and print
  to PDF. The maps are drawn with a fixed stage-by-stage layout (not the on-screen one); at most 40 observed arrows
  are drawn, and every transition is listed in the table on the next page.
- The Streamlit app still runs (`streamlit run app/main.py`) and works on the same analyses; Process definition
  (case ID) and AI settings stay there for now. Full guide: `docs/WEB_UI.md`.

| Day | Step | Status |
| --- | --- | --- |
| 1 | Upload, profile, map columns, convert | **Done** |
| 2 | Data quality gate and confidence score | Done |
| 3 | Mining engine and process map | Done |
| 4 | Fact store and report | Done |
| 5 | AI summary and Langfuse | Done |
| 6 | Testing and demo | See release report |

Tested: a 10-million-event Parquet file goes from upload to converted in about
**9 seconds**, and a 743 MB CSV in about **18 seconds**, on a 2-core machine
using 1.2 GB of memory.

---

## 1. One-time setup (about 15 minutes)

### Install these first
1. **Python 3.12** from python.org. On the first installer screen, tick **"Add python.exe to PATH"**.
2. **Git** from git-scm.com (default options are fine).
3. **VS Code**, then these extensions (Extensions panel, `Ctrl+Shift+X`):
   - *Python* (Microsoft)
   - *Pylance* (Microsoft)

### Open the project
1. Unzip `process-mining-app.zip` somewhere simple, e.g. `C:\projects\process-mining-app`.
2. VS Code → **File → Open Folder** → choose that folder.
3. Open a terminal inside VS Code: **Terminal → New Terminal**.

### Create a virtual environment and install packages
Run these in the VS Code terminal, one line at a time.

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.lock.txt
copy .env.example .env
```
If PowerShell says *running scripts is disabled*, run this once, then activate again:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**Mac / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.lock.txt
cp .env.example .env
```

**Which requirements file?**
- `requirements.lock.txt` installs an **exact copy** of the tested environment (every
  package, including sub-dependencies, at the same version). Use this.
- `requirements.txt` lists only the direct dependencies, pinned with `==`. Add
  `requirements-pm4py.txt` for the optional PM4Py process model (section 29), and
  `requirements-dev.txt` for the tests.

The environment was built and tested with Python 3.11. Nothing is upgraded
automatically: to move to newer versions, change the pins on purpose, run `pytest -q`,
and regenerate the lock file with `pip freeze > requirements.lock.txt`.

You will see `(.venv)` at the start of the terminal line when it is active.
Then press `Ctrl+Shift+P` → **Python: Select Interpreter** → choose the one in `.venv`.

### Start version control (do not skip)
```bash
git init
git add .
git commit -m "Day 1: upload, profiling, mapping, conversion"
```
Commit at the end of every working session, so any change can be undone.

---

## 2. Every day: run it

```bash
# 1. Activate the environment (if (.venv) is not shown)
.\.venv\Scripts\Activate.ps1        # Windows
source .venv/bin/activate          # Mac / Linux

# 2. Create test data (first time only)
python scripts/generate_synthetic.py --cases 20000 --out data/samples/clean_small.parquet
python scripts/generate_synthetic.py --cases 20000 --missing-ts 0.03 --ties 0.15 --duplicates 0.02 --typos 0.02 --date-only 0.4 --placeholder 0.01 --missing-case 0.01 --out data/samples/dirty_small.csv --ts-format "%d/%m/%Y %H:%M:%S"

# 3. Start the app (opens http://localhost:8501)
streamlit run app/main.py

# 4. Run the tests
pytest -q
```

Or press **F5** in VS Code and choose **Run app (Streamlit)**, which also lets you
set breakpoints.

For a 10-million-event test file (takes about a minute, ~120 MB):
```bash
python scripts/generate_synthetic.py --cases 1650000 --out data/samples/clean_10m.parquet
```

### Try it
1. Open **Try a sample** → choose `dirty_small.csv` → **Load sample**.
2. Check the profile table and the suggested mapping (green = confident).
3. Click **Validate and convert**.
4. The result is in `data/jobs/<job id>/events.parquet`.

---

## 3. How the code is organised

```
process-mining-app/
├── app/
│   ├── main.py              entry point and page menu
│   ├── core/                all logic, no user interface (testable)
│   │   ├── config.py        reads config/settings.yaml and .env
│   │   ├── db.py            opens DuckDB with memory limits and disk spilling
│   │   ├── jobs.py          one folder per upload under data/jobs/
│   │   ├── ingest.py        read file, profile columns, write events.parquet
│   │   └── mapping.py       date-format detection, column suggestions, validation
│   └── ui/                  Streamlit pages (screens only)
├── requirements.txt         packages the app needs
├── requirements-pm4py.txt   + PM4Py, for the optional discovered model (internal use only)
├── requirements-dev.txt     + test packages
├── config/settings.yaml     thresholds and options, no code changes needed
├── scripts/generate_synthetic.py   test logs with known process and known defects
├── tests/                   pytest tests
└── data/                    uploads, samples and job outputs (never in git)
```

**The pipeline so far:**

```
file (Parquet / CSV / Excel)
   → open_source()      DuckDB reads it in place; CSV read as text so nothing crashes
   → profile()          full-data counts + 200k-row sample for date detection
   → suggest_mapping()  name signal + data signal → best column per role, with confidence
   → user confirms      (always; suggestions are help, not decisions)
   → validate_mapping() checks on all rows: parse rate, missing values, events per case
   → normalise()        writes events.parquet with standard columns
```

**The standard event table (`events.parquet`)** that every later step reads:

| Column | Meaning |
| --- | --- |
| `case_id` | text |
| `activity` | text |
| `ts_start` | timestamp in UTC; NULL where the original could not be read |
| `ts_end` | timestamp in UTC, or NULL if no end column was mapped |
| `ts_start_raw` | the original timestamp text, for showing bad examples |
| `resource` | text, or NULL |
| `attr_*` | every other column, unchanged |

Rows with problems are **kept on purpose**: the Day 2 quality gate must count them
to calculate the confidence score.

---

## 4. Troubleshooting

| Problem | Fix |
| --- | --- |
| `streamlit` or `pytest` not recognised | Environment not active: run the activate command |
| `ModuleNotFoundError: app` | Run commands from the project folder, not from inside `app/` |
| Upload stops at 5 GB | Use the **Use a file on this computer** tab instead |
| Out of memory on a big file | Lower `DUCKDB_MEMORY_LIMIT` in `.env` (DuckDB then uses disk) |
| Dates read wrongly (day and month swapped) | Choose the correct date format on the mapping screen |
| `pm4py` fails to install | It is optional: install the app with `pip install -r requirements.txt`; page 3 then says the discovered model is not available |

---

## 5. Licence note on PM4Py

PM4Py is licensed **AGPL v3**. If it runs inside an app you sell or host for
clients, that app must be open-sourced, or a commercial PM4Py licence bought.
This project therefore computes every number (process map, variants, hours, waiting,
rework, report) in DuckDB SQL; the tests use PM4Py to cross-check the results.

The app is used **internally**, so PM4Py also draws one optional view: the process
model discovered with the Inductive Miner (section 29). It is imported only by
`app/core/pm4py_model.py`, only when `pm4py.enabled: true`. **Before the app is sold
to or hosted for clients, set `pm4py.enabled: false`** (or buy a commercial licence).
Confirm the licensing position with your company before shipping.

---

## 6. Real-world data: how the app handles messiness

Real logs are never clean, and every recording tool (ERP export, ticket system,
desktop capture, clickstream) produces a different shape. The app is built so
that **upload never blocks on messy data; it measures it**:

| Situation | What the app does |
| --- | --- |
| Unrelated rows (other apps, system noise) | Optional filters on page 1: leave out rows without a case or activity, or keep only rows where a column has chosen values. Left-out rows are always counted and reported |
| Missing values, odd counts | Warnings on page 1 with the fix; the quality gate (page 2) scores them |
| ID codes next to readable names (`task_id` vs `task_name`) | Readable names are preferred for the activity |
| Same step recorded many times in a row | Optional "Merge repeated steps"; the quality gate suggests it when repeats exceed 30% |
| Personal data, including half-anonymised columns | Detected and pre-selected to be left out of the converted file |
| Times with offsets (`+05:30`, `Z`) | Converted using their own offset; the chosen timezone applies only to times without one |

Only real blockers stop conversion: a required role not chosen, one column used
twice, no rows left after filters, or a date format that is clearly wrong.
All new thresholds live in `config/settings.yaml`.

---

## 7. Multi-file datasets

Recording tools often export one file per day, per user or per machine. The app
reads such a **dataset** as ONE event log.

| What you can load | How |
| --- | --- |
| A folder | **Use files on this computer** → enter the folder, e.g. `C:\data\exports`. All supported files inside it (and its subfolders) are read. Hidden files and Office lock files (`~$...`) are skipped |
| A pattern | Same tab, e.g. `C:\data\events_2026_*.parquet` |
| Several files | **Upload file(s)** → select them all at once |

- **One format per dataset.** A folder with both CSV and Parquet files is refused
  with a message that says how many of each it found. At most `ingest.max_files`
  files (5,000 by default) are read as one dataset.
- **Files with different columns** are combined by column name. When a file
  does not have a column, that column is empty for its rows. Page 1 lists such
  columns under **Files in this dataset** ("`resource` is empty or missing in 3 of
  57 files": a column that exists but is never filled looks the same).
- **Overlapping exports** (the same event in two files) are found by the
  duplicates check, which ignores the file name and says how many events appear
  in more than one file. The mining engine counts such events once.
- **Partition folders** such as `year=2025/month=01/` become columns.
- **If a column has incompatible types in different files** (e.g. a number in one
  file, a list in another), loading stops and names the column, so the export
  can be fixed.
- **`_ingest_file`** is added to every row: the name of the file it came from
  (never the full folder path, which can contain user names; when files share a
  name, the path below the dataset folder is used). It is copied to
  `events.parquet` as `attr_ingest_file`. With 2–50 files you can choose it under
  **Only keep rows where this column…** to analyse only some files.

Create a test dataset with one file per day (or `--split-by month`, and
`--format csv` for CSV files). `--out` is then a folder, and the ground truth is
written into it as `truth.json`:
```bash
python scripts/generate_synthetic.py --cases 20000 --split-by day --out data/samples/daily_dataset
```

---

## 8. Mining engine

`app/core/mining.py` computes everything the process map and the report need,
from `events.parquet`, in DuckDB SQL (tested: 300,000 cases / ~1.8M events in a
few seconds). It looks at the same events as the quality gate: rows with a case,
an activity and a realistic time, exact duplicates removed.

**All durations are in hours** (decimals, e.g. `36.5`). `format_hours()` turns them
into words for people: under 1 hour → minutes ("30 min"), under 48 hours → hours
("30.0 h"), under 14 days → days ("3.2 days"), otherwise weeks ("5.1 weeks").

| Analysis | What it returns |
| --- | --- |
| `kpis` | cases, events, activities, resources, first and last event, case duration (median, 90th percentile, mean), median events per case, whether end times exist |
| `dfg` (the map) | boxes (`nodes`: events, cases, median time inside the step when end times exist) and arrows (`edges`: how often one step directly follows another, in how many cases, median / 90th percentile / mean wait). Overlapping steps count as a wait of 0; their number is `overlaps` |
| `variants` | the most frequent paths with cases, share, cumulative share and median duration; also the total number of paths and how much the top 10 cover |
| `bottlenecks` | the slowest arrows by median wait and, with end times, the slowest steps. Only results seen in at least `min_support_cases` cases are ranked |
| `rework` | steps done more than once in a case (cases, share, extra occurrences), direct repeats (A → A) and back-and-forth (A → B → A) |
| `resources` | events and cases per resource and handovers between resources. Says "Not available: No resource/user column was mapped." when there is none |
| `trends` | cases started per month and their median duration |

**Start and end nodes.** Two artificial boxes, `▶ Start` and `■ End` (names in
`config/settings.yaml`), connect to each case's first and last step, so the map
shows where cases begin and finish. Their arrows have counts but no wait.

**Support.** Every result carries its number of cases. Results based on fewer than
`mining.min_support_cases` (30) cases are flagged `low_support`.

**What the quality band allows** (`mining.band_rules`). `run_mining()` returns
every section as `{available, reason, data}`:

| Band | Shown |
| --- | --- |
| High | everything |
| Medium | everything, with the label "Results shown with caveats" |
| Low | KPIs, map and variants only ("Exploratory only") |
| Not feasible | nothing; nothing heavy is computed |

A hidden section always says why: "Not available: data quality is Low."
The result is saved as `mining.json` in the job folder (`save_mining`).

**Map simplification** (`simplify`), used by the map sliders:
1. *Activities slider* (`default_activity_coverage`, 100%): keep the most frequent
   steps until they cover this share of all events, never more than `max_nodes` (40).
2. *Arrows slider* (`default_edge_coverage`, 90%): keep the most frequent arrows
   until they cover this share of transitions.
3. Every box shown keeps its most frequent incoming and outgoing arrow, so no box floats.

It also reports what is hidden (activities, arrows, share of events and
transitions shown). `to_dot()` turns the map into Graphviz DOT text that
Streamlit draws in the browser (no Graphviz install needed), in a frequency view
(counts) or a time view (waits; the slowest `highlight_slowest_share` of arrows,
10%, are drawn in orange).

PM4Py is used only in `tests/test_mining.py` to cross-check the map counts (see the
licence note above); it is never imported by the app. The performance test is
skipped by default; run it with `pytest -m slow`.

---

## 9. Page 3: Process map

Open **3. Process map**, pick a job that passed page 2, and click **Run analysis**.
The result is saved as `mining.json` in the job folder and reused next time; click
the button again to re-run. If the quality check runs again later, the page asks
for a new analysis instead of showing old results.

**How far you can trust it.** A banner at the top shows the quality band.
*Not feasible*: no map, only the top 3 fixes from page 2. *Low*: map, headline
numbers and variants only, marked "Exploratory". *Medium*: everything, with caveats.
*High*: everything. A hidden tab always says why it is hidden.

**Map controls**

| Control | What it does |
| --- | --- |
| Show: Frequency / Time | What the boxes and arrows say (see below) |
| Activities shown | Keeps the most frequent activities until they cover this share of events (at most 40 boxes) |
| Arrows shown | Keeps the most frequent arrows until they cover this share of transitions. Every box keeps at least one arrow in and one out |

The caption under the map says how much is hidden ("Showing 12 of 15 activities
(97% of events) and 20 of 31 arrows (90% of transitions)"). **Download map (DOT)**
saves the drawing as Graphviz text.

**Reading the two views**
- **Frequency**: each box shows how many times the step happened; each arrow,
  how many times one step was directly followed by the other. Thicker = more often.
- **Time**: each box shows the median time spent inside the step (only when the
  data has end times; otherwise its count); each arrow, the median **wait**
  between the end of one step and the start of the next in the same case. The
  slowest 10% of arrows are drawn in orange and listed under the map.

**Other tabs**: Variants (most common paths and a coverage curve), Bottlenecks
(slowest transitions and steps), Rework (repeated steps and back-and-forth),
Resources (who does the work and handovers), Trends (cases started and median
duration per month). Results seen in fewer than 30 cases are marked "low support".

---

## 10. Fact store and confidence badges

`app/core/facts.py` turns the quality and mining results into **facts**: every
number the report shows, saved as `facts.json` in the job folder. Each fact has:

| Field | Meaning |
| --- | --- |
| `id` | `F001`, `F002`… The same data always gives the same IDs |
| `label`, `value`, `unit` | e.g. "Median case duration", `497.3`, `hours` |
| `support` | how many cases are behind the number |
| `ci_low`, `ci_high` | 95% confidence interval, for medians of durations and waits |
| `stability` | for rankings: share of random half-samples in which the item stays in the top list |
| `badge` | **High**, **Medium** or **Exploratory** |
| `source` | how it was computed, e.g. "median of case durations, 10,907 cases" |

**How the confidence is measured**
- *Confidence interval (bootstrap)*: the durations or waits behind a median are
  resampled 200 times; the interval holds the middle 95% of the resampled medians.
  A narrow interval means the median would barely move with other cases.
- *Stability*: cases are split into 64 groups and the ranking is rebuilt on 20
  random halves. A top path that stays in the top 10 in every half has stability 1.0.

**Badges** (`facts.badges` in `config/settings.yaml`):
- **High**: at least 300 cases, an interval no wider than 25% of the value, and stability of at least 0.9.
- **Medium**: at least 30 cases.
- **Exploratory**: fewer cases.

Facts with fewer than 30 cases are not shown at all, except the facts about the
data itself (score, band, events, cases).

## 11. Report (HTML, Excel, PDF)

Open **5. Report**, pick an analysed job and click **Generate report**. The job
folder then holds `report.html`, `report.xlsx` and `facts.json`. The page shows a
preview and download buttons. If the analysis is re-run afterwards, the page asks
you to generate the report again.

The report has 14 sections. They follow the business order of the steps when it is confirmed, saved
or comes from a template. Otherwise the steps are sorted by cases reached, and the header says so.
1. **Key numbers**: cases, step runs, events, participants, applications, active hours, median and
   90% case duration, and cases reaching the last step (only with a trusted order).
2. **Where the data comes from**: rows and active hours from the file to the analysis (left out by the
   row filter, without case, without activity, in scope). The lines add up to the rows in the file.
3. **Executive summary**: at most 5 findings and 3 recommendations, in hours, cases and shares.
4. **Process map and steps**: the map and one row per step (cases reached, runs, events, active
   hours, share of hours, hours per case, median active time per run, top applications, rework,
   waiting). Steps without a stage are listed under the table and are not drawn.
5. **Time by stage and application**: hours by stage, a step × application matrix with totals, and
   hours by application.
6. **Inside each step (Level 4)**: a summary and the sub-steps with their share of the step's hours.
7. **Flow and conformance**: only with a confirmed, saved or template order.
8. **Waiting between steps**: pairs with at least `report.min_cases` cases and a median of at least
   `report.min_wait_minutes`, ranked by total waiting hours.
9. **Rework**: returns to a step after `report.rework_gap_minutes`. Quicker returns are quick
   switching, which is counted but not badged as rework.
10. **People and handovers**: participants masked as P01, P02 … Names are never shown.
11. **Applications and switching**
12. **Trend** per ISO week. Partial first and last weeks are flagged and never compared.
13. **Variants**: the count, the top-10 share and the top 5 paths.
14. **Appendix** (collapsed): calculation checks, definitions, data quality, mapping, settings,
    application mapping, AI provenance and every fact.

Every number comes from one place, `app/core/metrics.py` (`metrics.json`). Its definitions are in
`docs/METRICS.md`. Durations print as "38 s", "4.2 min", "5.3 h" or "3.4 days" (`app/core/fmt.py`),
never as "< 1 min".

**The report checks its own numbers.** `app/core/report_checks.py` recomputes every key number with an
independent query and compares it with the report (`checks.json`; table in the appendix). The header
says "All N calculation checks passed". If anything differs, the title starts with
"DRAFT — K checks failed" and the failed checks are listed at the top.

To check a real job yourself against its original file, recomputed with pandas:

    python scripts/verify_report.py <job_id>

It prints the report value, the recomputed value and ✓ / ✗ for the key numbers, the scope lines, the
hours, cases and runs of every step and the hours of every application. Exit code 0 means everything
matches. It never prints case IDs, users or window titles.

- **Time columns**: map *Active time*, and optionally *Wait time* and *Turnaround time*, in
  seconds on page 1. Idle time is never added to wait. Waiting share = wait ÷ turnaround time.
- **Process map**: drawn in the browser by the pinned, bundled Viz.js library
  (`report.viz_js_mode: inline`). The HTML works offline. The step table lists everything when
  drawing is switched off.
- **PDF**: open `report.html` in a browser and use **Print → Save as PDF** (A4).
- **Excel**: Summary, Key numbers, Data scope, Steps, Stage hours, Step x application, Applications,
  Waiting, Rework, People, Trend, Variants, Data quality, Facts, Checks and Mapping, plus Level 4,
  Conformance, Switching, Milestones and the business-definition sheets when they apply. Numbers are
  stored as numbers.
- **Settings** (`config/settings.yaml`, `report:`): `min_cases` 30, `min_wait_minutes` 60,
  `rework_gap_minutes` 30, `top_n` 10, `collapse_appendix` true, and the check tolerances
  `check_hours_tolerance` (0.05 h) and `check_share_tolerance_pp` (0.05 percentage points).
- **Screen labels** (Level 4): window titles lose the application part, person-like segments
  (1–4 capitalised words with no word from `screens.ui_vocabulary` / `level4.ui_words`), IDs, numbers of
  4 or more digits, dates and e-mails. "View Worker - Razumovs - Workday" becomes "View Worker", and
  "Prod Tracker 2026.xlsx" becomes "Prod Tracker.xlsx".

## 12. Run everything from the command line

`scripts/run_pipeline.py` runs the whole app for a file, folder or pattern,
without the user interface. It creates a job, reads, profiles, maps, validates,
converts, checks quality, analyses and writes the report:

```bash
# One file, columns chosen automatically
python scripts/run_pipeline.py --input data/samples/clean_small.parquet --auto-map

# A folder of daily exports: one app only, rows without a case left out, repeats merged
python scripts/run_pipeline.py --input "C:/exports/daily" --auto-map --drop-missing-case \
    --merge-repeats --filter-column app --filter-values SAP Outlook --timezone Asia/Kolkata
```

- Explicit columns (`--case`, `--activity`, `--timestamp`, `--end-timestamp`,
  `--resource`) override the suggestions. The date format is detected unless you
  give `--timestamp-format`.
- Columns that look like personal data are left out automatically, unless you
  use `--keep-sensitive`. `--exclude COL ...` leaves out more.
- `--data-dir PATH` writes the job somewhere other than `data/`.
- It prints every mapping check, then a summary: rows, rows left out, events,
  cases, merged steps, score and band, the fix list, the top 5 facts and the
  report paths.
- **Exit codes**: 0 = done; 2 = the input or mapping cannot be used (for
  example a wrong column or a date format that does not parse); 1 = unexpected error.
- `--no-ai` always writes the summary from the fixed templates; the summary line says which was used.


**XES event logs.** Convert an `.xes` or `.xes.gz` log (the IEEE standard used by many process
mining tools) to Parquet first, then load the Parquet file on page 1 or with `run_pipeline.py`:

```powershell
python scripts/xes_to_parquet.py log.xes.gz log.parquet
```

`concept:name`, `time:timestamp` and `org:resource` become `concept_name`, `time_timestamp` and
`org_resource`; trace attributes get the prefix `case_` (`case_concept_name` is the case ID). The old
misspelled name `scripts/xes_to_paraquet.py` only prints the new name.

## 13. AI summary and Langfuse

The executive summary, findings and recommendations can be written by an AI model.
With the AI off, or whenever its answer fails a check, the fixed-template summary
is used, so the report always works.

### Switch it on
In `.env`:
```
AI_ENABLED=true
LLM_BASE_URL=              # empty for OpenAI; e.g. http://localhost:11434/v1 for Ollama
LLM_API_KEY=...
LLM_MODEL=gpt-4o-mini      # any chat model the endpoint offers
```
Any OpenAI-compatible endpoint works: OpenAI, Azure OpenAI, Groq, Ollama and vLLM.
Endpoints without a JSON mode work too; the first JSON object in the reply is used.

### The guardrails, in plain words
1. **The AI only sees facts.** It receives a table of verified facts (ID, label,
   value, badge, number of cases). It never sees rows, case IDs, user names or
   other column values. Handover facts (which name people) are never sent.
2. **The AI never writes numbers.** It writes `{{F012}}` where a number goes; the
   app inserts the real value from the fact store.
3. **Every answer is checked by code** before anything is shown:
   - the JSON shape is right;
   - every fact it cites exists;
   - no digit is typed;
   - no causal wording ("caused by", "due to"…);
   - Exploratory facts are hedged ("may", "appears"…);
   - there are at most 6 findings and 4 recommendations.
   A broken answer goes back to the AI with the list of broken rules (up to 2
   retries); then the templates are used.
4. **Quality decides what the AI may use.** High quality: High and Medium facts.
   Medium: High facts only. Low or Not feasible: the AI is not called at all.
5. **No IDs or contact details reach the AI.** Before anything is sent, email
   addresses and runs of 6 or more digits (ticket or ID numbers) become `[id]`,
   so "PHR0243855 Case Tracking" is sent as "PHR[id] Case Tracking". If more than
   20% of the activity names sent contain such IDs, the AI is skipped and the
   templates are used ("activity names contain IDs or contact details"). Both
   limits are in `ai:` in `config/settings.yaml`.

**The provenance line** at the top of section 2 says how the summary was made:
"Written by AI from 24 verified facts; every number was inserted by the system
and checked (prompt version 3)", or "Generated from fixed templates (AI not used:
<reason>)". Every attempt and error is saved in `narrative.json` in the job
folder and listed in the appendix. Readers can rate an AI summary with 👍 / 👎 on page 5 (Report).

### Langfuse (optional, self-hosted)
Langfuse records every AI call (prompt, answer, scores) so the prompt can be
improved safely. Everything sent to it is masked first: email addresses, long
numbers and person-like names become `[redacted]`. If Langfuse is down or not
configured, reports carry on without it.

**Self-hosted Langfuse server ≥ v4.59.0 is required.** The installed Python SDK
(`langfuse==5.1.0`) uses only the Langfuse v4 public API (per the Langfuse SDK
documentation). If you run a Langfuse v3 server, either upgrade the server or pin
`langfuse` to a 4.x SDK. When Langfuse is enabled, the app asks the server for its
version once in the background, using `/api/public/health` with a 3-second timeout
and only that host. If the server is older, it logs a warning; it never blocks the app.

1. Start Langfuse with the official Docker Compose setup:
   ```bash
   git clone https://github.com/langfuse/langfuse.git
   cd langfuse
   # in docker-compose.yml, set TELEMETRY_ENABLED=false for the langfuse services
   docker compose up -d
   ```
2. Open http://localhost:3000, create a project, and copy its API keys into `.env`
   (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL=http://localhost:3000`).
3. Each report becomes one trace (session = job ID), with these scores:
   `schema_valid`, `citations_valid`, `numbers_grounded`, `causal_language_ok`,
   `hedging_ok`, `fallback_used`, `attempts`, and later `user_feedback`.

### Improving the prompt safely
The prompt lives in `app/prompts/` (the local fallback) and in Langfuse as
`pm-report-writer`. Reports use the version labelled `production`.
1. Edit the files in `app/prompts/`, then upload them as a new version. The new
   version gets **no** label:
   `python scripts/push_prompts.py`, which prints the new version number.
2. Evaluate that version on three known datasets (clean, dirty, tiny):
   `python scripts/evaluate_narrative.py --prompt-version <N>`
   It prints the six scores per dataset and the fallback rate, and exits with 1
   if an accepted answer was not grounded.
3. Only if it is at least as good, move the `production` label to version N in
   the Langfuse UI. That step is deliberately manual.

## 14. Application mapping

Desktop-recording exports often have an application column in which some values
are real names ("ServiceNow", "Excel") and others are **masked**: hashes such as
`8bea6433fd6ef458baacbeb02165d7ac`, UUIDs, or values without letters (patterns in
`apps.masked_patterns`). You usually know, or can find out, which application each
masked value stands for.

### Name them on page 1
Choose the **Application** column in the mapping (it is suggested when the column
is called e.g. `Application_name` or `app`). Optionally choose a **Window title**
column (`simplified_title`, `window_title`): it only helps you recognise each
application and is never converted. The **Application mapping** table then shows:
- one row per recorded value, with its events and users, whether it is masked, and
  up to 3 example window titles (cleaned of `#hex#` tokens, emails and long numbers);
- masked values shortened to 8 characters (the full values are in the expander, for copying);
- editable **Real name**, **Category** and **Notes**, pre-filled from earlier mappings.

The header says how much is still unnamed ("6 applications · 3 masked · 1 of 3
mapped (29% of events still unnamed)"). Click **Save mapping** before **Validate
and convert**. The checks then warn about masked applications without a name, and
note when two recorded values share one real name (they are combined).

### One shared map: `app_identity_map.csv`
Names are saved in `<data folder>/app_identity_map.csv` (`raw_value, real_name,
category, notes, updated_at`). A masked value means the same application in every
export, so the map is **shared across all datasets**. Saving never removes
applications from other datasets.
- **Download mapping (CSV)** exports it.
- **Import mapping (CSV/Excel)** merges a file with the same columns, or a plain
  2-column file (`raw_value, real_name`). It first shows what would be added,
  updated and ignored, and saves only after **Confirm import**.

### What happens on conversion
`events.parquet` gets an `application` column with the **shown name**: the real
name if mapped, the recorded value if it is readable, otherwise **"Unnamed app N"**
(numbered by events, most first). The recorded masked value is not stored, so raw
hashes and window titles never leave the mapping screen. They do not reach events,
facts, the report or the AI. Page 3 has an **Applications** tab (category, events,
cases, share); the report appendix lists the application mapping used, and the
Excel file has an **Applications** sheet.

### Command line
```bash
python scripts/run_pipeline.py --input export.parquet --auto-map --application Application_name \
    --app-map names.csv            # add --save-app-map to keep the names in the shared map
```
The run prints "Applications: N (M masked, K mapped)". Test data:
`python scripts/generate_synthetic.py --cases 2000 --apps --out data/samples/apps.parquet`
adds `Application_name` (3 of 6 applications masked) and `simplified_title`.

## 15. Simple process map

### Why desktop data often has no fixed order
Desktop and task-mining data record what people click, not a workflow engine's
steps. People switch between screens, come back, and do steps in whatever order
suits them. On such data, for almost every pair of steps, A → B and B → A happen
about equally often. A map of every transition is then "spaghetti", and a map
that shows one sequence would be invented.

So page 3 opens with a **simple map**: the **intended order** of steps, left to
right and grouped in stages. The data then shows how many cases reach each step,
how often cases really follow that order, and where people go back and forth.
The old map is still there: switch **View** to **Detailed (all transitions)**.

### Order strength
For each pair of steps, the app measures how one-sided their order is:
`(A→B − B→A) / (A→B + B→A + 1)`. That is 1 when A always comes first, and 0 when
both orders are equally common. The **order strength** is the average over all
pairs, weighted by how often each pair occurs:

| Score | Label | Meaning |
| --- | --- | --- |
| 0.5 or more | **Strong** | cases mostly follow one order; the map shows how the work flows |
| 0.2 to 0.5 | **Partial** | a main order exists, with many exceptions |
| below 0.2 | **Weak** | no fixed order; read the map as the intended order and how many cases reach each step |

The thresholds are in `reference_map.order_strength` in `config/settings.yaml`.

### Suggested or confirmed order
Until someone sets it, the order is **suggested from the data**: each step is
placed by its median first position in a case. With a **Stage / group** column
(mapped on page 1, e.g. `phase`, `stage`, `level2_process_name`), stages are put
in order too, and each step is drawn inside its stage. Steps reached by fewer
than 2% of cases (`reference_map.min_case_share`) go into one grey **Other
steps** box; their names are listed under the map.

To change it, open **Edit the step order** under the map:
- change positions or stages, untick **Show on map** to move a step to Other steps,
  and set the process name;
- tick **This order is confirmed by the business** once the process owner agrees;
- click **Save order**. **Use suggested order** goes back to the suggestion.

The order is saved per file layout (`<data folder>/process_orders/`), like column
mappings, so the next export with the same columns uses it automatically. Saving
does not re-run the analysis.

### Reading the map
The map is laid out to be read on one screen (up to about 12 steps plus Other
steps fit a 1,600-pixel-wide window):
- **Title (top)**: the process name, number of cases, the period, whether the order
  is suggested or confirmed, and the order strength. The map explains itself, even
  full screen or downloaded.
- **With a stage column**, each **stage is a column** (a box with the stage name in
  bold), stages run **left to right** in order, and the **steps of a stage run top
  to bottom** in their intended order.
- **Without a stage column**, the steps wrap into **rows of at most 5** and snake:
  row 1 runs left to right, row 2 right to left, and so on, so every arrow stays
  short (`reference_map.max_steps_per_row`).
- **Box** = a step. "**N cases · P%**" = how many cases reach it; darker = more
  cases. Names wrap at about 22 characters (`reference_map.label_wrap`).
- **Solid arrow** = the intended next step. Arrows carry no numbers: they show the
  agreed order, not how often it happened.
- **Dashed red ⇄ line** = people go back and forth between these two steps, with
  how often ("⇄ 969"). Lines are drawn between any two steps, also across stages.
  Tick **Show back-and-forth** to show them.
- **Other steps** (far right, grey dashed box): rarely reached steps, with how many
  there are, the share of cases, and up to 4 names ("+N more").
- **Legend (bottom)**: "Box = step · % = share of cases that reach it · darker =
  more cases · arrow = intended order · dashed ⇄ = people go back and forth".
- Under the map, short sentences say what it means, for example: "These steps are
  not done in a fixed order: only 4.1% of cases follow it."
- A badge says whether the order is **suggested from data** or **confirmed by business**.

**Download map (HTML)** saves the map as one web page that opens offline in any
browser (Viz.js is embedded, as in reports; `report.viz_js_mode`) and prints to PDF.
**Download map (DOT)** saves the Graphviz text.

The report's section 4 shows the same map, sentences and order status, with the
Main transitions table below it. The Excel file has a **Process steps** sheet,
and the AI summary is told the order strength and whether the order is
confirmed, so it never describes a Weak order as a sequence.

**Command line:** `--stage COL` maps the stage column, and `--order order.json`
uses an order saved from page 3 (`--save-order` keeps it for this layout). The run
prints e.g. "Order strength: 0.09 (Weak) · in order: 4.1% of cases · order:
suggested". Test data without a fixed order:
`python scripts/generate_synthetic.py --cases 3000 --shuffle-order --out data/samples/shuffled.parquet`
(and `--stages` adds a `phase` column).

## 16. Application switches

With an Application column mapped (section 14), the app counts how often people
switch from one application to another while doing the process, between which
applications, and in which steps. It uses the readable application names, never
raw hashes.

### Definition
An **application switch** is two consecutive events (by time) of the **same case
and the same user** (the same case only, when no user column is mapped), where
the application is different and both applications are known. These do not count:
- a pause longer than `app_switching.session_gap_minutes` (30 by default) between
  the two events: that is a new work session, not a switch;
- events whose application is unknown (empty);
- rows left out on page 1 by your scope filters.

### Counted before repeated steps are merged
"Merge repeated steps" turns a run of events into one step, which would hide most
switches (Workday → Excel → Workday inside one approval becomes one "Approve"
step). So switches are counted when you click **Validate and convert**, on the
rows in scope **before** merging, and written to `app_switches.parquet` in the job
folder. The numbers are the same with or without merging. Jobs converted before
this feature say "Re-run 'Validate and convert' on page 1 to count application switches."

### Reading the results (page 3 → Applications → Switching)
- **KPIs**: total switches, switches per case (median), switches per 100 events
  (events before merging), and the share of cases with at least one switch.
- **One sentence**, for example: "People switch application about 2 times per case;
  the most common switch is Excel → Workday (3,051 times). Approve Request has the
  most switching: 2.0 switches each time it is done."
- **Switch map**: each box is an application ("category · events"). Each arrow is a
  switch from → to, with how many times; thicker = more often. Two-way switching
  shows as two arrows.
- **Switching by step**: switches into each step divided by how often the step is
  done. A high number means people jump between tools while doing that step,
  which is a good candidate for integration or automation. Steps seen in fewer
  than 30 cases are marked "low support".
- **Steps × applications**: the share of each step's events done in each
  application, and how many applications each step uses (5% or more of its events).
- **Back and forth**: A → B → A patterns within one work session.

The report shows the same under section 8, "Applications and switching". The
Excel file has an **App switches** sheet when switches were counted, and the
command line prints "App switches: N total · median M per case · top: A → B".

## 17. Privacy: what leaves your computer

**By default, nothing.** The app, your data and every result stay on this computer.
Anything that could leave has to be switched on deliberately, and the app tells you
what is on: the line under the page menu (and at the top of `run_pipeline.py`
output) reads, for example,
"🔒 Data folder: D:\…\data · AI: off · Langfuse: off · Usage stats: off".

| Component | What is sent | Where | Default | How to turn it off / keep it local |
| --- | --- | --- | --- | --- |
| The app (Streamlit) | Pages to your browser | This computer only (`127.0.0.1`); other computers on the network cannot open it | local only | `server.address` in `.streamlit/config.toml` |
| Streamlit usage statistics | Nothing | No anonymous statistics are collected | off | `browser.gatherUsageStats = false` in `.streamlit/config.toml` |
| Report map drawing (Viz.js) | Nothing: the library is embedded in `report.html` | — | inline (no network) | `report.viz_js_mode`: `inline` (default), `off` (tables only); `cdn` would load the library (not your data) from jsdelivr.net |
| AI summary | Only verified facts (labels, values, badges, case counts); IDs and emails masked as `[id]`; never rows, case IDs or user names | `LLM_BASE_URL` | off (`AI_ENABLED=false`) | Keep `AI_ENABLED=false`, or use a local model (`http://localhost:11434/v1`). An endpoint outside your network is refused unless `ALLOW_EXTERNAL_AI=true` |
| Langfuse tracing | Prompts, answers and scores, masked (emails, long numbers, person-like names become `[redacted]`) | `LANGFUSE_BASE_URL` | off (no keys) | Leave the keys empty. A server outside your network (e.g. Langfuse Cloud) is refused unless `ALLOW_EXTERNAL_LANGFUSE=true` |
| Langfuse version check | One request for the server version | `LANGFUSE_BASE_URL/api/public/health` only | only when Langfuse is on | Turn Langfuse off |
| Langfuse server (if you self-host it) | Its own usage statistics to Langfuse | langfuse.com | depends on the server | `TELEMETRY_ENABLED=false` in the Langfuse **server's** `.env` |

**What counts as "your network":** `localhost`, `127.x.x.x`, `::1`; the private
ranges `10.x.x.x`, `172.16–31.x.x`, `192.168.x.x`; and host names ending in
`.local`, `.internal` or `.lan`. Everything else is external. An empty
`LLM_BASE_URL` means OpenAI's own service, so it is external too. The decision is
made from the address as written; nothing is looked up on the network.

When an external AI endpoint is refused, the report uses the fixed-template summary
and its provenance line says why: "AI endpoint is outside your network (set
ALLOW_EXTERNAL_AI=true to allow)". All AI guardrails (facts only, `[id]` masking,
checks, band rules) apply whether the endpoint is local or external.

## 18. Data quality vs flow confidence

Two separate scores, both shown in the report (section 4) and on page 3:

- **Data quality** (page 2): is the data **clean**? It checks missing values, valid
  times, duplicates, enough cases and so on.
- **Flow confidence** (0–100): is there a **reliable order of steps**? It decides what
  the map, the facts and the AI may claim.

Clean data can still have no flow. On desktop and task-mining data, people do the
steps in any order. A real HR export scored **99 (High)** for data quality, while its
order strength was 0.09. The report now says, for example: "Data quality: 99 High ·
Flow confidence: 22 None (no fixed order in the data; no business order given)".

**How flow confidence is scored** (`flow_confidence` in `config/settings.yaml`):
- **Order found in the data:** from the order strength (0.5+ gives 80+, 0.2–0.5 gives
  50–79, below 0.2 gives below 40). It is weighted by how **stable** the suggested order
  is: the share of 20 random halves of the cases whose order agrees with the whole data
  (Kendall tau ≥ 0.8; steps with near-equal positions count as ties, not as an order).
- **Order confirmed by the business:** the order is trusted, so the band is at least
  Medium. The score then says how well reality follows it (the share of step pairs
  done in that order).
- **Template or AI order:** like confirmed, but **capped at Medium** until the
  business confirms it ("order not yet confirmed by the business").

| Flow band | Map | Sequence facts (paths, waits between steps, in-order share) | AI may describe a sequence |
| --- | --- | --- | --- |
| High (80+) | "Process flow", with arrows | normal badges | yes |
| Medium (60–79) | "Intended order", with arrows and conformance | at most Medium | only as the intended order |
| Low (40–59) | "Intended order (weak evidence)" | Exploratory | no |
| None (below 40) | "Step overview — no fixed order found": boxes grouped by stage, **no arrows**, sorted by cases reached | Exploratory, left out of the summary | no |

With flow band Low or None, the AI check also rejects sentences that use sequence
words ("then", "followed by", "after", "before", "next") about sequence facts. The
template summary says plainly that no path is described.

## 19. Finding a better reading of the data

A log may show no order simply because the **case** or the **activity** was chosen
the wrong way: a batch number instead of a request ID, or a status instead of a step.
On page 2, open **Is there a better way to read this process?** and click **Search**.
It tries other definitions of the same rows, on a seeded sample of up to 50,000 cases:

- **Cases:** the mapped case column; other ID-like columns (2–200 rows per value, at
  least half filled; never times, applications, window titles, the user column,
  numbers with decimals or personal-data columns); each of these per user; each per
  user and **work session** (a new session after a 30-minute pause); and user work
  sessions alone.
- **Activities:** the mapped activity, the stage column, and other short text columns
  (3–60 values, at least half filled).

For each pair it measures, after merging repeated steps: order strength, stability,
the share of cases with 2 or more steps, rows covered, and median steps per case. The
**confidence** (0–100) is
`0.5 × order strength + 0.2 × stability + 0.15 × multi-step share + 0.15 × coverage`
(weights in `reading_finder.weights`). Two adjustments:
- order strength and stability are scaled down when fewer than half the cases have
  2 or more steps, because they would rest on a handful of transitions;
- stability counts in proportion to the order strength, because a stable ranking of
  an order that barely exists is not evidence of an order.

The table marks the current reading. Then:
- **If no reading reaches 40**, the page says: "No reading of this data shows a reliable
  order (best: X of N tried). The order must come from the business — see 'Process
  order' on page 3."
- **Otherwise**, **Use this reading** creates a **new job** with the better case and
  activity. The old job is kept. The new job is converted and quality-checked
  straight away, and `job.json` records where it came from (under `reading`).
  Session-based cases get a derived case ID: `<case> | <user> | <session number>`.

Results are saved as `readings.json`. On the command line, use
`python scripts/run_pipeline.py ... --find-readings` to print the table. It runs in
DuckDB only: about 30 seconds on 2.5 million rows (`pytest -m slow`).

## 20. Where the process order comes from (confirmed, template, AI, data)

At the top of page 3's simple map, **Order source** lists what is available, most
trusted first, each with a badge:

| Source | Badge | What it is |
| --- | --- | --- |
| Confirmed | green "Confirmed by business" | an order saved with "This order is confirmed by the business" ticked |
| Saved | orange "Saved: …" | an order saved but not yet confirmed |
| Template | blue "Template: HR onboarding (87% of events matched)" | a process template that matches the step names |
| AI | purple "AI suggestion — needs confirmation" | an order the AI suggested from the step names only |
| Data | orange "Found in data" | the order suggested by the data itself |

- **Templates**: the app ships none. A built-in template is not evidence about your
  process, so step names and order come from your documents (step 2, Business
  context) or from the data. The tests keep their own sample templates in
  `tests/sample_processes/`.
- **AI suggestion** (only when the AI is switched on and allowed): click **Ask the AI
  to suggest an order**. Only step and stage names are sent, never counts or data,
  masked like everything sent to the AI. The answer must use every step exactly once
  and invent none; otherwise it is retried, then dropped.
- **Confirmed**: edit the order on page 3, or **Download order (CSV)** (columns
  position, stage, step), have the process owner check it, and **Upload order (CSV)**.
  Tick **This order is confirmed by the business** before saving.

## 21. How reality compares

Under the simple map, a **How reality compares** box sets reality against the chosen
order, in plain sentences:
- how many cases follow the order exactly, and the share of step pairs done in
  that order;
- the steps most often done **earlier than intended** ("Hire Processing is done
  before Offer Processing in 412 cases");
- **mandatory** steps that are missing (when a template or the business marks steps
  as mandatory);
- the share of cases that reach the last step.

With flow band None it adds: "The data does not show a fixed order. The map shows
which steps occur and how often, grouped by stage." These numbers are also facts in
the report, with badges that follow the flow band.

## 22. When columns are missing

Exports differ: some have a case ID, process levels, applications and actions;
others have only some of these. The app works with whatever exists, says what each
missing column costs, and offers honest fallbacks. It never invents structure
without saying so.

### What this data supports (capability matrix)
After the mapping, page 1 shows **What this data supports**. The pipeline prints the
same list, and `job.json` keeps it (`capabilities`). Each feature is marked
✅ available, ⚠️ fallback in use, or ❌ not possible, with one plain line per gap:

| Feature | Needs | Without it |
| --- | --- | --- |
| Process map, variants, waits, rework | case, activity, timestamp | a derived case (below) is used, with its caveat; with no case at all, they are not available |
| Resources and handovers | user column | not shown |
| Applications / application switches | application column (+ user) | not shown / counted per case |
| Stages on the map | stage column | grouped by a business order or a process template ("Stages from template: …"); without either, no stage boxes |
| Sub-process hierarchy | process level columns | missing levels are skipped (the later hierarchy view must skip them too) |
| Milestones | an action / completion column | not detected |
| Step runs | a step-run ID | estimated from time gaps |
| Active time and effort | an end time | estimated from gaps between events |
| AI summary | AI switched on and allowed | fixed templates |

### No case ID column: derive it
On page 1, choose **Case ID: Derive it** (or use `--derive-case` on the command line).
Every derived case ID is stored as a **salted hash**. The salt is `CASE_SALT` in `.env`
or, if that's empty, a random salt kept in the data folder, so raw ticket or employee
numbers never reach `events.parquet`, the report or the AI. How the case was defined is
shown on page 3, in the report header and Appendix, and as a fact ("Case = work session
(30-min gap)").

| Method | How it works | Caveat (flow confidence cap) |
| --- | --- | --- |
| **ID found in a text column** | **Find IDs in text columns** scans a sample of the window title and other free-text columns for ticket numbers (`INC0012345`), codes (`PO-12345`), masked tokens (`#…hex…#`) and long numbers (`derived_case.id_patterns`). For each it shows how many rows have one, distinct IDs, rows per ID, and how many IDs span 2 or more steps (a case candidate). A row without an ID takes its session's ID when the IDs before and after it agree; otherwise it is left out and counted | at most **Medium** until you tick "The business confirms what this ID means" |
| **Work sessions** | user + a new session after a pause (30 min by default; optionally also at midnight) | at most **Medium**: "Cases are WORK SESSIONS, not business cases: the map shows how people work in one sitting, not the end-to-end business flow." |
| **Fixed time windows** | a calendar day or hour, per user or machine if you choose a column | at most **Low**: steps of different records on the same day are mixed |
| **Join with a system-of-record export** | upload `record_id, user, start_ts, end_ts` (from Workday, ServiceNow…). Events by that user inside a record's window get its ID (longest overlap wins); others are left out and counted | at most **Medium** |
| **Activity overview only** | no case at all | flow **None**: only activity counts, when work happens (hour, weekday, month), step runs, applications and application switches per user timeline. Map, variants, waits and conformance say "Not available: no case" |

The quality gate runs as usual on derived cases. The reading finder (page 2) also
offers IDs found in text as candidate readings, so you can compare them with the
mapped column.

### No activity column: screens
If there is no activity column but there is an application and a window title, tick
**Derive the activity from application + window title**. The title is cleaned (IDs,
numbers, `#hex#` tokens, emails and person-like names removed, first 6
words kept) and joined to the application name: "SAP Logon: SAP Easy Access Order".
**Names are protected:** a capitalised 2–3 word phrase is kept only if it does not look like a person name, is used by at least 5 users (`derived_activity.min_users_for_phrase`) on at least 200 events, and is not a value of a sensitive or person-like column; otherwise it becomes `<name>`, and "View Worker - *anything* - Workday" always becomes "Workday: View Worker <name>".
Screens with fewer than 200 events become "Other screen". **Activities are screens,
not business steps**, so flow confidence is capped at **Low** until a business
definition maps them to steps.

### Command line
```bash
python scripts/run_pipeline.py --input export.parquet --activity task --timestamp time \
    --resource user --window-title title --derive-case text_pattern --pattern ticket
python scripts/run_pipeline.py --input export.parquet --auto-map --derive-case work_session --session-gap 30
python scripts/run_pipeline.py --input export.parquet --auto-map --derive-case time_window --window day
python scripts/run_pipeline.py --input export.parquet --auto-map --record-file records.csv
python scripts/run_pipeline.py --input export.parquet --auto-map --overview-only
python scripts/run_pipeline.py --input export.parquet --auto-map --derive-activity
```

### What the business should provide for full results
- a **case / record ID on every event** (the request, ticket or employee record the work belongs to);
- **stage / process level names** (level 1–3), or confirm the stages on page 3;
- **application names** (or name the masked ones on page 1);
- **completion actions** (submitted, approved, closed), for milestones.

## 23. Business process definition

Runtime configuration: `DATA_DIR` selects the folder for jobs, saved definitions,
application maps and samples (relative to the project or an absolute path).
`DUCKDB_THREADS` controls DuckDB worker threads; `DUCKDB_MEMORY_LIMIT` sets its
memory budget. Use a temporary `DATA_DIR` for tests and release checks.

The **4. Business process** page turns the process as the business intends it into a
versioned definition, then places the data evidence on that definition. Start a
draft from the observed step order, a bundled process template, an SOP/process
document (when AI is enabled), or an Excel, CSV, YAML, JSON, or BPMN file. A draft
includes stages, steps, owners, systems, business rules, mandatory/optional flags,
SLAs, activity mappings, and allowed flows.

Edit the tables, validate them, and save the draft. Definitions are versioned under
`data/process_definitions`; only an approved version becomes the confirmed business
order used by the simple map. The evidence check is code-only and labels every claim
as **Supported**, **Partly supported**, **Contradicted**, or **No data**. Its thresholds
are in `evidence` in `config/settings.yaml`.

When AI is enabled, it may draft structure from document text or explain claims and
propose structured changes. It receives no event rows, counts, case IDs, users, or
raw application values. Its output is validated, and suggestions must cite
contradicted or partly supported claims. Nothing is changed until a person accepts
the suggestion; approval also requires the approver's name.

The **Final map** uses stage (or owner) swimlanes. Rounded boxes are steps, solid
borders are mandatory steps, dashed borders optional steps; solid arrows are next
steps, dashed arrows allowed loops, and dotted arrows optional skips. Evidence uses
green/amber/red/grey for supported/partly/contradicted/no-data items. Download DOT,
self-contained local-Viz.js HTML (print it to PDF), or BPMN 2.0 XML. The BPMN export
includes lanes, sequence flows, documentation and diagram positions and opens in
Camunda Modeler or bpmn.io.

## 24. Milestones from actions

Map the optional **Action / UI event** column (for example `activity_alias_name`) on
page 1. Suggestions require at least half the column to be filled and at least
three times as many distinct values as the activity column. Explicit mapping is
also available. Actions describe clicks inside a step; they are not process steps.

Before merging repeated steps, the app keeps only whole-word completion actions
(create, save, submit, approve and the other verbs in `milestones.verbs`). Defaults
require 300 events and three distinct users per cleaned action. Hash tokens,
emails, long digit runs and person-like values are removed or excluded. Raw action
columns are never copied into `events.parquet`. Without a mapped resource column,
the multi-user threshold cannot be met and the catalogue remains empty.

On page 3, open **Milestones (from actions)** in the Simple view. Filter by step,
tick useful milestones and give them business labels such as “Tax data created”.
Save the catalogue, or download/upload CSV. JSON downloads can be reused through
`--milestones`. Catalogues are saved atomically per file-layout signature; saving
recomputes the analysis. Confirmation of catalogue labels does not approve a
business process definition or turn its order into a confirmed business order.

**Confirmed sequences found in the data** lists chains whose links have at least
30 transitions and dependency of at least 0.8. Support counts cases containing the
whole chain, not the smallest link count. The **Milestone map** groups labelled
milestones by step and draws only qualifying one-way links. Repeats are merged;
equal timestamps do not establish a direction. Order stability uses half-samples
of cases, and the completion table measures the share of step runs containing a
milestone. The report and the Excel **Milestones** sheet use catalogue labels.

Coverage is the share of all cases recording at least two distinct milestones.
Below 50%, milestone flow confidence is capped at Medium. For example, a chain
recorded in 40% of cases says nothing about the remaining cases. The suggested
`data (milestones)` order is preselected only if its confidence band exceeds the
ordinary data order; business, template and AI order sources retain precedence.
Reading finder also compares milestone actions and first milestones per step.

AI may describe a verified chain as a sequence even when ordinary step order is
weak, but must cite its fact and use its labels. It never receives raw action
fields. Business definitions can select milestone labels for each mapped step;
the final map displays those labels. A forward milestone chain supports a defined
order, and a reverse chain contradicts it. Recheck evidence after catalogue edits.

```powershell
python scripts/generate_synthetic.py --cases 3000 --actions --shuffle-order --stages --out "$env:TEMP\milestone-log.parquet"
python scripts/run_pipeline.py --input "$env:TEMP\milestone-log.parquet" --auto-map --action activity_alias_name --stage phase --merge-repeats --no-ai --data-dir "$env:TEMP\milestone-check"
```

The synthetic chain “Create Tax Data → Create Benefits → Click Submit” appears
in 40% of cases, alongside noisy UI labels and a completion action used by only
one person. Thresholds are configurable under `milestones` in `config/settings.yaml`.

## 25. Sub-processes

Between steps there is often no fixed order, but **inside** a step the clicks are
usually far more consistent. The **Sub-processes** tab on page 3 (next to Process map)
shows how each step is actually done.

**Levels.** Map the optional columns on page 1: **Process level 1** (e.g.
`level1_process_name`), **Stage** (level 2, e.g. `level2_process_name`) and **Process
level 3** (e.g. `level3_process_name`). The step (activity) is the lowest level. The tab
starts with the tree level 1 → stage → level 3 → step, with cases and share at each
node; missing levels are skipped. Pick a **Stage** and a **Step** to drill down.

**Step runs.** A run is one execution of a step. When a **Step run ID** column is mapped
(e.g. `abstraction_instance_id`; it is only suggested when most of its values belong to a
single step), it defines the runs. Otherwise a run is consecutive events of the same
case, user and step, and a pause longer than 30 minutes starts a new run. An optional
**Active time (seconds)** column gives the effort per run. Runs need an **Action / UI
event** column. Actions are cleaned like milestone actions (no `#hex#` tokens, emails,
long numbers or person-like values) and captured before repeated steps are merged, so
the result is the same with or without merging. Per step, actions seen fewer than 100
times become "Other action", and the same action repeated back to back counts once.

**The standard way.** If the business has saved and confirmed a standard for a step, that
is the standard (badge "Confirmed by business"). Otherwise the most common path of two or
more actions is used (badge "Most common — from data"). You can pick a path from the
table, edit the standard (order and mandatory actions), confirm it and save it. Standards
are saved per file layout in `data/step_standards/`. Editing a standard re-compares only
that step.

**How runs compare.** Each run falls into the first class that fits: **Standard** (exactly
the standard path), **Standard + repeats** (the same actions, some repeated), **Extra
actions** (the standard path in order, plus other actions), **Different order** (the same
actions in another order), **Missing steps** (at least one standard action missing), and
**Other**. Until the standard is confirmed, these are "variation from the most common
way", never "incorrect". Once it is confirmed, the heading reads "Compared with the
business standard", and runs missing a mandatory action are counted as "Missing
mandatory actions".

**Step confidence.** The order strength of the actions inside the step and its stability
over half-samples of the runs give a badge: High (needs at least 300 runs), Medium or
Exploratory. The AI may describe the standard way ("is usually followed by") only for High
or Medium steps. It must use "variation" wording unless the standard is confirmed, and it
never names users.

**Variation by user** (collapsed) shows runs, median duration and the share following the
standard way for users with at least 30 runs. It shows how the same step is done by
different people; anonymised IDs stay anonymised. Use it to find training or tooling
needs, not to rate individuals.

The report has a "How each step is done" table in section 4, and the Excel file has a
**Sub-processes** sheet. Business process definitions link each step to a saved standard
(`standard_path_ref`). Approving the definition confirms those standards, and the
evidence check adds the claims "Runs follow the standard way" (≥ 60% Supported, 30–60%
Partly, below 30% Contradicted) and "Action X is done in every run".

```powershell
python scripts/generate_synthetic.py --cases 1500 --step-actions --out "$env:TEMP\steps.parquet"
python scripts/run_pipeline.py --input "$env:TEMP\steps.parquet" --auto-map --action activity_alias_name --no-ai --data-dir "$env:TEMP\steps-check"
```

Command line: `--level1`, `--level3`, `--step-run`, `--active-time` and `--standards
FILE.json` (`{step: {standard_path, mandatory_actions, confirmed_by_business}}`). The run
prints one line per step: runs, median actions, inside order strength, % standard.
Settings: `subprocess` in `config/settings.yaml` (`run_gap_minutes`, `min_action_count`,
`min_pair`, `top_paths`, `max_actions_on_map`, `min_user_runs`, `high_min_runs`).

## 26. Knowledge sources and combined confidence

Put everything the team knows about the process next to the data. On the **Business
process** page, the **Knowledge sources** tab takes:

| Kind | Files | How the text is read (all on this computer) |
|---|---|---|
| SOP / process document | .docx .pdf .md .txt | page anchors `[p.3]` |
| Transcript (Teams / Zoom, interviews) | .vtt .srt .txt .docx | `[00:12:41] Speaker A: …` |
| Video / audio walkthrough | .mp4 .mov .webm .m4a .mp3 .wav | speech with faster-whisper, screen text with ffmpeg + Tesseract |
| Notes / e-mail | .md .txt .docx .eml | subject, date and body; signatures and quoted replies removed |
| Process model | BPMN, Visio .vsdx, Excel definition | steps and connectors |
| Policy / SLA / RACI table | .xlsx .csv | one statement per row (step, role, SLA) |
| Expert answer | typed in the app or uploaded | question and answer |

**Local transcription and OCR on Windows** (optional; without them, video sources say what is
missing and the rest of the app works):

```powershell
pip install faster-whisper==1.2.1 pytesseract==0.3.13
winget install Gyan.FFmpeg                 # or: choco install ffmpeg
winget install UB-Mannheim.TesseractOCR    # or: choco install tesseract
```

The speech model (`knowledge.whisper_model`, default "small") is downloaded once and then
runs offline on the CPU. Nothing is sent to a cloud service.

**Reliability.** Each source has a reliability (editable): approved SOP 0.9, policy / SLA table
0.9, walkthrough video or transcript 0.8, expert answer 0.75, existing model 0.7, interview or
meeting transcript 0.6, notes / e-mail 0.5. A source dated more than 540 days ago counts × 0.8
and is marked "may be outdated". A source can be excluded and included again.

**Statements.** A source becomes statements such as "Offer before Vetting", "Hire is done by
the HR Partner" or "SLA 48 h". The AI may read the masked text and propose statements (only
when it is switched on). Without the AI, policy and model sources are read by fixed rules,
and you can add statements by hand. Code checks every statement: the quote (at most 25
words) must be found in the source, the anchor (page, timestamp, row) must exist, names must
match known steps, activities or applications (only a new step may have a new name), and an
SLA number must appear in its quote.

**Belief and conflicts.** Statements that agree raise the business belief:
1 − Π(1 − reliability), at most 0.99. Sources that disagree (opposite order, different owner,
system or SLA, mandatory vs optional) become a **conflict**. Conflicts are never decided by
the app: on **Conflicts & questions** a person picks a side (or "both are true: a variant"),
which is stored as an expert answer with reliability 0.95. **Update draft from knowledge**
proposes changes to the draft for statements with belief ≥ 0.6, each citing its sources, and
nothing is applied until you accept it.

**Questions for the experts.** Up to 10 questions are generated from fixed templates for:
open conflicts, documented rules the data contradicts, frequent activities no source
mentions, and elements nothing confirms. Answer them in the app, or download them as Excel,
send them to the client and upload the answers. Answers become expert-answer sources.

**Combined confidence.** Every element of the definition (step, flow, owner, system,
mandatory flag, SLA) gets a status from the business belief and the data verdict:

| Business sources | Data | Status | Colour |
|---|---|---|---|
| belief ≥ 0.6 | Supported | Confirmed | green |
| belief ≥ 0.6 | No data | Business only (the data cannot see it) | blue |
| belief ≥ 0.6 | Partly | Partly confirmed | amber |
| belief ≥ 0.6 | Contradicted | Documented ≠ actual (a finding, not an error) | red outline |
| belief < 0.6 or none | Supported | Undocumented practice (a finding) | purple |
| open conflict | any | Needs decision | orange |
| belief < 0.6 | No data / Partly | Unverified | grey |

Confidence per element = 100 × (0.5 × belief + 0.5 × data score), where Supported counts
1.0 × the badge factor (High 1, Medium 0.8, Exploratory 0.5), Partly 0.5 and Contradicted 0.
With no data, the belief alone counts, at most 70. The map confidence is the weighted mean
(steps × 2, flows × 1.5, others × 1), always shown with the number of elements per status.
On the **Final map**, colours follow these statuses; hover a box or an arrow to see its
sources ("S001 p.4, S003 [00:12:41]") and the data verdict. The report lists the top
"Documented ≠ actual" and "Undocumented practice" items as key findings. Pain points and
workarounds from sources are listed per step, marked "supported by data" when the step also
has one of the longest waits or the most application switching.

**Privacy.** Speaker names in transcripts become "Speaker A/B" (or the roles you give), and
e-mail senders are not kept. Text is masked (`payload_mask`) before any AI call, and the AI
receives only names besides the text. Sources appear in the report only as titles, kinds and
quotes of at most 25 words. Quotes are never sent to the report-writing AI, which may not
call a documented gap an error by people. Settings: `knowledge` and `combined` in
`config/settings.yaml`.

## 27. Using free AI providers

Without a local model you can use free cloud tiers. Each task goes to a list of providers in
order (`ai.routes` in `config/settings.yaml`), and the next one is used when a provider is
missing, over its limits or failing. When none can answer, the report uses the fixed templates
("AI not available: …").

| Provider | Key in `.env` | Default use | Free limits (check the console) |
|---|---|---|---|
| Google Gemini (AI Studio) | `GEMINI_API_KEY` | documents, transcripts, statement extraction; fallback for the rest | per project, shown in AI Studio |
| Groq | `GROQ_API_KEY` | report summary, order suggestion, evidence explanations | `openai/gpt-oss-120b`: 30 / min, 1,000 / day, 8,000 tokens / min, 200,000 tokens / day |
| OpenRouter (`:free` models) | `OPENROUTER_API_KEY` | last fallback | 20 / min, 50 / day (1,000 / day with at least $10 credit) |
| Cerebras | `CEREBRAS_API_KEY` | fast fallback after Groq (summary, order, evidence); also for documents | per model, shown in the Cerebras console |

1. Create the keys: Google AI Studio (aistudio.google.com → Get API key), the Groq console
   (console.groq.com → API keys), OpenRouter (openrouter.ai → Keys) and Cerebras
   (cloud.cerebras.ai → API keys). On OpenRouter, turn
   off "use my data for training" in the privacy settings if your client requires it.
2. Put them in `.env` with `AI_ENABLED=true` and `ALLOW_EXTERNAL_AI=true` (see `.env.example`).
   Keys stay in `.env`; the app never shows, logs or stores them.
3. Open **AI settings** (last menu entry). Per provider, click **List models**, pick one (Gemini:
   "flash" or "pro" models; OpenRouter: models ending in `:free`), then **Save**. Your choice goes
   to `config/ai_providers.local.yaml`, which is git-ignored. Click **Test** to send a tiny
   synthetic request.
4. Check everything at once: `python scripts/ai_check.py`. It lists models and tests each provider,
   and exits 1 when a task has no working provider. Then compare the summaries with
   `python scripts/evaluate_narrative.py` (add `--provider groq` to force one provider).

**Limits.** Before every request the app estimates the tokens (characters ÷ 4 plus the answer).
It skips a provider when the request is over its tokens per minute or context size, or when
today's counters would pass the requests or tokens per day. Counters are kept in
`data/ai_usage.json`. Gemini's day starts at midnight Pacific time; the other providers reset at
midnight UTC. Per-minute limits are respected by waiting, but never longer than
`ai.max_wait_seconds` (20 s).

**Errors.**
- A rate-limit answer (429): the app waits for `Retry-After` (if 20 s or less) and retries once,
  then moves on to the next provider.
- Server errors and timeouts: one retry.
- A rejected key (401 / 403): that provider is skipped for the rest of the session.
- An unknown model: "choose another in AI settings".

Long documents are cut to fit the first available provider: 60% of the smaller of its context
size and its tokens per minute.

**Data note.** On free tiers the provider may use prompts to improve its models. Do not send data
your client forbids. The app sends only verified facts, names and masked text, never rows, case
IDs or users. Each report's appendix and `narrative.json` record which provider and model wrote
the summary.

**Paid keys or Azure OpenAI later.** Use the same fields:
- **Another provider:** put the key in the matching variable and choose a paid model on the AI
  settings page.
- **One endpoint for everything:** set `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`, for example
  OpenAI, Azure OpenAI's OpenAI-compatible endpoint, or a local server. This endpoint is used
  first for every task.

## 28. Level 4 sub-steps

Inside a Level 3 step people click through many screens. Level 4 describes that work as 3–10
**sub-steps** in business language, such as "Find and open the HR case", "Email the candidate" or
"Maintain tax data". Each comes with a description of what is done and in which system. The
hierarchy reads Level 1 → Stage (Level 2) → Level 3 → Level 4. A Level 3 step is the `level3`
column when it is mapped, otherwise the task.

**What a click means: application + screen + click.** A click name alone is weak ("Search", "Send").
The **screen** gives it meaning, so map the window title on page 1 (role *Window title*, e.g.
`simplified_title`). The title is never stored or sent raw; only a cleaned screen label is kept:
- `#hex#` tokens, IDs, numbers of 4 or more digits and e-mails are removed;
- person names become `<name>`;
- a trailing " - <application>" or " | <application>" is dropped;
- clicks without a title (typed text) take the screen of the run they belong to.

**Typed text.** A click "name" with no UI word ("Hello", "Kind Regards") is stored only as
`<typed text>`; its content is never stored, shown or sent. The list of UI words is
`level4.ui_words` in the settings. The action catalogue (`action_catalogue.parquet`) then has one
row per Level 3 step, task, application, screen and click.

**The AI writes rules; code applies them.** Per Level 3 step the AI receives only the step, stage
and task names, the top screens with their top clicks, and lines
`U017 | application | screen | click | share %` (click names used by at least 3 users). It never
sees rows, case IDs or users. It returns sub-steps with a label, a description and **regex rules**
(application, `screen_regex`, `click_regex`). Code applies the rules to every click (first match
wins), so a new file or a new click name on a known screen is grouped automatically. Code checks
every answer:
- each regex compiles, has at most 200 characters and has no nested quantifiers;
- each regex is tested on all click names in a separate process that is stopped after 50 ms per name,
  so a runaway regex can never hang the app;
- rules of two sub-steps never match the same click;
- rules and explicit assignments cover at least 80% of the step's events, so at most 20% stays "Other";
- labels have at most 6 words, start with a verb, are business words (not a click name, not
  "Review in <app>") and are unique;
- descriptions are 1–2 sentences of at most 30 words;
- nothing contains digits, e-mails, `<name>`, person names or typed text.

A broken answer is sent back with the list of problems (up to 3 tries). After that, or when the AI
is off, sub-steps are formed per screen ("Work in Compose Email (ServiceNow)"). The source badge
always says why: "AI (model via provider) — not confirmed", "Confirmed by business", or "Rules — AI
not used: <reason>".

**It runs automatically.** With the AI on (and `level4.auto_build`), **Run analysis** on page 3 builds
the sub-steps for every step that has no confirmed or AI-built taxonomy yet, one step at a time and
within the provider limits, with progress shown. Confirmed steps are never changed by the AI.

**Summaries at every level.** These are written during the analysis and shown on page 3 and in the
report:
- per sub-step: its description plus its share of the work, median active time, main application
  and repeat share;
- per step: what happens, the usual order of the sub-steps (only when the step's confidence is High or
  Medium), where time goes and how varied the runs are;
- per stage: what it achieves through its steps and sub-steps;
- a process overview across the stages.

The AI writes them only from verified facts as `{{Fnnn}}` placeholders, through the report's checks
(no typed numbers, no causal words, no user names, no typed text). Otherwise fixed templates are
used.

**Maps.**
- The **storyboard** is the default view: stages as columns in order, each step as a box with the
  cases reaching it, its median active time and its numbered sub-steps with their share of the work
  (at most 6, then "+N more").
- The **Level 4 flow** of one step keeps the most frequent arrows until 80% of transitions plus any
  arrow used in at least 5% of runs, at most 12. A pair done both ways about equally becomes one
  dashed ⇄ arrow, and only the top 2 entry and exit arrows are shown. Labels are "% of runs", and
  **Show all arrows** draws every arrow.

**Editing and confirming.** Under **Edit Level 4** you can rename or describe sub-steps, add or remove
them, and edit their **rules** with a live preview ("matches N units / E events"); an invalid regex
is rejected with the error. The buttons are:
- **Save:** recomputes this step only.
- **Confirm as business view:** locks the step, so the AI never overwrites it.
- **Rebuild with AI:** replaces unconfirmed sub-steps.
- **Update with new actions:** assigns click names no rule covers.

The taxonomy, with its rules, is saved per file layout in `data/level4/<signature>.json`.

**Report and Excel.** Section 4 shows the process overview, the storyboard, each stage's summary, and
per step its summary, sub-step table (label, description, share, median active time) and flow map.
The Excel sheet "Level 4" includes the rules and descriptions, and "Summaries" holds the process,
stage and step texts with their source and model.

**Command line.** `--level4 reuse` is the default: during the analysis it builds steps that have no
AI taxonomy (AI on). `build` rebuilds every unconfirmed step, and `off` makes no AI calls. Try it with:

```powershell
python scripts/generate_synthetic.py --cases 600 --level4 --out "$env:TEMP\clicks.parquet"
python scripts/run_pipeline.py --input "$env:TEMP\clicks.parquet" --auto-map --merge-repeats
```

Settings: `level4` in `config/settings.yaml`, for example `min_coverage`, `max_other_share`,
`edge_coverage`, `max_edges`, `max_lines_per_step`, `regex_timeout_ms`, `auto_build`, `ui_words`
and `local_only`; and the AI routes `ai.routes.l4_bucketing` and `ai.routes.l4_summary`.

## 29. Discovered process model (PM4Py, optional)

Page 3 → Process map → **Discovered model (PM4Py)** shows a BPMN model mined from the data with
PM4Py's Inductive Miner. Next to the simple map (the intended order) it shows how the process really
runs, with its choices and parallel parts:
- boxes are steps; **×** = one of the paths (exclusive choice); **+** = paths done in any order or in
  parallel; arrows back = loops;
- **Fitness** (token-based replay, 0–1) and **Cases that fit fully** say how well the model replays the
  cases. A lower value means the model is simpler than what people do: rare paths are filtered out;
- **Download model (BPMN 2.0)** opens in Signavio, Camunda Modeler, bpmn.io or Visio.

By default, **Run analysis** computes it and stores it in `mining.json` and `process_model.bpmn` in the job
folder. Runtime depends on case count and the conformance samples; each PM4Py operation has its own timeout.
It uses the step runs (consecutive events of one step merged), so
it agrees with the map; only hashed case IDs, step names and times go to PM4Py, all on this computer.

Install: `pip install -r requirements-pm4py.txt` (already in the tested environment). Settings, `pm4py:`
in `config/settings.yaml`:
- `enabled` (true): switch the model on or off. **Off before the app is sold to or hosted for clients**
  (AGPL v3, section 5);
- `noise_threshold` (0.2): 0 keeps every path; higher gives a simpler model;
- `max_cases` (20,000): larger logs use a fixed sample of cases, so every run gives the same model;
- `fitness` (true): compute the fitness;
- `precision` (true) and `precision_max_cases` (200): precision, the slowest step, on a fixed sample;
- `alignment_max_cases` (200): cases sampled for the skipped, out-of-order and extra steps (1,000 always ran
  past the time limit on the HR data);
- `timeout_s` (120): the time limit of each PM4Py step;
- `build_on_run` (false): the model is built only with the **Build model** button on page 3; true also builds
  it with Run analysis.

**Conformance (page 3, same view).**
- **Precision:** token replay on a fixed sample of `pm4py.precision_max_cases` cases. 1.0 = the model allows
  only what really happens; low = it allows many paths nobody uses.
- **How well each case fits:** a histogram of per-case fitness (buckets 0–0.5, 0.5–0.8, 0.8–0.95 and 0.95–1).
- **Top skipped and extra steps:** from alignments on `pm4py.alignment_max_cases` sampled cases. When the
  alignments don't finish in time, token-replay problems per step are shown instead, and the page says so.
- All of it is saved in `pm4py_conformance.json`.

**When the data has no fixed order.** If the Inductive Miner puts (nearly) every step in parallel, optional and
repeatable, every case fits the model and it has nothing to show: page 3 says so plainly and skips the
alignments. This is what the HR onboarding data looks like.

**Check the cases against the step order (page 3, same view).** The button replays every case on the step
order page 3 and the report use (for HR, the onboarding template). It shows fitness, precision, the cases
that follow the order fully, and per step the share of cases that skip it, do it out of order (done, but never
where the order expects it) or repeat it. Saved in `pm4py_order_check.json`; the report appendix shows it.

**The approved process (page 4).** Once a definition is approved, **Check the approved process with PM4Py**
turns it into a model and replays the same case sample on it:
- **How the model is built:** steps in the approved order; a step that is not mandatory, or that an optional
  skip flow jumps over, may be skipped; steps with the same position may be done in any order.
- **Results:** fitness, precision, the cases that follow it fully, and per step the share of cases that skip it
  or do it at an unexpected point. The evidence table gets one extra column, "PM4Py: % of cases where this
  step is skipped"; the verdicts themselves are unchanged.
- **Steps not in the definition:** activities that no approved step covers are listed as "not in the approved
  process".
- These numbers measure recorded clicks: a step done outside the recorded apps (phone, paper, email) shows as
  skipped.
- All of it is saved in `pm4py_definition_check.json`.

**Report.** The appendix gets "Formal model (PM4Py)" only when a model was built for the job. It holds the
numbers above as facts; calculation check 15 compares them with the JSON files. The facts are never used in
the executive summary, and every other number in the report is unchanged.

**Time.** By default (`pm4py.build_on_run: false`) the model, fitness, precision and alignments are built only
with the **Build model** button on page 3; set it to true to build them with Run analysis. Each PM4Py step
(the model, precision and the alignments) runs in its own process and is stopped after `pm4py.timeout_s`
(120 s); a slow precision or alignment step never loses the model. On a log the size
of the HR job (2,814 cases, about 44,000 step runs), the build takes about 3 minutes:
- about 10 s for the model and fitness;
- about 40 s for precision on 200 cases;
- the alignments run on 200 cases (`pm4py.alignment_max_cases`); on 1,000 they hit the 120 s limit and token
  replay was used instead.

That is why the model is built on request: click **Build model** on page 3 when you need it.

Without PM4Py installed, or when switched off, page 3 says why and everything else works as before. A BPMN
file gets a diagram layout only when the Graphviz program is installed; otherwise BPMN tools lay it out
when they open it.

## 30. The order from your documents, sharper Level 4, and inside each step

**Why.** In the HR onboarding data a case holds only part of the process, so across a case the Level 3 order
is close to a coin toss: the data alone cannot say what the order is, and the built-in process template is not
evidence about *your* process. Inside one step, one person in one sitting follows real patterns. So:

**The template is kept but not used** (`templates.use_for_order: false`, `templates.use_for_definition_draft:
false`). Page 3 and the report no longer offer "Template: …" as the order, and page 4 no longer starts a
definition from a template. An old job whose page-3 pick was the template falls back to the next option with a
one-line notice; a saved order that came from the template is shown as "Saved (originally from the template,
not evidence)" and is never treated as confirmed; an approved definition that was drafted from a template keeps
its approval but shows "drafted from template — please re-check" on page 4 and in the report appendix.
Order options, most trusted first: confirmed · saved · **evidence** · AI · milestones · data.

**Proposed order (from your documents, checked against the data)** — page 4, Check tab (`order_evidence.py`,
`order_evidence.json`):
- the `order` statements of the knowledge sources you loaded (transcripts, SOPs, deck, SME answers), with their
  quotes, give the documented order (a topological sort). Steps no document places are put by the data and
  marked "position not stated in any document". Conflicts between documents are never decided: they are
  questions for the experts (Conflicts tab);
- the data says, per arrow, the share of **fully observed** cases (first event at least `order_evidence.edge_days`
  after the start of the data, last event at least that many days before its end) that do the first step first,
  with the number of cases: `supports` (≥ 0.65), `contradicts` (≤ 0.35) or `unclear` (in between, or fewer than
  30 cases);
- "Use this order as the draft" puts it into the editor; approving it makes it the confirmed order. Each step
  keeps where its position comes from (`document | milestones | data | sme`);
- without documents the label says "No business documents loaded. Order taken from the data (weak)";
- the report appendix has an "Order evidence" subsection, and calculation check 17 recomputes every share.

**Sharper Level 4** (`level4_quality.json`, the "Level 4 quality" panel in the Sub-processes tab, the report
appendix): per step the share of events in named sub-steps (target ≥ 90%), Other's share of hours (≤ 10%),
label rules (verb + object, ≤ 6 words, no app names, digits or person names), balance, users per sub-step,
units matching two rules (0), stability (same assignments on rerun) and app purity. A step that misses a target
gets a flag with the reason and the top 10 units behind it. New builds are refined by code: generic clicks
(`level4.navigation_words`) go to one sub-step "Navigate and search", or to the sub-step of their screen when
the screen is specific; labels that mean a sub-step name from your documents take that name (`named_from`
document | ai | sme | rule); the tail joins a sub-step of the same application by word overlap; what is left is
the review queue. A sub-step above `level4.split_hours_share` of the step with more than `level4.split_min_units`
units gets a split proposal, applied only when you accept it. **SME edits** (rename, merge, split, move a unit,
lock) are saved with your name and the date; locked sub-steps are never changed by an AI rebuild. Totals never
change: calculation check 13 compares Level 4 events, hours and cases per step with the step table.

**Inside each step** — page 3 tab (`inside_step.py`, `inside_steps.json`; report section 6). A run here is
consecutive clicks of one case and one step by **one person** with no pause over `inside_step.gap_minutes`
(the map's step run split by person and pause; the totals are the same). Per step: the Level 4 flow, the usual
patterns (top 5, the rest as "other patterns"), start and end sub-steps and how often a run ends with a completion
action, where the time goes (hours and median per run per sub-step), the work type (entry / lookup / review /
communication / navigation, from the words of the clicks, screens and sub-step names: `inside_step.work_types`),
rework inside the step, applications and switches per run, variation between people (masked; a user is flagged
when slower than the step's median by `inside_step.cv_flag` or when their main pattern is rarely used by others)
and candidates for SME review (facts only). Patterns need `min_runs` runs by `min_users` users. The PM4Py Level 4
model per step (fitness, precision, and conformance against the sub-steps your documents name) is built on
request with the button in the tab. Statuses: `observed`, `documented`, `sme_confirmed`. Calculation check 16
ties the patterns to the runs and the time split to the step's hours.

**The Level 3 PM4Py model** is built only with "Build model" by default (`pm4py.build_on_run: false`; it takes
about 3 minutes on the HR data and adds little on partial cases), or also with Run analysis when set to true. When its precision is below `pm4py.min_precision_to_show` (0.4) or it allows
every step in any order, it is not drawn by default:
page 3 and the report say "No stable order found in the data at Level 3 (precision …)" and a toggle shows it anyway.

The AI's only roles here: extracting statements from your documents (knowledge sources) and proposing sub-step
labels. Code computes every number; quotes stay in local files; no rows, names or IDs go to the AI.

## 31. Fixes from the live check (01c)

**Flow confidence.** For an order from the data (or its milestones) the band now follows the evidence of ALL the
data: when the order strength is below 0.3 or fewer than 25% of cases follow the order, flow confidence is Low
with a score of at most 50 (inside the Low range, 40–59; a score already below 40 stays "None"), and the reasons never say "the data shows a clear order" when the strength is weak. A
one-line formula is shown under the score on page 3 and in the report.

**Level 4 when the AI is busy.** Calls to each provider are paced (`ai.pace_seconds`); a 429 is retried up to
`ai.rate_limit_retries` times with backoff (Retry-After, else 2, 4 s), then the next provider, then the next step.
An AI answer that covers 60–80% of a step's events is kept (`coverage_by_ai`); the rest goes to the tail rules,
Other and the review queue. A rule-based build is refined like an AI build: generic clicks (Click Done, Navigate
to Details) go to "Navigate and search", labels are verb + object without application names, versions or file
extensions ("Update offers tool", not "Work in Offers_Team_Web_Tool_Testing-v1.xlsx"). One banner says "Level 4
built without AI for N of M steps (reason). Labels are rule-based." and page 4 offers **Retry AI for the steps
that fell back**.

**Step runs vs work sittings.** *Step runs* (map, steps table): consecutive events of one step in a case.
*Work sittings* (Level 4, sub-processes, inside each step): a step run split by person and by pauses over 30
minutes. A step has at least as many sittings as step runs; report check 18 checks that Level 4 and "Inside each
step" give the same number of sittings per step.

**Order words.** Summaries may say "fixed order", "final step", "drop-off" and the like only when the order is
confirmed by the business or the order strength is at least 0.6 (`order_claim_allowed`); otherwise the answer is
rejected and retried. Durations under 5 seconds are not sent to the AI.

**Variation between people** is labelled low (< 0.3), moderate (0.3 to `inside_step.cv_flag`) or high (flagged).

**PM4Py deviations.** When alignments are not run (the model allows any order, or the time limit), page 3 lists
the token-replay problems per step: *done but not expected* (missing tokens) and *expected but not done*
(remaining tokens), or "No deviations found by token replay".

**Two variant counts.** Page 2 counts *unique paths of events*; the report counts *distinct paths of step runs*
(repeated events of one step merged), so the report's number is smaller.

**Distinct counts on page 1** are exact for files under `ingest.exact_distinct_max_rows` (5,000,000) rows,
otherwise "about N".

**Dataset class.** Page 1 asks whether the data is *client* (default) or *public*. Client jobs send AI calls only
to providers with `private: true` in `config/ai_providers.yaml` (the hosted free tiers are `false`; a custom
`LLM_BASE_URL` on this computer or your network counts as private). `ai.allow_external_for_client: true` lifts the
rule and the sidebar shows a warning. Jobs saved before this change count as client.

## 32. No case ID: clickstream mapping (foundation)

Desktop and clickstream exports often have no case ID. The **no-case mode** works with them **without
inventing a case**: event order, the user or closeness in time is never a case. Case analysis only comes from
a real ID that passes the case-link tests. The case-ID mode is unchanged.

**On page 1**, choose Case ID → **No case ID (clickstream mapping)**: map the standard columns (`ts` is
required; `actor`, `app`, `screen`, `action`, `text`, `case_id`, `l1`–`l5`, `active_s`, `wait_s`, `run_id`,
`session_id` when the export has them), the rows in scope, the **eval** columns (kept only to measure accuracy,
e.g. the true case) and the **dataset class** (required: client data only goes to AI providers marked
`private: true`). **Convert for clickstream mapping** writes, in the job folder:

| File | What |
|---|---|
| `nc_events.parquet` | the standard events (times in UTC; actor and case_id as salted hashes; raw text only here) |
| `nc_scope.json` | rows in file = left out by the filter + unreadable time + in scope (checked) |
| `nc_capabilities.json` | what the data supports, measured: fill rates, case_id verdict, event counters, ID shapes (never values), label fill and consistency, time, `supports` |
| `nc_events_clean.parquet` | `screen_clean`, `action_clean` (`<typed text>`, `<person>`, `<other click>`), `is_generic`, `unit_id`; never drops a row |
| `nc_units.parquet` | one row per (app, screen, click): `U00001`… by events |

The menu section **Without case ID → Clickstream mapping** shows the scope, the capabilities and the units.
Pages 2–5 do not list no-case jobs. Settings: the `nocase:` block of `config/settings.yaml`.

**Command line** (`scripts/run_pipeline.py` is unchanged):

```
python scripts/nocase.py run --input "D:\exports\events_data_all_dates_with_app_names_6_1.parquet" --mapping config/nocase_mappings/hr_skan_nocase.yaml
python scripts/nocase.py profile --job <job id>        # also: ingest, clean, units
```

Inputs: everything page 1 reads, plus `.jsonl` and `.xes` / `.xes.gz`.

**Safety net:** `tests/regression/test_case_mode_baseline.py` reruns the case-ID pipeline on the golden file and
two fixed synthetic logs and compares every computed number with `tests/regression/baseline/`. Rewrite the
baseline (`python tests/regression/make_baseline.py`) only after a deliberate, reported change.

## 33. Test bench for the no-case mode (any clickstream or UI log)

`test_datasets/` (not in git, except each `dataset.yaml`) holds one folder per dataset: the ICC-like synthetic
files (copy them from `icc_like_test_data.zip`) and public logs you download by hand (Leno UI logs, labelled
AWT, BPI 2016 clicks, BPI 2019 / 2012 with the case ID hidden, optional SmartRPA and eCommerce). Each
`dataset.yaml` says where to get the files, how they map to the standard columns, which columns are **truth**
(evaluation only, written to `<job>/eval/`, never an input) and what the app should conclude.

    python scripts/bench.py --ai fake                  # all present datasets; others: "not downloaded"
    python scripts/bench.py --datasets icc_like_A --ai online --max-rows 50000

`--ai online` uses the configured online providers for public datasets only. Results go to
`test_datasets/_bench/<date>/bench.md|json`: rows in → in scope, mode decision and reason, PASS/DIFF/LATER
against `expected`, capabilities, stages run or skipped (with reasons), run time and peak memory. Accuracy
against truth is added as N2–N4 build their stages. `tests/nocase/test_universal.py` guards that the engine has
no process words and no literal thresholds, runs on every data shape, and never leaks truth. Case-ID results
no longer depend on the case-hash salt (`analysis.random_seed`, `app/core/stable.py`).

## 34. No-case mode: work episodes, case-link test, step assignment, automatic mode detection

**Stages (after units)**, each with interchangeable strategies chosen by `nocase.strategies` in settings (the
first that applies; assignment runs all as layers). Every choice and its reasons go to `nc_decisions.json`.

| Stage | Strategies | Output |
|---|---|---|
| Episodes (work sittings) | SourceSession, ActorGap (30 min idle), ActorAppContext (+ a new app/screen after a 10-min pause), DayWindow | `nc_episodes.parquet`, `episode_id` per event |
| Case link | CaseColumn, IdPatternInText (coverage, collisions, span), RecordJoin (`--records`), NoCase | `nc_case_links.parquet`, decision `case` / `partial` / `no_case` |
| Step assignment | SourceLabel (own label kept: `observed`; unit majority ≥ 0.9: `inferred_from_unit`), LibraryRules (`rule`), Unassigned | `nc_assignments.parquet`, `step`/`stage`/`step_source`/`step_status` per event, `nc_assign_quality.json` |

Events without an ID are never attached to a case by time; a no-case job that passes the case-link test is
reported as case-ready (convert it as a normal case-ID job). Process packs live in `<data dir>/library/<process>/`
(`pack.yaml` + `rules.yaml`), created on Business context (step 2). The app ships no sample packs.

```
python scripts/nocase.py run --input "<file>" --mapping config/nocase_mappings/hr_skan_nocase.yaml --process hr_onboarding
python scripts/nocase.py evaluate --job <job id>          # nc_eval.json: episode purity, link precision/recall, step accuracy
```

**Automatic mode detection at upload** (`app/intake/mode_detect.py`, `intake:` in settings): page 1 decides
whether the file has a case ID (a column passing coverage, not-a-counter, repeats, not-a-person, not a time or a
constant, not a category, sensible span — measured on the process rows when a process flag or level labels exist;
else an ID inside text), shows one banner with "Why?" and "Use the other mode", and pre-fills the existing
"Use a column" / "Derive it" options or the no-case form. Borderline evidence gives "confirm" (no-case, with a
question). It never changes how either mode calculates. `intake.auto_detect: false` turns it off.
`python scripts/detect_mode.py --input <file>` prints the decision (unattended: confirm → no-case).

## 35. No-case knowledge library and SME review

Approved process knowledge is shared by process in `<data dir>/library/<process>/`. The
existing pack (`pack.yaml`), step rules (`rules.yaml`), activity groups (`activities.yaml`) and
order edges (`order.yaml`) remain the files used by the no-case engine. New documents and AI
outputs are proposals; they do not become engine rules until a person accepts them.

The **Without case ID** section adds these review pages:

- **Review steps:** inspect proposed step rules, see matched cleaned units/events/hours, edit
  regexes with a live preview, accept or reject proposals, and assign an unassigned cleaned unit
  to a step to create an SME rule.
- **Review activities:** rename and approve Level 4/5 groups, or reject them.
- **Review order:** arrange the documented step order and mark it SME-confirmed; record
  exceptions alongside it.
- **SME questions:** answer questions from the evidence plan. Answers and every approval,
  rejection or edit are appended to `decisions.jsonl`.
- **Process pack:** inspect document-derived proposals and edit stages, step descriptions,
  terminology and exceptions.

Each approval batch snapshots the previous approved files under `versions/`. Rule edits
increment their version. Rejected rule patterns remain in `rules.yaml` with status `rejected`
and are filtered from later AI proposals. On the next file, the engine applies confirmed
`rules.yaml` rules and `activities.yaml` groups before requesting any AI help. The assignment
quality report shows the events and active hours covered by library rules; unassigned units are
the only ones offered to the rule proposal stage. Client jobs use private AI providers only.
Review screens show cleaned screens/clicks, not raw titles or IDs.

## 36. Deployment profiles, S3 backup and Docker

With `PM_PROFILE` unset, configuration is loaded exactly as before. Set `PM_PROFILE=local` for
the local overlay or `PM_PROFILE=aws` for the private AWS deployment. Profile settings are
deep-merged from `config/profiles/`. The sidebar and startup log show the selected profile,
data folder and private status of every routed AI provider. AWS refuses startup if any task
route includes a provider not marked private; it also clears custom `LLM_*` endpoints and uses
only the private vLLM provider.

**Docker on this computer:** copy `.env.example` to `.env`, choose AI models as usual, then run:

```bash
docker compose -f docker/compose.local.yaml up --build
```

Open `http://localhost:8501`. The host port is bound to `127.0.0.1`; local Ollama is off by
default. To run the optional Ollama service, use `--profile local-llm` and set `OLLAMA_MODEL`.
The image runs as a non-root user and never includes `.env`, `data/`, tests or the virtual
environment.

**AWS:** follow [the AWS deployment runbook](docs/AWS_DEPLOY.md). The app and GPU-backed vLLM
stay inside the private Compose network. Streamlit is reachable only through SSM port
forwarding. Mount the encrypted EBS data folder at `/data`; boto3 uses the EC2 instance role.

S3 transfer is manual with `python scripts/s3_sync.py push --bucket <bucket> --prefix <prefix>`
or `python scripts/s3_sync.py pull --bucket <bucket> --prefix <prefix>`. The sync transfers jobs,
process libraries and mappings; original uploads are omitted unless
`--include-raw` is supplied. To enable a best-effort automatic push after an analysis completes,
set `storage.s3_backup: true` in `config/settings.yaml` and `S3_BUCKET` / `S3_PREFIX` in the
environment. `storage.s3_include_raw` defaults to false. `python scripts/health.py` checks
data-folder read/write, DuckDB, disk space, provider privacy flags and S3 reachability. Add
`--check-ai` to send a small JSON probe to configured allowed providers.

## 35. No-case mode: AI layer, measures, candidate map, report, inside each step

**AI layer** (`app/nocase/ai_gateway.py`, wrapping `app.core.ai_client`; tasks `nc_*` in `ai.routes`). A client
dataset only reaches providers with `private: true` (`ollama_local`, `vllm_private`); with none configured the call
is not made ("AI not available (client data: private providers only)") and everything runs on rules. Every payload
passes `app/nocase/payloads.guard` (no e-mails, 4+ digit numbers, ID shapes, raw titles or names). Answers are
checked by code; one retry on the same provider, then rules with the reason.
- **Evidence planner** (`nc_evidence_plan.json`, `nc_evidence_scores.json`): which of 12 signals tell the step,
  scored 0–1 by code (coverage, consistency, agreement with labels, repeatability across people and weeks).
- **AI step rules** (`AiProposedRules`): proposals only fill units without a step; a rule that conflicts with a
  label or an approved rule, matches nothing, claims units of two steps or was rejected by an SME is dropped
  with the reason; accepted ones are `proposed` in `rules.proposed.yaml`.
- **Activities** (`nc_activities.parquet`): 3–10 Level 4 groups per step (AI, checked like Level 4), else one
  group per screen; Level 5 = clicks inside a big activity. Totals equal the step totals.

**Measures, map, report** (`python scripts/nocase.py report --job <id> --process <pack>`):
- `nc_measures.json`: per step, activity, app, person (P01…), stage — events, hours (measured, or estimated from
  gaps capped at 5 min), step runs, repetition, switches, time of day / week / month, step × app matrix.
- `nc_flow.json`: steps in pack order; inferred edges ("A then B in one person's episode", ≥ 20 episodes, ≥ 2
  people), the pack order as **documented** (never observed), **sme_confirmed** after review.
- `nc_confidence.json`, `nc_sme_questions.json`: evidence-weighted confidence bands and the conflicts to ask
  about; `calibrate` sets the weights from a dataset with truth.
- `nc_report.html` / `.xlsx`: 13 sections; every number is a fact; summaries use `{{F012}}` placeholders only;
  `nc_checks.json` recomputes everything with independent SQL — a failed check makes the title "DRAFT".
- `nc_inside_step.json`: per step the activity flow, usual patterns, start/end, time split, work type, rework,
  apps, variation between people and automation candidates (facts for SME review), all `inferred` until an SME
  confirms the pattern on **Review steps**.

Pages (menu **Without case ID**): Clickstream mapping (**Run analysis**), Clickstream map, Clickstream report.
Episode auto-pick uses a data-only score; its weights were calibrated once with
`scripts/calibrate_episodes.py` on the bench datasets with truth.
