#!/usr/bin/env python3
"""
evaluate_report.py - automatic evaluator for the CC audit POC.

Compares what the pipeline produced (ledger + reports + audit log) with the
answer key (the ground truth Jayesh built with the 15 synthetic statements).
Only this tool reads the answer key; nothing in the pipeline should.

What it measures
  1. Numerical correctness
       N1  header numbers in the ledger/report = printed values on the statement
       N2  every finding is internally consistent: difference = actual - expected
       N3  for planted errors, expected/actual match the answer key values
       N4  every money amount in LLM text exists in the deterministic data it was given
       N5  every money amount in the restored .md report exists in the masked data
  2. Groundedness
       G1  every evidence line in a finding is a real statement line (same date, amount, description)
       G2  when LLM text names a merchant next to an amount, that amount belongs to that merchant
           (a real line of that merchant, or a value of a finding whose evidence is that merchant)
  3. Completeness
       C1  every statement line is in the ledger (recall) and nothing extra was added (precision)
       C2  description of each matched line is unchanged
       C3  every alert has evidence (except rules that are about something missing, e.g. C09)
  4. Extra metrics
       D   alert detection vs planted rules: precision, recall, false alarms on clean statements,
           planted "not error" cases that stayed silent
       P   PII leaks: real PII values found in masked reports, ledger, LLM text or audit log
       T   token integrity: tokens in LLM text (CARD_1, NAME_1 ...) exist in the vault / payload
       F   LLM fallback rate (replies replaced by the deterministic template), if the report records it

Usage
  python evaluate_report.py --key answer_key.xlsx --data data/ [--out eval_out/]

  data/ is the pipeline data folder from architecture.txt:
      data/ledger/CARD_n/YYYY-MM.json
      data/reports/CARD_n/YYYY-MM.json   (+ YYYY-MM.md)
      data/vault/token_map.json          (optional, used for token checks)
      data/audit_log.jsonl               (optional, used for PII leak checks)

The report/ledger readers are tolerant: they search the JSON for the shapes
described in architecture.txt instead of fixed paths. If a key name differs in
the real code, change the lists in the CONFIG section below.

Outputs: console summary, <out>/eval_results.json, <out>/eval_report.md
"""
import argparse, glob, json, os, re, sys
from collections import Counter, defaultdict
from datetime import date, datetime

# ----------------------------------------------------------------------------- CONFIG
TOL = 1.00                          # rupee tolerance for amount comparisons (rulebook: Rs 1)
DESC_KEYS = ("description", "desc", "raw_description", "description_raw", "printed_description")
DC_KEYS = ("type", "dc", "direction", "dcm")
FINDING_KEYS = ("rule_id", "status")                       # a dict with both is a finding
HEADER_KEYS = ("statement_date", "total_amount_due")       # a dict with both is the header
LLM_TEXT_KEYS = {                                          # string fields written by the LLM
    "explanation", "summary", "answer", "body", "subject", "note", "notes",
    "ai_explanation", "ai_summary", "llm_text", "text", "rule_check", "message",
}
FALLBACK_HINTS = ("template", "fallback")                  # values/keys that mark a template reply
ABSENCE_RULES = {"C09", "C02", "C10", "F15", "F14", "C12"} # alerts that may have no evidence line
TOKEN_RE = re.compile(r"\b[A-Z]{2,12}_\d+\b")             # CARD_1, NAME_1, EMAIL_1 ...
# ----------------------------------------------------------------------------- helpers

MONEY_RE = re.compile(
    r"(?:(?:rs\.?|inr|₹)\s*)?(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{1,2}|\d{3,})(?![\d%]|\.\d)",
    re.I)


def money_in_text(text):
    """Money-like numbers in free text: '1,100.00', 'Rs 600', '40533.29'.
    Skips small integers (days, counts), percentages and 4-digit years."""
    out = []
    for m in MONEY_RE.finditer(text or ""):
        raw = m.group(1)
        v = float(raw.replace(",", ""))
        if re.fullmatch(r"(19|20)\d\d", raw):            # a year, not money
            continue
        out.append(round(v, 2))
    return out


def num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).replace(",", "").replace("₹", "").replace("Rs", "").strip()
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def iso(v):
    """Any date-ish value -> 'YYYY-MM-DD' (or None)."""
    if v is None or v == "":
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d %b %y", "%d %b %Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "%H" in fmt else s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def norm_desc(s):
    return " ".join(str(s or "").upper().split())


def close(a, b, tol=TOL):
    return a is not None and b is not None and abs(a - b) <= tol


def walk(o, path=""):
    """Yield (path, key, value) for every node of a JSON structure."""
    if isinstance(o, dict):
        for k, v in o.items():
            p = f"{path}.{k}" if path else k
            yield p, k, v
            yield from walk(v, p)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            p = f"{path}[{i}]"
            yield p, "", v
            yield from walk(v, p)


def first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

# ----------------------------------------------------------------------------- answer key


def load_key(path):
    try:
        import openpyxl
    except ImportError:
        sys.exit("pip install openpyxl")
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)

    def rows(name):
        it = wb[name].iter_rows(values_only=True)
        head = [str(h).strip() if h else "" for h in next(it)]
        return [dict(zip(head, r)) for r in it if any(c is not None for c in r)]

    key = {"statements": {}, "planted": defaultdict(list), "not_errors": defaultdict(list),
           "txns": defaultdict(list), "pii": defaultdict(list)}
    for r in rows("Statements"):
        rules = [x.strip() for x in str(r.get("Planted rule IDs") or "").split(",") if x.strip()]
        key["statements"][r["File"]] = {
            "file": r["File"], "person": r.get("Person key"), "last4": str(r.get("Card last 4") or ""),
            "statement_date": iso(r.get("Statement date")),
            "due_correct": iso(r.get("Correct due date")), "due_printed": iso(r.get("Printed due date")),
            "previous_balance": num(r.get("Previous balance")), "payments": num(r.get("Payments")),
            "credits": num(r.get("Credits/refunds/EMI transfers")),
            "purchases": num(r.get("Purchases + EMI instalments")),
            "fees": num(r.get("Fees+interest+GST")),
            "total_correct": num(r.get("Correct total due")), "total_printed": num(r.get("Printed total due")),
            "min_printed": num(r.get("Printed min due")), "planted": rules,
        }
    for r in rows("Planted errors"):
        key["planted"][r["File"]].append(r)
    for r in rows("Not errors"):
        key["not_errors"][r["File"]].append(r)
    for r in rows("Transactions"):
        key["txns"][r["File"]].append({
            "date": iso(r.get("Date")), "desc": norm_desc(r.get("Printed description")),
            "merchant": norm_desc(r.get("Merchant (as printed)")), "amount": num(r.get("Amount (Rs)")),
            "dc": r.get("D/C/M"), "type": r.get("Type"), "usd": num(r.get("USD amount"))})
    for r in rows("PII map"):
        key["pii"][r["Person"]].append((r["PII type"], str(r["Value printed on every statement"])))
    return key

# ----------------------------------------------------------------------------- pipeline data


def find_header(doc):
    for _, _, v in walk(doc):
        if isinstance(v, dict) and all(k in v for k in HEADER_KEYS):
            return v
    return None


def find_list_of(doc, pred):
    """Longest list in doc whose items satisfy pred."""
    best = []
    for _, _, v in walk({"root": doc}):
        if isinstance(v, list) and v and all(isinstance(x, dict) and pred(x) for x in v):
            if len(v) > len(best):
                best = v
    return best


def is_txn(x):
    return "amount" in x and any(k in x for k in DESC_KEYS)


def findings_of(doc):
    """Findings of a report. Real format (tools/evaluate.py): report["alerts"] and report["notes"];
    items in "alerts" are alerts even without a status field, items in "notes" never are."""
    if isinstance(doc, dict) and isinstance(doc.get("alerts"), list):
        out = []
        for a in doc["alerts"]:
            if isinstance(a, dict):
                out.append({**a, "status": "alert"})
        for n in doc.get("notes") or []:
            if isinstance(n, dict):
                out.append({**n, "status": n.get("status") if n.get("status") not in (None, "alert") else "info"})
        return out
    out = []
    for _, _, v in walk(doc):
        if isinstance(v, dict) and all(k in v for k in FINDING_KEYS):
            out.append(v)
    return out


def llm_texts(doc):
    """(path, text) for every string the LLM wrote."""
    out = []
    for p, k, v in walk(doc):
        if isinstance(v, str) and k.lower() in LLM_TEXT_KEYS and len(v) > 15:
            out.append((p, v))
    return out


def deterministic_numbers(doc, txns):
    """All numbers in the data the LLM was allowed to see (findings, header, transactions)."""
    nums = set()
    for p, k, v in walk(doc):
        if k.lower() in LLM_TEXT_KEYS:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.add(round(float(v), 2))
        elif isinstance(v, str):
            for n in money_in_text(v):
                nums.add(n)
    for t in txns:
        a = num(t.get("amount"))
        if a is not None:
            nums.add(a)
        for n in money_in_text(first(t, DESC_KEYS) or ""):
            nums.add(n)
    return nums


def load_pipeline(data_dir):
    months = []
    for rp in sorted(glob.glob(os.path.join(data_dir, "reports", "*", "*.json"))):
        card = os.path.basename(os.path.dirname(rp))
        month = os.path.splitext(os.path.basename(rp))[0]
        lp = os.path.join(data_dir, "ledger", card, month + ".json")
        mp = os.path.splitext(rp)[0] + ".md"
        report = load_json(rp)
        ledger = load_json(lp) if os.path.exists(lp) else {}
        months.append({
            "card": card, "month": month, "report_path": rp, "report": report, "ledger": ledger,
            "md": open(mp, encoding="utf-8").read() if os.path.exists(mp) else None,
            "header": find_header(ledger) or find_header(report),
            "txns": find_list_of(ledger, is_txn) or find_list_of(report, is_txn),
            "findings": findings_of(report),
        })
    vault = {}
    vp = os.path.join(data_dir, "vault", "token_map.json")
    if os.path.exists(vp):
        v = load_json(vp)
        vault = v if isinstance(v, dict) else {}
        if "tokens" in vault and isinstance(vault["tokens"], dict):
            vault = vault["tokens"]
    audit = []
    ap = os.path.join(data_dir, "audit_log.jsonl")
    if os.path.exists(ap):
        with open(ap, encoding="utf-8") as f:
            audit = [l for l in f if l.strip()]
    return months, vault, audit


def match_statement(m, key):
    """Link a pipeline month to an answer-key file by statement date + printed total due."""
    h = m["header"] or {}
    sd, tot = iso(h.get("statement_date")), num(h.get("total_amount_due"))
    for f, s in key["statements"].items():
        if s["statement_date"] == sd and close(s["total_printed"], tot, 0.01):
            return f
    for f, s in key["statements"].items():           # fallback: date + card last 4 in folder/vault
        if s["statement_date"] == sd and s["last4"] and s["last4"] in json.dumps(h):
            return f
    return None

# ----------------------------------------------------------------------------- checks


def check_numerical(m, ks, planted_rows, pii_values=()):
    issues, n_checked = [], 0
    h = m["header"] or {}
    summ = find_by_keys(m["ledger"], ("previous_balance", "payments")) or \
        find_by_keys(m["report"], ("previous_balance", "payments")) or {}
    pairs = [("total_amount_due", h.get("total_amount_due"), ks["total_printed"]),
             ("minimum_amount_due", h.get("minimum_amount_due"), ks["min_printed"]),
             ("payment_due_date", iso(h.get("payment_due_date")), ks["due_printed"]),
             ("previous_balance", summ.get("previous_balance"), ks["previous_balance"]),
             ("payments", summ.get("payments"), ks["payments"]),
             ("credits", first(summ, ("credits_refunds_emi_transfers", "credits")), ks["credits"]),
             ("purchases", first(summ, ("purchases_emi_instalments", "purchases")), ks["purchases"]),
             ("fees", first(summ, ("fees_interest_gst", "fees")), ks["fees"])]
    for name, got, want in pairs:                                            # N1
        if got is None or want is None:
            continue
        n_checked += 1
        ok = (got == want) if isinstance(want, str) else close(num(got), want, 0.01)
        if not ok:
            issues.append({"check": "N1", "field": name, "report": got, "statement": want})
    for f in m["findings"]:                                                  # N2
        e, a, d = num(f.get("expected")), num(f.get("actual")), num(f.get("difference"))
        if e is not None and a is not None and d is not None:
            n_checked += 1
            if not close(d, round(a - e, 2), 0.02):
                issues.append({"check": "N2", "rule": f.get("rule_id"), "expected": e, "actual": a,
                               "difference": d, "should_be": round(a - e, 2)})
    alerts = {f.get("rule_id"): f for f in m["findings"] if str(f.get("status")).lower() == "alert"}
    for row in planted_rows:                                                 # N3
        f = alerts.get(row["Rule ID"])
        if not f:
            continue
        for side, col in (("expected", "Expected value"), ("actual", "Printed value")):
            key_nums = money_in_text(str(row.get(col) or ""))
            key_date = iso(re.search(r"\d{1,2} \w{3} \d{2}", str(row.get(col) or "")).group(0)) \
                if re.search(r"\d{1,2} \w{3} \d{2}", str(row.get(col) or "")) else None
            got = f.get(side)
            txt = str(row.get(col) or "")
            if re.search(r"<|typical|approx", txt, re.I):     # descriptive, not an exact value
                key_nums = []
            if key_nums and num(got) is not None:
                n_checked += 1
                cands = key_nums + [round(sum(key_nums), 2)]    # e.g. principal + interest = EMI
                if not any(close(num(got), k, TOL) for k in cands):
                    issues.append({"check": "N3", "rule": row["Rule ID"], "side": side,
                                   "report": got, "answer_key": row.get(col)})
            elif key_date and isinstance(got, str) and iso(got) and re.match(r"\d{4}-", iso(got)):
                n_checked += 1
                if iso(got) != key_date:
                    issues.append({"check": "N3", "rule": row["Rule ID"], "side": side,
                                   "report": got, "answer_key": row.get(col)})
    allowed = deterministic_numbers({k: v for k, v in m["report"].items()}, m["txns"])
    for p, text in llm_texts(m["report"]):                                   # N4
        for n in money_in_text(text):
            n_checked += 1
            if not any(close(n, a, 0.01) for a in allowed):
                issues.append({"check": "N4", "where": p, "amount": n, "text": text[:160]})
    if m["md"]:                                                              # N5
        md_allowed = allowed | {round(x, 2) for x in allowed}
        md = m["md"]
        for _, val in pii_values:                         # the user view legitimately shows these
            md = md.replace(val, " ")
        md = re.sub(r"\b[\dX*]{4}[ -][\dX*]{2,4}[ -]?[\dX*]{0,4}[ -][X*]{4}[ -]\d{4}\b", " ", md)  # card numbers
        md = re.sub(r"\b[X*]{4}[ -][X*]{4}[ -][X*]{4}[ -]\d{4}\b", " ", md)
        for n in set(money_in_text(md)):
            n_checked += 1
            if not any(close(n, a, 0.01) for a in md_allowed):
                issues.append({"check": "N5", "amount": n})
    return issues, n_checked


def find_by_keys(doc, keys):
    for _, _, v in walk(doc):
        if isinstance(v, dict) and all(k in v for k in keys):
            return v
    return None


def check_grounded(m, key_txns, other_months=None):
    """other_months: {'YYYY-MM': [key lines]} for the same card, so evidence taken from
    earlier statements (subscriptions, annual fee, EMI history) is checked against its own month."""
    issues, n_checked = [], 0
    other_months = other_months or {}
    by_amount = defaultdict(list)
    for t in key_txns:
        by_amount[t["amount"]].append(t)
    for f in m["findings"]:                                                  # G1
        for ev in f.get("evidence") or []:
            if not isinstance(ev, dict) or num(ev.get("amount")) is None:
                continue
            n_checked += 1
            a, d, desc = num(ev.get("amount")), iso(ev.get("date")), norm_desc(first(ev, DESC_KEYS))
            ev_month = str(ev.get("statement_month") or "")[:7]
            if ev_month and ev_month in other_months:
                cands = [t for t in other_months[ev_month] if t["amount"] == a]
            else:
                cands = list(by_amount.get(a, []))
                if not cands and d:                       # no month given: try the month of the date
                    for lines in other_months.values():
                        cands += [t for t in lines if t["amount"] == a and t["date"] == d]
            ok = any((not d or not t["date"] or t["date"] == d) and
                     (not desc or desc == t["desc"] or desc in t["desc"] or t["desc"] in desc)
                     for t in cands)
            if not ok:
                issues.append({"check": "G1", "rule": f.get("rule_id"), "evidence": ev,
                               "why": "no statement line with this date+amount+description"
                               if cands else "amount not on the statement"})
    merchants = sorted({t["merchant"] for t in key_txns if t["merchant"] and len(t["merchant"]) > 3},
                       key=len, reverse=True)
    merchant_amounts = defaultdict(set)
    for t in key_txns:
        if t["merchant"]:
            merchant_amounts[t["merchant"]].add(t["amount"])
            if t.get("usd"):                              # foreign amount, e.g. USD 10.00
                merchant_amounts[t["merchant"]].add(t["usd"])
    finding_vals = defaultdict(set)                       # merchant -> values of findings about it
    for f in m["findings"]:
        ev_desc = " ".join(norm_desc(first(e, DESC_KEYS)) for e in f.get("evidence") or [] if isinstance(e, dict))
        vals = {num(f.get(k)) for k in ("expected", "actual", "difference")} - {None}
        vals |= {num(e.get("amount")) for e in f.get("evidence") or [] if isinstance(e, dict)} - {None}
        for mer in merchants:
            if mer in ev_desc or mer in norm_desc(f.get("title")):
                finding_vals[mer] |= vals
    for p, text in llm_texts(m["report"]):                                   # G2
        for sent in re.split(r"(?<=[.!?])\s+", text):
            up = norm_desc(sent)
            named = [mer for mer in merchants if mer in up]
            if len(named) != 1:                           # ambiguous or none: skip the sentence
                continue
            mer = named[0]
            for n in money_in_text(sent):
                n_checked += 1
                if not any(close(n, x, 0.01) for x in merchant_amounts[mer] | finding_vals[mer]):
                    issues.append({"check": "G2", "where": p, "merchant": mer, "amount": n,
                                   "sentence": sent[:160]})
    return issues, n_checked


def check_complete(m, key_txns):
    rep = [{"date": iso(t.get("date")), "amount": num(t.get("amount")),
            "desc": norm_desc(first(t, DESC_KEYS))} for t in m["txns"]]
    want = Counter((t["date"], t["amount"]) for t in key_txns)
    got = Counter((t["date"], t["amount"]) for t in rep)
    missing = list((want - got).elements())
    extra = list((got - want).elements())
    matched = sum((want & got).values())
    desc_bad = []
    key_desc = defaultdict(list)
    for t in key_txns:
        key_desc[(t["date"], t["amount"])].append(t["desc"])
    for t in rep:
        opts = key_desc.get((t["date"], t["amount"]))
        if opts and t["desc"] and t["desc"] not in opts:
            desc_bad.append({"date": t["date"], "amount": t["amount"], "report": t["desc"], "statement": opts})
    no_ev = [f.get("rule_id") for f in m["findings"]
             if str(f.get("status")).lower() == "alert" and not f.get("evidence")
             and f.get("rule_id") not in ABSENCE_RULES]
    return {
        "statement_lines": len(key_txns), "report_lines": len(rep), "matched": matched,
        "recall": round(matched / len(key_txns), 4) if key_txns else None,
        "precision": round(matched / len(rep), 4) if rep else None,
        "missing": [{"date": d, "amount": a} for d, a in missing],
        "extra": [{"date": d, "amount": a} for d, a in extra],
        "description_changed": desc_bad, "alerts_without_evidence": no_ev,
    }


def check_detection(m, ks, not_errors):
    alerts = [f for f in m["findings"] if str(f.get("status")).lower() == "alert"]
    pred = {f.get("rule_id") for f in alerts}
    covered = set(pred)
    for f in alerts:                                  # an F06 alert that names C11 also covers C11
        covered |= set(f.get("related_rules") or [])
    planted = set(ks["planted"])
    tp = planted & covered
    fn = planted - covered
    fp = pred - planted - {r for f in alerts if f.get("rule_id") in planted for r in (f.get("related_rules") or [])}
    ne = []
    for row in not_errors:
        cid = row["Case ID"]
        noted = any(f.get("allowed_by") == cid for f in m["findings"])
        ne.append({"case": cid, "explained_as_allowed": noted})
    return {"planted": sorted(planted), "alerted": sorted(pred), "caught": sorted(tp),
            "missed": sorted(fn), "false_alarms": sorted(fp), "not_error_cases": ne}


def check_pii(texts_by_source, pii_values):
    leaks = []
    for src, text in texts_by_source:
        up = text.upper()
        for kind, val in pii_values:
            if kind.lower().startswith("card last"):
                continue                               # last 4 digits are reported separately
            v = val.upper().strip()
            if len(v) >= 4 and v in up:
                leaks.append({"source": src, "pii_type": kind})
    return leaks


def check_tokens(m, vault):
    issues = []
    known = set(vault.keys())
    payload = json.dumps({k: v for k, v in m["report"].items()})
    for p, text in llm_texts(m["report"]):
        for tok in set(TOKEN_RE.findall(text)):
            if known and tok not in known:
                issues.append({"where": p, "token": tok, "why": "not in vault"})
            elif not known and tok not in payload.replace(text, ""):
                issues.append({"where": p, "token": tok, "why": "not in payload"})
    return issues


def fallback_rate(m):
    total = fb = 0
    for _, k, v in walk(m["report"]):
        if k.lower() in ("source", "generated_by", "origin", "text_source"):
            total += 1
            if any(h in str(v).lower() for h in FALLBACK_HINTS):
                fb += 1
        elif k.lower() in ("fallback", "used_template", "is_template"):
            total += 1
            fb += bool(v)
    return (fb, total)

# ----------------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--key", required=True, help="answer key .xlsx")
    ap.add_argument("--data", required=True, help="pipeline data folder")
    ap.add_argument("--out", default="eval_out", help="output folder")
    a = ap.parse_args()

    key = load_key(a.key)
    months, vault, audit = load_pipeline(a.data)
    if not months:
        sys.exit(f"No reports found under {a.data}/reports/*/*.json")
    os.makedirs(a.out, exist_ok=True)

    results, unmatched = [], []
    T = defaultdict(int)
    for m in months:
        f = match_statement(m, key)
        if not f:
            unmatched.append(f"{m['card']}/{m['month']}")
            continue
        ks = key["statements"][f]
        num_issues, n_num = check_numerical(m, ks, key["planted"][f], key["pii"].get(ks["person"], []))
        same_card = {s2["statement_date"][:7]: key["txns"][f2] for f2, s2 in key["statements"].items()
                     if s2["last4"] == ks["last4"] and s2["statement_date"]}
        g_issues, n_g = check_grounded(m, key["txns"][f], same_card)
        comp = check_complete(m, key["txns"][f])
        det = check_detection(m, ks, key["not_errors"][f])
        pii_vals = key["pii"].get(ks["person"], [])
        srcs = [("report.json", json.dumps(m["report"], ensure_ascii=False)),
                ("ledger.json", json.dumps(m["ledger"], ensure_ascii=False))]
        srcs += [(f"llm:{p}", t) for p, t in llm_texts(m["report"])]
        leaks = check_pii(srcs, pii_vals)
        last4_seen = bool(ks["last4"]) and ks["last4"] in json.dumps(m["report"]) + json.dumps(m["ledger"])
        tok = check_tokens(m, vault)
        fb, fb_total = fallback_rate(m)

        T["statements"] += 1
        T["num_checked"] += n_num; T["num_failed"] += len(num_issues)
        T["g_checked"] += n_g; T["g_failed"] += len(g_issues)
        T["lines_total"] += comp["statement_lines"]; T["lines_matched"] += comp["matched"]
        T["lines_extra"] += len(comp["extra"]); T["desc_changed"] += len(comp["description_changed"])
        T["planted"] += len(det["planted"]); T["caught"] += len(det["caught"])
        T["alerted_rules"] += len(det["alerted"]); T["false_alarms"] += len(det["false_alarms"])
        T["clean_statements"] += not det["planted"]
        T["clean_with_alerts"] += (not det["planted"]) and bool(det["alerted"])
        T["pii_leaks"] += len(leaks); T["last4_visible"] += last4_seen
        T["token_issues"] += len(tok); T["fallback"] += fb; T["llm_outputs"] += fb_total
        results.append({"file": f, "card": m["card"], "month": m["month"],
                        "numerical": num_issues, "grounded": g_issues, "completeness": comp,
                        "detection": det, "pii_leaks": leaks, "card_last4_visible": last4_seen,
                        "token_issues": tok, "fallback": [fb, fb_total]})

    audit_leaks = []
    if audit:
        allpii = [v for vals in key["pii"].values() for v in vals]
        audit_leaks = check_pii([(f"audit_log line {i+1}", l) for i, l in enumerate(audit)], allpii)

    def pct(x, y):
        return round(100.0 * x / y, 1) if y else None

    tp, fp_, fn_ = T["caught"], T["false_alarms"], T["planted"] - T["caught"]
    summary = {
        "statements_evaluated": T["statements"], "unmatched_reports": unmatched,
        "numerical_correctness_pct": pct(T["num_checked"] - T["num_failed"], T["num_checked"]),
        "groundedness_pct": pct(T["g_checked"] - T["g_failed"], T["g_checked"]),
        "completeness_recall_pct": pct(T["lines_matched"], T["lines_total"]),
        "extra_lines": T["lines_extra"], "descriptions_changed": T["desc_changed"],
        "detection_precision_pct": pct(tp, tp + fp_), "detection_recall_pct": pct(tp, tp + fn_),
        "planted_errors": T["planted"], "caught": tp, "missed": fn_, "false_alarm_rules": fp_,
        "clean_statements_with_alerts": f'{T["clean_with_alerts"]}/{T["clean_statements"]}',
        "pii_leaks_reports_ledger_llm": T["pii_leaks"], "pii_leaks_audit_log": len(audit_leaks),
        "card_last4_visible_in_masked_data": f'{T["last4_visible"]}/{T["statements"]}',
        "token_issues": T["token_issues"],
        "llm_fallback_rate_pct": pct(T["fallback"], T["llm_outputs"]),
    }
    json.dump({"summary": summary, "per_statement": results, "audit_log_leaks": audit_leaks},
              open(os.path.join(a.out, "eval_results.json"), "w"), indent=2, default=str)
    write_md(os.path.join(a.out, "eval_report.md"), summary, results)

    print("\nEVALUATION SUMMARY")
    for k, v in summary.items():
        print(f"  {k:38s} {v}")
    print(f"\nDetails: {a.out}/eval_results.json and {a.out}/eval_report.md")


def write_md(path, s, results):
    L = ["# Evaluation report", "", "| Metric | Value |", "|---|---|"]
    L += [f"| {k.replace('_', ' ')} | {v} |" for k, v in s.items() if k != "unmatched_reports"]
    L += ["", "## Per statement", "",
          "| File | Planted | Caught | Missed | False alarms | Num issues | Grounded issues | Lines matched | PII leaks |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        d, c = r["detection"], r["completeness"]
        L.append(f"| {r['file']} | {', '.join(d['planted']) or 'clean'} | {', '.join(d['caught']) or '-'} | "
                 f"{', '.join(d['missed']) or '-'} | {', '.join(d['false_alarms']) or '-'} | "
                 f"{len(r['numerical'])} | {len(r['grounded'])} | {c['matched']}/{c['statement_lines']} | "
                 f"{len(r['pii_leaks'])} |")
    L += ["", "## Issues found", ""]
    for r in results:
        items = r["numerical"] + r["grounded"]
        c = r["completeness"]
        if c["missing"]:
            items.append({"check": "C1", "missing_lines": c["missing"]})
        if c["extra"]:
            items.append({"check": "C1", "extra_lines": c["extra"]})
        if c["description_changed"]:
            items.append({"check": "C2", "description_changed": c["description_changed"]})
        if c["alerts_without_evidence"]:
            items.append({"check": "C3", "alerts_without_evidence": c["alerts_without_evidence"]})
        items += [{"check": "P", **x} for x in r["pii_leaks"]] + [{"check": "T", **x} for x in r["token_issues"]]
        if items:
            L.append(f"### {r['file']}")
            L += [f"- `{json.dumps(i, default=str)[:300]}`" for i in items]
            L.append("")
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()