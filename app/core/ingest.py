"""Step 1 of the pipeline: read any supported file, profile it, normalise it.

Gist
----
1. `open_source`   points DuckDB at the file (Parquet, CSV, Excel) without loading it.
                   A folder, a list of files or a glob pattern is read as ONE log
                   (`resolve_sources` finds the files, `dataset_overview` describes them).
2. `profile`       measures every column: type, nulls, distinct values, timestamp parse rate.
3. `normalise`     writes one standard event table (events.parquet) that every
                   later step reads, whatever the original file looked like.

Rows with problems (missing case IDs, unparseable timestamps) are KEPT in the
normalised file on purpose: the Day 2 quality gate must be able to count them.
"""
from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd

from app.core.config import settings
from app.core.db import quote_ident, sql_literal

SUPPORTED = {
    ".parquet": "parquet", ".pq": "parquet",
    ".csv": "csv", ".txt": "csv", ".tsv": "csv",
    ".xlsx": "excel", ".xls": "excel",
}

SOURCE_VIEW = "src"  # every module refers to the uploaded data by this view name


# --------------------------------------------------------------------------- #
# 1. Open the file as a DuckDB view
# --------------------------------------------------------------------------- #
def detect_format(path: Path) -> str:
    fmt = SUPPORTED.get(path.suffix.lower())
    if not fmt:
        raise ValueError(
            f"Unsupported file type '{path.suffix}'. Use one of: {', '.join(sorted(SUPPORTED))}"
        )
    return fmt


FORMAT_LABELS = {"parquet": "Parquet", "csv": "CSV", "excel": "Excel"}
GLOB_CHARS = ("*", "?", "[")


def _is_skipped(path: Path) -> bool:
    """Hidden files (".name") and Office lock files ("~$name") are never data."""
    return path.name.startswith((".", "~$"))


def _expand(item) -> list[Path]:
    """Turn one file, folder or glob pattern into the list of files it names."""
    text = str(item)
    path = Path(text)
    if path.is_dir():
        # Recursive, but skip anything inside hidden folders (e.g. ".git").
        return [p for p in path.rglob("*")
                if p.is_file() and not any(part.startswith(".") for part in p.relative_to(path).parts)]
    if path.is_file():
        return [path]
    if any(ch in text for ch in GLOB_CHARS):
        return [Path(p) for p in glob.glob(text, recursive=True) if Path(p).is_file()]
    raise ValueError(f"Nothing found at '{text}'. Check that the file or folder exists.")


def resolve_sources(source) -> tuple[str, list[Path]]:
    """Find the files of a dataset and their (single) format.

    `source` can be one file, a folder (searched recursively), a glob pattern
    such as "C:/exports/events_2026_*.parquet", or a list of any of these.
    Returns (format, files sorted by path). Raises ValueError with a plain
    message when nothing usable is found, formats are mixed, or there are more
    files than `ingest.max_files` allows.
    """
    items = list(source) if isinstance(source, (list, tuple)) else [source]
    # One file named directly keeps the old behaviour: an unsupported type is an error.
    if len(items) == 1 and Path(str(items[0])).is_file():
        path = Path(str(items[0]))
        return detect_format(path), [path]

    found: set[Path] = set()
    for item in items:
        found.update(p for p in _expand(item)
                     if p.suffix.lower() in SUPPORTED and not _is_skipped(p))
    files = sorted(found, key=lambda p: p.as_posix().lower())
    where = ", ".join(str(i) for i in items[:3]) + (" …" if len(items) > 3 else "")
    if not files:
        raise ValueError(
            f"No supported files found in {where}. "
            f"Supported file types: {', '.join(sorted(SUPPORTED))}."
        )

    counts: dict[str, int] = {}
    for f in files:
        fmt = SUPPORTED[f.suffix.lower()]
        counts[fmt] = counts.get(fmt, 0) + 1
    if len(counts) > 1:
        parts = ", ".join(f"{n} {FORMAT_LABELS[fmt]} file{'s' if n != 1 else ''}"
                          for fmt, n in sorted(counts.items()))
        raise ValueError(
            f"This dataset mixes file formats ({parts}). Keep one format per dataset: "
            "put the files of each format in their own folder, or use a pattern such as *.parquet."
        )

    max_files = settings()["ingest"]["max_files"]
    if len(files) > max_files:
        raise ValueError(
            f"{len(files):,} files found; at most {max_files:,} can be loaded as one dataset. "
            "Use a narrower folder or pattern, or combine the files first."
        )
    return next(iter(counts)), files


def _excel_to_parquet(path: Path, work_dir: Path, index: int) -> Path:
    """Convert one Excel file to Parquet once (Excel files are at most ~1M rows)."""
    # The index keeps two "events.xlsx" from different folders apart.
    suffix = f"__{index}" if index else ""
    converted = work_dir / (path.stem + suffix + "__from_excel.parquet")
    if not converted.exists():
        df = pd.read_excel(path, dtype=object)
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].map(lambda v: None if pd.isna(v) else str(v))
        df.columns = [str(c) for c in df.columns]
        df.to_parquet(converted, index=False)
    return converted


def _uses_hive_folders(files: list[Path]) -> bool:
    """True when the files sit in partition folders such as "year=2025/month=01/"."""
    return any(re.fullmatch(r"[^=]+=[^=]*", part) for f in files for part in f.parent.parts)


def _file_labels(files: list[Path]) -> tuple[list[str], str | None]:
    """The `_ingest_file` value of each file, and the folder prefix removed to get it.

    Normally just the file name. When names repeat (Hive-style exports often
    call every file "data.parquet"), the path below the dataset's own folder is
    used instead, e.g. "year=2025/month=01/data.parquet". Either way nothing
    above the dataset folder (which can contain user names) is kept.
    Returns (labels, None) for plain names, or (labels, "<common folder>/").
    """
    names = [f.name for f in files]
    if len(set(names)) == len(names):
        return names, None
    root = Path(os.path.commonpath([f.parent.as_posix() for f in files])).as_posix().rstrip("/") + "/"
    return [f.as_posix().removeprefix(root) for f in files], root


def _reader(fmt: str, files: list[Path], filename: bool, hive: bool) -> str:
    """The DuckDB table function that reads `files` as one table (columns matched by name)."""
    file_list = "[" + ", ".join(sql_literal(f.as_posix()) for f in files) + "]"
    options = ["union_by_name = true"]
    if filename:
        options.append("filename = true")
    if hive:
        options.append("hive_partitioning = true")
    if fmt == "parquet":
        return f"read_parquet({file_list}, {', '.join(options)})"
    # CSV is read with every column as text: see open_source.
    options += ["all_varchar = true", "auto_detect = true", "sample_size = 100000"]
    return f"read_csv({file_list}, {', '.join(options)})"


def _schema_error(err: Exception) -> ValueError | None:
    """Translate a DuckDB "column types do not match across files" error, if it is one."""
    msg = str(err)
    if not re.search(r"cast|type|schema", msg, re.IGNORECASE):
        return None
    column = re.search(r'column "([^"]+)"', msg, re.IGNORECASE)
    file = re.search(r'file "([^"]+)"', msg, re.IGNORECASE)
    what = f'The column "{column.group(1)}"' if column else "A column"
    where = f' (in file "{Path(file.group(1)).name}")' if file else ""
    return ValueError(
        f"{what} is stored with different, incompatible types in different files{where}. "
        "Fix the export so the column has one type in every file, then load the dataset again."
    )


def _check_types_match(con: duckdb.DuckDBPyConnection, fmt: str, files: list[Path],
                       hive: bool) -> None:
    """Fail early when one column has types across files that cannot be combined.

    DuckDB only notices such a clash while it reads the data (e.g. during
    profiling, deep inside a 10M-row scan). Comparing each file's schema with
    the combined schema reads file metadata only, so this check is cheap. Only
    where a file's type differs is ONE real value cast to the combined type
    (DuckDB accepts any cast of NULL, so a real value is needed).
    CSV is read as text, so its columns can never clash.
    """
    if fmt != "parquet" or len(files) < 2:
        return
    combined = {name: str(t) for name, t, *_ in con.execute(f"DESCRIBE {SOURCE_VIEW}").fetchall()}
    works: set[tuple[str, str]] = set()   # (file type, combined type) pairs already proven
    for f in files:
        reader = _reader(fmt, [f], filename=False, hive=hive)
        for name, t, *_ in con.execute(f"DESCRIBE SELECT * FROM {reader}").fetchall():
            target, own = combined.get(name), str(t)
            if target is None or own == target or (own, target) in works:
                continue
            q = quote_ident(name)
            try:
                found = con.execute(f"SELECT CAST({q} AS {target}) IS NULL FROM {reader} "
                                    f"WHERE {q} IS NOT NULL LIMIT 1").fetchone()
                if found is not None:  # a column that is empty in this file proves nothing
                    works.add((own, target))
            except duckdb.Error as err:
                raise ValueError(
                    f'The column "{name}" is {own} in file "{f.name}" but {target} in other files, '
                    "and the two cannot be combined. Fix the export so the column has one type "
                    "in every file, then load the dataset again."
                ) from err


def open_source(con: duckdb.DuckDBPyConnection, path, work_dir: Path) -> str:
    """Create the view `src` over the file(s) and return the detected format.

    `path` can be one file, a folder, a glob pattern or a list of files (see
    resolve_sources). All files are read as ONE table: columns are matched by
    name, and a column that a file does not have is NULL for that file's rows.
    The column `_ingest_file` holds the file name (never the folder path, which
    can contain user names) of every row, for one file as well as for many
    (see _file_labels for files that share a name). Hive-style partition
    folders ("year=2025/") become columns.

    CSV is read with every column as text (all_varchar). That is deliberate:
    automatic type guessing on a sample can fail halfway through a 10M-row file.
    We cast types ourselves later, with TRY_CAST, so bad values become NULL
    (and get counted by the quality gate) instead of crashing the upload.
    """
    fmt, files = resolve_sources(path)
    names, root = _file_labels(files)   # what _ingest_file shows
    if fmt == "excel":
        files = [_excel_to_parquet(f, work_dir, i) for i, f in enumerate(files)]
        read_fmt = "parquet"
    else:
        read_fmt = fmt
    hive = read_fmt == "parquet" and _uses_hive_folders(files)

    try:
        schema = con.execute(
            f"DESCRIBE SELECT * FROM {_reader(read_fmt, files, filename=False, hive=hive)}"
        ).fetchall()
        has_filename_col = any(str(c[0]).lower() == "filename" for c in schema)
        if fmt == "excel" or has_filename_col:
            # One SELECT per file with the name written in, so a source column
            # called "filename" survives and Excel rows show the original file name.
            rel = " UNION ALL BY NAME ".join(
                f"SELECT *, {sql_literal(name)} AS _ingest_file "
                f"FROM {_reader(read_fmt, [f], filename=False, hive=hive)}"
                for f, name in zip(files, names)
            )
        else:
            label = ("parse_filename(filename)" if root is None
                     else f"replace(filename, {sql_literal(root)}, '')")
            rel = (f"SELECT * EXCLUDE (filename), {label} AS _ingest_file "
                   f"FROM {_reader(read_fmt, files, filename=True, hive=hive)}")
        con.execute(f"CREATE OR REPLACE VIEW {SOURCE_VIEW} AS {rel}")
        _check_types_match(con, read_fmt, files, hive)
    except duckdb.Error as err:
        translated = _schema_error(err) if len(files) > 1 else None
        if translated:
            raise translated from err
        raise
    return fmt


def dataset_overview(con: duckdb.DuckDBPyConnection) -> dict:
    """Describe the files behind the `src` view, in ONE grouped full scan.

    Returns:
        files     number of files with at least one row
        rows      total rows
        per_file  [{file, rows}] sorted by file name
        columns_missing_in_files  [{column, missing_in}]: columns that are empty
                  (all NULL) in some files but filled in others. Combining files
                  by column name leaves a column NULL in every file that did not
                  have it, so this is how a missing column shows. (A column a
                  file has but never fills looks the same.)
    """
    cols = [c[0] for c in con.execute(f"DESCRIBE {SOURCE_VIEW}").fetchall()
            if c[0] != "_ingest_file"]
    counts = "".join(f", COUNT({quote_ident(c)})" for c in cols)
    groups = con.execute(
        f"SELECT _ingest_file, COUNT(*){counts} FROM {SOURCE_VIEW} "
        f"GROUP BY _ingest_file ORDER BY _ingest_file"
    ).fetchall()
    missing = []
    for i, col in enumerate(cols):
        empty_in = sum(1 for g in groups if g[2 + i] == 0)
        if 0 < empty_in < len(groups):
            missing.append({"column": col, "missing_in": empty_in})
    return {
        "files": len(groups),
        "rows": sum(g[1] for g in groups),
        "per_file": [{"file": g[0], "rows": g[1]} for g in groups],
        "columns_missing_in_files": missing,
    }


# --------------------------------------------------------------------------- #
# 2. Profile the columns
# --------------------------------------------------------------------------- #
@dataclass
class ColumnProfile:
    name: str
    dtype: str
    non_null: int
    null_pct: float
    distinct: int           # exact below ingest.exact_distinct_max_rows rows, else approximate (HyperLogLog)
    distinct_ratio: float   # distinct / non-null
    samples: list[str] = field(default_factory=list)
    ts_format: str | None = None   # best timestamp format found, if any
    ts_parse_rate: float = 0.0     # share of non-null sample values that parse
    # What the values look like (text columns only; shares of distinct sample values):
    code_like: float = 0.0         # hashes, UUIDs, long IDs: unreadable for people
    email_like: float = 0.0        # email addresses
    person_like: float = 0.0       # 2-3 capitalised words that are not an action ("Asha Rao")
    person_in_readable: float = 0.0  # person-like share among the values that are NOT codes
    distinct_exact: bool = True      # False: `distinct` is an estimate (shown as "about N")


@dataclass
class Profile:
    rows: int
    columns: list[ColumnProfile]
    seconds: float

    def by_name(self) -> dict[str, ColumnProfile]:
        return {c.name: c for c in self.columns}


TEXT_TYPES = ("VARCHAR", "STRING", "TEXT")
TIME_TYPES = ("TIMESTAMP", "DATE")
NUMBER_TYPES = ("BIGINT", "INTEGER", "HUGEINT", "DOUBLE", "FLOAT", "DECIMAL", "SMALLINT", "UBIGINT")

# Value shapes, used to tell readable names from codes and to spot personal data.
# A code: 16+ hex characters/dashes (hashes, UUIDs), or 20+ letters and digits mixed.
CODE_RE = re.compile(r"^(?=.*\d)[0-9A-Fa-f-]{16,}$|^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_-]{20,}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
# A person name: two or three capitalised words, e.g. "Asha Rao" or "Mary Anne O'Neil".
PERSON_RE = re.compile(r"^[A-Z][a-z]+(?: [A-Z][a-zA-Z'-]+){1,2}$")


def value_shapes(values: list[str]) -> dict[str, float]:
    """Measure what share of the values look like codes, emails or person names.

    "Approve Request" also has two capitalised words, so a value only counts as
    a person name when its first word is not a common action word.
    `person_in_readable` looks only at values that are NOT codes: it catches
    columns that are mostly hashed but still leak some real names.
    Returns {} for an empty list.
    """
    from app.core.mapping import _ACTION_WORDS  # local import avoids a cycle

    n = len(values)
    if n == 0:
        return {}
    codes = sum(1 for v in values if CODE_RE.match(v))
    emails = sum(1 for v in values if EMAIL_RE.match(v))
    persons = sum(1 for v in values
                  if PERSON_RE.match(v) and v.split(" ", 1)[0].lower() not in _ACTION_WORDS)
    readable = n - codes
    return {
        "code_like": round(codes / n, 3),
        "email_like": round(emails / n, 3),
        "person_like": round(persons / n, 3),
        "person_in_readable": round(persons / readable, 3) if readable else 0.0,
    }


def profile(con: duckdb.DuckDBPyConnection) -> Profile:
    """Measure every column.

    Counts and distinct values are computed on the FULL data, because sampling
    rows breaks case structure: a random 200k-row sample of a 10M-event log makes
    a case ID look almost unique. Timestamp parsing is tested on a sample only.
    """
    start = time.perf_counter()
    cols = con.execute(f"DESCRIBE {SOURCE_VIEW}").fetchall()  # (name, type, ...)
    names = [c[0] for c in cols]
    types = {c[0]: str(c[1]).upper() for c in cols}

    # One full pass: total rows, non-null count and distinct values per column: exact for files below
    # ingest.exact_distinct_max_rows rows, an estimate (HyperLogLog, about 2%) for bigger ones.
    total, = con.execute(f"SELECT COUNT(*) FROM {SOURCE_VIEW}").fetchone()
    exact = total < int(settings()["ingest"].get("exact_distinct_max_rows", 5_000_000))
    parts = ["COUNT(*) AS __rows"]
    for i, n in enumerate(names):
        q = quote_ident(n)
        parts.append(f"COUNT({q}) AS nn_{i}")
        parts.append(f"COUNT(DISTINCT {q}) AS d_{i}" if exact else f"approx_count_distinct({q}) AS d_{i}")
    stats = con.execute(f"SELECT {', '.join(parts)} FROM {SOURCE_VIEW}").fetchone()
    rows = stats[0]

    sample_n = settings()["ingest"]["profile_sample_rows"]
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE __sample AS SELECT * FROM {SOURCE_VIEW} "
        f"USING SAMPLE reservoir({int(sample_n)} ROWS) REPEATABLE (42)"
    )

    from app.core.mapping import detect_timestamp_format  # local import avoids a cycle

    profiles = []
    for i, n in enumerate(names):
        non_null = stats[1 + 2 * i]
        distinct = stats[2 + 2 * i]
        # Up to 2,000 distinct values, cast to text inside SQL so that
        # timezone-aware types never reach Python as timezone objects.
        q = quote_ident(n)
        values = [
            r[0] for r in con.execute(
                f"SELECT DISTINCT CAST({q} AS VARCHAR) FROM __sample "
                f"WHERE {q} IS NOT NULL LIMIT 2000"
            ).fetchall()
        ]
        samples = values[:5]
        # The shape of the values (codes, emails, names) only matters for text columns.
        looks = value_shapes(values) if types[n].startswith(TEXT_TYPES) else {}
        fmt, rate = detect_timestamp_format(con, "__sample", n, types[n])
        profiles.append(ColumnProfile(
            name=n,
            dtype=types[n],
            non_null=non_null,
            null_pct=0.0 if rows == 0 else round(100 * (rows - non_null) / rows, 2),
            distinct=distinct,
            distinct_ratio=0.0 if non_null == 0 else min(1.0, distinct / non_null),
            samples=samples,
            ts_format=fmt,
            ts_parse_rate=rate,
            distinct_exact=exact,
            **looks,
        ))
    return Profile(rows=rows, columns=profiles, seconds=round(time.perf_counter() - start, 2))


# --------------------------------------------------------------------------- #
# 3. Normalise to the standard event table
# --------------------------------------------------------------------------- #
def attribute_name(column: str) -> str:
    """Column name -> "attr_<clean name>"; e.g. "_ingest_file" -> "attr_ingest_file"."""
    clean = re.sub(r"[^0-9a-zA-Z]+", "_", column).strip("_").lower() or "col"
    return f"attr_{clean}"


def normalise(con: duckdb.DuckDBPyConnection, mapping: dict, out_path: Path,
              app_map: dict | None = None) -> dict:
    """Write events.parquet with the standard columns.

    Output columns:
        case_id      text
        activity     text
        ts_start     timestamp in UTC (NULL where the original could not be parsed)
        ts_end       timestamp in UTC, or NULL when no end column was mapped
        ts_start_raw the original text, kept so the quality gate can show bad examples
        resource     text, or NULL when not mapped
        application  the application's shown name (apps.resolve: the real name from the
                     application map, the readable value, or "Unnamed app N"), or NULL
                     when not mapped. The recorded (possibly masked) value is NOT kept.
        stage        the stage / group of the step (text), or NULL when not mapped
        attr_*       every other column, unchanged (except the ones left out, see below)
        merged_events  how many original events this row stands for (1 unless merged)

    The window title column (if mapped) only helps on page 1 and is never written.
    `app_map` replaces the shared application map (e.g. for a command-line run).

    Options in `mapping` (all chosen by the user on page 1):
        exclude_columns  columns NOT copied as attr_* (e.g. personal data). Mapped
                         role columns are never excluded, even if listed.
        scope            which rows to keep (see mapping.scope_where). Rows left
                         out are counted and reported in the summary.
        merge_repeats    when true, runs of the same activity in a case become
                         one event (see merge_repeats_sql).

    Returns a summary: events, events_before_merge, cases, activities,
    first_event, last_event, unparsed_timestamps, rows_in_file, rows_left_out,
    columns_left_out, seconds and size_mb.
    """
    from app.core import activity_renames
    from app.core.mapping import scope_where, timestamp_sql  # local import avoids a cycle

    start = time.perf_counter()
    tz = mapping.get("timezone") or "UTC"
    case_col, act_col, ts_col = mapping["case_id"], mapping["activity"], mapping["timestamp"]
    end_col, res_col = mapping.get("end_timestamp"), mapping.get("resource")
    exclude = set(mapping.get("exclude_columns") or [])

    from app.core.derived import CASE_COL, hash_case_sql   # local import avoids a cycle
    # Case IDs are stored as salted hashes in every job file (events, app switches, step actions,
    # milestone candidates): they all come from this one query. Derived cases are hashed already.
    case_sql = (f"CAST({quote_ident(case_col)} AS VARCHAR)" if case_col == CASE_COL
                else hash_case_sql(f"CAST({quote_ident(case_col)} AS VARCHAR)"))
    select = [
        f"{case_sql} AS case_id",
        # Near-identical names merged on page 2 (activity_renames.py), before repeats are merged.
        f"{activity_renames.sql(f'CAST({quote_ident(act_col)} AS VARCHAR)', mapping.get('activity_renames'))} AS activity",
        f"{timestamp_sql(ts_col, mapping['timestamp_format'], tz)} AS ts_start",
        (f"{timestamp_sql(end_col, mapping['end_timestamp_format'], tz)} AS ts_end"
         if end_col else "CAST(NULL AS TIMESTAMP) AS ts_end"),
        f"CAST({quote_ident(ts_col)} AS VARCHAR) AS ts_start_raw",
        (f"CAST({quote_ident(res_col)} AS VARCHAR) AS resource"
         if res_col else "CAST(NULL AS VARCHAR) AS resource"),
    ]
    app_col, title_col = mapping.get("application"), mapping.get("window_title")
    join = ""
    if app_col:
        from app.core.apps import name_table  # local import avoids a cycle
        # One row per recorded value -> shown name; joined, so only the name is written.
        con.register("__app_names", name_table(con, app_col, scope_where(mapping), app_map))
        join = f" LEFT JOIN __app_names ON __app_raw = CAST({quote_ident(app_col)} AS VARCHAR)"
        select.append("CAST(__app_name AS VARCHAR) AS application")
    else:
        select.append("CAST(NULL AS VARCHAR) AS application")
    stage_col = mapping.get("stage")
    select.append(f"CAST({quote_ident(stage_col)} AS VARCHAR) AS stage" if stage_col
                  else "CAST(NULL AS VARCHAR) AS stage")
    # Time columns in seconds (active, wait, turnaround) and the original row order, which breaks
    # ties between events with the same time (metrics.py, mining.build_log, merge_repeats_sql).
    for role, alias in TIME_ROLES.items():
        col = mapping.get(role)
        select.append(f"TRY_CAST({quote_ident(col)} AS DOUBLE) AS {alias}" if col
                      else f"CAST(NULL AS DOUBLE) AS {alias}")
    select.append("__src_row AS src_row")
    for level in ("level1", "level3"):          # process levels 1 and 3 (level 2 is the stage)
        col = mapping.get(level)
        select.append(f"CAST({quote_ident(col)} AS VARCHAR) AS {level}" if col
                      else f"CAST(NULL AS VARCHAR) AS {level}")
    # Role columns (and the window title) are never copied as attr_* columns.
    action_col = mapping.get("action")
    run_col, active_col = mapping.get("step_run"), mapping.get("active_time")
    used = {c for c in (case_col, act_col, ts_col, end_col, res_col, app_col, title_col, stage_col, action_col,
                        mapping.get("level1"), mapping.get("level3"), run_col, active_col,
                        mapping.get("wait_time"), mapping.get("tat_time")) if c}
    taken: set[str] = set()
    attr_aliases: list[str] = []
    left_out: list[str] = []
    for (name,) in con.execute(f"SELECT column_name FROM (DESCRIBE {SOURCE_VIEW})").fetchall():
        if name in used:
            continue  # role columns are always kept, even if listed in exclude_columns
        if name in exclude:
            left_out.append(name)
            continue
        alias = attribute_name(name)
        while alias in taken:
            alias += "_"
        taken.add(alias)
        attr_aliases.append(alias)
        select.append(f"{quote_ident(name)} AS {quote_ident(alias)}")

    # Only the rows the user chose to keep.
    # The row number is taken BEFORE the join, so it is the position in the file (scan order).
    rowed = f"(SELECT *, row_number() OVER () AS __src_row FROM {SOURCE_VIEW}) AS {SOURCE_VIEW}"
    std_query = f"SELECT {', '.join(select)} FROM {rowed}{join} WHERE {scope_where(mapping)}"
    if mapping.get("merge_repeats"):
        final_query = merge_repeats_sql(std_query, attr_aliases)
    else:
        final_query = f"SELECT *, 1 AS merged_events FROM ({std_query})"

    compression = settings()["ingest"]["parquet_compression"]
    apps_path = out_path.parent / APPS_FILE
    if mapping.get("merge_repeats") and (app_col or res_col):
        con.execute(f"COPY ({merged_apps_sql(std_query)}) TO {sql_literal(apps_path.as_posix())} "
                    f"(FORMAT parquet, COMPRESSION {compression})")
    elif apps_path.exists():
        apps_path.unlink()                       # stale from an earlier conversion with merging
    con.execute(
        f"COPY ({final_query}) "
        f"TO {sql_literal(out_path.as_posix())} (FORMAT parquet, COMPRESSION {compression})"
    )

    summary = con.execute(f"""
        SELECT COUNT(*)                         AS events,
               COALESCE(SUM(merged_events), 0)  AS events_before_merge,
               COUNT(DISTINCT case_id)          AS cases,
               COUNT(DISTINCT activity)         AS activities,
               MIN(ts_start)                    AS first_event,
               MAX(ts_start)                    AS last_event,
               COUNT(*) - COUNT(ts_start)       AS unparsed_timestamps
        FROM read_parquet({sql_literal(out_path.as_posix())})
    """).fetchone()
    keys = ["events", "events_before_merge", "cases", "activities", "first_event", "last_event",
            "unparsed_timestamps"]
    result = dict(zip(keys, summary))
    result["events_before_merge"] = int(result["events_before_merge"])
    # Nothing is dropped silently: report how many rows the filters left out.
    result["rows_in_file"] = con.execute(f"SELECT COUNT(*) FROM {SOURCE_VIEW}").fetchone()[0]
    result["rows_left_out"] = result["rows_in_file"] - result["events_before_merge"]
    result["columns_left_out"] = sorted(left_out)
    result["scope"] = capture_scope(con, mapping, out_path.parent / SCOPE_FILE)
    candidates_path = out_path.parent / "milestone_candidates.parquet"
    if action_col:
        from app.core.milestones import capture
        action_query = (f"SELECT {', '.join(select)}, CAST({quote_ident(action_col)} AS VARCHAR) AS __action "
                        f"FROM {rowed}{join} WHERE {scope_where(mapping)}")
        result["milestones"] = capture(con, action_query, candidates_path)
    elif candidates_path.exists():
        candidates_path.unlink()
    # Step runs and their UI actions, also on the rows in scope BEFORE merging (subprocess.py).
    from app.core.subprocess import ACTIONS_FILE, capture_step_actions
    actions_path = out_path.parent / ACTIONS_FILE
    if action_col:
        extra = (f", CAST({quote_ident(action_col)} AS VARCHAR) AS __action"
                 + (f", CAST({quote_ident(run_col)} AS VARCHAR) AS __run" if run_col else ", CAST(NULL AS VARCHAR) AS __run")
                 + (f", TRY_CAST({quote_ident(active_col)} AS DOUBLE) AS __active" if active_col
                    else ", CAST(NULL AS DOUBLE) AS __active")
                 # The window title only becomes a cleaned screen label (screens.clean_screen); never written raw.
                 + (f", CAST({quote_ident(title_col)} AS VARCHAR) AS __title" if title_col
                    else ", CAST(NULL AS VARCHAR) AS __title"))
        result["step_actions"] = capture_step_actions(
            con, f"SELECT {', '.join(select)}{extra} FROM {rowed}{join} WHERE {scope_where(mapping)}",
            actions_path, has_run_id=bool(run_col))
    elif actions_path.exists():
        actions_path.unlink()
    # Application switches are counted on the rows in scope BEFORE repeated steps are
    # merged: merging collapses runs of events, which would hide most switches.
    switches_path = out_path.parent / SWITCHES_FILE
    if app_col:
        result["app_switches_total"] = write_app_switches(con, std_query, switches_path)
    elif switches_path.exists():
        switches_path.unlink()          # a stale file from an earlier conversion with an app column
    result["seconds"] = round(time.perf_counter() - start, 2)
    result["size_mb"] = round(out_path.stat().st_size / 1e6, 1)
    return result


PROTECT_CASE_IDS = "Re-run Validate and convert to protect case IDs"


def case_ids_protected(events_path: Path) -> bool:
    """True when every case ID in events.parquet is a salted hash (written by this version);
    jobs converted earlier keep the original IDs until they are converted again."""
    n = int(settings()["derived_case"]["hash_length"])
    try:
        raw = duckdb.connect().execute(f"""
            SELECT COUNT(*) FROM read_parquet({sql_literal(Path(events_path).as_posix())})
            WHERE case_id IS NOT NULL AND NOT regexp_full_match(case_id, '[0-9a-f]{{{n}}}')""").fetchall()[0][0]
    except duckdb.Error:
        return False
    return raw == 0


SCOPE_FILE = "scope.json"
# Role -> column of events.parquet (seconds, as numbers).
TIME_ROLES = {"active_time": "active_s", "wait_time": "wait_s", "tat_time": "tat_s"}


def scope_conditions(mapping: dict) -> dict[str, str]:
    """SQL conditions on the source rows, one per line of the scope table, each row matching exactly one:
    left_out_by_filter, without_case, without_activity, without_time, in_scope (first reason that applies)."""
    from app.core.mapping import timestamp_sql
    from app.core.quality import utc_now
    scope = mapping.get("scope") or {}
    if scope.get("filter_column") and scope.get("filter_values"):
        values = ", ".join(sql_literal(str(v)) for v in scope["filter_values"])
        keep = f"COALESCE(CAST({quote_ident(scope['filter_column'])} AS VARCHAR) IN ({values}), FALSE)"
    else:
        keep = "TRUE"
    case = f"{quote_ident(mapping['case_id'])} IS NOT NULL"
    act = f"{quote_ident(mapping['activity'])} IS NOT NULL"
    ts = timestamp_sql(mapping["timestamp"], mapping.get("timestamp_format"), mapping.get("timezone") or "UTC")
    earliest = int(settings()["quality"]["earliest_valid_year"])
    ok_time = (f"COALESCE(({ts}) IS NOT NULL AND year({ts}) >= {earliest} "
               f"AND ({ts}) <= TIMESTAMP {sql_literal(utc_now())}, FALSE)")
    return {"left_out_by_filter": f"NOT ({keep})",
            "without_case": f"({keep}) AND NOT ({case})",
            "without_activity": f"({keep}) AND ({case}) AND NOT ({act})",
            "without_time": f"({keep}) AND ({case}) AND ({act}) AND NOT ({ok_time})",
            "in_scope": f"({keep}) AND ({case}) AND ({act}) AND ({ok_time})"}


def capture_scope(con: duckdb.DuckDBPyConnection, mapping: dict, path: Path | None = None) -> dict:
    """Where the analysed rows come from, BEFORE any filter: rows (and active seconds) in the file,
    left out by the row filter, then among the kept rows: without case ID, without activity, without
    a usable time, and in scope. Each row is counted under the FIRST reason that applies, so the lines
    add up to the rows in the file. Aggregates only (no IDs, names or values); saved as scope.json.
    """
    import json
    scope = mapping.get("scope") or {}
    active = mapping.get("active_time")
    sec = f"COALESCE(TRY_CAST({quote_ident(active)} AS DOUBLE), 0)" if active else "0"
    reasons = scope_conditions(mapping)
    filtered = bool(scope.get("filter_column") and scope.get("filter_values"))
    parts = ["COUNT(*)", f"SUM({sec})"] + [x for cond in reasons.values()
                                            for x in (f"COUNT(*) FILTER (WHERE {cond})",
                                                      f"COALESCE(SUM({sec}) FILTER (WHERE {cond}), 0)")]
    row = con.execute(f"SELECT {', '.join(parts)} FROM {SOURCE_VIEW}").fetchall()[0]
    out = {"rows_in_file": int(row[0]), "active_seconds_in_file": float(row[1] or 0) if active else None,
           "has_active_time": bool(active),
           "filter": ({"column": scope["filter_column"], "values": [str(v) for v in scope["filter_values"]]}
                      if filtered else None)}
    for i, key in enumerate(reasons):
        out[key] = {"rows": int(row[2 + 2 * i]), "active_seconds": float(row[3 + 2 * i]) if active else None}
    if path is not None:
        path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


SWITCHES_FILE = "app_switches.parquet"


def write_app_switches(con: duckdb.DuckDBPyConnection, std_query: str, path: Path) -> int:
    """Write one row per application switch to `path`; returns how many there are.

    A switch = two consecutive events (by time) of the same case AND the same user
    (same case only, when no user column is mapped), with two different, known
    applications, at most `app_switching.session_gap_minutes` apart (a longer pause
    starts a new work session). `std_query` holds the rows in scope with the
    readable application names (never raw hashes). Streamed to Parquet by DuckDB.
    Columns: ts, case_id, resource, activity (switched TO), from_app, to_app, gap_seconds, src_row (the row in the
    file: the tie-break between switches at the same time).
    """
    gap = float(settings()["app_switching"]["session_gap_minutes"]) * 60
    compression = settings()["ingest"]["parquet_compression"]
    con.execute(f"""
        COPY (
            WITH e AS (SELECT case_id, resource, activity, ts_start, application, src_row FROM ({std_query})
                       WHERE case_id IS NOT NULL AND ts_start IS NOT NULL),
                 w AS (SELECT *, lag(application) OVER p AS from_app, lag(ts_start) OVER p AS prev_ts FROM e
                       WINDOW p AS (PARTITION BY case_id, resource ORDER BY ts_start, src_row))
            SELECT ts_start AS ts, case_id, resource, activity, from_app, application AS to_app,
                   epoch(ts_start) - epoch(prev_ts) AS gap_seconds, src_row
            FROM w
            WHERE from_app IS NOT NULL AND application IS NOT NULL AND from_app <> application
              AND epoch(ts_start) - epoch(prev_ts) <= {gap}
        ) TO {sql_literal(path.as_posix())} (FORMAT parquet, COMPRESSION {compression})""")
    return con.execute(f"SELECT COUNT(*) FROM read_parquet({sql_literal(path.as_posix())})").fetchall()[0][0]


def merge_repeats_sql(std: str, attrs: list[str]) -> str:
    """Merge runs of the same activity inside a case into one event.

    Desktop, clickstream and ticket-status data often record one step many
    times in a row (e.g. 50 clicks in "Case Tracking"). On a process map each
    repeat becomes a loop and hides the real flow. This turns each run into one
    step that starts at its first event and ends at its last one.

    How the SQL works:
      s  the standard query (one row per original event).
      p  the rows that can be ordered (case ID and start time present). Each row
         gets __pos, its position in the case (by start time, then the row order in the file).
         __pos is numbered ONCE so that both steps below see exactly the same
         order, even when two events share the same time and activity.
      o  each row gets __new = 0 when the previous event (by __pos) in the same
         case has the same activity, otherwise 1 (a new run starts).
      r  a running total of __new in __pos order (__run): every row of one
         run gets the same number.
      (s, p, o, r are built by _runs_cte, shared with merged_apps_sql.)
      Then one row per (case, run): the activity, the first start time, the last
      end time (or start time when there is no end), the raw time, resource and
      every attribute of the FIRST event, the run's most frequent stage, the SUM of its
      active / wait / turnaround seconds, and merged_events = how many events it stands for.
      Rows without a case ID or start time cannot be ordered, so they are added
      back unchanged (merged_events = 1); the quality gate counts them.
    """
    first_attrs = "".join(
        f", arg_min({quote_ident(a)}, ts_start) AS {quote_ident(a)}" for a in attrs)
    return f"""
        {_runs_cte(std)}
        SELECT case_id,
               any_value(activity)               AS activity,
               MIN(ts_start)                     AS ts_start,
               MAX(COALESCE(ts_end, ts_start))   AS ts_end,
               arg_min(ts_start_raw, ts_start)   AS ts_start_raw,
               arg_min(resource, ts_start)       AS resource,
               arg_min(application, ts_start)    AS application,
               mode(stage)                       AS stage,
               mode(level1)                      AS level1,
               mode(level3)                      AS level3,
               SUM(active_s)                     AS active_s,
               SUM(wait_s)                       AS wait_s,
               SUM(tat_s)                        AS tat_s,
               MIN(src_row)                      AS src_row
               {first_attrs},
               COUNT(*)                          AS merged_events
        FROM r
        GROUP BY case_id, __run
        UNION ALL BY NAME
        SELECT *, 1 AS merged_events
        FROM s
        WHERE case_id IS NULL OR ts_start IS NULL
    """


APPS_FILE = "events_apps.parquet"


def merged_apps_sql(std: str) -> str:
    """With "Merge repeated steps", a merged row keeps ONE application and ONE user (its first event's), so
    per-application and per-user numbers cannot come from it. This query keeps, per merged run, application
    and user, the events and active seconds: (case_id, src_row = the run's key, i.e. MIN(src_row) as in
    events.parquet, activity, ts_start = the run's start, application, resource, events, active_s).
    Written to events_apps.parquet."""
    return f"""
        {_runs_cte(std)},
        k AS (SELECT *, MIN(src_row) OVER (PARTITION BY case_id, __run) AS __key,
                        MIN(ts_start) OVER (PARTITION BY case_id, __run) AS __start FROM r)
        SELECT case_id, __key AS src_row, any_value(activity) AS activity, any_value(__start) AS ts_start,
               application, resource, COUNT(*) AS events, SUM(active_s) AS active_s
        FROM k GROUP BY case_id, __key, application, resource
    """


def _runs_cte(std: str) -> str:
    """The CTEs s, p, o, r of merge_repeats_sql (see there): r numbers every run of a case (__run)."""
    return f"""
        WITH s AS ({std}),
        p AS (
            SELECT *,
                   row_number() OVER (PARTITION BY case_id ORDER BY ts_start, src_row) AS __pos
            FROM s
            WHERE case_id IS NOT NULL AND ts_start IS NOT NULL
        ),
        o AS (
            SELECT *,
                   CASE WHEN lag(activity) OVER (PARTITION BY case_id ORDER BY __pos)
                             IS NOT DISTINCT FROM activity THEN 0 ELSE 1 END AS __new
            FROM p
        ),
        r AS (
            SELECT *, SUM(__new) OVER (PARTITION BY case_id ORDER BY __pos
                                       ROWS UNBOUNDED PRECEDING) AS __run
            FROM o
        )"""
