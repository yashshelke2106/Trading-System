"""
swing_app.py — NO-API swing-trade finder (Streamlit). Zero credentials —
free yfinance daily bars. Direction-symmetric framework + outcome learner.

    streamlit run swing_app.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from core.swing_learner import SwingLearner
from swing_screen import MIN_TARGET_PCT, fetch, regime, screen

st.set_page_config(page_title="Swing Finder (no API)", page_icon="📈",
                   layout="wide")

st.title("📈 Swing Finder — symmetric 7-gate framework")
st.caption("No API keys — free daily data. Long AND short rules are exact mirrors; "
           "the regime gate decides which side is active. Entries at NEXT open · "
           "target 2×ATR · stop 2×ATR · time exit 10 sessions.")


@st.cache_data(ttl=900, show_spinner="Fetching NIFTY regime…")
def _regime():
    return regime()


@st.cache_data(ttl=900, show_spinner="Fetching ~2y bars for 100 liquid F&O stocks…")
def _candidates(regime_state: str):
    return screen(fetch(), SwingLearner(), regime_state)


nifty, ma = _regime()
dist = (nifty / ma - 1) * 100
risk_on = nifty > ma
regime_state = "risk_on" if risk_on else "risk_off"
allowed = "long" if risk_on else "short"

c1, c2, c3 = st.columns(3)
c1.metric("NIFTY", f"{nifty:,.0f}")
c2.metric("200-DMA", f"{ma:,.0f}", f"{dist:+.2f}% vs line")
c3.metric("Gate 1 · Regime", "RISK-ON" if risk_on else "RISK-OFF",
          f"{allowed.upper()} side active")

if allowed == "short":
    st.error("🛑 **SHORTS ARE PAPER-ONLY — do not fund.** 15-year evidence "
             "(2,816 trades, all 100 stocks): shorting rallies in downtrends "
             "loses **−125 bp/trade, PF 0.65** even at futures costs. "
             "Risk-off = **stand aside in cash**. The learner keeps testing "
             "shorts on paper; the money waits for the long side.")

cands = _candidates(regime_state)
tradeable = [x for x in cands if x["direction"] == allowed]
blocked = [x for x in cands if x["direction"] != allowed]

st.subheader(f"Tradeable now — {len(tradeable)} {allowed.upper()} candidates "
             f"(target ≥ {MIN_TARGET_PCT}%, ranked by move × learner weight)")

def _table(rows):
    df = pd.DataFrame(rows)[["symbol", "direction", "signal", "close",
                             "target", "stop", "target_pct", "weight", "bar"]]
    df.columns = ["Symbol", "Dir", "Signal", "Close", "Target", "Stop",
                  "Move %", "Learner W", "Bar date"]
    df.index = range(1, len(df) + 1)
    return df

if tradeable:
    st.dataframe(_table(tradeable), use_container_width=True,
                 column_config={"Move %": st.column_config.NumberColumn(format="%.1f%%")})
    st.download_button("Download CSV", _table(tradeable).to_csv().encode(),
                       "swing_candidates.csv", "text/csv")
else:
    st.info("No tradeable setups today — most days that is the correct answer.")

if blocked:
    with st.expander(f"Blocked by regime gate: {len(blocked)} "
                     f"{blocked[0]['direction']} setups (watchlist)"):
        st.dataframe(_table(blocked), use_container_width=True)

st.subheader("📒 Paper trades (journal → tracker → learner)")
import json as _json
import os as _os

from swing_screen import JOURNAL_FILE as _JF

if _os.path.exists(_JF):
    _rows = []
    for _line in open(_JF, encoding="utf-8"):
        try:
            _rows.append(_json.loads(_line))
        except Exception:
            pass
    if _rows:
        _j = pd.DataFrame(_rows)
        _open = _j[_j.status == "open"]
        _res = _j[_j.status == "resolved"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Open", len(_open))
        m2.metric("Resolved", len(_res))
        if len(_res):
            _net = _res.ret_net.astype(float)
            m3.metric("Win rate", f"{(_net > 0).mean()*100:.0f}%")
            m4.metric("Net P&L (sum)", f"{_net.sum()*100:+.2f}%")
        _tab1, _tab2 = st.tabs([f"Open ({len(_open)})", f"Resolved ({len(_res)})"])
        with _tab1:
            if len(_open):
                _o = _open[["symbol", "direction", "signal", "signal_date",
                            "close", "target", "stop"]].copy()
                _o.columns = ["Symbol", "Dir", "Signal", "Signal date",
                              "Ref close", "Target", "Stop"]
                _o.index = range(1, len(_o) + 1)
                st.dataframe(_o, use_container_width=True)
            else:
                st.info("No open paper trades — run `python swing_screen.py --journal`.")
        with _tab2:
            if len(_res):
                _r = _res[["symbol", "direction", "signal", "signal_date",
                           "entry_px", "exit_px", "exit_date", "outcome",
                           "ret_net"]].copy()
                _r["ret_net"] = (_r.ret_net.astype(float) * 100).round(2)
                _r.columns = ["Symbol", "Dir", "Signal", "Signal date", "Entry",
                              "Exit", "Exit date", "Outcome", "Net %"]
                _r.index = range(1, len(_r) + 1)
                st.dataframe(_r.sort_values("Exit date", ascending=False),
                             use_container_width=True)
            else:
                st.info("Nothing resolved yet — trades resolve within 10 sessions "
                        "(`python swing_tracker.py`, or the 16:30 autopilot).")
else:
    st.info("No journal yet — run `python swing_screen.py --journal` once, "
            "or wait for the 16:30 autopilot.")

with st.expander("🧠 Learner state — what the system has learned so far"):
    st.text(SwingLearner().report())
    st.caption("Weights move off 1.00 only after ≥20 resolved trades in a bucket "
               "AND the Wilson 95% interval clears 50% — it cannot learn noise. "
               "Feed it daily: `python swing_screen.py --journal` then "
               "`python swing_tracker.py`.")

with st.expander("The 7 gates & the evidence (read once)"):
    st.markdown("""
| Gate | Rule (identical for both directions) |
|---|---|
| 1 | **Regime**: NIFTY vs 200-DMA picks the active side — never fight it |
| 2 | **Universe**: top-100 F&O by measured turnover |
| 3 | **Setup**: with-trend stock + fresh counter-move (pullback/rally) |
| 4 | **Geometry**: next-open entry · 2×ATR target & stop · 10-session exit |
| 5 | **Cost hurdle**: 2×ATR ≥ 2% (longs 0.25% cash / shorts 0.10% futures) |
| 6 | **Sizing**: ≤1% risk per trade · ≤5 concurrent · sleeve ≤10% of capital |
| 7 | **Verdict**: judge on net rupees after 30 trades; ≤0 → stop |

**Evidence (2011–2026, 100 stocks, net of costs):** PF 1.10–1.14, +21–33bp/trade,
positive every era, never statistically significant, survivors-only upper bound.
**Sleeve-grade discipline, not validated alpha** — core capital belongs in the
allocation strategy.
""")

st.caption("Data: yfinance EOD (free, delayed). Cache 15 min. Nothing here places orders.")
