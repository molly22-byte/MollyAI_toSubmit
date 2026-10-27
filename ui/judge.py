"""Judge passkey for the "For judges" pages (Rulebook, Error Lab, answer-key view, scorecard).

The passkey to compare against is JUDGE_PASSKEY: from .env locally (load_dotenv() in streamlit_app.py),
from the environment / App settings -> Secrets on Streamlit Cloud. If none is configured the pages stay
locked. Unlocking lasts for this browser session only. The passkey is never logged, printed or shown.
"""
import hmac
import os
import time

import streamlit as st

from ui.components import md

LOCKED_MESSAGE = "This section is for hackathon judges. The passkey is in our Devpost submission notes."
MAX_TRIES = 5
WAIT_SECONDS = 60


def configured_passkey():
    key = os.getenv("JUDGE_PASSKEY")
    if not key:
        try:
            key = st.secrets.get("JUDGE_PASSKEY")
        except Exception:                    # no secrets.toml locally
            key = None
    return str(key) if key else None


def unlocked():
    return bool(st.session_state.get("judge_ok")) and configured_passkey() is not None


def seconds_to_wait():
    return max(0, int(st.session_state.get("judge_wait_until", 0) - time.time()))


def try_unlock(typed):
    """'ok', 'wrong', 'wait' or 'not_configured'. Five wrong tries -> 60 seconds before the next one."""
    expected = configured_passkey()
    if not expected:
        return "not_configured"
    if seconds_to_wait():
        return "wait"
    if typed and hmac.compare_digest(typed.encode("utf-8"), expected.encode("utf-8")):
        st.session_state.judge_ok = True
        st.session_state.judge_fails = 0
        return "ok"
    st.session_state.judge_fails = st.session_state.get("judge_fails", 0) + 1
    if st.session_state.judge_fails >= MAX_TRIES:
        st.session_state.judge_wait_until = time.time() + WAIT_SECONDS
        st.session_state.judge_fails = 0
    return "wrong"


def lock():
    st.session_state.judge_ok = False


def locked_notice():
    md(f'<div class="note" style="font-size:14.5px">&#128274; {LOCKED_MESSAGE}</div>')
