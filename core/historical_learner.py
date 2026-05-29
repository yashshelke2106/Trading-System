"""
Historical pattern learner for all F&O stocks.

Fetches 45 days of 1D + 15m + 5m OHLCV for every symbol in FO_UNIVERSE,
simulates signals at 3 daily checkpoints, records patterns fired per timeframe,
computes outcome (TARGET_HIT / SL_HIT), and aggregates WR stats per pattern.

Outputs:
  logs/historical_raw.jsonl   — one record per simulated signal
  logs/historical_stats.json  — per-pattern WR by timeframe confluence
  logs/learned_params.json    — PATTERN_WEIGHTS updated with historical priors

Usage:
  python -m core.historical_learner               # all 153 symbols, 45 days
  python -m core.historical_learner --symbols 20  # first 20 (quick test)
  python -m core.historical_learner --update      # also update learned_params
"""
import os
import sys
import json
import time
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.api_dhan import dhan_intraday, dhan_daily
from core.signal_engine import SignalEngine
from core.universe import FO_UNIVERSE

# ── Configuration ──────────────────────────────────────────────────────────────
CHECKPOINTS    = [("10:00", 10, 0), ("11:30", 11, 30), ("13:15", 13, 15)]
OUTCOME_BARS   = 12     # look-forward: 12 × 5m = 60 minutes
ATR_MULT_SL    = 1.5
RR_RATIO       = 2.0
MIN_PAT_COUNT  = 10     # minimum signals before computing meaningful weight
HIST_ALPHA     = 0.30   # blend: 30% historical into existing live weights
FETCH_SLEEP    = 0.25   # seconds between yfinance calls per worker

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATS_FILE  = os.path.join(_ROOT, "logs", "historical_stats.json")
RAW_FILE    = os.path.join(_ROOT, "logs", "historical_raw.jsonl")
PARAMS_FILE = os.path.join(_ROOT, "logs", "learned_params.json")

log = logging.getLogger("historical_learner")

# Thread-local SignalEngine (each worker gets its own)
_tl = threading.local()

def _get_engine() -> SignalEngine:
    if not hasattr(_tl, "engine"):
        _tl.engine = SignalEngine()
    return _tl.engine


# ── Data fetching ──────────────────────────────────────────────────────────────

def fetch_stock_data(symbol: str, days_back: int = 45) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return (df5, df15, df1d). Any may be empty on failure."""
    try:
        df5 = dhan_intraday(symbol, 5,  days_back=days_back)
        time.sleep(FETCH_SLEEP)
        df15 = dhan_intraday(symbol, 15, days_back=days_back)
        time.sleep(FETCH_SLEEP)
        df1d = dhan_daily(symbol, days_back=max(days_back + 15, 60))
        return df5, df15, df1d
    except Exception as e:
        log.warning("%s fetch failed: %s", symbol, e)
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()


# ── Signal simulation helpers ──────────────────────────────────────────────────

def _trading_dates(df5: pd.DataFrame) -> List[date]:
    return sorted(set(df5["date"].dt.date.tolist()))


def _slice_to(df: pd.DataFrame, cutoff: datetime) -> pd.DataFrame:
    """All rows with date <= cutoff."""
    return df[df["date"] <= cutoff].copy()


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return 0.0
    try:
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"]  - df["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        v = float(tr.rolling(period).mean().iloc[-1])
        return v if v == v else 0.0   # NaN guard
    except Exception:
        return 0.0


def _outcome(df5_day: pd.DataFrame, after_idx: int, direction: str,
             entry: float, sl: float, target: float) -> str:
    """
    Scan bars AFTER after_idx on the same day (up to OUTCOME_BARS).
    Return TARGET_HIT, SL_HIT, or NEUTRAL.
    """
    future = df5_day[df5_day.index > after_idx].head(OUTCOME_BARS)
    if future.empty:
        return "NEUTRAL"
    for _, bar in future.iterrows():
        if direction == "long":
            if float(bar["low"])  <= sl:     return "SL_HIT"
            if float(bar["high"]) >= target: return "TARGET_HIT"
        else:
            if float(bar["high"]) >= sl:     return "SL_HIT"
            if float(bar["low"])  <= target: return "TARGET_HIT"
    return "NEUTRAL"


# ── Per-symbol simulation ──────────────────────────────────────────────────────

def simulate_symbol(symbol: str,
                    df5: pd.DataFrame,
                    df15: pd.DataFrame,
                    df1d: pd.DataFrame) -> List[Dict]:
    """
    Walk through each trading date × 3 checkpoints.
    At each checkpoint:
      - Run generate_signal on 5m slice → primary signal
      - Run generate_signal on 15m slice → TF confirmation
      - Run generate_signal on 1D (prior days) → regime context
      - Compute outcome from forward 5m bars
    """
    records: List[Dict] = []
    if df5 is None or df5.empty or len(df5) < 30:
        return records

    engine = _get_engine()
    trading_dates = _trading_dates(df5)

    for td in trading_dates:
        # Day's 5m bars (for outcome lookup)
        day_mask  = df5["date"].dt.date == td
        df5_day   = df5[day_mask]

        for cp_label, cp_h, cp_m in CHECKPOINTS:
            cutoff = datetime(td.year, td.month, td.day, cp_h, cp_m, 0)

            # ── 5m slice ──────────────────────────────────────────────
            df5_sl = _slice_to(df5, cutoff)
            if len(df5_sl) < 25:
                continue

            try:
                sig5 = engine.generate_signal(symbol, df5_sl)
            except Exception:
                continue
            if sig5 is None:
                continue

            direction  = sig5.direction
            entry      = float(sig5.entry_price)
            atr_val    = _atr(df5_sl)
            if atr_val <= 0:
                continue

            risk   = atr_val * ATR_MULT_SL
            sl     = (entry - risk) if direction == "long" else (entry + risk)
            target = (entry + risk * RR_RATIO) if direction == "long" else (entry - risk * RR_RATIO)

            last_idx = int(df5_sl.index[-1])
            outcome  = _outcome(df5_day, last_idx, direction, entry, sl, target)
            if outcome == "NEUTRAL":
                continue  # skip non-events (theta decay not modelled here)

            # ── 15m slice ─────────────────────────────────────────────
            sig15_dir, sig15_pats = None, []
            if df15 is not None and not df15.empty:
                df15_sl = _slice_to(df15, cutoff)
                if len(df15_sl) >= 25:
                    try:
                        sig15 = engine.generate_signal(symbol, df15_sl)
                        if sig15:
                            sig15_dir  = sig15.direction
                            sig15_pats = list(sig15.patterns)
                    except Exception:
                        pass

            # ── 1D slice (prior days only) ─────────────────────────────
            sig1d_dir, sig1d_pats = None, []
            if df1d is not None and not df1d.empty:
                df1d_sl = df1d[df1d["date"].dt.date < td]
                if len(df1d_sl) >= 25:
                    try:
                        sig1d = engine.generate_signal(symbol, df1d_sl)
                        if sig1d:
                            sig1d_dir  = sig1d.direction
                            sig1d_pats = list(sig1d.patterns)
                    except Exception:
                        pass

            conf_15m = (sig15_dir == direction)
            conf_1d  = (sig1d_dir == direction)

            records.append({
                "symbol":      symbol,
                "date":        str(td),
                "checkpoint":  cp_label,
                "direction":   direction,
                "outcome":     outcome,
                "patterns_5m":  [p for p in sig5.patterns if p],
                "patterns_15m": sig15_pats,
                "patterns_1d":  sig1d_pats,
                "conf_15m":    conf_15m,
                "conf_1d":     conf_1d,
                "conf_all":    conf_15m and conf_1d,
                "rsi":         round(float(sig5.rsi), 1),
                "vol_ratio":   round(float(sig5.volume_ratio), 2),
            })

    return records


# ── Statistics aggregation ─────────────────────────────────────────────────────

def aggregate_stats(records: List[Dict]) -> Dict:
    """
    Compute per-pattern WR broken down by:
      - direction:pattern key
      - overall / with_15m / with_1d / all_3
    """
    stats: Dict = defaultdict(lambda: {
        "total": 0, "wins": 0,
        "with_15m_total": 0, "with_15m_wins": 0,
        "with_1d_total":  0, "with_1d_wins":  0,
        "all3_total":     0, "all3_wins":      0,
    })

    for rec in records:
        win = (rec["outcome"] == "TARGET_HIT")
        d   = rec["direction"]

        for pat in set(rec.get("patterns_5m", [])):
            pat = pat.strip().lower()
            if not pat or pat.startswith("vol_"):   # skip raw vol tags
                continue
            key = f"{d}:{pat}"
            s   = stats[key]
            s["total"] += 1
            s["wins"]  += int(win)
            if rec["conf_15m"]:
                s["with_15m_total"] += 1
                s["with_15m_wins"]  += int(win)
            if rec["conf_1d"]:
                s["with_1d_total"] += 1
                s["with_1d_wins"]  += int(win)
            if rec["conf_all"]:
                s["all3_total"] += 1
                s["all3_wins"]  += int(win)

    result = {}
    for key, s in stats.items():
        s["WR"]          = round(s["wins"] / s["total"], 4) if s["total"] else 0
        s["with_15m_WR"] = round(s["with_15m_wins"] / s["with_15m_total"], 4) if s["with_15m_total"] else None
        s["with_1d_WR"]  = round(s["with_1d_wins"]  / s["with_1d_total"],  4) if s["with_1d_total"]  else None
        s["all3_WR"]     = round(s["all3_wins"]      / s["all3_total"],     4) if s["all3_total"]     else None
        result[key] = dict(s)

    return result


def compute_pattern_weights(stats: Dict) -> Dict[str, float]:
    """
    direction:pattern → weight (ratio of pattern WR to direction baseline WR).
    Uses all3_WR when available (highest conviction), falls back to overall WR.
    """
    long_entries  = {k: v for k, v in stats.items() if k.startswith("long:")}
    short_entries = {k: v for k, v in stats.items() if k.startswith("short:")}

    def _base_wr(entries):
        tot = sum(v["total"] for v in entries.values())
        w   = sum(v["wins"]  for v in entries.values())
        return w / tot if tot > 0 else 0.5

    long_base  = _base_wr(long_entries)
    short_base = _base_wr(short_entries)

    weights: Dict[str, float] = {}
    for key, s in stats.items():
        if s["total"] < MIN_PAT_COUNT:
            continue
        base = long_base if key.startswith("long:") else short_base
        if base <= 0:
            continue
        # Prefer all3_WR (most selective), fall back to overall
        wr = s["all3_WR"] if (s["all3_WR"] is not None and s["all3_total"] >= 5) else s["WR"]
        ratio = wr / base
        weights[key] = round(max(0.5, min(2.0, ratio)), 4)

    return weights


# ── Update learned_params ──────────────────────────────────────────────────────

def update_learned_params(hist_weights: Dict[str, float]) -> None:
    """Blend historical weights into learned_params.json (30/70 blend)."""
    try:
        with open(PARAMS_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"params": {}}

    current_pw = data["params"].setdefault("PATTERN_WEIGHTS", {})
    updated = 0

    for key, hist_w in hist_weights.items():
        existing = float(current_pw.get(key, 1.0))
        blended  = round(HIST_ALPHA * hist_w + (1 - HIST_ALPHA) * existing, 4)
        if abs(blended - existing) >= 0.005:
            current_pw[key] = blended
            updated += 1

    data["params"]["PATTERN_WEIGHTS"] = current_pw
    data["updated_at"] = datetime.now().isoformat()
    data["historical_scan_at"] = datetime.now().isoformat()

    with open(PARAMS_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  Updated {updated} pattern weights in learned_params.json")


# ── Main scan ──────────────────────────────────────────────────────────────────

def run_historical_scan(symbols=None, days_back: int = 45,
                        max_workers: int = 5, save_raw: bool = True) -> Dict:
    if symbols is None:
        symbols = FO_UNIVERSE

    all_records: List[Dict] = []
    done = 0

    print(f"\nHistorical scan: {len(symbols)} symbols x {days_back}d x 3 checkpoints/day")
    secs_est = len(symbols) * (3 * FETCH_SLEEP + 0.8) / max_workers
    print(f"Estimated: {secs_est/60:.1f} min  (workers={max_workers})\n")

    def _process(sym):
        df5, df15, df1d = fetch_stock_data(sym, days_back=days_back)
        recs = simulate_symbol(sym, df5, df15, df1d)
        return sym, recs

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_process, sym): sym for sym in symbols}
        for fut in as_completed(futs):
            sym, recs = fut.result()
            all_records.extend(recs)
            done += 1
            w  = sum(1 for r in recs if r["outcome"] == "TARGET_HIT")
            t  = len(recs)
            wr = w / t * 100 if t else 0
            print(f"  [{done:3d}/{len(symbols)}] {sym:<16}  signals={t:3d}  WR={wr:.0f}%")

    print(f"\nTotal decided signals: {len(all_records)}")

    if save_raw:
        os.makedirs(os.path.dirname(RAW_FILE), exist_ok=True)
        with open(RAW_FILE, "w", encoding="utf-8") as f:
            for r in all_records:
                f.write(json.dumps(r) + "\n")
        print(f"Raw records saved: {RAW_FILE}")

    stats = aggregate_stats(all_records)
    os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now().isoformat(), "stats": stats}, f, indent=2)
    print(f"Stats saved: {STATS_FILE}")

    return stats


def print_report(stats: Dict, top_n: int = 25) -> None:
    rows = [
        (key, s["total"], s["WR"],
         s.get("with_15m_WR"), s.get("with_1d_WR"), s.get("all3_WR"),
         s.get("all3_total", 0))
        for key, s in stats.items()
        if s["total"] >= MIN_PAT_COUNT
    ]
    rows.sort(key=lambda x: -(x[6] or 0) * (x[5] or x[2]))  # sort by all3 volume × WR

    hdr = f"  {'Pattern':<45} {'N':>5} {'WR':>6} {'15m+':>7} {'1D+':>7} {'all3':>7} {'n3':>5}"
    print("\n" + hdr)
    print("  " + "-" * 83)
    for key, n, wr, wr15, wr1d, wr3, n3 in rows[:top_n]:
        def f(v): return f"{v:.0%}" if v is not None else "  --- "
        print(f"  {key:<45} {n:>5} {wr:.0%}  {f(wr15):>7} {f(wr1d):>7} {f(wr3):>7} {n3:>5}")

    # Timeframe insight: how much does confluence improve WR?
    print("\n  Confluence lift (average across all patterns with n3>=5):")
    with_15 = [(s["WR"], s["with_15m_WR"]) for s in stats.values()
               if s["with_15m_total"] >= 5 and s["with_15m_WR"] is not None]
    with_all = [(s["WR"], s["all3_WR"]) for s in stats.values()
                if s["all3_total"] >= 5 and s["all3_WR"] is not None]
    if with_15:
        base = sum(x[0] for x in with_15) / len(with_15)
        lift = sum(x[1] for x in with_15) / len(with_15)
        print(f"    5m-only WR={base:.0%}  -> with 15m={lift:.0%}  (+{(lift-base)*100:.1f}pp)")
    if with_all:
        base = sum(x[0] for x in with_all) / len(with_all)
        lift = sum(x[1] for x in with_all) / len(with_all)
        print(f"    5m-only WR={base:.0%}  -> all 3 TF={lift:.0%}  (+{(lift-base)*100:.1f}pp)")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    ap = argparse.ArgumentParser(description="Historical F&O pattern learner")
    ap.add_argument("--days",    type=int, default=45,
                    help="Days of history to fetch (max 60 for intraday)")
    ap.add_argument("--symbols", type=int, default=0,
                    help="Number of symbols (0 = all)")
    ap.add_argument("--workers", type=int, default=5,
                    help="Parallel fetch workers")
    ap.add_argument("--update",  action="store_true",
                    help="Update learned_params.json with computed weights")
    ap.add_argument("--report-only", action="store_true",
                    help="Skip scan, just print report from existing stats file")
    args = ap.parse_args()

    if args.report_only:
        with open(STATS_FILE) as f:
            data = json.load(f)
        print_report(data["stats"])
        sys.exit(0)

    syms = FO_UNIVERSE[:args.symbols] if args.symbols > 0 else FO_UNIVERSE

    stats = run_historical_scan(
        symbols=syms, days_back=args.days, max_workers=args.workers
    )

    print_report(stats)

    hist_weights = compute_pattern_weights(stats)
    print(f"\n  Computed {len(hist_weights)} historical weights:")
    sorted_w = sorted(hist_weights.items(), key=lambda x: -x[1])
    for key, w in sorted_w[:10]:
        arrow = "UP  " if w >= 1.0 else "DOWN"
        print(f"    {key:<45} {w:.3f}  [{arrow}]")
    if len(sorted_w) > 10:
        print(f"    ... and {len(sorted_w)-10} more")

    if args.update:
        print("\n  Updating learned_params.json...")
        update_learned_params(hist_weights)
    else:
        print("\n  Run with --update to apply to learned_params.json")
