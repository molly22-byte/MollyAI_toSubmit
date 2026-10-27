#!/usr/bin/env python3
"""Offline stress test for the Error Lab: inject errors into the clean sample statements and see what the rules catch.

Usage:  python tools/error_injection_test.py [--n 100] [--seed 42] [--out eval_out/]

Takes the clean statements (Status CLEAN in the answer key), applies N randomly chosen preset errors (one per run)
with the same functions the Error Lab page uses (ui/error_lab_presets.py), runs ONLY the deterministic rules engine
(no LLM, zero cost) and reports per-preset caught / missed, the overall detection rate, false alarms on the
untouched clean statements, related flags and presets that no clean statement can carry. Needs the sample cache
(python tools/build_sample_cache.py). Writes a Markdown and a CSV report to --out; nothing goes to data/.
"""
import argparse
import csv
import random
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cc_audit.config import use_data_dir  # noqa: E402
from cc_audit.parser import extract  # noqa: E402
from tools.evaluate import load_answer_key  # noqa: E402
from ui import error_lab_presets as lab  # noqa: E402
from ui import samples  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=100, help="number of injected errors (default 100)")
    ap.add_argument("--seed", type=int, default=42, help="random seed (default 42)")
    ap.add_argument("--out", default="eval_out/", help="folder for the .md and .csv report (default eval_out/)")
    args = ap.parse_args()
    lab.validate()
    if not samples.MANIFEST.exists():
        sys.exit("sample cache missing - run: python tools/build_sample_cache.py")

    statements = load_answer_key()["statements"]
    clean = [f for f in samples.sample_files() if statements.get(f, {}).get("Status") == "CLEAN"]
    rng = random.Random(args.seed)
    work_dir = Path(tempfile.mkdtemp(prefix="error_lab_"))
    try:
        with use_data_dir(work_dir):
            raws = {f: extract(samples.SAMPLES / f) for f in clean}
            ctxs = {f: lab.context(f) for f in clean}
            baseline = {}
            for f in clean:
                stmt, _ = lab.finalize(raws[f], lab.prepare(raws[f]))
                baseline[f] = lab.run(f, stmt)[1]["alerts"]
            pairs = [(f, p["id"]) for f in clean for p in lab.PRESETS if p["applies"](lab.prepare(raws[f]), ctxs[f])]
            unusable = [p for p in lab.PRESETS if not any(pid == p["id"] for _, pid in pairs)]
            rows = []
            for i in range(args.n):
                f, pid = rng.choice(pairs)
                work = lab.prepare(raws[f])
                change = lab.apply_preset(work, pid, rng, ctxs[f])
                stmt, rid_to_txn = lab.finalize(raws[f], work, keep_totals=True, gst_edited=change["gst_edited"])
                _, report = lab.run(f, stmt)
                result = lab.score([change], report, baseline[f], rid_to_txn)
                r = result["rows"][0]
                rows.append({"run": i + 1, "statement": f, "preset": pid, "expected_rules": " ".join(r["rules"]),
                             "status": r["status"], "flags": " ".join(x["rule_id"] for x in r["flags"]),
                             "related_flags": " ".join(x["rule_id"] for x in result["related"]),
                             "why_missed": "; ".join(r["why_missed"]), "change": r["note"]})
                print(f"{i + 1:>4} {r['status']:<22} {pid:<24} {f}  flags: {rows[-1]['flags'] or '-'}"
                      + (f"  related: {rows[-1]['related_flags']}" if rows[-1]["related_flags"] else ""))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"error_injection_seed{args.seed}_n{args.n}"
    with open(f"{stem}.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    per = defaultdict(Counter)
    for r in rows:
        per[r["preset"]][r["status"]] += 1
    total = Counter(r["status"] for r in rows)
    covered = total["CAUGHT"] + total["MISSED"]
    rate = total["CAUGHT"] / covered if covered else 0.0
    false_alarms = {f: [a["rule_id"] for a in alerts] for f, alerts in baseline.items()}
    related = Counter(rid for r in rows for rid in r["related_flags"].split())
    md = [f"# Error injection stress test (n={args.n}, seed={args.seed})", "",
          "Deterministic rules engine only (no LLM). One preset error per run, applied to the parsed data of a clean "
          "sample statement with the same functions as the Error Lab; statement totals kept consistent as a bank would print them.", "",
          f"- Clean statements: {', '.join(clean)}",
          f"- **Detection rate: {rate:.1%}** ({total['CAUGHT']} caught / {covered} injected errors a rule covers)",
          f"- Missed: {total['MISSED']} | Not covered by policy: {total['NOT COVERED BY POLICY']}",
          f"- False alarms on the untouched clean statements: {sum(len(v) for v in false_alarms.values())} "
          + (str({k: v for k, v in false_alarms.items() if v}) if any(false_alarms.values()) else "(none)"),
          f"- Related flags (extra alerts a change caused, not false alarms): {dict(related) or 'none'}", "",
          "| Preset | Rules | Runs | Caught | Missed | Not covered |", "|---|---|---|---|---|---|"]
    for p in lab.PRESETS:
        c = per.get(p["id"])
        if c:
            md.append(f"| {p['label']} | {', '.join(p['rules'])} | {sum(c.values())} | {c['CAUGHT']} | {c['MISSED']} | "
                      f"{c['NOT COVERED BY POLICY']} |")
    if unusable:
        md += ["", "Presets no clean statement can carry (not run):"]
        md += [f"- {p['label']} ({', '.join(p['rules'])}): needs {p['needs']}" for p in unusable]
    misses = [r for r in rows if r["status"] != "CAUGHT"]
    if misses:
        md += ["", "## Not caught", "", "| Run | Statement | Preset | Change | Why |", "|---|---|---|---|---|"]
        md += [f"| {r['run']} | {r['statement']} | {r['preset']} | {r['change']} | {r['why_missed'] or '-'} |" for r in misses]
    Path(f"{stem}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n" + "\n".join(md[:11]))
    print(f"\nreport: {stem}.md and {stem}.csv")


if __name__ == "__main__":
    main()
