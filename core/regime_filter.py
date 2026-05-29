"""
Regime filter — block strategies in unsuitable market regimes.

Premise: every strategy has a regime it works in and a regime that kills it.
Trading a trend-following strategy in chop = paying spread + commission for
no edge. This module computes NIFTY regime indicators and exposes a per-
strategy `trade_allowed()` gate that the scanner calls before emitting
signals.

Indicators (all on NIFTY daily bars, 1h cache):
  - ADX(14)              : trend strength. <20 = chop, >25 = trend
  - ATR(14) percentile   : volatility regime. >80%ile = panic, <20%ile = dead
  - 20d return           : direction bias for tactical filters
  - Earnings cluster     : >5 F&O stocks reporting in the next 3 sessions

Strategy gates:
  india_swing : ADX >= 20 (need trend) AND not in earnings_cluster_week
  ORB         : ATR pct >= 30 (need movement) AND not in earnings_cluster_week
  Vol expansion : ATR pct <= 30 (need compression to bet on expansion)
  Pullback     : same as base strategy (it wraps india_swing or ORB)

Cold-start safe: any fetch failure → trade_allowed=True (degrade open).
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────
ADX_TREND_MIN     = 20.0   # below = chop, block trend strategies
ADX_LOOKBACK      = 14
ATR_LOOKBACK      = 14
ATR_PCT_WINDOW    = 252    # 1yr rolling percentile window
ATR_PCT_TREND_MIN = 30.0   # ORB needs movement
ATR_PCT_VOL_MAX   = 30.0   # Vol expansion needs compression
CACHE_TTL_SEC     = 3600

_REGIME_CACHE: Dict[str, Tuple[float, "RegimeSnapshot"]] = {}


@dataclass
class RegimeSnapshot:
    adx: float
    atr_pct: float           # 0-100 percentile of ATR vs 1yr history
    ret_20d_pct: float       # signed 20d return
    earnings_cluster: bool   # 5+ F&O reports in next 3 sessions
    tag: str                 # human-readable summary
    valid: bool = True
    reason: str = "ok"


# ── Indicator math ────────────────────────────────────────────────────
def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h = df["high"]; l = df["low"]; c = df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def _adx(df: pd.DataFrame, n: int = ADX_LOOKBACK) -> float:
    """Wilder ADX. Returns last value or NaN."""
    h = df["high"].reset_index(drop=True)
    l = df["low"].reset_index(drop=True)
    c = df["close"].reset_index(drop=True)
    up = h.diff()
    dn = -l.diff()
    plus_dm  = pd.Series(np.where((up > dn) & (up > 0), up, 0.0))
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0))
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_n = tr.rolling(n, min_periods=n).mean()
    plus_di  = 100 * plus_dm.rolling(n, min_periods=n).mean()  / atr_n.replace(0, np.nan)
    minus_di = 100 * minus_dm.rolling(n, min_periods=n).mean() / atr_n.replace(0, np.nan)
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx_series = dx.rolling(n, min_periods=n).mean()
    if adx_series.dropna().empty:
        return float("nan")
    val = adx_series.dropna().iloc[-1]
    return float(val)


def _atr_percentile(df: pd.DataFrame) -> float:
    """Current ATR rank vs trailing ATR_PCT_WINDOW bars. 0-100."""
    atr_series = _atr(df, ATR_LOOKBACK)
    if len(atr_series.dropna()) < 30:
        return float("nan")
    window = atr_series.dropna().iloc[-ATR_PCT_WINDOW:]
    cur = atr_series.iloc[-1]
    if np.isnan(cur):
        return float("nan")
    return float((window <= cur).mean() * 100)


# ── NIFTY fetcher (Dhan only) ────────────────────────────────────────
def _fetch_nifty_daily(days: int = 400) -> Optional[pd.DataFrame]:
    """NIFTY daily OHLCV from Dhan only (index segment). Returns DataFrame
    indexed by date with lowercase OHLCV columns, or None on failure."""
    try:
        from core.api_dhan import dhan_daily
        raw = dhan_daily("NIFTY", days_back=days)
        if raw is None or raw.empty:
            return None
        df = raw.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        log.debug(f"[REGIME] NIFTY Dhan fetch failed: {e}")
        return None


# ── Earnings cluster (placeholder — wire to actual NSE earnings cal) ─
def _earnings_cluster_active(as_of_date=None) -> bool:
    """Stub: returns False until earnings_calendar.py is wired.
    Real impl: count F&O stocks with earnings in [today, today+3 sessions];
    cluster if >=5.
    """
    try:
        from core.earnings_filter import is_earnings_cluster  # may not exist
        return bool(is_earnings_cluster(as_of_date=as_of_date))
    except Exception:
        return False


# ── Public API ────────────────────────────────────────────────────────
def get_regime(as_of_date=None) -> RegimeSnapshot:
    """Return current NIFTY regime snapshot. Cached 1h.
    as_of_date for backtest use (point-in-time)."""
    cache_key = "live" if as_of_date is None else f"asof_{pd.Timestamp(as_of_date).date()}"
    cached = _REGIME_CACHE.get(cache_key)
    if cached and as_of_date is None and (_time.time() - cached[0]) < CACHE_TTL_SEC:
        return cached[1]
    if cached and as_of_date is not None:
        return cached[1]

    df = _fetch_nifty_daily()
    if df is None or len(df) < ATR_PCT_WINDOW:
        snap = RegimeSnapshot(0.0, 0.0, 0.0, False, "unknown", valid=False,
                              reason="nifty_data_unavailable")
        _REGIME_CACHE[cache_key] = (_time.time(), snap)
        return snap

    if as_of_date is not None:
        as_of = pd.Timestamp(as_of_date).normalize()
        df = df[df.index <= as_of]
        if len(df) < ATR_PCT_WINDOW // 2:
            snap = RegimeSnapshot(0.0, 0.0, 0.0, False, "unknown", valid=False,
                                  reason="too_few_bars_at_asof")
            _REGIME_CACHE[cache_key] = (_time.time(), snap)
            return snap

    adx = _adx(df)
    atr_pct = _atr_percentile(df)
    close = df["close"].dropna()
    ret_20d = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) >= 21 else 0.0
    earn_cluster = _earnings_cluster_active(as_of_date)

    # Tag
    if np.isnan(adx) or np.isnan(atr_pct):
        tag = "unknown"
    elif adx < ADX_TREND_MIN and atr_pct < 30:
        tag = "chop_quiet"
    elif adx < ADX_TREND_MIN:
        tag = "chop_volatile"
    elif atr_pct > 80:
        tag = "trend_high_vol"
    elif ret_20d > 0:
        tag = "trend_up"
    else:
        tag = "trend_down"

    snap = RegimeSnapshot(
        adx=round(adx, 1) if not np.isnan(adx) else 0.0,
        atr_pct=round(atr_pct, 1) if not np.isnan(atr_pct) else 0.0,
        ret_20d_pct=round(ret_20d, 2),
        earnings_cluster=earn_cluster,
        tag=tag,
    )
    _REGIME_CACHE[cache_key] = (_time.time(), snap)
    return snap


def trade_allowed(strategy: str, as_of_date=None) -> Tuple[bool, Dict]:
    """Gate: should this strategy emit signals right now?

    Returns (ok, info_dict). Cold-start safe: if regime data unavailable
    → ok=True (don't block on missing data, log warning).
    """
    snap = get_regime(as_of_date=as_of_date)
    info = {"regime": snap.tag, "adx": snap.adx, "atr_pct": snap.atr_pct,
            "earnings_cluster": snap.earnings_cluster}

    if not snap.valid:
        info["reason"] = "regime_unknown_pass"
        return True, info

    if snap.earnings_cluster:
        return False, {**info, "reason": "earnings_cluster_block"}

    s = strategy.lower()
    if s in ("india_swing", "swing", "trend"):
        if snap.adx < ADX_TREND_MIN:
            return False, {**info, "reason": f"adx_{snap.adx}_below_{ADX_TREND_MIN}"}
        return True, {**info, "reason": "trend_ok"}

    if s == "orb":
        if snap.atr_pct < ATR_PCT_TREND_MIN:
            return False, {**info, "reason": f"atr_pct_{snap.atr_pct}_below_{ATR_PCT_TREND_MIN}"}
        return True, {**info, "reason": "atr_ok"}

    if s in ("vol_expansion", "volatility", "straddle"):
        if snap.atr_pct > ATR_PCT_VOL_MAX:
            return False, {**info, "reason": f"atr_pct_{snap.atr_pct}_above_{ATR_PCT_VOL_MAX}"}
        return True, {**info, "reason": "compression_ok"}

    # Unknown strategy = pass-through
    return True, {**info, "reason": "unknown_strategy_pass"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    snap = get_regime()
    print(f"Regime: {snap}")
    for s in ("india_swing", "ORB", "vol_expansion"):
        ok, info = trade_allowed(s)
        print(f"  {s:14s} allowed={ok}  {info.get('reason')}")
