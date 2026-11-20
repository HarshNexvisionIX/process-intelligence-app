"""Case-link test: is there a REAL case ID? -> nc_case_links.parquet (eid, case_id, source, used) + a decision.

    CaseColumn       case_id mapped and the profile's verdict is usable    the column
    IdPatternInText  ID shapes found by the profile                        IDs extracted from text / screen
    RecordJoin       a record export is given (--records)                  events linked by actor + time window
    NoCase           always                                                no case

An ID-in-text shape must pass, on the events in scope: coverage (overall and per app), collisions (one ID used
by more than nocase.case_link.max_actors actors on the same day is rejected) and a sensible span (median at
most max_span_days). Events WITHOUT an ID are never attached to a case by time closeness: they stay unlinked.

Decision: case when coverage >= min_coverage and conflicts < max_conflict_share (and spans are sensible);
partial when some applications are well covered (they can be analysed on their own); otherwise no_case.
IDs are kept only as salted hashes (derived.hash_case_sql), in local files; never in reports or AI payloads.
"""
from __future__ import annotations

import re

from app.core.db import quote_ident, sql_literal
from app.core.derived import hash_case_sql
from app.nocase.strategies.base import Context, Strategy, cfg, register

LINKS_FILE = "nc_case_links.parquet"
_COLS = "eid BIGINT, case_id VARCHAR, source VARCHAR, used BOOLEAN"


def _lcfg() -> dict:
    return cfg()["case_link"]


def shape_regex(shape: str) -> str:
    """AAA9999999 -> [A-Za-z]{3}[0-9]{7}; AAA-999999 -> [A-Za-z]{3}\-[0-9]{6} (other characters literally)."""
    out = []
    for m in re.finditer(r"A+|9+|[^A9]+", shape):
        part = m.group(0)
        out.append(f"[A-Za-z]{{{len(part)}}}" if part[0] == "A" else f"[0-9]{{{len(part)}}}" if part[0] == "9"
                   else re.escape(part))
    return "".join(out)


def _empty_links(con, ctx: Context) -> None:
    con.execute(f"COPY (SELECT * FROM (SELECT NULL::BIGINT eid, NULL::VARCHAR case_id, NULL::VARCHAR source, "
                f"NULL::BOOLEAN used) WHERE FALSE) TO {sql_literal(ctx.path(LINKS_FILE).as_posix())} (FORMAT parquet)")


class _CaseLink(Strategy):
    stage = "case_link"
    result: dict

    def quality(self, con, ctx):
        return self.result


def decide(coverage: float, conflicts: float, span_ok: bool, per_app: list[dict], label: str) -> tuple[str, list[str]]:
    c = _lcfg()
    reasons = []
    if coverage >= float(c["min_coverage"]) and conflicts < float(c["max_conflict_share"]) and span_ok:
        return "case", [f"{label} covers {100 * coverage:.1f}% of events (at least {100 * float(c['min_coverage']):.0f}% "
                        f"needed), {100 * conflicts:.1f}% conflicts"]
    reasons.append(f"{label} covers {100 * coverage:.1f}% of events (at least {100 * float(c['min_coverage']):.0f}% "
                   "needed for case mode)")
    if conflicts >= float(c["max_conflict_share"]):
        reasons.append(f"{100 * conflicts:.1f}% of the linked events have an ID used by more than {c['max_actors']} "
                       "people on one day")
    if not span_ok:
        reasons.append(f"an ID typically spans more than {c['max_span_days']} days")
    good = [a for a in per_app if a["coverage"] >= float(c["min_coverage"])
            and a["share_of_events"] >= float(c["partial_min_app_share"])]
    if good and conflicts < float(c["max_conflict_share"]) and span_ok:
        names = ", ".join(a["app"] for a in good)
        return "partial", reasons + [f"well covered in {names}: partial mode for {names}"]
    return "no_case", reasons


@register
class CaseColumn(_CaseLink):
    name = "CaseColumn"

    def applies(self, caps, ctx):
        case = caps.get("case_id")
        if not case:
            return False, "no case_id column is mapped"
        if case["verdict"] != "usable":
            return False, f"the mapped case_id is {case['verdict'].replace('_', ' ')}: " + "; ".join(case["reasons"])
        return True, "the mapped case_id passes the case checks"

    def run(self, con, ctx):
        con.execute(f"""COPY (SELECT eid, case_id, 'case_id column' AS source, TRUE AS used FROM {ctx.events_sql()}
                              WHERE case_id IS NOT NULL ORDER BY eid)
                        TO {sql_literal(ctx.path(LINKS_FILE).as_posix())} (FORMAT parquet)""")
        n, linked = con.execute(f"SELECT COUNT(*), COUNT(case_id) FROM {ctx.events_sql()}").fetchone()
        cov = linked / n if n else 0.0
        decision, reasons = decide(cov, 0.0, True, [], "the case_id column")
        self.result = {"decision": decision, "reasons": reasons, "coverage": round(cov, 4), "conflict_share": 0.0,
                       "linked_events": int(linked), "unlinked_events": int(n - linked)}
        return LINKS_FILE


@register
class IdPatternInText(_CaseLink):
    name = "IdPatternInText"

    def applies(self, caps, ctx):
        shapes = (caps.get("id_shapes") or {}).get("overall") or []
        return (bool(shapes), f"ID shapes found: {', '.join(s['shape'] for s in shapes[:3])}" if shapes
                else "no ID shapes in text, screen or action")

    def test_shape(self, con, ctx: Context, shape: str) -> dict:
        c = _lcfg()
        rx = sql_literal(f"(^|[^A-Za-z0-9])({shape_regex(shape)})($|[^A-Za-z0-9])")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE __nc_ids AS
            SELECT eid, ts, actor, app,
                   COALESCE(NULLIF(regexp_extract(text, {rx}, 2), ''), NULLIF(regexp_extract(screen, {rx}, 2), ''),
                            NULLIF(regexp_extract(action, {rx}, 2), '')) AS raw_id
            FROM {ctx.raw_sql()}""")
        n, with_id = con.execute("SELECT COUNT(*), COUNT(raw_id) FROM __nc_ids").fetchone()
        con.execute(f"""CREATE OR REPLACE TEMP TABLE __nc_bad AS
            SELECT DISTINCT raw_id FROM __nc_ids WHERE raw_id IS NOT NULL AND actor IS NOT NULL
            GROUP BY raw_id, CAST(ts AS DATE) HAVING COUNT(DISTINCT actor) > {int(c['max_actors'])}""")
        bad_events = con.execute("SELECT COUNT(*) FROM __nc_ids WHERE raw_id IN (SELECT raw_id FROM __nc_bad)"
                                 ).fetchone()[0]
        bad_ids, ids = con.execute("SELECT (SELECT COUNT(*) FROM __nc_bad), COUNT(DISTINCT raw_id) FROM __nc_ids"
                                   ).fetchone()
        span = con.execute("""SELECT median(d) FROM (SELECT (epoch(MAX(ts)) - epoch(MIN(ts))) / 86400 AS d FROM __nc_ids
                              WHERE raw_id IS NOT NULL AND raw_id NOT IN (SELECT raw_id FROM __nc_bad) GROUP BY raw_id)"""
                           ).fetchone()[0]
        per_app = [{"app": a or "(no app)", "events": int(e), "coverage": round(k / e, 4),
                    "share_of_events": round(e / n, 4)}
                   for a, e, k in con.execute("""SELECT app, COUNT(*), COUNT(raw_id) FROM __nc_ids GROUP BY 1
                                                 ORDER BY 3 DESC, 2 DESC, 1""").fetchall() if k]
        coverage = (with_id - bad_events) / n if n else 0.0
        return {"shape": shape, "events": int(n), "events_with_id": int(with_id), "coverage": round(coverage, 4),
                "conflict_share": round(bad_events / with_id, 4) if with_id else 0.0, "ids": int(ids),
                "rejected_ids": int(bad_ids), "median_span_days": None if span is None else round(float(span), 3),
                "span_ok": span is None or float(span) <= float(c["max_span_days"]), "per_app": per_app,
                "actor_known": bool(con.execute("SELECT COUNT(actor) FROM __nc_ids").fetchone()[0])}

    def run(self, con, ctx):
        shapes = [s["shape"] for s in (ctx.caps.get("id_shapes") or {}).get("overall") or []]
        tests = [self.test_shape(con, ctx, s) for s in shapes[:int(_lcfg()["max_shapes"])]]
        best = max(tests, key=lambda t: (t["coverage"], -t["conflict_share"], t["shape"]))
        decision, reasons = decide(best["coverage"], best["conflict_share"], best["span_ok"], best["per_app"],
                                   f"the ID shape {best['shape']}")
        if not best["actor_known"]:
            reasons.append("no actor column: ID collisions between people could not be checked")
        covered = {r.split("partial mode for ")[-1] for r in reasons if "partial mode for" in r}
        apps = [a.strip() for x in covered for a in x.split(",")]
        self.test_shape(con, ctx, best["shape"])                 # rebuild __nc_ids for the best shape
        used = "TRUE" if decision == "case" else (
            f"COALESCE(app, '(no app)') IN ({', '.join(sql_literal(a) for a in apps)})" if apps else "FALSE")
        con.execute(f"""COPY (SELECT eid, {hash_case_sql('raw_id')} AS case_id, {sql_literal(best['shape'])} AS source,
                                     {used} AS used
                              FROM __nc_ids WHERE raw_id IS NOT NULL AND raw_id NOT IN (SELECT raw_id FROM __nc_bad)
                              ORDER BY eid)
                        TO {sql_literal(ctx.path(LINKS_FILE).as_posix())} (FORMAT parquet)""")
        con.execute("DROP TABLE IF EXISTS __nc_ids")
        self.result = {"decision": decision, "reasons": reasons, "shape": best["shape"],
                       "coverage": best["coverage"], "conflict_share": best["conflict_share"],
                       "median_span_days": best["median_span_days"], "rejected_ids": best["rejected_ids"],
                       "partial_apps": apps, "per_app": best["per_app"][:10],
                       "linked_events": best["events_with_id"], "unlinked_events": best["events"] - best["events_with_id"],
                       "shapes_tested": [{k: v for k, v in t.items() if k != "per_app"} for t in tests]}
        return LINKS_FILE


@register
class RecordJoin(_CaseLink):
    """Events by the same actor inside exactly ONE record's time window get that record's ID (ambiguous: none)."""
    name = "RecordJoin"

    def applies(self, caps, ctx):
        if not ctx.records:
            return False, "no record export given (--records)"
        return (caps.get("fill", {}).get("actor", {}).get("rows", 0) > 0,
                "a record export with actor and time windows" if caps.get("fill", {}).get("actor", {}).get("rows")
                else "a record export needs an actor column in the events")

    def run(self, con, ctx):
        r = ctx.records
        cols = {"id": "record_id", "actor": "user", "start": "start_ts", "end": "end_ts", **(r.get("columns") or {})}
        path = sql_literal(str(r["file"]).replace("\\", "/"))
        reader = f"read_csv_auto({path}, all_varchar=true)"
        con.execute(f"""CREATE OR REPLACE TEMP TABLE __nc_rec AS
            SELECT {hash_case_sql(f"CAST({quote_ident(cols['id'])} AS VARCHAR)")} AS case_id,
                   {hash_case_sql(f"TRIM(CAST({quote_ident(cols['actor'])} AS VARCHAR))")} AS actor,
                   TRY_CAST({quote_ident(cols['start'])} AS TIMESTAMP) AS s, TRY_CAST({quote_ident(cols['end'])} AS TIMESTAMP) AS e
            FROM {reader}""")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE __nc_rj AS
            SELECT ev.eid, MIN(r.case_id) AS case_id, COUNT(*) AS n FROM {ctx.events_sql()} ev
            JOIN __nc_rec r ON r.actor = ev.actor AND ev.ts BETWEEN r.s AND r.e GROUP BY ev.eid""")
        con.execute(f"""COPY (SELECT eid, case_id, 'record export' AS source, TRUE AS used FROM __nc_rj WHERE n = 1
                              ORDER BY eid) TO {sql_literal(ctx.path(LINKS_FILE).as_posix())} (FORMAT parquet)""")
        total, = con.execute(f"SELECT COUNT(*) FROM {ctx.events_sql()}").fetchone()
        linked, amb = con.execute("SELECT COUNT(*) FILTER (WHERE n = 1), COUNT(*) FILTER (WHERE n > 1) FROM __nc_rj"
                                  ).fetchone()
        cov = linked / total if total else 0.0
        conflicts = amb / (linked + amb) if linked + amb else 0.0
        decision, reasons = decide(cov, conflicts, True, [], "the record export")
        self.result = {"decision": decision, "reasons": reasons, "coverage": round(cov, 4),
                       "conflict_share": round(conflicts, 4), "linked_events": int(linked),
                       "ambiguous_events": int(amb), "unlinked_events": int(total - linked)}
        return LINKS_FILE


@register
class NoCase(_CaseLink):
    name = "NoCase"

    def applies(self, caps, ctx):
        return True, "always possible"

    def run(self, con, ctx):
        caps = ctx.caps
        reasons = []
        if caps.get("case_id"):
            reasons.append(f"the mapped case_id is {caps['case_id']['verdict'].replace('_', ' ')}: "
                           + "; ".join(caps["case_id"]["reasons"]))
        for cnt in caps.get("counters") or []:
            if not (cnt.get("role") == "case_id" and caps.get("case_id")):      # said already, just above
                reasons.append(f"{cnt['column']} is an event counter, not a case")
        if not (caps.get("id_shapes") or {}).get("overall"):
            reasons.append("no ID found in window titles, screens or clicks")
        if not ctx.records:
            reasons.append("no record export given")
        _empty_links(con, ctx)
        total, = con.execute(f"SELECT COUNT(*) FROM {ctx.events_sql()}").fetchone()
        self.result = {"decision": "no_case", "reasons": reasons, "coverage": 0.0, "conflict_share": 0.0,
                       "linked_events": 0, "unlinked_events": int(total)}
        return LINKS_FILE


def message(d: dict) -> str:
    """"Case link: no_case — the ID shape AAA9999999 covers 26.2% of events; ..." (never an ID value)."""
    return f"Case link: {d['decision']} — " + "; ".join(d.get("reasons") or [])
