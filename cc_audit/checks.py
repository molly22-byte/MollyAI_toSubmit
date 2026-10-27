"""Layer E - Rules and reconciliation engine (deterministic, runs on the device).

run_checks(current, history) compares this month's normalized statement with the rulebook and
with the card's own history, and returns findings:

    status "alert"  a rule is broken           -> goes to the exception list
    status "ok"     a charge was checked and is correct (shown as expected = actual)
    status "info"   worth knowing, not an error (incl. the rulebook's "Not errors" N01-N07)

Every finding carries the expected value, the actual value, how the expected value was
calculated, and the transactions it is based on (evidence). The LLM never changes these.
"""
import re
from collections import defaultdict
from datetime import date, timedelta

from . import calculators as calc
from .config import rulebook, settings
from .ledger import previous_cycle

SEVERITY = {"C08": "low", "C12": "medium", "C07": "medium", "C02": "medium", "C04": "medium", "C06": "medium"}


def d(iso):
    return date.fromisoformat(iso) if iso else None


def evidence(t, month=None):
    return {"txn_id": t["id"], "date": t["date"], "description": t["description"], "amount": t["amount"],
            "type": t["type"], **({"statement_month": month} if month else {})}


def lines(stmt, *categories):
    return [t for t in stmt["transactions"] if t["category"] in categories]


def emi_converted(stmt):
    """Purchases that were moved into an EMI plan in the same statement (not real spend)."""
    amounts = [t["amount"] for t in lines(stmt, "emi_transfer")]
    out = set()
    for t in lines(stmt, "purchase"):
        if t["pay_in_emi"] and t["amount"] in amounts:
            out.add(t["id"])
            amounts.remove(t["amount"])
    return out


class Context:
    def __init__(self, current, history):
        self.cur = current
        self.hist = history
        self.prev = previous_cycle(current, history)
        self.rb = rulebook()
        self.p = self.rb.params
        self.tol = self.rb.tolerance
        self.cfg = settings()
        self.findings = []
        self.coverage = {}
        self.sd = d(current["statement"]["statement_date"])
        self.overdue_minimum = 0.0
        self.fee_handlers = [(re.compile(f["pattern"]), f) for f in self.cfg["fee_lines"]]

    def add(self, rule_id, status, title, expected=None, actual=None, unit="INR", calculation=None,
            evidence_rows=(), related=(), allowed_by=None):
        f = {"rule_id": rule_id, "status": status, "title": title, "expected": expected, "actual": actual,
             "unit": unit, "calculation": calculation, "evidence": list(evidence_rows),
             "related_rules": list(related), "allowed_by": allowed_by}
        if unit == "INR" and isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            f["difference"] = calc.r2(actual - expected)
        if status == "alert":
            f["severity"] = SEVERITY.get(rule_id, "high")
        self.findings.append(f)
        self.cover(rule_id, "evaluated")
        return f

    def cover(self, rule_id, status, reason=None):
        if self.coverage.get(rule_id, {}).get("status") != "evaluated":
            self.coverage[rule_id] = {"status": status, **({"reason": reason} if reason else {})}

    def fee_lines(self, handler):
        return [t for t in lines(self.cur, "fee") if self.handler_for(t) and self.handler_for(t)["handler"] == handler]

    def handler_for(self, t):
        return next((f for rx, f in self.fee_handlers if rx.search(t["description"])), None)

    def money_differs(self, a, b):
        return abs(a - b) > self.tol


# ---------------------------------------------------------------- statement-level checks

def check_totals(c):
    """C10: the statement adds up, and follows on from last month."""
    a, s = c.cur["account_summary"], c.cur["statement"]
    computed = calc.r2(a["previous_balance"] - a["payments"] - a["credits_refunds_emi_transfers"]
                       + a["purchases_emi_instalments"] + a["fees_interest_gst"])
    formula = (f"{a['previous_balance']} - {a['payments']} - {a['credits_refunds_emi_transfers']} + "
               f"{a['purchases_emi_instalments']} + {a['fees_interest_gst']} = {computed}")
    if c.money_differs(computed, s["total_amount_due"]):
        c.add("C10", "alert", "Total amount due does not add up", computed, s["total_amount_due"], calculation=formula)
    else:
        c.add("C10", "ok", "Total amount due adds up", computed, s["total_amount_due"], calculation=formula)

    buckets = {"payments": ("payment",), "credits_refunds_emi_transfers": ("refund", "emi_transfer"),
               "purchases_emi_instalments": ("purchase", "emi_principal"),
               "fees_interest_gst": ("fee", "emi_interest", "finance_charge", "forex_markup", "gst")}
    for box, cats in buckets.items():
        rows = lines(c.cur, *cats)
        total = calc.r2(sum(t["amount"] for t in rows))
        if c.money_differs(total, a[box]):
            c.add("C10", "alert", f"Summary box '{box}' does not match its transactions", total, a[box],
                  calculation=f"sum of {len(rows)} lines = {total}", evidence_rows=[evidence(t) for t in rows])
    if c.prev and c.money_differs(c.prev["statement"]["total_amount_due"], a["previous_balance"]):
        c.add("C10", "alert", "Previous balance differs from last statement's total due",
              c.prev["statement"]["total_amount_due"], a["previous_balance"],
              calculation=f"total due on the {c.prev['month']} statement")


def check_due_date(c):
    """F15: due date = statement date + 20 days."""
    expected = calc.due_date(c.sd, c.p["due_date_days"])
    actual = d(c.cur["statement"]["payment_due_date"])
    status = "alert" if actual < expected else "ok"
    c.add("F15", status, "Payment due date " + ("is earlier than allowed" if status == "alert" else "is correct"),
          expected.isoformat(), actual.isoformat(), unit="date",
          calculation=f"statement date {c.sd} + {c.p['due_date_days']} days")


def check_gst(c):
    """F13: GST = 18% of fees, interest and charges."""
    rows = lines(c.cur, "fee", "emi_interest", "finance_charge", "forex_markup")
    base = calc.r2(sum(t["amount"] for t in rows))
    expected = calc.gst(base, c.p["gst_rate"])
    gst_rows = lines(c.cur, "gst")
    actual = calc.r2(sum(t["amount"] for t in gst_rows))
    if not rows and not gst_rows:
        return
    status = "alert" if c.money_differs(expected, actual) else "ok"
    c.add("F13", status, "GST on fees and interest" + (" differs from 18%" if status == "alert" else " is 18%"),
          expected, actual, calculation=f"{c.p['gst_rate']:.0%} x {base} (fees + interest + charges)",
          evidence_rows=[evidence(t) for t in rows + gst_rows])


def check_minimum_due(c):
    """F14: minimum amount due formula (run after the late-fee check, which finds any overdue minimum)."""
    s = c.cur["statement"]
    mandatory_rows = lines(c.cur, "gst", "emi_principal", "emi_interest", "fee", "forex_markup", "finance_charge")
    mandatory = calc.r2(sum(t["amount"] for t in mandatory_rows))
    emi_outstanding = sum(p["outstanding"] for p in c.cur["emi_plans"])
    over = max(0.0, calc.r2(s["total_amount_due"] + emi_outstanding - s["credit_limit"]))
    expected = calc.minimum_due(s["total_amount_due"], mandatory, over, c.p["mad_remaining_balance_rate"],
                                c.p["mad_floor"], c.overdue_minimum)
    status = "alert" if c.money_differs(expected, s["minimum_amount_due"]) else "ok"
    c.add("F14", status, "Minimum amount due " + ("differs from the formula" if status == "alert" else "is correct"),
          expected, s["minimum_amount_due"],
          calculation=(f"GST+EMI+fees+interest {mandatory} + over-limit {over} + max({c.p['mad_remaining_balance_rate']:.0%} "
                       f"of remaining, {c.p['mad_floor']}) + overdue minimum {c.overdue_minimum}, capped at total due"))


# ---------------------------------------------------------------- interest and fees

def carried_balance(stmt, prev):
    """Unpaid part of the previous total due that this statement carried forward."""
    if not prev:
        return 0.0
    paid = sum(t["amount"] for t in lines(stmt, "payment"))
    return max(0.0, calc.r2(prev["statement"]["total_amount_due"] - paid))


def check_finance_charge(c):
    """F01 / F02 / F03, with the allowed cases N06 and N07."""
    fc_rows = lines(c.cur, "finance_charge")
    actual = calc.r2(sum(t["amount"] for t in fc_rows))
    if not c.prev:
        c.cover("F01", "not_applicable", "previous statement not in the ledger")
        if fc_rows:
            c.add("F01", "info", "Finance charge present but cannot be verified without last month's statement",
                  None, actual, evidence_rows=[evidence(t) for t in fc_rows])
        return
    prev, prev_sd = c.prev, d(c.prev["statement"]["statement_date"])
    prev_prev = previous_cycle(prev, c.hist[:-1])
    prev_carried = carried_balance(prev, prev_prev)
    carried = carried_balance(c.cur, prev)
    due = max(d(prev["statement"]["payment_due_date"]), calc.due_date(prev_sd, c.p["due_date_days"]))
    payments = lines(c.cur, "payment")
    paid_by_due = sum(t["amount"] for t in payments if d(t["date"]) <= due)
    revolving = carried > c.tol or prev_carried > c.tol or paid_by_due < prev["statement"]["total_amount_due"] - c.tol

    if not revolving:
        if actual > 0:
            c.add("F02", "alert", "Finance charge although the previous bill was paid in full on time", 0.0, actual,
                  calculation=f"previous total due {prev['statement']['total_amount_due']} paid by {due}; nothing carried",
                  evidence_rows=[evidence(t) for t in fc_rows + payments])
        else:
            c.cover("F02", "evaluated")
            c.cover("F01", "not_applicable", "no balance carried")
        return

    converted = emi_converted(c.cur)
    purchases = [(t["amount"], (c.sd - d(t["date"])).days) for t in lines(c.cur, "purchase") if t["id"] not in converted]
    pays = [(t["amount"], (d(t["date"]) - prev_sd).days) for t in payments]
    cycle_days = (c.sd - prev_sd).days
    expected, breakdown = calc.finance_charge(carried, cycle_days, pays, purchases, c.p["finance_charge_annual"],
                                              c.p["min_finance_charge"])
    explanation = {"carried_balance": carried, "cycle_days": cycle_days, "previous_cycle_carried": prev_carried, **breakdown}
    ev = [evidence(t) for t in fc_rows + payments]
    if 0 < actual < c.p["min_finance_charge"]:
        c.add("F03", "alert", "Finance charge below the Rs. 25 minimum", float(c.p["min_finance_charge"]), actual,
              calculation=explanation, evidence_rows=ev)
    elif c.money_differs(expected, actual):
        implied = round(actual / expected * c.p["finance_charge_annual"] * 100, 2) if expected else None
        c.add("F01", "alert", "Finance charge differs from the 45% p.a. formula", expected, actual,
              calculation={**explanation, "implied_annual_rate_percent": implied}, evidence_rows=ev)
        if carried > c.tol and purchases:
            c.add("N07", "info", c.rb.not_errors["N07"]["looks_wrong"], allowed_by="N07",
                  calculation=c.rb.not_errors["N07"]["why_allowed"])
    else:
        allowed = "N06" if carried <= c.tol and prev_carried > c.tol else ("N07" if carried > c.tol and purchases else None)
        c.add("F01", "ok", "Finance charge matches the formula", expected, actual, calculation=explanation,
              evidence_rows=ev, allowed_by=allowed)
        if allowed:
            c.add(allowed, "info", c.rb.not_errors[allowed]["looks_wrong"], allowed_by=allowed,
                  calculation=c.rb.not_errors[allowed]["why_allowed"])


def minimum_missed(stmt, prev, p, tol):
    """Was the previous statement's minimum due unpaid by its due date + grace days? (F06, N01, N05)"""
    prev_sd = d(prev["statement"]["statement_date"])
    due = max(d(prev["statement"]["payment_due_date"]), calc.due_date(prev_sd, p["due_date_days"]))
    grace = due + timedelta(days=p["late_fee_grace_days"])
    paid = sum(t["amount"] for t in lines(stmt, "payment") if d(t["date"]) <= grace)
    paid += sum(t["amount"] for t in lines(stmt, "refund") if d(t["date"]) <= due)          # N05
    shortfall = calc.r2(prev["statement"]["minimum_amount_due"] - paid)
    return shortfall > tol, max(0.0, shortfall), due, grace


def check_late_fee(c):
    """F06 / F07, with the allowed cases N01 and N05."""
    fee_rows = c.fee_lines("late_fee")
    extra_rows = c.fee_lines("additional_late")
    if not c.prev:
        c.cover("F06", "not_applicable", "previous statement not in the ledger")
        for t in fee_rows:
            c.add("F06", "info", "Late fee present but cannot be verified without last month's statement", None, t["amount"],
                  evidence_rows=[evidence(t)])
        return
    missed, shortfall, due, grace = minimum_missed(c.cur, c.prev, c.p, c.tol)
    c.overdue_minimum = shortfall if missed else 0.0
    payments = lines(c.cur, "payment")
    actual = calc.r2(sum(t["amount"] for t in fee_rows))
    prev_total = c.prev["statement"]["total_amount_due"]
    expected = calc.late_fee(prev_total, c.rb.slabs) if missed else 0.0
    how = (f"minimum due {c.prev['statement']['minimum_amount_due']} of the {c.prev['month']} statement; due {due}, "
           f"grace until {grace}; slab for amount due {prev_total} = {calc.late_fee(prev_total, c.rb.slabs)}")
    ev = [evidence(t) for t in fee_rows + payments]
    if fee_rows and not missed:
        c.add("F06", "alert", "Late payment fee charged although the minimum due was paid on time", 0.0, actual,
              calculation=how, evidence_rows=ev, related=["C11"])
    elif fee_rows and c.money_differs(expected, actual):
        c.add("F06", "alert", "Late payment fee uses the wrong slab", expected, actual, calculation=how, evidence_rows=ev)
    elif fee_rows:
        c.add("F06", "ok", "Late payment fee is correct", expected, actual, calculation=how, evidence_rows=ev)
    elif missed:
        c.add("F06", "info", "Minimum due was missed but no late fee was charged (in your favour)", expected, 0.0,
              calculation=how, evidence_rows=ev)
    else:
        c.cover("F06", "evaluated")
        late_but_in_grace = [t for t in payments if due < d(t["date"]) <= grace]
        if late_but_in_grace:
            c.add("N01", "info", c.rb.not_errors["N01"]["looks_wrong"], calculation=how,
                  evidence_rows=[evidence(t) for t in late_but_in_grace], allowed_by="N01")
        refunds_counted = [t for t in lines(c.cur, "refund") if d(t["date"]) <= due]
        if refunds_counted and sum(t["amount"] for t in payments) < c.prev["statement"]["minimum_amount_due"]:
            c.add("N05", "info", c.rb.not_errors["N05"]["looks_wrong"], calculation=how,
                  evidence_rows=[evidence(t) for t in refunds_counted], allowed_by="N05")

    # F07: Rs. 100 extra only after two consecutive missed minimums
    prev_prev = previous_cycle(c.prev, c.hist[:-1])
    missed_before = bool(prev_prev) and minimum_missed(c.prev, prev_prev, c.p, c.tol)[0]
    for t in extra_rows:
        if missed and missed_before and not c.money_differs(t["amount"], c.p["additional_late_charge"]):
            c.add("F07", "ok", "Additional late charge is correct", float(c.p["additional_late_charge"]), t["amount"],
                  evidence_rows=[evidence(t)])
        else:
            c.add("F07", "alert", "Additional late charge without two consecutive missed minimums",
                  float(c.p["additional_late_charge"]) if missed and missed_before else 0.0, t["amount"],
                  calculation=f"missed this cycle: {missed}, missed last cycle: {missed_before}", evidence_rows=[evidence(t)])


def peak_exposure(c):
    """Highest balance + EMI outstanding during the cycle (what the credit limit applies to)."""
    if c.prev:
        opening = c.prev["statement"]["total_amount_due"] + sum(p["outstanding"] for p in c.prev["emi_plans"])
    else:
        opening = c.cur["account_summary"]["previous_balance"]
    sign = {"purchase": 1, "fee": 1, "emi_interest": 1, "finance_charge": 1, "forex_markup": 1, "gst": 1,
            "payment": -1, "refund": -1}          # EMI transfers and instalments only move money inside the limit
    dated = sorted((t for t in c.cur["transactions"] if t["date"] and t["category"] in sign), key=lambda t: t["date"])
    undated = [t for t in c.cur["transactions"] if not t["date"] and t["category"] in sign]
    running = peak = opening
    for t in dated + undated:
        running += sign[t["category"]] * t["amount"]
        peak = max(peak, running)
    return calc.r2(peak), calc.r2(opening)


def check_overlimit(c):
    """F08 (and N03): over-limit fee only when the balance went over the credit limit, once per cycle."""
    rows = c.fee_lines("overlimit")
    if not rows:
        c.cover("F08", "evaluated")
        return
    limit = c.cur["statement"]["credit_limit"]
    peak, opening = peak_exposure(c)
    over = calc.r2(peak - limit)
    how = f"opening balance incl. EMI outstanding {opening}; highest balance in the cycle {peak}; credit limit {limit}"
    ev = [evidence(t) for t in rows]
    if len(rows) > 1:
        c.add("F08", "alert", "Over-limit fee charged more than once in one cycle", rows[0]["amount"],
              calc.r2(sum(t["amount"] for t in rows)), calculation=how, evidence_rows=ev)
    if over <= 0:
        c.add("F08", "alert", "Over-limit fee charged although the balance stayed within the credit limit", 0.0,
              calc.r2(sum(t["amount"] for t in rows)), calculation=how, evidence_rows=ev, related=["C11"])
    else:
        expected = calc.overlimit_fee(over, c.p["overlimit_fee_rate"], c.p["overlimit_fee_min"])
        status = "alert" if c.money_differs(expected, rows[0]["amount"]) else "ok"
        c.add("F08", status, "Over-limit fee " + ("amount is wrong" if status == "alert" else "is correct"),
              expected, rows[0]["amount"], calculation=how + f"; over by {over}", evidence_rows=ev)


def check_forex(c):
    """F10 / F11 (and N02): 3.5% markup on each foreign-currency purchase."""
    foreign = sorted((t for t in lines(c.cur, "purchase") if t.get("foreign_currency")), key=lambda t: t["amount"])
    markups = sorted(lines(c.cur, "forex_markup"), key=lambda t: t["amount"])
    rate = c.p["forex_fee_rate"]
    if foreign or markups:
        c.cover("F10", "evaluated")
    if len(foreign) == len(markups):
        for t, m in zip(foreign, markups):
            expected = calc.forex_markup(t["amount"], rate)
            how = f"{rate:.1%} x {t['amount']} ({t.get('foreign_amount')} {t.get('foreign_currency')})"
            if c.money_differs(expected, m["amount"]):
                c.add("F10", "alert", "Foreign currency markup differs from 3.5%", expected, m["amount"],
                      calculation={"formula": how, "implied_rate_percent": round(m["amount"] / t["amount"] * 100, 2)},
                      evidence_rows=[evidence(t), evidence(m)])
            else:
                c.add("F10", "ok", "Foreign currency markup is 3.5%", expected, m["amount"], calculation=how,
                      evidence_rows=[evidence(t), evidence(m)])
    else:
        expected = calc.r2(sum(calc.forex_markup(t["amount"], rate) for t in foreign))
        actual = calc.r2(sum(m["amount"] for m in markups))
        if len(markups) > len(foreign):
            c.add("F11", "alert", "Foreign currency markup with no matching foreign transaction", expected, actual,
                  calculation=f"{len(foreign)} foreign purchases, {len(markups)} markup lines",
                  evidence_rows=[evidence(t) for t in foreign + markups], related=["C11"])
        elif c.money_differs(expected, actual):
            c.add("F10", "info", "Fewer markup lines than foreign purchases (in your favour)", expected, actual,
                  evidence_rows=[evidence(t) for t in foreign + markups])
    for t in lines(c.cur, "refund"):
        if t.get("foreign_currency"):
            c.add("N02", "info", c.rb.not_errors["N02"]["looks_wrong"], calculation=c.rb.not_errors["N02"]["why_allowed"],
                  evidence_rows=[evidence(t)], allowed_by="N02")


def card_terms(c):
    terms = c.cur.get("card_terms") or {}
    defaults = c.cfg["card_defaults"]
    return (terms.get("annual_fee") or defaults["annual_fee"],
            terms.get("annual_fee_waiver_spend") or defaults["annual_fee_waiver_spend"])


def check_annual_fee(c):
    """F12: annual fee once in 12 months, the card's amount, and not when the waiver spend was met."""
    rows = c.fee_lines("annual_fee")
    if not rows:
        c.cover("F12", "evaluated")
        return
    fee, waiver = card_terms(c)
    for t in rows:
        when = d(t["date"])
        earlier = [(s["month"], x) for s in c.hist for x in s["transactions"]
                   if x["category"] == "fee" and c.handler_for(x) and c.handler_for(x)["handler"] == "annual_fee"
                   and 0 <= (when - d(x["date"])).days < 365]
        if earlier:
            c.add("F12", "alert", "Annual fee charged a second time within 12 months", 0.0, t["amount"],
                  calculation=f"already charged on {earlier[0][1]['date']} ({earlier[0][0]} statement)",
                  evidence_rows=[evidence(t)] + [evidence(x, m) for m, x in earlier], related=["C11"])
            continue
        if c.money_differs(fee, t["amount"]):
            c.add("F12", "alert", "Annual fee amount differs from the card's fee", fee, t["amount"], evidence_rows=[evidence(t)])
            continue
        window = [x for s in c.hist + [c.cur] for x in s["transactions"]
                  if x["category"] in ("purchase", "refund") and x["date"] and 0 <= (when - d(x["date"])).days < 365
                  and x["id"] not in emi_converted(s)]
        spend = calc.r2(sum(x["amount"] if x["category"] == "purchase" else -x["amount"] for x in window))
        months = len(c.hist) + 1
        how = f"spend in the last 12 months found in the ledger: {spend} ({months} statements); waiver threshold {waiver}"
        if spend >= waiver:
            c.add("F12", "alert", "Annual fee charged although the waiver spend was reached", 0.0, t["amount"],
                  calculation=how, evidence_rows=[evidence(t)])
        else:
            c.add("F12", "ok", "Annual fee is the card's fee and the first in 12 months", fee, t["amount"],
                  calculation=how + ("" if months >= 12 else " - waiver can only be fully verified with 12 months of history"),
                  evidence_rows=[evidence(t)])


def check_emi_processing_fee(c):
    """Processing fee is billed once, in the month a plan is booked (EMI plan terms; no trigger -> C11)."""
    rows = c.fee_lines("emi_processing")
    start, end = d(c.cur["statement"]["period_from"]), d(c.cur["statement"]["period_to"])
    new_plans = [p for p in c.cur["emi_plans"] if start <= d(p["booked_on"]) <= end]
    if not rows:
        return
    if not new_plans:
        c.add("C11", "alert", "EMI processing fee with no new EMI plan this cycle", 0.0,
              calc.r2(sum(t["amount"] for t in rows)), evidence_rows=[evidence(t) for t in rows])
        return
    expected = 0.0
    for p in new_plans:
        plan_rate = c.rb.emi_plans.get(p["plan_ref"], {}).get("processing_fee_rate")
        expected += calc.emi_processing_fee(p["amount"], p["tenure_months"], plan_rate)
    actual = calc.r2(sum(t["amount"] for t in rows))
    status = "alert" if c.money_differs(expected, actual) else "ok"
    c.add("C06", status, "EMI processing fee " + ("differs from the plan terms" if status == "alert" else "matches the plan terms"),
          calc.r2(expected), actual, calculation="0% for 3-month plans, 1% of the booking amount for longer plans",
          evidence_rows=[evidence(t) for t in rows])


def check_other_fees(c):
    """Every other fee line needs a matching trigger on the statement (C11) and the right amount (F05, F09, F16-F25)."""
    dedicated = {"late_fee", "additional_late", "overlimit", "annual_fee", "emi_processing"}
    for t in lines(c.cur, "fee"):
        h = c.handler_for(t)
        if h and h["handler"] in dedicated:
            continue
        if not h:
            c.add("C11", "alert", "Unrecognised fee - no rule explains it", 0.0, t["amount"], evidence_rows=[evidence(t)])
            continue
        triggers = [x for x in lines(c.cur, "purchase", "payment")
                    if any(k in x["description"].upper() for k in h["trigger"])]
        if not triggers:
            c.add(h["rule"], "alert", f"{t['description']} with no matching event on the statement", 0.0, t["amount"],
                  evidence_rows=[evidence(t)], related=["C11"])
            continue
        if h["handler"] == "cycle_total":
            expected = calc.fee_formula(h["calc"], sum(x["amount"] for x in triggers), c.p)
        else:
            expected = calc.r2(sum(calc.fee_formula(h["calc"], x["amount"], c.p) or 0 for x in triggers))
        status = "alert" if expected is not None and c.money_differs(expected, t["amount"]) else "ok"
        c.add(h["rule"], status, f"{t['description']} " + ("amount is wrong" if status == "alert" else "is correct"),
              expected, t["amount"], evidence_rows=[evidence(t)] + [evidence(x) for x in triggers])


# ---------------------------------------------------------------- EMI tracking (uses history)

def interest_line(stmt, principal_row):
    rows = stmt["transactions"]
    i = rows.index(principal_row)
    return rows[i + 1] if i + 1 < len(rows) and rows[i + 1]["category"] == "emi_interest" else None


def check_emis(c):
    """C02-C06: each plan billed once a month, in sequence, at the plan amount, and never after the last instalment."""
    plans = {}                                         # plan_ref -> terms (first time the plan was printed)
    billed = defaultdict(list)                         # plan_ref -> [(month, instalment no)]
    for s in c.hist + [c.cur]:
        for p in s["emi_plans"]:
            plans.setdefault(p["plan_ref"], p)
    if not plans and not lines(c.cur, "emi_principal"):
        return

    def plan_for(row, last):
        cands = [r for r, p in plans.items() if p["tenure_months"] == row["emi_of"] and d(p["booked_on"]) <= d(row["date"])]
        exact = [r for r in cands if last.get(r, 0) + 1 == row["emi_no"]]
        return (exact or cands or [None])[0]

    last = {}
    for s in c.hist:
        for row in lines(s, "emi_principal"):
            ref = plan_for(row, last)
            if ref:
                billed[ref].append((s["month"], row["emi_no"]))
                last[ref] = max(last.get(ref, 0), row["emi_no"])

    start, end = d(c.cur["statement"]["period_from"]), d(c.cur["statement"]["period_to"])
    this_month = defaultdict(list)
    for row in lines(c.cur, "emi_principal"):
        ref = plan_for(row, last)
        if ref is None:
            c.add("C05", "alert", f"EMI line {row['description']} matches no known EMI plan", 0.0, row["amount"],
                  evidence_rows=[evidence(row)])
            continue
        this_month[ref].append(row)

    listed = {p["plan_ref"]: p for p in c.cur["emi_plans"]}
    for ref, plan in plans.items():
        P, rate, n = plan["amount"], plan["rate_pa_percent"] / 100, plan["tenure_months"]
        schedule = calc.emi_schedule(P, rate, n)
        done = last.get(ref, 0)
        booked_now = start <= d(plan["booked_on"]) <= end
        expected_no = done + 1 if done < n and (done > 0 or booked_now or c.hist) else None
        rows = this_month.get(ref, [])

        if ref in listed:                               # C06: the printed plan itself
            printed = listed[ref]
            formula = calc.emi_amount(P, rate, n)
            if c.money_differs(formula, printed["emi"]):
                c.add("C06", "alert", f"{ref}: printed EMI differs from the formula for the stated plan", formula,
                      printed["emi"], calculation=f"EMI = P r (1+r)^n / ((1+r)^n - 1), P={P}, rate={rate:.0%}, n={n}")
            if rows and printed["instalment_no"] <= n:
                closing = schedule[printed["instalment_no"] - 1]["closing"]
                if c.money_differs(closing, printed["outstanding"]):
                    c.add("C06", "alert", f"{ref}: outstanding principal differs from the schedule", closing,
                          printed["outstanding"], calculation=f"schedule closing balance after instalment {printed['instalment_no']}")

        if expected_no and not rows:
            c.add("C02", "alert", f"{ref}: EMI instalment {expected_no}/{n} is missing", schedule[expected_no - 1]["emi"], 0.0,
                  calculation=f"last billed instalment {done}/{n} ({billed[ref][-1][0] if billed[ref] else 'none'})")
        if len(rows) > 1:
            c.add("C03", "alert", f"{ref}: EMI charged {len(rows)} times this month", schedule[0]["emi"],
                  calc.r2(sum(r["amount"] + (interest_line(c.cur, r) or {"amount": 0})["amount"] for r in rows)),
                  evidence_rows=[evidence(r) for r in rows])
        for row in rows:
            interest = interest_line(c.cur, row)
            charged = calc.r2(row["amount"] + (interest["amount"] if interest else 0))
            ev = [evidence(row)] + ([evidence(interest)] if interest else [])
            if expected_no is None or row["emi_no"] > n:
                c.add("C05", "alert", f"{ref}: EMI charged after the last instalment", 0.0, charged,
                      calculation=f"{n}-month plan; instalments already billed: "
                                  + ", ".join(f"{no}/{n} ({m})" for m, no in billed[ref]),
                      evidence_rows=ev)
                continue
            if row["emi_no"] != expected_no:
                c.add("C05", "alert", f"{ref}: instalment number {row['emi_no']}/{n}, expected {expected_no}/{n}",
                      expected_no, row["emi_no"], unit="instalment", evidence_rows=ev)
                continue
            due = schedule[expected_no - 1]
            if c.money_differs(due["emi"], charged):
                c.add("C04", "alert", f"{ref}: EMI amount differs from the plan", due["emi"], charged,
                      calculation=f"principal {row['amount']} + interest {interest['amount'] if interest else 0}", evidence_rows=ev)
            elif interest and (c.money_differs(due["interest"], interest["amount"]) or c.money_differs(due["principal"], row["amount"])):
                c.add("C06", "alert", f"{ref}: principal/interest split differs from the schedule",
                      due["interest"], interest["amount"],
                      calculation=f"schedule for instalment {expected_no}: principal {due['principal']}, interest {due['interest']}",
                      evidence_rows=ev)
            else:
                c.add("C04", "ok", f"{ref}: EMI {expected_no}/{n} matches the plan", due["emi"], charged,
                      calculation=f"principal {due['principal']} + interest {due['interest']} (opening {due['opening']} x {rate:.0%}/12)",
                      evidence_rows=ev)
    for rule in ("C02", "C03", "C05", "C06"):
        c.cover(rule, "evaluated")


# ---------------------------------------------------------------- pattern checks (use history)

def check_duplicates(c):
    """C01: same date, merchant, amount and type twice."""
    seen = defaultdict(list)
    for t in lines(c.cur, "purchase", "fee"):
        seen[(t["date"], t["merchant_key"] or t["description"], t["amount"], t["type"])].append(t)
    for (when, merchant, amount, _), rows in seen.items():
        if len(rows) > 1:
            c.add("C01", "alert", f"Duplicate transaction: {merchant} on {when}", amount, calc.r2(amount * len(rows)),
                  calculation=f"{len(rows)} identical lines (same date, merchant and amount)",
                  evidence_rows=[evidence(t) for t in rows])
    c.cover("C01", "evaluated")


def monthly_charges(stmt):
    """merchant_key -> the single purchase this month (merchants charged more than once are not subscriptions)."""
    by = defaultdict(list)
    for t in lines(stmt, "purchase"):
        by[t["merchant_key"]].append(t)
    return {k: v[0] for k, v in by.items() if len(v) == 1}


def same_price(a, b, tol):
    if a.get("foreign_amount") and b.get("foreign_amount"):
        return a["foreign_currency"] == b["foreign_currency"] and abs(a["foreign_amount"] - b["foreign_amount"]) < 0.005
    return abs(a["amount"] - b["amount"]) <= tol


def price(t):
    return t["foreign_amount"] if t.get("foreign_amount") else t["amount"]


def check_subscriptions(c):
    """C07 (price increase on a recurring charge) and C08 (new recurring charge)."""
    if not c.prev:
        c.cover("C07", "not_applicable", "previous statement not in the ledger")
        return
    window = c.cfg["heuristics"]["recurring_day_window"]
    prev_prev = previous_cycle(c.prev, c.hist[:-1])
    cur, last = monthly_charges(c.cur), monthly_charges(c.prev)
    before = monthly_charges(prev_prev) if prev_prev else {}
    close_day = lambda a, b: abs(d(a["date"]).day - d(b["date"]).day) <= window            # noqa: E731
    for key, t in cur.items():
        p1 = last.get(key)
        if not p1 or not close_day(t, p1):
            continue
        p2 = before.get(key)
        history = [x["transactions"] for x in c.hist]
        amounts = [x["amount"] for rows in history for x in rows if x.get("merchant_key") == key] + [t["amount"]]
        if p2 and close_day(p1, p2) and same_price(p1, p2, c.tol):          # established subscription
            ev = [evidence(p2, prev_prev["month"]), evidence(p1, c.prev["month"]), evidence(t)]
            if same_price(t, p1, c.tol):
                c.add("C07", "ok", f"Recurring charge unchanged: {key}", p1["amount"], t["amount"], evidence_rows=ev)
            elif price(t) > price(p1):
                c.add("C07", "alert", f"Subscription price increase: {key}", p1["amount"], t["amount"],
                      calculation=f"charged {p1['amount']} in each earlier month; history {amounts}", evidence_rows=ev)
        elif p1 and same_price(t, p1, c.tol) and prev_prev and not any(
                x.get("merchant_key") == key for s in c.hist[:-1] for x in s["transactions"]):
            c.add("C08", "alert", f"New recurring charge: {key}", 0.0, t["amount"],
                  calculation="charged in the last two months at the same amount, never before",
                  evidence_rows=[evidence(p1, c.prev["month"]), evidence(t)])
    for rule in ("C07", "C08"):
        c.cover(rule, "evaluated")


REFUND_MSG = re.compile(r"Refund of Rs ([\d,.]+) .*?\((.+?), order dated (\d{2} \w{3} \d{2})\) was initiated by the "
                        r"merchant on (\d{2} \w{3} \d{2}) and will be credited within (\d+) working days")


def check_refunds(c):
    """C09: refunds promised on an earlier statement must show up as credits."""
    from .parser import iso_date
    for i, s in enumerate(c.hist):
        for msg in s["messages"]:
            m = REFUND_MSG.search(msg)
            if not m:
                continue
            amount, merchant = float(m.group(1).replace(",", "")), m.group(2)
            initiated = d(iso_date(m.group(4)))
            due = calc.add_working_days(initiated, int(m.group(5)))
            later = c.hist[i:] + [c.cur]
            got = [(x["month"], t) for x in later for t in lines(x, "refund")
                   if abs(t["amount"] - amount) <= c.tol and (t["merchant_key"] or "").startswith(merchant)]
            how = f"promised on the {s['month']} statement: initiated {initiated}, due by {due}"
            if got:
                if got[0][0] == c.cur["month"]:
                    c.add("C09", "ok", f"Expected refund received from {merchant}", amount, got[0][1]["amount"],
                          calculation=how, evidence_rows=[evidence(got[0][1])])
                continue
            if c.sd < due:
                continue
            first_after_due = all(d(x["statement"]["statement_date"]) < due for x in c.hist[i:])
            if first_after_due:
                c.add("C09", "alert", f"Missing refund: {merchant}", amount, 0.0, calculation=how,
                      evidence_rows=[{"statement_month": s["month"], "message": msg}])
            else:
                c.add("C09", "info", f"Refund from {merchant} still not received (flagged earlier)", amount, 0.0, calculation=how)
    c.cover("C09", "evaluated")


def check_new_merchants(c):
    """C12: a large charge from a merchant never seen before on this card."""
    if not c.hist:
        c.cover("C12", "not_applicable", "no earlier statements in the ledger")
        return
    seen = {t["merchant_key"] for s in c.hist for t in lines(s, "purchase", "refund")}
    largest = max((t["amount"] for s in c.hist for t in lines(s, "purchase") if t["id"] not in emi_converted(s)), default=0)
    multiple = c.cfg["heuristics"]["new_merchant_large_multiple"]
    for t in lines(c.cur, "purchase"):
        if t["merchant_key"] in seen or t["id"] in emi_converted(c.cur):
            continue
        how = f"never seen in {len(c.hist)} earlier statements; largest earlier purchase {largest}; threshold {multiple} x"
        if t["amount"] > multiple * largest:
            c.add("C12", "alert", f"Large charge from a new merchant: {t['merchant_key']}", calc.r2(multiple * largest),
                  t["amount"], calculation=how, evidence_rows=[evidence(t)])
        else:
            c.add("C12", "info", f"First-time merchant: {t['merchant_key']}", None, t["amount"], calculation=how,
                  evidence_rows=[evidence(t)])
        seen.add(t["merchant_key"])
    c.cover("C12", "evaluated")


CHECKS = [check_totals, check_due_date, check_gst, check_finance_charge, check_late_fee, check_overlimit,
          check_forex, check_annual_fee, check_emi_processing_fee, check_other_fees, check_emis,
          check_duplicates, check_subscriptions, check_refunds, check_new_merchants, check_minimum_due]


def run_checks(current, history):
    c = Context(current, history)
    for check in CHECKS:
        check(c)
    for rule_id in c.rb.rules:
        c.cover(rule_id, "not_applicable", "no matching charge or event on this statement")
    month = current["month"]
    for i, f in enumerate(c.findings, 1):
        f["id"] = f"{month}-{i:02d}-{f['rule_id']}"
    return {"findings": c.findings, "coverage": dict(sorted(c.coverage.items())), "had_previous_statement": bool(c.prev),
            "history_months": [s["month"] for s in history]}
