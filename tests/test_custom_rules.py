"""Judge-written rules: validated on save, and evaluate() never raises (fallback = an 'error' finding).
Run: python -m pytest tests/"""
import json
import random
from pathlib import Path

from ui import custom_rules as cr

LEDGER = Path(__file__).resolve().parent.parent / "sample_cache" / "workspace" / "ledger"


def records():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(LEDGER.glob("*/*.json"))]


def saved(examples=cr.EXAMPLES):
    rules = []
    for r in examples:
        rules.append(cr.new_rule(rules, r))
    return rules


def test_examples_are_valid_and_run_on_every_sample():
    rules = saved()
    assert [r["id"] for r in rules] == ["J01", "J02", "J03", "J04"]
    for rec in records():
        for f in cr.evaluate_all(rules, rec):
            assert f["status"] in ("ok", "alert"), f


def test_gst_ratio_rule_agrees_with_the_engine_on_clean_months():
    gst = saved()[3]
    statuses = {rec["month"] + rec["card_id"]: cr.result_label(cr.evaluate(gst, rec)) for rec in records()}
    assert all(v == "passes" for v in statuses.values()), statuses


def test_bad_rules_are_refused_on_save():
    for bad in [{}, {"name": "x", "kind": "python", "code": "import os"}, {"name": "", "kind": "line", "amount_op": ">", "amount": 1},
                {"name": "x", "kind": "line", "amount_op": ">", "amount": "1; drop"}, {"name": "x" * 99, "kind": "line"},
                {"name": "x", "kind": "field", "field": "__class__", "op": ">", "compare_to": "value", "value": 1}]:
        assert cr.validate(bad), bad


def test_evaluate_never_raises():
    rng = random.Random(7)
    keys = ["kind", "categories", "op", "value", "field", "compare_to", "other_field", "factor", "agg", "amount_op",
            "amount", "text", "text_op", "categories_b", "tolerance", "name", "id"]
    junk = [None, "", "x", -1, 0, 1e30, float("nan"), [], ["fee"], {"a": 1}, ">", "line", "field", "ratio", "total"]
    rec = records()[0]
    for _ in range(2000):
        rule = {k: rng.choice(junk) for k in rng.sample(keys, rng.randint(0, len(keys)))}
        out = cr.evaluate(rule, rec)
        assert isinstance(out, list) and out
    broken = {**saved()[1], "field": "minimum_amount_due"}
    out = cr.evaluate(broken, {"month": "2026-01", "statement": {}, "transactions": []})   # field missing on the record
    assert out[0]["status"] == "error" and "could not be evaluated" in out[0]["title"]


def test_covering_maps_changes_to_judge_rules():
    rules = saved()
    assert cr.covering(rules, {"where": {"kind": "field", "field": "minimum_amount_due"}}) == ["J02"]
    assert set(cr.covering(rules, {"where": {"kind": "txn"}}, category="fee")) == {"J01", "J03", "J04"}
    assert cr.covering(rules, {"where": {"kind": "field", "field": "available_credit"}}) == []


def test_saved_rules_persist_delete_and_survive_a_damaged_file(tmp_path, monkeypatch):
    path = tmp_path / "rules.json"
    monkeypatch.setenv("MOLLYAI_JUDGE_RULES", str(path))
    assert cr.load_rules() == []
    a = cr.add_saved(cr.EXAMPLES[0])
    b = cr.add_saved(cr.EXAMPLES[1])
    assert [r["id"] for r in cr.load_rules()] == ["J01", "J02"]          # read back from disk
    cr.delete_saved("J01")
    assert [r["id"] for r in cr.load_rules()] == ["J02"]
    assert cr.add_saved(cr.EXAMPLES[2])["id"] == "J01"                   # freed ID is reused
    path.write_text("{ not json", encoding="utf-8")                       # damaged file
    assert cr.load_rules() == [] and list(tmp_path.glob("rules.broken-*.json"))
    path.write_text(json.dumps({"rules": [{"id": "J07", "name": "bad", "kind": "field", "factor": "x"}, "junk", {"no": "id"}]}))
    rules = cr.load_rules()
    assert [r["id"] for r in rules] == ["J07"]                           # junk skipped, bad rule kept so it can be deleted
    assert cr.evaluate(rules[0], records()[0])[0]["status"] == "error"   # ...and the fallback reports it
    assert a["id"] == "J01" and b["id"] == "J02"
