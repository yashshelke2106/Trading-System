"""
F&O Signal Terminal — Institutional-grade dashboard
Run: streamlit run streamlit_app.py
"""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

import config
from core import secrets as sec
from core.dashboard_data import (
    JOURNAL_FILE,
    TRADES_CSV,
    calc_chain_analytics,
    clear_cache,
    get_index_quotes,
    get_live_positions,
    get_option_chain,
    get_option_chain_view,
    get_signal_payload,
    get_intraday_spike_alerts,
    get_live_signal_tracking,
    get_signal_pnl_summary,
    get_trade_summary,
    get_volume_analytics,
    load_signal_journal_frame,
    load_trades_frame,
    market_elapsed_minutes,
    refresh_api,
    seconds_to_market_open,
)
from core.option_translator import get_option_rec
from core.rag_engine import build_default_rag_engine
from core.signal_writer import SIGNALS_PATH
from core.universe import FO_UNIVERSE

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MANUAL_CSV_COLS = [
    "trade_id", "timestamp", "symbol", "direction",
    "entry_price", "exit_price", "quantity", "pnl", "pnl_percent",
    "status", "exit_reason",
]

st.set_page_config(
    page_title="F&O Terminal",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Institutional CSS ─────────────────────────────────────────────────────────
_CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {
  --bg:       #060c18;
  --c1:       #0a1525;
  --c2:       #0d1b30;
  --c3:       #111f38;
  --bd:       #1a2d47;
  --bdh:      #264466;
  --bda:      #3a6ea8;
  --tx:       #c8d8e8;
  --txd:      #6b84a0;
  --txs:      #3a4f66;
  --g:        #00c896;
  --r:        #ff3d5e;
  --y:        #f59e0b;
  --b:        #38b2f0;
  --p:        #a78bfa;
  --atm-bg:   #091e3a;
  --atm-bd:   #1a5090;
}
*,*::before,*::after{box-sizing:border-box}

/* ── Shell ── */
.stApp{background:var(--bg)!important;font-family:'Inter',-apple-system,BlinkMacSystemFont,system-ui,sans-serif!important;color:var(--tx)!important}
.stApp>header{background:transparent!important;border-bottom:1px solid var(--bd)!important}
.main .block-container{padding:.5rem 1.75rem 2rem!important;max-width:1920px}

/* ── Sidebar ── */
[data-testid="stSidebar"]{background:#06101e!important;border-right:1px solid var(--bd)!important}
[data-testid="stSidebar"] .block-container{padding:.5rem .75rem!important}

/* ── Metrics ── */
[data-testid="metric-container"]{background:var(--c1)!important;border:1px solid var(--bd)!important;border-radius:8px!important;padding:10px 14px!important;transition:border-color .2s}
[data-testid="metric-container"]:hover{border-color:var(--bdh)!important}
[data-testid="stMetricLabel"]>div{color:var(--txd)!important;font-size:.63em!important;font-weight:600!important;text-transform:uppercase!important;letter-spacing:.1em!important}
[data-testid="stMetricValue"]>div{color:var(--tx)!important;font-size:1.4em!important;font-weight:700!important;font-family:'JetBrains Mono',monospace!important}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"]{background:var(--c1)!important;border-radius:8px 8px 0 0!important;border:1px solid var(--bd)!important;border-bottom:1px solid var(--bdh)!important;gap:0!important;padding:0 4px!important}
.stTabs [data-baseweb="tab"]{color:var(--txd)!important;font-size:.7em!important;font-weight:600!important;text-transform:uppercase!important;letter-spacing:.08em!important;padding:8px 12px!important;border-bottom:2px solid transparent!important;background:transparent!important;transition:color .15s!important}
.stTabs [data-baseweb="tab"]:hover{color:var(--tx)!important}
.stTabs [aria-selected="true"]{color:var(--b)!important;border-bottom:2px solid var(--b)!important}
[data-testid="stTabContent"]{background:var(--c1)!important;border:1px solid var(--bd)!important;border-top:none!important;border-radius:0 0 8px 8px!important;padding:14px!important}

/* ── DataFrames ── */
[data-testid="stDataFrame"]{border:1px solid var(--bd)!important;border-radius:8px!important;overflow:hidden!important}

/* ── Expander ── */
[data-testid="stExpander"]{border:1px solid var(--bd)!important;border-radius:8px!important;background:var(--c1)!important;margin-bottom:8px!important}
details[data-testid="stExpander"]>summary{font-size:.72em!important;font-weight:600!important;color:var(--txd)!important;text-transform:uppercase!important;letter-spacing:.08em!important;padding:9px 14px!important}

/* ── Buttons ── */
.stButton>button{border:1px solid var(--bdh)!important;background:var(--c2)!important;color:var(--tx)!important;border-radius:6px!important;font-size:.76em!important;font-weight:600!important;letter-spacing:.04em!important;padding:6px 14px!important;transition:all .15s!important}
.stButton>button:hover{border-color:var(--b)!important;color:var(--b)!important;background:var(--c3)!important}
.stButton>button[kind="primary"]{background:rgba(56,178,240,.08)!important;border-color:var(--b)!important;color:var(--b)!important}

/* ── Inputs ── */
.stTextInput input,.stTextArea textarea,.stNumberInput input{background:var(--c2)!important;border:1px solid var(--bdh)!important;color:var(--tx)!important;border-radius:6px!important;font-size:.82em!important}
.stTextInput input:focus,.stTextArea textarea:focus,.stNumberInput input:focus{border-color:var(--b)!important;box-shadow:0 0 0 2px rgba(56,178,240,.12)!important}
.stTextInput label,.stTextArea label,.stNumberInput label,.stSelectbox label,.stSlider label,.stCheckbox label{font-size:.68em!important;font-weight:600!important;text-transform:uppercase!important;letter-spacing:.07em!important;color:var(--txd)!important}

/* ── Alerts ── */
[data-testid="stAlert"]{border-radius:6px!important;font-size:.8em!important;padding:8px 12px!important}

/* ── Misc ── */
hr{border:none!important;border-top:1px solid var(--bd)!important;margin:12px 0!important}
[data-testid="stCaptionContainer"]{font-size:.68em!important;color:var(--txs)!important}
[data-testid="stArrowVegaLiteChart"]{background:var(--c1)!important;border:1px solid var(--bd)!important;border-radius:8px!important;padding:8px!important}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bdh);border-radius:2px}

/* ── Animations ── */
@keyframes pulse-g{0%,100%{box-shadow:0 0 0 0 rgba(0,200,150,.5)}50%{box-shadow:0 0 0 5px rgba(0,200,150,0)}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

/* ── Badges ── */
.bLive{display:inline-flex;align-items:center;gap:3px;background:rgba(0,200,150,.1);color:#00c896;border:1px solid rgba(0,200,150,.3);border-radius:4px;font-size:.62em;font-weight:700;letter-spacing:.08em;padding:1px 6px;text-transform:uppercase;vertical-align:middle}
.bLive::before{content:'';display:inline-block;width:5px;height:5px;border-radius:50%;background:#00c896;animation:pulse-g 2s infinite}
.bModel{display:inline-flex;align-items:center;background:rgba(245,158,11,.1);color:#f59e0b;border:1px solid rgba(245,158,11,.25);border-radius:4px;font-size:.62em;font-weight:700;letter-spacing:.08em;padding:1px 6px;text-transform:uppercase;vertical-align:middle}
.bStale{display:inline-flex;align-items:center;gap:3px;background:rgba(255,61,94,.1);color:#ff3d5e;border:1px solid rgba(255,61,94,.25);border-radius:4px;font-size:.62em;font-weight:700;letter-spacing:.08em;padding:1px 6px;text-transform:uppercase;vertical-align:middle;animation:blink 1.5s infinite}

/* ── Signal card ── */
.sCard{border:1px solid var(--bd);border-radius:10px;background:var(--c1);margin-bottom:14px;overflow:hidden;transition:border-color .2s,box-shadow .2s}
.sCard:hover{border-color:var(--bdh);box-shadow:0 4px 28px rgba(0,0,0,.5)}
.sHdr{display:flex;align-items:center;justify-content:space-between;padding:10px 14px 8px;border-bottom:1px solid var(--bd)}
.sSym{font-size:1.05em;font-weight:800;letter-spacing:.02em}
.sGrade{font-size:.68em;font-weight:700;text-transform:uppercase;letter-spacing:.08em;padding:2px 9px;border-radius:4px}
.sPrices{display:grid;grid-template-columns:repeat(4,1fr);padding:10px 14px;border-bottom:1px solid var(--bd);gap:0}
.sPCol{padding:0 12px 0 0}
.sPCol+.sPCol{padding-left:12px;border-left:1px solid var(--bd)}
.sLbl{font-size:.6em;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:var(--txd);margin-bottom:3px}
.sVal{font-size:.98em;font-weight:700;font-family:'JetBrains Mono',monospace}
.sMeta{padding:7px 14px;font-size:.73em;color:var(--txd);border-bottom:1px solid var(--bd)}
.sReason{padding:6px 14px 8px;font-size:.71em;color:var(--txs);line-height:1.45}
.sOpt{padding:9px 14px;background:var(--c2);border-top:1px solid var(--bd)}
.sOptHdr{display:flex;align-items:center;gap:6px;margin-bottom:8px;font-size:.76em;font-weight:600;flex-wrap:wrap}
.sOptGrid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}

/* ── Status bar ── */
.statBar{display:flex;align-items:stretch;flex-wrap:wrap;background:var(--c1);border:1px solid var(--bd);border-radius:8px;margin-bottom:14px;overflow:hidden}
.statItem{flex:1;min-width:90px;padding:8px 14px;border-right:1px solid var(--bd)}
.statItem:last-child{border-right:none}
.statLbl{font-size:.58em;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:var(--txs);margin-bottom:3px}
.statVal{font-size:.84em;font-weight:700;color:var(--tx);font-family:'JetBrains Mono',monospace}

/* ── Section header ── */
.secHdr{display:flex;align-items:center;gap:8px;margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid var(--bd)}
.secDot{width:3px;height:16px;border-radius:2px;flex-shrink:0}
.secTitle{font-size:.7em;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--txd)}

/* ── Option chain table ── */
.chainWrap{overflow:auto;border:1px solid var(--bd);border-radius:8px;max-height:500px;background:var(--c1)}
.chainTbl{width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:.8em}
.chainTbl th{background:var(--c2);color:var(--txd);font-size:.66em;font-weight:700;text-transform:uppercase;letter-spacing:.08em;padding:8px 10px;border-bottom:1px solid var(--bdh);position:sticky;top:0;z-index:1;white-space:nowrap}
.chainTbl td{padding:5px 10px;border-bottom:1px solid rgba(26,45,71,.6);color:var(--tx);white-space:nowrap}
.chainTbl tr:hover td{background:var(--c2)}
.chainTbl tr.atm td{background:var(--atm-bg)!important;border-top:1px solid var(--atm-bd);border-bottom:1px solid var(--atm-bd);font-weight:600}
.chainTbl tr.atm:hover td{background:#0c2444!important}
.cCE{color:#4cc9f0}.cPE{color:#ff9eb5}.cIVhi{color:#f59e0b}.cR{text-align:right}
.cStrike{font-weight:700;color:var(--tx);text-align:center!important}
.atmBadge{font-size:.58em;font-weight:700;text-transform:uppercase;letter-spacing:.06em;background:var(--atm-bd);color:#80c0ff;padding:1px 5px;border-radius:3px;margin-left:5px}

/* ── App title bar ── */
.appTitle{display:flex;align-items:center;justify-content:space-between;padding:10px 0 12px;border-bottom:1px solid var(--bd);margin-bottom:14px}
.appName{font-size:.95em;font-weight:800;text-transform:uppercase;letter-spacing:.12em;color:var(--tx)}
.appTag{font-size:.6em;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--b);border:1px solid var(--bda);padding:2px 7px;border-radius:4px;margin-left:8px}

/* ── Sidebar section label ── */
.sbSec{font-size:.62em;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:var(--txs);margin:14px 0 6px;padding-bottom:4px;border-bottom:1px solid var(--bd)}

/* ── Index ticker bar ── */
.ixBar{display:flex;align-items:center;background:var(--c2);border:1px solid var(--bd);border-radius:8px;margin-bottom:10px;overflow:hidden}
.ixItem{flex:1;min-width:130px;padding:8px 16px;border-right:1px solid var(--bd)}
.ixItem:last-child{border-right:none}
.ixLbl{font-size:.58em;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:var(--txs);margin-bottom:3px}
.ixVal{font-size:1.02em;font-weight:800;font-family:'JetBrains Mono',monospace;display:inline}
.ixChg{font-size:.72em;font-weight:600;font-family:'JetBrains Mono',monospace;margin-left:8px;vertical-align:middle}
.ixTs{font-size:.58em;color:var(--txs);margin-left:auto;padding:8px 14px;align-self:center}

/* ── PCR badge ── */
.pcrBar{display:flex;align-items:center;gap:12px;padding:8px 14px;background:var(--c2);border:1px solid var(--bd);border-radius:6px;margin-bottom:10px;flex-wrap:wrap}
.pcrItem{display:flex;flex-direction:column}
.pcrLbl{font-size:.58em;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--txs);margin-bottom:2px}
.pcrVal{font-size:.92em;font-weight:700;font-family:'JetBrains Mono',monospace}
</style>"""


def _inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def _reset_runtime_caches(refresh_api_client: bool = False) -> None:
    clear_cache()
    st.cache_data.clear()
    st.cache_resource.clear()
    if refresh_api_client:
        refresh_api()


def _api_url() -> str:
    base = config.DASHBOARD_API_URL.strip()
    return base if base else f"http://{config.DASHBOARD_API_HOST}:{config.DASHBOARD_API_PORT}"


def _fmt_ts(ts_str: str) -> str:
    try:
        return datetime.fromisoformat(ts_str).strftime("%d-%m %H:%M")
    except Exception:
        return ""


def _style_ratio(val):
    try:
        val = float(val)
    except Exception:
        return ""
    if val >= 2.0:
        return "background-color:#0e2818;color:#00c896;font-weight:bold"
    if val >= 1.5:
        return "background-color:#2a2206;color:#f59e0b"
    return ""


def _dir_style(value: str) -> str:
    v = str(value).upper()
    if v == "LONG":
        return "color:#00c896;font-weight:bold"
    if v == "SHORT":
        return "color:#ff3d5e;font-weight:bold"
    return ""


def _grade_style(value: str) -> str:
    return {
        "A": "color:#00c896;font-weight:bold",
        "B": "color:#f59e0b;font-weight:bold",
        "C": "color:#ff9800;font-weight:bold",
    }.get(str(value).upper(), "")


def _append_trade(row: Dict) -> None:
    os.makedirs(os.path.dirname(TRADES_CSV), exist_ok=True)
    write_header = not os.path.exists(TRADES_CSV) or os.path.getsize(TRADES_CSV) == 0
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANUAL_CSV_COLS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


@st.cache_resource(show_spinner=False)
def _rag():
    return build_default_rag_engine()


@st.cache_data(ttl=60, show_spinner=False)
def _cached_option_rec(
    symbol: str,
    direction: str,
    entry: float,
    sl: float,
    target: float,
) -> Optional[dict]:
    from core.dashboard_data import get_spot_prices
    chain = get_option_chain(symbol)
    spot_map = get_spot_prices([symbol])
    spot = float(spot_map.get(symbol.upper(), 0) or 0) or entry
    return get_option_rec(
        symbol, direction, spot, entry, sl, target,
        chain_data=chain if chain else None,
    )


def _option_rec_from_signal(sig: dict) -> Optional[dict]:
    """Reconstruct option_rec from signal dict's pre-enriched fields.

    signal_agent._persist_signal already populates option_strike/entry_prem/etc.
    at scan time. Use those instead of re-fetching the chain (which can fail
    when Dhan is unauthenticated or the stock has no F&O entry).
    """
    if not sig.get("option_strike") or not sig.get("entry_prem"):
        return None
    try:
        e_prem = float(sig["entry_prem"])
        t_prem = float(sig.get("target_prem", 0) or 0)
        s_prem = float(sig.get("sl_prem", 0) or 0)
        rr = abs(t_prem - e_prem) / abs(e_prem - s_prem) if abs(e_prem - s_prem) > 0 else 0
        expiry_str = str(sig.get("option_expiry", ""))
        # Try to compute days-to-expiry from yyyy-mm-dd string
        expiry_days = 0
        try:
            from datetime import date as _date, datetime as _dt
            exp_d = _dt.strptime(expiry_str.split("T")[0], "%Y-%m-%d").date()
            expiry_days = (exp_d - _date.today()).days
        except Exception:
            pass
        return {
            "option_type":  sig.get("option_type", "CE"),
            "strike":       float(sig["option_strike"]),
            "expiry_str":   expiry_str,
            "expiry_days":  expiry_days,
            "iv_pct":       float(sig.get("iv_pct", 0) or 0),
            "delta":        float(sig.get("delta", 0) or 0),
            "entry_prem":   round(e_prem, 2),
            "target_prem":  round(t_prem, 2),
            "sl_prem":      round(s_prem, 2),
            "rr":           round(rr, 2),
            "source":       sig.get("prem_source", "signal_enriched"),
        }
    except Exception:
        return None


def _section_header(title: str, color: str = "#38b2f0") -> None:
    st.markdown(
        f'<div class="secHdr"><div class="secDot" style="background:{color}"></div>'
        f'<div class="secTitle">{title}</div></div>',
        unsafe_allow_html=True,
    )


def _fmt_inr(v: float, sign: bool = False) -> str:
    """Compact Indian rupee: ₹-11.2K instead of ₹-11,232 so metric cards don't overflow."""
    prefix = f"{'+'if v >= 0 else ''}" if sign else ""
    av = abs(v)
    if av >= 100_000:
        return f"₹{prefix}{v/100_000:.1f}L"
    if av >= 10_000:
        return f"₹{prefix}{v/1_000:.1f}K"
    return f"₹{prefix}{v:,.0f}"


def _render_option_chain_html(chain_rows: List[Dict], atm_strike: Optional[float]) -> str:
    def _fmt_ltp(v: float) -> str:
        return f"₹{v:.2f}" if v > 0 else "—"

    def _fmt_oi(v: int) -> str:
        if v >= 1_000_000:
            return f"{v/1_000_000:.2f}M"
        if v >= 1_000:
            return f"{v/1_000:.1f}K"
        return str(v) if v > 0 else "—"

    def _fmt_iv(v: float) -> str:
        return f"{v:.1f}%" if v > 0 else "—"

    rows_html = ""
    for row in chain_rows:
        strike = float(row["strike"])
        is_atm = atm_strike is not None and abs(strike - atm_strike) < 0.5
        ce_ltp = float(row.get("ce_ltp", 0) or 0)
        ce_oi  = int(row.get("ce_oi", 0) or 0)
        ce_iv  = float(row.get("ce_iv", 0) or 0)
        pe_ltp = float(row.get("pe_ltp", 0) or 0)
        pe_oi  = int(row.get("pe_oi", 0) or 0)
        pe_iv  = float(row.get("pe_iv", 0) or 0)

        ce_iv_cls = "cIVhi" if ce_iv > 40 else "cCE"
        pe_iv_cls = "cIVhi" if pe_iv > 40 else "cPE"
        atm_cls  = "atm" if is_atm else ""
        atm_badge = '<span class="atmBadge">ATM</span>' if is_atm else ""

        rows_html += (
            f'<tr class="{atm_cls}">'
            f'<td class="cCE cR">{_fmt_ltp(ce_ltp)}</td>'
            f'<td class="cCE cR" style="font-size:.9em">{_fmt_oi(ce_oi)}</td>'
            f'<td class="{ce_iv_cls} cR">{_fmt_iv(ce_iv)}</td>'
            f'<td class="cStrike">{strike:.0f}{atm_badge}</td>'
            f'<td class="{pe_iv_cls}">{_fmt_iv(pe_iv)}</td>'
            f'<td class="cPE" style="font-size:.9em">{_fmt_oi(pe_oi)}</td>'
            f'<td class="cPE">{_fmt_ltp(pe_ltp)}</td>'
            f'</tr>'
        )

    return (
        '<div class="chainWrap">'
        '<table class="chainTbl">'
        '<thead><tr>'
        '<th class="cR" style="color:#4cc9f0">CE LTP</th>'
        '<th class="cR" style="color:#4cc9f0">OI</th>'
        '<th class="cR" style="color:#4cc9f0">IV%</th>'
        '<th style="text-align:center">STRIKE</th>'
        '<th style="color:#ff9eb5">IV%</th>'
        '<th style="color:#ff9eb5">OI</th>'
        '<th style="color:#ff9eb5">PE LTP</th>'
        '</tr></thead>'
        f'<tbody>{rows_html}</tbody>'
        '</table></div>'
    )


def _render_signal_card(signal: Dict, option_rec: Optional[dict] = None) -> None:
    symbol    = signal.get("symbol", "?")
    direction = str(signal.get("direction", "")).upper()
    grade     = signal.get("confluence_grade", "?")
    score     = signal.get("confluence_score", 0)
    entry     = float(signal.get("entry_price", 0) or 0)
    stop      = float(signal.get("sl_price", 0) or 0)
    target    = float(signal.get("target_price", 0) or 0)
    rr        = float(signal.get("rr_ratio", 0) or 0)
    reason    = str(signal.get("reason", "") or "")
    patterns  = ", ".join((signal.get("patterns_combined") or [])[:4])
    delivered = _fmt_ts(signal.get("ts", ""))

    is_long   = direction == "LONG"
    clr       = "#00c896" if is_long else "#ff3d5e"
    dir_bg    = "rgba(0,200,150,.06)" if is_long else "rgba(255,61,94,.06)"
    arrow     = "▲" if is_long else "▼"

    _grade = {
        "A": ("rgba(0,200,150,.1)",  "#00c896", "rgba(0,200,150,.3)"),
        "B": ("rgba(245,158,11,.1)", "#f59e0b", "rgba(245,158,11,.25)"),
        "C": ("rgba(255,152,0,.1)",  "#ff9800", "rgba(255,152,0,.25)"),
    }.get(grade, ("rgba(80,80,80,.1)", "#888", "rgba(80,80,80,.2)"))
    gbg, gclr, gbd = _grade

    sl_pct  = abs(entry - stop)   / entry * 100 if entry > 0 else 0
    tgt_pct = abs(target - entry) / entry * 100 if entry > 0 else 0

    ts_html = (
        f'<span style="font-size:.66em;color:#3a4f66">{delivered}</span>'
        if delivered else ""
    )

    opt_html = ""
    if option_rec:
        is_live  = option_rec.get("source") != "theoretical"
        opt_clr  = "#4cc9f0" if option_rec.get("option_type") == "CE" else "#ff9eb5"
        src_badge = '<span class="bLive">LIVE</span>' if is_live else '<span class="bModel">BSM</span>'
        ltp_lbl  = "LTP" if is_live else "BSM EST"
        e_prem   = option_rec["entry_prem"]
        t_prem   = option_rec["target_prem"]
        s_prem   = option_rec["sl_prem"]
        sl_pp    = round((e_prem - s_prem) / e_prem * 100, 1) if e_prem > 0 else 0
        tg_pp    = round((t_prem - e_prem) / e_prem * 100, 1) if e_prem > 0 else 0

        opt_html = f"""
<div class="sOpt">
  <div class="sOptHdr">
    <span style="color:{opt_clr};font-weight:800">{symbol}&nbsp;{option_rec['strike']:.0f}&nbsp;{option_rec['option_type']}</span>
    <span style="color:#3a4f66">·</span>
    <span style="color:#6b84a0">{option_rec['expiry_str']} ({option_rec['expiry_days']}d)</span>
    <span style="color:#3a4f66">·</span>
    <span style="color:#6b84a0">IV&nbsp;{option_rec['iv_pct']:.1f}%</span>
    <span style="color:#3a4f66">·</span>
    <span style="color:#6b84a0">Δ&nbsp;{option_rec['delta']:+.3f}</span>
    {src_badge}
  </div>
  <div class="sOptGrid">
    <div><div class="sLbl">{ltp_lbl}</div><div class="sVal" style="color:{opt_clr}">₹{e_prem}</div></div>
    <div><div class="sLbl">Target (+{tg_pp}%)</div><div class="sVal" style="color:#00c896">₹{t_prem}</div></div>
    <div><div class="sLbl">Stop (−{sl_pp}%)</div><div class="sVal" style="color:#ff3d5e">₹{s_prem}</div></div>
    <div><div class="sLbl">R:R</div><div class="sVal">1:{option_rec['rr']:.1f}</div></div>
  </div>
</div>"""

    meta_html = (
        f'<div class="sMeta">{patterns}</div>' if patterns else ""
    )
    reason_html = (
        f'<div class="sReason">{reason[:180]}</div>' if reason else ""
    )

    st.markdown(f"""
<div class="sCard" style="border-left:3px solid {clr}">
  <div class="sHdr" style="background:{dir_bg}">
    <div style="display:flex;align-items:center;gap:8px">
      <span style="color:{clr};font-size:.88em">{arrow}</span>
      <span class="sSym" style="color:{clr}">{symbol}</span>
      <span style="font-size:.7em;font-weight:600;color:{clr};opacity:.75;text-transform:uppercase;letter-spacing:.1em">{direction}</span>
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      {ts_html}
      <span class="sGrade" style="background:{gbg};color:{gclr};border:1px solid {gbd}">Grade {grade}&nbsp;·&nbsp;{score}</span>
    </div>
  </div>
  <div class="sPrices">
    <div class="sPCol">
      <div class="sLbl">Entry</div>
      <div class="sVal">₹{entry:,.2f}</div>
    </div>
    <div class="sPCol">
      <div class="sLbl">Stop (−{sl_pct:.1f}%)</div>
      <div class="sVal" style="color:#ff3d5e">₹{stop:,.2f}</div>
    </div>
    <div class="sPCol">
      <div class="sLbl">Target (+{tgt_pct:.1f}%)</div>
      <div class="sVal" style="color:#00c896">₹{target:,.2f}</div>
    </div>
    <div class="sPCol" style="border-left:1px solid var(--bd);padding-left:12px">
      <div class="sLbl">R:R</div>
      <div class="sVal">1:{rr:.1f}</div>
    </div>
  </div>
  {meta_html}{reason_html}{opt_html}
</div>""", unsafe_allow_html=True)


def _fmt_oi_short(v: int) -> str:
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v/1_000:.1f}K"
    return str(v) if v > 0 else "—"


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        '<div style="display:flex;align-items:center;gap:8px;padding:4px 0 12px">'
        '<span style="font-size:.88em;font-weight:800;text-transform:uppercase;'
        'letter-spacing:.1em;color:#c8d8e8">F&O Terminal</span>'
        '<span style="font-size:.58em;font-weight:700;text-transform:uppercase;'
        'color:#38b2f0;border:1px solid #264466;padding:1px 6px;border-radius:4px">NSE</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown('<div class="sbSec">Trading API</div>', unsafe_allow_html=True)
    current_client_id = sec.get_client_id()
    sidebar_trade_health = sec.token_health()

    with st.form("form_client_id", clear_on_submit=False):
        client_id_input = st.text_input("Client ID", value=current_client_id)
        save_client = st.form_submit_button("Save Client ID", use_container_width=True)
    if save_client:
        try:
            sec.save_client_id(client_id_input.strip())
            _reset_runtime_caches(refresh_api_client=True)
            st.success("Client ID saved.")
            st.rerun()
        except Exception as exc:
            st.error(f"Save failed: {exc}")

    if sidebar_trade_health.valid and not sidebar_trade_health.needs_refresh:
        st.success(f"Token healthy · {sidebar_trade_health.message}")
    elif sidebar_trade_health.valid:
        st.warning(f"Refresh soon · {sidebar_trade_health.message}")
    else:
        st.error(f"Token issue · {sidebar_trade_health.message}")

    with st.expander("Update Trading Token", expanded=sidebar_trade_health.needs_refresh or not sidebar_trade_health.valid):
        with st.form("form_trade_token", clear_on_submit=True):
            token_input = st.text_area("Paste JWT Access Token", height=100, placeholder="eyJ...")
            c1, c2 = st.columns(2)
            save_token  = c1.form_submit_button("Save Token",  use_container_width=True, type="primary")
            clear_token = c2.form_submit_button("Clear Token", use_container_width=True)
        if save_token:
            if not token_input.strip():
                st.error("Token is empty.")
            elif not token_input.strip().startswith("eyJ"):
                st.error("Expected JWT (starts with 'eyJ').")
            else:
                try:
                    sec.save_access_token(token_input.strip())
                    _reset_runtime_caches(refresh_api_client=True)
                    st.success("Token saved.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Save failed: {exc}")
        if clear_token:
            sec.clear_access_token()
            _reset_runtime_caches(refresh_api_client=True)
            st.success("Token cleared.")
            st.rerun()

    st.markdown('<div class="sbSec">Data API</div>', unsafe_allow_html=True)
    data_health = sec.data_token_health()
    if data_health.valid:
        st.success(f"Data API ready · {data_health.message}")
    else:
        st.warning("Data API not set — yfinance fallback active.")

    with st.expander("Set Data API Credentials", expanded=not data_health.valid):
        with st.form("form_data_creds", clear_on_submit=False):
            api_key_input    = st.text_input("API Key",    value=sec.get_data_api_key(),    type="password")
            api_secret_input = st.text_input("API Secret", value=sec.get_data_api_secret(), type="password")
            dc1, dc2 = st.columns(2)
            save_data_creds  = dc1.form_submit_button("Save",  use_container_width=True, type="primary")
            clear_data_creds = dc2.form_submit_button("Clear", use_container_width=True)
        if save_data_creds:
            try:
                if not api_key_input.strip():
                    raise ValueError("API key is required")
                sec.save_data_api_key(api_key_input.strip())
                if api_secret_input.strip():
                    sec.save_data_api_secret(api_secret_input.strip())
                _reset_runtime_caches(refresh_api_client=True)
                st.success("Data API credentials saved.")
                st.rerun()
            except Exception as exc:
                st.error(f"Save failed: {exc}")
        if clear_data_creds:
            sec.clear_data_credentials()
            _reset_runtime_caches(refresh_api_client=True)
            st.success("Credentials cleared.")
            st.rerun()

    with st.expander("Credential Diagnostics", expanded=False):
        live_token  = sec.get_access_token()
        live_key    = sec.get_data_api_key()
        live_secret = sec.get_data_api_secret()
        st.text(f"Trading token : {live_token[:16] + '…' if live_token else 'NOT SET'}")
        st.text(f"Token health  : {sec.token_health(live_token).message}")
        st.text(f"Data API key  : {live_key[:8] + '…' if live_key else 'NOT SET'}")
        st.text(f"Data secret   : {'SET' if live_secret else 'NOT SET'}")
        if st.button("Rebuild API sessions", use_container_width=True):
            _reset_runtime_caches(refresh_api_client=True)
            st.success("Caches cleared and sessions rebuilt.")
            st.rerun()

    st.markdown('<div class="sbSec">Dashboard</div>', unsafe_allow_html=True)
    pause_refresh = st.checkbox("Pause auto-refresh", value=False)
    refresh_sec   = st.slider("Refresh interval (s)", 10, 120, 30, disabled=pause_refresh)
    top_n         = st.slider("Option chain top symbols", 3, 15, 5)
    top_n_signals = st.slider("Max signals shown (by confidence)", 5, 20, 10)
    show_spikes      = st.checkbox("Volume Spike Alerts", value=True)
    show_volume      = st.checkbox("Volume Analytics",    value=True)
    show_chain       = st.checkbox("Option Chain",        value=True)
    show_trades      = st.checkbox("Trade P&L",           value=True)
    show_accuracy    = st.checkbox("Signal Accuracy",     value=True)
    show_intelligence = st.checkbox("RAG Intelligence",   value=True)
    show_learning    = st.checkbox("Adaptive Learning",   value=True)

    st.markdown('<div class="sbSec">FastAPI Sidecar</div>', unsafe_allow_html=True)
    st.caption(f"`{_api_url()}`")
    st.code("python dashboard_api.py", language="bash")
    st.caption(f"Signals: `{SIGNALS_PATH}`")


# ── Fragment intervals ────────────────────────────────────────────────────────
live_every   = None if pause_refresh else refresh_sec
medium_every = None if pause_refresh else max(refresh_sec, 20)
slow_every   = None if pause_refresh else max(refresh_sec, 45)

# ── Title + CSS injection ─────────────────────────────────────────────────────
_inject_css()
st.markdown(
    '<div class="appTitle">'
    '<div><span class="appName">F&O Signal Terminal</span>'
    '<span class="appTag">NSE · India</span></div>'
    '</div>',
    unsafe_allow_html=True,
)


# ── Fragments ─────────────────────────────────────────────────────────────────

@st.fragment(run_every=medium_every)
def render_index_fragment() -> None:
    quotes = get_index_quotes()
    if not quotes:
        return
    _LABELS = {"NIFTY50": "Nifty 50", "BANKNIFTY": "Bank Nifty", "INDIAVIX": "India VIX"}
    items = []
    for key, label in _LABELS.items():
        q = quotes.get(key, {})
        ltp = q.get("ltp", 0)
        chg = q.get("chg", 0)
        pct = q.get("pct", 0)
        if ltp <= 0:
            continue
        is_vix = key == "INDIAVIX"
        # VIX rising = fear up = amber; VIX falling = calm = green
        if is_vix:
            clr = "#f59e0b" if chg >= 0 else "#00c896"
        else:
            clr = "#00c896" if chg >= 0 else "#ff3d5e"
        arrow = "▼" if chg < 0 else "▲"
        items.append(
            f'<div class="ixItem">'
            f'<div class="ixLbl">{label}</div>'
            f'<div><span class="ixVal" style="color:{clr}">{ltp:,.2f}</span>'
            f'<span class="ixChg" style="color:{clr}">{arrow} {abs(chg):,.2f} ({abs(pct):.2f}%)</span>'
            f'</div></div>'
        )
    if items:
        ts = datetime.now().strftime("%H:%M:%S")
        st.markdown(
            f'<div class="ixBar">{"".join(items)}'
            f'<div class="ixTs">Updated {ts}</div></div>',
            unsafe_allow_html=True,
        )


@st.fragment(run_every=live_every)
def render_header_fragment() -> None:
    now     = datetime.now()
    elapsed = market_elapsed_minutes(now)
    wait_open = seconds_to_market_open(now)
    trade_health     = sec.token_health()
    data_health_live = sec.data_token_health()

    if now.weekday() >= 5:
        mkt_status, mkt_clr = "WEEKEND", "#3a4f66"
        mkt_detail = "Market closed"
    elif elapsed <= 0 and now.hour * 60 + now.minute < 9 * 60 + 15:
        mkt_status, mkt_clr = "PRE-MKT", "#f59e0b"
        mkt_detail = f"Opens in {wait_open // 3600}h {(wait_open % 3600) // 60}m"
    elif now.hour > 15 or (now.hour == 15 and now.minute >= 30):
        mkt_status, mkt_clr = "CLOSED", "#3a4f66"
        mkt_detail = f"Next open {wait_open // 3600}h {(wait_open % 3600) // 60}m"
    else:
        mkt_status, mkt_clr = "LIVE", "#00c896"
        mkt_detail = f"{elapsed // 60}h {elapsed % 60}m elapsed"

    is_live = mkt_status == "LIVE"
    dot_anim = "animation:pulse-g 2s infinite" if is_live else ""
    dot_html = (
        f'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;'
        f'background:{mkt_clr};margin-right:5px;vertical-align:middle;{dot_anim}"></span>'
    )

    tok_ok  = trade_health.valid and not trade_health.needs_refresh
    tok_clr = "#00c896" if tok_ok else ("#f59e0b" if trade_health.valid else "#ff3d5e")
    tok_icon = "✓" if tok_ok else ("⚠" if trade_health.valid else "✗")
    hrs_str  = f"{trade_health.hours_left:.0f}h left" if trade_health.hours_left is not None else "—"
    tok_val  = f'<span style="color:{tok_clr}">{tok_icon} {hrs_str}</span>'

    data_clr  = "#00c896" if data_health_live.valid else "#f59e0b"
    data_icon = "✓" if data_health_live.valid else "○"
    data_lbl  = "Active" if data_health_live.valid else "Fallback"
    data_val  = f'<span style="color:{data_clr}">{data_icon} {data_lbl}</span>'

    rf_val = "Paused" if pause_refresh else f"{refresh_sec}s"

    st.markdown(f"""
<div class="statBar">
  <div class="statItem" style="min-width:110px">
    <div class="statLbl">Market</div>
    <div class="statVal">{dot_html}<span style="color:{mkt_clr}">{mkt_status}</span></div>
  </div>
  <div class="statItem">
    <div class="statLbl">IST Time</div>
    <div class="statVal">{now.strftime('%H:%M:%S')}</div>
  </div>
  <div class="statItem" style="min-width:140px">
    <div class="statLbl">Session</div>
    <div class="statVal" style="color:#6b84a0;font-size:.78em">{mkt_detail}</div>
  </div>
  <div class="statItem">
    <div class="statLbl">Universe</div>
    <div class="statVal">{len(FO_UNIVERSE)}</div>
  </div>
  <div class="statItem" style="margin-left:auto">
    <div class="statLbl">Trading API</div>
    <div class="statVal">{tok_val}</div>
  </div>
  <div class="statItem">
    <div class="statLbl">Data API</div>
    <div class="statVal">{data_val}</div>
  </div>
  <div class="statItem">
    <div class="statLbl">Refresh</div>
    <div class="statVal" style="font-size:.78em;color:#6b84a0">{rf_val}</div>
  </div>
</div>""", unsafe_allow_html=True)

    if trade_health.hours_left is not None and trade_health.hours_left <= 6:
        if trade_health.hours_left <= 0:
            st.error("Trading token expired. Refresh from sidebar before placing live orders.")
        else:
            st.warning(f"Trading token expires in {trade_health.hours_left:.1f}h.")
    if not data_health_live.valid:
        st.info("Data API inactive. Some views fall back to slower or limited sources.")


@st.fragment(run_every=live_every)
def render_signals_fragment(top_n_signals: int = 10) -> None:
    _section_header("Live Trade Signals", "#00c896")
    payload = get_signal_payload()

    if not payload:
        st.info(
            "Scanner not writing live signals. Start the Aladdin runner:\n\n"
            "```bash\npython aladdin_runner.py --force\n```\n\n"
            "Or the standalone scanner:\n"
            "```bash\npython scan_only_v2.py\n```"
        )
        st.divider()
        return

    all_signals = payload.get("signals", []) or []
    by_grade  = payload.get("by_grade", {})
    snap_ts   = payload.get("ts", "")
    meta      = payload.get("meta", {})

    # Sort by confidence descending, keep top N
    all_signals_sorted = sorted(
        all_signals,
        key=lambda s: float(s.get("confluence_score", 0) or 0),
        reverse=True,
    )
    signals = all_signals_sorted[:top_n_signals]
    total_count = len(all_signals_sorted)

    age_str = "—"
    stale   = True
    try:
        age_sec = (datetime.now() - datetime.fromisoformat(snap_ts)).total_seconds()
        age_str = f"{age_sec:.0f}s ago"
        stale   = age_sec > 120  # scanner writes every 30s; 120s = 4 missed cycles
    except Exception:
        pass

    badge = '<span class="bStale">STALE</span>' if stale else '<span class="bLive">LIVE</span>'
    grade_html = (
        f"<span style='color:#00c896;font-weight:600'>A: {by_grade.get('A', 0)}</span>&nbsp;·&nbsp;"
        f"<span style='color:#f59e0b;font-weight:600'>B: {by_grade.get('B', 0)}</span>&nbsp;·&nbsp;"
        f"<span style='color:#ff9800;font-weight:600'>C: {by_grade.get('C', 0)}</span>"
    )

    c1, c2, c3, c4 = st.columns([3, 2, 1, 2])
    c1.markdown(
        f"{badge}&nbsp;&nbsp;<span style='color:#6b84a0;font-size:.82em'>"
        f"Last scan {age_str} · {meta.get('elapsed_sec','?')}s · {meta.get('universe_size','?')} symbols</span>",
        unsafe_allow_html=True,
    )
    c2.markdown(grade_html, unsafe_allow_html=True)
    c3.metric("Total", total_count)
    if total_count > top_n_signals:
        c4.markdown(
            f"<span style='background:#1a3a5c;color:#00c896;padding:3px 8px;"
            f"border-radius:4px;font-size:.8em;font-weight:600'>"
            f"Showing top {len(signals)} of {total_count} · highest confidence</span>",
            unsafe_allow_html=True,
        )

    if not signals:
        # Show diagnostics when no signals to help the user understand why
        cooldown = payload.get("_cooldown", {})
        st.info("No signals above current threshold.")
        with st.expander("Signal Diagnostics", expanded=True):
            d1, d2, d3 = st.columns(3)
            d1.metric("Last Scan", age_str)
            d2.metric("Symbols in Cooldown", len(cooldown))
            d3.metric("Source", meta.get("source", "scan_only_v2"))
            if cooldown:
                cd_syms = list(cooldown.keys())[:10]
                st.caption(f"Cooldown symbols (next 10): {', '.join(cd_syms)}")
            st.caption(
                "**No signals?** Possible reasons:\n"
                "- Market is range-bound (no volume surges)\n"
                "- Signal thresholds are strict (min_strength, min_votes)\n"
                "- All detected symbols are in cooldown\n"
                "- Scanner not running (`python aladdin_runner.py --force`)"
            )
        st.divider()
        return

    # Recount grades for filtered set
    filtered_by_grade = {"S": 0, "A": 0, "B": 0, "C": 0}
    for s in signals:
        g = s.get("confluence_grade", "")
        if g in filtered_by_grade:
            filtered_by_grade[g] += 1

    tab_s, tab_a, tab_b, tab_c, tab_table = st.tabs([
        f"Grade S  ({filtered_by_grade['S']})  [~75-80% WR]",
        f"Grade A  ({filtered_by_grade['A']})",
        f"Grade B  ({filtered_by_grade['B']})",
        f"Grade C  ({filtered_by_grade['C']})",
        "Table View",
    ])

    with tab_s:
        grade_s_sigs = [s for s in signals if s.get("confluence_grade") == "S"]
        if not grade_s_sigs:
            st.caption("No Grade S signals this scan.")
        else:
            st.success(f"Grade S = Ultra-high conviction | A-tier symbol + 15m confirms + score>=95 | T1 target (~75-80% est. WR)")
            cols = st.columns(min(2, len(grade_s_sigs)))
            for idx, sig in enumerate(grade_s_sigs):
                with cols[idx % len(cols)]:
                    option_rec = _option_rec_from_signal(sig) or _cached_option_rec(
                        sig.get("symbol", ""),
                        sig.get("direction", "long"),
                        float(sig.get("entry_price", 0) or 0),
                        float(sig.get("sl_price", 0) or 0),
                        float(sig.get("target_price", 0) or 0),
                    )
                    _render_signal_card(sig, option_rec)

    for tab, grade in zip((tab_a, tab_b, tab_c), ("A", "B", "C")):
        with tab:
            grade_signals = [s for s in signals if s.get("confluence_grade") == grade]
            if not grade_signals:
                st.caption(f"No Grade {grade} signals.")
                continue
            cols = st.columns(min(2, len(grade_signals)))
            for idx, sig in enumerate(grade_signals):
                with cols[idx % len(cols)]:
                    option_rec = _option_rec_from_signal(sig) or _cached_option_rec(
                        sig.get("symbol", ""),
                        sig.get("direction", "long"),
                        float(sig.get("entry_price", 0) or 0),
                        float(sig.get("sl_price", 0) or 0),
                        float(sig.get("target_price", 0) or 0),
                    )
                    _render_signal_card(sig, option_rec)

    with tab_table:
        rows = []
        for sig in signals:
            entry  = float(sig.get("entry_price", 0) or 0)
            stop   = float(sig.get("sl_price", 0) or 0)
            target = float(sig.get("target_price", 0) or 0)
            opt = _option_rec_from_signal(sig) or _cached_option_rec(
                sig.get("symbol", ""), sig.get("direction", "long"), entry, stop, target)
            opt_txt = ""
            if opt:
                opt_txt = (
                    f"{opt['strike']:.0f} {opt['option_type']} @₹{opt['entry_prem']} "
                    f"→ ₹{opt['target_prem']} / SL ₹{opt['sl_prem']}"
                )
            rows.append({
                "Time":    _fmt_ts(sig.get("ts", "")),
                "Symbol":  sig.get("symbol"),
                "Dir":     str(sig.get("direction", "")).upper(),
                "Grade":   sig.get("confluence_grade"),
                "Score":   sig.get("confluence_score"),
                "Entry ₹": round(entry, 2),
                "SL ₹":    round(stop, 2),
                "Target ₹":round(target, 2),
                "R:R":     sig.get("rr_ratio", 0),
                "Option":  opt_txt,
                "Reason":  str(sig.get("reason", ""))[:70],
            })
        df = pd.DataFrame(rows)
        st.dataframe(
            df.style
              .map(_dir_style,   subset=["Dir"])
              .map(_grade_style, subset=["Grade"]),
            use_container_width=True, hide_index=True, height=420,
        )
    st.divider()


@st.fragment(run_every=medium_every)
def render_positions_fragment() -> None:
    with st.expander("Dhan Live Positions — Unrealized P&L", expanded=True):
        positions = get_live_positions()
        if not positions:
            st.info("No open positions reported by Dhan.")
            return

        total_unreal = 0.0
        rows = []
        for pos in positions:
            qty    = float(pos.get("netQty",        pos.get("quantity",     0)) or 0)
            avg_px = float(pos.get("avgCostPrice",  pos.get("averagePrice", 0)) or 0)
            ltp    = float(pos.get("lastTradedPrice", pos.get("ltp",        0)) or 0)
            sym    = pos.get("tradingSymbol", pos.get("symbol", "?"))
            unreal = (ltp - avg_px) * qty if ltp and avg_px else 0.0
            total_unreal += unreal
            rows.append({"Symbol": sym, "Qty": int(qty),
                         "Avg ₹": round(avg_px, 2), "LTP ₹": round(ltp, 2),
                         "Unrealized": round(unreal, 2)})

        p1, p2 = st.columns(2)
        p1.metric("Open Positions",     len(rows))
        p2.metric("Total Unrealized P&L", f"₹{total_unreal:+,.0f}")

        def _pnl_clr(v):
            try:
                v = float(v)
                return "color:#00c896;font-weight:bold" if v > 0 else ("color:#ff3d5e;font-weight:bold" if v < 0 else "")
            except Exception:
                return ""

        st.dataframe(
            pd.DataFrame(rows).style.map(_pnl_clr, subset=["Unrealized"]),
            use_container_width=True, hide_index=True,
        )
    st.divider()


@st.fragment(run_every=medium_every)
def render_volume_fragment() -> None:
    if not show_volume:
        return

    _section_header("Volume Analytics", "#38b2f0")
    analytics = get_volume_analytics(FO_UNIVERSE)

    errors = analytics.get("errors", {})
    if errors:
        with st.expander(f"{len(errors)} symbols failed to load", expanded=False):
            st.dataframe(
                pd.DataFrame([{"Symbol": k, "Error": v} for k, v in errors.items()]),
                use_container_width=True, hide_index=True,
            )

    rows_1h = analytics.get("rows_1h", [])
    rows_5m = analytics.get("rows_5m", [])
    if not rows_1h and not rows_5m:
        st.warning("No intraday data available.")
        st.divider()
        return

    left, right = st.columns(2)
    with left:
        st.markdown(
            '<div style="font-size:.72em;font-weight:600;text-transform:uppercase;'
            'letter-spacing:.08em;color:#6b84a0;margin-bottom:8px">Last 1 Hour — vs expected pace</div>',
            unsafe_allow_html=True,
        )
        df_1h = pd.DataFrame(rows_1h)
        if not df_1h.empty:
            df_1h = df_1h.rename(columns={
                "symbol": "Symbol", "price": "Price ₹", "vol_1h": "Vol (1h)",
                "expected_1h": "Expected", "ratio": "Ratio", "flag": "",
            })
            st.dataframe(
                df_1h.style.map(_style_ratio, subset=["Ratio"]),
                use_container_width=True, hide_index=True,
                height=min(520, 50 + len(df_1h) * 35),
            )

    with right:
        st.markdown(
            '<div style="font-size:.72em;font-weight:600;text-transform:uppercase;'
            'letter-spacing:.08em;color:#6b84a0;margin-bottom:8px">Last 5 Minutes — vs prev bar</div>',
            unsafe_allow_html=True,
        )
        df_5m = pd.DataFrame(rows_5m)
        if not df_5m.empty:
            df_5m = df_5m.rename(columns={
                "symbol": "Symbol", "price": "Price ₹", "vol_last5": "Last 5m",
                "vol_prev5": "Prev 5m", "ratio": "Ratio", "flag": "",
            })
            st.dataframe(
                df_5m.style.map(_style_ratio, subset=["Ratio"]),
                use_container_width=True, hide_index=True,
                height=min(520, 50 + len(df_5m) * 35),
            )
    st.divider()


@st.fragment(run_every=medium_every)
def render_option_chain_fragment() -> None:
    if not show_chain:
        return

    _section_header("Option Chain", "#a78bfa")

    payload = get_signal_payload()
    signal_symbols = [
        sig.get("symbol") for sig in (payload.get("signals") or []) if sig.get("symbol")
    ]
    top_choices = signal_symbols[:top_n] if signal_symbols else FO_UNIVERSE[:top_n]
    current_symbol = st.session_state.get("chain_symbol")
    if current_symbol and current_symbol not in top_choices:
        top_choices = [current_symbol] + top_choices
    top_choices = list(dict.fromkeys(top_choices))

    col_sel, col_search = st.columns([2, 1])
    with col_sel:
        selected_symbol = st.selectbox(
            "Symbol", top_choices, key="chain_symbol",
            help="Defaults to strongest live signals.",
        )
    with col_search:
        with st.expander("Full universe search"):
            manual_symbol = st.selectbox("Pick symbol", sorted(FO_UNIVERSE), key="chain_manual")
            if st.button("Load", key="chain_manual_apply"):
                st.session_state["chain_symbol"] = manual_symbol
                st.rerun()

    view       = get_option_chain_view(selected_symbol)
    chain_rows = view.get("rows", [])
    atm        = view.get("atm")
    spot       = float(view.get("spot", 0) or 0)
    expiry_shown = str(view.get("expiry", "") or "")

    if not chain_rows:
        st.warning(
            f"No option chain data for {selected_symbol}. "
            "Verify Dhan trading token is valid and symbol is F&O-enabled."
        )
        st.divider()
        return

    if expiry_shown:
        st.caption(f"Expiry: **{expiry_shown}**")

    # Analytics from full chain (PCR + Max Pain)
    full_chain = get_option_chain(selected_symbol)
    analytics  = calc_chain_analytics(full_chain) if full_chain else {}
    pcr        = analytics.get("pcr", 0.0)
    max_pain   = analytics.get("max_pain")
    tot_ce_oi  = analytics.get("total_ce_oi", 0)
    tot_pe_oi  = analytics.get("total_pe_oi", 0)

    pcr_clr = (
        "#00c896" if pcr > 1.2 else
        ("#ff3d5e" if pcr < 0.8 else "#f59e0b")
    )
    pcr_bias = (
        "Bullish bias" if pcr > 1.2 else
        ("Bearish bias" if pcr < 0.8 else "Neutral")
    )
    max_pain_str = f"₹{max_pain:,.0f}" if max_pain else "—"
    mp_vs_spot = ""
    if max_pain and spot > 0:
        diff = max_pain - spot
        mp_vs_spot = f" ({diff:+.0f} from spot)"

    pcr_items = [
        ("PCR",       f'<span style="color:{pcr_clr};font-weight:800">{pcr:.2f}</span> <span style="color:{pcr_clr};font-size:.75em">{pcr_bias}</span>'),
        ("Max Pain",  f'<span style="font-weight:700">{max_pain_str}</span><span style="font-size:.8em;color:#6b84a0">{mp_vs_spot}</span>'),
        ("Total CE OI", f'<span style="color:#4cc9f0;font-weight:700">{_fmt_oi_short(tot_ce_oi)}</span>'),
        ("Total PE OI", f'<span style="color:#ff9eb5;font-weight:700">{_fmt_oi_short(tot_pe_oi)}</span>'),
    ]
    pcr_html = "".join(
        f'<div class="pcrItem"><div class="pcrLbl">{lbl}</div><div class="pcrVal">{val}</div></div>'
        for lbl, val in pcr_items
    )
    st.markdown(f'<div class="pcrBar">{pcr_html}</div>', unsafe_allow_html=True)

    if atm:
        a1, a2, a3, a4 = st.columns(4)
        a1.metric("ATM Strike",       f"₹{atm['strike']:,.0f}")
        a2.metric("Spot (Underlying)", f"₹{spot:,.2f}" if spot else "—")
        a3.metric("ATM CE IV",         f"{float(atm.get('ce_iv', 0) or 0):.1f}%")
        a4.metric("ATM PE IV",         f"{float(atm.get('pe_iv', 0) or 0):.1f}%")

        ce_iv = float(atm.get("ce_iv", 0) or 0)
        pe_iv = float(atm.get("pe_iv", 0) or 0)
        if ce_iv > 0 and pe_iv > 0 and abs(ce_iv - pe_iv) > 0.5:
            bias = "Bullish skew (CE IV > PE IV)" if ce_iv > pe_iv else "Bearish skew (PE IV > CE IV)"
            st.info(f"IV skew: **{bias}**")

    atm_strike = atm["strike"] if atm else None
    st.markdown(
        _render_option_chain_html(chain_rows, atm_strike),
        unsafe_allow_html=True,
    )
    st.divider()


@st.fragment(run_every=live_every)
def render_spike_alerts_fragment() -> None:
    if not show_spikes:
        return

    _section_header("⚡ Additional: Sudden Movement Alerts", "#a78bfa")
    st.markdown(
        "<span style='background:#2d1b69;color:#a78bfa;padding:3px 10px;"
        "border-radius:4px;font-size:.8em;font-weight:600'>"
        "Volume spike trades · shown in addition to top signals above</span>",
        unsafe_allow_html=True,
    )

    alerts = get_intraday_spike_alerts(FO_UNIVERSE, min_confidence=55)

    if not alerts:
        st.caption("No high-confidence spikes detected. Scanner runs every 60s during market hours.")
        st.divider()
        return

    # Summary strip
    high_conf  = sum(1 for a in alerts if a["confidence"] >= 80)
    long_count = sum(1 for a in alerts if a["direction"] == "long")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Total Alerts",    len(alerts))
    s2.metric("High Conf (≥80)", high_conf)
    s3.metric("Bullish",         long_count)
    s4.metric("Bearish",         len(alerts) - long_count)

    # Alert cards
    def _conf_color(c: int) -> str:
        if c >= 85: return "#00c896"
        if c >= 75: return "#4ade80"
        if c >= 65: return "#f59e0b"
        return "#ff9800"

    def _dir_icon(d: str) -> str:
        return "▲ LONG" if d == "long" else "▼ SHORT"

    def _dir_bg(d: str) -> str:
        return "#0a2518" if d == "long" else "#2a0a0f"

    rows = []
    for a in alerts[:20]:
        clr  = _conf_color(a["confidence"])
        icon = _dir_icon(a["direction"])
        rows.append({
            "Symbol":    a["symbol"],
            "Dir":       icon,
            "Conf":      a["confidence"],
            "Vol×":      a["vol_ratio"],
            "Price ₹":   a["price"],
            "VWAP ₹":    a["vwap"],
            "Move%":     round(a["price_move_pct"], 2),
            "Prime":     "✓" if a["in_prime"] else "",
            "Breakout":  "✓" if a["is_breakout"] else "",
            "Reason":    a["reason"],
        })

    df_spike = pd.DataFrame(rows)

    def _style_conf(v):
        if not isinstance(v, (int, float)): return ""
        if v >= 85: return "color:#00c896;font-weight:700"
        if v >= 75: return "color:#4ade80;font-weight:600"
        if v >= 65: return "color:#f59e0b"
        return "color:#ff9800"

    def _style_dir(v):
        if not isinstance(v, str): return ""
        return "color:#00c896" if "LONG" in v else "color:#ff3d5e"

    st.dataframe(
        df_spike.style
            .map(_style_conf, subset=["Conf"])
            .map(_style_dir, subset=["Dir"]),
        use_container_width=True,
        hide_index=True,
        height=min(80 + len(rows) * 38, 520),
    )
    st.caption(f"Last scan: {datetime.now().strftime('%H:%M:%S')} · Min confidence 55 · Vol threshold 2.5×")
    st.divider()


@st.fragment(run_every=medium_every)
def render_trades_fragment() -> None:
    if not show_trades:
        return

    _section_header("Trade P&L", "#f59e0b")

    with st.expander("Log a Trade", expanded=False):
        with st.form("form_add_trade", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            symbol_in    = c1.text_input("Symbol", placeholder="RELIANCE")
            direction_in = c2.selectbox("Direction", ["long", "short"])
            outcome_in   = c3.selectbox("Outcome", ["TARGET_HIT", "SL_HIT", "CLOSED"])
            n1, n2, n3, n4 = st.columns(4)
            entry_in = n1.number_input("Entry ₹", min_value=0.0, step=0.5)
            exit_in  = n2.number_input("Exit ₹",  min_value=0.0, step=0.5)
            qty_in   = n3.number_input("Qty (overridden by lot size)", min_value=1, step=1, help="Auto-replaced by NSE lot size on submit")
            note_in  = n4.text_input("Note")
            st.caption("⚠️ Qty is auto-set to 1 lot from NSE_LOT_SIZES on submit. Enter symbol correctly.")
            submit_trade = st.form_submit_button("Add Trade", type="primary", use_container_width=True)

        if submit_trade:
            if not symbol_in.strip():
                st.error("Symbol is required.")
            elif entry_in <= 0:
                st.error("Entry price must be > 0.")
            else:
                sym_up   = symbol_in.strip().upper()
                lot_size = config.NSE_LOT_SIZES.get(sym_up, qty_in)
                pnl_value = (
                    (exit_in - entry_in) * lot_size if direction_in == "long"
                    else (entry_in - exit_in) * lot_size
                )
                pnl_pct = (pnl_value / (entry_in * lot_size) * 100) if entry_in > 0 else 0
                _append_trade({
                    "trade_id":    f"MANUAL_{uuid.uuid4().hex[:8]}",
                    "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol":      sym_up,
                    "direction":   direction_in.upper(),
                    "entry_price": entry_in,
                    "exit_price":  exit_in,
                    "quantity":    lot_size,
                    "pnl":         round(pnl_value, 2),
                    "pnl_percent": round(pnl_pct, 2),
                    "status":      "WIN" if outcome_in == "TARGET_HIT" else ("LOSS" if outcome_in == "SL_HIT" else "CLOSED"),
                    "exit_reason": note_in or outcome_in,
                })
                # Resolve matching journal entry so adaptive learner can learn from it
                try:
                    from core.signal_journal import resolve_by_symbol_entry
                    from core.adaptive_learner import get_learner
                    resolved = resolve_by_symbol_entry(
                        sym_up, entry_in, outcome_in, exit_in, lot_size=lot_size
                    )
                    if resolved:
                        get_learner().maybe_update()
                except Exception:
                    pass
                _reset_runtime_caches()
                st.success(f"Trade logged · {sym_up} · {lot_size} lots · P&L ₹{pnl_value:+.2f}")
                st.rerun()

    with st.expander("Manage Trade Log", expanded=False):
        st.caption(TRADES_CSV)
        if os.path.exists(TRADES_CSV):
            if st.button("Archive & clear trades.csv", key="archive_trades_btn"):
                arc = TRADES_CSV.replace(".csv", f"_archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
                shutil.copy(TRADES_CSV, arc)
                os.remove(TRADES_CSV)
                _reset_runtime_caches()
                st.success(f"Archived to {os.path.basename(arc)}.")
                st.rerun()
        else:
            st.info("No trade log exists yet.")

    trade_df = load_trades_frame()
    summary  = get_trade_summary()["summary"]
    if trade_df.empty:
        st.info("No trades logged yet.")
        st.divider()
        return

    closed   = trade_df[trade_df["outcome"].isin(["TARGET_HIT", "SL_HIT", "CLOSED"])].copy()
    open_t   = trade_df[trade_df["outcome"] == "OPEN"].copy()
    today    = datetime.now().date()

    # ── Quality filter — only count trades with COMPLETE data ─────────────
    # entry, exit, SL, target ALL > 0; pnl_percent not None.
    # Stubs (exit=entry, pnl=None) are excluded from every metric.
    before_n = len(closed)
    required = ["entry_price", "exit_price", "sl_price", "target_price"]
    for col in required:
        if col in closed.columns:
            closed = closed[pd.to_numeric(closed[col], errors="coerce") > 0]
    if "pnl_percent" in closed.columns:
        closed = closed[pd.to_numeric(closed["pnl_percent"], errors="coerce").notna()]
    skipped_incomplete = before_n - len(closed)
    if skipped_incomplete > 0:
        st.caption(
            f"⚠ Excluded {skipped_incomplete} incomplete trade(s) "
            f"(missing entry/exit/SL/target/pnl) — only fully detailed trades counted."
        )

    today_cl = closed[closed["date"] == today] if "date" in closed.columns else closed.iloc[0:0]

    # ── Period filter ──────────────────────────────────────────────────────
    period = st.radio(
        "Period", ["7D", "30D", "90D", "All"],
        horizontal=True, key="pnl_period",
        label_visibility="collapsed",
    )
    cutoff = {
        "7D":  datetime.now() - timedelta(days=7),
        "30D": datetime.now() - timedelta(days=30),
        "90D": datetime.now() - timedelta(days=90),
        "All": None,
    }[period]
    if cutoff is not None and "timestamp" in closed.columns:
        closed_f = closed[pd.to_datetime(closed["timestamp"], errors="coerce") >= cutoff].copy()
    else:
        closed_f = closed.copy()

    wins_f   = closed_f[closed_f["outcome"] == "TARGET_HIT"]
    losses_f = closed_f[closed_f["outcome"] == "SL_HIT"]

    # ── Streak ────────────────────────────────────────────────────────────
    streak_n, streak_type = 0, ""
    if not closed_f.empty and "timestamp" in closed_f.columns:
        sorted_outcomes = closed_f.sort_values("timestamp")["outcome"].tolist()
        last_type = None
        for o in reversed(sorted_outcomes):
            cur = "W" if o == "TARGET_HIT" else "L"
            if last_type is None:
                last_type = cur
            if cur == last_type:
                streak_n += 1
            else:
                break
        streak_type = last_type or ""

    streak_label = (
        f"[{streak_type}] {streak_n} in a row" if streak_n > 1 else "—"
    )

    # ── Derived metrics ────────────────────────────────────────────────────
    total_pnl_f  = float(closed_f["pnl"].sum()) if not closed_f.empty else 0.0
    today_pnl    = float(today_cl["pnl"].sum()) if not today_cl.empty else 0.0
    n_closed     = len(closed_f)
    win_rate_f   = len(wins_f) / n_closed * 100 if n_closed > 0 else 0.0
    avg_win_f    = float(wins_f["pnl"].mean())   if not wins_f.empty   else 0.0
    avg_loss_f   = float(losses_f["pnl"].mean()) if not losses_f.empty else 0.0
    gross_profit = float(wins_f["pnl"].sum())    if not wins_f.empty   else 0.0
    gross_loss   = abs(float(losses_f["pnl"].sum())) if not losses_f.empty else 0.0
    pf_f         = gross_profit / gross_loss if gross_loss > 0 else 0.0
    expectancy_f = total_pnl_f / n_closed if n_closed > 0 else 0.0

    # ── Metrics row 1 ─────────────────────────────────────────────────────
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric(
        "Total P&L", _fmt_inr(total_pnl_f, sign=True),
        delta=f"Today {_fmt_inr(today_pnl, sign=True)}" if today_pnl != 0 else None,
    )
    m2.metric(
        "Win Rate", f"{win_rate_f:.1f}%",
        delta=f"{len(wins_f)}W / {len(losses_f)}L", delta_color="off",
    )
    m3.metric("Profit Factor", f"{pf_f:.2f}" if pf_f else "—")
    m4.metric("Streak",        streak_label)
    m5.metric("Open",          summary["open_positions"])

    # ── Metrics row 2 + charts ─────────────────────────────────────────────
    if not closed_f.empty:
        eq = closed_f.sort_values("timestamp").copy()
        eq["cumulative_pnl"] = eq["pnl"].cumsum()
        eq["running_max"]    = eq["cumulative_pnl"].cummax()
        eq["drawdown"]       = eq["cumulative_pnl"] - eq["running_max"]
        max_dd  = float(eq["drawdown"].min())
        pnl_arr = eq["pnl"].values
        sharpe  = (
            float(np.mean(pnl_arr) / np.std(pnl_arr) * np.sqrt(len(pnl_arr)))
            if len(pnl_arr) > 1 and np.std(pnl_arr) > 0 else 0.0
        )
        best_pnl  = float(closed_f["pnl"].max())
        worst_pnl = float(closed_f["pnl"].min())

        n1, n2, n3, n4, n5 = st.columns(5)
        n1.metric("Max DD",       _fmt_inr(max_dd, sign=True))
        n2.metric("Trade Sharpe", f"{sharpe:.2f}")
        n3.metric("Avg Win",      _fmt_inr(avg_win_f, sign=True)  if avg_win_f  else "—")
        n4.metric("Avg Loss",     _fmt_inr(avg_loss_f, sign=True) if avg_loss_f else "—")
        n5.metric("Expectancy",   _fmt_inr(expectancy_f, sign=True) if expectancy_f else "—")

        # Best / Worst highlight
        bw1, bw2 = st.columns(2)
        bw1.metric("Best Trade",  _fmt_inr(best_pnl,  sign=True))
        bw2.metric("Worst Trade", _fmt_inr(worst_pnl, sign=True))

        # Equity curve — date X-axis
        st.markdown(
            '<div style="font-size:.7em;font-weight:600;text-transform:uppercase;'
            'letter-spacing:.08em;color:#6b84a0;margin:14px 0 4px">Equity Curve</div>',
            unsafe_allow_html=True,
        )
        eq_plot = eq.copy()
        eq_plot["ts_dt"] = pd.to_datetime(eq_plot.get("timestamp", pd.Series()), errors="coerce")
        eq_dated = eq_plot.dropna(subset=["ts_dt"]).set_index("ts_dt")
        if not eq_dated.empty:
            st.line_chart(eq_dated["cumulative_pnl"], use_container_width=True, height=160)
        else:
            eq["trade_no"] = range(1, len(eq) + 1)
            st.line_chart(eq.set_index("trade_no")["cumulative_pnl"], use_container_width=True, height=160)

        # Drawdown
        st.markdown(
            '<div style="font-size:.7em;font-weight:600;text-transform:uppercase;'
            'letter-spacing:.08em;color:#ff3d5e;margin:6px 0 4px">Drawdown</div>',
            unsafe_allow_html=True,
        )
        if not eq_dated.empty:
            st.area_chart(eq_dated["drawdown"], use_container_width=True, height=90)
        else:
            st.area_chart(eq.set_index("trade_no")["drawdown"], use_container_width=True, height=90)

        # Daily P&L bar chart (altair green/red)
        if "date" in closed_f.columns:
            daily_pnl = (
                closed_f.groupby("date")["pnl"].sum()
                .reset_index()
                .rename(columns={"date": "Date", "pnl": "PnL"})
            )
            daily_pnl["Date"]  = daily_pnl["Date"].astype(str)
            daily_pnl["color"] = daily_pnl["PnL"].apply(lambda v: "win" if v >= 0 else "loss")
            st.markdown(
                '<div style="font-size:.7em;font-weight:600;text-transform:uppercase;'
                'letter-spacing:.08em;color:#6b84a0;margin:10px 0 4px">Daily P&L</div>',
                unsafe_allow_html=True,
            )
            try:
                import altair as alt
                chart = (
                    alt.Chart(daily_pnl)
                    .mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2)
                    .encode(
                        x=alt.X("Date:O", sort=None, title=""),
                        y=alt.Y("PnL:Q", title="P&L (₹)"),
                        color=alt.Color(
                            "color:N",
                            scale=alt.Scale(domain=["win", "loss"], range=["#00c896", "#ff3d5e"]),
                            legend=None,
                        ),
                        tooltip=["Date", alt.Tooltip("PnL:Q", format=",.0f", title="P&L")],
                    )
                    .properties(height=140)
                )
                st.altair_chart(chart, use_container_width=True)
            except Exception:
                st.bar_chart(daily_pnl.set_index("Date")["PnL"], use_container_width=True, height=140)

    # ── Trade tables ───────────────────────────────────────────────────────
    tab_all, tab_today, tab_wins, tab_losses, tab_open, tab_sym, tab_dir, tab_pat = st.tabs([
        "All Closed", "Today", "Wins", "Losses", "Open", "By Symbol", "By Direction", "By Pattern",
    ])

    def _render_trade_table(df: pd.DataFrame) -> None:
        if df.empty:
            st.caption("No trades in this view.")
            return
        shown = df.copy()
        if "timestamp" in shown.columns:
            shown["timestamp"] = shown["timestamp"].astype(str)
        cols = [c for c in [
            "timestamp", "symbol", "direction", "entry_price", "exit_price",
            "quantity", "pnl", "pnl_percent", "outcome", "status", "exit_reason",
        ] if c in shown.columns]
        styled = shown[cols].style.map(_dir_style, subset=["direction"] if "direction" in cols else [])
        if "pnl" in cols:
            styled = styled.map(
                lambda v: "color:#00c896;font-weight:bold" if float(v) > 0 else (
                    "color:#ff3d5e;font-weight:bold" if float(v) < 0 else ""
                ),
                subset=["pnl"],
            )
        st.dataframe(styled, use_container_width=True, hide_index=True, height=360)

    with tab_all:    _render_trade_table(closed_f)
    with tab_today:  _render_trade_table(today_cl)
    with tab_wins:   _render_trade_table(wins_f)
    with tab_losses: _render_trade_table(losses_f)
    with tab_open:   _render_trade_table(open_t)

    with tab_sym:
        if closed_f.empty:
            st.caption("No closed trades.")
        else:
            by_sym = (
                closed_f.groupby("symbol")
                .agg(Trades=("pnl","count"), Total_PnL=("pnl","sum"),
                     Avg_PnL=("pnl","mean"),
                     Win_Rate=("outcome", lambda v: (v == "TARGET_HIT").mean() * 100))
                .round(2).sort_values("Total_PnL", ascending=False).reset_index()
            )
            st.dataframe(by_sym, use_container_width=True, hide_index=True)

    with tab_dir:
        if closed_f.empty:
            st.caption("No closed trades.")
        else:
            by_dir = (
                closed_f.groupby("direction")
                .agg(Trades=("pnl","count"), Total_PnL=("pnl","sum"),
                     Avg_PnL=("pnl","mean"),
                     Win_Rate=("outcome", lambda v: (v == "TARGET_HIT").mean() * 100))
                .round(2).reset_index()
            )
            st.dataframe(by_dir, use_container_width=True, hide_index=True)

    with tab_pat:
        pat_col = next((c for c in ["patterns_combined", "exit_reason"] if c in closed_f.columns), None)
        if closed_f.empty or pat_col is None:
            st.caption("No pattern data available.")
        else:
            exploded = (
                closed_f.assign(_pat=closed_f[pat_col].astype(str).str.split(r"[,|]", regex=True))
                .explode("_pat")
                .assign(_pat=lambda x: x["_pat"].str.strip())
            )
            # Skip rows that are outcome strings not pattern names
            outcome_vals = {"TARGET_HIT", "SL_HIT", "EXPIRED", "CLOSED", "OPEN", "WIN", "LOSS", "nan", ""}
            exploded = exploded[
                exploded["_pat"].notna()
                & ~exploded["_pat"].isin(outcome_vals)
                & (exploded["_pat"].str.len() > 0)
            ]
            if exploded.empty:
                st.caption("No pattern data available.")
            else:
                pat_agg = (
                    exploded.groupby("_pat")
                    .agg(Trades=("pnl","count"), Total_PnL=("pnl","sum"),
                         Avg_PnL=("pnl","mean"),
                         Win_Rate=("outcome", lambda v: (v == "TARGET_HIT").mean() * 100))
                    .round(2).sort_values("Total_PnL", ascending=False)
                    .reset_index().rename(columns={"_pat": "Pattern"})
                )
                st.dataframe(pat_agg, use_container_width=True, hide_index=True)

    st.divider()


@st.fragment(run_every=slow_every)
def render_intelligence_fragment() -> None:
    if not show_intelligence:
        return

    _section_header("RAG Intelligence", "#a78bfa")
    rag   = _rag()
    brief = rag.build_market_brief()

    col_l, col_r = st.columns([2, 1])
    with col_l:
        st.markdown(
            '<div style="font-size:.7em;font-weight:600;text-transform:uppercase;'
            'letter-spacing:.08em;color:#6b84a0;margin-bottom:6px">Market Brief</div>',
            unsafe_allow_html=True,
        )
        for line in brief.get("summary", []):
            st.write(f"- {line}")
    with col_r:
        st.markdown(
            '<div style="font-size:.7em;font-weight:600;text-transform:uppercase;'
            'letter-spacing:.08em;color:#6b84a0;margin-bottom:6px">Recommendations</div>',
            unsafe_allow_html=True,
        )
        for line in brief.get("recommendations", []):
            st.write(f"- {line}")

    evidence = brief.get("evidence", [])
    if evidence:
        with st.expander("Supporting Evidence", expanded=False):
            for item in evidence:
                st.markdown(f"**{item['title']}** [{item['source']}]")
                st.caption(item["snippet"])

    with st.expander("Query Knowledge Base", expanded=False):
        with st.form("rag_query_form", clear_on_submit=False):
            question = st.text_input(
                "Question",
                value=st.session_state.get("rag_question", ""),
                placeholder="Which setups are strongest right now?",
            )
            run_query = st.form_submit_button("Run Query", type="primary")
        if run_query and question.strip():
            st.session_state["rag_question"]  = question.strip()
            st.session_state["rag_response"]  = rag.query(question.strip(), top_k=5)

        response = st.session_state.get("rag_response")
        if response:
            st.code(response.get("answer", ""), language="text")
            for item in response.get("matches", []):
                st.markdown(f"**{item['title']}** [{item['source']}]")
                st.caption(item["snippet"])
    st.divider()


@st.fragment(run_every=medium_every)
def render_accuracy_fragment() -> None:
    if not show_accuracy:
        return

    _section_header("P&L Report", "#38b2f0")

    journal_df = load_signal_journal_frame()

    # ── Empty state ────────────────────────────────────────────────────────
    if journal_df.empty:
        st.info("No signal outcomes yet. Run signal_tracker.py alongside the scanner.")
        st.caption(f"Tracker file: `{JOURNAL_FILE}`")

        # Still show live tracking if signals exist
        tracking = get_live_signal_tracking()
        if tracking:
            st.subheader("Live Signal Tracking")
            _render_live_tracking(tracking)
        st.divider()
        return

    # ── Precompute core stats ──────────────────────────────────────────────
    decided_raw = journal_df[journal_df["outcome"].isin(["WIN", "LOSS"])].copy()

    # Quality filter — only trades with full entry/exit/SL/target details
    decided = decided_raw.copy()
    for col in ("entry_price", "exit_price", "sl_price", "target_price"):
        if col in decided.columns:
            decided = decided[pd.to_numeric(decided[col], errors="coerce") > 0]
    if "pnl_pct" in decided.columns:
        decided = decided[pd.to_numeric(decided["pnl_pct"], errors="coerce").notna()]
    skipped = len(decided_raw) - len(decided)
    if skipped > 0:
        st.caption(
            f"⚠ Excluded {skipped} signal(s) with incomplete data — "
            f"only trades with full entry/exit/SL/target counted."
        )

    wins_df   = decided[decided["outcome"] == "WIN"]
    losses_df = decided[decided["outcome"] == "LOSS"]
    timeouts  = int((journal_df["outcome"] == "TIMEOUT").sum())
    total     = len(journal_df)
    n_wins    = len(wins_df)
    n_losses  = len(losses_df)
    win_rate  = n_wins / len(decided) * 100 if len(decided) else 0.0
    pnl_s     = get_signal_pnl_summary()

    # Sharpe + max drawdown (₹)
    if not decided.empty and "pnl_rupees" in decided.columns:
        decided_sorted = decided.sort_values("ts_outcome")
        pnl_arr = decided_sorted["pnl_rupees"].values
        sharpe  = float(np.mean(pnl_arr) / np.std(pnl_arr) * np.sqrt(len(pnl_arr))) \
                  if np.std(pnl_arr) > 0 else 0.0
        cum     = np.cumsum(pnl_arr)
        max_dd  = float((cum - np.maximum.accumulate(cum)).min())
    else:
        sharpe = 0.0
        max_dd = 0.0

    # ── Header metrics row 1 ───────────────────────────────────────────────
    h1, h2, h3, h4, h5, h6 = st.columns(6)
    h1.metric("Total P&L",    _fmt_inr(pnl_s['total_pnl_rupees'], sign=True))
    h2.metric("Signals",       total)
    h3.metric("Win Rate",      f"{win_rate:.1f}%")
    h4.metric("W / L",         f"{n_wins} / {n_losses}")
    h5.metric("Prof. Factor",  f"{pnl_s['profit_factor']:.2f}" if pnl_s["profit_factor"] else "—")
    h6.metric("Timeouts",      timeouts)

    # Header metrics row 2
    h7, h8, h9, h10, h11, h12 = st.columns(6)
    h7.metric("Avg Win",       _fmt_inr(pnl_s['avg_win_rupees'],  sign=True) if pnl_s["avg_win_rupees"]  else "—")
    h8.metric("Avg Loss",      _fmt_inr(pnl_s['avg_loss_rupees'], sign=True) if pnl_s["avg_loss_rupees"] else "—")
    if "pnl_pct" in journal_df.columns:
        avg_pnl_pct = journal_df.loc[journal_df["outcome"] != "TIMEOUT", "pnl_pct"].mean()
    else:
        avg_pnl_pct = float("nan")
    h9.metric("Avg P&L %",    f"{avg_pnl_pct:+.2f}%" if pd.notna(avg_pnl_pct) else "—")
    h10.metric("Sharpe",       f"{sharpe:.2f}" if sharpe else "—")
    h11.metric("Max DD",       _fmt_inr(max_dd, sign=True) if max_dd else "—")
    expectancy = (win_rate/100 * pnl_s['avg_win_rupees'] + (1-win_rate/100) * pnl_s['avg_loss_rupees']) if pnl_s["avg_win_rupees"] else None
    h12.metric("Expectancy",   _fmt_inr(expectancy, sign=True) if expectancy is not None else "—")
    st.caption("₹ P&L = 1 lot × |price move| from entry. Lot sizes: config.NSE_LOT_SIZES.")

    # ── Live signal tracking panel ─────────────────────────────────────────
    tracking = get_live_signal_tracking()
    if tracking:
        with st.expander(f"🎯 Live Signal Tracking ({len(tracking)} active)", expanded=True):
            _render_live_tracking(tracking)

    # ── Main tabs ─────────────────────────────────────────────────────────
    tab_log, tab_today, tab_grade, tab_dir, tab_curve, tab_daily, tab_top = st.tabs([
        "📋 Full Log", "📅 Today", "🏅 By Grade", "↕ By Direction",
        "📈 Equity Curve", "📊 Daily Report", "🏆 Best / Worst",
    ])

    # Helper: style outcome column
    def _style_outcome(v):
        if not isinstance(v, str): return ""
        if v == "WIN":     return "color:#00c896;font-weight:700"
        if v == "LOSS":    return "color:#ff3d5e;font-weight:700"
        if v == "TIMEOUT": return "color:#6b84a0"
        return ""

    def _style_pnl_rs(v):
        if not isinstance(v, (int, float)): return ""
        return "color:#00c896;font-weight:600" if v > 0 else ("color:#ff3d5e;font-weight:600" if v < 0 else "")

    def _style_dir(v):
        if not isinstance(v, str): return ""
        return "color:#00c896" if v.upper() == "LONG" else "color:#ff3d5e"

    # ── Tab: Full Log ──────────────────────────────────────────────────────
    with tab_log:
        log_df = journal_df.sort_values("ts_outcome", ascending=False).head(200).copy()
        if "ts_outcome" in log_df.columns:
            log_df["ts_outcome"] = log_df["ts_outcome"].dt.strftime("%m-%d %H:%M")
        log_df["icon"] = log_df["outcome"].map({"WIN": "✅", "LOSS": "❌", "TIMEOUT": "⏱"}).fillna("?")
        display_cols = [c for c in [
            "icon", "ts_outcome", "symbol", "direction", "grade", "score",
            "entry_price", "exit_price", "target_price", "sl_price",
            "lot_size", "pnl_pct", "pnl_rupees", "outcome",
        ] if c in log_df.columns]
        st.dataframe(
            log_df[display_cols].style
                .map(_style_outcome, subset=["outcome"] if "outcome" in display_cols else [])
                .map(_style_pnl_rs,  subset=["pnl_rupees"] if "pnl_rupees" in log_df.columns else [])
                .map(_style_dir,     subset=["direction"] if "direction" in display_cols else []),
            use_container_width=True,
            hide_index=True,
            height=440,
        )

    # ── Tab: Today ─────────────────────────────────────────────────────────
    with tab_today:
        today = datetime.now().date()
        today_df = journal_df[journal_df.get("date", pd.Series(dtype=object)) == today].copy() \
                   if "date" in journal_df.columns else journal_df.iloc[0:0]

        if today_df.empty:
            st.caption("No outcomes recorded today.")
        else:
            t_wins   = int((today_df["outcome"] == "WIN").sum())
            t_losses = int((today_df["outcome"] == "LOSS").sum())
            t_pnl    = float(today_df.get("pnl_rupees", pd.Series([0])).sum())
            ta, tb, tc, td = st.columns(4)
            ta.metric("Today Signals", len(today_df))
            tb.metric("Wins",  t_wins)
            tc.metric("Losses", t_losses)
            td.metric("Today ₹ P&L", f"₹{t_pnl:+,.0f}")

            today_df["icon"] = today_df["outcome"].map({"WIN": "✅", "LOSS": "❌", "TIMEOUT": "⏱"}).fillna("?")
            t_cols = [c for c in ["icon", "symbol", "direction", "grade",
                                   "entry_price", "exit_price", "pnl_pct", "pnl_rupees", "outcome"]
                      if c in today_df.columns]
            st.dataframe(
                today_df[t_cols].style
                    .map(_style_outcome, subset=["outcome"] if "outcome" in t_cols else [])
                    .map(_style_pnl_rs,  subset=["pnl_rupees"] if "pnl_rupees" in today_df.columns else []),
                use_container_width=True,
                hide_index=True,
            )

    # ── Tab: By Grade ──────────────────────────────────────────────────────
    with tab_grade:
        if "grade" in journal_df.columns:
            agg = {
                "Signals":       ("outcome", "count"),
                "WIN":           ("outcome", lambda v: (v == "WIN").sum()),
                "LOSS":          ("outcome", lambda v: (v == "LOSS").sum()),
                "TIMEOUT":       ("outcome", lambda v: (v == "TIMEOUT").sum()),
                "Win_Rate_%":    ("outcome", lambda v: round((v == "WIN").mean() * 100, 1)),
                "Avg_PnL_%":     ("pnl_pct", "mean"),
            }
            if "pnl_rupees" in journal_df.columns:
                agg["Total_PnL_₹"] = ("pnl_rupees", "sum")
                agg["Avg_PnL_₹"]   = ("pnl_rupees", "mean")
            by_grade = (
                journal_df.groupby("grade").agg(**agg)
                .round(2).reset_index().sort_values("Win_Rate_%", ascending=False)
            )
            st.dataframe(by_grade, use_container_width=True, hide_index=True)
        else:
            st.caption("No grade data.")

    # ── Tab: By Direction ──────────────────────────────────────────────────
    with tab_dir:
        if "direction" in journal_df.columns:
            dir_df = journal_df.copy()
            dir_df["direction"] = dir_df["direction"].str.upper()
            agg_d = {
                "Signals":    ("outcome", "count"),
                "WIN":        ("outcome", lambda v: (v == "WIN").sum()),
                "LOSS":       ("outcome", lambda v: (v == "LOSS").sum()),
                "Win_Rate_%": ("outcome", lambda v: round((v == "WIN").mean() * 100, 1)),
                "Avg_PnL_%":  ("pnl_pct", "mean"),
            }
            if "pnl_rupees" in dir_df.columns:
                agg_d["Total_PnL_₹"] = ("pnl_rupees", "sum")
            by_dir = dir_df.groupby("direction").agg(**agg_d).round(2).reset_index()
            st.dataframe(by_dir, use_container_width=True, hide_index=True)

            # Hour-of-day breakdown
            if "ts_outcome" in journal_df.columns:
                hourly_df = decided.copy()
                hourly_df["hour"] = pd.to_datetime(hourly_df["ts_outcome"], errors="coerce").dt.hour
                if hourly_df["hour"].notna().any():
                    h_agg = {
                        "Signals":   ("outcome", "count"),
                        "Win_Rate":  ("outcome", lambda v: round((v == "WIN").mean() * 100, 1)),
                        "Sum_PnL_Rs": ("pnl_rupees", "sum") if "pnl_rupees" in hourly_df.columns else ("pnl_pct", "sum"),
                    }
                    by_hour = hourly_df.groupby("hour").agg(**h_agg).round(2).reset_index()
                    st.caption("P&L by hour of day (IST)")
                    st.bar_chart(by_hour.set_index("hour")["Sum_PnL_Rs"], use_container_width=True, height=160)
                    st.dataframe(by_hour, use_container_width=True, hide_index=True)
        else:
            st.caption("No direction data.")

    # ── Tab: Equity Curve ──────────────────────────────────────────────────
    with tab_curve:
        if decided.empty:
            st.caption("No WIN/LOSS outcomes yet.")
        else:
            eq = decided.sort_values("ts_outcome").copy()
            eq["#"] = range(1, len(eq) + 1)
            eq["Cum_PnL_%"]  = eq["pnl_pct"].cumsum()
            eq["Cum_PnL_₹"]  = eq["pnl_rupees"].cumsum() if "pnl_rupees" in eq.columns else 0
            eq["RunMax"]     = eq["Cum_PnL_₹"].cummax()
            eq["Drawdown_₹"] = eq["Cum_PnL_₹"] - eq["RunMax"]

            ca, cb = st.columns(2)
            with ca:
                st.caption("Cumulative P&L (₹) — 1 lot each signal")
                st.line_chart(eq.set_index("#")["Cum_PnL_₹"], use_container_width=True, height=200)
            with cb:
                st.caption("Drawdown (₹)")
                st.area_chart(eq.set_index("#")["Drawdown_₹"], use_container_width=True, height=200)

            st.caption("Cumulative P&L (%)")
            st.line_chart(eq.set_index("#")["Cum_PnL_%"], use_container_width=True, height=160)

    # ── Tab: Daily Report ──────────────────────────────────────────────────
    with tab_daily:
        if decided.empty:
            st.caption("No WIN/LOSS outcomes yet.")
        else:
            daily_agg = {
                "Signals":    ("outcome", "count"),
                "WIN":        ("outcome", lambda v: (v == "WIN").sum()),
                "LOSS":       ("outcome", lambda v: (v == "LOSS").sum()),
                "Win_Rate_%": ("outcome", lambda v: round((v == "WIN").mean() * 100, 1)),
                "Sum_PnL_%":  ("pnl_pct", "sum"),
            }
            if "pnl_rupees" in decided.columns:
                daily_agg["Sum_PnL_₹"] = ("pnl_rupees", "sum")
                daily_agg["Avg_PnL_₹"] = ("pnl_rupees", "mean")
            daily = (
                decided.groupby("date").agg(**daily_agg)
                .round(2).reset_index().sort_values("date", ascending=False)
            )
            chart_col = "Sum_PnL_₹" if "Sum_PnL_₹" in daily.columns else "Sum_PnL_%"
            st.bar_chart(daily.set_index("date")[chart_col], use_container_width=True, height=200)
            st.dataframe(
                daily.style.map(
                    lambda v: "color:#00c896;font-weight:600" if isinstance(v,(int,float)) and v > 0
                    else ("color:#ff3d5e;font-weight:600" if isinstance(v,(int,float)) and v < 0 else ""),
                    subset=[chart_col],
                ),
                use_container_width=True,
                hide_index=True,
            )

    # ── Tab: Best / Worst ──────────────────────────────────────────────────
    with tab_top:
        if decided.empty or "pnl_rupees" not in decided.columns:
            st.caption("No outcome data yet.")
        else:
            sort_col = "pnl_rupees"
            show_cols = [c for c in ["symbol", "direction", "grade", "entry_price",
                                      "exit_price", "lot_size", "pnl_pct", "pnl_rupees",
                                      "outcome", "ts_outcome"] if c in decided.columns]

            bc, wc = st.columns(2)
            with bc:
                st.markdown("**🏆 Top 10 Wins**")
                best = decided.nlargest(10, sort_col)[show_cols].copy()
                if "ts_outcome" in best.columns:
                    best["ts_outcome"] = best["ts_outcome"].astype(str).str[:16]
                st.dataframe(
                    best.style.map(_style_pnl_rs, subset=["pnl_rupees"]),
                    use_container_width=True, hide_index=True,
                )
            with wc:
                st.markdown("**💀 Top 10 Losses**")
                worst = decided.nsmallest(10, sort_col)[show_cols].copy()
                if "ts_outcome" in worst.columns:
                    worst["ts_outcome"] = worst["ts_outcome"].astype(str).str[:16]
                st.dataframe(
                    worst.style.map(_style_pnl_rs, subset=["pnl_rupees"]),
                    use_container_width=True, hide_index=True,
                )

    st.divider()


def _render_live_tracking(tracking: List[Dict]) -> None:
    """Render live signal tracking panel with progress bars and status."""
    rows = []
    for t in tracking:
        direction = str(t.get("direction", "long")).upper()
        pct_entry = float(t.get("pct_from_entry", 0))
        pct_tgt   = float(t.get("pct_to_target", 0))
        pct_sl    = float(t.get("pct_to_sl", 0))
        progress  = float(t.get("progress", 50))
        status    = t.get("status", "RUNNING")
        unreal    = float(t.get("unrealized_pnl", 0))

        status_icon = {"AT_TARGET": "🎯", "NEAR_TARGET": "🟢", "RUNNING": "▶",
                       "NEAR_SL": "🟠", "AT_SL": "🔴"}.get(status, "▶")

        rows.append({
            "":        status_icon,
            "Symbol":  t.get("symbol", ""),
            "Dir":     direction,
            "Grade":   t.get("grade", "?"),
            "Entry ₹": t.get("entry", 0),
            "Now ₹":   t.get("current", 0),
            "SL ₹":    t.get("sl", 0),
            "Tgt ₹":   t.get("target", 0),
            "vs Entry%": f"{pct_entry:+.2f}%",
            "→Target%":  f"{pct_tgt:+.2f}%",
            "→SL%":      f"{pct_sl:+.2f}%",
            "Progress": round(progress, 0),
            "Unreal ₹": f"₹{unreal:+,.0f}",
            "Age":      f"{t.get('age_min', 0)}m",
            "Status":   status,
        })

    if not rows:
        st.caption("No active signals.")
        return

    df_track = pd.DataFrame(rows)

    def _style_status(v):
        if not isinstance(v, str): return ""
        m = {"AT_TARGET": "color:#00c896;font-weight:700",
             "NEAR_TARGET": "color:#4ade80",
             "NEAR_SL":     "color:#f59e0b",
             "AT_SL":       "color:#ff3d5e;font-weight:700",
             "RUNNING":     "color:#6b84a0"}
        return m.get(v, "")

    def _style_dir(v):
        if not isinstance(v, str): return ""
        return "color:#00c896" if "LONG" in v else "color:#ff3d5e"

    st.dataframe(
        df_track.style
            .map(_style_status, subset=["Status"])
            .map(_style_dir, subset=["Dir"]),
        use_container_width=True,
        hide_index=True,
        height=min(80 + len(rows) * 38, 400),
    )
    st.caption("Progress: 0% = at SL, 100% = at target. Unrealized = 1 lot × price move.")


@st.fragment(run_every=medium_every)
def render_learning_fragment() -> None:
    if not show_learning:
        return

    _section_header("Adaptive Learning", "#6366f1")

    try:
        from core.adaptive_learner import get_learner
        from core.signal_journal import get_resolved_signals, get_open_signals
        learner = get_learner()
        resolved = get_resolved_signals(days=90)
        decided  = [r for r in resolved if r.get("outcome") in ("TARGET_HIT", "SL_HIT")]
        open_sigs = get_open_signals()
    except Exception as e:
        st.warning(f"Learning engine unavailable: {e}")
        return

    wins   = sum(1 for d in decided if d["outcome"] == "TARGET_HIT")
    losses = sum(1 for d in decided if d["outcome"] == "SL_HIT")
    total  = len(decided)
    wr     = wins / total * 100 if total else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Resolved Signals", total)
    c2.metric("Win Rate",         f"{wr:.1f}%")
    c3.metric("Pending",          len(open_sigs))
    c4.metric("Target Hits",      wins)

    if total >= 5:
        tab_pat, tab_regime, tab_params, tab_log = st.tabs(
            ["Pattern Stats", "Regime Stats", "Learned Params", "Change Log"]
        )

        with tab_pat:
            pattern_stats = learner.get_pattern_stats()
            if pattern_stats:
                rows = sorted(
                    [{"Pattern": p, "Trades": s["total"], "Wins": s["wins"],
                      "Win Rate": f"{s['win_rate']:.1%}", "Avg P&L ₹": f"{s['avg_pnl']:+.0f}",
                      "EMA Win Rate": f"{s['ema_win_rate']:.1%}"}
                     for p, s in pattern_stats.items() if s["total"] >= 2],
                    key=lambda x: -float(x["Win Rate"].rstrip("%"))
                )
                if rows:
                    df_pat = pd.DataFrame(rows)
                    st.dataframe(df_pat, use_container_width=True, hide_index=True)
                else:
                    st.caption("Not enough per-pattern data yet.")
            else:
                st.caption("No pattern data yet.")

        with tab_regime:
            regime_stats = learner.get_regime_stats()
            if regime_stats:
                rows = [
                    {"Regime": k.replace("session:", "sess:").replace("bias:", "bias:").replace("vol:", "vol:"),
                     "Trades": s["total"], "Win Rate": f"{s['win_rate']:.1%}",
                     "Avg P&L ₹": f"{s['avg_pnl']:+.0f}"}
                    for k, s in regime_stats.items() if s["total"] >= 3
                ]
                rows.sort(key=lambda x: -float(x["Win Rate"].rstrip("%")))
                if rows:
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                else:
                    st.caption("Not enough regime data yet (need 3+ trades per regime).")
            else:
                st.caption("No regime data yet.")

        with tab_params:
            param_summary = learner.get_param_summary()
            if param_summary:
                rows = []
                for p in param_summary:
                    drift = p["current"] - p["default"]
                    rows.append({
                        "Parameter":  p["param"],
                        "Section":    p["section"],
                        "Default":    p["default"],
                        "Current":    p["current"],
                        "Drift":      f"{drift:+.2f}",
                        "Range":      f"[{p['min']}, {p['max']}]",
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            c_force, c_reset = st.columns(2)
            if c_force.button("Force learning cycle now", use_container_width=True):
                changes = learner.maybe_update(force=True)
                if changes:
                    st.success(f"Updated {len(changes)} params: {list(changes.keys())}")
                else:
                    st.info("No parameter changes warranted at this time.")
                st.rerun()
            if c_reset.button("Reset all params to defaults", use_container_width=True):
                learner.reset_all()
                st.success("All learned parameters reset to defaults.")
                st.rerun()

        with tab_log:
            log_file = os.path.join(os.path.dirname(SIGNALS_PATH), "param_changes.jsonl")
            if os.path.exists(log_file):
                entries = []
                with open(log_file, encoding="utf-8") as f:
                    for line in f:
                        try:
                            entries.append(json.loads(line.strip()))
                        except Exception:
                            pass
                if entries:
                    rows = []
                    for e in reversed(entries[-50:]):
                        for param, info in e.get("changes", {}).items():
                            rows.append({
                                "Time":    e.get("ts", "")[:16],
                                "Param":   param,
                                "From":    info.get("from"),
                                "To":      info.get("to"),
                                "Reason":  info.get("reason", ""),
                                "N Trades": e.get("n_trades", ""),
                            })
                    if rows:
                        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                    else:
                        st.caption("No changes logged yet.")
                else:
                    st.caption("No change log entries yet.")
            else:
                st.caption("No parameter changes made yet.")
    else:
        needed = 5 - total
        st.info(f"Need {needed} more resolved signals before learning stats appear. Log trade outcomes or wait for auto-resolution.")

    st.divider()


@st.fragment(run_every=live_every)
def render_footer_fragment() -> None:
    now = datetime.now()
    data_health_live = sec.data_token_health()
    src = "Dhan Data API" if data_health_live.valid else "Fallback / partial"
    st.markdown(
        f'<div class="appTitle" style="margin-top:8px;padding-top:8px">'
        f'<span style="font-size:.65em;color:#3a4f66">'
        f'Rendered {now.strftime("%H:%M:%S")} · Data: {src} · '
        f'Signals: <code>{SIGNALS_PATH}</code> · FastAPI: <code>{_api_url()}</code> · '
        f'{"Refresh paused" if pause_refresh else f"Fragments every {refresh_sec}s"}'
        f'</span></div>',
        unsafe_allow_html=True,
    )


# ── Render ────────────────────────────────────────────────────────────────────
render_index_fragment()
render_header_fragment()
render_signals_fragment(top_n_signals=top_n_signals)
render_spike_alerts_fragment()
render_positions_fragment()
render_volume_fragment()
render_option_chain_fragment()
render_trades_fragment()
render_intelligence_fragment()
render_accuracy_fragment()
render_learning_fragment()
render_footer_fragment()
