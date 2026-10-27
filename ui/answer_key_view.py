"""Judge view: compare a sample's report with the answer key. Shown only for sample statements.

This is the only UI module that reads the answer key, and it does so through tools/evaluate.py
(load_answer_key / find_statement / score_statement), so the UI scores exactly like the evaluator.
The engine (cc_audit/) never imports or reads it; tests/test_engine_isolation.py enforces that.
"""
from datetime import datetime
from functools import lru_cache

import streamlit as st

from cc_audit.config import rulebook
from tools.evaluate import DEFAULT_KEY, find_statement, load_answer_key, score_statement
from ui.components import esc, md, pill, show_value, table

TOLERANCE = 1.0          # Rs 1, as in the rulebook


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


def as_number(text):
    """'799.00' -> 799.0, '11 Sep 26' -> '2026-09-11'; anything else (e.g. '2 occurrences') -> None."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    try:
        return float(str(text).replace(",", "").replace("Rs", "").strip())
    except ValueError:
        pass
    try:
        return datetime.strptime(str(text).strip(), "%d %b %y").date().isoformat()
    except ValueError:
        return None


def same(key_value, our_value):
    a = as_number(key_value)
    if a is None or our_value is None:
        return None
    if isinstance(a, float) and isinstance(our_value, (int, float)):
        return abs(a - our_value) <= TOLERANCE
    return str(a) == str(our_value)


STATUS_PILL = {"Caught": "ok", "Correctly not flagged": "ok", "Missed": "bad", "False alarm": "bad", "Check false alarms": "inf"}


def rule_name(rule_id):
    r = rulebook().rules.get(rule_id)
    if r:
        return r["rule"]
    ne = rulebook().not_errors.get(rule_id)
    return ne["looks_wrong"] if ne else rule_id


def render(report):
    c = compare(report)
    if c is None:
        st.info("This report does not match a row of the answer key.")
        return
    md(f'<div class="stats">'
       f'<div class="stat"><span class="k">Planted errors</span><b>{c["planted"]}</b></div>'
       f'<div class="stat"><span class="k">Caught</span><b style="color:var(--ok)">{c["caught"]}</b></div>'
       f'<div class="stat"><span class="k">Missed</span><b style="color:{"var(--alert)" if c["missed"] else "var(--primary)"}">{c["missed"]}</b></div>'
       f'<div class="stat"><span class="k">False alarms</span><b style="color:{"var(--alert)" if c["false_alarms"] else "var(--primary)"}">{c["false_alarms"]}</b></div>'
       f'<div class="stat"><span class="k">Not-errors handled correctly</span><b>{c["not_errors_ok"]} of {c["not_errors"]}</b></div>'
       f'</div>')
    good = not c["missed"] and not c["false_alarms"]
    md(f'<div class="{"fact" if good else "note"}" style="font-weight:600">{esc(verdict(c))}'
       f' <span class="muted" style="font-weight:400">&middot; answer-key row {esc(c["file"])}</span></div>')

    out = []
    for r in c["rows"]:
        a = r["alert"]
        name = f'{esc(rule_name(r["rule_id"]))}<br><span class="muted" style="font-size:11.5px">{esc(r["rule_id"])}</span>'
        if r["status"] in ("Correctly not flagged", "Check false alarms"):
            key_cell = f'Not an error<br><span class="muted">{esc(r["what"])}</span>'
            ours = f'No alert &middot; {esc(r["handled"])}'
        else:
            key_cell = (f'{esc(r["key_expected"])} vs {esc(r["key_printed"])}<br><span class="muted">{esc(r["what"])}</span>'
                        if r["key_expected"] is not None else '<span class="muted">Not planted</span>')
            if a:
                ours = f'{esc(show_value(a["expected"], a["unit"]))} vs {esc(show_value(a["actual"], a["unit"]))}'
                checks = [same(r["key_expected"], a["expected"]), same(r["key_printed"], a["actual"])]
                if r["key_expected"] is not None and None not in checks:
                    ours += " " + (pill("numbers match", "ok") if all(checks) else pill("numbers differ", "inf"))
            else:
                ours = '<span class="muted">No alert</span>'
        out.append([name, key_cell, ours, pill(r["status"], STATUS_PILL[r["status"]])])
    if out:
        md(table(["Rule", "Answer key: expected vs printed", "Our report: expected vs actual", "Status"], out))
    else:
        md('<p class="muted">Nothing was planted on this statement and nothing was raised.</p>')

    with st.expander("How to read this"):
        st.markdown(
            "- **Caught**: the answer key planted this error and our report raised an alert for the same rule.\n"
            "- **Missed**: the answer key planted this error but our report did not raise it.\n"
            "- **False alarm**: our report raised an alert the answer key does not list as an error.\n"
            "- **Correctly not flagged**: something that looks wrong but is allowed (a *not-error*), and we did not "
            "raise an alert for it. *Recognised as allowed* means the report also explains why in *Good to know*.\n"
            "- **numbers match**: the answer key's expected / printed values equal our expected / actual within Rs 1 "
            "(dates must be equal). Counts such as *2 occurrences* are not compared.\n\n"
            "Rows are matched to the answer key by statement date and printed total due, with the same scoring "
            "code as `python tools/evaluate.py`. The audit engine never reads the answer key.")


def scorecard(reports):
    """reports: {sample file: masked report or None}. Renders the all-samples table with totals."""
    rows, tot = [], {"planted": 0, "caught": 0, "missed": 0, "false_alarms": 0, "not_errors": 0, "not_errors_ok": 0}
    for f, rep in reports.items():
        c = compare(rep) if rep else None
        if c is None:
            rows.append([esc(f), "-", "-", "-", "-", "-", '<span class="muted">not in the cache</span>'])
            continue
        for k in tot:
            tot[k] += c[k]
        ok = not c["missed"] and not c["false_alarms"]
        rows.append([esc(f), pill("Clean", "ok") if c["clean"] else pill("Has issues", "inf"),
                     str(c["planted"]), str(c["caught"]), str(c["missed"]), str(c["false_alarms"]),
                     pill(verdict(c), "ok" if ok else "bad")])
    md(f'<div class="stats">'
       f'<div class="stat"><span class="k">Planted errors</span><b>{tot["planted"]}</b></div>'
       f'<div class="stat"><span class="k">Caught</span><b style="color:var(--ok)">{tot["caught"]}</b></div>'
       f'<div class="stat"><span class="k">Missed</span><b>{tot["missed"]}</b></div>'
       f'<div class="stat"><span class="k">False alarms</span><b>{tot["false_alarms"]}</b></div>'
       f'<div class="stat"><span class="k">Not-errors handled correctly</span><b>{tot["not_errors_ok"]} of {tot["not_errors"]}</b></div>'
       f'</div>')
    md(table(["Sample", "Answer key", "Planted", "Caught", "Missed", "False alarms", "Verdict"], rows, num_cols=(2, 3, 4, 5)))
    return tot
