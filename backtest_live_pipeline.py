"""
backtest_live_pipeline.py — measure the EDGE OF THE PIPELINE YOU ACTUALLY RUN.

The existing backtest (backtest_india_swing.py) measures G1-G5 only and fills
stops at exactly the stop price. The live scanner (scan_only_v2.py) runs a much
longer chain and Indian stocks gap through stops on news/earnings. This harness
closes both gaps as far as is honestly possible on daily bars.

WHAT IT REPRODUCES (live parity)
--------------------------------
  * Signals from the REAL strategy: generate_signal_india_swing (G0-G10),
    point-in-time via as_of_date (no look-ahead in G9 sector-leader / G10 ML).
  * entry_guard.check_entry  — the exact live pre-entry guard (pure on the dict).
  * finalize "lite" — the DETERMINISTIC, point-in-time parts of the live
    finalize_and_select: pattern-conflict block, volume floor, and the
    sector-correlation cap (MAX_PER_SECTOR). Real constants imported from
    core.signal_finalize so they stay in sync.
  * Signal's OWN sl_price / target_price as the exit (the strategy RR, ~1.5),
    NOT a re-anchored 2R like the old harness.
  * Daily entry cap (risk_engine max_trades_per_day) + one-position-per-symbol
    (matches the live (symbol,direction,date) dedup + re-entry block).
  * GAP-HONEST exits: if a bar OPENS through the stop, you fill at the open
    (worse than the stop), not at the stop price. Same for target gaps.

WHAT IT DOES *NOT* MODEL (and says so in the report — do not pretend otherwise)
-----------------------------------------------------------------------------
  * finalize_and_select's calibrator P(win) gate (fit to the biased journal),
    its live-NIFTY regime detection, and its wall-clock death-hour block.
  * pullback_entry retest queue and monte_carlo — need intraday data.
  * Option-leg microstructure (OI walls, IV-rank, theta, smart-money, spread).
    In INSTRUMENT_MODE="futures" the live path skips these anyway.
  * SURVIVORSHIP: the universe is whatever you pass. The defaults are TODAY's
    F&O names → survivor-biased. Supply a point-in-time list via --universe-file
    for an honest read. This harness CANNOT invent delisted-stock data.

DATA: Dhan daily bars (core.api_dhan.dhan_daily). This sandbox cannot reach
Dhan (SSL interception + simulated clock) — run on the real machine. Use
`python backtest_live_pipeline.py --selftest` to verify the exit mechanics
anywhere (synthetic data, no network).
"""

from __future__ import annotations

import argparse
import os
import sys
import logging
import warnings
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.strategy_india_swing import generate_signal_india_swing

# ── Cost / sim config (overridable by env, matching the project's knobs) ─────
try:
    import config
    COST_RT = float(getattr(config, "FUT_COST_ROUNDTRIP_PCT", 0.06)) / 100.0
    MAX_TRADES_PER_DAY = int(getattr(config, "RISK_CONFIG", {}).get("max_trades_per_day", 3))
except Exception:
    COST_RT = 0.0006
    MAX_TRADES_PER_DAY = 3

SLIPPAGE_SIDE = float(os.environ.get("BLP_SLIPPAGE", "0.0005"))   # 0.05%/fill
HOLD_HORIZON  = int(os.environ.get("BLP_HOLD", "12"))             # bars (days)
BE_TRAIL_R    = float(os.environ.get("BLP_BE_TRAIL_R", "0.7"))    # move SL->entry after +0.7R
WARMUP_BARS   = 60
DEFAULT_DAYS  = 730

DEFAULT_UNIVERSE = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
    "BHARTIARTL", "KOTAKBANK", "BAJFINANCE", "HINDUNILVR", "ITC",
    "LT", "AXISBANK", "MARUTI", "ASIANPAINT", "WIPRO", "HCLTECH",
    "TECHM", "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB",
    "TATAMOTORS", "M&M", "BAJAJ-AUTO", "ULTRACEMCO", "POWERGRID",
    "NTPC", "ONGC", "NESTLEIND",
]

# Live finalize constants — imported so the harness can't drift from production.
try:
    from core.signal_finalize import (
        SECTOR_MAP, MAX_PER_SECTOR, SELECTIVE_FIRE_KEEP,
        LONG_OPPOSING, SHORT_OPPOSING, MIN_VOLUME_RATIO,
    )
    _HAVE_FINALIZE = True
except Exception:
    SECTOR_MAP, MAX_PER_SECTOR, SELECTIVE_FIRE_KEEP = {}, 2, 12
    LONG_OPPOSING, SHORT_OPPOSING, MIN_VOLUME_RATIO = set(), set(), 0.70
    _HAVE_FINALIZE = False

try:
    from core.entry_guard import check_entry
    _HAVE_GUARD = True
except Exception:
    _HAVE_GUARD = False


# ───────────────────────────── data fetch ───────────────────────────────────

def fetch_daily(symbol: str, days: int = DEFAULT_DAYS) -> Optional[pd.DataFrame]:
    """Daily OHLCV from Dhan, date-indexed. None on failure. No yfinance."""
    try:
        from core.api_dhan import dhan_daily
        df = dhan_daily(symbol, days_back=days)
        if df is None or df.empty:
            return None
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_localize(None)
        need = {"open", "high", "low", "close", "volume"}
        if not need.issubset(df.columns):
            return None
        return df.dropna(subset=list(need)).sort_index()
    except Exception:
        return None


def fetch_universe(symbols: List[str], days: int) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    print(f"[BLP] Fetching {len(symbols)} symbols + NIFTY ({days}d) from Dhan ...")
    for sym in symbols:
        df = fetch_daily(sym, days)
        if df is not None and len(df) >= WARMUP_BARS + 30:
            out[sym] = df
            print(f"  {sym:14s} {len(df)} bars  {df.index[0].date()} -> {df.index[-1].date()}")
        else:
            print(f"  {sym:14s} SKIP (no/short data)")
    return out


# ───────────────────────── gap-honest exit engine ───────────────────────────

def simulate_exit(df: pd.DataFrame, entry_idx: int, entry_fill: float,
                  sl: float, tgt: float, direction: str,
                  hold: int = HOLD_HORIZON, be_trail_r: float = BE_TRAIL_R
                  ) -> Tuple[str, float, int, bool]:
    """
    Walk forward from entry_idx+1 on OHLC. GAP-HONEST:
      - if a bar OPENS beyond the stop, you fill at the OPEN (gap-through),
        which is worse than the stop price. (The old harness filled at `sl`.)
      - if a bar OPENS beyond the target, you fill at the OPEN.
      - otherwise intrabar: if both stop & target touched in one bar, assume
        STOP first (conservative).
      - breakeven trail: after +be_trail_r favourable, ratchet SL to entry.

    Returns (outcome, exit_price, hold_bars, gapped_stop).
      outcome in {TARGET, SL, BE_STOP, TIME_EXIT}
      gapped_stop True only when the stop exit filled on a gap (open beyond stop).
    """
    n = len(df)
    o_ = df["open"].values
    h_ = df["high"].values
    l_ = df["low"].values
    c_ = df["close"].values

    risk = abs(entry_fill - sl)
    if risk <= 0:
        return "TIME_EXIT", entry_fill, 0, False
    be_threshold = (entry_fill + be_trail_r * risk if direction == "long"
                    else entry_fill - be_trail_r * risk)
    moved_to_be = False
    last_close = c_[entry_idx]

    for k in range(1, hold + 1):
        i = entry_idx + k
        if i >= n:
            return "TIME_EXIT", last_close, k - 1, False
        o, h, l, c = o_[i], h_[i], l_[i], c_[i]
        last_close = c

        if direction == "long":
            if o <= sl:                                   # gap-down through stop
                return ("BE_STOP" if moved_to_be else "SL"), o, k, True
            if o >= tgt:                                  # gap-up through target
                return "TARGET", o, k, False
            if l <= sl:                                   # intrabar stop (first)
                return ("BE_STOP" if moved_to_be else "SL"), sl, k, False
            if h >= tgt:                                  # intrabar target
                return "TARGET", tgt, k, False
            if not moved_to_be and h >= be_threshold:
                sl = entry_fill
                moved_to_be = True
        else:  # short
            if o >= sl:
                return ("BE_STOP" if moved_to_be else "SL"), o, k, True
            if o <= tgt:
                return "TARGET", o, k, False
            if h >= sl:
                return ("BE_STOP" if moved_to_be else "SL"), sl, k, False
            if l <= tgt:
                return "TARGET", tgt, k, False
            if not moved_to_be and l <= be_threshold:
                sl = entry_fill
                moved_to_be = True

    return "TIME_EXIT", last_close, hold, False


# ─────────────────────── live-faithful candidate filters ────────────────────

def _pattern_conflict(sig: Dict) -> bool:
    direction = sig.get("direction", "long")
    pats = set(sig.get("patterns") or sig.get("patterns_combined") or [])
    opposing = LONG_OPPOSING if direction == "long" else SHORT_OPPOSING
    return len(pats & opposing) >= 2


def finalize_lite(cands: List[Dict], use_finalize: bool) -> List[Dict]:
    """Deterministic, point-in-time subset of finalize_and_select:
    pattern-conflict drop, volume floor, rank by score, sector cap, count cap.
    Skips calibrator/regime/hour (not point-in-time safe)."""
    if not use_finalize:
        return cands
    kept = []
    for s in cands:
        if _pattern_conflict(s):
            continue
        vol = s.get("volume_ratio")
        try:
            if vol is not None and float(vol) < MIN_VOLUME_RATIO:
                continue
        except (TypeError, ValueError):
            pass
        kept.append(s)
    kept.sort(key=lambda s: s.get("confluence_score", 0), reverse=True)
    sector_count: Dict[str, int] = defaultdict(int)
    out = []
    for s in kept:
        sym = s.get("symbol", "")
        sector = SECTOR_MAP.get(sym, sym)
        if sector_count[sector] >= MAX_PER_SECTOR:
            continue
        sector_count[sector] += 1
        out.append(s)
        if len(out) >= SELECTIVE_FIRE_KEEP:
            break
    return out


# ─────────────────────────────── backtest ───────────────────────────────────

def run_backtest(udata: Dict[str, pd.DataFrame], nifty_df: pd.DataFrame,
                 use_guard: bool, use_finalize: bool,
                 max_per_day: int) -> Dict:
    """DATE-MAJOR walk so the daily selection (sector cap, per-day count) is
    faithful to the live scan, with one-position-per-symbol overlap control."""
    # Precompute date -> iloc for each symbol.
    idx_of: Dict[str, Dict[pd.Timestamp, int]] = {}
    nf_by_sym: Dict[str, pd.DataFrame] = {}
    for sym, df in udata.items():
        idx_of[sym] = {ts: i for i, ts in enumerate(df.index)}
        nf_by_sym[sym] = nifty_df.reindex(df.index, method="ffill")

    all_dates = sorted(set().union(*[set(df.index) for df in udata.values()]))
    trades: List[Dict] = []
    open_until: Dict[str, pd.Timestamp] = {}     # sym -> last date position is open
    guard_kills: Counter = Counter()
    n_signals = 0

    for d in all_dates:
        # 1) gather point-in-time candidates across the whole universe for date d
        cands: List[Dict] = []
        for sym, df in udata.items():
            t = idx_of[sym].get(d)
            if t is None or t < WARMUP_BARS or t >= len(df) - 1:
                continue
            if sym in open_until and d <= open_until[sym]:
                continue   # position already open in this symbol
            try:
                sig = generate_signal_india_swing(
                    sym, df.iloc[:t + 1], nf_by_sym[sym].iloc[:t + 1], as_of_date=d)
            except Exception:
                sig = None
            if sig is None:
                continue
            n_signals += 1
            cand = sig.to_dict()
            cand["_sym"] = sym
            cand["_t"] = t
            # 2) entry guard (exact live function)
            if use_guard and _HAVE_GUARD:
                ok, reason = check_entry(cand)
                if not ok:
                    guard_kills[reason] += 1
                    continue
            cands.append(cand)

        if not cands:
            continue

        # 3) live-faithful selection (sector cap etc.) then daily count cap
        selected = finalize_lite(cands, use_finalize)
        if max_per_day > 0:
            selected = selected[:max_per_day]

        # 4) fill at t+1 open (slippage), exit gap-honestly using signal's OWN sl/target
        for s in selected:
            sym, t = s["_sym"], s["_t"]
            df = udata[sym]
            fill_idx = t + 1
            raw = float(df["open"].iloc[fill_idx])
            direction = s["direction"]
            sig_entry = float(s["entry_price"])
            sig_sl = float(s["sl_price"])
            sig_tgt = float(s["target_price"])
            risk_u = abs(sig_entry - sig_sl)
            if risk_u <= 0:
                continue
            if direction == "long":
                entry_fill = raw * (1 + SLIPPAGE_SIDE)
                sl = entry_fill - risk_u
                tgt = entry_fill + (sig_tgt - sig_entry)   # preserve signal's RR
            else:
                entry_fill = raw * (1 - SLIPPAGE_SIDE)
                sl = entry_fill + risk_u
                tgt = entry_fill - (sig_entry - sig_tgt)

            outcome, exit_raw, hold_bars, gapped = simulate_exit(
                df, fill_idx, entry_fill, sl, tgt, direction)

            if direction == "long":
                exit_fill = exit_raw * (1 - SLIPPAGE_SIDE)
                pnl_pct = (exit_fill - entry_fill) / entry_fill
                R = (exit_fill - entry_fill) / risk_u
            else:
                exit_fill = exit_raw * (1 + SLIPPAGE_SIDE)
                pnl_pct = (entry_fill - exit_fill) / entry_fill
                R = (entry_fill - exit_fill) / risk_u
            pnl_pct -= COST_RT

            exit_idx = min(fill_idx + hold_bars, len(df) - 1)
            open_until[sym] = df.index[exit_idx]

            trades.append({
                "symbol": sym, "direction": direction,
                "entry_date": df.index[fill_idx].date().isoformat(),
                "exit_date": df.index[exit_idx].date().isoformat(),
                "entry_price": round(entry_fill, 2),
                "sl": round(sl, 2), "target": round(tgt, 2),
                "exit_price": round(exit_fill, 2),
                "outcome": outcome, "gapped_stop": gapped,
                "hold_bars": hold_bars,
                "pnl_pct": round(pnl_pct * 100, 3), "R": round(R, 3),
                "grade": s.get("confluence_grade"), "score": s.get("confluence_score"),
                "rsi": round(float(s.get("rsi", 0) or 0), 0),
            })

    return _metrics(trades, n_signals, guard_kills, use_guard, use_finalize, max_per_day)


def _metrics(trades, n_signals, guard_kills, use_guard, use_finalize, max_per_day) -> Dict:
    m: Dict = {"trades": trades, "signals_generated": n_signals,
               "guard_kills": dict(guard_kills),
               "parity": {"entry_guard": use_guard and _HAVE_GUARD,
                          "finalize_lite": use_finalize and _HAVE_FINALIZE,
                          "max_per_day": max_per_day}}
    if not trades:
        m["total_trades"] = 0
        return m
    pnls = [t["pnl_pct"] for t in trades]
    Rs = [t["R"] for t in trades]
    wins = [t for t in trades if t["outcome"] == "TARGET"]
    sls = [t for t in trades if t["outcome"] == "SL"]
    be = [t for t in trades if t["outcome"] == "BE_STOP"]
    te = [t for t in trades if t["outcome"] == "TIME_EXIT"]
    gw = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p < 0))
    # fixed-fractional equity (1% risk/trade on R) for an honest drawdown
    eq, peak, mdd = 1.0, 1.0, 0.0
    for R in Rs:
        eq *= (1 + 0.01 * max(-1.0, min(5.0, R)))
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak * 100)
    gap_stops = [t for t in trades if t["gapped_stop"]]
    # extra loss caused by gap fills vs naive stop-price fill (the honesty delta)
    extra_gap_loss = 0.0
    for t in gap_stops:
        # naive would have exited ~ -1R; gap exit R is worse
        extra_gap_loss += (-1.0 - t["R"])
    m.update({
        "total_trades": len(trades),
        "wins": len(wins), "losses": len(sls), "be_stops": len(be), "time_exits": len(te),
        "wr_pct": round(100 * len(wins) / max(len(wins) + len(sls), 1), 1),
        "expectancy_pct": round(float(np.mean(pnls)), 3),
        "expectancy_R": round(float(np.mean(Rs)), 3),
        "profit_factor": round(gw / max(gl, 1e-9), 3),
        "max_dd_pct": round(mdd, 1),
        "avg_hold": round(float(np.mean([t["hold_bars"] for t in trades])), 1),
        "gap_stops": len(gap_stops),
        "gap_stop_extra_R": round(extra_gap_loss, 1),
        "long": Counter(t["direction"] for t in trades).get("long", 0),
        "short": Counter(t["direction"] for t in trades).get("short", 0),
        "by_grade": {g: sum(1 for t in trades if t["grade"] == g) for g in ("S", "A", "B")},
    })
    return m


def print_report(m: Dict) -> None:
    print("\n" + "=" * 70)
    print("  LIVE-PIPELINE BACKTEST (gap-honest, point-in-time)")
    print("=" * 70)
    par = m.get("parity", {})
    print("  LIVE-PARITY (what ran):")
    print(f"    india_swing G0-G10 ........ YES (point-in-time as_of_date)")
    print(f"    entry_guard.check_entry ... {'YES' if par.get('entry_guard') else 'no'}")
    print(f"    finalize (sector cap etc.). {'YES (deterministic subset)' if par.get('finalize_lite') else 'no'}")
    print(f"    max trades/day ............ {par.get('max_per_day')}")
    print("  NOT MODELED: calibrator P(win) gate, live-NIFTY regime, death-hour")
    print("    block, pullback retest, Monte-Carlo, option microstructure.")
    print("    Universe survivorship NOT corrected unless you passed a PIT list.")
    print("-" * 70)
    if not m.get("total_trades"):
        print(f"  signals generated: {m.get('signals_generated', 0)}")
        print("  NO TRADES after filters. Gates/universe too tight for this period.")
        print("=" * 70)
        return
    print(f"  Signals generated: {m['signals_generated']}   Trades taken: {m['total_trades']}")
    gk = m.get("guard_kills") or {}
    if gk:
        top = ", ".join(f"{k}={v}" for k, v in sorted(gk.items(), key=lambda x: -x[1])[:6])
        print(f"  entry_guard kills: {top}")
    print(f"  Wins {m['wins']} | SL {m['losses']} | BE {m['be_stops']} | Time {m['time_exits']}")
    print("-" * 70)
    print(f"  Win rate (W/(W+L)):  {m['wr_pct']:.1f}%")
    print(f"  Profit factor:       {m['profit_factor']:.3f}")
    print(f"  Expectancy:          {m['expectancy_pct']:+.3f}% / trade   ({m['expectancy_R']:+.3f}R)")
    print(f"  Max drawdown (1%/tr):{m['max_dd_pct']:.1f}%")
    print(f"  Avg hold:            {m['avg_hold']:.1f} bars   Long {m['long']} / Short {m['short']}")
    print(f"  by grade: {m['by_grade']}")
    print("-" * 70)
    print(f"  GAP-HONESTY DELTA: {m['gap_stops']} stop(s) filled on a gap-through "
          f"(open beyond stop)")
    print(f"    extra loss vs naive stop-price fill: {m['gap_stop_extra_R']:+.1f}R "
          f"(this is what the old harness hid)")
    print("=" * 70)
    if m["profit_factor"] < 1.0 or m["expectancy_pct"] <= 0:
        print("  VERDICT: NET-NEGATIVE on this (honest) measurement. Do not deploy.")
    else:
        print("  VERDICT: positive here — confirm on a POINT-IN-TIME (non-survivor)")
        print("  universe across >=2 regimes incl. a drawdown before risking capital.")
    print("=" * 70)


# ───────────────────────────── self-test ────────────────────────────────────

def _mk(bars: List[Tuple[float, float, float, float]]) -> pd.DataFrame:
    """bars = list of (open,high,low,close); row0 is the entry bar."""
    return pd.DataFrame(bars, columns=["open", "high", "low", "close"])


def selftest() -> int:
    """Verify the gap-honest exit mechanics WITHOUT any network/data."""
    fails = 0

    def check(name, got, want):
        nonlocal fails
        ok = (got[0] == want[0] and abs(got[1] - want[1]) < 1e-6
              and got[3] == want[3])
        print(f"  [{'OK' if ok else 'FAIL'}] {name}: {got} (want outcome={want[0]} "
              f"px={want[1]} gap={want[3]})")
        if not ok:
            fails += 1

    # entry 100, risk 5 (sl 95), target 110 (2R) — long
    # 1) normal intrabar stop: bar opens 99, dips to 94 -> SL at 95
    df = _mk([(100, 100, 100, 100), (99, 99.5, 94, 96)])
    check("long normal stop", simulate_exit(df, 0, 100, 95, 110, "long", be_trail_r=99),
          ("SL", 95, 1, False))
    # 2) gap-down through stop: bar opens 90 -> fill at OPEN 90, gap=True
    df = _mk([(100, 100, 100, 100), (90, 92, 88, 89)])
    check("long gap stop", simulate_exit(df, 0, 100, 95, 110, "long", be_trail_r=99),
          ("SL", 90, 1, True))
    # 3) intrabar target: opens 101, highs 111 -> TARGET at 110
    df = _mk([(100, 100, 100, 100), (101, 111, 100, 110)])
    check("long target", simulate_exit(df, 0, 100, 95, 110, "long", be_trail_r=99),
          ("TARGET", 110, 1, False))
    # 4) gap-up through target: opens 112 -> fill at OPEN 112
    df = _mk([(100, 100, 100, 100), (112, 113, 111, 112)])
    check("long gap target", simulate_exit(df, 0, 100, 95, 110, "long", be_trail_r=99),
          ("TARGET", 112, 1, False))
    # 5) BE trail: bar1 high 104 (>=103.5 be) moves SL->100; bar2 dips to 99 -> BE_STOP@100
    df = _mk([(100, 100, 100, 100), (101, 104, 100.5, 103), (101, 102, 99, 99.5)])
    check("long BE stop", simulate_exit(df, 0, 100, 95, 120, "long", be_trail_r=0.7),
          ("BE_STOP", 100, 2, False))
    # 6) time exit: nothing hit within hold
    df = _mk([(100, 100, 100, 100), (100, 101, 99.5, 100.2), (100, 101, 99.6, 100.4)])
    out = simulate_exit(df, 0, 100, 95, 120, "long", hold=2, be_trail_r=99)
    check("long time exit", out, ("TIME_EXIT", out[1], out[2], False))
    # 7) short gap-up through stop (sl 105): opens 110 -> fill 110 gap=True
    df = _mk([(100, 100, 100, 100), (110, 112, 108, 111)])
    check("short gap stop", simulate_exit(df, 0, 100, 105, 90, "short", be_trail_r=99),
          ("SL", 110, 1, True))

    print(f"\n  self-test: {'ALL PASS' if fails == 0 else str(fails)+' FAILED'}")
    return 1 if fails else 0


# ─────────────────────────────── main ───────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--full", action="store_true", help="use core.universe.FO_UNIVERSE")
    p.add_argument("--universe-file", help="point-in-time symbol list (one/line) — "
                                           "the honest, non-survivor option")
    p.add_argument("--no-guard", action="store_true", help="disable entry_guard")
    p.add_argument("--no-finalize", action="store_true", help="disable finalize-lite")
    p.add_argument("--max-per-day", type=int, default=MAX_TRADES_PER_DAY,
                   help="0 = unlimited (signal view); default = risk_engine cap")
    p.add_argument("--selftest", action="store_true",
                   help="verify exit mechanics on synthetic data (no network)")
    args = p.parse_args()

    if args.selftest:
        return selftest()

    # Universe
    if args.universe_file:
        with open(args.universe_file) as f:
            base = [ln.strip().upper() for ln in f if ln.strip() and not ln.startswith("#")]
        print(f"[BLP] universe from {args.universe_file}: {len(base)} symbols")
    elif args.full:
        try:
            from core.universe import FO_UNIVERSE
            base = list(dict.fromkeys(FO_UNIVERSE))
        except Exception as e:
            print(f"[BLP] --full failed ({e}); using defaults")
            base = DEFAULT_UNIVERSE
        print("[BLP] !!! SURVIVORSHIP WARNING: --full = TODAY's F&O names. Results are")
        print("[BLP]     optimistic. Use --universe-file with point-in-time lists for truth.")
    else:
        base = DEFAULT_UNIVERSE
        print("[BLP] !!! SURVIVORSHIP WARNING: default universe = today's survivors.")

    symbols = base[:args.limit] if args.limit else base

    nifty_df = fetch_daily("NIFTY", args.days)
    if nifty_df is None or nifty_df.empty:
        print("[BLP] FATAL: NIFTY data unavailable (Dhan unreachable here? run on real machine).")
        return 1
    udata = fetch_universe(symbols, args.days)
    if not udata:
        print("[BLP] FATAL: no universe data fetched.")
        return 1

    print(f"\n[BLP] walk-forward on {len(udata)} symbols "
          f"(guard={'off' if args.no_guard else 'on'}, "
          f"finalize={'off' if args.no_finalize else 'on'}, "
          f"max/day={args.max_per_day}) ...")
    res = run_backtest(udata, nifty_df,
                       use_guard=not args.no_guard,
                       use_finalize=not args.no_finalize,
                       max_per_day=args.max_per_day)
    print_report(res)
    if res.get("trades"):
        out = "backtest_live_pipeline_trades.csv"
        pd.DataFrame(res["trades"]).to_csv(out, index=False)
        print(f"\n[BLP] trades CSV: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
