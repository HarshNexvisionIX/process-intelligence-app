"""The standard event table of the no-case mode and its mapping (NcMapping).

Standard columns (nc_events.parquet):
    ts, ts_end                    TIMESTAMP, UTC
    actor                         VARCHAR (salted hash, like stored case IDs; never the raw user)
    app                           VARCHAR (the application map applied)
    screen, action, text          VARCHAR (raw; only in nc_events.parquet, later stages use cleaned columns)
    case_id                       VARCHAR (salted hash; only a candidate until the case-link test passes)
    l1 .. l5                      VARCHAR (step labels from the source, when present)
    active_s, wait_s              DOUBLE (seconds)
    run_id, session_id            VARCHAR
plus eid (row number in the file: the tie-break), dataset_class, and eval_<name> columns (copied ONLY to
measure accuracy; read only by app/nocase/eval/).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from app.core.config import data_dir

TIME_COLUMNS = ("ts", "ts_end")
TEXT_COLUMNS = ("actor", "app", "screen", "action", "text", "case_id", "l1", "l2", "l3", "l4", "l5",
                "run_id", "session_id")
NUMBER_COLUMNS = ("active_s", "wait_s")
STANDARD_COLUMNS = ("ts", "ts_end", "actor", "app", "screen", "action", "text", "case_id",
                    "l1", "l2", "l3", "l4", "l5", "active_s", "wait_s", "run_id", "session_id")
LABEL_COLUMNS = ("l1", "l2", "l3", "l4", "l5")
REQUIRED = ("ts",)
HASHED = ("actor", "case_id")            # stored as salted hashes (derived.hash_case_sql)
DATASET_CLASSES = ("public", "client")
EVAL_PREFIX = "eval_"                    # eval columns: written by ingest.py, read only by app/nocase/eval/
TIME_FORMATS = ("native", "native_tz", "iso", "epoch_s", "epoch_ms")

COLUMN_HELP = {
    "ts": "when the event happened (required)", "ts_end": "when it ended (optional)",
    "actor": "the user / participant (stored as a hash)", "app": "the application",
    "screen": "the screen or simplified window title", "action": "the click / activity name",
    "text": "the full window title or typed text (raw, kept only in nc_events.parquet)",
    "case_id": "a case / record ID, if the export has one (tested before it is trusted)",
    "l1": "process level 1", "l2": "process level 2", "l3": "process level 3 (step)", "l4": "level 4",
    "l5": "level 5", "active_s": "active time in seconds", "wait_s": "wait time in seconds",
    "run_id": "a step-run / instance ID", "session_id": "a session ID",
}


@dataclass
class NcMapping:
    """How a source file maps to the standard columns, plus scope filters and eval columns."""
    columns: dict[str, str | None] = field(default_factory=dict)     # standard column -> source column
    dataset_class: str | None = None                                 # "public" | "client" (required)
    ts_format: str | None = None                                     # None: detected
    timezone: str = "UTC"                                            # for times without an offset
    keep_rows_where: dict[str, list] = field(default_factory=dict)   # source column -> values to keep
    drop_rows_where: dict[str, list] = field(default_factory=dict)   # source column -> values to leave out
    eval: dict[str, str] = field(default_factory=dict)               # name -> source column (accuracy only)

    # ------------------------------------------------------------------ build / save
    @classmethod
    def from_dict(cls, d: dict) -> "NcMapping":
        d = dict(d or {})
        cols = {str(k): (None if v in (None, "") else str(v)) for k, v in (d.get("columns") or {}).items()}

        def where(x):
            return {str(k): [v for v in (vals if isinstance(vals, list) else [vals])]
                    for k, vals in (x or {}).items()}
        return cls(columns=cols, dataset_class=d.get("dataset_class"), ts_format=d.get("ts_format"),
                   timezone=d.get("timezone") or "UTC", keep_rows_where=where(d.get("keep_rows_where")),
                   drop_rows_where=where(d.get("drop_rows_where")),
                   eval={str(k): str(v) for k, v in (d.get("eval") or {}).items() if v})

    @classmethod
    def from_yaml(cls, path: Path | str) -> "NcMapping":
        return cls.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})

    def to_dict(self) -> dict:
        return asdict(self)

    def mapped(self) -> dict[str, str]:
        """Standard column -> source column, only the mapped ones."""
        return {k: v for k, v in self.columns.items() if v and k in STANDARD_COLUMNS}

    # ------------------------------------------------------------------ checks
    def problems(self, source_columns: list[str] | None = None) -> list[str]:
        """Every problem, in plain words (empty = usable)."""
        out: list[str] = []
        unknown = sorted(k for k in self.columns if k not in STANDARD_COLUMNS)
        if unknown:
            out.append(f"Unknown standard column(s): {', '.join(unknown)}. "
                       f"Known: {', '.join(STANDARD_COLUMNS)}.")
        for r in REQUIRED:
            if not self.columns.get(r):
                out.append(f"'{r}' ({COLUMN_HELP[r]}) is not mapped.")
        if self.dataset_class not in DATASET_CLASSES:
            out.append("Choose the dataset class: 'public' or 'client' (client data is only sent to AI "
                       "providers marked private).")
        if self.ts_format and self.ts_format not in TIME_FORMATS and "%" not in self.ts_format:
            out.append(f"Time format '{self.ts_format}' is not known: use one of {', '.join(TIME_FORMATS)} "
                       "or a strptime pattern such as %d/%m/%Y %H:%M:%S.")
        if self.timezone:
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(self.timezone)
            except Exception:  # noqa: BLE001
                out.append(f"Timezone '{self.timezone}' is not known (e.g. UTC, Asia/Kolkata).")
        used: dict[str, str] = {}
        for std, src in self.mapped().items():
            if src in used and {std, used[src]} != {"screen", "text"}:
                out.append(f"Source column '{src}' is mapped to both '{used[src]}' and '{std}'.")
            used.setdefault(src, std)
        for name, src in self.eval.items():
            if not re.fullmatch(r"[A-Za-z0-9_]+", name):
                out.append(f"Eval name '{name}' must use letters, digits and _ only.")
            if src in used:
                out.append(f"Eval column '{src}' is also mapped as '{used[src]}': eval columns are kept apart.")
        for label, where in (("keep_rows_where", self.keep_rows_where), ("drop_rows_where", self.drop_rows_where)):
            for col, vals in where.items():
                if not vals:
                    out.append(f"{label}: no values given for '{col}'.")
        if source_columns is not None:
            have = set(source_columns)
            refs = ([(f"'{std}'", src) for std, src in self.mapped().items()]
                    + [(f"eval '{n}'", s) for n, s in self.eval.items()]
                    + [("keep_rows_where", c) for c in self.keep_rows_where]
                    + [("drop_rows_where", c) for c in self.drop_rows_where])
            for what, src in refs:
                if src not in have:
                    out.append(f"{what}: column '{src}' is not in the file.")
        return out


def layout_signature(column_names: list[str]) -> str:
    """Fingerprint of a file layout; ignores column order and upper / lower case."""
    names = sorted({str(c).strip().lower() for c in column_names if c != "_ingest_file"})
    return hashlib.sha1("|".join(names).encode()).hexdigest()[:16]


def _mappings_dir() -> Path:
    d = data_dir() / "nocase_mappings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_mapping(column_names: list[str], mapping: NcMapping) -> Path:
    path = _mappings_dir() / f"{layout_signature(column_names)}.json"
    path.write_text(json.dumps(mapping.to_dict(), indent=2), encoding="utf-8")
    return path


def load_mapping(column_names: list[str]) -> NcMapping | None:
    path = _mappings_dir() / f"{layout_signature(column_names)}.json"
    return NcMapping.from_dict(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None
