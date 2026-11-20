"""Clean and mask the screen and action labels -> nc_events_clean.parquet (never drops a row).

    screen_clean   screen_norm.normalise first (segments, file names, periods, short codes, app suffixes), then
                   screens.clean_screen (person names, codes, dates, file versions out) on the screen column
                   (the text column when no screen is mapped), then ID shapes (5+ characters, 3+ digits) and
                   dates removed; an empty screen takes the same actor's previous screen in the same app within
                   nocase.carry_forward_seconds (carried = true)
    action_clean   typed text -> "<typed text>", a person's name -> "<person>", otherwise
                   milestones.scrub_action; nothing usable left -> "<other click>"
    is_generic     the cleaned action is in nocase.generic_actions

The raw text and screen stay only in nc_events.parquet; later stages and the AI use the cleaned columns.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from app.core.config import settings
from app.core.db import connect, sql_literal
from app.core.milestones import OTHER_CLICK, looks_like_person, scrub_action
from app.core.screens import TYPED, clean_screen, is_typed
from app.nocase.ingest import EVENTS_FILE
from app.nocase.screen_norm import fallback, normalise

CLEAN_FILE = "nc_events_clean.parquet"
PERSON = "<person>"
# The standard columns later stages may read (never text, screen or eval columns).
KEEP = ("eid", "ts", "ts_end", "actor", "app", "case_id", "l1", "l2", "l3", "l4", "l5", "active_s", "wait_s",
        "run_id", "session_id", "dataset_class")
_DATE = re.compile(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                   r"[a-z]*\s+\d{2,4}\b", re.I)
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)*")


def strip_ids(text: str | None) -> str | None:
    """Remove ID-shaped tokens (nocase.id_shapes: 5+ characters with 3+ digits) and dates."""
    if not text:
        return None
    cfg = settings()["nocase"]["id_shapes"]
    out = _DATE.sub(" ", text)
    out = _TOKEN.sub(lambda m: " " if len(m.group(0)) >= int(cfg["min_length"]) and
                     sum(ch.isdigit() for ch in m.group(0)) >= int(cfg["min_digits"]) else m.group(0), out)
    out = re.sub(r"\s+([-|–:])\s+(?=[-|–:]|\s*$)", " ", out)
    out = " ".join(out.split()).strip(" -|–:,#")
    return out or None


def _core_label(screen: str | None, app: str | None) -> str | None:
    # ID shapes go first (clean_screen would cut "INV-500007" to "INV"), and again after (dates, leftovers)
    return strip_ids(clean_screen(strip_ids(screen), app, extra_vocabulary=settings()["nocase"].get("screen_words") or []))


def screen_label_with_source(screen: str | None, app: str | None) -> tuple[str | None, str | None]:
    """(label, source). source: core (the existing cleaner on the raw title gives the same as on the prepared
    one), normalised (screen_norm prepared the title first), fallback (the existing cleaner gave nothing:
    screen_norm.fallback). A non-empty title never gives None."""
    if screen is None or not str(screen).strip():
        return None, None
    prepared = normalise(screen, app)
    label = _core_label(prepared, app) if prepared else None
    if label:
        return label, ("core" if label == _core_label(screen, app) else "normalised")
    return fallback(screen, app), "fallback"


def screen_label(screen: str | None, app: str | None) -> str | None:
    return screen_label_with_source(screen, app)[0]


def action_label(action: str | None) -> str:
    """The cleaned action; never None (every row keeps a label)."""
    if not action or not str(action).strip():
        return OTHER_CLICK
    text = " ".join(str(action).split())
    if is_typed(text):
        return TYPED
    if looks_like_person(text):
        return PERSON
    scrubbed = scrub_action(text)
    if not scrubbed:
        return OTHER_CLICK
    scrubbed = strip_ids(scrubbed)
    return scrubbed or OTHER_CLICK


def build(folder: Path) -> dict:
    """Write nc_events_clean.parquet; returns counts (rows, screens carried forward, generic share)."""
    con = connect()
    ev = f"read_parquet({sql_literal((folder / EVENTS_FILE).as_posix())})"
    has_screen = con.execute(f"SELECT COUNT(screen) FROM {ev}").fetchone()[0] > 0
    src = "screen" if has_screen else "text"
    pairs = con.execute(f"SELECT DISTINCT {src}, app FROM {ev} WHERE {src} IS NOT NULL").fetchall()
    labelled = [screen_label_with_source(s, a) for s, a in pairs]
    screens = pd.DataFrame({"__s": [p[0] for p in pairs], "__a": [p[1] for p in pairs],
                            "__clean": [x[0] for x in labelled], "__src": [x[1] for x in labelled]})
    actions = [a for (a,) in con.execute(f"SELECT DISTINCT action FROM {ev} WHERE action IS NOT NULL").fetchall()]
    acts = pd.DataFrame({"__act": actions, "__aclean": [action_label(a) for a in actions]})
    generic = sorted({str(g).lower() for g in settings()["nocase"]["generic_actions"]})
    con.register("__nc_screens", screens.astype({"__s": object, "__a": object, "__clean": object, "__src": object}))
    con.register("__nc_actions", acts.astype({"__act": object, "__aclean": object}))
    gap = float(settings()["nocase"]["carry_forward_seconds"])
    target = folder / CLEAN_FILE
    con.execute(f"""COPY (
        WITH e AS (
            SELECT ev.*, s.__clean AS screen0, s.__src AS screen_src0, COALESCE(a.__aclean, {sql_literal(OTHER_CLICK)}) AS action_clean
            FROM {ev} ev
            LEFT JOIN __nc_screens s ON s.__s = ev.{src} AND s.__a IS NOT DISTINCT FROM ev.app
            LEFT JOIN __nc_actions a ON a.__act = ev.action),
        c AS (
            SELECT *,
                   lag(screen0) OVER w AS prev_screen, lag(ts) OVER w AS prev_ts, lag(app) OVER w AS prev_app
            FROM e WINDOW w AS (PARTITION BY actor ORDER BY ts, eid))
        SELECT {', '.join(KEEP)},
               CASE WHEN screen0 IS NULL AND actor IS NOT NULL AND prev_screen IS NOT NULL
                         AND prev_app IS NOT DISTINCT FROM app AND epoch(ts) - epoch(prev_ts) <= {gap}
                    THEN prev_screen ELSE screen0 END AS screen_clean,
               (screen0 IS NULL AND actor IS NOT NULL AND prev_screen IS NOT NULL
                AND prev_app IS NOT DISTINCT FROM app AND epoch(ts) - epoch(prev_ts) <= {gap}) AS screen_carried,
               CASE WHEN screen0 IS NULL AND actor IS NOT NULL AND prev_screen IS NOT NULL
                         AND prev_app IS NOT DISTINCT FROM app AND epoch(ts) - epoch(prev_ts) <= {gap}
                    THEN 'carried' ELSE screen_src0 END AS screen_source,
               action_clean,
               (action IS NOT NULL AND lower(action_clean) IN ({', '.join(sql_literal(g) for g in generic)})) AS is_generic
        FROM c ORDER BY eid)
        TO {sql_literal(target.as_posix())} (FORMAT parquet, COMPRESSION zstd)""")
    rows_in, = con.execute(f"SELECT COUNT(*) FROM {ev}").fetchone()
    rows, carried, gen, empty, fb, nscreens = con.execute(f"""
        SELECT COUNT(*), COUNT(*) FILTER (WHERE screen_carried), AVG(CASE WHEN is_generic THEN 1.0 ELSE 0 END),
               COUNT(*) FILTER (WHERE screen_clean IS NULL), COUNT(*) FILTER (WHERE screen_source = 'fallback'),
               COUNT(DISTINCT (app, screen_clean))
        FROM read_parquet({sql_literal(target.as_posix())})""").fetchone()
    raw_n, = con.execute(f"SELECT COUNT(DISTINCT ({src}, app)) FROM {ev} WHERE {src} IS NOT NULL").fetchone()
    assert rows == rows_in, (rows, rows_in)          # cleaning never drops a row
    return {"rows": int(rows), "screens_carried_forward": int(carried), "screen_empty": int(empty),
            "screen_empty_share": round(int(empty) / rows, 4) if rows else 0.0,
            "screen_fallback": int(fb), "screens_distinct": int(nscreens), "raw_titles_distinct": int(raw_n),
            "fragmentation": round(int(raw_n) / int(nscreens), 2) if nscreens else None,
            "generic_share": round(float(gen or 0), 4), "screen_source": src}
