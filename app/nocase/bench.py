"""The universal test bench: run the no-case stages on every registered dataset (test_datasets/<name>/dataset.yaml).

For each dataset that is present: prepare the input (ZIP extracted once, XES converted once to Parquet in the
dataset folder, a sample by rows or days), run the stages built so far, and record rows in and out, run time,
peak memory, the mode decision with its reason, the capabilities, the stages run or skipped (with reasons),
accuracy against the truth where it can be measured yet, and PASS / DIFF / LATER against `expected`.
A missing dataset is reported ("not downloaded - see source_url"), never an error.

Truth is evaluation only: in-file truth columns are mapped as `eval` (ingest writes them to <job>/eval/), a
truth file is joined into <job>/eval/ by its key. Neither is ever an input.
"""
from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path

import yaml

from app.core.config import PROJECT_ROOT, settings
from app.core.db import connect, quote_ident, sql_literal

ROOT = PROJECT_ROOT / "test_datasets"
BUILT = ("ingest", "profile", "clean", "units")
LATER: dict[str, str] = {}


def _cfg() -> dict:
    return settings()["nocase"]["bench"]


# --------------------------------------------------------------------------- specs
def load_specs(root: Path = ROOT, names: list[str] | None = None) -> list[tuple[dict, Path]]:
    out = []
    for f in sorted(root.glob("*/dataset.yaml")):
        spec = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        spec.setdefault("name", f.parent.name)
        if names and spec["name"] not in names and f.parent.name not in names:
            continue
        out.append((spec, f.parent))
    return out


def _todo(x) -> bool:
    return x is None or (isinstance(x, str) and x.strip().upper().startswith("TODO"))


def presence(spec: dict, folder: Path) -> tuple[bool, str]:
    """(ready to run, why not)."""
    if _todo(spec.get("files")):
        return False, f"not downloaded — see {spec.get('source_url')} (then list the columns and fill in dataset.yaml)"
    missing = [f["path"] for f in spec["files"] if not (folder / f["path"]).exists()]
    if missing:
        return False, f"not downloaded — see {spec.get('source_url')} (missing: {', '.join(missing)})"
    if _todo(spec.get("mapping")):
        return False, "files present, mapping TODO — list the real columns and fill in `mapping` in dataset.yaml"
    return True, ""


def check_spec(spec: dict) -> list[str]:
    """Rules every dataset.yaml must follow: public class, truth never mapped as an input role."""
    out = []
    if spec.get("dataset_class") != "public":
        out.append(f"{spec.get('name')}: bench datasets are public (dataset_class: public)")
    mapping, truth = spec.get("mapping"), spec.get("truth")
    if isinstance(mapping, dict) and isinstance(truth, dict):
        cols = mapping.get("columns") or {}
        for role in ("case_id", "actor", "text", "l1", "l2", "l3", "l4", "l5"):
            if cols.get(role) and cols[role] in (truth.get("columns") or []):
                out.append(f"{spec['name']}: truth column '{cols[role]}' is mapped as {role}")
    return out


# --------------------------------------------------------------------------- input
def _expand(folder: Path, files: list[dict]) -> list[Path]:
    """ZIP extracted once into _extracted/<zip name>/, XES converted once to Parquet next to it."""
    out: list[Path] = []
    for f in files:
        path = folder / f["path"]
        if path.suffix.lower() == ".zip":
            target = folder / "_extracted" / path.stem
            if not target.exists():
                with zipfile.ZipFile(path) as z:
                    z.extractall(target)
            inner = f.get("inner")
            out += [target / inner] if inner else sorted(p for p in target.rglob("*") if p.is_file())
            continue
        out.append(path)
    final = []
    for p in out:
        name = p.name.lower()
        if name.endswith((".xes", ".xes.gz")):
            pq = p.with_name(p.name.split(".")[0] + ".parquet")
            if not pq.exists():
                from scripts.xes_to_parquet import convert
                convert(str(p), str(pq))
            final.append(pq)
        else:
            final.append(p)
    return final


def prepare_input(spec: dict, folder: Path, work: Path, max_rows: int | None) -> tuple[Path, dict]:
    """One Parquet file with the rows to use (sample by rows or days); returns (path, sample info)."""
    from app.nocase.readers import open_nc_source
    files = _expand(folder, spec["files"])
    con = connect()
    work.mkdir(parents=True, exist_ok=True)
    open_nc_source(con, files if len(files) > 1 else files[0], work / "raw")
    total = con.execute("SELECT COUNT(*) FROM src").fetchone()[0]
    sample = spec.get("sample") or {}
    rows = [n for n in (sample.get("rows"), max_rows) if n]
    where, info = "TRUE", {"rows_in_source": int(total)}
    if sample.get("days"):
        ts = quote_ident(spec["mapping"]["columns"]["ts"])
        where = (f"TRY_CAST({ts} AS TIMESTAMP) < (SELECT MIN(TRY_CAST({ts} AS TIMESTAMP)) FROM src) "
                 f"+ INTERVAL {int(sample['days'])} DAY")
        info["days"] = int(sample["days"])
    limit = f"LIMIT {min(rows)}" if rows else ""
    if rows:
        info["rows"] = min(rows)
    target = work / "input.parquet"
    con.execute(f"COPY (SELECT * FROM src WHERE {where} {limit}) TO {sql_literal(target.as_posix())} (FORMAT parquet)")
    info["rows_used"] = int(con.execute(f"SELECT COUNT(*) FROM read_parquet({sql_literal(target.as_posix())})")
                            .fetchone()[0])
    return target, info


def attach_truth_file(spec: dict, folder: Path, job_folder: Path) -> int | None:
    """A separate truth file -> <job>/eval/ (the eval package joins it by its key)."""
    truth = spec.get("truth")
    if not isinstance(truth, dict) or not truth.get("file"):
        return None
    from app.nocase.eval import attach_truth_file as attach
    return attach(folder / truth["file"], truth["join"], truth.get("columns") or [], job_folder)


# --------------------------------------------------------------------------- decisions and checks
def decide_mode(caps: dict) -> tuple[str, str]:
    """A provisional decision from the capability profile (N2 / N2b replace it with the case-link test):
    case_ready when a mapped case_id passes the case checks; partial when ID shapes cover enough events to try
    the case-link test; else no_case. Never invents a case."""
    case = caps.get("case_id")
    if case and case["verdict"] == "usable":
        return "case_ready", "the mapped case_id passes the case checks"
    share = (caps.get("id_shapes") or {}).get("any", {}).get("share", 0.0)
    need = float(_cfg()["partial_min_id_share"])
    counters = [c["column"] for c in caps.get("counters") or []]
    why = []
    if case:
        why.append(f"the mapped case_id is {case['verdict'].replace('_', ' ')}")
    if counters:
        why.append(f"{', '.join(counters)}: an event counter, not a case")
    if share >= need:
        return "partial", "; ".join(why + [f"ID shapes in {100 * share:.1f}% of events: worth a case-link test"])
    why.append(f"ID shapes in only {100 * share:.1f}% of events (at least {100 * need:.0f}% needed for a case link)")
    return "no_case", "; ".join(why)


def compare_expected(expected: dict, mode: str, caps: dict) -> list[dict]:
    out = []
    want = (expected or {}).get("mode")
    if want:
        ok = mode in want.split("_or_") if "_or_" in want else mode == want
        if want == "case_ready_or_partial":
            ok = mode in ("case_ready", "partial")
        out.append({"check": "mode", "result": "PASS" if ok else "DIFF", "reason": f"expected {want}, got {mode}"})
    if (expected or {}).get("counters"):
        found = {c["column"] for c in caps.get("counters") or []}
        miss = [c for c in expected["counters"] if c not in found]
        out.append({"check": "counters", "result": "DIFF" if miss else "PASS",
                    "reason": f"not reported as an event counter: {', '.join(miss)}" if miss else
                    "reported as an event counter, not a case"})
    if (expected or {}).get("id_shape_share"):
        lo, hi = expected["id_shape_share"]
        share = (caps.get("id_shapes") or {}).get("any", {}).get("share", 0.0)
        out.append({"check": "id_shape_share", "result": "PASS" if lo <= share <= hi else "DIFF",
                    "reason": f"{100 * share:.1f}% of events (expected {100 * lo:.0f}–{100 * hi:.0f}%)"})
    if "source_labels_used" in (expected or {}):
        ok = caps["supports"]["source_step_labels"]["possible"] == bool(expected["source_labels_used"])
        out.append({"check": "source_labels_used", "result": "PASS" if ok else "DIFF",
                    "reason": caps["supports"]["source_step_labels"]["reason"]})
    if (expected or {}).get("later"):
        out.append({"check": "later", "result": "LATER", "reason": expected["later"]})
    return out


# --------------------------------------------------------------------------- one dataset
def peak_memory_mb() -> float | None:
    """Peak memory of this process (working set on Windows, max RSS elsewhere)."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            kernel, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
            kernel.GetCurrentProcess.restype = wintypes.HANDLE          # 64-bit handles: declare the types
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return None
            return round(pmc.PeakWorkingSetSize / 2 ** 20, 1)
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:  # noqa: BLE001
        return None


def apply_scenario(spec: dict, scenario: str | None, work: Path) -> tuple[dict, str | None]:
    """A bench scenario from dataset.yaml (scenarios.<name>: {process, pack}): the pack folder (relative to the
    project) is copied into the run's scratch library, so it never touches data/. Returns (spec, note)."""
    if not scenario:
        return spec, None
    sc = (spec.get("scenarios") or {}).get(scenario)
    if not sc:
        return spec, f"scenario {scenario} not defined for this dataset"
    import shutil
    from app.core.config import PROJECT_ROOT
    src = PROJECT_ROOT / sc["pack"]
    dst = work / "data" / "library" / sc["process"]
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return {**spec, "process": sc["process"]}, f"scenario {scenario}: pack {sc['pack']} as process {sc['process']}"


def run_dataset(spec: dict, folder: Path, work: Path, ai: str = "fake", max_rows: int | None = None,
                proxy=None, fake=None, scenario: str | None = None) -> dict:
    """Run every built stage on one dataset; never raises (a failing stage is recorded with its error)."""
    from app.core import jobs
    from app.nocase.ai_proxy import AIProxy
    from app.nocase.stages import run_all
    from app.nocase.canonical import NcMapping
    from app.nocase.ingest import ingest
    name = spec["name"]
    spec, scenario_note = apply_scenario(spec, scenario, work)
    row: dict = {"scenario": scenario_note, "dataset": name, "source_url": spec.get("source_url"), "dataset_class": spec.get("dataset_class"),
                 "ai": ai}
    ok, why = presence(spec, folder)
    if not ok:
        return {**row, "present": False, "status": why}
    problems = check_spec(spec)
    if problems:
        return {**row, "present": True, "status": "dataset.yaml breaks the bench rules: " + "; ".join(problems)}
    proxy = proxy or AIProxy(ai, spec.get("dataset_class") or "client")
    from app.nocase.ai_gateway import Gateway
    gw = Gateway(spec.get("dataset_class") or "client", ai,
                 fake=(fake or (lambda task, messages: "{}")) if ai == "fake" else None)
    row.update(present=True, ai_available=proxy.available, ai_note=proxy.why_not())
    start = time.perf_counter()
    stages: dict[str, dict] = {}
    old_dir = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(work / "data")
    try:
        t = time.perf_counter()
        try:
            src, sample = prepare_input(spec, folder, work, max_rows)
        except Exception as err:  # noqa: BLE001
            return {**row, "status": f"input could not be read: {type(err).__name__}: {err}"}
        row["sample"] = sample
        try:                                   # what the upload page would decide (app/intake/mode_detect.py)
            from app.core.ingest import open_source
            from app.intake.mode_detect import detect
            dcon = connect()
            open_source(dcon, src, work / "detect")
            det = detect(dcon, None, NcMapping.from_dict({**spec["mapping"], "dataset_class": spec.get("dataset_class")}))
            row["intake_decision"] = {"decision": det["decision"], "message": det["message"], "seconds": det["seconds"]}
        except Exception as err:  # noqa: BLE001 - the bench goes on
            row["intake_decision"] = {"decision": "error", "message": f"{type(err).__name__}: {err}"}
        stages["prepare"] = {"status": "ran", "seconds": round(time.perf_counter() - t, 2)}
        mapping = NcMapping.from_dict({**spec["mapping"], "dataset_class": spec.get("dataset_class")})
        t = time.perf_counter()
        try:
            job, scope = ingest(src, mapping, source_name=name)
            job_folder = jobs.job_folder(job["job_id"])
            row["scope"] = scope
            row["truth_rows"] = attach_truth_file(spec, folder, job_folder)
            stages["ingest"] = {"status": "ran", "seconds": round(time.perf_counter() - t, 2),
                                "output": {"in_scope": scope["in_scope"]}}
        except Exception as err:  # noqa: BLE001 - recorded, the bench goes on
            stages["ingest"] = {"status": "failed", "reason": f"{type(err).__name__}: {err}"}
        if stages["ingest"]["status"] == "ran":
            t = time.perf_counter()
            try:
                done = run_all(job_folder, process=spec.get("process"), ai=gw)
                row["capabilities"] = done["profile"]["output"]
                for stage, info in done.items():
                    out = info.get("output") or {}
                    stages[stage] = {"status": info["status"], "reason": info.get("reason"),
                                     "output": {k: v for k, v in out.items() if not isinstance(v, (dict, list))}}
                    if stage == "inside_step" and info["status"] == "ran":
                        row["inside_step"] = out.get("lines")
                    if stage in ("episodes", "case_link", "evidence", "assign", "activities", "measures", "flow",
                                 "confidence", "inside_step") and info["status"] == "ran":
                        row.setdefault("decisions", {})[stage] = out.get("message")
                        if stage == "case_link":
                            row["case_link"] = {"decision": out["decision"], "reasons": out["reasons"],
                                                "strategy": out["strategy"]}
                        if stage == "episodes":
                            row["episode_strategy"] = out["strategy"]
                stages["profile"]["seconds"] = round(time.perf_counter() - t, 2)
            except Exception as err:  # noqa: BLE001
                stages["profile"] = {"status": "failed", "reason": f"{type(err).__name__}: {err}"}
        else:
            for stage in BUILT[1:]:
                stages[stage] = {"status": "skipped", "reason": "ingest did not run"}
        for stage, prompt in LATER.items():
            stages[stage] = {"status": "skipped", "reason": f"not built yet ({prompt})"}
        row["stages"] = stages
        cl_out = (stages.get("clean") or {}).get("output") or {}
        if cl_out:
            row["screens"] = {k: cl_out.get(k) for k in ("screen_empty_share", "screens_distinct", "raw_titles_distinct",
                                                          "fragmentation", "screen_fallback")}
        caps = row.get("capabilities")
        if caps:
            mode, reason = decide_mode(caps)
            if row.get("case_link"):              # the case-link test (N2) decides; the profile was provisional
                cl = row["case_link"]
                mode = {"case": "case_ready"}.get(cl["decision"], cl["decision"])
                reason = "; ".join(cl["reasons"])
            row.update(mode=mode, mode_reason=reason,
                       expected=compare_expected(spec.get("expected") or {}, mode, caps),
                       capabilities_found=[k for k, v in caps["supports"].items() if v["possible"]])
            want = (spec.get("expected") or {}).get("mode")
            row["accuracy"] = {"mode_correct": None if not want else next(
                (e["result"] == "PASS" for e in row["expected"] if e["check"] == "mode"), None),
                **accuracy(job_folder, row.get("episode_strategy"),
                           (spec.get("expected") or {}).get("target_process")),
                }
        s = row.get("scope") or {}
        if s:
            row["scope_adds_up"] = s["rows_in_file"] == s["left_out_by_filter"] + s["unreadable_time"] + s["in_scope"]
        row["status"] = "ok" if all(v["status"] != "failed" for v in stages.values()) else "stage failed"
        row["ai_calls"] = len(proxy.calls) + len(gw.calls)
    finally:
        if old_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = old_dir
        row["seconds"] = round(time.perf_counter() - start, 2)
        row["peak_memory_mb"] = peak_memory_mb()
    return row


def accuracy(job_folder: Path, strategy: str | None, target: str | None) -> dict:
    """Scores against the truth (app/nocase/eval/evaluate.py), where truth exists."""
    from app.nocase.eval.evaluate import evaluate
    try:
        e = evaluate(job_folder, target)
    except Exception as err:  # noqa: BLE001 - the bench goes on
        return {"evaluation_error": f"{type(err).__name__}: {err}"}
    if not e:
        return {"truth": "none"}
    eps = e.get("episodes") or {}
    chosen = eps.get(strategy or "") or {}
    pur = [v.get("purity") for v in eps.values() if v.get("applies") and v.get("purity") is not None]
    out = {"episode_strategy": strategy, "episode_purity": chosen.get("purity"),
           "pick_regret": round(max(pur) - chosen["purity"], 4) if pur and chosen.get("purity") is not None else None,
           "episode_fragmentation": chosen.get("fragmentation_median"),
           "episode_purity_by_strategy": {k: v.get("purity") for k, v in eps.items() if v.get("applies")}}
    if e.get("case_link"):
        out["case_link_precision"] = e["case_link"].get("precision")
        out["case_link_recall"] = e["case_link"].get("recall")
    if e.get("steps"):
        out["step_accuracy"] = e["steps"]["accuracy_events"]
        out["step_unassigned_share"] = e["steps"]["unassigned_share"]
    if e.get("process"):
        out["other_work_given_a_step"] = e["process"]["other_work_given_a_step_share"]
    if e.get("steps_target"):
        t = e["steps_target"]
        out.update(target_correct=t["accuracy_events"], target_unassigned=t["unassigned_share"],
                   target_wrong=t["wrong_share"], target_by_step=t.get("accuracy_by_true_step"))
    if (e.get("flow") or {}).get("direction_right_share") is not None:
        out["flow_direction_right"] = e["flow"]["direction_right_share"]
    return out


# --------------------------------------------------------------------------- report
def _acc(a: dict | None) -> str:
    if not a:
        return "–"
    parts = []
    alt = a.get("episode_purity_by_strategy") or {}
    if a.get("episode_purity") is not None and len(alt) > 1:
        parts.append("episodes " + ", ".join(f"{k} {v:.2f}" + (" (chosen)" if v == a["episode_purity"] and k == a.get(
            "episode_strategy") else "") for k, v in alt.items() if v is not None))
    for key, label in (("episode_purity", "episode purity"), ("pick_regret", "pick regret"),
                       ("flow_direction_right", "flow direction right"), ("case_link_precision", "link precision"),
                       ("case_link_recall", "link recall"), ("step_accuracy", "step accuracy"),
                       ("step_unassigned_share", "unassigned"), ("target_correct", "target correct"),
                       ("target_unassigned", "target unassigned"), ("target_wrong", "target wrong"),
                       ("other_work_given_a_step", "other work stepped")):
        if a.get(key) is not None:
            parts.append(f"{label} {a[key]:.2f}")
    return ", ".join(parts) or ("no truth" if a.get("truth") == "none" else "–")


def markdown(rows: list[dict]) -> str:
    head = ("| Dataset | Rows in → in scope | Upload decision | Mode (reason) | Expected | Accuracy | Capabilities | "
            "Stages run / skipped | Time s | Peak MB |\n|---|---|---|---|---|---|---|---|---|---|")
    lines = [head]
    for r in rows:
        if not r.get("present") or not r.get("stages"):
            lines.append(f"| {r['dataset']} | – | – | {r['status']} | – | – | – | – | – | – |")
            continue
        s = r.get("scope") or {}
        ran = [k for k, v in r["stages"].items() if v["status"] == "ran" and k != "prepare"]
        failed = [k for k, v in r["stages"].items() if v["status"] == "failed"]
        skipped = [k for k, v in r["stages"].items() if v["status"] == "skipped"]
        exp = ", ".join(f"{e['check']} {e['result']}" for e in r.get("expected") or []) or "–"
        stage_text = f"ran {', '.join(ran)}" + (f"; FAILED {', '.join(failed)}" if failed else "") + \
            (f"; skipped {len(skipped)} (later prompts)" if skipped else "")
        sc = r.get("screens") or {}
        scr = (f"; screens: {100 * (sc.get('screen_empty_share') or 0):.1f}% empty, {sc.get('screens_distinct')} distinct, "
               f"fragmentation {sc.get('fragmentation')}") if sc else ""
        lines.append(f"| {r['dataset']}{' (' + r['scenario'].split(':')[0] + ')' if r.get('scenario') else ''} | "
                     f"{s.get('rows_in_file', 0):,} → {s.get('in_scope', 0):,} | "
                     f"{(r.get('intake_decision') or {}).get('decision', '–')} | "
                     f"{r.get('mode', '–')} ({r.get('mode_reason', '')}) | {exp} | {_acc(r.get('accuracy'))}{scr} | "
                     f"{', '.join(r.get('capabilities_found') or []) or '–'} | {stage_text} | {r['seconds']} | "
                     f"{r.get('peak_memory_mb') or '–'} |")
    good = [r["dataset"] for r in rows if r.get("expected") and all(e["result"] != "DIFF" for e in r["expected"])]
    weak = [r["dataset"] for r in rows if any(e["result"] == "DIFF" for e in r.get("expected") or [])]
    lines += ["", f"Works as expected on: {', '.join(good) or '–'}.", f"Differs from expected on: {', '.join(weak) or '–'}.",
              "Accuracy against truth (episodes, steps, case link, flow) is added as N2–N4 build those stages."]
    return "\n".join(lines) + "\n"


def write_report(rows: list[dict], out: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    j, m = out / "bench.json", out / "bench.md"
    clean_rows = [{k: v for k, v in r.items() if k != "capabilities"} | (
        {"capabilities": {k: r["capabilities"][k] for k in ("supports", "fill", "case_id", "counters", "labels", "time")}}
        if r.get("capabilities") else {}) for r in rows]
    j.write_text(json.dumps(clean_rows, indent=2, default=str), encoding="utf-8")
    m.write_text(markdown(rows), encoding="utf-8")
    return j, m
