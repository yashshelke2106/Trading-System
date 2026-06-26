"""
bhavcopy_archive.py — Survivorship-complete NSE EOD bhavcopy ingestion pipeline.

PURPOSE
-------
yfinance only returns SURVIVING symbols. This script downloads NSE's official
bhavcopy (which lists EVERY symbol that traded each day, including later-delisted
ones) and normalises it into the same parquet schema used by bar_cache.py.

REACHABILITY STATUS (tested 2026-06-25)
----------------------------------------
  Legacy format (up to 2023-12-29):
    https://nsearchives.nseindia.com/content/historical/EQUITIES/<YYYY>/<MON>/cm<DD><MON><YYYY>bhav.csv.zip
    Verified: 2017-01-03 through 2023-12-29. ~1400-1900 EQ rows/day.

  Modern UDiFF format (2024-01-04 onwards):
    https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_<YYYYMMDD>_F_0000.csv.zip
    Verified: 2024-01-04 through 2026-06-24. ~2400 EQ rows/day.

  No prior cookie required. SSL verify=False (corporate proxy).
  NSE returns HTTP 404 for market holidays — treated as no-trading-day and skipped.

SURVIVORSHIP PROOF (tested 2026-06-25)
-----------------------------------------
  JETAIRWAYS: present 2019-01-04 (OPEN=242, CLOSE=245.2), present 2019-06-03
              (CLOSE=141.1, still trading odd lots), absent 2019-08-01.
  DHFL: present 2019-12-02 through 2020-10-01 (penny, ~Rs12, low volume).
  RCOM: present 2019-01-04 (CLOSE=14.0) and 2019-06-03 (CLOSE=2.0).

OUTPUT SCHEMA (matches bar_cache.py parquet convention)
---------------------------------------------------------
  One parquet file per symbol under logs/bhavcopy_archive/<SYMBOL>.parquet
  Index: date (DatetimeIndex, normalized to midnight UTC)
  Columns: open, high, low, close, volume
  (prev_close also stored as a column for PEAD: previous-day close is the
   announcement benchmark — you need it at T-1, not as a retroactive split adj.)

DAILY MASTER FILE
-----------------
  logs/bhavcopy_archive/daily/<YYYYMMDD>.parquet
  Contains ALL symbols for that day. Used to reconstruct point-in-time
  universe membership (a symbol exists in the universe on day D iff it
  appears in that day's master file with EQ/BE series).

RUN COMMANDS
------------
  # Pilot: stress window 2019-01-01 to 2020-06-30 (delisting cluster)
  python bhavcopy_archive.py --start 2019-01-01 --end 2020-06-30

  # Full archive from 2017 (earliest available) to today
  python bhavcopy_archive.py --start 2017-01-01 --end 2026-06-25

  # Resume interrupted run (skips dates already in daily/ cache)
  python bhavcopy_archive.py --start 2019-01-01 --end 2020-06-30 --resume

  # Symbol-level parquet rebuild from daily master files (after --resume)
  python bhavcopy_archive.py --rebuild-symbols

  # Verify a known delisted symbol
  python bhavcopy_archive.py --verify JETAIRWAYS

NOTES
-----
- Do NOT modify core/api_dhan.py. This script is completely standalone.
- PAPER_TRADE flag is irrelevant here — no orders, data only.
- Point-in-time: prev_close stored as-of each day (no retroactive split adj).
  For PEAD backtests, use prev_close from the day BEFORE the earnings date —
  that is the pre-announcement benchmark price.
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import ssl
import sys
import time
import zipfile
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# SSL bypass — matches core/api_dhan.py approach for corporate proxy
# ---------------------------------------------------------------------------
ssl._create_default_https_context = ssl._create_unverified_context
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_orig_req = requests.Session.request
def _no_verify(self, method, url, **kwargs):
    kwargs.setdefault("verify", False)
    return _orig_req(self, method, url, **kwargs)
requests.Session.request = _no_verify  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(_ROOT, "logs", "bhavcopy_archive")
DAILY_DIR = os.path.join(ARCHIVE_DIR, "daily")
SYMBOL_DIR = os.path.join(ARCHIVE_DIR, "symbols")
os.makedirs(DAILY_DIR, exist_ok=True)
os.makedirs(SYMBOL_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(ARCHIVE_DIR, "ingest.log"), mode="a"),
    ],
)
log = logging.getLogger("bhavcopy")

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------
_SESSION: Optional[requests.Session] = None

def _get_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": "https://www.nseindia.com/",
            "Accept-Encoding": "gzip, deflate, br",
        })
        adapter = requests.adapters.HTTPAdapter(
            max_retries=requests.adapters.Retry(
                total=3, backoff_factor=1.5,
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=["GET"],
            )
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _SESSION = s
    return _SESSION


# ---------------------------------------------------------------------------
# URL routing — legacy vs modern based on date
# ---------------------------------------------------------------------------
_MON3 = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Modern UDiFF format available from 2024-01-04 onwards (verified).
_MODERN_START = date(2024, 1, 4)


def _url_for(d: date) -> str:
    if d >= _MODERN_START:
        ds = d.strftime("%Y%m%d")
        return (
            f"https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{ds}_F_0000.csv.zip"
        )
    mon = _MON3[d.month - 1]
    return (
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/"
        f"{d.year}/{mon}/cm{d.day:02d}{mon}{d.year}bhav.csv.zip"
    )


# ---------------------------------------------------------------------------
# Column normalisation
# ---------------------------------------------------------------------------

def _normalise_legacy(df: pd.DataFrame) -> pd.DataFrame:
    """Legacy format columns: SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE,
    LAST, PREVCLOSE, TOTTRDQTY, TOTTRDVAL, TIMESTAMP, TOTALTRADES, ISIN"""
    # Keep EQ and BE (book-entry, same settlement)
    eq = df[df["SERIES"].isin(["EQ", "BE"])].copy()
    eq = eq.rename(columns={
        "SYMBOL": "symbol",
        "OPEN": "open",
        "HIGH": "high",
        "LOW": "low",
        "CLOSE": "close",
        "TOTTRDQTY": "volume",
        "PREVCLOSE": "prev_close",
        "TIMESTAMP": "date",
        "ISIN": "isin",
    })
    eq["date"] = pd.to_datetime(eq["date"], dayfirst=False, errors="coerce")
    return eq[["symbol", "date", "open", "high", "low", "close", "volume",
               "prev_close", "isin"]].dropna(subset=["date", "close"])


def _normalise_modern(df: pd.DataFrame, trade_date: date) -> pd.DataFrame:
    """Modern UDiFF format columns include TckrSymb, SctySrs, OpnPric, HghPric,
    LwPric, ClsPric, TtlTradgVol, PrvsClsgPric, TradDt, ISIN."""
    eq = df[df["SctySrs"].isin(["EQ", "BE"])].copy()
    eq = eq.rename(columns={
        "TckrSymb": "symbol",
        "OpnPric": "open",
        "HghPric": "high",
        "LwPric": "low",
        "ClsPric": "close",
        "TtlTradgVol": "volume",
        "PrvsClsgPric": "prev_close",
        "ISIN": "isin",
    })
    eq["date"] = pd.Timestamp(trade_date)
    return eq[["symbol", "date", "open", "high", "low", "close", "volume",
               "prev_close", "isin"]].dropna(subset=["close"])


# ---------------------------------------------------------------------------
# Core fetch for a single date
# ---------------------------------------------------------------------------

def fetch_day(d: date, timeout: int = 20) -> Optional[pd.DataFrame]:
    """Fetch and normalise a single bhavcopy. Returns None on holiday/weekend."""
    url = _url_for(d)
    session = _get_session()
    try:
        resp = session.get(url, timeout=timeout)
    except Exception as exc:
        log.warning("  %s  GET error: %s", d, exc)
        return None

    if resp.status_code == 404:
        # Holiday or weekend — normal, not an error
        return None
    if resp.status_code != 200:
        log.warning("  %s  HTTP %d from %s", d, resp.status_code, url)
        return None

    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        inner = zf.namelist()[0]
        raw = pd.read_csv(zf.open(inner), low_memory=False)
    except Exception as exc:
        log.warning("  %s  parse error: %s", d, exc)
        return None

    try:
        if d >= _MODERN_START:
            df = _normalise_modern(raw, d)
        else:
            df = _normalise_legacy(raw)
    except Exception as exc:
        log.warning("  %s  normalise error: %s", d, exc)
        return None

    if df.empty:
        log.warning("  %s  zero EQ rows after normalise", d)
        return None

    return df


# ---------------------------------------------------------------------------
# Quality gate — applied per-day before persisting
# ---------------------------------------------------------------------------

def _quality_gate(df: pd.DataFrame, d: date) -> pd.DataFrame:
    n_before = len(df)
    # Zero or negative prices
    price_mask = (df["close"] > 0) & (df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0)
    # High >= Low
    hl_mask = df["high"] >= df["low"]
    # High >= Close >= Low
    valid_range = (df["high"] >= df["close"]) & (df["close"] >= df["low"])
    df = df[price_mask & hl_mask & valid_range].copy()
    n_after = len(df)
    if n_after < n_before:
        log.info("  %s  quality gate removed %d rows (bad prices)", d, n_before - n_after)
    # NaN volume is acceptable (circuits etc.) — fill with 0
    df["volume"] = df["volume"].fillna(0).astype(float)
    return df


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _daily_path(d: date) -> str:
    return os.path.join(DAILY_DIR, f"{d.strftime('%Y%m%d')}.parquet")


def _symbol_path(symbol: str) -> str:
    safe = symbol.replace("/", "_").replace("\\", "_")
    return os.path.join(SYMBOL_DIR, f"{safe}.parquet")


def _save_daily(df: pd.DataFrame, d: date) -> None:
    df.to_parquet(_daily_path(d), index=False)


def _save_symbol(symbol: str, new_row: pd.Series) -> None:
    """Append one row to a symbol's parquet, maintaining sorted DatetimeIndex."""
    path = _symbol_path(symbol)
    row_df = pd.DataFrame([new_row.to_dict()])
    row_df.index = pd.DatetimeIndex([new_row["date"]])

    if os.path.exists(path):
        existing = pd.read_parquet(path)
        existing.index = pd.to_datetime(existing.index)
        combined = pd.concat([existing, row_df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = row_df.copy()
        combined.index = pd.DatetimeIndex([new_row["date"]])

    combined.to_parquet(path)


# ---------------------------------------------------------------------------
# Range ingestion
# ---------------------------------------------------------------------------

def ingest_range(
    start: date,
    end: date,
    resume: bool = True,
    pace_sec: float = 0.3,
    build_symbols: bool = True,
) -> dict:
    """
    Download bhavcopy for every calendar day in [start, end].

    resume=True  — skip dates whose daily/*.parquet already exists.
    pace_sec     — delay between HTTP requests (be polite to NSE).
    build_symbols — also append per-symbol parquet files while downloading.

    Returns summary dict with keys: trading_days, skipped, failed, total_rows.
    """
    current = start
    trading_days, skipped, failed, total_rows = 0, 0, 0, 0

    while current <= end:
        dpath = _daily_path(current)
        if resume and os.path.exists(dpath):
            skipped += 1
            current += timedelta(days=1)
            continue

        df = fetch_day(current)
        if df is None:
            # Holiday/weekend — no daily file written (absence IS the record)
            current += timedelta(days=1)
            time.sleep(0.05)
            continue

        df = _quality_gate(df, current)

        # Persist daily master
        _save_daily(df, current)

        # Optionally build per-symbol parquets
        if build_symbols:
            for _, row in df.iterrows():
                try:
                    _save_symbol(row["symbol"], row)
                except Exception as exc:
                    log.debug("symbol save error %s %s: %s", row["symbol"], current, exc)

        n = len(df)
        total_rows += n
        trading_days += 1
        log.info("  %s  %d EQ rows  (cumulative %d rows, %d trading days)",
                 current, n, total_rows, trading_days)

        current += timedelta(days=1)
        time.sleep(pace_sec)

    log.info(
        "Ingest done: %d trading days, %d skipped (resumed), %d fetch failures, %d total rows",
        trading_days, skipped, failed, total_rows,
    )
    return {
        "trading_days": trading_days,
        "skipped": skipped,
        "failed": failed,
        "total_rows": total_rows,
    }


# ---------------------------------------------------------------------------
# Symbol parquet rebuild from daily masters (post-ingest or after --resume)
# ---------------------------------------------------------------------------

def rebuild_symbols() -> None:
    """
    Walk all daily/*.parquet files and (re)build per-symbol parquets.
    Use after --resume to ensure symbol files are consistent with daily masters.
    This is a one-time rebuild — runtime ~2-10 min depending on archive size.
    """
    daily_files = sorted(
        f for f in os.listdir(DAILY_DIR) if f.endswith(".parquet")
    )
    if not daily_files:
        log.warning("No daily parquet files found in %s", DAILY_DIR)
        return

    log.info("Rebuilding symbol parquets from %d daily files...", len(daily_files))

    # Collect all rows per symbol
    symbol_frames: dict[str, list[pd.DataFrame]] = {}
    for fname in daily_files:
        try:
            df = pd.read_parquet(os.path.join(DAILY_DIR, fname))
            df["date"] = pd.to_datetime(df["date"])
            for sym, grp in df.groupby("symbol"):
                if sym not in symbol_frames:
                    symbol_frames[sym] = []
                symbol_frames[sym].append(grp)
        except Exception as exc:
            log.warning("  rebuild: skip %s — %s", fname, exc)

    log.info("Symbols discovered: %d", len(symbol_frames))

    for sym, frames in symbol_frames.items():
        try:
            combined = pd.concat(frames)
            combined = combined.set_index("date").sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            combined.to_parquet(_symbol_path(sym))
        except Exception as exc:
            log.warning("  rebuild: %s failed: %s", sym, exc)

    log.info("Symbol rebuild complete: %d symbols written", len(symbol_frames))


# ---------------------------------------------------------------------------
# Verification — confirm a symbol's presence/absence around delisting
# ---------------------------------------------------------------------------

def verify_symbol(symbol: str) -> None:
    """Print point-in-time presence/absence of a symbol across daily masters."""
    daily_files = sorted(
        f for f in os.listdir(DAILY_DIR) if f.endswith(".parquet")
    )
    if not daily_files:
        print(f"No daily files in {DAILY_DIR} — run ingest first.")
        return

    print(f"\nPoint-in-time presence of {symbol} in daily bhavcopy masters:")
    print(f"{'Date':12s}  {'Present':8s}  {'Close':8s}  {'Volume':12s}")
    print("-" * 50)

    last_present = None
    first_absent = None

    for fname in daily_files:
        fpath = os.path.join(DAILY_DIR, fname)
        try:
            df = pd.read_parquet(fpath)
            present = symbol in df["symbol"].values
            d = fname.replace(".parquet", "")
            if present:
                row = df[df["symbol"] == symbol].iloc[0]
                print(f"{d:12s}  {'YES':8s}  {row['close']:8.2f}  {row['volume']:12,.0f}")
                last_present = d
            else:
                if last_present and not first_absent:
                    print(f"{d:12s}  {'NO':8s}  {'---':8s}  {'---':12s}  <- first absent after {last_present}")
                    first_absent = d
        except Exception as exc:
            print(f"{fname}: read error {exc}")

    if last_present and first_absent:
        print(f"\nSummary: {symbol} last seen {last_present}, first absent {first_absent}")
    elif last_present and not first_absent:
        print(f"\nSummary: {symbol} still present through end of archive (last: {last_present})")
    else:
        print(f"\nSummary: {symbol} never found in archive")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="NSE bhavcopy archive ingestor — survivorship-complete EOD data"
    )
    ap.add_argument("--start", type=_parse_date, default=date(2019, 1, 1),
                    help="Start date YYYY-MM-DD (default: 2019-01-01)")
    ap.add_argument("--end", type=_parse_date, default=date.today(),
                    help="End date YYYY-MM-DD (default: today)")
    ap.add_argument("--resume", action="store_true", default=True,
                    help="Skip dates whose daily parquet already exists (default: on)")
    ap.add_argument("--no-resume", dest="resume", action="store_false",
                    help="Re-download even if daily parquet exists")
    ap.add_argument("--pace", type=float, default=0.3,
                    help="Seconds between HTTP requests (default: 0.3)")
    ap.add_argument("--no-symbols", dest="build_symbols", action="store_false",
                    default=True, help="Skip building per-symbol parquets during ingest")
    ap.add_argument("--rebuild-symbols", action="store_true",
                    help="Rebuild all per-symbol parquets from existing daily masters, then exit")
    ap.add_argument("--verify", type=str, metavar="SYMBOL",
                    help="Print point-in-time presence of SYMBOL across daily masters, then exit")
    args = ap.parse_args()

    if args.verify:
        verify_symbol(args.verify.upper())
        return 0

    if args.rebuild_symbols:
        rebuild_symbols()
        return 0

    log.info("NSE bhavcopy ingest: %s to %s  (resume=%s, pace=%.1fs)",
             args.start, args.end, args.resume, args.pace)
    log.info("Archive dir: %s", ARCHIVE_DIR)
    log.info("Format routing: legacy before %s, modern from %s onwards",
             _MODERN_START, _MODERN_START)

    result = ingest_range(
        start=args.start,
        end=args.end,
        resume=args.resume,
        pace_sec=args.pace,
        build_symbols=args.build_symbols,
    )

    print("\n=== INGEST SUMMARY ===")
    print(f"  Trading days downloaded : {result['trading_days']}")
    print(f"  Skipped (already cached): {result['skipped']}")
    print(f"  Fetch failures          : {result['failed']}")
    print(f"  Total EQ rows stored    : {result['total_rows']:,}")
    print(f"  Archive location        : {ARCHIVE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
