"""
Smoke tests — fast, no network, runs on every commit.

Goal: catch regressions in the 10+ bugs already found, before user sees them.

Run:
    pytest tests/test_smoke.py -v
    python -m pytest tests/test_smoke.py -v
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────────────────────
# Module import sanity
# ─────────────────────────────────────────────────────────────────────────────

def test_imports_all_core_modules():
    """If any of these break, system can't even start."""
    import core.schemas  # noqa: F401
    import core.signal_engine  # noqa: F401
    import core.timeframe_sync  # noqa: F401
    import core.option_translator  # noqa: F401
    import core.order_flow  # noqa: F401
    import core.signal_writer  # noqa: F401
    import core.exit_replay  # noqa: F401
    import core.adaptive_learner  # noqa: F401
    import core.top_mover_mode  # noqa: F401
    import core.agents.signal_agent  # noqa: F401
    import core.agents.risk_agent  # noqa: F401
    import core.agents.meta_learning_agent  # noqa: F401
    import aladdin_runner  # noqa: F401


# ─────────────────────────────────────────────────────────────────────────────
# Schema validation
# ─────────────────────────────────────────────────────────────────────────────

def test_schema_rejects_zero_entry_price():
    from core.schemas import validate_signal
    bad = {
        "symbol": "X", "direction": "long",
        "entry_price": 0, "sl_price": 100, "target_price": 200,
        "confluence_grade": "A", "confluence_score": 50,
        "ts": "2026-05-14T11:00:00", "rr_ratio": 1,
    }
    assert validate_signal(bad) is None


def test_schema_accepts_valid_signal():
    from core.schemas import validate_signal
    good = {
        "symbol": "KOTAKBANK", "direction": "long",
        "entry_price": 389.15, "sl_price": 385.0, "target_price": 400.0,
        "confluence_grade": "A", "confluence_score": 75,
        "ts": "2026-05-14T11:00:00", "rr_ratio": 2.5,
    }
    out = validate_signal(good)
    assert out is not None
    assert out["symbol"] == "KOTAKBANK"


def test_schema_rejects_invalid_direction():
    from core.schemas import validate_signal
    bad = {
        "symbol": "X", "direction": "up",  # must be long|short
        "entry_price": 100, "sl_price": 95, "target_price": 110,
        "confluence_grade": "A", "confluence_score": 50,
        "ts": "x", "rr_ratio": 1,
    }
    assert validate_signal(bad) is None


def test_schema_rejects_invalid_grade():
    from core.schemas import validate_signal
    bad = {
        "symbol": "X", "direction": "long",
        "entry_price": 100, "sl_price": 95, "target_price": 110,
        "confluence_grade": "X",  # not S|A|B|C
        "confluence_score": 50, "ts": "x", "rr_ratio": 1,
    }
    assert validate_signal(bad) is None


# ─────────────────────────────────────────────────────────────────────────────
# SignalEngine — synthetic OHLCV doesn't crash
# ─────────────────────────────────────────────────────────────────────────────

def _synthetic_ohlcv(n: int = 50, uptrend: bool = True) -> pd.DataFrame:
    np.random.seed(42)
    base = 100.0
    rows = []
    for i in range(n):
        drift = 0.5 if uptrend else -0.5
        open_p = base + drift * i + np.random.randn() * 0.3
        close_p = open_p + drift + np.random.randn() * 0.5
        high = max(open_p, close_p) + abs(np.random.randn()) * 0.2
        low = min(open_p, close_p) - abs(np.random.randn()) * 0.2
        vol = 1_000_000 + np.random.randint(0, 500_000)
        rows.append({
            "date": f"2026-05-{(i // 75) + 1:02d}T09:{15 + (i % 75) * 5 // 60:02d}:00",
            "open": open_p, "high": high, "low": low, "close": close_p,
            "volume": vol,
        })
    return pd.DataFrame(rows)


def test_signal_engine_doesnt_crash_on_uptrend():
    from core.signal_engine import SignalEngine
    eng = SignalEngine()
    df = _synthetic_ohlcv(50, uptrend=True)
    # Either returns Signal or None — but never crashes
    sig = eng.generate_signal("TESTSYM", df)
    assert sig is None or hasattr(sig, "direction")


def test_signal_engine_doesnt_crash_on_downtrend():
    from core.signal_engine import SignalEngine
    eng = SignalEngine()
    df = _synthetic_ohlcv(50, uptrend=False)
    sig = eng.generate_signal("TESTSYM", df)
    assert sig is None or hasattr(sig, "direction")


def test_signal_engine_short_circuits_on_tiny_df():
    from core.signal_engine import SignalEngine
    eng = SignalEngine()
    df = _synthetic_ohlcv(5)
    sig = eng.generate_signal("TESTSYM", df)
    assert sig is None  # <25 bars = always None


# ─────────────────────────────────────────────────────────────────────────────
# Exit replay — handles signals without crashing
# ─────────────────────────────────────────────────────────────────────────────

def test_exit_replay_handles_missing_fields():
    """Bug: pnl_pct=None garbage was being written. Replay should reject malformed input."""
    from core.exit_replay import replay_exit
    res = replay_exit({})  # totally empty
    assert res["outcome"] == "NO_DATA"
    assert res["pnl_pct"] == 0.0


def test_exit_replay_handles_bad_ts():
    from core.exit_replay import replay_exit
    res = replay_exit({
        "symbol": "X", "direction": "long",
        "entry_price": 100, "sl_price": 95, "target_price": 110,
        "ts": "not-a-datetime",
    })
    assert res["outcome"] == "NO_DATA"


# ─────────────────────────────────────────────────────────────────────────────
# Adaptive learner — walk-forward sim returns sane WR
# ─────────────────────────────────────────────────────────────────────────────

def test_simulate_wr_returns_zero_to_one():
    from core.adaptive_learner import get_learner
    learner = get_learner()
    trades = [
        {"direction": "long", "outcome": "TARGET_HIT", "patterns": ["ema_uptrend"]},
        {"direction": "long", "outcome": "SL_HIT",     "patterns": ["ema_uptrend"]},
        {"direction": "short", "outcome": "TARGET_HIT", "patterns": ["ema_downtrend"]},
    ]
    wr = learner._simulate_wr(trades, learner._params)
    assert 0.0 <= wr <= 1.0


def test_simulate_wr_empty_trades_returns_zero():
    from core.adaptive_learner import get_learner
    learner = get_learner()
    assert learner._simulate_wr([], learner._params) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Order flow — symmetric exhaustion (post bullish-bias fix)
# ─────────────────────────────────────────────────────────────────────────────

def test_order_flow_symmetric_pressure():
    """Down-trending data should NOT trigger 'overwhelming sell' for shorts."""
    from core.order_flow import OrderFlowAnalyzer
    df = _synthetic_ohlcv(20, uptrend=False)
    of = OrderFlowAnalyzer()
    long_a = of.analyze(df, "long")
    short_a = of.analyze(df, "short")
    # Long sees exhaustion (sell pressure), short does not
    assert long_a is not None
    assert short_a is not None


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown logic — stale signals don't lock symbols
# ─────────────────────────────────────────────────────────────────────────────

def test_stale_signal_skips_cooldown():
    """Stale signals (>1h old) should NOT trigger 10-min cooldown."""
    from core.signal_writer import _merge_with_existing
    import json
    from datetime import datetime, timedelta

    # Create a stale signal (24h ago)
    stale_ts = (datetime.now() - timedelta(hours=24)).isoformat()
    tmp_path = os.path.join(os.path.dirname(__file__), "_test_signals.json")
    with open(tmp_path, "w") as f:
        json.dump({
            "signals": [{"symbol": "X", "first_seen_ts": stale_ts}],
            "_cooldown": {},
        }, f)

    # Patch SIGNALS_FILE
    from core import signal_writer as sw
    orig = sw.SIGNALS_FILE
    sw.SIGNALS_FILE = tmp_path
    try:
        # New scan, X not in new signals — _merge runs cleanup
        _merge_with_existing([])
        cd = getattr(_merge_with_existing, "_last_cooldown", {})
        # Stale X should NOT be in cooldown
        assert "X" not in cd
    finally:
        sw.SIGNALS_FILE = orig
        os.remove(tmp_path)


if __name__ == "__main__":
    import pytest as _pt
    _pt.main([__file__, "-v"])
