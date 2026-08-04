"""
Replay Engine — feed historical market data through the agent pipeline.

Replays recorded intraday data (from logs/replay_data/) or fetches from
yfinance, then pushes events through the same EventBus agents use live.

Usage:
    python -m core.replay_engine --date 2026-05-12 --symbols RELIANCE,TCS
    python -m core.replay_engine --date 2026-05-12 --all  # full universe

Output:
    logs/replay_results_{date}.json — all signals generated, trades taken,
    P&L, win rate, per-pattern stats.

This lets you test code changes in 5 minutes instead of waiting for next
trading day. Same agents, same bus, same logic — different data source.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

import pandas as pd

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ReplayResult:
    """Accumulates results from a replay session."""

    def __init__(self, replay_date: str):
        self.date = replay_date
        self.signals_generated: List[Dict] = []
        self.trades_entered: List[Dict] = []
        self.trades_closed: List[Dict] = []
        self.events_published: Dict[str, int] = {}
        self.start_time = time.time()
        self.end_time: float = 0

    def add_signal(self, payload: Dict) -> None:
        self.signals_generated.append(payload)

    def add_trade_enter(self, payload: Dict) -> None:
        self.trades_entered.append(payload)

    def add_trade_close(self, payload: Dict) -> None:
        self.trades_closed.append(payload)

    def summary(self) -> Dict:
        wins = [t for t in self.trades_closed if float(t.get("pnl", 0)) > 0]
        losses = [t for t in self.trades_closed if float(t.get("pnl", 0)) <= 0]
        total_pnl = sum(float(t.get("pnl", 0)) for t in self.trades_closed)

        # Per-pattern breakdown
        pattern_stats: Dict[str, Dict] = {}
        for t in self.trades_closed:
            pnl = float(t.get("pnl", 0))
            for pat in t.get("patterns", []):
                if pat not in pattern_stats:
                    pattern_stats[pat] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
                if pnl > 0:
                    pattern_stats[pat]["wins"] += 1
                else:
                    pattern_stats[pat]["losses"] += 1
                pattern_stats[pat]["total_pnl"] += pnl

        self.end_time = time.time()
        return {
            "date": self.date,
            "elapsed_sec": round(self.end_time - self.start_time, 1),
            "signals": len(self.signals_generated),
            "trades_entered": len(self.trades_entered),
            "trades_closed": len(self.trades_closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": f"{len(wins) / max(len(self.trades_closed), 1) * 100:.0f}%",
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / max(len(self.trades_closed), 1), 2),
            "pattern_stats": pattern_stats,
            "signals_detail": self.signals_generated,
            "trades_detail": self.trades_closed,
        }

    def save(self) -> str:
        path = os.path.join(_PROJECT_ROOT, "logs", f"replay_results_{self.date}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.summary(), f, indent=2, default=str)
        return path


class ReplayScanner:
    """Mock scanner that serves historical data instead of live API calls."""

    def __init__(self, historical_data: Dict[str, "pd.DataFrame"]):
        """
        historical_data: {symbol: DataFrame with OHLCV 5m candles}
        """
        self._data = historical_data
        self._cursor: Dict[str, int] = {}  # sym -> current candle index
        self._daily_cache: Dict[str, "pd.DataFrame"] = {}

    def get_intraday_data(self, symbol: str, interval: int = 5,
                          days_back: int = 5) -> "pd.DataFrame | None":
        import pandas as pd
        df = self._data.get(symbol)
        if df is None or df.empty:
            return None

        cursor = self._cursor.get(symbol, len(df))
        # Return all candles up to cursor (simulates "current time")
        subset = df.iloc[:cursor].copy()
        if len(subset) < 10:
            return None
        return subset

    def get_daily_data(self, symbol: str, days: int = 25) -> "pd.DataFrame | None":
        if symbol in self._daily_cache:
            return self._daily_cache[symbol]
        # Fetch real daily data (historical, doesn't change)
        try:
            from core.api_dhan import dhan_daily
            _sym = symbol.replace(".NS", "")
            df = dhan_daily(_sym, days_back=days + 5)
            if df is not None and len(df) >= 10:
                df = df.copy()
                df.columns = [c.lower() for c in df.columns]
                self._daily_cache[symbol] = df
                return df
        except Exception:
            pass
        return None

    def get_market_data(self, symbol: str, bars: int = 30) -> "pd.DataFrame | None":
        return self.get_intraday_data(symbol, 5, 5)

    def advance_cursor(self, symbol: str, candles: int = 1) -> bool:
        """Advance time by N candles. Returns False if end of data."""
        df = self._data.get(symbol)
        if df is None:
            return False
        cursor = self._cursor.get(symbol, 0)
        new_cursor = min(cursor + candles, len(df))
        self._cursor[symbol] = new_cursor
        return new_cursor < len(df)

    def advance_all(self, candles: int = 1) -> bool:
        """Advance all symbols. Returns False if all exhausted."""
        any_active = False
        for sym in self._data:
            if self.advance_cursor(sym, candles):
                any_active = True
        return any_active

    def set_start(self, candles_from_start: int = 30) -> None:
        """Set all cursors to N candles in (enough history for indicators)."""
        for sym, df in self._data.items():
            self._cursor[sym] = min(candles_from_start, len(df))


def fetch_replay_data(symbols: List[str], replay_date: str,
                      interval: int = 5) -> Dict[str, "pd.DataFrame"]:
    """Fetch historical intraday data for replay.

    Priority: Dhan API (primary) -> LiquidityScanner (yfinance fallback).
    Returns {symbol: DataFrame}.
    """
    import pandas as pd
    import time as _t

    data = {}
    target = datetime.strptime(replay_date, "%Y-%m-%d").date()

    # Try Dhan first (actual data source the system uses)
    try:
        from core.scanner import LiquidityScanner
        scanner = LiquidityScanner()
        for sym in symbols:
            try:
                _t.sleep(0.5)  # rate limit
                df = scanner.get_intraday_data(sym, interval=interval, days_back=5)
                if df is not None and len(df) >= 20:
                    # Filter to target date
                    if 'date' in df.columns:
                        df['date'] = pd.to_datetime(df['date'])
                        mask = df['date'].dt.date == target
                        day_df = df[mask].reset_index(drop=True)
                        if len(day_df) >= 20:
                            data[sym] = day_df
                            log.info(f"[Replay] {sym}: {len(day_df)} candles (Dhan/scanner)")
                            continue
                    # If no date filter possible, use last day
                    data[sym] = df.tail(75).reset_index(drop=True)  # ~1 trading day
                    log.info(f"[Replay] {sym}: {len(data[sym])} candles (last session)")
            except Exception as e:
                log.debug(f"[Replay] {sym} scanner failed: {e}")
        if data:
            return data
    except Exception as e:
        log.warning(f"[Replay] scanner init failed: {e}")

    # Dhan-only intraday (no yfinance)
    for sym in symbols:
        try:
            from core.api_dhan import dhan_intraday
            _sym = sym.replace(".NS", "")
            df = dhan_intraday(_sym, interval_min=interval, days_back=7)
            if df is None or df.empty:
                log.warning(f"[Replay] no data for {sym}")
                continue

            df = df.copy()
            df.columns = [c.lower() for c in df.columns]

            # Filter to target date only
            if 'datetime' in df.columns:
                df['date'] = df['datetime']
            dt_col = 'date' if 'date' in df.columns else df.columns[0]
            df[dt_col] = pd.to_datetime(df[dt_col])
            mask = df[dt_col].dt.date == target
            day_df = df[mask].reset_index(drop=True)

            if len(day_df) >= 20:
                data[sym] = day_df
                log.info(f"[Replay] {sym}: {len(day_df)} candles for {replay_date}")
            else:
                log.warning(f"[Replay] {sym}: only {len(day_df)} candles, skipping")
        except Exception as e:
            log.warning(f"[Replay] {sym} fetch failed: {e}")

    return data


def run_replay(symbols: List[str], replay_date: str,
               capital: float = 100000, step_candles: int = 1) -> ReplayResult:
    """Run full replay through agent pipeline.

    Steps through historical data candle-by-candle, triggering the same
    event flow as live trading.
    """
    from core.agent_bus import SharedState, EventBus
    from core.signal_engine import SignalEngine

    # Enable replay mode — bypasses time-of-day filters in SignalEngine
    os.environ["REPLAY_MODE"] = "1"

    log.info(f"[Replay] === Starting replay for {replay_date} ===")
    log.info(f"[Replay] Symbols: {symbols}")

    # Fetch data
    data = fetch_replay_data(symbols, replay_date)
    if not data:
        log.error("[Replay] no data fetched — abort")
        return ReplayResult(replay_date)

    result = ReplayResult(replay_date)
    scanner = ReplayScanner(data)
    scanner.set_start(30)  # 30 candles of history for indicators

    # Minimal agent setup (signal generation only — no execution in replay)
    state = SharedState()
    bus = EventBus()
    engine = SignalEngine()

    # Step through time
    step = 0
    max_steps = 500  # safety limit
    while step < max_steps:
        step += 1

        # Scan all symbols at current cursor position
        for sym in data:
            try:
                df = scanner.get_intraday_data(sym, 5, 5)
                if df is None or len(df) < 25:
                    continue

                signal = engine.generate_signal(sym, df)
                if signal is None:
                    continue

                entry = signal.entry_price
                atr = signal.atr or (entry * 0.01)
                sl_dist = atr * 1.5
                sl_dist = max(sl_dist, entry * 0.010)
                sl_dist = min(sl_dist, entry * 0.025)

                if signal.direction == "long":
                    sl = entry - sl_dist
                    target = entry + sl_dist * 3.5
                else:
                    sl = entry + sl_dist
                    target = entry - sl_dist * 3.5

                sig_data = {
                    "symbol": sym,
                    "direction": signal.direction,
                    "entry_price": entry,
                    "sl_price": round(sl, 2),
                    "target_price": round(target, 2),
                    "strength": signal.strength,
                    "patterns": signal.patterns or [],
                    "rsi": signal.rsi,
                    "candle_index": scanner._cursor.get(sym, 0),
                }
                result.add_signal(sig_data)

                # Simulate trade outcome: walk forward and check SL/target hit
                outcome = _simulate_trade(data[sym], scanner._cursor.get(sym, 0),
                                          signal.direction, entry, sl, target)
                if outcome:
                    outcome["patterns"] = signal.patterns or []
                    outcome["symbol"] = sym
                    outcome["direction"] = signal.direction
                    outcome["entry_price"] = entry
                    outcome["sl_price"] = round(sl, 2)
                    result.add_trade_close(outcome)

                    # Feed back into signal memory for learning
                    try:
                        from core.signal_memory import get_signal_memory
                        mem = get_signal_memory()
                        won = float(outcome.get("pnl", 0)) > 0
                        risk = abs(entry - sl) if sl else entry * 0.01
                        exit_p = float(outcome.get("exit_price", entry))
                        if signal.direction == "long":
                            r_mult = (exit_p - entry) / risk if risk > 0 else 0
                        else:
                            r_mult = (entry - exit_p) / risk if risk > 0 else 0
                        mem.record_outcome(
                            symbol=sym, direction=signal.direction,
                            patterns=signal.patterns or [],
                            entry_hour=10, won=won, r_multiple=round(r_mult, 2),
                            pnl_pct=float(outcome.get("pnl_pct", 0)),
                        )
                    except Exception:
                        pass

            except Exception as e:
                log.debug(f"[Replay] {sym} error at step {step}: {e}")

        # Advance time
        if not scanner.advance_all(step_candles):
            break

    # Save results
    path = result.save()
    summary = result.summary()
    log.info(f"[Replay] === Done ===")
    log.info(f"[Replay] Signals={summary['signals']} Trades={summary['trades_closed']} "
             f"WR={summary['win_rate']} PnL={summary['total_pnl']}")
    log.info(f"[Replay] Results saved to {path}")

    # Clean up replay mode flag
    os.environ.pop("REPLAY_MODE", None)

    return result


def _simulate_trade(df: "pd.DataFrame", entry_idx: int,
                    direction: str, entry: float, sl: float,
                    target: float) -> Optional[Dict]:
    """Walk forward from entry candle and check if SL or target hit first."""
    if entry_idx >= len(df):
        return None

    for i in range(entry_idx, min(entry_idx + 75, len(df))):  # max 75 candles (6.25 hrs)
        row = df.iloc[i]
        high = float(row.get("high", 0))
        low = float(row.get("low", 0))
        close = float(row.get("close", 0))

        if direction == "long":
            if low <= sl:
                pnl = sl - entry
                return {"exit_price": sl, "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl / entry, 4),
                        "reason": "SL_HIT", "bars_held": i - entry_idx}
            if high >= target:
                pnl = target - entry
                return {"exit_price": target, "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl / entry, 4),
                        "reason": "TARGET_HIT", "bars_held": i - entry_idx}
        else:
            if high >= sl:
                pnl = entry - sl
                return {"exit_price": sl, "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl / entry, 4),
                        "reason": "SL_HIT", "bars_held": i - entry_idx}
            if low <= target:
                pnl = entry - target
                return {"exit_price": target, "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl / entry, 4),
                        "reason": "TARGET_HIT", "bars_held": i - entry_idx}

    # Time exit at last candle
    last_close = float(df.iloc[min(entry_idx + 74, len(df) - 1)]["close"])
    if direction == "long":
        pnl = last_close - entry
    else:
        pnl = entry - last_close
    return {"exit_price": last_close, "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / entry, 4),
            "reason": "TIME_EXIT", "bars_held": min(75, len(df) - entry_idx)}


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, _PROJECT_ROOT)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s - %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Replay Engine — backtest through agent pipeline")
    parser.add_argument("--date", required=True, help="Replay date YYYY-MM-DD")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols (default: top 20)")
    parser.add_argument("--all", action="store_true", help="Use full F&O universe")
    parser.add_argument("--capital", type=float, default=100000)
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    elif args.all:
        from core.universe import FO_UNIVERSE
        symbols = FO_UNIVERSE[:50]  # cap at 50 for replay speed
    else:
        # Default: top liquid stocks
        symbols = ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "SBIN",
                    "BHARTIARTL", "INFY", "KOTAKBANK", "AXISBANK", "ITC",
                    "TITAN", "VEDL", "BPCL", "CIPLA", "DIVISLAB",
                    "DRREDDY", "JSWSTEEL", "TATASTEEL", "BAJFINANCE", "WIPRO"]

    result = run_replay(symbols, args.date, args.capital)
    summary = result.summary()

    print("\n" + "=" * 60)
    print(f"  REPLAY RESULTS — {args.date}")
    print("=" * 60)
    print(f"  Signals generated: {summary['signals']}")
    print(f"  Trades closed:     {summary['trades_closed']}")
    print(f"  Win rate:          {summary['win_rate']}")
    print(f"  Total P&L:         Rs.{summary['total_pnl']:+,.2f}")
    print(f"  Avg P&L/trade:     Rs.{summary['avg_pnl']:+,.2f}")
    print(f"  Elapsed:           {summary['elapsed_sec']}s")
    print()

    if summary.get("pattern_stats"):
        print("  Pattern Breakdown:")
        for pat, stats in sorted(summary["pattern_stats"].items(),
                                  key=lambda x: x[1]["wins"] + x[1]["losses"],
                                  reverse=True)[:10]:
            total = stats["wins"] + stats["losses"]
            wr = stats["wins"] / total * 100 if total else 0
            print(f"    {pat:30s} WR={wr:4.0f}% ({stats['wins']}W/{stats['losses']}L) "
                  f"PnL={stats['total_pnl']:+.2f}")
    print("=" * 60)
