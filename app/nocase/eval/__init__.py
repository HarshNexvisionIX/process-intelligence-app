"""Accuracy measurement for the no-case mode: the ONLY code that reads eval_* columns.

Eval columns (mapping `eval`, e.g. the true case or step) are carried through the scope by ingest.py and
written here, to <job>/eval/truth.parquet (eid + eval_<name>), never into nc_events.parquet. A separate truth
file (the bench) is joined into <job>/eval/ too. Nothing else in app/nocase/ or app/ui/nocase_*.py may read
them (a static test checks this).
"""
from __future__ import annotations

from pathlib import Path

from app.core.db import connect, sql_literal
from app.nocase.canonical import EVAL_PREFIX
from app.nocase.ingest import EVENTS_FILE


EVAL_DIR = "eval"
TRUTH_FILE = "truth.parquet"


def write_truth(con, scoped: str, in_scope: str, folder: Path, names: list[str]) -> Path | None:
    """<job>/eval/truth.parquet: eid + eval_<name> for the rows in scope (from ingest's scoped view)."""
    if not names:
        return None
    out = folder / EVAL_DIR
    out.mkdir(parents=True, exist_ok=True)
    cols = ", ".join('"' + (EVAL_PREFIX + n).replace('"', '""') + '"' for n in names)
    target = out / TRUTH_FILE
    con.execute(f"COPY (SELECT eid, {cols} FROM {scoped} WHERE {in_scope} ORDER BY eid) "
                f"TO {sql_literal(target.as_posix())} (FORMAT parquet)")
    return target


def eval_table(folder: Path) -> str:
    """SQL relation: eid + eval_<name> (empty when the job has no eval columns)."""
    path = folder / EVAL_DIR / TRUTH_FILE
    if not path.exists():
        return f"SELECT eid FROM read_parquet({sql_literal((folder / EVENTS_FILE).as_posix())}) WHERE FALSE"
    return f"SELECT * FROM read_parquet({sql_literal(path.as_posix())})"


def eval_columns(folder: Path) -> list[str]:
    """The eval names available in a job (without the prefix)."""
    cols = [r[0] for r in connect().execute(f"DESCRIBE {eval_table(folder)}").fetchall()]
    return [c[len(EVAL_PREFIX):] for c in cols if c.startswith(EVAL_PREFIX)]


def truth(folder: Path, name: str):
    """eid -> the eval value of `name` (a DataFrame with columns eid, truth)."""
    path = (folder / EVAL_DIR / TRUTH_FILE).as_posix()
    col = '"' + (EVAL_PREFIX + name).replace('"', '""') + '"'
    return connect().execute(f"SELECT eid, {col} AS truth FROM read_parquet({sql_literal(path)})").df()


def attach_truth_file(path: Path, key: str, columns: list[str], folder: Path) -> int:
    """A separate truth file (e.g. the bench's *_TRUTH.parquet) -> <job>/eval/truth_file.parquet: eid + its
    columns, joined on `key` (which the mapping keeps as the eval column of the same name)."""
    q = lambda c: '"' + str(c).replace('"', '""') + '"'      # noqa: E731
    out = folder / EVAL_DIR / "truth_file.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    con = connect()
    sel = ", ".join(f"t.{q(c)}" for c in columns)
    con.execute(f"""COPY (SELECT e.eid, {sel} FROM ({eval_table(folder)}) e
                          JOIN read_parquet({sql_literal(path.as_posix())}) t ON t.{q(key)} = e.{q(EVAL_PREFIX + key)}
                          ORDER BY e.eid) TO {sql_literal(out.as_posix())} (FORMAT parquet)""")
    return int(con.execute(f"SELECT COUNT(*) FROM read_parquet({sql_literal(out.as_posix())})").fetchone()[0])
