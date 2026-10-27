#!/usr/bin/env python3
"""Privacy-first credit card statement audit - command line (POC).

  python audit.py ingest <pdf or folder>                 add statements to the local ledger
  python audit.py cards                                  list cards and months in the ledger
  python audit.py reconcile <CARD_n> <YYYY-MM|all> [--no-llm]
  python audit.py run <folder> [--no-llm]                ingest a folder, then reconcile every month
  python audit.py ask <CARD_n> <YYYY-MM> "question"      chat over one month (needs the LLM)
  python audit.py dispute <CARD_n> <YYYY-MM> <alert id>  draft a dispute for one alert

Reports go to data/reports/<CARD_n>/<YYYY-MM>.md (your view) and .json (masked, what the AI saw).
The LLM is configured in config/settings.json (default: Nemotron 3 Super on Nebius, key in NEBIUS_API_KEY).
Use another profile from "llm_examples" for one run:  python audit.py --llm nemotron_nebius_nano reconcile CARD_3 all
"""
import argparse
import json
import sys

from cc_audit import pipeline
from cc_audit.config import settings
from cc_audit.ledger import Ledger
from cc_audit.llm import LLMError, get_llm
from cc_audit.masking import TokenVault


def print_report(report, md_path):
    h = report["headline"]
    ai = " (with AI explanations)" if report["llm"] else ""
    print(f"{report['card_id']} {report['month']}: total due {h['total_due']:,.2f}, "
          f"{h['alerts']} alert(s), {len(report['checks_passed'])} checks OK{ai} -> {md_path}")
    for f in report["alerts"]:
        verdict = f" [AI: {f['ai']['verdict']}]" if f.get("ai", {}).get("verdict") else ""
        print(f"   ! {f['id']}  {f['title']}  expected {f['expected']}  actual {f['actual']}{verdict}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", metavar="PROFILE", help="a profile name from llm_examples in config/settings.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest"); p.add_argument("path")
    sub.add_parser("cards")
    p = sub.add_parser("reconcile"); p.add_argument("card"); p.add_argument("month"); p.add_argument("--no-llm", action="store_true")
    p = sub.add_parser("run"); p.add_argument("folder"); p.add_argument("--no-llm", action="store_true")
    p = sub.add_parser("ask"); p.add_argument("card"); p.add_argument("month"); p.add_argument("question")
    p = sub.add_parser("dispute"); p.add_argument("card"); p.add_argument("month"); p.add_argument("alert_id")
    args = ap.parse_args()

    ledger = Ledger()
    try:
        profiles = settings()["llm_examples"]
        if args.llm and args.llm not in profiles:
            sys.exit(f"unknown LLM profile '{args.llm}' - known: {', '.join(k for k in profiles if not k.startswith('_'))}")
        chosen = (lambda: get_llm(profiles[args.llm])) if args.llm else get_llm
        if args.cmd == "ingest":
            from pathlib import Path
            target = Path(args.path)
            results = pipeline.ingest_folder(target) if target.is_dir() else [pipeline.ingest(target)]
            for record, replaced in results:
                print(f"{'updated' if replaced else 'added'}  {record['card_id']} {record['month']}  "
                      f"({len(record['transactions'])} transactions)")

        elif args.cmd == "cards":
            vault = TokenVault()
            for card in ledger.cards():
                print(f"{card}  ({vault.restore(card)})  months: {', '.join(ledger.months(card))}")

        elif args.cmd in ("reconcile", "run"):
            if args.cmd == "run":
                for record, replaced in pipeline.ingest_folder(args.folder):
                    print(f"{'updated' if replaced else 'added'}  {record['card_id']} {record['month']}")
                jobs = [(c, m) for c in ledger.cards() for m in ledger.months(c)]
            else:
                months = ledger.months(args.card) if args.month == "all" else [args.month]
                jobs = [(args.card, m) for m in months]
            llm = None if args.no_llm else chosen()
            for card, month in jobs:
                report, _, md_path = pipeline.reconcile(card, month, use_llm=not args.no_llm, llm=llm)
                print_report(report, md_path)

        elif args.cmd == "ask":
            result = pipeline.ask(args.card, args.month, args.question, llm=chosen())
            print(result["answer"])
            if result.get("evidence"):
                print(f"\nEvidence: {result['evidence']}")
            print(f"[{result['source']}]")

        elif args.cmd == "dispute":
            out = pipeline.dispute(args.card, args.month, args.alert_id, llm=chosen())
            print(json.dumps(out, indent=2, ensure_ascii=False))
    except LLMError as e:
        sys.exit(f"LLM error: {e}\nTip: run with --no-llm for the deterministic checks only.")


if __name__ == "__main__":
    main()
