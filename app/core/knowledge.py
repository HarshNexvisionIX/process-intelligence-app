"""Knowledge sources: everything the team knows about the process, as traceable statements.

Sources (SOPs, transcripts, walkthrough videos, notes and e-mails, BPMN / Visio / Excel
models, SLA / RACI tables, answers from subject-matter experts) are stored per process
definition:

    <data_dir>/process_definitions/<slug>/sources/<S001>/
        <original file>   source.json   text.md (anchored text)   statements.json

All text extraction is local: python-docx / pypdf, an own VTT / SRT parser, and for audio
and video faster-whisper (speech) plus ffmpeg key frames and Tesseract OCR (screen text).
Missing tools give a clear message, never an exception and never a cloud call.

Statements ({type, subject, object, value, quote, anchor}) come from the AI (only when it is
switched on; validated by code), from fixed rules for policy and model sources, or from a
person. Code then merges them into keys with a business belief 1 - Π(1 - reliability),
finds conflicts (never resolved automatically) and generates questions for the experts.
"""
from __future__ import annotations

import email
import email.policy
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from app.core.config import settings
from app.core.definition import ProcessDefinition, _write_json, definitions_dir, slug

KINDS = ("sop", "transcript", "video", "notes", "model", "policy", "sme_answer")
KIND_LABELS = {"sop": "SOP / process document", "transcript": "Transcript", "video": "Video / audio recording",
               "notes": "Notes / e-mail", "model": "Process model (BPMN, Visio, Excel)",
               "policy": "Policy / SLA / RACI table", "sme_answer": "Expert answer"}
EXTENSIONS = {"sop": (".docx", ".pdf", ".md", ".txt"), "transcript": (".vtt", ".srt", ".txt", ".docx"),
              "video": (".mp4", ".mov", ".webm", ".m4a", ".mp3", ".wav"), "notes": (".md", ".txt", ".docx", ".eml"),
              "model": (".bpmn", ".xml", ".vsdx", ".xlsx"), "policy": (".xlsx", ".csv"), "sme_answer": (".txt",)}
TYPES = ("step_exists", "order", "owner", "system", "mandatory", "optional", "sla", "rule", "exception",
         "loop_allowed", "start", "end", "pain_point", "workaround")
NEW_NAME_TYPES = {"step_exists", "system", "owner"}
TWO_STEP_TYPES = {"order", "loop_allowed"}
ANCHOR_RE = re.compile(r"\[(p\.\d+|\d{2}:\d{2}:\d{2}|row \d+|shape \d+|q\d+)\]")
INSTALL = {
    "faster-whisper": "pip install faster-whisper==1.2.1 (the speech model is downloaded once, then runs offline)",
    "ffmpeg": "winget install Gyan.FFmpeg   (or: choco install ffmpeg)",
    "tesseract": "winget install UB-Mannheim.TesseractOCR   (or: choco install tesseract), then pip install pytesseract==0.3.13",
}


def _cfg() -> dict:
    return settings()["knowledge"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hms(seconds: float) -> str:
    s = int(round(seconds or 0))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------- #
# 1. Storage
# --------------------------------------------------------------------------- #
def sources_dir(definition_name: str) -> Path:
    d = definitions_dir() / slug(definition_name) / "sources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_sources(definition_name: str, active_only: bool = False) -> list[dict]:
    out = []
    for folder in sorted(sources_dir(definition_name).glob("S*")):
        path = folder / "source.json"
        if path.exists():
            src = json.loads(path.read_text(encoding="utf-8"))
            if not active_only or src.get("status") == "active":
                out.append(with_effective_reliability(src))
    return out


def load_source(definition_name: str, source_id: str) -> dict | None:
    path = sources_dir(definition_name) / source_id / "source.json"
    return with_effective_reliability(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None


def source_text(definition_name: str, source_id: str) -> str:
    path = sources_dir(definition_name) / source_id / "text.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _save_source(definition_name: str, src: dict) -> None:
    keep = {k: v for k, v in src.items() if k not in ("effective_reliability", "stale")}
    _write_json(sources_dir(definition_name) / src["id"] / "source.json", keep)


def update_source(definition_name: str, source_id: str, **changes) -> dict:
    """Change reliability, status ("active" | "excluded"), title or recorded_on of a source."""
    src = load_source(definition_name, source_id)
    if src is None:
        raise ValueError(f"Unknown source {source_id}.")
    if "reliability" in changes:
        r = float(changes["reliability"])
        if not 0 <= r <= 1:
            raise ValueError("Reliability must be between 0 and 1.")
        changes["reliability"] = r
    if "status" in changes and changes["status"] not in ("active", "excluded"):
        raise ValueError("Status must be active or excluded.")
    src.update({k: v for k, v in changes.items() if k in ("reliability", "status", "title", "recorded_on")})
    _save_source(definition_name, src)
    return with_effective_reliability(src)


def is_stale(src: dict, today: date | None = None) -> bool:
    if not src.get("recorded_on"):
        return False
    try:
        recorded = date.fromisoformat(str(src["recorded_on"])[:10])
    except ValueError:
        return False
    return ((today or date.today()) - recorded).days > int(_cfg()["stale_after_days"])


def with_effective_reliability(src: dict, today: date | None = None) -> dict:
    """The source with `effective_reliability` (x stale_factor when older than stale_after_days)
    and `stale` ("may be outdated")."""
    stale = is_stale(src, today)
    r = float(src.get("reliability") or 0.0)
    return {**src, "stale": stale, "effective_reliability": round(r * (float(_cfg()["stale_factor"]) if stale else 1.0), 4)}


def default_reliability(kind: str, walkthrough: bool = False) -> float:
    r = _cfg()["reliability"]
    if walkthrough and kind in ("transcript", "video"):
        return float(r["walkthrough"])
    return float(r.get(kind, r["notes"]))


def _next_id(definition_name: str) -> str:
    ids = [int(p.name[1:]) for p in sources_dir(definition_name).glob("S*") if p.name[1:].isdigit()]
    return f"S{(max(ids) + 1) if ids else 1:03d}"


# --------------------------------------------------------------------------- #
# 2. Text extraction (local only)
# --------------------------------------------------------------------------- #
def tools_status() -> dict[str, bool]:
    """Which optional local tools are installed (nothing is called)."""
    try:
        import faster_whisper  # noqa: F401
        whisper = True
    except ImportError:
        whisper = False
    try:
        import pytesseract  # noqa: F401
        ocr = shutil.which("tesseract") is not None or Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe").exists()
    except ImportError:
        ocr = False
    return {"faster-whisper": whisper, "ffmpeg": shutil.which("ffmpeg") is not None, "tesseract": ocr}


def _docx_pages(data: bytes) -> list[str]:
    """Paragraph text per page (explicit page breaks start a new page)."""
    import docx
    from docx.oxml.ns import qn
    doc = docx.Document(io.BytesIO(data))
    pages, cur = [], []
    for p in doc.paragraphs:
        if any(br.get(qn("w:type")) == "page" for br in p._element.iter(qn("w:br"))) and cur:
            pages.append("\n".join(cur))
            cur = []
        if p.text.strip():
            cur.append(p.text.strip())
    for table in doc.tables:
        cur += [" | ".join(c.text.strip() for c in row.cells) for row in table.rows]
    pages.append("\n".join(cur))
    return pages


def _pages_text(pages: list[str]) -> str:
    return "\n\n".join(f"[p.{i}] {p.strip()}" for i, p in enumerate(pages, 1) if p.strip())


def document_text(file_name: str, data: bytes) -> str:
    """SOP / document text with page anchors: [p.1] ..."""
    ext = Path(file_name).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        return _pages_text([(page.extract_text() or "") for page in PdfReader(io.BytesIO(data)).pages])
    if ext == ".docx":
        return _pages_text(_docx_pages(data))
    return _pages_text(data.decode("utf-8-sig", errors="replace").split("\f"))


_CUE_TIME = re.compile(r"(\d{1,2}:)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->")
_VOICE = re.compile(r"<v(?:\.[^ >]+)?\s+([^>]+)>")
_SPEAKER = re.compile(r"^([A-Z][\w.'-]*(?: [A-Z][\w.'-]*){0,2})\s*:\s+(.*)$")
_TXT_LINE = re.compile(r"^\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s*(.*)$")


def _seconds(text: str) -> float:
    parts = [float(x) for x in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def parse_cues(text: str) -> list[tuple[float, str | None, str]]:
    """(start seconds, speaker or None, text) per cue of a .vtt / .srt file, or per line of a
    plain transcript ("[00:01:02] Name: text", "00:01 Name: text")."""
    cues: list[tuple[float, str | None, str]] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    has_cues = "-->" in text
    if has_cues:
        for block in blocks:
            lines = [ln for ln in block.strip().split("\n") if ln.strip()]
            stamp = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
            if stamp is None:
                continue
            m = _CUE_TIME.search(lines[stamp])
            start = _seconds(f"{(m.group(1) or '0:')}{m.group(2)}:{m.group(3)}") if m else 0.0
            body = " ".join(lines[stamp + 1:])
            speaker = None
            v = _VOICE.search(body)
            if v:
                speaker = v.group(1).strip()
            body = re.sub(r"<[^>]+>", "", body).strip()
            s = _SPEAKER.match(body)
            if speaker is None and s:
                speaker, body = s.group(1), s.group(2)
            if body:
                cues.append((start, speaker, body))
        return cues
    last = 0.0
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        t = _TXT_LINE.match(line)
        if t and ":" in t.group(1):
            last, line = _seconds(t.group(1)), t.group(2).strip()
        s = _SPEAKER.match(line)
        cues.append((last, s.group(1) if s else None, s.group(2) if s else line))
    return cues


def mask_speakers(cues: list[tuple[float, str | None, str]], roles: list[str] | None = None
                  ) -> tuple[list[tuple[float, str | None, str]], dict[str, str]]:
    """Speaker names -> "Speaker A", "Speaker B" ... (or the given roles, in order of first
    appearance); the names are also replaced inside the spoken text."""
    names: dict[str, str] = {}
    for _, sp, _ in cues:
        if sp and sp not in names:
            i = len(names)
            names[sp] = (roles[i] if roles and i < len(roles) and roles[i] else
                         f"Speaker {chr(ord('A') + i % 26)}{'' if i < 26 else i // 26}")
    parts = {p: names[n] for n in names for p in [n, *n.split()] if len(p) > 2}
    pattern = re.compile(r"\b(" + "|".join(re.escape(p) for p in sorted(parts, key=len, reverse=True)) + r")\b") \
        if parts else None
    out = []
    for t, sp, body in cues:
        if pattern:
            body = pattern.sub(lambda m: parts[m.group(0)], body)
        out.append((t, names.get(sp) if sp else None, body))
    return out, names


def cues_text(cues: list[tuple[float, str | None, str]]) -> str:
    return "\n".join(f"[{hms(t)}] {sp + ': ' if sp else ''}{body}" for t, sp, body in cues)


def transcript_text(file_name: str, data: bytes, roles: list[str] | None = None) -> str:
    """A transcript with "[hh:mm:ss] Speaker A: ..." anchors (speaker names masked)."""
    if Path(file_name).suffix.lower() == ".docx":
        raw = "\n".join(_docx_pages(data))
    else:
        raw = data.decode("utf-8-sig", errors="replace")
    cues, _ = mask_speakers(parse_cues(raw), roles)
    return cues_text(cues)


def transcribe_audio(path: Path) -> list[tuple[float, str]]:
    """(start seconds, text) segments from faster-whisper, locally on the CPU (int8)."""
    from faster_whisper import WhisperModel
    model = WhisperModel(str(_cfg()["whisper_model"]), device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(path))
    return [(float(s.start), s.text.strip()) for s in segments if s.text.strip()]


def ocr_frames(path: Path) -> list[tuple[float, str]]:
    """(seconds, screen text) for scene-change key frames at least frame_interval_seconds apart
    (ffmpeg extracts the frames, Tesseract reads them)."""
    import subprocess as sysproc
    import pytesseract
    from PIL import Image
    cfg = _cfg()
    tess = shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    pytesseract.pytesseract.tesseract_cmd = tess
    with tempfile.TemporaryDirectory() as tmp:
        sysproc.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf",
                     f"select='gt(scene,{cfg['scene_threshold']})',showinfo", "-vsync", "vfr",
                     str(Path(tmp) / "f_%05d.png")], check=True, capture_output=True)
        probe = sysproc.run([shutil.which("ffmpeg"), "-hide_banner", "-i", str(path), "-vf",
                             f"select='gt(scene,{cfg['scene_threshold']})',showinfo", "-f", "null", "-"],
                            capture_output=True, text=True)
        times = [float(m) for m in re.findall(r"pts_time:([\d.]+)", probe.stderr)]
        out, last = [], -1e9
        for t, frame in zip(times, sorted(Path(tmp).glob("f_*.png"))):
            if t - last < float(cfg["frame_interval_seconds"]):
                continue
            text = " ".join(pytesseract.image_to_string(Image.open(frame)).split())
            if text:
                out.append((t, text))
                last = t
        return out


def media_text(path: Path, roles: list[str] | None = None) -> tuple[str, list[str]]:
    """Anchored text of a recording: speech ("[hh:mm:ss] ...") and screen text ("[hh:mm:ss] screen: ...").
    Returns (text, messages); a missing tool gives a message, never an exception."""
    status = tools_status()
    messages, lines = [], []
    if not status["faster-whisper"]:
        messages.append("Speech-to-text needs faster-whisper, which is not installed: " + INSTALL["faster-whisper"])
    else:
        try:
            lines += [(t, f"[{hms(t)}] {text}") for t, text in transcribe_audio(path)]
        except Exception as err:  # noqa: BLE001 - a broken file or model is a message, not a crash
            messages.append(f"Speech-to-text failed: {type(err).__name__}: {err}")
    video = path.suffix.lower() in (".mp4", ".mov", ".webm")
    if video:
        missing = [t for t in ("ffmpeg", "tesseract") if not status[t]]
        if missing:
            messages.append("Screen text (OCR) is skipped; not installed: "
                            + "; ".join(f"{t} ({INSTALL[t]})" for t in missing))
        else:
            try:
                lines += [(t, f"[{hms(t)}] screen: {text}") for t, text in ocr_frames(path)]
            except Exception as err:  # noqa: BLE001
                messages.append(f"Screen text (OCR) failed: {type(err).__name__}: {err}")
    lines.sort(key=lambda x: x[0])
    return "\n".join(x for _, x in lines), messages


def email_text(data: bytes) -> str:
    """Subject, date and body of an e-mail; signatures and quoted replies removed. The sender
    and recipients are not kept."""
    msg = email.message_from_bytes(data, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain",)) if msg.is_multipart() else msg
    text = body.get_content() if body is not None else ""
    kept = []
    for line in str(text).replace("\r\n", "\n").split("\n"):
        if line.strip() in ("--", "-- ") or re.match(r"^(On .+ wrote:|-----Original Message-----|From: )", line.strip()):
            break
        if line.startswith(">"):
            continue
        kept.append(line)
    return _pages_text([f"Subject: {msg.get('subject', '')}\nDate: {msg.get('date', '')}\n" + "\n".join(kept).strip()])


def vsdx_text(data: bytes) -> tuple[str, list[tuple[str, str]]]:
    """Shape texts ([shape N] text) and connectors (from text, to text) of a Visio .vsdx file."""
    shapes: dict[str, str] = {}
    links: list[tuple[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in sorted(n for n in z.namelist() if re.match(r"visio/pages/page\d+\.xml$", n)):
            root = __import__("xml.etree.ElementTree", fromlist=["ElementTree"]).fromstring(z.read(name))
            for el in root.iter():
                if el.tag.endswith("}Shape") and el.get("ID"):
                    text = " ".join("".join(t.itertext()).split() for t in el if t.tag.endswith("}Text"))
                    if text:
                        shapes[el.get("ID")] = text
            ends: dict[str, dict[str, str]] = {}
            for el in root.iter():
                if el.tag.endswith("}Connect"):
                    side = "from" if el.get("FromCell", "").startswith("Begin") else "to"
                    ends.setdefault(el.get("FromSheet"), {})[side] = el.get("ToSheet")
            links += [(shapes[e["from"]], shapes[e["to"]]) for e in ends.values()
                      if e.get("from") in shapes and e.get("to") in shapes]
    order = list(shapes.values())
    return "\n".join(f"[shape {i}] {t}" for i, t in enumerate(order, 1)), links


def _table(file_name: str, data: bytes) -> pd.DataFrame:
    if Path(file_name).suffix.lower() == ".csv":
        return pd.read_csv(io.BytesIO(data), sep=None, engine="python", dtype=str, keep_default_na=False,
                           encoding="utf-8-sig")
    return pd.read_excel(io.BytesIO(data), dtype=str).fillna("")


def policy_text(file_name: str, data: bytes) -> str:
    df = _table(file_name, data)
    return "\n".join(f"[row {i}] " + " | ".join(f"{c}: {v}" for c, v in r.items() if str(v).strip())
                     for i, r in enumerate(df.to_dict("records"), 1))


def extract_source_text(kind: str, file_name: str, data: bytes, path: Path, roles: list[str] | None = None
                        ) -> tuple[str, list[str]]:
    """(anchored text, messages) for any kind (see the module docstring)."""
    ext = Path(file_name).suffix.lower()
    if kind == "sop":
        return document_text(file_name, data), []
    if kind == "transcript":
        return transcript_text(file_name, data, roles), []
    if kind == "video":
        return media_text(path, roles)
    if kind == "notes":
        if ext == ".eml":
            return email_text(data), []
        return document_text(file_name, data), []
    if kind == "model":
        if ext == ".vsdx":
            return vsdx_text(data)[0], []
        from app.core.definition import load_file
        defn, notes = load_file(file_name, data, Path(file_name).stem)
        lines = [f"[shape {i}] {s['name']}" for i, s in enumerate(defn.ordered_steps(), 1)]
        return "\n".join(lines), notes
    if kind == "policy":
        return policy_text(file_name, data), []
    if kind == "sme_answer":
        return data.decode("utf-8", errors="replace"), []
    raise ValueError(f"Unknown source kind '{kind}'.")


def anchors_of(text: str) -> set[str]:
    return set(ANCHOR_RE.findall(text or ""))


def add_source(definition_name: str, kind: str, file_name: str, data: bytes, title: str | None = None,
               added_by: str = "user", recorded_on: str | None = None, speaker_roles: list[str] | None = None,
               reliability: float | None = None, walkthrough: bool = False) -> tuple[dict, list[str]]:
    """Store a source (original file, source.json, text.md). Returns (source, messages)."""
    if kind not in KINDS:
        raise ValueError(f"Unknown source kind '{kind}' (use one of {', '.join(KINDS)}).")
    ext = Path(file_name).suffix.lower()
    if kind != "sme_answer" and ext not in EXTENSIONS[kind]:
        raise ValueError(f"A {KIND_LABELS[kind]} must be one of: {', '.join(EXTENSIONS[kind])}.")
    if len(data) > float(_cfg()["max_file_mb"]) * 1024 * 1024:
        raise ValueError(f"The file is larger than {_cfg()['max_file_mb']} MB.")
    sid = _next_id(definition_name)
    folder = sources_dir(definition_name) / sid
    folder.mkdir(parents=True)
    path = folder / Path(file_name).name
    path.write_bytes(data)
    try:
        text, messages = extract_source_text(kind, file_name, data, path, speaker_roles)
    except Exception as err:  # noqa: BLE001 - an unreadable file is a message for the user
        text, messages = "", [f"The text could not be extracted: {type(err).__name__}: {err}"]
    (folder / "text.md").write_text(text, encoding="utf-8")
    src = {"id": sid, "kind": kind, "title": title or Path(file_name).stem, "file_name": Path(file_name).name,
           "added_by": added_by, "added_at": _now(), "recorded_on": recorded_on,
           "speaker_roles": list(speaker_roles or []),
           "reliability": float(reliability if reliability is not None else default_reliability(kind, walkthrough)),
           "status": "active", "messages": messages}
    _save_source(definition_name, src)
    _write_json(folder / "statements.json", {"statements": []})
    return with_effective_reliability(src), messages


# --------------------------------------------------------------------------- #
# 3. Statements
# --------------------------------------------------------------------------- #
@dataclass
class Statement:
    type: str
    subject: str
    object: str = ""
    value: str = ""
    quote: str = ""
    anchor: str = ""
    source_id: str = ""
    reliability: float = 0.0
    new_name: bool = False
    added_by: str = "ai"
    resolves: str | None = None      # a conflict topic this statement decides (expert answers)
    parent: str = ""                 # step_exists only: the Level 3 step this (sub-)step belongs to, if the document says

    def to_dict(self) -> dict:
        return asdict(self)


def load_statements(definition_name: str, source_id: str) -> list[dict]:
    path = sources_dir(definition_name) / source_id / "statements.json"
    return json.loads(path.read_text(encoding="utf-8")).get("statements", []) if path.exists() else []


def save_statements(definition_name: str, source_id: str, statements: list[dict]) -> None:
    _write_json(sources_dir(definition_name) / source_id / "statements.json", {"statements": statements})


def all_statements(definition_name: str) -> list[dict]:
    """Statements of every ACTIVE source, each with the source's effective reliability."""
    out = []
    for src in list_sources(definition_name, active_only=True):
        for s in load_statements(definition_name, src["id"]):
            out.append({**s, "source_id": src["id"], "reliability": src["effective_reliability"],
                        "source_kind": src["kind"], "source_title": src["title"]})
    return out


def _names(defn: ProcessDefinition | None, activities, stages, apps) -> dict[str, str]:
    """Known names (lower case -> canonical): definition steps and their activities, activities,
    stages and applications."""
    out: dict[str, str] = {}
    for s in (defn.steps if defn else []):
        out[s["name"].lower()] = s["name"]
        for a in s.get("mapped_activities") or []:
            out.setdefault(a.lower(), s["name"])
    for n in [*(activities or []), *(stages or []), *(apps or [])]:
        out.setdefault(str(n).lower(), str(n))
    return out


def canonical(name: str, known: dict[str, str]) -> str | None:
    """A known name for `name`: exact (case-insensitive), else the best word overlap (the
    template synonym matcher, templates.min_token_overlap)."""
    from app.core.templates import tokens
    n = str(name or "").strip()
    if not n:
        return None
    if n.lower() in known:
        return known[n.lower()]
    words = tokens(n)
    best, hit = 0.0, None
    for k, v in known.items():
        kw = tokens(k)
        if kw and words:
            score = len(words & kw) / max(len(words), len(kw))
            if score > best:
                best, hit = score, v
    return hit if best >= float(settings()["templates"]["min_token_overlap"]) else None


def _numbers(text: str) -> list[float]:
    return [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", text or "")]


def validate_statement(st: dict, text: str, known: dict[str, str]) -> tuple[dict | None, str | None]:
    """(normalised statement, None) when valid, else (None, why it was dropped)."""
    from app.core.definition_ai import quote_found
    from app.core.narrative import payload_mask
    t = st.get("type")
    if t not in TYPES:
        return None, f"unknown type '{t}'"
    quote = str(st.get("quote") or "").strip()
    if not quote:
        return None, "no quote"
    if len(quote.split()) > int(_cfg()["max_quote_words"]):
        return None, f"quote longer than {_cfg()['max_quote_words']} words"
    if not quote_found(quote, payload_mask(text)):
        return None, "quote not found in the source text"
    anchor = str(st.get("anchor") or "").strip().strip("[]")
    if anchor not in anchors_of(text):
        return None, f"anchor '{anchor}' not found in the source"
    subject, obj, value = str(st.get("subject") or "").strip(), str(st.get("object") or "").strip(), \
        str(st.get("value") or "").strip()
    if re.search(r"\d", subject + obj):
        return None, "digits in subject or object"
    if t == "sla":
        nums = _numbers(value)
        if not nums:
            return None, "an SLA needs a number of hours in value"
        h, in_quote = nums[0], _numbers(quote)
        if not any(abs(q - x) < 1e-6 for q in in_quote for x in (h, h / 24, h / 168, h * 60)):
            return None, "the SLA value does not appear in the quote"
        value = f"{h:g}"
    elif re.search(r"\d", value):
        return None, "numbers are only allowed in the value of an SLA"
    new = False
    s_c = canonical(subject, known)
    if s_c is None:
        if t != "step_exists":        # only a new step may have an unknown name as its subject
            return None, f"unknown step '{subject}'"
        new, s_c = True, subject
    o_c = obj
    if t in TWO_STEP_TYPES:
        o_c = canonical(obj, known)
        if o_c is None:
            return None, f"unknown step '{obj}'"
    elif t == "system" and obj:
        o_c = canonical(obj, known) or obj
        new = new or o_c == obj and obj.lower() not in known
    parent = ""
    if t == "step_exists" and str(st.get("parent") or "").strip():
        parent = canonical(str(st["parent"]).strip(), known) or ""
        if not parent:
            return None, f"unknown parent step '{st['parent']}'"
    return {"type": t, "subject": s_c, "object": o_c, "value": value, "quote": quote, "anchor": anchor,
            "new_name": new, "parent": parent}, None


def rule_statements(src: dict, text: str, data: bytes, known: dict[str, str]) -> list[dict]:
    """Statements without the AI: policy tables (step, owner / responsible, SLA) and process
    models (steps and their order)."""
    out = []
    if src["kind"] == "policy":
        df = _table(src["file_name"], data)
        cols = {c.lower().strip(): c for c in df.columns}
        pick = lambda *names: next((cols[n] for n in cols if any(x in n for x in names)), None)  # noqa: E731
        step_c = pick("step", "activity", "task", "process")
        role_c = pick("owner", "responsible", "role", "r (")
        sla_c = pick("sla", "hours", "target", "due")
        for i, r in enumerate(df.to_dict("records"), 1):
            step = str(r.get(step_c) or "").strip() if step_c else ""
            if not step:
                continue
            row = f"[row {i}] " + " | ".join(f"{c}: {v}" for c, v in r.items() if str(v).strip())
            if role_c and str(r.get(role_c) or "").strip():
                out.append({"type": "owner", "subject": step, "object": str(r[role_c]).strip(),
                            "quote": " ".join(f"{role_c}: {r[role_c]}".split()), "anchor": f"row {i}", "_row": row})
            if sla_c and _numbers(str(r.get(sla_c) or "")):
                v = str(r[sla_c])
                hours = _numbers(v)[0] * (24 if "day" in (v + sla_c).lower() else 1)
                out.append({"type": "sla", "subject": step, "value": f"{hours:g}",
                            "quote": " ".join(f"{sla_c}: {v}".split()), "anchor": f"row {i}", "_row": row})
            out.append({"type": "step_exists", "subject": step, "quote": " ".join(f"{step_c}: {step}".split()),
                        "anchor": f"row {i}"})
    elif src["kind"] == "model":
        names = [re.sub(r"^\[shape \d+\]\s*", "", ln) for ln in text.split("\n") if ln.startswith("[shape")]
        links = vsdx_text(data)[1] if src["file_name"].lower().endswith(".vsdx") else list(zip(names, names[1:]))
        for i, n in enumerate(names, 1):
            out.append({"type": "step_exists", "subject": n, "quote": n, "anchor": f"shape {i}"})
        for a, b in links:
            if a in names:
                out.append({"type": "order", "subject": a, "object": b, "quote": a,
                            "anchor": f"shape {names.index(a) + 1}"})
    kept = []
    for s in out:
        s.pop("_row", None)
        v, _why = validate_statement(s, text, known)
        if v:
            kept.append({**v, "added_by": "rules"})
    return kept


def extract_statements(definition_name: str, source_id: str, defn: ProcessDefinition | None = None,
                       activities=None, stages=None, apps=None, client=None, job_id: str = "knowledge"
                       ) -> tuple[list[dict], list[str], list[str]]:
    """Statements of one source: (kept, dropped with reasons, messages). Saved to statements.json.

    The AI (only when switched on, or a test `client`) reads the masked text in chunks plus only
    names (activities, stages, applications, definition steps). Policy and model sources also
    work without the AI (fixed rules); other kinds then wait for statements typed by a person.
    """
    from app.core.definition_ai import available, chunks
    from app.core.narrative import PROMPTS, _call, _fill, parse_reply, payload_mask
    from app.core import observability
    src = load_source(definition_name, source_id)
    text = source_text(definition_name, source_id)
    known = _names(defn, activities, stages, apps)
    data = (sources_dir(definition_name) / source_id / src["file_name"]).read_bytes()
    from app.core import privacy
    use_ai = (client is not None and privacy.ai_allowed()[0]) or available()[0]
    kept, dropped, messages = [], [], []
    if not use_ai:
        if src["kind"] in ("policy", "model"):
            kept = rule_statements(src, text, data, known)
            messages.append(f"{len(kept)} statements read from the {KIND_LABELS[src['kind']].lower()} by fixed rules.")
        else:
            messages.append("AI is switched off: add statements for this source by hand.")
    else:
        cfg = settings()["ai"]
        system = (PROMPTS / "knowledge_extractor_system.txt").read_text(encoding="utf-8")
        user_t = (PROMPTS / "knowledge_extractor_user.txt").read_text(encoding="utf-8")
        steps = [s["name"] for s in (defn.steps if defn else [])]
        model = os.getenv("LLM_MODEL") or "unknown-model"
        with observability.trace(job_id, tags=["knowledge-extract", src["kind"]], metadata={"source": source_id},
                                 allow_names=set(known.values()), name="knowledge-extract"):
            prompt, _v = observability.get_prompt(_cfg()["prompt_name"], cfg["prompt_label"],
                                                  [{"role": "system", "content": system},
                                                   {"role": "user", "content": user_t}])
            from app.core import ai_client
            if client is None:
                client = ai_client.session("knowledge_extract")
            size = ai_client.chunk_chars("knowledge_extract", int(_cfg()["max_chunk_chars"]))
            for part in chunks(text, size):
                values = {"types": ", ".join(TYPES), "steps": "\n".join(f"- {s}" for s in steps) or "(none)",
                          "activities": "\n".join(f"- {payload_mask(a)}" for a in activities or []) or "(none)",
                          "stages": "\n".join(f"- {s}" for s in stages or []) or "(none)",
                          "applications": "\n".join(f"- {a}" for a in apps or []) or "(none)",
                          "kind": KIND_LABELS[src["kind"]], "text": payload_mask(part)}
                messages_ = [{"role": m["role"], "content": _fill(m["content"], values)} for m in prompt]
                try:
                    reply = _call(client, model, messages_, cfg["temperature"], {})
                except ai_client.AIUnavailable as err:
                    messages.append(f"AI not available: {err.short()}")
                    break
                except Exception as err:  # noqa: BLE001
                    messages.append(f"The AI call failed: {type(err).__name__}")
                    continue
                out = parse_reply(reply)
                items = out.get("statements") if isinstance(out, dict) else None
                if not isinstance(items, list):
                    messages.append("The AI answer had no statements list.")
                    continue
                for item in items:
                    v, why = validate_statement(item if isinstance(item, dict) else {}, text, known)
                    if v:
                        kept.append({**v, "added_by": "ai", "provider": getattr(client, "provider", None),
                                     "model": getattr(client, "model", None),
                                     "fallback": client.notes() if hasattr(client, "notes") else []})
                    else:
                        dropped.append(f"{(item or {}).get('type', '?')} {(item or {}).get('subject', '')}: {why}")
            observability.score("knowledge_statements_kept_share",
                                len(kept) / (len(kept) + len(dropped)) if kept or dropped else 0)
    manual = [s for s in load_statements(definition_name, source_id) if s.get("added_by") not in ("ai", "rules")]
    save_statements(definition_name, source_id, manual + kept)
    return kept, dropped, messages


def add_manual_statement(definition_name: str, source_id: str, statement: dict, defn: ProcessDefinition | None,
                         activities=None, stages=None, apps=None, added_by: str = "user") -> dict:
    """A statement typed by a person (validated the same way: the quote and anchor must exist)."""
    text = source_text(definition_name, source_id)
    v, why = validate_statement(statement, text, _names(defn, activities, stages, apps))
    if v is None:
        raise ValueError(f"The statement was not added: {why}.")
    v.update(added_by=added_by, resolves=statement.get("resolves"))
    save_statements(definition_name, source_id, load_statements(definition_name, source_id) + [v])
    return v


# --------------------------------------------------------------------------- #
# 4. Merging, belief and conflicts
# --------------------------------------------------------------------------- #
def key_of(st: dict) -> str:
    t, s, o = st["type"], st["subject"], st.get("object") or ""
    if t == "order":
        return f"order:{s}<{o}"
    if t == "loop_allowed":
        return f"loop_allowed:{s}>{o}"
    if t in ("owner", "system"):
        return f"{t}:{s}={o}"
    if t == "sla":
        return f"sla:{s}={st.get('value')}"
    if t in ("mandatory", "optional"):
        return f"mandatory:{s}={'yes' if t == 'mandatory' else 'no'}"
    if t in ("rule", "exception", "pain_point", "workaround"):
        return f"{t}:{s}:{(st.get('value') or st.get('object') or st['quote'])[:60]}"
    return f"{t}:{s}"


def topic_of(key: str) -> str | None:
    """The conflict topic of a key: keys with the same topic but different values disagree."""
    kind, _, rest = key.partition(":")
    if kind == "order":
        a, b = rest.split("<", 1)
        return "order:" + "|".join(sorted((a, b)))
    if kind in ("owner", "system", "sla", "mandatory"):
        return f"{kind}:{rest.split('=', 1)[0]}"
    return None


def belief(reliabilities: list[float]) -> float:
    """1 - Π(1 - r) over agreeing statements, capped at knowledge.max_belief."""
    p = 1.0
    for r in reliabilities:
        p *= 1 - max(0.0, min(1.0, float(r)))
    return round(min(float(_cfg()["max_belief"]), 1 - p), 4)


def merge(statements: list[dict]) -> dict:
    """{"keys": {key: {belief, type, subject, object, value, statements}}, "conflicts": [...]}.

    One reliability per source and key (the same source saying it twice counts once). A
    conflict = two keys of one topic (opposite order, different owner / system / SLA,
    mandatory vs optional). A conflict is resolved only by an expert answer (`resolves`);
    then the other side's statements no longer count.
    """
    resolved: dict[str, str] = {}
    for st in statements:
        if st.get("resolves"):
            resolved[st["resolves"]] = key_of(st)
    groups: dict[str, list[dict]] = {}
    for st in statements:
        k = key_of(st)
        t = topic_of(k)
        if t in resolved and resolved[t] != k and not resolved[t].startswith("exception:"):
            continue
        groups.setdefault(k, []).append(st)
    keys = {}
    for k, sts in groups.items():
        per_source: dict[str, float] = {}
        for st in sts:
            per_source[st["source_id"]] = max(per_source.get(st["source_id"], 0.0), float(st.get("reliability") or 0))
        first = sts[0]
        keys[k] = {"key": k, "belief": belief(list(per_source.values())), "type": first["type"],
                   "subject": first["subject"], "object": first.get("object") or "", "value": first.get("value") or "",
                   "topic": topic_of(k), "statements": sts}
    by_topic: dict[str, list[str]] = {}
    for k, v in keys.items():
        if v["topic"]:
            by_topic.setdefault(v["topic"], []).append(k)
    conflicts = []
    for topic, ks in sorted(by_topic.items()):
        if len(ks) < 2 and topic not in resolved:
            continue
        sides = [{"key": k, "label": describe_key(k), "belief": keys[k]["belief"],
                  "sources": [{"source_id": s["source_id"], "quote": s["quote"], "anchor": s["anchor"],
                               "reliability": s.get("reliability")} for s in keys[k]["statements"]]} for k in sorted(ks)]
        if len(ks) >= 2 or topic in resolved:
            conflicts.append({"id": f"X{len(conflicts) + 1:03d}", "topic": topic, "sides": sides,
                              "status": "resolved" if topic in resolved else "open",
                              "resolution": resolved.get(topic)})
    return {"keys": keys, "conflicts": conflicts}


def describe_key(key: str) -> str:
    kind, _, rest = key.partition(":")
    if kind == "order":
        a, b = rest.split("<", 1)
        return f"{a} before {b}"
    if kind == "loop_allowed":
        a, b = rest.split(">", 1)
        return f"{a} may go back to {b}"
    if kind in ("owner", "system", "sla", "mandatory"):
        s, v = rest.split("=", 1)
        return {"owner": f"{s} is done by {v}", "system": f"{s} is done in {v}", "sla": f"{s} within {v} h",
                "mandatory": f"{s} is {'mandatory' if v == 'yes' else 'optional'}"}[kind]
    return f"{kind.replace('_', ' ')}: {rest}"


def resolve_conflict(definition_name: str, conflict: dict, choice: str, by: str = "user") -> dict:
    """Store a person's decision on a conflict as an expert answer (reliability 0.95).
    `choice` is a side's key, or "variant" (both are true)."""
    if choice != "variant" and choice not in [s["key"] for s in conflict["sides"]]:
        raise ValueError("Pick one of the sides, or 'variant'.")
    label = "Both are true: a variant" if choice == "variant" else describe_key(choice)
    question = "Which is right? " + " / ".join(s["label"] for s in conflict["sides"])
    text = f"[q1] Question: {question}\nAnswer: {label}"
    src, _ = add_source(definition_name, "sme_answer", f"decision_{conflict['id']}.txt", text.encode("utf-8"),
                        title=f"Decision on {conflict['id']}: {conflict['topic']}", added_by=by,
                        reliability=float(_cfg()["resolution_reliability"]))
    if choice == "variant":
        subject = conflict["topic"].split(":", 1)[1].split("|")[0]
        st = {"type": "exception", "subject": subject, "value": "both are valid variants"}
    else:
        kind, _, rest = choice.partition(":")
        if kind == "order":
            a, b = rest.split("<", 1)
            st = {"type": "order", "subject": a, "object": b}
        else:
            subject, v = rest.split("=", 1)
            st = {"type": ("mandatory" if v == "yes" else "optional") if kind == "mandatory" else kind,
                  "subject": subject, "object": v if kind in ("owner", "system") else "",
                  "value": v if kind == "sla" else ""}
    st.update(quote=label, anchor="q1")
    stored = {**{k: st.get(k, "") for k in ("type", "subject", "object", "value", "quote", "anchor")},
              "new_name": False, "added_by": by, "resolves": conflict["topic"]}
    save_statements(definition_name, src["id"], [stored])
    return src


def proposals(defn: ProcessDefinition, merged: dict) -> list[dict]:
    """"Update draft from knowledge": keys with belief >= min_belief that the draft does not yet
    say, as operations (accepted or rejected by a person), each citing its sources."""
    min_b = float(_cfg()["min_belief"])
    by_name = {s["name"]: s for s in defn.steps}
    flows = {(f["from_step"], f["to_step"]) for f in defn.effective_flows()}
    out = []
    for k, v in sorted(merged["keys"].items()):
        if v["belief"] < min_b or any(c["status"] == "open" and c["topic"] == v["topic"] for c in merged["conflicts"]):
            continue
        s = by_name.get(v["subject"])
        cite = sorted({f"{x['source_id']} [{x['anchor']}]" for x in v["statements"]})
        op = None
        if v["type"] == "step_exists" and s is None:
            op = {"op": "add_step", "args": {"name": v["subject"]}}
        elif s is None:
            continue
        elif v["type"] == "system" and (s.get("system") or "").lower() != v["object"].lower():
            op = {"op": "change_system", "args": {"step_id": s["id"], "system": v["object"]}}
        elif v["type"] == "owner" and (s.get("owner_role") or "").lower() != v["object"].lower():
            op = {"op": "change_owner", "args": {"step_id": s["id"], "owner_role": v["object"]}}
        elif v["type"] == "mandatory" and not s.get("mandatory", True):
            op = {"op": "set_mandatory", "args": {"step_id": s["id"]}}
        elif v["type"] == "optional" and s.get("mandatory", True):
            op = {"op": "set_optional", "args": {"step_id": s["id"]}}
        elif v["type"] == "sla" and s.get("sla_hours") != float(v["value"]):
            op = {"op": "change_sla", "args": {"step_id": s["id"], "sla_hours": float(v["value"])}}
        elif v["type"] == "order" and v["object"] in by_name and (s["id"], by_name[v["object"]]["id"]) not in flows:
            op = {"op": "add_flow", "args": {"from_step": s["id"], "to_step": by_name[v["object"]]["id"],
                                             "kind": "next"}}
        if op:
            out.append({**op, "key": k, "belief": v["belief"], "sources": cite,
                        "reason": f"{describe_key(k)} (business belief {100 * v['belief']:.0f}%, sources {', '.join(cite)})",
                        "status": "open"})
    return out


# --------------------------------------------------------------------------- #
# 5. Questions for the experts
# --------------------------------------------------------------------------- #
def questions(defn: ProcessDefinition, merged: dict, evidence: dict | None, combined: dict | None = None,
              activity_shares: dict[str, float] | None = None, kinds: dict[str, str] | None = None) -> list[dict]:
    """Up to knowledge.max_questions questions from fixed templates: open conflicts, business
    statements the data contradicts, frequent activities no source mentions, and elements
    with low belief and no data."""
    out: list[dict] = []
    kinds = kinds or {}

    def add(kind: str, text: str, topic: str) -> None:
        if not any(q["text"] == text for q in out):
            out.append({"id": f"Q{len(out) + 1:02d}", "kind": kind, "text": text, "topic": topic, "answer": ""})

    for c in merged["conflicts"]:
        if c["status"] != "open":
            continue
        a, b = c["sides"][:2]
        ref = lambda side: ", ".join(sorted({s["source_id"] for s in side["sources"]}))  # noqa: E731
        if c["topic"].startswith("order:"):
            x, y = b["label"].split(" before ")
            add("conflict", f"Source {ref(a)} says {a['label']}, {ref(b)} says {y} before {x} — which is right?",
                c["topic"])
        else:
            add("conflict", f"Source {ref(a)} says {a['label']}, {ref(b)} says {b['label']} — which is right?",
                c["topic"])
    claims = (evidence or {}).get("claims") or []
    by_step_name = {s["id"]: s["name"] for s in defn.steps}
    for c in claims:
        if c["verdict"] != "Contradicted" or c["kind"] not in ("mandatory", "order", "system", "sla", "step_exists"):
            continue
        step = by_step_name.get((c.get("refs") or {}).get("step_id") or "")
        key = f"mandatory:{step}=yes" if c["kind"] == "mandatory" else None
        v = merged["keys"].get(key) if key else None
        if c["kind"] == "mandatory" and v:
            src = v["statements"][0]["source_id"]
            what = {"sop": "The SOP", "policy": "The policy"}.get(kinds.get(src, ""), f"Source {src}")
            share = c.get("measures", {}).get("value", c.get("value"))
            add("contradicted", f"{what} says {step} is mandatory, but the data shows it in "
                f"{100 * (share or 0):.0f}% of cases — is it done elsewhere or optional?", key)
    mentioned = {st["subject"] for v in merged["keys"].values() for st in v["statements"]} | \
                {v["object"] for v in merged["keys"].values()}
    for act, share in sorted((activity_shares or {}).items(), key=lambda x: -x[1]):
        if share >= float(settings()["evidence"]["uncovered_activity_share"]) and act not in mentioned and \
                not any(act in (s.get("mapped_activities") or []) and s["name"] in mentioned for s in defn.steps):
            add("undocumented", f"The data shows {act} in {100 * share:.0f}% of cases but no source mentions it — "
                f"what is it and where does it belong?", f"activity:{act}")
    for e in (combined or {}).get("elements") or []:
        if e["status"] == "Unverified" and e["kind"] in ("step", "owner", "system", "sla"):
            add("unverified", f"Nothing confirms that {e['label']} — is this right?", e["key"])
    return out[:int(_cfg()["max_questions"])]


def questions_excel(qs: list[dict]) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame([{"id": q["id"], "question": q["text"], "answer": q.get("answer", ""), "topic": q["topic"]}
                  for q in qs], columns=["id", "question", "answer", "topic"]).to_excel(buf, index=False)
    return buf.getvalue()


def answers_from_excel(data: bytes) -> list[dict]:
    df = pd.read_excel(io.BytesIO(data), dtype=str).fillna("")
    df.columns = [str(c).strip().lower() for c in df.columns]
    if not {"question", "answer"} <= set(df.columns):
        raise ValueError("The answers file needs the columns question and answer.")
    return [{"id": r.get("id", ""), "question": r["question"], "answer": r["answer"].strip(), "topic": r.get("topic", "")}
            for r in df.to_dict("records") if r["answer"].strip()]


def add_answers(definition_name: str, answers: list[dict], by: str = "expert") -> dict | None:
    """Expert answers become one `sme_answer` source ([q1] Question / Answer anchors)."""
    if not answers:
        return None
    text = "\n\n".join(f"[q{i}] Question: {a['question']}\nAnswer: {a['answer']}" for i, a in enumerate(answers, 1))
    src, _ = add_source(definition_name, "sme_answer", "expert_answers.txt", text.encode("utf-8"),
                        title=f"Expert answers ({len(answers)})", added_by=by)
    return src


def pain_points(statements: list[dict]) -> list[dict]:
    """Pain points and workarounds per step, with their quotes and sources."""
    return [{"step": s["subject"], "type": s["type"], "quote": s["quote"], "source_id": s["source_id"],
             "anchor": s["anchor"]} for s in statements if s["type"] in ("pain_point", "workaround")]


def save_state(folder: Path, name: str, data) -> Path:
    path = folder / name
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return path


def load_state(folder: Path, name: str, default=None):
    path = folder / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
