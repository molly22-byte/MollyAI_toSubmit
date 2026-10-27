#!/usr/bin/env python3
"""Streamlit web UI for the statement audit (hostable on Streamlit Community Cloud).

Run:  streamlit run streamlit_app.py

Every browser session gets its own data folder (ledger, reports, token map, audit log) in a temp
directory, so visitors of a hosted demo never see each other's statements. Uploaded PDFs are
written to a temp file inside that folder only for parsing and deleted right after.
The API key comes from st.secrets["NEBIUS_API_KEY"], else the NEBIUS_API_KEY environment variable.
"""
import json
import io
import os
import shutil
import tempfile
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from cc_audit import pipeline
from cc_audit.config import data_dir, settings, use_data_dir
from cc_audit.ledger import Ledger
from cc_audit.llm import get_llm
from cc_audit.masking import TokenVault
from cc_audit.report import payment_advice
from tools.unlock_pdf import WrongPassword, is_encrypted, open_statement
from ui import answer_key_view, custom_rules_page, error_lab, judge, rules, samples
from ui.components import esc, inr, md, pill, show_value, table

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)   # local .env (NEBIUS_API_KEY, JUDGE_PASSKEY) wins over stale shell vars;
                                            # there is no .env on Streamlit Cloud (Secrets are used there)
SESSIONS = Path(tempfile.gettempdir()) / "mollyai_sessions"
SESSION_MAX_AGE_HOURS = 12
MAX_UPLOAD_MB = 10
LIVE_RUNS_PER_SESSION = 3
FEATURED = ["Statement_2093_2026-09.pdf", "Statement_4821_2026-08.pdf", "Statement_7305_2026-07.pdf"]
AI_SPARK = ('<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l2.4 6.6L21 11l-6.6 '
            '2.4L12 20l-2.4-6.6L3 11l6.6-2.4z"/></svg>')

st.set_page_config(page_title="MollyAI - Statement audit", page_icon="🛡️", layout="wide",
                   initial_sidebar_state="collapsed")


# ---------- setup: API key and per-session data folder ----------

def load_api_key():
    """Copy the key from Streamlit secrets into the environment variable the LLM client reads.
    Secrets win; the environment variable is only the fallback."""
    env = settings()["llm"].get("api_key_env", "LLM_API_KEY")
    try:
        key = st.secrets.get(env)
    except Exception:                       # no secrets.toml: fine, the environment variable is the fallback
        key = None
    if key:
        os.environ[env] = str(key)


def prune_old_sessions():
    """Streamlit has no reliable session-end hook, so folders untouched for a while are removed."""
    if not SESSIONS.exists():
        return
    cutoff = time.time() - SESSION_MAX_AGE_HOURS * 3600
    for folder in SESSIONS.iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


def session_dir():
    folder = st.session_state.get("data_dir")
    if not folder or not Path(folder).exists():
        prune_old_sessions()
        SESSIONS.mkdir(parents=True, exist_ok=True)
        folder = tempfile.mkdtemp(prefix="s_", dir=SESSIONS)
        st.session_state.data_dir = folder
    os.utime(folder)                        # mark the session as active for prune_old_sessions()
    return Path(folder)


def sample_dir():
    """Samples get their own ledger and token map, so CARD_1..3 never mix with the visitor's uploads."""
    return session_dir() / "samples"


def errorlab_dir():
    """The Error Lab's own folder, so planted errors never touch the visitor's uploads or sample reports."""
    return session_dir() / "errorlab"


WORKSPACES = {"sample": sample_dir, "errorlab": errorlab_dir}


def llm_available():
    cfg = settings()["llm"]
    return bool(os.environ.get(cfg.get("api_key_env", "LLM_API_KEY")))


def cards_overview():
    """One entry per card with its months and the alert count of each saved report."""
    ledger, vault = Ledger(), TokenVault()
    out = []
    for card in ledger.cards():
        months = []
        for m in ledger.months(card):
            path = data_dir() / "reports" / card / f"{m}.json"
            alerts = len(json.loads(path.read_text(encoding="utf-8"))["alerts"]) if path.exists() else None
            months.append({"month": m, "alerts": alerts})
        if not months:
            continue
        rec = ledger.load(card, months[-1]["month"])
        out.append({"card": card, "number": vault.restore(card), "name": vault.restore(rec["cardholder"]["name"]),
                    "months": months})
    return out


# ---------- navigation and messages ----------

def go(view, **kw):
    st.session_state.view = {"name": view, **kw}


def open_rule(rule_id):
    """Rule links: open the Rulebook filtered to this rule."""
    st.session_state.rb_search = rule_id
    go("rulebook", rule=rule_id)


def flash(msg, kind="info"):
    st.session_state.setdefault("flash", []).append((kind, msg))


def show_flash():
    for kind, msg in st.session_state.pop("flash", []):
        (st.error if kind == "error" else st.success if kind == "success" else st.info)(msg)


# ---------- formatting ----------

def ai_label(source, ai_text, other_text):
    return f'{AI_SPARK}{ai_text}' if source == "ai" else other_text


# ---------- look and feel ----------

CSS = """
<style>
:root{
 --header:#000B18; --primary:#1F3B73; --accent:#49A8C0; --accent-bg:#E8F4F8;
 --bg:#F3F1F2; --surface:#FFFFFF; --border:#E2DFE1; --text:#0E1A2B; --muted:#667085;
 --ok:#2E7D5B; --ok-bg:#E4F2EB; --alert:#B42335; --alert-bg:#FBE8EA;
 --warn:#9A6A1F; --warn-bg:#FBF1DE; --ai:#1F3B73; --ai-bg:#EAF5F8; --ai-line:#C9E5ED;
 --serif:"Playfair Display",Georgia,"Times New Roman",serif;
}
#MainMenu, footer, [data-testid="stMainMenu"], [data-testid="stAppDeployButton"], [data-testid="stDecoration"],
[data-testid="stStatusWidget"]{display:none !important; visibility:hidden}
[data-testid="stHeader"]{background:transparent}
.block-container{max-width:1120px; padding-top:1.2rem; padding-bottom:1rem}
h1, h2, h3{font-family:var(--serif) !important; color:var(--primary) !important}

.topbar{background:var(--header);color:#fff;border-bottom:3px solid var(--accent);border-radius:10px;padding:14px 20px;
 display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:10px;color:#FBF8F2;font:600 21px var(--serif)}
.brand small{display:block;font:500 11px Inter,sans-serif;color:#9FD3E1;letter-spacing:.14em;text-transform:uppercase}
.logo{width:34px;height:34px;border-radius:50%;border:1.5px solid var(--accent);display:grid;place-items:center}
.privacy-badge{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:500;color:#E6F4FA;
 background:rgba(73,168,192,.16);border:1px solid rgba(73,168,192,.6);padding:5px 11px;border-radius:999px}

.hero h1{font-size:34px;line-height:1.15;margin:0 0 8px}
.lead{color:var(--muted);font-size:15px}
.feat{list-style:none;padding:0;margin:16px 0 0;display:grid;gap:12px}
.feat li{display:flex;gap:10px;align-items:flex-start;font-size:14px}
.feat .ic{flex:none;width:28px;height:28px;border-radius:50%;border:1px solid var(--accent);background:var(--accent-bg);
 color:var(--accent);display:grid;place-items:center;font-weight:700;font-size:13px}
.muted{color:var(--muted);font-size:13.5px}
.sec{font:600 19px var(--serif);color:var(--primary);margin:0 0 10px;padding-bottom:8px;border-bottom:1px solid var(--border);
 display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.sec .muted{font-family:Inter,sans-serif;font-weight:500}

.pill{display:inline-flex;align-items:center;gap:4px;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;
 line-height:1.6;white-space:nowrap;font-family:Inter,sans-serif}
.bad{background:var(--alert-bg);color:var(--alert)} .ok{background:var(--ok-bg);color:var(--ok)}
.inf{background:var(--warn-bg);color:var(--warn)} .code{background:var(--bg);color:var(--muted);border:1px solid var(--border);font-weight:500}

.cc-num{font:600 17px var(--serif);color:var(--primary);letter-spacing:.06em;white-space:nowrap}
.cc-who{font-size:13px;color:var(--muted);margin:2px 0 6px}
.cc-top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;flex-wrap:wrap}

.split{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}
.split div{border-radius:10px;padding:10px 12px;font-size:13.5px}
.split .you{background:var(--bg);border:1px solid var(--border)}
.split .them{background:var(--ai-bg);border:1px solid var(--ai-line)}
.split span{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;color:var(--muted);margin-bottom:2px}
.split .them span{color:var(--ai)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:8px 0}
.stat{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.stat .k{font-size:12px;color:var(--muted);font-weight:500;text-transform:uppercase;letter-spacing:.04em}
.stat b{display:block;font:600 21px var(--serif);margin-top:3px;color:var(--primary)}

.fact{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin:10px 0;color:var(--text)}
.ai{background:var(--ai-bg);border:1px solid var(--ai-line);border-radius:10px;padding:12px 14px;margin:10px 0;color:var(--ai)}
.note{background:var(--warn-bg);border:1px solid var(--warn-bg);border-radius:10px;padding:10px 12px;margin:10px 0;color:var(--warn);font-size:13.5px}
.label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;color:var(--muted);margin-bottom:4px;
 display:flex;align-items:center;gap:6px}
.ai .label{color:var(--ai)}
.ai ul{margin:8px 0 0;padding-left:20px}
.facts3{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:6px}
.facts3 div{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 10px}
.facts3 span{display:block;font-size:12px;color:var(--muted)}
.facts3 b{font:600 17px var(--serif);color:var(--primary)}
.facts3 .neg b{color:var(--alert)}
.alert-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font:600 19px var(--serif);color:var(--primary);margin-bottom:4px}
.alert-head .t{flex:1;min-width:200px}

.table-wrap{border:1px solid var(--border);border-radius:10px;overflow:auto;margin:8px 0;background:#fff}
.table-wrap table{width:100%;border-collapse:collapse;font-size:14px;margin:0}
.table-wrap th,.table-wrap td{text-align:left;padding:8px 10px;border:0;border-bottom:1px solid var(--border);vertical-align:top}
.table-wrap th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em;background:var(--bg)}
.table-wrap tr:last-child td{border-bottom:0}
.table-wrap .num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
code.tok{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg);border:1px solid var(--border);
 padding:1px 5px;border-radius:5px;color:var(--text)}

/* bordered containers = cards; alert cards get the crimson left border */
[data-testid="stVerticalBlockBorderWrapper"], .stVerticalBlock[class*="st-key-"]{background:var(--surface)}
div[class*="st-key-alert_"]{border-left:4px solid var(--alert) !important}
div[class*="st-key-card_"]{border-top:3px solid var(--primary) !important}
div[class*="st-key-chip_"] button, div[class*="st-key-tab_"] button{padding:2px 10px;min-height:0;font-size:13px;border-radius:8px}
div[class*="st-key-chip_bad_"] button{color:var(--alert);border-color:#F1C4CA;background:var(--alert-bg)}
div[class*="st-key-chip_ok_"] button{color:var(--ok);border-color:#BFDECF;background:var(--ok-bg)}

.app-footer{margin:14px 0 8px;padding:12px 16px;background:#E3F3F9;border:1px solid #BFE3EE;border-radius:10px;
 color:var(--primary);font-size:12.5px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
div.st-key-data_box{margin-top:8px}
div.st-key-judge_box{margin-top:24px}
div.st-key-conclusion{border-left:4px solid var(--primary) !important}
div.st-key-data_box [data-testid="stCaptionContainer"]{margin:0}
@media (max-width:640px){
 .hero h1{font-size:24px} .split{grid-template-columns:1fr} .brand{font-size:18px}
 .block-container{padding-left:1rem;padding-right:1rem}
}
</style>
"""

HEADER = """
<div class="topbar">
 <div class="brand"><span class="logo"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#49A8C0"
  stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6l8-3z"/>
  <path d="M8.5 12l2.5 2.5 4.5-5"/></svg></span><span>MollyAI<small>Credit card statement audit</small></span></div>
 <span class="privacy-badge"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
  stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/>
  </svg>Private &middot; your identity never reaches the AI</span>
</div>
"""

FOOTER = """
<div class="app-footer"><span>Calculations are deterministic. AI explanations by NVIDIA Nemotron on Nebius Token Factory.</span>
<span>Names and card numbers are masked before any AI call.</span></div>
"""


# ---------- actions (each calls the cc_audit pipeline functions) ----------

def upload_statement(uploaded, password, use_llm):
    data = uploaded.getvalue()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        flash(f"This file is larger than {MAX_UPLOAD_MB} MB.", "error")
        return
    folder = data_dir() / "_upload"
    folder.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if is_encrypted(tmp):
            with open_statement(tmp, password):    # raises WrongPassword; opened in memory, nothing written
                pass
        record, replaced = pipeline.ingest(tmp, password=password)
    except WrongPassword as e:
        flash(str(e), "error")
        return "wrong_password"
    except Exception as e:                          # not a statement this parser understands
        flash(f"Could not read this statement: {e}", "error")
        return
    finally:
        Path(tmp).unlink(missing_ok=True)
        password = None
    card, month = record["card_id"], record["month"]
    pipeline.reconcile(card, month, use_llm=use_llm)
    later = [m for m in Ledger().months(card) if m > month]
    if replaced:
        flash(f"{month} was already uploaded; it has been replaced and checked again.")
    if later:
        flash(f"Later months ({', '.join(later)}) were checked without this month. Use 'Re-check later months' below.")
    go("report", card=card, month=month)


def live_runs_left():
    return LIVE_RUNS_PER_SESSION - st.session_state.get("live_runs", 0)


def count_live_run():
    """One live Nemotron run used (sample 'Re-run live', uncached samples and Error Lab runs share the limit)."""
    st.session_state.live_runs = st.session_state.get("live_runs", 0) + 1


def run_sample(file, live):
    """Audit a sample with its card's earlier months, from the cache or live (parse -> mask -> rules -> Nemotron)."""
    cached = not live and samples.cached_report(file) is not None
    use_llm = llm_available() and (cached or live_runs_left() > 0)
    if not cached and not use_llm:
        flash("Live AI runs are used up for this session (or no API key is set), so this sample was checked "
              "without AI explanations. Every calculation and alert is the same.")
    months = len(samples.history_files(file))
    msg = ("Loading the cached report..." if cached else
           f"Running the audit live on {months} month(s) of this card"
           + (" with Nemotron on Nebius - about 30-60 seconds per alert..." if use_llm else "..."))
    with st.spinner(msg):
        with use_data_dir(sample_dir()):
            info = samples.run_sample(file, use_llm=use_llm, live=live,
                                      llm=get_llm() if use_llm and not cached else None)
    if info["source"] == "live" and use_llm:
        count_live_run()
    st.session_state.setdefault("sample_runs", {})[file] = info
    go("report", card=info["card"], month=info["month"], ws="sample")


def reset_data():
    shutil.rmtree(st.session_state.pop("data_dir", ""), ignore_errors=True)
    for key in ("answers", "dispute", "confirm_reset", "sample_runs", "lab"):
        st.session_state.pop(key, None)
    go("home")
    flash("All local data (ledger, reports, token map, audit log) deleted.", "success")


# ---------- views ----------

def set_confirm_reset(value):
    st.session_state.confirm_reset = value


def data_section():
    """Small strip at the bottom of every page: where the data lives, and the reset (with a confirmation step)."""
    with st.container(border=True, key="data_box"):
        if not st.session_state.get("confirm_reset"):
            c1, c2 = st.columns([5, 1.3], vertical_alignment="center")
            c1.caption(f"🔒 **Your data** stays in a private temporary folder for this browser session only - no other "
                       f"visitor can see it. It is deleted after {SESSION_MAX_AGE_HOURS} hours of inactivity.")
            c2.button("Reset all local data", key="reset", width="stretch", on_click=set_confirm_reset, args=(True,))
        else:
            c1, c2, c3 = st.columns([4, 1.1, 1], vertical_alignment="center")
            c1.markdown("**Delete all uploaded statements, reports, the token map and the audit log?**")
            c2.button("Yes, delete", key="reset_yes", type="primary", width="stretch", on_click=reset_data)
            c3.button("Cancel", key="reset_no", width="stretch", on_click=set_confirm_reset, args=(False,))


def judge_submit():
    result = judge.try_unlock(st.session_state.get("judge_typed", ""))
    st.session_state.judge_typed = ""                       # never keep what was typed
    st.session_state.judge_msg = {"ok": None, "wrong": "Wrong passkey", "not_configured": "Judge access is not configured.",
                                  "wait": None}[result]
    if result == "ok":
        go("rulebook")                                      # judges land on the full list of rules


def judges_section():
    """'For judges' strip at the bottom: passkey, then links to the Rulebook, Error Lab and scorecard."""
    with st.container(border=True, key="judge_box"):
        if judge.configured_passkey() is None:
            st.caption("**For judges** · Judge access is not configured.")
        elif judge.unlocked():
            with st.container(horizontal=True, gap="small", vertical_alignment="center"):
                st.caption("**For judges** · unlocked for this session")
                st.button("Rulebook", key="nav_rulebook", on_click=go, args=("rulebook",))
                st.button("Error Lab", key="nav_errorlab", on_click=go, args=("errorlab",), kwargs={"ws": "errorlab"})
                st.button("Custom rules", key="nav_customrules", on_click=go, args=("customrules",))
                st.button("All samples scorecard", key="nav_scorecard", on_click=go, args=("scorecard",))
                st.button("Lock", key="judge_lock", on_click=judge.lock)
        else:
            c1, c2, c3 = st.columns([2.2, 2.4, 1], vertical_alignment="bottom")
            c1.caption("**For judges** · Rulebook, Error Lab and answer-key view")
            c2.text_input("Judge passkey", type="password", key="judge_typed", label_visibility="collapsed",
                          placeholder="Judge passkey")
            wait = judge.seconds_to_wait()
            c3.button("Unlock", key="judge_unlock", on_click=judge_submit, disabled=bool(wait), width="stretch")
            if wait:
                st.caption(f"Too many wrong tries - please wait {wait} seconds.")
            elif st.session_state.get("judge_msg"):
                st.caption(st.session_state.pop("judge_msg"))


def judge_page(title, render):
    st.button("← Home", on_click=go, args=("home",))
    with st.container(border=True):
        st.markdown(f"## {title}")
        if judge.unlocked():
            render()
        else:
            judge.locked_notice()


def home():
    llm_ok, cfg = llm_available(), settings()["llm"]
    left, right = st.columns([1.1, 1], gap="large")
    with left:
        md("""<div class="hero"><h1>Find what's wrong in your credit card bill</h1>
<p class="lead">Upload your monthly statement. MollyAI checks every fee, EMI and refund against the bank's rules and your
own history, and explains each problem in plain language.</p>
<ul class="feat">
 <li><span class="ic">&#8377;</span><span><b>Every charge recalculated</b><br><span class="muted">Interest, late fees, forex
 markup, GST and EMIs checked with exact formulas.</span></span></li>
 <li><span class="ic">&#8635;</span><span><b>Compared with your past months</b><br><span class="muted">Duplicates, price
 increases, missing refunds and EMIs that should have ended.</span></span></li>
 <li><span class="ic">&#9673;</span><span><b>Your identity is masked before the AI</b><br><span class="muted">Name and card
 number are replaced with tokens before the AI sees anything.</span></span></li>
</ul></div>""")
    with right:
        with st.container(border=True):
            md('<div class="sec">Upload a statement</div>')
            n = st.session_state.setdefault("upload_n", 0)
            uploaded = st.file_uploader("Statement PDF", type=["pdf"], key=f"upload_{n}",
                                        help=f"One month at a time, oldest first. PDF only, up to {MAX_UPLOAD_MB} MB.")
            password = None
            if uploaded is not None:
                try:
                    encrypted = is_encrypted(io.BytesIO(uploaded.getvalue()))
                except Exception:
                    encrypted = False                   # not a readable PDF: the parser reports it on upload
                if encrypted:
                    password = st.text_input("This statement is password protected. Enter its password",
                                             type="password", key=f"pw_{n}_{st.session_state.setdefault('pw_n', 0)}",
                                             help="Used only to open the PDF in memory. It is never stored or logged.")
            use_llm = st.toggle("Add AI explanations", value=llm_ok, disabled=not llm_ok, key="use_llm",
                                help="AI explains each alert, checks it against the rule text and summarises the month.")
            st.caption(f"NVIDIA Nemotron on Nebius · `{cfg['model']}`")
            if st.button("Upload and check", type="primary", width="stretch", disabled=uploaded is None):
                msg = ("Checking your statement... with AI explanations this takes about 30-60 seconds per alert."
                       if use_llm else "Checking your statement...")
                with st.spinner(msg):
                    result = upload_statement(uploaded, password, use_llm and llm_ok)
                password = None
                if result == "wrong_password":
                    st.session_state.pw_n += 1      # clears the password field, keeps the file
                else:
                    st.session_state.upload_n = n + 1   # drops the file and the password from the widgets
                st.rerun()
            st.caption("Upload one month at a time, oldest first. Each month is compared with the months uploaded before it.")

    sample_section(llm_ok)

    cards = cards_overview()
    if cards:
        st.write("")
        md('<div class="sec">Your cards <span class="muted">&middot; click a month to open its report</span></div>')
        cols = st.columns(min(3, len(cards)))
        for i, c in enumerate(cards):
            total = sum(m["alerts"] or 0 for m in c["months"])
            with cols[i % len(cols)]:
                with st.container(border=True, key=f"card_{c['card']}"):
                    badge = pill(f"{total} to review", "bad") if total else pill("all good", "ok")
                    md(f'<div class="cc-top"><div><div class="cc-num">{esc(c["number"])}</div>'
                       f'<div class="cc-who">{esc(c["name"])}</div></div>{badge}</div>')
                    with st.container(horizontal=True, gap="small"):
                        for m in c["months"]:
                            if m["alerts"]:
                                label, kind = f"{m['month']} · {m['alerts']} alert{'s' if m['alerts'] > 1 else ''}", "bad"
                            elif m["alerts"] == 0:
                                label, kind = f"{m['month']} · OK", "ok"
                            else:
                                label, kind = m["month"], "na"
                            st.button(label, key=f"chip_{kind}_{c['card']}_{m['month']}", on_click=go,
                                      args=("report",), kwargs={"card": c["card"], "month": m["month"]})


def sample_section(llm_ok):
    files, entries, runs = samples.sample_files(), samples.manifest(), st.session_state.get("sample_runs", {})
    st.write("")
    with st.container(border=True):
        md('<div class="sec">Try a sample statement <span class="muted">&middot; no upload needed</span></div>')
        featured = [f for f in FEATURED if f in entries]
        if featured:
            st.caption("**Start here** - each of these statements has problems for MollyAI to find:")
            with st.container(horizontal=True, gap="small"):
                for f in featured:
                    st.button(f"{entries[f]['card']} · {entries[f]['month']}", key=f"featured_{f}", type="primary",
                              on_click=run_sample, args=(f, False))
        st.caption("Or pick any of the 15 synthetic statements (3 cards x 5 months, May-Sep 2026). Each sample is audited "
                   "together with its card's earlier months, exactly as if they had been uploaded oldest first.")

        def label(f):
            e = entries.get(f)
            name = f"{e['card']} · {e['month']}" if e else f
            if f in runs and e:                    # the tag is only revealed once the report has been shown
                tag = answer_key_view.statement_status(e["statement_date"], e["total_due"])
                name += f"  ·  {tag}" if tag else ""
            return f"{name}  ({f})" if e else name

        c1, c2, c3 = st.columns([2.2, 1.2, 1], vertical_alignment="bottom")
        file = c1.selectbox("Sample statement", files, format_func=label, key="sample_file")
        cached = samples.cached_report(file) is not None
        c2.button("Run audit on this sample", type="primary", width="stretch",
                  on_click=run_sample, args=(file, False))
        left = live_runs_left()
        c3.button("Re-run live", width="stretch", disabled=not llm_ok or left <= 0,
                  on_click=run_sample, args=(file, True),
                  help="Ignore the cache and call Nemotron on Nebius Token Factory now.")
        notes = ["Cached result from a live Nemotron run - loads instantly." if cached else
                 "Not cached yet - the first run calls Nemotron live and caches the report."]
        if not llm_ok:
            notes.append("Live AI runs need NEBIUS_API_KEY.")
        elif left > 0:
            notes.append(f"{left} of {LIVE_RUNS_PER_SESSION} live runs left in this session.")
        else:
            notes.append(f"You have used all {LIVE_RUNS_PER_SESSION} live runs for this session - thanks for trying "
                         f"it! Cached results stay available.")
        st.caption(" ".join(notes))


def scorecard_view():
    def render():
        md(f'<p class="muted">The cached report of every sample (live Nemotron runs, model '
           f'<code class="tok">{esc(samples.model_name())}</code>, rulebook {esc(samples.rulebook_version())}) '
           f'scored against the answer key with the same code as <code class="tok">python tools/evaluate.py</code>. '
           f'Alerts are decided by deterministic code; the AI only explains them.</p>')
        answer_key_view.scorecard({f: samples.cached_report(f) for f in samples.sample_files()})
    judge_page("All samples scorecard", render)


def rulebook_view(rule=None):
    judge_page("Rulebook", lambda: rules.rulebook_page(samples.rulebook_version(), selected=rule))


def errorlab_view():
    judge_page("Error Lab - plant an error, see if we catch it",
               lambda: error_lab.page(open_rule, go_lab_report, live_runs_left, count_live_run, llm_available(),
                                      go_customrules=lambda: go("customrules")))


def customrules_view():
    judge_page("Custom rules - write your own and test it",
               lambda: custom_rules_page.page(lambda: go("errorlab", ws="errorlab")))


def go_lab_report(card, month):
    go("report", card=card, month=month, ws="errorlab")


def report_view(card, month, ws="upload"):
    ledger, vault = Ledger(), TokenVault()
    if month not in ledger.months(card):
        go("home")
        st.rerun()
    sample = ws == "sample"
    rep = vault.restore(pipeline.load_report(card, month))
    rec = ledger.load(card, month)
    months, later = ledger.months(card), [m for m in ledger.months(card) if m > month]
    name, number, ai_name = vault.restore(rec["cardholder"]["name"]), vault.restore(card), rec["cardholder"]["name"]
    llm_ok = llm_available()
    h = rep["headline"]

    if ws == "errorlab":
        st.button("← Back to the Error Lab", on_click=go, args=("errorlab",), kwargs={"ws": "errorlab"})
        md('<div class="note">This report is for a statement edited in the Error Lab - not a real or sample statement.</div>')
    else:
        st.button("← Home" if sample else "← All cards", on_click=go, args=("home",))

    with st.container(border=True):
        st.markdown(f"## Statement {month}")
        md(f'<div class="muted">{esc(name)} &middot; card {esc(number)}</div>'
           f'<div class="split"><div class="you"><span>You see</span>{esc(name)} &middot; {esc(number)}</div>'
           f'<div class="them"><span>The AI saw only</span><code class="tok">{esc(ai_name)}</code> &middot; '
           f'<code class="tok">{esc(card)}</code></div></div>')
        with st.container(horizontal=True, gap="small"):
            for m in months:
                st.button(m, key=f"tab_{card}_{m}", type="primary" if m == month else "secondary",
                          on_click=go, args=("report",), kwargs={"card": card, "month": m, "ws": ws})
        n = len(rep["alerts"])
        review = pill(f"{n} alert{'s' if n > 1 else ''}", "bad") if n else pill("All good", "ok")
        change = inr(h["change"]) if h.get("change") is not None else "First month"
        md(f'<div class="stats">'
           f'<div class="stat"><span class="k">Total due</span><b>{esc(inr(h["total_due"]))}</b></div>'
           f'<div class="stat"><span class="k">Minimum due</span><b>{esc(inr(h["minimum_due"]))}</b></div>'
           f'<div class="stat"><span class="k">Due date</span><b>{esc(h["due_date"])}</b></div>'
           f'<div class="stat"><span class="k">Change vs last month</span><b>{esc(change)}</b></div>'
           f'<div class="stat"><span class="k">Needs review</span><b>{review}</b></div></div>')
        history = (", ".join(rep["history_months"]) if rep["history_months"] else
                   "none - this is the first statement of this card, so month-to-month checks start from the next upload")
        md(f'<p class="muted" style="margin:6px 0 0">History used: {esc(history)}</p>')
        if sample:
            run = next((r for r in st.session_state.get("sample_runs", {}).values()
                        if r["card"] == card and r["month"] == month), None)
            if run and run["source"] == "cache":
                md(f'<div class="ai"><div class="label">{AI_SPARK}Sample</div>Cached result from a live Nemotron run on '
                   f'{esc((run["generated_at"] or "")[:10])}. Use <b>Re-run live</b> on the home page to call Nemotron now.</div>')
            elif run:
                md(f'<div class="ai"><div class="label">{AI_SPARK}Sample</div>Live run just now'
                   f'{" with Nemotron on Nebius Token Factory" if run["ai"] else " (without AI explanations)"}.</div>')
        if later and not sample:
            md(f'<div class="note">Later months ({esc(", ".join(later))}) were checked before this month was uploaded.</div>')
            c1, c2 = st.columns([1, 2])
            recheck_llm = c1.checkbox("with AI", value=llm_ok, disabled=not llm_ok, key=f"recheck_llm_{card}_{month}")
            if c2.button("Re-check later months", key=f"recheck_{card}_{month}"):
                with st.spinner("Working... with AI this takes about 30-60 seconds per alert."):
                    for m in later:
                        pipeline.reconcile(card, m, use_llm=recheck_llm and llm_ok)
                flash("Later months re-checked with the full history.", "success")
                st.rerun()

    if sample:
        tab_report, tab_key = st.tabs(["Report", "Compare with answer key"])
        with tab_report:
            report_body(rep, card, month, llm_ok, ws)
        with tab_key:
            if judge.unlocked():
                answer_key_view.render(rep)
                rules.rule_chips([r["rule_id"] for r in (answer_key_view.compare(rep) or {"rows": []})["rows"]],
                                 "answer_key", open_rule)
            else:
                judge.locked_notice()
    else:
        report_body(rep, card, month, llm_ok, ws)


def conclusion(rep):
    """Short conclusion: what went wrong, what to pay, what to address (figures from cc_audit.report.payment_advice)."""
    a = payment_advice(rep)
    with st.container(border=True, key="conclusion"):
        md('<div class="sec">Conclusion</div>')
        if not rep["alerts"]:
            md(f'<div class="fact"><div class="label">Calculated fact</div>Nothing went wrong on this statement. Pay '
               f'<b>{esc(inr(a["total_due"]))}</b> (or at least the minimum {esc(inr(a["minimum_due"]))}) by '
               f'<b>{esc(a["due_date"])}</b>.</div>')
            return
        amounts = {x["id"]: x["amount"] for x in a["overcharges"] + a["refunds_owed"]}
        wrong = "".join(f'<li>{esc(f["title"])}{" - " + esc(inr(amounts[f["id"]])) if f["id"] in amounts else ""}</li>'
                        for f in rep["alerts"])
        md(f'<div class="label">What went wrong</div><ul style="margin:4px 0 10px">{wrong}</ul>')
        md(f'<div class="stats">'
           f'<div class="stat"><span class="k">Total due on the statement</span><b>{esc(inr(a["total_due"]))}</b></div>'
           f'<div class="stat"><span class="k">Overcharged</span><b style="color:var(--alert)">{esc(inr(a["disputed_total"]))}</b></div>'
           f'<div class="stat"><span class="k">Refunds owed to you</span><b>{esc(inr(a["refunds_total"]))}</b></div>'
           f'<div class="stat"><span class="k">What you should actually owe</span><b style="color:var(--ok)">'
           f'{esc(inr(a["suggested_payment"]))}</b></div>'
           f'<div class="stat"><span class="k">Pay by</span><b>{esc(a["due_date"])}</b></div></div>')
        todo = ([f'<li><b>Dispute</b> {esc(x["title"])} ({esc(inr(x["amount"]))})</li>' for x in a["overcharges"]]
                + [f'<li><b>Ask for the refund</b>: {esc(x["title"])} ({esc(inr(x["amount"]))})</li>' for x in a["refunds_owed"]]
                + [f'<li><b>Check</b>: {esc(x["title"])}</li>' for x in a["to_verify"]])
        md(f'<div class="label">What to address</div><ul style="margin:4px 0 8px">{"".join(todo)}</ul>')
        st.caption(f"What you should owe = total due minus the overcharges and refunds above (GST charged on a wrong fee "
                   f"is refundable too and is not included). Safest: pay the full {inr(a['total_due'])} by {a['due_date']} "
                   f"and raise the disputes - if you pay less and the bank rejects a dispute, interest can apply on the "
                   f"unpaid part. Use 'Draft a dispute' on each alert below.")


def report_body(rep, card, month, llm_ok, ws):
    conclusion(rep)
    with st.container(border=True):
        md('<div class="sec">Summary</div>')
        if rep["comparison"].get("change_explained"):
            md(f'<div class="fact"><div class="label">Calculated fact</div>{esc(rep["comparison"]["change_explained"])}</div>')
        s = rep.get("ai_summary")
        if s:
            actions = "".join(f"<li>{esc(a)}</li>" for a in s.get("actions") or [])
            md(f'<div class="ai"><div class="label">{ai_label(s.get("source"), "AI explanation &middot; Nemotron", "Summary")}'
               f'</div>{esc(s.get("summary"))}{"<ul>" + actions + "</ul>" if actions else ""}</div>')
        elif not rep["alerts"]:
            st.write("Nothing on this statement needs review.")

    for i, f in enumerate(rep["alerts"]):
        alert_card(f, i, card, month, llm_ok, ws)

    if rep["checks_passed"]:
        with st.container(border=True):
            md(f'<div class="sec">Checked and correct {pill(str(len(rep["checks_passed"])) + " checks", "ok")}</div>')
            rows = [[esc(f["title"]), rules.label_html(f["rule_id"]), esc(show_value(f["expected"], f["unit"])),
                     esc(show_value(f["actual"], f["unit"])), pill("OK", "ok")] for f in rep["checks_passed"]]
            md(table(["Item", "Rule", "Expected", "On statement", ""], rows, num_cols=(2, 3)))
            rules.rule_chips([f["rule_id"] for f in rep["checks_passed"]], f"checked_{card}_{month}", open_rule)

    if rep["notes"]:
        with st.container(border=True):
            md('<div class="sec">Good to know <span class="muted">&middot; looks unusual, but allowed (not errors)</span></div>')
            items = "".join(f'<li style="margin:6px 0">{pill(f["rule_id"], "inf") if judge.unlocked() else ""} {esc(f["title"])}'
                            f'{" <span class=muted>- " + esc(f["calculation"]) + "</span>" if f.get("allowed_by") else ""}</li>'
                            for f in rep["notes"])
            md(f'<ul style="padding-left:0;list-style:none;margin:0">{items}</ul>')
            rules.rule_chips([f["rule_id"] for f in rep["notes"]], f"notes_{card}_{month}", open_rule)

    ask_box(card, month, llm_ok)

    with st.expander("Rule coverage and AI details"):
        evaluated = " ".join(rules.label_html(k) if judge.unlocked() else pill(rules.rule_name(k), "code")
                             for k, v in rep["coverage"].items() if v["status"] == "evaluated")
        skipped = " ".join(rules.label_html(k) if judge.unlocked() else pill(rules.rule_name(k), "code")
                           for k, v in rep["coverage"].items() if v["status"] != "evaluated")
        md(f'<p>Evaluated: {evaluated}</p><p>Not applicable this month: {skipped}</p>')
        if rep.get("llm"):
            md(f'<p class="muted">AI: {esc(rep["llm"]["provider"])} / {esc(rep["llm"]["model"])} - every payload is '
               f'in this session\'s audit_log.jsonl</p>')


def alert_card(f, i, card, month, llm_ok, ws):
    with st.container(border=True, key=f"alert_{i}"):
        c1, c2 = st.columns([6, 1.6], vertical_alignment="center")
        with c1:
            md(f'<div class="alert-head">{pill(str(f.get("severity", "")).capitalize(), "bad")}<span class="t">'
               f'{esc(f["title"])}</span></div>')
        with c2:
            rules.rule_chip(f["rule_id"], f"alert_{i}_{f['id']}", open_rule)
        diff = (f'<div class="neg"><span>Difference</span><b>{esc(inr(f["difference"]))}</b></div>'
                if f.get("difference") is not None else "")
        md(f'<div class="fact"><div class="label">Calculated fact</div><div class="facts3">'
           f'<div><span>Expected</span><b>{esc(show_value(f["expected"], f["unit"]))}</b></div>'
           f'<div><span>On statement</span><b>{esc(show_value(f["actual"], f["unit"]))}</b></div>{diff}</div></div>')
        if f.get("calculation"):
            with st.expander("How it was calculated"):
                calc = f["calculation"]
                if isinstance(calc, dict):
                    md(table(None, [[esc(k.replace("_", " ")), esc(v)] for k, v in calc.items()]))
                else:
                    st.write(calc)
        if f.get("evidence"):
            md('<div class="label" style="margin-top:12px">Evidence from your statements</div>')
            rows = []
            for e in f["evidence"]:
                if e.get("message"):
                    rows.append([f'Statement {esc(e.get("statement_month"))}: {esc(e["message"])}', "", "", ""])
                else:
                    rows.append([f'<code class="tok">{esc(e.get("txn_id"))}</code>', esc(e.get("date") or "-"),
                                 esc(e.get("description")), esc(f'{inr(e.get("amount"))} {e.get("type") or ""}')])
            md(table(["Line", "Date", "Description", "Amount"], rows, num_cols=(3,)))
        ai = f.get("ai")
        if ai:
            verdict = f' &middot; AI rule check: {esc(ai["verdict"])}' if ai.get("verdict") else ""
            md(f'<div class="ai"><div class="label">{ai_label(ai.get("source"), "AI explanation &middot; Nemotron", "Explanation")}'
               f'{verdict}</div>{esc(ai.get("explanation"))}</div>')
        help_text = None if llm_ok else "AI not reachable - a template draft will be used"
        st.button("Draft a dispute", key=f"dispute_{f['id']}", type="primary", help=help_text,
                  on_click=go, args=("dispute",), kwargs={"card": card, "month": month, "alert_id": f["id"], "ws": ws})


def ask_box(card, month, llm_ok):
    answers = st.session_state.setdefault("answers", {})
    with st.container(border=True):
        md('<div class="sec">Ask about this statement</div>')
        prev = answers.get((card, month))
        if prev:
            evidence = (f'<div class="muted" style="margin-top:6px">Based on: {esc(", ".join(map(str, prev["evidence"])))}</div>'
                        if prev.get("evidence") else "")
            md(f'<div class="ai"><div class="label">Q: {esc(prev["question"])}</div>{esc(prev["answer"])}{evidence}</div>')
        with st.form(f"ask_{card}_{month}", clear_on_submit=True, border=False):
            c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
            question = c1.text_input("Question", placeholder="e.g. Why is my bill higher this month?",
                                     disabled=not llm_ok, label_visibility="collapsed")
            asked = c2.form_submit_button("Ask", disabled=not llm_ok, width="stretch")
        if not llm_ok:
            st.caption("Set NEBIUS_API_KEY to ask questions.")
        if asked and question.strip():
            with st.spinner("Thinking..."):
                result = pipeline.ask(card, month, question.strip())
            answers[(card, month)] = {**result, "question": question.strip()}
            st.rerun()


def dispute_view(card, month, alert_id, ws="upload"):
    st.button(f"← Back to the {month} report", on_click=go, args=("report",),
              kwargs={"card": card, "month": month, "ws": ws})
    out = st.session_state.get("dispute")
    if not out or out.get("for") != (card, month, alert_id):    # draft once per alert, not on every rerun
        with st.spinner("Drafting the dispute... with AI this can take 30-60 seconds."):
            try:
                out = {**pipeline.dispute(card, month, alert_id), "for": (card, month, alert_id)}
            except SystemExit as e:                 # pipeline.dispute exits when the alert id is unknown
                st.error(str(e))
                return
        st.session_state.dispute = out
    c, d = out["case"], out["draft"]
    with st.container(border=True):
        st.markdown("## Dispute draft")
        md('<p class="muted">Review the draft, then send it to your bank through the channel below.</p>')
        diff = f' {pill("difference " + str(c["difference"]), "bad")}' if c.get("difference") is not None else ""
        rows = [
            ['<span class="muted">Item</span>', f'{esc(c["item"])} {rules.label_html(c["rule_id"])}'],
            ['<span class="muted">Expected / charged</span>', f'{esc(c["expected"])} / {esc(c["actual"])}{diff}'],
            ['<span class="muted">First appeared</span>', esc(c["first_appearance"])],
            ['<span class="muted">Dispute deadline</span>',
             f'<b>{esc(c["deadline"])}</b> {pill(str(c["days_left"]) + " days left", "inf")} <span class="muted">'
             f'({esc(c["dispute_window_days"])} days from {esc(c["counted_from"])})</span>'],
            ['<span class="muted">Where to raise it</span>', esc(c["channel"])],
        ]
        md(f'<div class="fact"><div class="label">Calculated facts</div>{table(None, rows)}</div>')
        label = ai_label(d.get("source"), "AI draft &middot; Nemotron", "Template draft")
        md(f'<div class="ai"><div class="label">{label} - review before sending</div>'
           f'<p style="margin:6px 0"><b>{esc(d.get("subject"))}</b></p>'
           f'<p style="white-space:pre-wrap;margin:0">{esc(d.get("body"))}</p></div>')
        st.caption("Copy text (use the copy icon at the top right of the box):")
        st.code(f"Subject: {d.get('subject', '')}\n\n{d.get('body', '')}", language=None, wrap_lines=True)


def main():
    load_api_key()
    md(CSS)
    md(HEADER)
    view = st.session_state.setdefault("view", {"name": "home"})
    ws = view.get("ws", "upload")
    with use_data_dir(WORKSPACES.get(ws, session_dir)()):
        show_flash()
        if view["name"] == "report":
            report_view(view["card"], view["month"], ws)
        elif view["name"] == "dispute":
            dispute_view(view["card"], view["month"], view["alert_id"], ws)
        elif view["name"] == "scorecard":
            scorecard_view()
        elif view["name"] == "rulebook":
            rulebook_view(view.get("rule"))
        elif view["name"] == "errorlab":
            errorlab_view()
        elif view["name"] == "customrules":
            customrules_view()
        else:
            home()
        judges_section()
        data_section()
    md(FOOTER)


main()
