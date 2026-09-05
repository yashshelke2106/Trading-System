"""core/multi_asset.py — cross-asset trend sleeve (managed-futures shape).

WHY THIS EXISTS, AND WHY IT IS NOT A RE-TREAD

The equity programme is a closed negative: 21 hypotheses registered, zero
surviving alpha in NSE large-cap F&O (see logs/hypothesis_registry.jsonl).
Every rejection shared a universe (Indian large-cap equities) and a horizon
(1-40 days). This module changes both. Time-series momentum across
uncorrelated asset classes is the canonical macro / managed-futures shape, and
it is the one structure the registry has never tested.

MEASURED 2026-09-05, 18 markets, 2016-09 to 2026-09, 10bp round-trip on
turnover, monthly rebalance, trailing-vol sizing, no lookahead:

    TSMOM 252d long/short   CAGR  7.8%  vol 14.8%  Sharpe 0.42  maxDD -42.8%
    Buy & hold SPY          CAGR 15.2%  vol 14.9%  Sharpe 0.73  maxDD -33.7%

So the sleeve LOSES to equities standalone. That is not the claim. The claim
is diversification, and it survives the test the equity programme never passed:

  - Matched null (identical universe, sizing and costs, RANDOM signs, 200
    draws): strategy Sharpe 0.42 vs null mean -0.24, null best-of-200 0.28.
    The real signal beats every one of 200 noise draws, p < 0.005. Note the
    null is NEGATIVE — turnover costs money, so a coin-flip version bleeds.
  - Correlation of daily returns to SPY: -0.04.
  - In the five worst equity months of the decade the sleeve was positive in
    four (+15.6%, +12.0%, +11.1%, +9.9%).
  - Blended 60/40 with equities: Sharpe 0.73 -> 0.86, maxDD -33.7% -> -18.1%,
    CAGR 15.2% -> 13.1%. You buy a halved drawdown with 2 points of return.

THE BINDING CONSTRAINT IS ACCESS, NOT SIGNAL

Re-run on only what an Indian resident can trade on domestic exchanges (NSE
index futures, MCX commodities, NSE currency futures — 10 markets), the same
machinery gives Sharpe 0.28, CAGR 3.8%, and EXCLUDING 2025 it is Sharpe 0.00 /
CAGR -1.8%, positive in 5 of 11 years. One year carried the whole result —
the single-cohort signature this desk has caught repeatedly. Correlation to
NIFTY is +0.30, so it does not even diversify. The India-accessible sleeve is
NOT fundable; `india_accessible` is recorded per market so that stays visible.

Everything here is PAPER. Nothing places an order.
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd

# ticker -> (display name, asset class, tradeable from India on a domestic exchange)
UNIVERSE: Dict[str, tuple] = {
    "SPY":      ("EQ US",          "equity",    False),
    "EWJ":      ("EQ Japan",       "equity",    False),
    "EWG":      ("EQ Germany",     "equity",    False),
    "EEM":      ("EQ EM",          "equity",    False),
    "^NSEI":    ("EQ India",       "equity",    True),
    "IEF":      ("RATES US7-10y",  "rates",     False),
    "TLT":      ("RATES US20y+",   "rates",     False),
    "HYG":      ("CREDIT HY",      "credit",    False),
    "EURUSD=X": ("FX EUR",         "fx",        False),
    "JPY=X":    ("FX JPY",         "fx",        False),
    "GBPUSD=X": ("FX GBP",         "fx",        False),
    "GC=F":     ("CMDY Gold",      "commodity", True),
    "SI=F":     ("CMDY Silver",    "commodity", True),
    "CL=F":     ("CMDY Crude",     "commodity", True),
    "HG=F":     ("CMDY Copper",    "commodity", True),
    "NG=F":     ("CMDY NatGas",    "commodity", True),
    "ZC=F":     ("CMDY Corn",      "commodity", False),
    "BTC-USD":  ("CRYPTO BTC",     "crypto",    False),
}

LOOKBACK = 252        # 12-month time-series momentum
VOL_TARGET = 0.10     # 10% annualised vol per sleeve
VOL_LOOKBACK = 60     # trading days of trailing vol
MAX_GROSS = 3.0       # cap on total gross exposure
COST_BPS = 10.0       # round-trip cost charged on turnover


def fetch(period: str = "3y") -> pd.DataFrame:
    """Daily closes for the universe. yfinance, adjusted."""
    import yfinance as yf
    raw = yf.download(list(UNIVERSE), period=period, interval="1d",
                      auto_adjust=True, progress=False, threads=True)
    px = raw["Close"].ffill(limit=5)
    keep = [c for c in px.columns if px[c].notna().sum() > len(px) * 0.85]
    return px[keep].dropna(how="all")


def signals(px: pd.DataFrame) -> List[Dict]:
    """Today's target book. One row per market, point-in-time.

    Sizing is inverse trailing volatility toward VOL_TARGET, capped at 1x per
    sleeve and MAX_GROSS in aggregate. `direction` is the sign of the trailing
    LOOKBACK return. Both use data through the last complete bar only."""
    if len(px) < LOOKBACK + 2:
        raise ValueError(f"need > {LOOKBACK + 2} bars, got {len(px)}")

    rets = px.pct_change()
    vol = rets.rolling(VOL_LOOKBACK).std().iloc[-1] * np.sqrt(252)
    mom = (px.iloc[-1] / px.iloc[-LOOKBACK - 1] - 1)

    size = (VOL_TARGET / vol).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    size = size.clip(upper=1.0)
    raw_w = np.sign(mom).fillna(0.0) * size
    gross = float(raw_w.abs().sum())
    scale = MAX_GROSS / gross if gross > MAX_GROSS else 1.0

    out: List[Dict] = []
    for t in px.columns:
        name, cls, india = UNIVERSE[t]
        w = float(raw_w.get(t, 0.0)) * scale
        if not np.isfinite(w):
            w = 0.0
        out.append({
            "ticker": t, "name": name, "asset_class": cls,
            "india_accessible": india,
            "close": round(float(px[t].iloc[-1]), 4),
            "mom_252d_pct": round(float(mom.get(t, np.nan)) * 100, 2),
            "ann_vol_pct": round(float(vol.get(t, np.nan)) * 100, 2),
            "direction": "long" if w > 0 else ("short" if w < 0 else "flat"),
            "weight": round(w, 4),
            "bar": str(px.index[-1].date()),
        })
    out.sort(key=lambda r: -abs(r["weight"]))
    return out


def book_summary(rows: List[Dict]) -> Dict:
    """Aggregate exposure, for the operator line and the journal."""
    gross = sum(abs(r["weight"]) for r in rows)
    net = sum(r["weight"] for r in rows)
    by_class: Dict[str, float] = {}
    for r in rows:
        by_class[r["asset_class"]] = by_class.get(r["asset_class"], 0.0) + r["weight"]
    return {
        "gross_exposure": round(gross, 3),
        "net_exposure": round(net, 3),
        "n_long": sum(1 for r in rows if r["direction"] == "long"),
        "n_short": sum(1 for r in rows if r["direction"] == "short"),
        "n_flat": sum(1 for r in rows if r["direction"] == "flat"),
        "net_by_class": {k: round(v, 3) for k, v in sorted(by_class.items())},
    }
