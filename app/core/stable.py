"""Results that do not depend on the case-hash salt.

Stored case IDs are salted hashes (derived.case_salt): a new data folder gets a new salt, so anything that
groups or orders cases by their hashed ID would change from folder to folder. Instead, every case gets a
CONTENT key built from what the case is: its events' times and steps (never its ID), hashed with
settings `analysis.random_seed`. Two cases with exactly the same content get the same key, so the result is
the same whichever of them comes first.

    case_key_sql(table)   per case: ckey (text) and bucket (0-63) for half-samples
    seed()                analysis.random_seed (42): bootstrap and half-sample draws
"""
from __future__ import annotations

import random

from app.core.config import settings

CASE_KEY = "case_key"
BUCKETS = 64


def seed() -> int:
    return int((settings().get("analysis") or {}).get("random_seed", 42))


def half(i: int) -> set[int]:
    """The i-th half-sample: 32 of the 64 buckets, drawn with analysis.random_seed."""
    return set(random.Random(seed() * 1000 + i).sample(range(BUCKETS), BUCKETS // 2))


def case_key_sql(table: str, parts: str, order: str, case_col: str = "case_id") -> str:
    """SELECT case_id, ckey, bucket from `table`: ckey = md5(seed | the case's `parts`, joined in `order`)."""
    return f"""
        SELECT {case_col} AS case_id, ckey, hash(ckey) % {BUCKETS} AS bucket FROM (
            SELECT {case_col}, md5('{seed()}|' || string_agg({parts}, '>' ORDER BY {order})) AS ckey
            FROM {table} WHERE {case_col} IS NOT NULL GROUP BY {case_col})"""


def build_case_keys(con, table: str = "mlog", ts: str = "ts_start", activity: str = "activity",
                    name: str = CASE_KEY) -> str:
    """TEMP table `name` (case_id, ckey, bucket) from an event table: the key is every event's time and step."""
    parts = f"CAST({ts} AS VARCHAR) || '~' || COALESCE(CAST({activity} AS VARCHAR), '')"
    con.execute(f"CREATE OR REPLACE TEMP TABLE {name} AS {case_key_sql(table, parts, f'{ts}, {activity}')}")
    return name


def has_table(con, name: str) -> bool:
    return bool(con.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?", [name]).fetchone()[0])
