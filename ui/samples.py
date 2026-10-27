"""Ready-made sample statements for judges, with a cache of live Nemotron reports (cost control).

sample_cache/ (built once by tools/build_sample_cache.py, committed with the app):
    manifest.json                   one entry per sample PDF: card token, month, statement date, total due
    workspace/ledger/CARD_n/*.json  the 15 samples, parsed and masked (as `audit.py ingest` stores them)
    workspace/vault/token_map.json  the token map of the sample cardholders, so CARD_1..3 are stable
    reports/<key>.json              the masked report of each sample from a live Nemotron run
    build_audit_log.jsonl           every payload sent to Nemotron while building the cache

Cache key = sample file + rulebook version + model, so changing either makes the cache miss.
A sample is always audited with its card's earlier months as history, exactly as if the user had
uploaded them oldest first. Only the sample PDFs are ever cached; user uploads never are.
"""
import hashlib
import json
import re
import shutil
from pathlib import Path

from cc_audit import pipeline
from cc_audit.config import ROOT, data_dir, settings
from cc_audit.ledger import Ledger
from cc_audit.masking import TokenVault

SAMPLES = ROOT / "all_15_statements"
CACHE = ROOT / "sample_cache"
WORKSPACE = CACHE / "workspace"
MANIFEST = CACHE / "manifest.json"
FILE_RE = re.compile(r"^Statement_(\d{4})_(\d{4}-\d{2})\.pdf$")


def sample_files():
    return sorted(p.name for p in SAMPLES.glob("*.pdf") if FILE_RE.match(p.name))


def manifest():
    """{sample file: {"card", "month", "statement_date", "total_due"}} ({} until the cache is built)."""
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["samples"] if MANIFEST.exists() else {}


def rulebook_version():
    return hashlib.sha256((ROOT / settings()["rulebook"]).read_bytes()).hexdigest()[:12]


def model_name():
    return settings()["llm"]["model"]


def cache_key(file):
    return f"{Path(file).stem}__rb-{rulebook_version()}__{re.sub(r'[^A-Za-z0-9.-]+', '-', model_name())}"


def cache_path(file):
    return CACHE / "reports" / f"{cache_key(file)}.json"


def cached_report(file):
    path = cache_path(file)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_to_cache(file, report):
    path = cache_path(file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def history_files(file):
    """This sample and the same card's earlier months, oldest first (the history its checks need)."""
    last4, month = FILE_RE.match(file).groups()
    return [f for f in sample_files() if FILE_RE.match(f).group(1) == last4 and FILE_RE.match(f).group(2) <= month]


def run_sample(file, use_llm, live=False, llm=None):
    """Audit one sample into the current data_dir() (the visitor's own sample folder).

    live=False: use the cached report when there is one; on a miss, run live and, with AI, save it.
    live=True:  always run the pipeline (parse -> mask -> ledger -> rules -> LLM) and leave the cache alone.
    Returns {"card", "month", "source": "cache" | "live", "generated_at", "ai"}.
    """
    canonical = (WORKSPACE / "vault" / "token_map.json").exists()
    entry = manifest().get(file)
    if canonical:                                   # same CARD_n tokens as the cache and the manifest
        vault_path = data_dir() / "vault" / "token_map.json"
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(WORKSPACE / "vault" / "token_map.json", vault_path)

    report = None if live else cached_report(file)
    if report is not None and entry:
        for f in history_files(file):
            e = manifest()[f]
            target = data_dir() / "ledger" / e["card"] / f"{e['month']}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(WORKSPACE / "ledger" / e["card"] / f"{e['month']}.json", target)
        json_path, _ = pipeline.report_paths(entry["card"], entry["month"])
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"card": entry["card"], "month": entry["month"], "source": "cache",
                "generated_at": report.get("generated_at"), "ai": bool(report.get("llm"))}

    vault, ledger = TokenVault(), Ledger()
    for f in history_files(file):
        record, _ = pipeline.ingest(SAMPLES / f, vault, ledger)
    report, _, _ = pipeline.reconcile(record["card_id"], record["month"], use_llm=use_llm, llm=llm)
    if use_llm and not live and canonical:
        save_to_cache(file, report)
    return {"card": record["card_id"], "month": record["month"], "source": "live",
            "generated_at": report.get("generated_at"), "ai": use_llm}
