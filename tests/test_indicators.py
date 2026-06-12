"""Basic correctness of core.signal_engine indicators (RSI/ATR/EMA)."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from core.signal_engine import SignalEngine


def _df(closes):
    return pd.DataFrame({
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": closes,
        "volume": [1000] * len(closes),
    })


def test_rsi_in_bounds_and_high_on_uptrend():
    e = SignalEngine()
    rsi = e.calculate_rsi(_df(list(range(100, 140))))
    assert 0.0 <= rsi <= 100.0 and rsi > 60


def test_rsi_low_on_downtrend():
    e = SignalEngine()
    rsi = e.calculate_rsi(_df(list(range(140, 100, -1))))
    assert rsi < 40


def test_atr_non_negative():
    e = SignalEngine()
    atr = e.calculate_atr(_df([100 + (i % 5) for i in range(40)]))
    assert atr >= 0


def test_ema_length_and_value():
    e = SignalEngine()
    df = _df(list(range(100, 140)))
    ema = e.calculate_ema(df, 9)
    assert len(ema) == len(df) and ema.iloc[-1] > 0
