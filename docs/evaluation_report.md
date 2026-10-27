# Evaluation report (latest)

Generated from the shipped `sample_cache/` (the 15 sample statements, reconciled with live NVIDIA Nemotron 3 Super calls on Nebius Token Factory) against `answer_key_DO_NOT_SHARE_WITH_ENGINE.xlsx`.
Reproduce: see "Run locally" in the README (`tools/evaluate.py` and `tools/evaluate_report.py`).

## tools/evaluate.py

```
== Extraction (field level, against the Transactions sheet)
   field accuracy 1277/1277 = 100.0%
== Reconciliation (alerts vs planted errors)
   OK  Statement_2093_2026-05.pdf  planted -  raised -
   OK  Statement_2093_2026-06.pdf  planted ['C12']  raised ['C12']
   OK  Statement_2093_2026-07.pdf  planted ['F08']  raised ['F08']
   OK  Statement_2093_2026-08.pdf  planted ['C05']  raised ['C05']
   OK  Statement_2093_2026-09.pdf  planted ['C10', 'F06']  raised ['C10', 'F06']
   OK  Statement_4821_2026-05.pdf  planted -  raised -
   OK  Statement_4821_2026-06.pdf  planted -  raised -
   OK  Statement_4821_2026-07.pdf  planted ['C01']  raised ['C01']
   OK  Statement_4821_2026-08.pdf  planted ['C07', 'F15']  raised ['C07', 'F15']
   OK  Statement_4821_2026-09.pdf  planted ['F12']  raised ['F12']
   OK  Statement_7305_2026-05.pdf  planted -  raised -
   OK  Statement_7305_2026-06.pdf  planted -  raised -
   OK  Statement_7305_2026-07.pdf  planted ['F01']  raised ['F01']
        not-error N01: recognised as allowed
        not-error N07: recognised as allowed
   OK  Statement_7305_2026-08.pdf  planted ['C09']  raised ['C09']
        not-error N06: recognised as allowed
   OK  Statement_7305_2026-09.pdf  planted ['F10']  raised ['F10']
        not-error N02: recognised as allowed
   precision 100% (12/12)   recall 100% (12/12)
== Privacy
   masking recall 100% (30/30 planted PII values never appear in the ledger, masked reports or LLM traffic)
   over-masking 0/308 transaction descriptions contain a token
== LLM layer (from data/audit_log.jsonl)
   calls 27, blocked before sending 0, replies with PII 0, replies with invented amounts 0 (0%, these fall back to templates)
   AI rule-check verdicts on alerts: {'agree': 12}
```

## tools/evaluate_report.py


| Metric | Value |
|---|---|
| statements evaluated | 15 |
| numerical correctness pct | 100.0 |
| groundedness pct | 100.0 |
| completeness recall pct | 100.0 |
| extra lines | 0 |
| descriptions changed | 0 |
| detection precision pct | 100.0 |
| detection recall pct | 100.0 |
| planted errors | 12 |
| caught | 12 |
| missed | 0 |
| false alarm rules | 0 |
| clean statements with alerts | 0/5 |
| pii leaks reports ledger llm | 0 |
| pii leaks audit log | 0 |
| card last4 visible in masked data | 0/15 |
| token issues | 0 |
| llm fallback rate pct | 0.0 |

## Per statement

| File | Planted | Caught | Missed | False alarms | Num issues | Grounded issues | Lines matched | PII leaks |
|---|---|---|---|---|---|---|---|---|
| Statement_2093_2026-05.pdf | clean | - | - | - | 0 | 0 | 23/23 | 0 |
| Statement_2093_2026-06.pdf | C12 | C12 | - | - | 0 | 0 | 21/21 | 0 |
| Statement_2093_2026-07.pdf | F08 | F08 | - | - | 0 | 0 | 24/24 | 0 |
| Statement_2093_2026-08.pdf | C05 | C05 | - | - | 0 | 0 | 21/21 | 0 |
| Statement_2093_2026-09.pdf | C10, F06 | C10, F06 | - | - | 0 | 0 | 21/21 | 0 |
| Statement_4821_2026-05.pdf | clean | - | - | - | 0 | 0 | 19/19 | 0 |
| Statement_4821_2026-06.pdf | clean | - | - | - | 0 | 0 | 20/20 | 0 |
| Statement_4821_2026-07.pdf | C01 | C01 | - | - | 0 | 0 | 19/19 | 0 |
| Statement_4821_2026-08.pdf | C07, F15 | C07, F15 | - | - | 0 | 0 | 18/18 | 0 |
| Statement_4821_2026-09.pdf | F12 | F12 | - | - | 0 | 0 | 16/16 | 0 |
| Statement_7305_2026-05.pdf | clean | - | - | - | 0 | 0 | 24/24 | 0 |
| Statement_7305_2026-06.pdf | clean | - | - | - | 0 | 0 | 20/20 | 0 |
| Statement_7305_2026-07.pdf | F01 | F01 | - | - | 0 | 0 | 20/20 | 0 |
| Statement_7305_2026-08.pdf | C09 | C09 | - | - | 0 | 0 | 22/22 | 0 |
| Statement_7305_2026-09.pdf | F10 | F10 | - | - | 0 | 0 | 20/20 | 0 |

## Issues found

