"""
Walk-forward backtest for the ORB (Opening Range Breakout) strategy.

Honest constraints:
  - yfinance caps 5-min intraday history at 60 days. So this is a 60d window,
    not a 2-year window. Sample is small — treat the numbers as DIRECTIONAL,
    not as a calibrated edge estimate.
  - For each trading day we compute the opening range from bars 9:15-9:45 IST
    (re-implementing core/orb_strategy.compute_opening_range so the
    datetime.now() gating in detect_orb_signal doesn't bite).
  - Entry: first post-OR bar (>= 9:45, < 12:00) that satisfies
    {close beyond OR boundary in trade direction, vol >= 1.5x OR avg vol,
     body/range >= 0.5, extension <= 0.8%}.
    Fill at that bar's close, plus 0.05% slippage.
  - Exits: SL at opposite OR boundary, T2 at 2x range, or hard exit at 15:10
    (5min before session close). Bar-by-bar on OHLC.
  - Costs: 0.10% round-trip commission, 0.05% per-side slippage (same as
    backtest_india_swing for apples-to-apples).
  - Only the FIRST signal per symbol per day is taken (matches scanner intent).

Output: backtest_orb_trades.csv with columns matching backtest_india_swing
where applicable, plus ORB-specific fields (or_range_pct, vol_mult, body_ratio).
"""

from __future__ import annotations

import sys
import os
import logging
import warnings
from datetime import datetime, time as dt_time
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# ── Config (mirror orb_strategy.py params) ─────────────────────────────
OR_START   = dt_time(9, 15)
OR_END     = dt_time(9, 45)
ENTRY_CUT  = dt_time(12, 0)
SESSION_END = dt_time(15, 10)   # exit 5min before close to avoid auction slippage

MIN_RANGE_PCT   = 0.30
MAX_RANGE_PCT   = 3.0
MIN_VOLUME_MULT = 1.5
MIN_BODY_RATIO  = 0.50
MAX_EXTENSION_PCT = 0.8
T1_MULT = 1.0
T2_MULT = 2.0

COMMISSION_RT  = 0.001    # 0.10% round trip
SLIPPAGE_SIDE  = 0.0005   # 0.05% per side
DAYS_BACK      = 60

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
    return remap.get(sym, f"{sym}.NS")


def fetch_5m(symbol: str, days: int = DAYS_BACK) -> Optional[pd.DataFrame]:
    """Use project's curl_cffi-based yfinance fetcher (bypasses SSL interception)."""
    from core.api_dhan import _yfinance_intraday
    df = _yfinance_intraday(symbol, 5, days)
    if df is None or df.empty:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.reset_index(drop=True)


def fetch_universe(symbols: List[str]) -> Dict[str, pd.DataFrame]:
    out = {}
    print(f"[BT-ORB] Fetching 5m bars for {len(symbols)} symbols ({DAYS_BACK}d max) ...")
    for sym in symbols:
        df = fetch_5m(sym)
        if df is not None and len(df) >= 60:
            out[sym] = df
            unique_days = df["date"].dt.date.nunique()
            print(f"  {sym:14s} {len(df)} bars  {unique_days} days")
        else:
            print(f"  {sym:14s} SKIP (no/short data)")
    return out


def compute_or_for_day(day_bars: pd.DataFrame) -> Optional[Dict]:
    """Extract OR from a single day's bars. None if invalid."""
    or_bars = day_bars[day_bars["date"].dt.time.between(OR_START, OR_END, inclusive="left")]
    if len(or_bars) < 3:
        return None
    or_high = float(or_bars["high"].max())
    or_low  = float(or_bars["low"].min())
    or_close = float(or_bars["close"].iloc[-1])
    or_vol_avg = float(or_bars["volume"].mean())
    range_size = or_high - or_low
    range_pct = (range_size / or_close * 100) if or_close > 0 else 0
    if range_pct < MIN_RANGE_PCT or range_pct > MAX_RANGE_PCT:
        return None
    return {
        "or_high": or_high, "or_low": or_low, "or_close": or_close,
        "or_vol_avg": or_vol_avg, "range_size": range_size, "range_pct": range_pct,
    }


def find_entry_bar(day_bars: pd.DataFrame, or_data: Dict) -> Optional[Dict]:
    """Find the first post-OR bar that triggers an ORB signal."""
    post_or = day_bars[
        (day_bars["date"].dt.time >= OR_END) &
        (day_bars["date"].dt.time < ENTRY_CUT)
    ]
    for _, bar in post_or.iterrows():
        cur_open = float(bar["open"])
        cur_high = float(bar["high"])
        cur_low  = float(bar["low"])
        cur_close = float(bar["close"])
        cur_vol = float(bar["volume"])

        vol_mult = cur_vol / or_data["or_vol_avg"] if or_data["or_vol_avg"] > 0 else 0
        if vol_mult < MIN_VOLUME_MULT:
            continue

        rng = cur_high - cur_low
        body = abs(cur_close - cur_open)
        body_ratio = body / rng if rng > 0 else 0
        if body_ratio < MIN_BODY_RATIO:
            continue

        direction = None
        breakout_price = None
        if cur_close > or_data["or_high"] and cur_close > cur_open:
            direction = "long"
            breakout_price = or_data["or_high"]
            extension_pct = (cur_close - or_data["or_high"]) / or_data["or_high"] * 100
        elif cur_close < or_data["or_low"] and cur_close < cur_open:
            direction = "short"
            breakout_price = or_data["or_low"]
            extension_pct = (or_data["or_low"] - cur_close) / or_data["or_low"] * 100
        else:
            continue
        if extension_pct > MAX_EXTENSION_PCT:
            continue

        return {
            "entry_ts": bar["date"],
            "entry_price": cur_close,
            "direction": direction,
            "breakout_price": breakout_price,
            "vol_mult": vol_mult,
            "body_ratio": body_ratio,
        }
    return None


def simulate_exit(day_bars: pd.DataFrame, entry_ts, entry_price: float,
                  sl: float, t1: float, t2: float, direction: str) -> Dict:
    """Walk forward from entry bar, exit on SL / T2 / session-end."""
    forward = day_bars[day_bars["date"] > entry_ts]
    hit_t1 = False
    last_close = entry_price
    hold_bars = 0
    for _, bar in forward.iterrows():
        hold_bars += 1
        hi = float(bar["high"]); lo = float(bar["low"])
        last_close = float(bar["close"])

        # Hard session-end exit (independent of price)
        if bar["date"].time() >= SESSION_END:
            return {"outcome": "EOD", "exit_price": last_close,
                    "hold_bars": hold_bars, "hit_t1": hit_t1}

        if direction == "long":
            # SL first (conservative — assume worst intra-bar order)
            if lo <= sl:
                return {"outcome": "SL", "exit_price": sl,
                        "hold_bars": hold_bars, "hit_t1": hit_t1}
            if hi >= t2:
                return {"outcome": "T2", "exit_price": t2,
                        "hold_bars": hold_bars, "hit_t1": True}
            if not hit_t1 and hi >= t1:
                hit_t1 = True
        else:
            if hi >= sl:
                return {"outcome": "SL", "exit_price": sl,
                        "hold_bars": hold_bars, "hit_t1": hit_t1}
            if lo <= t2:
                return {"outcome": "T2", "exit_price": t2,
                        "hold_bars": hold_bars, "hit_t1": True}
            if not hit_t1 and lo <= t1:
                hit_t1 = True
    # ran out of bars without explicit exit
    return {"outcome": "EOD", "exit_price": last_close,
            "hold_bars": hold_bars, "hit_t1": hit_t1}


def backtest_symbol(symbol: str, df: pd.DataFrame) -> List[Dict]:
    """Run ORB backtest on one symbol's 60d of 5m bars. Returns list of trade dicts."""
    trades = []
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["day"] = df["date"].dt.date

    for day, day_bars in df.groupby("day"):
        if len(day_bars) < 10:
            continue
        or_data = compute_or_for_day(day_bars)
        if or_data is None:
            continue
        entry = find_entry_bar(day_bars, or_data)
        if entry is None:
            continue

        direction = entry["direction"]
        entry_px = entry["entry_price"]
        breakout = entry["breakout_price"]
        rng = or_data["range_size"]

        if direction == "long":
            sl = or_data["or_low"]
            t1 = breakout + rng * T1_MULT
            t2 = breakout + rng * T2_MULT
        else:
            sl = or_data["or_high"]
            t1 = breakout - rng * T1_MULT
            t2 = breakout - rng * T2_MULT

        # Apply slippage to entry (worse fill in direction of trade)
        fill_px = entry_px * (1 + SLIPPAGE_SIDE) if direction == "long" else entry_px * (1 - SLIPPAGE_SIDE)

        exit_info = simulate_exit(day_bars, entry["entry_ts"],
                                  fill_px, sl, t1, t2, direction)

        # Apply slippage to exit (worse fill in direction of close)
        raw_exit = exit_info["exit_price"]
        exit_px = raw_exit * (1 - SLIPPAGE_SIDE) if direction == "long" else raw_exit * (1 + SLIPPAGE_SIDE)

        # PnL
        if direction == "long":
            pnl_pct = (exit_px - fill_px) / fill_px * 100
        else:
            pnl_pct = (fill_px - exit_px) / fill_px * 100
        pnl_pct -= COMMISSION_RT * 100   # round-trip commission

        # R-multiple (1R = initial risk = |entry - sl|)
        initial_risk_pct = abs(fill_px - sl) / fill_px * 100
        R = pnl_pct / initial_risk_pct if initial_risk_pct > 0 else 0

        trades.append({
            "symbol": symbol,
            "direction": direction,
            "entry_date": str(entry["entry_ts"]),
            "entry_price": round(fill_px, 2),
            "sl": round(sl, 2),
            "target": round(t2, 2),
            "exit_price": round(exit_px, 2),
            "outcome": exit_info["outcome"],
            "hit_t1": exit_info["hit_t1"],
            "hold_bars": exit_info["hold_bars"],
            "pnl_pct": round(pnl_pct, 2),
            "R": round(R, 2),
            "or_range_pct": round(or_data["range_pct"], 2),
            "vol_mult": round(entry["vol_mult"], 2),
            "body_ratio": round(entry["body_ratio"], 2),
        })
    return trades


def main():
    data = fetch_universe(DEFAULT_UNIVERSE)
    if not data:
        print("No data fetched. Bailing.")
        return

    print(f"\n[BT-ORB] Running backtest on {len(data)} symbols ...")
    all_trades = []
    for sym, df in data.items():
        sym_trades = backtest_symbol(sym, df)
        all_trades.extend(sym_trades)
        if sym_trades:
            wr = sum(1 for t in sym_trades if t["pnl_pct"] > 0) / len(sym_trades) * 100
            print(f"  {sym:14s} {len(sym_trades):3d} trades  WR {wr:.0f}%")

    if not all_trades:
        print("No trades. Bailing.")
        return

    df_out = pd.DataFrame(all_trades)
    out_path = "backtest_orb_trades.csv"
    df_out.to_csv(out_path, index=False)

    print(f"\n=== ORB backtest summary (60d, {len(data)} symbols) ===")
    print(f"Total trades   : {len(df_out)}")
    print(f"Wins / Losses  : {(df_out.pnl_pct > 0).sum()} / {(df_out.pnl_pct <= 0).sum()}")
    print(f"Win rate       : {(df_out.pnl_pct > 0).mean()*100:.1f}%")
    print(f"Avg pnl_pct    : {df_out.pnl_pct.mean():.2f}%")
    print(f"Median pnl_pct : {df_out.pnl_pct.median():.2f}%")
    print(f"Total return % : {df_out.pnl_pct.sum():.1f}%")
    wins = df_out[df_out.pnl_pct > 0].pnl_pct.sum()
    losses = abs(df_out[df_out.pnl_pct <= 0].pnl_pct.sum())
    pf = wins / losses if losses > 0 else float("inf")
    print(f"Profit factor  : {pf:.2f}")
    print(f"Avg R          : {df_out.R.mean():.2f}")
    print(f"\nOutcome breakdown:")
    print(df_out.outcome.value_counts())
    print(f"\nHit T1 (interim): {df_out.hit_t1.sum()} / {len(df_out)} ({df_out.hit_t1.mean()*100:.0f}%)")
    print(f"Avg hold bars  : {df_out.hold_bars.mean():.1f} (5m bars)")
    print(f"\nWritten: {out_path}")


if __name__ == "__main__":
    main()
