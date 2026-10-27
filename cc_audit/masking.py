"""Layer B - PII masking and restore (runs on the device; the token map never leaves it).

Identity fields are swapped for stable placeholder tokens (NAME_1, CARD_1, ...). The same real
value always gets the same token, so CARD_1 is the same card in every month's statement.
The token map lives in data/vault/token_map.json and is only read here and by restore().
"""
import copy
import json
import re

from .config import data_dir

# cardholder field -> token prefix
FIELD_TOKENS = {
    "name": "NAME",
    "address": "ADDR",
    "mobile": "PHONE",
    "email": "EMAIL",
    "card_number": "CARD",
    "customer_id": "CUSTID",
    "pan": "PAN",
}

# Safety net for PII that turns up inside free text (descriptions, messages)
PATTERNS = {
    "EMAIL": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "PHONE": re.compile(r"(?<![\w])(?:\+91[ -]?)?[6-9]\d{4} ?\d{5}(?!\d)"),
    "CARD": re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)"),
    "PAN": re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
}
TOKEN_RE = re.compile(r"\b(?:NAME|ADDR|PHONE|EMAIL|CARD|CUSTID|PAN|STMTNO)_\d+\b")


class TokenVault:
    def __init__(self, path=None):
        self.path = path or data_dir() / "vault" / "token_map.json"
        self.tokens = {}            # token -> {"value", "type"}
        if self.path.exists():
            self.tokens = json.loads(self.path.read_text(encoding="utf-8"))
        self.by_value = {(v["type"], v["value"]): t for t, v in self.tokens.items()}

    def token_for(self, value, prefix):
        key = (prefix, value)
        if key not in self.by_value:
            n = 1 + sum(1 for t in self.tokens if t.startswith(prefix + "_"))
            token = f"{prefix}_{n}"
            self.tokens[token] = {"value": value, "type": prefix}
            self.by_value[key] = token
        return self.by_value[key]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.tokens, indent=2, ensure_ascii=False), encoding="utf-8")

    def pii_values(self):
        """Every real value in the vault (used by the guardrails to scan outbound text)."""
        return [v["value"] for v in self.tokens.values() if v["value"]]

    def restore(self, obj):
        """Put real values back into any JSON-like object. Only for the local user view."""
        if isinstance(obj, str):
            return TOKEN_RE.sub(lambda m: self.tokens.get(m.group(0), {}).get("value", m.group(0)), obj)
        if isinstance(obj, list):
            return [self.restore(v) for v in obj]
        if isinstance(obj, dict):
            return {k: self.restore(v) for k, v in obj.items()}
        return obj


def card_network(card_number):
    """Card network from the first digit (kept: it decides the dispute window, and is not identifying)."""
    first = card_number.strip()[:1]
    return {"4": "VISA", "5": "MASTERCARD", "3": "AMEX", "6": "RUPAY"}.get(first, "UNKNOWN")


def mask_statement(raw, vault):
    """Return (masked copy of a parsed statement, list of masked field names)."""
    stmt = copy.deepcopy(raw)
    holder = stmt["cardholder"]
    masked_fields = []
    known = []                                      # (real value, token) to scrub from free text
    for field, prefix in FIELD_TOKENS.items():
        value = holder.get(field)
        if isinstance(value, list):
            holder[field] = [vault.token_for(v, prefix) for v in value]
            known += list(zip(value, holder[field]))
        elif value:
            holder[field] = vault.token_for(value, prefix)
            known.append((value, holder[field]))
        masked_fields.append(field)

    stmt["card_network"] = card_network(raw["cardholder"].get("card_number", ""))
    number = stmt["statement"].get("statement_no")
    if number:                                      # embeds the card's last 4 digits
        stmt["statement"]["statement_no"] = vault.token_for(number, "STMTNO")
        known.append((number, stmt["statement"]["statement_no"]))
        masked_fields.append("statement_no")

    known.sort(key=lambda kv: -len(kv[0]))

    def scrub(text):
        for value, token in known:
            text = text.replace(value, token)
        for prefix, pattern in PATTERNS.items():
            text = pattern.sub(lambda m: vault.token_for(m.group(0), prefix), text)
        return text

    for t in stmt["transactions"]:
        t["description"] = scrub(t["description"])
    stmt["messages"] = [scrub(m) for m in stmt["messages"]]
    stmt["how_to_pay"] = [scrub(m) for m in stmt["how_to_pay"]]
    return stmt, masked_fields
