"""Decide at upload whether a file is a CASE-ID dataset or a NO-CASE dataset -> <job>/mode_decision.json.

It only decides and pre-fills page 1; it never changes how either mode calculates. Wrongly choosing case mode
invents cases (worse than missing one), so borderline evidence gives "confirm" (no-case, with a question).

    1 scope      process rows (a keep/drop filter, a process flag such as is_process, or filled level labels),
                 else all rows; the base used is recorded
    2 candidates every column, plus a name hint (case, ticket, order ...) that never passes a column by itself
    3 tests      coverage, not an event counter, repeats, not a person, not a time or a constant, sensible span
    4 IDs in text when no column passes: ID shapes in text columns, with the case-link thresholds of the
                 no-case mode (coverage, collisions, span); "Derive it" is pre-filled only when one of its
                 ID patterns expresses the shape, else case_ready
    5 decision   case (with margin) | confirm | partial | no_case
Column names and statistics only: no value is ever stored. Uses DuckDB aggregates; only the "rises with time"
check samples (fixed seed).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from app.core.config import data_dir, settings
from app.core.db import quote_ident, sql_literal

DECISION_FILE = "mode_decision.json"
SCOPE = "__intake_scope"


def _cfg() -> dict:
    return settings()["intake"]


def _near(value: float, limit: float, higher_is_better: bool) -> bool:
    """Within intake.margin (relative) of the limit, on the passing side."""
    m = float(_cfg()["margin"])
    return value < limit * (1 + m) if higher_is_better else value > limit * (1 - m)


def _test(name: str, ok: bool, value, limit, reason: str, near: bool = False) -> dict:
    return {"test": name, "passed": bool(ok), "value": value, "limit": limit, "near_limit": bool(ok and near),
            "reason": None if ok else reason}


# --------------------------------------------------------------------------- scope and roles
def _columns(con) -> dict[str, str]:
    return {r[0]: str(r[1]).upper() for r in con.execute("DESCRIBE src").fetchall()
            if not r[0].startswith("_ingest_file")}


def roles(con, prof=None, nc_mapping=None) -> dict:
    """ts (column, format), actor, app and text columns: from the no-case mapping, else page 1's suggestions."""
    if nc_mapping is not None:
        m = nc_mapping.mapped()
        return {"ts": m.get("ts"), "ts_format": nc_mapping.ts_format, "actor": m.get("actor"), "app": m.get("app"),
                "text": [c for c in (m.get("text"), m.get("screen")) if c], "activity": m.get("action") or m.get("l3"),
                "tz": nc_mapping.timezone}
    from app.core.ingest import profile
    from app.core.mapping import suggest_mapping
    prof = prof or profile(con)
    s = suggest_mapping(prof)
    by = prof.by_name()
    ts = s["timestamp"].column
    actor = s["resource"].column
    if not actor:                       # a small file can leave the suggestion empty: fall back to the name
        from app.core.mapping import name_score
        named = [c.name for c in prof.columns if name_score(c.name, "resource") >= 1.0 and c.name != ts]
        actor = named[0] if named else None
    texts = [c for c in [s["window_title"].column] if c]
    texts += [c.name for c in sorted(prof.columns, key=lambda c: -c.distinct)
              if c.dtype.startswith(("VARCHAR", "STRING", "TEXT")) and c.name not in texts
              and c.name not in (ts, actor, "_ingest_file")][: int(_cfg()["text_columns_max"]) - len(texts)]
    return {"ts": ts, "ts_format": (by[ts].ts_format or "iso") if ts else None, "actor": actor,
            "app": s["application"].column, "text": texts, "activity": s["activity"].column, "tz": "UTC",
            "time_like": [c.name for c in prof.columns if c.ts_format and c.ts_parse_rate >= float(_cfg()["time_parse_rate"])]}


def scope_rule(cols: dict[str, str], nc_mapping=None) -> tuple[str, str]:
    """(SQL condition, the base in words)."""
    if nc_mapping is not None and (nc_mapping.keep_rows_where or nc_mapping.drop_rows_where):
        from app.nocase.ingest import drop_sql, keep_sql
        return f"({keep_sql(nc_mapping)}) AND NOT ({drop_sql(nc_mapping)})", "the rows kept by the mapping's filter"
    flags = [c for c in cols if c.lower() in {f.lower() for f in _cfg()["process_flag_columns"]}]
    if flags:
        q = quote_ident(flags[0])
        yes = ", ".join(sql_literal(v) for v in _cfg()["process_flag_values"])
        return f"lower(TRIM(CAST({q} AS VARCHAR))) IN ({yes})", f"process rows ({flags[0]} set)"
    labels = [c for c in cols if re.match(_cfg()["label_column_regex"], c, re.I)]
    if labels:
        cond = " OR ".join(f"NULLIF(TRIM(CAST({quote_ident(c)} AS VARCHAR)), '') IS NOT NULL" for c in labels)
        return f"({cond})", f"process rows (a level label is filled: {', '.join(labels)})"
    return "TRUE", "all rows"


# --------------------------------------------------------------------------- one candidate column
def test_column(con, col: str, dtype: str, r: dict, n: int, time_like: bool) -> dict:
    c = _cfg()
    q = quote_ident(col)
    hint = any(re.search(rf"(^|[^a-z]){re.escape(h)}([^a-z]|$)", col.lower()) for h in c["name_hints"])
    actor = f"CAST({quote_ident(r['actor'])} AS VARCHAR)" if r.get("actor") else "NULL"
    shared_max = int(settings()["nocase"]["case_link"]["max_actors"])
    filled, distinct, med, single, span, d_actor, pairs, shared, day_share, days = con.execute(f"""
        WITH v AS (SELECT NULLIF(TRIM(CAST({q} AS VARCHAR)), '') AS k, __ts, {actor} AS who FROM {SCOPE}),
             per AS (SELECT k, COUNT(*) AS n, (epoch(MAX(__ts)) - epoch(MIN(__ts))) / 86400 AS span
                     FROM v WHERE k IS NOT NULL GROUP BY k)
        SELECT (SELECT COUNT(k) FROM v), (SELECT COUNT(*) FROM per), (SELECT median(n) FROM per),
               (SELECT AVG(CASE WHEN n = 1 THEN 1.0 ELSE 0 END) FROM per), (SELECT median(span) FROM per),
               (SELECT COUNT(DISTINCT who) FROM v WHERE k IS NOT NULL),
               (SELECT COUNT(*) FROM (SELECT DISTINCT k, who FROM v WHERE k IS NOT NULL AND who IS NOT NULL)),
               (SELECT AVG(CASE WHEN p > {shared_max} THEN 1.0 ELSE 0 END) FROM (
                    SELECT COUNT(DISTINCT who) AS p FROM v WHERE k IS NOT NULL AND who IS NOT NULL
                    GROUP BY k, CAST(__ts AS DATE))),
               (SELECT median(dd) / NULLIF((SELECT COUNT(DISTINCT CAST(__ts AS DATE)) FROM v), 0) FROM (
                    SELECT COUNT(DISTINCT CAST(__ts AS DATE)) AS dd FROM v WHERE k IS NOT NULL GROUP BY k)),
               (SELECT COUNT(DISTINCT CAST(__ts AS DATE)) FROM v)
        """).fetchone()
    coverage = filled / n if n else 0.0
    tests = [_test("coverage", coverage >= float(c["min_case_coverage"]), round(coverage, 4),
                   float(c["min_case_coverage"]), f"filled in {100 * coverage:.0f}% of scope rows",
                   _near(coverage, float(c["min_case_coverage"]), True))]
    # not an event counter (nearly unique per row AND rising with time across all actors; sampled, fixed seed)
    counter = False
    if distinct and filled and distinct / filled >= float(c["counter"]["min_unique_share"]):
        inc = con.execute(f"""
            WITH s AS (SELECT TRY_CAST(TRIM(CAST({q} AS VARCHAR)) AS HUGEINT) AS v, __ts FROM {SCOPE}
                       WHERE {q} IS NOT NULL USING SAMPLE reservoir({int(c['counter']['sample_rows'])} ROWS)
                       REPEATABLE ({int(settings()['analysis']['random_seed'])})),
                 d AS (SELECT v - lag(v) OVER (ORDER BY __ts, v) AS step FROM s WHERE v IS NOT NULL)
            SELECT AVG(CASE WHEN step > 0 THEN 1.0 ELSE 0 END) FROM d WHERE step IS NOT NULL""").fetchone()[0]
        counter = inc is not None and inc >= float(c["counter"]["min_increasing_share"])
    tests.append(_test("not_an_event_counter", not counter, None, None, "an event counter, not a case"))
    tests.append(_test("repeats", (med or 0) >= float(c["min_median_events"]) and (single or 0) <= float(c["max_single_share"]),
                       {"median_events": med, "single_event_share": None if single is None else round(single, 4)},
                       {"median_events": c["min_median_events"], "single_event_share": c["max_single_share"]},
                       "almost every value appears once" if (single or 0) > float(c["max_single_share"])
                       else f"a median of {med or 0:g} event(s) per value",
                       _near(float(med or 0), float(c["min_median_events"]), True)
                       or _near(float(single or 0), float(c["max_single_share"]), False)))
    person = False
    if r.get("actor") and col != r["actor"] and distinct and d_actor:
        lo, hi = c["person_ratio"]
        person = lo <= distinct / d_actor <= hi and pairs / max(distinct, d_actor) <= float(c["person_pairs_ratio"])
    if r.get("actor") == col:
        person = True
    tests.append(_test("not_a_person", not person, None, None, "matches the user column"))
    # Added to the prompt's list (the safety rule): a duration or amount repeats and covers every row too.
    decimals = dtype.startswith(("DOUBLE", "FLOAT", "DECIMAL", "REAL")) or (con.execute(f"""
        SELECT AVG(CASE WHEN regexp_full_match(TRIM(CAST({q} AS VARCHAR)), '-?[0-9]*[.,][0-9]+') THEN 1.0 ELSE 0 END)
        FROM (SELECT {q} FROM {SCOPE} WHERE {q} IS NOT NULL LIMIT {int(c['measure_sample_rows'])})""").fetchone()[0]
                                                                              or 0) >= float(c["max_decimal_share"])
    tests.append(_test("not_a_measure", not decimals, None, None, "a number with decimals (a measure such as a "
                                                                   "duration or amount), not an ID"))
    # Added: a case ID is an identifier, never free text. A window title or comment repeats, covers every row
    # and can even look like a case (one title per document), but it is a screen, not a case.
    words = con.execute(f"""
        SELECT AVG(CASE WHEN len(string_split(TRIM(CAST({q} AS VARCHAR)), ' ')) >= 3 THEN 1.0 ELSE 0 END)
        FROM (SELECT {q} FROM {SCOPE} WHERE {q} IS NOT NULL LIMIT {int(c['measure_sample_rows'])})""").fetchone()[0] or 0
    tests.append(_test("not_free_text", words <= float(c["max_free_text_share"]), round(float(words), 4),
                       float(c["max_free_text_share"]),
                       f"{100 * words:.0f}% of the values are several words (free text such as a window title), "
                       "not an ID"))
    flat = time_like or dtype.startswith(("TIMESTAMP", "DATE", "TIME"))
    too_few = distinct < int(c["min_distinct"])
    too_many = n and distinct / n > float(c["max_distinct_share"])
    tests.append(_test("not_time_or_constant", not (flat or too_few or too_many), distinct, None,
                       "a date / time column" if flat else f"only {distinct} distinct values" if too_few
                       else "almost every row has its own value"))
    # Added to the prompt's list (the safety rule): an application or click name repeats, covers every row and
    # may span little time, but it is a category - few values, each used by many people on the same day.
    # Without a user column: a value present on most days of a file spanning at least category_min_days days.
    everyday = (shared is None and (days or 0) >= int(c["category_min_days"])
                and (day_share or 0) > float(c["max_value_day_share"]))
    category = distinct < int(c["min_case_values"]) or (shared or 0) > float(c["max_shared_value_days"]) or everyday
    tests.append(_test("not_a_category", not category,
                       {"distinct": int(distinct), "shared_value_days": None if shared is None else round(shared, 4)},
                       {"distinct": c["min_case_values"], "shared_value_days": c["max_shared_value_days"]},
                       f"only {distinct} values" if distinct < int(c["min_case_values"]) else
                       f"a typical value occurs on {100 * (day_share or 0):.0f}% of the days (a category, not a case)"
                       if everyday else
                       f"{100 * (shared or 0):.0f}% of its values are used by more than {shared_max} people on the same day "
                       "(a category such as an application, not a case)"))
    tests.append(_test("sensible_span", span is None or span <= float(c["max_span_days"]),
                       None if span is None else round(float(span), 3), float(c["max_span_days"]),
                       f"one value typically spans {span or 0:.0f} days",
                       span is not None and _near(float(span), float(c["max_span_days"]), False)))
    ok = all(t["passed"] for t in tests)
    score = coverage + min(1.0, float(med or 0) / float(c["repeat_score_events"])) * float(c["repeat_weight"]) \
        + (float(c["hint_weight"]) if hint else 0.0)
    return {"column": col, "name_hint": hint, "passed": ok, "near_limit": ok and any(t["near_limit"] for t in tests),
            "score": round(score, 4), "coverage": round(coverage, 4), "distinct": int(distinct),
            "median_events": med, "tests": tests,
            "fail_reason": next((t["reason"] for t in tests if not t["passed"]), None)}


# --------------------------------------------------------------------------- IDs inside text
def text_ids(con, r: dict, n: int, exclude: set[str] | None = None) -> dict | None:
    """The best ID shape in the text columns, tested like the no-case case-link test."""
    from app.nocase.profile import DATE_SHAPE, shape_sql, token_sql
    from app.nocase.strategies.case_link import shape_regex
    cl = settings()["nocase"]["case_link"]
    ids = settings()["nocase"]["id_shapes"]
    best = None
    for col in r.get("text") or []:
        if col in (exclude or set()):
            continue
        q = quote_ident(col)
        rows = con.execute(f"""
            WITH t AS (SELECT unnest({token_sql(f'CAST({q} AS VARCHAR)')}) AS tok FROM {SCOPE} WHERE {q} IS NOT NULL
                       USING SAMPLE reservoir({int(_cfg()['shape_sample_rows'])} ROWS) REPEATABLE ({int(settings()['analysis']['random_seed'])}))
            SELECT {shape_sql('tok')} AS shape, COUNT(*) FROM t
            WHERE length(tok) >= {int(ids['min_length'])} AND length(regexp_replace(tok, '[^0-9]', '', 'g')) >= {int(ids['min_digits'])}
            GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {int(cl['max_shapes'])}""").fetchall()
        for shape, _k in rows:
            if re.match(DATE_SHAPE, shape):
                continue
            rx = sql_literal(f"(^|[^A-Za-z0-9])({shape_regex(shape)})($|[^A-Za-z0-9])")
            actor = f"CAST({quote_ident(r['actor'])} AS VARCHAR)" if r.get("actor") else "NULL"
            app = f"CAST({quote_ident(r['app'])} AS VARCHAR)" if r.get("app") else "NULL"
            con.execute(f"""CREATE OR REPLACE TEMP TABLE __intake_ids AS
                SELECT NULLIF(regexp_extract(CAST({q} AS VARCHAR), {rx}, 2), '') AS id, __ts, {actor} AS who, {app} AS app,
                       NULLIF(regexp_extract(CAST({q} AS VARCHAR), {rx}, 2), '') = TRIM(CAST({q} AS VARCHAR)) AS whole
                FROM {SCOPE}""")
            whole = con.execute("SELECT AVG(CASE WHEN whole THEN 1.0 ELSE 0 END) FROM __intake_ids WHERE id IS NOT NULL"
                                ).fetchone()[0]
            if whole is not None and whole >= float(_cfg()["whole_value_share"]):
                continue                      # a column OF IDs: the column tests judge it, not "IDs in text"
            with_id, bad_ev, span = con.execute(f"""
                WITH bad AS (SELECT DISTINCT id FROM __intake_ids WHERE id IS NOT NULL AND who IS NOT NULL
                             GROUP BY id, CAST(__ts AS DATE) HAVING COUNT(DISTINCT who) > {int(cl['max_actors'])})
                SELECT COUNT(id), COUNT(*) FILTER (WHERE id IN (SELECT id FROM bad)),
                       (SELECT median(d) FROM (SELECT (epoch(MAX(__ts)) - epoch(MIN(__ts))) / 86400 AS d FROM __intake_ids
                        WHERE id IS NOT NULL AND id NOT IN (SELECT id FROM bad) GROUP BY id))
                FROM __intake_ids""").fetchone()
            per_app = [{"app": a, "coverage": round(k / e, 4), "share_of_rows": round(e / n, 4)}
                       for a, e, k in con.execute("SELECT app, COUNT(*), COUNT(id) FROM __intake_ids GROUP BY 1 "
                                                  "ORDER BY 3 DESC, 1").fetchall() if k and a is not None]
            sample = [x for (x,) in con.execute("SELECT DISTINCT id FROM __intake_ids WHERE id IS NOT NULL LIMIT "
                                                f"{int(_cfg()['pattern_check_ids'])}").fetchall()]
            derive = _derive_pattern(sample)          # the IDs are only checked here, never stored
            cov = (with_id - bad_ev) / n if n else 0.0
            res = {"column": col, "shape": shape, "coverage": round(cov, 4),
                   "conflict_share": round(bad_ev / with_id, 4) if with_id else 0.0,
                   "median_span_days": None if span is None else round(float(span), 3),
                   "span_ok": span is None or float(span) <= float(cl["max_span_days"]),
                   "per_app": per_app[:10], "derive_pattern": derive}
            if best is None or res["coverage"] > best["coverage"]:
                best = res
    con.execute("DROP TABLE IF EXISTS __intake_ids")
    return best


def _derive_pattern(ids: list[str]) -> str | None:
    """The page-1 "Derive it" pattern (derived_case.id_patterns) that matches (nearly) every found ID, if any."""
    if not ids:
        return None
    need = float(_cfg()["derive_pattern_match"])
    for name, p in settings()["derived_case"]["id_patterns"].items():
        rx = re.compile(p["regex"])
        if sum(1 for x in ids if rx.fullmatch(x) or (rx.search(x) and rx.search(x).group(0) == x)) / len(ids) >= need:
            return name
    return None


# --------------------------------------------------------------------------- decision
def detect(con, prof=None, nc_mapping=None) -> dict:
    """The decision for the file in the `src` view (see the module docstring)."""
    from app.core.mapping import timestamp_sql
    start = time.perf_counter()
    c = _cfg()
    cols = _columns(con)
    r = roles(con, prof, nc_mapping)
    cond, base = scope_rule(cols, nc_mapping)
    ts = timestamp_sql(r["ts"], r["ts_format"] or "iso", r.get("tz") or "UTC") if r.get("ts") else "CAST(NULL AS TIMESTAMP)"
    con.execute(f"CREATE OR REPLACE TEMP VIEW {SCOPE} AS SELECT *, {ts} AS __ts FROM src WHERE {cond}")
    n, total = con.execute(f"SELECT (SELECT COUNT(*) FROM {SCOPE}), (SELECT COUNT(*) FROM src)").fetchone()
    skip = {r.get("ts")} | ({nc_mapping.columns.get(k) for k in ("actor", "active_s", "wait_s", "ts_end")}
                            if nc_mapping is not None else set())
    candidates = [test_column(con, col, dtype, r, n, col in (r.get("time_like") or []))
                  for col, dtype in cols.items() if col not in skip]
    candidates.sort(key=lambda x: (-x["passed"], -x["score"], x["column"]))
    passing = [x for x in candidates if x["passed"]]
    best = passing[0] if passing else None
    out = {"scope_base": base, "scope_rows": int(n), "rows": int(total), "roles": {k: v for k, v in r.items() if k != "time_like"},
           "candidates": [{k: v for k, v in x.items()} for x in candidates]}
    text = None
    if best:
        margin_ok = best["coverage"] >= float(c["case_coverage_with_margin"]) and not best["near_limit"]
        decision = "case" if margin_ok else "confirm"
        out.update(decision=decision, column=best["column"], prefill={"case_mode": "Use a column", "column": best["column"]},
                   reasons=[f"Column {best['column']} covers {100 * best['coverage']:.0f}% of {base}; median "
                            f"{best['median_events'] or 0:g} events per case; "
                            + next(f"{100 * t['value']['single_event_share']:.0f}% single-event cases"
                                   for t in best["tests"] if t["test"] == "repeats")]
                   + ([] if margin_ok else ["near a limit: please confirm before using it as the case"]))
    else:
        counters = {x["column"] for x in candidates
                    if any(t["test"] == "not_an_event_counter" and not t["passed"] for t in x["tests"])}
        text = text_ids(con, r, n, counters)
        cl = settings()["nocase"]["case_link"]
        if text and text["coverage"] >= float(cl["min_coverage"]) and text["conflict_share"] < float(cl["max_conflict_share"]) \
                and text["span_ok"]:
            margin_ok = text["coverage"] >= float(c["case_coverage_with_margin"])
            if text["derive_pattern"]:
                decision = "case" if margin_ok else "confirm"
                out["prefill"] = {"case_mode": "Derive it", "method": "text_pattern", "column": text["column"],
                                  "pattern": text["derive_pattern"]}
            else:
                decision = "case_ready" if margin_ok else "confirm"
            out.update(decision=decision, reasons=[
                f"An ID ({text['shape']}) in {text['column']} covers {100 * text['coverage']:.0f}% of {base} with "
                f"{100 * text['conflict_share']:.1f}% conflicts"]
                + ([] if text["derive_pattern"] else ["'Derive it' has no pattern for this ID shape: case_ready"]))
        else:
            good = [a for a in (text or {}).get("per_app") or []
                    if a["coverage"] >= float(cl["min_coverage"]) and a["share_of_rows"] >= float(cl["partial_min_app_share"])]
            shown = [x for x in candidates if x["name_hint"] or x["coverage"] > 0]
            first = [x for x in shown if x["fail_reason"] == "an event counter, not a case"]   # the telling reason first
            why = [f"{x['column']}: {x['fail_reason']}" for x in first + [x for x in shown if x not in first]][
                :int(c["reasons_shown"])]
            if text:
                why.append(f"IDs in {text['column']} ({text['shape']}) cover only {100 * text['coverage']:.0f}% of {base}")
            else:
                why.append("no ID found in the text columns")
            if good:
                names = ", ".join(a["app"] for a in good)
                out.update(decision="partial", apps=[a["app"] for a in good],
                           reasons=why + [f"IDs cover {names} well: those rows could be analysed as cases"])
            else:
                out.update(decision="no_case", reasons=why)
    out["text_ids"] = text
    out["seconds"] = round(time.perf_counter() - start, 2)
    out["message"] = message(out)
    return out


def message(d: dict) -> str:
    if d["decision"] == "case":
        return "Detected: case ID mode. " + " ".join(d["reasons"])
    if d["decision"] == "case_ready":
        return "Detected: a case ID inside text (case-ready). " + " ".join(d["reasons"])
    if d["decision"] == "confirm":
        return (f"This file may have a case ID ({d.get('column') or (d.get('text_ids') or {}).get('column')}). "
                + " ".join(d["reasons"]))
    if d["decision"] == "partial":
        return f"Detected: no case ID for the whole file; IDs cover {', '.join(d['apps'])}. " + "; ".join(d["reasons"])
    return "Detected: no case ID. " + "; ".join(d["reasons"])


def unattended(d: dict) -> str:
    """The mode an unattended run uses: confirm (and partial) go to no-case."""
    return "case" if d["decision"] in ("case", "case_ready") else "no_case"


# --------------------------------------------------------------------------- files
def save(d: dict, folder: Path | None, column_names: list[str]) -> None:
    from app.nocase.canonical import layout_signature
    text = json.dumps(d, indent=2, default=str)
    if folder is not None:
        (Path(folder) / DECISION_FILE).write_text(text, encoding="utf-8")
    mem = data_dir() / "intake_decisions"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / f"{layout_signature(column_names)}.json").write_text(text, encoding="utf-8")


def remembered(column_names: list[str]) -> dict | None:
    from app.nocase.canonical import layout_signature
    p = data_dir() / "intake_decisions" / f"{layout_signature(column_names)}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def detect_and_save(con, folder: Path | None, prof=None, nc_mapping=None) -> dict:
    """Detect, compare with the decision remembered for this layout (a warning when it differs), save both."""
    names = list(_columns(con))
    before = remembered(names)
    d = detect(con, prof, nc_mapping)
    if before and (before.get("decision"), before.get("column")) != (d["decision"], d.get("column")):
        d["warning"] = (f"This layout was detected as {before.get('decision')}"
                        + (f" ({before.get('column')})" if before.get("column") else "")
                        + f" before; this file gives {d['decision']}.")
    d["remembered_before"] = bool(before)
    save(d, folder, names)
    return d
