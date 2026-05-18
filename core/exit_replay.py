"""
Exit replay — walk forward through historical OHLCV bars to determine the
real outcome of a signal that expired without live tick tracking.

Used by:
  - core/signal_tracker.py: replace broken Mode B EXPIRED stub with real outcome
  - core/replay_engine.py: simulate trade outcomes during backtests
  - scripts/backfill_journal.py: patch historical journal entries with real pnl

Logic:
  Given signal {symbol, direction, entry_price, sl_price, target_price, ts}
  fetch 5m bars from ts → now (or +6.5h cap).
  Walk bar by bar:
    long:   high >= target  → TARGET_HIT  (exit = target)
            low  <= sl      → SL_HIT      (exit = sl)
    short:  low  <= target  → TARGET_HIT  (exit = target)
            high >= sl      → SL_HIT      (exit = sl)
  If neither hit before end of window → TIME_EXIT (exit = last close).
  Also tracks max-favorable-excursion (MFE) and max-adverse-excursion (MAE).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)

# Walk-forward window from the active trade mode (intraday 6.5h /
# swing ~10 days). Recomputed per call in replay_exit so a mode flip
# takes effect without reimport.
try:
    from core.trade_mode import get_mode as _get_mode
    MAX_WINDOW_HOURS = float(_get_mode().replay_window_hours)
except Exception:
    MAX_WINDOW_HOURS = 6.5


def _fetch_bars_daily(symbol: str, start_ts: datetime, end_ts: datetime
                      ) -> Optional[pd.DataFrame]:
    """Fetch DAILY bars for the swing walk-forward window (yfinance)."""
    try:
        import yfinance as yf
        from core.api_dhan import _YF_TICKER_MAP
        yf_sym = _YF_TICKER_MAP.get(symbol.upper(), f"{symbol}.NS")
        df = yf.Ticker(yf_sym).history(
            start=(start_ts - timedelta(days=2)).strftime('%Y-%m-%d'),
            end=(end_ts + timedelta(days=2)).strftime('%Y-%m-%d'),
            interval='1d', auto_adjust=True,
        )
        if df is None or df.empty:
            return None
        df = df.rename(columns={c: c.lower() for c in df.columns})
        df = df.reset_index().rename(columns={'Date': 'date', 'index': 'date'})
        df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
        mask = (df['date'] >= start_ts - timedelta(days=2)) & \
               (df['date'] <= end_ts + timedelta(days=2))
        sub = df[mask]
        return sub.reset_index(drop=True) if len(sub) >= 1 else None
    except Exception as e:
        log.debug(f"[ExitReplay] daily fetch failed for {symbol}: {e}")
        return None


def _fetch_bars(symbol: str, start_ts: datetime, end_ts: datetime
                ) -> Optional[pd.DataFrame]:
    """Fetch bars between start_ts and end_ts. 5m for intraday mode,
    daily for swing mode (walking 5m over a 10-day swing is noise).

    Tries Dhan first (if configured) then yfinance fallback.
    """
    try:
        if _get_mode().replay_bar == "1d":
            return _fetch_bars_daily(symbol, start_ts, end_ts)
    except Exception:
        pass
    # Pad start by 5min to ensure we get the entry bar
    start_pad = start_ts - timedelta(minutes=5)
    end_pad = end_ts + timedelta(minutes=5)

    # Try Dhan via scanner (already used by replay_engine)
    try:
        from core.scanner import LiquidityScanner
        scanner = LiquidityScanner()
        df = scanner.get_intraday_data(symbol, interval=5, days_back=2)
        if df is not None and not df.empty and 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            mask = (df['date'] >= start_pad) & (df['date'] <= end_pad)
            sub = df[mask]
            if len(sub) >= 1:
                return sub.reset_index(drop=True)
    except Exception as e:
        log.debug(f"[ExitReplay] dhan/scanner failed for {symbol}: {e}")

    # yfinance fallback
    try:
        import yfinance as yf
        from core.api_dhan import _YF_TICKER_MAP
        yf_sym = _YF_TICKER_MAP.get(symbol.upper(), f"{symbol}.NS")
        # yfinance 5m only goes back 60 days
        df = yf.Ticker(yf_sym).history(
            start=start_pad.strftime('%Y-%m-%d'),
            end=(end_pad + timedelta(days=1)).strftime('%Y-%m-%d'),
            interval='5m', auto_adjust=True,
        )
        if df is None or df.empty:
            return None
        df = df.rename(columns={c: c.lower() for c in df.columns})
        df = df.reset_index().rename(columns={'datetime': 'date', 'Datetime': 'date'})
        if 'date' not in df.columns and 'index' in df.columns:
            df = df.rename(columns={'index': 'date'})
        df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
        mask = (df['date'] >= start_pad) & (df['date'] <= end_pad)
        sub = df[mask]
        return sub.reset_index(drop=True) if len(sub) >= 1 else None
    except Exception as e:
        log.debug(f"[ExitReplay] yfinance failed for {symbol}: {e}")
        return None


def replay_exit(signal: Dict) -> Dict:
    """
    Replay forward from signal entry. Returns outcome dict:
      {
        outcome:    "TARGET_HIT" | "SL_HIT" | "TIME_EXIT" | "NO_DATA",
        exit_price: float,
        exit_reason: str,
        pnl_pct:    float,    # signed: + for win, - for loss
        bars_held:  int,
        mfe_pct:    float,    # best price hit, signed in trade direction
        mae_pct:    float,    # worst price hit, signed against trade direction
        bars_to_hit: Optional[int],   # bars from entry to outcome (None if TIME_EXIT)
      }

    On failure, returns outcome="NO_DATA" with zeros.
    """
    sym       = signal.get("symbol", "")
    direction = signal.get("direction", "long").lower()
    entry     = float(signal.get("entry_price") or signal.get("entry") or 0)
    sl        = float(signal.get("sl_price") or signal.get("sl") or 0)
    target    = float(signal.get("target_price") or signal.get("target") or 0)
    ts_str    = signal.get("ts") or signal.get("ts_signal") or ""

    default = {
        "outcome": "NO_DATA", "exit_price": entry, "exit_reason": "no_data",
        "pnl_pct": 0.0, "bars_held": 0, "mfe_pct": 0.0, "mae_pct": 0.0,
        "bars_to_hit": None,
    }

    if not sym or entry <= 0 or sl <= 0 or target <= 0 or not ts_str:
        return default

    try:
        start_ts = datetime.fromisoformat(ts_str.replace('Z', ''))
    except Exception:
        return default

    # Window from active mode, read fresh (mode flip takes effect now).
    try:
        _win_h = float(_get_mode().replay_window_hours)
    except Exception:
        _win_h = MAX_WINDOW_HOURS
    end_ts = min(start_ts + timedelta(hours=_win_h), datetime.now())
    if end_ts <= start_ts:
        return default

    df = _fetch_bars(sym, start_ts, end_ts)
    if df is None or df.empty:
        return default

    lng = direction == "long"
    best_price = entry
    worst_price = entry
    exit_price = entry
    outcome = "TIME_EXIT"
    exit_reason = "time_exit_window_closed"
    bars_to_hit: Optional[int] = None

    for i, row in df.iterrows():
        hi = float(row['high'])
        lo = float(row['low'])
        cl = float(row['close'])

        if lng:
            if hi > best_price:
                best_price = hi
            if lo < worst_price:
                worst_price = lo
            # Target hit if high crosses target
            if hi >= target:
                exit_price = target
                outcome = "TARGET_HIT"
                exit_reason = "target_hit"
                bars_to_hit = int(i)
                break
            # SL hit if low crosses sl
            if lo <= sl:
                exit_price = sl
                outcome = "SL_HIT"
                exit_reason = "sl_hit"
                bars_to_hit = int(i)
                break
        else:
            if lo < best_price:
                best_price = lo
            if hi > worst_price:
                worst_price = hi
            if lo <= target:
                exit_price = target
                outcome = "TARGET_HIT"
                exit_reason = "target_hit"
                bars_to_hit = int(i)
                break
            if hi >= sl:
                exit_price = sl
                outcome = "SL_HIT"
                exit_reason = "sl_hit"
                bars_to_hit = int(i)
                break
        # Track running close as potential time exit
        exit_price = cl

    # Compute signed PnL %
    if lng:
        pnl_pct = (exit_price - entry) / entry * 100
        mfe_pct = (best_price - entry) / entry * 100
        mae_pct = (worst_price - entry) / entry * 100
    else:
        pnl_pct = (entry - exit_price) / entry * 100
        mfe_pct = (entry - best_price) / entry * 100  # signed FOR short = lower price is better
        mae_pct = (entry - worst_price) / entry * 100

    return {
        "outcome": outcome,
        "exit_price": round(exit_price, 4),
        "exit_reason": exit_reason,
        "pnl_pct": round(pnl_pct, 4),
        "bars_held": len(df),
        "mfe_pct": round(mfe_pct, 4),
        "mae_pct": round(mae_pct, 4),
        "bars_to_hit": bars_to_hit,
    }


if __name__ == "__main__":
    # Smoke test on a recent journal entry
    import json
    jp = os.path.join(os.path.dirname(__file__), "..", "logs", "signal_journal.jsonl")
    with open(jp) as f:
        lines = f.readlines()
    sample = json.loads(lines[-5])
    print("Input signal:", {k: sample.get(k) for k in
                            ['symbol', 'direction', 'entry_price', 'sl_price',
                             'target_price', 'ts']})
    print("Outcome:", replay_exit(sample))
