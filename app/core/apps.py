"""Application mapping: give every masked application value its real name.

Desktop-recording exports often hold an application column where some values
are readable ("ServiceNow", "Excel") and some are masked (32-character hashes
such as 8bea6433fd6ef458baacbeb02165d7ac). The user names each masked value
once on page 1; the names are kept in ONE shared file for all datasets,

    <data_dir>/app_identity_map.csv   raw_value, real_name, category, notes, updated_at

because a masked value stands for the same application in every export.
`normalise` then writes only the resolved name (`resolve`) to events.parquet:
a raw hash never travels further than the mapping screen, and neither do
window titles (they only help the user recognise an application).
"""
from __future__ import annotations

import csv
import io
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from app.core.config import data_dir, settings
from app.core.db import quote_ident
from app.core.ingest import EMAIL_RE, SOURCE_VIEW

MAP_FILE = "app_identity_map.csv"
COLUMNS = ["raw_value", "real_name", "category", "notes", "updated_at"]
_EMAIL_IN_TEXT = re.compile(EMAIL_RE.pattern.strip("^$"))


def map_path() -> Path:
    return data_dir() / MAP_FILE


# --------------------------------------------------------------------------- #
# Masked values and names
# --------------------------------------------------------------------------- #
def is_masked(value) -> bool:
    """True for hashes, UUIDs and values without letters (patterns in settings: apps.masked_patterns)."""
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    return any(re.fullmatch(p, text) for p in settings()["apps"]["masked_patterns"])


def number_unnamed(counts: list[tuple[str, int]], the_map: dict) -> dict[str, int]:
    """Masked values without a real name -> 1, 2, 3... most events first (ties by value), so stable."""
    unnamed = [(raw, n) for raw, n in counts
               if is_masked(raw) and not (the_map.get(raw) or {}).get("real_name")]
    unnamed.sort(key=lambda x: (-x[1], x[0]))
    return {raw: i for i, (raw, _) in enumerate(unnamed, 1)}


def resolve(raw_value, the_map: dict, unnamed_numbers: dict) -> str | None:
    """The name shown everywhere: the mapped real name, the readable value, or "Unnamed app N".

    A masked value is never returned as is.
    """
    if raw_value is None:
        return None
    raw = str(raw_value)
    name = (the_map.get(raw) or {}).get("real_name")
    if name:
        return name
    if not is_masked(raw):
        return raw
    number = unnamed_numbers.get(raw)
    label = settings()["apps"]["unnamed_label"]
    return f"{label} {number}" if number else label


def clean_title(title) -> str:
    """A window title safe to show as an example: no #hex# tokens, emails or 6+ digit runs."""
    text = re.sub(r"#[0-9a-fA-F]+#", " ", str(title or ""))
    text = _EMAIL_IN_TEXT.sub(" ", text)
    text = re.sub(r"\d{6,}", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -|:·")


# --------------------------------------------------------------------------- #
# The shared map file
# --------------------------------------------------------------------------- #
def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        return [{c: (r.get(c) or "").strip() for c in COLUMNS} for r in csv.DictReader(f)]


def load_map() -> dict[str, dict]:
    """{raw_value: {real_name, category, notes}} from app_identity_map.csv ({} when there is none)."""
    return {r["raw_value"]: {"real_name": r["real_name"], "category": r["category"], "notes": r["notes"]}
            for r in _read_rows(map_path()) if r["raw_value"]}


def _clean(value) -> str:
    return "" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value).strip()


def save_map(rows) -> dict:
    """Merge `rows` (dicts with raw_value, real_name, category, notes) into the shared map.

    Applications not in `rows` keep their entry (they may belong to another dataset).
    A row whose name, category and notes are all empty removes that entry.
    The file is written to a temporary file first and then renamed, so a crash
    never leaves a half-written map. Returns counts: added, updated, removed, total.
    """
    path = map_path()
    current = {r["raw_value"]: r for r in _read_rows(path) if r["raw_value"]}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = updated = removed = 0
    for row in rows:
        raw = _clean(row.get("raw_value"))
        if not raw:
            continue
        new = {"raw_value": raw, "real_name": _clean(row.get("real_name")),
               "category": _clean(row.get("category")), "notes": _clean(row.get("notes"))}
        old = current.get(raw)
        if not (new["real_name"] or new["category"] or new["notes"]):
            if old is not None:
                del current[raw]
                removed += 1
            continue
        if old is None:
            added += 1
        elif any(old[k] != new[k] for k in ("real_name", "category", "notes")):
            updated += 1
        else:
            continue
        current[raw] = {**new, "updated_at": now}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".app_map_", suffix=".csv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(sorted(current.values(), key=lambda r: r["raw_value"]))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {"added": added, "updated": updated, "removed": removed, "total": len(current)}


def import_map(data: bytes) -> dict:
    """Read a CSV or Excel mapping file and compare it with the shared map (nothing is saved).

    Accepts the full columns (raw_value, real_name, category, notes) or a plain
    2-column file (raw value, real name), with or without a header. Spaces are
    trimmed; rows without a value or a real name are ignored. Returns
    {"rows": [...], "added", "updated", "unchanged", "ignored"}; pass "rows" to save_map.
    """
    if data[:4] == b"PK\x03\x04":   # .xlsx files are zip archives
        df = pd.read_excel(io.BytesIO(data), dtype=str, header=None)
    else:
        df = pd.read_csv(io.BytesIO(data), dtype=str, header=None, keep_default_na=False,
                         sep=None, engine="python", encoding="utf-8-sig")
    df = df.fillna("")
    first = [_clean(v).lower() for v in df.iloc[0]] if len(df) else []
    if "raw_value" in first:
        df.columns = first
        df = df.iloc[1:]
    else:
        names = ["raw_value", "real_name", "category", "notes"][:df.shape[1]]
        df = df.iloc[:, :len(names)]
        df.columns = names
        if first[:2] in (["raw value", "real name"], ["value", "name"]):
            df = df.iloc[1:]
    current = load_map()
    rows, added, updated, unchanged, ignored = [], 0, 0, 0, 0
    for rec in df.to_dict("records"):
        raw, name = _clean(rec.get("raw_value")), _clean(rec.get("real_name"))
        if not raw or not name:
            ignored += 1
            continue
        old = current.get(raw) or {}
        row = {"raw_value": raw, "real_name": name,
               "category": _clean(rec.get("category")) or old.get("category", ""),
               "notes": _clean(rec.get("notes")) or old.get("notes", "")}
        if not old:
            added += 1
        elif any(old.get(k, "") != row[k] for k in ("real_name", "category", "notes")):
            updated += 1
        else:
            unchanged += 1
        rows.append(row)
    return {"rows": rows, "added": added, "updated": updated, "unchanged": unchanged, "ignored": ignored}


def export_map() -> bytes:
    """The shared map as CSV (only the header when nothing is mapped yet)."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(_read_rows(map_path()))
    return out.getvalue().encode("utf-8")


def category_of(name: str, the_map: dict | None = None) -> str:
    """The category given to a real name (first one found), or ""."""
    the_map = load_map() if the_map is None else the_map
    return next((v["category"] for v in the_map.values() if v["real_name"] == name and v["category"]), "")


# --------------------------------------------------------------------------- #
# The applications of the current dataset
# --------------------------------------------------------------------------- #
def app_counts(con: duckdb.DuckDBPyConnection, app_col: str, where: str = "TRUE") -> list[tuple[str, int]]:
    """(raw value, events) per application value in the `src` view (rows matching `where`)."""
    q = quote_ident(app_col)
    return con.execute(
        f"SELECT CAST({q} AS VARCHAR) AS v, COUNT(*) FROM {SOURCE_VIEW} "
        f"WHERE {q} IS NOT NULL AND ({where}) GROUP BY v ORDER BY 2 DESC, v").fetchall()


def name_table(con: duckdb.DuckDBPyConnection, app_col: str, where: str = "TRUE",
               the_map: dict | None = None) -> pd.DataFrame:
    """raw value -> shown name for every application in the rows to convert (used by normalise)."""
    the_map = load_map() if the_map is None else the_map
    counts = app_counts(con, app_col, where)
    numbers = number_unnamed(counts, the_map)
    return pd.DataFrame({"__app_raw": [raw for raw, _ in counts],
                         "__app_name": [resolve(raw, the_map, numbers) for raw, _ in counts]})


def app_inventory(con: duckdb.DuckDBPyConnection, app_col: str, title_col: str | None = None,
                  resource_col: str | None = None, the_map: dict | None = None) -> pd.DataFrame:
    """One row per application value: events, users, masked, the saved name and example titles.

    Columns: raw_value, events, users (None without a user column), masked,
    real_name, category, notes, examples (cleaned window titles, " | "-joined).
    Sorted by events, most first.
    """
    the_map = load_map() if the_map is None else the_map
    q = quote_ident(app_col)
    users = f"COUNT(DISTINCT {quote_ident(resource_col)})" if resource_col else "NULL"
    rows = con.execute(f"""
        SELECT CAST({q} AS VARCHAR) AS v, COUNT(*) AS events, {users} AS users
        FROM {SOURCE_VIEW} WHERE {q} IS NOT NULL GROUP BY v ORDER BY events DESC, v""").fetchall()
    examples: dict[str, list[str]] = {}
    if title_col:
        k = int(settings()["apps"]["example_titles"])
        t = quote_ident(title_col)
        # A few of the most frequent titles per application; cleaned and de-duplicated in Python.
        for v, title in con.execute(f"""
                SELECT v, title FROM (
                    SELECT CAST({q} AS VARCHAR) AS v, CAST({t} AS VARCHAR) AS title,
                           row_number() OVER (PARTITION BY CAST({q} AS VARCHAR) ORDER BY COUNT(*) DESC,
                                              CAST({t} AS VARCHAR)) AS rn
                    FROM {SOURCE_VIEW} WHERE {q} IS NOT NULL AND {t} IS NOT NULL GROUP BY 1, 2)
                WHERE rn <= {4 * k} ORDER BY v, rn""").fetchall():
            cleaned = clean_title(title)
            shown = examples.setdefault(v, [])
            if cleaned and cleaned not in shown and len(shown) < k:
                shown.append(cleaned)
    out = []
    for v, events, n_users in rows:
        saved = the_map.get(v) or {}
        out.append({"raw_value": v, "events": events, "users": n_users, "masked": is_masked(v),
                    "real_name": saved.get("real_name", ""), "category": saved.get("category", ""),
                    "notes": saved.get("notes", ""), "examples": " | ".join(examples.get(v, []))})
    return pd.DataFrame(out, columns=["raw_value", "events", "users", "masked", "real_name", "category",
                                      "notes", "examples"])


def inventory_summary(inv: pd.DataFrame) -> dict:
    """Counts for the header line: applications, masked, masked mapped, share of events still unnamed."""
    masked = inv[inv["masked"]] if len(inv) else inv
    unnamed = masked[masked["real_name"].fillna("").str.strip() == ""] if len(masked) else masked
    total = int(inv["events"].sum()) if len(inv) else 0
    return {"applications": len(inv), "masked": len(masked), "mapped": len(masked) - len(unnamed),
            "unnamed_share": (int(unnamed["events"].sum()) / total) if total else 0.0,
            "unnamed": unnamed["raw_value"].tolist()}
