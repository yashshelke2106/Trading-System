"""Tests for core.metrics_writer — the honest-metric layer. Verifies that
option-premium rows are EXCLUDED (the PF~16 mirage fix) and that the window
stats compute on the trustworthy directional pnl only."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.metrics_writer as mw


def test_clean_pnl_prefers_spot():
    assert mw._clean_pnl_pct({"spot_pnl_pct": 5.0, "pnl_pct": -80}) == 5.0


def test_clean_pnl_excludes_option_premium_row():
    # option leg (entry_prem present), no spot label -> NOT trustworthy -> None
    assert mw._clean_pnl_pct({"pnl_pct": 40.0, "entry_prem": 12.0}) is None


def test_clean_pnl_accepts_pure_futures():
    assert mw._clean_pnl_pct({"pnl_pct": 2.0, "instrument": "FUT"}) == 2.0


def test_classify_on_spot_sign():
    assert mw._classify({"spot_pnl_pct": 3.0}) == "WIN"
    assert mw._classify({"spot_pnl_pct": -3.0}) == "LOSS"
    assert mw._classify({"spot_pnl_pct": 0.0}) == "TIMEOUT"


def test_window_stats_excludes_premium_and_computes_pf():
    rows = [
        {"spot_pnl_pct": 2.0, "outcome": "WIN", "exit_ts": "2026-06-01T10:00:00"},
        {"spot_pnl_pct": -1.0, "outcome": "SL", "exit_ts": "2026-06-02T10:00:00"},
        {"pnl_pct": 50.0, "entry_prem": 10, "outcome": "TARGET_HIT",
         "exit_ts": "2026-06-03T10:00:00"},   # premium row -> excluded
    ]
    st = mw._window_stats(rows)
    assert st["clean_n"] == 2
    assert st["excluded_premium"] == 1
    assert abs(st["pf"] - 2.0) < 1e-6           # 2.0 / 1.0


def test_is_junk_replay_failed():
    assert mw._is_junk({"extra": {"replay_failed": True}}) is True
    assert mw._is_junk({"extra": {}}) is False
    assert mw._is_junk({}) is False
