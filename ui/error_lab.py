"""Error Lab page: a judge plants errors in a clean sample statement and checks whether the agent flags them.

The judge becomes the answer key. Scoring logic lives in ui/error_lab_presets.py (shared with
tools/error_injection_test.py); this module only draws the page and keeps the page state.
"""
import copy
import json
import math
import random

import pandas as pd
import streamlit as st

from cc_audit.llm import get_llm
from cc_audit.parser import extract, line_details
from ui import answer_key_view, custom_rules, rules, samples
from ui import error_lab_presets as lab
from ui.components import esc, md, pill, table

STATUS_PILL = {"CAUGHT": "ok", "CORRECTLY IGNORED": "ok", "MISSED": "bad", "FALSE ALARM": "bad",
               "NOT COVERED BY POLICY": "inf"}


def clean_samples():
    """The clean sample statements (0 known errors) - the only starting points the lab offers."""
    entries = samples.manifest()
    return [f for f in samples.sample_files()
            if f in entries and answer_key_view.statement_status(entries[f]["statement_date"], entries[f]["total_due"]) == "Clean"]


def load(file):
    raw = extract(samples.SAMPLES / file)
    ctx = lab.context(file)
    stmt, _ = lab.finalize(raw, lab.prepare(raw))
    baseline_record, baseline_report = lab.run(file, stmt)       # the untouched statement (deterministic rules only)
    st.session_state.lab = {"file": file, "raw": raw, "ctx": ctx, "baseline": baseline_report["alerts"],
                            "baseline_record": baseline_record, "base": lab.prepare(raw),
                            "changes": [], "n": 0, "result": None, "expect": {}}


def judge_rules():
    """The saved custom rules the judge left ticked in the Error Lab (all of them by default)."""
    off = st.session_state.get("lab_rules_off", set())
    return [r for r in custom_rules.load_rules() if r["id"] not in off]


def judge_cover(ch, edited):
    w = ch["where"]
    row = lab.find_row(edited, w["rid"]) if w["kind"] == "txn" else None
    return custom_rules.covering(judge_rules(), ch, row["category"] if row else w.get("category"))


def rule_checklist(s, go_customrules):
    """Section 4: which saved custom rules to check with. A deleted rule disappears here (and from old results)."""
    saved = custom_rules.load_rules()
    off = st.session_state.setdefault("lab_rules_off", set())
    with st.container(border=True):
        md('<div class="sec">4. Also check with your custom rules <span class="muted">&middot; optional</span></div>')
        if not saved:
            st.caption("No custom rules saved yet.")
        for r in saved:
            on = st.checkbox(f"{r['id']} · {r.get('name', '(no name)')}", value=r["id"] not in off,
                             key=f"lab_rule_{r['id']}", help=custom_rules.describe(r))
            (off.discard if on else off.add)(r["id"])
        if go_customrules:
            st.button("Write or delete custom rules", key="lab_to_rules", on_click=go_customrules)
    ids = {r["id"] for r in saved}
    if s["result"] and any(j not in ids for j in s["result"].get("judge_rules", [])):
        s["result"] = None                          # it used a rule that has since been deleted
        st.info("A custom rule used in the last run was deleted, so that result was cleared - run the audit again.")


def with_judge_rules(changes, edited, record, report, baseline_alerts, baseline_record):
    """Add the judge's own rules (J01...) to the scoring: their flags count as alerts, their scope as coverage."""
    jr = judge_rules()
    if not jr:
        return changes, report, baseline_alerts, []
    found = custom_rules.evaluate_all(jr, record)
    base = [f for f in custom_rules.evaluate_all(jr, baseline_record) if f["status"] == "alert"]
    scored = [{**ch, "rules": list(dict.fromkeys(ch["rules"] + judge_cover(ch, edited)))} for ch in changes]
    report = {**report, "alerts": report["alerts"] + [f for f in found if f["status"] == "alert"]}
    errors = [f for f in found if f["status"] == "error"]
    for rid in sorted({f["rule_id"] for f in base}):
        errors.append({"rule_id": rid, "title": f"{rid} already flags the unedited statement, so it cannot tell your "
                                                f"change apart (a flag only counts if the change causes it). Narrow the "
                                                f"rule with a description filter or an 'Only check when' condition."})
    return scored, report, baseline_alerts + base, errors


# ---------------------------------------------------------------- editors <-> working copy

def header_frame(work):
    return pd.DataFrame([{"Field": label, "Value": str(lab.header_value(work, field))}
                         for _, field, label in lab.HEADER_FIELDS])


def txn_frame(work):
    return pd.DataFrame([{"_rid": t["_rid"], "date": t["date"] or "", "description": t["description"],
                          "amount": t["amount"], "type": t["type"] or "", "category": t["category"]}
                         for t in work["transactions"]])


def text(value):
    return "" if value is None or (isinstance(value, float) and math.isnan(value)) else str(value).strip()


def number(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return text(value)                       # left as text: check_inputs() reports it


def from_editors(base, hdr, txns):
    work = copy.deepcopy(base)
    for (part, field, _), value in zip(lab.HEADER_FIELDS, hdr["Value"].tolist()):
        work[part][field] = text(value) if field == "payment_due_date" else number(value)
    by_rid, used, rows = {t["_rid"]: t for t in base["transactions"]}, set(), []
    for rec in txns.to_dict("records"):
        rid = rec.get("_rid")
        rid = int(rid) if rid is not None and not (isinstance(rid, float) and math.isnan(rid)) else None
        values = {"date": text(rec.get("date")) or None, "description": text(rec.get("description")),
                  "amount": number(rec.get("amount")), "type": text(rec.get("type")) or None}
        if rid in by_rid and rid not in used:
            used.add(rid)
            t = copy.deepcopy(by_rid[rid])
            for col, v in values.items():
                if t.get(col) != v:
                    lab.set_cell(t, col, v)
        else:
            t = {**values, **line_details(values["description"], lab.section_for(values["description"], values["type"]),
                                          values["type"]), "_rid": lab.new_rid(work)}
        rows.append(t)
    work["transactions"] = rows
    return work


def signature(change):
    return json.dumps([change["where"], change.get("column"), change.get("preset")], sort_keys=True, default=str)


# ---------------------------------------------------------------- page

def page(on_open_rule, go_report, live_runs_left, count_live_run, llm_ok, go_customrules=None):
    md('<div class="fact"><div class="label">How the Error Lab works</div>Pick a clean sample statement, plant up to '
       f'{lab.MAX_CHANGES} errors (one-click presets or your own edits), say what you expect, and run the audit. '
       '<b>Edits are applied to the parsed statement data - after PDF extraction, before masking and the rules '
       'engine.</b> The engine only receives the edited statement; your list of changes and expectations stay on '
       'this page and are used only to score the result afterwards.</div>')
    files = clean_samples()
    if not files or not samples.WORKSPACE.exists():
        st.warning("The sample cache is missing - run python tools/build_sample_cache.py.")
        return
    entries = samples.manifest()
    current = st.session_state.get("lab", {}).get("file")
    file = st.selectbox("Clean sample statement (0 known errors)", files, key="lab_file",
                        index=files.index(current) if current in files else 0,     # keep the lab when coming back
                        format_func=lambda f: f"{entries[f]['card']} · {entries[f]['month']}  ({f})")
    if st.session_state.get("lab", {}).get("file") != file:
        with st.spinner("Reading the statement..."):
            load(file)
    s = st.session_state.lab
    month = entries[file]["month"]
    if not s["ctx"]["prev"]:
        st.caption(f"{month} is this card's first statement, so checks that compare with last month "
                   "(late fees, interest, price increases) cannot run on it. Pick a June statement for those.")

    # ---- quick errors
    with st.container(border=True):
        md('<div class="sec">1. Quick errors <span class="muted">&middot; one click each</span></div>')
        options = [p["id"] for p in lab.PRESETS]
        ok = {pid: lab.PRESET_BY_ID[pid]["applies"](s["base"], s["ctx"]) for pid in options}
        pid = st.selectbox("Preset error", options, key="lab_preset",
                           format_func=lambda p: f"{lab.PRESET_BY_ID[p]['label']}  ->  should trigger "
                                                 f"{', '.join(lab.PRESET_BY_ID[p]['rules'])}"
                                                 + ("" if ok[p] else "  (not available on this statement)"))
        rules.rule_chips(lab.PRESET_BY_ID[pid]["rules"], f"preset_{pid}", on_open_rule, label="Should trigger:")
        if not ok[pid]:
            st.caption(f"Needs {lab.PRESET_BY_ID[pid]['needs']}.")
        add = st.button("Add this error", disabled=not ok[pid], key="lab_add")

    # ---- manual edits
    with st.container(border=True):
        md('<div class="sec">2. Manual edit <span class="muted">&middot; change any cell, add or delete a line</span></div>')
        hdr = st.data_editor(header_frame(s["base"]), key=f"lab_hdr_{s['n']}", hide_index=True, width="stretch",
                             disabled=["Field"], column_config={"Value": st.column_config.TextColumn("Value")})
        ro = " · ".join(f"{label}: {lab.header_value(s['base'], field)}" for _, field, label in lab.READ_ONLY_FIELDS)
        st.caption(f"{ro} (fixed - they decide which month this is). Finance charges, GST and fees are transaction lines below.")
        txns = st.data_editor(
            txn_frame(s["base"]), key=f"lab_txn_{s['n']}", hide_index=True, width="stretch", num_rows="dynamic",
            column_order=["date", "description", "amount", "type", "category"], disabled=["category"],
            column_config={"date": st.column_config.TextColumn("Date (YYYY-MM-DD)"),
                           "description": st.column_config.TextColumn("Description", width="large"),
                           "amount": st.column_config.NumberColumn("Amount (Rs)", format="%.2f", min_value=0.0),
                           "type": st.column_config.SelectboxColumn("Type", options=["D", "C", "M", ""],
                                                                    help="D debit, C credit, M EMI instalment"),
                           "category": st.column_config.TextColumn("Category (set by the parser)")})
        keep = st.toggle("Keep the statement's own totals consistent with my edits (recommended)", value=True,
                         key="lab_keep", help="Carries line edits through to GST, the summary boxes, total due and minimum "
                                              "due, as the bank would print them - so only the error you planted is "
                                              "wrong. Header values you change yourself are kept.")

    edited = from_editors(s["base"], hdr, txns)
    pending = lab.diff(s["base"], edited)
    if add:
        if len(s["changes"]) + len(pending) >= lab.MAX_CHANGES:
            st.error(f"At most {lab.MAX_CHANGES} changes per run.")
        else:
            work = copy.deepcopy(edited)
            try:
                change = lab.apply_preset(work, pid, random.Random(), s["ctx"])
            except ValueError as e:
                st.error(str(e))
            else:
                s["changes"] += pending + [change]       # freeze the manual edits made so far
                s["base"], s["n"], s["result"] = work, s["n"] + 1, None
                st.rerun()
    changes = s["changes"] + pending
    rule_checklist(s, go_customrules)

    # ---- your changes and expectations
    with st.container(border=True):
        md(f'<div class="sec">3. Your changes <span class="muted">&middot; {len(changes)} of {lab.MAX_CHANGES}</span></div>')
        if not changes:
            st.caption("No changes yet.")
        for i, ch in enumerate(changes):
            sig = signature(ch)
            c1, c2 = st.columns([5, 1.6], vertical_alignment="center")
            if ch["source"] == "preset":
                what = f'<b>{esc(ch["label"])}</b> <span class="muted">(preset)</span><br><span class="muted">{esc(ch["note"])}</span>'
            else:
                what = (f'<b>{esc(ch["label"])}</b> &middot; {esc(ch["column"])}<br><span class="muted">'
                        f'{esc(ch["old"]) if ch["old"] is not None else "-"} &rarr; {esc(ch["new"]) if ch["new"] is not None else "-"}</span>')
            covered = ", ".join(dict.fromkeys(ch["rules"] + judge_cover(ch, edited))) or "no rule"
            c1.markdown(f'{what}<br><span class="muted">Checked by: {esc(covered)}</span>', unsafe_allow_html=True)
            s["expect"][sig] = c2.checkbox("Should be flagged", value=s["expect"].get(sig, True), key=f"lab_expect_{i}_{s['n']}")
            ch["should_flag"] = s["expect"][sig]
        if changes:
            if st.button("Start over", key="lab_reset"):
                load(file)
                st.rerun()

    if judge_rules():
        st.caption("Ticked custom rules (" + ", ".join(r["id"] for r in judge_rules()) + ") run on this statement too, "
                   "after the audit engine. A change one of them flags counts as CAUGHT.")
    errors = lab.check_inputs(edited, changes)
    for e in errors:
        st.error(e)
    live_left = live_runs_left()
    use_ai = llm_ok and live_left > 0
    st.caption("With Nemotron explanations (uses one of your live AI runs)." if use_ai else
               "AI text is skipped for this run (live AI runs used up or no API key) - the rules run exactly the same.")
    if st.button("Run the audit on my edited statement", type="primary", disabled=bool(errors) or not changes, key="lab_run"):
        with st.spinner("Running masking, rules" + (" and Nemotron (30-60 seconds per alert)..." if use_ai else "...")):
            final, rid_to_txn = lab.finalize(s["raw"], edited, keep_totals=keep,
                                             gst_edited=any(c.get("gst_edited") for c in changes))
            record, report = lab.run(file, final, use_llm=use_ai, llm=get_llm() if use_ai else None)
        if use_ai:
            count_live_run()
        scored, report, baseline, rule_errors = with_judge_rules(changes, edited, record, report, s["baseline"],
                                                                 s["baseline_record"])
        s["result"] = {**lab.score(scored, report, baseline, rid_to_txn), "card": record["card_id"],
                       "month": record["month"], "ai": use_ai, "rule_errors": rule_errors,
                       "judge_rules": [r["id"] for r in judge_rules()]}
        s["changes"], s["base"], s["n"] = changes, edited, s["n"] + 1    # keep the grid edits when leaving the page
        st.rerun()

    if s["result"]:
        results(s["result"], on_open_rule, go_report)


def results(r, on_open_rule, go_report):
    with st.container(border=True):
        md('<div class="sec">Did the agent catch it?</div>')
        t = r["tally"]
        md(f'<div class="stats">'
           f'<div class="stat"><span class="k">Changes made</span><b>{r["changes"]}</b></div>'
           f'<div class="stat"><span class="k">Caught</span><b style="color:var(--ok)">{t["CAUGHT"]}</b></div>'
           f'<div class="stat"><span class="k">Missed</span><b style="color:{"var(--alert)" if t["MISSED"] else "var(--primary)"}">{t["MISSED"]}</b></div>'
           f'<div class="stat"><span class="k">Not covered</span><b>{t["NOT COVERED BY POLICY"]}</b></div>'
           f'</div>')
        if t["CORRECTLY IGNORED"] or t["FALSE ALARM"]:
            st.caption(f"Correctly ignored: {t['CORRECTLY IGNORED']} · False alarms: {t['FALSE ALARM']}")
        rows = []
        for row in r["rows"]:
            change = esc(row["note"] if row["source"] == "preset" else f'{row["label"]} · {row["column"]}: {row["old"]} -> {row["new"]}')
            expected = ("should be flagged" if row["should_flag"] else "should NOT be flagged") + (
                f'<br><span class="muted">{esc(", ".join(row["rules"]) or "no rule covers this")}</span>')
            flags = "<br>".join(f'{pill(f["rule_id"], "code")} {esc(f["title"])}' for f in row["flags"]) or '<span class="muted">none</span>'
            if row["why_missed"]:
                flags += f'<br><span class="muted">{esc("; ".join(row["why_missed"]))}</span>'
            rows.append([change, expected, flags, pill(row["status"], STATUS_PILL[row["status"]])])
        md(table(["Change", "Expected", "Flags raised for it", "Status"], rows))
        if t["NOT COVERED BY POLICY"]:
            md(f'<div class="note">{esc(lab.NOT_COVERED_MESSAGE)} You can also write such a rule yourself on the '
               f'Custom rules page and run this again.</div>')
        for f in r.get("rule_errors", []):
            md(f'<div class="note">{pill(f["rule_id"], "code")} {esc(f["title"])}</div>')
        if r["related"]:
            md("<b>Related flags</b> <span class=\"muted\">- extra alerts your changes caused elsewhere (e.g. a total "
               "that no longer adds up). They are not false alarms.</span>")
            md("<ul>" + "".join(f'<li>{pill(f["rule_id"], "code")} {esc(f["title"])}</li>' for f in r["related"]) + "</ul>")
        ids = [f["rule_id"] for row in r["rows"] for f in row["flags"]] + [x for row in r["rows"] for x in row["rules"]] \
            + [f["rule_id"] for f in r["related"]]
        rules.rule_chips(ids, "lab_result", on_open_rule)
        st.button("Open the full report for this run" + (" (with Nemotron explanations)" if r["ai"] else ""),
                  key="lab_open_report", on_click=go_report, args=(r["card"], r["month"]))
