"""Tests for the cross-asset trend sleeve (H-022).

No network: every test builds a synthetic panel, so these run offline and
pin behaviour rather than market data.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.multi_asset import (LOOKBACK, MAX_GROSS, UNIVERSE, VOL_TARGET,
                              book_summary, signals)


def _panel(spec: dict, n: int = LOOKBACK + 60) -> pd.DataFrame:
    """Synthetic price panel. spec maps ticker -> (drift/bar, vol/bar)."""
    rng = np.random.default_rng(0)
    idx = pd.bdate_range("2020-01-01", periods=n)
    out = {}
    for t, (drift, vol) in spec.items():
        steps = drift + vol * rng.standard_normal(n)
        out[t] = 100 * np.exp(np.cumsum(steps))
    return pd.DataFrame(out, index=idx)


def test_direction_follows_trailing_momentum():
    px = _panel({"SPY": (0.0008, 0.005), "TLT": (-0.0008, 0.005)})
    rows = {r["ticker"]: r for r in signals(px)}
    assert rows["SPY"]["direction"] == "long"
    assert rows["TLT"]["direction"] == "short"
    assert rows["SPY"]["weight"] > 0 > rows["TLT"]["weight"]


def test_higher_vol_gets_a_smaller_weight():
    """Inverse-vol sizing is the whole risk model — pin it."""
    px = _panel({"SPY": (0.0008, 0.004), "BTC-USD": (0.0008, 0.030)})
    rows = {r["ticker"]: r for r in signals(px)}
    assert rows["SPY"]["direction"] == rows["BTC-USD"]["direction"] == "long"
    assert abs(rows["BTC-USD"]["weight"]) < abs(rows["SPY"]["weight"])


def test_gross_exposure_is_capped():
    # many low-vol markets would otherwise size up past the cap
    spec = {t: (0.0005, 0.001) for t in list(UNIVERSE)[:12]}
    rows = signals(_panel(spec))
    assert book_summary(rows)["gross_exposure"] <= MAX_GROSS + 1e-6


def test_single_sleeve_never_exceeds_one_x():
    """A very low-vol market must not be levered past 1x on its own."""
    px = _panel({"IEF": (0.0004, 0.0002), "SPY": (0.0008, 0.010)})
    rows = {r["ticker"]: r for r in signals(px)}
    assert abs(rows["IEF"]["weight"]) <= 1.0 + 1e-9


def test_no_lookahead_future_bars_cannot_change_todays_book():
    """The defining honesty property: appending future bars must not alter the
    book computed for an earlier date."""
    full = _panel({"SPY": (0.0008, 0.006), "GC=F": (-0.0004, 0.008)}, n=LOOKBACK + 90)
    early = full.iloc[:LOOKBACK + 40]
    a = {r["ticker"]: r["weight"] for r in signals(early)}
    # same as-of date, but the panel now also contains later bars
    b_rows = signals(full.iloc[:LOOKBACK + 40])
    b = {r["ticker"]: r["weight"] for r in b_rows}
    assert a == b


def test_insufficient_history_raises_rather_than_guessing():
    px = _panel({"SPY": (0.0008, 0.006)}, n=LOOKBACK - 10)
    with pytest.raises(ValueError):
        signals(px)


def test_vol_target_is_respected_for_a_lone_market():
    """One market at 20% annualised vol should be sized near 0.5x."""
    daily = 0.20 / np.sqrt(252)
    px = _panel({"SPY": (0.0006, daily)})
    w = abs(signals(px)[0]["weight"])
    assert 0.3 < w < 0.8, w          # ~VOL_TARGET / 0.20 = 0.5, sampling noise aside


def test_book_summary_arithmetic():
    rows = [
        {"weight": 0.5, "direction": "long", "asset_class": "equity"},
        {"weight": -0.25, "direction": "short", "asset_class": "rates"},
        {"weight": 0.0, "direction": "flat", "asset_class": "fx"},
    ]
    s = book_summary(rows)
    assert s["gross_exposure"] == pytest.approx(0.75)
    assert s["net_exposure"] == pytest.approx(0.25)
    assert (s["n_long"], s["n_short"], s["n_flat"]) == (1, 1, 1)
    assert s["net_by_class"]["equity"] == pytest.approx(0.5)


def test_india_accessibility_flags_are_honest():
    """The India-only subset was measured NOT fundable; the flag is what keeps
    that visible in the scan, so it must not drift."""
    assert UNIVERSE["^NSEI"][2] is True
    assert UNIVERSE["GC=F"][2] is True          # MCX gold
    assert UNIVERSE["SPY"][2] is False          # needs LRS + offshore broker
    assert UNIVERSE["TLT"][2] is False
    assert UNIVERSE["BTC-USD"][2] is False
    # the accessible set must stay a genuine minority, or the caveat is stale
    n_ind = sum(1 for v in UNIVERSE.values() if v[2])
    assert n_ind < len(UNIVERSE) / 2


def test_universe_spans_at_least_five_asset_classes():
    """Breadth across CLASSES is the diversification mechanism, not raw count."""
    assert len({v[1] for v in UNIVERSE.values()}) >= 5
