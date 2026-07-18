"""
Retrospective corporate-event study.

Joins dated events (logs/corporate_events.jsonl, built by
scripts/build_events_archive.py) to the 10yr daily OHLC archive
(data/history/<SYM>.csv) and measures the ACTUAL stock reaction around each
event, aggregated PER EVENT TYPE across all history.

This is the real answer to "how do dividends/bonus/splits/order-wins/buybacks
drive the stock" - a backtest over past dated events, not forward-collection.

METRICS (per event, per horizon in trading days):
    raw    = close[t0+n]/close[t0] - 1          (t0 = first trading day >= event date)
    excess = raw - NIFTY raw over same window    (strips market drift; the honest number)
    pre5   = run-up over the 5 days BEFORE t0     (detects "buy rumour, sell news")

OUTPUT: table per event_type (n, avg excess % at each horizon, % positive),
plus logs/event_study_results.json.

CAVEATS printed with the result: event dates from the exchange can be ex/record
dates, not the announcement day; small-n rows are noise; survivorship (yfinance)
applies. Descriptive, not a validated edge.

RUN:
    python -m core.event_study                 # all event types
    python -m core.event_study --min-n 5       # hide types with <5 events
    python -m core.event_study --type dividends
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd

log = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_JSONL = os.path.join(_ROOT, "logs", "corporate_events.jsonl")
HISTORY_DIR = os.path.join(_ROOT, "data", "history")
RESULTS = os.path.join(_ROOT, "logs", "event_study_results.json")

HORIZONS = (1, 3, 5, 10, 20)
_INDEX_TICKER = "^NSEI"  # NIFTY 50 for market adjustment

_YF_OVERRIDES = {
    "TATAMOTORS": "TMCV.NS", "MCDOWELL-N": "UNITDSPR.NS", "DEEPAKNT": "DEEPAKNTR.NS",
}


def _yf_ticker(symbol: str) -> str:
    return _YF_OVERRIDES.get(symbol.upper(), f"{symbol.upper()}.NS")


# ─────────────────────────── data loading ───────────────────────────

_ohlc_cache: Dict[str, pd.DataFrame] = {}


def _load_ohlc(symbol: str) -> pd.DataFrame:
    """Daily close series for a symbol. Archive CSV first, yfinance fallback.
    Columns: date (datetime.date), close. Sorted ascending."""
    if symbol in _ohlc_cache:
        return _ohlc_cache[symbol]

    df = pd.DataFrame()
    safe = symbol.replace("/", "_")
    csv = os.path.join(HISTORY_DIR, f"{safe}.csv")
    if os.path.exists(csv):
        try:
            df = pd.read_csv(csv)[["date", "close"]]
        except Exception:
            df = pd.DataFrame()

    if df.empty:
        try:
            import yfinance as yf
            tkr = symbol if symbol.startswith("^") else _yf_ticker(symbol)
            raw = yf.download(tkr, period="10y", interval="1d",
                              progress=False, auto_adjust=False)
            if raw is not None and not raw.empty:
                if isinstance(raw.columns, pd.MultiIndex):
                    raw.columns = raw.columns.get_level_values(0)
                df = raw.reset_index()[["Date", "Close"]]
                df.columns = ["date", "close"]
        except Exception as e:
            log.warning("ohlc %s failed: %s", symbol, e)

    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.dropna().sort_values("date").reset_index(drop=True)
    _ohlc_cache[symbol] = df
    return df


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%b/%Y", "%d-%b-%y",
                "%d %b %Y", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    # last resort: pandas
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


# ─────────────────────────── return math ───────────────────────────

def _t0_index(df: pd.DataFrame, event_day: date) -> Optional[int]:
    """Index of the first trading bar on/after the event date."""
    idx = df.index[df["date"] >= event_day]
    return int(idx[0]) if len(idx) else None


def _ret(df: pd.DataFrame, i0: int, n: int) -> Optional[float]:
    """Pct return from bar i0 to bar i0+n. None if out of range."""
    if i0 + n >= len(df) or i0 < 0:
        return None
    c0, c1 = df["close"].iloc[i0], df["close"].iloc[i0 + n]
    if c0 <= 0:
        return None
    return (c1 / c0 - 1) * 100


def _pre_ret(df: pd.DataFrame, i0: int, n: int) -> Optional[float]:
    if i0 - n < 0:
        return None
    c0, c1 = df["close"].iloc[i0 - n], df["close"].iloc[i0]
    if c0 <= 0:
        return None
    return (c1 / c0 - 1) * 100


# ─────────────────────────── study ───────────────────────────

_baseline_cache: Dict[str, Dict[int, float]] = {}


def _baseline(symbol: str, df: pd.DataFrame, index_df: pd.DataFrame) -> Dict[int, float]:
    """That stock's AVERAGE n-day excess-vs-NIFTY over ALL windows. This is the
    drift the stock has regardless of any event (survivorship makes it positive
    for the F&O survivor universe). Event 'abnormal' return = event excess minus
    this baseline; if the event carries no information, abnormal ~ 0."""
    if symbol in _baseline_cache:
        return _baseline_cache[symbol]
    out: Dict[int, float] = {}
    m_mean = {}
    if not index_df.empty:
        for n in HORIZONS:
            ms = [_ret(index_df, j, n) for j in range(len(index_df))]
            ms = [x for x in ms if x is not None]
            m_mean[n] = sum(ms) / len(ms) if ms else 0.0
    for n in HORIZONS:
        rs = [_ret(df, i, n) for i in range(len(df))]
        rs = [x for x in rs if x is not None]
        r_mean = sum(rs) / len(rs) if rs else 0.0
        out[n] = r_mean - m_mean.get(n, 0.0)
    _baseline_cache[symbol] = out
    return out


def _load_events() -> List[Dict]:
    if not os.path.exists(EVENTS_JSONL):
        return []
    out = []
    with open(EVENTS_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


def run(min_n: int = 3, only_type: Optional[str] = None) -> Dict:
    events = _load_events()
    if not events:
        print(f"No events at {EVENTS_JSONL}. Run build_events_archive first.")
        return {}

    index_df = _load_ohlc(_INDEX_TICKER)
    if index_df.empty:
        print("[!] NIFTY (^NSEI) unavailable - reporting RAW returns only.")

    # accumulate excess returns per event_type per horizon
    agg: Dict[str, Dict[int, List[float]]] = defaultdict(lambda: defaultdict(list))
    pre_agg: Dict[str, List[float]] = defaultdict(list)
    counted = 0
    skipped_nodate = skipped_nobar = 0

    for ev in events:
        etype = ev.get("event_type") or ev.get("category") or "unknown"
        if only_type and etype != only_type:
            continue
        d = _parse_date(ev.get("date"))
        if not d:
            skipped_nodate += 1
            continue
        sym = ev.get("symbol")
        df = _load_ohlc(sym)
        if df.empty:
            continue
        i0 = _t0_index(df, d)
        if i0 is None:
            skipped_nobar += 1
            continue

        # ABNORMAL return = event excess-vs-NIFTY MINUS the stock's own baseline
        # drift. Strips survivorship: a rising survivor beats NIFTY every window,
        # so only the event-window OUTPERFORMANCE vs its own norm counts.
        base = _baseline(sym, df, index_df)
        i0_idx = _t0_index(index_df, df["date"].iloc[i0]) if not index_df.empty else None
        for n in HORIZONS:
            r = _ret(df, i0, n)
            if r is None:
                continue
            m = _ret(index_df, i0_idx, n) if i0_idx is not None else None
            excess = r - m if m is not None else r
            agg[etype][n].append(excess - base.get(n, 0.0))
        pre = _pre_ret(df, i0, 5)
        if pre is not None:
            pre_agg[etype].append(pre)
        counted += 1

    return _report(agg, pre_agg, counted, skipped_nodate, skipped_nobar,
                   min_n, bool(index_df.empty))


def _stat(vals: List[float]):
    n = len(vals)
    avg = sum(vals) / n
    pos = sum(1 for v in vals if v > 0) / n * 100
    return avg, pos, n


def _report(agg, pre_agg, counted, skip_nd, skip_nb, min_n, raw_only) -> Dict:
    label = "raw" if raw_only else "excess (vs NIFTY)"
    print("\n" + "=" * 78)
    print(f"RETROSPECTIVE EVENT STUDY   metric = ABNORMAL (excess vs NIFTY, minus stock baseline) %")
    print("=" * 78)
    print(f"events used: {counted}   skipped(no date): {skip_nd}   "
          f"skipped(no price bar): {skip_nb}")
    print(f"(hiding event types with n < {min_n})\n")

    hdr = f"  {'event_type':20s} {'n':>4s} {'pre5':>7s}  " + \
          "  ".join(f"+{h}d" .rjust(7) for h in HORIZONS)
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    results: Dict[str, Dict] = {}
    # order by sample size
    for etype in sorted(agg, key=lambda k: -len(agg[k].get(HORIZONS[0], []))):
        n0 = len(agg[etype].get(HORIZONS[0], []))
        if n0 < min_n:
            continue
        pre = pre_agg.get(etype, [])
        pre_avg = sum(pre) / len(pre) if pre else float("nan")
        cells = []
        block = {"n": n0, "pre5_avg": round(pre_avg, 2) if pre else None}
        for h in HORIZONS:
            vals = agg[etype].get(h, [])
            if vals:
                avg, pos, n = _stat(vals)
                cells.append(f"{avg:+6.2f}")
                block[f"h{h}"] = {"avg": round(avg, 2), "pct_pos": round(pos, 1), "n": n}
            else:
                cells.append("   -- ")
        pre_s = f"{pre_avg:+6.2f}" if pre else "   -- "
        print(f"  {etype:20s} {n0:4d} {pre_s}  " + "  ".join(cells))
        results[etype] = block

    # %positive line for the headline horizon
    print("\n  %positive at +5d:")
    for etype, b in results.items():
        h5 = b.get("h5")
        if h5:
            print(f"    {etype:20s} {h5['pct_pos']:.0f}%  (n={h5['n']})")

    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    json.dump({"metric": label, "events_used": counted, "by_event_type": results},
              open(RESULTS, "w", encoding="utf-8"), indent=2)
    print(f"\nsaved: {RESULTS}")
    print("\nCAVEAT: exchange dates may be ex/record (not announcement) dates; "
          "small-n rows are noise; yfinance = survivors-only. Descriptive.")
    return results


def main(argv: List[str]) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Retrospective corporate-event study")
    p.add_argument("--min-n", type=int, default=3, help="hide event types with fewer events")
    p.add_argument("--type", type=str, default=None, help="only this event_type")
    args = p.parse_args(argv)
    run(min_n=args.min_n, only_type=args.type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
