"""AI help for the business process definition (only when the AI is switched on).

Golden rule: CODE decides every verdict (evidence.py). The AI only
    (a) drafts a definition from a document the user gives (`draft_from_document`), and
    (b) explains verdicts and proposes structured edits (`suggest_corrections`).
Every AI answer is validated by code; nothing is applied until a person accepts it
(`apply_ops`). Same client, masking (narrative._call -> payload_mask), retries and
Langfuse tracing as the report summary.

What is sent: (a) the document text plus the job's activity, stage and application
NAMES; (b) the claims (id, statement, verdict, badge) and the definition's ids and
names. Never data rows, counts, case IDs or users.
"""
from __future__ import annotations

import copy
import difflib
import io
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.core import ai_client, jobs, observability, privacy
from app.core.ai_client import AIUnavailable
from app.core.config import settings
from app.core.definition import FLOW_KINDS, ProcessDefinition, _now
from app.core.evidence import CONTRADICTED, PARTLY, SUPPORTED, claim_facts
from app.core.ingest import EMAIL_RE
from app.core.narrative import PROMPTS, _call, _fill, _words_pattern, ai_status, parse_reply, payload_mask

OPS = ("move_step", "add_step", "remove_step", "change_system", "set_mandatory", "set_optional", "add_flow",
       "remove_flow", "map_activity", "change_sla")
PLACEHOLDER = re.compile(r"\{\{([FC]\d{3,})\}\}")
# Which claim a suggestion would undo, per operation: (claim kind, the arg naming the step).
UNDOES = {"move_step": ("order", None), "remove_step": ("step_exists", "step_id"),
          "change_system": ("system", "step_id"), "set_optional": ("mandatory", "step_id"),
          "remove_flow": ("order", None), "change_sla": ("sla", "step_id")}


def _cfg() -> dict:
    return settings()["definition_ai"]


def available() -> tuple[bool, str | None]:
    """Whether the AI help may be used (switched on, configured, allowed by the privacy guard)."""
    ok, why = ai_status()
    if not ok:
        return False, why
    return privacy.ai_allowed()


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
def extract_text(file_name: str, data: bytes) -> str:
    """Plain text of a .docx, .pdf, .txt or .md document (at most definition_ai.max_doc_mb)."""
    if len(data) > float(_cfg()["max_doc_mb"]) * 1024 * 1024:
        raise ValueError(f"The document is larger than {_cfg()['max_doc_mb']} MB.")
    ext = Path(file_name).suffix.lower()
    if ext == ".docx":
        import docx
        doc = docx.Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            parts += [" | ".join(c.text for c in row.cells) for row in table.rows]
        return "\n".join(p for p in parts if p.strip())
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(data)).pages)
    if ext in (".txt", ".md", ""):
        return data.decode("utf-8-sig", errors="replace")
    raise ValueError(f"Unsupported document type '{ext}' (use .docx, .pdf, .txt or .md).")


def chunks(text: str, size: int | None = None) -> list[str]:
    """The text in pieces of at most `size` characters, cut at paragraph ends when possible."""
    size = int(size or _cfg()["max_doc_chars"])
    out, cur = [], ""
    for para in text.split("\n"):
        while len(para) > size:
            out.append(para[:size])
            para = para[size:]
        if len(cur) + len(para) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{para}" if cur else para
    if cur.strip():
        out.append(cur)
    return out or [""]


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w%]+", " ", text.lower()).split())


def quote_found(quote: str, document: str, threshold: float | None = None) -> bool:
    """True when `quote` appears in the document, exactly or fuzzily (ratio >= quote_match)."""
    threshold = float(threshold if threshold is not None else _cfg()["quote_match"])
    q, doc = _norm(quote or ""), _norm(document)
    if not q:
        return False
    if q in doc:
        return True
    words, qn = doc.split(), len(q.split())
    best = 0.0
    for size in {max(1, qn - 1), qn, qn + 1}:
        for i in range(0, max(1, len(words) - size + 1)):
            window = " ".join(words[i:i + size])
            m = difflib.SequenceMatcher(None, q, window)
            if m.real_quick_ratio() < threshold or m.quick_ratio() < threshold:
                continue
            best = max(best, m.ratio())
            if best >= threshold:
                return True
    return False


# --------------------------------------------------------------------------- #
# 4a. Draft from a document
# --------------------------------------------------------------------------- #
@dataclass
class DraftResult:
    definition: ProcessDefinition | None
    dropped: list[str] = field(default_factory=list)      # "not found in the document"
    errors: list[str] = field(default_factory=list)
    attempts: int = 0
    trace_id: str | None = None
    kept_share: float | None = None
    provider: str | None = None          # which AI provider answered (never a key)
    model: str | None = None


def validate_draft(output, activities: list[str]) -> list[str]:
    """Schema problems in a drafted definition (each one sent back to the AI)."""
    if not isinstance(output, dict) or not isinstance(output.get("steps"), list) or not output["steps"]:
        return ['The answer must be one JSON object with a non-empty "steps" list.']
    errors, known = [], set(activities)
    max_words = int(_cfg()["max_quote_words"])
    ids = []
    for i, s in enumerate(output["steps"]):
        if not isinstance(s, dict) or not str(s.get("name") or "").strip():
            errors.append(f"steps[{i}] must be an object with a 'name'.")
            continue
        ids.append(str(s.get("id") or ""))
        mapped = s.get("mapped_activities") or []
        if not isinstance(mapped, list):
            errors.append(f"steps[{i}].mapped_activities must be a list.")
            continue
        invented = [str(a) for a in mapped if str(a) not in known]
        if invented:
            errors.append(f"Step '{s['name']}' maps activities that are not in the list (do not invent or rename): "
                          + ", ".join(invented) + ".")
        quote = s.get("source_quote")
        if not isinstance(quote, str) or not quote.strip():
            errors.append(f"Step '{s['name']}' needs a source_quote from the document.")
        elif len(quote.split()) > max_words:
            errors.append(f"Step '{s['name']}': the source_quote has more than {max_words} words.")
    for i, f in enumerate(output.get("flows") or []):
        if not isinstance(f, dict) or str(f.get("from_step")) not in ids or str(f.get("to_step")) not in ids:
            errors.append(f"flows[{i}] must connect two step ids from 'steps'.")
        elif f.get("kind", "next") not in FLOW_KINDS:
            errors.append(f"flows[{i}].kind must be one of {', '.join(FLOW_KINDS)}.")
        else:
            quote = f.get("source_quote")
            if not isinstance(quote, str) or not quote.strip():
                errors.append(f"flows[{i}] needs a source_quote from the document.")
            elif len(quote.split()) > max_words:
                errors.append(f"flows[{i}].source_quote has more than {max_words} words.")
    for i, st in enumerate(output.get("stages") or []):
        if not isinstance(st, dict) or not str(st.get("name") or "").strip():
            errors.append(f"stages[{i}] must be an object with a 'name'.")
    return errors


def _retrying(client, model: str, messages: list[dict], check) -> tuple[object, list[str], int]:
    """Call, validate with `check(output) -> problems`, send problems back; returns
    (accepted output or None, all errors, attempts)."""
    cfg = settings()["ai"]
    state: dict = {}
    errors: list[str] = []
    for attempt in range(1, cfg["max_retries"] + 2):
        try:
            reply = _call(client, model, messages, cfg["temperature"], state)
        except AIUnavailable as err:          # no provider left: stop (the caller falls back)
            errors.append(f"attempt {attempt}: AI not available: {err.short()}")
            return None, errors, attempt
        except Exception as err:  # noqa: BLE001 - a failed call counts as a failed attempt
            errors.append(f"attempt {attempt}: the AI call failed ({type(err).__name__}: {err})")
            continue
        output = parse_reply(reply)
        problems = check(output)
        if not problems:
            return output, errors, attempt
        errors += [f"attempt {attempt}: {p}" for p in problems]
        messages = messages + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": "Your previous answer broke these rules:\n- " + "\n- ".join(problems)
             + "\nReturn the corrected JSON object only."}]
    return None, errors, cfg["max_retries"] + 1


def draft_from_document(job_id: str, document: str, file_name: str, activities: list[str],
                        stages: list[str] | None = None, applications: list[str] | None = None,
                        client=None, process_name: str | None = None) -> DraftResult:
    """A DRAFT definition from one document (see draft_from_documents)."""
    return draft_from_documents(job_id, [(file_name, document)], activities, stages, applications, client,
                                process_name)


def _key(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(name).lower()))


def draft_from_documents(job_id: str, documents: list[tuple[str, str]], activities: list[str],
                         stages: list[str] | None = None, applications: list[str] | None = None,
                         client=None, process_name: str | None = None) -> DraftResult:
    """A DRAFT definition from one or more documents (an SOP, a deck, a transcript ...). Every document is cut
    into chunks that fit the smallest provider of the route; each chunk is drafted on its own, and a step found
    in several chunks or documents becomes one step (its mapped activities joined, its first quote kept).
    Steps whose source_quote is not found in their own document are dropped and listed.
    Returns DraftResult (definition None when it failed)."""
    documents = [(n, t) for n, t in documents if str(t or "").strip()]
    if not documents:
        return DraftResult(None, errors=["The documents are empty."])
    file_name = documents[0][0] if len(documents) == 1 else \
        f"{len(documents)} documents ({', '.join(n for n, _ in documents)})"
    if client is None:
        ok, why = available()
        if not ok:
            return DraftResult(None, errors=[f"AI not available: {why}"])
    elif not privacy.ai_allowed()[0]:
        return DraftResult(None, errors=["AI not allowed by the privacy settings"])
    masked = {payload_mask(a): a for a in activities}
    model = os.getenv("LLM_MODEL") or "unknown-model"
    system = (PROMPTS / "definition_drafter_system.txt").read_text(encoding="utf-8")
    user_t = (PROMPTS / "definition_drafter_user.txt").read_text(encoding="utf-8")
    steps, flows, stage_rows, meta, dropped, errors, attempts = [], [], [], {}, [], [], 0
    with observability.trace(job_id, tags=["definition-draft"], metadata={"model": model, "file": file_name},
                             allow_names=set(activities), name="definition-draft") as trace_id:
        prompt, _version = observability.get_prompt(
            _cfg()["prompt_name_draft"], settings()["ai"]["prompt_label"],
            [{"role": "system", "content": system}, {"role": "user", "content": user_t}])
        if client is None:
            client = ai_client.session("definition_draft")
        size = ai_client.chunk_chars("definition_draft", int(_cfg()["max_doc_chars"]))
        by_key: dict[str, dict] = {}
        parts = [(name, part) for name, text in documents for part in chunks(text, size)]
        for doc_name, part in parts:
            masked_doc = payload_mask(part)
            values = {"activities": "\n".join(f"- {m}" for m in masked) or "(none)",
                      "stages": "\n".join(f"- {payload_mask(s)}" for s in stages or [] if s) or "(none)",
                      "applications": "\n".join(f"- {payload_mask(a)}" for a in applications or [] if a) or "(none)",
                      "document": masked_doc}
            messages = [{"role": m["role"], "content": _fill(m["content"], values)} for m in prompt]
            output, errs, n = _retrying(client, model, messages, lambda o: validate_draft(o, list(masked)))
            errors += errs
            attempts += n
            if output is None:
                continue
            meta = meta or {k: output.get(k) for k in ("name", "description", "start_trigger", "end_result")}
            id_map = {}
            for s in output["steps"]:
                if not quote_found(s.get("source_quote", ""), masked_doc):
                    dropped.append(f"{s['name']} (quote not found in {doc_name})")
                    continue
                acts = [masked[str(a)] for a in s.get("mapped_activities") or []]
                same = by_key.get(_key(s["name"]))
                if same is not None:            # the same step in another chunk or document: one step
                    id_map[str(s.get("id"))] = same["id"]
                    same["mapped_activities"] = sorted(set(same["mapped_activities"]) | set(acts))
                    continue
                new_id = f"S{len(steps) + 1:02d}"
                id_map[str(s.get("id"))] = new_id
                source = f" [Source: “{s['source_quote']}”" + (f" — {doc_name}]" if len(documents) > 1 else "]")
                steps.append({**{k: v for k, v in s.items() if k != "source_quote"}, "id": new_id,
                              "position": len(steps) + 1, "mapped_activities": acts,
                              "description": (str(s.get("description") or "") + source).strip()})
                by_key[_key(s["name"])] = steps[-1]
            for st in output.get("stages") or []:
                if not any(x["id"] == st.get("id") for x in stage_rows):
                    stage_rows.append(st)
            for f in output.get("flows") or []:
                if str(f.get("from_step")) in id_map and str(f.get("to_step")) in id_map:
                    if quote_found(f.get("source_quote", ""), masked_doc):
                        flow = {**f, "from_step": id_map[str(f["from_step"])], "to_step": id_map[str(f["to_step"])]}
                        if flow["from_step"] != flow["to_step"] and not any(
                                (x["from_step"], x["to_step"], x.get("kind")) == (flow["from_step"], flow["to_step"],
                                                                                  flow.get("kind")) for x in flows):
                            flows.append(flow)
        total = len(steps) + len(dropped)
        kept = round(len(steps) / total, 3) if total else None
        if kept is not None:
            observability.score("draft_steps_kept_share", kept)
    provider, used_model = getattr(client, "provider", None), getattr(client, "model", None)
    if hasattr(client, "notes"):          # routed providers: show that a fallback or retry happened
        errors += client.notes()
    if not steps:
        return DraftResult(None, dropped, errors or ["No step of the draft was found in the document."],
                           attempts, trace_id, kept, provider, used_model)
    d = ProcessDefinition.from_dict({
        "name": process_name or meta.get("name") or Path(file_name).stem, "description": meta.get("description"),
        "start_trigger": meta.get("start_trigger"), "end_result": meta.get("end_result"),
        "stages": stage_rows, "steps": steps, "flows": flows,
        "source": (f"Drafted by AI ({used_model} via {provider}) from {file_name}; check every row" if provider
                   else f"Drafted by AI from {file_name}; check every row")})
    from app.core.definition import _with_stages
    return DraftResult(_with_stages(d), dropped, errors, attempts, trace_id, kept, provider, used_model)


# --------------------------------------------------------------------------- #
# 4b. Explain the evidence and suggest corrections
# --------------------------------------------------------------------------- #
@dataclass
class Suggestions:
    explanations: list[dict] = field(default_factory=list)
    suggestions: list[dict] = field(default_factory=list)    # + "status": open | accepted | rejected
    errors: list[str] = field(default_factory=list)
    attempts: int = 0
    trace_id: str | None = None
    dropped: int = 0
    provider: str | None = None          # which AI provider answered (never a key)
    model: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _names_to_avoid(job_id: str) -> list[str]:
    """Resource values of the job (people or systems): never allowed in AI text."""
    try:
        m = json.loads((jobs.job_folder(job_id) / "mining.json").read_text(encoding="utf-8"))
        return [r["resource"] for r in (m["resources"]["data"] or {}).get("resources", []) if r.get("resource")]
    except Exception:  # noqa: BLE001 - no resource list: nothing to avoid
        return []


def _text_problems(text, where: str, cited: list[str], known_ids: set[str], avoid: list[str]) -> list[str]:
    cfg = settings()["ai"]
    if not isinstance(text, str) or not text.strip():
        return [f"{where} needs a non-empty text."]
    out = []
    for pid in PLACEHOLDER.findall(text):
        if pid not in known_ids:
            out.append(f"{where} uses {{{{{pid}}}}}, which is not a known claim or fact.")
        elif pid.startswith("C") and pid not in cited:
            out.append(f"{where} uses {{{{{pid}}}}} but does not list {pid} in 'claims'.")
    bare = PLACEHOLDER.sub("", text)
    digit = re.search(r".{0,20}\d.{0,20}", bare)
    if digit:
        out.append(f"{where} contains a typed digit ('{digit.group(0).strip()}'). Use placeholders for numbers.")
    for m in _words_pattern(cfg["banned_causal_phrases"]).finditer(text):
        out.append(f"{where} uses the causal phrase '{m.group(0)}'. Say 'is associated with' instead.")
    if re.search(EMAIL_RE.pattern.strip("^$"), text):
        out.append(f"{where} contains an email address.")
    for name in avoid:
        if len(name) > 2 and re.search(r"\b" + re.escape(name) + r"\b", text, re.IGNORECASE):
            out.append(f"{where} names a person or user ('{name}'). Never name people.")
    return out


def validate_suggestion(item, defn: ProcessDefinition, claims: dict[str, dict], activities: list[str],
                        known_ids: set[str], avoid: list[str], where: str = "suggestion") -> list[str]:
    """Problems in one suggestion; [] when it may be shown to the user."""
    if not isinstance(item, dict):
        return [f"{where} must be an object with op, args, reason and claims."]
    op, args, cited = item.get("op"), item.get("args"), item.get("claims")
    if op not in OPS:
        return [f"{where} uses the unknown operation '{op}'."]
    if not isinstance(args, dict):
        return [f"{where} needs 'args' (an object)."]
    if not isinstance(cited, list) or not cited:
        return [f"{where} must list the claims it relies on in 'claims'."]
    errors = []
    cited = [str(c) for c in cited]
    unknown = [c for c in cited if c not in claims]
    if unknown:
        errors.append(f"{where} cites unknown claims: {', '.join(unknown)}.")
    verdicts = [claims[c]["verdict"] for c in cited if c in claims]
    if any(v == SUPPORTED for v in verdicts):
        errors.append(f"{where} cites a Supported claim; suggestions may only rely on Contradicted or Partly "
                      "supported claims.")
    if not any(v in (CONTRADICTED, PARTLY) for v in verdicts):
        errors.append(f"{where} must cite at least one Contradicted or Partly supported claim.")
    steps = {s["id"] for s in defn.steps}
    stages = {s["id"] for s in defn.stages}
    step_arg = args.get("step_id")
    if op in ("move_step", "remove_step", "change_system", "set_mandatory", "set_optional", "map_activity",
              "change_sla") and step_arg not in steps:
        errors.append(f"{where}: step_id '{step_arg}' is not a step of the definition.")
    if op == "move_step":
        p = args.get("position")
        if not isinstance(p, (int, float)) or not 1 <= int(p) <= len(defn.steps):
            errors.append(f"{where}: position must be a number from 1 to {len(defn.steps)}.")
    if op == "add_step":
        if not str(args.get("name") or "").strip():
            errors.append(f"{where}: add_step needs a 'name'.")
        elif any(s["name"].lower() == str(args["name"]).strip().lower() for s in defn.steps):
            errors.append(f"{where}: a step named '{args['name']}' already exists.")
        if stages and args.get("stage_id") not in stages:
            errors.append(f"{where}: stage_id '{args.get('stage_id')}' is not a stage of the definition.")
        bad = [a for a in args.get("mapped_activities") or [] if a not in activities]
        if bad:
            errors.append(f"{where}: activities not in the data: {', '.join(map(str, bad))}.")
    if op == "change_system" and not str(args.get("system") or "").strip():
        errors.append(f"{where}: change_system needs a 'system'.")
    if op in ("add_flow", "remove_flow"):
        a, b = args.get("from_step"), args.get("to_step")
        if a not in steps or b not in steps:
            errors.append(f"{where}: from_step and to_step must be steps of the definition.")
        if op == "add_flow" and args.get("kind", "next") not in FLOW_KINDS:
            errors.append(f"{where}: kind must be one of {', '.join(FLOW_KINDS)}.")
        if op == "remove_flow" and not any(f["from_step"] == a and f["to_step"] == b for f in defn.effective_flows()):
            errors.append(f"{where}: there is no flow {a} → {b} to remove.")
    if op == "map_activity" and args.get("activity") not in activities:
        errors.append(f"{where}: activity '{args.get('activity')}' is not in the data.")
    if op == "change_sla":
        h = args.get("sla_hours")
        if not isinstance(h, (int, float)) or h <= 0:
            errors.append(f"{where}: sla_hours must be a positive number.")
    # A suggestion may not undo what the data supports.
    kind, key = UNDOES.get(op, (None, None))
    if kind:
        for c in claims.values():
            refs = c.get("refs") or {}
            if c["kind"] != kind or c["verdict"] != SUPPORTED:
                continue
            if key and refs.get("step_id") == args.get(key):
                errors.append(f"{where} would undo the Supported claim {c['id']}.")
            elif op == "remove_flow" and refs.get("from_step") == args.get("from_step") \
                    and refs.get("to_step") == args.get("to_step"):
                errors.append(f"{where} would undo the Supported claim {c['id']}.")
            elif op == "move_step" and step_arg in (refs.get("from_step"), refs.get("to_step")):
                errors.append(f"{where} would undo the Supported claim {c['id']} (the step's order is supported).")
    errors += _text_problems(item.get("reason"), f"{where}.reason", cited, known_ids, avoid)
    return errors


def _claims_text(claims: list[dict]) -> str:
    return "\n".join(f"{c['id']} | {c['statement']} | {c['verdict']} | {c['badge']}" for c in claims)


def _definition_text(defn: ProcessDefinition) -> str:
    lines = ["Stages: " + "; ".join(f"{s['id']} {s['name']}" for s in defn.stages)]
    for s in defn.ordered_steps():
        lines.append(f"Step {s['id']} | {s['name']} | stage {s['stage_id']} | position {s['position']} | "
                     f"{'mandatory' if s.get('mandatory', True) else 'optional'} | system {s.get('system') or '-'} | "
                     f"SLA {'yes' if s.get('sla_hours') is not None else 'no'} | activities: "
                     + ", ".join(s.get("mapped_activities") or []))
    lines.append("Flows: " + "; ".join(f"{f['from_step']} -> {f['to_step']} ({f['kind']})"
                                       for f in defn.effective_flows()))
    return "\n".join(lines)


def suggest_corrections(job_id: str, defn: ProcessDefinition, evidence: dict, activities: list[str],
                        client=None, facts=None) -> Suggestions:
    """Explanations and suggested edits for the claims. Invalid items are sent back (retry);
    items still invalid after the retries are dropped. Texts are returned rendered."""
    from app.core.summary import render
    if client is None:
        ok, why = available()
        if not ok:
            return Suggestions(errors=[f"AI not available: {why}"])
    elif not privacy.ai_allowed()[0]:
        return Suggestions(errors=["AI not allowed by the privacy settings"])
    claims = {c["id"]: c for c in evidence["claims"]}
    all_facts = list(facts or []) + claim_facts(evidence)
    known_ids = {f.id for f in all_facts}
    avoid = _names_to_avoid(job_id)
    model = os.getenv("LLM_MODEL") or "unknown-model"
    system = (PROMPTS / "definition_reviewer_system.txt").read_text(encoding="utf-8")
    user_t = (PROMPTS / "definition_reviewer_user.txt").read_text(encoding="utf-8")

    def problems_of(output) -> tuple[list[str], list[dict], list[dict], int]:
        if not isinstance(output, dict) or not isinstance(output.get("suggestions", []), list) \
                or not isinstance(output.get("explanations", []), list):
            return ['The answer must be one JSON object with "explanations" and "suggestions" lists.'], [], [], 0
        problems, good_s, good_e, bad = [], [], [], 0
        for i, e in enumerate(output.get("explanations") or []):
            cited = [str(c) for c in (e.get("claims") or [])] if isinstance(e, dict) else []
            errs = (["explanations[%d] must list its claims." % i] if not cited else []) + \
                [f"explanations[{i}] cites the unknown claim {c}." for c in cited if c not in claims]
            errs += _text_problems(e.get("text") if isinstance(e, dict) else None, f"explanations[{i}]", cited,
                                   known_ids, avoid)
            problems += errs
            if errs:
                bad += 1
            else:
                good_e.append(e)
        for i, s in enumerate(output.get("suggestions") or []):
            errs = validate_suggestion(s, defn, claims, activities, known_ids, avoid, f"suggestions[{i}]")
            problems += errs
            if errs:
                bad += 1
            else:
                good_s.append(s)
        return problems, good_e, good_s, bad

    with observability.trace(job_id, tags=["definition-review"], metadata={"model": model,
                             "claims": len(claims)}, allow_names=set(activities), name="definition-review") as tid:
        prompt, _v = observability.get_prompt(
            _cfg()["prompt_name_suggest"], settings()["ai"]["prompt_label"],
            [{"role": "system", "content": system}, {"role": "user", "content": user_t}])
        values = {"claims": _claims_text(evidence["claims"]), "definition": _definition_text(defn),
                  "activities": "\n".join(f"- {a}" for a in activities) or "(none)"}
        messages = [{"role": m["role"], "content": _fill(m["content"], values)} for m in prompt]
        if client is None:
            client = ai_client.session("evidence_explain")
        cfg = settings()["ai"]
        state: dict = {}
        errors: list[str] = []
        best: tuple[list, list, int] = ([], [], 0)
        attempts = 0
        for attempts in range(1, cfg["max_retries"] + 2):
            try:
                reply = _call(client, model, messages, cfg["temperature"], state)
            except AIUnavailable as err:
                errors.append(f"attempt {attempts}: AI not available: {err.short()}")
                break
            except Exception as err:  # noqa: BLE001
                errors.append(f"attempt {attempts}: the AI call failed ({type(err).__name__}: {err})")
                continue
            problems, good_e, good_s, bad = problems_of(parse_reply(reply))
            best = (good_e, good_s, bad)
            if not problems:
                break
            errors += [f"attempt {attempts}: {p}" for p in problems]
            messages = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": "Your previous answer broke these rules:\n- " + "\n- ".join(problems)
                 + "\nReturn the corrected JSON object only."}]
    good_e, good_s, bad = best
    return Suggestions(
        explanations=[{"text": render(e["text"], all_facts), "claims": e["claims"]} for e in good_e],
        suggestions=[{"op": s["op"], "args": s["args"], "reason": render(s["reason"], all_facts),
                      "claims": s["claims"], "status": "open"} for s in good_s],
        errors=errors + (client.notes() if hasattr(client, "notes") else []), attempts=attempts, trace_id=tid,
        dropped=bad,
        provider=getattr(client, "provider", None), model=getattr(client, "model", None))


def describe_op(op: dict, defn: ProcessDefinition) -> str:
    """"Move Approve request to position 2" — a suggestion in plain words."""
    a = op.get("args") or {}
    step = (defn.step(a.get("step_id") or "") or {}).get("name") or a.get("step_id")
    name = lambda sid: (defn.step(sid) or {}).get("name") or sid  # noqa: E731
    return {
        "move_step": f"Move {step} to position {a.get('position')}",
        "add_step": f"Add the step {a.get('name')} at position {a.get('position', 'end')}",
        "remove_step": f"Remove the step {step}",
        "change_system": f"Change the system of {step} to {a.get('system')}",
        "change_owner": f"Change the owner of {step} to {a.get('owner_role')}",
        "set_mandatory": f"Make {step} mandatory", "set_optional": f"Make {step} optional",
        "add_flow": f"Add a {a.get('kind', 'next')} flow {name(a.get('from_step'))} → {name(a.get('to_step'))}",
        "remove_flow": f"Remove the flow {name(a.get('from_step'))} → {name(a.get('to_step'))}",
        "map_activity": f"Map the activity {a.get('activity')} to {step}",
        "change_sla": f"Set the SLA of {step} to {a.get('sla_hours')} hours",
    }.get(op.get("op"), str(op.get("op")))


# --------------------------------------------------------------------------- #
# Applying accepted operations (code)
# --------------------------------------------------------------------------- #
def _renumber(defn: ProcessDefinition, ordered: list[dict]) -> None:
    for i, s in enumerate(ordered, 1):
        s["position"] = i
    defn.steps = ordered
    if defn.flows:   # explicit flows: "next" flows follow the new order, other kinds are kept
        others = [f for f in defn.flows if f["kind"] != "next" and defn.step(f["from_step"]) and defn.step(f["to_step"])]
        nexts = [{"from_step": a["id"], "to_step": b["id"], "kind": "next",
                  "condition": next((f.get("condition", "") for f in defn.flows if f["kind"] == "next"
                                     and f["from_step"] == a["id"] and f["to_step"] == b["id"]), "")}
                 for a, b in zip(ordered, ordered[1:])]
        defn.flows = nexts + others


def apply_ops(defn: ProcessDefinition, ops: list[dict], by: str) -> ProcessDefinition:
    """Apply accepted operations to a copy of the draft (an approved version first becomes
    the next draft). Every operation is recorded in the change log."""
    from app.core.definition import new_draft
    d = (new_draft(defn, by, "Started from the approved version") if defn.status == "approved"
         else ProcessDefinition.from_dict(copy.deepcopy(defn.to_dict())))
    for op in ops:
        a = op.get("args") or {}
        steps = d.ordered_steps()
        if op["op"] == "move_step":
            s = d.step(a["step_id"])
            steps.remove(s)
            steps.insert(int(a["position"]) - 1, s)
            _renumber(d, steps)
        elif op["op"] == "add_step":
            n = 1 + max([int(re.sub(r"\D", "", s["id"]) or 0) for s in d.steps] or [0])
            new = {"id": f"S{n:02d}", "name": str(a["name"]).strip(), "stage_id": a.get("stage_id") or
                   (d.stages[0]["id"] if d.stages else ""), "position": 0, "owner_role": "", "system": "",
                   "description": "", "inputs": "", "outputs": "", "mandatory": True, "sla_hours": None,
                   "business_rules": "", "mapped_activities": list(a.get("mapped_activities") or []),
                   "milestone_labels": [], "standard_path_ref": None}
            at = int(a.get("position") or len(steps) + 1)
            steps.insert(max(0, min(at - 1, len(steps))), new)
            _renumber(d, steps)
        elif op["op"] == "remove_step":
            d.flows = [f for f in d.flows if a["step_id"] not in (f["from_step"], f["to_step"])]
            _renumber(d, [s for s in steps if s["id"] != a["step_id"]])
        elif op["op"] == "change_system":
            d.step(a["step_id"])["system"] = str(a["system"]).strip()
        elif op["op"] == "change_owner":       # knowledge proposals only; not an AI operation
            d.step(a["step_id"])["owner_role"] = str(a["owner_role"]).strip()
        elif op["op"] in ("set_mandatory", "set_optional"):
            d.step(a["step_id"])["mandatory"] = op["op"] == "set_mandatory"
        elif op["op"] == "add_flow":
            if not d.flows:
                d.flows = d.effective_flows()
            d.flows.append({"from_step": a["from_step"], "to_step": a["to_step"], "kind": a.get("kind", "next"),
                            "condition": str(a.get("condition") or "")})
        elif op["op"] == "remove_flow":
            if not d.flows:
                d.flows = d.effective_flows()
            d.flows = [f for f in d.flows if not (f["from_step"] == a["from_step"] and f["to_step"] == a["to_step"])]
        elif op["op"] == "map_activity":
            for s in d.steps:          # an activity belongs to one step
                s["mapped_activities"] = [x for x in s.get("mapped_activities") or [] if x != a["activity"]]
            d.step(a["step_id"])["mapped_activities"].append(a["activity"])
        elif op["op"] == "change_sla":
            d.step(a["step_id"])["sla_hours"] = float(a["sla_hours"])
        d.change_log.append({"version": d.version, "at": _now(), "by": by or "unknown",
                             "summary": "Accepted suggestion: " + describe_op(op, defn if op["op"] != "add_step" else d)})
    return d


# --------------------------------------------------------------------------- #
# Saving suggestions and decisions
# --------------------------------------------------------------------------- #
def save_suggestions(result: Suggestions, folder: Path) -> Path:
    path = folder / "suggestions.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")
    return path


def load_suggestions(folder: Path) -> Suggestions | None:
    path = folder / "suggestions.json"
    return Suggestions(**json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None


def record_decision(folder: Path, index: int, accepted: bool) -> None:
    """Accept / reject one suggestion: kept in suggestions.json, scored in Langfuse
    (`suggestion_accepted` 1/0 on the suggestions' trace)."""
    result = load_suggestions(folder)
    if result is None or not 0 <= index < len(result.suggestions):
        return
    result.suggestions[index]["status"] = "accepted" if accepted else "rejected"
    save_suggestions(result, folder)
    observability.score("suggestion_accepted", 1 if accepted else 0, trace_id=result.trace_id)
