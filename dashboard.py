"""
Streamlit dashboard for multi-agent F&O trading system.

Reads logs/state.json written by MultiAgentOrchestrator every 10s.
Auto-refreshes every 5 seconds.

Run:
    streamlit run dashboard.py
"""

import json
import os
from datetime import datetime

import streamlit as st
import pandas as pd

STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "state.json")
PERF_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "performance.json")
NOTIF_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "notifications.json")
TRADES_CSV  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "trades.csv")


def load_trades_today_from_csv() -> list:
    """Fallback: read trades.csv and filter to today's date.

    Why this exists: state.json's `today_trades` is sourced from
    `SharedState.today_trades`, which the multi-agent runner never populates
    (ExecutionAgent logs to TradeLogger but doesn't push into SharedState).
    Result: dashboard always showed "No trades yet today" even when trades
    were happening. This fallback closes that gap.
    """
    if not os.path.exists(TRADES_CSV):
        return []
    try:
        df = pd.read_csv(TRADES_CSV)
        if df.empty or "timestamp" not in df.columns:
            return []
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        today = datetime.now().date()
        df = df[df["timestamp"].dt.date == today].copy()
        if df.empty:
            return []
        return df.to_dict("records")
    except Exception:
        return []

st.set_page_config(
    page_title="F&O Trading Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Auto-refresh every 5 seconds
st.markdown(
    '<meta http-equiv="refresh" content="5">',
    unsafe_allow_html=True,
)

# ── Load state ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def load_perf() -> dict:
    if not os.path.exists(PERF_FILE):
        return {}
    try:
        with open(PERF_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def load_notifications() -> list:
    if not os.path.exists(NOTIF_FILE):
        return []
    try:
        with open(NOTIF_FILE) as f:
            return json.load(f)
    except Exception:
        return []

state = load_state()
perf  = load_perf()
notifs = load_notifications()

# ── Header ────────────────────────────────────────────────────────────────────

st.title("F&O Multi-Agent Trading")

if not state:
    st.warning("No state file found. Start the runner first: `python multi_agent_runner.py`")
    st.stop()

ts_str = state.get("ts", "")
try:
    ts = datetime.fromisoformat(ts_str)
    age_sec = (datetime.now() - ts).total_seconds()
    age_label = f"{age_sec:.0f}s ago"
    stale = age_sec > 30
except Exception:
    age_label = "unknown"
    stale = True

col_title, col_ts = st.columns([5, 1])
with col_ts:
    if stale:
        st.error(f"Last update: {age_label}")
    else:
        st.success(f"Last update: {age_label}")

# ── Row 1: Regime + Market metrics ────────────────────────────────────────────

regime = state.get("market_regime", "unknown").upper()
nifty  = state.get("nifty_change_pct", 0.0)
bnf    = state.get("banknifty_change_pct", 0.0)
vix    = state.get("vix_proxy", 15.0)
heat   = state.get("portfolio_heat", 0.0)
pnl    = state.get("daily_pnl", 0.0)
halted = state.get("trading_halted", False)

REGIME_COLOR = {
    "BULL": "🟢",
    "BEAR": "🔴",
    "VOLATILE": "🟠",
    "NEUTRAL": "🔵",
}
regime_icon = REGIME_COLOR.get(regime, "⚪")

r1c1, r1c2, r1c3, r1c4, r1c5, r1c6 = st.columns(6)

with r1c1:
    st.metric("Regime", f"{regime_icon} {regime}")
with r1c2:
    st.metric("NIFTY", f"{nifty:+.2f}%")
with r1c3:
    st.metric("BANKNIFTY", f"{bnf:+.2f}%")
with r1c4:
    st.metric("VIX Proxy", f"{vix:.1f}")
with r1c5:
    heat_pct = heat * 100
    heat_color = "normal" if heat_pct < 15 else ("off" if heat_pct < 22 else "inverse")
    st.metric("Portfolio Heat", f"{heat_pct:.1f}%")
with r1c6:
    pnl_delta = "+" if pnl >= 0 else ""
    st.metric("Daily P&L", f"₹{pnl:,.0f}")

# Halt banner
if halted:
    st.error(f"TRADING HALTED — {state.get('halt_reason', '')}")

st.divider()

# ── Row 2: Stats strip ────────────────────────────────────────────────────────

trades   = state.get("total_trades_today", 0)
c_losses = state.get("consecutive_losses", 0)
w_streak = state.get("win_streak", 0)
n_pos    = len(state.get("open_positions", {}))
n_surges = len(state.get("volume_surges", {}))

s1, s2, s3, s4, s5 = st.columns(5)
s1.metric("Trades Today", trades)
s2.metric("Open Positions", n_pos)
s3.metric("Volume Surges", n_surges)
s4.metric("Win Streak", w_streak)
s5.metric("Consec. Losses", c_losses, delta_color="inverse")

st.divider()

# ── Row 3: Open positions + Volume surges ─────────────────────────────────────

col_pos, col_surge = st.columns([3, 2])

with col_pos:
    st.subheader("Open Positions")
    positions = state.get("open_positions", {})
    if positions:
        rows = []
        for sym, pos in positions.items():
            upnl       = pos.get("upnl")
            pct        = pos.get("pct")
            sl_dist    = pos.get("sl_dist_pct")
            current    = pos.get("current_price")
            sl_price   = pos.get("sl_price", 0)
            target     = pos.get("target_price", 0)

            upnl_str  = (f"₹{upnl:+,.0f}" if upnl is not None else "—")
            pct_str   = (f"{pct:+.1f}%"   if pct  is not None else "—")
            cur_str   = (f"₹{current:,.2f}" if current else "—")
            sl_str    = (f"{sl_dist:.1f}%" if sl_dist is not None else "—")
            rows.append({
                "Symbol":    sym,
                "Dir":       pos.get("direction", "").upper(),
                "Entry":     f"₹{pos.get('entry', 0):,.2f}",
                "Now":       cur_str,
                "Unreal P&L": upnl_str,
                "Chg%":      pct_str,
                "SL Dist":   sl_str,
                "Conf":      f"{pos.get('confidence', 0)*100:.0f}%",
            })
        df_pos = pd.DataFrame(rows)

        def color_row(row):
            styles = [""] * len(row)
            dir_idx = df_pos.columns.get_loc("Dir")
            pnl_idx = df_pos.columns.get_loc("Unreal P&L")
            chg_idx = df_pos.columns.get_loc("Chg%")
            sl_idx  = df_pos.columns.get_loc("SL Dist")

            if row["Dir"] == "LONG":
                styles[dir_idx] = "color:#00c853"
            elif row["Dir"] == "SHORT":
                styles[dir_idx] = "color:#ff1744"

            val = row["Unreal P&L"]
            if "+" in str(val):
                styles[pnl_idx] = "color:#00c853;font-weight:bold"
            elif val.startswith("₹-"):
                styles[pnl_idx] = "color:#ff1744;font-weight:bold"

            try:
                sl_num = float(str(row["SL Dist"]).replace("%", ""))
                if 0 < sl_num <= 2.0:
                    styles[sl_idx] = "color:#ff1744;font-weight:bold"
                elif 0 < sl_num <= 5.0:
                    styles[sl_idx] = "color:#ff9800"
            except ValueError:
                pass

            return styles

        st.dataframe(
            df_pos.style.apply(color_row, axis=1),
            width='stretch',
            hide_index=True,
        )
    else:
        st.info("No open positions")

with col_surge:
    st.subheader("Volume Surges")
    surges = state.get("volume_surges", {})
    if surges:
        rows = sorted(surges.items(), key=lambda x: x[1], reverse=True)
        df_surge = pd.DataFrame(rows, columns=["Symbol", "Vol Ratio"])
        df_surge["Vol Ratio"] = df_surge["Vol Ratio"].map(lambda x: f"{x:.2f}x")
        st.dataframe(df_surge, width='stretch', hide_index=True)
    else:
        st.info("No surges detected")

st.divider()

# ── Row 4: Today's trades ─────────────────────────────────────────────────────

st.subheader("Today's Trades")
today_trades = state.get("today_trades", [])
# Fallback: state.today_trades is empty due to ExecutionAgent not syncing
# back to SharedState. Read trades.csv (TradeLogger source-of-truth) for today.
if not today_trades:
    today_trades = load_trades_today_from_csv()
    if today_trades:
        st.caption(f"Loaded {len(today_trades)} trades from trades.csv (state.json was empty)")
if today_trades:
    df_trades = pd.DataFrame(today_trades)
    if "pnl" in df_trades.columns:
        total = df_trades["pnl"].sum()
        wins  = (df_trades["pnl"] > 0).sum()
        wr    = wins / len(df_trades) * 100

        t1, t2, t3 = st.columns(3)
        t1.metric("Total P&L", f"₹{total:,.0f}")
        t2.metric("Win Rate", f"{wr:.0f}%")
        t3.metric("Trades", len(df_trades))

    df_trades["pnl_fmt"] = df_trades["pnl"].map(lambda x: f"₹{x:+,.0f}")

    def color_pnl(val):
        if "+" in str(val):
            return "color: #00c853"
        elif "-" in str(val):
            return "color: #ff1744"
        return ""

    display_cols = [c for c in ["symbol", "direction", "reason", "pnl_fmt"] if c in df_trades.columns]
    st.dataframe(
        df_trades[display_cols].rename(columns={"pnl_fmt": "P&L"})
        .style.map(color_pnl, subset=["P&L"]),
        width='stretch',
        hide_index=True,
    )
else:
    st.info("No trades yet today")

st.divider()

# ── Row 5: Agent list + performance history ───────────────────────────────────

col_agents, col_perf = st.columns([2, 3])

with col_agents:
    st.subheader("Active Agents")
    agents = state.get("agents", [])
    if agents:
        for a in agents:
            st.markdown(f"🤖 **{a}**")
    else:
        st.info("Runner not started")

with col_perf:
    st.subheader("Performance History")
    if perf:
        history = perf.get("history", [])
        if history:
            df_perf = pd.DataFrame(history)
            if "date" in df_perf.columns and "pnl" in df_perf.columns:
                df_perf = df_perf.sort_values("date").tail(30)
                st.line_chart(df_perf.set_index("date")["pnl"])
        else:
            st.info("No performance history yet")
    else:
        st.info("No performance data yet")

st.divider()

# ── Row 6: Live notifications feed ────────────────────────────────────────────

st.subheader("Live Alerts")

NOTIF_ICON = {
    "TRADE_ENTERED":  "🟢",
    "TRADE_CLOSED":   "🔴",
    "REGIME_CHANGED": "⚠️",
    "DAILY_SUMMARY":  "📊",
    "HALT":           "🚫",
}

NOTIF_BG = {
    "TRADE_ENTERED":  "#1a3a1a",
    "TRADE_CLOSED":   "#2a1a1a",
    "REGIME_CHANGED": "#2a2a10",
    "DAILY_SUMMARY":  "#1a1a2a",
    "HALT":           "#3a1a1a",
}

if notifs:
    # Show most recent first, last 20 events
    recent = list(reversed(notifs[-20:]))
    for n in recent:
        evt   = n.get("type", "")
        icon  = NOTIF_ICON.get(evt, "ℹ️")
        bg    = NOTIF_BG.get(evt, "#1a1a1a")
        sym   = n.get("symbol", "")
        pnl   = n.get("pnl", 0)
        ts_raw = n.get("ts", "")

        # Format timestamp
        try:
            ts_dt = datetime.fromisoformat(ts_raw)
            ts_label = ts_dt.strftime("%H:%M:%S")
        except Exception:
            ts_label = ts_raw[:19]

        # Format P&L badge
        pnl_badge = ""
        if evt == "TRADE_CLOSED" and pnl != 0:
            sign = "+" if pnl >= 0 else ""
            color = "#00c853" if pnl >= 0 else "#ff1744"
            pnl_badge = f' <span style="color:{color};font-weight:bold">₹{sign}{pnl:,.0f}</span>'

        # Message uses HTML (<b>, \n) — keep tags, convert newlines to <br>
        raw_msg = n.get("message", "").replace("\n", "<br>")

        sym_badge = f'&nbsp;&middot;&nbsp;<b>{sym}</b>' if sym else ''
        html = (
            f'<div style="background:{bg};border-radius:6px;padding:8px 12px;margin-bottom:6px;">'
            f'<span style="color:#888;font-size:12px">{ts_label}</span>&nbsp;'
            f'{icon} <b>{evt.replace("_", " ")}</b>{sym_badge}{pnl_badge}'
            f'<br><span style="font-size:13px;color:#ccc">{raw_msg}</span>'
            f'</div>'
        )
        st.markdown(html, unsafe_allow_html=True)
else:
    st.info("No alerts yet — alerts appear here as trades happen")

# Footer
st.caption(f"State: {STATE_FILE} | Alerts: {NOTIF_FILE} | Refreshes every 5s")
