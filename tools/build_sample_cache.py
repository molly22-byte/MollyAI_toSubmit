#!/usr/bin/env python3
"""Pre-generate the sample cache the Streamlit app serves to judges (run once before deploying).

Usage:  python tools/build_sample_cache.py [--skip-existing]

Ingests the 15 PDFs in all_15_statements/ (oldest first, as `audit.py run` does) into
sample_cache/workspace, then reconciles every month with the LLM (Nemotron on Nebius, key in
NEBIUS_API_KEY) and saves each masked report under sample_cache/reports/. --skip-existing keeps
reports already cached for the current rulebook and model. Nothing is written to data/.
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cc_audit import pipeline  # noqa: E402
from cc_audit.config import use_data_dir  # noqa: E402
from cc_audit.llm import LLMError, get_llm  # noqa: E402
from ui import samples  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-existing", action="store_true", help="keep reports already in the cache")
    args = ap.parse_args()
    llm = get_llm()
    try:
        llm.chat("Reply with OK.", "ping")
    except LLMError as e:
        sys.exit(f"LLM not reachable, cache not built: {e}")

    work = Path(tempfile.mkdtemp(prefix="sample_cache_"))
    try:
        with use_data_dir(work):
            files = samples.sample_files()
            entries = {}
            for f in files:
                record, _ = pipeline.ingest(samples.SAMPLES / f)
                entries[f] = {"card": record["card_id"], "month": record["month"],
                              "statement_date": record["statement"]["statement_date"],
                              "total_due": record["statement"]["total_amount_due"]}
                print(f"ingested {f} -> {record['card_id']} {record['month']}")
            for f in files:
                e = entries[f]
                if args.skip_existing and samples.cache_path(f).exists():
                    print(f"cached   {f} (kept)")
                    continue
                report, _, _ = pipeline.reconcile(e["card"], e["month"], use_llm=True, llm=llm)
                samples.save_to_cache(f, report)
                ai = sum(a.get("ai", {}).get("source") == "ai" for a in report["alerts"])
                print(f"cached   {f}: {len(report['alerts'])} alert(s), {ai} AI-reviewed, "
                      f"summary {report['ai_summary']['source']} -> {samples.cache_path(f).relative_to(ROOT)}")

        shutil.rmtree(samples.WORKSPACE, ignore_errors=True)
        samples.WORKSPACE.mkdir(parents=True)
        shutil.copytree(work / "ledger", samples.WORKSPACE / "ledger")
        shutil.copytree(work / "vault", samples.WORKSPACE / "vault")
        log = samples.CACHE / "build_audit_log.jsonl"
        with open(log, "a" if args.skip_existing else "w", encoding="utf-8") as out:
            out.write((work / "audit_log.jsonl").read_text(encoding="utf-8"))
        samples.MANIFEST.write_text(json.dumps({"rulebook_version": samples.rulebook_version(),
                                                "model": samples.model_name(), "samples": entries}, indent=2),
                                    encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"done: {len(entries)} samples, manifest {samples.MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
