"""Evaluate the AI report writer on three known datasets before promoting a prompt.

Builds three synthetic logs in a temporary folder and takes each through the
pipeline (templates only), then asks the AI to write the summary:

    clean   expected quality High        -> AI is called
    dirty   expected quality Medium      -> AI is called with High facts only
    tiny    expected Low / Not feasible  -> no AI call; templates must be used

It prints the six guardrail scores per dataset and the fallback rate.

    python scripts/evaluate_narrative.py                       # local prompt files
    python scripts/evaluate_narrative.py --prompt-version 7    # a Langfuse prompt version

Exit code 1 if an ACCEPTED AI answer scores 0 on numbers_grounded or
citations_valid (that would mean the checks let something wrong through), or if
the tiny dataset reached the AI. With Langfuse on, the inputs are also appended
to the Langfuse dataset "pm-report-writer-eval".
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATASET = "pm-report-writer-eval"
CASES = {
    "clean": ["--cases", "3000"],
    "dirty": ["--cases", "3000", "--ties", "0.15", "--duplicates", "0.03", "--typos", "0.02"],
    "tiny": ["--cases", "50"],
}
COLUMNS = ("schema_valid", "citations_valid", "numbers_grounded", "causal_language_ok", "hedging_ok",
           "fallback_used")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prompt-version", type=int, help="Langfuse prompt version (default: local prompt files)")
    p.add_argument("--task", choices=["narrative", "definition"], default="narrative",
                   help="evaluation target; definition records the definition-review prompt target")
    p.add_argument("--provider", help="force one AI provider (gemini, groq, openrouter or custom) for every call")
    args = p.parse_args(argv)
    if args.provider:
        os.environ["AI_FORCE_PROVIDER"] = args.provider    # read by ai_client.Session
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    work = Path(tempfile.mkdtemp(prefix="pm-eval-"))
    os.environ["DATA_DIR"] = str(work / "data")   # before app modules read it

    from app.core import jobs, observability
    from app.core.facts import load_facts
    from app.core.narrative import ai_status, facts_table, select_facts, write_summary
    from app.core.config import settings
    from scripts.generate_synthetic import main as generate
    from scripts.run_pipeline import main as pipeline

    ok, why = ai_status()
    if not ok:
        print(f"Note: the AI is {why}; AI datasets will fall back to templates.\n")

    rows, failed = [], False
    for name, extra in CASES.items():
        src = work / f"{name}.parquet"
        generate([*extra, "--start", "2024-01-01", "--months", "12", "--out", str(src)])
        before = {j["job_id"] for j in jobs.list_jobs()}
        if pipeline(["--input", str(src), "--auto-map", "--no-ai"]) != 0:
            print(f"{name}: the pipeline failed; skipped.")
            failed = True
            continue
        job = next(j for j in jobs.list_jobs() if j["job_id"] not in before)   # the job just created
        facts = load_facts(jobs.job_folder(job["job_id"]))
        band = job["band"]
        result = write_summary(job["job_id"], facts, band, prompt_version=args.prompt_version)
        rows.append((name, band, result))
        observability.add_dataset_item(
            DATASET, {"band": band, "facts_table": facts_table(select_facts(facts, band,
                                                                              settings()["ai"]["max_facts"]))},
            {"dataset": name})
        if result.source == "ai" and (result.scores.get("numbers_grounded") == 0
                                      or result.scores.get("citations_valid") == 0):
            failed = True
        if name == "tiny" and result.attempts > 0:
            print("tiny: the AI was called although the quality band does not allow it.")
            failed = True
    observability.flush()

    print("\n" + " | ".join(["dataset", "band", "source", "provider / model", "attempts", *COLUMNS]))
    for name, band, r in rows:
        cells = [str(r.scores.get(c, "–")) for c in COLUMNS]
        used = f"{r.provider} / {r.model}" if r.provider else "–"
        print(" | ".join([name, band, r.source, used, str(r.attempts), *cells])
              + ("" if r.source == "ai" or not r.reason else f"   ({r.reason})"))
    tried = [r for _, _, r in rows if r.attempts > 0]
    if tried:
        rate = sum(r.scores.get("fallback_used", 0) for r in tried) / len(tried)
        print(f"\nFallback rate: {rate:.0%} of {len(tried)} AI run(s)")
    else:
        print("\nFallback rate: no AI run (AI not configured or not allowed)")
    print(f"Prompt: {'version ' + str(args.prompt_version) if args.prompt_version else 'local files / production label'}")
    if args.task == "definition":
        print("Definition prompt target selected. Definition drafts and suggestions are scored with "
              "draft_steps_kept_share and suggestion_accepted in Langfuse.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
