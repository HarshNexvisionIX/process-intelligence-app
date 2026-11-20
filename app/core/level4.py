"""Level 4: how the work is done inside each Level 3 step, as named sub-steps.

    Level 1 -> Stage (Level 2) -> Level 3 -> Level 4

A unit is (task, application, screen, click): the screen (cleaned window title, screens.py) gives a
click its meaning ("Send" on "Compose Email"). Per Level 3 the AI reads ONLY the action catalogue -
step / stage / task names, the top screens with their top clicks, and lines
"U017 | application | screen | click | share %" - and writes sub-steps with a business label, a
description and regex RULES (application, screen_regex, click_regex). Code validates everything
(regex safety in a separate process with a time limit, overlaps, coverage), applies the rules to every
unit (explicit member assignments first, then the rules in order: first match wins) and computes the
maps, numbers and summaries. New files and new click names are grouped automatically by the saved rules.

Taxonomy: <data_dir>/level4/<layout signature>.json  {l3: {version: 2, source, provider, model,
prompt_version, confirmed_by_business, updated_at, reason, substeps: [{id, label, description,
rules: [{application, screen_regex, click_regex}], members: [{activity, application, screen, action}]}],
rule_stats}}.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from app.core.config import data_dir, settings
from app.core.db import sql_literal
from app.core.screens import word_pattern

OTHER = "Other"
# Words a sub-step description may not use: it says what is done, in business words, not how it is clicked.
TECH_WORDS = re.compile(r"\b(clicks?|clicking|clicked|screens?|regex|regular expression|units?|buttons?)\b", re.I)
PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
BUCKETING_PROMPT, SUMMARY_PROMPT = "pm-level4-bucketing", "pm-level4-summary"
STOPWORDS = {"the", "a", "an", "of", "for", "and", "in", "on", "with", "to", "by", "from", "all", "other", "misc"}
GENERIC_LABEL = re.compile(r"^(review|update|complete|work|other work|other)\s+(in|on)\b", re.I)
NESTED = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")
UNIT_FIELDS = ("activity", "application", "screen", "action")


def _cfg() -> dict:
    return settings()["level4"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_prompts(kind: str) -> list[dict]:
    """The local prompt files ("bucketing" or "summary") as chat messages (pushed by push_prompts.py)."""
    return [{"role": "system", "content": (PROMPTS / f"level4_{kind}_system.txt").read_text(encoding="utf-8")},
            {"role": "user", "content": (PROMPTS / f"level4_{kind}_user.txt").read_text(encoding="utf-8")}]


def _fill(template: str, values: dict) -> str:
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def unit_key(activity, application, screen, action) -> tuple[str, str, str, str]:
    return (str(activity or ""), str(application or ""), str(screen or ""), str(action or ""))


def _member(u: dict) -> dict:
    return {k: u.get(k) or "" for k in UNIT_FIELDS}


# --------------------------------------------------------------------------- #
# 1. Catalogue and units
# --------------------------------------------------------------------------- #
def catalogue(folder: Path) -> pd.DataFrame:
    from app.core.subprocess import CATALOGUE_FILE
    path = Path(folder) / CATALOGUE_FILE
    cols = ["l3", "activity", "application", "screen", "action", "events", "runs", "cases", "users", "active_seconds"]
    if not path.exists():
        return pd.DataFrame(columns=cols)
    df = duckdb.connect().execute(f"SELECT * FROM read_parquet({sql_literal(path.as_posix())})").df()
    if "screen" not in df.columns:
        df["screen"] = ""
    df["screen"] = df["screen"].fillna("")
    df["application"] = df["application"].fillna("")
    return df


def units_of(cat: pd.DataFrame, l3: str) -> list[dict]:
    rows = cat[cat["l3"] == l3]
    return [{"activity": r.activity, "application": r.application, "screen": r.screen, "action": r.action,
             "events": int(r.events), "users": int(r.users)} for r in rows.itertuples()]


def select_for_ai(cat: pd.DataFrame, l3: str) -> list[dict]:
    """Units the AI may see: users >= min_users and events >= min_events, most frequent first, until
    coverage_target of the L3's events or max_units_per_l3. Text is masked (payload_mask)."""
    from app.core.narrative import payload_mask
    cfg = _cfg()
    rows = cat[cat["l3"] == l3]
    total = float(rows["events"].sum()) or 1.0
    ok = rows[(rows["users"] >= int(cfg["min_users"])) & (rows["events"] >= int(cfg["min_events"]))]
    ok = ok.sort_values(["events", "screen", "action"], ascending=[False, True, True])
    units, covered = [], 0.0
    for r in ok.itertuples():
        if covered / total >= float(cfg["coverage_target"]) or len(units) >= int(cfg["max_units_per_l3"]):
            break
        units.append({"id": f"U{len(units) + 1:03d}", "activity": r.activity, "application": r.application,
                      "screen": r.screen, "action": r.action, "events": int(r.events),
                      "share": round(float(r.events) / total, 4), "shown_screen": payload_mask(str(r.screen or "")),
                      "shown": payload_mask(str(r.action))})
        covered += float(r.events)
    return units


def top_screens(cat: pd.DataFrame, l3: str) -> list[str]:
    """Lines "screen (application) — share% — top clicks: a, b, c" (masked, users >= min_users)."""
    from app.core.narrative import payload_mask
    rows = cat[(cat["l3"] == l3) & (cat["users"] >= int(_cfg()["min_users"]))]
    total = float(cat[cat["l3"] == l3]["events"].sum()) or 1.0
    groups = sorted(rows.groupby(["screen", "application"]), key=lambda kv: -kv[1]["events"].sum())
    out = []
    for (screen, app), g in groups[:int(_cfg()["top_screens"])]:
        clicks = ", ".join(payload_mask(str(a)) for a in g.sort_values("events", ascending=False)["action"][:6])
        out.append(f"{payload_mask(str(screen)) or '(no screen)'} ({app or '-'}) — "
                   f"{100 * g['events'].sum() / total:.1f}% — top clicks: {clicks}")
    return out


# --------------------------------------------------------------------------- #
# 2. Rules: safety, applying, preview
# --------------------------------------------------------------------------- #
_GUARD = r"""
import json, re, sys, time
d = json.load(sys.stdin)
for i, p in enumerate(d["patterns"]):
    r = re.compile(p)
    t = time.perf_counter()
    for s in d["texts"]:
        r.search(s)
    print(i, round((time.perf_counter() - t) * 1000, 3), flush=True)
"""


def static_regex_problem(pattern: str) -> str | None:
    """Too long, does not compile, or a nested quantifier such as (a+)+; None when fine."""
    if len(pattern) > int(_cfg()["max_regex_chars"]):
        return f"is longer than {_cfg()['max_regex_chars']} characters"
    try:
        re.compile(pattern)
    except re.error as err:
        return f"does not compile ({err})"
    if NESTED.search(pattern):
        return "has a nested quantifier such as (a+)+"
    return None


def regex_problems(patterns: list[str], texts: list[str]) -> dict[str, str]:
    """{pattern: problem} for unsafe patterns. Speed is tested in a SEPARATE process that is killed after
    level4.regex_timeout_ms per text (at most regex_timeout_cap_ms per pattern): a runaway regex can never
    hang the app."""
    cfg = _cfg()
    out: dict[str, str] = {}
    ok = []
    for p in dict.fromkeys(patterns):
        why = static_regex_problem(p)
        if why:
            out[p] = why
        else:
            ok.append(p)
    per = min(float(cfg["regex_timeout_ms"]) * max(len(texts), 1), float(cfg["regex_timeout_cap_ms"])) / 1000
    pending = ok
    while pending:
        done: dict[int, float] = {}
        proc = subprocess.Popen([sys.executable, "-c", _GUARD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        try:
            stdout, _ = proc.communicate(json.dumps({"patterns": pending, "texts": texts}),
                                         timeout=1.5 + per * len(pending))
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
        for line in (stdout or "").splitlines():
            i, ms = line.split()
            done[int(i)] = float(ms)
        for i, p in enumerate(pending):
            if i in done and done[i] / 1000 > per:
                out[p] = "is too slow on the click names (timed out)"
        if len(done) >= len(pending):
            break
        stuck = len(done)                      # the first pattern without a result hung
        out[pending[stuck]] = "is too slow on the click names (timed out)"
        pending = pending[stuck + 1:]
    return out


def _compiled(entry: dict):
    rules = []
    for s in entry.get("substeps") or []:
        for r in s.get("rules") or []:
            app = str(r.get("application") or "*").strip() or "*"
            try:
                # Plain-word patterns match whole words only ("hi" never matches "History").
                sr = re.compile(word_pattern(r["screen_regex"])) if r.get("screen_regex") else None
                cr = re.compile(word_pattern(r["click_regex"])) if r.get("click_regex") else None
            except re.error:
                continue
            if sr is None and cr is None:
                continue
            rules.append((s["label"], app.lower(), sr, cr))
    members = {}
    for s in entry.get("substeps") or []:
        for m in s.get("members") or []:
            members[unit_key(m.get("activity"), m.get("application"), m.get("screen"), m.get("action"))] = s["label"]
    return rules, members


def _rule_hits(rule, unit) -> bool:
    _label, app, sr, cr = rule
    if app != "*" and app != str(unit.get("application") or "").lower():
        return False
    if sr is not None and not sr.search(str(unit.get("screen") or "")):
        return False
    return cr is None or bool(cr.search(str(unit.get("action") or "")))


def assigner(entry: dict):
    """unit -> sub-step label: explicit member assignments first, then the rules in order (first match
    wins), else "Other"."""
    rules, members = _compiled(entry)

    def label(unit: dict) -> str:
        key = unit_key(unit.get("activity"), unit.get("application"), unit.get("screen"), unit.get("action"))
        if key in members:
            return members[key]
        for rule in rules:
            if _rule_hits(rule, unit):
                return rule[0]
        return OTHER
    return label


def rule_preview(rule: dict, units: list[dict]) -> dict:
    """How many units / events one rule matches: {"units", "events", "error"}."""
    for k in ("screen_regex", "click_regex"):
        if rule.get(k):
            why = static_regex_problem(str(rule[k]))
            if why:
                return {"units": 0, "events": 0, "error": f"{k} {why}"}
    if not rule.get("screen_regex") and not rule.get("click_regex"):
        return {"units": 0, "events": 0, "error": "a screen regex or a click regex is needed"}
    compiled = ("", str(rule.get("application") or "*").lower() or "*",
                re.compile(word_pattern(rule["screen_regex"])) if rule.get("screen_regex") else None,
                re.compile(word_pattern(rule["click_regex"])) if rule.get("click_regex") else None)
    hit = [u for u in units if _rule_hits(compiled, u)]
    return {"units": len(hit), "events": sum(u["events"] for u in hit), "error": None}


# --------------------------------------------------------------------------- #
# 3. Validation of the AI answer (code decides)
# --------------------------------------------------------------------------- #
def _text_problem(text: str, where: str, max_words: int) -> list[str]:
    from app.core.ingest import EMAIL_RE
    from app.core.milestones import NAME_TOKEN, mask_names
    from app.core.screens import TYPED
    if not isinstance(text, str) or not text.strip():
        return [f"{where} is empty."]
    out = []
    if len(text.split()) > max_words:
        out.append(f"{where} has more than {max_words} words.")
    if re.search(r"\d", text):
        out.append(f"{where} contains a digit.")
    if re.search(EMAIL_RE.pattern.strip("^$"), text) or NAME_TOKEN in text or TYPED in text:
        out.append(f"{where} contains an e-mail address, a masked name or typed text.")
    if mask_names(text) != text:
        out.append(f"{where} contains a person-like name.")
    return out


def validate(output, sent: list[dict], all_units: list[dict], apps: set[str]) -> tuple[list[str], dict]:
    """Problems in an AI answer (sent back on retry), or ([], {"substeps", "coverage", "rule_stats"})."""
    cfg = _cfg()
    if not isinstance(output, dict) or not isinstance(output.get("substeps"), list):
        return ['The answer must be one JSON object: {"substeps": [...], "assign_unmatched": {...}}.'], {}
    subs = output["substeps"]
    errors: list[str] = []
    if not int(cfg["min_buckets"]) <= len(subs) <= int(cfg["max_buckets"]):
        errors.append(f"Use between {cfg['min_buckets']} and {cfg['max_buckets']} sub-steps (found {len(subs)}).")
    clicks = {str(u["action"]).strip().lower() for u in all_units}
    known_apps = {a.lower() for a in apps}
    labels, ids, patterns = [], [], []
    for i, s in enumerate(subs):
        if not isinstance(s, dict):
            errors.append(f"substeps[{i}] must be an object.")
            continue
        sid, label = str(s.get("id") or ""), str(s.get("label") or "").strip()
        ids.append(sid)
        errors += _text_problem(label, f"{sid} label", int(cfg["max_name_words"]))
        words = label.split()
        if len(words) < 2 or words[0].lower() in STOPWORDS or not words[0].isalpha():
            errors.append(f"{sid} label must be a verb and an object in business words, e.g. 'Find and open the HR case'.")
        if label.lower() in clicks or GENERIC_LABEL.search(label):
            errors.append(f"{sid} label '{label}' is a click name or a generic label; describe the business activity.")
        if label.lower() in labels:
            errors.append(f"The label '{label}' is used twice.")
        labels.append(label.lower())
        desc = str(s.get("description") or "")
        errors += _text_problem(desc, f"{sid} description", int(cfg.get("max_description_words", 20)))
        if isinstance(desc, str) and TECH_WORDS.search(desc):
            errors.append(f"{sid} description talks about '{TECH_WORDS.search(desc).group(0)}': say in plain business "
                          "words what the person does and why, not which clicks or screens.")
        if len([x for x in re.split(r"[.!?]+", desc) if x.strip()]) > 2:
            errors.append(f"{sid} description must be one or two sentences.")
        rules = s.get("rules")
        if not isinstance(rules, list) or not rules:
            errors.append(f"{sid} needs at least one rule.")
            continue
        for j, r in enumerate(rules):
            if not isinstance(r, dict):
                errors.append(f"{sid} rules[{j}] must be an object.")
                continue
            app = str(r.get("application") or "*").strip()
            if app != "*" and app.lower() not in known_apps:
                errors.append(f"{sid} rules[{j}]: application '{app}' is not known; use a known application or '*'.")
            if not r.get("screen_regex") and not r.get("click_regex"):
                errors.append(f"{sid} rules[{j}] needs a screen_regex or a click_regex.")
            patterns += [str(r[k]) for k in ("screen_regex", "click_regex") if r.get(k)]
    if len(set(ids)) != len(ids):
        errors.append("Sub-step ids must be unique.")
    texts = sorted({str(u["action"]) for u in all_units} | {str(u["screen"]) for u in all_units})
    for p, why in (regex_problems(patterns, texts) if patterns else {}).items():
        errors.append(f"The regex {p!r} {why}.")
    assign = output.get("assign_unmatched") or {}
    if not isinstance(assign, dict):
        errors.append('"assign_unmatched" must be an object {"U088": "S3"}.')
        assign = {}
    sent_ids = {u["id"]: u for u in sent}
    unknown = sorted(k for k in assign if k not in sent_ids)
    if unknown:
        errors.append("Unknown unit ids in assign_unmatched: " + ", ".join(unknown[:10]) + ".")
    bad_targets = sorted({str(v) for v in assign.values() if str(v) not in ids})
    if bad_targets:
        errors.append("Unknown sub-step ids in assign_unmatched: " + ", ".join(bad_targets) + ".")
    if errors:
        return errors, {}
    substeps = [{"id": str(s["id"]), "label": str(s["label"]).strip(), "description": str(s.get("description") or ""),
                 "rules": [{"application": str(r.get("application") or "*"), "screen_regex": r.get("screen_regex") or None,
                            "click_regex": r.get("click_regex") or None} for r in s["rules"]], "members": []}
                for s in subs]
    by_id = {s["id"]: s for s in substeps}
    for uid, sid in assign.items():
        by_id[str(sid)]["members"].append(_member(sent_ids[uid]))
    entry = {"substeps": substeps}
    rules, _m = _compiled(entry)
    label_of = assigner(entry)
    total = sum(u["events"] for u in all_units) or 1
    stats = {s["label"]: [0, 0] for s in substeps}
    id_of = {unit_key(*(u[k] for k in UNIT_FIELDS)): u["id"] for u in sent}
    overlaps, unmatched = [], []
    for u in all_units:
        key = unit_key(*(u[k] for k in UNIT_FIELDS))
        hit = {r[0] for r in rules if _rule_hits(r, u)}
        if len(hit) > 1:
            overlaps.append(f"{id_of.get(key, u['action'])} matched by {', '.join(sorted(hit))}")
        lab = label_of(u)
        if lab == OTHER:
            unmatched.append(u)
        else:
            stats[lab][0] += 1
            stats[lab][1] += u["events"]
    if overlaps:
        errors.append("These units are matched by the rules of two sub-steps (make the rules exclusive): "
                      + "; ".join(overlaps[:6]) + ".")
    covered = sum(v[1] for v in stats.values()) / total
    if covered < float(cfg["min_coverage"]) or 1 - covered > float(cfg["max_other_share"]):
        biggest = sorted(unmatched, key=lambda u: -u["events"])[:12]
        lines = [f"{id_of.get(unit_key(*(u[k] for k in UNIT_FIELDS)), '(not sent)')} | {u['application']} | "
                 f"{u['screen']} | {u['action']}" for u in biggest]
        errors.append(f"The rules cover only {100 * covered:.0f}% of the step's events (at least "
                      f"{100 * float(cfg['min_coverage']):.0f}% needed). Add rules or assignments for the largest "
                      "unmatched units: " + "; ".join(lines) + ".")
    empty = [lab for lab, (n, _e) in stats.items() if n == 0]
    if empty:
        errors.append("These sub-steps match no unit: " + ", ".join(empty) + ".")
    if errors:
        # Short only on coverage (>= level4.min_partial_coverage): retried, but kept if no try does better.
        partial = (len(errors) == 1 and errors[0].startswith("The rules cover only")
                   and covered >= float(cfg.get("min_partial_coverage", 0.6)))
        return errors, ({"substeps": substeps, "coverage": round(covered, 4), "partial_ok": True,
                         "rule_stats": {lab: {"units": n, "events": e} for lab, (n, e) in stats.items()}}
                        if partial else {})
    return [], {"substeps": substeps, "coverage": round(covered, 4),
                "rule_stats": {lab: {"units": n, "events": e} for lab, (n, e) in stats.items()}}


# --------------------------------------------------------------------------- #
# 4. Proposing a taxonomy (AI rules; rules fallback)
# --------------------------------------------------------------------------- #
def _client(job_id: str):
    """The routed AI session for l4_bucketing, or (None, reason). With level4.local_only only providers in
    your own network (local / private) may be used."""
    from app.core import ai_client, privacy
    from app.core.narrative import ai_status
    ok, why = ai_status()
    if not ok:
        return None, f"AI {why}" if (why or "").startswith("switched") else f"AI not available: {why}"
    known = ai_client.providers()
    usable = [n for n in ai_client.route("l4_bucketing") if n in known and ai_client.usable(known[n])[0]]
    if _cfg().get("local_only"):
        local = [n for n in usable if privacy.classify_endpoint(known[n].get("base_url") or "") in ("local", "private")]
        if not local:
            return None, "level4.local_only is true and no provider in your own network is configured"
        return ai_client.session("l4_bucketing", only=local[0]), None
    if not privacy.ai_allowed()[0]:
        return None, "external AI providers are not allowed (ALLOW_EXTERNAL_AI)"
    if not usable:
        return None, "no provider for l4_bucketing (add a key and choose a model on the AI settings page)"
    return ai_client.session("l4_bucketing"), None


_NOISE = {"team", "web", "testing", "test", "prod", "production", "dev", "uat", "final", "copy", "draft", "old",
          "sheet", "book", "new", "tool", "tools", "page", "screen", "view", "the", "of", "and", "for", "to", "in", "on"}


def fallback_label(screen: str, actions: list[str]) -> str:
    """A rule-based sub-step label in verb + object form, without the application, version strings, file
    extensions or digits: "View Worker" -> "View worker record", "Offers_Team_Web_Tool_Testing-v1.xlsx" + clicks
    "Update ..." -> "Update offers tool"."""
    from app.core.screens import FILE_EXT, app_names
    verbs = {str(v).lower() for v in _cfg().get("label_verbs") or []}
    raw = FILE_EXT.sub("", str(screen or ""))
    raw = re.sub(r"[-_ ]v\d+(\.\d+)*\b|\d+", " ", raw, flags=re.I)
    words = [w for w in re.findall(r"[A-Za-z]+", raw)]
    apps = {a.lower() for a in app_names()}
    words = [w for w in words if w.lower() not in apps]
    verb = None
    if words and words[0].lower() in verbs:
        verb, words = words[0].lower(), words[1:]
    if verb is None:
        for a in actions:
            first = (re.findall(r"[A-Za-z]+", str(a)) or [""])[0].lower()
            if first in verbs and first not in ("navigate", "select", "open", "view"):
                verb = first
                break
    if verb is None:
        verb = "view" if any(str(a).lower().startswith("view") for a in actions) else "review"
    obj = [w.lower() for w in words if w.lower() not in _NOISE] or [w.lower() for w in words if w.lower() not in
                                                                    {"the", "of", "and"}]
    if FILE_EXT.search(str(screen or "")) and "tool" in str(screen).lower() and "tool" not in obj:
        obj.append("tool")
    if not obj:
        obj = ["record"]
    elif len(obj) == 1 and obj[0] not in ("tool", "record", "list", "inbox", "case", "cases"):
        obj.append("record")
    label = " ".join([verb.capitalize()] + obj[:5])
    return label


def rules_entry(l3: str, units: list[dict], reason: str = "") -> dict:
    """Fallback without AI: one sub-step per screen (largest first, at most level4.max_buckets), labelled verb +
    object from the screen and its most common clicks (fallback_label), then refined like an AI build (navigation
    clicks to "Navigate and search", screen context, the tail by word overlap). The reason is kept."""
    groups: dict[tuple[str, str], int] = {}
    actions: dict[tuple[str, str], dict[str, int]] = {}
    for u in units:
        k = (str(u.get("application") or ""), str(u.get("screen") or ""))
        groups[k] = groups.get(k, 0) + int(u.get("events", 0))
        a = actions.setdefault(k, {})
        a[str(u.get("action") or "")] = a.get(str(u.get("action") or ""), 0) + int(u.get("events", 0))
    ranked = sorted(groups.items(), key=lambda kv: (kv[0][1] == "", -kv[1]))[:int(_cfg()["max_buckets"])]
    substeps: list[dict] = []
    by_label: dict[str, dict] = {}
    for (app, screen), _n in ranked:
        top = [a for a, _ in sorted(actions[(app, screen)].items(), key=lambda kv: -kv[1])[:5]]
        label = fallback_label(screen, top) if screen else fallback_label(app or "work", top)
        rule = {"application": app or "*", "screen_regex": "^" + re.escape(screen) + "$" if screen else None,
                "click_regex": None if screen else ".*"}
        if label in by_label:                       # two screens with the same name: one sub-step
            by_label[label]["rules"].append(rule)
            continue
        by_label[label] = {"id": f"S{len(substeps) + 1}", "label": label, "named_from": "rule",
                           "description": f"Work done in {screen or 'the application'}.", "rules": [rule], "members": []}
        substeps.append(by_label[label])
    entry = {"version": 2, "source": "rules", "provider": None, "model": None, "prompt_version": None,
             "confirmed_by_business": False, "updated_at": _now(), "reason": reason or "AI not used",
             "substeps": substeps}
    refined = refine(entry, units, screen_context=False)
    for s in refined["substeps"]:
        s["named_from"] = s.get("named_from") if s.get("named_from") in ("rule", "document") else "rule"
    return refined


def _ask(client, messages: list[dict], check) -> tuple[object, list[str], dict]:
    """Call with retries (ai.max_retries): code errors are sent back. Returns (output, errors, result)."""
    from app.core.ai_client import AIUnavailable
    from app.core.narrative import _call, parse_reply
    cfg = settings()["ai"]
    errors: list[str] = []
    kept = None                                    # the best answer that is short only on coverage
    for attempt in range(1, int(cfg["max_retries"]) + 2):
        try:
            reply = _call(client, os.getenv("LLM_MODEL") or "unknown-model", messages, cfg["temperature"], {})
        except AIUnavailable as err:
            errors.append(f"AI not available: {err.short()}")
            return (kept[0], errors, kept[1]) if kept else (None, errors, {})
        except Exception as err:  # noqa: BLE001
            errors.append(f"attempt {attempt}: the AI call failed ({type(err).__name__})")
            continue
        output = parse_reply(reply)
        problems, result = check(output)
        if not problems:
            return output, errors, result
        if result.get("partial_ok") and (kept is None or result["coverage"] > kept[1]["coverage"]):
            kept = (output, result)
        errors += [f"attempt {attempt}: {p}" for p in problems]
        messages = messages + [{"role": "assistant", "content": reply},
                               {"role": "user", "content": "Your previous answer broke these rules:\n- "
                                + "\n- ".join(problems) + "\nReturn the corrected JSON object only."}]
    if kept:
        errors.append(f"kept the AI answer covering {100 * kept[1]['coverage']:.0f}% of the step's events; the rest "
                      "goes to the tail rules, Other and the review queue")
        return kept[0], errors, kept[1]
    return None, errors, {}


def _units_lines(units: list[dict]) -> str:
    return "\n".join(f"{u['id']} | {u['application'] or '-'} | {u['shown_screen'] or '-'} | {u['shown']} | "
                     f"{100 * u['share']:.1f}%" for u in units)


def _phase2(client, prompt_system: str, substeps: list[dict], units: list[dict]) -> list[str]:
    """Assign leftover units (no rule matched) to sub-steps in chunks; returns errors."""
    sid = {s["id"]: s for s in substeps}
    fixed = "\n".join(f"{s['id']} | {s['label']} | {s['description']}" for s in substeps)
    errors: list[str] = []
    size = int(_cfg()["chunk_units"])
    for start in range(0, len(units), size):
        chunk = units[start:start + size]
        ids = [u["id"] for u in chunk]
        msgs = [{"role": "system", "content": prompt_system},
                {"role": "user", "content": "Phase 2. Assign each unit to one sub-step id, or \"other\".\n"
                 f"Sub-steps (id | label | description):\n{fixed}\n\nUnits (id | application | screen | click | share):\n"
                 f"{_units_lines(chunk)}\n\n" 'Return {"assign": {"U151": "S2", "U152": "other"}}.'}]

        def check(o, ids=ids):
            if not isinstance(o, dict) or not isinstance(o.get("assign"), dict):
                return ['Return {"assign": {...}}.'], {}
            errs = [f"Assign exactly these unit ids: {sorted(set(ids) ^ set(o['assign']))[:8]}"] \
                if set(o["assign"]) != set(ids) else []
            bad = sorted({str(v) for v in o["assign"].values() if str(v) not in sid and str(v) != "other"})
            return errs + ([f"Unknown sub-step ids: {bad}"] if bad else []), {}
        out, errs, _ = _ask(client, msgs, check)
        errors += errs
        by = {u["id"]: u for u in chunk}
        for uid, target in ((out or {}).get("assign") or {}).items():
            if str(target) in sid:
                sid[str(target)]["members"].append(_member(by[uid]))
    return errors


def propose(job_id: str, l3: str, stage: str | None, tasks: list[str], cat: pd.DataFrame, client=None,
            use_ai: bool = True) -> dict:
    """A taxonomy entry for one Level 3: AI sub-steps with rules (validated, retried), or the fallback."""
    from app.core import observability
    cfg = _cfg()
    all_units = units_of(cat, l3)
    fallback = lambda why: refine(rules_entry(l3, all_units, why), all_units, doc_names(job_id, l3),
                                       screen_context=False)  # noqa: E731
    if not use_ai:
        return fallback("AI switched off for this run")
    reason = None
    if client is None:
        client, reason = _client(job_id)
    if client is None:
        return fallback(reason or "AI not available")
    units = select_for_ai(cat, l3)
    if len(units) < int(cfg["min_buckets"]):
        return fallback("too few frequent click names for AI sub-steps")
    apps = {str(u["application"]) for u in all_units if u["application"]}
    phase1, rest = units[:int(cfg["phase1_units"])], units[int(cfg["phase1_units"]):]
    names = doc_names(job_id, l3)
    context = doc_context(job_id, l3)
    values = {"l3": l3, "stage": stage or "-", "tasks": ", ".join(tasks) or "-", "apps": ", ".join(sorted(apps)) or "-",
              "preferred": "; ".join(names) or "(none)",
              "context": "\n".join(f"- {x}" for x in context) or "(no business documents loaded for this step)",
              "screens": "\n".join(top_screens(cat, l3)) or "(none)", "units": _units_lines(phase1),
              "min_buckets": cfg["min_buckets"], "max_buckets": cfg["max_buckets"],
              "min_coverage": int(100 * float(cfg["min_coverage"]))}
    with observability.trace(job_id, tags=["level4", "rules"], metadata={"l3_units": len(units)},
                             allow_names={l3, *tasks}, name="level4-rules"):
        prompt, version = observability.get_prompt(BUCKETING_PROMPT, settings()["ai"]["prompt_label"],
                                                   local_prompts("bucketing"))
        msgs = [{"role": m["role"], "content": _fill(m["content"], values)} for m in prompt]
        out, errors, result = _ask(client, msgs, lambda o: validate(o, phase1, all_units, apps))
        if out is None:
            tries = sum(1 for e in errors if re.match(r"attempt \d+: ", e) and "failed (" not in e)
            first = next((e.split(": ", 1)[1] for e in errors if re.match(r"attempt 1: ", e)), None)
            why = (f"answer invalid after {int(settings()['ai']['max_retries']) + 1} tries: {first}" if first and tries
                   else (errors[-1] if errors else "no answer"))
            entry = fallback(why)
            entry["errors"] = errors
            return entry
        substeps = result["substeps"]
        label_of = assigner({"substeps": substeps})
        errors += _phase2(client, prompt[0]["content"], substeps, [u for u in rest if label_of(u) == OTHER])
    notes = client.notes() if hasattr(client, "notes") else []
    entry = {"version": 2, "source": "ai", "provider": getattr(client, "provider", None),
             "model": getattr(client, "model", None), "prompt_version": version, "confirmed_by_business": False,
             "updated_at": _now(), "reason": None, "notes": notes + errors, "substeps": substeps,
             "rule_stats": result.get("rule_stats"), "coverage": result.get("coverage"), "doc_names": names,
             "coverage_by_ai": result.get("coverage")}
    return refine(entry, all_units, names)       # names, navigation clicks, the tail (section 11)


# --------------------------------------------------------------------------- #
# 5. Storage and building
# --------------------------------------------------------------------------- #
def taxonomy_path(signature: str) -> Path:
    safe = signature if re.fullmatch(r"[\w-]+", signature or "") else hashlib.sha256(signature.encode()).hexdigest()
    return data_dir() / "level4" / f"{safe}.json"


def _upgrade(entry: dict) -> dict:
    """A version 1 entry (buckets with member lists) as version 2 (sub-steps with members, no rules)."""
    if not entry or "substeps" in entry:
        return entry
    subs = [{"id": b["id"], "label": b["name"], "description": b.get("description", ""), "rules": [],
             "members": [{**m, "screen": m.get("screen", "")} for m in b.get("members") or []]}
            for b in entry.get("buckets") or []]
    return {**{k: v for k, v in entry.items() if k != "buckets"}, "version": 2, "substeps": subs}


def load(signature: str) -> dict:
    p = taxonomy_path(signature)
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return {k: _upgrade(v) for k, v in data.items()}


def save(signature: str, taxonomy: dict) -> Path:
    from app.core.milestones import atomic_json
    return atomic_json(taxonomy_path(signature), taxonomy)


def save_entry(signature: str, l3: str, entry: dict) -> dict:
    tax = load(signature)
    tax[l3] = {**entry, "version": 2, "updated_at": _now()}
    save(signature, tax)
    return tax[l3]


def entry_hash(entry: dict | None) -> str:
    """The taxonomy's content hash. The save time (updated_at) is not content: a rules taxonomy rebuilt with the
    same sub-steps keeps its hash, so the page-3 summary stays matched and reruns give the same mining.json."""
    content = {k: v for k, v in (entry or {}).items() if k != "updated_at"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _l3_context(folder: Path, l3: str) -> tuple[str | None, list[str]]:
    src = f"read_parquet({sql_literal((Path(folder) / 'events.parquet').as_posix())})"
    con = duckdb.connect()
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    l3col = "COALESCE(level3, activity)" if "level3" in cols else "activity"
    row = con.execute(f"SELECT mode(stage), list(DISTINCT activity) FROM {src} WHERE {l3col} = ?", [l3]).fetchall()[0]
    return row[0], sorted(row[1] or [])


def update_entry(job_id: str, l3: str, entry: dict, cat: pd.DataFrame, client=None) -> dict:
    """"Update with new actions": click names no rule or member covers go to the AI (phase 2) when
    available; otherwise they stay in Other."""
    entry = json.loads(json.dumps(entry))
    label_of = assigner(entry)
    units = [u for u in select_for_ai(cat, l3) if label_of(u) == OTHER]
    if not units:
        return entry
    if client is None:
        client, _why = _client(job_id)
    if client is None:
        return entry
    _phase2(client, local_prompts("bucketing")[0]["content"], entry["substeps"], units)
    entry["updated_at"] = _now()
    return entry


def build_or_update(job: dict, mode: str = "reuse", client=None, l3s: list[str] | None = None,
                    use_ai: bool = True, progress=None) -> dict:
    """reuse: build steps without an entry, or with a rules entry when the AI can be used now; build:
    rebuild every unconfirmed entry; update: assign new click names. Confirmed entries are never touched.
    One Level 3 at a time (through ai_client limits). Returns {l3: what happened}."""
    from app.core import jobs
    folder = jobs.job_folder(job["job_id"])
    sig = job.get("layout_signature") or f"job-{job['job_id']}"
    return _build(folder, sig, job["job_id"], mode, client, l3s, use_ai, progress)


def _build(folder: Path, sig: str, job_id: str, mode: str, client, l3s, use_ai: bool, progress=None) -> dict:
    cat = catalogue(folder)
    tax = load(sig)
    done = {}
    ai_now = use_ai and (client is not None or _client(job_id)[0] is not None)
    l3_list = sorted(cat["l3"].dropna().unique()) if len(cat) else []
    for i, l3 in enumerate(l3_list, 1):
        if l3s and l3 not in l3s:
            continue
        entry = tax.get(l3)
        if entry and entry.get("confirmed_by_business"):
            done[l3] = "kept (confirmed)"
            continue
        if mode == "reuse" and entry and (entry.get("source") != "rules" or not ai_now):
            done[l3] = "kept"
            continue
        if progress:
            progress(f"Level 4: building sub-steps for {l3} ({i} of {len(l3_list)})")
        if mode == "update" and entry:
            tax[l3] = update_entry(job_id, l3, entry, cat, client if use_ai else None)
            done[l3] = "updated"
        else:
            stage, tasks = _l3_context(folder, l3)
            tax[l3] = keep_locked(entry, propose(job_id, l3, stage, tasks, cat, client, use_ai))
            done[l3] = f"built ({tax[l3]['source']})"
        save(sig, tax)
    return done


# --------------------------------------------------------------------------- #
# 6. Analysis per Level 3 (code only)
# --------------------------------------------------------------------------- #
def _l3_rows(con, folder: Path, l3: str) -> pd.DataFrame:
    """Click rows of one Level 3 with an L3-level run id. A run is as on the map: consecutive rows of the same
    Level 3 in a case (by time, then row order); a new step run (run_id: a new step or a new step-run ID)
    only splits it further, so a step's events, hours and cases are the same as in the step table."""
    from app.core.subprocess import ACTIONS_FILE
    src = f"read_parquet({sql_literal((Path(folder) / ACTIONS_FILE).as_posix())})"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    l3col = "COALESCE(level3, activity)" if "level3" in cols else "activity"
    screen = "COALESCE(screen, '')" if "screen" in cols else "''"
    order = "ts, src_row, action" if "src_row" in cols else "ts, action"
    gap = float(settings()["inside_step"]["gap_minutes"]) * 60
    # A Level 4 "run" is a WORK SITTING, as in "Inside each step": one case, one Level 3, ONE person, no pause over
    # inside_step.gap_minutes (a step run of the map, split by person and pause). Same events, hours and cases.
    return con.execute(f"""
        WITH a AS (SELECT *, {l3col} AS l3, COALESCE(resource, '') AS who FROM {src}),
        g AS (SELECT *, CASE WHEN lag(l3) OVER w IS DISTINCT FROM l3 OR lag(run_id) OVER w IS DISTINCT FROM run_id
                                  OR epoch(ts) - epoch(lag(ts) OVER w) > {gap}
                             THEN 1 ELSE 0 END AS new FROM a
              WINDOW w AS (PARTITION BY case_id, who ORDER BY {order})),
        n AS (SELECT *, SUM(new) OVER (PARTITION BY case_id, who ORDER BY {order} ROWS UNBOUNDED PRECEDING) AS k FROM g)
        SELECT 'L' || md5(concat_ws('|', case_id, who, CAST(k AS VARCHAR))) AS l3_run,
               case_id, resource, activity, COALESCE(application, '') AS application, {screen} AS screen, action, ts,
               COALESCE(active_seconds, 0) AS active_seconds, repeats
        FROM n WHERE l3 = ? ORDER BY l3_run, {order}""", [l3]).df()


def _median(values) -> float | None:
    vals = sorted(float(v) for v in values if v is not None and v == v)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _hours(s) -> float | None:
    return None if s is None else s / 3600


def detail(con, folder: Path, l3: str, entry: dict, standard: dict | None = None) -> dict:
    """Everything shown for one Level 3 at Level 4."""
    from app.core import subprocess as sp
    from app.core.narrative import payload_mask
    from app.core.reference import _pairs, dependency
    rows = _l3_rows(con, folder, l3)
    label_of = assigner(entry)
    keys = list(zip(rows["activity"], rows["application"], rows["screen"], rows["action"]))
    names = {k: label_of(dict(zip(UNIT_FIELDS, k))) for k in set(keys)}
    rows["l4"] = [names[k] for k in keys]
    total = float(rows["repeats"].sum()) or 1.0
    named = float(rows.loc[rows["l4"] != OTHER, "repeats"].sum())
    active_total = float(rows["active_seconds"].fillna(0).sum())
    run_rows, segments, cur, seg = [], [], None, None
    for run_id, case, res, l4, ts, active in zip(rows["l3_run"].tolist(), rows["case_id"].tolist(),
                                                 rows["resource"].tolist(), rows["l4"].tolist(), rows["ts"].tolist(),
                                                 rows["active_seconds"].tolist()):
        if cur is None or cur["run_id"] != run_id:
            cur = {"run_id": run_id, "case_id": case, "resource": res, "path": [], "t0": ts, "t1": ts, "active": 0.0}
            run_rows.append(cur)
            seg = None
        if seg is None or seg["l4"] != l4:
            seg = {"run": run_id, "l4": l4, "t0": ts, "t1": ts, "active": 0.0}
            segments.append(seg)
            cur["path"].append(l4)
        seg["t1"] = ts
        seg["active"] += float(active or 0)
        cur["active"] += float(active or 0)
        cur["t1"] = ts
    for r in run_rows:
        r["seconds"] = (r["t1"] - r["t0"]).total_seconds()
    runs = pd.DataFrame(run_rows, columns=["run_id", "case_id", "resource", "path", "seconds", "t0", "t1", "active"])
    n = len(runs)
    segs = pd.DataFrame(segments, columns=["run", "l4", "t0", "t1", "active"])
    descriptions = {s["label"]: s.get("description", "") for s in entry.get("substeps") or []}
    nodes = []
    for name in [s["label"] for s in entry.get("substeps") or []] + [OTHER]:
        sub = rows[rows["l4"] == name]
        if sub.empty:
            continue
        in_runs = segs[segs["l4"] == name]
        apps = sub.groupby("application")["repeats"].sum().sort_values(ascending=False)
        runs_with = int(in_runs["run"].nunique())
        repeats = sum(1 for p in runs["path"] if p.count(name) >= 2)
        main_app = float(apps.iloc[0]) / float(sub["repeats"].sum()) if len(apps) and sub["repeats"].sum() else None
        nodes.append({"name": name, "description": descriptions.get(name, "Clicks that fit no named sub-step."),
                      "users": int(sub["resource"].nunique()), "app_purity": main_app,
                      "runs": runs_with, "cases": int(sub["case_id"].nunique()), "events": int(sub["repeats"].sum()),
                      "share": round(float(sub["repeats"].sum()) / total, 4),
                      # Share of the step's ACTIVE HOURS (the report's measure); None without active time.
                      "active_s": float(sub["active_seconds"].fillna(0).sum()),
                      "active_share": (float(sub["active_seconds"].fillna(0).sum()) / active_total
                                       if active_total else None),
                      "median_active_s": _median(in_runs.groupby("run")["active"].sum()) if runs_with else None,
                      "apps": [str(a) for a in apps.index[:2] if a],
                      "repeat_share": round(repeats / runs_with, 4) if runs_with else 0.0})
    counts: dict[tuple[str, str], list] = {}
    for a, b in zip(segments, segments[1:]):
        if a["run"] != b["run"]:
            continue
        c = counts.setdefault((a["l4"], b["l4"]), [0, set(), []])
        c[0] += 1
        c[1].add(a["run"])
        c[2].append((b["t0"] - a["t1"]).total_seconds())
    edges = [{"src": a, "dst": b, "count": c[0], "runs": len(c[1]), "median_wait_s": _median(c[2])}
             for (a, b), c in sorted(counts.items(), key=lambda x: -x[1][0])]
    for side in ("Start", "End"):
        tally: dict[str, int] = {}
        for p in runs["path"]:
            if p:
                key = p[0] if side == "Start" else p[-1]
                tally[key] = tally.get(key, 0) + 1
        edges += [{"src": "Start", "dst": k, "count": v, "runs": v, "median_wait_s": None} if side == "Start"
                  else {"src": k, "dst": "End", "count": v, "runs": v, "median_wait_s": None} for k, v in tally.items()]
    strength, label, stability, _e = sp._order(con, runs) if n else (0.0, "Weak", None, [])
    pair_counts = _pairs([e for e in edges if e["src"] not in ("Start", "End") and e["dst"] not in ("Start", "End")])
    balanced = [{"a": a, "b": b, "count": x + y} for (a, b), (x, y) in pair_counts.items()
                if x and y and abs(dependency(x, y)) < 0.2]
    ranked: dict[str, list] = {}
    for p, s in zip(runs["path"], runs["seconds"]):
        ranked.setdefault(" → ".join(p), [p, []])[1].append(s)
    order = sorted(ranked.items(), key=lambda kv: (-len(kv[1][1]), kv[0]))
    cum, paths = 0.0, []
    for _k, (p, secs) in order[:10]:
        share = len(secs) / n if n else 0.0
        cum += share
        paths.append({"path": p, "runs": len(secs), "share": round(share, 4), "cumulative_share": round(cum, 4),
                      "median_duration_s": _median(secs)})
    common = next((p for _, (p, _s) in order if len(p) >= 2), order[0][1][0] if order else [])
    if standard and standard.get("standard_path"):
        std, src_label = list(standard["standard_path"]), (sp.SOURCE_BUSINESS if standard.get("confirmed_by_business")
                                                            else "Saved — not confirmed")
        mandatory, confirmed = list(standard.get("mandatory_actions") or []), bool(standard.get("confirmed_by_business"))
    else:
        std, src_label, mandatory, confirmed = common, sp.SOURCE_DATA, [], False
    comp = sp.compare(runs, std, mandatory, confirmed) if n else {"classes": [], "standard_share": 0.0,
                                                                   "top_missing": [], "top_extra": []}
    comp.pop("per_run", None)
    other = rows[rows["l4"] == OTHER].groupby(["application", "screen", "action"])["repeats"].sum() \
        .sort_values(ascending=False)
    return {"l3": l3, "runs": n, "cases": int(runs["case_id"].nunique()) if n else 0, "events": int(total),
            "coverage": round(named / total, 4), "nodes": nodes, "edges": edges, "balanced": balanced,
            "order_strength": strength, "order_strength_label": label, "stability": stability,
            "confidence": sp.confidence(strength, stability, n), "paths": paths,
            "median_active_s": _median(runs["active"]) if n else None,
            "active_h": active_total / 3600 if active_total else None,
            "users": int(rows["resource"].nunique()),
            "standard": {"path": std, "source": src_label, "mandatory_actions": mandatory, "confirmed": confirmed},
            "comparison": comp, "source": entry.get("source"), "provider": entry.get("provider"),
            "sme_by": entry.get("sme_by"), "sme_at": entry.get("sme_at"),
            "model": entry.get("model"), "prompt_version": entry.get("prompt_version"), "reason": entry.get("reason"),
            "confirmed": bool(entry.get("confirmed_by_business")), "taxonomy_hash": entry_hash(entry),
            "unmatched": {"units": int(len(other)),
                          "top": [f"{a or '-'} | {payload_mask(s) or '-'} | {payload_mask(c)}" for (a, s, c) in other.index[:10]]},
            "substeps": [{"label": s["label"], "description": s.get("description", ""), "rules": s.get("rules") or [],
                          "members_shown": sorted({payload_mask(f"{m.get('screen') or '-'} | {m.get('action')}")
                                                   for m in s.get("members") or []})[:50]}
                         for s in entry.get("substeps") or []]}


def source_label(d: dict) -> str:
    if d.get("confirmed"):
        return "Confirmed by business"
    if d.get("source") == "ai":
        return f"AI ({d.get('model')} via {d.get('provider')}) — not confirmed"
    if d.get("source") == "edited":
        return "Edited — not confirmed"
    if d.get("source") == "sme":
        return f"Edited by {d.get('sme_by') or 'SME'} on {(d.get('sme_at') or '')[:10]} — not confirmed"
    return f"Rules — AI not used: {d.get('reason') or 'unknown reason'}"


# --------------------------------------------------------------------------- #
# 7. Summaries (sub-step, step, stage, process): facts + placeholders, template fallback
# --------------------------------------------------------------------------- #
def _facts(rows: list[tuple]) -> list:
    """Local facts for one summary: rows (label, value, unit, support, meta) -> Fact F001..."""
    from app.core.facts import Fact
    return [Fact(id=f"F{i:03d}", type="level4", label=lab, value=val, unit=unit, support=sup, badge="High",
                 section="map", meta=meta or {}) for i, (lab, val, unit, sup, meta) in enumerate(rows, 1)]


def _write(kind: str, name: str, context: str, facts: list, template: str, confidence: str | None, ai: dict,
           avoid: list[str]) -> dict:
    """One summary: the AI (route l4_summary) through the narrative checks, else the template."""
    from app.core import observability
    from app.core.ai_client import AIUnavailable, session
    from app.core.facts import format_fact
    from app.core.narrative import _call, check, parse_reply
    from app.core.screens import TYPED
    from app.core.summary import render
    result = {"text": render(template, facts), "source": "template", "provider": None, "model": None,
              "reason": ai.get("reason")}
    if not ai.get("ok") or not facts:
        return result
    cfg = settings()["ai"]
    (sys_m, user_m), _v = observability.get_prompt(SUMMARY_PROMPT, cfg["prompt_label"], local_prompts("summary"))
    table = "\n".join(f"{f.id} | {f.label} | {format_fact(f)} | {f.badge}" for f in facts)
    values = {"kind": kind, "name": name, "context": context, "confidence": confidence or "-", "facts": table}
    msgs = [{"role": "system", "content": _fill(sys_m["content"], values)},
            {"role": "user", "content": _fill(user_m["content"], values)}]
    client = ai.get("client") or session("l4_summary")
    seq = re.compile(r"\b(" + "|".join(map(re.escape, settings()["flow_confidence"]["sequence_words"])) + r")\b", re.I)
    allowed, by_id = {f.id for f in facts}, {f.id: f for f in facts}
    for _attempt in range(int(cfg["max_retries"]) + 1):
        try:
            reply = _call(client, os.getenv("LLM_MODEL") or "unknown-model", msgs, cfg["temperature"], {})
        except AIUnavailable as err:
            result["reason"] = f"AI not available: {err.short()}"
            return result
        except Exception as err:  # noqa: BLE001
            result["reason"] = f"the AI call failed ({type(err).__name__})"
            continue
        output = parse_reply(reply)
        claims = ai.get("order_claims", True) if kind in ("stage", "process") else confidence in ("High", "Medium")
        problems = [m for ms in check(output, allowed, by_id, None, avoid, claims).values() for m in ms]
        items = output.get("summary") if isinstance(output, dict) and isinstance(output.get("summary"), list) else []
        for item in items:
            text = str(item.get("text", "")) if isinstance(item, dict) else ""
            if TYPED in text or "typed text" in text.lower():
                problems.append("Do not mention typed text.")
            if kind == "step" and confidence == "Exploratory" and seq.search(text):
                problems.append(f"The order of sub-steps is not reliable: do not use '{seq.search(text).group(0)}'.")
        if not problems:
            return {"text": " ".join(render(x["text"], facts) for x in items), "source": "ai",
                    "provider": getattr(client, "provider", None), "model": getattr(client, "model", None), "reason": None}
        result["reason"] = "the AI answer failed the checks"
        msgs = msgs + [{"role": "assistant", "content": reply},
                       {"role": "user", "content": "Your previous answer broke these rules:\n- " + "\n- ".join(problems)
                        + "\nReturn the corrected JSON object only."}]
    return result


def substep_summary(nd: dict) -> str:
    """The sub-step's description and two numbers, in plain words (written by code)."""
    from app.core.mining import format_hours
    share = nd.get("active_share") if nd.get("active_share") is not None else nd["share"]
    what = "time" if nd.get("active_share") is not None else "work"
    text = f"{100 * share:.0f}% of the step's {what}"
    if nd.get("median_active_s") is not None:
        text += f", about {format_hours(_hours(nd['median_active_s']))} per run"
    return f"{nd.get('description') or ''} ({text}).".strip()


def step_summary(d: dict, ai: dict, avoid: list[str], reach: float | None = None) -> dict:
    """2-3 sentences about one step: its active hours and case share first, then its sub-steps."""
    named = [nd for nd in d["nodes"] if nd["name"] != OTHER]
    top = sorted(named, key=lambda x: -x["share"])[:2]
    slow = max((nd for nd in named if nd.get("median_active_s") is not None), key=lambda x: x["median_active_s"],
               default=None)
    head = []
    if d.get("active_h"):
        head.append((f"{d['l3']}: active hours", d["active_h"], "work_hours", d["cases"], {}))
    if reach is not None:
        head.append((f"{d['l3']}: share of cases that reach the step", reach, "share", d["cases"], {}))
    rows = head + [(f"{d['l3']}: {nd['name']} — share of the step's work", nd["share"], "share", d["cases"],
                    {"bucket": nd["name"]}) for nd in top]
    if slow:
        rows.append((f"{d['l3']}: {slow['name']} — median active time per run", _hours(slow["median_active_s"]), "hours",
                     d["cases"], {"bucket": slow["name"]}))
    if d["paths"]:
        rows.append((f"{d['l3']}: work sittings that follow the most common sub-step path", d["paths"][0]["share"], "share",
                     d["cases"], {}))
    facts = _facts(rows)
    ids = [f.id for f in facts]
    h = len(head)
    tpl = ""
    if head:
        bits = []
        if d.get("active_h"):
            bits.append(f"takes {{{{{ids[0]}}}}} of active work")
        if reach is not None:
            bits.append(f"is reached in {{{{{ids[h - 1]}}}}} of cases")
        tpl = f"{d['l3']} " + " and ".join(bits) + ". "
    tpl += f"{d['l3']} consists of {len(named)} sub-step{'s' if len(named) != 1 else ''}"
    tpl += ("; most of the work is " + " and ".join(f"{t['name'][0].lower()}{t['name'][1:]} ({{{{{ids[h + i]}}}}})"
                                                    for i, t in enumerate(top)) + ".") if top else "."
    if d["confidence"] in ("High", "Medium") and len(d["standard"]["path"]) >= 2:
        tpl += " The usual order is " + " → ".join(d["standard"]["path"]) + "."
    else:
        tpl += " The sub-steps are worked in no fixed order."
    k = h + len(top)
    if slow:
        tpl += f" Most time goes to {slow['name'][0].lower()}{slow['name'][1:]} ({{{{{ids[k]}}}}} per run)."
        k += 1
    if d["paths"]:
        tpl += f" The most common way through the step covers {{{{{ids[k]}}}}} of work sittings."
    context = "Sub-steps: " + "; ".join(f"{nd['name']}: {nd.get('description', '')}" for nd in named)
    return _write("step", d["l3"], context, facts, tpl, d["confidence"], ai, avoid)


def stage_summary(stage: str, steps: list[dict], reach: dict[str, float], ai: dict, avoid: list[str]) -> dict:
    rows = []
    for d in steps:
        rows.append((f"{d['l3']}: share of cases that reach the step", reach.get(d["l3"], 0.0), "share", d["cases"],
                     {"l3": d["l3"]}))
    timed = [d for d in steps if d.get("median_active_s") is not None]
    slow = max(timed, key=lambda x: x["median_active_s"], default=None)
    stage_h = sum(d.get("active_h") or 0 for d in steps)
    if stage_h:
        rows.append((f"{stage}: active hours", stage_h, "work_hours", max((d["cases"] for d in steps), default=0),
                     {"stage": stage}))
    if slow:
        rows.append((f"{slow['l3']}: median active time per run", _hours(slow["median_active_s"]), "hours", slow["cases"],
                     {"l3": slow["l3"]}))
    facts = _facts(rows)
    tpl = f"{stage} covers {len(steps)} step{'s' if len(steps) != 1 else ''}: " + ", ".join(
        f"{d['l3']} ({{{{{facts[i].id}}}}} of cases)" for i, d in enumerate(steps)) + "."
    if stage_h:
        tpl += f" Together they take {{{{{facts[len(steps)].id}}}}} of active work."
    for d in steps[:4]:
        top = max((nd for nd in d["nodes"] if nd["name"] != OTHER), key=lambda x: x["share"], default=None)
        if top:
            tpl += f" In {d['l3']} most of the work is {top['name'][0].lower()}{top['name'][1:]}."
    if slow:
        tpl += f" Most time goes to {slow['l3']} ({{{{{facts[-1].id}}}}} per run)."
    context = "Steps and their sub-steps: " + "; ".join(
        f"{d['l3']}: " + ", ".join(nd["name"] for nd in d["nodes"] if nd["name"] != OTHER) for d in steps)
    return _write("stage", stage, context, facts, tpl, None, ai, avoid)


def process_summary(stages: list[dict], details: dict[str, dict], ai: dict, avoid: list[str]) -> dict:
    rows, names = [], []
    for st in stages:
        timed = [details[x] for x in st["l3s"] if details[x].get("median_active_s") is not None]
        if timed:
            slow = max(timed, key=lambda x: x["median_active_s"])
            rows.append((f"{st['stage']}: median active time per run in {slow['l3']}", _hours(slow["median_active_s"]),
                         "hours", slow["cases"], {"stage": st["stage"]}))
            names.append((st["stage"], slow["l3"]))
    facts = _facts(rows)
    tpl = f"The process runs through {len(stages)} stage{'s' if len(stages) != 1 else ''}: " + \
        ", ".join(s["stage"] for s in stages) + "."
    for f, (stage, l3) in list(zip(facts, names))[:5]:
        tpl += f" In {stage}, most time goes to {l3} ({{{{{f.id}}}}} per run)."
    context = "Stages and steps: " + "; ".join(f"{s['stage']}: {', '.join(s['l3s'])}" for s in stages)
    return _write("process", "the process", context, facts, tpl, None, ai, avoid)


def provenance(s: dict | None) -> str:
    if not s:
        return ""
    if s.get("source") == "ai":
        return f"Written by AI ({s.get('model')} via {s.get('provider')}); every number inserted by the system."
    return "Generated from fixed templates" + (f" ({s['reason']})." if s.get("reason") else ".")


# --------------------------------------------------------------------------- #
# 8. The mining section
# --------------------------------------------------------------------------- #
def _structure(folder: Path, l3s: list[str]) -> tuple[list[dict], dict[str, float]]:
    """Stages in the order their steps usually start (median position in the case), each with its Level 3
    steps in that order; and the share of cases reaching each Level 3."""
    src = f"read_parquet({sql_literal((Path(folder) / 'events.parquet').as_posix())})"
    con = duckdb.connect()
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    l3col = "COALESCE(level3, activity)" if "level3" in cols else "activity"
    stage = "COALESCE(stage, '(no stage)')" if "stage" in cols else "'(no stage)'"
    total = con.execute(f"SELECT COUNT(DISTINCT case_id) FROM {src}").fetchall()[0][0] or 1
    rows = con.execute(f"""
        WITH e AS (SELECT case_id, {l3col} AS l3, {stage} AS stage,
                          epoch(ts_start) - epoch(MIN(ts_start) OVER (PARTITION BY case_id)) AS pos FROM {src}
                   WHERE case_id IS NOT NULL AND ts_start IS NOT NULL)
        SELECT l3, mode(stage), COUNT(DISTINCT case_id), median(pos) FROM e GROUP BY 1""").fetchall()
    wanted = set(l3s)
    info = {r[0]: (r[1], r[2] / total, r[3] or 0) for r in rows if r[0] in wanted}
    by_stage: dict[str, list] = {}
    for l3, (st, _share, pos) in info.items():
        by_stage.setdefault(st, []).append((pos, l3))
    ordered = sorted(by_stage.items(), key=lambda kv: min(p for p, _ in kv[1]))
    return ([{"stage": st, "l3s": [l3 for _, l3 in sorted(v)]} for st, v in ordered],
            {l3: v[1] for l3, v in info.items()})


def analyse(con, events_path: Path, signature: str, use_ai: bool = True, progress=None) -> dict:
    """The `level4` section of mining.json. With the AI on and level4.auto_build, unconfirmed steps without an
    AI taxonomy are built first (one at a time). Then per Level 3 the detail and summaries, per stage a
    summary, and a process overview."""
    from app.core.narrative import ai_status, user_names
    from app.core.subprocess import ACTIONS_FILE, load_standards
    folder = Path(events_path).parent
    if not (folder / ACTIONS_FILE).exists():
        return {"available": False, "reason": "No Action / UI event column was mapped, so Level 4 sub-steps cannot "
                                              "be built. Map an action column on Import data."}
    cat = catalogue(folder)
    if cat.empty:
        return {"available": False, "reason": "Not available: convert the file again to build the action catalogue."}
    if use_ai and _cfg().get("auto_build"):
        _build(folder, signature, folder.name, "reuse", None, None, use_ai, progress)
    tax = load(signature)
    standards = load_standards(signature)
    why_not = (_client(folder.name)[1] if use_ai else "AI switched off for this run") or "no saved taxonomy yet"
    details = {}
    for l3 in sorted(cat["l3"].dropna().unique()):
        entry = tax.get(l3) or rules_entry(l3, units_of(cat, l3), why_not)
        details[l3] = detail(con, folder, l3, entry, standards.get(f"L4:{l3}"))
    stages, reach = _structure(folder, list(details))
    ok, why = ai_status() if use_ai else (False, "switched off for this run")
    from app.core import privacy
    if ok and not privacy.ai_allowed()[0]:
        ok, why = False, privacy.ai_allowed()[1]
    ai = {"ok": bool(ok), "reason": None if ok else f"AI not used: {why}"}
    from app.core.reference import load_order
    saved = load_order(signature)
    ai["order_claims"] = bool(saved and saved.confirmed_by_business)   # stages / process: only a confirmed order
    avoid = user_names(folder.name)
    for l3, d in details.items():
        if progress:
            progress(f"Level 4: summarising {l3}")
        for nd in d["nodes"]:
            nd["summary"] = substep_summary(nd)
        d["summary"] = step_summary(d, ai, avoid, reach.get(l3))
    for st in stages:
        st["summary"] = stage_summary(st["stage"], [details[x] for x in st["l3s"]], reach, ai, avoid)
    # Quality measures per Level 3 (level4_quality.json; level4_view and the report appendix).
    entries = {l3: tax.get(l3) or rules_entry(l3, units_of(cat, l3), why_not) for l3 in details}
    previous = None
    try:
        previous = json.loads((folder / QUALITY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    from app.core.milestones import atomic_json
    atomic_json(folder / QUALITY_FILE, quality(details, entries, {l3: units_of(cat, l3) for l3 in details}, previous))
    return {"available": True, "reason": None, "l3s": [details[x] for x in sorted(details)], "stages": stages,
            "reach": reach, "overview": process_summary(stages, details, ai, avoid)}


# --------------------------------------------------------------------------- #
# 9. Maps
# --------------------------------------------------------------------------- #
def storyboard_dot(section: dict) -> str:
    """Stages as columns (left to right), each with its Level 3 steps as boxes listing their sub-steps
    "1. Label — P%" (at most level4.max_lines_per_step, then "+N more"). No arrows between sub-steps."""
    import html as _html
    from app.core.mining import _dot_text, format_hours
    details = {d["l3"]: d for d in section.get("l3s") or []}
    reach = section.get("reach") or {}
    max_lines = int(_cfg()["max_lines_per_step"])
    lines = ["digraph storyboard {", "  rankdir=LR;", "  newrank=true;", "  nodesep=0.25;", "  ranksep=0.6;",
             '  node [shape=plain, fontname="Helvetica", fontsize=11];']
    firsts = []
    for i, st in enumerate(section.get("stages") or []):
        lines += [f"  subgraph cluster_{i} {{", f"    label=<<b>{_html.escape(st['stage'])}</b>>;",
                  '    style="rounded,filled"; fillcolor="#f6f8fb"; color="#d9e2ec"; fontname="Helvetica"; fontsize=14;']
        for j, l3 in enumerate(st["l3s"]):
            d = details[l3]
            named = sorted((nd for nd in d["nodes"] if nd["name"] != OTHER), key=lambda x: -x["share"])
            t = format_hours(_hours(d["median_active_s"])) if d.get("median_active_s") is not None else "–"
            head = (f'<tr><td align="left" bgcolor="#dbe6f4"><b>{_html.escape(l3)}</b><br/>'
                    f'{100 * reach.get(l3, 0):.0f}% of cases · median {_html.escape(t)}</td></tr>')
            body = "".join(f'<tr><td align="left">{k}. {_html.escape(nd["name"])} — {100 * nd["share"]:.0f}%</td></tr>'
                           for k, nd in enumerate(named[:max_lines], 1))
            more = f'<tr><td align="left"><i>+{len(named) - max_lines} more</i></td></tr>' if len(named) > max_lines else ""
            node = f"s{i}_{j}"
            lines.append(f'    {node} [label=<<table border="1" cellborder="0" cellspacing="0" cellpadding="4" '
                         f'color="#4a6fa5" bgcolor="white">{head}{body}{more}</table>>];')
            if j == 0:
                firsts.append(node)
        lines.append("  }")
    for a, b in zip(firsts, firsts[1:]):
        lines.append(f"  {a} -> {b} [style=invis];")
    legend = ("Columns = stages in order · box = step (cases reached, median active time per run) · "
              "list = its sub-steps with their share of the step's work")
    lines.append(f'  legend [shape=plaintext, label="{_dot_text(legend)}", fontsize=10, fontcolor="#616e7c"];')
    if firsts:
        lines.append(f"  {firsts[0]} -> legend [style=invis];")
    lines.append("}")
    return "\n".join(lines)


def flow_dot(d: dict, show_all: bool = False) -> str:
    """The Level 4 flow of one step, filtered to stay readable: the most frequent arrows until
    level4.edge_coverage of transitions plus every arrow in >= min_edge_run_share of runs (at most max_edges);
    pairs done both ways about equally as ONE dashed ⇄ arrow; the top 2 entry and exit arrows only. Labels are
    "% of runs". `show_all` draws every arrow."""
    from app.core.mining import _dot_text, format_hours
    from app.core.reference import dependency
    cfg = _cfg()
    runs = max(d["runs"], 1)
    nodes = sorted(d["nodes"], key=lambda x: -x["events"])[:int(cfg["max_map_buckets"])]
    ids = {nd["name"]: f"b{i}" for i, nd in enumerate(nodes)}
    inner = [e for e in d["edges"] if e["src"] in ids and e["dst"] in ids and e["src"] != e["dst"]]
    starts = sorted((e for e in d["edges"] if e["src"] == "Start" and e["dst"] in ids), key=lambda e: -e["count"])
    ends = sorted((e for e in d["edges"] if e["dst"] == "End" and e["src"] in ids), key=lambda e: -e["count"])
    if not show_all:
        total = sum(e["count"] for e in inner) or 1
        keep, covered = [], 0
        for e in sorted(inner, key=lambda e: -e["count"]):
            if covered / total < float(cfg["edge_coverage"]) or e["runs"] / runs >= float(cfg["min_edge_run_share"]):
                keep.append(e)
                covered += e["count"]
        inner = keep
        starts, ends = starts[:int(cfg["max_start_end_edges"])], ends[:int(cfg["max_start_end_edges"])]
    by = {(e["src"], e["dst"]): e for e in (inner if show_all else [x for x in d["edges"] if x["src"] in ids
                                                                    and x["dst"] in ids])}
    drawn, done = [], set()
    for e in sorted(inner, key=lambda e: -e["count"]):
        key = (e["src"], e["dst"])
        if key in done:
            continue
        back = by.get((e["dst"], e["src"]))
        if back and abs(dependency(e["count"], back["count"])) < 0.2:
            drawn.append((e["src"], e["dst"], (e["runs"] + back["runs"]) / runs, True))
            done |= {key, (e["dst"], e["src"])}
        else:
            drawn.append((e["src"], e["dst"], e["runs"] / runs, False))
            done.add(key)
    if not show_all:
        drawn = drawn[:int(cfg["max_edges"])]
    lines = ["digraph level4 {", "  rankdir=LR;", '  node [shape=box, style="rounded,filled", fillcolor="#eef3fb", '
             'color="#4a6fa5", fontname="Helvetica", fontsize=11];', '  edge [color="#666666", fontname="Helvetica", '
             'fontsize=9];', '  Start [shape=circle, label="", width=0.25, style=filled, fillcolor="#2f9e44"];',
             '  End [shape=doublecircle, label="", width=0.22, style=filled, fillcolor="#1f2933"];']
    for nd in nodes:
        t = f" · median {format_hours(_hours(nd['median_active_s']))}" if nd.get("median_active_s") is not None else ""
        fill = "#f0f0f0" if nd["name"] == OTHER else "#eef3fb"
        lines.append(f'  {ids[nd["name"]]} [label="{_dot_text(nd["name"])}\\n{100 * nd["share"]:.0f}% of work{t}", '
                     f'fillcolor="{fill}"];')
    for e in starts:
        lines.append(f'  Start -> {ids[e["dst"]]} [label="{100 * e["runs"] / runs:.0f}%"];')
    for e in ends:
        lines.append(f'  {ids[e["src"]]} -> End [label="{100 * e["runs"] / runs:.0f}%"];')
    for a, b, share, both in drawn:
        style = ', dir=both, style=dashed, color="#c0504d"' if both else ""
        lines.append(f'  {ids[a]} -> {ids[b]} [label="{"⇄ " if both else ""}{100 * share:.0f}%"{style}];')
    legend = ("Box = sub-step (share of the step's work · median active time per run) · arrow = next sub-step "
              "(% of runs) · dashed ⇄ = done both ways" + ("" if show_all else " · rare arrows hidden"))
    lines.append(f'  legend [shape=plaintext, label="{_dot_text(legend)}", fontsize=9, fontcolor="#616e7c"];')
    lines.append("  End -> legend [style=invis];")
    lines.append("}")
    return "\n".join(lines)


def edge_count(dot: str) -> dict:
    """Arrows in a flow DOT: {"inner", "start", "end", "both"}."""
    lines = [ln.strip() for ln in dot.splitlines() if "->" in ln and "invis" not in ln]
    return {"start": sum(ln.startswith("Start ->") for ln in lines), "end": sum("-> End" in ln for ln in lines),
            "inner": sum(not ln.startswith("Start ->") and "-> End" not in ln for ln in lines),
            "both": sum("dir=both" in ln for ln in lines)}


# --------------------------------------------------------------------------- #
# 10. Tables and updates
# --------------------------------------------------------------------------- #
def level_rows(tree: list[dict], section: dict | None) -> list[dict]:
    """The process levels as one row per leaf with EVERY level column filled (Level 1, Stage, Level 3, Sub-step),
    plus cases and share. Without a level 3 column the task is the Level 3."""
    if not tree:
        return []
    names = []
    for n in sorted(tree, key=lambda x: x["depth"]):
        if n["level"] not in names:
            names.append(n["level"])
    l3_level = "Process level 3" if "Process level 3" in names else "Step"
    cols = names[:names.index(l3_level) + 1]
    by_l3 = {d["l3"]: d for d in (section or {}).get("l3s") or []}
    rows = []
    for n in sorted((x for x in tree if x["level"] == l3_level), key=lambda x: x["path"]):
        base = {("Level 3" if c in ("Process level 3", "Step") else c): n["path"][i] for i, c in enumerate(cols)}
        d = by_l3.get(n["name"])
        if not d:
            rows.append({**base, "Sub-step": "(no sub-steps)", "Cases": n["cases"],
                         "Share of cases %": round(100 * n["share"], 1)})
            continue
        for nd in d["nodes"]:
            rows.append({**base, "Sub-step": nd["name"], "Cases": nd["cases"],
                         "Share of cases %": round(100 * n["share"], 1),
                         "Share of the step's work %": round(100 * nd["share"], 1)})
    return rows


def update_mining(folder: Path, d: dict) -> None:
    """Write one recomputed Level 3 into mining.json (after an edit on page 3), keeping its summaries."""
    path = Path(folder) / "mining.json"
    if not path.exists():
        return
    m = json.loads(path.read_text(encoding="utf-8"))
    sec = m.get("level4") or {}
    if not sec.get("available"):
        return
    old = next((x for x in sec["data"]["l3s"] if x["l3"] == d["l3"]), None)
    if old and "summary" in old and "summary" not in d:
        d["summary"] = old["summary"]
    for nd in d["nodes"]:
        nd.setdefault("summary", substep_summary(nd))
    sec["data"]["l3s"] = sorted([x for x in sec["data"]["l3s"] if x["l3"] != d["l3"]] + [d], key=lambda x: x["l3"])
    path.write_text(json.dumps(m, indent=2, default=str), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 11. Sharper Level 4: refinement of a new build, names from your documents, SME edits, quality
# --------------------------------------------------------------------------- #
NAVIGATE = "Navigate and search"
QUALITY_FILE = "level4_quality.json"


def _words(text) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", str(text or "").lower()) if w not in STOPWORDS}


def is_navigation(action) -> bool:
    """A click made only of navigation words (level4.navigation_words), e.g. "Select Search", "View List"."""
    words = _words(action)
    return bool(words) and words <= {str(w).lower() for w in _cfg().get("navigation_words") or []}


def business_definition(job_id: str):
    """The business definition behind a job: the one checked against it on page 4, or - before any check - the
    newest definition whose steps cover the job's steps (its knowledge sources are the documents you loaded)."""
    from app.core import definition as dmod, jobs
    try:
        job = jobs.load_job(job_id)
    except Exception:  # noqa: BLE001 - no job: no definition
        return None
    defn = dmod.for_job(job)
    if defn is not None:
        return defn
    try:
        acts = set(catalogue(jobs.job_folder(job_id))["l3"].dropna())
    except Exception:  # noqa: BLE001
        acts = set()
    best, score = None, 0
    for name in dmod.list_definitions():
        d = dmod.load(name)
        if d is None:
            continue
        covered = {a for s in d.steps for a in (s.get("mapped_activities") or [])} | {s["name"] for s in d.steps}
        n = len(covered & acts)
        if n > score:
            best, score = d, n
    return best


def doc_context(job_id: str, l3: str, max_lines: int = 15) -> list[str]:
    """What your documents say about this Level 3, as short lines for the Level 4 AI: the definition step's own
    description and business rules, and the knowledge statements about it (sub-steps, rules, systems, owners,
    exceptions, workarounds, pain points). Statement fields only, masked like every AI payload: never the
    document quotes, rows, users or IDs."""
    from app.core import knowledge
    from app.core.narrative import payload_mask
    defn = business_definition(job_id)
    if defn is None:
        return []
    step = next((s for s in defn.steps if l3 in (s.get("mapped_activities") or []) or s["name"] == l3), None)
    names = {l3.lower()} | ({step["name"].lower()} if step else set())
    lines = []
    if step:
        desc = re.sub(r"\s*\[Source:.*?\]", "", str(step.get("description") or "")).strip()
        for label, text in (("description", desc), ("business rules", step.get("business_rules")),
                            ("system", step.get("system")), ("owner", step.get("owner_role"))):
            if str(text or "").strip():
                lines.append(f"{label}: {str(text).strip()[:200]}")
    wanted = {"step_exists", "rule", "system", "owner", "exception", "workaround", "pain_point", "optional",
              "mandatory", "sla"}
    try:
        statements = knowledge.all_statements(defn.name)
    except Exception:  # noqa: BLE001 - no sources
        statements = []
    for st_ in statements:
        about = {str(st_.get(k) or "").lower() for k in ("subject", "object", "parent")}
        if st_.get("type") not in wanted or not (about & names):
            continue
        if st_["type"] == "step_exists" and str(st_.get("subject") or "").lower() in names:
            continue                                   # "the step exists" says nothing about how it is done
        detail = st_.get("value") or (st_.get("object") if str(st_.get("object") or "").lower() not in names else "")
        line = f"{st_['type'].replace('_', ' ')}: {st_['subject']}" + (f" — {detail}" if detail else "")
        if line not in lines:
            lines.append(line)
    return [payload_mask(x)[:220] for x in lines[:max_lines]]


def doc_names(job_id: str, l3: str) -> list[str]:
    """Sub-step names your documents give for this Level 3 (step_exists statements whose parent is this step, or
    a definition step mapped to it), at most level4.max_doc_names. Names only: never quotes or rows."""
    from app.core import knowledge
    defn = business_definition(job_id)
    if defn is None:
        return []
    parents = {l3.lower()} | {s["name"].lower() for s in defn.steps if l3 in (s.get("mapped_activities") or [])}
    names = []
    for st in knowledge.all_statements(defn.name):
        if st.get("type") == "step_exists" and str(st.get("parent") or "").lower() in parents:
            if st["subject"] not in names and st["subject"].lower() not in parents:
                names.append(st["subject"])
    return names[:int(_cfg().get("max_doc_names", 30))]


def _closest(label: str, names: list[str]) -> str | None:
    """The document name a label means (word overlap >= level4.tail_overlap), if any."""
    lw = _words(label)
    best, score = None, 0.0
    for n in names:
        nw = _words(n)
        if lw and nw:
            s = len(lw & nw) / len(lw | nw)
            if s > score:
                best, score = n, s
    return best if score >= float(_cfg()["tail_overlap"]) else None


def refine(entry: dict, units: list[dict], names: list[str] | None = None, screen_context: bool = True) -> dict:
    """Deterministic clean-up of a NEW build (never applied to a saved taxonomy, so saved assignments stay):
    1. labels that mean a document name take that name (named_from document, else ai);
    2. navigation clicks go to the sub-step of their screen when the screen is specific (most of its other clicks
       are in one sub-step), else to one sub-step "Navigate and search" - their hours are kept (a rules build,
       `screen_context=False`, has one sub-step per screen, so all its navigation clicks go to "Navigate and search");
    3. the remaining tail joins a sub-step of the same application when the click words overlap at least
       level4.tail_overlap; what is still unassigned becomes the review queue."""
    entry = json.loads(json.dumps(entry))
    subs = entry.get("substeps") or []
    for s in subs:
        doc = _closest(s["label"], names or [])
        s["named_from"] = s.get("named_from") or ("document" if doc else "ai")
        if doc:
            s["label"] = doc
    label_of = assigner(entry)
    assigned = {unit_key(u["activity"], u["application"], u["screen"], u["action"]): label_of(u) for u in units}
    by_screen: dict[tuple, dict[str, int]] = {}
    for u in units:
        lab = assigned[unit_key(u["activity"], u["application"], u["screen"], u["action"])]
        if lab != OTHER and not is_navigation(u["action"]) and u.get("screen"):
            d = by_screen.setdefault((u["application"], u["screen"]), {})
            d[lab] = d.get(lab, 0) + int(u.get("events", 0))
    target = {s["label"]: s for s in subs}

    def add_member(label: str, u: dict, named_from: str = "ai") -> None:
        if label not in target:
            target[label] = {"id": f"S{len(subs) + 1}", "label": label, "description": "", "rules": [], "members": [],
                             "named_from": named_from}
            subs.append(target[label])
        target[label]["members"].append(_member(u))
        assigned[unit_key(u["activity"], u["application"], u["screen"], u["action"])] = label

    for u in units:
        if not is_navigation(u["action"]):
            continue
        counts = by_screen.get((u["application"], u["screen"])) or {}
        top = max(counts, key=lambda k: (counts[k], k)) if counts else None
        if screen_context and top and counts[top] >= 0.6 * sum(counts.values()):
            add_member(top, u)                                       # e.g. "Select Search" on the offer screen
        else:
            add_member(NAVIGATE, u, "rule")
    if NAVIGATE in target:
        target[NAVIGATE]["description"] = "Moving around and searching the applications (generic clicks)."
    pool = {}
    for u in units:
        lab = assigned[unit_key(u["activity"], u["application"], u["screen"], u["action"])]
        if lab not in (OTHER, NAVIGATE):
            pool.setdefault(u["application"], []).append((_words(u["action"]), lab))
    queue = []
    for u in sorted(units, key=lambda u: -int(u.get("events", 0))):
        if assigned[unit_key(u["activity"], u["application"], u["screen"], u["action"])] != OTHER:
            continue
        w, best, score = _words(u["action"]), None, 0.0
        for ow, lab in pool.get(u["application"], []):
            if w and ow:
                s = len(w & ow) / len(w | ow)
                if s > score:
                    best, score = lab, s
        if best and score >= float(_cfg()["tail_overlap"]):
            add_member(best, u)
        else:
            queue.append({"application": u["application"], "screen": u.get("screen") or "", "action": u["action"],
                          "events": int(u.get("events", 0))})
    entry["substeps"] = subs
    entry["review_queue"] = queue[:50]
    return entry


def keep_locked(old: dict | None, new: dict) -> dict:
    """A rebuild never changes LOCKED sub-steps: their label, description, rules and members are kept as they are
    (members win over rules), and an AI sub-step with the same label is dropped."""
    locked = [s for s in (old or {}).get("substeps") or [] if s.get("locked")]
    if not locked:
        return new
    keep = {s["label"] for s in locked}
    rest = [s for s in new.get("substeps") or [] if s["label"] not in keep]
    return {**new, "substeps": locked + rest}


def _sme(entry: dict, by: str) -> dict:
    return {**entry, "source": "sme", "sme_by": by or "SME", "sme_at": _now(), "confirmed_by_business": False}


def _assigned_units(entry: dict, units: list[dict], label: str) -> list[dict]:
    label_of = assigner(entry)
    return [u for u in units if label_of(u) == label]


def sme_rename(entry: dict, label: str, new: str, by: str = "") -> dict:
    e = json.loads(json.dumps(entry))
    for s in e["substeps"]:
        if s["label"] == label:
            s["label"], s["named_from"] = new.strip(), "sme"
    return _sme(e, by)


def sme_merge(entry: dict, labels: list[str], new: str, units: list[dict], by: str = "") -> dict:
    """Merge sub-steps into one: their units become members of the merged sub-step."""
    e = json.loads(json.dumps(entry))
    moved = [_member(u) for lab in labels for u in _assigned_units(entry, units, lab)]
    e["substeps"] = [s for s in e["substeps"] if s["label"] not in labels]
    e["substeps"].append({"id": f"S{len(e['substeps']) + 1}", "label": new.strip(), "description": "", "rules": [],
                          "members": moved, "named_from": "sme"})
    return _sme(e, by)


def sme_split(entry: dict, label: str, screens: list[str], new: str, units: list[dict], by: str = "") -> dict:
    """Move the units of a sub-step on the given screens into a new sub-step (a Level 5 split, when accepted)."""
    e = json.loads(json.dumps(entry))
    moved = [_member(u) for u in _assigned_units(entry, units, label) if (u.get("screen") or "") in set(screens)]
    e["substeps"].append({"id": f"S{len(e['substeps']) + 1}", "label": new.strip(), "description": "", "rules": [],
                          "members": moved, "named_from": "sme"})
    return _sme(e, by)


def sme_move(entry: dict, unit: dict, target: str, by: str = "") -> dict:
    """Move one unit (activity, application, screen, action) to another sub-step."""
    e = json.loads(json.dumps(entry))
    key = unit_key(unit.get("activity"), unit.get("application"), unit.get("screen"), unit.get("action"))
    for s in e["substeps"]:
        s["members"] = [m for m in s.get("members") or []
                        if unit_key(m.get("activity"), m.get("application"), m.get("screen"), m.get("action")) != key]
    for s in e["substeps"]:
        if s["label"] == target:
            s["members"].append(_member(unit))
    return _sme(e, by)


def sme_lock(entry: dict, label: str, locked: bool = True, by: str = "") -> dict:
    e = json.loads(json.dumps(entry))
    for s in e["substeps"]:
        if s["label"] == label:
            s["locked"] = bool(locked)
    return _sme(e, by)


def assignment_hash(entry: dict, units: list[dict]) -> str:
    """The assignments (unit -> sub-step) as a hash: the same input gives the same hash (stability)."""
    label_of = assigner(entry)
    pairs = sorted((json.dumps(unit_key(u["activity"], u["application"], u["screen"], u["action"])), label_of(u))
                   for u in units)
    return hashlib.sha256(json.dumps(pairs).encode()).hexdigest()[:16]


def label_problems(label: str, apps: set[str]) -> list[str]:
    from app.core.milestones import first_names, looks_like_person
    from app.core.screens import app_names
    words = label.split()
    out = []
    if label in (OTHER,):
        return out
    if len(words) > 6:
        out.append("more than 6 words")
    verbs = {str(v).lower() for v in _cfg().get("label_verbs") or []}
    if words and words[0].lower() not in verbs:
        out.append("does not start with a verb")
    if re.search(r"\d", label):
        out.append("has digits")
    low = label.lower()
    if any(a and a.lower() in low for a in apps | set(app_names())):
        out.append("names an application")
    if looks_like_person(label) or any(w.lower().strip(".,") in first_names() for w in words[1:]):
        out.append("may name a person")
    return out


def quality(details: dict[str, dict], entries: dict[str, dict], units: dict[str, list[dict]],
            previous: dict | None = None) -> dict:
    """Per Level 3 the quality measures of its sub-steps (targets in README section 28): events in named
    sub-steps, Other's share of hours, label rules, balance, users, rule overlap, stability, app purity, split
    proposals. A step that misses a target gets a flag with the reason and the top 10 units causing it."""
    cfg = _cfg()
    out = {}
    for l3, d in details.items():
        entry, us = entries.get(l3) or {}, units.get(l3) or []
        label_of = assigner(entry)
        hours_based = any(n.get("active_share") is not None for n in d["nodes"])
        share = lambda n: (n.get("active_share") if hours_based else n["share"]) or 0.0  # noqa: E731
        other = next((n for n in d["nodes"] if n["name"] == OTHER), None)
        apps = {str(u["application"]) for u in us if u.get("application")}
        rules, _members = _compiled(entry)
        overlap = []
        for u in us:
            hits = {r[0] for r in rules if _rule_hits(r, u)}
            if len(hits) > 1:
                overlap.append({"unit": f"{u['application'] or '-'} | {u.get('screen') or '-'} | {u['action']}",
                                "sub_steps": sorted(hits), "events": int(u.get("events", 0))})
        per = {}
        for n in d["nodes"]:
            mine = [u for u in us if label_of(u) == n["name"]]
            per[n["name"]] = {"units": len(mine), "share": share(n), "users": n.get("users"),
                              "app_purity": n.get("app_purity"),
                              "label_problems": label_problems(n["name"], apps) if n["name"] != OTHER else [],
                              "top_units": [f"{u['application'] or '-'} | {u.get('screen') or '-'} | {u['action']}"
                                            for u in sorted(mine, key=lambda u: -int(u.get("events", 0)))[:10]]}
        h = assignment_hash(entry, us)
        prev = (previous or {}).get(l3) or {}
        if not prev:
            stability = "first run"
        elif prev.get("taxonomy_hash") != d.get("taxonomy_hash"):
            stability = "taxonomy changed"          # a new or edited taxonomy: nothing to compare
        else:
            stability = "stable" if prev.get("assignment_hash") == h else "changed"
        flags = []
        if d["coverage"] < 0.9:
            flags.append({"measure": "Events in named sub-steps", "reason": f"{100 * d['coverage']:.1f}% (target 90%)",
                          "units": per.get(OTHER, {}).get("top_units", [])})
        if other and share(other) > 0.10:
            flags.append({"measure": "Other's share of hours", "reason": f"{100 * share(other):.1f}% (target ≤ 10%)",
                          "units": per[OTHER]["top_units"]})
        bad = [(k, v["label_problems"]) for k, v in per.items() if v["label_problems"]]
        if bad:
            flags.append({"measure": "Labels", "reason": "; ".join(f"{k}: {', '.join(p)}" for k, p in bad[:5]),
                          "units": [k for k, _ in bad][:10]})
        big = [k for k, v in per.items() if k != OTHER and v["share"] > 0.5 and v["units"] > 3]
        if big:
            flags.append({"measure": "Balance", "reason": f"{big[0]} holds {100 * per[big[0]]['share']:.0f}% of the step",
                          "units": per[big[0]]["top_units"]})
        lonely = [k for k, v in per.items() if k != OTHER and v["users"] is not None and v["users"] < 2]
        if lonely:
            flags.append({"measure": "Used by people", "reason": f"used by fewer than 2 users: {', '.join(lonely[:5])}",
                          "units": [u for k in lonely[:3] for u in per[k]["top_units"][:3]]})
        if overlap:
            flags.append({"measure": "Overlap", "reason": f"{len(overlap)} units match more than one rule",
                          "units": [o["unit"] for o in sorted(overlap, key=lambda o: -o["events"])[:10]]})
        if stability == "changed":
            flags.append({"measure": "Stability", "reason": "the same input gave different assignments", "units": []})
        splits = [{"sub_step": k, "share": v["share"], "units": v["units"],
                   "screens": sorted({u.split(" | ")[1] for u in v["top_units"]})}
                  for k, v in per.items() if k not in (OTHER, NAVIGATE) and v["share"] > float(cfg["split_hours_share"])
                  and v["units"] > int(cfg["split_min_units"])]
        out[l3] = {"named_events_share": d["coverage"], "other_hours_share": share(other) if other else 0.0,
                   "hours_based": hours_based, "sub_steps": per, "overlap_units": len(overlap), "overlap": overlap[:10],
                   "assignment_hash": h, "taxonomy_hash": d.get("taxonomy_hash"), "stability": stability,
                   "flags": flags, "split_proposals": splits, "review_queue": (entry.get("review_queue") or [])[:20],
                   "events": d["events"], "users": d.get("users"), "active_h": d.get("active_h")}
    return out
