"""The evidence check: every statement of a process definition, checked against the data by code.

`check_definition(definition, job)` turns the definition into claims (C001, C002...).
Each claim has a plain-language text, the measures behind it, its support (cases),
a badge and a verdict decided by fixed rules (thresholds in settings.yaml -> evidence):

    Supported | Partly supported | Contradicted | No data
    ("Reported" for allowed loops and optional skips: their rates are shown, no verdict)

Claims: step exists, activity not in the definition, order of consecutive steps,
mandatory step, system per step, SLA, start / end step, loops and skips.
When the job's flow band is None, order claims are never Supported unless a
milestone chain (Prompt 5: milestones.json in the job folder) links the two steps.

The result is saved as evidence.json in the job folder; claims become facts of type
"claim" (facts.json) so the report and the AI can cite them.
"""
from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from app.core import jobs
from app.core.config import settings
from app.core.db import connect
from app.core.definition import ProcessDefinition
from app.core.facts import Fact, badge
from app.core.mining import build_log, format_hours
from app.core.reference import dependency, first_positions

SUPPORTED, PARTLY, CONTRADICTED, NO_DATA, REPORTED = (
    "Supported", "Partly supported", "Contradicted", "No data", "Reported")
VERDICTS = (SUPPORTED, PARTLY, CONTRADICTED, NO_DATA)
COLORS = {SUPPORTED: "#2f9e44", PARTLY: "#e8a317", CONTRADICTED: "#d63a3a", NO_DATA: "#9aa5b1",
          REPORTED: "#4a6fa5"}
WEAK_ORDER_NOTE = "order evidence is weak in this data; order claims can only be Partly or No data"
ORDER_KINDS = {"order"}


@dataclass
class Claim:
    id: str
    kind: str                    # step_exists | uncovered_activity | order | mandatory | system | sla | start | end | loop | skip
    statement: str               # what the definition says (no numbers from the data)
    text: str                    # the verdict in plain words, with the numbers
    verdict: str
    measures: dict = field(default_factory=dict)
    value: float | None = None   # the main number (a share 0-1, unless unit says otherwise)
    unit: str = "share"
    support: int = 0
    badge: str = "Exploratory"
    refs: dict = field(default_factory=dict)   # step_id, from_step, to_step, activity
    note: str = ""
    cites: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _cfg() -> dict:
    return settings()["evidence"]


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.0f}%"


def job_flow_band(folder: Path) -> str | None:
    """The job's step-level flow band: reference.json, else the analysis (mining.json)."""
    sources = [("reference.json", lambda d: (d.get("flow") or {}).get("band")),
               ("mining.json", lambda d: ((d.get("flow_confidence") or {}).get("data") or {}).get("band"))]
    sources.sort(key=lambda item: (folder / item[0]).stat().st_mtime if (folder / item[0]).exists() else 0, reverse=True)
    for name, get in sources:
        path = folder / name
        if path.exists():
            band = get(json.loads(path.read_text(encoding="utf-8")))
            if band:
                return band
    return None


def milestone_chains(folder: Path) -> list[dict]:
    """Milestone chains from Prompt 5 when present (milestones.json -> chains:
    [{id, from_activity, to_activity, cases, share}]); [] otherwise."""
    path = folder / "milestones.json"
    if not path.exists():
        return []
    try:
        chains = list(json.loads(path.read_text(encoding="utf-8")).get("chains") or [])
        expanded = []
        for chain in chains:
            steps = chain.get("steps", [])
            if not steps:
                expanded.append(chain)
            for i, a in enumerate(steps):
                for b in steps[i+1:]:
                    if a != b:
                        expanded.append({**chain, "from_activity": a, "to_activity": b})
        return expanded
    except (ValueError, AttributeError):
        return []


def _step_runs(con, folder: Path) -> str:
    """TEMP table druns(step_id, case_id, hours): one row per run of a step.

    A run = consecutive events of the same step in a case; its time = from the end of the
    event before the run (the start of the run for the first event of a case) to the end of
    the run's last event. Prompt 6 runs (step_runs.parquet: step_id or activity, hours) are
    used instead when present.
    """
    p6 = folder / "step_runs.parquet"
    if p6.exists():
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{p6.as_posix()}')").fetchall()]
        if "hours" in cols and ("step_id" in cols or "activity" in cols):
            key = "step_id" if "step_id" in cols else "m.step_id"
            join = "" if "step_id" in cols else " JOIN defmap m USING (activity)"
            con.execute(f"""CREATE OR REPLACE TEMP TABLE druns AS SELECT {key} AS step_id, case_id, hours
                            FROM read_parquet('{p6.as_posix()}'){join}""")
            return "druns"
    con.execute("""
        CREATE OR REPLACE TEMP TABLE druns AS
        WITH e AS (
            SELECT l.case_id, l.seq, l.ts_start, COALESCE(l.ts_end, l.ts_start) AS te, m.step_id,
                   lag(COALESCE(l.ts_end, l.ts_start)) OVER w AS prev_end, lag(m.step_id) OVER w AS prev_step
            FROM mlog l LEFT JOIN defmap m USING (activity)
            WINDOW w AS (PARTITION BY l.case_id ORDER BY l.seq)),
        r AS (
            SELECT *, prev_step IS DISTINCT FROM step_id AS first_of_run,
                   SUM(CASE WHEN prev_step IS DISTINCT FROM step_id THEN 1 ELSE 0 END)
                       OVER (PARTITION BY case_id ORDER BY seq) AS run
            FROM e)
        SELECT step_id, case_id, run,
               (epoch(MAX(te)) - epoch(MAX(CASE WHEN first_of_run THEN COALESCE(prev_end, ts_start) END))) / 3600.0
                   AS hours
        FROM r WHERE step_id IS NOT NULL GROUP BY step_id, case_id, run""")
    return "druns"


def check_definition(defn: ProcessDefinition, job: dict, con: duckdb.DuckDBPyConnection | None = None) -> dict:
    """Check every statement of `defn` against the job's data. Returns the evidence:

    {"claims": [Claim dicts], "score": {...}, "step_stats": {step_id: {...}}, "stats": {...},
     "flow_band", "definition": {name, version, status}, "checked_at"}
    """
    cfg = _cfg()
    folder = jobs.job_folder(job["job_id"])
    con = con or connect()
    build_log(con, folder / "events.parquet")
    first_positions(con)
    n_cases, first, last = con.execute("SELECT COUNT(*), MIN(start_ts), MAX(start_ts) FROM mcases").fetchall()[0]
    n_cases = n_cases or 0
    band = job_flow_band(folder)
    chains = milestone_chains(folder)
    a2s = defn.activity_to_step()
    con.register("defmap", pd.DataFrame({"activity": pd.Series(list(a2s), dtype=object),
                                         "step_id": pd.Series(list(a2s.values()), dtype=object)}))
    con.execute("""CREATE OR REPLACE TEMP TABLE dstep AS
                   SELECT r.case_id, m.step_id, MIN(r.first_seq) AS fs FROM rfirst r JOIN defmap m USING (activity)
                   GROUP BY 1, 2""")
    con.execute("""CREATE OR REPLACE TEMP TABLE dstepmax AS
                   SELECT l.case_id, m.step_id, MAX(l.seq) AS ls FROM mlog l JOIN defmap m USING (activity)
                   GROUP BY 1, 2""")
    reach = dict(con.execute("SELECT step_id, COUNT(*) FROM dstep GROUP BY 1").fetchall())
    present = {a for (a,) in con.execute("SELECT DISTINCT activity FROM rfirst").fetchall()}
    _step_runs(con, folder)
    runs: dict[str, list[float]] = {}
    for sid, h in con.execute("SELECT step_id, hours FROM druns WHERE hours IS NOT NULL").fetchall():
        runs.setdefault(sid, []).append(max(float(h), 0.0))

    claims: list[Claim] = []

    def add(kind, statement, text, verdict, value=None, support=0, unit="share", note="", cites=None, **refs):
        c = Claim(id="", kind=kind, statement=statement, text=text, verdict=verdict, value=value, unit=unit,
                  support=int(support or 0), refs=refs, note=note, cites=list(cites or []))
        c.badge = badge(value, c.support)
        c.measures = {"value": value, "support": c.support}
        claims.append(c)
        return c

    steps = defn.ordered_steps()
    name = {s["id"]: s["name"] for s in steps}

    # 1. Step exists
    for s in steps:
        mapped = [a for a in s.get("mapped_activities") or [] if a in present]
        c = reach.get(s["id"], 0)
        share = c / n_cases if n_cases else 0.0
        st = f"The step {s['name']} happens in this process."
        if not s.get("mapped_activities"):
            add("step_exists", st, f"{s['name']} is not mapped to any activity in the data.", NO_DATA,
                None, 0, step_id=s["id"])
        elif not mapped:
            add("step_exists", st, f"None of the activities mapped to {s['name']} occur in the data.", NO_DATA,
                0.0, 0, step_id=s["id"])
        elif share >= cfg["min_case_share"]:
            add("step_exists", st, f"{s['name']} is seen in {_pct(share)} of cases ({c:,} cases).", SUPPORTED,
                share, c, step_id=s["id"])
        else:
            add("step_exists", st, f"{s['name']} is rarely seen: {_pct(share)} of cases ({c:,} cases).", PARTLY,
                share, c, step_id=s["id"])

    # 2. Activities in the data that are not in the definition
    for act, c in con.execute("SELECT activity, COUNT(*) FROM rfirst GROUP BY 1 ORDER BY 2 DESC, 1").fetchall():
        share = c / n_cases if n_cases else 0.0
        if act not in a2s and share >= cfg["uncovered_activity_share"]:
            add("uncovered_activity", f"The definition covers the activity {act}.",
                f"Activity {act} is not in the definition, but occurs in {_pct(share)} of cases ({c:,} cases).",
                CONTRADICTED, share, c, activity=act)

    # 3. Order of consecutive steps ("next" flows), loops back, allowed loops, optional skips
    flows = defn.effective_flows()
    pos = {s["id"]: s.get("position") for s in steps}
    for f in flows:
        a, b = f["from_step"], f["to_step"]
        if a not in name or b not in name:
            continue
        n_ab, n_ba, both, loops = con.execute("""
            SELECT COUNT(*) FILTER (WHERE x.fs < y.fs), COUNT(*) FILTER (WHERE y.fs < x.fs), COUNT(*),
                   COUNT(*) FILTER (WHERE x.fs < y.fs AND xm.ls > y.fs)
            FROM dstep x JOIN dstep y ON x.case_id = y.case_id AND x.step_id = ? AND y.step_id = ?
            JOIN dstepmax xm ON xm.case_id = x.case_id AND xm.step_id = x.step_id""", [a, b]).fetchall()[0]
        if f["kind"] == "allowed_loop":
            with_a = reach.get(a, 0)
            back = con.execute("""SELECT COUNT(*) FROM dstep x JOIN dstepmax ym ON x.case_id = ym.case_id
                                  AND x.step_id = ? AND ym.step_id = ? AND ym.ls > x.fs""", [a, b]).fetchall()[0][0]
            rate = back / with_a if with_a else None
            add("loop", f"After {name[a]}, cases may go back to {name[b]} (allowed loop).",
                f"{_pct(rate)} of cases with {name[a]} go back to {name[b]} afterwards." if rate is not None
                else f"No case contains {name[a]}.", REPORTED, rate, with_a, from_step=a, to_step=b)
            continue
        if f["kind"] == "optional_skip":
            between = [s["id"] for s in steps if pos[a] < s["position"] < pos[b]]
            skipped = con.execute(f"""
                SELECT COUNT(*) FROM (SELECT case_id FROM dstep WHERE step_id = ? INTERSECT
                                      SELECT case_id FROM dstep WHERE step_id = ?) c
                WHERE NOT EXISTS (SELECT 1 FROM dstep d WHERE d.case_id = c.case_id
                                  AND d.step_id IN (SELECT unnest(?::VARCHAR[])))""", [a, b, between]).fetchall()[0][0]
            rate = skipped / both if both else None
            add("skip", f"Cases may go from {name[a]} straight to {name[b]} (optional skip).",
                f"{_pct(rate)} of cases with both steps skip the steps between them." if rate is not None
                else f"No case contains both {name[a]} and {name[b]}.", REPORTED, rate, both, from_step=a, to_step=b)
            continue
        st = f"{name[a]} comes before {name[b]}."
        dep = dependency(n_ab, n_ba)
        share = n_ab / (n_ab + n_ba) if (n_ab + n_ba) else None
        chain = next((ch for ch in chains
                      if ch.get("from_activity") in (defn.step(a) or {}).get("mapped_activities", [])
                      and ch.get("to_activity") in (defn.step(b) or {}).get("mapped_activities", [])), None)
        note = WEAK_ORDER_NOTE if band == "None" else ""
        reverse_chain = next((ch for ch in chains
                              if ch.get("from_activity") in (defn.step(b) or {}).get("mapped_activities", [])
                              and ch.get("to_activity") in (defn.step(a) or {}).get("mapped_activities", [])), None)
        if both < cfg["order_min_cases"]:
            verdict, text = NO_DATA, f"Too few cases contain both {name[a]} and {name[b]} ({both:,} cases)."
        elif dep <= -cfg["order_dep_supported"]:
            verdict = CONTRADICTED
            text = f"{name[b]} usually comes before {name[a]}: only {_pct(share)} of {both:,} cases follow the declared order."
        elif dep >= cfg["order_dep_supported"] and share is not None and share >= cfg["order_pair_share_supported"]:
            verdict, text = SUPPORTED, f"{_pct(share)} of {both:,} cases do {name[a]} before {name[b]}."
        elif abs(dep) < cfg["order_dep_balanced"]:
            verdict = PARTLY
            text = f"{name[a]} and {name[b]} are done in both orders ({_pct(share)} of {both:,} cases in the declared order)."
        else:
            verdict = PARTLY
            text = f"{_pct(share)} of {both:,} cases do {name[a]} before {name[b]}: a partial order."
        cites = []
        if reverse_chain is not None:
            verdict = CONTRADICTED
            cites = [str(reverse_chain.get("id"))]
            text += f" Milestone chain {reverse_chain.get('id')} runs against the declared order."
        elif chain is not None:
            verdict = SUPPORTED
            cites = [str(chain.get("id"))]
            text += f" Milestone chain {chain.get('id')} links the two steps."
        elif band == "None" and verdict == SUPPORTED:
            verdict = PARTLY
            text += " The data shows no reliable order overall, so this cannot be fully supported."
        c = add("order", st, text, verdict, share, both, note=note, cites=cites, from_step=a, to_step=b)
        c.measures.update(dependency=round(dep, 3), n_ab=n_ab, n_ba=n_ba)
        loop_share = loops / n_ab if n_ab else 0.0
        if n_ab >= cfg["order_min_cases"] and loop_share >= cfg["loop_contradiction_share"]:
            add("loop", f"After {name[b]}, cases do not go back to {name[a]}.",
                f"{_pct(loop_share)} of cases go back to {name[a]} after {name[b]}, but the definition has no "
                "allowed loop here.", CONTRADICTED, loop_share, n_ab, from_step=a, to_step=b)

    # 4. Mandatory steps (in cases that reach the end step)
    if steps:
        end = steps[-1]["id"]
        end_cases = reach.get(end, 0)
        for s in steps:
            if not s.get("mandatory") or s["id"] == end:
                continue
            st = f"{s['name']} is mandatory."
            if not end_cases:
                add("mandatory", st, f"No case reaches the last step {name[end]}.", NO_DATA, None, 0, step_id=s["id"])
                continue
            with_s = con.execute("""SELECT COUNT(*) FROM dstep e JOIN dstep x ON e.case_id = x.case_id
                                    AND e.step_id = ? AND x.step_id = ?""", [end, s["id"]]).fetchall()[0][0]
            share = with_s / end_cases
            verdict = SUPPORTED if share >= cfg["mandatory_supported"] else (
                PARTLY if share >= cfg["mandatory_partly"] else CONTRADICTED)
            add("mandatory", st, f"{_pct(share)} of the {end_cases:,} cases that reach {name[end]} contain {s['name']}.",
                verdict, share, end_cases, step_id=s["id"])

    # 5. System per step
    has_apps = con.execute("SELECT COUNT(application) FROM mlog").fetchall()[0][0] > 0
    for s in steps:
        if not s.get("system"):
            continue
        st = f"{s['name']} is done in {s['system']}."
        rows = con.execute("""SELECT application, COUNT(*) AS n FROM mlog l JOIN defmap m USING (activity)
                              WHERE m.step_id = ? AND application IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1""",
                           [s["id"]]).fetchall()
        total = sum(n for _, n in rows)
        if not has_apps or not total:
            add("system", st, "No application is recorded for this step's events.", NO_DATA, None, 0,
                step_id=s["id"])
            continue
        declared = sum(n for app, n in rows if str(app).strip().lower() == s["system"].strip().lower())
        share = declared / total
        main = rows[0][0]
        cases = reach.get(s["id"], 0)
        if share >= cfg["system_supported"]:
            add("system", st, f"{_pct(share)} of the step's events are in {s['system']}.", SUPPORTED, share, cases,
                step_id=s["id"], main_app=main)
        elif share >= cfg["system_partly"]:
            add("system", st, f"Only {_pct(share)} of the step's events are in {s['system']}; the main application "
                f"is {main}.", PARTLY, share, cases, step_id=s["id"], main_app=main)
        else:
            add("system", st, f"{_pct(share)} of the step's events are in {s['system']}; the main application is "
                f"{main}.", CONTRADICTED, share, cases, step_id=s["id"], main_app=main)

    # 6. SLA
    for s in steps:
        if s.get("sla_hours") is None:
            continue
        st = f"{s['name']} is done within {format_hours(s['sla_hours'])} (SLA)."
        vals = runs.get(s["id"], [])
        if not vals:
            add("sla", st, "No run of this step in the data.", NO_DATA, None, 0, step_id=s["id"])
            continue
        met = sum(1 for v in vals if v <= s["sla_hours"]) / len(vals)
        verdict = SUPPORTED if met >= cfg["sla_target_share"] else (
            PARTLY if met >= cfg["sla_partly_share"] else CONTRADICTED)
        add("sla", st, f"SLA met in {_pct(met)} of {len(vals):,} runs.", verdict, met, reach.get(s["id"], 0),
            step_id=s["id"], runs=len(vals))

    # 6b. The standard way inside a step (sub-processes), for steps linked to a saved standard
    from app.core import definition as dmod
    from app.core.subprocess import ACTIONS_FILE, compare, runs_of
    standard_share: dict[str, float] = {}
    linked = dmod.linked_standards(job, defn)
    actions_path = folder / ACTIONS_FILE
    for s in steps:
        std = linked.get(s["id"])
        if not std:
            continue
        st = f"Runs of {s['name']} follow the standard way."
        runs = runs_of(con, actions_path, std["key"]) if actions_path.exists() else None
        if runs is None or not len(runs):
            add("standard", st, "No runs with recorded actions for this step.", NO_DATA, None, 0, step_id=s["id"])
            continue
        comp = compare(runs, std["standard_path"], std.get("mandatory_actions"), bool(std.get("confirmed_by_business")))
        share = comp["standard_share"]
        standard_share[s["id"]] = share
        verdict = SUPPORTED if share >= cfg["standard_supported"] else (
            PARTLY if share >= cfg["standard_partly"] else CONTRADICTED)
        add("standard", st, f"{_pct(share)} of {len(runs):,} runs follow the standard way.", verdict, share,
            int(runs["case_id"].nunique()), step_id=s["id"], runs=len(runs))
        for action in std.get("mandatory_actions") or []:
            done = sum(1 for p in runs["path"] if action in list(p)) / len(runs)
            verdict = SUPPORTED if done >= cfg["mandatory_supported"] else (
                PARTLY if done >= cfg["mandatory_partly"] else CONTRADICTED)
            add("mandatory_action", f"Action {action} is done in every run of {s['name']}.",
                f"{_pct(done)} of {len(runs):,} runs contain {action}.", verdict, done,
                int(runs["case_id"].nunique()), step_id=s["id"], action=action)

    # 7. Start and end
    top = int(cfg["start_end_top"])
    for kind, col, step in (("start", "start_activity", steps[0] if steps else None),
                            ("end", "end_activity", steps[-1] if steps else None)):
        if step is None:
            continue
        tops = [a for a, _ in con.execute(f"SELECT {col}, COUNT(*) FROM mcases GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {top}")
                .fetchall()]
        st = f"The process {'starts' if kind == 'start' else 'ends'} with {step['name']}."
        mapped = step.get("mapped_activities") or []
        if not mapped or not n_cases:
            add(kind, st, "The step is not mapped to activities in the data.", NO_DATA, None, 0, step_id=step["id"])
            continue
        n = con.execute(f"SELECT COUNT(*) FROM mcases WHERE {col} IN (SELECT unnest(?::VARCHAR[]))",
                        [mapped]).fetchall()[0][0]
        ok = any(a in tops for a in mapped)
        word = "start" if kind == "start" else "end"
        add(kind, st, f"{step['name']} is {'among' if ok else 'not among'} the {top} most common {word} activities "
            f"({_pct(n / n_cases)} of cases {word} with it).", SUPPORTED if ok else CONTRADICTED, n / n_cases, n_cases,
            step_id=step["id"])

    if band == "None":
        for c in claims:
            if c.kind in ORDER_KINDS and not c.note:
                c.note = WEAK_ORDER_NOTE
    for i, c in enumerate(claims, 1):
        c.id = f"C{i:03d}"

    step_stats = {}
    for s in steps:
        vals = sorted(runs.get(s["id"], []))
        sla = next((c.value for c in claims if c.kind == "sla" and c.refs.get("step_id") == s["id"]), None)
        step_stats[s["id"]] = {"cases": reach.get(s["id"], 0),
                               "share": round(reach.get(s["id"], 0) / n_cases, 4) if n_cases else 0.0,
                               "median_h": vals[len(vals) // 2] if vals else None, "sla_met": sla,
                               "standard_share": standard_share.get(s["id"])}
    return {"claims": [c.to_dict() for c in claims], "score": score(claims),
            "step_stats": step_stats, "flow_band": band,
            "stats": {"cases": n_cases, "first_month": first.strftime("%Y-%m") if first else None,
                      "last_month": last.strftime("%Y-%m") if last else None},
            "definition": {"name": defn.name, "version": defn.version, "status": defn.status},
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def score(claims) -> dict:
    """{"total", "supported", "partly", "contradicted", "no_data"} over claims with a verdict."""
    vs = [c.verdict if isinstance(c, Claim) else c["verdict"] for c in claims]
    counted = [v for v in vs if v in VERDICTS]
    return {"total": len(counted), "supported": counted.count(SUPPORTED), "partly": counted.count(PARTLY),
            "contradicted": counted.count(CONTRADICTED), "no_data": counted.count(NO_DATA)}


def score_line(evidence: dict) -> str:
    s = evidence["score"]
    return f"{s['supported']} of {s['total']} claims supported, {s['contradicted']} contradicted"


def summary_line(evidence: dict) -> str:
    """"Claims: N · supported S · partly P · contradicted C · no data D" (command line)."""
    s = evidence["score"]
    return (f"Claims: {s['total']} · supported {s['supported']} · partly {s['partly']} · "
            f"contradicted {s['contradicted']} · no data {s['no_data']}")


def top_contradicted(evidence: dict, n: int = 5) -> list[dict]:
    return sorted((c for c in evidence["claims"] if c["verdict"] == CONTRADICTED),
                  key=lambda c: (-(c.get("support") or 0), c["id"]))[:n]


def save_evidence(evidence: dict, folder: Path) -> Path:
    path = folder / "evidence.json"
    path.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    return path


def definition_fingerprint(defn: ProcessDefinition) -> str:
    content = defn.to_dict()
    for key in ("status", "approved_by", "approved_at", "change_log", "source"):
        content.pop(key, None)
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def load_evidence(folder: Path, definition: ProcessDefinition | None = None) -> dict | None:
    path = folder / "evidence.json"
    if not path.exists():
        return None
    result = json.loads(path.read_text(encoding="utf-8"))
    if definition is not None:
        if result.get("definition_fingerprint") != definition_fingerprint(definition):
            return None
        if any(p.exists() and p.stat().st_mtime > path.stat().st_mtime
               for p in (folder / "events.parquet", folder / "mining.json", folder / "milestones.json")):
            return None
    return result


def claim_facts(evidence: dict | None) -> list[Fact]:
    """Claims as facts (type "claim", IDs C001...) for the report and the AI."""
    out = []
    for c in (evidence or {}).get("claims") or []:
        number = "" if c.get("value") is None else f" ({_pct(c['value'])})" if c.get("unit") == "share" else ""
        out.append(Fact(id=c["id"], type="claim", label=c["statement"], value=None, unit="count",
                        support=c.get("support") or 0, badge=c.get("badge") or "Exploratory", section="map",
                        source="evidence check of the business process definition",
                        text=f"{c['verdict']}{number}", meta={"verdict": c["verdict"], "kind": c["kind"],
                                                              **(c.get("refs") or {})}))
    return out


def run_check(job: dict, defn: ProcessDefinition) -> dict:
    """Check, save evidence.json, link the job to the definition, and add the claims to facts.json."""
    from app.core import definition as dmod
    from app.core.facts import load_facts, save_facts
    folder = jobs.job_folder(job["job_id"])
    evidence = check_definition(defn, job)
    evidence["definition_fingerprint"] = definition_fingerprint(defn)
    dmod.save(defn)
    save_evidence(evidence, folder)
    dmod.link_job(job, defn)
    if (folder / "facts.json").exists():
        facts = [f for f in load_facts(folder) if f.type != "claim"] + claim_facts(evidence)
        save_facts(facts, folder)
    return evidence
