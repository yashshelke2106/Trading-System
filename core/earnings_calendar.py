"""
Earnings blackout gate.

Premise: Indian F&O stocks gap 5-15% on earnings nights. Even a clean swing
setup is roulette if earnings drop 2-5 days into the hold. Block all entries
EARNINGS_BLACKOUT_DAYS before next earnings.

Data sources tried in order:
  1. yfinance ticker.calendar (free, somewhat reliable for large caps)
  2. yfinance ticker.earnings_dates (deeper history + future estimates)
  3. Manual CSV override at logs/earnings_calendar_manual.csv (user-editable)

Cache: 24h per symbol in logs/earnings_cache.json — earnings dates don't
change intra-day. Re-fetch on miss or stale.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Tuple, List

log = logging.getLogger(__name__)

EARNINGS_BLACKOUT_DAYS = 5    # block entries this many trading days before earnings
CACHE_TTL_SEC          = 86400  # 24h
CACHE_PATH = Path("logs/earnings_cache.json")
MANUAL_OVERRIDE_PATH = Path("logs/earnings_calendar_manual.csv")


def _ensure_cache_dir() -> None:
    CACHE_PATH.parent.mkdir(exist_ok=True)


def _load_cache() -> Dict[str, Dict]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: Dict[str, Dict]) -> None:
    _ensure_cache_dir()
    try:
        CACHE_PATH.write_text(json.dumps(cache, default=str))
    except Exception as e:
        log.debug(f"[Earnings] cache save failed: {e}")


def _load_manual_overrides() -> Dict[str, List[str]]:
    """CSV format: symbol,YYYY-MM-DD per row (one earnings date per row)."""
    if not MANUAL_OVERRIDE_PATH.exists():
        return {}
    overrides: Dict[str, List[str]] = {}
    try:
        with MANUAL_OVERRIDE_PATH.open() as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 2 or row[0].startswith("#"):
                    continue
                sym, dt = row[0].strip().upper(), row[1].strip()
                overrides.setdefault(sym, []).append(dt)
    except Exception as e:
        log.debug(f"[Earnings] manual CSV parse failed: {e}")
    return overrides


def _yfinance_ticker_for(symbol: str) -> str:
    """NSE F&O symbol → yfinance ticker (e.g. INFY → INFY.NS)."""
    s = symbol.upper().strip()
    # Apply known re-maps from project (see CLAUDE.md ticker map)
    remap = {
        "TATAMOTORS": "TMCV.NS",
        "MCDOWELL-N": "UNITDSPR.NS",
        "DEEPAKNT": "DEEPAKNTR.NS",
    }
    if s in remap:
        return remap[s]
    return f"{s}.NS"


def _fetch_yf_earnings(symbol: str) -> List[str]:
    """Return list of upcoming earnings dates (YYYY-MM-DD)."""
    # Dhan has no earnings-calendar endpoint and yfinance is permanently
    # removed. Earnings-date enrichment is disabled until a dedicated
    # fundamentals provider is wired. Returns [] (no known earnings) so the
    # expiry/earnings gates degrade open rather than crash.
    return []
    try:  # dead path kept for when a real provider replaces yfinance
        import yfinance as yf
        ticker = yf.Ticker(_yfinance_ticker_for(symbol))
        dates: List[str] = []
        try:
            ed = ticker.earnings_dates
            if ed is not None and not ed.empty:
                for idx in ed.index:
                    try:
                        d = idx.date() if hasattr(idx, "date") else None
                        if d is None:
                            continue
                        if d >= date.today():
                            dates.append(d.isoformat())
                    except Exception:
                        continue
        except Exception:
            pass
        # Fallback: ticker.calendar
        if not dates:
            try:
                cal = ticker.calendar
                if cal is not None and not cal.empty and "Earnings Date" in cal.index:
                    ed_val = cal.loc["Earnings Date"].iloc[0]
                    if ed_val is not None:
                        try:
                            d = ed_val.date() if hasattr(ed_val, "date") else None
                            if d and d >= date.today():
                                dates.append(d.isoformat())
                        except Exception:
                            pass
            except Exception:
                pass
        return sorted(set(dates))
    except Exception as e:
        log.debug(f"[Earnings] yf fetch failed {symbol}: {e}")
        return []


def get_next_earnings(symbol: str) -> Optional[str]:
    """Return next upcoming earnings date YYYY-MM-DD or None."""
    sym = symbol.upper().strip()

    # 1. Manual overrides win
    manuals = _load_manual_overrides()
    if sym in manuals:
        future = [d for d in manuals[sym] if d >= date.today().isoformat()]
        if future:
            return min(future)

    # 2. Cache
    cache = _load_cache()
    entry = cache.get(sym)
    now = time.time()
    if entry and (now - entry.get("fetched_at", 0)) < CACHE_TTL_SEC:
        dates = [d for d in entry.get("dates", []) if d >= date.today().isoformat()]
        if dates:
            return min(dates)

    # 3. Fresh yfinance fetch
    dates = _fetch_yf_earnings(sym)
    cache[sym] = {"fetched_at": now, "dates": dates}
    _save_cache(cache)
    future = [d for d in dates if d >= date.today().isoformat()]
    if future:
        return min(future)
    return None


def is_blackout(symbol: str, blackout_days: int = EARNINGS_BLACKOUT_DAYS
                ) -> Tuple[bool, Dict]:
    """
    Return (blocked, info).
    True if next earnings is within `blackout_days` calendar days from today.
    """
    nxt = get_next_earnings(symbol)
    if nxt is None:
        # FIX (audit #9): no data means we CANNOT confirm the stock isn't about
        # to report. The feed is currently dead, so this path is the norm, not
        # the exception — trading here is unhedged gap risk. Fail closed when
        # config.REQUIRE_EARNINGS_DATA is set.
        try:
            import config as _cfg
            if bool(getattr(_cfg, "REQUIRE_EARNINGS_DATA", False)):
                return True, {"reason": "no_earnings_data_fail_closed",
                              "next_earnings": None}
        except Exception:
            pass
        return False, {"reason": "no_earnings_data", "next_earnings": None}
    try:
        next_dt = date.fromisoformat(nxt)
    except Exception:
        return False, {"reason": "bad_earnings_format", "next_earnings": nxt}
    days_to = (next_dt - date.today()).days
    if 0 <= days_to <= blackout_days:
        return True, {
            "reason": f"earnings_in_{days_to}d",
            "next_earnings": nxt,
            "days_to": days_to,
        }
    return False, {
        "reason": "ok",
        "next_earnings": nxt,
        "days_to": days_to,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    for sym in ["INFY", "RELIANCE", "HDFCBANK", "TCS", "TATAMOTORS"]:
        blocked, info = is_blackout(sym)
        print(f"{sym:12s}  blocked={blocked}  {info}")
