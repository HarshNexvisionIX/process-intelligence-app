"""Completion actions: capture privately, label explicitly, and measure sequences in SQL."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path

import pandas as pd

from app.core.config import data_dir, settings
from app.core.db import connect, sql_literal


def atomic_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path


NAME_TOKEN = "<name>"


@lru_cache(maxsize=1)
def first_names() -> frozenset[str]:
    """Common first names (config/first_names.txt, lower case): mark person names inside labels."""
    from app.core.config import PROJECT_ROOT
    path = PROJECT_ROOT / settings()["level4"]["first_names_file"]
    if not path.exists():
        return frozenset()
    return frozenset(w.strip().lower() for w in path.read_text(encoding="utf-8").splitlines()
                     if w.strip() and not w.startswith("#"))


def mask_names(value: str) -> str:
    """Replace a person name ANYWHERE in a label with "<name>": a capitalised first name (from the list)
    followed by a capitalised word, e.g. "Select Priya Sharma Profile" -> "Select <name> Profile"."""
    words, names, out, i = value.split(), first_names(), [], 0
    while i < len(words):
        w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if (w[:1].isupper() and w.strip(".,:;").lower() in names and nxt[:1].isupper()
                and re.fullmatch(r"[A-Za-z'-]+[.,:;]?", nxt)):
            if not out or out[-1] != NAME_TOKEN:
                out.append(NAME_TOKEN)
            i += 2
            continue
        out.append(w)
        i += 1
    return " ".join(out)


def scrub_action(value: str | None) -> str | None:
    """An action label without #hex# tokens, long hashes, emails or 6+ digit runs, with person names
    inside it replaced by "<name>"; None when nothing (or only "<name>") is left or the whole
    label is a person's name (looks_like_person). Callers that must keep every event store None as
    OTHER_CLICK ("<other click>"), never drop the row."""
    if not value:
        return None
    value = re.sub(r"#[a-fA-F0-9]+#|\b[a-fA-F0-9]{32,}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\d{6,}", "", value)
    value = mask_names(" ".join(value.split()))
    if not value.replace(NAME_TOKEN, "").strip():
        return None
    if looks_like_person(value):
        return None
    return value


OTHER_CLICK = "<other click>"


def looks_like_person(value: str) -> bool:
    """A whole click label that is a person's name. Only when (a) its first word is a known first name
    (config/first_names.txt) and it has the shape of a name ("John Smith"), or (b) it is in the
    user-supplied list milestones.person_names - and in both cases NONE of its words is an action word
    (mapping._ACTION_WORDS, milestones.verbs) or a UI word (screens.vocabulary). So "Core Activity",
    "Work Notes" and "Worker Profile" are clicks, not people."""
    from app.core.ingest import PERSON_RE
    from app.core.mapping import _ACTION_WORDS
    from app.core.screens import vocabulary
    words = [w.strip(".,:;'\"()") for w in value.split()]
    if not words or len(words) > 4:
        return False
    ui = set(_ACTION_WORDS) | {str(v).lower() for v in settings()["milestones"]["verbs"]} | vocabulary()
    if any(w.lower() in ui for w in words):
        return False
    listed = {str(n).strip().lower() for n in settings()["milestones"].get("person_names") or []}
    if value.strip().lower() in listed:
        return True
    return bool(PERSON_RE.fullmatch(value.strip())) and words[0].lower() in first_names()


def clean_action(value: str | None) -> str | None:
    value = scrub_action(value)
    if value is None:
        return None
    verbs = settings()["milestones"]["verbs"]
    return value if re.search(r"\b(?:" + "|".join(re.escape(v) for v in verbs) + r")\b", value, re.I) else None


def capture(con, query: str, path: Path) -> dict:
    """Filter cleaned labels on all scoped rows, before merging. Never copy raw actions."""
    cfg = settings()["milestones"]
    con.execute(f"CREATE OR REPLACE TEMP VIEW ms_capture AS {query}")
    names = [(r[0], clean_action(r[0])) for r in con.execute("SELECT DISTINCT __action FROM ms_capture").fetchall()]
    con.register("ms_clean_names", pd.DataFrame(names, columns=["raw", "clean"]))
    try:
        con.execute(f"""COPY (
            WITH cleaned AS (
                SELECT ts_start AS ts, case_id, resource, activity, clean AS action
                FROM ms_capture JOIN ms_clean_names ON __action = raw
                WHERE clean IS NOT NULL AND case_id IS NOT NULL AND activity IS NOT NULL AND ts_start IS NOT NULL),
            kept AS (SELECT action FROM cleaned GROUP BY action
                     HAVING COUNT(*) >= {int(cfg['min_count'])} AND COUNT(DISTINCT resource) >= {int(cfg['min_users'])})
            SELECT c.* FROM cleaned c JOIN kept USING(action)
        ) TO {sql_literal(path.as_posix())} (FORMAT parquet, COMPRESSION zstd)""")
        events, actions, cases = con.execute(f"SELECT COUNT(*), COUNT(DISTINCT action), COUNT(DISTINCT case_id) "
                                            f"FROM read_parquet({sql_literal(path.as_posix())})").fetchall()[0]
        return {"events": events, "actions": actions, "cases": cases}
    finally:
        con.unregister("ms_clean_names")


def catalogue_path(signature: str) -> Path:
    # Hash arbitrary imported signatures so they cannot escape the catalogue directory.
    safe = signature if re.fullmatch(r"[\w-]+", signature or "") else hashlib.sha256(signature.encode()).hexdigest()
    return data_dir() / "milestone_catalogues" / f"{safe}.json"


def load_catalogue(signature: str) -> dict:
    path = catalogue_path(signature)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"rows": [], "confirmed_by_business": False}


def save_catalogue(signature: str, rows: list[dict], confirmed_by_business: bool = False) -> Path:
    from app.core.narrative import payload_mask
    seen, labels, cleaned = set(), set(), []
    for row in rows:
        key = (str(row["step"]), str(row["action"]))
        label = str(row.get("label") or "").strip()
        if key in seen or not label or (row.get("is_milestone") and label in labels):
            raise ValueError("Each step/action and each selected label must be unique; labels cannot be empty.")
        if payload_mask(label) != label:
            raise ValueError("Milestone labels must not contain personal identifiers.")
        if not isinstance(row.get("is_milestone"), bool):
            raise ValueError("Milestone selection must be true or false.")
        seen.add(key)
        if row["is_milestone"]:
            labels.add(label)
        cleaned.append({**row, "label": label})
    return atomic_json(catalogue_path(signature), {"rows": cleaned, "confirmed_by_business": bool(confirmed_by_business)})


def catalogue_for(con, folder: Path, signature: str) -> list[dict]:
    path = folder / "milestone_candidates.parquet"
    if not path.exists():
        return []
    saved = {(r["step"], r["action"]): r for r in load_catalogue(signature)["rows"]}
    rows = []
    for step, action, events, cases, users in con.execute(f"""SELECT activity, action, COUNT(*),
            COUNT(DISTINCT case_id), COUNT(DISTINCT resource) FROM read_parquet({sql_literal(path.as_posix())})
            GROUP BY 1, 2 ORDER BY 1, 2""").fetchall():
        old = saved.get((step, action), {})
        rows.append(dict(step=step, action=action, events=events, cases=cases, users=users,
                         is_milestone=old.get("is_milestone", True), label=old.get("label", f"{step} · {action}")))
    return rows


def milestone_catalogue(job: dict) -> list[dict]:
    from app.core.jobs import job_folder
    from app.core.reference import order_key
    with connect() as con:
        return catalogue_for(con, job_folder(job["job_id"]), order_key(job))


def _sequence_stats(con, query: str, prefix: str) -> dict:
    from app.core.reference import _pairs, order_strength, order_stability
    con.execute(f"""CREATE OR REPLACE TEMP TABLE {prefix}_seq AS
        SELECT *, row_number() OVER (PARTITION BY case_id ORDER BY ts, activity) seq FROM (
            SELECT *, lag(activity) OVER (PARTITION BY case_id ORDER BY ts, activity) prev FROM ({query}))
        WHERE prev IS NULL OR prev <> activity""")
    edges = [dict(src=a, dst=b, count=n) for a, b, n in con.execute(f"""SELECT activity, nxt, COUNT(*) FROM (
        SELECT *, lead(activity) OVER p nxt, lead(ts) OVER p nts FROM {prefix}_seq
        WINDOW p AS (PARTITION BY case_id ORDER BY seq))
        WHERE nxt IS NOT NULL AND ts < nts GROUP BY 1, 2""").fetchall()]
    minimum = int(settings()["milestones"]["min_pair"])
    pairs = {p for p, counts in _pairs(edges).items() if sum(counts) >= minimum}
    edges = [e for e in edges if tuple(sorted((e["src"], e["dst"]))) in pairs]
    strength, band = order_strength(edges)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE {prefix}_first AS
        SELECT case_id, activity, (min(seq) - 1)::DOUBLE / greatest(max(n) - 1, 1) rel_pos FROM (
            SELECT *, max(seq) OVER (PARTITION BY case_id) n FROM {prefix}_seq) GROUP BY 1, 2""")
    order = [r[0] for r in con.execute(f"SELECT activity FROM {prefix}_first GROUP BY 1 ORDER BY median(rel_pos), activity").fetchall()]
    return dict(order_strength=strength, strength_label=band, order=order,
                stability=order_stability(con, order, f"{prefix}_first"), edges=edges)


def analyse(con, events_path: Path, signature: str) -> dict | None:
    from app.core.flow_confidence import flow_confidence
    from app.core.reference import dependency, order_stability
    folder = events_path.parent
    if not (folder / "milestone_candidates.parquet").exists():
        return None
    catalogue = catalogue_for(con, folder, signature)
    chosen = [r for r in catalogue if r["is_milestone"]]
    con.register("ms_labels", pd.DataFrame([(r["step"], r["action"], r["label"]) for r in chosen],
                                          columns=["step", "action", "label"]))
    try:
        con.execute(f"""CREATE OR REPLACE TEMP TABLE ms_events AS
            SELECT c.case_id, c.ts, c.activity step, l.label activity FROM
            read_parquet({sql_literal((folder / 'milestone_candidates.parquet').as_posix())}) c
            JOIN ms_labels l ON c.activity = l.step AND c.action = l.action""")
    finally:
        con.unregister("ms_labels")
    stats = _sequence_stats(con, "SELECT case_id, ts, activity FROM ms_events", "ms")
    step_stats = _sequence_stats(con, "SELECT case_id, step activity, min(ts) ts FROM ms_events GROUP BY 1, 2", "ms_step")
    total = con.execute("SELECT COUNT(*) FROM mcases").fetchone()[0]
    covered = con.execute("SELECT COUNT(*) FROM (SELECT case_id FROM ms_events GROUP BY 1 HAVING COUNT(DISTINCT activity)>=2)").fetchone()[0]
    coverage = covered / total if total else 0.0
    counts = {(e["src"], e["dst"]): e["count"] for e in stats["edges"]}
    cfg = settings()["milestones"]
    links = [dict(src=a, dst=b, count=n, back_count=counts.get((b, a), 0)) for (a, b), n in counts.items()
             if n >= cfg["min_pair"] and dependency(n, counts.get((b, a), 0)) >= cfg["chain_dependency"]]
    adjacency = {}
    for link in sorted(links, key=lambda x: (x["src"], x["dst"])):   # SQL row order is not stable
        adjacency.setdefault(link["src"], []).append(link["dst"])
    paths = []
    explored = 0
    for a in sorted(adjacency):
        stack = [[a]]
        while stack and explored < 10000:
            path = stack.pop()
            explored += 1
            nxt = [b for b in adjacency.get(path[-1], []) if b not in path]
            if not nxt and len(path) > 1:
                paths.append(path)
            stack.extend(path + [b] for b in reversed(nxt))
    maximal = [p for p in paths if not any(len(q) > len(p) and any(q[i:i+len(p)] == p for i in range(len(q)-len(p)+1)) for q in paths)]
    by_label = {r["label"]: r["step"] for r in chosen}
    chains = []
    for labels in maximal:
        # Count cases containing the whole chain in this order, not min(edge counts).
        joins = " ".join(f"JOIN ms_seq s{i} ON s{i}.case_id=s0.case_id AND s{i}.seq=s{i-1}.seq+1 AND s{i}.ts>s{i-1}.ts" for i in range(1, len(labels)))
        where = " AND ".join(f"s{i}.activity=?" for i in range(len(labels)))
        cases = con.execute(f"SELECT COUNT(DISTINCT s0.case_id) FROM ms_seq s0 {joins} WHERE {where}", labels).fetchone()[0]
        if cases < cfg["min_pair"]:
            continue
        chain_strength = min(dependency(counts[a,b], counts.get((b,a), 0)) for a,b in zip(labels,labels[1:]))
        chain_flow = flow_confidence(chain_strength, "data (milestones)", None, None,
                                    order_stability(con, labels, "ms_first"), coverage=cases / total if total else 0)
        chains.append(dict(id=f"M{len(chains)+1:03}", labels=labels, steps=[by_label[x] for x in labels], flow=chain_flow,
                           cases=cases, link_counts=[counts[a,b] for a,b in zip(labels,labels[1:])],
                           back_counts=[counts.get((b,a),0) for a,b in zip(labels,labels[1:])]))
    # Count milestone presence per run of its step, including repeated runs.
    con.execute(f"""CREATE OR REPLACE TEMP TABLE ms_runs AS SELECT *, sum(is_new) OVER
        (PARTITION BY case_id ORDER BY ts_start, activity ROWS UNBOUNDED PRECEDING) run FROM (
            SELECT case_id, activity, ts_start, ts_end, CASE WHEN lag(activity) OVER
            (PARTITION BY case_id ORDER BY ts_start, activity)=activity THEN 0 ELSE 1 END is_new
            FROM read_parquet({sql_literal(events_path.as_posix())}) WHERE case_id IS NOT NULL AND ts_start IS NOT NULL)""")
    con.execute("""CREATE OR REPLACE TEMP TABLE ms_run_ranges AS SELECT *, lead(start_ts) OVER
        (PARTITION BY case_id ORDER BY run) next_ts FROM (
        SELECT case_id, activity, run, min(ts_start) start_ts FROM ms_runs GROUP BY 1,2,3)""")
    denominators = dict(con.execute("SELECT activity, COUNT(*) FROM ms_run_ranges GROUP BY 1").fetchall())
    completion = []
    for step, label, occurrences in con.execute("""SELECT e.step, e.activity, COUNT(DISTINCT (e.case_id,r.run))
        FROM ms_events e JOIN ms_run_ranges r ON e.case_id=r.case_id AND e.step=r.activity
        AND e.ts>=r.start_ts AND (r.next_ts IS NULL OR e.ts<r.next_ts) GROUP BY 1,2 ORDER BY 3 DESC,1,2""").fetchall():
        completion.append(dict(step=step, label=label, occurrences=occurrences,
                               share=occurrences / denominators[step] if denominators.get(step) else 0))
    flow = flow_confidence(stats["order_strength"], "data (milestones)", None, None, stats["stability"], coverage=coverage)
    step_stats["flow"] = flow_confidence(step_stats["order_strength"], "data (milestones)", None, None,
                                        step_stats["stability"], coverage=coverage)
    return {**stats, "coverage": coverage, "cases": covered, "total_cases": total, "flow": flow,
            "step_order": step_stats, "links": links, "chains": chains, "completion": completion,
            "catalogue": [{k:v for k,v in r.items() if k != "action"} for r in catalogue],
            "actions": len(chosen), "paths_truncated": explored >= 10000}


def sequence_lines(data: dict) -> list[str]:
    lines = [f"{' → '.join(c['labels'])} ({c['cases']:,} cases; reverse order seen {sum(c['back_counts']):,} times)"
            for c in data["chains"]] or ["No one-way sequences found in the milestone actions."]
    if data.get("paths_truncated"):
        lines.append("Chain search reached its limit; only the explored chains are listed. Reduce selected milestones to examine more.")
    return lines


def to_dot(data: dict) -> str:
    from app.core.mining import _dot_text
    links = data["links"]
    labels = {e[k] for e in links for k in ("src", "dst")}
    rows = [r for r in data["catalogue"] if r["is_milestone"] and r["label"] in labels]
    lines = ['digraph milestones {', 'rankdir=LR;', 'node [shape=box];',
             'label="box = milestone (step · action) · arrow = happens in this order in ≥ 80% of cases";']
    for i, step in enumerate(sorted({r["step"] for r in rows})):
        lines += [f'subgraph cluster_{i} {{', f'label="{_dot_text(step)}";']
        lines += [f'"{_dot_text(r["label"])}";' for r in rows if r["step"] == step]
        lines.append('}')
    for e in links:
        lines.append(f'"{_dot_text(e["src"])}" -> "{_dot_text(e["dst"])}" [label="{e["count"]}", style=solid];')
    return '\n'.join(lines + ['}'])
