"""The universal test bench: the no-case stages on every registered dataset (test_datasets/<name>/dataset.yaml).

    python scripts/bench.py [--datasets a,b] [--ai off|fake|online] [--max-rows N] [--scenario sme_rules]

Writes test_datasets/_bench/<date>/bench.json and bench.md and prints the table. A dataset whose files are not
there is listed as "not downloaded — see source_url"; that is never an error. --ai online uses the configured
online providers for PUBLIC datasets only (pacing and 429 retries as in ai_client); the default is fake (no
network). Each present dataset runs in its own process, so run time and peak memory are its own.

Exit codes: 0 done (DIFFs are information, not failures), 1 a stage failed with an error.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--datasets", help="comma-separated dataset names (default: all registered)")
    p.add_argument("--ai", choices=["off", "fake", "online"], default="fake")
    p.add_argument("--max-rows", type=int, help="use at most this many rows of each dataset")
    p.add_argument("--scenario", help="a scenario defined in dataset.yaml (scenarios.<name>), e.g. sme_rules")
    p.add_argument("--root", help="the datasets folder (default: test_datasets/)")
    p.add_argument("--out", help="where to write bench.json / bench.md (default: <root>/_bench/<date>)")
    p.add_argument("--one", help=argparse.SUPPRESS)          # child process: run one dataset ...
    p.add_argument("--result", help=argparse.SUPPRESS)       # ... and write its row here
    p.add_argument("--in-process", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def _child(args, root: Path, out: Path) -> int:
    from app.nocase.bench import load_specs, run_dataset
    (spec, folder), = load_specs(root, [args.one])
    row = run_dataset(spec, folder, out / "_work" / spec["name"], args.ai, args.max_rows, scenario=args.scenario)
    Path(args.result).write_text(json.dumps(row, default=str), encoding="utf-8")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    from app.nocase.bench import ROOT as DEFAULT_ROOT, load_specs, markdown, presence, run_dataset, write_report
    root = Path(args.root) if args.root else DEFAULT_ROOT
    out = Path(args.out) if args.out else root / "_bench" / datetime.now().strftime("%Y-%m-%d")
    if args.one:
        return _child(args, root, out)
    names = [n.strip() for n in args.datasets.split(",")] if args.datasets else None
    specs = load_specs(root, names)
    if not specs:
        print(f"No dataset.yaml found under {root}" + (f" for {', '.join(names)}" if names else ""))
        return 0
    rows = []
    for spec, folder in specs:
        ok, why = presence(spec, folder)
        if not ok:
            rows.append({"dataset": spec["name"], "present": False, "status": why,
                         "source_url": spec.get("source_url")})
            print(f"{spec['name']}: {why}")
            continue
        print(f"{spec['name']}: running ...", flush=True)
        if args.in_process:
            rows.append(run_dataset(spec, folder, out / "_work" / spec["name"], args.ai, args.max_rows,
                                    scenario=args.scenario))
            continue
        result = out / "_work" / f"{spec['name']}.json"
        result.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(Path(__file__).resolve()), "--one", spec["name"], "--result", str(result),
               "--ai", args.ai, "--root", str(root), "--out", str(out)]
        if args.max_rows:
            cmd += ["--max-rows", str(args.max_rows)]
        if args.scenario:
            cmd += ["--scenario", args.scenario]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not result.exists():
            rows.append({"dataset": spec["name"], "present": True,
                         "status": f"the bench process failed: {(proc.stderr or proc.stdout).strip()[-400:]}"})
            continue
        rows.append(json.loads(result.read_text(encoding="utf-8")))
    j, m = write_report(rows, out)
    print()
    print(markdown(rows))
    for r in rows:                          # how each step is done (N4b), per dataset
        if r.get("inside_step"):
            print(f"Inside each step — {r['dataset']}:")
            for line in r["inside_step"]:
                print(f"  {line}")
    print(f"Written: {j}\n         {m}")
    failed = any(r.get("status") in ("stage failed",) or str(r.get("status", "")).startswith("the bench process")
                 for r in rows)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
