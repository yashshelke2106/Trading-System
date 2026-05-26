"""
Delivery % from NSE bhavcopy — institutional accumulation tell.

Premise: high delivery % (≥ 50%) on a daily bar = shares actually changed hands
(institutional buyers / long-term holders), not just intraday jobber churn. Low
delivery (< 35%) on a big-volume day = day-traders pushing price, fades fast.

Long entries on stocks with rolling 5-day avg delivery < 35% have weak follow-
through. Block them.

Data source: NSE bhavcopy (free, EOD). 5-day rolling delivery % per symbol.
Cached daily.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Tuple, List

import requests

log = logging.getLogger(__name__)

# Thresholds tuned for F&O liquid stocks
MIN_DELIVERY_PCT_LONG = 35.0   # below this = jobber-driven, block longs
LOOKBACK_DAYS = 5              # rolling avg window
CACHE_TTL_SEC = 86400          # 24h
CACHE_PATH = Path("logs/delivery_pct_cache.json")


# NSE delivery bhavcopy endpoint
# Each line: SYMBOL,SERIES,DATE,DELIV_QTY,DELIV_PER ...
# Multiple URL patterns NSE has used; we try in order.
_BHAVCOPY_URL_PATTERNS = [
    "https://www.nseindia.com/api/historical/sec-bhavdata?symbol={sym}&from={frm}&to={to}",
]

_SESSION: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """NSE requires browser-like headers + cookie warm-up."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/csv, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        s.get("https://www.nseindia.com/", timeout=10)
        s.get("https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE", timeout=10)
    except Exception:
        pass
    _SESSION = s
    return s


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
        log.debug(f"[Delivery] cache save failed: {e}")


def _fetch_nse_delivery(symbol: str, days: int = LOOKBACK_DAYS + 5) -> List[Dict]:
    """
    Fetch last N days delivery data from NSE.
    Returns list of {date, deliv_qty, deliv_pct, total_qty} dicts.
    """
    end = date.today()
    start = end - timedelta(days=days * 2 + 4)  # buffer for weekends/holidays
    s = _get_session()
    url = _BHAVCOPY_URL_PATTERNS[0].format(
        sym=symbol.upper(),
        frm=start.strftime("%d-%m-%Y"),
        to=end.strftime("%d-%m-%Y"),
    )
    try:
        resp = s.get(url, timeout=15)
        if resp.status_code != 200:
            log.debug(f"[Delivery] {symbol}: HTTP {resp.status_code}")
            return []
        data = resp.json()
        rows = data.get("data") or []
        out: List[Dict] = []
        for r in rows:
            # NSE response uses inconsistent key casing; normalize
            keys = {k.strip(): v for k, v in r.items()}
            try:
                d_str = keys.get("CH_TIMESTAMP") or keys.get("mTIMESTAMP") or ""
                deliv_pct = float(keys.get("COP_DELIV_PERC") or
                                  keys.get("CA_DLY_PERC") or 0)
                deliv_qty = float(keys.get("COP_DELIV_QTY") or
                                  keys.get("CA_DELIVRY_QTY") or 0)
                tot_qty = float(keys.get("COP_TOTAL_TRADES") or
                                keys.get("CH_TOT_TRADED_QTY") or 0)
                if deliv_pct > 0:
                    out.append({
                        "date": d_str,
                        "deliv_qty": deliv_qty,
                        "deliv_pct": deliv_pct,
                        "tot_qty": tot_qty,
                    })
            except Exception:
                continue
        return out[-days:]
    except Exception as e:
        log.debug(f"[Delivery] {symbol} fetch failed: {e}")
        return []


def get_delivery_pct(symbol: str) -> Optional[float]:
    """
    Return rolling-5-day average delivery % for symbol, or None if unavailable.
    """
    sym = symbol.upper().strip()
    cache = _load_cache()
    entry = cache.get(sym)
    now = time.time()
    if entry and (now - entry.get("fetched_at", 0)) < CACHE_TTL_SEC:
        return entry.get("avg_deliv_pct")

    rows = _fetch_nse_delivery(sym, days=LOOKBACK_DAYS + 3)
    if not rows:
        # Negative cache short TTL to avoid hammering on persistent failures
        cache[sym] = {"fetched_at": now, "avg_deliv_pct": None}
        _save_cache(cache)
        return None

    recent = rows[-LOOKBACK_DAYS:]
    avg = sum(r["deliv_pct"] for r in recent) / max(len(recent), 1)
    cache[sym] = {
        "fetched_at": now,
        "avg_deliv_pct": round(avg, 1),
        "samples": len(recent),
    }
    _save_cache(cache)
    return round(avg, 1)


def check_delivery(symbol: str, direction: str,
                   min_pct: float = MIN_DELIVERY_PCT_LONG) -> Tuple[bool, Dict]:
    """
    Gate function. Block long entries when delivery % too low.
    Shorts are not blocked here (different logic — shorts thrive on jobber-led
    fades; future enhancement could invert this for shorts).
    """
    if direction != "long":
        return True, {"reason": "short_no_check"}

    pct = get_delivery_pct(symbol)
    if pct is None:
        # Degrade gracefully — no data, no block
        return True, {"reason": "no_delivery_data", "deliv_pct": None}

    if pct < min_pct:
        return False, {
            "reason": f"low_delivery_{pct:.0f}_lt_{min_pct:.0f}",
            "deliv_pct": pct,
        }
    return True, {"reason": "ok", "deliv_pct": pct}


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    for sym in ["RELIANCE", "HDFCBANK", "INFY", "TATAMOTORS", "TCS"]:
        ok, info = check_delivery(sym, "long")
        print(f"{sym:12s}  ok={ok}  {info}")
