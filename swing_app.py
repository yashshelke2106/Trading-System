"""
swing_app.py — NO-API swing-trade finder (Streamlit).

Zero credentials required: free yfinance daily bars only. This is the
swing interface; the full terminal (start_trading.bat -> :3000) is the
API-powered interface for live/option features.

    streamlit run swing_app.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from swing_screen import MIN_TARGET_PCT, fetch, regime, screen

st.set_page_config(page_title="Swing Finder (no API)", page_icon="📈",
                   layout="wide")

st.title("📈 Swing Finder — 7-gate framework")
st.caption("No API keys needed — free daily data (yfinance). "
           "Entries at NEXT open · target 2×ATR · stop 2×ATR · time exit 10 sessions.")


@st.cache_data(ttl=900, show_spinner="Fetching NIFTY regime…")
def _regime():
    return regime()


@st.cache_data(ttl=900, show_spinner="Fetching ~2y bars for 100 liquid F&O stocks…")
def _candidates():
    return screen(fetch())


nifty, ma = _regime()
dist = (nifty / ma - 1) * 100
risk_on = nifty > ma

c1, c2, c3 = st.columns(3)
c1.metric("NIFTY", f"{nifty:,.0f}")
c2.metric("200-DMA", f"{ma:,.0f}", f"{dist:+.2f}% vs line")
c3.metric("Gate 1 · Regime",
          "RISK-ON ✅" if risk_on else "RISK-OFF 🛑",
          "longs allowed" if risk_on else "no new longs")

if risk_on:
    st.success("Gate 1 OPEN — candidates below are tradeable at next open. "
               "Risk ≤1% of sleeve per trade · max 4–5 positions · sleeve ≤10% of capital.")
else:
    st.warning(f"Gate 1 CLOSED — NIFTY is {abs(dist):.2f}% below its 200-DMA. "
               "The list below is a WATCHLIST for the flip, not trades today.")

cands = _candidates()
st.subheader(f"Gates 2–5 · {len(cands)} pullback-in-uptrend candidates "
             f"(target ≥ {MIN_TARGET_PCT}% move)")

if cands:
    df = pd.DataFrame(cands)[["symbol", "close", "target", "stop", "target_pct", "bar"]]
    df.columns = ["Symbol", "Close", "Target (2×ATR)", "Stop (2×ATR)", "Move %", "Bar date"]
    df.index = range(1, len(df) + 1)
    st.dataframe(df, use_container_width=True,
                 column_config={"Move %": st.column_config.NumberColumn(format="%.1f%%")})
    st.download_button("Download CSV", df.to_csv().encode(),
                       "swing_candidates.csv", "text/csv")
else:
    st.info("No setups today — most days that's the correct answer. Patience is Gate 7.")

with st.expander("The 7 gates & the evidence (read once)"):
    st.markdown("""
| Gate | Rule |
|---|---|
| 1 | **Regime**: NIFTY > 200-DMA, else no new longs |
| 2 | **Universe**: top-100 F&O by measured turnover only |
| 3 | **Setup**: stock above its own *rising* 200-DMA + fresh pullback (2% under 5-DMA / 3 down closes / RSI-2 < 10) |
| 4 | **Geometry**: enter next open · target 2×ATR · stop 2×ATR · exit by 10 sessions — never buy accuracy with tight targets |
| 5 | **Cost hurdle**: 2×ATR move ≥ 2% (≈8× the 0.25% delivery cost) |
| 6 | **Sizing**: risk ≤1% per trade · ≤5 concurrent · sleeve ≤10% of capital |
| 7 | **Verdict**: judge on net rupees after 30 trades; ≤0 → stop |

**Honest evidence (2011–2026, 100 stocks, ~9,000 trades, net of costs):**
PF 1.10–1.14, +21–33bp/trade, positive in every era but never statistically
significant, on a survivors-only universe (upper bound). This is
**sleeve-grade discipline, not validated alpha** — the core capital belongs
in the allocation strategy (Allocation tab of the full terminal).
""")

st.caption("Data: yfinance EOD (free, delayed). Refresh: press R or rerun — cache is 15 min. "
           "Execution is manual in your broker app; nothing here places orders.")
