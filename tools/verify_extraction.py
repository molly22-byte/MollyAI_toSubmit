#!/usr/bin/env python3
"""Step 1 - reconcile the JSON produced by extract_statement.py against the PDF itself.

Usage:  python tools/verify_extraction.py <pdf or folder> [--json-dir DIR]

For each PDF it runs extract_statement's logic (or reads DIR/<name>.json if given) and checks,
without using the answer key:
  1. Transaction table  - the table is re-read a second, independent way (word positions on the
                          page instead of text lines) and every row must match the JSON row.
  2. Amount coverage    - every money amount printed anywhere in the PDF is present in the JSON,
                          and every amount in the JSON is printed in the PDF.
  3. Date coverage      - every date printed in the PDF is present in the JSON, and vice versa.
  4. Header fields      - totals, dates, limits, summary and rewards appear in the PDF as printed.
  5. Internal maths     - account summary equals the sum of its transaction lines.
  6. Masking            - none of the raw PII values appear in the masked JSON.
"""
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cc_audit.parser import extract  # noqa: E402

AMOUNT = re.compile(r"(?<![\d,.])\d{1,3}(?:,\d{2,3})+(?:\.\d+)?(?![\d,])|(?<![\d,.])\d+\.\d{2}(?!\d)")
DATE = re.compile(r"\b(\d{2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{2})\b")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def amount_value(text):
    return round(float(text.replace(",", "")), 2)


def walk(obj):
    """Yield every scalar inside a JSON object, with its key."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from walk(v)
            else:
                yield k, v
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v) if isinstance(v, (dict, list)) else [(None, v)]


def table_by_position(pdf):
    """Independent read of the transaction table: group words into rows by their y position."""
    rows = []
    for page in pdf.pages:
        words = sorted(page.extract_words(), key=lambda w: w["top"])
        lines, cur = [], []
        for w in words:
            if cur and abs(w["top"] - cur[0]["top"]) > 3:
                lines.append(sorted(cur, key=lambda x: x["x0"]))
                cur = []
            cur.append(w)
        if cur:
            lines.append(sorted(cur, key=lambda x: x["x0"]))
        inside = False
        for ln in lines:
            text = " ".join(w["text"] for w in ln)
            if text.startswith("Transaction Details"):
                inside = True
                continue
            if text.startswith("D = Debit"):
                inside = False
            if not inside or text.startswith(("Date ", "for Statement Period", "TRANSACTIONS FOR")):
                continue
            toks = [w["text"] for w in ln]
            marker = toks.pop() if toks[-1] in ("D", "C", "M") else None
            amount = amount_value(toks.pop())
            d = None
            if len(toks) >= 3 and DATE.fullmatch(" ".join(toks[:3])):
                dd, mon, yy = toks[:3]
                d = date(2000 + int(yy), MONTHS.index(mon) + 1, int(dd)).isoformat()
                toks = toks[3:]
            rows.append((d, " ".join(toks), amount, marker))
    return rows


def verify(pdf_path, json_dir=None):
    problems = []
    raw = extract(pdf_path)                                     # unmasked, only used for checks 6
    if json_dir:
        data = json.loads((Path(json_dir) / (Path(pdf_path).stem + ".json")).read_text())
    else:
        data = extract(pdf_path, mask=True)

    with pdfplumber.open(pdf_path) as pdf:
        pdf_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        table = table_by_position(pdf)

    # 1. transaction table, row by row and in order
    js_rows = [(t["date"], t["description"], t["amount"], t["type"]) for t in data["transactions"]]
    if len(table) != len(js_rows):
        problems.append(f"transaction count: PDF {len(table)} vs JSON {len(js_rows)}")
    for i, (p, j) in enumerate(zip(table, js_rows)):
        if p != j:
            problems.append(f"transaction row {i + 1}: PDF {p} vs JSON {j}")

    # 2. every amount printed in the PDF <-> every amount held in the JSON
    no_slabs = "\n".join(l for l in pdf_text.split("\n") if not l.startswith("Amount due "))   # slab rows: check 4.
    pdf_amounts = Counter(amount_value(a) for a in AMOUNT.findall(no_slabs))
    json_amounts = Counter()
    for key, v in walk({k: v for k, v in data.items() if k != "late_fee_slabs"}):   # slabs are checked in 4.
        if isinstance(v, str):
            json_amounts.update(amount_value(a) for a in AMOUNT.findall(v))
        elif isinstance(v, float) and key not in ("tax", "foreign_amount") and not (key == "processing_fee" and v == 0):
            # tax / foreign_amount are also inside the description; a 0 processing fee is printed as "Nil"
            json_amounts[round(v, 2)] += 1
    for a in pdf_amounts:
        if a not in json_amounts:
            problems.append(f"amount {a} is printed in the PDF but missing from the JSON")
    for a in json_amounts:
        if a not in pdf_amounts:
            problems.append(f"amount {a} is in the JSON but not printed in the PDF")

    # 3. dates
    pdf_dates = {date(2000 + int(y), MONTHS.index(m) + 1, int(d)).isoformat() for d, m, y in DATE.findall(pdf_text)}
    json_dates = {v for _, v in walk(data) if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)}
    json_dates |= {date(2000 + int(y), MONTHS.index(m) + 1, int(d)).isoformat()
                   for _, v in walk(data) if isinstance(v, str) for d, m, y in DATE.findall(v)}
    for d in sorted(pdf_dates - json_dates):
        problems.append(f"date {d} printed in the PDF but missing from the JSON")
    for d in sorted(json_dates - pdf_dates):
        problems.append(f"date {d} in the JSON but not printed in the PDF")

    # 4. header fields must be printed where expected
    s, a, r = data["statement"], data["account_summary"], data["rewards"]
    if any(v is None for v in s.values()) or len(a) != 6 or len(r) != 5:
        problems.append("a header / summary / rewards field is empty")
    fmt = lambda x: f"{x:,.2f}"                                           # noqa: E731
    summary_line = " - ".join(fmt(a[k]) for k in ("previous_balance", "payments", "credits_refunds_emi_transfers"))
    summary_line += f" + {fmt(a['purchases_emi_instalments'])} + {fmt(a['fees_interest_gst'])} = {fmt(a['total_amount_due'])}"
    if summary_line.replace(",", "") not in pdf_text.replace(",", ""):
        problems.append(f"account summary line not found in PDF: {summary_line}")
    rewards_line = f"{r['previous_balance']} + {r['earned']} - {r['redeemed_expired_forfeited']} = {r['closing_balance']} {r['points_expiry']}"
    if rewards_line not in pdf_text:
        problems.append(f"rewards line not found in PDF: {rewards_line}")
    for slab in data["late_fee_slabs"]:
        fee = "Nil" if slab["fee"] == 0 else f"{slab['fee']:,.0f}"
        if f"Amount due {slab['amount_due']} {fee}" not in pdf_text:
            problems.append(f"late fee slab not found in PDF: {slab}")
    for plan in data["emi_plans"]:
        fee = "Nil" if plan["processing_fee"] == 0 else fmt(plan["processing_fee"])
        if f"{plan['plan_ref']} {plan['merchant']}" not in pdf_text or f" {fee} {plan['instalment_no']:02d} of {plan['instalments_total']:02d} {fmt(plan['outstanding'])}" not in pdf_text:
            problems.append(f"EMI plan row not found in PDF: {plan['plan_ref']}")
    if s["statement_no"] not in pdf_text:
        problems.append("statement number not found in PDF")

    # 5. internal maths: summary boxes = sum of the transaction lines
    sums = Counter()
    for t in data["transactions"]:
        bucket = {"payment": "payments", "refund": "credits_refunds_emi_transfers", "emi_transfer": "credits_refunds_emi_transfers",
                  "purchase": "purchases_emi_instalments", "emi_principal": "purchases_emi_instalments"}.get(t["category"], "fees_interest_gst")
        sums[bucket] += t["amount"]
    for k, v in sums.items():
        if abs(v - a[k]) > 0.005:
            problems.append(f"summary {k}: lines add up to {v:.2f} but summary prints {a[k]:.2f}")
    if abs(s["total_amount_due"] - a["total_amount_due"]) > 0.005:
        problems.append("header total differs from summary total")

    # 6. masking: raw PII must not survive in the masked JSON
    out = json.dumps(data, ensure_ascii=False)
    pii = [v for k, v in walk(raw["cardholder"]) if v]
    for v in pii:
        if v in out:
            problems.append(f"PII not masked: {v[:3]}...")
    return data, problems


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    json_dir = None
    if "--json-dir" in args:
        i = args.index("--json-dir")
        json_dir = args[i + 1]
        del args[i:i + 2]
    target = Path(args[0])
    pdfs = sorted(target.glob("*.pdf")) if target.is_dir() else [target]
    failed = 0
    for pdf in pdfs:
        data, problems = verify(pdf, json_dir)
        n = len(data["transactions"])
        print(f"{'PASS' if not problems else 'FAIL'}  {pdf.name}  ({n} transactions)")
        for p in problems:
            print(f"      - {p}")
        failed += bool(problems)
    print(f"\n{len(pdfs) - failed}/{len(pdfs)} statements fully reconciled with their PDF")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
