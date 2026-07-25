"""
core/allocation.py — path-#1 allocation engine.

After an exhaustive edge hunt found no robust tradeable alpha in this universe
(see docs/research/*.md), the evidence-backed strategy is to capture the equity
RISK PREMIUM cheaply, with disciplined drawdown control — not to manufacture an
edge. This module defines that allocation, computes the current target, and
backtests it honestly net of cost.

Core: a low-cost broad-index ETF (NIFTYBEES / Nifty 50), proxied here by the
NIFTY index in logs/bar_cache. Optional 200-DMA trend overlay de-risks below the
moving average (cuts drawdown ~half, lowers return — a risk-tolerance choice, not
a free lunch; measured, not assumed). Factor tilts are deliberately NOT defaulted
on: the backtested factor outperformance is survivorship-inflated (today's
constituents) and cross-sectional momentum was already rejected under proper
testing. Use real factor-INDEX ETFs for that sleeve if desired.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd

ALLOCATION_CONFIG = {
    "core_instrument": "NIFTYBEES",     # Nifty 50 index ETF (proxied by NIFTY index here)
    "benchmark": "NIFTY",
    "equity_weight": 1.0,               # target core equity exposure when risk-on
    "trend_overlay": {
        "enabled": False,               # opt-in drawdown control (default off = pure index core)
        "ma_window": 200,
        "risk_off_weight": 0.5,         # equity weight when NIFTY < MA (de-risk, not full cash)
    },
    "cash_yield_annual": 0.065,         # India ~risk-free on the de-risked portion
    "cost_per_switch": 0.0010,          # 0.10% turnover cost on a trend state change
    "etf_expense_annual": 0.0005,       # ~0.05% NIFTYBEES expense ratio
}


@dataclass
class TargetAllocation:
    asof: str
    state: str                          # "risk_on" | "risk_off" | "core_only"
    equity_weight: float
    cash_weight: float
    note: str = ""


# A NIFTY cache older than this many calendar days is treated as stale. 4 covers
# a Fri->Mon weekend plus one public holiday; anything beyond that means the feed
# has actually stopped, not that the exchange was shut.
MAX_CACHE_AGE_DAYS = 4


@dataclass
class CacheRefresh:
    """Outcome of a NIFTY cache refresh.

    Exists because the previous version returned the same bare date string on
    success and on failure, so no caller could tell a live feed from a dead one.
    A silent fall back to stale bars once produced a confident 'risk_off 50/50'
    target computed from 3-day-old prices (2026-07-24). Freshness is now data.
    """
    last_date: str                      # last bar on disk (YYYY-MM-DD)
    ok: bool                            # did the remote fetch succeed?
    added: int = 0                      # bars appended this run
    error: str = ""                     # exception text when ok is False

    def age_days(self, today: Optional[pd.Timestamp] = None) -> int:
        now = pd.Timestamp.now().normalize() if today is None else pd.Timestamp(today).normalize()
        return int((now - pd.Timestamp(self.last_date).normalize()).days)

    def is_stale(self, today: Optional[pd.Timestamp] = None,
                 max_age_days: int = MAX_CACHE_AGE_DAYS) -> bool:
        """Stale if the feed errored, or the newest bar is too old to be current."""
        return (not self.ok) or self.age_days(today) > max_age_days


def _equity_weight_series(nifty: pd.Series, cfg: dict) -> pd.Series:
    """Daily target equity weight (point-in-time: depends only on prior close)."""
    ov = cfg["trend_overlay"]
    full = cfg["equity_weight"]
    if not ov["enabled"]:
        return pd.Series(full, index=nifty.index)
    ma = nifty.rolling(ov["ma_window"]).mean()
    above = (nifty > ma)
    w = pd.Series(np.where(above, full, ov["risk_off_weight"]), index=nifty.index)
    # before the MA is defined, hold core (no signal yet)
    w[ma.isna()] = full
    return w


def compute_target_allocation(nifty: pd.Series, asof: Optional[str] = None,
                              cfg: dict = None) -> TargetAllocation:
    """Today's (or asof's) target allocation from the latest available close."""
    cfg = cfg or ALLOCATION_CONFIG
    s = nifty.sort_index()
    if asof is not None:
        s = s[s.index <= pd.to_datetime(asof)]
    if len(s) == 0:
        raise ValueError("no price history on/before asof")
    w = _equity_weight_series(s, cfg)
    eq = float(w.iloc[-1])
    ov = cfg["trend_overlay"]
    if not ov["enabled"]:
        state, note = "core_only", "pure index core (trend overlay off)"
    else:
        ma = s.rolling(ov["ma_window"]).mean().iloc[-1]
        above = s.iloc[-1] > ma if not pd.isna(ma) else True
        state = "risk_on" if above else "risk_off"
        note = f"NIFTY {s.iloc[-1]:.0f} {'>' if above else '<'} {ov['ma_window']}DMA {ma:.0f}"
    return TargetAllocation(asof=str(s.index[-1].date()), state=state,
                            equity_weight=eq, cash_weight=round(1 - eq, 4), note=note)


def backtest_allocation(nifty: pd.Series, cfg: dict = None) -> pd.Series:
    """Daily net portfolio returns for the allocation (no lookahead; net of
    switch cost + ETF expense + cash yield on the de-risked portion)."""
    cfg = cfg or ALLOCATION_CONFIG
    s = nifty.sort_index()
    ret = s.pct_change().fillna(0.0)
    w = _equity_weight_series(s, cfg).shift(1).fillna(cfg["equity_weight"])  # act on prior signal
    cash_daily = cfg["cash_yield_annual"] / 252
    expense_daily = cfg["etf_expense_annual"] / 252
    switch_cost = w.diff().abs().fillna(0.0) * cfg["cost_per_switch"]
    port = w * ret + (1 - w) * cash_daily - switch_cost - expense_daily * w
    return port.rename("alloc_ret")


def perf_summary(r: pd.Series, rf: float = 0.065) -> Dict[str, float]:
    r = r.dropna()
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    cagr = (1 + r).prod() ** (1 / yrs) - 1
    vol = r.std() * np.sqrt(252)
    eq = (1 + r).cumprod()
    return {
        "cagr": round(cagr, 4), "vol": round(vol, 4),
        "sharpe": round((cagr - rf) / vol, 3) if vol else 0.0,
        "max_dd": round((eq / eq.cummax() - 1).min(), 4),
        "years": round(yrs, 1),
    }


HORIZONS = [("1 day", 1), ("1 week", 5), ("1 month", 21), ("3 months", 63),
            ("6 months", 126), ("1 year", 252), ("2 years", 504), ("3 years", 756)]


def horizon_accuracy(r: pd.Series) -> list:
    """Probability of profit + avg/worst return by holding period (overlapping
    windows — a 'what are my odds if I enter today' readout, not indep. samples)."""
    eq = (1 + r.dropna()).cumprod()
    rows = []
    for label, days in HORIZONS:
        w = eq.pct_change(days).dropna()
        if len(w) < 30:
            continue
        rows.append({
            "horizon": label, "days": days,
            "accuracy": round(float((w > 0).mean()), 4),
            "avg_return": round(float(w.mean()), 4),
            "worst": round(float(w.min()), 4),
            "windows": int(len(w)),
        })
    return rows


def load_nifty(path: str = "logs/bar_cache/NIFTY.parquet") -> pd.Series:
    return pd.read_parquet(path).sort_index()["close"]


def top100_fno_composite(cache_dir: str = "logs/bar_cache") -> pd.Series:
    """Equal-weight composite of the top-100 F&O stocks by average turnover.
    SURVIVORS-ONLY: the cache holds today's F&O members, so this backtest
    silently deletes every delisted blow-up. Measured inflation on the
    survivorship-complete bhavcopy archive: ~+9.7pp CAGR (2019-20 window).
    Comparison/monitoring ONLY — never a tradeable-performance claim."""
    import glob
    import os
    px, turn = {}, {}
    for f in glob.glob(os.path.join(cache_dir, "*.parquet")):
        sym = os.path.splitext(os.path.basename(f))[0]
        if sym.startswith("_") or sym in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            continue
        d = pd.read_parquet(f).sort_index()
        if len(d) < 500 or "volume" not in d:
            continue
        px[sym] = d["close"]
        turn[sym] = float((d["close"] * d["volume"]).mean())
    top = sorted(turn, key=turn.get, reverse=True)[:100]
    ret = pd.DataFrame({s: px[s] for s in top}).sort_index().pct_change(fill_method=None)
    ew = ret.mean(axis=1).dropna()
    return ((1 + ew).cumprod() * 100).rename("close")


def equity_curve_points(series: pd.Series, freq: str = "ME") -> list:
    """Growth-of-100 sampled at freq — compact payload for the dashboard chart."""
    s = series.sort_index()
    s = s / s.iloc[0] * 100
    pts = s.resample(freq).last().dropna()
    return [{"d": d.strftime("%Y-%m"), "v": round(float(v), 2)} for d, v in pts.items()]


def refresh_nifty_cache(path: str = "logs/bar_cache/NIFTY.parquet") -> CacheRefresh:
    """Append fresh NIFTY daily bars from yfinance (fallback feed).

    Never raises — on any fetch failure the stale cache stands, so the daily job
    survives a flaky feed. But the failure is REPORTED: the returned CacheRefresh
    carries ok/error/age so the caller can refuse to present stale bars as live.
    Callers must check `.is_stale()`; ignoring it reintroduces the silent-failure
    bug this type was added to kill.
    """
    old = pd.read_parquet(path).sort_index()
    try:
        import yfinance as yf
        start = (old.index.max() - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
        new = yf.download("^NSEI", start=start, progress=False, auto_adjust=False)
        if new is None or len(new) == 0:
            return CacheRefresh(str(old.index.max().date()), ok=False,
                                error="yfinance returned no rows for ^NSEI")
        if isinstance(new.columns, pd.MultiIndex):
            new.columns = [c[0].lower() for c in new.columns]
        else:
            new.columns = [str(c).lower() for c in new.columns]
        new = new[["open", "high", "low", "close", "volume"]]
        # cache convention stores bars at prior-day 18:30 (UTC-shifted IST dates)
        off = old.index[-1] - old.index[-1].normalize()
        if off > pd.Timedelta(hours=12):
            new.index = new.index.normalize() + off - pd.Timedelta(days=1)
        old_days = set((old.index + pd.Timedelta(hours=6)).normalize())
        add = new[~(new.index + pd.Timedelta(hours=6)).normalize().isin(old_days)]
        if len(add):
            pd.concat([old, add]).sort_index().to_parquet(path)
            return CacheRefresh(str(add.index.max().date()), ok=True, added=len(add))
        # Fetch succeeded but had nothing new — normal on a weekend/holiday. The
        # cache date still drives staleness, so a feed frozen at an old date is
        # caught by age even though ok is True.
        return CacheRefresh(str(old.index.max().date()), ok=True, added=0)
    except Exception as e:
        return CacheRefresh(str(old.index.max().date()), ok=False,
                            error=f"{type(e).__name__}: {e}")
