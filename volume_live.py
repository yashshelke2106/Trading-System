"""
Live F&O volume dashboard — two tables:
  Table 1: Cumulative spike  — total vol from 9:15 to now vs 20d avg pace
  Table 2: 5-min surge       — vol in last 5 bars vs previous 5 bars

Fetches live 1-min bars from Dhan every 5s. UI refreshes every 1s.

Run:
    streamlit run volume_live.py
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from core import secrets as sec
from core.api_dhan import DhanAPI, _token_expiry
from core.universe import FO_UNIVERSE

MARKET_OPEN_MIN  = 9 * 60 + 15   # 9:15 in minutes since midnight
MARKET_TOTAL_MIN = 375            # 9:15 → 15:30

st.set_page_config(
    page_title="F&O Volume Scanner",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ── Token health ───────────────────────────────────────────────────────────────

def _token_hours_left():
    t   = sec.get_access_token()
    exp = _token_expiry(t)
    if exp is None:
        return None
    return (exp - datetime.utcnow()).total_seconds() / 3600


def _minutes_elapsed() -> float:
    now = datetime.now()
    elapsed = now.hour * 60 + now.minute - MARKET_OPEN_MIN
    return max(elapsed, 1.0)


# ── Data (process-level cache shared across every 1s rerun) ───────────────────

@st.cache_data(ttl=1800, show_spinner=False)
def _get_baseline() -> dict:
    """20-day average daily volume per symbol."""
    api = DhanAPI()

    def fetch(sym):
        try:
            df = api.get_historical_data(sym, from_date=25)
            if df is not None and len(df) >= 20:
                return sym, float(df["volume"].rolling(20).mean().iloc[-1])
        except Exception:
            pass
        return sym, 0.0

    out = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for sym, avg in pool.map(fetch, FO_UNIVERSE):
            out[sym] = avg
    return out


@st.cache_data(ttl=5, show_spinner=False)
def _get_live() -> dict:
    """
    Per symbol from today's 1-min bars:
      cumvol   — sum of all bars today (total from open)
      last5    — sum of last 5 bars (last 5 min volume)
      prev5    — sum of bars[-10:-5] (previous 5 min volume)
      price    — latest close
    """
    api = DhanAPI()

    def fetch(sym):
        try:
            df = api.get_intraday_data(sym, interval=1, days_back=1)
            if df is None or df.empty:
                return sym, None

            # Keep only today's bars
            today = datetime.now().date()
            if hasattr(df["date"].iloc[0], "date"):
                df = df[df["date"].dt.date == today]
            elif "date" in df.columns:
                df = df[pd.to_datetime(df["date"]).dt.date == today]

            if df.empty:
                return sym, None

            vols   = df["volume"].values
            cumvol = int(vols.sum())
            last5  = int(vols[-5:].sum())  if len(vols) >= 5  else int(vols.sum())
            prev5  = int(vols[-10:-5].sum()) if len(vols) >= 10 else 0
            price  = float(df["close"].iloc[-1])

            return sym, {
                "price":  price,
                "cumvol": cumvol,
                "last5":  last5,
                "prev5":  prev5,
            }
        except Exception:
            pass
        return sym, None

    out = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        for sym, data in pool.map(fetch, FO_UNIVERSE):
            if data:
                out[sym] = data
    return out


# ── Styling ────────────────────────────────────────────────────────────────────

def _style_ratio(val):
    if not isinstance(val, float):
        return ""
    if val >= 2.0:
        return "background-color:#1a3a1a;color:#00e676;font-weight:bold"
    if val >= 1.5:
        return "background-color:#2a2510;color:#ffd600"
    return ""


# ── Page ──────────────────────────────────────────────────────────────────────

st.title("📊 F&O Live Volume Scanner")

# Token banner
hours = _token_hours_left()
if hours is not None:
    if hours <= 0:
        st.error("Dhan token **EXPIRED** — refresh it from the main dashboard sidebar.")
    elif hours <= 6:
        st.warning(f"Token expires in **{hours:.1f}h** — refresh it from the dashboard soon.")

if config.USE_MOCK_DATA:
    st.error("USE_MOCK_DATA = True — set False in config.py for live data.")

elapsed_min = _minutes_elapsed()
c1, c2 = st.columns([3, 2])
with c1:
    st.markdown(f"**{datetime.now().strftime('%H:%M:%S')}** &nbsp;|&nbsp; "
                f"{elapsed_min:.0f} min since open &nbsp;|&nbsp; Dhan live feed",
                unsafe_allow_html=True)
with c2:
    st.caption(f"{len(FO_UNIVERSE)} F&O stocks  ·  cumulative spike ≥2x  ·  5-min surge ≥1.5x")

st.divider()

# Load data
baseline = _get_baseline()
live     = _get_live()

if not live:
    st.error("No data. Check Dhan credentials.")
    st.stop()

# ── Build both row sets ────────────────────────────────────────────────────────

cum_rows  = []   # table 1: cumulative spike
surge_rows = []  # table 2: 5-min surge

for sym in FO_UNIVERSE:
    avg  = baseline.get(sym, 0)
    data = live.get(sym)
    if data is None:
        continue

    price  = data["price"]
    cumvol = data["cumvol"]
    last5  = data["last5"]
    prev5  = data["prev5"]

    # Table 1: compare cumulative vol vs expected pace (20d avg × elapsed fraction)
    expected_so_far = avg * (elapsed_min / MARKET_TOTAL_MIN) if avg > 0 else 0
    cum_ratio = round(cumvol / expected_so_far, 2) if expected_so_far > 0 else 0.0

    cum_rows.append({
        "Symbol":        sym,
        "Price (₹)":     price,
        "Vol Since Open": cumvol,
        "Expected Pace": int(expected_so_far),
        "Ratio":         cum_ratio,
        "":              "🔥" if cum_ratio >= 2.0 else ("📈" if cum_ratio >= 1.5 else ""),
    })

    # Table 2: last 5 bars vs previous 5 bars
    surge_ratio = round(last5 / prev5, 2) if prev5 > 0 else 0.0

    surge_rows.append({
        "Symbol":       sym,
        "Price (₹)":    price,
        "Last 5-min Vol": last5,
        "Prev 5-min Vol": prev5,
        "5-min Ratio":  surge_ratio,
        "":             "🔥" if surge_ratio >= 2.0 else ("📈" if surge_ratio >= 1.5 else ""),
    })

df_cum   = pd.DataFrame(cum_rows).sort_values("Ratio", ascending=False)
df_surge = pd.DataFrame(surge_rows).sort_values("5-min Ratio", ascending=False)

# ── Metrics strip ──────────────────────────────────────────────────────────────

spikes_cum   = (df_cum["Ratio"]      >= 2.0).sum()
spikes_surge = (df_surge["5-min Ratio"] >= 2.0).sum()

m1, m2, m3, m4 = st.columns(4)
m1.metric("🔥 Cumul. Spikes (≥2x)",  spikes_cum)
m2.metric("🔥 5-min Surges (≥2x)",   spikes_surge)
m3.metric("📈 Cumul. Elevated (1.5x)", int((df_cum["Ratio"].between(1.5, 2.0)).sum()))
m4.metric("📈 5-min Elevated (1.5x)",  int((df_surge["5-min Ratio"].between(1.5, 2.0)).sum()))

st.divider()

# ── Two-table layout ───────────────────────────────────────────────────────────

col_left, col_right = st.columns(2)

with col_left:
    st.subheader("📦 Total Volume Spike (Market Open → Now)")
    st.caption("Cumulative volume vs expected pace based on 20d avg")
    st.dataframe(
        df_cum.style.map(_style_ratio, subset=["Ratio"]),
        use_container_width=True,
        hide_index=True,
        height=min(50 + len(df_cum) * 35, 650),
    )

with col_right:
    st.subheader("⚡ Most Increasing Volume (Last 5 Min)")
    st.caption("Last 5-min volume vs previous 5-min window")
    st.dataframe(
        df_surge.style.map(_style_ratio, subset=["5-min Ratio"]),
        use_container_width=True,
        hide_index=True,
        height=min(50 + len(df_surge) * 35, 650),
    )

# ── 1-second auto-refresh ─────────────────────────────────────────────────────
time.sleep(1)
st.rerun()
