"""Judge-written rules (J01, J02, ...): fill-in templates evaluated on a ledger record, with a fallback.

Judges cannot type code: a rule is a small dict built from four templates, every value is checked
against fixed lists and number ranges when the rule is saved, and evaluate() never raises - a rule that
cannot run returns one "error" finding saying why, and every other rule still runs. Rules live in the
judge's own browser session and run in this UI layer after the audit engine, on the same masked ledger
record; the engine (cc_audit/) never sees them.

Findings have the engine's shape (rule_id, status, title, expected, actual, unit, evidence) plus
"custom": True, so reports and the Error Lab can show and score them like any other alert.
"""
import copy
import math
from datetime import date

MAX_RULES = 20
MAX_TEXT = 60
CATEGORIES = ["purchase", "payment", "refund", "fee", "gst", "finance_charge", "forex_markup", "emi_principal",
              "emi_interest", "emi_transfer"]
CATEGORY_LABELS = {"purchase": "purchases", "payment": "payments", "refund": "refunds / credits", "fee": "fees",
                   "gst": "GST", "finance_charge": "finance charges (interest)", "forex_markup": "forex markup",
                   "emi_principal": "EMI instalments", "emi_interest": "EMI interest", "emi_transfer": "EMI transfers"}
FIELDS = {"total_amount_due": "Total amount due", "minimum_amount_due": "Minimum amount due",
          "credit_limit": "Credit limit", "available_credit": "Available credit",
          "available_cash_limit": "Available cash limit", "previous_balance": "Previous balance",
          "payments": "Payments (summary box)", "purchases_emi_instalments": "Purchases + EMI (summary box)",
          "fees_interest_gst": "Fees + interest + GST (summary box)",
          "days_to_due": "Days from statement date to due date"}
OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
       "=": lambda a, b: abs(a - b) <= 0.005, "!=": lambda a, b: abs(a - b) > 0.005}
TEXT_OPS = ["contains", "does not contain", "starts with"]
KINDS = {"line": "Line rule - flag single lines", "total": "Total rule - flag a sum or count of lines",
         "field": "Field rule - flag a statement field", "ratio": "Ratio rule - one total should be a share of another"}
SEVERITIES = ["low", "medium", "high"]

EXAMPLES = [
    {"name": "Any single fee over Rs 1,000", "kind": "line", "categories": ["fee"], "amount_op": ">", "amount": 1000.0,
     "severity": "medium"},
    {"name": "Minimum due below 5% of the total due", "kind": "field", "field": "minimum_amount_due", "op": "<",
     "compare_to": "field", "other_field": "total_amount_due", "factor": 0.05, "severity": "high"},
    {"name": "Fees + interest above Rs 2,000 in one month", "kind": "total", "agg": "sum",
     "categories": ["fee", "finance_charge"], "op": ">", "value": 2000.0, "severity": "medium"},
    {"name": "GST must be 18% of fees, interest and markup", "kind": "ratio", "categories": ["gst"],
     "categories_b": ["fee", "finance_charge", "forex_markup", "emi_interest"], "factor": 0.18, "tolerance": 1.0,
     "severity": "high"},
]


# ---------------------------------------------------------------- checking a rule before it is saved

def _number(rule, key, errors, low=-1e12, high=1e12):
    v = rule.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or math.isnan(v) or not low <= v <= high:
        errors.append(f"'{key}' must be a number between {low:g} and {high:g}.")


def validate(rule):
    """Problems with a rule (empty list = it can be saved). Only fixed choices and bounded numbers are accepted."""
    errors = []
    if not isinstance(rule, dict):
        return ["A rule must be a set of choices from the form."]
    name = rule.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_TEXT:
        errors.append(f"Give the rule a name (1-{MAX_TEXT} characters).")
    kind = rule.get("kind")
    if kind not in KINDS:
        return errors + [f"Rule type must be one of: {', '.join(KINDS)}."]
    if rule.get("severity", "medium") not in SEVERITIES:
        errors.append("Severity must be low, medium or high.")
    cats = rule.get("categories") or []
    if not isinstance(cats, list) or any(c not in CATEGORIES for c in cats):
        errors.append("Unknown transaction category.")
    when = rule.get("when")
    if when is not None:
        if (not isinstance(when, dict) or not isinstance(when.get("categories"), list) or not when["categories"]
                or any(c not in CATEGORIES for c in when["categories"]) or when.get("agg", "sum") not in ("sum", "count")
                or when.get("op") not in OPS):
            errors.append("'Only check when': choose the lines, sum or count, a comparison and a value.")
        else:
            _number(when, "value", errors, 0, 1e9)
    if kind in ("line", "total", "ratio"):
        text = rule.get("text") or ""
        if text and (not isinstance(text, str) or len(text) > MAX_TEXT or rule.get("text_op") not in TEXT_OPS):
            errors.append(f"Description filter: pick contains / does not contain / starts with and up to {MAX_TEXT} characters.")
    if kind == "line":
        if rule.get("amount_op") not in (None, *OPS):
            errors.append("Unknown amount comparison.")
        if rule.get("amount_op"):
            _number(rule, "amount", errors, 0, 1e9)
        if not rule.get("amount_op") and not (rule.get("text") or "").strip() and not cats:
            errors.append("A line rule needs at least one condition (category, description or amount).")
    elif kind == "total":
        if rule.get("agg") not in ("sum", "count"):
            errors.append("Total rule: choose sum or count.")
        if rule.get("op") not in OPS:
            errors.append("Unknown comparison.")
        _number(rule, "value", errors, 0, 1e9)
    elif kind == "field":
        if rule.get("field") not in FIELDS:
            errors.append("Unknown statement field.")
        if rule.get("op") not in OPS:
            errors.append("Unknown comparison.")
        if rule.get("compare_to") == "field":
            if rule.get("other_field") not in FIELDS:
                errors.append("Unknown field to compare with.")
            _number(rule, "factor", errors, 0, 1000)
        elif rule.get("compare_to") == "value":
            _number(rule, "value", errors)
        else:
            errors.append("Compare with a number or with another field.")
    elif kind == "ratio":
        if not (cats or (rule.get("text") or "").strip()) or not rule.get("categories_b") \
                or any(c not in CATEGORIES for c in rule.get("categories_b") or []):
            errors.append("Ratio rule: choose the lines to check (by category and/or description) and the lines "
                          "they are a share of.")
        if not isinstance(rule.get("only_if_present", False), bool):
            errors.append("'Only when such lines are on the statement' must be on or off.")
        _number(rule, "factor", errors, 0, 100)
        _number(rule, "tolerance", errors, 0, 1e6)
    return errors


def describe(rule):
    """The rule as one plain-English sentence."""
    try:
        return _describe(rule) + _describe_when(rule.get("when"))
    except Exception:                                   # describing must never break the page either
        return "(this rule cannot be described - check its settings)"


def _describe_when(when):
    if not when:
        return ""
    cats = " / ".join(CATEGORY_LABELS[c] for c in when["categories"])
    what = "total amount" if when.get("agg", "sum") == "sum" else "number"
    value = f"Rs {when['value']:,.2f}" if when.get("agg", "sum") == "sum" else f"{when['value']:g}"
    return f" Only checked when the {what} of {cats} lines is {when['op']} {value}."


def _describe(rule):
    try:
        cats = " / ".join(CATEGORY_LABELS[c] for c in rule.get("categories") or []) or "any"
        text = f" whose description {rule['text_op']} '{rule['text']}'" if (rule.get("text") or "").strip() else ""
        if rule["kind"] == "line":
            amount = f" with amount {rule['amount_op']} Rs {rule['amount']:,.2f}" if rule.get("amount_op") else ""
            return f"Flag every {cats} line{text}{amount}."
        if rule["kind"] == "total":
            what = "total amount" if rule["agg"] == "sum" else "number"
            unit = f"Rs {rule['value']:,.2f}" if rule["agg"] == "sum" else f"{rule['value']:g}"
            return f"Flag if the {what} of {cats} lines{text} is {rule['op']} {unit}."
        if rule["kind"] == "field":
            other = (f"{rule['factor']:g} x {FIELDS[rule['other_field']]}" if rule["compare_to"] == "field"
                     else f"{rule['value']:,.2f}")
            return f"Flag if {FIELDS[rule['field']]} {rule['op']} {other}."
        b = " / ".join(CATEGORY_LABELS[c] for c in rule["categories_b"])
        present = " Lines like this must be on the statement to be checked." if rule.get("only_if_present") else ""
        return (f"Flag if {cats} lines{text} do not add up to {rule['factor']:g} x the {b} lines "
                f"(within Rs {rule['tolerance']:g}).{present}")
    except Exception:                                   # describing must never break the page either
        return "(this rule cannot be described - check its settings)"


# ---------------------------------------------------------------- evaluation (never raises)

def _evidence(t):
    return {"txn_id": t.get("id"), "date": t.get("date"), "description": t.get("description"),
            "amount": t.get("amount"), "type": t.get("type")}


def _matches(rule, t):
    cats = rule.get("categories") or []
    if cats and t.get("category") not in cats:
        return False
    text = (rule.get("text") or "").strip().upper()
    if text:
        desc = (t.get("description") or "").upper()
        hit = desc.startswith(text) if rule.get("text_op") == "starts with" else text in desc
        if (rule.get("text_op") == "does not contain") == hit:
            return False
    return True


def _field(record, name):
    if name == "days_to_due":
        s = record["statement"]
        return float((date.fromisoformat(s["payment_due_date"]) - date.fromisoformat(s["statement_date"])).days)
    for part in ("statement", "account_summary"):
        if name in record.get(part, {}):
            return float(record[part][name])
    raise KeyError(f"field {name} is not on this statement")


def _finding(rule, status, title, expected=None, actual=None, evidence=(), fields=()):
    return {"rule_id": rule["id"], "status": status, "title": title, "expected": expected, "actual": actual,
            "unit": "INR", "evidence": list(evidence), "fields": list(fields), "related_rules": [],
            "severity": rule.get("severity", "medium") if status == "alert" else None, "custom": True}


def _run(rule, record):
    lines = [t for t in record.get("transactions", []) if _matches(rule, t)]
    name = rule["name"]
    when = rule.get("when")
    if when:
        rows = [t for t in record.get("transactions", []) if t.get("category") in when["categories"]]
        value = round(sum(float(t["amount"]) for t in rows), 2) if when.get("agg", "sum") == "sum" else float(len(rows))
        if not OPS[when["op"]](value, when["value"]):
            return [{**_finding(rule, "ok", f"{name}: not checked - condition not met ({value:,.2f})", actual=value),
                     "skipped": True}]
    if rule["kind"] == "line":
        hits = [t for t in lines if not rule.get("amount_op") or OPS[rule["amount_op"]](float(t["amount"]), rule["amount"])]
        if not hits:
            return [_finding(rule, "ok", f"{name}: no line matches", actual=0)]
        return [_finding(rule, "alert", f"{name}: {t['description']}", rule.get("amount"), t["amount"], [_evidence(t)])
                for t in hits]
    if rule["kind"] == "total":
        actual = round(sum(float(t["amount"]) for t in lines), 2) if rule["agg"] == "sum" else float(len(lines))
        flagged = OPS[rule["op"]](actual, rule["value"])
        return [_finding(rule, "alert" if flagged else "ok", f"{name}: {rule['agg']} = {actual:,.2f}", rule["value"],
                         actual, [_evidence(t) for t in lines])]
    if rule["kind"] == "field":
        actual = _field(record, rule["field"])
        expected = (round(rule["factor"] * _field(record, rule["other_field"]), 2) if rule["compare_to"] == "field"
                    else rule["value"])
        flagged = OPS[rule["op"]](actual, expected)
        fields = [rule["field"]] + ([rule["other_field"]] if rule["compare_to"] == "field" else [])
        fields += ["payment_due_date"] if "days_to_due" in fields else []
        return [_finding(rule, "alert" if flagged else "ok", f"{name}: {FIELDS[rule['field']]} = {actual:,.2f}",
                         expected, actual, fields=fields)]
    if rule.get("only_if_present") and not lines:
        return [{**_finding(rule, "ok", f"{name}: no such line on this statement", 0.0, 0.0), "skipped": True}]
    base = sum(float(t["amount"]) for t in record.get("transactions", [])
               if t.get("category") in rule["categories_b"])
    expected = round(rule["factor"] * base, 2)
    actual = round(sum(float(t["amount"]) for t in lines), 2)
    if not lines and not base:
        return [_finding(rule, "ok", f"{name}: nothing to compare on this statement", 0.0, 0.0)]
    flagged = abs(actual - expected) > rule["tolerance"]
    return [_finding(rule, "alert" if flagged else "ok", f"{name}: {actual:,.2f} vs expected {expected:,.2f}",
                     expected, actual, [_evidence(t) for t in lines])]


def evaluate(rule, record):
    """Findings for one rule. Never raises: a rule that cannot run gives one 'error' finding with the reason."""
    try:
        problems = validate(rule)
        if problems:
            raise ValueError(problems[0])
        return _run(rule, record)
    except Exception as e:                              # the fallback: report it, keep everything else running
        reason = str(e) or type(e).__name__
        return [_finding({"id": (rule or {}).get("id", "J??") if isinstance(rule, dict) else "J??", "severity": None},
                         "error", f"{(rule or {}).get('name', 'Custom rule') if isinstance(rule, dict) else 'Custom rule'}"
                                  f" could not be evaluated: {reason}")]


def evaluate_all(rules, record):
    findings = []
    for rule in rules or []:
        findings += evaluate(rule, record)
    for i, f in enumerate(findings, 1):
        f["id"] = f"{record.get('month', '')}-{f['rule_id']}-{i:02d}"
    return findings


def result_label(findings):
    """'passes' / 'flagged N line(s)' / 'could not evaluate' for one rule's findings."""
    if any(f["status"] == "error" for f in findings):
        return "could not evaluate"
    alerts = [f for f in findings if f["status"] == "alert"]
    if alerts:
        return f"flagged {len(alerts)}"
    return "not checked" if any(f.get("skipped") for f in findings) else "passes"


# ---------------------------------------------------------------- Error Lab: which judge rules look at a change

def covering(rules, change, category=None):
    """IDs of judge rules that check what a change touched (a line of this category, or this header field)."""
    out = []
    w = change.get("where", {})
    for r in rules or []:
        try:
            if w.get("kind") == "field":
                fields = {r.get("field"), r.get("other_field") if r.get("compare_to") == "field" else None}
                if "days_to_due" in fields:
                    fields.add("payment_due_date")
                if r.get("kind") == "field" and w.get("field") in fields:
                    out.append(r["id"])
            elif category and r.get("kind") in ("line", "total", "ratio"):
                cats = set(r.get("categories") or []) | set(r.get("categories_b") or [])
                cats |= set((r.get("when") or {}).get("categories") or [])
                if not r.get("categories") or category in cats:          # no category = any line (e.g. by description)
                    out.append(r["id"])
        except Exception:                               # coverage is a hint; never let it break the page
            continue
    return out


def new_rule(rules, rule):
    """Validated copy of `rule` with the next free ID (J01...), or raise ValueError with the problems."""
    if len(rules) >= MAX_RULES:
        raise ValueError(f"At most {MAX_RULES} custom rules per session.")
    problems = validate(rule)
    if problems:
        raise ValueError(" ".join(problems))
    used = {r["id"] for r in rules}
    rid = next(f"J{i:02d}" for i in range(1, 100) if f"J{i:02d}" not in used)
    return {**copy.deepcopy(rule), "id": rid, "name": rule["name"].strip()}


# ---------------------------------------------------------------- saved rules (shared by everyone with the judge passkey)
# One JSON file on the server, so rules survive a refresh, Lock and new sessions. Judges share one passkey,
# so the file is shared by all judges; public visitors never see it. On Streamlit Cloud the file lasts until
# the app restarts. Writes are atomic (temp file + rename) and serialised; a damaged file is set aside, never fatal.
import json as _json
import os as _os
import threading as _threading
import time as _time
from pathlib import Path as _Path

_LOCK = _threading.Lock()


def rules_file():
    default = _Path(__file__).resolve().parent.parent / "judge_rules" / "rules.json"
    return _Path(_os.environ.get("MOLLYAI_JUDGE_RULES") or default)


def load_rules():
    """Saved rules ([] if none). Entries that are not even rule-shaped are skipped; rules with bad values are kept,
    so evaluate()'s fallback can report them and a judge can delete them. A file that is not JSON is set aside."""
    path = rules_file()
    with _LOCK:
        if not path.exists():
            return []
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            try:
                path.replace(path.with_name(f"rules.broken-{int(_time.time())}.json"))
            except OSError:
                pass
            return []
    rules = data.get("rules") if isinstance(data, dict) else None
    return [r for r in rules or [] if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"].startswith("J")]


def save_rules(rules):
    path = rules_file()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".rules-{_os.getpid()}-{_threading.get_ident()}.tmp")
        tmp.write_text(_json.dumps({"rules": rules}, indent=2, ensure_ascii=False), encoding="utf-8")
        _os.replace(tmp, path)


def add_saved(rule):
    """Validate, give the next free ID, save. Returns the saved rule; raises ValueError with the problems."""
    rules = load_rules()
    saved = new_rule(rules, rule)
    save_rules(rules + [saved])
    return saved


def delete_saved(rule_id):
    save_rules([r for r in load_rules() if r.get("id") != rule_id])
