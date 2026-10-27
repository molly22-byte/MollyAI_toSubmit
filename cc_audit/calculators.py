"""Deterministic calculators (pure functions, no I/O, no LLM).

Every expected value in an alert comes from here, so the arithmetic is testable and the
LLM never has to do maths. Money is rounded half-up to paise, like the statements.
"""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal


def r2(x):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def gst(amount, rate):
    """F13: GST on a fee, interest or charge."""
    return r2(amount * rate)


def due_date(statement_date, days):
    """F15: payment due date."""
    return statement_date + timedelta(days=days)


def finance_charge(carried, cycle_days, payments, purchases, annual_rate, minimum):
    """F01/F03/N06/N07: simplified average-daily-balance interest.

    carried    unpaid part of the previous total due, charged for the whole cycle
    payments   [(amount, days from previous statement date to payment date)]
               the paid part of the previous balance still earns interest until it is paid
    purchases  [(amount, days from transaction date to statement date)]
               only counted while an earlier balance is carried (interest-free period lost, N07)
    """
    carried_days = carried * cycle_days
    paid_days = sum(a * n for a, n in payments)
    purchase_days = sum(a * n for a, n in purchases) if carried > 0 else 0
    product = carried_days + paid_days + purchase_days
    charge = r2(product * annual_rate / 365)
    if 0 < charge < minimum:
        charge = float(minimum)
    return charge, {
        "carried_x_days": r2(carried_days),
        "paid_x_days": r2(paid_days),
        "new_purchases_x_days": r2(purchase_days),
        "annual_rate": annual_rate,
        "formula": "annual_rate / 365 x (carried x cycle days + payments x days to payment + new purchases x days, only while carrying)",
    }


def forex_markup(inr_amount, rate):
    """F10: foreign currency transaction fee."""
    return r2(inr_amount * rate)


def late_fee(amount_due, slabs):
    """F06: slab fee for the outstanding amount due."""
    for s in slabs:
        if amount_due > s["above"] and (s["up_to"] is None or amount_due <= s["up_to"]):
            return float(s["fee"])
    return 0.0


def overlimit_fee(over_amount, rate, minimum):
    """F08: higher of rate x amount over the limit, or the minimum."""
    return r2(max(over_amount * rate, minimum)) if over_amount > 0 else 0.0


def emi_amount(principal, annual_rate, months):
    """C06: EMI = P x r x (1+r)^n / ((1+r)^n - 1)."""
    r = annual_rate / 12
    if r == 0:
        return r2(principal / months)
    return r2(principal * r * (1 + r) ** months / ((1 + r) ** months - 1))


def emi_schedule(principal, annual_rate, months):
    """C02-C06: month-by-month split, same method as the rulebook's EMI schedule sheet."""
    emi = emi_amount(principal, annual_rate, months)
    rows, opening = [], principal
    for n in range(1, months + 1):
        interest = r2(opening * annual_rate / 12)
        principal_part = r2(emi - interest)
        closing = r2(opening - principal_part)
        rows.append({"no": n, "opening": r2(opening), "interest": interest, "principal": principal_part,
                     "emi": emi, "closing": closing})
        opening = closing
    return rows


def emi_processing_fee(principal, tenure, plan_rate=None):
    """Processing fee: the plan's own rate if known, else 0% for 3 months and 1% for longer plans."""
    rate = plan_rate if plan_rate is not None else (0.0 if tenure <= 3 else 0.01)
    return r2(principal * rate)


def minimum_due(total, mandatory, over_limit_amount, remaining_rate, floor, overdue_minimum=0.0):
    """F14: 100% of GST, EMI, fees and interest + over-limit amount + max(2% of the rest, floor),
    plus any unpaid minimum due, capped at the total due."""
    remaining = total - mandatory - over_limit_amount
    value = mandatory + over_limit_amount + max(r2(remaining * remaining_rate), floor) + overdue_minimum
    return r2(min(total, value))


def add_working_days(start, days):
    """C09: 'credited within 10 working days' -> calendar date (Mon-Fri)."""
    current, left = start, days
    while left > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            left -= 1
    return current


# fee formulas for the generic fee catalogue (config/settings.json -> fee_lines)
def fee_formula(name, amount, p):
    """Expected fee for one trigger transaction of `amount`, using rulebook parameters `p`."""
    formulas = {
        "cash_advance_fee": lambda a: max(a * p["cash_advance_fee_rate"], p["cash_advance_fee_min"]),
        "dishonour_fee": lambda a: max(a * p["dishonour_fee_rate"], p["dishonour_fee_min"]),
        "dcc_fee": lambda a: a * p["dcc_fee_rate"] if a >= p["dcc_min_transaction"] else 0,
        "utility_fee": lambda a: a * p["utility_fee_rate"] if a > p["utility_fee_threshold"] else 0,
        "education_fee": lambda a: a * p["education_fee_rate"],
        "wallet_fee": lambda a: a * p["wallet_fee_rate"] if a > p["wallet_fee_threshold"] else 0,
        "rent_fee": lambda a: p["rent_processing_fee"],
        "fuel_surcharge": lambda a: a * p["fuel_surcharge_rate"],
        "cash_payment_fee": lambda a: p["cash_payment_fee"],
        "cheque_payment_fee": lambda a: p["cheque_payment_fee"],
        "redemption_fee": lambda a: p["rewards_redemption_fee"],
        "card_replacement_fee": lambda a: p["card_replacement_fee_max"],
    }
    f = formulas.get(name)
    return r2(f(amount)) if f else None
