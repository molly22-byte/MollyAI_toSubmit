"""Reports: the masked report (what the AI layer saw) and the restored view (what the user sees)."""
from datetime import datetime

from . import calculators as calc
from .checks import emi_converted, lines
from .config import settings


def statement_facts(record):
    s = record["statement"]
    return {"card": record["card_id"], "cardholder": record["cardholder"]["name"], "month": record["month"],
            "statement_date": s["statement_date"], "period": f"{s['period_from']} to {s['period_to']}",
            "payment_due_date": s["payment_due_date"], "total_amount_due": s["total_amount_due"],
            "minimum_amount_due": s["minimum_amount_due"], "credit_limit": s["credit_limit"]}


def buckets(record):
    converted = emi_converted(record)
    total = lambda *cats: calc.r2(sum(t["amount"] for t in lines(record, *cats)))          # noqa: E731
    return {
        "purchases": calc.r2(sum(t["amount"] for t in lines(record, "purchase") if t["id"] not in converted)),
        "emi_instalments": total("emi_principal", "emi_interest"),
        "fees": total("fee"),
        "finance_charges": total("finance_charge"),
        "forex_markup": total("forex_markup"),
        "gst": total("gst"),
        "payments": total("payment"),
        "refunds": total("refund"),
        "total_amount_due": record["statement"]["total_amount_due"],
    }


def carried_forward(record):
    """Previous balance minus payments and credits (what was left unpaid, plus EMI transfers' effect)."""
    a = record["account_summary"]
    other_credits = sum(t["amount"] for t in lines(record, "emi_transfer"))        # EMI transfers cancel a purchase, not debt
    return calc.r2(a["previous_balance"] - a["payments"] - (a["credits_refunds_emi_transfers"] - other_credits))


def money(x):
    return f"Rs. {abs(x):,.2f}"


def change_sentence(comparison):
    """Deterministic explanation of the month-over-month change (shown as a calculated fact)."""
    drivers = comparison.get("change_drivers")
    if not drivers:
        return None
    total = drivers["total_change"]
    direction = "went up" if total > 0 else "went down"
    main = drivers["pushed_the_bill_up"] if total > 0 else drivers["pulled_the_bill_down"]
    other = drivers["pulled_the_bill_down"] if total > 0 else drivers["pushed_the_bill_up"]
    verb = lambda m: f"{m['item']} {'up' if m['change'] > 0 else 'down'} {money(m['change'])}"        # noqa: E731
    text = f"Total due {direction} by {money(total)} versus last month"
    if main:
        text += ", mainly because of " + ", ".join(verb(m) for m in main[:3])
    if other:
        text += "; partly offset by " + ", ".join(verb(m) for m in other[:2])
    text += "."
    if drivers.get("not_explained_by_any_line"):
        text += f" {money(drivers['not_explained_by_any_line'])} of the change is not explained by any line on the statement."
    return text


def compare(cur, prev):
    """Month-over-month view, computed in code so the LLM can quote it instead of doing maths."""
    now = buckets(cur)
    out = {"this_month": now}
    if prev:
        before = buckets(prev)
        out["last_month"] = before
        out["change"] = {k: calc.r2(now[k] - before[k]) for k in now}
        # total due = (previous balance - payments - credits) carried forward + this month's new charges,
        # so the change in the bill splits exactly into these parts (computed here, quoted by the LLM)
        parts = {"unpaid balance carried from the previous bill": (carried_forward(cur), carried_forward(prev)),
                 "card purchases": (now["purchases"], before["purchases"]),
                 "EMI instalments": (now["emi_instalments"], before["emi_instalments"]),
                 "fees": (now["fees"], before["fees"]),
                 "finance charges (interest)": (now["finance_charges"], before["finance_charges"]),
                 "foreign currency markup": (now["forex_markup"], before["forex_markup"]),
                 "GST": (now["gst"], before["gst"])}
        moves = [{"item": k, "this_month": a, "last_month": b, "change": calc.r2(a - b)} for k, (a, b) in parts.items()]
        out["change_drivers"] = {
            "total_change": out["change"]["total_amount_due"],
            "pushed_the_bill_up": sorted((m for m in moves if m["change"] >= 1), key=lambda m: -m["change"]),
            "pulled_the_bill_down": sorted((m for m in moves if m["change"] <= -1), key=lambda m: m["change"]),
        }
        residual = calc.r2(out["change"]["total_amount_due"] - sum(m["change"] for m in moves))
        if abs(residual) >= 1:
            out["change_drivers"]["not_explained_by_any_line"] = residual        # the statement does not add up (C10)
    out["change_explained"] = change_sentence(out)
    out["largest_purchases"] = [
        {"txn_id": t["id"], "date": t["date"], "merchant": t["merchant_key"], "amount": t["amount"]}
        for t in sorted(lines(cur, "purchase"), key=lambda t: -t["amount"])[:5]]
    return out


def build_report(record, results, prev):
    findings = results["findings"]
    s = record["statement"]
    return {
        "card_id": record["card_id"],
        "month": record["month"],
        "statement_date": s["statement_date"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "history_months": results["history_months"],
        "had_previous_statement": results["had_previous_statement"],
        "headline": {
            "total_due": s["total_amount_due"],
            "previous_total_due": prev["statement"]["total_amount_due"] if prev else None,
            "change": calc.r2(s["total_amount_due"] - prev["statement"]["total_amount_due"]) if prev else None,
            "minimum_due": s["minimum_amount_due"],
            "due_date": s["payment_due_date"],
            "alerts": sum(f["status"] == "alert" for f in findings),
        },
        "comparison": compare(record, prev),
        "alerts": [f for f in findings if f["status"] == "alert"],
        "checks_passed": [f for f in findings if f["status"] == "ok"],
        "notes": [f for f in findings if f["status"] == "info"],
        "coverage": results["coverage"],
        "llm": None,
        "ai_summary": None,
    }


def payment_advice(report):
    """The conclusion: what went wrong, what to pay and what to take up with the bank (rule groups in settings.json)."""
    cfg = settings()["payment_advice"]
    h = report["headline"]
    overcharges, refunds, verify = [], [], []
    for f in report["alerts"]:
        exp, act = f.get("expected"), f.get("actual")
        money_alert = f.get("unit") == "INR" and isinstance(exp, (int, float)) and isinstance(act, (int, float))
        printed_total = f["rule_id"] != "C10" or (money_alert and abs(act - h["total_due"]) < 0.005)
        if money_alert and f["rule_id"] in cfg["overcharge"] and printed_total and act > exp:
            overcharges.append({"id": f["id"], "rule_id": f["rule_id"], "title": f["title"], "amount": calc.r2(act - exp)})
        elif money_alert and f["rule_id"] in cfg["refund_owed"] and exp > act:
            refunds.append({"id": f["id"], "rule_id": f["rule_id"], "title": f["title"], "amount": calc.r2(exp - act)})
        else:
            verify.append({"id": f["id"], "rule_id": f["rule_id"], "title": f["title"]})
    disputed = calc.r2(sum(x["amount"] for x in overcharges))
    owed = calc.r2(sum(x["amount"] for x in refunds))
    return {"total_due": h["total_due"], "minimum_due": h["minimum_due"], "due_date": h["due_date"],
            "overcharges": overcharges, "refunds_owed": refunds, "to_verify": verify,
            "disputed_total": disputed, "refunds_total": owed,
            "suggested_payment": calc.r2(max(0.0, h["total_due"] - disputed - owed))}


def llm_facts(report, record):
    """Compact context for the month summary and chat (all masked, all numbers precomputed)."""
    return {
        "statement": statement_facts(record),
        "headline": report["headline"],
        "comparison": report["comparison"],
        "alerts": [{"id": f["id"], "rule_id": f["rule_id"], "title": f["title"], "expected": f["expected"],
                    "actual": f["actual"], "difference": f.get("difference")} for f in report["alerts"]],
        "checks_passed": [f["title"] for f in report["checks_passed"]],
        "notes": [f["title"] for f in report["notes"]],
        "emi_plans": record["emi_plans"],
    }


def fmt(value, unit="INR"):
    if value is None:
        return "-"
    if unit == "INR" and isinstance(value, (int, float)):
        return f"Rs. {value:,.2f}"
    return str(value)


def render_markdown(report, record, vault):
    """The user's view: tokens restored to real details, calculated facts kept apart from AI text."""
    h = report["headline"]
    real = vault.restore
    out = [f"# Statement audit - {record['month']}",
           "",
           f"Cardholder: {real(record['cardholder']['name'])}  |  Card: {real(record['card_id'])}  |  "
           f"Issuer: {record['issuer']}",
           f"(The AI layer only saw: {record['cardholder']['name']}, {record['card_id']})",
           "",
           f"Total due {fmt(h['total_due'])}  |  Minimum due {fmt(h['minimum_due'])}  |  Due {h['due_date']}"
           + (f"  |  Change vs last month {fmt(h['change'])}" if h["change"] is not None else ""),
           f"History used: {', '.join(report['history_months']) or 'none (first statement)'}",
           ""]
    out += ["## Summary", ""]
    if report["comparison"].get("change_explained"):
        out += [f"**Calculated fact:** {report['comparison']['change_explained']}", ""]
    if report["ai_summary"]:
        label = "AI explanation" if report["ai_summary"]["source"] == "ai" else "Summary (template)"
        out += [f"**{label}:** {real(report['ai_summary']['summary'])}", ""]
        for a in report["ai_summary"].get("actions") or []:
            out.append(f"- {real(a)}")
        out.append("")

    out += ["## Expected vs actual", "", "| Item | Rule | Expected | Actual | Status |", "|---|---|---|---|---|"]
    for f in report["alerts"]:
        out.append(f"| {real(f['title'])} | {f['rule_id']} | {fmt(f['expected'], f['unit'])} | {fmt(f['actual'], f['unit'])} | "
                   f"**Review** ({f['severity']}) |")
    for f in report["checks_passed"]:
        out.append(f"| {real(f['title'])} | {f['rule_id']} | {fmt(f['expected'], f['unit'])} | {fmt(f['actual'], f['unit'])} | OK |")
    out.append("")

    for f in report["alerts"]:
        out += [f"## {f['id']}: {real(f['title'])}", "",
                f"**Calculated fact** - expected {fmt(f['expected'], f['unit'])}, actual {fmt(f['actual'], f['unit'])}"
                + (f", difference {fmt(f['difference'])}" if f.get("difference") is not None else "") + ".",
                f"How: {real(str(f['calculation']))}" if f.get("calculation") else "", ""]
        if f.get("evidence"):
            out.append("Evidence:")
            for e in f["evidence"]:
                if "message" in e:
                    out.append(f"- statement {e['statement_month']} message: {real(e['message'])}")
                else:
                    out.append(f"- {e['txn_id']}  {e['date'] or '(no date)'}  {real(e['description'])}  {fmt(e['amount'])} {e['type'] or ''}")
            out.append("")
        ai = f.get("ai")
        if ai:
            label = "AI explanation" if ai["source"] == "ai" else "Explanation (template - AI output not used)"
            out.append(f"**{label}:** {real(ai['explanation'])}")
            if ai.get("verdict"):
                out.append(f"AI rule check: {ai['verdict']} - {real(ai['rule_check'] or '')}")
            out.append("")

    if report["notes"]:
        out += ["## Notes (not errors)", ""]
        out += [f"- {f['rule_id']}: {real(f['title'])}" + (f" - {real(str(f['calculation']))}" if f.get("calculation") else "")
                for f in report["notes"]]
        out.append("")
    evaluated = sorted(r for r, v in report["coverage"].items() if v["status"] == "evaluated")
    out += ["## Rule coverage", "", f"Evaluated this month: {', '.join(evaluated)}",
            f"Not applicable: {len(report['coverage']) - len(evaluated)} rules (no matching charge or event)", ""]
    if report["llm"]:
        out.append(f"AI explanations by {report['llm']['provider']}:{report['llm']['model']}; payloads in data/audit_log.jsonl")
    return "\n".join(out) + "\n"
