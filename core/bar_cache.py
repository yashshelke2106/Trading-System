"""
bar_cache.py — local daily-bar cache to kill the Dhan 429 bottleneck.

The research loop (edge hunts, permutation tests, walk-forwards) re-fetches the
SAME daily bars from Dhan every run, and Dhan rate-limits /charts/* hard (429 ->
30s global backoff). A single 120-name run can stall 10-25 min on the fetch alone.

This cache stores ONE parquet per symbol holding the DEEPEST history fetched, and
serves any shorter window by slicing. After the first warm, every research run
reads bars from local disk in milliseconds. Daily bars only change once/day, so a
few-day staleness tolerance is safe; a stale or short cache transparently refetches.

Drop-in: research code that did
    from backtest_live_pipeline import fetch_daily
can switch to
    from core.bar_cache import cached_daily as fetch_daily
with no other changes (same signature, same Dhan-shaped DataFrame).

Warm the whole F&O universe once (gently paced to respect the Dhan throttle):
    .venv/Scripts/python.exe -m core.bar_cache --warm --days 3000
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(_ROOT, "logs", "bar_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# Always fetch/store the deepest history on a miss, then serve any sub-window from
# it. So one deep fetch satisfies every shallower request without re-hitting Dhan.
_MAX_DAYS = 3000
_NEED_COLS = ("open", "high", "low", "close", "volume")


def _path(symbol: str) -> str:
    safe = symbol.replace("/", "_").replace("\\", "_")
    return os.path.join(CACHE_DIR, f"{safe}.parquet")


def _read(symbol: str) -> Optional[pd.DataFrame]:
    p = _path(symbol)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
        df.index = pd.to_datetime(df.index)
        return df.sort_index()
    except Exception:
        return None


def _fresh(df: Optional[pd.DataFrame], max_age_days: int) -> bool:
    """Daily bars are fresh if the last bar is within max_age_days CALENDAR days
    (so a Mon run still trusts Fri's close; covers weekends + holidays)."""
    if df is None or df.empty:
        return False
    last = pd.Timestamp(df.index.max()).normalize()
    return (pd.Timestamp.now().normalize() - last).days <= max_age_days


def _covers(df: Optional[pd.DataFrame], days: int) -> bool:
    if df is None or df.empty:
        return False
    span = (pd.Timestamp(df.index.max()) - pd.Timestamp(df.index.min())).days
    return span >= days * 0.9          # within 10% of the requested span


def _window(df: pd.DataFrame, days: int) -> pd.DataFrame:
    cutoff = pd.Timestamp(df.index.max()) - pd.Timedelta(days=days)
    return df[df.index >= cutoff]


def cached_daily(symbol: str, days: int = 1460, *, refresh: bool = False,
                 max_age_days: int = 4) -> Optional[pd.DataFrame]:
    """Drop-in for backtest_live_pipeline.fetch_daily, cache-first.

    HIT  (cache fresh + deep enough): slice and return from disk, no Dhan call.
    MISS (absent / stale / too-short): fetch _MAX_DAYS from Dhan, persist, slice.
    If Dhan fails on a miss but a stale cache exists, serve the stale window
    (better a day-old bar than nothing for research).
    """
    cached = None if refresh else _read(symbol)
    if cached is not None and _fresh(cached, max_age_days) and _covers(cached, days):
        return _window(cached, days)

    # MISS -> deep fetch from Dhan (lazy import avoids a circular dependency,
    # since backtest_live_pipeline imports config/core at module load).
    try:
        from backtest_live_pipeline import fetch_daily as _dhan_fetch_daily
    except Exception:
        return _window(cached, days) if cached is not None else None

    fresh = _dhan_fetch_daily(symbol, max(_MAX_DAYS, days))
    if fresh is None or fresh.empty or not set(_NEED_COLS).issubset(fresh.columns):
        return _window(cached, days) if cached is not None else None
    try:
        fresh = fresh[~fresh.index.duplicated(keep="last")].sort_index()
        fresh.to_parquet(_path(symbol))
    except Exception:
        pass                            # caching is best-effort; never block research
    return _window(fresh, days)


def warm_cache(symbols: List[str], days: int = _MAX_DAYS, pace_sec: float = 0.4,
               refresh: bool = False) -> dict:
    """Prefetch a universe once, gently paced to avoid tripping the Dhan throttle.
    Returns {symbol: bars or 0}. Run after a research job frees the rate limit."""
    out, hits, miss = {}, 0, 0
    for i, s in enumerate(symbols, 1):
        had = os.path.exists(_path(s)) and not refresh
        df = cached_daily(s, days, refresh=refresh)
        n = 0 if df is None else len(df)
        out[s] = n
        if had and n:
            hits += 1
        else:
            miss += 1
            time.sleep(pace_sec)        # only pause when we actually hit Dhan
        print(f"  [{i:>3}/{len(symbols)}] {s:14s} {n:>5} bars"
              f"{'  (cache)' if had and n else ''}")
    print(f"[bar_cache] warmed {len(symbols)} names: {hits} cache-hits, {miss} fetched")
    return out


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warm", action="store_true", help="prefetch the F&O universe")
    ap.add_argument("--days", type=int, default=_MAX_DAYS)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pace", type=float, default=0.4, help="seconds between Dhan fetches")
    ap.add_argument("--refresh", action="store_true", help="ignore + overwrite cache")
    args = ap.parse_args()
    if not args.warm:
        print(f"[bar_cache] cache dir: {CACHE_DIR}")
        print(f"[bar_cache] cached symbols: {len(os.listdir(CACHE_DIR))}")
        return 0
    sys.path.insert(0, _ROOT)
    try:
        from core.universe import FO_UNIVERSE
        syms = list(dict.fromkeys(FO_UNIVERSE))
    except Exception:
        from backtest_live_pipeline import DEFAULT_UNIVERSE
        syms = DEFAULT_UNIVERSE
    if args.limit:
        syms = syms[:args.limit]
    warm_cache(syms, days=args.days, pace_sec=args.pace, refresh=args.refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
