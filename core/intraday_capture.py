"""
intraday_capture.py — forward-accruing 5-minute bar archive.

WHY THIS EXISTS
---------------
yfinance serves at most ~60 calendar days of 5-minute history. Older intraday
bars are not purchasable from any free source and are not reconstructible from
EOD data. That makes intraday history a CAPTURE problem, not a download
problem: the archive can only ever be built FORWARD, and every session that is
not captured inside the 60-day window is lost permanently.

logs/intraday_5m/ was populated once (2026-04-01 .. 2026-06-25) for the
gap-fade probe (backfill_intraday_5m.py) and then abandoned when that question
was answered. This module turns that one-shot fetch into an idempotent,
resumable capture that can be run daily without re-fetching what it already
holds.

DATA SOURCE
-----------
yfinance. NOT Dhan — the Dhan Data API subscription has expired, so
core/api_dhan.py's chart endpoints and core/market_feed.py's WebSocket are both
unavailable. yfinance intraday is verified working (bars ~3 min behind live).

What this costs us versus a real feed: no bid/ask, no order-book depth, no
buy/sell queue. Order-flow factors are simply NOT capturable on this stack —
volume and price only. Do not let a downstream module pretend otherwise.

MERGE SEMANTICS — point-in-time
-------------------------------
On conflict, the bar ALREADY ON DISK wins. It was captured closer to the event
and has had less opportunity to be retroactively restated. The consequence is
deliberate and must be understood: after a stock split, newly fetched bars
arrive split-adjusted while previously captured bars remain as-quoted, so a
symbol's series can straddle an adjustment boundary. That is what point-in-time
means. `capture()` counts and reports conflicts so the boundary is detectable
rather than silent; `coverage()` flags symbols whose recent conflict count is
high enough to suspect a corporate action.

STORAGE
-------
logs/intraday_5m/<SYMBOL>.parquet
  Index:   tz-aware DatetimeIndex (Asia/Kolkata)
  Columns: open, high, low, close, volume

RUN
---
    python -m core.intraday_capture --capture               # TOP100 liquid, full window
    python -m core.intraday_capture --capture --universe fo # full F&O universe
    python -m core.intraday_capture --capture RELIANCE INFY # named symbols
    python -m core.intraday_capture --coverage              # what we hold, and gaps
"""
from __future__ import annotations

import argparse
import logging
import os
import ssl
import sys
import time
import warnings
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ── SSL bypass (mirrors backfill_intraday_5m.py — same proxy environment) ────
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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(_ROOT, "logs", "intraday_5m")
os.makedirs(CACHE_DIR, exist_ok=True)

log = logging.getLogger(__name__)

IST = "Asia/Kolkata"
_COLS = ["open", "high", "low", "close", "volume"]

# yfinance hard limit for 5m bars. Requesting more silently truncates.
MAX_LOOKBACK_DAYS = 60

# Bars per full NSE session: 09:15 .. 15:25 inclusive, 5-min stamps.
BARS_PER_SESSION = 75

# A bar stamped T covers [T, T+BAR_SECONDS) and is only complete after T+300.
# Capturing mid-session would otherwise store the still-forming bar, and the
# existing-wins merge rule would then freeze that partial bar FOREVER — a run
# at 11:33 stored the 11:30 bar with volume 2520 against a true 3458. The
# point-in-time rule exists to protect against retroactive restatement, not to
# enshrine incomplete data, so forming bars are dropped before they are ever
# written. GRACE covers feed lag on the boundary.
BAR_SECONDS = 300
FORMING_GRACE_SEC = 30


# ── Symbol mapping ──────────────────────────────────────────────────────────

def _ticker_map() -> Dict[str, str]:
    """NSE symbol -> yfinance ticker overrides.

    Sourced from core.api_dhan so the corporate-action edge cases (TATAMOTORS
    -> TMCV.NS after the 2024 demerger, MCDOWELL-N -> UNITDSPR.NS, etc.) stay
    in one place. Falls back to an empty map if that import is unavailable —
    the default SYMBOL.NS form is correct for the large majority of names.
    """
    try:
        from core.api_dhan import _YF_TICKER_MAP
        return dict(_YF_TICKER_MAP)
    except Exception:
        log.warning("[capture] api_dhan ticker map unavailable; using default .NS form")
        return {}


def yf_ticker(symbol: str) -> str:
    """Resolve an NSE symbol to its yfinance ticker."""
    return _ticker_map().get(symbol.upper(), f"{symbol.upper()}.NS")


# ── Persistence ─────────────────────────────────────────────────────────────

def _path(symbol: str) -> str:
    safe = symbol.replace("/", "_").replace("\\", "_")
    return os.path.join(CACHE_DIR, f"{safe}.parquet")


def _legacy_path(symbol: str) -> Optional[str]:
    """Path under backfill_intraday_5m.py's older naming, if it differs.

    That script (line ~155) additionally maps "&" -> "AND", so M&M was stored
    as MANDM.parquet. Capturing under the canonical name silently forked the
    same instrument across two files, and the legacy one then aged out of the
    60-day window holding bars that could no longer be re-fetched. Absorbed on
    read rather than deleted, because those bars are unrecoverable.
    """
    legacy = symbol.replace("/", "_").replace("\\", "_").replace("&", "AND")
    if legacy == symbol.replace("/", "_").replace("\\", "_"):
        return None
    p = os.path.join(CACHE_DIR, f"{legacy}.parquet")
    return p if os.path.exists(p) else None


def _is_absorbed_alias(stem: str) -> bool:
    """True if `stem` is a legacy AND-form file whose canonical twin exists.

    MANDM.parquet is the legacy spelling of M&M.parquet. Once its bars have
    been folded into the canonical file it must stop appearing in coverage,
    otherwise it reports as permanently stale (it is never re-captured, since
    nothing fetches "MANDM") and masks genuine staleness elsewhere.
    """
    if "AND" not in stem:
        return False
    canonical = stem.replace("AND", "&")
    return os.path.exists(os.path.join(CACHE_DIR, f"{canonical}.parquet"))


def read_symbol(symbol: str) -> Optional[pd.DataFrame]:
    """Load a symbol's stored bars, or None if never captured."""
    p = _path(symbol)
    if not os.path.exists(p):
        legacy = _legacy_path(symbol)
        if legacy:
            log.info("[capture] %s: adopting legacy archive %s",
                     symbol, os.path.basename(legacy))
            p = legacy
        else:
            return None
    try:
        df = pd.read_parquet(p)
        df.index = pd.to_datetime(df.index)
        if df.index.tz is None:
            df.index = df.index.tz_localize(IST)
        else:
            df.index = df.index.tz_convert(IST)
        return df.sort_index()
    except Exception as exc:
        log.warning("[capture] %s unreadable: %s", symbol, exc)
        return None


# ── Fetch ───────────────────────────────────────────────────────────────────

def fetch_symbol(symbol: str, days: int = MAX_LOOKBACK_DAYS) -> Optional[pd.DataFrame]:
    """Fetch up to `days` of 5-minute bars. Returns None on failure/empty."""
    days = min(days, MAX_LOOKBACK_DAYS)
    try:
        import yfinance as yf
        raw = yf.Ticker(yf_ticker(symbol)).history(period=f"{days}d", interval="5m")
    except Exception as exc:
        log.warning("[capture] %s fetch error: %s", symbol, exc)
        return None

    if raw is None or raw.empty:
        return None

    df = raw.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    missing = [c for c in _COLS if c not in df.columns]
    if missing:
        log.warning("[capture] %s missing columns %s", symbol, missing)
        return None

    df = df[_COLS].copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)

    # Drop rows yfinance pads with NaN for non-trading stamps.
    df = df.dropna(subset=["close"])
    df = drop_forming_bars(df)
    return df.sort_index() if not df.empty else None


def drop_forming_bars(df: pd.DataFrame, now: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Remove bars whose interval has not closed yet.

    Keeps bar T only when T + BAR_SECONDS + GRACE <= now, so a mid-session run
    never persists a partially-filled bar. See the BAR_SECONDS comment.
    """
    if df.empty:
        return df
    now = now or pd.Timestamp.now(tz=IST)
    cutoff = now - pd.Timedelta(seconds=BAR_SECONDS + FORMING_GRACE_SEC)
    return df[df.index <= cutoff]


# ── Merge ───────────────────────────────────────────────────────────────────

def merge_symbol(symbol: str, fresh: pd.DataFrame) -> Dict:
    """Union fresh bars into the stored series, existing bars winning conflicts.

    Returns {added, kept, conflicts, total} where `conflicts` counts timestamps
    present in both whose close differs — the corporate-action tripwire
    described in the module docstring.

    If a legacy-named archive exists for this symbol (see _legacy_path) its
    bars are folded in too, so the two namings converge on the canonical file
    instead of drifting apart.
    """
    existing = read_symbol(symbol)

    legacy = _legacy_path(symbol)
    if legacy and os.path.exists(_path(symbol)):
        try:
            old = pd.read_parquet(legacy)
            old.index = pd.to_datetime(old.index)
            old.index = (old.index.tz_localize(IST) if old.index.tz is None
                         else old.index.tz_convert(IST))
            existing = (old if existing is None else
                        pd.concat([existing, old.loc[old.index.difference(existing.index)]])
                        .sort_index())
        except Exception as exc:
            log.warning("[capture] %s: legacy merge failed: %s", symbol, exc)

    if existing is None or existing.empty:
        fresh.to_parquet(_path(symbol))
        return {"added": len(fresh), "kept": 0, "conflicts": 0, "total": len(fresh)}

    overlap = existing.index.intersection(fresh.index)
    conflicts = 0
    if len(overlap):
        a = existing.loc[overlap, "close"].astype(float)
        b = fresh.loc[overlap, "close"].astype(float)
        # Relative tolerance — float round-trip through parquet is not exact.
        conflicts = int((~((a - b).abs() <= (a.abs() * 1e-6 + 1e-9))).sum())

    new_only = fresh.loc[fresh.index.difference(existing.index)]
    combined = pd.concat([existing, new_only]).sort_index()
    combined = combined[~combined.index.duplicated(keep="first")]
    combined.to_parquet(_path(symbol))

    return {
        "added": len(new_only),
        "kept": len(overlap),
        "conflicts": conflicts,
        "total": len(combined),
    }


def repair_symbol(symbol: str, days: int = 3) -> Dict:
    """Force-overwrite recently stored bars with freshly fetched values.

    The inverse of merge_symbol's existing-wins rule, deliberately scoped to
    the last `days`. Needed because bars captured mid-session were stored
    while still forming and can never be corrected by a normal merge. Only
    completed bars are written, and only inside the recent window, so this
    cannot silently restate older history across a split boundary.
    """
    stored = read_symbol(symbol)
    if stored is None or stored.empty:
        return {"repaired": 0, "total": 0}

    fresh = fetch_symbol(symbol, days=min(days + 2, MAX_LOOKBACK_DAYS))
    if fresh is None or fresh.empty:
        return {"repaired": 0, "total": len(stored)}

    window_start = pd.Timestamp.now(tz=IST).normalize() - pd.Timedelta(days=days)
    target = fresh.index[(fresh.index >= window_start) & fresh.index.isin(stored.index)]
    if len(target) == 0:
        return {"repaired": 0, "total": len(stored)}

    before = stored.loc[target, "close"].astype(float).to_numpy()
    after = fresh.loc[target, "close"].astype(float).to_numpy()
    changed = int((~np.isclose(before, after, rtol=1e-6)).sum())

    stored.loc[target, _COLS] = fresh.loc[target, _COLS].values
    stored.sort_index().to_parquet(_path(symbol))
    return {"repaired": changed, "total": len(stored)}


# ── Capture driver ──────────────────────────────────────────────────────────

def capture(
    symbols: List[str],
    days: int = MAX_LOOKBACK_DAYS,
    pace_sec: float = 0.4,
) -> Dict:
    """Fetch and merge every symbol. Idempotent — safe to run repeatedly."""
    stats = {
        "symbols": len(symbols), "ok": 0, "failed": 0,
        "bars_added": 0, "conflicts": 0, "failures": [],
    }

    for i, sym in enumerate(symbols, 1):
        fresh = fetch_symbol(sym, days=days)
        if fresh is None:
            stats["failed"] += 1
            stats["failures"].append(sym)
            log.warning("  [%3d/%3d] %-14s FAILED", i, len(symbols), sym)
        else:
            m = merge_symbol(sym, fresh)
            stats["ok"] += 1
            stats["bars_added"] += m["added"]
            stats["conflicts"] += m["conflicts"]
            flag = f"  !! {m['conflicts']} conflicts" if m["conflicts"] else ""
            log.info("  [%3d/%3d] %-14s +%-5d new  (%d total)%s",
                     i, len(symbols), sym, m["added"], m["total"], flag)
        time.sleep(pace_sec)

    return stats


# ── Coverage report ─────────────────────────────────────────────────────────

def coverage(symbols: Optional[List[str]] = None) -> pd.DataFrame:
    """Per-symbol coverage: span, session count, and days since last capture.

    `stale_days` is the operational number to watch. Once it exceeds
    MAX_LOOKBACK_DAYS the missing sessions are gone for good.
    """
    if symbols is None:
        symbols = sorted(
            f[:-8] for f in os.listdir(CACHE_DIR) if f.endswith(".parquet")
        )
        symbols = [s for s in symbols if not _is_absorbed_alias(s)]

    today = pd.Timestamp.now(tz=IST).normalize()
    rows = []
    for sym in symbols:
        df = read_symbol(sym)
        if df is None or df.empty:
            rows.append({"symbol": sym, "bars": 0, "sessions": 0,
                         "first": None, "last": None, "stale_days": None,
                         "recoverable": None})
            continue
        sessions = df.index.normalize().nunique()
        last = df.index.max()
        stale = int((today - last.normalize()).days)
        rows.append({
            "symbol": sym,
            "bars": len(df),
            "sessions": sessions,
            "first": df.index.min().date(),
            "last": last.date(),
            "stale_days": stale,
            "recoverable": stale <= MAX_LOOKBACK_DAYS,
        })

    return pd.DataFrame(rows)


# ── Universe helpers ────────────────────────────────────────────────────────

def _resolve_universe(name: str) -> List[str]:
    from core.universe import FO_UNIVERSE, TOP100_LIQUID
    if name == "fo":
        return list(FO_UNIVERSE)
    if name == "top100":
        return list(TOP100_LIQUID)
    if name == "held":
        return sorted(f[:-8] for f in os.listdir(CACHE_DIR) if f.endswith(".parquet"))
    raise ValueError(f"unknown universe: {name}")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Forward-accruing 5-minute intraday bar capture (yfinance)."
    )
    ap.add_argument("symbols", nargs="*", help="Explicit symbols (default: universe)")
    ap.add_argument("--capture", action="store_true", help="Fetch and merge bars")
    ap.add_argument("--coverage", action="store_true", help="Report what is held")
    ap.add_argument("--repair", type=int, metavar="DAYS", default=None,
                    help="Force-overwrite the last DAYS of stored bars with "
                         "fresh values (fixes bars captured while still forming)")
    ap.add_argument("--universe", default="top100",
                    choices=["fo", "top100", "held"],
                    help="Symbol set when none named (default: top100)")
    ap.add_argument("--days", type=int, default=MAX_LOOKBACK_DAYS,
                    help=f"Lookback days, capped at {MAX_LOOKBACK_DAYS}")
    ap.add_argument("--pace", type=float, default=0.4,
                    help="Seconds between fetches (default: 0.4)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if args.coverage:
        cov = coverage(args.symbols or None)
        if cov.empty:
            print("No intraday archive yet.")
            return 0
        print(cov.to_string(index=False))
        held = cov[cov["bars"] > 0]
        print(f"\nSymbols held      : {len(held)}")
        if not held.empty:
            print(f"Sessions (median) : {int(held['sessions'].median())}")
            print(f"Span              : {held['first'].min()} .. {held['last'].max()}")
            lost = held[~held["recoverable"].astype(bool)]
            if not lost.empty:
                print(f"UNRECOVERABLE gaps: {len(lost)} symbols stale >{MAX_LOOKBACK_DAYS}d")
        return 0

    if args.repair is not None:
        symbols = args.symbols or _resolve_universe("held")
        log.info("Repairing last %dd for %d symbols", args.repair, len(symbols))
        total_fixed = 0
        for i, sym in enumerate(symbols, 1):
            r = repair_symbol(sym, days=args.repair)
            total_fixed += r["repaired"]
            if r["repaired"]:
                log.info("  [%3d/%3d] %-14s repaired %d bars",
                         i, len(symbols), sym, r["repaired"])
            time.sleep(args.pace)
        print(f"\nRepaired {total_fixed} bars across {len(symbols)} symbols.")
        return 0

    if args.capture:
        symbols = args.symbols or _resolve_universe(args.universe)
        log.info("Intraday capture: %d symbols, %dd window, pace %.2fs",
                 len(symbols), min(args.days, MAX_LOOKBACK_DAYS), args.pace)
        st = capture(symbols, days=args.days, pace_sec=args.pace)
        print("\n=== CAPTURE SUMMARY ===")
        print(f"  Symbols     : {st['symbols']}")
        print(f"  Succeeded   : {st['ok']}")
        print(f"  Failed      : {st['failed']}")
        print(f"  Bars added  : {st['bars_added']:,}")
        print(f"  Conflicts   : {st['conflicts']}  (existing bar kept; "
              f"non-zero suggests a corporate action)")
        if st["failures"]:
            print(f"  Failed syms : {', '.join(st['failures'][:20])}")
        print(f"  Archive     : {CACHE_DIR}")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
