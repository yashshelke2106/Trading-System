"""
core/corporate_actions.py — stop splits and bonuses masquerading as returns.

THE PROBLEM
-----------
NSE bhavcopy carries RAW traded prices. A 1:2 split halves the quoted price
overnight and a 1:3 bonus cuts it to a third, and neither is a loss -- the
holder owns proportionally more shares. Measured on the archive, 296 such
moves appear in 400 trading days:

    2024-12-27  NMDC        214.45 -> 69.32   ratio 0.323   ~1:3 bonus
    2024-12-27  MAZDOCK    4729.75 -> 2317.40 ratio 0.490   ~1:2 split
    2024-12-30  BANCOINDIA 1056.10 -> 477.05  ratio 0.452   split

Read naively these are -68%, -51% and -55% "returns". They contaminate every
ranking, every volatility estimate and every backtest that touches raw closes,
and they hit LARGE names (NMDC, MAZDOCK), so a liquidity filter does not save
you.

Rights ENTITLEMENTS are a second trap: the "-RE"/"-RE1"/"-RE2" symbols are
temporary instruments that expire worthless by design. They are not the
underlying company and must never enter a price panel.

WHAT THIS MODULE DOES, AND WHAT IT REFUSES TO DO
------------------------------------------------
It DETECTS suspected corporate actions and neutralises the return for that
day (treats it as missing). It deliberately does NOT invent an adjustment
factor: guessing "that looked like a 1:3" and rescaling the whole history on
a guess turns one visible error into an invisible one. Dropping the day is
lossy and honest; rescaling on inference is lossless and wrong.

For a properly adjusted series, use a corporate-action feed (or yfinance's
adjusted closes, which are already split/bonus adjusted -- data/history is
built that way).
"""

from __future__ import annotations

from typing import Iterable, Optional

import numpy as np
import pandas as pd

# A real single-day equity move beyond this is far more likely a corporate
# action than a price move. Deliberately wide: NSE circuit limits are commonly
# 20%, and genuine 30%+ single-day moves do occur on news.
DROP_BELOW = -0.35
DROP_ABOVE = 0.60

# Common split/bonus ratios: close/prev lands near these when unadjusted.
_RATIOS = (1/2, 1/3, 1/4, 1/5, 1/10, 2/3, 2/5, 3/4, 3/10, 1/20, 1/100)
_RATIO_TOL = 0.03

# Temporary instruments that are not the underlying company.
_NON_EQUITY_SUFFIXES = ("-RE", "-RE1", "-RE2", "-RE3", "-RE4", "-RE5",
                        "-PP", "-BL", "-SG", "-IV", "-W")


def is_tradeable_equity_symbol(symbol: str) -> bool:
    """False for rights entitlements, warrants and other temporary lines.

    These expire worthless by construction, so any study that ranks them will
    'discover' a spectacular decay effect that cannot be traded.
    """
    s = str(symbol).upper().strip()
    if not s:
        return False
    return not any(s.endswith(suf) for suf in _NON_EQUITY_SUFFIXES)


def looks_like_corporate_action(prev_close: float, close: float) -> bool:
    """True when the move is better explained by a split/bonus than by trading."""
    if not prev_close or prev_close <= 0 or close <= 0:
        return False
    ratio = close / prev_close
    ret = ratio - 1.0
    if ret > DROP_ABOVE or ret < DROP_BELOW:
        return True
    # Near-exact split ratios inside the threshold (e.g. a 3:4 at -25%).
    return any(abs(ratio - r) <= _RATIO_TOL * r for r in _RATIOS if r < 0.9)


def clean_returns(close: pd.Series, prev_close: Optional[pd.Series] = None,
                  ) -> pd.Series:
    """Daily returns with suspected corporate-action days set to NaN.

    prev_close, when supplied, is used as the same-row reference (this is how
    bhavcopy is shaped); otherwise the series is lagged.
    """
    close = pd.to_numeric(close, errors="coerce")
    ref = (pd.to_numeric(prev_close, errors="coerce")
           if prev_close is not None else close.shift(1))
    ret = close / ref - 1.0
    ratio = close / ref
    bad = (
        ref.isna() | (ref <= 0) | close.isna() | (close <= 0)
        | (ret > DROP_ABOVE) | (ret < DROP_BELOW)
    )
    for r in _RATIOS:
        if r < 0.9:
            bad |= (ratio - r).abs() <= _RATIO_TOL * r
    return ret.mask(bad)


def flag_panel(close: pd.DataFrame) -> pd.DataFrame:
    """Boolean panel: True where a day looks like a corporate action.

    Panel-shaped input (rows=dates, cols=symbols), lagged internally.
    """
    ratio = close / close.shift(1)
    ret = ratio - 1.0
    bad = (ret > DROP_ABOVE) | (ret < DROP_BELOW)
    for r in _RATIOS:
        if r < 0.9:
            bad = bad | ((ratio - r).abs() <= _RATIO_TOL * r)
    return bad.fillna(False)


def clean_panel_returns(close: pd.DataFrame) -> pd.DataFrame:
    """Panel of daily returns with corporate-action days neutralised."""
    ret = close / close.shift(1) - 1.0
    return ret.mask(flag_panel(close))


def filter_symbols(symbols: Iterable[str]) -> list:
    """Drop rights entitlements / warrants from a symbol list."""
    return [s for s in symbols if is_tradeable_equity_symbol(s)]
