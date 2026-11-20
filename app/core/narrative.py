"""Day 5: the AI-written summary, with guardrails so no wrong number can reach the report.

How it stays honest
-------------------
1. The AI receives ONLY facts (ID, label, formatted value, badge, support), never
   rows, case IDs, resource values or attribute values (`select_facts`).
2. The AI never writes numbers: it writes placeholders such as {{F012}} and the
   code fills in the real values from the fact store (`summary.render`).
3. Every answer is checked by code (`validate`). A broken answer is sent back
   with the list of broken rules; after `ai.max_retries` retries the fixed-template
   summary from Part 4 is used instead.
4. Band rules: High -> High and Medium facts; Medium -> High facts only;
   Low / Not feasible -> no AI call at all.
5. Langfuse tracing is optional (observability.py) and never breaks a report.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.core import jobs, observability, privacy
from app.core.config import settings
from app.core.facts import SEQUENCE_TYPES, Fact, format_fact
from app.core.ingest import EMAIL_RE
from app.core.summary import PLACEHOLDER, allowed_badges, render, template_recommendations, template_summary

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
KEYS = ("summary", "findings", "recommendations")
# Facts sent first when the list is capped (the data facts give the AI the size of the log).
SECTION_PRIORITY = ("data", "overview", "bottlenecks", "variants", "map", "rework", "resources", "trends")
# Data facts the AI may use; the others (band text, rows left out) are internals, not findings.
DATA_TYPES_FOR_AI = {"cases", "events", "activities", "feasibility_score"}
# Facts whose labels hold resource values (people or systems) are never sent.
NEVER_SENT_TYPES = {"handover"}
CHECKS = ("schema_valid", "citations_valid", "numbers_grounded", "causal_language_ok", "hedging_ok",
          "sequence_language_ok", "step_language_ok", "wording_ok")
# Facts about the work inside one step (subprocess.py); meta holds confidence and standard_confirmed.
STEP_TYPES = {"step_runs", "step_median_actions", "step_median_duration", "step_order_strength",
              "step_standard_share", "step_missing_share", "step_standard_source"}
JUDGING_WORDS = ("incorrect", "incorrectly", "wrong", "wrongly")
BLAME_WORDS = ("error", "errors", "mistake", "mistakes", "fault", "faults", "wrong", "incorrect", "failure",
               "failed", "non-compliant", "violation", "violations")


@dataclass
class NarrativeResult:
    summary: list[dict]           # [{"text": rendered text, "facts": [ids]}]
    findings: list[dict]
    recommendations: list[dict]
    source: str                   # "ai" | "template"
    attempts: int = 0
    errors: list[str] = field(default_factory=list)
    trace_id: str | None = None
    prompt_version: str | None = None
    reason: str | None = None     # why the AI was not used (template only)
    facts_sent: int = 0
    scores: dict = field(default_factory=dict)
    provider: str | None = None   # which AI provider answered (ai_client.py); never a key
    model: str | None = None
    feedback: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 1. What the AI may see
# --------------------------------------------------------------------------- #
SHORT_DURATION_TYPES = {"step_median_duration"}


def select_facts(facts: list[Fact], band: str, max_facts: int) -> list[Fact]:
    """The facts the AI may use: allowed badges only, no internals, no resource values."""
    ok = allowed_badges(band)
    # The optional PM4Py results (section "pm4py") are shown in the appendix only: never in the summary.
    chosen = [f for f in facts if f.badge in ok and f.type not in NEVER_SENT_TYPES and f.section != "pm4py"
              and (f.section != "data" or f.type in DATA_TYPES_FOR_AI)
              # an elapsed run duration under 5 s is a single click, not a duration ("runs last 0 s"): not sent
              and not (f.type in SHORT_DURATION_TYPES and f.value is not None and f.value * 3600 < 5)]
    chosen.sort(key=lambda f: (SECTION_PRIORITY.index(f.section) if f.section in SECTION_PRIORITY
                               else len(SECTION_PRIORITY), -(f.support or 0), f.id))
    return chosen[:max_facts]


def facts_table(facts: list[Fact]) -> str:
    """One line per fact: "F012 | label | formatted value | badge | support"."""
    return "\n".join(f"{f.id} | {f.label} | {format_fact(f)} | {f.badge} | {f.support or 0:,} cases"
                     for f in facts)


def names_in_facts(facts: list[Fact]) -> list[str]:
    """Every activity name a fact refers to (steps, transitions, paths), in order, once each."""
    names: list[str] = []
    for f in facts:
        found = [f.meta.get(k) for k in ("activity", "src", "dst", "a", "b")] + list(f.meta.get("path") or []) + list(f.meta.get("labels") or [])
        names += [a for a in found if a and a not in names]
    return names


def activity_names(job_id: str, facts: list[Fact]) -> list[str]:
    """All activity names of the job, most frequent first (mining.json, else the facts)."""
    try:
        with open(jobs.job_folder(job_id) / "mining.json", encoding="utf-8") as fh:
            g = json.load(fh)["map"]["data"]
        special = {g["start_node"], g["end_node"]}
        names = [x["activity"] for x in g["nodes"] if x["activity"] not in special]
    except Exception:  # noqa: BLE001 - no map yet: use the steps named in the facts
        names = []
    return names + [a for a in names_in_facts(facts) if a not in names]


def order_info(job_id: str) -> dict:
    """What the job's simple map (reference.json) says about the order, or "not known".

    Keys: strength (order strength label), confirmed ("yes"/"no"), flow_band
    (High/Medium/Low/None, or None when not known) and order_source.
    """
    try:
        with open(jobs.job_folder(job_id) / "reference.json", encoding="utf-8") as fh:
            ref = json.load(fh)
        flow = ref.get("flow") or {}
        return {"strength": ref["stats"]["order_strength_label"],
                "confirmed": "yes" if ref["order"]["confirmed_by_business"] else "no",
                "flow_band": flow.get("band"), "order_source": flow.get("source", "not known"),
                "order_claim_allowed": order_claim_allowed(ref["order"]["confirmed_by_business"],
                                                           ref["stats"].get("order_strength"))}
    except Exception:  # noqa: BLE001 - no simple map for this job
        return {"strength": "not known", "confirmed": "not known", "flow_band": None, "order_source": "not known",
                "order_claim_allowed": False}


def process_hint(job_id: str, facts: list[Fact], n: int = 5) -> str:
    """The most frequent activity names, as one line for the prompt."""
    names = activity_names(job_id, facts)[:n]
    return ", ".join(names) if names else "not known"


# Payload-safe mask for everything sent to the AI. Unlike the Langfuse mask it does
# NOT touch person-shaped words, which would damage step names like "Block Invoice".
_EMAIL_IN_TEXT = re.compile(EMAIL_RE.pattern.strip("^$"))


def payload_mask(text: str) -> str:
    """Replace email addresses and runs of `ai.payload_mask_min_digits`+ digits with "[id]"."""
    digits = int(settings()["ai"]["payload_mask_min_digits"])
    return re.sub(r"\d{%d,}" % digits, "[id]", _EMAIL_IN_TEXT.sub("[id]", text))


def has_identifier(name: str) -> bool:
    """True when the name holds an email address or a ticket/ID number."""
    return payload_mask(name) != name


# --------------------------------------------------------------------------- #
# 2. Checking an answer
# --------------------------------------------------------------------------- #
def _words_pattern(phrases: list[str]) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(re.escape(p) for p in phrases) + r")\b", re.IGNORECASE)


ORDER_CLAIM = re.compile(r"\b(fixed order|fixed sequence|sequenced|in sequence|in a set order|final step|last step|"
                         r"first step|drop[- ]?off|drops? off)\b", re.I)


def order_claim_allowed(confirmed: bool, order_strength: float | None) -> bool:
    """Text may say the steps have an order ("final step", "fixed order", "drop-off") only when the order is
    confirmed by the business, or the data shows a strong order (order strength >= 0.6)."""
    return bool(confirmed) or (order_strength is not None and float(order_strength) >= 0.6)


def check(output, allowed_ids: set, facts_by_id: dict, flow_band: str | None = None,
          avoid_names: list[str] | None = None, order_claims: bool = True) -> dict[str, list[str]]:
    """Run every rule; returns {check name: [error messages]} (all empty = valid).

    With `flow_band` Low or None (no reliable order), a sentence that cites a sequence
    fact (paths, waits between steps, in-order share) may not use sequence words
    ("then", "followed by", "after", "before", "next").
    """
    cfg = settings()["ai"]
    errors: dict[str, list[str]] = {k: [] for k in CHECKS}
    schema = errors["schema_valid"]
    # Rule 1 and 6: the JSON shape, limits and no empty text.
    if not isinstance(output, dict):
        schema.append("The answer must be one JSON object with keys summary, findings, recommendations.")
        return errors
    if set(output) != set(KEYS):
        schema.append(f"The JSON object must have exactly the keys {', '.join(KEYS)} "
                      f"(found: {', '.join(map(str, output)) or 'none'}).")
    items = []
    for key in KEYS:
        value = output.get(key, [])
        if not isinstance(value, list):
            schema.append(f"'{key}' must be a list.")
            continue
        for i, item in enumerate(value):
            where = f"{key}[{i}]"
            if not isinstance(item, dict) or not isinstance(item.get("text"), str) \
                    or not isinstance(item.get("facts"), list):
                schema.append(f"{where} must be an object with 'text' (text) and 'facts' (a list of IDs).")
                continue
            if not item["text"].strip():
                schema.append(f"{where} has an empty text.")
            if not item["facts"]:
                schema.append(f"{where} must list the IDs of the facts it uses in 'facts'.")
            items.append((where, item))
    if isinstance(output.get("summary"), list) and not output["summary"]:
        schema.append("'summary' must contain at least one sentence.")
    for key, limit in (("findings", cfg["max_findings"]), ("recommendations", cfg["max_recommendations"])):
        if isinstance(output.get(key), list) and len(output[key]) > limit:
            schema.append(f"'{key}' has {len(output[key])} items; at most {limit} are allowed.")

    causal = _words_pattern(cfg["banned_causal_phrases"])
    sequence = _words_pattern(settings()["flow_confidence"]["sequence_words"])
    hedges = _words_pattern(cfg["exploratory_hedges"])
    for where, item in items:
        text = item["text"]
        listed = [str(x) for x in item["facts"]]
        used = PLACEHOLDER.findall(text)
        # Rule 2: citations point to allowed facts, and every placeholder is cited.
        for fid in sorted(set(listed) | set(used)):
            if fid not in allowed_ids:
                errors["citations_valid"].append(f"{where} uses {fid}, which is not in the fact table.")
        for fid in sorted(set(used) - set(listed)):
            errors["citations_valid"].append(f"{where} uses {{{{{fid}}}}} but does not list {fid} in 'facts'.")
        # Rule 3: no typed numbers once the placeholders are removed.
        bare = PLACEHOLDER.sub("", text)
        digit = re.search(r".{0,20}\d.{0,20}", bare)
        if digit:
            errors["numbers_grounded"].append(
                f"{where} contains a typed digit ('{digit.group(0).strip()}'). "
                "Use only {{Fnnn}} placeholders for numbers.")
        # Rule 4: no causal wording.
        for m in causal.finditer(text):
            errors["causal_language_ok"].append(
                f"{where} uses the causal phrase '{m.group(0)}'. Say 'is associated with' instead.")
        # Rule 5: Exploratory facts need hedged sentences.
        exploratory = {fid for fid in set(listed) | set(used)
                       if getattr(facts_by_id.get(fid), "badge", None) == "Exploratory"}
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            in_sentence = set(PLACEHOLDER.findall(sentence)) & exploratory
            if in_sentence and not hedges.search(sentence):
                errors["hedging_ok"].append(
                    f"{where} states the Exploratory fact {', '.join(sorted(in_sentence))} without a hedge "
                    f"word ({', '.join(cfg['exploratory_hedges'])}).")
        cited_only = exploratory - set(used)
        if cited_only and not hedges.search(text):
            errors["hedging_ok"].append(
                f"{where} relies on the Exploratory fact {', '.join(sorted(cited_only))} without a hedge word.")
        # Rule 7: no sequence language about sequence facts when there is no reliable order.
        if flow_band in ("Low", "None"):
            seq = {fid for fid in set(listed) | set(used)
                   if getattr(facts_by_id.get(fid), "type", None) in SEQUENCE_TYPES}
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                cited = set(PLACEHOLDER.findall(sentence)) & seq or (seq if not used else set())
                word = sequence.search(sentence)
                chain_ids = {fid for fid in PLACEHOLDER.findall(sentence)
                             if getattr(facts_by_id.get(fid), "type", None) == "milestone_chain"}
                valid_chain = False
                for fid in chain_ids:
                    labels = facts_by_id[fid].meta.get("labels", [])
                    valid_chain |= any(a.lower() in sentence.lower() and b.lower() in sentence.lower()
                                       and sentence.lower().find(a.lower()) < sentence.lower().find(b.lower())
                                       for a, b in zip(labels, labels[1:]))
                # The standard way INSIDE a step may be told as a sequence when that step's
                # confidence is High or Medium (its own facts are cited).
                valid_step = any(getattr(facts_by_id.get(fid), "type", None) in STEP_TYPES
                                 and facts_by_id[fid].meta.get("confidence") in ("High", "Medium")
                                 for fid in PLACEHOLDER.findall(sentence))
                if word and (cited or not (valid_chain or valid_step)):
                    errors["sequence_language_ok"].append(
                        f"{where} uses the sequence word '{word.group(0)}' with {', '.join(sorted(cited))}, but the "
                        f"flow confidence is {flow_band}: the data shows no reliable order. Describe the steps as "
                        "worked together, not as a sequence.")
        # Rule 8: steps are compared with a standard way. Unless that standard is confirmed by the
        # business, differences are "variation", never "incorrect"; individual users are never named.
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            judging = _words_pattern(list(JUDGING_WORDS)).search(sentence)
            if judging:
                steps = [facts_by_id.get(fid) for fid in PLACEHOLDER.findall(sentence)]
                steps = [f for f in steps if getattr(f, "type", None) in STEP_TYPES]
                if not steps or not all(f.meta.get("standard_confirmed") for f in steps):
                    errors["step_language_ok"].append(
                        f"{where} uses '{judging.group(0)}', but the standard way is not confirmed by the business. "
                        "Describe differences as variation from the most common way.")
        # Rule 9: a gap between the documented and the observed process is a finding, not an error.
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            gaps = [fid for fid in PLACEHOLDER.findall(sentence)
                    if getattr(facts_by_id.get(fid), "type", None) == "combined_status"
                    and facts_by_id[fid].meta.get("status") == "Documented ≠ actual"]
            blame = _words_pattern(list(BLAME_WORDS)).search(sentence)
            if gaps and blame:
                errors["step_language_ok"].append(
                    f"{where} calls the gap {', '.join(gaps)} '{blame.group(0)}'. Describe it as a gap between the "
                    "documented and the observed process, never as an error by people.")
        # Rule 10: plain wording. No filler that hides the measure ("as indicated by {{F001}}"), no "< 1 min",
        # and every numeric placeholder sits in a sentence that says what it measures (cases, hours, share ...).
        filler = _words_pattern(cfg.get("banned_filler_phrases") or [])
        for m in filler.finditer(text):
            errors["wording_ok"].append(f"{where} uses '{m.group(0)}'. Say what the number measures instead.")
        if re.search(r"<\s*1\s*min", text, re.IGNORECASE):
            errors["wording_ok"].append(f"{where} uses '< 1 min'. Use the value from the fact table.")
        measure = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in cfg.get("measure_words") or []) + r")\w*",
                             re.IGNORECASE)
        for sentence in re.split(r"(?<=[.!?;:])\s+", text):
            numeric = [fid for fid in PLACEHOLDER.findall(sentence)
                       if getattr(facts_by_id.get(fid), "value", None) is not None
                       and getattr(facts_by_id.get(fid), "text", None) is None]
            if numeric and cfg.get("measure_words") and not measure.search(PLACEHOLDER.sub(" ", sentence)):
                errors["wording_ok"].append(
                    f"{where}: '{sentence.strip()[:60]}' gives {', '.join(numeric)} without saying what it measures "
                    "(cases, hours, share, runs ...).")
        if not order_claims and ORDER_CLAIM.search(text):
            errors["sequence_language_ok"].append(
                f"{where} says '{ORDER_CLAIM.search(text).group(0)}', but the order of the steps is not confirmed and the "
                "data shows no strong order: do not speak of a fixed order, a first or final step, or drop-off.")
        for name in avoid_names or []:
            if len(str(name)) > 2 and re.search(r"\b" + re.escape(str(name)) + r"\b", text, re.IGNORECASE):
                errors["step_language_ok"].append(f"{where} names an individual user. Never mention individual users.")
    return errors


def validate(output, allowed_ids: set, facts_by_id: dict, flow_band: str | None = None,
             avoid_names: list[str] | None = None, order_claims: bool = True) -> list[str]:
    """All broken rules as plain messages; an empty list means the answer is valid."""
    return [msg for msgs in check(output, allowed_ids, facts_by_id, flow_band, avoid_names, order_claims).values()
            for msg in msgs]


def user_names(job_id: str) -> list[str]:
    """Resource values of the job (people or systems): the AI may never write them."""
    try:
        with open(jobs.job_folder(job_id) / "mining.json", encoding="utf-8") as fh:
            m = json.load(fh)
        return [r["resource"] for r in ((m.get("resources") or {}).get("data") or {}).get("resources", [])
                if r.get("resource")]
    except Exception:  # noqa: BLE001 - no resource list: nothing to avoid
        return []


def parse_reply(text: str):
    """The JSON object in the reply: the whole reply, or else the first {...} in it."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    start = (text or "").find("{")
    if start < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 3. Writing the summary
# --------------------------------------------------------------------------- #
def ai_status() -> tuple[bool, str | None]:
    """Whether the AI may be called, and if not, why (shown in the report)."""
    if os.getenv("AI_ENABLED", "false").strip().lower() not in ("1", "true", "yes", "on"):
        return False, "switched off (AI_ENABLED is not true)"
    from app.core import ai_client
    if not ai_client.configured():
        return False, ("no AI provider configured (put a key in .env and choose a model on the AI settings page, "
                       "or set LLM_MODEL)")
    return True, None


def local_prompt() -> list[dict]:
    return [{"role": "system", "content": (PROMPTS / "report_writer_system.txt").read_text(encoding="utf-8")},
            {"role": "user", "content": (PROMPTS / "report_writer_user.txt").read_text(encoding="utf-8")}]


def _fill(template: str, values: dict) -> str:
    """Fill {band}, {facts_table}... by plain replacement (the JSON example keeps its braces)."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return template


def _rendered(items: list[dict], facts: list[Fact]) -> list[dict]:
    return [{"text": render(x["text"], facts), "facts": [str(i) for i in x["facts"]]} for x in items]


def _template(facts: list[Fact], band: str, reason: str, **extra) -> NarrativeResult:
    return NarrativeResult(summary=_rendered(template_summary(facts, band), facts), findings=[],
                           recommendations=_rendered(template_recommendations(facts, band), facts),
                           source="template", reason=reason, **extra)


def _call(client, model: str, messages: list[dict], temperature: float, state: dict) -> str:
    """One model call. JSON mode is asked for; endpoints that refuse it are called without.

    Every message is masked here (payload_mask), the single point every request
    passes, so no email address or ID number can reach the AI by any route.
    """
    from app.core.ai_client import Session
    safe = [{"role": m["role"], "content": payload_mask(m["content"])} for m in messages]
    if isinstance(client, Session):          # routed providers (ai_client.py): limits, fallback, cleaning
        return client.call(safe, json_expected=True)
    kwargs = {"model": model, "messages": safe, "temperature": temperature}
    if state.get("json_mode", True):
        try:
            reply = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
            return reply.choices[0].message.content or ""
        except Exception as err:  # noqa: BLE001
            if "response_format" not in str(err) and "json" not in str(err).lower():
                raise
            state["json_mode"] = False
    reply = client.chat.completions.create(**kwargs)
    return reply.choices[0].message.content or ""


def write_summary(job_id: str, facts: list[Fact], band: str, client=None, use_ai: bool = True,
                  prompt_version: int | None = None) -> NarrativeResult:
    """The executive summary, findings and recommendations for a report.

    Uses the AI when allowed (band High/Medium, AI switched on and configured, or
    a `client` passed in by tests); otherwise, or when the AI's answers keep
    failing the checks, the fixed-template summary. Items come back rendered:
    placeholders replaced by the real values, badges shown for Medium/Exploratory.
    """
    cfg = settings()["ai"]
    if not use_ai:
        return _template(facts, band, "switched off for this run")
    if band not in ("High", "Medium"):
        return _template(facts, band, f"data quality is {band}")
    if client is None:
        ok, why = ai_status()
        if not ok:
            return _template(facts, band, why)
    # Nothing is sent outside the user's network unless ALLOW_EXTERNAL_AI=true (privacy.py).
    ok, why = privacy.ai_allowed()
    if not ok:
        return _template(facts, band, why)
    selected = select_facts(facts, band, cfg["max_facts"])
    if not selected:
        return _template(facts, band, "no fact is reliable enough to send")
    # Activity names that are mostly IDs or contact details would turn the summary
    # into "[id]" soup (and are a privacy risk): use the templates instead.
    all_names = activity_names(job_id, facts)
    sent_names = list(dict.fromkeys(all_names[:5] + names_in_facts(selected)))
    if sent_names and sum(map(has_identifier, sent_names)) / len(sent_names) > cfg["max_masked_activity_share"]:
        return _template(facts, band, "activity names contain IDs or contact details")
    allowed = {f.id for f in selected}
    by_id = {f.id: f for f in facts}
    model = os.getenv("LLM_MODEL") or "unknown-model"

    with observability.trace(job_id, tags=["report", band],
                             metadata={"band": band, "facts_sent": len(selected), "model": model},
                             allow_names=set(all_names)) as trace_id:
        prompt, version = observability.get_prompt(cfg["prompt_name"], cfg["prompt_label"], local_prompt(),
                                                   version=prompt_version)
        info = order_info(job_id)
        values = {"band": band, "process_hint": process_hint(job_id, facts), "facts_table": facts_table(selected),
                  "order_strength_label": info["strength"], "order_confirmed": info["confirmed"],
                  "flow_band": info["flow_band"] or "not known", "order_source": info["order_source"],
                  "max_findings": cfg["max_findings"], "max_recommendations": cfg["max_recommendations"]}
        messages = [{"role": m["role"], "content": _fill(m["content"], values)} for m in prompt]
        if client is None:
            from app.core import ai_client
            client = ai_client.session("summary")

        from app.core.ai_client import AIUnavailable
        unavailable = None
        avoid = user_names(job_id)
        state: dict = {}
        accepted, found, all_errors, attempts = None, {k: ["not checked"] for k in CHECKS}, [], 0
        claims = info.get("order_claim_allowed", True)       # "final step", "fixed order" ... only when allowed
        for attempts in range(1, cfg["max_retries"] + 2):
            try:
                reply = _call(client, model, messages, cfg["temperature"], state)
            except AIUnavailable as err:        # every provider failed or was skipped: templates
                unavailable = err
                all_errors.append(f"attempt {attempts}: AI not available: {err.short()}")
                found = {k: ["no answer"] for k in CHECKS}
                break
            except Exception as err:  # noqa: BLE001 - network, timeout, auth: count as a failed attempt
                all_errors.append(f"attempt {attempts}: the AI call failed ({type(err).__name__}: {err})")
                found = {k: ["no answer"] for k in CHECKS}
                continue
            output = parse_reply(reply)
            found = check(output, allowed, by_id, info["flow_band"], avoid, claims)
            problems = [m for msgs in found.values() for m in msgs]
            if not problems:
                accepted = output
                break
            all_errors += [f"attempt {attempts}: {p}" for p in problems]
            messages = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": "Your previous answer broke these rules:\n- " + "\n- ".join(problems)
                 + "\nReturn the corrected JSON object only."}]

        scores = {k: 0 if found[k] else 1 for k in CHECKS}
        scores.update(fallback_used=0 if accepted else 1, attempts=attempts)
        for name, value in scores.items():
            observability.score(name, value)
    # No flush here: Langfuse sends in the background, so a slow or unreachable
    # Langfuse server never delays a report. Scripts flush once at the end.

    if hasattr(client, "notes"):          # routed providers: show that a fallback or retry happened
        all_errors += client.notes()
    extra = {"attempts": attempts, "errors": all_errors, "trace_id": trace_id, "prompt_version": version,
             "facts_sent": len(selected), "scores": scores,
             "provider": getattr(client, "provider", None), "model": getattr(client, "model", None)}
    if unavailable is not None:
        return _template(facts, band, f"AI not available: {unavailable.short()}", **extra)
    if accepted is None:
        return _template(facts, band, f"the AI answer failed the checks {attempts} times", **extra)
    return NarrativeResult(summary=_rendered(accepted["summary"], facts),
                           findings=_rendered(accepted["findings"], facts),
                           recommendations=_rendered(accepted["recommendations"], facts),
                           source="ai", **extra)


# --------------------------------------------------------------------------- #
# 4. Saving, loading, feedback
# --------------------------------------------------------------------------- #
def save_narrative(result: NarrativeResult, folder: Path) -> Path:
    path = folder / "narrative.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, indent=2, default=str)
    return path


def load_narrative(folder: Path) -> NarrativeResult | None:
    path = folder / "narrative.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return NarrativeResult(**json.load(fh))


# --------------------------------------------------------------------------- #
# 5. AI-suggested order of steps (names only)
# --------------------------------------------------------------------------- #
def validate_order_suggestion(output, step_names: list[str]) -> list[str]:
    """Problems in an AI order suggestion: every step exactly once, no new or renamed steps."""
    if not isinstance(output, dict) or not isinstance(output.get("stages"), list) or not output["stages"]:
        return ['The answer must be one JSON object: {"stages": [{"name": ..., "steps": [...]}]}.']
    errors, seen = [], []
    for i, stage in enumerate(output["stages"]):
        if not isinstance(stage, dict) or not str(stage.get("name") or "").strip() \
                or not isinstance(stage.get("steps"), list):
            errors.append(f"stages[{i}] must have a 'name' and a list of 'steps'.")
            continue
        seen += [str(s) for s in stage["steps"]]
    known = set(step_names)
    unknown = sorted({s for s in seen if s not in known})
    if unknown:
        errors.append("These steps are not in the list (do not invent or rename steps): " + ", ".join(unknown) + ".")
    twice = sorted({s for s in seen if seen.count(s) > 1})
    if twice:
        errors.append("These steps appear more than once: " + ", ".join(twice) + ".")
    missing = [s for s in step_names if s not in seen]
    if missing:
        errors.append("These steps are missing: " + ", ".join(missing) + ".")
    return errors


def suggest_order_ai(job_id: str, step_names: list[str], stage_names: list[str] | None = None,
                     process_name: str = "Process", client=None):
    """An order suggested by the AI from step and stage NAMES only (no counts, no data).

    Names are masked first (payload_mask: emails and ID numbers become "[id]").
    The answer is validated by code (every step exactly once, no new names) and
    retried like the summary; if it still fails, None is returned (no error shown).
    Returns an Order with source "ai" (needs confirmation by the business), or None.
    """
    from app.core.reference import Order   # local import avoids a cycle

    cfg = settings()["ai"]
    if client is None:
        ok, _ = ai_status()
        if not ok:
            return None
    if not privacy.ai_allowed()[0] or len(step_names) < 2:
        return None
    masked = {payload_mask(n): n for n in step_names}
    if len(masked) != len(step_names):
        return None                             # two names collapse when masked: cannot map back
    stages = sorted({payload_mask(s) for s in stage_names or [] if s})
    messages = [{"role": "system", "content": (PROMPTS / "order_suggester_system.txt").read_text(encoding="utf-8")},
                {"role": "user", "content": _fill((PROMPTS / "order_suggester_user.txt").read_text(encoding="utf-8"),
                                                  {"activities": "\n".join(f"- {m}" for m in masked),
                                                   "stages": "\n".join(f"- {s}" for s in stages) or "(none)"})}]
    model = os.getenv("LLM_MODEL") or "unknown-model"
    with observability.trace(job_id, tags=["order-suggestion"], metadata={"steps": len(step_names), "model": model},
                             allow_names=set(step_names)):
        if client is None:
            from app.core import ai_client
            client = ai_client.session("order_suggestion")
        state: dict = {}
        for _attempt in range(cfg["max_retries"] + 1):
            try:
                reply = _call(client, model, messages, cfg["temperature"], state)
            except Exception:  # noqa: BLE001 - give up silently: the data order stays available
                return None
            output = parse_reply(reply)
            problems = validate_order_suggestion(output, list(masked))
            if not problems:
                return Order(process_name, [{"name": str(st["name"]).strip(),
                                             "steps": [masked[str(x)] for x in st["steps"]]}
                                            for st in output["stages"] if st["steps"]],
                             [], "ai", False, "")
            messages = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": "Your previous answer broke these rules:\n- " + "\n- ".join(problems)
                 + "\nReturn the corrected JSON object only."}]
    return None


def record_feedback(job_id: str, value: int) -> None:
    """A reader's 👍 (1) or 👎 (0): kept in narrative.json and sent to the report's Langfuse trace."""
    folder = jobs.job_folder(job_id)
    result = load_narrative(folder)
    if result is None:
        return
    result.feedback.append(int(value))
    save_narrative(result, folder)
    observability.score("user_feedback", int(value), trace_id=result.trace_id)  # sent in the background
