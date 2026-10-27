"""The audit engine must never see the answer key. Run: python -m pytest tests/  (or python tests/test_engine_isolation.py)"""
import re
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "cc_audit"
FORBIDDEN = re.compile(r"answer[_ ]?key|DO_NOT_SHARE|tools\.evaluate|from ui\b|import ui\b", re.I)


def test_engine_never_mentions_the_answer_key():
    hits = [f"{p.relative_to(ENGINE.parent)}:{n}: {line.strip()}"
            for p in ENGINE.rglob("*.py")
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if FORBIDDEN.search(line)]
    assert not hits, "cc_audit/ must not read or import the answer key:\n" + "\n".join(hits)


if __name__ == "__main__":
    test_engine_never_mentions_the_answer_key()
    print("ok: cc_audit/ never mentions the answer key")
