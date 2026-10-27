"""Layer G - Guardrails around every LLM call (runs on the device).

Before the call   check_outbound: the payload must not contain any real PII value from the token
                  vault, nor anything that looks like an email, phone, card number or PAN.
After the call    check_output: the reply must not contain PII, and every amount in it must
                  exist in the payload (the LLM explains, it does not calculate).
Prompt injection  statement text is sent as JSON inside <data> tags with a system rule that it is
                  data, and replies are parsed into fixed fields; anything else is discarded.
"""
import re

from .masking import PATTERNS

NUMBER = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|(?<![\w.,])\d+(?:\.\d+)?")


def numbers(text):
    out = set()
    for n in NUMBER.findall(text):
        try:
            out.add(round(float(n.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def find_pii(text, vault):
    hits = []
    low = text.lower()
    for value in vault.pii_values():
        if len(value) >= 4 and value.lower() in low:
            hits.append(value)
    for kind, pattern in PATTERNS.items():
        hits += [f"{kind} pattern" for _ in pattern.findall(text)]
    return hits


def check_outbound(payload_text, vault):
    leaks = find_pii(payload_text, vault)
    return {"passed": not leaks, "pii_found": len(leaks)}


def check_output(reply_text, payload_text, vault):
    leaks = find_pii(reply_text, vault)
    allowed = numbers(payload_text)
    invented = sorted(n for n in numbers(reply_text)
                      if n not in allowed and not (n <= 31 and n == int(n)) and not (2000 <= n <= 2100))
    return {"passed": not leaks and not invented, "pii_found": len(leaks), "invented_numbers": invented}
