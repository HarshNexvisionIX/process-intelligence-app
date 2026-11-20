"""Step assignment in layers -> nc_assignments.parquet (per unit) + step on every event.

Two levels, so source labels are never overwritten:
    event level   an event with its own l3 label keeps it: status observed, source source_label
    unit level    only for events WITHOUT a label, decided per unit (app, screen, click) by the layers:

    SourceLabel   the majority label of the unit's labelled events; status inferred_from_unit when its share is
                  at least nocase.assign.inferred_min_share, else the unit is a conflict and goes on
    LibraryRules  the process pack's rules.yaml (only statuses in nocase.assign.rule_statuses; first match wins;
                  regexes checked for nested quantifiers and speed); status rule
    Unassigned    the rest; status unassigned

nc_assign_quality.json: events and hours per source, the conflicts, the rules that were skipped and why.
"""
from __future__ import annotations

import hashlib
import json
import re

import pandas as pd

from app.nocase import library
from app.nocase.strategies.base import Context, Strategy, cfg, order, register, save_decision

ASSIGN_FILE = "nc_assignments.parquet"
QUALITY_FILE = "nc_assign_quality.json"


def _acfg() -> dict:
    return cfg()["assign"]


class _Layer(Strategy):
    stage = "assign"

    def layer(self, units: pd.DataFrame, state: dict) -> None:
        """Fill rows of `units` that are still open (status NaN)."""
        raise NotImplementedError


@register
class SourceLabel(_Layer):
    name = "SourceLabel"

    def applies(self, caps, ctx):
        n = ((caps.get("fill") or {}).get("l3") or {}).get("rows") or 0
        return n > 0, ("the export has step labels (l3) on some events" if n else "no l3 labels in the export")

    def layer(self, units, state):
        lab = state["labels"]                         # unit_id, l3, n
        if lab.empty:
            return
        tot = lab.groupby("unit_id")["n"].sum()
        top = lab.sort_values(["unit_id", "n", "l3"], ascending=[True, False, True]).groupby("unit_id").head(1)
        top = top.set_index("unit_id")
        share = (top["n"] / tot).reindex(top.index)
        need = float(_acfg()["inferred_min_share"])
        for uid in top.index:
            if uid not in units.index or pd.notna(units.at[uid, "status"]):
                continue
            if share[uid] >= need:
                units.loc[uid, ["step", "source", "status", "confidence"]] = [
                    top.at[uid, "l3"], "inferred_from_unit", "inferred_from_unit", round(float(share[uid]), 4)]
            else:
                labels = lab[lab["unit_id"] == uid].sort_values(["n", "l3"], ascending=[False, True]).head(3)
                state["conflicts"].append({"unit_id": uid, "labelled_events": int(tot[uid]),
                                           "labels": [{"step": r.l3, "share": round(r.n / tot[uid], 4)}
                                                      for r in labels.itertuples()]})


@register
class LibraryRules(_Layer):
    name = "LibraryRules"

    def applies(self, caps, ctx):
        if not ctx.process:
            return False, "no process chosen (--process)"
        rules = library.load_rules(ctx.process)
        return (bool(rules), f"{len(rules)} rule(s) in the {ctx.process} library" if rules
                else f"the {ctx.process} library has no rules.yaml yet")

    def layer(self, units, state):
        rules = state["rules"]
        allowed = set(_acfg()["rule_statuses"])
        texts = sorted(set(units["screen_clean"].dropna().astype(str)) | set(units["action_clean"].dropna().astype(str)))
        bad = library.rule_problems(rules, texts[:int(_acfg()["regex_test_texts"])])
        state["rule_problems"] = bad
        usable = []
        for r in rules:
            rid = str(r.get("id"))
            if rid in bad:
                continue
            if r.get("status") not in allowed:
                state["rules_not_applied"].append({"rule_id": rid, "status": r.get("status"),
                                                   "reason": f"status {r.get('status')} is not applied"})
                continue
            m = r.get("match") or {}
            usable.append((rid, r, str(m.get("app") or "*").lower(),
                           re.compile(m["screen"], re.I) if m.get("screen") else None,
                           re.compile(m["action"], re.I) if m.get("action") else None))
        for uid, u in units[units["status"].isna()].iterrows():
            for rid, r, app, scr, act in usable:
                if app != "*" and str(u["app"] or "").lower() != app:
                    continue
                if scr and not scr.search(str(u["screen_clean"] or "")):
                    continue
                if act and not act.search(str(u["action_clean"] or "")):
                    continue
                units.loc[uid, ["step", "stage", "source", "status", "confidence", "rule_id"]] = [
                    r["step"], r.get("stage"), "rule", "rule", float(r.get("confidence", 1.0)), rid]
                break


@register
class AiProposedRules(_Layer):
    """The AI proposes regex rules for the units still without a step (task nc_assign_rules); code validates them:
    known steps only, safe regexes (no nested quantifiers, at most level4.max_regex_chars, fast), no rule may match
    a unit an earlier layer gave another step, and two new rules may not claim the same unit (sent back once).
    Applied rules only fill units without a step; status proposed, source ai:<model>. Generic units are left to
    the episode neighbours (see infer_generic)."""
    name = "AiProposedRules"

    def applies(self, caps, ctx):
        if not ctx.process:
            return False, "no process chosen (--process)"
        if ctx.ai is None:
            return False, "AI not used for this run"
        why = ctx.ai.why_not("nc_assign_rules")
        return (why is None, "the AI may propose rules for this dataset" if why is None else why)

    def layer(self, units, state):
        import yaml
        from app.core.level4 import regex_problems, static_regex_problem
        from app.nocase.payloads import step_lines, unit_lines
        pack, ctx = state["pack"], state["ctx"]
        steps = library.step_order(pack)
        # a generic click (Select Cell, Scroll) on a known screen follows its screen; only generic clicks without a
        # screen are left to the episode neighbours. (A file without a click column is all "generic" clicks.)
        classify_mode = bool(_acfg().get("ai_classify_prompt"))
        keep = ~units["is_generic"].astype(bool) | (units["screen_clean"].notna() if classify_mode else False)
        open_ = units[units["status"].isna() & keep].sort_values(
            ["events", "unit_id"], ascending=[False, True])
        if open_.empty:
            state["ai_note"] = "no unassigned units left"
            return
        total = int(units["events"].sum())
        neighbours = _neighbour_steps(state["events"], units)
        assigned = units[units["status"].notna() & units["step"].notna()]
        examples = []
        for st in steps:
            ex = assigned[assigned["step"] == st].head(int(_acfg()["ai_examples_per_step"]))
            if len(ex):
                examples.append(f"{st}: " + "; ".join(unit_lines(ex.reset_index(), total)))
        texts = sorted(set(units["screen_clean"].dropna().astype(str)) | set(units["action_clean"].dropna().astype(str)))
        texts = texts[:int(_acfg()["regex_test_texts"])]
        per = int(_acfg()["ai_units_per_call"])
        applied, rejected, cannot, n_named, n_other = [], [], [], 0, 0
        classify = bool(_acfg().get("ai_classify_prompt"))
        open_ = open_.head(int(_acfg()["ai_max_units"]))
        if classify:
            # one decision per application + screen (a screen serves one step; its clicks follow it); a unit
            # without a screen is its own group. Context: the clicks on it and the screens worked on alongside.
            open_ = open_.assign(_g=[f"{u.app}\x1f{u.screen_clean}" if u.screen_clean else f"{u.app}\x1f\x1f{uid}"
                                     for uid, u in open_.iterrows()])
            gorder = open_.groupby("_g", sort=False)["events"].sum().sort_values(ascending=False).index.tolist()
            gid = {g: f"S{i + 1:03d}" for i, g in enumerate(gorder)}
            open_ = open_.assign(_gid=open_["_g"].map(gid))
            with_ = _co_screens(state["events"], open_)
            batches = [gorder[i:i + per] for i in range(0, len(gorder), per)]
        else:
            batches = [open_.index[i:i + per].tolist() for i in range(0, len(open_), per)]
        for batch in batches:
            if classify:
                chunk = open_[open_["_g"].isin(batch)]
                lines = []
                for g in batch:
                    us = chunk[chunk["_g"] == g].sort_values("events", ascending=False)
                    u0 = us.iloc[0]
                    clicks = ", ".join(str(x) for x in us["action_clean"].dropna().head(4)) or "-"
                    lines.append(f"{gid[g]} | {u0['app'] or '-'} | {u0['screen_clean'] or '-'} | clicks: {clicks} | "
                                 f"{100 * us['events'].sum() / max(total, 1):.1f}% | with: "
                                 f"{'; '.join(with_.get(gid[g], [])) or '-'}")
                header = ("Screens without a step (id | application | screen | its clicks | share of events | with: "
                          "screens worked on in the same sittings):")
            else:
                chunk = open_.loc[batch]
                lines = [f"{ln} | near: {', '.join(neighbours.get(uid, [])) or '-'}"
                         for ln, uid in zip(unit_lines(chunk.reset_index(), total), chunk.index)]
                header = "Units without a step (id | app | screen | click | share | near: steps nearby):"
            msgs = [{"role": "system", "content": _acfg().get("ai_classify_prompt") or _acfg()["ai_prompt"]},
                    {"role": "user", "content": "Steps:\n" + "\n".join(f"- {x}" for x in step_lines(pack))
                     + "\n\nAlready assigned examples:\n" + "\n".join(examples or ["(none)"])
                     + "\n\n" + header + "\n" + "\n".join(lines)}]

            def check(data, chunk=chunk):
                """Structure only: known steps, safe regexes. Overlaps and conflicts are handled per rule.
                Two answer shapes: {"assign": [{"unit", "step"}]} (one step per unit, the default prompt) or
                {"rules": [...]} (regex rules)."""
                errs = []
                if "assign" in data:
                    items = data.get("assign")
                    if not isinstance(items, list):
                        return ['"assign" must be a list']
                    ids = set(map(str, chunk["_gid"])) if "_gid" in chunk else set(map(str, chunk.index))
                    for i, it in enumerate(items):
                        if not isinstance(it, dict) or str(it.get("unit")) not in ids:
                            errs.append(f"assign[{i}].unit must be one of the unit ids given")
                        elif it.get("step") not in steps and str(it.get("step")).upper() != OTHER_WORK:
                            errs.append(f"assign[{i}].step must be one of the steps given, or {OTHER_WORK}")
                    missing = sorted(ids - {str(it.get("unit")) for it in items if isinstance(it, dict)})
                    if missing:                  # an empty or partial answer is not "everything is other work"
                        errs.append(f"give a step (or {OTHER_WORK}) for EVERY unit id; missing {len(missing)}: "
                                    + ", ".join(missing[:30]))
                    return errs
                rules = data.get("rules")
                if not isinstance(rules, list):
                    return ['"rules" must be a list']
                for i, r in enumerate(rules):
                    if not isinstance(r, dict) or r.get("step") not in steps:
                        errs.append(f"rules[{i}].step must be one of the steps given")
                        continue
                    m = r.get("match") or {}
                    for key in ("screen", "action"):
                        if m.get(key):
                            why = static_regex_problem(str(m[key]))
                            if why:
                                errs.append(f"rules[{i}].match.{key} {why}")
                    if not (m.get("screen") or m.get("action")):
                        errs.append(f"rules[{i}] needs a screen or action regex")
                return errs

            def overlaps(rules, chunk=chunk) -> dict[int, str]:
                """rule index -> why, for a rule claiming a unit an earlier NEW rule gives to another step."""
                claims: dict[str, str] = {}
                out: dict[int, str] = {}
                for i, r in enumerate(rules):
                    for uid in _matches(r, chunk):
                        if uid in claims and claims[uid] != r["step"]:
                            out[i] = f"it matches {uid}, which another proposed rule gives to {claims[uid]!r}"
                        claims.setdefault(uid, r["step"])
                return out
            a = ctx.ai.ask("nc_assign_rules", msgs, check)
            if not a.ok:
                state["ai_note"] = a.reason
                continue
            if "assign" in a.data:      # one step per screen -> an exact rule (app + whole screen, any click)
                seen, as_rules = set(), []
                for it in a.data["assign"]:
                    key = str(it.get("unit"))
                    if key in seen:
                        continue
                    seen.add(key)
                    us = chunk[chunk["_gid"] == key] if "_gid" in chunk else chunk.loc[[key]]
                    if str(it.get("step")).upper() == OTHER_WORK:
                        n_other += 1
                        cannot.append({"unit_ids": list(map(str, us.index)), "reason": it.get("why") or "other work"})
                        continue
                    n_named += 1
                    u = us.iloc[0]
                    one = "_gid" not in chunk or not u["screen_clean"]       # no screen: the click decides
                    as_rules.append({"step": it["step"], "why": it.get("why") or "AI: screen linked to this step",
                                     "match": {"app": u["app"] or "*",
                                               "screen": "^" + re.escape(str(u["screen_clean"])) + "$"
                                               if u["screen_clean"] else None,
                                               "action": "^" + re.escape(str(u["action_clean"])) + "$"
                                               if one and u["action_clean"] else None}})
                state["ai_note"] = (f"AI ({a.model or a.provider}) linked {n_named} screen(s) to a step and marked "
                                    f"{n_other} as other work")
                a.data = {"rules": as_rules, "cannot_assign": []}
            over = overlaps(a.data["rules"])
            if over:                               # back to the AI once, with the overlaps
                again = ctx.ai.ask("nc_assign_rules", msgs + [
                    {"role": "assistant", "content": json.dumps(a.data)[:4000]},
                    {"role": "user", "content": "These rules overlap (one unit, two steps):\n- "
                     + "\n- ".join(f"rules[{i}]: {w}" for i, w in over.items()) + "\nReturn the corrected JSON."}],
                    check)
                if again.ok:
                    a = again
                    over = overlaps(a.data["rules"])
            pats = [str(m[k]) for r in a.data["rules"] for m in [r.get("match") or {}] for k in ("screen", "action")
                    if m.get(k)]
            slow = regex_problems(pats, texts) if pats else {}
            conf = state["ai_confidence"]
            sme_rejected = {json.dumps(r.get("match") or {}, sort_keys=True)
                            for r in library.load_rules(ctx.process) if r.get("status") == "rejected"}
            for i, r in enumerate(a.data["rules"]):
                m = r.get("match") or {}
                why = None
                if json.dumps(m, sort_keys=True) in sme_rejected:
                    why = "an SME rejected this rule before: it is never proposed again"
                elif i in over:
                    why = over[i] + " (it matches more than one step)"
                elif any(str(m.get(k)) in slow for k in ("screen", "action") if m.get(k)):
                    why = "a regex is too slow"
                else:
                    clash = [u for u in _matches(r, assigned) if units.at[u, "step"] != r["step"]]
                    if clash:
                        why = (f"it matches {len(clash)} unit(s) already assigned to another step by a source label "
                               "or an approved library rule")
                hit = [] if why else _matches(r, units[units["status"].isna() & ~units["is_generic"].astype(bool)])
                if not why and not hit:
                    why = "it matches no unit that is still without a step"
                if why:
                    rejected.append({"rule": i, "step": r.get("step"), "match": m, "reason": why})
                    continue
                rid = "AI-" + hashlib.sha1(json.dumps([r["step"], m], sort_keys=True).encode()).hexdigest()[:8]   # stable across runs
                for u in hit:
                    units.loc[u, ["step", "source", "status", "confidence", "rule_id"]] = [
                        r["step"], f"ai:{a.model}", "proposed", conf, rid]
                applied.append({"id": rid, "step": r["step"], "stage": library.step_stage(pack).get(r["step"]),
                                "match": {k: m[k] for k in ("app", "screen", "action") if m.get(k)},
                                "why": r.get("why"), "status": "proposed", "source": f"ai:{a.model}",
                                "units": len(hit), "version": 1})
            cannot += [c for c in a.data.get("cannot_assign") or [] if isinstance(c, dict)]
        state["ai_rules"], state["ai_rejected"], state["ai_cannot"] = applied, rejected, cannot
        if applied and ctx.process:
            path = library.pack_dir(ctx.process) / "rules.proposed.yaml"
            path.write_text(yaml.safe_dump({"rules": applied}, sort_keys=False, allow_unicode=True), encoding="utf-8")


OTHER_WORK = "OTHER"     # the AI's answer for a unit that is not part of the process (left without a step)


def _matches(rule: dict, units) -> list[str]:
    """Unit ids of `units` matched by a rule {match: {app, screen, action}}."""
    m = rule.get("match") or {}
    try:
        scr = re.compile(str(m["screen"]), re.I) if m.get("screen") else None
        act = re.compile(str(m["action"]), re.I) if m.get("action") else None
    except re.error:
        return []
    app = str(m.get("app") or "*").lower()
    out = []
    for uid, u in units.iterrows():
        if app != "*" and str(u["app"] or "").lower() != app:
            continue
        if scr and not scr.search(str(u["screen_clean"] or "")):
            continue
        if act and not act.search(str(u["action_clean"] or "")):
            continue
        out.append(uid)
    return out


def _co_screens(ev: pd.DataFrame, open_: pd.DataFrame, top: int = 3) -> dict[str, list[str]]:
    """group id -> the screens most often worked on in the same work sittings (context for the AI; cleaned
    screen names only)."""
    if "episode_id" not in ev.columns or "unit_id" not in ev.columns:
        return {}
    scr = ev[["episode_id", "unit_id", "screen_clean"]].dropna(subset=["screen_clean"])
    gid = open_["_gid"]
    d = scr[["episode_id", "unit_id"]].drop_duplicates()
    d = d[d["unit_id"].isin(gid.index)].assign(g=lambda x: x["unit_id"].map(gid))[["episode_id", "g"]].drop_duplicates()
    s2 = scr[["episode_id", "screen_clean"]].drop_duplicates()
    pairs = d.merge(s2, on="episode_id")
    own = open_.groupby("_gid")["screen_clean"].first()
    pairs = pairs[pairs["screen_clean"] != pairs["g"].map(own)]
    cnt = pairs.groupby(["g", "screen_clean"]).size().reset_index(name="n").sort_values(
        ["g", "n", "screen_clean"], ascending=[True, False, True])
    return {g: list(x["screen_clean"].head(top)) for g, x in cnt.groupby("g")}


def _neighbour_steps(ev: pd.DataFrame, units: pd.DataFrame) -> dict[str, list[str]]:
    """unit -> the two most common steps of the OTHER events in the episodes it occurs in."""
    if "episode_id" not in ev.columns:
        return {}
    step = ev["l3"].fillna(ev["unit_id"].map(units["step"]))
    d = ev.assign(step=step)
    known = d.dropna(subset=["step"])[["episode_id", "unit_id", "step"]]
    pairs = d[["episode_id", "unit_id"]].drop_duplicates().merge(known, on="episode_id", suffixes=("", "_n"))
    pairs = pairs[pairs["unit_id"] != pairs["unit_id_n"]]
    top = pairs.groupby(["unit_id", "step"]).size().reset_index(name="n").sort_values(
        ["unit_id", "n", "step"], ascending=[True, False, True])
    return {u: list(g["step"].head(2)) for u, g in top.groupby("unit_id")}


def infer_generic(ev: pd.DataFrame, generic: set[str]) -> pd.DataFrame:
    """Events of generic units still without a step take the step of their episode neighbours (the non-generic
    events with a step in the same episode) when at least nocase.assign.generic_neighbour_share of them agree;
    status inferred, source episode_neighbours."""
    if "episode_id" not in ev.columns or ev.empty:
        return ev
    need = float(_acfg()["generic_neighbour_share"])
    known = ev[ev["step"].notna() & ~ev["unit_id"].isin(generic)]
    if known.empty:
        return ev
    dist = known.groupby(["episode_id", "step"]).size().reset_index(name="n")
    tot = dist.groupby("episode_id")["n"].transform("sum")
    dist["share"] = dist["n"] / tot
    best = dist.sort_values(["episode_id", "share", "step"], ascending=[True, False, True]).groupby("episode_id").head(1)
    best = best[best["share"] >= need].set_index("episode_id")
    mask = ev["step"].isna() & ev["unit_id"].isin(generic) & ev["episode_id"].isin(best.index)
    ev.loc[mask, "step"] = ev.loc[mask, "episode_id"].map(best["step"])
    ev.loc[mask, "step_source"] = "episode_neighbours"
    ev.loc[mask, "step_status"] = "inferred"
    return ev


@register
class Unassigned(_Layer):
    name = "Unassigned"

    def applies(self, caps, ctx):
        return True, "always: whatever is left"

    def layer(self, units, state):
        open_ = units["status"].isna()
        units.loc[open_, ["source", "status", "confidence"]] = ["unassigned", "unassigned", 0.0]


def run_assign(con, ctx: Context) -> dict:
    """Run the layers in settings order (or the forced one plus Unassigned); write the outputs; return the decision."""
    from app.core.db import sql_literal
    from app.nocase.units import UNITS_FILE
    units = con.execute(f"""SELECT unit_id, app, screen_clean, action_clean, events, is_generic
                            FROM read_parquet({sql_literal(ctx.path(UNITS_FILE).as_posix())}) ORDER BY unit_id"""
                        ).df().set_index("unit_id")
    for c in ("step", "stage", "source", "status", "confidence", "rule_id"):
        units[c] = pd.Series([None] * len(units), index=units.index, dtype=object)
    labels = con.execute(f"""SELECT unit_id, l3, COUNT(*) AS n FROM {ctx.events_sql()}
                             WHERE l3 IS NOT NULL GROUP BY 1, 2""").df()
    pack = library.load_pack(ctx.process) if ctx.process else None
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {ctx.events_sql()}").fetchall()]
    events = con.execute(f"SELECT eid, unit_id, l3, screen_clean{', episode_id' if 'episode_id' in cols else ''} "
                         f"FROM {ctx.events_sql()}").df()
    from app.nocase.ai.evidence import load_scores
    sc = load_scores(ctx.folder)
    conf = max([sc.get(k, {}).get("score") or 0.0 for k in ("screen", "action")] + [0.0])
    state = {"labels": labels, "conflicts": [], "rules": library.load_rules(ctx.process) if ctx.process else [],
             "rule_problems": {}, "rules_not_applied": [], "pack": pack, "ctx": ctx, "events": events,
             "ai_confidence": round(float(conf), 4) if conf else float(_acfg()["ai_default_confidence"]),
             "ai_rules": [], "ai_rejected": [], "ai_cannot": [], "ai_note": None}
    stage_of = library.step_stage(pack)
    names = order("assign")
    if "assign" in ctx.forced:
        names = [ctx.forced["assign"], "Unassigned"]
    from app.nocase.strategies.base import REGISTRY
    layers = []
    for name in names:
        s = REGISTRY["assign"][name]()
        ok, why = s.applies(ctx.caps, ctx)
        layers.append({"strategy": name, "applies": bool(ok), "reason": why})
        if ok:
            s.layer(units, state)
    # stage per unit: the pack's stage of the step, else the most common l2 of the unit's labelled events
    l2 = con.execute(f"""SELECT unit_id, l3, l2 FROM (SELECT unit_id, l3, l2, row_number() OVER (
                             PARTITION BY unit_id, l3 ORDER BY COUNT(*) DESC, l2) r
                         FROM {ctx.events_sql()} WHERE l3 IS NOT NULL AND l2 IS NOT NULL GROUP BY 1, 2, 3) WHERE r = 1""").df()
    l2_of = {(r.unit_id, r.l3): r.l2 for r in l2.itertuples()}
    for uid, u in units.iterrows():
        if u["step"] is not None and u["stage"] is None:
            units.at[uid, "stage"] = stage_of.get(u["step"]) or l2_of.get((uid, u["step"]))
    out = units.reset_index()[["unit_id", "step", "stage", "source", "status", "confidence", "rule_id", "events",
                               "is_generic"]]
    out["confidence"] = out["confidence"].astype(float)
    out.to_parquet(ctx.path(ASSIGN_FILE), index=False)
    # events: own label first (observed), else the unit's assignment
    con.register("__nc_assign", out)
    con.register("__nc_stage", pd.DataFrame({"step": list(stage_of), "pstage": list(stage_of.values())}).astype(object))
    ev = con.execute(f"""
        SELECT e.eid,
               COALESCE(e.l3, a.step) AS step,
               CASE WHEN e.l3 IS NOT NULL THEN COALESCE(ps.pstage, e.l2) ELSE a.stage END AS stage,
               CASE WHEN e.l3 IS NOT NULL THEN 'source_label' ELSE a.source END AS step_source,
               CASE WHEN e.l3 IS NOT NULL THEN 'observed' ELSE a.status END AS step_status
               {', e.episode_id' if 'episode_id' in cols else ''}, e.unit_id
        FROM {ctx.events_sql()} e JOIN __nc_assign a USING (unit_id)
        LEFT JOIN __nc_stage ps ON ps.step = e.l3""").df()
    generic = set(out.loc[out["is_generic"].astype(bool), "unit_id"])
    ev.loc[ev["step_status"] == "unassigned", "step"] = None
    if any(x["strategy"] == "AiProposedRules" and x["applies"] for x in layers):   # part of the AI layer (N3 4)
        ev = infer_generic(ev, generic)
    ev.loc[ev["step"].isna(), ["step_source", "step_status"]] = ["unassigned", "unassigned"]
    inferred = ev["step_status"] == "inferred"
    ev.loc[inferred, "stage"] = ev.loc[inferred, "step"].map(stage_of)
    ev = ev[["eid", "step", "stage", "step_source", "step_status"]]
    from app.nocase.strategies.episodes import attach
    attach(con, ctx, ev)
    quality = assign_quality(ctx, state, layers)
    if state["ai_rules"] or state["ai_rejected"] or state["ai_note"]:
        quality["ai"] = {"rules": state["ai_rules"], "rejected": state["ai_rejected"],
                         "cannot_assign": state["ai_cannot"][:100], "note": state["ai_note"]}
        (ctx.path(QUALITY_FILE)).write_text(json.dumps(quality, indent=2, default=str), encoding="utf-8")
    decision = {"stage": "assign", "layers": layers, "process": ctx.process, "ai_note": state["ai_note"],
                "message": assign_message(quality), **{k: quality[k] for k in ("by_source", "conflicts_count")}}
    save_decision(ctx.folder, "assign", decision)
    return decision


def assign_quality(ctx: Context, state: dict, layers: list[dict]) -> dict:
    from app.core.db import connect
    con = connect()
    rows = con.execute(f"""SELECT step_source, COUNT(*), SUM(active_s) / 3600 FROM {ctx.events_sql()} GROUP BY 1
                           ORDER BY 2 DESC, 1""").fetchall()
    n = sum(r[1] for r in rows) or 1
    h = sum(r[2] or 0 for r in rows)
    by = {s: {"events": int(e), "share": round(e / n, 4), "hours": None if hrs is None else round(float(hrs), 4),
              "hours_share": None if not h or hrs is None else round(float(hrs) / h, 4)} for s, e, hrs in rows}
    order_ = library.step_order(library.load_pack(ctx.process)) if ctx.process else []
    steps = con.execute(f"""SELECT step_source, step, COUNT(*), SUM(active_s) / 3600 FROM {ctx.events_sql()}
                            WHERE step IS NOT NULL GROUP BY 1, 2""").fetchall()
    rank = {s: i for i, s in enumerate(order_)}
    by_step = [{"source": src, "step": st, "events": int(e), "hours": None if hrs is None else round(float(hrs), 4)}
               for src, st, e, hrs in sorted(steps, key=lambda r: (r[0], rank.get(r[1], len(rank)), -(r[3] or 0), r[1]))]
    q = {"by_source": by, "hours_by_step": by_step, "conflicts": state["conflicts"][:200], "conflicts_count": len(state["conflicts"]),
         "rules_skipped": [{"rule_id": k, "reason": v} for k, v in state["rule_problems"].items()]
         + state["rules_not_applied"], "layers": layers}
    (ctx.path(QUALITY_FILE)).write_text(json.dumps(q, indent=2, default=str), encoding="utf-8")
    return q


def assign_message(q: dict) -> str:
    parts = [f"{s} {100 * v['share']:.1f}%" for s, v in q["by_source"].items()]
    return (f"Step assignment: {', '.join(parts)} of events; {q['conflicts_count']} unit(s) with conflicting labels"
            + (f"; {len(q['rules_skipped'])} rule(s) not applied" if q["rules_skipped"] else "") + ".")
