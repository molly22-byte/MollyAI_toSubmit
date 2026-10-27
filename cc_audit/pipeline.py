"""Orchestration: parse -> mask -> normalize -> store -> reconcile -> explain -> restore."""
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

from . import audit_log, explainer
from .checks import run_checks
from .config import data_dir, rulebook, settings
from .ledger import Ledger, d, previous_cycle
from .llm import get_llm
from .masking import TokenVault, mask_statement
from .normalizer import normalize
from .parser import extract
from .report import build_report, llm_facts, render_markdown, statement_facts


def ingest(pdf_path, vault=None, ledger=None, password=None):
    """Parse one PDF, mask it, add it to the ledger. The PDF itself is not copied anywhere.
    password (for encrypted statements) is only passed to the parser, never stored or logged."""
    pdf_path = Path(pdf_path)
    raw = extract(pdf_path, password=password)
    return ingest_parsed(raw, hashlib.sha256(pdf_path.read_bytes()).hexdigest(), vault, ledger)


def ingest_parsed(raw, source_sha256, vault=None, ledger=None):
    """Mask, normalize and store an already-parsed statement (the rest of ingest(), from the parser's output on)."""
    vault, ledger = vault or TokenVault(), ledger or Ledger()
    masked, fields = mask_statement(raw, vault)
    vault.save()
    record = normalize(masked, source_sha256)
    path, replaced = ledger.save(record)
    audit_log.log("ingest", card_id=record["card_id"], month=record["month"], fields_masked=fields,
                  transactions=len(record["transactions"]), stored_at=str(path.relative_to(data_dir())), replaced=replaced)
    return record, replaced


def ingest_folder(folder):
    vault, ledger = TokenVault(), Ledger()
    return [ingest(p, vault, ledger) for p in sorted(Path(folder).glob("*.pdf"))]


def report_paths(card_id, month):
    folder = data_dir() / "reports" / card_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{month}.json", folder / f"{month}.md"


def reconcile(card_id, month, use_llm=True, llm=None):
    """Run the rules for one statement against its history; optionally add AI explanations."""
    ledger, vault = Ledger(), TokenVault()
    record = ledger.load(card_id, month)
    history = ledger.history(card_id, month)
    results = run_checks(record, history)
    report = build_report(record, results, previous_cycle(record, history))

    if use_llm:
        llm = llm or get_llm()
        report["llm"] = llm.describe()
        facts = statement_facts(record)
        for f in report["alerts"]:
            f["ai"] = explainer.review_finding(llm, f, facts, vault, card_id, month)
        report["ai_summary"] = explainer.summarize_month(llm, llm_facts(report, record), vault, card_id, month)

    json_path, md_path = report_paths(card_id, month)
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(report, record, vault), encoding="utf-8")
    return report, json_path, md_path


def load_report(card_id, month):
    json_path, _ = report_paths(card_id, month)
    if not json_path.exists():
        reconcile(card_id, month, use_llm=False)
    return json.loads(json_path.read_text(encoding="utf-8"))


def ask(card_id, month, question, llm=None):
    ledger, vault = Ledger(), TokenVault()
    record = ledger.load(card_id, month)
    report = load_report(card_id, month)
    result = explainer.answer_question(llm or get_llm(), question, llm_facts(report, record), vault, card_id, month)
    result["answer"] = vault.restore(result["answer"])                 # restored only for the local user
    return result


def dispute(card_id, month, finding_id, llm=None):
    """Dispute summary: facts, channel and deadline come from code; the LLM only drafts the wording."""
    ledger, vault = Ledger(), TokenVault()
    record = ledger.load(card_id, month)
    report = load_report(card_id, month)
    finding = next((f for f in report["alerts"] if f["id"] == finding_id), None)
    if not finding:
        raise SystemExit(f"no alert {finding_id} in {card_id} {month}; alerts: {[f['id'] for f in report['alerts']]}")
    # the disputed line is the first dated evidence row (later rows are supporting context, e.g. the payment)
    dates = [d(e["date"]) for e in finding["evidence"] if e.get("date") and e.get("statement_month") is None]
    start = dates[0] if dates else d(record["statement"]["statement_date"])
    p = rulebook().params
    rupay = record.get("card_network") in settings()["dispute"]["rupay_domestic_networks"]
    window = p["dispute_window_days_rupay_domestic"] if rupay else p["dispute_window_days"]
    deadline = start + timedelta(days=window)
    first_seen = None
    for s in ledger.history(card_id, month) + [record]:
        keys = {e.get("description") for e in finding["evidence"]}
        if any(t["description"] in keys for t in s["transactions"]):
            first_seen = s["month"]
            break
    first_seen = first_seen or min((e["statement_month"] for e in finding["evidence"] if e.get("statement_month")), default=month)
    case = {
        "card": card_id, "cardholder": record["cardholder"]["name"], "statement_date": record["statement"]["statement_date"],
        "item": finding["title"], "rule_id": finding["rule_id"], "expected": finding["expected"],
        "actual": finding["actual"], "difference": finding.get("difference"), "reason": finding["title"],
        "calculation": finding.get("calculation"), "evidence": finding["evidence"], "first_appearance": first_seen,
        "deadline": deadline.isoformat(), "dispute_window_days": window, "counted_from": start.isoformat(),
        "days_left": (deadline - date.today()).days, "channel": settings()["dispute"]["channel"],
    }
    draft = explainer.draft_dispute(llm or get_llm(), case, vault, card_id, month)
    return vault.restore({"case": case, "draft": draft})
