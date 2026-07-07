"""
swing_screen.py — daily EOD swing-trade screen (the 7-gate framework).

Gate 1  regime: NIFTY must be above its 200-DMA, else NO new longs.
Gate 2  universe: TOP100_LIQUID (measured turnover ranking).
Gate 3  setup: stock in its own uptrend (close > rising 200-DMA) with a fresh
        pullback (2% under 5-DMA, or 3 down closes, or RSI-2 < 10).
Gate 4  geometry printed per candidate: entry next open, target 2xATR,
        stop 2xATR, time exit 10 sessions.
Gate 5  cost hurdle: 2xATR move must be >= 2% (~8x the 0.25% cash cost).

Evidence base (2011-2026, 100 stocks, net of costs): PF ~1.10-1.14,
+21-33bp/trade, positive in every era but never statistically significant,
survivors-only upper bound. Sleeve-grade discipline, NOT validated alpha.
Cap the sleeve at <=10% of capital; risk <=1% per trade.

    python swing_screen.py            # screen on latest data (fetches ~2y bars)
    python swing_screen.py --json     # also write logs/swing_screen.json
"""

from __future__ import annotations

import argparse
import json
import os
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from core.universe import TOP100_LIQUID

YMAP = {"MCDOWELL-N": "UNITDSPR"}   # NSE name -> yfinance base symbol
MIN_TARGET_PCT = 2.0                # gate 5: 2xATR must clear this


def _rsi(series: pd.Series, n: int = 2) -> pd.Series:
    d = series.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def fetch(period: str = "2y") -> dict:
    import yfinance as yf
    tickers = {s: YMAP.get(s, s) + ".NS" for s in TOP100_LIQUID}
    raw = yf.download(list(tickers.values()), period=period, group_by="ticker",
                      auto_adjust=True, threads=True, progress=False)
    out = {}
    for sym, tk in tickers.items():
        try:
            d = raw[tk][["Open", "High", "Low", "Close", "Volume"]].dropna()
            if len(d) >= 260:
                d.columns = ["open", "high", "low", "close", "volume"]
                out[sym] = d
        except Exception:
            pass
    return out


def regime() -> tuple:
    import yfinance as yf
    n = yf.download("^NSEI", period="2y", auto_adjust=True, progress=False)["Close"]
    if isinstance(n, pd.DataFrame):
        n = n.iloc[:, 0]
    ma = n.rolling(200).mean()
    return float(n.iloc[-1]), float(ma.iloc[-1])


def screen(data: dict) -> list:
    cands = []
    for sym, df in data.items():
        tr = np.maximum(df.high - df.low,
             np.maximum((df.high - df.close.shift()).abs(),
                        (df.low - df.close.shift()).abs()))
        atr = tr.rolling(14).mean()
        ma200 = df.close.rolling(200).mean()
        ma5 = df.close.rolling(5).mean()
        rsi2 = _rsi(df.close, 2)
        dn3 = (df.close.diff() < 0).rolling(3).sum() == 3
        c, a, m2, m5 = df.close.iloc[-1], atr.iloc[-1], ma200.iloc[-1], ma5.iloc[-1]
        if pd.isna(a) or pd.isna(m2):
            continue
        uptrend = c > m2 and m2 > ma200.iloc[-21]          # rising 200-DMA
        pullback = (c < m5 * 0.98) or bool(dn3.iloc[-1]) or rsi2.iloc[-1] < 10
        tgt_pct = 2 * a / c * 100
        if uptrend and pullback and tgt_pct >= MIN_TARGET_PCT:
            cands.append({
                "symbol": sym, "close": round(float(c), 2),
                "target": round(float(c + 2 * a), 2),
                "stop": round(float(c - 2 * a), 2),
                "target_pct": round(float(tgt_pct), 2),
                "bar": str(df.index[-1].date()),
            })
    cands.sort(key=lambda x: -x["target_pct"])
    return cands


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="write logs/swing_screen.json")
    args = ap.parse_args()

    nifty, ma = regime()
    risk_on = nifty > ma
    print("=" * 66)
    print("SWING SCREEN - 7-gate framework (pullback in uptrend)")
    print("=" * 66)
    print(f"Gate 1  NIFTY {nifty:.0f} vs 200DMA {ma:.0f} "
          f"({(nifty/ma-1)*100:+.2f}%)  ->  "
          f"{'RISK-ON: longs allowed' if risk_on else 'RISK-OFF: NO NEW LONGS'}")

    data = fetch()
    cands = screen(data)
    print(f"Gates 2-5  {len(cands)} candidates from {len(data)} liquid names\n")
    hdr = f"{'#':>2} {'SYMBOL':<14}{'CLOSE':>10}{'TARGET':>10}{'STOP':>10}{'MOVE%':>7}"
    print(hdr); print("-" * len(hdr))
    for i, x in enumerate(cands[:15], 1):
        print(f"{i:>2} {x['symbol']:<14}{x['close']:>10}{x['target']:>10}"
              f"{x['stop']:>10}{x['target_pct']:>6.1f}%")
    print()
    if risk_on:
        print("Entry: NEXT OPEN. Exit: target/stop above, or 10 sessions.")
        print("Risk <=1% of sleeve per trade, max 4-5 concurrent, sleeve <=10% capital.")
    else:
        print("Gate 1 CLOSED -> watchlist only. Re-run after a close above the 200DMA.")

    if args.json:
        os.makedirs("logs", exist_ok=True)
        with open(os.path.join("logs", "swing_screen.json"), "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.now().isoformat(timespec="seconds"),
                       "risk_on": risk_on, "nifty": round(nifty, 1),
                       "ma200": round(ma, 1), "candidates": cands}, f, indent=2)
        print("written -> logs/swing_screen.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
