"""Does this file have a case ID? Prints the automatic decision (app/intake/mode_detect.py) with its reasons.

    python scripts/detect_mode.py --input "D:/exports/events.parquet" [--mapping config/nocase_mappings/x.yaml]

In an unattended run a "confirm" (or "partial") result means no-case. Column names and statistics only: no
value is printed or stored. scripts/run_pipeline.py is not changed by this.
Exit codes: 0 done, 2 the input could not be read.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", required=True, help="a file, a folder or a glob pattern")
    p.add_argument("--mapping", help="a no-case mapping YAML (its time, actor and filters are used)")
    p.add_argument("--json", help="also write the full decision here")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    import tempfile
    from app.core.db import connect
    from app.intake.mode_detect import detect, unattended
    from app.nocase.canonical import NcMapping
    from app.nocase.readers import open_nc_source
    con = connect()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            open_nc_source(con, args.input, Path(tmp))
            d = detect(con, None, NcMapping.from_yaml(args.mapping) if args.mapping else None)
    except (ValueError, OSError) as err:
        print(f"Cannot read the input: {err}", file=sys.stderr)
        return 2
    print(d["message"])
    print(f"Decision: {d['decision']} (unattended run: {unattended(d)}) · measured on {d['scope_base']} "
          f"({d['scope_rows']:,} of {d['rows']:,} rows) in {d['seconds']:.1f} s")
    for c in d["candidates"][:10]:
        print(f"  {'✅' if c['passed'] else '❌'} {c['column']:<30} coverage {100 * c['coverage']:5.1f}%"
              + (f"  — {c['fail_reason']}" if c["fail_reason"] else ""))
    if args.json:
        Path(args.json).write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
