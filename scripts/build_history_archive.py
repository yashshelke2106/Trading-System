"""
Bulk 10-year daily-history archive for the whole system universe, via indianapi.in.

WHAT:
    For every symbol in the F&O universe, pull up to `period` of daily bars and
    write one CSV per symbol to data/history/<SYMBOL>.csv, plus a manifest.

WHY a script (not inline):
    ~170 symbols x network = minutes + rate limits. This is resumable (skips
    symbols already on disk), throttled, and retries transient failures, so it
    can be re-run safely until the archive is complete.

SHAPE SAFETY:
    The FIRST fetched symbol is reported in detail - row count, date range, and
    whether the payload carried full OHLC or only close. If it's close-only,
    candlestick-body patterns are impossible from this source (keep yfinance for
    candles); the archive is still valid for returns / the news-reaction study.
    Use --probe to fetch ONLY the first symbol and stop, before committing to
    the full run.

RUN (on the real machine, key installed):
    python -m scripts.build_history_archive --probe                 # 1 symbol, inspect shape
    python -m scripts.build_history_archive                         # full FO_UNIVERSE, 10yr
    python -m scripts.build_history_archive --top100 --period 5yr
    python -m scripts.build_history_archive --symbols RELIANCE,INFY --refresh
    python -m scripts.build_history_archive --resume                # continue after interruption (default)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Dict, List

# allow `python scripts/build_history_archive.py` as well as -m
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.universe import FO_UNIVERSE, TOP100_LIQUID  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(_ROOT, "data", "history")
MANIFEST = os.path.join(OUT_DIR, "_manifest.json")


def _has_real_ohlc(df) -> bool:
    """True if O/H/L are genuinely distinct from close (not the get_daily
    close-fill fallback)."""
    if df.empty or not {"open", "high", "low", "close"}.issubset(df.columns):
        return False
    # get_daily fills o/h/l = close when the source is close-only -> all equal.
    sample = df.tail(50)
    return bool(((sample["high"] - sample["low"]).abs() > 1e-9).any())


def _fetch_one(api, symbol: str, period: str, retries: int = 3, backoff: float = 2.0):
    last_err = None
    for attempt in range(retries):
        try:
            df = api.get_daily(symbol, period=period)
            return df, None
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            time.sleep(backoff * (attempt + 1))
    return None, last_err


def _load_manifest() -> Dict:
    if os.path.exists(MANIFEST):
        try:
            return json.load(open(MANIFEST, encoding="utf-8"))
        except Exception:
            pass
    return {"built_at": None, "period": None, "symbols": {}}


def _save_manifest(m: Dict) -> None:
    m["built_at"] = datetime.now().isoformat(timespec="seconds")
    json.dump(m, open(MANIFEST, "w", encoding="utf-8"), indent=2, default=str)


_YF_PERIOD = {"1m": "1mo", "6m": "6mo", "1yr": "1y", "3yr": "3y",
              "5yr": "5y", "10yr": "10y", "max": "max"}


class _YFSource:
    """yfinance OHLC source - REAL candlestick bodies (indianapi historical is
    close-only). Survivors-only, but the F&O universe is all still-listed
    large-caps so that bias is negligible here."""
    name = "yfinance"

    def get_daily(self, symbol, period="10yr"):
        import pandas as pd
        import yfinance as yf
        from core.news_reaction import _yf_ticker
        raw = yf.download(_yf_ticker(symbol), period=_YF_PERIOD.get(period, "10y"),
                          interval="1d", progress=False, auto_adjust=False)
        if raw is None or raw.empty:
            return pd.DataFrame()
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        df = raw.reset_index()[["Date", "Open", "High", "Low", "Close", "Volume"]]
        df.columns = ["date", "open", "high", "low", "close", "volume"]
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df.sort_values("date").reset_index(drop=True)


def build(symbols: List[str], period: str, sleep: float,
          refresh: bool, probe: bool, source: str = "yfinance") -> int:
    if source == "indianapi":
        from core.api_indianstock import IndianStockAPI  # keyless --help stays fine
        api = IndianStockAPI()  # raises clean error if no key
    else:
        api = _YFSource()      # default: real OHLC candles
    os.makedirs(OUT_DIR, exist_ok=True)
    manifest = _load_manifest()
    manifest["period"] = period

    ok = failed = skipped = close_only = 0
    first_reported = False

    for i, sym in enumerate(symbols, 1):
        safe = sym.replace("/", "_")
        path = os.path.join(OUT_DIR, f"{safe}.csv")

        if not refresh and os.path.exists(path):
            skipped += 1
            continue

        df, err = _fetch_one(api, sym, period)
        if df is None or df.empty:
            failed += 1
            manifest["symbols"][sym] = {"status": "failed", "error": err or "empty"}
            print(f"[{i}/{len(symbols)}] {sym:14s} FAILED  {err or 'empty payload'}")
            time.sleep(sleep)
            continue

        has_ohlc = _has_real_ohlc(df)
        if not has_ohlc:
            close_only += 1
        df.to_csv(path, index=False)
        ok += 1
        rng = f"{df['date'].iloc[0]} ... {df['date'].iloc[-1]}"
        manifest["symbols"][sym] = {
            "status": "ok", "rows": len(df), "range": rng,
            "ohlc": has_ohlc, "file": os.path.relpath(path, _ROOT),
        }

        if not first_reported:
            first_reported = True
            print("\n" + "=" * 60)
            print(f"SHAPE CHECK - first symbol: {sym}")
            print(f"  rows: {len(df)}   range: {rng}")
            print(f"  columns: {list(df.columns)}")
            print(f"  full OHLC candles: {'YES' if has_ohlc else 'NO (close-only)'}")
            if not has_ohlc:
                print("  [!] close-only -> NOT usable for candlestick bodies.")
                print("    Keep yfinance for candles; this archive still good")
                print("    for returns + news-reaction study.")
            print("=" * 60 + "\n")
            if probe:
                _save_manifest(manifest)
                print("--probe set: stopping after 1 symbol.")
                return 0

        print(f"[{i}/{len(symbols)}] {sym:14s} ok  {len(df):5d} rows  "
              f"{'OHLC' if has_ohlc else 'close-only'}  {rng}")
        _save_manifest(manifest)   # checkpoint every symbol -> safe resume
        time.sleep(sleep)

    _save_manifest(manifest)
    print("\n" + "=" * 60)
    print(f"DONE  ok={ok}  failed={failed}  skipped(existing)={skipped}  "
          f"close_only={close_only}")
    print(f"archive: {OUT_DIR}")
    print(f"manifest: {MANIFEST}")
    if close_only:
        print(f"[!] {close_only} symbol(s) came back close-only - candles from "
              f"yfinance for those.")
    print("=" * 60)
    return 0


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description="Bulk 10yr daily history via indianapi.in")
    p.add_argument("--top100", action="store_true", help="TOP100_LIQUID instead of full FO_UNIVERSE")
    p.add_argument("--symbols", type=str, default="", help="comma-separated override list")
    p.add_argument("--period", type=str, default="10yr",
                   help="1m/6m/1yr/3yr/5yr/10yr/max (default 10yr)")
    p.add_argument("--sleep", type=float, default=1.5, help="seconds between calls (rate-limit)")
    p.add_argument("--refresh", action="store_true", help="re-download even if CSV exists")
    p.add_argument("--probe", action="store_true", help="fetch only the first symbol, report shape, stop")
    p.add_argument("--source", choices=["yfinance", "indianapi"], default="yfinance",
                   help="yfinance=real OHLC candles (default); indianapi=close-only")
    args = p.parse_args(argv)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.top100:
        symbols = list(TOP100_LIQUID)
    else:
        symbols = list(FO_UNIVERSE)

    print(f"universe: {len(symbols)} symbols   period: {args.period}   "
          f"source: {args.source}   out: {OUT_DIR}")
    # yfinance has no per-call quota; safe to drop the throttle unless overridden
    sleep = args.sleep if args.source == "indianapi" else min(args.sleep, 0.2)
    return build(symbols, args.period, sleep, args.refresh, args.probe, args.source)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
