"""Layer A - Statement parser (runs on the device).

Turns a credit card statement PDF into a structured dict. `extract(path)` returns the RAW
values, including PII; callers decide how to mask them:
  * mask_for_display()  -> star masking, used by extract_statement.py
  * cc_audit.masking    -> reversible tokens (NAME_1, CARD_1 ...), used by the pipeline
"""
import re
from datetime import datetime
from pathlib import Path

import pdfplumber

DATE = r"\d{2} \w{3} \d{2}"   # e.g. 22 May 26
NUM = r"[\d,]+\.\d{2}"        # e.g. 1,50,000.00
LOOSE = r"[\d,]+(?:\.\d+)?"   # header amounts are sometimes printed without decimals, e.g. 1,49,552


# ---------- small helpers ----------

def money(text):
    """'1,50,000.00' -> 150000.0   ('Nil' -> 0.0)"""
    if text is None:
        return None
    return 0.0 if text == "Nil" else float(text.replace(",", ""))


def iso_date(text):
    """'22 May 26' -> '2026-05-22'"""
    return datetime.strptime(text, "%d %b %y").date().isoformat() if text else None


def find(pattern, text):
    """Groups of the first match of `pattern`, or None if it is not on the statement."""
    m = re.search(pattern, text, re.M)
    return m.groups() if m else None


def field(text, label):
    """(text left of label, text right of label) taken from the line that holds `label`."""
    return find(rf"^(.*?) ?{re.escape(label)} (.+)$", text) or ("", "")


def mask(value, keep_last=0):
    """Replace letters and digits with *, keeping the last `keep_last` of them."""
    total = sum(c.isalnum() for c in value)
    out, seen = [], 0
    for c in value:
        if c.isalnum():
            seen += 1
            out.append(c if seen > total - keep_last else "*")
        else:
            out.append(c)
    return "".join(out)


def mask_name(name):
    """'ROHAN V DESHPANDE' -> 'R**** V D********'"""
    return " ".join(w[0] + "*" * (len(w) - 1) for w in name.split())


def mask_email(email):
    """'rohan.deshpande@example.com' -> 'r**************@example.com'"""
    local, _, domain = email.partition("@")
    return local[:1] + "*" * (len(local) - 1) + "@" + domain


# ---------- sections of the statement ----------

def parse_cardholder(text):
    """Identity fields exactly as printed (unmasked)."""
    name, card = field(text, "Credit Card No.")
    address = [field(text, label)[0] for label in ("Statement No.", "Customer ID (CIF)", "PAN (masked)")]
    return {
        "name": name,
        "address": address,
        "mobile": field(text, "Billing Period")[0].replace("Mobile:", "").strip(),
        "email": field(text, "Statement Date")[0].replace("Email:", "").strip(),
        "card_number": card,
        "customer_id": field(text, "Customer ID (CIF)")[1],
        "pan": field(text, "PAN (masked)")[1],
    }


def mask_for_display(cardholder):
    """Star masking for human-readable output: 'R**** V D********', '**** **** **** 4821'."""
    return {
        "name": mask_name(cardholder["name"]),
        "address": [mask(line) for line in cardholder["address"]],
        "mobile": mask(cardholder["mobile"], keep_last=4),
        "email": mask_email(cardholder["email"]),
        "card_number": mask(cardholder["card_number"], keep_last=4),
        "customer_id": mask(cardholder["customer_id"], keep_last=2),
        "pan": mask(cardholder["pan"], keep_last=2),
    }


def parse_statement(text):
    period = find(rf"Billing Period ({DATE}) to ({DATE})", text) or (None, None)
    totals = find(rf"^({LOOSE}) ({LOOSE}) ({DATE}) ({LOOSE}) / ({LOOSE})$", text) or (None,) * 5
    cash = find(rf"Available Cash Limit: \S+ ({LOOSE})", text) or (None,)
    return {
        "statement_no": field(text, "Statement No.")[1] or None,
        "statement_date": iso_date((find(rf"Statement Date ({DATE})", text) or (None,))[0]),
        "period_from": iso_date(period[0]),
        "period_to": iso_date(period[1]),
        "payment_due_date": iso_date(totals[2]),
        "total_amount_due": money(totals[0]),
        "minimum_amount_due": money(totals[1]),
        "credit_limit": money(totals[3]),
        "available_credit": money(totals[4]),
        "available_cash_limit": money(cash[0]),
    }


def parse_account_summary(text):
    keys = ["previous_balance", "payments", "credits_refunds_emi_transfers",
            "purchases_emi_instalments", "fees_interest_gst", "total_amount_due"]
    values = find(rf"^({NUM}) - ({NUM}) - ({NUM}) \+ ({NUM}) \+ ({NUM}) = ({NUM})$", text)
    return dict(zip(keys, map(money, values))) if values else {}


def parse_rewards(text):
    values = find(r"^(\d+) \+ (\d+) - (\d+) = (\d+) (.+)$", text)
    if not values:
        return {}
    return {
        "previous_balance": int(values[0]),
        "earned": int(values[1]),
        "redeemed_expired_forfeited": int(values[2]),
        "closing_balance": int(values[3]),
        "points_expiry": values[4],
    }


def parse_emi_plans(text):
    row = rf"^(EMI-\w+) (.+?) ({DATE}) ({NUM}) (\d+)% (\d+) mo ({NUM}) ({NUM}|Nil) (\d+) of (\d+) ({NUM})$"
    return [
        {
            "plan_ref": m[1],
            "merchant": m[2],
            "booked_on": iso_date(m[3]),
            "amount": money(m[4]),
            "rate_pa_percent": int(m[5]),
            "tenure_months": int(m[6]),
            "emi": money(m[7]),
            "processing_fee": money(m[8]),
            "instalment_no": int(m[9]),
            "instalments_total": int(m[10]),
            "outstanding": money(m[11]),
        }
        for m in re.finditer(row, text, re.M)
    ]


def parse_bullets(lines, heading):
    """Bullet points printed under `heading` (a bullet may wrap over several lines)."""
    bullets, on = [], False
    for line in lines:
        if line.startswith(heading):
            on = True
        elif on and line.startswith("•"):
            bullets.append(line.lstrip("• ").strip())
        elif on and bullets and "|" not in line:
            bullets[-1] += " " + line.strip()
        elif on and bullets:
            break                                   # page footer reached
    return bullets


def category(description, section, marker):
    rules = [("PAYMENT RECEIVED", "payment"), ("FP EMI", "emi_principal"), ("INTEREST ON EMI", "emi_interest"),
             ("FINANCE CHARGES", "finance_charge"), ("FORGN CURR MARKUP", "forex_markup"), ("IGST", "gst"),
             ("TRANSFER TO MERCHANT EMI", "emi_transfer")]
    for prefix, name in rules:
        if description.startswith(prefix):
            return name
    if section == "summary":
        return "fee"
    return "refund" if marker == "C" else "purchase"


def parse_transactions(lines):
    """Every line of the transaction table. D = debit, C = credit, M = EMI instalment."""
    transactions, on, section = [], False, "summary"
    for line in lines:
        if line.startswith("Transaction Details"):
            on = True
        elif line.startswith("D = Debit"):
            on = False
        elif line.startswith("TRANSACTIONS FOR"):
            section = "card_transactions"
        elif on:
            m = re.match(rf"^(?:({DATE}) )?(.+?) ({NUM})(?: ([DCM]))?$", line)
            if not m:
                continue
            description = re.sub(r"\s+", " ", m[2])
            transactions.append({"date": iso_date(m[1]), "description": description, "amount": money(m[3]), "type": m[4],
                                 **line_details(description, section, m[4])})
    return transactions


def line_details(description, section, marker):
    """What the parser derives from one printed line: category, section, tax and foreign amount."""
    out = {"category": category(description, section, marker), "section": section}
    tax = find(rf"\(EXCL TAX ({NUM})\)", description)
    if tax:
        out["tax"] = money(tax[0])
    foreign = find(rf"({NUM}) ([A-Z]{{3}})$", description)
    if foreign:
        out["foreign_amount"], out["foreign_currency"] = money(foreign[0]), foreign[1]
    return out


def parse_fee_schedule(pdf):
    """The 'FEES AND CHARGES' table as {fee: rule}, split at the x position where the second column starts."""
    for page in pdf.pages:
        rows, row, on = [], [], False
        for word in sorted(page.extract_words(), key=lambda w: (round(w["top"]), w["x0"])):
            if row and abs(word["top"] - row[-1]["top"]) > 3:
                text = " ".join(w["text"] for w in row)
                if text.startswith("LATE PAYMENT FEE SLABS"):
                    break
                if on:
                    rows.append(row)
                on = on or text.startswith("FEES AND CHARGES")
                row = []
            row.append(word)
        if not rows:
            continue
        shared = set.intersection(*[{round(w["x0"]) for w in r} for r in rows])
        column = max(shared)
        return {
            " ".join(w["text"] for w in r if round(w["x0"]) < column): " ".join(w["text"] for w in r if round(w["x0"]) >= column)
            for r in rows
        }
    return {}


def parse_late_fee_slabs(text):
    return [{"amount_due": m[1], "fee": money(m[2])} for m in re.finditer(r"^Amount due (.+) (Nil|[\d,]+)$", text, re.M)]


# ---------- entry point ----------

def extract(pdf_path, mask=False, password=None):
    """Parse one statement PDF. mask=True applies star masking to the cardholder block.
    password opens an encrypted PDF in memory; it is not stored anywhere."""
    with pdfplumber.open(pdf_path, password=password) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
        fee_schedule = parse_fee_schedule(pdf)
    text = "\n".join(pages)
    lines = text.split("\n")
    cardholder = parse_cardholder(pages[0])
    return {
        "bank": lines[0].strip(),
        "cardholder": mask_for_display(cardholder) if mask else cardholder,
        "statement": parse_statement(pages[0]),
        "account_summary": parse_account_summary(text),
        "rewards": parse_rewards(text),
        "emi_plans": parse_emi_plans(text),
        "messages": parse_bullets(lines, "IMPORTANT MESSAGES"),
        "transactions": parse_transactions(lines),
        "fee_schedule": fee_schedule,
        "late_fee_slabs": parse_late_fee_slabs(text),
        "how_to_pay": parse_bullets(lines, "HOW TO PAY"),
    }
