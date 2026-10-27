"""Settings and rulebook loading (paths are relative to the project root)."""
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Per-run override of the data folder. A ContextVar (not an env var) so that concurrent users of a
# hosted web UI, who share one process, each get their own ledger, reports, vault and audit log.
_data_dir_override = ContextVar("data_dir_override", default=None)


@lru_cache(maxsize=None)
def settings():
    return json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))


def data_dir():
    """The use_data_dir() override, else $MOLLYAI_DATA_DIR, else "data_dir" in settings.json."""
    path = Path(_data_dir_override.get() or os.environ.get("MOLLYAI_DATA_DIR") or ROOT / settings()["data_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def use_data_dir(path):
    """Within this block (in the current thread / context only) all storage goes to `path`."""
    token = _data_dir_override.set(Path(path))
    try:
        yield Path(path)
    finally:
        _data_dir_override.reset(token)


class Rulebook:
    """Read-only view of config/rulebook.json (built from reconciliation_rulebook.xlsx)."""

    def __init__(self, path=None):
        path = path or ROOT / settings()["rulebook"]
        if not Path(path).exists():
            raise SystemExit(f"{path} not found - run: python tools/build_rulebook.py")
        self.raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.params = self.raw["parameters"]
        self.rules = self.raw["rules"]
        self.not_errors = self.raw["not_errors"]
        self.slabs = self.raw["late_fee_slabs"]
        self.emi_plans = self.raw["emi_plans"]

    def p(self, name):
        return self.params[name]

    @property
    def tolerance(self):
        return float(self.params.get("rounding_tolerance", 1))

    def rule_text(self, rule_id):
        """The rule as the LLM should read it."""
        r = self.rules.get(rule_id)
        if not r:
            return None
        return {"rule_id": rule_id, "rule": r["rule"], "when_it_applies": r["when_it_applies"],
                "expected": r["expected"], "flag_if": r["flag_if"], "worked_example": r["worked_example"]}

    def not_error_text(self, case_id):
        n = self.not_errors.get(case_id)
        return {"case_id": case_id, **n} if n else None


@lru_cache(maxsize=None)
def rulebook():
    return Rulebook()
