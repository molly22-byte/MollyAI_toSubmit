#!/usr/bin/env python3
"""Convert reconciliation_rulebook.xlsx into config/rulebook.json (what the engine reads).

Usage:  python tools/build_rulebook.py [reconciliation_rulebook.xlsx] [config/rulebook.json]

Run it again whenever the Excel changes. The 'Source' sheet is deliberately not copied
(the rulebook says it is internal only), and formulas are not needed: the engine computes
EMI schedules itself from the plan inputs.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent


def sheet_rows(ws, first_header):
    """Rows below the header row (the row whose first cell is `first_header`), as dicts."""
    rows = list(ws.iter_rows(values_only=True))
    start = next(i for i, r in enumerate(rows) if r and r[0] == first_header)
    header = rows[start]
    return [dict(zip(header, r)) for r in rows[start + 1:] if any(c is not None for c in r)]


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reconciliation_rulebook.xlsx"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "config" / "rulebook.json"
    wb = openpyxl.load_workbook(src, data_only=False)

    rules = {}
    for r in sheet_rows(wb["Bank fee rules"], "Rule ID"):
        rules[r["Rule ID"]] = {
            "group": "bank_fee",
            "rule": r["Rule"],
            "when_it_applies": r["When it applies"],
            "expected": r["Expected value or formula"],
            "flag_if": r["Flag (raise alert) if"],
            "worked_example": r["Worked example"],
            "mitc_section": r["MITC section"],
        }
    for r in sheet_rows(wb["Common-sense rules"], "Rule ID"):
        rules[r["Rule ID"]] = {
            "group": "common_sense",
            "rule": r["Rule"],
            "when_it_applies": r["What to check"],
            "expected": r["What to check"],
            "flag_if": r["Flag (raise alert) if"],
            "worked_example": None,
            "mitc_section": None,
        }

    not_errors = {
        r["Case ID"]: {"looks_wrong": r["Looks wrong"], "why_allowed": r["Why it is allowed (do not raise an alert)"],
                       "mitc_section": r["MITC section"]}
        for r in sheet_rows(wb["Not errors"], "Case ID")
    }

    parameters = {r["Parameter"]: r["Value"] for r in sheet_rows(wb["Parameters"], "Parameter")}

    slabs = []
    for r in sheet_rows(wb["Late fee slabs"], "Amount due greater than (Rs.)"):
        upper = r["Up to (Rs.)"]
        slabs.append({"above": r["Amount due greater than (Rs.)"],
                      "up_to": upper if isinstance(upper, (int, float)) else None,
                      "fee": r["Late fee (Rs.)"]})

    emi_plans = {}
    for r in sheet_rows(wb["EMI plans"], "Plan ID"):
        if isinstance(r.get("Booking amount (Rs.)"), (int, float)):
            emi_plans[r["Plan ID"]] = {
                "booking_amount": r["Booking amount (Rs.)"],
                "annual_rate": r["Annual interest rate"],
                "tenure_months": r["Tenure (months)"],
                "processing_fee_rate": r["Processing fee rate"],
            }

    assumptions = [r["Details"] for r in sheet_rows(wb["Read me"], "Item") if str(r["Item"]).startswith("Assumption")]

    rulebook = {
        "built_from": src.name,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "assumptions": assumptions,
        "parameters": parameters,
        "late_fee_slabs": slabs,
        "emi_plans": emi_plans,
        "rules": rules,
        "not_errors": not_errors,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rulebook, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(rules)} rules, {len(not_errors)} not-error cases, {len(parameters)} parameters, "
          f"{len(slabs)} late fee slabs, {len(emi_plans)} EMI plans -> {out}")


if __name__ == "__main__":
    main()
