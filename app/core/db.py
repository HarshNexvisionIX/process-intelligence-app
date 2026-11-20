"""One place to open DuckDB with memory limits and disk spilling.

DuckDB runs inside our Python process (no server). With a memory limit and a
temp directory set, it spills to disk instead of crashing on large files.
"""
from __future__ import annotations

import duckdb

from app.core.config import data_dir, duckdb_limits


def connect() -> duckdb.DuckDBPyConnection:
    limits = duckdb_limits()
    con = duckdb.connect()  # in-memory catalogue; data stays in Parquet files
    spill_dir = data_dir() / "_duckdb_tmp"
    spill_dir.mkdir(exist_ok=True)
    con.execute(f"SET memory_limit = '{limits['memory_limit']}'")
    con.execute(f"SET threads = {limits['threads']}")
    con.execute(f"SET temp_directory = '{spill_dir.as_posix()}'")
    con.execute("SET TimeZone = 'UTC'")  # all timestamps are stored in UTC
    return con


def quote_ident(name: str) -> str:
    """Safely quote a column name for SQL (column names come from user files)."""
    return '"' + name.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    """Safely quote a text value for SQL."""
    return "'" + value.replace("'", "''") + "'"
