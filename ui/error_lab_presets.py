"""Error Lab core (no Streamlit): preset errors, change tracking, statement totals, coverage map and scoring.

Shared by the Error Lab page (ui/error_lab.py) and tools/error_injection_test.py.

Everything here works on the parser's output (`cc_audit.parser.extract`), i.e. after PDF extraction and
before masking, normalizing and the rules engine. The engine only ever receives the modified statement
(pipeline.ingest_parsed -> pipeline.reconcile); the list of changes and the judge's expectations stay here
and are used only to score the report afterwards.

A transaction keeps a hidden "_rid" (row id) while it is being edited, so a change can be traced to the
line the engine will number "<month>#<position>" (the normalizer numbers lines by position, from 1).
"""
import copy
import hashlib
import json
import re
import shutil
from datetime import date, timedelta

from cc_audit import calculators as calc
from cc_audit import pipeline
from cc_audit.config import data_dir, rulebook, settings
from cc_audit.ledger import Ledger
from cc_audit.masking import TokenVault
from cc_audit.parser import line_details
from ui import samples

MAX_CHANGES = 5
MAX_ROWS = 60
TXN_COLUMNS = ["date", "description", "amount", "type"]
SUMMARY_PREFIXES = ("FP EMI", "INTEREST ON EMI", "FINANCE CHARGES", "FORGN CURR MARKUP", "IGST")
BOXES = {"payments": ("payment",), "credits_refunds_emi_transfers": ("refund", "emi_transfer"),
         "purchases_emi_instalments": ("purchase", "emi_principal"),
         "fees_interest_gst": ("fee", "emi_interest", "finance_charge", "forex_markup", "gst")}
GST_BASE = ("fee", "emi_interest", "finance_charge", "forex_markup")
MANDATORY = ("gst", "emi_principal", "emi_interest", "fee", "forex_markup", "finance_charge")

# Header fields a judge may edit (statement date and period stay fixed: they decide the month and its history).
HEADER_FIELDS = [
    ("account_summary", "previous_balance", "Previous balance"),
    ("account_summary", "payments", "Payments (summary box)"),
    ("account_summary", "credits_refunds_emi_transfers", "Credits, refunds, EMI transfers (summary box)"),
    ("account_summary", "purchases_emi_instalments", "Purchases + EMI instalments (summary box)"),
    ("account_summary", "fees_interest_gst", "Fees + interest + GST (summary box)"),
    ("statement", "total_amount_due", "Total amount due"),
    ("statement", "minimum_amount_due", "Minimum amount due"),
    ("statement", "payment_due_date", "Payment due date"),
    ("statement", "credit_limit", "Credit limit"),
    ("statement", "available_credit", "Available credit"),
    ("statement", "available_cash_limit", "Available cash limit"),
]
READ_ONLY_FIELDS = [("statement", "statement_date", "Statement date"), ("statement", "period_from", "Period from"),
                    ("statement", "period_to", "Period to")]

# ---------------------------------------------------------------- coverage map (which rules look at what)
# Built from the rule texts in config/rulebook.json; every ID is checked against the rulebook (see validate()).
FIELD_RULES = {
    "total_amount_due": ["C10", "F14"], "minimum_amount_due": ["F14"], "payment_due_date": ["F15"],
    "credit_limit": ["F08", "F14"], "previous_balance": ["C10"], "payments": ["C10"],
    "credits_refunds_emi_transfers": ["C10"], "purchases_emi_instalments": ["C10"], "fees_interest_gst": ["C10"],
    "available_credit": [], "available_cash_limit": [],
}
AMOUNT_RULES = {
    "purchase": ["C01", "C07", "C08", "C12", "F08"], "payment": ["F06", "F01", "F02"], "refund": ["C09"],
    "emi_transfer": [], "gst": ["F13"], "finance_charge": ["F01", "F02", "F03"], "forex_markup": ["F10", "F11"],
    "emi_principal": ["C04", "C05", "C06"], "emi_interest": ["C04", "C06"],
}
DATE_RULES = {"purchase": ["C01", "C07", "C08"], "payment": ["F06", "F01", "F02"], "refund": ["C09"], "fee": ["C01", "F12"]}
DESCRIPTION_RULES = {"purchase": ["C01", "C07", "C08", "C12"], "refund": ["C09"]}
DELETE_RULES = {"payment": ["F06", "F01", "F02"], "emi_principal": ["C02"], "refund": ["C09"], "gst": ["F13"],
                "finance_charge": ["F01", "F02"]}

NOT_COVERED_MESSAGE = ("No rule in this card's policy (MITC) checks this. Our sample bank's rulebook is a dummy built "
                       "from SBI's MITC; with a real bank's full policy loaded, a rule for this would be added as "
                       "config, not code.")


def fee_rule(description):
    """The rule a fee line falls under: its fee-catalogue rule in settings.json, else C11 (unrecognised fee)."""
    for f in settings()["fee_lines"]:
        if re.search(f["pattern"], description or ""):
            return f["rule"]
    return "C11"


def covered_rules(category, column, description=None, foreign=False):
    if category == "fee":
        rules = [fee_rule(description), "F13"] if column in ("amount", "description", "added", "type") else DATE_RULES["fee"]
    elif column == "date":
        rules = DATE_RULES.get(category, [])
    elif column == "description":
        rules = DESCRIPTION_RULES.get(category, [])
    elif column == "deleted":
        rules = DELETE_RULES.get(category, [])
    else:
        rules = AMOUNT_RULES.get(category, [])
    if category == "purchase" and foreign and column in ("amount", "added"):
        rules = rules + ["F10"]
    return list(dict.fromkeys(rules))


def validate():
    """Every rule ID used by the presets and the coverage map exists in config/rulebook.json."""
    known = set(rulebook().rules) | set(rulebook().not_errors)
    used = {r for p in PRESETS for r in p["rules"]}
    used |= {r for m in (FIELD_RULES, AMOUNT_RULES, DATE_RULES, DESCRIPTION_RULES, DELETE_RULES) for v in m.values() for r in v}
    used |= {f["rule"] for f in settings()["fee_lines"]}
    unknown = sorted(used - known)
    if unknown:
        raise ValueError(f"rule IDs not in config/rulebook.json: {unknown}")


# ---------------------------------------------------------------- working copy of a parsed statement

def prepare(raw):
    """A working copy with a row id on every transaction."""
    work = copy.deepcopy(raw)
    for i, t in enumerate(work["transactions"]):
        t["_rid"] = i
    work["_next_rid"] = len(work["transactions"])
    return work


def new_rid(work):
    work["_next_rid"] += 1
    return work["_next_rid"] - 1


def find_row(work, rid):
    return next((t for t in work["transactions"] if t["_rid"] == rid), None)


def section_for(description, txn_type):
    if txn_type == "M" or (description or "").startswith(SUMMARY_PREFIXES) or fee_rule(description) != "C11" \
            or re.search(r"\bFEES?\b|\bCHARGES?\b|SURCHARGE|\bTAX\b|\bLEVY\b", (description or "").upper()):
        return "summary"
    return "card_transactions"


def make_line(work, txn_date, description, amount, txn_type, section=None, after=None):
    """Add a line exactly as the parser would have read it (category, tax and foreign amount derived by the parser)."""
    section = section or section_for(description, txn_type)
    row = {"date": txn_date, "description": description, "amount": calc.r2(amount), "type": txn_type,
           **line_details(description, section, txn_type), "_rid": new_rid(work)}
    rows = work["transactions"]
    pos = next((i + 1 for i, t in enumerate(rows) if t["_rid"] == after), None) if after is not None else None
    if pos is None:                                   # summary lines go with the summary lines, others at the end
        pos = max((i + 1 for i, t in enumerate(rows) if t.get("section") == "summary"), default=0) \
            if section == "summary" else len(rows)
    rows.insert(pos, row)
    return row


def set_cell(row, column, value):
    row[column] = value
    if column in ("description", "type"):
        for k in ("tax", "foreign_amount", "foreign_currency"):
            row.pop(k, None)
        row.update(line_details(row["description"], row.get("section") or section_for(row["description"], row["type"]),
                                row["type"]))


def header_value(work, field):
    part = next(p for p, f, _ in HEADER_FIELDS + READ_ONLY_FIELDS if f == field)
    return work[part][field]


def set_header(work, field, value):
    part = next(p for p, f, _ in HEADER_FIELDS if f == field)
    work[part][field] = value


def fee_text(name, amount):
    return f"{name} (EXCL TAX {calc.gst(amount, rulebook().params['gst_rate']):.2f})"


def lines_of(work, *cats):
    return [t for t in work["transactions"] if t["category"] in cats]


def in_period(work, days_before_end):
    return (date.fromisoformat(work["statement"]["statement_date"]) - timedelta(days=days_before_end)).isoformat()


# ---------------------------------------------------------------- preset errors

def p_late_fee_on_time(work, rng, ctx):
    fee = calc.late_fee(ctx["prev"]["statement"]["total_amount_due"], rulebook().slabs) or 500
    row = make_line(work, in_period(work, rng.randint(3, 9)), fee_text("LATE PAYMENT FEE", fee), fee, "D")
    return {"rid": row["_rid"], "note": f"late fee {fee} added although the minimum due was paid on time"}


def p_late_fee_wrong_slab(work, rng, ctx):
    prev = ctx["prev"]["statement"]
    grace_end = date.fromisoformat(prev["payment_due_date"]) + timedelta(days=rulebook().params["late_fee_grace_days"])
    for t in lines_of(work, "payment"):            # pay after the grace period, so a late fee is due
        t["date"] = (grace_end + timedelta(days=2)).isoformat()
    right = calc.late_fee(prev["total_amount_due"], rulebook().slabs)
    wrong = rng.choice([s["fee"] for s in rulebook().slabs if s["fee"] and abs(s["fee"] - right) > 1])
    row = make_line(work, (grace_end + timedelta(days=3)).isoformat(), fee_text("LATE PAYMENT FEE", wrong), wrong, "D")
    return {"rid": row["_rid"], "note": f"payment moved after the grace period; late fee {wrong} instead of the slab's {right}"}


def p_gst_wrong(work, rng, ctx):
    row = lines_of(work, "gst")[0]
    old = row["amount"]
    set_cell(row, "amount", calc.r2(old * rng.uniform(1.3, 1.8) + 25))
    return {"rid": row["_rid"], "note": f"GST {old} -> {row['amount']}", "gst_edited": True}


def p_interest_paid_in_full(work, rng, ctx):
    amount = calc.r2(ctx["prev"]["statement"]["total_amount_due"] * rng.uniform(0.01, 0.0375))
    row = make_line(work, work["statement"]["statement_date"], fee_text("FINANCE CHARGES", amount), amount, "D")
    return {"rid": row["_rid"], "note": f"finance charge {amount} although last bill was paid in full on time"}


def p_interest_wrong(work, rng, ctx):
    prev = ctx["prev"]["statement"]
    pay = max(lines_of(work, "payment"), key=lambda t: t["amount"])
    paid = calc.r2(max(prev["minimum_amount_due"] + 100, pay["amount"] * 0.6))
    carried = prev["total_amount_due"] - paid
    set_cell(pay, "amount", paid)
    amount = calc.r2(carried * rulebook().params["finance_charge_monthly"] * rng.uniform(1.6, 2.4))
    row = make_line(work, work["statement"]["statement_date"], fee_text("FINANCE CHARGES", amount), amount, "D")
    return {"rid": row["_rid"], "note": f"payment cut to {paid} (balance carried); finance charge {amount} instead of the formula"}


def p_cash_fee_no_withdrawal(work, rng, ctx):
    row = make_line(work, in_period(work, rng.randint(3, 9)), fee_text("CASH ADVANCE FEE", 500), 500, "D")
    return {"rid": row["_rid"], "note": "cash advance fee with no cash withdrawal on the statement"}


def p_cash_fee_wrong(work, rng, ctx):
    amount = rng.choice([5000, 8000, 12000])
    when = in_period(work, rng.randint(5, 12))
    make_line(work, when, "ATM WDL CASH WITHDRAWAL MUMBAI", amount, "D", section="card_transactions")
    p = rulebook().params
    right = max(amount * p["cash_advance_fee_rate"], p["cash_advance_fee_min"])
    wrong = calc.r2(right + rng.choice([150, 250, 400]))
    row = make_line(work, when, fee_text("CASH ADVANCE FEE", wrong), wrong, "D")
    return {"rid": row["_rid"], "note": f"cash withdrawal {amount}; fee {wrong} instead of {calc.r2(right)}"}


def p_overlimit_within_limit(work, rng, ctx):
    fee = rulebook().params["overlimit_fee_min"]
    row = make_line(work, in_period(work, rng.randint(3, 9)), fee_text("OVER LIMIT FEE", fee), fee, "D")
    return {"rid": row["_rid"], "note": f"over-limit fee {fee} although the balance stayed within the limit"}


def p_forex_wrong(work, rng, ctx):
    row = lines_of(work, "forex_markup")[0]
    old = row["amount"]
    set_cell(row, "amount", calc.r2(old * rng.uniform(1.4, 1.9)))
    return {"rid": row["_rid"], "note": f"forex markup {old} -> {row['amount']} (no longer 3.5%)"}


def p_duplicate(work, rng, ctx):
    src = rng.choice([t for t in lines_of(work, "purchase") if not t.get("foreign_currency") and t["date"]])
    row = make_line(work, src["date"], src["description"], src["amount"], src["type"], section=src.get("section"),
                    after=src["_rid"])
    return {"rid": row["_rid"], "note": f"{src['description']} {src['amount']} charged twice"}


def p_emi_amount(work, rng, ctx):
    row = lines_of(work, "emi_principal")[0]
    old = row["amount"]
    set_cell(row, "amount", calc.r2(old + rng.choice([250, 500, 750])))
    return {"rid": row["_rid"], "note": f"EMI principal {old} -> {row['amount']}"}


def p_emi_interest(work, rng, ctx):
    row = lines_of(work, "emi_interest")[0]
    old = row["amount"]
    set_cell(row, "amount", calc.r2(old * rng.uniform(1.4, 2.0) + 10))
    return {"rid": row["_rid"], "note": f"EMI interest {old} -> {row['amount']}"}


def p_total_wrong(work, rng, ctx):
    old = work["statement"]["total_amount_due"]
    set_header(work, "total_amount_due", calc.r2(old + rng.choice([500, 1000, 2500])))
    return {"field": "total_amount_due", "note": f"total due {old} -> {work['statement']['total_amount_due']}"}


def p_min_due_wrong(work, rng, ctx):
    old = work["statement"]["minimum_amount_due"]
    set_header(work, "minimum_amount_due", calc.r2(old * rng.uniform(1.2, 1.5) + 100))
    return {"field": "minimum_amount_due", "note": f"minimum due {old} -> {work['statement']['minimum_amount_due']}"}


def p_unknown_fee(work, rng, ctx):
    amount = rng.choice([199, 250, 349])
    row = make_line(work, in_period(work, rng.randint(3, 9)), fee_text("ACCOUNT SERVICE CHARGE", amount), amount, "D")
    return {"rid": row["_rid"], "note": f"unexplained 'account service charge' {amount}"}


def p_due_date_early(work, rng, ctx):
    old = work["statement"]["payment_due_date"]
    set_header(work, "payment_due_date", (date.fromisoformat(old) - timedelta(days=rng.choice([5, 8, 10]))).isoformat())
    return {"field": "payment_due_date", "note": f"due date {old} -> {work['statement']['payment_due_date']}"}


def p_annual_fee_wrong(work, rng, ctx):
    fee = (ctx.get("card_annual_fee") or settings()["card_defaults"]["annual_fee"]) + rng.choice([249, 499])
    row = make_line(work, in_period(work, rng.randint(3, 9)), fee_text("ANNUAL FEE", fee), fee, "D")
    return {"rid": row["_rid"], "note": f"annual fee {fee}, not the card's fee"}


def p_refund_missing(work, rng, ctx):
    raise ValueError("needs a refund promised on an earlier statement")


def has(*cats):
    return lambda work, ctx: all(lines_of(work, c) for c in cats)


def needs_prev(extra=None):
    return lambda work, ctx: ctx.get("prev") is not None and (extra is None or extra(work, ctx))


def paid_in_full(work, ctx):
    prev = ctx["prev"]["statement"]
    due = date.fromisoformat(prev["payment_due_date"])
    paid = sum(t["amount"] for t in lines_of(work, "payment") if t["date"] and date.fromisoformat(t["date"]) <= due)
    return paid >= prev["total_amount_due"] - 1 and not lines_of(work, "finance_charge")


def refund_promised(work, ctx):
    return any("Refund of Rs" in m for s in ctx.get("history", []) for m in s.get("messages", []))


PRESETS = [
    {"id": "late_fee_on_time", "label": "Late fee although the minimum was paid on time", "rules": ["F06"],
     "apply": p_late_fee_on_time, "applies": needs_prev(), "needs": "the previous month's statement"},
    {"id": "late_fee_wrong_slab", "label": "Wrong late fee slab", "rules": ["F06"],
     "apply": p_late_fee_wrong_slab, "applies": needs_prev(has("payment")), "needs": "the previous month's statement"},
    {"id": "gst_wrong", "label": "GST not 18% of fees and interest", "rules": ["F13"],
     "apply": p_gst_wrong, "applies": has("gst"), "needs": "a GST line"},
    {"id": "interest_wrong", "label": "Wrong finance charge (interest)", "rules": ["F01"],
     "apply": p_interest_wrong, "applies": needs_prev(has("payment")), "needs": "the previous month's statement"},
    {"id": "interest_paid_in_full", "label": "Finance charge although the bill was paid in full", "rules": ["F02"],
     "apply": p_interest_paid_in_full, "applies": needs_prev(paid_in_full),
     "needs": "the previous month's statement, paid in full"},
    {"id": "cash_fee_no_withdrawal", "label": "Cash advance fee without a cash withdrawal", "rules": ["F05"],
     "apply": p_cash_fee_no_withdrawal, "applies": lambda w, c: True, "needs": ""},
    {"id": "cash_fee_wrong", "label": "Cash advance fee - wrong amount", "rules": ["F05"],
     "apply": p_cash_fee_wrong, "applies": lambda w, c: True, "needs": ""},
    {"id": "overlimit_within_limit", "label": "Over-limit fee while within the credit limit", "rules": ["F08"],
     "apply": p_overlimit_within_limit, "applies": lambda w, c: True, "needs": ""},
    {"id": "forex_wrong", "label": "Forex markup not 3.5%", "rules": ["F10"],
     "apply": p_forex_wrong, "applies": has("forex_markup"), "needs": "a foreign-currency purchase"},
    {"id": "duplicate", "label": "Duplicate transaction", "rules": ["C01"],
     "apply": p_duplicate, "applies": lambda w, c: any(not t.get("foreign_currency") and t["date"] for t in lines_of(w, "purchase")),
     "needs": "a purchase"},
    {"id": "emi_amount", "label": "EMI amount does not match the plan", "rules": ["C04"],
     "apply": p_emi_amount, "applies": has("emi_principal"), "needs": "an EMI instalment"},
    {"id": "emi_interest", "label": "EMI interest does not match the plan", "rules": ["C04", "C06"],
     "apply": p_emi_interest, "applies": has("emi_principal", "emi_interest"), "needs": "an EMI instalment with interest"},
    {"id": "total_wrong", "label": "Total amount due does not add up", "rules": ["C10"],
     "apply": p_total_wrong, "applies": lambda w, c: True, "needs": ""},
    {"id": "min_due_wrong", "label": "Minimum amount due is wrong", "rules": ["F14"],
     "apply": p_min_due_wrong, "applies": lambda w, c: True, "needs": ""},
    {"id": "unknown_fee", "label": "Unknown / unexplained fee", "rules": ["C11"],
     "apply": p_unknown_fee, "applies": lambda w, c: True, "needs": ""},
    {"id": "due_date_early", "label": "Payment due date too early", "rules": ["F15"],
     "apply": p_due_date_early, "applies": lambda w, c: True, "needs": ""},
    {"id": "annual_fee_wrong", "label": "Annual fee - wrong amount", "rules": ["F12"],
     "apply": p_annual_fee_wrong, "applies": lambda w, c: True, "needs": ""},
    {"id": "refund_missing", "label": "Promised refund missing", "rules": ["C09"],
     "apply": p_refund_missing, "applies": refund_promised,
     "needs": "a refund promised on an earlier statement (none of the clean samples has one)"},
]
PRESET_BY_ID = {p["id"]: p for p in PRESETS}


def apply_preset(work, preset_id, rng, ctx):
    """Apply one preset to the working copy; returns the change record (one change, however many cells it touched)."""
    p = PRESET_BY_ID[preset_id]
    if not p["applies"](work, ctx):
        raise ValueError(f"'{p['label']}' needs {p['needs']}")
    info = p["apply"](work, rng, ctx)
    where = {"kind": "txn", "rid": info["rid"]} if "rid" in info else {"kind": "field", "field": info["field"]}
    return {"source": "preset", "preset": preset_id, "label": p["label"], "where": where, "note": info["note"],
            "rules": list(p["rules"]), "should_flag": True, "gst_edited": info.get("gst_edited", False)}


# ---------------------------------------------------------------- manual edits (diff between two working copies)

def diff(before, after):
    """Change records for what differs between two working copies (header fields and transaction rows)."""
    out = []
    for part, field, label in HEADER_FIELDS:
        old, new = before[part][field], after[part][field]
        if old != new:
            out.append({"source": "manual", "label": f"{label}", "where": {"kind": "field", "field": field},
                        "column": field, "old": old, "new": new, "rules": FIELD_RULES.get(field, []), "should_flag": True})
    old_rows = {t["_rid"]: t for t in before["transactions"]}
    new_rows = {t["_rid"]: t for t in after["transactions"]}
    for rid, t in new_rows.items():
        if rid not in old_rows:
            out.append({"source": "manual", "label": "Added line", "where": {"kind": "txn", "rid": rid}, "column": "added",
                        "old": None, "new": f"{t['date'] or ''} {t['description']} {t['amount']} {t['type'] or ''}".strip(),
                        "rules": covered_rules(t["category"], "added", t["description"], bool(t.get("foreign_currency"))),
                        "should_flag": True, "gst_edited": t["category"] == "gst"})
            continue
        o = old_rows[rid]
        for col in TXN_COLUMNS:
            if o.get(col) != t.get(col):
                out.append({"source": "manual", "label": f"Line {o['description'][:40]}", "where": {"kind": "txn", "rid": rid},
                            "column": col, "old": o.get(col), "new": t.get(col),
                            "rules": covered_rules(t["category"], col, t["description"], bool(t.get("foreign_currency"))),
                            "should_flag": True, "gst_edited": t["category"] == "gst"})
    for rid, o in old_rows.items():
        if rid not in new_rows:
            out.append({"source": "manual", "label": f"Deleted {o['description'][:40]}",
                        "where": {"kind": "deleted", "rid": rid, "category": o["category"]}, "column": "deleted",
                        "old": f"{o['date'] or ''} {o['description']} {o['amount']}".strip(), "new": None,
                        "rules": covered_rules(o["category"], "deleted", o["description"]), "should_flag": True,
                        "gst_edited": o["category"] == "gst"})
    return out


def check_inputs(work, changes):
    """Validation errors for the edited statement (empty list = fine)."""
    errors = []
    if len(changes) > MAX_CHANGES:
        errors.append(f"At most {MAX_CHANGES} changes per run (you have {len(changes)}).")
    if len(work["transactions"]) > MAX_ROWS:
        errors.append(f"At most {MAX_ROWS} transaction lines.")
    for t in work["transactions"]:
        if not isinstance(t["amount"], (int, float)) or t["amount"] != t["amount"] or t["amount"] < 0:
            errors.append(f"Amount must be a number of 0 or more: {t['description'][:40]}")
        if not (t["description"] or "").strip():
            errors.append("Every line needs a description.")
        if t["date"]:
            try:
                date.fromisoformat(t["date"])
            except ValueError:
                errors.append(f"Not a valid date (YYYY-MM-DD): {t['date']}")
        if t["type"] not in ("D", "C", "M", None):
            errors.append(f"Type must be D, C or M: {t['description'][:40]}")
    for part, field, label in HEADER_FIELDS:
        v = work[part][field]
        if field == "payment_due_date":
            try:
                date.fromisoformat(str(v))
            except ValueError:
                errors.append(f"{label} must be a date (YYYY-MM-DD).")
        elif not isinstance(v, (int, float)) or v != v:
            errors.append(f"{label} must be a number.")
    return list(dict.fromkeys(errors))


# ---------------------------------------------------------------- the statement the bank would have printed

def consistent(original, work, keep_gst=True):
    """Carry transaction edits through to GST, the summary boxes, total due and minimum due, as a bank would.

    Header values a judge or preset changed are kept and the transaction deltas are added on top, so a planted
    header error (e.g. total due +1000) stays an error. GST is only adjusted if no change touched a GST line.
    """
    out = copy.deepcopy(work)
    tot = lambda w, cats: sum(t["amount"] for t in w["transactions"] if t["category"] in cats)          # noqa: E731
    gst_rows = lines_of(out, "gst")
    if keep_gst and gst_rows:
        delta_base = tot(out, GST_BASE) - tot(original, GST_BASE)
        if abs(delta_base) > 0.004:
            gst_rows[0]["amount"] = calc.r2(max(0.0, gst_rows[0]["amount"] + calc.gst(delta_base, rulebook().params["gst_rate"])))
    deltas = {box: calc.r2(tot(out, cats) - tot(original, cats)) for box, cats in BOXES.items()}
    for box, dv in deltas.items():
        out["account_summary"][box] = calc.r2(out["account_summary"][box] + dv)
    d_total = calc.r2(deltas["purchases_emi_instalments"] + deltas["fees_interest_gst"]
                      - deltas["payments"] - deltas["credits_refunds_emi_transfers"])
    d_mandatory = calc.r2(tot(out, MANDATORY) - tot(original, MANDATORY))
    s = out["statement"]
    if abs(d_total) > 0.004 or abs(d_mandatory) > 0.004:
        s["total_amount_due"] = calc.r2(s["total_amount_due"] + d_total)
        out["account_summary"]["total_amount_due"] = calc.r2(out["account_summary"]["total_amount_due"] + d_total)
        rate = rulebook().params["mad_remaining_balance_rate"]
        s["minimum_amount_due"] = calc.r2(min(s["total_amount_due"],
                                              max(0.0, s["minimum_amount_due"] + d_mandatory + rate * (d_total - d_mandatory))))
        s["available_credit"] = calc.r2(s["available_credit"] - d_total)
    return out


def finalize(original, work, keep_totals=True, gst_edited=False):
    """The statement handed to the engine: the edited copy (made consistent if asked), without row ids."""
    out = consistent(original, work, keep_gst=not gst_edited) if keep_totals else copy.deepcopy(work)
    rid_to_txn = {}
    month = out["statement"]["statement_date"][:7]
    for i, t in enumerate(out["transactions"], 1):
        rid_to_txn[t.pop("_rid")] = f"{month}#{i:02d}"              # the id the normalizer will give this line
    out.pop("_next_rid", None)
    return out, rid_to_txn


# ---------------------------------------------------------------- running and scoring

def run(file, statement, use_llm=False, llm=None):
    """Audit a modified sample statement in the current data_dir() (a per-visitor folder, never data/).

    History comes from the sample cache (the same masked months a judge's sample run uses); the modified
    month goes through pipeline.ingest_parsed -> pipeline.reconcile like any upload.
    """
    root = data_dir()
    for sub in ("ledger", "reports", "vault"):
        shutil.rmtree(root / sub, ignore_errors=True)
    entries = samples.manifest()
    (root / "vault").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(samples.WORKSPACE / "vault" / "token_map.json", root / "vault" / "token_map.json")
    for f in samples.history_files(file)[:-1]:
        e = entries[f]
        target = root / "ledger" / e["card"] / f"{e['month']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(samples.WORKSPACE / "ledger" / e["card"] / f"{e['month']}.json", target)
    sha = hashlib.sha256(json.dumps(statement, sort_keys=True, default=str).encode()).hexdigest()
    record, _ = pipeline.ingest_parsed(statement, sha, TokenVault(), Ledger())
    report, _, _ = pipeline.reconcile(record["card_id"], record["month"], use_llm=use_llm, llm=llm)
    return record, report


def context(file):
    """What presets may look at besides the statement: the previous month and the history (masked, from the cache)."""
    entries = samples.manifest()
    history = []
    for f in samples.history_files(file)[:-1]:
        e = entries[f]
        history.append(json.loads((samples.WORKSPACE / "ledger" / e["card"] / f"{e['month']}.json").read_text(encoding="utf-8")))
    cur = entries[file]
    prev = history[-1] if history else None
    if prev:                                           # only the immediately preceding cycle counts as "previous"
        from cc_audit.ledger import previous_cycle
        rec = json.loads((samples.WORKSPACE / "ledger" / cur["card"] / f"{cur['month']}.json").read_text(encoding="utf-8"))
        prev = previous_cycle(rec, history)
        card_fee = (rec.get("card_terms") or {}).get("annual_fee")
    else:
        card_fee = None
    return {"prev": prev, "history": history, "card_annual_fee": card_fee}


def alert_key(a):
    return (a["rule_id"], a["title"])


def points_at(change, alert, rid_to_txn):
    """Does this alert point at the changed line or field? (Matched by line id or field, not 'any alert'.)"""
    w = change["where"]
    if w["kind"] == "txn":
        txn = rid_to_txn.get(w["rid"])
        if any(e.get("txn_id") == txn for e in alert.get("evidence", [])):
            return True
        return not alert.get("evidence") and alert["rule_id"] in change["rules"]    # alert with no lines to point at
    if w["kind"] == "field":
        return alert["rule_id"] in FIELD_RULES.get(w["field"], []) or alert["rule_id"] in change["rules"]
    return alert["rule_id"] in change["rules"]                                       # deleted line: by rule


def score(changes, report, baseline_alerts, rid_to_txn):
    """One result row per change, plus the flags no change explains ('related flags')."""
    base = {alert_key(a) for a in baseline_alerts}
    new = [a for a in report["alerts"] if alert_key(a) not in base]
    rows, used = [], set()
    for ch in changes:
        hits = [a for a in new if points_at(ch, a, rid_to_txn)]
        used |= {a["id"] for a in hits}
        if ch["should_flag"]:
            status = "CAUGHT" if hits else ("MISSED" if ch["rules"] else "NOT COVERED BY POLICY")
        else:
            status = "FALSE ALARM" if hits else "CORRECTLY IGNORED"
        reasons = [f"{r}: {report['coverage'][r].get('reason')}" for r in ch["rules"]
                   if report["coverage"].get(r, {}).get("status") == "not_applicable" and report["coverage"][r].get("reason")]
        reasons += [f"{n['rule_id']} (note): {n['title']}" for n in report.get("notes", [])
                    if n["rule_id"] in ch["rules"] and points_at(ch, n, rid_to_txn)]
        rows.append({**ch, "status": status, "flags": [{"id": a["id"], "rule_id": a["rule_id"], "title": a["title"]} for a in hits],
                     "why_missed": reasons if status == "MISSED" else []})
    related = [{"id": a["id"], "rule_id": a["rule_id"], "title": a["title"]} for a in new if a["id"] not in used]
    tally = {k: sum(r["status"] == k for r in rows) for k in
             ("CAUGHT", "MISSED", "NOT COVERED BY POLICY", "CORRECTLY IGNORED", "FALSE ALARM")}
    return {"rows": rows, "related": related, "tally": tally, "changes": len(rows)}
