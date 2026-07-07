"""
swing_screen.py — daily EOD swing screen (7-gate framework), DIRECTION-
SYMMETRIC, with a reinforcement-style learner re-ranking candidates.

LONGS : NIFTY > 200-DMA, stock above its rising 200-DMA, fresh pullback
        (2% under 5-DMA / 3 down closes / RSI-2 < 10).
SHORTS: exact mirror — NIFTY < 200-DMA, stock below its falling 200-DMA,
        fresh rally (2% over 5-DMA / 3 up closes / RSI-2 > 90).
        NOTE: Indian cash market allows no overnight retail shorts —
        short swings execute via STOCK FUTURES (cost ~0.10% RT, lot sizes).

Geometry both ways: enter NEXT open, target 2xATR, stop 2xATR, 10-session
time exit. Learner (core/swing_learner.py) multiplies each candidate's rank
by its bucket weight — buckets earn/lose trust from resolved outcomes only.

    python swing_screen.py                # screen + ranked candidates
    python swing_screen.py --json         # also write logs/swing_screen.json
    python swing_screen.py --journal      # also journal today's tradeable
                                          # candidates for paper tracking
    python swing_screen.py --learner      # print learner bucket report
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

from core.swing_learner import SwingLearner
from core.universe import TOP100_LIQUID

YMAP = {"MCDOWELL-N": "UNITDSPR"}
MIN_TARGET_PCT = 2.0
JOURNAL_FILE = os.path.join("logs", "swing_paper_journal.jsonl")


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


def screen(data: dict, learner: SwingLearner = None, regime_state: str = "risk_on") -> list:
    """Symmetric candidate scan. Returns BOTH directions; caller applies the
    regime gate. Rank = target% x learner bucket weight."""
    learner = learner or SwingLearner()
    cands = []
    for sym, df in data.items():
        tr = np.maximum(df.high - df.low,
             np.maximum((df.high - df.close.shift()).abs(),
                        (df.low - df.close.shift()).abs()))
        atr = tr.rolling(14).mean()
        ma200 = df.close.rolling(200).mean()
        ma5 = df.close.rolling(5).mean()
        rsi2 = _rsi(df.close, 2)
        dchg = df.close.diff()
        dn3 = (dchg < 0).rolling(3).sum() == 3
        up3 = (dchg > 0).rolling(3).sum() == 3
        c, a = df.close.iloc[-1], atr.iloc[-1]
        m2, m5v = ma200.iloc[-1], ma5.iloc[-1]
        if pd.isna(a) or pd.isna(m2):
            continue
        tgt_pct = 2 * a / c * 100
        if tgt_pct < MIN_TARGET_PCT:
            continue

        # identical logic, mirrored — no direction is privileged
        setups = []
        if c > m2 and m2 > ma200.iloc[-21]:            # own uptrend
            if c < m5v * 0.98: setups.append(("long", "2pct_under_5dma"))
            if bool(dn3.iloc[-1]): setups.append(("long", "3_down_days"))
            if rsi2.iloc[-1] < 10: setups.append(("long", "rsi2_oversold"))
        if c < m2 and m2 < ma200.iloc[-21]:            # own downtrend
            if c > m5v * 1.02: setups.append(("short", "2pct_over_5dma"))
            if bool(up3.iloc[-1]): setups.append(("short", "3_up_days"))
            if rsi2.iloc[-1] > 90: setups.append(("short", "rsi2_overbought"))

        for direction, signal in setups[:1]:           # one candidate per symbol
            sign = 1 if direction == "long" else -1
            w = learner.weight(direction, signal, regime_state)
            cands.append({
                "symbol": sym, "direction": direction, "signal": signal,
                "close": round(float(c), 2),
                "target": round(float(c + sign * 2 * a), 2),
                "stop": round(float(c - sign * 2 * a), 2),
                "target_pct": round(float(tgt_pct), 2),
                "weight": w,
                "rank": round(float(tgt_pct) * w, 2),
                "bar": str(df.index[-1].date()),
            })
    cands.sort(key=lambda x: -x["rank"])
    return cands


def journal_candidates(cands: list, regime_state: str) -> int:
    """Append today's regime-allowed candidates as OPEN paper trades. The
    tracker resolves them later and feeds the learner — win AND loss alike."""
    os.makedirs("logs", exist_ok=True)
    seen = set()
    if os.path.exists(JOURNAL_FILE):
        for line in open(JOURNAL_FILE, encoding="utf-8"):
            try:
                r = json.loads(line)
                seen.add((r["symbol"], r["signal_date"]))
            except Exception:
                pass
    n = 0
    with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
        for x in cands:
            key = (x["symbol"], x["bar"])
            if key in seen:
                continue
            f.write(json.dumps({**x, "signal_date": x["bar"],
                                "regime": regime_state, "status": "open",
                                "journaled": datetime.now().isoformat(timespec="seconds")}) + "\n")
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--journal", action="store_true",
                    help="journal today's tradeable candidates (paper)")
    ap.add_argument("--learner", action="store_true",
                    help="print learner bucket report and exit")
    args = ap.parse_args()

    learner = SwingLearner()
    if args.learner:
        print(learner.report())
        return 0

    nifty, ma = regime()
    dist = (nifty / ma - 1) * 100
    regime_state = "risk_on" if nifty > ma else "risk_off"
    allowed = "long" if regime_state == "risk_on" else "short"

    print("=" * 78)
    print("SWING SCREEN - symmetric 7-gate framework + outcome learner")
    print("=" * 78)
    print(f"Gate 1  NIFTY {nifty:.0f} vs 200DMA {ma:.0f} ({dist:+.2f}%) -> "
          f"{regime_state.upper()}: {allowed.upper()} side active")
    if allowed == "short":
        print("        *** SHORTS ARE PAPER-ONLY: 15y evidence (2,816 trades) = PF 0.65,")
        print("        *** -125bp/trade. DO NOT fund short swings. Risk-off = stand aside")
        print("        *** in cash; the learner keeps testing shorts on paper only.")

    data = fetch()
    cands = screen(data, learner, regime_state)
    tradeable = [x for x in cands if x["direction"] == allowed]
    other = [x for x in cands if x["direction"] != allowed]

    print(f"\nTRADEABLE now ({allowed} side): {len(tradeable)} candidates")
    hdr = (f"{'#':>2} {'SYMBOL':<13}{'DIR':<6}{'SIGNAL':<18}"
           f"{'CLOSE':>9}{'TARGET':>9}{'STOP':>9}{'MOVE%':>7}{'W':>6}")
    print(hdr); print("-" * len(hdr))
    for i, x in enumerate(tradeable[:15], 1):
        print(f"{i:>2} {x['symbol']:<13}{x['direction']:<6}{x['signal']:<18}"
              f"{x['close']:>9}{x['target']:>9}{x['stop']:>9}"
              f"{x['target_pct']:>6.1f}%{x['weight']:>6.2f}")
    if other:
        print(f"\n(blocked by regime gate: {len(other)} {other[0]['direction']} setups - watchlist)")

    if args.journal and tradeable:
        n = journal_candidates(tradeable, regime_state)
        print(f"\njournaled {n} new paper trades -> {JOURNAL_FILE}")
        print("resolve + learn with: python swing_tracker.py")

    if args.json:
        with open(os.path.join("logs", "swing_screen.json"), "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.now().isoformat(timespec="seconds"),
                       "regime": regime_state, "nifty": round(nifty, 1),
                       "ma200": round(ma, 1), "candidates": cands}, f, indent=2)
        print("written -> logs/swing_screen.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
