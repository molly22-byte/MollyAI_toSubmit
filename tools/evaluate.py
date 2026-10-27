#!/usr/bin/env python3
"""Score the pipeline against the answer key (the scope document's Step 5 metrics).

Usage:  python tools/evaluate.py [answer_key_DO_NOT_SHARE_WITH_ENGINE.xlsx]

Run `python audit.py run all_15_statements` (with or without --no-llm) first.
Only this evaluation tool reads the answer key; the engine never does.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cc_audit import audit_log  # noqa: E402
from cc_audit.config import data_dir  # noqa: E402
from cc_audit.ledger import Ledger  # noqa: E402
from cc_audit.masking import TokenVault  # noqa: E402


DEFAULT_KEY = ROOT / "answer_key_DO_NOT_SHARE_WITH_ENGINE.xlsx"


def sheet(wb, name):
    it = wb[name].iter_rows(values_only=True)
    header = next(it)
    return [dict(zip(header, r)) for r in it if any(c is not None for c in r)]


def load_answer_key(key_path=DEFAULT_KEY):
    """The answer-key sheets this tool (and the judge view in ui/answer_key_view.py) score against."""
    wb = openpyxl.load_workbook(key_path, read_only=True)
    return {"statements": {r["File"]: r for r in sheet(wb, "Statements")}, "transactions": sheet(wb, "Transactions"),
            "planted": sheet(wb, "Planted errors"), "not_errors": sheet(wb, "Not errors"), "pii": sheet(wb, "PII map")}


def find_statement(statements, statement_date, total_due):
    """The answer-key file whose statement date and printed total due match a report (no token vault needed)."""
    return next((f for f, s in statements.items() if str(s["Statement date"]) == str(statement_date)
                 and abs(float(s["Printed total due"]) - float(total_due)) < 0.005), None)


def score_statement(statement_row, not_error_rows, report):
    """Planted rule IDs vs the rule IDs the report raised, and how each not-error case was handled."""
    planted = {x.strip() for x in (statement_row["Planted rule IDs"] or "").split(",") if x.strip()}
    raised = {a["rule_id"] for a in report["alerts"]}
    notes = {x["rule_id"] for x in report["notes"]}
    return {"planted": planted, "raised": raised, "caught": planted & raised, "false_alarms": raised - planted,
            "missed": planted - raised,
            "not_errors": [(ne["Case ID"], "recognised as allowed" if ne["Case ID"] in notes else "no alert raised")
                           for ne in not_error_rows]}


def main():
    key = load_answer_key(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_KEY)
    statements, key_txns, not_errors, pii = key["statements"], key["transactions"], key["not_errors"], key["pii"]
    ledger, vault = Ledger(), TokenVault()

    # map ledger (CARD_n, month) -> answer-key file name, via the local token vault
    files = {}
    for card in ledger.cards():
        last4 = vault.restore(card)[-4:]
        for month in ledger.months(card):
            files[f"Statement_{last4}_{month}.pdf"] = (card, month)
    missing = sorted(set(statements) - set(files))

    print("== Extraction (field level, against the Transactions sheet)")
    fields_ok = fields_total = 0
    for f, (card, month) in files.items():
        rec = ledger.load(card, month)
        expected = Counter((r["Date"], re.sub(r"\s+", " ", r["Printed description"]), round(float(r["Amount (Rs)"]), 2), r["D/C/M"])
                           for r in key_txns if r["File"] == f)
        got = Counter((t["date"], t["description"], t["amount"], t["type"]) for t in rec["transactions"])
        fields_total += 4 * sum(expected.values())
        fields_ok += 4 * sum((expected & got).values())
        s = statements[f]
        for k_key, value in [("Printed total due", rec["statement"]["total_amount_due"]), ("Printed min due", rec["statement"]["minimum_amount_due"]),
                             ("Previous balance", rec["account_summary"]["previous_balance"])]:
            fields_total += 1
            fields_ok += abs(float(s[k_key]) - value) < 0.005
    print(f"   field accuracy {fields_ok}/{fields_total} = {fields_ok / fields_total:.1%}"
          + (f"   (statements not ingested: {missing})" if missing else ""))

    print("\n== Reconciliation (alerts vs planted errors)")
    tp = fp = fn = 0
    per_rule = Counter()
    for f in sorted(statements):
        if f not in files:
            continue
        card, month = files[f]
        rpath = data_dir() / "reports" / card / f"{month}.json"
        if not rpath.exists():
            print(f"   {f}: no report - run audit.py reconcile")
            continue
        report = json.loads(rpath.read_text())
        score = score_statement(statements[f], [x for x in not_errors if x["File"] == f], report)
        planted, raised = score["planted"], score["raised"]
        t, p, n = score["caught"], score["false_alarms"], score["missed"]
        tp, fp, fn = tp + len(t), fp + len(p), fn + len(n)
        for r in t:
            per_rule[r] += 1
        status = "OK " if not p and not n else "DIFF"
        print(f"   {status} {f}  planted {sorted(planted) or '-'}  raised {sorted(raised) or '-'}"
              + (f"  false alarms {sorted(p)}" if p else "") + (f"  missed {sorted(n)}" if n else ""))
        for case_id, handled in score["not_errors"]:
            print(f"        not-error {case_id}: {handled}")
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    print(f"   precision {precision:.0%} ({tp}/{tp + fp})   recall {recall:.0%} ({tp}/{tp + fn})")

    print("\n== Privacy")
    outbound = []
    for path in list((data_dir() / "ledger").rglob("*.json")) + list((data_dir() / "reports").rglob("*.json")):
        outbound.append(path.read_text(encoding="utf-8"))
    sent = [e.get("payload", "") + e.get("response", "") for e in audit_log.read() if e.get("sent")]
    masked_store = "\n".join(outbound)
    values = [(r["Person"], r["PII type"], r["Value printed on every statement"]) for r in pii]
    leaks = [(p, t) for p, t, v in values if v in masked_store or any(v in s for s in sent)]
    print(f"   masking recall {1 - len(leaks) / len(values):.0%} ({len(values) - len(leaks)}/{len(values)} planted PII values never "
          f"appear in the ledger, masked reports or LLM traffic)")
    if leaks:
        print(f"   leaks: {leaks}")
    descs = [t["description"] for c in ledger.cards() for m in ledger.months(c) for t in ledger.load(c, m)["transactions"]]
    over = sum(bool(re.search(r"\b(?:NAME|ADDR|PHONE|EMAIL|CARD|CUSTID|PAN|STMTNO)_\d+\b", d)) for d in descs)
    print(f"   over-masking {over}/{len(descs)} transaction descriptions contain a token")

    calls = [e for e in audit_log.read() if e["stage"] in ("review_finding", "summarize_month", "answer_question", "draft_dispute")]
    if calls:
        answered = [e for e in calls if e.get("output_check")]
        invented = [e for e in answered if e["output_check"]["invented_numbers"]]
        pii_out = [e for e in answered if e["output_check"]["pii_found"]]
        blocked = [e for e in calls if not e.get("sent")]
        print("\n== LLM layer (from data/audit_log.jsonl)")
        print(f"   calls {len(calls)}, blocked before sending {len(blocked)}, replies with PII {len(pii_out)}, "
              f"replies with invented amounts {len(invented)} ({len(invented) / max(1, len(answered)):.0%}, these fall back to templates)")
        for e in invented[:5]:
            print(f"      {e['stage']} {e['card_id']} {e['month']}: {e['output_check']['invented_numbers']}")
        reviews = []
        for f, (card, month) in files.items():
            rpath = data_dir() / "reports" / card / f"{month}.json"
            if rpath.exists():
                reviews += [a.get("ai", {}).get("verdict") for a in json.loads(rpath.read_text())["alerts"]]
        print(f"   AI rule-check verdicts on alerts: {dict(Counter(reviews))}")


if __name__ == "__main__":
    main()
