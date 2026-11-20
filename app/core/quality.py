"""Day 2: the Data Quality and Feasibility Gate.

Gist
----
Before we draw any process map, we ask two questions:
  1. Is the data clean enough?            (checks 1-6)
  2. Does it hold enough repeated pattern  (checks 7-11)
     for a discovered process to be trusted?

Each check returns a value, a status (pass / warn / fail / info) and a score
0-100. Scores roll up into six dimensions, the dimensions into one Feasibility
Score, and the score into a band (High / Medium / Low / Not feasible).
Any failed check caps the score at 39, so one serious problem cannot be
averaged away by good results elsewhere.

All counting runs in DuckDB on events.parquet, so it works on 10M+ rows.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from app.core.config import settings
from app.core.db import quote_ident, sql_literal

# The source file of each event (multi-file datasets). Not part of the event
# itself: the same event in two overlapping exports is still one event.
FILE_COLUMN = "attr_ingest_file"

# Checks 12-15 (holdout fitness, drift, mixed processes, shared objects) come in v1.1.
CHECK_TITLES = {
    "required_fields": "1. Required fields present",
    "timestamp_validity": "2. Timestamps readable and realistic",
    "timestamp_granularity": "3. Time of day recorded",
    "ordering_ties": "4. Event order is clear",
    "duplicates": "5. No duplicate events",
    "label_hygiene": "6. Activity names are consistent",
    "case_completeness": "7. Cases have a normal start and end",
    "case_size": "8. Cases have more than one event",
    "volume": "9. Enough cases",
    "pattern_strength": "10. A dominant process exists",
    "model_stability": "11. The process map is stable",
    "attribute_coverage": "16. Extra columns are filled",
    "repeated_steps": "17. Repeated steps in a row",
}


@dataclass
class CheckResult:
    key: str
    title: str
    value: float | None          # the measured number (share, count or similarity)
    shown: str                   # the value written for people, e.g. "3.2% of rows"
    status: str                  # pass | warn | fail | info
    score: int | None            # 0-100; None for info-only checks
    explanation: str             # what the number means, in plain English
    fix: str | None = None       # what to do about it (only when not pass)
    examples: list[str] = field(default_factory=list)


@dataclass
class QualityReport:
    feasibility: int
    band: str
    capped: bool
    dimensions: dict[str, int]
    checks: list[CheckResult]
    fixes: list[dict]
    stats: dict
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Grading: turn a measured value into a status and a score
# --------------------------------------------------------------------------- #
def grade(value: float, warn: float, fail: float, higher_is_worse: bool = True) -> tuple[str, int]:
    """pass = 100; warn = 85 down to 50 between the two thresholds; fail = 20."""
    if higher_is_worse:
        if value <= warn:
            return "pass", 100
        if value > fail:
            return "fail", 20
        frac = (value - warn) / (fail - warn)
    else:
        if value >= warn:
            return "pass", 100
        if value < fail:
            return "fail", 20
        frac = (warn - value) / (warn - fail)
    return "warn", round(85 - 35 * frac)


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


# --------------------------------------------------------------------------- #
# Preparation: two working tables used by most checks
# --------------------------------------------------------------------------- #
def utc_now() -> str:
    """The current UTC time as SQL-ready text ("2026-11-04 10:15:00"); later times are 'future'."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def analysable_where(now: str) -> str:
    """SQL condition for rows that can be analysed: case, activity and a realistic time.

    "Realistic" means not a placeholder date (before `quality.earliest_valid_year`)
    and not in the future (after `now`, see utc_now). Shared by the quality gate
    and the mining engine, so both look at exactly the same events.
    """
    earliest = int(settings()["quality"]["earliest_valid_year"])
    return (f"case_id IS NOT NULL AND activity IS NOT NULL AND ts_start IS NOT NULL "
            f"AND year(ts_start) >= {earliest} AND ts_start <= TIMESTAMP {sql_literal(now)}")


def prepare(con: duckdb.DuckDBPyConnection, events_path: Path) -> dict:
    """Create:
    ev     every row exactly as normalised on Day 1 (problems included)
    clean  only rows we can analyse: case, activity and a realistic time present,
           exact duplicates removed. Pattern checks (7-11) run on this.
    cases  one row per case: number of events, first and last activity, path hash.
    """
    now = utc_now()
    src = f"read_parquet({sql_literal(events_path.as_posix())})"
    # src_row (the row's position in the file) is not part of the event: two copies of one event must stay equal.
    has_row = "src_row" in {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    con.execute(f"CREATE OR REPLACE VIEW ev AS SELECT * {'EXCLUDE (src_row) ' if has_row else ''}FROM {src}")
    # seq = position of the event inside its case (1, 2, 3...). Events are ordered
    # by time; equal times by name, so the result is the same on every run.
    # Computing seq once here makes every later check fast.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE clean AS
        SELECT *, row_number() OVER (PARTITION BY case_id ORDER BY ts_start, activity) AS seq
        FROM (SELECT DISTINCT case_id, activity, ts_start, resource
              FROM ev
              WHERE {analysable_where(now)})
    """)
    # path_hash: a fingerprint of the whole path. Same steps in the same order
    # give the same number, so counting distinct fingerprints counts variants.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE cases AS
        SELECT case_id,
               COUNT(*)                                       AS n_events,
               min_by(activity, seq)                          AS start_activity,
               max_by(activity, seq)                          AS end_activity,
               SUM(CAST(hash(activity, seq) AS HUGEINT))      AS path_hash
        FROM clean GROUP BY case_id
    """)
    rows, = con.execute("SELECT COUNT(*) FROM ev").fetchone()
    clean_rows, = con.execute("SELECT COUNT(*) FROM clean").fetchone()
    n_cases, = con.execute("SELECT COUNT(*) FROM cases").fetchone()
    return {"rows": rows, "clean_rows": clean_rows, "cases": n_cases, "now": now}


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #
def check_required_fields(con, stats, cfg) -> CheckResult:
    missing, = con.execute("""
        SELECT COUNT(*) FILTER (WHERE case_id IS NULL OR activity IS NULL OR ts_start_raw IS NULL)
        FROM ev""").fetchone()
    share = missing / stats["rows"] if stats["rows"] else 1.0
    status, score = grade(share, cfg["warn"], cfg["fail"])
    return CheckResult(
        "required_fields", CHECK_TITLES["required_fields"], share,
        f"{pct(share)} of rows ({missing:,})", status, score,
        "Rows without a case ID, activity or timestamp cannot be placed in the process.",
        None if status == "pass" else
        "Ask the data owner why these fields are empty; re-extract with the missing fields filled.",
    )


def check_timestamp_validity(con, stats, cfg, earliest_year) -> CheckResult:
    unreadable, placeholder, future, present = con.execute(f"""
        SELECT COUNT(*) FILTER (WHERE ts_start_raw IS NOT NULL AND ts_start IS NULL),
               COUNT(*) FILTER (WHERE year(ts_start) < {int(earliest_year)}),
               COUNT(*) FILTER (WHERE ts_start > TIMESTAMP '{stats['now']}'),
               COUNT(ts_start_raw)
        FROM ev""").fetchone()
    bad = unreadable + placeholder + future
    share = bad / present if present else 1.0
    status, score = grade(share, cfg["warn"], cfg["fail"])
    examples = [r[0] for r in con.execute("""
        SELECT DISTINCT ts_start_raw FROM ev
        WHERE ts_start_raw IS NOT NULL AND ts_start IS NULL LIMIT 5""").fetchall()]
    return CheckResult(
        "timestamp_validity", CHECK_TITLES["timestamp_validity"], share,
        f"{pct(share)} invalid ({unreadable:,} unreadable, {placeholder:,} placeholder, {future:,} future)",
        status, score,
        f"Placeholder dates (before {earliest_year}, e.g. 1970-01-01) and future dates are system "
        "defaults or errors; they break event order and durations.",
        None if status == "pass" else
        "Check the date format on the mapping screen; ask why default dates appear in the source system.",
        examples,
    )


def check_granularity(con, cfg) -> CheckResult:
    midnight, total = con.execute("""
        SELECT COUNT(*) FILTER (WHERE ts_start = date_trunc('day', ts_start)), COUNT(*)
        FROM clean""").fetchone()
    share = midnight / total if total else 1.0
    status, score = grade(share, cfg["warn"], cfg["fail"])
    return CheckResult(
        "timestamp_granularity", CHECK_TITLES["timestamp_granularity"], share,
        f"{pct(share)} of times are exactly 00:00:00", status, score,
        "When only the date is recorded, the order of steps on the same day is a guess, "
        "and waiting times under a day cannot be measured.",
        None if status == "pass" else
        "Extract the full date and time (change log or audit table) instead of a date-only field.",
    )


def check_ties(con, cfg) -> CheckResult:
    tied, total = con.execute("""
        SELECT COALESCE(SUM(n) FILTER (WHERE n > 1), 0), COALESCE(SUM(n), 0)
        FROM (SELECT COUNT(*) AS n FROM clean GROUP BY case_id, ts_start)""").fetchone()
    share = tied / total if total else 1.0
    status, score = grade(share, cfg["warn"], cfg["fail"])
    return CheckResult(
        "ordering_ties", CHECK_TITLES["ordering_ties"], share,
        f"{pct(share)} of events share their time with another event in the same case",
        status, score,
        "If two steps have the same time, nobody can tell which came first, "
        "so arrows on the map may point the wrong way.",
        None if status == "pass" else
        "Add seconds or a sequence number to the extract; if one exists, map it on the mapping screen.",
    )


def check_duplicates(con, stats, cfg) -> CheckResult:
    """Exact copies of a row. The source file name (attr_ingest_file) is ignored,
    so the same event exported in two overlapping files counts as a copy."""
    cols = [r[0] for r in con.execute("DESCRIBE ev").fetchall()]
    has_file = FILE_COLUMN in cols
    rows_sql = f"SELECT DISTINCT * EXCLUDE ({FILE_COLUMN}) FROM ev" if has_file else "SELECT DISTINCT * FROM ev"
    distinct, = con.execute(f"SELECT COUNT(*) FROM ({rows_sql})").fetchone()
    dupes = stats["rows"] - distinct
    share = dupes / stats["rows"] if stats["rows"] else 0.0
    status, score = grade(share, cfg["warn"], cfg["fail"])
    examples = []
    if dupes and has_file:
        # Events whose copies come from at least two different files.
        others = ", ".join(quote_ident(c) for c in cols if c != FILE_COLUMN)
        across, = con.execute(f"""
            SELECT COUNT(*) FROM (
                SELECT COUNT(DISTINCT {FILE_COLUMN}) AS files FROM ev GROUP BY {others})
            WHERE files > 1""").fetchone()
        if across:
            examples.append(f"{across:,} duplicate events appear in more than one file "
                            "(overlapping exports?)")
    return CheckResult(
        "duplicates", CHECK_TITLES["duplicates"], share,
        f"{pct(share)} of rows ({dupes:,}) are exact copies", status, score,
        "Copies inflate counts and create fake loops (a step that seems to repeat). "
        "They are removed automatically before analysis.",
        None if status == "pass" else
        "Usually caused by joining tables during extraction; check the extract query.",
        examples,
    )


def _label_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", label.lower())


def _typo_pairs(keys) -> list[tuple[str, str]]:
    """Find pairs of names that are probably one typo apart ("symmetric delete").

    For every name of 6+ characters, we write down the name itself and every form
    with one letter deleted ("approve" -> "pprove", "arove", ...). Two names that
    differ by one inserted, deleted or changed letter always share at least one
    of these forms. So instead of comparing every name with every other name
    (19,000 names = 180 million comparisons, ~4 minutes), we only compare names
    that land in the same bucket, which takes about a second.

    Buckets with more than 20 names are skipped: they come from generated names
    ("Screen 1", "Screen 2", ...), not typos. Pairs that become equal when digits
    are removed are also skipped, because "Page 1" and "Page 10" are different steps.
    """
    buckets: dict[str, set[str]] = {}
    for key in set(keys):
        if len(key) < 6:
            continue
        buckets.setdefault(key, set()).add(key)
        for i in range(len(key)):
            buckets.setdefault(key[:i] + key[i + 1:], set()).add(key)
    pairs: set[tuple[str, str]] = set()
    for members in buckets.values():
        if not 2 <= len(members) <= 20:
            continue
        ordered = sorted(members)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                if re.sub(r"\d", "", a) != re.sub(r"\d", "", b):
                    pairs.add((a, b))
    return sorted(pairs)


def near_identical_groups(labels: list[tuple[str, int]], max_labels: int) -> list[list[tuple[str, int]]]:
    """Groups of names that differ only in capitals, spaces, punctuation or one typo, each sorted
    most frequent first; the groups with the most events first. `labels` = [(name, count)]."""
    groups: dict[str, list[tuple[str, int]]] = {}
    for label, n in labels:
        groups.setdefault(_label_key(label), []).append((label, n))
    # Also join keys that are one typo apart, using a small union-find:
    # parent[k] points towards the representative key of k's cluster.
    # Skipped for very many names, where the search itself would be too slow.
    if len(groups) <= max_labels:
        parent = {k: k for k in groups}

        def find(k: str) -> str:
            while parent[k] != k:
                parent[k] = parent[parent[k]]  # shorten the path as we go
                k = parent[k]
            return k

        for a, b in _typo_pairs(groups):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
        merged: dict[str, list[tuple[str, int]]] = {}
        for k, members in groups.items():
            merged.setdefault(find(k), []).extend(members)
        groups = merged
    clusters = [sorted(v, key=lambda x: -x[1]) for v in groups.values() if len(v) > 1]
    clusters.sort(key=lambda c: -sum(n for _, n in c))  # biggest problems first
    return clusters


def check_label_hygiene(con, cfg, max_labels: int) -> CheckResult:
    labels = con.execute(
        "SELECT activity, COUNT(*) AS n FROM clean GROUP BY activity ORDER BY n DESC").fetchall()
    clusters = near_identical_groups(labels, max_labels)
    n_acts = len(labels)
    status = "warn" if clusters or n_acts > cfg["warn_activities"] else "pass"
    score = 100 if status == "pass" else max(50, 85 - 5 * len(clusters))
    examples = [" = ".join(f"'{l}' ({n:,})" for l, n in c) for c in clusters[:5]]
    return CheckResult(
        "label_hygiene", CHECK_TITLES["label_hygiene"], float(len(clusters)),
        f"{n_acts} activities, {len(clusters)} group(s) of near-identical names", status, score,
        "The same step written two ways (capitals, spaces, typos) appears as two boxes "
        "on the map and splits its counts.",
        None if status == "pass" else
        "Merge the names listed below (the most frequent spelling is first): tick them under this check "
        "on page 2 and click Apply merges, then convert again.",
        examples,
    )


def check_case_completeness(con, stats, cfg, usual_share) -> CheckResult:
    n = stats["cases"]
    if n == 0:
        return CheckResult("case_completeness", CHECK_TITLES["case_completeness"], 1.0,
                           "no cases", "fail", 20, "No analysable cases.")
    incomplete, = con.execute(f"""
        WITH s AS (SELECT start_activity FROM cases GROUP BY 1 HAVING COUNT(*) >= {usual_share} * {n}),
             e AS (SELECT end_activity   FROM cases GROUP BY 1 HAVING COUNT(*) >= {usual_share} * {n})
        SELECT COUNT(*) FROM cases
        WHERE start_activity NOT IN (SELECT * FROM s) OR end_activity NOT IN (SELECT * FROM e)
    """).fetchone()
    share = incomplete / n
    status, score = grade(share, cfg["warn"], cfg["fail"])
    starts = [r[0] for r in con.execute(f"""
        SELECT start_activity FROM cases GROUP BY 1 HAVING COUNT(*) >= {usual_share} * {n}
        ORDER BY COUNT(*) DESC, 1 LIMIT 5""").fetchall()]
    ends = [r[0] for r in con.execute(f"""
        SELECT end_activity FROM cases GROUP BY 1 HAVING COUNT(*) >= {usual_share} * {n}
        ORDER BY COUNT(*) DESC, 1 LIMIT 5""").fetchall()]
    return CheckResult(
        "case_completeness", CHECK_TITLES["case_completeness"], share,
        f"{pct(share)} of cases ({incomplete:,}) start or end unusually", status, score,
        "Cases cut off by the extract window (started before it or still running) look "
        "shorter than they really are and add fake paths.",
        None if status == "pass" else
        "Extract a wider time window, or filter to cases that both started and finished inside it.",
        [f"Usual starts: {', '.join(starts)}", f"Usual ends: {', '.join(ends)}"],
    )


def check_case_size(con, stats, cfg) -> CheckResult:
    single, p999, longest = con.execute("""
        SELECT COUNT(*) FILTER (WHERE n_events = 1),
               quantile_disc(n_events, 0.999), MAX(n_events)
        FROM cases""").fetchone()
    share = single / stats["cases"] if stats["cases"] else 1.0
    status, score = grade(share, cfg["warn"], cfg["fail"])
    return CheckResult(
        "case_size", CHECK_TITLES["case_size"], share,
        f"{pct(share)} of cases have one event; longest case {longest or 0:,} events "
        f"(99.9% have ≤ {p999 or 0:,})", status, score,
        "One-event cases have no path to analyse. Very long cases often mean a case ID "
        "was reused for many different items.",
        None if status == "pass" else
        "Check that the case ID column is right; one-event cases are often a different object type.",
    )


def check_volume(con, stats, cfg) -> CheckResult:
    n = stats["cases"]
    status, score = grade(n, cfg["warn"], cfg["fail"], higher_is_worse=False)
    return CheckResult(
        "volume", CHECK_TITLES["volume"], float(n), f"{n:,} analysable cases", status, score,
        "With few cases, a pattern can appear by chance. Results become reliable from "
        f"about {cfg['warn']:,} cases.",
        None if status == "pass" else "Extract a longer period or more business units.",
    )


def check_pattern_strength(con, stats, cfg) -> CheckResult:
    n = stats["cases"]
    if n == 0:
        return CheckResult("pattern_strength", CHECK_TITLES["pattern_strength"], 1.0,
                           "no cases", "fail", 20, "No analysable cases.")
    variants, top10, entropy = con.execute("""
        WITH v AS (SELECT path_hash, COUNT(*) AS n FROM cases GROUP BY 1)
        SELECT COUNT(*),
               (SELECT SUM(n) FROM (SELECT n FROM v ORDER BY n DESC LIMIT 10)),
               -SUM((n / s.total) * log2(n / s.total))
        FROM v, (SELECT SUM(n) AS total FROM v) s""").fetchone()
    ratio = variants / n
    status, score = grade(ratio, cfg["warn"], cfg["fail"])
    return CheckResult(
        "pattern_strength", CHECK_TITLES["pattern_strength"], ratio,
        f"{variants:,} unique paths of events for {n:,} cases (ratio {ratio:.2f}; the report counts paths of step "
        f"runs, with repeated events merged, so it finds fewer); "
        f"top 10 paths cover {pct(top10 / n)}", status, score,
        "If almost every case takes its own path, there is no main process to show, "
        "and the map becomes 'spaghetti'.",
        None if status == "pass" else
        "Analyse one sub-process, region or case type at a time, or merge fine-grained steps.",
        [f"Path entropy: {entropy or 0:.2f} bits (lower = more standardised)"],
    )


def check_model_stability(con, stats, cfg, samples: int) -> CheckResult:
    """Rebuild the main arrows of the map on many random halves of the cases.

    Cases are hashed into 64 buckets once; each half-sample is 32 random buckets.
    If the main arrows change between halves, the map reflects chance, not the process.
    """
    # Each arrow = an event joined to the next event (seq + 1) in the same case. Cases go to buckets by their
    # content (stable.py), not their salted ID, so the result is the same in every data folder.
    from app.core.stable import build_case_keys, half
    build_case_keys(con, "clean")
    rows = con.execute("""
        SELECT k.bucket, a.activity AS src, b.activity AS dst, COUNT(*) AS n
        FROM clean a JOIN clean b ON a.case_id = b.case_id AND b.seq = a.seq + 1
        JOIN case_key k ON k.case_id = a.case_id
        GROUP BY ALL""").fetchall()
    if not rows:
        return CheckResult("model_stability", CHECK_TITLES["model_stability"], 0.0,
                           "no transitions", "fail", 20, "Cases have no step-to-step transitions.")

    def top_edges(selected: set[int] | None, k: int | None = None) -> list[tuple]:
        totals: dict[tuple, int] = {}
        for b, s, d, n in rows:
            if selected is None or b in selected:
                totals[(s, d)] = totals.get((s, d), 0) + n
        ranked = sorted(totals.items(), key=lambda x: (-x[1], x[0]))
        if k is None:  # the arrows that carry 95% of all transitions (max 60)
            total, running, k = sum(totals.values()), 0, 0
            for _, n in ranked:
                running += n
                k += 1
                if running >= 0.95 * total or k >= 60:
                    break
        return [e for e, _ in ranked[:k]]

    main = top_edges(None)
    k = len(main)
    sims = []
    for seed in range(samples):
        sample_top = set(top_edges(half(seed), k))
        sims.append(len(sample_top & set(main)) / len(sample_top | set(main)))
    similarity = sum(sims) / len(sims)
    status, score = grade(similarity, cfg["warn"], cfg["fail"], higher_is_worse=False)
    return CheckResult(
        "model_stability", CHECK_TITLES["model_stability"], similarity,
        f"{similarity:.2f} average similarity of the top {k} arrows across {samples} half-samples "
        f"(worst {min(sims):.2f})", status, score,
        "Built again from different halves of the data, a trustworthy map shows the same main "
        "arrows. If it changes, the map is driven by chance.",
        None if status == "pass" else
        "Add more cases, or reduce variety first (merge labels, focus on one case type).",
    )


def check_attribute_coverage(con) -> CheckResult:
    cols = [r[0] for r in con.execute("SELECT column_name FROM (DESCRIBE ev)").fetchall()
            if r[0].startswith("attr_") or r[0] == "resource"]
    if not cols:
        return CheckResult("attribute_coverage", CHECK_TITLES["attribute_coverage"], None,
                           "no extra columns", "info", None,
                           "Without extra columns (vendor, region, amount...), root-cause analysis "
                           "can compare paths but not explain them.")
    counts = con.execute(
        "SELECT COUNT(*), " + ", ".join(f'COUNT("{c}")' for c in cols) + " FROM ev").fetchone()
    total = counts[0] or 1
    filled = {c: counts[i + 1] / total for i, c in enumerate(cols)}
    usable = [c for c, s in filled.items() if s >= 0.8]
    return CheckResult(
        "attribute_coverage", CHECK_TITLES["attribute_coverage"], len(usable) / len(cols),
        f"{len(usable)} of {len(cols)} extra columns are at least 80% filled", "info", None,
        "Only well-filled columns can be used to explain slow or unusual cases.",
        None,
        [f"{c.removeprefix('attr_')}: {pct(s)} filled" for c, s in sorted(filled.items(), key=lambda x: -x[1])][:10],
    )


def check_repeated_steps(con, tip_share: float) -> CheckResult:
    """Share of steps that repeat the step just before them in the same case (info only).

    Desktop, clickstream and ticket-status data often record one step many times
    in a row. Each repeat is drawn as a loop on the map and hides the real flow.
    When repeats are common and were not merged on page 1, the fix suggests it.
    """
    repeats, total = con.execute("""
        SELECT COUNT(*) FILTER (WHERE a.activity = b.activity), COUNT(*)
        FROM clean b JOIN clean a ON a.case_id = b.case_id AND a.seq = b.seq - 1""").fetchone()
    share = repeats / total if total else 0.0
    # Files converted before this feature have no merged_events column.
    ev_cols = {r[0] for r in con.execute("DESCRIBE ev").fetchall()}
    merged = ("merged_events" in ev_cols
              and (con.execute("SELECT MAX(merged_events) FROM ev").fetchone()[0] or 0) > 1)
    return CheckResult(
        "repeated_steps", CHECK_TITLES["repeated_steps"], share,
        f"{pct(share)} of steps repeat the step before them"
        + (" (repeats already merged)" if merged else ""),
        "info", None,
        "Common in desktop, clickstream and status data: the same step is recorded many times "
        "in a row. Each repeat becomes a loop on the map and hides the real flow.",
        "Go back to Import data, tick 'Merge repeated steps' and convert again."
        if share >= tip_share and not merged else None,
    )


# --------------------------------------------------------------------------- #
# Roll-up: checks -> dimensions -> feasibility score -> band -> fix list
# --------------------------------------------------------------------------- #
def run_quality(con: duckdb.DuckDBPyConnection, events_path: Path) -> QualityReport:
    q = settings()["quality"]
    c = q["checks"]
    stats = prepare(con, events_path)
    checks = [
        check_required_fields(con, stats, c["required_fields"]),
        check_timestamp_validity(con, stats, c["timestamp_validity"], q["earliest_valid_year"]),
        check_granularity(con, c["timestamp_granularity"]),
        check_ties(con, c["ordering_ties"]),
        check_duplicates(con, stats, c["duplicates"]),
        check_label_hygiene(con, c["label_hygiene"], q["max_labels_for_typo_search"]),
        check_case_completeness(con, stats, c["case_completeness"], q["usual_start_end_share"]),
        check_case_size(con, stats, c["case_size"]),
        check_volume(con, stats, c["volume"]),
        check_pattern_strength(con, stats, c["pattern_strength"]),
        check_model_stability(con, stats, c["model_stability"], q["stability_samples"]),
        check_attribute_coverage(con),
        check_repeated_steps(con, q["repeats_tip_share"]),
    ]
    by_key = {ch.key: ch for ch in checks}

    dimensions: dict[str, int] = {}
    total = 0.0
    for name, dim in q["dimensions"].items():
        scores = [by_key[k].score for k in dim["checks"]]
        dimensions[name] = round(sum(scores) / len(scores))
        total += dim["weight"] * dimensions[name]
    feasibility = round(total)
    # Two safety caps: one failure -> at most 39; many warnings -> at most Medium.
    capped = False
    if any(ch.status == "fail" for ch in checks) and feasibility > q["fail_cap"]:
        feasibility, capped = q["fail_cap"], True
    warnings = sum(ch.status == "warn" for ch in checks)
    if warnings > q["warn_cap"]["max_warnings"] and feasibility > q["warn_cap"]["cap"]:
        feasibility, capped = q["warn_cap"]["cap"], True
    band = next(b["name"] for b in q["bands"] if feasibility >= b["min"])

    # Fix list: every check with a fix (info tips included). Failures first, then
    # warnings, then tips; within each, by how many points the fix would win back.
    weight_of = {k: d["weight"] / len(d["checks"]) for d in q["dimensions"].values() for k in d["checks"]}
    status_order = {"fail": 0, "warn": 1, "info": 2}
    fixes = sorted(
        ({"check": ch.title, "status": ch.status, "problem": ch.shown, "fix": ch.fix,
          "points": round(weight_of.get(ch.key, 0) * (100 - (ch.score or 100)), 1)}
         for ch in checks if ch.fix),
        key=lambda f: (status_order.get(f["status"], 3), -f["points"]),
    )
    return QualityReport(
        feasibility, band, capped, dimensions, checks, fixes, stats,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def save_report(report: QualityReport, folder: Path) -> Path:
    path = folder / "quality.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, default=str)
    return path