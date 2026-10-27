"""Layer D - Financial ledger (runs on the device).

Storage (plain JSON, one immutable file per card per statement month):

    data/ledger/CARD_1/2026-05.json
    data/ledger/CARD_1/2026-06.json
    ...

Every file is a normalized, masked statement (see normalizer.py). Nothing derived is stored:
EMI progress, recurring merchants, fee history and pending refunds are rebuilt from these files
each time, so re-ingesting a corrected statement can never leave stale state behind.
"""
import json
from datetime import date

from .config import data_dir


class Ledger:
    def __init__(self, root=None):
        self.root = root or data_dir() / "ledger"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, record):
        folder = self.root / record["card_id"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{record['month']}.json"
        replaced = path.exists()
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        return path, replaced

    def cards(self):
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def months(self, card_id):
        folder = self.root / card_id
        return sorted(p.stem for p in folder.glob("*.json")) if folder.exists() else []

    def load(self, card_id, month):
        path = self.root / card_id / f"{month}.json"
        if not path.exists():
            raise FileNotFoundError(f"no statement for {card_id} {month} in the ledger")
        return json.loads(path.read_text(encoding="utf-8"))

    def history(self, card_id, month):
        """All earlier statements of this card, oldest first. This is how past months are used."""
        return [self.load(card_id, m) for m in self.months(card_id) if m < month]

    def latest_month(self, card_id):
        months = self.months(card_id)
        return months[-1] if months else None


def d(iso):
    return date.fromisoformat(iso) if iso else None


def previous_cycle(current, history):
    """The statement immediately before `current`, or None if that month is missing from the ledger."""
    if not history:
        return None
    prev = history[-1]
    starts = d(current["statement"]["period_from"])
    if d(prev["statement"]["statement_date"]).toordinal() + 1 != starts.toordinal():
        return None
    return prev
