"""The rules engine only ever receives the (modified) statement - never the judge's diff or expected rules.
Run: python -m pytest tests/"""
import inspect
import re
from pathlib import Path

from cc_audit import pipeline
from cc_audit.checks import run_checks

ENGINE = Path(__file__).resolve().parent.parent / "cc_audit"
SCORING_WORDS = re.compile(r"expect|diff|change|judge|should_flag|preset", re.I)


def test_engine_inputs_carry_no_diff_or_expectations():
    assert list(inspect.signature(run_checks).parameters) == ["current", "history"]
    for fn in (pipeline.ingest_parsed, pipeline.reconcile):
        bad = [p for p in inspect.signature(fn).parameters if SCORING_WORDS.search(p)]
        assert not bad, f"{fn.__name__} takes scoring data: {bad}"


def test_engine_never_imports_the_error_lab_or_ui():
    hits = [f"{p.name}:{n}" for p in ENGINE.rglob("*.py")
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"error_lab|expected_rules|^\s*(from|import)\s+ui\b", line)]
    assert not hits, hits


def test_presets_use_real_rule_ids():
    from ui import error_lab_presets
    error_lab_presets.validate()            # raises if a preset or the coverage map names a rule not in rulebook.json
