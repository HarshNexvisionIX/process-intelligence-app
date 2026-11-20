"""Level 4 / 5 activities inside each step -> nc_activities.parquet + activity (and activity_l5) on every event.

Per step, the step's units (app, screen, click) are grouped into 3-10 activities:
    1 confirmed groups from <library>/activities.yaml (reused first, status sme_confirmed)
    2 AI groups (task nc_activities): label verb + object (at most 6 words, no digits, apps or names),
      description (at most 30 words, no digits or names) and regex rules; code checks them like level4 does:
      3-10 groups, coverage of the step's events >= min_coverage, Other <= max_other, unique labels, safe regexes.
      Saved to activities.proposed.yaml (status proposed).
    3 fallback: one group per main screen, "Work in <screen> (<app>)", the rest Other, with the reason.
Level 5: inside an activity with more than level5_min_units units, its clicks (the top ones, the rest Other).
Totals check: activity events, hours and actors add up to the step's (no row lost).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import yaml

from app.core.config import settings
from app.core.db import connect, sql_literal
from app.nocase import library

ACT_FILE = "nc_activities.parquet"
CHECK_FILE = "nc_activities_check.json"
OTHER = "Other"


def _cfg() -> dict:
    return settings()["nocase"]["activities"]


def _hits(rule: dict, u) -> bool:
    app = str(rule.get("app") or "*").lower()
    if app != "*" and str(u["app"] or "").lower() != app:
        return False
    for key, col in (("screen", "screen_clean"), ("action", "action_clean")):
        if rule.get(key) and not re.search(str(rule[key]), str(u[col] or ""), re.I):
            return False
    return bool(rule.get("screen") or rule.get("action"))


def apply_groups(units: pd.DataFrame, groups: list[dict]) -> pd.Series:
    """unit_id -> group label (first matching group, in order), Other when none."""
    out = {}
    for uid, u in units.iterrows():
        out[uid] = next((g["label"] for g in groups if any(_hits(r, u) for r in g.get("rules") or [])), OTHER)
    return pd.Series(out, dtype=object)


def check_groups(data, units: pd.DataFrame, apps: set[str]) -> list[str]:
    from app.core.level4 import label_problems, static_regex_problem
    c = _cfg()
    groups = data.get("groups")
    if not isinstance(groups, list):
        return ['"groups" must be a list of {"label", "description", "rules": [{"app", "screen", "action"}]}']
    errs = []
    if not int(c["min_groups"]) <= len(groups) <= int(c["max_groups"]):
        errs.append(f"use between {c['min_groups']} and {c['max_groups']} groups (found {len(groups)})")
    labels = [str(g.get("label") or "").strip() for g in groups if isinstance(g, dict)]
    if len(set(x.lower() for x in labels)) != len(labels):
        errs.append("labels must be unique")
    clicks = {str(a).lower() for a in units["action_clean"].dropna()}
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            errs.append(f"groups[{i}] must be an object")
            continue
        lab = str(g.get("label") or "").strip()
        errs += [f"groups[{i}].label '{lab}' {p}" for p in label_problems(lab, apps)]
        if lab.lower() in clicks:
            errs.append(f"groups[{i}].label '{lab}' is a click name; describe the business activity")
        desc = str(g.get("description") or "")
        if len(desc.split()) > int(c["max_description_words"]) or re.search(r"\d", desc):
            errs.append(f"groups[{i}].description must have at most {c['max_description_words']} words and no digits")
        for j, r in enumerate(g.get("rules") or []):
            for key in ("screen", "action"):
                if r.get(key):
                    why = static_regex_problem(str(r[key]))
                    if why:
                        errs.append(f"groups[{i}].rules[{j}].{key} {why}")
        if not g.get("rules"):
            errs.append(f"groups[{i}] needs at least one rule")
    if errs:
        return errs
    assigned = apply_groups(units, groups)
    ev = units["events"]
    cover = float(ev[assigned != OTHER].sum() / ev.sum()) if ev.sum() else 0.0
    if cover < float(c["min_coverage"]):
        errs.append(f"the groups cover {100 * cover:.0f}% of the step's events (at least "
                    f"{100 * float(c['min_coverage']):.0f}% needed)")
    if 1 - cover > float(c["max_other"]):
        errs.append(f"Other holds {100 * (1 - cover):.0f}% (at most {100 * float(c['max_other']):.0f}%)")
    return errs


def fallback_groups(units: pd.DataFrame) -> list[dict]:
    """One group per main screen (largest first, at most max_groups - 1), labelled verb + object with the
    case-ID mode's rule-based labeller (level4.fallback_label: "Update cost model", "Review account
    matching"); two screens that give the same label are told apart by their application."""
    from app.core.level4 import fallback_label
    by = units.groupby(["app", "screen_clean"], dropna=False)["events"].sum().sort_values(ascending=False)
    groups, seen = [], {}
    for (app, screen), _n in by.head(int(_cfg()["max_groups"]) - 1).items():
        has_screen = isinstance(screen, str) and bool(screen)
        sel = units[(units["app"] == app) if isinstance(app, str) else units["app"].isna()]
        sel = sel[(sel["screen_clean"] == screen) if has_screen else sel["screen_clean"].isna()]
        acts = list(sel.sort_values("events", ascending=False)["action_clean"].dropna().astype(str)[:10])
        # "Click Send" -> "Send"; typing and pasting into a screen is updating it
        acts = [re.sub(r"(?i)^(click|press|select)\s+(?=\w)", "", a) for a in acts]
        acts = [re.sub(r"(?i)^(paste|type|copy|<typed text>)\b.*", "Update", a) for a in acts]
        text = screen if has_screen else (app or "application")
        # transaction codes (AB12, F.13) name the screen for people but make poor words: dropped from the label,
        # like single letters left from "G/L"
        words = [w for w in re.split(r"[\s/]+", text) if w and not re.fullmatch(r"(?=.*\d)[A-Za-z0-9.]{2,6}", w)
                 and len(re.sub(r"[^A-Za-z]", "", w)) > 1
                 and w.lower().strip(":-") not in {"re", "fw", "fwd", "-"}]
        label = fallback_label(" ".join(words) or text, acts)
        if label in seen and isinstance(app, str) and app:
            label = f"{label} ({app})"
        seen[label] = True
        rule = {"app": app if isinstance(app, str) and app else "*",
                "screen": "^" + re.escape(screen) + "$" if has_screen else None,
                "action": None if has_screen else ".*"}
        groups.append({"label": label, "description": "", "rules": [rule]})
    return groups


def _confirmed(process: str | None) -> dict:
    if not process:
        return {}
    p = library.pack_dir(process) / "activities.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("steps", {}) if p.exists() else {}


def build(folder: Path, process: str | None = None, gw=None) -> dict:
    from app.nocase.clean import CLEAN_FILE
    from app.nocase.payloads import unit_lines
    con = connect()
    path = folder / CLEAN_FILE
    ev = con.execute(f"SELECT eid, step, unit_id, app, screen_clean, action_clean, actor, active_s "
                     f"FROM read_parquet({sql_literal(path.as_posix())})").df()
    confirmed = _confirmed(process)
    apps = set(ev["app"].dropna().astype(str))
    rows, per_event, proposed, notes = [], [], {}, {}
    for step, sev in ev[ev["step"].notna()].groupby("step", sort=True):
        units = sev.groupby("unit_id").agg(app=("app", "first"), screen_clean=("screen_clean", "first"),
                                           action_clean=("action_clean", "first"), events=("eid", "size"))
        groups, source, status, reason = None, None, None, None
        if confirmed.get(step):
            groups, source, status = confirmed[step], "library", "sme_confirmed"
        elif gw is not None and len(units) >= int(_cfg()["min_groups"]):
            top = units.sort_values(["events"], ascending=False).head(int(_cfg()["ai_units"]))
            msgs = [{"role": "system", "content": _cfg()["ai_prompt"]},
                    {"role": "user", "content": f"Step: {step}\nUnits (id | app | screen | click | share):\n"
                     + "\n".join(unit_lines(top.reset_index(), int(units['events'].sum())))}]
            a = gw.ask("nc_activities", msgs, lambda d, u=units: check_groups(d, u, apps))
            if a.ok:
                groups, source, status = a.data["groups"], f"ai:{a.model}", "proposed"
                proposed[step] = [{k: g.get(k) for k in ("label", "description", "rules")} for g in groups]
            else:
                reason = a.reason
        elif gw is None:
            reason = "AI not used for this run"
        else:
            reason = f"only {len(units)} unit(s): too few to group with the AI"
        if groups is None:
            groups, source, status = fallback_groups(units), "fallback: screens", "rule"
            notes[step] = f"screen-based groups ({reason})"
        label_of = apply_groups(units, groups)
        sev = sev.assign(activity=sev["unit_id"].map(label_of))
        # Level 5: the clicks inside a big activity
        sev["activity_l5"] = None
        for act, g in sev.groupby("activity"):
            if g["unit_id"].nunique() > int(_cfg()["level5_min_units"]):
                top = g["action_clean"].value_counts().head(int(_cfg()["max_groups"]) - 1).index
                sev.loc[g.index, "activity_l5"] = g["action_clean"].where(g["action_clean"].isin(top), OTHER)
        desc = {g["label"]: g.get("description") or "" for g in groups}
        for act, g in sev.groupby("activity"):
            rows.append({"step": step, "activity": act, "description": desc.get(act, ""), "events": int(len(g)),
                         "hours": float(g["active_s"].sum() / 3600) if g["active_s"].notna().any() else None,
                         "actors": int(g["actor"].nunique()), "units": int(g["unit_id"].nunique()),
                         "source": source if act != OTHER else "remainder", "status": status if act != OTHER
                         else "unassigned", "level5": int(g["activity_l5"].notna().any())})
        per_event.append(sev[["eid", "activity", "activity_l5"]])
    acts = pd.DataFrame(rows, columns=["step", "activity", "description", "events", "hours", "actors", "units",
                                       "source", "status", "level5"])
    acts.to_parquet(folder / ACT_FILE, index=False)
    from app.nocase.strategies.base import Context
    from app.nocase.strategies.episodes import attach
    pe = pd.concat(per_event) if per_event else pd.DataFrame({"eid": [], "activity": [], "activity_l5": []})
    attach(con, Context(folder), pe.astype({"activity": object, "activity_l5": object}))
    if proposed and process:
        (library.pack_dir(process) / "activities.proposed.yaml").write_text(
            yaml.safe_dump({"steps": proposed}, sort_keys=False, allow_unicode=True), encoding="utf-8")
    checks = totals_check(folder)
    out = {"steps": int(acts["step"].nunique()) if len(acts) else 0, "activities": int(len(acts)),
           "fallback_steps": notes, "checks": checks}
    (folder / CHECK_FILE).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out


def totals_check(folder: Path) -> dict:
    """Per step: activity events / hours sum to the step's, and the activities' actors are the step's actors."""
    from app.nocase.clean import CLEAN_FILE
    con = connect()
    ev = f"read_parquet({sql_literal((folder / CLEAN_FILE).as_posix())})"
    acts = f"read_parquet({sql_literal((folder / ACT_FILE).as_posix())})"
    bad = con.execute(f"""
        WITH s AS (SELECT step, COUNT(*) e, COALESCE(SUM(active_s), 0) / 3600 h, COUNT(DISTINCT actor) a,
                          COUNT(activity) withact FROM {ev} WHERE step IS NOT NULL GROUP BY 1),
             a AS (SELECT step, SUM(events) e, COALESCE(SUM(hours), 0) h FROM {acts} GROUP BY 1)
        SELECT s.step, s.e, a.e, s.h, a.h, s.withact FROM s LEFT JOIN a USING (step)
        WHERE a.e IS DISTINCT FROM s.e OR abs(s.h - a.h) > {float(_cfg()['hours_tolerance'])} OR s.withact <> s.e""").fetchall()
    return {"passed": not bad, "failed_steps": [r[0] for r in bad]}


def load(folder: Path) -> pd.DataFrame:
    p = folder / ACT_FILE
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()
