"""Tests for core/ohlcv_fallback.py — the scanner's missing data tier.

The bug this fixes: Dhan 401 -> empty frame -> scanner sees no bars -> zero
trades for every symbol. What must hold:
  - output matches the Dhan column contract exactly (callers unchanged);
  - never raises — a data-source failure returns an empty frame, not a crash;
  - 15m is a correct resample of 5m (OHLC aggregation, volume summed);
  - the corporate-action ticker map is honoured.

Network is monkeypatched — offline, deterministic.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from core import ohlcv_fallback as of

_DHAN_COLS = ["date", "open", "high", "low", "close", "volume"]


def _fake_yf_frame(n=60, freq="D", start="2026-05-01"):
    idx = pd.date_range(start, periods=n, freq=freq, tz="Asia/Kolkata")
    base = 100 + np.arange(n) * 0.5
    return pd.DataFrame({"Open": base, "High": base + 1, "Low": base - 1,
                         "Close": base + 0.5, "Volume": np.full(n, 1000)}, index=idx)


class _FakeTicker:
    def __init__(self, frame):
        self._f = frame
    def history(self, period, interval):
        return self._f


def _install(monkeypatch, frame):
    import types
    fake = types.SimpleNamespace(Ticker=lambda t: _FakeTicker(frame))
    monkeypatch.setitem(sys.modules, "yfinance", fake)


def test_daily_matches_dhan_shape(monkeypatch):
    _install(monkeypatch, _fake_yf_frame(60, "D"))
    df = of.daily("RELIANCE", days_back=60)
    assert list(df.columns) == _DHAN_COLS
    assert len(df) == 60
    assert str(df["date"].dtype).startswith("datetime64")
    # tz stripped
    assert df["date"].dt.tz is None


def test_daily_respects_days_back(monkeypatch):
    _install(monkeypatch, _fake_yf_frame(120, "D"))
    df = of.daily("INFY", days_back=30)
    assert len(df) == 30


def test_intraday_5m_shape(monkeypatch):
    _install(monkeypatch, _fake_yf_frame(200, "5min"))
    df = of.intraday("RELIANCE", interval=5, days_back=5)
    assert list(df.columns) == _DHAN_COLS
    assert len(df) == 200


def test_intraday_15m_is_correct_resample(monkeypatch):
    _install(monkeypatch, _fake_yf_frame(30, "5min"))
    df5 = of.intraday("RELIANCE", interval=5, days_back=5)
    df = of.intraday("RELIANCE", interval=15, days_back=5)
    assert list(df.columns) == _DHAN_COLS
    # ~1/3 as many bars (exact count depends on right-closed boundary).
    assert 9 <= len(df) <= 11
    # aggregation must conserve total volume (summed, never averaged).
    assert df["volume"].sum() == pytest.approx(df5["volume"].sum())
    # a full 3-bar bucket sums to 3000; find one and check.
    assert (df["volume"] == 3000).any()
    # high of a bucket >= any of its constituents' opens (max aggregation).
    assert (df["high"] >= df["open"]).all()


def test_empty_source_returns_empty_frame_not_crash(monkeypatch):
    _install(monkeypatch, pd.DataFrame())        # yfinance returns nothing
    d = of.daily("X"); i = of.intraday("X", 5)
    assert list(d.columns) == _DHAN_COLS and d.empty
    assert list(i.columns) == _DHAN_COLS and i.empty


def test_never_raises_on_source_exception(monkeypatch):
    import types
    def boom(t):
        raise RuntimeError("network down")
    monkeypatch.setitem(sys.modules, "yfinance",
                        types.SimpleNamespace(Ticker=boom))
    assert of.daily("X").empty
    assert of.intraday("X", 5).empty


def test_ticker_map_honoured():
    # TATAMOTORS must resolve to the post-demerger entity.
    assert of._yf_ticker("TATAMOTORS") == "TMCV.NS"
    assert of._yf_ticker("RELIANCE") == "RELIANCE.NS"
