#!/usr/bin/env python3
"""Streamlit web UI for the statement audit (hostable on Streamlit Community Cloud).

Run:  streamlit run streamlit_app.py

Every browser session gets its own data folder (ledger, reports, token map, audit log) in a temp
directory, so visitors of a hosted demo never see each other's statements. Uploaded PDFs are
written to a temp file inside that folder only for parsing and deleted right after.
The API key comes from st.secrets["NEBIUS_API_KEY"], else the NEBIUS_API_KEY environment variable.

Screens: home (landing) -> analyze (upload or sample) -> report (overview / issues / checked / ask /
answer key) -> dispute; "My cards" lists every uploaded month; "Judges" holds the passkey-protected pages.
"""
import json
import io
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from cc_audit import pipeline
from cc_audit.config import data_dir, rulebook, settings, use_data_dir
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
SUGGESTED_QUESTIONS = ["Why is my bill higher this month?", "What should I dispute?", "Which charges are fees?"]

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


def month_label(month):
    """'2026-09' -> 'Sep 2026'."""
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%b %Y")
    except (TypeError, ValueError):
        return str(month)


def sample_label(file, entries):
    """'•••• 2093 · Sep 2026' for a sample file name."""
    e = entries.get(file)
    last4 = samples.FILE_RE.match(file).group(1)
    return f"•••• {last4} · {month_label(e['month'])}" if e else file


def icon(path, size=20):
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">{path}</svg>')


ICONS = {
    "upload": '<path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3"/>',
    "mask": '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/>',
    "check": '<path d="M9 11l3 3 8-8"/><path d="M20 12v6a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h9"/>',
    "chat": '<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>',
    "calc": '<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M8 6h8M8 10h2M14 10h2M8 14h2M14 14h2M8 18h2M14 18h2"/>',
    "history": '<path d="M3 12a9 9 0 109-9 9.7 9.7 0 00-6.7 2.8L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
    "trash": '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/>',
    "book": '<path d="M4 19.5A2.5 2.5 0 016.5 17H20V3H6.5A2.5 2.5 0 004 5.5z"/><path d="M4 19.5V21h16"/>',
    "flask": '<path d="M9 3h6"/><path d="M10 3v6L4 19a1 1 0 00.9 1.5h14.2A1 1 0 0020 19l-6-10V3"/>',
    "pen": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/>',
    "shield": '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6l8-3z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
}


# ---------- look and feel ----------

CSS = """
<style>
:root{
 --header:#000B18; --primary:#1F3B73; --accent:#49A8C0; --accent-bg:#E8F4F8;
 --bg:#F3F1F2; --surface:#FFFFFF; --border:#E2DFE1; --text:#0E1A2B; --muted:#667085;
 --ok:#2E7D5B; --ok-bg:#E4F2EB; --alert:#B42335; --alert-bg:#FBE8EA;
 --warn:#9A6A1F; --warn-bg:#FBF1DE; --ai:#1F3B73; --ai-bg:#EAF5F8; --ai-line:#C9E5ED;
 --serif:"Playfair Display",Georgia,"Times New Roman",serif;
 --shadow:0 1px 2px rgba(14,26,43,.04),0 10px 30px rgba(14,26,43,.06); --radius:16px;
}
#MainMenu, footer, [data-testid="stMainMenu"], [data-testid="stAppDeployButton"], [data-testid="stDecoration"],
[data-testid="stStatusWidget"]{display:none !important; visibility:hidden}
[data-testid="stHeader"]{background:transparent; height:0}
.block-container{max-width:1160px; padding-top:1rem; padding-bottom:1rem}
.stApp, .stApp p, .stApp li, .stApp td, .stApp th, .stApp label, .stApp input, .stApp textarea, .stApp button{
 font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
h1, h2, h3{font-family:var(--serif) !important; color:var(--primary) !important}

/* ---- bordered containers = cards ---- */
[data-testid="stVerticalBlockBorderWrapper"], .stVerticalBlock[class*="st-key-"]{background:var(--surface)}
div[data-testid="stVerticalBlockBorderWrapper"], .stVerticalBlock[class*="st-key-"]{border-radius:var(--radius)}

/* ---- top navigation ---- */
.stVerticalBlock.st-key-topnav{background:var(--header) !important;border-radius:var(--radius);padding:10px 18px;
 border-bottom:3px solid var(--accent);margin-bottom:10px}
.brand{display:flex;align-items:center;gap:10px;color:#FBF8F2;font:600 21px/1.15 var(--serif);padding:2px 0}
.brand small{display:block;font:500 10px/1.3 Inter,sans-serif;color:#9FD3E1;letter-spacing:.14em;text-transform:uppercase;margin-top:1px}
.st-key-topnav [data-testid="stMarkdownContainer"]{margin-bottom:0}
.logo{width:34px;height:34px;border-radius:50%;border:1.5px solid var(--accent);display:grid;place-items:center;color:var(--accent)}
div[class*="st-key-nav_"] button{background:transparent !important;border:0 !important;color:#C9DCE6 !important;
 border-radius:999px;padding:4px 14px;min-height:0}
div[class*="st-key-nav_"] button p{font-weight:500;font-size:14px}
div[class*="st-key-nav_"] button:hover{color:#fff !important;background:rgba(255,255,255,.08) !important}
div[class*="st-key-nav_"][class*="_act"] button{color:#fff !important;background:rgba(73,168,192,.24) !important}
div.st-key-nav_cta button{background:var(--accent) !important;color:var(--header) !important;font-weight:600}
div.st-key-nav_cta button:hover{background:#6FBFD3 !important;color:var(--header) !important}

/* ---- landing ---- */
.eyebrow{display:inline-flex;align-items:center;gap:8px;background:var(--accent-bg);color:var(--primary);
 border:1px solid var(--ai-line);padding:6px 12px;border-radius:999px;font-size:12.5px;font-weight:600}
.hero{padding-top:22px}
.hero h1{font-size:50px;line-height:1.06;margin:18px 0 14px;letter-spacing:-.01em}
.hero h1 em{color:var(--accent);font-style:italic}
.lead{color:var(--muted);font-size:17px;line-height:1.5;max-width:30em;margin:0 0 6px}
.preview{background:var(--surface);border:1px solid var(--border);border-radius:22px;box-shadow:var(--shadow);padding:20px;
 margin-top:12px}
.pv-head{display:flex;justify-content:space-between;align-items:center;font-size:13px;color:var(--muted);font-weight:600}
.pv-big{margin:14px 0 12px}
.pv-big span{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:600}
.pv-big b{display:block;font:600 34px var(--serif);color:var(--alert)}
.pv-row{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:9px 0;border-top:1px solid var(--border);
 font-size:14px}
.pv-row .v{font-weight:600;font-variant-numeric:tabular-nums}
.pv-foot{margin-top:12px;background:var(--ok-bg);color:var(--ok);border-radius:12px;padding:10px 12px;font-size:13.5px;
 display:flex;justify-content:space-between;font-weight:600}
.section-title{text-align:center;margin:46px 0 18px}
.section-title h2{font-size:32px;margin:6px 0 0}
.kicker{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:700}
.flow{display:grid;grid-template-columns:repeat(4,1fr);gap:18px}
.flow .step{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px;position:relative;
 box-shadow:var(--shadow)}
.flow .step:not(:last-child)::after{content:"";position:absolute;right:-14px;top:50%;width:10px;height:10px;
 border-top:2px solid var(--accent);border-right:2px solid var(--accent);transform:translateY(-50%) rotate(45deg)}
.step .ic{width:42px;height:42px;border-radius:12px;background:var(--accent-bg);color:var(--primary);display:grid;place-items:center}
.step .n{display:block;font:700 11px Inter,sans-serif;color:var(--accent);letter-spacing:.12em;margin:14px 0 2px}
.step b{display:block;font:600 18px var(--serif);color:var(--primary)}
.step p{margin:4px 0 0;font-size:13.5px;color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.tile{display:flex;gap:12px;align-items:flex-start;background:var(--surface);border:1px solid var(--border);border-radius:14px;
 padding:14px}
.tile .ic{flex:none;width:36px;height:36px;border-radius:50%;background:var(--primary);color:#fff;display:grid;place-items:center}
.tile b{display:block;font-size:14.5px;color:var(--text)}
.tile span{font-size:13px;color:var(--muted)}
.stVerticalBlock.st-key-band{background:var(--header) !important;border-radius:22px;padding:34px 28px;margin-top:40px;
 text-align:center;align-items:center}
.st-key-band h2{color:#FBF8F2 !important;font-size:30px;margin:0}
.st-key-band p{color:#9FD3E1;margin:6px 0 4px}
.st-key-band button{background:var(--accent) !important;border-color:var(--accent) !important;color:var(--header) !important}
.st-key-band button:hover{background:#6FBFD3 !important}
.steps.mini{display:flex;gap:8px;flex-wrap:wrap;margin:4px 0 8px}
.steps.mini div{display:flex;align-items:center;gap:8px;background:var(--accent-bg);border:1px solid var(--ai-line);
 border-radius:999px;padding:5px 12px 5px 5px;font-size:13px;color:var(--primary);font-weight:500}
.steps.mini b{width:22px;height:22px;border-radius:50%;background:var(--primary);color:#fff;display:grid;place-items:center;font-size:12px}

/* ---- common bits ---- */
.muted{color:var(--muted);font-size:13.5px}
.sec{font:600 20px var(--serif);color:var(--primary);margin:0 0 8px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sec .ic{width:34px;height:34px;border-radius:10px;background:var(--accent-bg);color:var(--primary);display:grid;place-items:center}
.sec .muted{font-family:Inter,sans-serif;font-weight:500}
.pill{display:inline-flex;align-items:center;gap:4px;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;
 line-height:1.6;white-space:nowrap;font-family:Inter,sans-serif}
.bad{background:var(--alert-bg);color:var(--alert)} .ok{background:var(--ok-bg);color:var(--ok)}
.inf{background:var(--warn-bg);color:var(--warn)} .code{background:var(--bg);color:var(--muted);border:1px solid var(--border);font-weight:500}
.pill.rid{cursor:help;color:var(--primary);border-color:var(--ai-line);background:var(--accent-bg)}
.note{background:var(--warn-bg);border:1px solid #F0DDB8;border-radius:12px;padding:10px 12px;margin:8px 0;color:var(--warn);font-size:13.5px}
.scope{display:flex;gap:10px;align-items:flex-start}
.scope .ic{flex:none;margin-top:1px}
.fact{background:var(--bg);border:1px solid var(--border);border-radius:12px;padding:12px 14px;margin:10px 0;color:var(--text)}
.ai{background:var(--ai-bg);border:1px solid var(--ai-line);border-radius:12px;padding:12px 14px;margin:10px 0;color:var(--ai)}
.label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;color:var(--muted);margin-bottom:4px;
 display:flex;align-items:center;gap:6px}
.ai .label{color:var(--ai)}
.ai ul{margin:8px 0 0;padding-left:20px}
.banner{border-radius:12px;padding:12px 16px;font-weight:600;margin:10px 0;display:flex;gap:10px;align-items:center}
.banner.ok{background:var(--ok-bg);color:var(--ok)} .banner.bad{background:var(--alert-bg);color:var(--alert)}
.empty{text-align:center;padding:26px 10px;color:var(--muted)}
.empty .big{width:54px;height:54px;border-radius:50%;margin:0 auto 10px;display:grid;place-items:center;font-size:24px}
.empty b{display:block;color:var(--text);font-size:16px}

.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:12px 0 4px}
.kpi{background:var(--bg);border:1px solid var(--border);border-radius:14px;padding:12px 14px}
.kpi span{display:block;font-size:11.5px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.kpi b{display:block;font:600 22px var(--serif);margin-top:2px;color:var(--primary)}
.kpi.ok b{color:var(--ok)} .kpi.bad b{color:var(--alert)}
.kpi.ok{background:var(--ok-bg);border-color:#BFDECF} .kpi.bad{background:var(--alert-bg);border-color:#F1C4CA}

.rhead{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap}
.rhead h2{margin:0 !important;font-size:30px}
.privacy{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:13px}
.privacy .box{border-radius:10px;padding:7px 11px;border:1px solid var(--border);background:var(--bg)}
.privacy .box.ai-side{background:var(--ai-bg);border-color:var(--ai-line);color:var(--ai)}
.privacy .box span{display:block;font-size:10px;text-transform:uppercase;letter-spacing:.08em;font-weight:700;color:var(--muted)}
.privacy .box.ai-side span{color:var(--ai)}
.privacy .arrow{color:var(--accent);font-weight:700}
code.tok{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg);border:1px solid var(--border);
 padding:1px 5px;border-radius:5px;color:var(--text)}

/* overview visuals */
.ov{display:grid;grid-template-columns:200px 1fr;gap:24px;align-items:center}
.donut{width:150px;height:150px;border-radius:50%;margin:0 auto;display:grid;place-items:center;
 background:conic-gradient(var(--ok) 0 calc(var(--p) * 1%), var(--surface) 0 calc(var(--p) * 1% + .6%),
 var(--alert) 0 99.4%, var(--surface) 0)}
.donut.full{background:var(--ok)}
.donut > div{width:110px;height:110px;border-radius:50%;background:var(--surface);display:grid;place-content:center;text-align:center}
.donut b{font:600 26px var(--serif);color:var(--primary);line-height:1}
.donut span{font-size:11.5px;color:var(--muted);margin-top:4px}
.legend{display:flex;justify-content:center;gap:14px;margin-top:10px;font-size:12.5px;color:var(--text)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.bars{display:grid;gap:12px}
.bar .t{display:flex;justify-content:space-between;font-size:13.5px;margin-bottom:5px;color:var(--text)}
.bar .t b{font-variant-numeric:tabular-nums}
.bar .track{height:14px;border-radius:999px;background:var(--bg);overflow:hidden;display:flex;gap:2px}
.bar .fill{height:100%;border-radius:999px}
.wrong{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 14px}
.todo{list-style:none;padding:0 !important;margin:6px 0 0 !important;display:grid;gap:6px}
.todo li{margin:0 !important;display:flex;gap:10px;align-items:center;font-size:14px;background:var(--bg);border-radius:10px;padding:8px 12px}
.todo .tag{flex:none;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;border-radius:6px;padding:2px 8px}
.todo .amt{margin-left:auto;font-weight:600;font-variant-numeric:tabular-nums}

.facts3{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:10px 0}
.facts3 div{background:var(--bg);border:1px solid var(--border);border-radius:12px;padding:10px 12px}
.facts3 span{display:block;font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.facts3 b{font:600 19px var(--serif);color:var(--primary)}
.facts3 .neg{background:var(--alert-bg);border-color:#F1C4CA} .facts3 .neg b{color:var(--alert)}
.alert-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font:600 19px var(--serif);color:var(--primary)}
.alert-head .t{flex:1;min-width:200px}
.srow{display:flex;gap:10px;align-items:center;border:1px solid var(--border);border-radius:12px;padding:10px 12px;margin:6px 0;
 background:var(--surface);font-size:14px}
.srow .t{flex:1}

.table-wrap{border:1px solid var(--border);border-radius:12px;overflow:auto;margin:8px 0;background:#fff}
.table-wrap table{width:100%;border-collapse:collapse;font-size:14px;margin:0}
.table-wrap th,.table-wrap td{text-align:left;padding:8px 10px;border:0;border-bottom:1px solid var(--border);vertical-align:top}
.table-wrap th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em;background:var(--bg)}
.table-wrap tr:last-child td{border-bottom:0}
.table-wrap .num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}

/* card tiles and chips */
div[class*="st-key-alert_"]{border-left:4px solid var(--alert) !important}
div[class*="st-key-card_"]{border-top:3px solid var(--primary) !important}
.cc{background:linear-gradient(135deg,var(--primary),#0B1C3D);color:#fff;border-radius:14px;padding:16px;margin-bottom:10px;
 box-shadow:var(--shadow)}
.cc .n{font:600 18px var(--serif);letter-spacing:.08em}
.cc .w{font-size:12.5px;color:#9FD3E1;margin-top:14px;text-transform:uppercase;letter-spacing:.08em}
.cc .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}
.cc .chip{width:30px;height:22px;border-radius:5px;background:linear-gradient(135deg,#E9D8A6,#B8994B)}
div[class*="st-key-chip_"] button, div[class*="st-key-tab_"] button{padding:2px 12px;min-height:0;font-size:13px;border-radius:999px}
div[class*="st-key-chip_bad_"] button{color:var(--alert);border-color:#F1C4CA;background:var(--alert-bg)}
div[class*="st-key-chip_ok_"] button{color:var(--ok);border-color:#BFDECF;background:var(--ok-bg)}
div[class*="st-key-sub_"] button{border-radius:999px;padding:4px 16px;min-height:0}
div[class*="st-key-feat_"] button{border-radius:12px;justify-content:flex-start;padding:10px 14px}
div[class*="st-key-feat_"] button p{font-weight:600}
div[class*="st-key-ask_s_"] button{border-radius:999px;font-size:13px;padding:2px 12px;min-height:0;
 background:var(--accent-bg);border-color:var(--ai-line);color:var(--primary)}
div[class*="st-key-hub_"] button{min-height:44px;border-radius:12px}
div.st-key-conclusion{border-left:4px solid var(--primary) !important}

.app-footer{margin:40px 0 8px;padding:18px 20px;background:var(--header);border-radius:var(--radius);color:#C9DCE6;font-size:12.5px;
 display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center}
.app-footer b{color:#FBF8F2;font:600 16px var(--serif)}
@media (max-width:900px){
 .flow,.tiles{grid-template-columns:1fr 1fr} .flow .step::after{display:none} .hero h1{font-size:36px}
}
@media (max-width:640px){
 .flow,.tiles{grid-template-columns:1fr} .hero h1{font-size:30px} .ov{grid-template-columns:1fr} .brand{font-size:18px}
 .block-container{padding-left:1rem;padding-right:1rem}
}
</style>
"""

BRAND = f"""
<div class="brand"><span class="logo">{icon(ICONS["shield"], 18)}</span>
<span>MollyAI<small>Credit card statement audit</small></span></div>
"""

FOOTER = """
<div class="app-footer"><span><b>MollyAI</b> &nbsp;&middot;&nbsp; Find what's wrong in your card bill.</span>
<span>Math by code &middot; identity masked before any AI call &middot; data auto-deleted</span></div>
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
    """One live AI run used (sample 'Re-run live', uncached samples and Error Lab runs share the limit)."""
    st.session_state.live_runs = st.session_state.get("live_runs", 0) + 1


def run_sample(file, live):
    """Audit a sample with its card's earlier months, from the cache or live (parse -> mask -> rules -> AI)."""
    cached = not live and samples.cached_report(file) is not None
    use_llm = llm_available() and (cached or live_runs_left() > 0)
    if not cached and not use_llm:
        flash("Live AI runs are used up for this session (or no API key is set), so this sample was checked "
              "without AI explanations. Every calculation and alert is the same.")
    months = len(samples.history_files(file))
    msg = ("Loading the cached report..." if cached else
           f"Running the audit live on {months} month(s) of this card"
           + (" with AI explanations - about 30-60 seconds per alert..." if use_llm else "..."))
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
    for key in ("answers", "chats", "dispute", "confirm_reset", "sample_runs", "lab"):
        st.session_state.pop(key, None)
    go("home")
    flash("All local data (ledger, reports, token map, audit log) deleted.", "success")


# ---------- shared page parts ----------

NAV = [("home", "Home"), ("analyze", "Analyze"), ("cards", "My cards"), ("judges", "For judges")]
JUDGE_VIEWS = {"judges", "rulebook", "errorlab", "customrules"}


def nav(current, ws="upload"):
    active = current
    if current in ("report", "dispute"):
        active = {"sample": "analyze", "errorlab": "judges"}.get(ws, "cards")
    elif current in JUDGE_VIEWS:
        active = "judges"
    with st.container(key="topnav"):
        c1, c2 = st.columns([1, 2], vertical_alignment="center")
        c1.markdown(BRAND, unsafe_allow_html=True)
        with c2:
            with st.container(horizontal=True, horizontal_alignment="right", vertical_alignment="center", gap="small"):
                # Read the click from the return value, not on_click: after a long idle the server may start a
                # fresh session that has no callbacks yet, and an on_click button would do nothing on first click.
                target = None
                for name, label in NAV:
                    if st.button(label, key=f"nav_{name}_{'act' if name == active else 'idle'}", type="tertiary"):
                        target = name
                if st.button("Upload statement", key="nav_cta"):
                    target = "analyze"
    if target:
        go(target)
        st.rerun()


def set_confirm_reset(value):
    st.session_state.confirm_reset = value


def data_section():
    """Where the data lives, and the reset (with a confirmation step)."""
    with st.container(border=True, key="data_box"):
        if not st.session_state.get("confirm_reset"):
            c1, c2 = st.columns([5, 1.4], vertical_alignment="center")
            c1.caption(f"🔒 Private to this browser session · auto-deleted after {SESSION_MAX_AGE_HOURS} h of inactivity")
            c2.button("Reset all data", key="reset", width="stretch", icon=":material/delete:",
                      on_click=set_confirm_reset, args=(True,))
        else:
            c1, c2, c3 = st.columns([4, 1.1, 1], vertical_alignment="center")
            c1.markdown("**Delete all statements, reports, the token map and the audit log?**")
            c2.button("Yes, delete", key="reset_yes", type="primary", width="stretch", on_click=reset_data)
            c3.button("Cancel", key="reset_no", width="stretch", on_click=set_confirm_reset, args=(False,))


def sec(title, ic=None, extra=""):
    md(f'<div class="sec">{f"<span class=ic>{icon(ICONS[ic], 18)}</span>" if ic else ""}{esc(title)}'
       f'{" <span class=muted>" + extra + "</span>" if extra else ""}</div>')


# ---------- home (landing) ----------

def home():
    left, right = st.columns([1.15, 1], gap="large")
    with left:
        md(f'<div class="hero"><span class="eyebrow">{icon(ICONS["mask"], 13)} Privacy-first &middot; auto-deleted after '
           f'your session</span><h1>Find the money your card bill <em>hides</em>.</h1>'
           f'<p class="lead">Every fee, interest charge and EMI re-checked in seconds - explained in plain English.</p></div>')
        with st.container(horizontal=True, gap="small"):
            st.button("Analyze a statement", type="primary", key="hero_go", icon=":material/arrow_forward:",
                      on_click=go, args=("analyze",))
            st.button("Try a sample", key="hero_sample", on_click=go, args=("analyze",))
    with right:
        md('<div class="preview"><div class="pv-head"><span>Example report &middot; Sep 2026</span>'
           f'{pill("2 issues", "bad")}</div>'
           '<div class="pv-big"><span>Overcharged</span><b>Rs. 1,298.00</b></div>'
           f'<div class="pv-row"><span>Late payment fee</span><span class="v">{pill("Wrong", "bad")}</span></div>'
           f'<div class="pv-row"><span>GST on fees</span><span class="v">{pill("Wrong", "bad")}</span></div>'
           f'<div class="pv-row"><span>Interest rate</span><span class="v">{pill("OK", "ok")}</span></div>'
           f'<div class="pv-row"><span>EMI instalments</span><span class="v">{pill("OK", "ok")}</span></div>'
           '<div class="pv-foot"><span>You should actually pay</span><span>Rs. 22,119.84</span></div></div>')

    n_rules = len(rulebook().rules)
    md('<div class="section-title"><span class="kicker">How it works</span><h2>Four steps. Zero guesswork.</h2></div>')
    steps = [("upload", "Upload", "Your monthly statement PDF"),
             ("mask", "Mask", "Name and card become tokens"),
             ("calc", "Check", f"{n_rules} rules + your past months"),
             ("chat", "Act", "Plain reason + dispute draft")]
    md('<div class="flow">' + "".join(
        f'<div class="step"><span class="ic">{icon(ICONS[ic])}</span><span class="n">STEP {i:02d}</span><b>{t}</b>'
        f'<p>{d}</p></div>' for i, (ic, t, d) in enumerate(steps, 1)) + '</div>')

    md('<div class="section-title"><span class="kicker">Why trust it</span><h2>Built to be checked.</h2></div>')
    tiles = [("mask", "Identity masked", "The AI never sees who you are"),
             ("calc", "Math by code", "Every number is calculated, not guessed"),
             ("history", "Month vs month", "Duplicates, price hikes, missing refunds"),
             ("trash", "Auto-deleted", "Nothing is kept after your session")]
    md('<div class="tiles">' + "".join(f'<div class="tile"><span class="ic">{icon(ICONS[ic], 17)}</span><div><b>{t}</b>'
                                       f'<span>{d}</span></div></div>' for ic, t, d in tiles) + '</div>')

    with st.container(key="band", horizontal_alignment="center"):
        md("<h2>See what your bill is hiding.</h2><p>Takes less than a minute.</p>")
        st.button("Analyze a statement", type="primary", key="band_go", icon=":material/arrow_forward:",
                  on_click=go, args=("analyze",))


# ---------- analyze: upload or sample ----------

def analyze():
    llm_ok = llm_available()
    md('<div class="section-title" style="margin-top:18px"><span class="kicker">Analyze</span>'
       '<h2>Check a statement</h2></div>')
    left, right = st.columns(2, gap="large")
    with left:
        with st.container(border=True, height="stretch"):
            sec("Upload your statement", "upload")
            md(f'<div class="note scope"><span class="ic">{icon(ICONS["shield"], 16)}</span><span><b>Scope: dummy '
               f'statements only.</b> Every bank uses its own PDF layout, so for now MollyAI reads our dummy statement '
               f'format only. Its transactions and overall format are close to a real Indian credit card statement.'
               f'</span></div>')
            n = st.session_state.setdefault("upload_n", 0)
            uploaded = st.file_uploader("Statement PDF", type=["pdf"], key=f"upload_{n}", label_visibility="collapsed",
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
            use_llm = st.toggle("AI explanations", value=llm_ok, disabled=not llm_ok, key="use_llm",
                                help="AI explains each alert and summarises the month. All numbers come from code.")
            if st.button("Upload and check", type="primary", width="stretch", disabled=uploaded is None,
                         icon=":material/fact_check:"):
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
            st.caption("Upload one month at a time, oldest first - each month is compared with the ones before it.")
    with right:
        sample_section(llm_ok)
    data_section()


def sample_section(llm_ok):
    files, entries, runs = samples.sample_files(), samples.manifest(), st.session_state.get("sample_runs", {})
    with st.container(border=True, height="stretch"):
        sec("Try a sample", "check", "· no upload needed")
        featured = [f for f in FEATURED if f in entries]
        if featured:
            md('<div class="label">Start here - each one has problems to find</div>')
            for f in featured:
                st.button(f"Card {sample_label(f, entries)}", key=f"feat_{f}", width="stretch",
                          icon=":material/credit_card:", on_click=run_sample, args=(f, False))

        def label(f):
            name = sample_label(f, entries)
            e = entries.get(f)
            if f in runs and e:                    # the tag is only revealed once the report has been shown
                tag = answer_key_view.statement_status(e["statement_date"], e["total_due"])
                name += f"  ·  {tag}" if tag else ""
            return name

        md('<div class="label" style="margin-top:14px">Or pick any of 15 (3 cards &times; 5 months)</div>')
        file = st.selectbox("Sample statement", files, format_func=label, key="sample_file", label_visibility="collapsed")
        cached = samples.cached_report(file) is not None
        left = live_runs_left()
        c1, c2 = st.columns([1.6, 1])
        c1.button("Run audit", type="primary", width="stretch", icon=":material/play_arrow:",
                  on_click=run_sample, args=(file, False))
        c2.button("Re-run live", width="stretch", disabled=not llm_ok or left <= 0,
                  on_click=run_sample, args=(file, True), help="Ignore the cache and run the AI now.")
        notes = ["⚡ Cached - loads instantly" if cached else "Runs live the first time"]
        if llm_ok:
            notes.append(f"{max(left, 0)} of {LIVE_RUNS_PER_SESSION} live AI runs left")
        st.caption(" · ".join(notes))


# ---------- my cards ----------

def cards_view():
    md('<div class="section-title" style="margin-top:18px"><span class="kicker">Your statements</span>'
       '<h2>My cards</h2></div>')
    cards = cards_overview()
    if not cards:
        with st.container(border=True):
            md(f'<div class="empty"><div class="big" style="background:var(--accent-bg);color:var(--primary)">'
               f'{icon(ICONS["upload"], 24)}</div><b>No statements uploaded yet</b>Upload a statement or try a sample '
               f'to see it here.</div>')
            with st.container(horizontal=True, horizontal_alignment="center"):
                st.button("Analyze a statement", type="primary", key="cards_go", on_click=go, args=("analyze",))
    else:
        cols = st.columns(min(3, len(cards)))
        for i, c in enumerate(cards):
            total = sum(m["alerts"] or 0 for m in c["months"])
            with cols[i % len(cols)]:
                with st.container(border=True, key=f"card_{c['card']}"):
                    badge = pill(f"{total} to review", "bad") if total else pill("all good", "ok")
                    md(f'<div class="cc"><div class="top"><span class="chip"></span>{badge}</div>'
                       f'<div class="n">{esc(c["number"])}</div><div class="w">{esc(c["name"])}</div></div>')
                    with st.container(horizontal=True, gap="small"):
                        for m in c["months"]:
                            if m["alerts"]:
                                label, kind = f"{month_label(m['month'])} · {m['alerts']} ⚠", "bad"
                            elif m["alerts"] == 0:
                                label, kind = f"{month_label(m['month'])} · ✓", "ok"
                            else:
                                label, kind = month_label(m["month"]), "na"
                            st.button(label, key=f"chip_{kind}_{c['card']}_{m['month']}", on_click=go,
                                      args=("report",), kwargs={"card": c["card"], "month": m["month"]})
    data_section()


# ---------- judges ----------

def judge_submit():
    result = judge.try_unlock(st.session_state.get("judge_typed", ""))
    st.session_state.judge_typed = ""                       # never keep what was typed
    st.session_state.judge_msg = {"ok": None, "wrong": "Wrong passkey", "not_configured": "Judge access is not configured.",
                                  "wait": None}[result]


def judges_view():
    md('<div class="section-title" style="margin-top:18px"><span class="kicker">For judges</span>'
       '<h2>Test the engine yourself</h2></div>')
    if not judge.unlocked():
        _, mid, _ = st.columns([1, 1.4, 1])
        with mid, st.container(border=True):
            sec("Judge access", "mask")
            if judge.configured_passkey() is None:
                st.caption("Judge access is not configured.")
                return
            st.caption("The passkey is in our Devpost submission notes.")
            with st.form("judge_form", border=False):
                st.text_input("Judge passkey", type="password", key="judge_typed", label_visibility="collapsed",
                              placeholder="Judge passkey")
                wait = judge.seconds_to_wait()
                st.form_submit_button("Unlock", type="primary", on_click=judge_submit, disabled=bool(wait), width="stretch")
            if wait:
                st.caption(f"Too many wrong tries - please wait {wait} seconds.")
            elif st.session_state.get("judge_msg"):
                st.error(st.session_state.pop("judge_msg"))
        return
    hubs = [("book", "Rulebook", "Every rule the engine runs, with formulas", "rulebook", {}),
            ("flask", "Error Lab", "Plant an error, see if we catch it", "errorlab", {"ws": "errorlab"}),
            ("pen", "Custom rules", "Write your own rule and test it", "customrules", {})]
    cols = st.columns(3)
    for col, (ic, title, desc, view, kw) in zip(cols, hubs):
        with col, st.container(border=True):
            md(f'<div class="step" style="border:0;box-shadow:none;padding:4px"><span class="ic">{icon(ICONS[ic])}</span>'
               f'<b style="margin-top:12px">{title}</b><p>{desc}</p></div>')
            st.button(f"Open {title}", key=f"hub_{view}", type="primary", width="stretch", on_click=go, args=(view,),
                      kwargs=kw)
    md('<p class="muted" style="text-align:center;margin-top:14px">Sample reports also get an <b>Answer key</b> tab '
       '- every transaction compared with the planted errors.</p>')
    with st.container(horizontal=True, horizontal_alignment="center"):
        st.button("Lock judge pages", key="judge_lock", icon=":material/lock:", on_click=judge.lock)


def judge_page(title, render):
    st.button("← For judges", on_click=go, args=("judges",))
    with st.container(border=True):
        st.markdown(f"## {title}")
        if judge.unlocked():
            render()
        else:
            judge.locked_notice()


def rulebook_view(rule=None):
    judge_page("Rulebook", lambda: rules.rulebook_page(samples.rulebook_version(), selected=rule))


def errorlab_view():
    judge_page("Error Lab",
               lambda: error_lab.page(open_rule, go_lab_report, live_runs_left, count_live_run, llm_available(),
                                      go_customrules=lambda: go("customrules")))


def customrules_view():
    judge_page("Custom rules", lambda: custom_rules_page.page(lambda: go("errorlab", ws="errorlab")))


def go_lab_report(card, month):
    go("report", card=card, month=month, ws="errorlab")


# ---------- report ----------

def report_view(card, month, ws="upload", tab="overview"):
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
        st.button("← Analyze" if sample else "← My cards", on_click=go, args=("analyze" if sample else "cards",))

    n = len(rep["alerts"])
    with st.container(border=True):
        md(f'<div class="rhead"><div><div class="kicker">Statement</div><h2>{esc(month_label(month))}</h2></div>'
           f'<div class="privacy"><div class="box"><span>You see</span>{esc(name)} &middot; {esc(number)}</div>'
           f'<span class="arrow">&rarr;</span><div class="box ai-side"><span>{icon(ICONS["mask"], 10)} The AI sees only</span>'
           f'<code class="tok">{esc(ai_name)}</code> &middot; <code class="tok">{esc(card)}</code></div></div></div>')
        if len(months) > 1:
            with st.container(horizontal=True, gap="small"):
                for m in months:
                    st.button(month_label(m), key=f"tab_{card}_{m}", type="primary" if m == month else "secondary",
                              on_click=go, args=("report",), kwargs={"card": card, "month": m, "ws": ws, "tab": tab})
        change = h.get("change")
        change_txt = "First month" if change is None else ("▲ " if change > 0 else "▼ " if change < 0 else "") + inr(abs(change))
        md(f'<div class="kpis">'
           f'<div class="kpi"><span>Total due</span><b>{esc(inr(h["total_due"]))}</b></div>'
           f'<div class="kpi"><span>Minimum due</span><b>{esc(inr(h["minimum_due"]))}</b></div>'
           f'<div class="kpi"><span>Due date</span><b>{esc(h["due_date"])}</b></div>'
           f'<div class="kpi"><span>vs last month</span><b>{esc(change_txt)}</b></div>'
           f'<div class="kpi {"bad" if n else "ok"}"><span>Issues found</span><b>{n if n else "✓ None"}</b></div></div>')
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

    sections = [("overview", "Overview", ":material/dashboard:"), ("issues", f"Issues · {n}", ":material/error:"),
                ("checked", f"Checked OK · {len(rep['checks_passed'])}", ":material/task_alt:"),
                ("ask", "Ask AI", ":material/forum:")]
    if sample:
        sections.append(("key", "Answer key", ":material/fact_check:"))
    if tab not in {s[0] for s in sections}:
        tab = "overview"
    with st.container(horizontal=True, gap="small"):
        for key, label, ic in sections:
            st.button(label, key=f"sub_{key}", icon=ic, type="primary" if key == tab else "secondary",
                      on_click=go, args=("report",), kwargs={"card": card, "month": month, "ws": ws, "tab": key})

    if tab == "issues":
        issues(rep, card, month, llm_ok, ws)
    elif tab == "checked":
        checked(rep, card, month)
    elif tab == "ask":
        ask_box(card, month, llm_ok, ws)
    elif tab == "key":
        with st.container(border=True):
            sec("Compare with answer key", "check", "· every transaction vs the planted errors")
            if judge.unlocked():
                answer_key_view.render(rep, rec)
                rules.rule_chips([r["rule_id"] for r in (answer_key_view.compare(rep) or {"rows": []})["rows"]],
                                 "answer_key", open_rule)
            else:
                judge.locked_notice()
                st.button("Unlock on the judges page", key="key_unlock", on_click=go, args=("judges",))
    else:
        overview(rep, card, month, ws)


def overview(rep, card, month, ws):
    """Conclusion at a glance: checks donut, what you owe vs what you should owe, what to address
    (figures from cc_audit.report.payment_advice)."""
    a = payment_advice(rep)
    n_ok, n_bad = len(rep["checks_passed"]), len(rep["alerts"])
    total = n_ok + n_bad
    with st.container(border=True, key="conclusion"):
        sec("Conclusion")
        pct = 100 * n_ok / total if total else 100
        donut = (f'<div class="donut{" full" if not n_bad else ""}" style="--p:{pct:.1f}"><div><b>{n_ok}/{total}</b>'
                 f'<span>checks OK</span></div></div><div class="legend"><span><i style="background:var(--ok)"></i>'
                 f'Correct {n_ok}</span><span><i style="background:var(--alert)"></i>Issues {n_bad}</span></div>')
        if not rep["alerts"]:
            right = (f'<div class="banner ok">&#10003; Nothing went wrong on this statement.</div>'
                     f'<div class="bars"><div class="bar"><div class="t"><span>Pay by <b>{esc(a["due_date"])}</b></span>'
                     f'<b>{esc(inr(a["total_due"]))}</b></div><div class="track"><div class="fill" style="width:100%;'
                     f'background:var(--ok)"></div></div></div></div>'
                     f'<p class="muted" style="margin-top:8px">Or at least the minimum {esc(inr(a["minimum_due"]))}.</p>')
        else:
            owe = max(a["suggested_payment"], 0)
            share = 100 * owe / a["total_due"] if a["total_due"] else 100
            wrong = "".join(pill(f["title"], "bad") for f in rep["alerts"])
            right = (f'<div class="label">What went wrong</div><div class="wrong">{wrong}</div>'
                     f'<div class="bars">'
                     f'<div class="bar"><div class="t"><span>Total due on the statement</span><b>{esc(inr(a["total_due"]))}</b>'
                     f'</div><div class="track"><div class="fill" style="width:100%;background:var(--primary)"></div></div></div>'
                     f'<div class="bar"><div class="t"><span>What you should actually owe</span>'
                     f'<b style="color:var(--ok)">{esc(inr(a["suggested_payment"]))}</b></div><div class="track">'
                     f'<div class="fill" style="width:{share:.1f}%;background:var(--ok)"></div>'
                     f'<div class="fill" style="flex:1;background:var(--alert)"></div></div></div></div>'
                     f'<div class="kpis"><div class="kpi {"bad" if a["disputed_total"] else ""}"><span>Overcharged</span><b>{esc(inr(a["disputed_total"]))}</b></div>'
                     f'<div class="kpi"><span>Refunds owed</span><b>{esc(inr(a["refunds_total"]))}</b></div>'
                     f'<div class="kpi"><span>Pay by</span><b>{esc(a["due_date"])}</b></div></div>')
        md(f'<div class="ov"><div>{donut}</div><div>{right}</div></div>')

    if rep["alerts"]:
        with st.container(border=True):
            sec("What to do")
            todo = ([("Dispute", "bad", x["title"], inr(x["amount"])) for x in a["overcharges"]]
                    + [("Refund", "inf", x["title"], inr(x["amount"])) for x in a["refunds_owed"]]
                    + [("Check", "code", x["title"], "") for x in a["to_verify"]])
            md('<ul class="todo">' + "".join(f'<li><span class="tag {k}">{t}</span>{esc(title)}<span class="amt">'
                                             f'{esc(amt)}</span></li>' for t, k, title, amt in todo) + '</ul>')
            st.caption(f"Safest: pay the full {inr(a['total_due'])} and raise the disputes - unpaid disputed amounts can "
                       f"attract interest if a dispute is rejected.")
            st.button("Review issues and draft disputes", key="ov_issues", type="primary", icon=":material/arrow_forward:",
                      on_click=go, args=("report",), kwargs={"card": card, "month": month, "ws": ws, "tab": "issues"})

    s = rep.get("ai_summary")
    change = rep["comparison"].get("change_explained")
    if s or change:
        with st.container(border=True):
            sec("Summary", "chat")
            if s:
                actions = "".join(f"<li>{esc(x)}</li>" for x in s.get("actions") or [])
                md(f'<div class="ai"><div class="label">{ai_label(s.get("source"), "AI explanation", "Summary")}</div>'
                   f'{esc(s.get("summary"))}{"<ul>" + actions + "</ul>" if actions else ""}</div>')
            if change:
                md(f'<div class="fact"><div class="label">Why the total changed</div>{esc(change)}</div>')
            history = ", ".join(month_label(m) for m in rep["history_months"]) or "none (first statement of this card)"
            st.caption(f"Compared with: {history}")


def issues(rep, card, month, llm_ok, ws):
    if not rep["alerts"]:
        with st.container(border=True):
            md('<div class="empty"><div class="big ok">&#10003;</div><b>No issues on this statement</b>'
               'Every charge matched the rules and your history.</div>')
        return
    for i, f in enumerate(rep["alerts"]):
        alert_card(f, i, card, month, llm_ok, ws)


def checked(rep, card, month):
    with st.container(border=True):
        sec("Checked and correct", "check", pill(f'{len(rep["checks_passed"])} checks', "ok"))
        if rep["checks_passed"]:
            rows = [[esc(f["title"]), rules.label_html(f["rule_id"]), esc(show_value(f["expected"], f["unit"])),
                     esc(show_value(f["actual"], f["unit"])), pill("✓ OK", "ok")] for f in rep["checks_passed"]]
            md(table(["Item", "Rule", "Expected", "On statement", ""], rows, num_cols=(2, 3)))
            rules.rule_chips([f["rule_id"] for f in rep["checks_passed"]], f"checked_{card}_{month}", open_rule)
        else:
            st.caption("No amount checks applied to this statement.")
    if rep["notes"]:
        with st.container(border=True):
            sec("Good to know", None, "· looks unusual, but allowed")
            items = "".join(f'<div class="srow">{rules.id_pill(f["rule_id"]) if judge.unlocked() else pill("Allowed", "inf")}'
                            f'<span class="t">{esc(f["title"])}'
                            f'{"<br><span class=muted>" + esc(f["calculation"]) + "</span>" if f.get("allowed_by") else ""}'
                            f'</span></div>' for f in rep["notes"])
            md(items)
            rules.rule_chips([f["rule_id"] for f in rep["notes"]], f"notes_{card}_{month}", open_rule)


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
        md(f'<div class="facts3"><div><span>Expected</span><b>{esc(show_value(f["expected"], f["unit"]))}</b></div>'
           f'<div><span>On statement</span><b>{esc(show_value(f["actual"], f["unit"]))}</b></div>{diff}</div>')
        ai = f.get("ai")
        if ai:
            verdict = f' &middot; rule check: {esc(ai["verdict"])}' if ai.get("verdict") else ""
            md(f'<div class="ai"><div class="label">{ai_label(ai.get("source"), "AI explanation", "Explanation")}'
               f'{verdict}</div>{esc(ai.get("explanation"))}</div>')
        c1, c2 = st.columns(2)
        if f.get("calculation"):
            with c1.expander("How it was calculated", icon=":material/calculate:"):
                calc = f["calculation"]
                if isinstance(calc, dict):
                    md(table(None, [[esc(k.replace("_", " ")), esc(v)] for k, v in calc.items()]))
                else:
                    st.write(calc)
        if f.get("evidence"):
            with c2.expander(f"Evidence · {len(f['evidence'])} line{'s' if len(f['evidence']) > 1 else ''}",
                             icon=":material/receipt_long:"):
                rows = []
                for e in f["evidence"]:
                    if e.get("message"):
                        rows.append([f'Statement {esc(e.get("statement_month"))}: {esc(e["message"])}', "", ""])
                    else:
                        rows.append([esc(e.get("date") or "-"), esc(e.get("description")),
                                     esc(f'{inr(e.get("amount"))} {e.get("type") or ""}')])
                md(table(["Date", "Description", "Amount"], rows, num_cols=(2,)))
        help_text = None if llm_ok else "AI not reachable - a template draft will be used"
        st.button("Draft a dispute", key=f"dispute_{f['id']}", type="primary", help=help_text, icon=":material/edit_note:",
                  on_click=go, args=("dispute",), kwargs={"card": card, "month": month, "alert_id": f["id"], "ws": ws})


# ---------- ask (chat with history) ----------

def queue_question(question):
    st.session_state.chat_pending = question


def clear_chat(chat_key):
    st.session_state.setdefault("chats", {}).pop(chat_key, None)


def ask_box(card, month, llm_ok, ws):
    chat_key = (ws, card, month)
    history = st.session_state.setdefault("chats", {}).setdefault(chat_key, [])
    with st.container(border=True):
        c1, c2 = st.columns([5, 1], vertical_alignment="center")
        with c1:
            sec("Ask about this statement", "chat")
        if history:
            c2.button("Clear chat", key="ask_clear", icon=":material/delete_sweep:", width="stretch",
                      on_click=clear_chat, args=(chat_key,))
        if not history:
            md('<p class="muted">Answers use only this statement\'s facts - your name and card number stay masked.</p>')
        for turn in history:
            with st.chat_message("user"):
                md(esc(turn["question"]))
            with st.chat_message("assistant", avatar="🛡️"):
                md(esc(turn["answer"]))
                if turn.get("evidence"):
                    st.caption("Based on: " + ", ".join(map(str, turn["evidence"])))
        with st.container(horizontal=True, gap="small"):
            for i, q in enumerate(SUGGESTED_QUESTIONS):
                st.button(q, key=f"ask_s_{i}", disabled=not llm_ok, on_click=queue_question, args=(q,))
        typed = st.chat_input("Ask a question, e.g. Why is my bill higher this month?", key=f"chat_{ws}_{card}_{month}",
                              disabled=not llm_ok)
        if not llm_ok:
            st.caption("Set NEBIUS_API_KEY to ask questions.")
        question = (typed or st.session_state.pop("chat_pending", None) or "").strip()
        if question and llm_ok:
            with st.chat_message("user"):
                md(esc(question))
            with st.chat_message("assistant", avatar="🛡️"), st.spinner("Thinking..."):
                result = pipeline.ask(card, month, question)
            history.append({**result, "question": question})
            st.rerun()


# ---------- dispute ----------

def dispute_view(card, month, alert_id, ws="upload"):
    st.button(f"← Back to the {month_label(month)} report", on_click=go, args=("report",),
              kwargs={"card": card, "month": month, "ws": ws, "tab": "issues"})
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
        md(f'<div class="rhead"><div><div class="kicker">Dispute draft</div><h2>{esc(c["item"])}</h2></div>'
           f'{rules.label_html(c["rule_id"])}</div>')
        diff = (f'<div class="kpi bad"><span>Difference</span><b>{esc(inr(c["difference"]))}</b></div>'
                if c.get("difference") is not None else "")
        money = inr if c.get("difference") is not None else str     # date checks (e.g. due date) have no difference
        urgent = "bad" if isinstance(c["days_left"], int) and c["days_left"] < 15 else ""
        md(f'<div class="kpis">'
           f'<div class="kpi"><span>Expected</span><b>{esc(money(c["expected"]))}</b></div>'
           f'<div class="kpi"><span>Charged</span><b>{esc(money(c["actual"]))}</b></div>{diff}'
           f'<div class="kpi {urgent}"><span>Dispute by</span><b>{esc(c["deadline"])}</b></div>'
           f'<div class="kpi"><span>Days left</span><b>{esc(c["days_left"])}</b></div></div>')
        st.caption(f'{c["dispute_window_days"]} days from {c["counted_from"]} · first appeared {c["first_appearance"]}')
        md(f'<div class="fact"><div class="label">Where to raise it</div>{esc(c["channel"])}</div>')
        label = ai_label(d.get("source"), "AI draft", "Template draft")
        md(f'<div class="ai"><div class="label">{label} - review before sending</div>'
           f'<p style="margin:6px 0"><b>{esc(d.get("subject"))}</b></p>'
           f'<p style="white-space:pre-wrap;margin:0">{esc(d.get("body"))}</p></div>')
        st.caption("Copy (icon at the top right):")
        st.code(f"Subject: {d.get('subject', '')}\n\n{d.get('body', '')}", language=None, wrap_lines=True)


def main():
    load_api_key()
    md(CSS)
    view = st.session_state.setdefault("view", {"name": "home"})
    ws = view.get("ws", "upload")
    with use_data_dir(WORKSPACES.get(ws, session_dir)()):
        nav(view["name"], ws)
        show_flash()
        if view["name"] == "report":
            report_view(view["card"], view["month"], ws, view.get("tab", "overview"))
        elif view["name"] == "dispute":
            dispute_view(view["card"], view["month"], view["alert_id"], ws)
        elif view["name"] == "analyze":
            analyze()
        elif view["name"] == "cards":
            cards_view()
        elif view["name"] == "judges":
            judges_view()
        elif view["name"] == "rulebook":
            rulebook_view(view.get("rule"))
        elif view["name"] == "errorlab":
            errorlab_view()
        elif view["name"] == "customrules":
            customrules_view()
        else:
            home()
    md(FOOTER)


main()
