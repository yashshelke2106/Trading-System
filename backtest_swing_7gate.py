"""
backtest_swing_7gate.py — replay the LIVE swing lane (swing_screen.py) over
Dhan daily bars.

WHY THIS EXISTS
---------------
The swing sleeve's published evidence (docs/research/exit_style_gate.md,
logs/backtest_15y_trades.csv) was measured on yfinance. yfinance shows only
today's survivors and has been measured in this project to inflate PF by
1.5-2x. This harness re-measures the same rules on Dhan bars, which is what
the live system actually trades off.

PARITY, NOT REIMPLEMENTATION
----------------------------
Entries reproduce swing_screen.screen() exactly (same ATR/MA200/MA5/RSI2
math, same setup precedence, same one-candidate-per-symbol rule).
Exits do not reproduce anything — they CALL the live resolver
swing_tracker._resolve, so the backtest cannot silently drift from the code
that resolves real paper trades. Costs come from swing_tracker.COST for the
same reason (long 0.25% cash, short 0.10% futures).

NO LOOK-AHEAD
-------------
Indicators at bar t use bars <= t. Entry fills at t+1 open. The regime gate
reads NIFTY's 200-DMA as of bar t. The learner is deliberately NOT applied:
it only re-RANKS candidates (it cannot add or remove a trade), and its
present-day weights are not knowable at a past bar.

WHAT IT CANNOT FIX
------------------
SURVIVORSHIP. The universe is core.universe.TOP100_LIQUID — today's liquid
names. Any stock that was liquid in 2018 and has since died is absent, so
LEVELS here are an upper bound. Dhan cannot serve delisted-stock history;
only the NSE bhavcopy archive can, and it starts 2019.

    python backtest_swing_7gate.py                 # 10y, full TOP100
    python backtest_swing_7gate.py --days 1825     # 5y
    python backtest_swing_7gate.py --limit 25      # quick pass
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")

from core.universe import TOP100_LIQUID
from swing_screen import (MIN_TARGET_PCT, STOP_ATR_MULT, TARGET_ATR_MULT,
                          _rsi)
from swing_tracker import COST, _resolve

# Dhan stamps daily bars at IST midnight expressed as epoch seconds, which
# pandas reads back as 18:30 the PREVIOUS day. Undo that or every bar is
# filed one calendar day early.
IST_SHIFT = pd.Timedelta(hours=5, minutes=30)

FUT_COST = 0.0010   # what a long would cost via futures instead of delivery


def load_bars(symbol: str, days: int) -> Optional[pd.DataFrame]:
    """Dhan daily bars, date-indexed, IST-corrected. None on failure."""
    from core.api_dhan import dhan_daily
    try:
        df = dhan_daily(symbol, days_back=days)
    except Exception:
        return None
    if df is None or df.empty or "date" not in df.columns:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]) + IST_SHIFT
    df = df.drop_duplicates(subset="date", keep="last").set_index("date").sort_index()
    df.index = df.index.normalize()
    return df if len(df) >= 260 else None


def candidates_for(symbol: str, df: pd.DataFrame) -> List[Dict]:
    """Every bar at which swing_screen.screen() would have emitted a setup.

    Vectorised over history, but the arithmetic is bar-for-bar identical to
    the live screen: rolling windows only look backwards, and the 200-DMA
    slope test compares against ma200 twenty bars earlier (screen() reads
    ma200.iloc[-21] while the current bar is iloc[-1]).
    """
    tr = np.maximum(df.high - df.low,
         np.maximum((df.high - df.close.shift()).abs(),
                    (df.low - df.close.shift()).abs()))
    atr = tr.rolling(14).mean()
    ma200 = df.close.rolling(200).mean()
    ma200_prev = ma200.shift(20)
    ma5 = df.close.rolling(5).mean()
    rsi2 = _rsi(df.close, 2)
    dchg = df.close.diff()
    dn3 = (dchg < 0).rolling(3).sum() == 3
    up3 = (dchg > 0).rolling(3).sum() == 3
    tgt_pct = TARGET_ATR_MULT * atr / df.close * 100

    out: List[Dict] = []
    for i in range(len(df)):
        c, a, m2, m2p, m5 = (df.close.iloc[i], atr.iloc[i], ma200.iloc[i],
                             ma200_prev.iloc[i], ma5.iloc[i])
        if pd.isna(a) or pd.isna(m2) or pd.isna(m2p) or pd.isna(m5):
            continue
        tp = float(tgt_pct.iloc[i])
        if tp < MIN_TARGET_PCT:
            continue

        # Setup precedence matters: screen() takes setups[:1].
        setup = None
        if c > m2 and m2 > m2p:
            if c < m5 * 0.98:
                setup = ("long", "2pct_under_5dma")
            elif bool(dn3.iloc[i]):
                setup = ("long", "3_down_days")
            elif rsi2.iloc[i] < 10:
                setup = ("long", "rsi2_oversold")
        elif c < m2 and m2 < m2p:
            if c > m5 * 1.02:
                setup = ("short", "2pct_over_5dma")
            elif bool(up3.iloc[i]):
                setup = ("short", "3_up_days")
            elif rsi2.iloc[i] > 90:
                setup = ("short", "rsi2_overbought")
        if setup is None:
            continue

        direction, signal = setup
        sign = 1 if direction == "long" else -1
        out.append({
            "symbol": symbol, "direction": direction, "signal": signal,
            "close": round(float(c), 2),
            "target": round(float(c + sign * TARGET_ATR_MULT * a), 2),
            "stop": round(float(c - sign * STOP_ATR_MULT * a), 2),
            "target_pct": round(tp, 2),
            "signal_date": str(df.index[i].date()),
            "exit_style": "C",
        })
    return out


def regime_series(days: int) -> Optional[pd.Series]:
    """risk_on / risk_off per date, from NIFTY vs its own 200-DMA."""
    nifty = load_bars("NIFTY", days)
    if nifty is None:
        return None
    ma200 = nifty.close.rolling(200).mean()
    return pd.Series(np.where(nifty.close > ma200, "risk_on", "risk_off"),
                     index=nifty.index).where(ma200.notna())


def clustered_t(trades: pd.DataFrame, col: str = "ret_net") -> float:
    """t-stat on DAILY means, not on trades.

    Trades opened on the same date share the market's move that week, so
    treating them as independent inflates t by roughly sqrt(trades-per-day).
    """
    daily = trades.groupby("signal_date")[col].mean()
    if len(daily) < 3 or daily.std(ddof=1) == 0:
        return float("nan")
    return float(daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily))))


def summarise(trades: pd.DataFrame, label: str, cost_override: float = None) -> Dict:
    if trades.empty:
        return {"label": label, "n": 0}
    t = trades.copy()
    if cost_override is not None:
        t["ret_net"] = t["ret_gross"] - cost_override
        t["won"] = t["ret_net"] > 0
    wins, losses = t[t.ret_net > 0], t[t.ret_net <= 0]
    gross_win = wins.ret_net.sum()
    gross_loss = -losses.ret_net.sum()
    return {
        "label": label,
        "n": len(t),
        "win_pct": 100.0 * len(wins) / len(t),
        "avg_bp": 10000.0 * t.ret_net.mean(),
        "total_pct": 100.0 * t.ret_net.sum(),
        "pf": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "t": clustered_t(t),
    }


def print_table(rows: List[Dict], title: str) -> None:
    print(f"\n{title}")
    print(f"  {'bucket':<26} {'n':>7} {'win%':>7} {'avg bp':>9} {'PF':>7} {'clust t':>8}")
    print(f"  {'-'*26} {'-'*7} {'-'*7} {'-'*9} {'-'*7} {'-'*8}")
    for r in rows:
        if not r.get("n"):
            print(f"  {r['label']:<26} {'0':>7}")
            continue
        pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        print(f"  {r['label']:<26} {r['n']:>7} {r['win_pct']:>7.1f} "
              f"{r['avg_bp']:>9.1f} {pf:>7} {r['t']:>8.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3650, help="Dhan lookback (max ~10y)")
    ap.add_argument("--limit", type=int, default=None, help="first N symbols only")
    ap.add_argument("--out", default="logs/backtest_swing_7gate_trades.csv")
    args = ap.parse_args()

    universe = list(TOP100_LIQUID)[: args.limit] if args.limit else list(TOP100_LIQUID)

    print("=" * 74)
    print("SWING 7-GATE (swing_screen.py) — Dhan daily bars")
    print("=" * 74)
    print(f"universe   : {len(universe)} names (core.universe.TOP100_LIQUID)")
    print(f"lookback   : {args.days} calendar days")
    print(f"exit       : style C via swing_tracker._resolve (LIVE code)")
    print(f"costs      : long {COST['long']*100:.2f}% / short {COST['short']*100:.2f}% "
          f"(swing_tracker.COST)")
    print("survivorship: TOP100_LIQUID is TODAY's list -> levels are an UPPER BOUND")

    print("\nfetching regime (NIFTY)...")
    regimes = regime_series(args.days)
    if regimes is None:
        print("FATAL: no NIFTY bars from Dhan — cannot compute the regime gate.")
        return 1

    all_trades: List[Dict] = []
    bad: List[str] = []
    unresolved = 0
    for k, sym in enumerate(universe, 1):
        bars = load_bars(sym, args.days)
        if bars is None:
            bad.append(sym)
            print(f"  [{k:>3}/{len(universe)}] {sym:<14} no data")
            continue
        cands = candidates_for(sym, bars)
        n_ok = 0
        for row in cands:
            res = _resolve(row, bars)
            if res is None:
                unresolved += 1
                continue
            reg = regimes.get(pd.Timestamp(row["signal_date"]))
            all_trades.append({**row, **res,
                               "regime": reg if isinstance(reg, str) else "unknown"})
            n_ok += 1
        print(f"  [{k:>3}/{len(universe)}] {sym:<14} bars={len(bars):>5} trades={n_ok:>4}")

    if not all_trades:
        print("\nNo trades generated — check Dhan connectivity.")
        return 1

    t = pd.DataFrame(all_trades)
    t["signal_date"] = pd.to_datetime(t["signal_date"])
    t = t.sort_values("signal_date")
    os.makedirs("logs", exist_ok=True)
    t.to_csv(args.out, index=False)

    span = f"{t.signal_date.min().date()} to {t.signal_date.max().date()}"
    print("\n" + "=" * 74)
    print(f"RESULTS   {len(t):,} trades   {span}   ({len(bad)} symbols had no data, "
          f"{unresolved} still open at the end)")
    print("=" * 74)

    print_table([summarise(t, "ALL (both sides)"),
                 summarise(t[t.direction == "long"], "long"),
                 summarise(t[t.direction == "short"], "short")],
                "By direction — at live costs")

    fundable = t[(t.direction == "long") & (t.regime == "risk_on")]
    print_table([summarise(fundable, "FUNDED (long+risk_on)"),
                 summarise(fundable, "  same, futures cost", cost_override=FUT_COST)],
                "What the system actually funds (fundable flag)")

    print_table([summarise(t[t.signal == s], s) for s in sorted(t.signal.unique())],
                "By setup")

    print_table([summarise(t[t.regime == r], r) for r in sorted(t.regime.unique())],
                "By regime")

    mid = t.signal_date.min() + (t.signal_date.max() - t.signal_date.min()) / 2
    print_table([summarise(t[t.signal_date <= mid], f"H1 (to {mid.date()})"),
                 summarise(t[t.signal_date > mid], f"H2 (from {mid.date()})")],
                "Stability — split-half (funded side would be the one to trust)")

    print_table([summarise(fundable[fundable.signal_date <= mid], "H1 funded"),
                 summarise(fundable[fundable.signal_date > mid], "H2 funded")],
                "Stability — funded side only")

    print(f"\nexit-reason mix: "
          + ", ".join(f"{k} {v}" for k, v in t.outcome.value_counts().items()))
    print(f"trades written to {args.out}")
    if bad:
        print(f"no Dhan data: {', '.join(bad)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
