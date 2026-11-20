"""What the no-case data supports, measured, never assumed -> nc_capabilities.json.

    basics      rows, period, actors, apps; fill rate of every standard column
    case_id     (when mapped) events per case, span, single-event share -> usable | weak | not_a_case
    counters    integer columns that are event counters (ingest.id_column_checks), with the reason
    id_shapes   ID-like tokens in text / screen / action as SHAPES only (ABC0263894 -> AAA9999999): share of
                events and distinct count, overall and per app. Values are never stored.
    labels      fill rate of l1..l5 (consistency per unit is added by units.py)
    screen / action   useful-screen share, generic-action share
    time        active time measured / estimated, ts_end present, median gap per actor
    supports    the analyses possible, each with a reason
"""
from __future__ import annotations

import json
from pathlib import Path

from app.core.config import settings
from app.core.db import connect, sql_literal
from app.nocase.canonical import LABEL_COLUMNS, STANDARD_COLUMNS
from app.nocase.ingest import EVENTS_FILE, ID_COLUMNS_FILE, load_scope

CAPS_FILE = "nc_capabilities.json"
SHAPE_SOURCES = ("text", "screen", "action")
DATE_SHAPE = r"^9{1,4}([-/._:]9{1,4}){1,2}$"          # dates and clock times are not IDs


def _cfg() -> dict:
    return settings()["nocase"]


def _ev(folder: Path) -> str:
    return f"read_parquet({sql_literal((folder / EVENTS_FILE).as_posix())})"


def shape_sql(expr: str) -> str:
    """SQL: letters -> A, digits -> 9, everything else kept (the length too)."""
    return f"regexp_replace(regexp_replace({expr}, '[A-Za-z]', 'A', 'g'), '[0-9]', '9', 'g')"


def token_sql(expr: str) -> str:
    """SQL list of ID-candidate tokens in a text: letters/digits joined by - or _ (INV-123456 is one token)."""
    return f"regexp_extract_all({expr}, '[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)*')"


def id_shapes(con, ev: str, rows: int) -> dict:
    """Top ID shapes (share of events, distinct values) overall and per app; values are never returned."""
    cfg = _cfg()["id_shapes"]
    cols = [c for c in SHAPE_SOURCES
            if con.execute(f"SELECT COUNT({c}) FROM {ev}").fetchone()[0]]
    if not cols or not rows:
        return {"overall": [], "per_app": {}, "columns": cols, "any": {"events": 0, "share": 0.0}}
    parts = " UNION ALL ".join(f"SELECT eid, app, unnest({token_sql(c)}) AS tok FROM {ev} WHERE {c} IS NOT NULL"
                               for c in cols)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE __nc_tok AS
        SELECT eid, app, {shape_sql('tok')} AS shape, md5(tok) AS h FROM ({parts})
        WHERE length(tok) >= {int(cfg['min_length'])}
          AND length(regexp_replace(tok, '[^0-9]', '', 'g')) >= {int(cfg['min_digits'])}""")
    con.execute(f"DELETE FROM __nc_tok WHERE regexp_matches(shape, {sql_literal(DATE_SHAPE)})")
    top = int(cfg["top"])
    any_events = con.execute("SELECT COUNT(DISTINCT eid) FROM __nc_tok").fetchone()[0]
    overall = [{"shape": s, "events": int(e), "share": round(e / rows, 4), "distinct": int(d)}
               for s, e, d in con.execute(f"""SELECT shape, COUNT(DISTINCT eid) e, COUNT(DISTINCT h) d FROM __nc_tok
                                              GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {top}""").fetchall()]
    per_app: dict[str, list] = {}
    for app, s, e, d, n in con.execute(f"""
            WITH a AS (SELECT app, COUNT(*) n FROM {ev} GROUP BY 1),
                 t AS (SELECT app, shape, COUNT(DISTINCT eid) e, COUNT(DISTINCT h) d,
                              row_number() OVER (PARTITION BY app ORDER BY COUNT(DISTINCT eid) DESC, shape) r
                       FROM __nc_tok GROUP BY 1, 2)
            SELECT t.app, shape, e, d, a.n FROM t JOIN a ON a.app IS NOT DISTINCT FROM t.app
            WHERE r <= {int(cfg['top_per_app'])} ORDER BY t.app NULLS LAST, e DESC""").fetchall():
        per_app.setdefault(app or "(no app)", []).append(
            {"shape": s, "events": int(e), "share_of_app": round(e / n, 4), "distinct": int(d)})
    return {"overall": overall, "per_app": per_app, "columns": cols,
            "any": {"events": int(any_events), "share": round(any_events / rows, 4)}}


def case_verdict(con, ev: str, counters: list[dict]) -> dict | None:
    """Events per case, span, single-event share of a mapped case_id, and a verdict with reasons."""
    n_cases = con.execute(f"SELECT COUNT(DISTINCT case_id) FROM {ev}").fetchone()[0]
    if not n_cases:
        return None
    cfg = _cfg()["case_link"]
    med_events, med_span_h, single, fill = con.execute(f"""
        WITH c AS (SELECT case_id, COUNT(*) n, epoch(MAX(ts)) - epoch(MIN(ts)) span
                   FROM {ev} WHERE case_id IS NOT NULL GROUP BY 1)
        SELECT median(n), median(span) / 3600, AVG(CASE WHEN n = 1 THEN 1.0 ELSE 0 END),
               (SELECT AVG(CASE WHEN case_id IS NULL THEN 0.0 ELSE 1 END) FROM {ev})
        FROM c""").fetchone()
    reasons, verdict = [], "usable"
    counter = next((c for c in counters if c.get("role") == "case_id" and c["counter"]), None)
    if counter:
        verdict = "not_a_case"
        reasons.append(counter["reason"])
    if med_events < cfg["min_median_events"]:
        verdict = "not_a_case"
        reasons.append(f"a case has a median of {med_events:g} event(s)")
    if single >= float(cfg["not_a_case_single_share"]):
        verdict = "not_a_case"
        reasons.append(f"{100 * single:.0f}% of cases have one event")
    elif single > cfg["max_single_event_share"] and verdict == "usable":
        verdict = "weak"
        reasons.append(f"{100 * single:.0f}% of cases have one event")
    if fill < float(cfg["min_fill"]) and verdict == "usable":
        verdict = "weak"
        reasons.append(f"only {100 * fill:.0f}% of events have a case ID")
    if verdict == "usable":
        reasons.append(f"a median of {med_events:g} events per case over {med_span_h:.1f} h")
    return {"cases": int(n_cases), "median_events": float(med_events), "median_span_hours": round(float(med_span_h), 4),
            "single_event_share": round(float(single), 4), "fill": round(float(fill), 4), "verdict": verdict,
            "reasons": reasons}


def build(folder: Path) -> dict:
    """Measure the capabilities of the job in `folder`; writes and returns nc_capabilities.json."""
    con = connect()
    ev = _ev(folder)
    scope = load_scope(folder) or {}
    rows, first, last, actors, apps = con.execute(
        f"SELECT COUNT(*), MIN(ts), MAX(ts), COUNT(DISTINCT actor), COUNT(DISTINCT app) FROM {ev}").fetchone()
    fills = dict(zip(STANDARD_COLUMNS, con.execute(
        "SELECT " + ", ".join(f"COUNT({c})" for c in STANDARD_COLUMNS) + f" FROM {ev}").fetchone()))
    fill = {c: {"rows": int(n), "share": round(n / rows, 4) if rows else 0.0} for c, n in fills.items()}
    counters = json.loads((folder / ID_COLUMNS_FILE).read_text(encoding="utf-8")) \
        if (folder / ID_COLUMNS_FILE).exists() else []
    case = case_verdict(con, ev, counters) if fills["case_id"] else None
    shapes = id_shapes(con, ev, rows)

    generic = sorted({str(a).lower() for a in _cfg()["generic_actions"]})
    gen_share = con.execute(f"""SELECT AVG(CASE WHEN lower(action) IN ({', '.join(sql_literal(g) for g in generic)})
                                THEN 1.0 ELSE 0 END) FROM {ev} WHERE action IS NOT NULL""").fetchone()[0]
    med_gap = con.execute(f"""
        WITH g AS (SELECT actor, epoch(ts) - epoch(lag(ts) OVER (PARTITION BY actor ORDER BY ts, eid)) gap FROM {ev}
                   WHERE actor IS NOT NULL),
             a AS (SELECT actor, median(gap) m FROM g WHERE gap IS NOT NULL GROUP BY 1)
        SELECT median(m) FROM a""").fetchone()[0]
    labels = {c: fill[c] for c in LABEL_COLUMNS}
    mapped_levels = [c for c in LABEL_COLUMNS if fills[c]]
    lowest = mapped_levels[-1] if mapped_levels else None
    active_measured = fill["active_s"]["share"] >= float(_cfg()["active_time_min_fill"])
    caps = {
        "rows": int(rows), "period": {"first": str(first) if first else None, "last": str(last) if last else None},
        "actors": int(actors), "apps": int(apps), "fill": fill, "dataset_class": scope.get("dataset_class"),
        "case_id": case, "counters": [c for c in counters if c["counter"]], "id_columns": counters,
        "id_shapes": shapes,
        "labels": {"fill": labels, "lowest_level": lowest,
                   "lowest_fill": labels[lowest]["share"] if lowest else 0.0, "consistency": None},
        "screen": {"useful_share": fill["screen"]["share"] if fills["screen"] else fill["text"]["share"]},
        "action": {"generic_share": None if gen_share is None else round(float(gen_share), 4)},
        "time": {"active_time": "measured" if active_measured else "estimated", "ts_end": bool(fills["ts_end"]),
                 "median_gap_per_actor_s": None if med_gap is None else round(float(med_gap), 3)},
    }
    caps["supports"] = supports(caps)
    write(folder, caps)
    return caps


def supports(caps: dict) -> dict:
    """{analysis: {"possible": bool, "reason": str}}."""
    f = caps["fill"]
    has = {c: f[c]["rows"] > 0 for c in f}
    shapes = (caps.get("id_shapes") or {}).get("overall") or []
    case = caps.get("case_id")
    low_fill = caps["labels"]["lowest_fill"]
    min_fill = float(_cfg()["label_fill_min"])

    def row(ok: bool, yes: str, no: str) -> dict:
        return {"possible": bool(ok), "reason": yes if ok else no}
    return {
        "episodes": row(has["actor"] and has["ts"], "actor and time are mapped", "needs an actor column"),
        "switching": row(has["actor"] and has["app"], "actor and application are mapped",
                         "needs actor and application columns"),
        "source_step_labels": row(low_fill >= min_fill,
                                  f"{caps['labels']['lowest_level']} is {100 * low_fill:.1f}% filled",
                                  f"the lowest step-label level is only {100 * low_fill:.1f}% filled "
                                  f"(at least {100 * min_fill:.0f}% needed)" if caps["labels"]["lowest_level"]
                                  else "no step-label column is mapped"),
        "level4_5": row(has["screen"] or has["action"] or has["text"], "screen or action is mapped",
                        "needs a screen or action column"),
        "case_link_test": row(bool(shapes), f"ID shapes found (top {shapes[0]['shape']})" if shapes else "",
                              "no ID shapes found in the text (a system-of-record export would be needed)"),
        "case_analysis": row(bool(case and case["verdict"] == "usable"),
                             "the mapped case_id passes the case checks",
                             "no case_id mapped" if not case else
                             f"the mapped case_id is {case['verdict'].replace('_', ' ')}: " + "; ".join(case["reasons"])),
    }


def write(folder: Path, caps: dict) -> None:
    (folder / CAPS_FILE).write_text(json.dumps(caps, indent=2, default=str), encoding="utf-8")


def load(folder: Path) -> dict | None:
    p = folder / CAPS_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
