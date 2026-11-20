"""Strategy framework: interchangeable ways to do one stage (episodes, case_link, assign).

Each strategy says whether it applies to this data (with a reason), runs, and reports its quality. The order
comes from settings `nocase.strategies.<stage>`. For episodes and case_link the engine runs the FIRST strategy
that applies; for assign it runs all of them as layers. Every decision, with its reasons, goes to
nc_decisions.json. `forced` (command line --strategy stage=Name) picks one strategy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.core.db import connect, sql_literal

DECISIONS_FILE = "nc_decisions.json"
REGISTRY: dict[str, dict[str, type]] = {"episodes": {}, "case_link": {}, "assign": {}}


@dataclass
class Context:
    folder: Path
    caps: dict = field(default_factory=dict)
    forced: dict[str, str] = field(default_factory=dict)       # stage -> strategy name
    process: str | None = None
    records: dict | None = None                                # {"file": ..., "columns": {...}}
    ai: object = None                                          # app.nocase.ai_gateway.Gateway, or None (rules only)

    def path(self, name: str) -> Path:
        return self.folder / name

    def events_sql(self) -> str:
        """The cleaned events (never eval columns; they are not in this file)."""
        from app.nocase.clean import CLEAN_FILE
        return f"read_parquet({sql_literal(self.path(CLEAN_FILE).as_posix())})"

    def raw_sql(self) -> str:
        """The standard events with the raw text / screen (local only; used to find IDs)."""
        from app.nocase.ingest import EVENTS_FILE
        return f"read_parquet({sql_literal(self.path(EVENTS_FILE).as_posix())})"


class Strategy:
    name: str = ""
    stage: str = ""

    def applies(self, caps: dict, ctx: Context) -> tuple[bool, str]:  # (ok, reason)
        raise NotImplementedError

    def run(self, con, ctx: Context) -> str:                          # writes its table, returns its name
        raise NotImplementedError

    def quality(self, con, ctx: Context) -> dict:                     # coverage, conflicts, notes
        return {}


def register(cls):
    REGISTRY[cls.stage][cls.name] = cls
    return cls


def cfg() -> dict:
    return settings()["nocase"]


def order(stage: str) -> list[str]:
    return list((cfg().get("strategies") or {}).get(stage) or REGISTRY[stage])


def choose(stage: str, ctx: Context) -> tuple[Strategy, list[dict]]:
    """The strategy to run for a single-choice stage, and what was tried (name, applies, reason)."""
    tried = []
    names = [ctx.forced[stage]] if stage in ctx.forced else order(stage)
    for name in names:
        if name not in REGISTRY[stage]:
            raise ValueError(f"Unknown {stage} strategy '{name}'. Known: {', '.join(REGISTRY[stage])}")
        s = REGISTRY[stage][name]()
        ok, why = s.applies(ctx.caps, ctx)
        tried.append({"strategy": name, "applies": bool(ok), "reason": why,
                      "forced": stage in ctx.forced})
        if ok:
            return s, tried
    raise ValueError(f"No {stage} strategy applies: " + "; ".join(f"{t['strategy']}: {t['reason']}" for t in tried))


def load_decisions(folder: Path) -> dict:
    p = folder / DECISIONS_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_decision(folder: Path, stage: str, decision: dict) -> None:
    d = load_decisions(folder)
    d[stage] = decision
    (folder / DECISIONS_FILE).write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")


def new_con():
    return connect()
