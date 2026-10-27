"""Layer H - Audit log: an append-only record of what was masked and what was sent to the LLM.

data/audit_log.jsonl, one JSON object per line. It holds only masked text (the payload exactly as
the model received it), so the user can check the privacy claim themselves.
"""
import hashlib
import json
from datetime import datetime

from .config import data_dir


def log(stage, **fields):
    entry = {"timestamp": datetime.now().isoformat(timespec="seconds"), "stage": stage, **fields}
    with open(data_dir() / "audit_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def sha256(text):
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


def read(stage=None):
    path = data_dir() / "audit_log.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if stage is None or r["stage"] == stage]
