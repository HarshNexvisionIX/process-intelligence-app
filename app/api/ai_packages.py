"""AI packages: what would be sent to the AI, shown first, sent only after a reviewer approves it.

Adopted from ProcessLens (approve-before-send with a SHA-256 fingerprint), on top of our own AI gateway:

    1. prepare   the analysis runs with a PreviewGateway: every AI request is built and checked by the privacy
                 guard exactly as it would be sent, then KEPT (not sent). The rules-only result is saved as usual.
                 Each request becomes a package: purpose, what is in it, what is never in it, the exact messages,
                 the size and a SHA-256 fingerprint of the exact messages.
    2. approve   a reviewer approves packages by fingerprint (per task). Approval is stored in the job folder
                 (ai_approvals.json) with who and when.
    3. send      the analysis runs again with an ApprovedGateway: a request is sent only when its task was
                 approved. Requests built from the AI's own earlier answers (e.g. activity names for screens the
                 AI has just linked) cannot exist at preview time; they are sent under the same task approval and
                 marked so in the audit log. Every request actually sent is logged (ai_audit.jsonl) with its
                 fingerprint, whether it matched an approved package, provider and model.

Nothing here chooses a model or holds a key: providers, routes and keys stay in Settings / .env.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.nocase.ai_gateway import Answer, Gateway
from app.nocase.payloads import PayloadError, guard

PACKAGES_FILE = "ai_packages.json"
APPROVALS_FILE = "ai_approvals.json"
AUDIT_FILE = "ai_audit.jsonl"

TASKS = {
    "nc_evidence_plan": {
        "title": "Evidence plan",
        "purpose": "Ask the AI which signals in the data best separate the process steps, and which questions an "
                   "SME should answer.",
        "includes": "Approved step names and descriptions; counts per application; screen groups with event counts.",
        "never": "Raw event rows, user IDs, case IDs, timestamps of single events, document text.",
        "uses_answer": "Questions for the SME (Review steps) and the evidence weights of the run."},
    "nc_assign_rules": {
        "title": "Link screens to steps",
        "purpose": "Ask the AI to link each application + screen group to one approved step, or to mark it as other "
                   "work. Each answer becomes a proposed exact-match rule that a reviewer accepts on Review steps.",
        "includes": "Approved step names and descriptions; per screen group: application, cleaned screen name, "
                    "event count, share of hours, the screens usually used with it.",
        "never": "Raw event rows, user IDs, case IDs, single-event timestamps, document text, numbers of 4+ digits.",
        "uses_answer": "Proposed rules (rules.proposed.yaml) used on the map as 'proposed' until accepted."},
    "nc_activities": {
        "title": "Name activities inside each step",
        "purpose": "Ask the AI to group the clicks inside one step into a few named Level 4 activities.",
        "includes": "One step name; its screen + click units with counts and hours.",
        "never": "Raw event rows, user IDs, case IDs, single-event timestamps.",
        "uses_answer": "Proposed Level 4 activities (Review activities)."},
    "nc_summary": {
        "title": "Word the report summary",
        "purpose": "Ask the AI to word the report summary using only checked facts by their IDs ({{F001}}). Numbers "
                   "are filled in by the app, never typed by the AI.",
        "includes": "Fact IDs with their labels; the summary rules.",
        "never": "Raw data, names, the numbers themselves.",
        "uses_answer": "The summary paragraph of the report; checked and replaced by the fact-only text on failure."},
    "summary": {
        "title": "Executive summary (case ID)",
        "purpose": "Ask the AI to word the report's executive summary, findings and recommendations from the "
                   "checked facts, using fact placeholders; the app inserts and checks every number.",
        "includes": "The selected reliable facts (ID, label, value) with the data-quality band and order strength.",
        "never": "Raw event rows, case IDs, user names, document text.",
        "uses_answer": "The report's executive summary; the fixed-template summary is used on any problem."},
    "nc_readout": {
        "title": "AI Analyst: transformation readout (seven sections)",
        "purpose": "Ask the AI to turn the checked facts into a stakeholder story: findings, risks, interventions, a "
                   "to-be workflow with agents under human control, outcomes and decisions.",
        "includes": "ProcessLens's transformation context: scope, the evidence catalog E01-E11 (id, topic, "
                    "observed_fact, limitation) plus this app's step evidence (E12 ...), step summaries and rules.",
        "never": "Raw event rows, screens, user IDs, case IDs, names of people, document text.",
        "uses_answer": "The AI readout page and PDF. Each claim must cite one evidence ID and may contain no "
                       "numbers; the app shows the checked value beside it."},
    "nc_stage_explain": {
        "title": "Explain this step (Level 4)",
        "purpose": "Ask the AI what people do inside one step, a summary of each of its Level 4 activities, possible "
                   "friction and questions for the SME (ProcessLens stage explanation).",
        "includes": "For one step: hours and events per application; grouped application + click + cleaned screen "
                    "patterns with counts; the measured Level 4 activities.",
        "never": "Raw event rows, people, case IDs, typed text, e-mail addresses, long numbers.",
        "uses_answer": "The step panel on the Process Map, labelled as a hypothesis."},
    "nc_process_map": {
        "title": "AI-enhanced conceptual map",
        "purpose": "Ask the AI for a short conceptual process map (3-7 stages and arrows) as a hypothesis beside the "
                   "measured map (ProcessLens 'Enhance the map with AI').",
        "includes": "The compact map summary (ProcessLens format): counts, opaque step IDs with hints (the approved "
                    "step names without a case ID), top transitions, application and activity classes, limitations.",
        "never": "Raw event rows, titles, activity text, people, case IDs, application names.",
        "uses_answer": "The conceptual map drawn on the Process map page, labelled as a hypothesis."},
    "nc_pack_from_docs": {
        "title": "Stages and steps from documents",
        "purpose": "Ask the AI to read the business documents and propose stages and steps.",
        "includes": "Document text after masking (names, e-mails, IDs and numbers of 4+ digits are masked).",
        "never": "Event data of any kind.",
        "uses_answer": "A proposed process pack, approved in Process library."},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(messages: list[dict]) -> str:
    text = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_json(messages: list[dict]):
    """The JSON part of a message, when the message is (or ends with) a JSON object: shown as JSON in the UI."""
    out = []
    for m in messages:
        text = str(m.get("content", ""))
        start = text.find("{")
        parsed = None
        if start >= 0:
            try:
                parsed = json.loads(text[start:])
            except ValueError:
                parsed = None
        out.append({"role": m.get("role"), "text": text if parsed is None else text[:start].strip(),
                    "json": parsed})
    return out


def package(task: str, messages: list[dict], gw: Gateway) -> dict:
    info = TASKS.get(task, {"title": task, "purpose": "", "includes": "", "never": "", "uses_answer": ""})
    size = len(json.dumps(messages, ensure_ascii=False))
    try:
        providers = gw.providers_for(task) if gw.mode == "online" else []
    except Exception:  # noqa: BLE001 - a broken provider file must not stop the preview
        providers = []
    return {"task": task, **info, "fingerprint": fingerprint(messages), "approx_tokens": size // 4,
            "characters": size, "messages": messages, "parts": _content_json(messages),
            "would_go_to": providers, "dataset_class": gw.dataset_class, "guard": "passed",
            "prepared_at": _now()}


class PreviewGateway(Gateway):
    """Builds and guards every request, keeps it as a package, sends nothing (the run uses rules for that part)."""

    def __init__(self, dataset_class: str):
        super().__init__(dataset_class, "online")
        self.packages: list[dict] = []

    def why_not(self, task: str) -> str | None:
        return None          # always build the package, so the reviewer sees it even before AI is set up

    def ask(self, task, messages, check, retry=None):
        try:
            guard(messages)
        except PayloadError as exc:
            self.blocked.append({"task": task, "why": str(exc)})
            return Answer(reason=f"not sent: {exc}")
        self.packages.append(package(task, messages, self))
        return Answer(reason="prepared for approval (not sent)")


class ApprovedGateway(Gateway):
    """Sends a request only when its task is approved; logs every request actually sent."""

    def __init__(self, dataset_class: str, approved: dict, folder: Path):
        super().__init__(dataset_class, "online")
        self.approved = approved                         # task -> {"fingerprints": [...], "by", "at"}
        self.folder = folder

    def ask(self, task, messages, check, retry=None):
        if task not in self.approved:
            return Answer(reason="not approved: review the AI package first")
        return super().ask(task, messages, check, retry)

    def _call(self, task, messages, prefer):
        fp = fingerprint(messages)
        ok = fp in set(self.approved.get(task, {}).get("fingerprints", []))
        entry = {"at": _now(), "task": task, "fingerprint": fp,
                 "matched": "exact approved package" if ok else "built after earlier AI answers (task approved)",
                 "approved_by": self.approved.get(task, {}).get("by"), "characters": len(json.dumps(messages))}
        try:
            text, provider, model = super()._call(task, messages, prefer)
            entry.update(provider=provider, model=model, result="answered")
            return text, provider, model
        except Exception as exc:
            entry.update(result=f"failed: {type(exc).__name__}")
            raise
        finally:
            with open(self.folder / AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- storage
def save_packages(folder: Path, packages: list[dict], kind: str) -> dict:
    doc = load_packages(folder)
    doc[kind] = {"prepared_at": _now(), "packages": packages}
    (folder / PACKAGES_FILE).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def load_packages(folder: Path) -> dict:
    p = folder / PACKAGES_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def summary(folder: Path, kind: str) -> list[dict]:
    """One row per task: packages, size, fingerprints, approval."""
    pk = (load_packages(folder).get(kind) or {}).get("packages", [])
    appr = load_approvals(folder).get(kind, {})
    out: dict[str, dict] = {}
    for p in pk:
        t = out.setdefault(p["task"], {"task": p["task"], "title": p["title"], "purpose": p["purpose"],
                                       "includes": p["includes"], "never": p["never"],
                                       "uses_answer": p["uses_answer"], "would_go_to": p["would_go_to"],
                                       "requests": 0, "approx_tokens": 0, "fingerprints": []})
        t["requests"] += 1
        t["approx_tokens"] += p["approx_tokens"]
        t["fingerprints"].append(p["fingerprint"])
    for t in out.values():
        a = appr.get(t["task"])
        t["task_fingerprint"] = hashlib.sha256("".join(sorted(t["fingerprints"])).encode()).hexdigest()
        t["approved"] = bool(a and a.get("task_fingerprint") == t["task_fingerprint"])
        t["approved_by"], t["approved_at"] = (a or {}).get("by"), (a or {}).get("at")
    return list(out.values())


def load_approvals(folder: Path) -> dict:
    p = folder / APPROVALS_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def approve(folder: Path, kind: str, task: str, task_fingerprint: str, by: str) -> dict:
    """Approve one task's packages, but only if the reviewer saw exactly the current ones."""
    rows = {r["task"]: r for r in summary(folder, kind)}
    if task not in rows:
        raise ValueError("Nothing is prepared for this task. Prepare the AI packages first.")
    if rows[task]["task_fingerprint"] != task_fingerprint:
        raise ValueError("The packages changed after you opened them. Reload and review them again.")
    if not by.strip():
        raise ValueError("Enter your name to approve.")
    doc = load_approvals(folder)
    doc.setdefault(kind, {})[task] = {"task_fingerprint": task_fingerprint, "fingerprints": rows[task]["fingerprints"],
                                      "by": by.strip(), "at": _now()}
    (folder / APPROVALS_FILE).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return doc[kind][task]


def revoke(folder: Path, kind: str, task: str) -> None:
    doc = load_approvals(folder)
    doc.get(kind, {}).pop(task, None)
    (folder / APPROVALS_FILE).write_text(json.dumps(doc, indent=1), encoding="utf-8")


def approved_tasks(folder: Path, kind: str) -> dict:
    """Approvals that still match the prepared packages."""
    current = {r["task"]: r for r in summary(folder, kind)}
    return {t: a for t, a in load_approvals(folder).get(kind, {}).items()
            if t in current and current[t]["task_fingerprint"] == a.get("task_fingerprint")}


def audit(folder: Path) -> list[dict]:
    p = folder / AUDIT_FILE
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# --------------------------------------------------------------------------- case-ID summary (OpenAI-style client)
class _Reply:
    def __init__(self, text: str):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]


class _Chat:
    def __init__(self, owner):
        self.completions = type("X", (), {"create": staticmethod(lambda **kw: owner.handle(kw["messages"]))})()


class RecordingClient:
    """Stands in for the AI client of app/core/narrative.write_summary: keeps the request, sends nothing."""

    def __init__(self, dataset_class: str):
        self.dataset_class, self.packages, self.provider, self.model = dataset_class, [], None, None
        self.chat = _Chat(self)
        self._gw = Gateway(dataset_class, "online")

    def handle(self, messages):
        from app.core.ai_client import AIUnavailable
        if not any(p["fingerprint"] == fingerprint(messages) for p in self.packages):
            self.packages.append(package("summary", messages, self._gw))
        raise AIUnavailable(["prepared for approval (not sent)"])

    def notes(self):
        return []


class ApprovedClient(RecordingClient):
    """Sends the summary request only when the task is approved; logs it like ApprovedGateway."""

    def __init__(self, dataset_class: str, approved: dict, folder: Path):
        super().__init__(dataset_class)
        self.approved, self.folder = approved, folder

    def handle(self, messages):
        from app.core import ai_client
        if "summary" not in self.approved:
            raise ai_client.AIUnavailable(["not approved: review the summary request first"])
        fp = fingerprint(messages)
        ok = fp in set(self.approved["summary"].get("fingerprints", []))
        entry = {"at": _now(), "task": "summary", "fingerprint": fp,
                 "matched": "exact approved package" if ok else "retry after a failed check (task approved)",
                 "approved_by": self.approved["summary"].get("by"), "characters": len(json.dumps(messages))}
        try:
            sess = ai_client.session("summary")
            text = sess.call(messages, json_expected=True)
            self.provider, self.model = sess.provider, sess.model
            entry.update(provider=sess.provider, model=sess.model, result="answered")
            return _Reply(text)
        except Exception as exc:
            entry.update(result=f"failed: {type(exc).__name__}")
            raise
        finally:
            with open(self.folder / AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
