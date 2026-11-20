# Metric inventory

Every number the app shows, what it means, how it is computed, and where. Written for the people who
check the report's numbers and for developers changing the code.

**Scope** in this file means: the rows kept by the page-1 row filter, with a case ID, an activity and a
usable time (`quality.analysable_where`: a parsed time, not before `quality.earliest_valid_year`,
not in the future). All times are UTC. Hours are computed at full precision and rounded only for
display, by `app/core/fmt.py`. Durations print as "38 s", "4.2 min", "5.3 h", "3.4 days", or "2.1 weeks"
from 14 days on. "< 1 min" is never printed.

**The report's numbers have one source: `app/core/metrics.py` (`metrics.json`).**
`app/core/report_checks.py` recomputes them independently on every report (`checks.json`), and
`scripts/verify_report.py` recomputes them with pandas from the original file.

## 1. From the file to the analysis (report section 2, page 1)

| Number | Definition | Formula | Input columns | Computed in | Shown in |
|---|---|---|---|---|---|
| Rows in file | every row of the uploaded file(s) | `COUNT(*)` over the source | – | `ingest.capture_scope` (`scope.json`) | report §2, Excel "Data scope", verify |
| Active hours in file | active time of every row | `SUM(active_time) / 3600` | active_time | `ingest.capture_scope` | report §2 |
| Left out by the row filter | rows whose filter column is not one of the chosen values (NULL counts as not chosen) | `NOT (CAST(col AS VARCHAR) IN values)` | filter column | `ingest.capture_scope` | report §2 |
| Without a case ID | kept by the filter, case ID NULL | first applicable reason | case_id | `ingest.capture_scope` | report §2 |
| Without an activity | kept, with a case, activity NULL | first applicable reason | activity | `ingest.capture_scope` | report §2 |
| Without a usable time | kept, with case and activity, the time does not parse or is unrealistic | first applicable reason | timestamp | `ingest.capture_scope` | report §2 (line shown only when > 0) |
| In scope | everything else | rows and hours | – | `ingest.capture_scope` | report §2, check 1 |
| Active hours not in this analysis | file hours − scope hours, split by reason | sum of the four reason lines | active_time | `report.scope_section` | report §2 |
| Rows left out (page 1) | rows in file − rows written to events.parquet | `rows_in_file − SUM(merged_events)` | – | `ingest.normalise` | page 1, appendix, fact `rows_left_out` |
| Events (page 1 summary) | rows written to events.parquet | `COUNT(*)` | – | `ingest.normalise` | page 1 |

Each row is counted under the FIRST reason that applies, so the lines add up to the rows in the
file. Check 1 verifies this.

## 2. Key numbers (report §1) — `metrics.compute` → `totals`

| Number | Definition / formula | Filters | Fact | Shown in |
|---|---|---|---|---|
| Cases | `COUNT(DISTINCT case_id)` over events in scope (salted hashes: same count as the raw IDs) | scope | `cases` | §1, summary, Excel |
| Step runs | consecutive events of the same activity in a case, sorted by time and then the row order in the file (`src_row`) | scope | `step_runs_total` | §1 |
| Events before merging | `SUM(merged_events)` (1 per row unless page 1 merged repeats) | scope | `events` | §1 |
| Participants | `COUNT(DISTINCT resource)` | scope | `participants` | §1, §10 |
| Applications | `COUNT(DISTINCT application)` (shown names) | scope | `applications` | §1 |
| Active hours in scope | `SUM(active_s) / 3600` | scope | `active_hours_total` | §1, §5 |
| Median / 90% / mean case duration | per case `MAX(COALESCE(ts_end, ts_start)) − MIN(ts_start)`; median, `quantile_cont(0.9)`, mean. One-event cases count with 0 | scope | `case_duration_median`, `case_duration_p90`, `case_duration_mean` (95% bootstrap CI) | §1, summary |
| Cases reaching the last step | cases with an event of the last ordered step ÷ cases | trusted order only | `reach_last_share` | §1, §7 |
| Activities | `COUNT(DISTINCT activity)` | scope | `activities` | appendix |

## 3. Steps, stages and applications (report §4, §5) — `metrics.compute`

| Number | Definition / formula | Fact | Shown in |
|---|---|---|---|
| Cases reached (step) | cases with ≥ 1 event of the step; % = ÷ cases in scope | `step_cases` | §4, Excel "Steps" |
| Runs (step) | step runs of the activity | – | §4 |
| Events (step) | `SUM(merged_events)` | – | §4 |
| Active hours (step) | `SUM(active_s) / 3600` over the step's events | `step_hours` | §4, summary |
| Share of hours (step) | step hours ÷ hours in scope | `step_hours_share` | §4, summary |
| Hours per case reached | step hours ÷ cases reached | – | §4 |
| Median active time per run | median over runs of the run's SUM of active time (never the run's duration) | – | §4 |
| Top 2 applications | applications by the step's active hours (events when no active time) | – | §4 |
| Rework share (step) | cases with a rework return ÷ cases reaching the step (see §6) | `step_rework_share` | §4, §9 |
| Waiting hours (step) | `SUM(wait_s) / 3600` | – | §4 (when a wait column is mapped) |
| Waiting share (step) | `SUM(wait_s) / SUM(tat_s)` (or ÷ active + wait without a TAT column). Idle time is never added | `step_waiting_share` | §4 |
| Stage of a step | from the trusted order; otherwise the most frequent stage-column value. "" or "(no stage)" is no stage: such steps are listed in "Other activities without a stage", never drawn | – | §4, §5 |
| Hours by stage | sum of its steps' hours; stages ordered by their first step | `stage_hours_share` | §5 |
| Step × application matrix | `SUM(active_s)` per (step, application), with row and column totals; "(no application)" holds events without one | – | §5, Excel "Step x application" |
| Hours by application | `SUM(active_s)`, share ÷ hours in scope, cases. With "Merge repeated steps" a merged row keeps one application, so per-application hours, events and cases come from `events_apps.parquet` (per run and application, `ingest.merged_apps_sql`); check 3 recomputes them from the raw input rows | `app_hours`, `app_hours_share` | §5, summary, page 3 Applications |
| Step order of the table | trusted order (confirmed, saved/edited, template); otherwise by cases reached, descending | `order_source` | §4 header badge |

## 4. Flow and conformance (report §7) — `metrics.conformance` (trusted order only)

| Number | Definition / formula | Fact | Shown in |
|---|---|---|---|
| In-order share | cases with 2+ ordered steps whose FIRST occurrences (time, then row order) follow the order ÷ those cases | `in_order_share` | §7 |
| Pair-order share | pairs of ordered steps (first occurrences) in the intended order ÷ all such pairs | `pair_order_share` | §7 |
| Steps done earlier than intended | (step, step meant to come first, cases), top 5 | `done_earlier` | §7 |
| Cases reaching each step | as in §3 | – | §7 |
| Order strength, flow confidence | weighted one-sidedness of directly-follows pairs; flow score from it, stability and source (`flow_confidence.py`) | `order_strength`, `flow_confidence` | appendix, page 3 |
| Missing mandatory steps | cases without a mandatory step ÷ cases | `missing_mandatory` | page 3, appendix |

The order is chosen once for the whole app by `reference.resolve`: the pick made on page 3 (job.json
`order_choice`), otherwise confirmed > saved > template > AI > milestones > data. Page 3, the report, the
facts, conformance and flow confidence all use it. Without a trusted order, §7 shows one line ("Step order not confirmed — enter it on page 4"). The
guessed order is not used for conformance.

## 5. Waiting between steps (report §8) — `metrics.compute` → `transitions`, `waiting`

| Number | Definition / formula | Fact | Shown in |
|---|---|---|---|
| Wait | start of the next RUN − end of the previous RUN in the same case; overlaps count as 0 | – | – |
| Cases, count | per (from, to) pair: distinct cases, transitions | – | §8, Excel "Waiting" |
| Median / 90% wait | `median`, `quantile_cont(0.9)` of the waits | `waiting_median` | §8 |
| Total waiting hours | `SUM` of the waits over all cases; the table's ranking | `waiting_total` | §8, summary |
| Pairs left out | fewer than `report.min_cases` cases, or a median under `report.min_wait_minutes` (counted in the caption) | – | §8 |

## 6. Rework (report §9) — `metrics.compute` → `rework`

| Number | Definition / formula | Shown in |
|---|---|---|
| Rework run | a run of a step that already had a run in the case, starting ≥ `report.rework_gap_minutes` after that run's end | §9 |
| Quick switch | the same within the gap (screen switching); never badged as rework | §9 caption line |
| Cases with rework, % | distinct cases with a rework run ÷ cases reaching the step | §9 |
| Extra runs, extra hours | rework runs and their active hours | §9 |
| Back and forth | runs A → B → A where the second A starts ≥ the gap after the first A ended | §9 |

## 7. People, applications, trend, variants (report §10–13)

| Number | Definition / formula | Computed in | Fact | Shown in |
|---|---|---|---|---|
| Participants (masked) | P01, P02 … by active hours (then cases); cases, runs worked in, events, steps worked, active hours. With merged repeats, from the per-run, per-user totals of `events_apps.parquet` (a run worked by two users counts for each with its own events and hours); check 10 recomputes them from the raw input rows. Names never shown | `metrics.compute` | – | §10 |
| Handovers | consecutive runs of a case with different users (masked) | `metrics.compute` | `handover` | §10 |
| Application switch | consecutive events (time, then row order) of the same case AND user with different known applications, ≤ `app_switching.session_gap_minutes` apart, BEFORE merging | `ingest.write_app_switches` → `app_switches.parquet` | `app_switches_total`, `app_switch_pair`, `app_switch_case_share`, `app_switches_per_case`, `app_switches_per_100_events` | §11, page 3 |
| Switches per run (step) | switches INTO the step ÷ runs of the step | `metrics.compute` | `app_switches_per_step` | §11 |
| Trend | per ISO week (Mon–Sun, UTC): cases started, active hours, median duration of the cases started; first/last week flagged partial and never compared | `metrics.compute` | – | §12 |
| Variants | distinct paths of step runs; top-10 share; top 5 paths with 2+ steps | `metrics.compute` | `total_variants`, `variant_coverage_top10`, `variant_share` | §13 |

## 8. Level 4 (report §6, page 3) — `level4.detail`

| Number | Definition | Fact |
|---|---|---|
| Runs and events | a run is the map's run (consecutive events of the step in a case); a step-run ID only splits it further. Every event is kept: a click name that cannot be shown is "<other click>". Check 13: events, hours and cases per step = the step table | – |
| Share of the step's hours | active seconds of the sub-step's clicks ÷ the step's active seconds (`active_share`) | – |
| Share of the step's clicks | events of the sub-step ÷ events of the step (`share`; used when there is no active time) | `level4` (kind bucket_share) |
| Median active time per run | median over runs of the sub-step's summed active time | `level4` (bucket_active) |
| Coverage | events in named sub-steps ÷ events of the step | `level4` (coverage) |
| Order strength, top path, standard share | inside runs, as for Level 3 (`subprocess.py`) | `level4` |
| Step hours, case share | for the step summary | local facts of `level4.step_summary` |

## 9. Page 2 (quality) and page 3 (map, sub-processes)

| Number | Definition | Computed in | Fact |
|---|---|---|---|
| Feasibility score, band, checks | quality gate (`quality.run_quality`) | `quality.py` | `feasibility_score`, `quality_band` |
| Analysable events (page 2) | analysable rows after removing EXACT duplicates | `quality.prepare` | (`events` without metrics) |
| Map boxes and arrows | directly-follows graph on the de-duplicated event log `mlog`: events per activity, arrow counts, event-to-event waits | `mining.dfg` | `transition_count` |
| Bottlenecks (page 3) | slowest directly-follows arrows by median event-to-event wait | `mining.bottlenecks` | `bottleneck_wait` (only without metrics) |
| Repeats (page 3) | activities occurring more than once in a case (any repeat) | `mining.rework` | `rework_share`, `ping_pong` (only without metrics) |
| Resources (page 3) | events and cases per user | `mining.resources` | `resources` |
| Monthly trend (page 3) | cases started per calendar month | `mining.trends` | `cases_started` (only without metrics) |
| Milestones | completion milestones and chains | `milestones.py` | `milestone_strength`, `milestone_coverage`, `milestone_chain`, `milestone_completion` |
| How each step is done | runs, median actions and duration, order strength, standard way | `subprocess.py` | `step_runs`, `step_median_actions`, `step_median_duration`, `step_order_strength`, `step_standard_share`, `step_missing_share`, `step_standard_source` |
| Simple map: cases reaching a step | from first occurrences on `mlog` | `reference.reference_stats` | `step_reach` (only without metrics), `back_and_forth` |
| Business definition evidence | claims and verdicts | `evidence.py` | `claim` |
| Combined confidence | business belief × data verdict | `combined_confidence.py` | `combined_overall`, `combined_count`, `combined_status` |
| Case definition | how cases were derived | `derived.py` | `case_definition` |
| Events per case | median events per case on `mlog` | `mining.kpis` | `events_per_case` |

## 10. Numbers computed in more than one place

| Number | Places | Same definition? | After this audit |
|---|---|---|---|
| Cases | `quality.prepare`, `mining.kpis`, `reference_stats`, `metrics.compute` | yes (distinct cases are not changed by de-duplication) | report and facts use `metrics`; check 5 recomputes |
| Events | `quality.prepare` (exact duplicates removed), `metrics.compute` (all rows in scope) | **no** | the report shows `metrics` ("events in scope"); page 2 keeps "analysable events after removing duplicates" and says so; the `events` fact comes from metrics |
| Case duration | `mining.kpis` (on `mlog`), `metrics.compute` | yes | facts take the value from metrics; check 6 recomputes |
| Waits between steps | `mining.dfg` (event to event on `mlog`), `metrics` (run end → run start) | **no** | the report uses runs (§8); page 3's map keeps event-to-event arrows; `bottleneck_wait` facts only without metrics |
| Rework | `mining.rework` (any repeat), `metrics` (return after the gap) | **no** | the report uses the gap rule (§9); `rework_share` facts only without metrics |
| Variants | `mining.variants` (events), `metrics` (runs) | **no** with repeated events | the report and its facts use runs |
| Cases reaching a step | `reference_stats`, `metrics.compute` | yes | report uses metrics; `step_reach` facts only without metrics |
| Switches per step | `mining.app_switching` (per event), `metrics` (per run) | **no** | the report and facts use per run |
| Handovers | `mining.resources` (raw user values), `metrics` (masked) | yes (counts) | facts use the masked ones |
| Time ties | `ingest.merge_repeats_sql`, `mining.build_log`, `metrics`, `ingest.write_app_switches` | now yes | all order by time, then `src_row` (row order in the file) |

## 11. Order evidence, Level 4 quality, inside each step (README section 30)

| Number | Definition | Computed in | Fact / check |
|---|---|---|---|
| Precedence share | among FULLY OBSERVED cases (first event ≥ `order_evidence.edge_days` after the data start, last event ≥ that before the end) that contain both steps: share where A's first occurrence comes before B's; verdict supports ≥ 0.65, contradicts ≤ 0.35, else unclear (also when < 30 cases) | `order_evidence.compute` | `evidence_pair`; check 17 recomputes it from the events |
| Documented order | topological sort of the `order` statements of the knowledge sources (open conflicts left out); steps no statement places: by their median first position in the cases | `order_evidence.compute` | – |
| Level 4 quality | per step: events in named sub-steps ÷ events; Other's hours ÷ step hours; units matching 2+ rules; assignments hash (stability); per sub-step units, users, app purity (events in its main application ÷ its events), label problems | `level4.quality` | appendix table; check 13 (totals) |
| Inside-step run | consecutive clicks of one case and one Level 3 by one person, no gap > `inside_step.gap_minutes`; a step run split by person and pause (same totals) | `inside_step.runs_of` | check 16 |
| Pattern share | runs with this Level 4 sequence (consecutive repeats merged) ÷ runs; top 5, the rest "other patterns" | `inside_step.analyse_step` | `inside_top_pattern`; check 16 (patterns add up to the runs) |
| Time split | active seconds of the sub-step's clicks per run; hours and median per run | `inside_step.analyse_step` | `inside_top_hours`; check 16 |
| Rework inside the step | runs where a sub-step comes back after a different one | `inside_step.analyse_step` | `inside_rework` |
| Variation between people | per masked user: runs, median active time per run, main pattern; flags: slower than the step's median by `cv_flag`, or main pattern used by < `other_pattern_share` of the others; spread = coefficient of variation of the users' medians | `inside_step.analyse_step` | – |
| PM4Py per step | Inductive Miner on ≤ `inside_step.max_runs` runs (fixed sample): fitness, precision (sample), and conformance against the sub-steps the documents name | `inside_step.build_models` | `inside_pm_fitness`, `inside_pm_precision` |

## 12. Live-check fixes (README section 31)

| Measure | Where | Definition |
|---|---|---|
| Step runs | page 3 map, report steps table | consecutive events of one step in a case (a step-run ID splits further) |
| Work sittings | Level 4, sub-processes, inside each step | a step run split by person and by pauses over `inside_step.gap_minutes`; check 18: Level 4 = inside the step |
| Flow confidence (data orders) | page 3, report §7 | strength score × stability; Low with score ≤ 50 when all-data strength < 0.3 or < 25% of cases in order |
| `coverage_by_ai` | Level 4 entry | share of the step's events covered by the AI rules (kept when ≥ 0.6) |
| Unique paths of events | page 2 | distinct event sequences per case |
| Distinct paths of step runs | report variants | distinct step-run sequences per case (repeats merged) |
| Token-replay deviations | page 3 PM4Py | per step: share of cases with the step done but not expected / expected but not done |
| Distinct values | page 1 profile | exact below 5,000,000 rows, else HyperLogLog estimate shown as "about N" |
