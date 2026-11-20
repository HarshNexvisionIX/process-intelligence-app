"""Units: one row per (app, screen_clean, action_clean) -> nc_units.parquet; unit_id written onto the events.

    unit_id      U00001, U00002 ... sorted by events (most first), then by the key (stable across runs)
    events, actors, active_hours, first_seen, last_seen, share (of events in scope), is_generic
    top_l3, top_l3_share   the most common source step label among the unit's events (NULL when none)

Also adds the label consistency per unit to nc_capabilities.json (the events-weighted share of labelled
events that carry their unit's top l3).
"""
from __future__ import annotations

import os
from pathlib import Path

from app.core.db import connect, sql_literal
from app.nocase.clean import CLEAN_FILE

UNITS_FILE = "nc_units.parquet"


def build(folder: Path) -> dict:
    con = connect()
    clean = folder / CLEAN_FILE
    ev = f"read_parquet({sql_literal(clean.as_posix())})"
    total, = con.execute(f"SELECT COUNT(*) FROM {ev}").fetchone()
    con.execute(f"""CREATE OR REPLACE TEMP TABLE __nc_units AS
        WITH u AS (
            SELECT app, screen_clean, action_clean, COUNT(*) AS events, COUNT(DISTINCT actor) AS actors,
                   SUM(active_s) / 3600 AS active_hours, MIN(ts) AS first_seen, MAX(ts) AS last_seen,
                   bool_or(is_generic) AS is_generic
            FROM {ev} GROUP BY 1, 2, 3),
        l AS (
            SELECT app, screen_clean, action_clean, l3, COUNT(*) AS n,
                   row_number() OVER (PARTITION BY app, screen_clean, action_clean ORDER BY COUNT(*) DESC, l3) AS r,
                   SUM(COUNT(*)) OVER (PARTITION BY app, screen_clean, action_clean) AS labelled
            FROM {ev} WHERE l3 IS NOT NULL GROUP BY 1, 2, 3, 4)
        SELECT printf('U%05d', row_number() OVER (ORDER BY u.events DESC, COALESCE(u.app, ''),
                                                  COALESCE(u.screen_clean, ''), COALESCE(u.action_clean, ''))) AS unit_id,
               u.app, u.screen_clean, u.action_clean, u.events, u.actors, u.active_hours, u.first_seen, u.last_seen,
               u.events / {max(total, 1)}::DOUBLE AS share, u.is_generic,
               l.l3 AS top_l3, l.n / l.labelled::DOUBLE AS top_l3_share, l.labelled AS labelled_events
        FROM u LEFT JOIN l ON l.r = 1 AND l.app IS NOT DISTINCT FROM u.app
             AND l.screen_clean IS NOT DISTINCT FROM u.screen_clean AND l.action_clean IS NOT DISTINCT FROM u.action_clean
        ORDER BY unit_id""")
    target = folder / UNITS_FILE
    con.execute(f"COPY (SELECT * FROM __nc_units ORDER BY unit_id) TO {sql_literal(target.as_posix())} (FORMAT parquet)")
    # unit_id onto the cleaned events (written to a new file, then swapped in)
    tmp = folder / (CLEAN_FILE + ".tmp")
    con.execute(f"""COPY (
        SELECT e.* EXCLUDE (unit_id), u.unit_id
        FROM (SELECT *, NULL::VARCHAR AS unit_id FROM {ev}) e JOIN __nc_units u
          ON u.app IS NOT DISTINCT FROM e.app AND u.screen_clean IS NOT DISTINCT FROM e.screen_clean
         AND u.action_clean IS NOT DISTINCT FROM e.action_clean
        ORDER BY e.eid) TO {sql_literal(tmp.as_posix())} (FORMAT parquet, COMPRESSION zstd)""")
    con.close()
    os.replace(tmp, clean)
    con = connect()
    units, events, labelled, consistent = con.execute("""
        SELECT COUNT(*), SUM(events), SUM(labelled_events), SUM(top_l3_share * labelled_events)
        FROM read_parquet(?)""", [target.as_posix()]).fetchone()
    assert int(events) == int(total), (events, total)          # every event is in exactly one unit
    consistency = None if not labelled else round(float(consistent) / float(labelled), 4)
    from app.nocase import profile
    caps = profile.load(folder)
    if caps is not None:
        caps["labels"]["consistency"] = consistency
        profile.write(folder, caps)
    return {"units": int(units), "events": int(events), "label_consistency": consistency}
