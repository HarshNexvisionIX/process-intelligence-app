"""Open a no-case source as the `src` view.

Everything the case mode reads (CSV / TSV / TXT / Parquet / Excel, a folder or a glob) goes through the
existing ingest.open_source. Added here: JSON Lines (.jsonl) and XES (.xes / .xes.gz), converted once to a
Parquet file in the job's work folder (the original is never changed).
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from app.core.db import sql_literal
from app.core.ingest import open_source

EXTRA = (".jsonl", ".xes", ".xes.gz")


def _kind(path: Path) -> str | None:
    name = path.name.lower()
    for ext in (".xes.gz", ".xes", ".jsonl"):
        if name.endswith(ext):
            return ext
    return None


def open_nc_source(con: duckdb.DuckDBPyConnection, source, work_dir: Path) -> str:
    """Create the `src` view over `source`; returns the format ("jsonl", "xes" or ingest's format)."""
    if isinstance(source, (str, Path)) and _kind(Path(source)):
        path = Path(source)
        if not path.exists():
            raise ValueError(f"File not found: {path}")
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / (path.name.split(".")[0] + "_converted.parquet")
        kind = _kind(path)
        if kind == ".jsonl":
            con.execute(f"COPY (SELECT * FROM read_json_auto({sql_literal(path.as_posix())}, format='newline_delimited')) "
                        f"TO {sql_literal(target.as_posix())} (FORMAT parquet)")
            fmt = "jsonl"
        else:
            from scripts.xes_to_parquet import convert
            convert(str(path), str(target))
            fmt = "xes"
        open_source(con, target, work_dir)
        return fmt
    return open_source(con, source, work_dir)
