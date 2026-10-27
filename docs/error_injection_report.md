# Error injection stress test (n=100, seed=42)

Deterministic rules engine only (no LLM). One preset error per run, applied to the parsed data of a clean sample statement with the same functions as the Error Lab; statement totals kept consistent as a bank would print them.

- Clean statements: Statement_2093_2026-05.pdf, Statement_4821_2026-05.pdf, Statement_4821_2026-06.pdf, Statement_7305_2026-05.pdf, Statement_7305_2026-06.pdf
- **Detection rate: 100.0%** (100 caught / 100 injected errors a rule covers)
- Missed: 0 | Not covered by policy: 0
- False alarms on the untouched clean statements: 0 (none)
- Related flags (extra alerts a change caused, not false alarms): {'F01': 2, 'F14': 2, 'C12': 1}

| Preset | Rules | Runs | Caught | Missed | Not covered |
|---|---|---|---|---|---|
| Late fee although the minimum was paid on time | F06 | 9 | 9 | 0 | 0 |
| Wrong late fee slab | F06 | 2 | 2 | 0 | 0 |
| GST not 18% of fees and interest | F13 | 7 | 7 | 0 | 0 |
| Wrong finance charge (interest) | F01 | 3 | 3 | 0 | 0 |
| Finance charge although the bill was paid in full | F02 | 3 | 3 | 0 | 0 |
| Cash advance fee without a cash withdrawal | F05 | 7 | 7 | 0 | 0 |
| Cash advance fee - wrong amount | F05 | 7 | 7 | 0 | 0 |
| Over-limit fee while within the credit limit | F08 | 4 | 4 | 0 | 0 |
| Forex markup not 3.5% | F10 | 4 | 4 | 0 | 0 |
| Duplicate transaction | C01 | 11 | 11 | 0 | 0 |
| EMI amount does not match the plan | C04 | 6 | 6 | 0 | 0 |
| EMI interest does not match the plan | C04, C06 | 8 | 8 | 0 | 0 |
| Total amount due does not add up | C10 | 5 | 5 | 0 | 0 |
| Minimum amount due is wrong | F14 | 4 | 4 | 0 | 0 |
| Unknown / unexplained fee | C11 | 11 | 11 | 0 | 0 |
| Payment due date too early | F15 | 5 | 5 | 0 | 0 |
| Annual fee - wrong amount | F12 | 4 | 4 | 0 | 0 |

Presets no clean statement can carry (not run):
- Promised refund missing (C09): needs a refund promised on an earlier statement (none of the clean samples has one)
