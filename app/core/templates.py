"""Process templates: business knowledge about the usual order of a process's steps.

The app ships NO templates: a template is not evidence about the user's process. Templates are read only from
the folder in PM_PROCESS_TEMPLATES_DIR (set by the tests to tests/sample_processes/process_templates). Each has stages, and each stage has steps with
synonyms and a "mandatory" flag. `match_template` matches the activity names of a
log to the templates by their words; the best template becomes an order suggestion
(source "template") when it covers at least `templates.min_event_share` of events.
Only activity NAMES are used: no data leaves this module.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

import os

from app.core.config import settings

TEMPLATE_DIR = Path(os.environ["PM_PROCESS_TEMPLATES_DIR"]) if os.environ.get("PM_PROCESS_TEMPLATES_DIR") else None
_STOPWORDS = {"and", "of", "the", "a", "an", "for", "to", "in", "on", "with", "by"}


@lru_cache(maxsize=1)
def load_templates() -> dict[str, dict]:
    """{template key (file name): template} for every YAML file in TEMPLATE_DIR (none in the app)."""
    out = {}
    if not TEMPLATE_DIR:
        return out
    for path in sorted(Path(TEMPLATE_DIR).glob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            out[path.stem] = yaml.safe_load(f)
    return out


def _stem(word: str) -> str:
    """A very small stemmer, enough for step names: tasks -> task, tracking -> track."""
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word


def tokens(text: str) -> set[str]:
    """Lower-case words without punctuation or filler words, lightly stemmed."""
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", str(text).lower()) if w not in _STOPWORDS}


def _step_score(activity: set[str], step: dict) -> float:
    """Best share of a template step's (or synonym's) words that appear in the activity name."""
    best = 0.0
    for phrase in [step["name"], *(step.get("synonyms") or [])]:
        words = tokens(phrase)
        if words:
            # Ties go to the phrase that also covers more of the activity name.
            share = len(activity & words) / len(words) + 0.001 * len(activity & words)
            best = max(best, share)
    return best


def map_activities(template: dict, activity_names) -> dict[str, str | None]:
    """{activity: template step name, or None when no step matches well enough}."""
    min_overlap = float(settings()["templates"]["min_token_overlap"])
    steps = [s for stage in template["stages"] for s in stage["steps"]]
    out: dict[str, str | None] = {}
    for activity in activity_names:
        words = tokens(activity)
        scored = sorted(((_step_score(words, s), i) for i, s in enumerate(steps)), reverse=True)
        score, i = scored[0] if scored else (0.0, -1)
        out[activity] = steps[i]["name"] if score >= min_overlap else None
    return out


def match_template(activity_names) -> tuple[dict | None, dict[str, str | None], float]:
    """The best-matching template, the activity -> step mapping, and the share of events matched.

    `activity_names` is a list of names (each counts once) or {name: events}. The
    template is returned only when it matches at least `templates.min_event_share`
    of events; otherwise (None, {}, best share).
    """
    weights = dict(activity_names) if isinstance(activity_names, dict) else {a: 1 for a in activity_names}
    total = sum(weights.values()) or 1
    best: tuple[float, str | None, dict] = (0.0, None, {})
    for key, template in load_templates().items():
        mapping = map_activities(template, list(weights))
        share = sum(w for a, w in weights.items() if mapping.get(a)) / total
        if share > best[0]:
            best = (share, key, mapping)
    share, key, mapping = best
    if key is None or share < float(settings()["templates"]["min_event_share"]):
        return None, {}, round(share, 4)
    return {"key": key, **load_templates()[key]}, mapping, round(share, 4)


def template_order(template: dict, mapping: dict[str, str | None], share: float, process_name: str):
    """An order (source "template") from a matched template: its stages and step order.

    Activities that matched no step go to Other steps. Activities matched to a
    mandatory template step become mandatory steps (checked by conformance).
    """
    from app.core.reference import Order   # local import avoids a cycle

    position = {}
    mandatory = set()
    for si, stage in enumerate(template["stages"]):
        for ti, step in enumerate(stage["steps"]):
            position[step["name"]] = (si, ti)
            if step.get("mandatory"):
                mandatory.add(step["name"])
    stages = []
    for si, stage in enumerate(template["stages"]):
        steps = sorted((a for a, s in mapping.items() if s and position[s][0] == si),
                       key=lambda a: (position[mapping[a]][1], a))
        if steps:
            stages.append({"name": stage["name"], "steps": steps})
    other = sorted(a for a, s in mapping.items() if not s)
    return Order(process_name, stages, other, "template", False, "",
                 mandatory_steps=[a for a, s in mapping.items() if s in mandatory],
                 template_name=template["name"], template_match_share=share)
