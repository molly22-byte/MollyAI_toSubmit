"""Judge view: compare a sample's report with the answer key, line by line. Shown only for sample statements.

This is the only UI module that reads the answer key, and it does so through tools/evaluate.py
(load_answer_key / find_statement / score_statement), so the UI scores exactly like the evaluator.
The engine (cc_audit/) never imports or reads it; tests/test_engine_isolation.py enforces that.
"""
import re
from functools import lru_cache

import streamlit as st

from cc_audit.config import rulebook
from tools.evaluate import DEFAULT_KEY, find_statement, load_answer_key, score_statement
from ui.components import esc, inr, md, pill, table
from ui.rules import id_pill


@lru_cache(maxsize=1)
def answer_key():
    return load_answer_key(DEFAULT_KEY)


def statement_status(statement_date, total_due):
    """'Clean' or 'Has issues' from the answer key (None if this is not one of the samples)."""
    key = answer_key()
    f = find_statement(key["statements"], statement_date, total_due)
    return None if f is None else ("Clean" if key["statements"][f]["Status"] == "CLEAN" else "Has issues")


def compare(report):
    """Everything the comparison shows, for one (masked or restored) report. None if no answer-key row matches."""
    key = answer_key()
    f = find_statement(key["statements"], report["statement_date"], report["headline"]["total_due"])
    if f is None:
        return None
    not_error_rows = [x for x in key["not_errors"] if x["File"] == f]
    score = score_statement(key["statements"][f], not_error_rows, report)
    rows = []
    alerts_by_rule = {}
    for a in report["alerts"]:
        alerts_by_rule.setdefault(a["rule_id"], []).append(a)
    for p in (x for x in key["planted"] if x["File"] == f):
        alert = (alerts_by_rule.get(p["Rule ID"]) or [None]).pop(0)
        rows.append({"rule_id": p["Rule ID"], "what": p["What was planted"], "key_expected": p["Expected value"],
                     "key_printed": p["Printed value"], "alert": alert, "status": "Caught" if alert else "Missed"})
    for rule_id, alerts in alerts_by_rule.items():
        for a in alerts:                            # raised, but not planted by the answer key
            rows.append({"rule_id": rule_id, "what": a["title"], "key_expected": None, "key_printed": None,
                         "alert": a, "status": "False alarm"})
    for case_id, handled in score["not_errors"]:
        ne = next(x for x in not_error_rows if x["Case ID"] == case_id)
        rows.append({"rule_id": case_id, "what": ne["Why it must NOT be flagged"], "key_expected": "not an error",
                     "key_printed": None, "alert": None, "handled": handled,
                     "status": "Correctly not flagged" if not score["false_alarms"] else "Check false alarms"})
    handled_ok = sum(r["status"] == "Correctly not flagged" for r in rows)
    return {"file": f, "clean": key["statements"][f]["Status"] == "CLEAN", "score": score, "rows": rows,
            "planted": len(score["planted"]), "caught": len(score["caught"]), "missed": len(score["missed"]),
            "false_alarms": len(score["false_alarms"]), "not_errors": len(not_error_rows), "not_errors_ok": handled_ok,
            "raised": len(report["alerts"])}


def verdict(c):
    if c["clean"]:
        return f"Clean statement: 0 alerts expected, {c['raised']} raised"
    return (f"{c['caught']} of {c['planted']} planted error{'s' if c['planted'] != 1 else ''} caught, "
            f"{c['false_alarms']} false alarm{'s' if c['false_alarms'] != 1 else ''}")


RESULT_PILL = {"Caught": "bad", "OK": "ok", "Missed": "bad", "False alarm": "bad"}


def rule_name(rule_id):
    r = rulebook().rules.get(rule_id)
    if r:
        return r["rule"]
    ne = rulebook().not_errors.get(rule_id)
    return ne["looks_wrong"] if ne else rule_id


def line_key(date, description, amount, kind):
    return (date, re.sub(r"\s+", " ", str(description or "")).strip(), round(float(amount or 0), 2), kind)


def by_transaction(report, record):
    """One row per statement line: what the answer key planted on it (if anything) and what our report did.

    Lines the answer key does not mark are 'OK' unless our report raised an alert for them that the key does not
    list (a false alarm). Planted errors that are not on a line (e.g. the due date) come back as statement-level rows.
    Caught / missed is decided per rule ID, as in tools/evaluate.py.
    """
    c = compare(report)
    if c is None:
        return None
    key, f, month = answer_key(), c["file"], record["month"]
    planted = [p for p in key["planted"] if p["File"] == f]
    planted_ids = {p["Rule ID"] for p in planted}
    raised = {a["rule_id"] for a in report["alerts"]}
    marked = {}
    for t in key["transactions"]:
        if t["File"] == f and t["Planted rule on this line"]:
            marked[line_key(t["Date"], t["Printed description"], t["Amount (Rs)"], t["D/C/M"])] = t["Planted rule on this line"]
    flagged = {}                                    # this month's txn_id -> rule IDs our report raised on it
    for a in report["alerts"]:
        for e in a.get("evidence") or []:
            tid = str(e.get("txn_id") or "")
            if tid.startswith(f"{month}#") and e.get("statement_month") in (None, month):
                flagged.setdefault(tid, []).append(a["rule_id"])
    lines, on_lines = [], set()
    for t in record["transactions"]:
        rule = marked.get(line_key(t["date"], t["description"], t["amount"], t["type"]))
        ours = list(dict.fromkeys(flagged.get(t["id"], [])))
        if rule:
            on_lines.add(rule)
            status = "Caught" if rule in raised else "Missed"
        else:
            status = "False alarm" if any(r not in planted_ids for r in ours) else "OK"
        lines.append({"date": t["date"], "description": t["description"], "amount": t["amount"], "type": t["type"],
                      "planted": rule, "ours": ours, "status": status})
    statement_level = [{"rule_id": p["Rule ID"], "what": p["What was planted"], "where": p["Where on statement"],
                        "expected": p["Expected value"], "printed": p["Printed value"],
                        "status": "Caught" if p["Rule ID"] in raised else "Missed"}
                       for p in planted if p["Rule ID"] not in on_lines]
    on_any_line = {r for ln in lines for r in ln["ours"]}
    for a in report["alerts"]:
        if a["rule_id"] not in planted_ids and a["rule_id"] not in on_any_line:
            statement_level.append({"rule_id": a["rule_id"], "what": a["title"], "where": "Statement",
                                    "expected": None, "printed": None, "status": "False alarm"})
    return {**c, "lines": lines, "statement_level": statement_level}


def render(report, record):
    v = by_transaction(report, record)
    if v is None:
        st.info("This report does not match a row of the answer key.")
        return
    good = not v["missed"] and not v["false_alarms"]
    md(f'<div class="kpis">'
       f'<div class="kpi"><span>Planted errors</span><b>{v["planted"]}</b></div>'
       f'<div class="kpi bad"><span>Caught</span><b>{v["caught"]}</b></div>'
       f'<div class="kpi {"bad" if v["missed"] else ""}"><span>Missed</span><b>{v["missed"]}</b></div>'
       f'<div class="kpi {"bad" if v["false_alarms"] else ""}"><span>False alarms</span><b>{v["false_alarms"]}</b></div>'
       f'</div>')
    md(f'<div class="banner {"ok" if good else "bad"}">{"&#10003;" if good else "!"} {esc(verdict(v))}</div>')

    if v["statement_level"]:
        md('<div class="label" style="margin-top:14px">Statement-level items</div>')
        for r in v["statement_level"]:
            values = (f'<span class="muted">expected {esc(r["expected"])} &middot; printed {esc(r["printed"])}</span>'
                      if r["expected"] is not None else "")
            md(f'<div class="srow">{id_pill(r["rule_id"])}<span class="t"><b>{esc(r["where"])}</b> &middot; '
               f'{esc(r["what"])}<br>{values}</span>{pill(r["status"], RESULT_PILL[r["status"]])}</div>')

    md('<div class="label" style="margin-top:14px">Every transaction on this statement</div>')
    rows = []
    for ln in v["lines"]:
        key_cell = (f'{id_pill(ln["planted"])} <span class="muted">planted</span>' if ln["planted"]
                    else '<span class="muted">OK</span>')
        ours = " ".join(id_pill(r) for r in ln["ours"]) or '<span class="muted">-</span>'
        rows.append([esc(ln["date"] or "-"), esc(ln["description"]), esc(inr(ln["amount"])), key_cell, ours,
                     pill(ln["status"], RESULT_PILL[ln["status"]])])
    md(table(["Date", "Description", "Amount", "Answer key", "MollyAI flagged", "Result"], rows, num_cols=(2,)))
    ne = [f'{id_pill(case_id)} {pill("not flagged", "ok")}' for case_id, _ in v["score"]["not_errors"]]
    if ne:
        md(f'<p class="muted" style="margin-top:8px">Looks wrong but allowed: {" ".join(ne)}</p>')
