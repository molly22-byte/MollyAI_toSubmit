"""Custom rules page (judges only): build a rule from a template, then test it on the sample statements."""
import json

import streamlit as st

from ui import custom_rules as cr
from ui import samples
from ui.components import esc, md, pill, table
from ui.rules import id_pill

RESULT_PILL = {"passes": "ok", "could not evaluate": "inf", "not checked": "code"}


def rules_in_session():
    return cr.load_rules()


def sample_record(file):
    e = samples.manifest()[file]
    return json.loads((samples.WORKSPACE / "ledger" / e["card"] / f"{e['month']}.json").read_text(encoding="utf-8"))


def builder():
    """The form; returns the rule dict as it stands (not yet validated or saved)."""
    c1, c2, c3 = st.columns([2.2, 2, 1])
    name = c1.text_input("Rule name", key="cr_name", max_chars=cr.MAX_TEXT, placeholder="e.g. Any single fee over Rs 1,000")
    kind = c2.selectbox("Rule type", list(cr.KINDS), format_func=cr.KINDS.get, key="cr_kind")
    severity = c3.selectbox("Severity", cr.SEVERITIES, index=1, key="cr_severity")
    rule = {"name": name, "kind": kind, "severity": severity}
    cat_label = lambda c: cr.CATEGORY_LABELS[c]                                     # noqa: E731
    if kind in ("line", "total", "ratio"):
        c1, c2, c3 = st.columns([2.2, 1.2, 1.8])
        rule["categories"] = c1.multiselect("Lines to check (leave empty for any)" if kind != "ratio" else
                                            "These lines (leave empty for any) ...", cr.CATEGORIES,
                                            format_func=cat_label, key="cr_cats")
        rule["text_op"] = c2.selectbox("Description", cr.TEXT_OPS, key="cr_text_op")
        rule["text"] = c3.text_input("text (optional)", key="cr_text", max_chars=cr.MAX_TEXT,
                                     placeholder="e.g. TAX ON PAYMENT")
    if kind == "line":
        c1, c2 = st.columns([1.2, 2])
        op = c1.selectbox("Amount", ["(any amount)", *cr.OPS], key="cr_amount_op")
        rule["amount_op"] = None if op == "(any amount)" else op
        if rule["amount_op"]:
            rule["amount"] = float(c2.number_input("Rs", min_value=0.0, value=1000.0, step=100.0, key="cr_amount"))
    elif kind == "total":
        c1, c2, c3 = st.columns([1.2, 1, 1.6])
        rule["agg"] = c1.radio("Of the matching lines, take the", ["sum", "count"], horizontal=True, key="cr_agg")
        rule["op"] = c2.selectbox("Flag if it is", list(cr.OPS), key="cr_total_op")
        rule["value"] = float(c3.number_input("this value", min_value=0.0, value=2000.0, step=100.0, key="cr_value"))
    elif kind == "field":
        c1, c2 = st.columns([2, 1])
        rule["field"] = c1.selectbox("Flag if", list(cr.FIELDS), format_func=cr.FIELDS.get, key="cr_field")
        rule["op"] = c2.selectbox("is", list(cr.OPS), index=2, key="cr_field_op")
        to = st.radio("compared with", ["another field x a factor", "a fixed number"], horizontal=True, key="cr_compare")
        if to == "a fixed number":
            rule["compare_to"] = "value"
            rule["value"] = float(st.number_input("Number", value=0.0, step=100.0, key="cr_field_value"))
        else:
            rule["compare_to"] = "field"
            c1, c2 = st.columns([1, 2])
            rule["factor"] = float(c1.number_input("factor", min_value=0.0, max_value=1000.0, value=0.05, step=0.01,
                                                   format="%.4f", key="cr_factor"))
            rule["other_field"] = c2.selectbox("x field", list(cr.FIELDS), index=0, format_func=cr.FIELDS.get,
                                               key="cr_other")
    elif kind == "ratio":
        rule["categories_b"] = st.multiselect("... should add up to a share of these lines", cr.CATEGORIES,
                                              format_func=cat_label, default=["payment"], key="cr_cats_b")
        c1, c2 = st.columns(2)
        rule["factor"] = float(c1.number_input("share (e.g. 0.18 = 18%)", min_value=0.0, max_value=100.0, value=0.18,
                                               step=0.01, format="%.4f", key="cr_ratio"))
        rule["tolerance"] = float(c2.number_input("tolerance Rs", min_value=0.0, value=1.0, step=1.0, key="cr_tol"))
        rule["only_if_present"] = st.checkbox("Only when such lines are on the statement (a charge that is missing "
                                              "altogether is not flagged)", value=True, key="cr_present")
    if st.checkbox("Only check when ... (optional condition)", key="cr_use_when"):
        c1, c2, c3, c4 = st.columns([2.2, 1.2, 0.8, 1.4])
        when_cats = c1.multiselect("the lines", cr.CATEGORIES, default=["payment"], format_func=cat_label,
                                   key="cr_when_cats")
        when_agg = c2.radio("take the", ["sum", "count"], horizontal=True, key="cr_when_agg")
        when_op = c3.selectbox("is", list(cr.OPS), key="cr_when_op")
        when_value = float(c4.number_input("value", min_value=0.0, value=10000.0, step=1000.0, key="cr_when_value"))
        rule["when"] = {"categories": when_cats, "agg": when_agg, "op": when_op, "value": when_value}
    return rule


def add_rule(rule):
    try:
        saved = cr.add_saved(rule)
        st.session_state.cr_msg = ("success", f"Saved as {saved['id']} - it is now a choice in the Error Lab.")
    except ValueError as e:
        st.session_state.cr_msg = ("error", str(e))


def load_examples():
    for ex in cr.EXAMPLES:
        rules = rules_in_session()
        if len(rules) < cr.MAX_RULES and not any(r.get("name") == ex["name"] for r in rules):
            cr.add_saved(ex)


def delete_rule(rule_id):
    cr.delete_saved(rule_id)
    st.session_state.cr_msg = ("success", f"{rule_id} deleted - also removed from the Error Lab.")


def page(go_errorlab):
    md('<div class="steps mini"><div><b>1</b>Build from a template</div><div><b>2</b>Save it</div>'
       '<div><b>3</b>Test on samples</div><div><b>4</b>Use in the Error Lab</div></div>')
    st.caption("No code is typed or run - every choice comes from fixed lists. The engine and the rulebook are never "
               "changed; rules are shared by everyone with the judge passkey.")

    with st.container(border=True):
        md(f'<div class="sec">New rule <span class="muted">&middot; up to {cr.MAX_RULES}, shared by all judges</span></div>')
        rule = builder()
        problems = cr.validate(rule)
        md(f'<div class="ai"><div class="label">Your rule reads</div>{esc(cr.describe(rule))}</div>')
        if problems and (rule.get("name") or "").strip():
            st.caption("To fix before saving: " + " ".join(problems))
        c1, c2 = st.columns([1, 3])
        c1.button("Save rule", type="primary", key="cr_add", on_click=add_rule, args=(rule,), disabled=bool(problems))
        c2.button("Load 4 example rules", key="cr_examples", on_click=load_examples)
        msg = st.session_state.pop("cr_msg", None)
        if msg:
            (st.success if msg[0] == "success" else st.error)(msg[1])

    rules = rules_in_session()
    with st.container(border=True):
        md(f'<div class="sec">Your rules <span class="muted">&middot; {len(rules)}</span></div>')
        if not rules:
            st.caption("No rules yet - build one above or load the examples.")
        for r in rules:
            c1, c2 = st.columns([6, 1], vertical_alignment="center")
            c1.markdown(f'{id_pill(r["id"])} <b>{esc(r.get("name", "(no name)"))}</b> {pill(str(r.get("severity", "medium")), "inf")}'
                        f'<br><span class="muted">{esc(cr.describe(r))}</span>', unsafe_allow_html=True)
            c2.button("Delete", key=f"cr_del_{r['id']}", on_click=delete_rule, args=(r["id"],))

    if not rules:
        return
    with st.container(border=True):
        md('<div class="sec">Test your rules</div>')
        entries = samples.manifest()
        files = [f for f in samples.sample_files() if f in entries]
        c1, c2, c3 = st.columns([2, 1.3, 1.3], vertical_alignment="bottom")
        file = c1.selectbox("Sample statement", files, key="cr_test_file",
                            format_func=lambda f: f"{entries[f]['card']} · {entries[f]['month']}  ({f})")
        one = c2.button("Test on this statement", type="primary", key="cr_test_one")
        every = c3.button("Test on all 15 samples", key="cr_test_all")
        if one:
            rec = sample_record(file)
            rows = []
            for r in rules:
                found = cr.evaluate(r, rec)
                label = cr.result_label(found)
                details = "<br>".join(esc(f["title"]) for f in found if f["status"] in ("alert", "error")) \
                    or f'<span class="muted">{esc(found[0]["title"])}</span>'
                rows.append([f'{id_pill(r["id"])} {esc(r.get("name", "(no name)"))}', pill(label, RESULT_PILL.get(label, "bad")), details])
            md(table(["Rule", "Result", "Details"], rows))
        if every:
            rows = []
            for f in files:
                rec = sample_record(f)
                cells = [cr.result_label(cr.evaluate(r, rec)) for r in rules]
                rows.append([esc(f"{entries[f]['card']} · {entries[f]['month']}"),
                             *(pill(c, RESULT_PILL.get(c, "bad")) for c in cells)])
            md(table(["Statement", *(esc(r["id"]) for r in rules)], rows))
            for r in rules:
                hits = sum(cr.result_label(cr.evaluate(r, sample_record(f))).startswith("flagged") for f in files)
                if hits >= len(files) // 2:
                    st.warning(f"{r['id']} flags {hits} of {len(files)} unedited samples. In the Error Lab a flag only "
                               f"counts as CAUGHT if your change causes it, so a rule that already fires on the clean "
                               f"statement can never catch anything there - it is probably too broad (add a description "
                               f"filter or an 'Only check when' condition).")
        st.caption("Samples are the unedited statements. To see whether a rule catches an error you plant, open the "
                   "Error Lab - your rules run there too and count towards CAUGHT.")
        st.button("Open the Error Lab", key="cr_to_lab", on_click=go_errorlab)
