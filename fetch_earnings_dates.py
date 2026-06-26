"""
fetch_earnings_dates.py — standalone earnings-date + surprise fetcher for the
mid/small-cap PEAD study (path #2).

The repo's logs/earnings_cache.json is empty (broken historical fetch). This
script fetches earnings announcement dates AND Surprise(%) via yfinance
get_earnings_dates (confirmed reaching back to ~2013), and caches one parquet
per symbol at logs/earnings_dates/<SYM>.parquet. Resumable: skips symbols
already cached unless --refresh.

Usage:
  python fetch_earnings_dates.py --universe-asof 2019-07-01      # fetch the midcap band as of a date
  python fetch_earnings_dates.py --symbols BATAINDIA,DIXON,PAGEIND
  python fetch_earnings_dates.py --all-archive                  # every symbol in the bhavcopy archive
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import time
from typing import List

import pandas as pd

LOG = logging.getLogger("earnings_fetch")
OUT_DIR = os.path.join("logs", "earnings_dates")

# NSE symbol -> yfinance ticker overrides (most are just <SYMBOL>.NS).
# Mirrors the edge cases documented in core/api_dhan.py.
_OVERRIDES = {
    "TATAMOTORS": "TMCV.NS",
    "MCDOWELL-N": "UNITDSPR.NS",
}


def _yf_ticker(symbol: str) -> str:
    return _OVERRIDES.get(symbol.upper(), f"{symbol.upper()}.NS")


def fetch_one(symbol: str, limit: int = 100) -> pd.DataFrame:
    """Fetch earnings events for one symbol. Returns DataFrame
    [date, surprise_pct, reported_eps, eps_estimate] (may be empty)."""
    import yfinance as yf
    t = yf.Ticker(_yf_ticker(symbol))
    try:
        df = t.get_earnings_dates(limit=limit)
    except Exception as e:
        LOG.warning("  %s: fetch error %s", symbol, e)
        return pd.DataFrame()
    if df is None or len(df) == 0:
        return pd.DataFrame()
    df = df.reset_index()
    date_col = next((c for c in df.columns if "Date" in str(c)), df.columns[0])
    out = pd.DataFrame({
        "date": pd.to_datetime(df[date_col]).dt.tz_localize(None).dt.normalize(),
        "surprise_pct": pd.to_numeric(df.get("Surprise(%)"), errors="coerce"),
        "reported_eps": pd.to_numeric(df.get("Reported EPS"), errors="coerce"),
        "eps_estimate": pd.to_numeric(df.get("EPS Estimate"), errors="coerce"),
    })
    # keep only ANNOUNCED events (reported EPS present); drop future placeholders
    out = out.dropna(subset=["reported_eps"]).sort_values("date").reset_index(drop=True)
    return out


def _universe_symbols(asof: str) -> List[str]:
    from core.midcap_universe import build_universe
    return build_universe(asof).symbols


def _archive_symbols() -> List[str]:
    paths = glob.glob(os.path.join("logs", "bhavcopy_archive", "symbols", "*.parquet"))
    return [os.path.splitext(os.path.basename(p))[0] for p in paths]


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbols", type=str, help="comma-separated NSE symbols")
    g.add_argument("--universe-asof", type=str, help="fetch the midcap band as of YYYY-MM-DD")
    g.add_argument("--all-archive", action="store_true", help="every symbol in the bhavcopy archive")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--refresh", action="store_true", help="re-fetch even if cached")
    ap.add_argument("--pace", type=float, default=0.4)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs(OUT_DIR, exist_ok=True)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.universe_asof:
        symbols = _universe_symbols(args.universe_asof)
    else:
        symbols = _archive_symbols()

    LOG.info("Earnings fetch: %d symbols → %s", len(symbols), OUT_DIR)
    ok = empty = skipped = 0
    for i, sym in enumerate(symbols, 1):
        path = os.path.join(OUT_DIR, f"{sym}.parquet")
        if os.path.exists(path) and not args.refresh:
            skipped += 1
            continue
        df = fetch_one(sym, limit=args.limit)
        if len(df):
            df.to_parquet(path)
            ok += 1
            if i % 25 == 0 or i == len(symbols):
                LOG.info("  [%d/%d] %s: %d events (%s..%s)", i, len(symbols), sym,
                         len(df), df["date"].min().date(), df["date"].max().date())
        else:
            empty += 1
        time.sleep(args.pace)
    LOG.info("Done: %d fetched, %d empty, %d skipped (already cached)", ok, empty, skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
