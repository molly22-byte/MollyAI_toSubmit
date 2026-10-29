"""Rulebook page and rule chips. Everything is read from config/rulebook.json, the file the engine runs on.

Rule chips: for judges (unlocked) a rule ID opens a popover with that rule and a link to its Rulebook row;
for everyone else only the plain-English name is shown. The page itself never shows which rules are
planted in which sample statement (that stays in the answer-key view).
"""
import json

import streamlit as st

from cc_audit.checks import SEVERITY
from cc_audit.config import ROOT, rulebook, settings
from ui import judge
from ui.components import esc, md, pill, table

DISCLAIMER = ("<b>Demo rulebook - for hackathon evaluation only.</b><br>These rules are our own interpretation of a "
              "sample card's Most Important Terms &amp; Conditions (MITC), built to test this project on dummy statements. "
              "They are not an official bank policy, may be incomplete or outdated, and are not financial or legal advice. "
              "Please do not copy, redistribute or rely on them for real decisions. Your bank's current MITC is the only "
              "authoritative source.")
DISCLAIMER_SHORT = "Demo rule for evaluation only - not official bank policy."
GROUPS = [("bank_fee", "Bank rules"), ("common_sense", "Common-sense checks"), ("not_errors", "Not errors")]
# 1-2 word hover labels for rule ID pills
SHORT = {
    "C01": "Duplicate charge", "C02": "Missing EMI", "C03": "Repeated EMI", "C04": "EMI changed", "C05": "EMI count",
    "C06": "EMI plan", "C07": "Price increase", "C08": "New subscription", "C09": "Missing refund",
    "C10": "Totals mismatch", "C11": "Unexplained fee", "C12": "New merchant",
    "F01": "Interest rate", "F02": "Interest-free", "F03": "Minimum interest", "F04": "Cash interest",
    "F05": "Cash fee", "F06": "Late fee", "F07": "Extra late fee", "F08": "Over-limit fee", "F09": "Bounce fee",
    "F10": "Forex markup", "F11": "DCC fee", "F12": "Annual fee", "F13": "GST", "F14": "Minimum due",
    "F15": "Due date", "F16": "Utility fee", "F17": "Education fee", "F18": "Wallet fee", "F19": "Rent fee",
    "F20": "Fuel surcharge", "F21": "Cash payment", "F22": "Cheque fee", "F23": "Redemption fee",
    "F24": "Replacement fee", "F25": "Unpaid EMI",
    "N01": "Grace period", "N02": "Forex kept", "N03": "Over-limit kept", "N04": "Rent fee kept",
    "N05": "No late fee", "N06": "Carried interest", "N07": "Immediate interest",
}


def raw_rulebook():
    return json.loads((ROOT / settings()["rulebook"]).read_text(encoding="utf-8"))


def judge_rule(rule_id):
    if not str(rule_id).startswith("J"):
        return None
    from ui.custom_rules import load_rules
    return next((r for r in load_rules() if r["id"] == rule_id), None)


def rule_name(rule_id):
    jr = judge_rule(rule_id)
    if jr:
        return jr.get("name") or rule_id
    rb = rulebook()
    if rule_id in rb.rules:
        return rb.rules[rule_id].get("rule") or rule_id
    if rule_id in rb.not_errors:
        return rb.not_errors[rule_id].get("looks_wrong") or rule_id
    return rule_id


def short_label(rule_id):
    """The 1-2 word hover text of a rule ID (custom J rules show their own name)."""
    return SHORT.get(rule_id) or rule_name(rule_id)


def id_pill(rule_id):
    """A rule ID pill that shows its short description on hover."""
    return f'<span class="pill code rid" title="{esc(short_label(rule_id))}">{esc(rule_id)}</span>'


def dash(value):
    return "-" if value in (None, "") else value


def rule_rows(rule_id):
    """(label, value) pairs for one rule, straight from rulebook.json; '-' where a field is missing."""
    rb = rulebook()
    jr = judge_rule(rule_id)
    if jr:
        from ui.custom_rules import describe
        return [("Rule", describe(jr)), ("Source", "written by a judge (custom rule, not in rulebook.json)"),
                ("Severity when flagged", jr.get("severity", "medium"))]
    if rule_id in rb.not_errors:
        n = rb.not_errors[rule_id]
        return [("Looks wrong", dash(n.get("looks_wrong"))), ("Why it is allowed", dash(n.get("why_allowed"))),
                ("Source", f"MITC section {n['mitc_section']}" if n.get("mitc_section") else "-")]
    r = rb.rules.get(rule_id, {})
    source = (f"MITC section {r['mitc_section']}" if r.get("mitc_section")
              else "common sense" if r.get("group") == "common_sense" else "-")
    return [("When it applies", dash(r.get("when_it_applies"))), ("What it checks (flag if)", dash(r.get("flag_if"))),
            ("Formula / limit", f"{dash(r.get('expected'))} (tolerance Rs {rb.tolerance:g})"),
            ("Worked example", dash(r.get("worked_example"))), ("Source", source),
            ("Severity when flagged", SEVERITY.get(rule_id, "high"))]


def label_html(rule_id):
    """For HTML tables: the rule ID for judges, the plain-English name for everyone else."""
    return id_pill(rule_id) if judge.unlocked() else esc(rule_name(rule_id))


def rule_chip(rule_id, key, on_open=None):
    """A clickable rule ID (popover with the rule) for judges; the plain-English name otherwise."""
    if not judge.unlocked():
        md(pill(rule_name(rule_id), "code"))
        return
    with st.popover(rule_id, help=short_label(rule_id)):
        md(f'<div style="font-weight:600;color:var(--primary)">{esc(rule_id)} &middot; {esc(rule_name(rule_id))}</div>')
        md(table(None, [[f'<span class="muted">{esc(k)}</span>', esc(v)] for k, v in rule_rows(rule_id)]))
        st.caption(DISCLAIMER_SHORT)
        if on_open and not judge_rule(rule_id):
            st.button("Open in Rulebook", key=f"open_{key}_{rule_id}", on_click=on_open, args=(rule_id,))


def rule_chips(rule_ids, key, on_open=None, label="Rule details:"):
    """A row of rule chips (judges only; nothing is shown to others, who already see the names)."""
    ids = list(dict.fromkeys(rule_ids))
    if not ids or not judge.unlocked():
        return
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        st.caption(label)
        for rid in ids:
            rule_chip(rid, key, on_open)


def rulebook_page(version, selected=None):
    raw = raw_rulebook()
    rb = rulebook()
    md(f'<div class="note">{esc(DISCLAIMER_SHORT)} <span class="muted">&middot; source <code class="tok">'
       f'config/rulebook.json</code> &middot; version <code class="tok">{esc(version)}</code></span></div>')
    with st.expander("Disclaimer"):
        md(f'<p style="font-size:14px;line-height:1.5;margin:0">{DISCLAIMER}</p>')
    query = st.text_input("Search by rule ID or keyword", key="rb_search", placeholder="e.g. F06, late fee, GST")
    q = (query or "").strip().lower()
    groups = {"bank_fee": sorted(k for k, r in rb.rules.items() if r.get("group") == "bank_fee"),
              "common_sense": sorted(k for k, r in rb.rules.items() if r.get("group") == "common_sense"),
              "not_errors": sorted(rb.not_errors)}
    other = sorted(k for k, r in rb.rules.items() if r.get("group") not in ("bank_fee", "common_sense"))
    if other:
        groups["bank_fee"] += other                 # never drop a rule the engine has
    tabs = st.tabs([f"{title} ({ids[0]}-{ids[-1]})" if ids else title
                    for (_, title), ids in zip(GROUPS, groups.values())])
    for tab, (group, _) in zip(tabs, GROUPS):
        with tab:
            ids = groups[group]
            shown = [rid for rid in ids if not q or q in rid.lower()
                     or any(q in str(v).lower() for _, v in rule_rows(rid)) or q in rule_name(rid).lower()]
            st.caption(f"{len(shown)} of {len(ids)} shown")
            if group == "not_errors":
                head = ["Case", "Looks wrong", "Why it is allowed", "Source"]
                rows = [[id_pill(rid), *(esc(v) for _, v in rule_rows(rid))] for rid in shown]
            else:
                head = ["Rule ID", "Plain-English name", "What it checks", "Formula / limit", "Source", "Severity"]
                rows = []
                for rid in shown:
                    f = dict(rule_rows(rid))
                    rows.append([id_pill(rid), esc(rule_name(rid)), esc(f["What it checks (flag if)"]),
                                 esc(f["Formula / limit"]), esc(f["Source"]), esc(f["Severity when flagged"])])
            if rows:
                md(table(head, rows))
            for rid in shown:
                with st.expander(f"{rid} - {rule_name(rid)}", expanded=rid == selected or len(shown) == 1):
                    md(table(None, [[f'<span class="muted">{esc(k)}</span>', esc(v)] for k, v in rule_rows(rid)]))
                    st.caption(DISCLAIMER_SHORT)
    with st.expander("All parameters, late fee slabs and EMI plans (from rulebook.json)"):
        md(table(["Parameter", "Value"], [[esc(k), esc(v)] for k, v in sorted(raw.get("parameters", {}).items())]))
        slabs = raw.get("late_fee_slabs") or []
        if slabs:
            md(table(["Amount due above", "Up to", "Late fee"],
                     [[esc(s.get("above")), esc(dash(s.get("up_to"))), esc(s.get("fee"))] for s in slabs]))
        plans = raw.get("emi_plans") or {}
        if plans:
            keys = sorted({k for p in plans.values() for k in p})
            md(table(["Plan", *keys], [[esc(ref), *(esc(dash(p.get(k))) for k in keys)] for ref, p in plans.items()]))
        for a in raw.get("assumptions") or []:
            st.caption(f"Assumption: {a}")
