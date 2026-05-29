"""
Per-stock breakout indicator study.

Scans F&O universe history, finds real breakout moments (2%+ moves),
snapshots ALL indicator values at breakout start. Builds per-stock
profile showing which indicators cross and at what values when
breakouts happen.

Output: logs/stock_profiles/<SYMBOL>.json per stock with:
  - All breakout events detected
  - Indicator values at each breakout start
  - Indicator crossing summary (which indicators fire most often)
  - Optimal RSI range, volume ratio, EMA state for this stock

Usage:
  python -m core.breakout_study               # all 100 F&O stocks
  python -m core.breakout_study --symbol TCS   # single stock
  python -m core.breakout_study --top 20       # top 20 only
"""

import json
import logging
import os
import time
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

PROFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "logs", "stock_profiles")

# Breakout detection params
MIN_MOVE_PCT = 2.0          # minimum % move to qualify as breakout
LOOKBACK_WINDOW = 60        # bars to measure move over (5h)
LOOKBACK_DAYS = 10          # days of 5m data to fetch
MIN_BARS = 100              # minimum bars needed


def _fetch_5m_data(symbol: str) -> Optional[pd.DataFrame]:
    """Fetch 5m OHLCV data for symbol."""
    try:
        from core.api_dhan import dhan_intraday
        df = dhan_intraday(symbol, 5, LOOKBACK_DAYS)
        if df is not None and len(df) >= MIN_BARS:
            return df
    except Exception as e:
        log.debug(f"[Study] fetch {symbol}: {e}")
    return None


def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all indicators for every bar. Returns enriched DataFrame."""
    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    volume = df['volume'].values.astype(float)
    n = len(df)

    # ── EMAs ─────────────────────────────────────────────────────────
    def ema(arr, period):
        out = np.zeros(len(arr))
        out[0] = arr[0]
        k = 2.0 / (period + 1)
        for i in range(1, len(arr)):
            out[i] = arr[i] * k + out[i-1] * (1-k)
        return out

    df = df.copy()
    df['ema8'] = ema(close, 8)
    df['ema9'] = ema(close, 9)
    df['ema21'] = ema(close, 21)
    df['ema50'] = ema(close, 50) if n >= 50 else close

    # EMA crossover state
    df['ema8_above_21'] = df['ema8'] > df['ema21']
    df['ema9_above_21'] = df['ema9'] > df['ema21']

    # EMA cross events (transition from below to above or vice versa)
    df['ema_bull_cross'] = df['ema9_above_21'] & ~df['ema9_above_21'].shift(1).fillna(False)
    df['ema_bear_cross'] = ~df['ema9_above_21'] & df['ema9_above_21'].shift(1).fillna(True)

    # EMA stack: 8 > 21 > 50 (bull) or 8 < 21 < 50 (bear)
    if n >= 50:
        df['ema_stack_bull'] = (df['ema8'] > df['ema21']) & (df['ema21'] > df['ema50'])
        df['ema_stack_bear'] = (df['ema8'] < df['ema21']) & (df['ema21'] < df['ema50'])
    else:
        df['ema_stack_bull'] = False
        df['ema_stack_bear'] = False

    # ── RSI ──────────────────────────────────────────────────────────
    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(span=14, adjust=False).mean()
    avg_loss = loss.ewm(span=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))
    df['rsi'] = df['rsi'].fillna(50)

    # ── VWAP ─────────────────────────────────────────────────────────
    tp = (high + low + close) / 3
    cum_tp_vol = np.cumsum(tp * volume)
    cum_vol = np.cumsum(volume)
    df['vwap'] = np.where(cum_vol > 0, cum_tp_vol / cum_vol, close)
    df['above_vwap'] = close > df['vwap'].values

    # ── Volume ratio ─────────────────────────────────────────────────
    vol_ma20 = pd.Series(volume).rolling(20, min_periods=5).mean()
    df['volume_ratio'] = np.where(vol_ma20 > 0, volume / vol_ma20, 1.0)

    # ── ATR ───────────────────────────────────────────────────────────
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - np.roll(close, 1)),
                               np.abs(low - np.roll(close, 1))))
    tr[0] = high[0] - low[0]
    atr = pd.Series(tr).ewm(span=14, adjust=False).mean().values
    df['atr'] = atr
    df['atr_pct'] = np.where(close > 0, atr / close * 100, 0)

    # ── Supertrend ────────────────────────────────────────────────────
    period, mult = 10, 3.0
    if n >= period + 2:
        atr_st = pd.Series(tr).rolling(period).mean().values
        hl2 = (high + low) / 2
        upper = hl2 + mult * atr_st
        lower = hl2 - mult * atr_st
        st_dir = np.ones(n)  # 1 = uptrend
        for i in range(1, n):
            if close[i] > upper[i-1]:
                st_dir[i] = 1
            elif close[i] < lower[i-1]:
                st_dir[i] = -1
            else:
                st_dir[i] = st_dir[i-1]
                if st_dir[i] == 1:
                    lower[i] = max(lower[i], lower[i-1])
                else:
                    upper[i] = min(upper[i], upper[i-1])
        df['supertrend_up'] = st_dir > 0
    else:
        df['supertrend_up'] = True

    # ── MACD ──────────────────────────────────────────────────────────
    ema12 = ema(close, 12)
    ema26 = ema(close, 26)
    macd_line = ema12 - ema26
    signal_line = ema(macd_line, 9)
    df['macd'] = macd_line
    df['macd_signal'] = signal_line
    df['macd_hist'] = macd_line - signal_line
    df['macd_bull_cross'] = (macd_line > signal_line) & (np.roll(macd_line, 1) <= np.roll(signal_line, 1))
    df['macd_bear_cross'] = (macd_line < signal_line) & (np.roll(macd_line, 1) >= np.roll(signal_line, 1))

    # ── Bollinger Bands ───────────────────────────────────────────────
    bb_ma = pd.Series(close).rolling(20, min_periods=5).mean()
    bb_std = pd.Series(close).rolling(20, min_periods=5).std()
    df['bb_upper'] = bb_ma + 2 * bb_std
    df['bb_lower'] = bb_ma - 2 * bb_std
    df['bb_width_pct'] = np.where(bb_ma > 0, (4 * bb_std / bb_ma * 100), 0)

    # ── Price position ────────────────────────────────────────────────
    rolling_high = pd.Series(high).rolling(20, min_periods=5).max()
    rolling_low = pd.Series(low).rolling(20, min_periods=5).min()
    rng = rolling_high - rolling_low
    df['price_position'] = np.where(rng > 0, (close - rolling_low) / rng, 0.5)

    return df


def _detect_breakouts(df: pd.DataFrame, min_move_pct: float = MIN_MOVE_PCT
                       ) -> List[Dict]:
    """Find breakout moments: bars where forward move >= min_move_pct.

    Returns list of breakout events with bar index and direction.
    """
    close = df['close'].values.astype(float)
    n = len(close)
    breakouts = []
    used_ranges = []  # avoid overlapping events

    for i in range(n - 20):  # need at least 20 bars forward
        # Look forward up to LOOKBACK_WINDOW bars
        end = min(i + LOOKBACK_WINDOW, n)
        future = close[i+1:end]
        if len(future) < 10:
            continue

        max_up = (np.max(future) - close[i]) / close[i] * 100
        max_down = (close[i] - np.min(future)) / close[i] * 100

        # Check if this overlaps with existing breakout
        overlaps = any(s <= i <= e for s, e in used_ranges)
        if overlaps:
            continue

        if max_up >= min_move_pct and max_up > max_down:
            peak_idx = i + 1 + np.argmax(future)
            breakouts.append({
                "bar_idx": i,
                "direction": "long",
                "move_pct": round(max_up, 2),
                "bars_to_peak": int(peak_idx - i),
            })
            used_ranges.append((i, peak_idx))
        elif max_down >= min_move_pct and max_down > max_up:
            trough_idx = i + 1 + np.argmin(future)
            breakouts.append({
                "bar_idx": i,
                "direction": "short",
                "move_pct": round(max_down, 2),
                "bars_to_peak": int(trough_idx - i),
            })
            used_ranges.append((i, trough_idx))

    return breakouts


def _snapshot_indicators(df: pd.DataFrame, bar_idx: int) -> Dict:
    """Capture all indicator values at a specific bar."""
    row = df.iloc[bar_idx]

    snapshot = {
        # Price
        "close": round(float(row['close']), 2),
        # EMAs
        "ema8": round(float(row.get('ema8', 0)), 2),
        "ema9": round(float(row.get('ema9', 0)), 2),
        "ema21": round(float(row.get('ema21', 0)), 2),
        "ema8_above_21": bool(row.get('ema8_above_21', False)),
        "ema9_above_21": bool(row.get('ema9_above_21', False)),
        "ema_bull_cross": bool(row.get('ema_bull_cross', False)),
        "ema_bear_cross": bool(row.get('ema_bear_cross', False)),
        "ema_stack_bull": bool(row.get('ema_stack_bull', False)),
        "ema_stack_bear": bool(row.get('ema_stack_bear', False)),
        # RSI
        "rsi": round(float(row.get('rsi', 50)), 1),
        # VWAP
        "above_vwap": bool(row.get('above_vwap', False)),
        "vwap_dist_pct": round((float(row['close']) - float(row.get('vwap', row['close'])))
                                / float(row['close']) * 100, 2) if float(row['close']) > 0 else 0,
        # Volume
        "volume_ratio": round(float(row.get('volume_ratio', 1)), 2),
        # ATR
        "atr_pct": round(float(row.get('atr_pct', 0)), 3),
        # Supertrend
        "supertrend_up": bool(row.get('supertrend_up', True)),
        # MACD
        "macd_hist": round(float(row.get('macd_hist', 0)), 4),
        "macd_bull_cross": bool(row.get('macd_bull_cross', False)),
        "macd_bear_cross": bool(row.get('macd_bear_cross', False)),
        # Bollinger
        "bb_width_pct": round(float(row.get('bb_width_pct', 0)), 2),
        # Price position in range (0=low, 1=high)
        "price_position": round(float(row.get('price_position', 0.5)), 2),
    }

    # Check EMA pullback: price near EMA21 (within 0.3%)
    if float(row['close']) > 0:
        ema21_dist = abs(float(row['close']) - float(row.get('ema21', row['close']))) / float(row['close']) * 100
        snapshot["ema21_dist_pct"] = round(ema21_dist, 2)
        snapshot["ema21_pullback"] = ema21_dist < 0.3
    else:
        snapshot["ema21_dist_pct"] = 0
        snapshot["ema21_pullback"] = False

    return snapshot


def _build_summary(breakouts: List[Dict]) -> Dict:
    """Aggregate breakout statistics for a stock."""
    if not breakouts:
        return {}

    long_events = [b for b in breakouts if b["direction"] == "long"]
    short_events = [b for b in breakouts if b["direction"] == "short"]

    def _indicator_stats(events: List[Dict], key: str) -> Dict:
        """Compute min/max/median/mean for a numeric indicator across events."""
        vals = [e["indicators"].get(key) for e in events
                if e["indicators"].get(key) is not None]
        if not vals:
            return {}
        vals = [float(v) for v in vals]
        return {
            "min": round(min(vals), 2),
            "max": round(max(vals), 2),
            "median": round(float(np.median(vals)), 2),
            "mean": round(float(np.mean(vals)), 2),
        }

    def _bool_rate(events: List[Dict], key: str) -> Optional[float]:
        """What % of breakout events had this indicator True?"""
        vals = [e["indicators"].get(key) for e in events
                if e["indicators"].get(key) is not None]
        if not vals:
            return None
        return round(sum(1 for v in vals if v) / len(vals), 3)

    summary = {
        "total_breakouts": len(breakouts),
        "long_breakouts": len(long_events),
        "short_breakouts": len(short_events),
        "avg_move_pct": round(float(np.mean([b["move_pct"] for b in breakouts])), 2),
        "avg_bars_to_peak": round(float(np.mean([b["bars_to_peak"] for b in breakouts])), 1),
    }

    # Per-direction indicator profiles
    for label, events in [("long", long_events), ("short", short_events)]:
        if not events:
            continue
        prefix = f"{label}_"
        summary[f"{prefix}count"] = len(events)

        # Numeric indicators: range at breakout
        for key in ["rsi", "volume_ratio", "atr_pct", "bb_width_pct",
                     "price_position", "ema21_dist_pct", "vwap_dist_pct", "macd_hist"]:
            stats = _indicator_stats(events, key)
            if stats:
                summary[f"{prefix}{key}"] = stats

        # Boolean indicators: hit rate at breakout
        for key in ["ema8_above_21", "ema9_above_21", "ema_bull_cross", "ema_bear_cross",
                     "ema_stack_bull", "ema_stack_bear", "above_vwap",
                     "supertrend_up", "macd_bull_cross", "macd_bear_cross",
                     "ema21_pullback"]:
            rate = _bool_rate(events, key)
            if rate is not None:
                summary[f"{prefix}{key}_rate"] = rate

    return summary


def study_stock(symbol: str) -> Optional[Dict]:
    """Run full breakout study for one stock. Returns profile dict."""
    df = _fetch_5m_data(symbol)
    if df is None:
        log.info(f"[Study] {symbol}: no data")
        return None

    # Compute all indicators
    df = _compute_indicators(df)

    # Find breakout events
    raw_breakouts = _detect_breakouts(df)
    if not raw_breakouts:
        log.info(f"[Study] {symbol}: no breakouts found")
        return None

    # Snapshot indicators at each breakout
    breakouts = []
    for b in raw_breakouts:
        b["indicators"] = _snapshot_indicators(df, b["bar_idx"])
        # Add timestamp if available
        if hasattr(df.index, 'strftime') or 'datetime' in str(df.index.dtype):
            try:
                b["timestamp"] = str(df.index[b["bar_idx"]])
            except Exception:
                pass
        breakouts.append(b)

    # Build summary
    summary = _build_summary(breakouts)

    profile = {
        "symbol": symbol,
        "study_date": time.strftime("%Y-%m-%d"),
        "data_bars": len(df),
        "data_days": LOOKBACK_DAYS,
        "breakouts": breakouts,
        "summary": summary,
    }

    # Save to file
    os.makedirs(PROFILE_DIR, exist_ok=True)
    fpath = os.path.join(PROFILE_DIR, f"{symbol}.json")
    try:
        with open(fpath, "w") as f:
            json.dump(profile, f, indent=2, default=str)
        log.info(f"[Study] {symbol}: {len(breakouts)} breakouts saved to {fpath}")
    except Exception as e:
        log.error(f"[Study] {symbol}: save failed: {e}")

    return profile


def load_profile(symbol: str) -> Optional[Dict]:
    """Load a stock's breakout profile from disk."""
    fpath = os.path.join(PROFILE_DIR, f"{symbol}.json")
    if not os.path.exists(fpath):
        return None
    try:
        with open(fpath) as f:
            return json.load(f)
    except Exception:
        return None


def score_current_vs_profile(symbol: str, direction: str,
                              rsi: float, volume_ratio: float,
                              ema9_above_21: bool, supertrend_up: bool,
                              above_vwap: bool) -> Optional[Dict]:
    """Score current indicator values against stock's breakout profile.

    Returns dict with:
      - match_score: 0.0-1.0 (how well current conditions match breakout fingerprint)
      - rsi_in_range: bool (RSI within breakout RSI range for this stock)
      - vol_adequate: bool (volume ratio >= median breakout volume)
      - details: human-readable breakdown

    Returns None if no profile exists.
    """
    profile = load_profile(symbol)
    if not profile:
        return None

    summary = profile.get("summary", {})
    prefix = f"{direction}_"
    count = summary.get(f"{prefix}count", 0)
    if count < 2:
        return None

    checks = []
    score = 0.0
    total_weight = 0.0

    # 1. RSI in breakout range (weight 3)
    rsi_stats = summary.get(f"{prefix}rsi")
    if rsi_stats:
        rsi_min, rsi_max = rsi_stats["min"], rsi_stats["max"]
        rsi_in = rsi_min - 5 <= rsi <= rsi_max + 5  # 5pt grace
        if rsi_in:
            score += 3
            checks.append(f"RSI {rsi:.0f} in [{rsi_min:.0f}-{rsi_max:.0f}]")
        else:
            checks.append(f"RSI {rsi:.0f} OUTSIDE [{rsi_min:.0f}-{rsi_max:.0f}]")
        total_weight += 3

    # 2. Volume adequate (weight 2)
    vol_stats = summary.get(f"{prefix}volume_ratio")
    if vol_stats:
        vol_med = vol_stats["median"]
        vol_ok = volume_ratio >= vol_med * 0.7  # 30% grace
        if vol_ok:
            score += 2
            checks.append(f"Vol {volume_ratio:.1f}x >= {vol_med:.1f}x med")
        else:
            checks.append(f"Vol {volume_ratio:.1f}x < {vol_med:.1f}x med")
        total_weight += 2

    # 3. EMA alignment (weight 2)
    ema_rate = summary.get(f"{prefix}ema9_above_21_rate")
    if ema_rate is not None:
        # If 70%+ of breakouts had ema9>21, current should match
        expected = ema_rate >= 0.5
        matches = (ema9_above_21 == expected)
        if matches:
            score += 2
            checks.append(f"EMA9>21={ema9_above_21} matches {ema_rate:.0%} rate")
        else:
            checks.append(f"EMA9>21={ema9_above_21} vs {ema_rate:.0%} rate")
        total_weight += 2

    # 4. Supertrend alignment (weight 2)
    st_rate = summary.get(f"{prefix}supertrend_up_rate")
    if st_rate is not None:
        expected_st = st_rate >= 0.5
        matches_st = (supertrend_up == expected_st)
        if matches_st:
            score += 2
            checks.append(f"ST_up={supertrend_up} matches {st_rate:.0%} rate")
        else:
            checks.append(f"ST_up={supertrend_up} vs {st_rate:.0%} rate")
        total_weight += 2

    # 5. VWAP position (weight 1)
    vwap_rate = summary.get(f"{prefix}above_vwap_rate")
    if vwap_rate is not None:
        expected_vwap = vwap_rate >= 0.5
        matches_vwap = (above_vwap == expected_vwap)
        if matches_vwap:
            score += 1
            checks.append(f"VWAP={above_vwap} matches {vwap_rate:.0%} rate")
        else:
            checks.append(f"VWAP={above_vwap} vs {vwap_rate:.0%} rate")
        total_weight += 1

    match_score = score / total_weight if total_weight > 0 else 0.5

    return {
        "match_score": round(match_score, 3),
        "rsi_in_range": rsi_in if rsi_stats else None,
        "vol_adequate": vol_ok if vol_stats else None,
        "checks": checks,
        "breakout_count": count,
    }


def study_universe(symbols: List[str] = None, top_n: int = 100) -> Dict:
    """Run breakout study on F&O universe. Returns summary dict."""
    if symbols is None:
        from core.universe import FO_UNIVERSE
        symbols = FO_UNIVERSE[:top_n]

    results = {}
    total = len(symbols)
    for i, sym in enumerate(symbols, 1):
        print(f"  [{i}/{total}] {sym}...", end=" ", flush=True)
        try:
            profile = study_stock(sym)
            if profile:
                n = profile["summary"].get("total_breakouts", 0)
                print(f"{n} breakouts")
                results[sym] = profile["summary"]
            else:
                print("skip")
        except Exception as e:
            print(f"err: {e}")

    # Write combined summary
    summary_path = os.path.join(PROFILE_DIR, "_summary.json")
    try:
        with open(summary_path, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nSummary saved to {summary_path}")
    except Exception as e:
        print(f"Summary save error: {e}")

    return results


# ── CLI ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Per-stock breakout indicator study")
    parser.add_argument("--symbol", help="Single stock to study")
    parser.add_argument("--top", type=int, default=100, help="Top N stocks (default 100)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")

    if args.symbol:
        profile = study_stock(args.symbol)
        if profile:
            s = profile["summary"]
            print(f"\n{args.symbol}: {s.get('total_breakouts',0)} breakouts "
                  f"({s.get('long_breakouts',0)}L / {s.get('short_breakouts',0)}S)")
            print(f"Avg move: {s.get('avg_move_pct',0):.1f}%  "
                  f"Avg bars to peak: {s.get('avg_bars_to_peak',0):.0f}")

            # Print key indicator ranges
            for direction in ["long", "short"]:
                prefix = f"{direction}_"
                count = s.get(f"{prefix}count", 0)
                if count == 0:
                    continue
                print(f"\n  {direction.upper()} breakouts ({count}):")
                for key in ["rsi", "volume_ratio", "atr_pct", "bb_width_pct"]:
                    stats = s.get(f"{prefix}{key}")
                    if stats:
                        print(f"    {key:20} median={stats['median']:>6}  range=[{stats['min']}, {stats['max']}]")
                for key in ["ema9_above_21", "supertrend_up", "above_vwap",
                            "ema_bull_cross", "ema_bear_cross", "ema21_pullback"]:
                    rate = s.get(f"{prefix}{key}_rate")
                    if rate is not None:
                        print(f"    {key:20} {rate:>5.0%} of breakouts")
        else:
            print(f"No data for {args.symbol}")
    else:
        study_universe(top_n=args.top)
