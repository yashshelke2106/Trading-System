"""
Walk-forward backtest for india_swing 5-gate strategy.

Honest constraints:
  - Daily bars only (G1 uses daily EMA20/50; G3 confirmation is on daily close)
  - G6 sector / G7 earnings / G8 delivery SKIPPED here — they depend on live
    yfinance / NSE bhavcopy data which doesn't reconstruct cleanly for past
    dates without survivor bias.
  - Signal at bar t close → fill at bar t+1 open
  - Exit walks forward bar-by-bar: SL hit, target hit, or HOLD_HORIZON_BARS
    elapsed (time exit). SL/target checked against bar OHLC.
  - Costs: 0.10% round-trip commission, 0.05% per-side slippage.

What this measures: the price-based edge of G1-G5 (trend + pullback +
confirmation + structural risk + RS). Live G6-G8 should ADD to this WR on
real trading, not subtract.
"""

from __future__ import annotations

import sys
import os
import logging
import warnings
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

from core.strategy_india_swing import generate_signal_india_swing


# ── Config ─────────────────────────────────────────────────────────────────
HOLD_HORIZON_BARS = 25      # v3: was 15, winners avg 10 bars but tail extends
COMMISSION_RT     = 0.001   # 0.1% round trip
SLIPPAGE_SIDE     = 0.0005  # 0.05% per fill
WARMUP_BARS       = 60      # need history for indicators
DEFAULT_DAYS      = 730     # 2 years lookback
BREAKEVEN_TRAIL_R = 0.7     # v3: move SL to breakeven after price hits 0.7R favourable

# Liquid F&O universe (top ~30 by typical turnover)
DEFAULT_UNIVERSE = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
    "BHARTIARTL", "KOTAKBANK", "BAJFINANCE", "HINDUNILVR", "ITC",
    "LT", "AXISBANK", "MARUTI", "ASIANPAINT", "WIPRO", "HCLTECH",
    "TECHM", "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB",
    "TATAMOTORS", "M&M", "BAJAJ-AUTO", "ULTRACEMCO", "POWERGRID",
    "NTPC", "ONGC", "NESTLEIND",
]


def _yf_ticker(sym: str) -> str:
    remap = {
        "TATAMOTORS": "TMCV.NS",
        "MCDOWELL-N": "UNITDSPR.NS",
        "DEEPAKNT": "DEEPAKNTR.NS",
        "M&M": "M%26M.NS",
        "BAJAJ-AUTO": "BAJAJ-AUTO.NS",
    }
    if sym in remap:
        return remap[sym]
    return f"{sym}.NS"


def fetch_daily(ticker: str, days: int = DEFAULT_DAYS) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV via yfinance. Returns columns: open/high/low/close/volume."""
    import yfinance as yf
    try:
        df = yf.download(ticker, period=f"{days}d", interval="1d",
                         progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df.columns = [c.lower() for c in df.columns]
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(df.columns):
            return None
        df = df.dropna(subset=list(required))
        return df
    except Exception:
        return None


def fetch_universe(symbols: List[str], days: int = DEFAULT_DAYS) -> Dict[str, pd.DataFrame]:
    """Bulk-fetch + map symbol → daily DataFrame."""
    out: Dict[str, pd.DataFrame] = {}
    print(f"[BT] Fetching {len(symbols)} symbols + NIFTY ({days}d) from yfinance ...")
    for sym in symbols:
        tkr = _yf_ticker(sym)
        df = fetch_daily(tkr, days)
        if df is not None and len(df) >= WARMUP_BARS + 30:
            out[sym] = df
            print(f"  {sym:14s} {len(df)} bars  {df.index[0].date()} -> {df.index[-1].date()}")
        else:
            print(f"  {sym:14s} SKIP (no/short data)")
    return out


def simulate_one_signal(df: pd.DataFrame, entry_idx: int, sl: float,
                        target: float, direction: str,
                        entry_price: float = None) -> Tuple[str, float, int]:
    """
    Walk forward from entry_idx+1. Check SL/target bar-by-bar on OHLC.

    v3: when price has moved 0.7R in favor, move SL to breakeven (entry price).
    Reduces -1R losses on noise after the trade was working.

    Returns (outcome, exit_price, holding_bars).
    outcome ∈ {"TARGET", "SL", "BE_STOP", "TIME_EXIT"}
    """
    n = len(df)
    if entry_price is None:
        entry_price = float(df['close'].iloc[entry_idx])  # crude fallback
    initial_risk = abs(entry_price - sl)
    be_threshold = entry_price + BREAKEVEN_TRAIL_R * initial_risk if direction == "long" \
                   else entry_price - BREAKEVEN_TRAIL_R * initial_risk
    moved_to_be = False
    last_close = float(df['close'].iloc[entry_idx])

    for k in range(1, HOLD_HORIZON_BARS + 1):
        i = entry_idx + k
        if i >= n:
            return "TIME_EXIT", last_close, k - 1
        hi = float(df['high'].iloc[i])
        lo = float(df['low'].iloc[i])
        last_close = float(df['close'].iloc[i])

        if direction == "long":
            # SL hit first (intra-bar order check)
            if lo <= sl:
                return ("BE_STOP" if moved_to_be else "SL"), sl, k
            if hi >= target:
                return "TARGET", target, k
            # Move SL to breakeven once price reached BE threshold
            if not moved_to_be and hi >= be_threshold:
                sl = entry_price
                moved_to_be = True
        else:  # short
            if hi >= sl:
                return ("BE_STOP" if moved_to_be else "SL"), sl, k
            if lo <= target:
                return "TARGET", target, k
            if not moved_to_be and lo <= be_threshold:
                sl = entry_price
                moved_to_be = True
    return "TIME_EXIT", last_close, HOLD_HORIZON_BARS


def run_backtest(universe_data: Dict[str, pd.DataFrame],
                 nifty_df: pd.DataFrame,
                 start_idx_offset: int = WARMUP_BARS) -> Dict:
    """
    Walk every symbol bar-by-bar. At each bar t, call generate_signal_india_swing
    on df.iloc[:t+1] (no look-ahead). If signal, simulate fill at t+1 open and
    walk exit.

    Returns metrics dict + trades list.
    """
    trades: List[Dict] = []

    for sym, df in universe_data.items():
        n = len(df)
        # Align NIFTY to this symbol's index
        nf = nifty_df.reindex(df.index, method="ffill")

        # Walk every bar after warmup
        signals_emitted = 0
        for t in range(start_idx_offset, n - 1):  # leave room for t+1 fill
            # Snapshot up to and including bar t (no look-ahead)
            sub_df = df.iloc[:t + 1]
            sub_nf = nf.iloc[:t + 1]
            try:
                sig = generate_signal_india_swing(sym, sub_df, sub_nf)
            except Exception:
                sig = None
            if sig is None:
                continue

            signals_emitted += 1

            # Fill at t+1 open with slippage
            fill_idx = t + 1
            raw_fill = float(df['open'].iloc[fill_idx])
            if sig.direction == "long":
                entry_fill = raw_fill * (1 + SLIPPAGE_SIDE)
                # Re-anchor SL/target relative to fill for honesty
                risk = sig.entry_price - sig.sl_price
                sl = entry_fill - risk
                target = entry_fill + 3.0 * risk
            else:
                entry_fill = raw_fill * (1 - SLIPPAGE_SIDE)
                risk = sig.sl_price - sig.entry_price
                sl = entry_fill + risk
                target = entry_fill - 3.0 * risk

            outcome, exit_raw, hold_bars = simulate_one_signal(
                df, fill_idx, sl, target, sig.direction, entry_price=entry_fill,
            )
            # Exit slippage applied against trader
            if sig.direction == "long":
                exit_fill = exit_raw * (1 - SLIPPAGE_SIDE)
                gross_r = (exit_fill - entry_fill) / max(abs(entry_fill - sl), 1e-9)
                pnl_pct = (exit_fill - entry_fill) / entry_fill
            else:
                exit_fill = exit_raw * (1 + SLIPPAGE_SIDE)
                gross_r = (entry_fill - exit_fill) / max(abs(sl - entry_fill), 1e-9)
                pnl_pct = (entry_fill - exit_fill) / entry_fill

            pnl_pct -= COMMISSION_RT  # round-trip commission

            trades.append({
                "symbol": sym,
                "direction": sig.direction,
                "entry_date": df.index[fill_idx].date().isoformat(),
                "entry_price": round(entry_fill, 2),
                "sl": round(sl, 2),
                "target": round(target, 2),
                "exit_price": round(exit_fill, 2),
                "outcome": outcome,
                "hold_bars": hold_bars,
                "pnl_pct": round(pnl_pct * 100, 2),
                "R": round(gross_r, 2),
                "grade": sig.confluence_grade,
                "score": sig.confluence_score,
                "patterns": ",".join(sig.patterns),
                "rsi": round(sig.rsi, 0),
                "rs_vs_nifty": round(sig.rs_vs_nifty, 3),
                "near_52wh": sig.near_52wh,
                "near_52wl": sig.near_52wl,
            })

        print(f"  {sym:14s} {signals_emitted:>4d} signals emitted")

    # ── Metrics ─────────────────────────────────────────────────────
    if not trades:
        print("\n[BT] NO trades generated. Universe may not have passed gates in this period.")
        return {"total_trades": 0, "trades": trades}

    wins = [t for t in trades if t["outcome"] == "TARGET"]
    losses = [t for t in trades if t["outcome"] == "SL"]
    be_stops = [t for t in trades if t["outcome"] == "BE_STOP"]
    times = [t for t in trades if t["outcome"] == "TIME_EXIT"]

    # v3: BE_STOP counts as scratch (saved a loss). For WR, count it neither
    # as win nor loss — it's a neutral exit.
    n_real = len(wins) + len(losses)
    wr_strict = len(wins) / max(n_real, 1) * 100
    wr_broad = len(wins) / max(len(trades), 1) * 100
    # Adjusted WR: wins / (wins + losses, excluding BE saves)
    wr_adj = len(wins) / max(len(wins) + len(losses), 1) * 100

    pnls = [t["pnl_pct"] for t in trades]
    expectancy_pct = float(np.mean(pnls))
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = gross_win / max(gross_loss, 0.001)

    rs = [t["R"] for t in trades]
    expectancy_R = float(np.mean(rs))

    avg_hold = float(np.mean([t["hold_bars"] for t in trades]))

    metrics = {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "be_stops": len(be_stops),
        "time_exits": len(times),
        "wr_strict_pct": round(wr_strict, 1),
        "wr_adj_pct": round(wr_adj, 1),
        "wr_broad_pct": round(wr_broad, 1),
        "expectancy_pct": round(expectancy_pct, 2),
        "expectancy_R": round(expectancy_R, 2),
        "profit_factor": round(profit_factor, 2),
        "gross_win_pct": round(gross_win, 1),
        "gross_loss_pct": round(gross_loss, 1),
        "avg_hold_bars": round(avg_hold, 1),
        "max_R": round(max(rs), 2) if rs else 0,
        "min_R": round(min(rs), 2) if rs else 0,
    }

    # Direction breakdown
    long_t  = [t for t in trades if t["direction"] == "long"]
    short_t = [t for t in trades if t["direction"] == "short"]
    long_wins  = sum(1 for t in long_t if t["outcome"] == "TARGET")
    long_loss  = sum(1 for t in long_t if t["outcome"] == "SL")
    short_wins = sum(1 for t in short_t if t["outcome"] == "TARGET")
    short_loss = sum(1 for t in short_t if t["outcome"] == "SL")

    metrics["long"] = {
        "n": len(long_t),
        "wins": long_wins,
        "losses": long_loss,
        "wr_pct": round(long_wins / max(long_wins + long_loss, 1) * 100, 1),
    }
    metrics["short"] = {
        "n": len(short_t),
        "wins": short_wins,
        "losses": short_loss,
        "wr_pct": round(short_wins / max(short_wins + short_loss, 1) * 100, 1),
    }

    # Grade breakdown
    grade_stats = {}
    for g in ("S", "A", "B"):
        gt = [t for t in trades if t["grade"] == g]
        gw = sum(1 for t in gt if t["outcome"] == "TARGET")
        gl = sum(1 for t in gt if t["outcome"] == "SL")
        if gt:
            grade_stats[g] = {
                "n": len(gt),
                "wins": gw, "losses": gl,
                "wr_pct": round(gw / max(gw + gl, 1) * 100, 1),
                "avg_R": round(float(np.mean([t["R"] for t in gt])), 2),
            }
    metrics["by_grade"] = grade_stats

    return {**metrics, "trades": trades}


def print_report(m: Dict) -> None:
    n = m.get("total_trades", 0)
    print("\n" + "=" * 60)
    print("  INDIA_SWING 5-GATE BACKTEST RESULTS (G1-G5 only)")
    print("=" * 60)
    if n == 0:
        print("  No trades. Gates too strict for this period/universe.")
        print("=" * 60)
        return
    print(f"  Total trades:    {n:>10d}")
    print(f"  Wins (TARGET):   {m['wins']:>10d}")
    print(f"  Losses (SL):     {m['losses']:>10d}")
    print(f"  BE stops:        {m.get('be_stops', 0):>10d}  (breakeven save)")
    print(f"  Time exits:      {m['time_exits']:>10d}")
    print("-" * 60)
    print(f"  WR strict (W/(W+L)):    {m['wr_strict_pct']:>6.1f}%   <-- accuracy")
    print(f"  WR adjusted (BE neutral): {m.get('wr_adj_pct', m['wr_strict_pct']):>6.1f}%")
    print(f"  WR broad  (W/total):    {m['wr_broad_pct']:>6.1f}%")
    print("-" * 60)
    print(f"  Expectancy:   {m['expectancy_pct']:>+6.2f}% per trade   ({m['expectancy_R']:+.2f}R)")
    print(f"  Profit factor:{m['profit_factor']:>6.2f}   (gross win/loss)")
    print(f"  Gross win:   +{m['gross_win_pct']:>5.1f}%")
    print(f"  Gross loss:  -{m['gross_loss_pct']:>5.1f}%")
    print(f"  Avg hold:     {m['avg_hold_bars']:>5.1f} bars")
    print(f"  Best R:       {m['max_R']:>+5.2f}    Worst R: {m['min_R']:>+5.2f}")
    print("-" * 60)
    print("  BY DIRECTION")
    print(f"    Long:   n={m['long']['n']:>3d}  WR={m['long']['wr_pct']:>5.1f}%   "
          f"({m['long']['wins']}W / {m['long']['losses']}L)")
    print(f"    Short:  n={m['short']['n']:>3d}  WR={m['short']['wr_pct']:>5.1f}%   "
          f"({m['short']['wins']}W / {m['short']['losses']}L)")
    print("-" * 60)
    print("  BY GRADE")
    for g in ("S", "A", "B"):
        if g in m.get("by_grade", {}):
            s = m["by_grade"][g]
            print(f"    {g}:  n={s['n']:>3d}  WR={s['wr_pct']:>5.1f}%  avgR={s['avg_R']:+.2f}  "
                  f"({s['wins']}W / {s['losses']}L)")
    print("=" * 60)


def save_trades(trades: List[Dict], path: str = "backtest_india_swing_trades.csv") -> None:
    if not trades:
        return
    pd.DataFrame(trades).to_csv(path, index=False)
    print(f"\n[BT] Trades CSV saved: {path}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help="Historical lookback in calendar days")
    p.add_argument("--limit", type=int, default=len(DEFAULT_UNIVERSE),
                   help="Cap universe size for quicker runs")
    args = p.parse_args()

    symbols = DEFAULT_UNIVERSE[:args.limit]
    print(f"[BT] Universe: {len(symbols)} symbols, {args.days}d history")

    # 1. Fetch NIFTY benchmark
    nifty_df = fetch_daily("^NSEI", args.days)
    if nifty_df is None or nifty_df.empty:
        print("[BT] FATAL: NIFTY (^NSEI) data unavailable")
        sys.exit(1)
    print(f"[BT] NIFTY: {len(nifty_df)} bars")

    # 2. Fetch universe
    udata = fetch_universe(symbols, args.days)
    if not udata:
        print("[BT] FATAL: no universe data fetched")
        sys.exit(1)

    # 3. Run
    print(f"\n[BT] Running walk-forward on {len(udata)} symbols ...")
    result = run_backtest(udata, nifty_df)

    # 4. Report
    print_report(result)
    save_trades(result.get("trades", []))
