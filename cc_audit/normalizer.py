"""Layer C - Normalizer: masked statement -> ledger record (one per card per month).

Adds what the reconciliation needs on top of the parser output:
  * card_id (the CARD_n token) and month (YYYY-MM of the statement date)
  * a stable id per transaction ("2026-07#07") so alerts can point at evidence
  * merchant_key: the description without EMI tags, foreign amounts and country code,
    so the same merchant matches across months
  * EMI instalment numbers, the card terms printed in the fee schedule
"""
import re

SCHEMA_VERSION = 1


def merchant_key(description):
    """'NOTION LABS INC SAN FRANCISCO US 10.00 USD' -> 'NOTION LABS INC SAN FRANCISCO'"""
    d = re.sub(r"\s*\(Pay in EMIs\)$", "", description)
    d = re.sub(r"\s+[\d,]+\.\d{2} [A-Z]{3}$", "", d)
    d = re.sub(r"\s+[A-Z]{2}$", "", d)
    return d.strip()


def card_terms(fee_schedule):
    """Annual fee and its waiver threshold as printed on the statement, if present."""
    text = fee_schedule.get("Annual fee", "")
    fee = re.search(r"₹\s*([\d,]+)", text)
    waiver = re.search(r"spends? of ₹\s*([\d,]+)", text)
    return {
        "annual_fee": float(fee.group(1).replace(",", "")) if fee else None,
        "annual_fee_waiver_spend": float(waiver.group(1).replace(",", "")) if waiver else None,
    }


def normalize(masked, source_sha256):
    month = masked["statement"]["statement_date"][:7]
    txns = []
    for i, t in enumerate(masked["transactions"], 1):
        n = dict(t)
        n["id"] = f"{month}#{i:02d}"
        n["merchant_key"] = merchant_key(t["description"]) if t["category"] in ("purchase", "refund") else None
        n["pay_in_emi"] = "(Pay in EMIs)" in t["description"]
        m = re.match(r"FP EMI (\d+)/(\d+)", t["description"])
        if m:
            n["emi_no"], n["emi_of"] = int(m.group(1)), int(m.group(2))
        txns.append(n)
    return {
        "schema_version": SCHEMA_VERSION,
        "card_id": masked["cardholder"]["card_number"],
        "card_network": masked["card_network"],
        "month": month,
        "issuer": masked["bank"],
        "source_sha256": source_sha256,
        "cardholder": masked["cardholder"],
        "statement": masked["statement"],
        "account_summary": masked["account_summary"],
        "rewards": masked["rewards"],
        "emi_plans": masked["emi_plans"],
        "card_terms": card_terms(masked["fee_schedule"]),
        "messages": masked["messages"],
        "transactions": txns,
        "fee_schedule": masked["fee_schedule"],
        "late_fee_slabs": masked["late_fee_slabs"],
        "how_to_pay": masked["how_to_pay"],
    }
