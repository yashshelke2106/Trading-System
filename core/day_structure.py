"""
day_structure.py — per-session movement descriptors from captured 5m bars.

WHAT THIS IS
------------
For every completed session and symbol in logs/intraday_5m/, compute a row of
DESCRIPTIVE movement statistics: how the day moved, not what it means. This is
the "day pattern" half of the movement-capture layer (swing_structure.py is
the other half). No signals, no priors, no expected direction — the repo's
alpha hunt is a closed negative and this module does not reopen it. It exists
so questions like "how often does an opening-range break extend?" can be
answered from recorded fact instead of memory.

WHY DERIVED-BUT-SNAPSHOTTED
---------------------------
Everything here is recomputable from the 5m archive, so nothing is lost if a
run is missed — PROVIDED the bars themselves were captured. The 5m archive has
a 60-day recovery window; this layer inherits that constraint transitively.
Snapshotting daily keeps the dataset queryable without re-deriving 150 symbols
x N sessions every time.

SESSION COMPLETENESS — the honesty rule
---------------------------------------
A day-type label computed mid-session is wrong by construction (a "trend day"
at 12:00 can close as a range day). Today's session is therefore only eligible
after 15:35 IST, and sessions with fewer than MIN_FULL_BARS bars are labelled
day_type="partial" with complete=False rather than silently classified.
Circuit-locked sessions (high == low) are labelled "locked", not classified —
per the repo's gap-honest-fills lesson, those bars are untradeable anyway.

DAY TYPE RULES (descriptive, fixed, documented)
-----------------------------------------------
  locked      high == low (price band hit, no real two-way trade)
  partial     incomplete session (never classified)
  gap_fade    |gap| >= 1% and close retraced more than half the gap
  trend_up    opened in bottom 30% of range, closed in top 30% (open-drive up)
  trend_down  mirror image
  range       everything else
These are shape labels for the tape that PRINTED. They carry no forward claim.

STORAGE
-------
logs/day_structure/<YYYYMMDD>.parquet — one file per session, all symbols
(same daily-master convention as the bhavcopy archive).

RUN
---
    python -m core.day_structure --build            # incremental, all held symbols
    python -m core.day_structure --build --rebuild  # recompute every session
    python -m core.day_structure --show RELIANCE    # recent sessions for a symbol
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import warnings
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

log = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(_ROOT, "logs", "day_structure")
os.makedirs(OUT_DIR, exist_ok=True)

IST = "Asia/Kolkata"

# 09:15 .. 15:25 stamps = 75 five-minute bars in a full NSE session.
FULL_BARS = 75
# Accept a few missing prints (thin names drop bars) but refuse to classify
# a session that is materially incomplete.
MIN_FULL_BARS = 65
# Opening range = first 30 minutes = 6 bars (09:15 .. 09:40 stamps).
OR_BARS = 6
# First trading hour = 12 bars, used for the volume front-loading ratio.
HOUR_BARS = 12
# Today's session may only be processed after the last bar (15:25, completes
# 15:30) is definitely closed.
SESSION_DONE_HHMM = (15, 35)

# Sessions inside this window are recomputed even if their parquet exists.
# Reason: a session first processed from an incomplete bar set (capture ran
# mid-day, then the clock passed 15:35 -> eligible but partial) would be
# frozen as day_type="partial" forever, because build() skips existing files.
# The rolling window lets the next run overwrite it once full bars exist —
# mirrors intraday_capture.repair_symbol's recent-window overwrite.
RECOMPUTE_DAYS = 5


# ── Per-session computation ─────────────────────────────────────────────────

def compute_session(bars: pd.DataFrame, prev_close: Optional[float]) -> Dict:
    """Movement descriptors for ONE symbol-session of 5m bars.

    `bars` must be a single session (one calendar day), ascending index.
    `prev_close` is the prior session's last close (NaN-safe: gap fields are
    None when unavailable, e.g. the first captured session of a symbol).
    """
    n = len(bars)
    o = float(bars["open"].iloc[0])
    h = float(bars["high"].max())
    l = float(bars["low"].min())
    c = float(bars["close"].iloc[-1])
    v = float(bars["volume"].sum())
    complete = n >= MIN_FULL_BARS

    rng = h - l
    locked = rng <= 0

    gap_pct = ret_cc = None
    if prev_close and prev_close > 0:
        gap_pct = round((o - prev_close) / prev_close * 100, 3)
        ret_cc = round((c - prev_close) / prev_close * 100, 3)
    ret_oc = round((c - o) / o * 100, 3) if o > 0 else None
    range_pct = round(rng / o * 100, 3) if o > 0 else None

    open_loc = clv = None
    if not locked:
        open_loc = round((o - l) / rng, 3)   # where in the range the day opened
        clv = round((c - l) / rng, 3)        # close location value, 0..1

    # Opening range and first break of it.
    or_high = or_low = None
    or_break_dir, or_break_min = 0, None
    if n > OR_BARS:
        or_high = float(bars["high"].iloc[:OR_BARS].max())
        or_low = float(bars["low"].iloc[:OR_BARS].min())
        post = bars.iloc[OR_BARS:]
        up = post.index[post["close"] > or_high]
        dn = post.index[post["close"] < or_low]
        first_up = up[0] if len(up) else None
        first_dn = dn[0] if len(dn) else None
        if first_up is not None and (first_dn is None or first_up < first_dn):
            or_break_dir, brk = +1, first_up
        elif first_dn is not None:
            or_break_dir, brk = -1, first_dn
        else:
            brk = None
        if brk is not None:
            or_break_min = int((brk - bars.index[0]).total_seconds() // 60)

    # Timing of the session extremes (minutes from open). A trend day tends to
    # put one extreme near the open and the other near the close.
    t0 = bars.index[0]
    hod_min = int((bars["high"].idxmax() - t0).total_seconds() // 60)
    lod_min = int((bars["low"].idxmin() - t0).total_seconds() // 60)

    # Session VWAP from typical price; None when the tape printed no volume.
    tp = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vwap = close_vs_vwap = None
    if v > 0:
        vwap = float((tp * bars["volume"]).sum() / v)
        close_vs_vwap = round((c - vwap) / vwap * 100, 3)
        vwap = round(vwap, 2)

    front_vol = None
    if v > 0 and n >= HOUR_BARS:
        front_vol = round(float(bars["volume"].iloc[:HOUR_BARS].sum()) / v, 3)

    # ── Day-type label (see module docstring for the rule table) ─────────
    if locked:
        day_type = "locked"
    elif not complete:
        day_type = "partial"
    elif (gap_pct is not None and abs(gap_pct) >= 1.0 and prev_close
          and ((gap_pct > 0 and c <= o - 0.5 * (o - prev_close))
               or (gap_pct < 0 and c >= o + 0.5 * (prev_close - o)))):
        day_type = "gap_fade"
    elif open_loc is not None and open_loc <= 0.30 and clv >= 0.70:
        day_type = "trend_up"
    elif open_loc is not None and open_loc >= 0.70 and clv <= 0.30:
        day_type = "trend_down"
    else:
        day_type = "range"

    return {
        "bars": n, "complete": complete,
        "prev_close": round(prev_close, 2) if prev_close else None,
        "open": round(o, 2), "high": round(h, 2),
        "low": round(l, 2), "close": round(c, 2), "volume": v,
        "gap_pct": gap_pct, "ret_oc_pct": ret_oc, "ret_cc_pct": ret_cc,
        "range_pct": range_pct, "open_loc": open_loc, "clv": clv,
        "or_high": round(or_high, 2) if or_high else None,
        "or_low": round(or_low, 2) if or_low else None,
        "or_break_dir": or_break_dir, "or_break_min": or_break_min,
        "hod_min": hod_min, "lod_min": lod_min,
        "vwap": vwap, "close_vs_vwap_pct": close_vs_vwap,
        "front_vol_ratio": front_vol,
        "day_type": day_type,
    }


# ── Build driver ────────────────────────────────────────────────────────────

def _out_path(d) -> str:
    return os.path.join(OUT_DIR, f"{pd.Timestamp(d).strftime('%Y%m%d')}.parquet")


def _eligible_dates(now: Optional[pd.Timestamp] = None):
    """A session date is eligible once its close is definitely behind us."""
    now = now or pd.Timestamp.now(tz=IST)
    cutoff = now.normalize()
    hh, mm = SESSION_DONE_HHMM
    if (now.hour, now.minute) >= (hh, mm):
        cutoff = cutoff + pd.Timedelta(days=1)   # today counts
    return cutoff


def build(symbols: Optional[List[str]] = None, rebuild: bool = False) -> Dict:
    """Compute day-structure rows for every held session not yet on disk."""
    from core.intraday_capture import read_symbol, CACHE_DIR, _is_absorbed_alias

    if symbols is None:
        symbols = sorted(
            f[:-8] for f in os.listdir(CACHE_DIR) if f.endswith(".parquet")
        )
        symbols = [s for s in symbols if not _is_absorbed_alias(s)]

    cutoff = _eligible_dates()
    recent = cutoff - pd.Timedelta(days=RECOMPUTE_DAYS)

    # date -> list of row dicts
    per_date: Dict[pd.Timestamp, List[Dict]] = {}
    for sym in symbols:
        df = read_symbol(sym)
        if df is None or df.empty:
            continue
        prev_close: Optional[float] = None
        for d, day in df.groupby(df.index.normalize()):
            if d >= cutoff:
                break
            if len(day) >= 5:   # ignore stray fragments entirely
                if rebuild or d >= recent or not os.path.exists(_out_path(d)):
                    row = compute_session(day, prev_close)
                    row["symbol"] = sym
                    row["date"] = d.date().isoformat()
                    per_date.setdefault(d, []).append(row)
            prev_close = float(day["close"].iloc[-1])

    written, rows = 0, 0
    for d, recs in sorted(per_date.items()):
        pd.DataFrame(recs).to_parquet(_out_path(d), index=False)
        written += 1
        rows += len(recs)

    total_files = sum(1 for f in os.listdir(OUT_DIR) if f.endswith(".parquet"))
    return {"sessions_written": written, "rows": rows,
            "sessions_on_disk": total_files, "symbols": len(symbols)}


def load_symbol(symbol: str) -> pd.DataFrame:
    """All stored day-structure rows for one symbol, ascending by date."""
    frames = []
    for f in sorted(os.listdir(OUT_DIR)):
        if not f.endswith(".parquet"):
            continue
        df = pd.read_parquet(os.path.join(OUT_DIR, f))
        hit = df[df["symbol"] == symbol.upper()]
        if not hit.empty:
            frames.append(hit)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Per-session movement descriptors.")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="Recompute every session, not just missing ones")
    ap.add_argument("--show", metavar="SYMBOL", help="Print recent sessions")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    if args.show:
        df = load_symbol(args.show)
        if df.empty:
            print(f"No day-structure rows for {args.show} — run --build first.")
            return 1
        cols = ["date", "day_type", "gap_pct", "ret_oc_pct", "clv",
                "or_break_dir", "or_break_min", "close_vs_vwap_pct", "complete"]
        print(df[cols].tail(20).to_string(index=False))
        print("\nday_type counts:")
        print(df["day_type"].value_counts().to_string())
        return 0

    if args.build or args.rebuild:
        res = build(rebuild=args.rebuild)
        print(f"\nSessions written : {res['sessions_written']}")
        print(f"Rows             : {res['rows']:,}")
        print(f"Sessions on disk : {res['sessions_on_disk']}")
        print(f"Symbols scanned  : {res['symbols']}")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
