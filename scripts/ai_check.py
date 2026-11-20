"""Check the configured AI providers: list their models and send a tiny synthetic request.

    python scripts/ai_check.py

Reads keys from .env at run time and never prints them. Prints one line per provider
(provider · model · ok · latency · reason) and which provider each task route would use.
Exit code 0 when at least one provider works for every route, 1 otherwise.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    from app.core import ai_client, privacy
    print(privacy.status_line())
    working = set()
    print("provider | model | models listed | ok | latency | reason")
    for key, p in ai_client.providers().items():
        ok, why = ai_client.usable(p)
        listed = "–"
        if key != "custom" and ai_client.has_key(p):
            try:
                listed = str(len(ai_client.list_models(key)))
            except Exception as err:  # noqa: BLE001 - a reason, never the key
                listed = f"failed ({type(err).__name__})"
        if not ok:
            print(f"{key} | {p.get('model') or '–'} | {listed} | no | – | {why}")
            continue
        r = ai_client.test_provider(key)
        if r["ok"]:
            working.add(key)
        latency = f"{r['latency_s']:.2f} s" if r.get("latency_s") is not None else "–"
        print(f"{key} | {r['model']} | {listed} | {'yes' if r['ok'] else 'no'} | {latency} | {r['reason'] or ''}")
    print()
    failed = False
    for task in ai_client.TASKS:
        use = next((n for n in ai_client.route(task) if n in working), None)
        failed |= use is None
        print(f"{task}: {use or 'NO WORKING PROVIDER (templates will be used)'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
