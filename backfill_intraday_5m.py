"""
backfill_intraday_5m.py — standalone 5-min intraday bar fetcher and gap-fade probe.

PURPOSE
-------
The quant-researcher found an overnight gap-fade edge measured OPEN->close.
The mirage-test: if the reversal completes in the first ~5 min of the session,
the realistic fillable entry (09:20 or 09:30) captures nothing.  This script:

  1. Fetches ~60 days of 5-min OHLCV for a liquid F&O subset via yfinance.
  2. Caches to logs/intraday_5m/ (parquet, one file per symbol) — mirrors the
     bar_cache daily pattern so it is instantly re-usable without re-fetching.
  3. Runs the gap-fade feasibility probe:
       - identifies overnight gap events (|open/prev_close - 1| >= 2% and >= 3%)
       - computes fade returns three ways:
           (a) OPEN bar open -> day close  (theoretical/unfillable)
           (b) 09:20 bar open -> day close  (realistic 5-min-after-open entry)
           (c) 09:30 bar open -> day close  (more conservative entry)
       - net of 0.06% and 0.12% round-trip cost
  4. Reports coverage stats and a final verdict.

NON-DESTRUCTIVE:
  - Does NOT touch core/api_dhan.py or its yfinance removal.
  - Does NOT modify logs/bar_cache (daily cache untouched).
  - PAPER_TRADE is irrelevant here — this is pure historical research, no orders.

CACHE LOCATION: logs/intraday_5m/<SYMBOL>.parquet
  Columns: open, high, low, close, volume  (IST-aware DatetimeTZDtype index)

RUN:
    .venv/Scripts/python.exe backfill_intraday_5m.py            # full fetch + probe
    .venv/Scripts/python.exe backfill_intraday_5m.py --probe-only  # skip fetch, use cache
    .venv/Scripts/python.exe backfill_intraday_5m.py --symbol RELIANCE  # single symbol test
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import warnings
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

warnings.filterwarnings("ignore")

# ── SSL bypass (mirrors api_dhan.py — same corporate proxy environment) ─────
if os.environ.get("DHAN_SSL_STRICT", "").lower() not in ("1", "true", "yes"):
    ssl._create_default_https_context = ssl._create_unverified_context
    os.environ["PYTHONHTTPSVERIFY"] = "0"
    os.environ["CURL_CA_BUNDLE"] = ""
    os.environ["REQUESTS_CA_BUNDLE"] = ""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

import requests as _requests

if os.environ.get("DHAN_SSL_STRICT", "").lower() not in ("1", "true", "yes"):
    _orig_req = _requests.Session.request
    def _patched(self, method, url, **kw):
        kw.setdefault("verify", False)
        return _orig_req(self, method, url, **kw)
    _requests.Session.request = _patched  # type: ignore[assignment]

import pandas as pd
import numpy as np

_ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_ROOT, "logs", "intraday_5m")
os.makedirs(CACHE_DIR, exist_ok=True)

EARNINGS_CACHE = os.path.join(_ROOT, "logs", "earnings_cache.json")

# ── Ticker map — mirrors _YF_TICKER_MAP in core/api_dhan.py exactly ────────
_YF_TICKER_MAP: Dict[str, str] = {
    "TATAMOTORS": "TMCV.NS",
    "MCDOWELL-N": "UNITDSPR.NS",
    "DEEPAKNTR":  "DEEPAKNTR.NS",
    "DEEPAKNT":   "DEEPAKNTR.NS",     # legacy alias used in bar_cache filenames
    "MOTHERSON":  "MOTHERSON.NS",
    "PVRINOX":    "PVRINOX.NS",
    "JIOFIN":     "JIOFIN.NS",
    "LICI":       "LICI.NS",
    "IRFC":       "IRFC.NS",
    "ADANIGREEN": "ADANIGREEN.NS",
    "ADANIPOWER": "ADANIPOWER.NS",
    "INDUSTOWER": "INDUSTOWER.NS",
    "CROMPTON":   "CROMPTON.NS",
    "OLECTRA":    "OLECTRA.NS",
    "NYKAA":      "NYKAA.NS",
    "PAYTM":      "PAYTM.NS",
    "ETERNAL":    "ETERNAL.NS",
    "ZOMATO":     "ETERNAL.NS",
    "LTIM":       "LTIM.NS",
    "NALCO":      "NATIONALUM.NS",
    "NATIONALUM": "NATIONALUM.NS",
    "VEDANTA":    "VEDL.NS",
    "VEDL":       "VEDL.NS",
    "KPIT":       "KPITTECH.NS",
    "BOSCH":      "BOSCHLTD.NS",
    "BOSCHLTD":   "BOSCHLTD.NS",
    "WAAREE":     "WAAREEENER.NS",
    "SAMMAAN":    "SAMMAANCAP.NS",
    "ICICISEC":   "ISEC.NS",
    "BAJAJ-AUTO": "BAJAJ-AUTO.NS",
    "M&M":        "M&M.NS",
    "M&MFIN":     "M&MFIN.NS",
}


def _to_yf_ticker(symbol: str) -> str:
    """Convert NSE symbol to yfinance .NS ticker, preserving all edge cases."""
    if symbol in _YF_TICKER_MAP:
        return _YF_TICKER_MAP[symbol]
    # Default: append .NS
    return f"{symbol}.NS"


# ── Liquid F&O subset (~35 names) ──────────────────────────────────────────
# Selection criteria: index constituents + highest avg daily turnover
# Includes sector diversity; excludes illiquid/PSU-only names.
# NIFTY (index) excluded — no F&O gap-fade meaningful (no individual gaps).
FO_SUBSET = [
    # Banks & Financials
    "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "SBIN",
    "BAJFINANCE", "BAJAJFINSV", "INDUSINDBK", "HDFCLIFE",
    # IT
    "TCS", "INFY", "HCLTECH", "WIPRO", "TECHM",
    # Consumer / FMCG
    "HINDUNILVR", "ITC", "BRITANNIA", "NESTLEIND", "TITAN",
    # Energy / Oil
    "RELIANCE", "BPCL", "ONGC", "GAIL",
    # Auto
    "TATAMOTORS", "MARUTI", "M&M", "BAJAJ-AUTO", "EICHERMOT",
    # Pharma
    "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB",
    # Metals / Industrials
    "TATASTEEL", "HINDALCO", "JSWSTEEL", "LT",
]

# IST timezone constant
_IST_TZ = "Asia/Kolkata"
_SESSION_OPEN_HOUR = 9
_SESSION_OPEN_MIN = 15


# ── Cache helpers ────────────────────────────────────────────────────────────

def _cache_path(symbol: str) -> str:
    safe = symbol.replace("/", "_").replace("\\", "_").replace("&", "AND")
    return os.path.join(CACHE_DIR, f"{safe}.parquet")


def _read_cache(symbol: str) -> Optional[pd.DataFrame]:
    p = _cache_path(symbol)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(_IST_TZ)
        return df.sort_index()
    except Exception as e:
        print(f"  [cache-read-err] {symbol}: {e}")
        return None


def _cache_fresh(df: Optional[pd.DataFrame], max_age_hours: int = 4) -> bool:
    """Cache is fresh if the last bar is within max_age_hours (intraday bars change
    during the session; we allow a 4h window so a post-market run still trusts today)."""
    if df is None or df.empty:
        return False
    now = pd.Timestamp.now(tz=_IST_TZ)
    last = pd.Timestamp(df.index.max())
    return (now - last).total_seconds() < max_age_hours * 3600


def _write_cache(symbol: str, df: pd.DataFrame) -> None:
    p = _cache_path(symbol)
    tmp = p + ".tmp"
    try:
        df.to_parquet(tmp)
        os.replace(tmp, p)
    except Exception as e:
        print(f"  [cache-write-err] {symbol}: {e}")


# ── Fetch ────────────────────────────────────────────────────────────────────

def fetch_5m(symbol: str, period: str = "60d") -> pd.DataFrame:
    """Fetch 5-min OHLCV from yfinance for one symbol.

    Returns DataFrame with IST-tz-aware DatetimeIndex and columns:
    open, high, low, close, volume.
    Returns empty DataFrame on failure — caller must detect and skip.

    Point-in-time note: yfinance returns split-adjusted bars (auto_adjust=True).
    For a 60-day feasibility probe this is acceptable; splits in 60d are rare and
    yfinance adjusts them uniformly so there is no look-ahead leakage within the
    window (all bars in the window see the same adjustment factor that existed at
    the END of the window — a very minor bias, but acceptable for a feasibility
    probe; not acceptable for a multi-year backtest).
    """
    import yfinance as yf
    yt = _to_yf_ticker(symbol)
    try:
        raw = yf.Ticker(yt).history(period=period, interval="5m", auto_adjust=True)
    except Exception as e:
        print(f"  [yf-fetch-err] {symbol} ({yt}): {e}")
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    # Normalise column names
    raw.columns = [c.lower() for c in raw.columns]
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in raw.columns]
    df = raw[keep].copy()

    # Ensure IST-tz-aware index
    if df.index.tz is None:
        df.index = df.index.tz_localize(_IST_TZ, ambiguous="NaT", nonexistent="NaT")
    else:
        df.index = df.index.tz_convert(_IST_TZ)

    # Drop NaT index rows
    df = df[df.index.notna()]

    # Quality gates
    if "close" in df.columns:
        n_before = len(df)
        df = df[df["close"].notna() & (df["close"] > 0)]
        dropped = n_before - len(df)
        if dropped > 0:
            print(f"  [quality] {symbol}: dropped {dropped} zero/NaN close bars")

    if "volume" in df.columns:
        zero_vol = (df["volume"] == 0).sum()
        if zero_vol > len(df) * 0.3:
            print(f"  [quality-warn] {symbol}: {zero_vol}/{len(df)} bars have zero volume")

    return df.sort_index()


def fetch_and_cache(symbol: str, period: str = "60d",
                    refresh: bool = False) -> Optional[pd.DataFrame]:
    """Cache-first fetch: return cached data if fresh, else refetch."""
    if not refresh:
        cached = _read_cache(symbol)
        if cached is not None and _cache_fresh(cached):
            return cached

    df = fetch_5m(symbol, period)
    if df.empty:
        # Stale cache is better than nothing for probe purposes
        stale = _read_cache(symbol)
        if stale is not None and not stale.empty:
            print(f"  [stale-fallback] {symbol}: using cached bars (fetch failed)")
            return stale
        return None

    _write_cache(symbol, df)
    return df


# ── Daily-bar reconstruction from 5m ─────────────────────────────────────────

def to_daily_bars(df_5m: pd.DataFrame) -> pd.DataFrame:
    """Collapse 5-min bars to daily OHLCV with IST session dates.

    Uses the 5-min bar's date in IST as the session date.  Correct for NSE:
    09:15 IST open means the date label is unambiguous (no UTC midnight crossing
    within a session).

    Returns DataFrame indexed by date (not datetime), columns:
    date, open, high, low, close, volume, open_5m_09_20, open_5m_09_30

    open          = first bar of the session (09:15 bar open)
    open_5m_09_20 = first bar whose minute >= 09:20 (the 09:20 bar open)
    open_5m_09_30 = first bar whose minute >= 09:30 (the 09:30 bar open)
    close         = last bar of the session close
    """
    if df_5m.empty:
        return pd.DataFrame()

    df = df_5m.copy()
    df["_date"] = df.index.date
    df["_hour"] = df.index.hour
    df["_min"] = df.index.minute
    df["_hm"] = df["_hour"] * 100 + df["_min"]   # e.g. 920, 930, 1525

    results = []
    for session_date, grp in df.groupby("_date"):
        grp = grp.sort_index()
        if len(grp) < 5:
            continue   # skip truncated/holiday sessions

        session_open  = float(grp.iloc[0]["open"])
        session_close = float(grp.iloc[-1]["close"])
        session_high  = float(grp["high"].max())
        session_low   = float(grp["low"].min())
        session_vol   = float(grp["volume"].sum())

        # 09:20 entry: the bar that starts at or after 09:20
        bars_920 = grp[grp["_hm"] >= 920]
        open_920 = float(bars_920.iloc[0]["open"]) if not bars_920.empty else float("nan")

        # 09:30 entry: the bar that starts at or after 09:30
        bars_930 = grp[grp["_hm"] >= 930]
        open_930 = float(bars_930.iloc[0]["open"]) if not bars_930.empty else float("nan")

        results.append({
            "date": session_date,
            "open": session_open,
            "high": session_high,
            "low": session_low,
            "close": session_close,
            "volume": session_vol,
            "open_920": open_920,
            "open_930": open_930,
        })

    if not results:
        return pd.DataFrame()

    out = pd.DataFrame(results).set_index("date")
    out.index = pd.to_datetime(out.index)
    return out.sort_index()


# ── Earnings filter ──────────────────────────────────────────────────────────

def _load_earnings_dates() -> Dict[str, List[date]]:
    """Load earnings dates from cache. Returns {symbol: [date, ...]}."""
    if not os.path.exists(EARNINGS_CACHE):
        return {}
    try:
        with open(EARNINGS_CACHE, encoding="utf-8") as f:
            raw = json.load(f)
        out: Dict[str, List[date]] = {}
        for sym, v in raw.items():
            dates_str = v.get("dates", []) if isinstance(v, dict) else []
            parsed = []
            for ds in dates_str:
                try:
                    parsed.append(pd.Timestamp(ds).date())
                except Exception:
                    pass
            if parsed:
                out[sym] = parsed
        return out
    except Exception:
        return {}


def _is_earnings_gap(symbol: str, session_date, earnings: Dict[str, List[date]],
                     window_days: int = 2) -> bool:
    """True if session_date is within window_days of a known earnings date."""
    dates = earnings.get(symbol, [])
    if not dates:
        return False
    d = session_date.date() if hasattr(session_date, "date") else session_date
    return any(abs((d - ed).days) <= window_days for ed in dates)


# ── Probe ─────────────────────────────────────────────────────────────────────

def run_probe(daily_map: Dict[str, pd.DataFrame],
              gap_thresholds: Tuple[float, ...] = (0.02, 0.03),
              costs: Tuple[float, ...] = (0.0006, 0.0012)) -> None:
    """
    Gap-fade feasibility probe.

    For each trading day with |open/prev_close - 1| >= threshold:
      direction = FADE (short gap-ups, long gap-downs)
      (a) OPEN bar open  -> day close
      (b) 09:20 bar open -> day close
      (c) 09:30 bar open -> day close

    Reports mean fade return + n for each method × threshold, net of costs.
    """
    earnings = _load_earnings_dates()

    # Collect all gap events across all symbols
    events: List[Dict] = []

    for sym, daily in daily_map.items():
        if daily is None or len(daily) < 5:
            continue

        daily = daily.copy()
        daily["prev_close"] = daily["close"].shift(1)
        daily["gap_pct"] = (daily["open"] / daily["prev_close"] - 1.0)

        for idx, row in daily.iterrows():
            if pd.isna(row["gap_pct"]) or pd.isna(row["prev_close"]):
                continue
            if row["prev_close"] <= 0 or row["open"] <= 0:
                continue

            gap = float(row["gap_pct"])
            close = float(row["close"])
            o_open = float(row["open"])
            o_920 = float(row.get("open_920", float("nan")))
            o_930 = float(row.get("open_930", float("nan")))

            if pd.isna(close) or close <= 0:
                continue

            # Skip earnings windows
            if _is_earnings_gap(sym, idx, earnings):
                continue

            # Direction: fade = short gap-ups, long gap-downs
            # Return = direction * (exit/entry - 1)
            direction = -1.0 if gap > 0 else +1.0

            # (a) OPEN -> close
            ret_a = direction * (close / o_open - 1.0) if o_open > 0 else float("nan")

            # (b) 09:20 -> close
            ret_b = direction * (close / o_920 - 1.0) if (not pd.isna(o_920) and o_920 > 0) else float("nan")

            # (c) 09:30 -> close
            ret_c = direction * (close / o_930 - 1.0) if (not pd.isna(o_930) and o_930 > 0) else float("nan")

            events.append({
                "symbol": sym,
                "date": idx,
                "gap_pct": gap,
                "abs_gap_pct": abs(gap),
                "direction": direction,
                "ret_a": ret_a,
                "ret_b": ret_b,
                "ret_c": ret_c,
            })

    if not events:
        print("\n[probe] No gap events found. Check data coverage.")
        return

    ev = pd.DataFrame(events)
    print(f"\n[probe] Total gap events (all sizes): {len(ev)}")

    # ── Results table ─────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("GAP-FADE FEASIBILITY PROBE — 5-min intraday data (~60-day window)")
    print("Direction: short gap-ups, long gap-downs")
    print("=" * 80)

    header = (f"{'Threshold':>10}  {'Entry':>8}  {'n':>5}  "
              f"{'Mean raw%':>10}  {'Net @0.06%':>11}  {'Net @0.12%':>11}")
    print(header)
    print("-" * 80)

    for thr in gap_thresholds:
        mask = ev["abs_gap_pct"] >= thr
        sub = ev[mask]
        n_total = len(sub)

        for col, label in [("ret_a", "OPEN"), ("ret_b", "09:20"), ("ret_c", "09:30")]:
            valid = sub[col].dropna()
            n = len(valid)
            if n == 0:
                print(f"  thr={thr*100:.0f}%  {label:>8}: n=0 (no data)")
                continue
            mean_raw = valid.mean() * 100.0
            net_06 = (mean_raw - 0.06)
            net_12 = (mean_raw - 0.12)
            print(f"  thr={thr*100:.0f}%  {label:>8}  {n:>5}  "
                  f"{mean_raw:>+9.3f}%  {net_06:>+10.3f}%  {net_12:>+10.3f}%")
        print("-" * 80)

    # ── Haircut analysis (a vs b) ─────────────────────────────────────────
    print("\nHAIRCUT ANALYSIS: How much of (a) OPEN edge survives to (b) 09:20 entry?")
    print("-" * 60)
    for thr in gap_thresholds:
        mask = ev["abs_gap_pct"] >= thr
        sub = ev[mask]
        valid_ab = sub.dropna(subset=["ret_a", "ret_b"])
        n = len(valid_ab)
        if n == 0:
            continue
        mean_a = valid_ab["ret_a"].mean() * 100.0
        mean_b = valid_ab["ret_b"].mean() * 100.0
        haircut = mean_b - mean_a
        haircut_pct = (haircut / mean_a * 100.0) if mean_a != 0 else float("nan")

        print(f"  thr={thr*100:.0f}%  n={n}  "
              f"OPEN={mean_a:+.3f}%  09:20={mean_b:+.3f}%  "
              f"haircut={haircut:+.3f}pp  ({haircut_pct:+.1f}% of OPEN edge)")

    # ── Direction breakdown ───────────────────────────────────────────────
    print("\nDIRECTION BREAKDOWN (2% threshold, entry=09:20):")
    thr = 0.02
    mask = ev["abs_gap_pct"] >= thr
    sub = ev[mask].dropna(subset=["ret_b"])
    gap_ups   = sub[sub["direction"] < 0]   # fading gap-ups = short
    gap_downs = sub[sub["direction"] > 0]   # fading gap-downs = long
    print(f"  Gap-ups   (short fade): n={len(gap_ups):>4}  "
          f"mean={gap_ups['ret_b'].mean()*100:+.3f}%  net@0.06%={gap_ups['ret_b'].mean()*100-0.06:+.3f}%")
    print(f"  Gap-downs (long  fade): n={len(gap_downs):>4}  "
          f"mean={gap_downs['ret_b'].mean()*100:+.3f}%  net@0.06%={gap_downs['ret_b'].mean()*100-0.06:+.3f}%")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch 5-min intraday bars and run gap-fade feasibility probe."
    )
    ap.add_argument("--probe-only", action="store_true",
                    help="Skip fetching; run probe on existing cache only.")
    ap.add_argument("--symbol", type=str, default=None,
                    help="Fetch/probe a single symbol (test mode).")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-fetch all symbols even if cache is fresh.")
    ap.add_argument("--period", type=str, default="60d",
                    help="yfinance period string (default: 60d).")
    ap.add_argument("--pace", type=float, default=0.5,
                    help="Seconds to wait between yfinance fetches (default: 0.5).")
    args = ap.parse_args()

    symbols = [args.symbol.upper()] if args.symbol else FO_SUBSET

    # ── Step 1: Fetch and cache 5-min bars ──────────────────────────────
    print(f"\n{'='*60}")
    print(f"STEP 1: Fetch 5-min bars — {len(symbols)} symbols, period={args.period}")
    print(f"Cache dir: {CACHE_DIR}")
    print(f"{'='*60}")

    fetched_ok: List[str] = []
    failed: List[str] = []

    for i, sym in enumerate(symbols, 1):
        yf_tick = _to_yf_ticker(sym)
        if args.probe_only:
            df_5m = _read_cache(sym)
            status = "cache" if df_5m is not None else "MISSING"
        else:
            df_5m = fetch_and_cache(sym, period=args.period, refresh=args.refresh)
            status = f"{len(df_5m)} bars" if df_5m is not None else "FAILED"
            if i < len(symbols) and not args.probe_only:
                time.sleep(args.pace)

        if df_5m is not None and len(df_5m) > 0:
            fetched_ok.append(sym)
        else:
            failed.append(sym)

        print(f"  [{i:>3}/{len(symbols)}] {sym:16s} ({yf_tick:20s}): {status}")

    print(f"\n[fetch] OK: {len(fetched_ok)}  FAILED: {len(failed)}")
    if failed:
        print(f"[fetch] FAILED symbols: {failed}")

    if not fetched_ok:
        print("\n[BLOCKED] All intraday fetches failed. This sandbox may block "
              "yfinance intraday — run on a real machine or use a paid provider.")
        return 1

    # ── Step 2: Build daily bars from 5m ────────────────────────────────
    print(f"\n{'='*60}")
    print("STEP 2: Build daily OHLCV from 5-min bars")
    print(f"{'='*60}")

    daily_map: Dict[str, pd.DataFrame] = {}
    total_5m_bars = 0
    trading_days_set = set()
    gap_events_2pct = 0
    gap_events_3pct = 0

    for sym in fetched_ok:
        df_5m = _read_cache(sym)
        if df_5m is None or df_5m.empty:
            continue
        total_5m_bars += len(df_5m)
        trading_days_set.update(df_5m.index.date)

        daily = to_daily_bars(df_5m)
        if daily.empty:
            continue

        daily_map[sym] = daily

        # Count gap events for coverage report
        daily_tmp = daily.copy()
        daily_tmp["prev_close"] = daily_tmp["close"].shift(1)
        daily_tmp["gap_pct"] = (daily_tmp["open"] / daily_tmp["prev_close"] - 1.0).abs()
        g2 = (daily_tmp["gap_pct"] >= 0.02).sum()
        g3 = (daily_tmp["gap_pct"] >= 0.03).sum()
        gap_events_2pct += int(g2)
        gap_events_3pct += int(g3)

    # ── Coverage report ──────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("DATA COVERAGE REPORT")
    print(f"{'='*60}")
    print(f"  Symbols fetched OK          : {len(daily_map)}")
    print(f"  Approx trading days (union) : {len(trading_days_set)}")
    print(f"  Total 5-min bars            : {total_5m_bars:,}")
    print(f"  Gap events >=2% (cross-sym) : {gap_events_2pct}")
    print(f"  Gap events >=3% (cross-sym) : {gap_events_3pct}")

    if gap_events_2pct < 30:
        print(f"\n  [WARN] Only {gap_events_2pct} gap events at >=2%. "
              "Probe will be noisy. Results are indicative only.")
    if gap_events_3pct < 15:
        print(f"  [WARN] Only {gap_events_3pct} gap events at >=3%. "
              "Too few for reliable statistics at this threshold.")

    # ── Step 3: Run probe ────────────────────────────────────────────────
    if not daily_map:
        print("\n[probe] No daily bars available — cannot run probe.")
        return 1

    run_probe(daily_map)

    # ── Verdict ──────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("VERDICT AND CAVEATS")
    print(f"{'='*80}")
    print("""
INTERPRETATION GUIDE:
  - If (b) 09:20 mean is close to (a) OPEN mean (+/- 0.05pp): edge survives the
    entry delay; the OPEN-price mirage is NOT the dominant risk; proceed to data buy.

  - If (b) 09:20 mean is substantially smaller than (a) (haircut > 50% of (a)):
    most of the edge is consumed in the first 5 min; realistic returns are much
    smaller; factor in costs carefully before a data buy.

  - If (b) 09:20 mean is negative net of costs: edge does NOT survive to a
    fillable entry; recommend AGAINST the 3-year data buy without other evidence.

MANDATORY CAVEATS (this probe is NOT validation):
  1. ~60 CALENDAR DAYS is a single market regime. India 2026-Q2 may have
     atypical vol or macro conditions. Sign-stability over 8yr daily bars
     (the researcher's result) is a much stronger prior.
  2. ~35 symbols x ~58 days = ~2030 symbol-days, of which only a fraction
     are gap events. Small-n means each outlier moves the mean significantly.
  3. yfinance 5-min bars have known data quality issues (occasional bad prints,
     volume=0 on illiquid names, split-adjustment applied end-of-window).
  4. This probe uses MIDPOINT fills (bar open) — real slippage is unknown.
     Actual 09:20 fills on a gap-up open may be worse than the bar open.
  5. The 0.06%/0.12% cost estimates may be too low for intraday F&O legs.
  6. No look-ahead leakage in the gap calculation (prev_close is the prior
     day's actual close). The open/close within each day are point-in-time.

DECISION THRESHOLD: If net return at 09:20 entry, 0.12% costs, and >=2% gap
threshold is > +0.10%, AND direction breakdown shows both gap-ups and gap-downs
contributing positively, the probe is CONSISTENT with the researcher's edge
surviving to fillable entry. Full 3-year intraday data buy is JUSTIFIED.
Otherwise, mark as INCONCLUSIVE or NEGATIVE and state reason.
""")

    print(f"Script: {os.path.abspath(__file__)}")
    print(f"Cache:  {CACHE_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
