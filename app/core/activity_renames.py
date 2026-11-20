"""Activity renames: merge near-identical activity names ("Aprove Request" -> "Approve Request").

The quality check (quality.check_label_hygiene) finds groups of names that differ only in
capitals, spaces or a typo. On page 2 the user ticks which groups to merge and into which
spelling; the result is saved per file layout:

    <data_dir>/activity_renames/<layout signature>.json   {"renames": {"old name": "new name"}}

At conversion the renames are copied into the mapping (mapping["activity_renames"]) and applied
in ingest.normalise before repeated steps are merged, so every job file uses the merged names.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app.core.config import data_dir, settings
from app.core.db import sql_literal


def path(signature: str) -> Path:
    safe = signature if re.fullmatch(r"[\w-]+", signature or "") else hashlib.sha256(signature.encode()).hexdigest()
    return data_dir() / "activity_renames" / f"{safe}.json"


def load(signature: str | None) -> dict[str, str]:
    if not signature:
        return {}
    p = path(signature)
    return dict(json.loads(p.read_text(encoding="utf-8")).get("renames") or {}) if p.exists() else {}


def validate(renames: dict[str, str]) -> dict[str, str]:
    """Clean names; no empty target, no chains (a target that is itself renamed), no self-renames."""
    # Old names must match the recorded values exactly (a trailing space is part of a typo);
    # only the target names are cleaned.
    out = {str(a): str(b).strip() for a, b in (renames or {}).items()}
    out = {a: b for a, b in out.items() if a.strip() and a != b}
    if any(not b for b in out.values()):
        raise ValueError("Every merge needs a target name.")
    chained = sorted(b for b in out.values() if b in out)
    if chained:
        raise ValueError("A target name is itself renamed: " + ", ".join(chained) + ".")
    return out


def save(signature: str, renames: dict[str, str], replace: bool = False) -> dict[str, str]:
    """Save renames for a file layout (added to the saved ones unless `replace`); returns them all."""
    from app.core.milestones import atomic_json
    merged = validate({**({} if replace else load(signature)), **(renames or {})})
    atomic_json(path(signature), {"renames": merged})
    return merged


def sql(column_sql: str, renames: dict[str, str]) -> str:
    """SQL: the activity with the renames applied (the column unchanged when there are none)."""
    if not renames:
        return column_sql
    whens = " ".join(f"WHEN {sql_literal(a)} THEN {sql_literal(b)}" for a, b in sorted(renames.items()))
    return f"(CASE {column_sql} {whens} ELSE {column_sql} END)"


def groups(events_path: Path) -> list[list[tuple[str, int]]]:
    """Groups of near-identical activity names in a job's events (most frequent spelling first),
    found the same way as the quality check."""
    from app.core.db import connect
    from app.core.quality import near_identical_groups
    labels = connect().execute(f"""SELECT activity, COUNT(*) FROM read_parquet({sql_literal(Path(events_path).as_posix())})
                                   WHERE activity IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    return near_identical_groups(labels, int(settings()["quality"]["max_labels_for_typo_search"]))

