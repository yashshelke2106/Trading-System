"""Tests for the movement-structure layer.

core/day_structure.py  — per-session shape descriptors from 5m bars
core/swing_structure.py — swing pivots, breakout events, forward fills

The one invariant that must never regress: NO LOOKAHEAD. Pivot events are
timestamped at confirmation (pivot + W bars), day-type labels only exist for
completed sessions, breakout levels exclude the trigger bar.

All tests offline.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from core import day_structure as ds
from core import swing_structure as ss


# ── Fixtures ────────────────────────────────────────────────────────────────

def _session(closes, date="2026-07-21", vol=100.0, spread=0.05) -> pd.DataFrame:
    """One 5m session from a close path. Bars start 09:15 IST, 5-min spacing."""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    idx = pd.date_range(f"{date} 09:15", periods=n, freq="5min", tz=ds.IST)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) + spread,
            "low": np.minimum(opens, closes) - spread,
            "close": closes,
            "volume": np.full(n, vol),
        },
        index=idx,
    )


def _daily(closes, spread=0.5) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range(end="2026-07-24", periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "open": closes, "high": closes + spread,
            "low": closes - spread, "close": closes,
            "volume": np.full(len(closes), 1000.0),
        },
        index=idx,
    )


# ── day_structure ───────────────────────────────────────────────────────────

def test_trend_up_day():
    bars = _session(np.linspace(100, 110, ds.FULL_BARS))
    row = ds.compute_session(bars, prev_close=100.0)
    assert row["complete"] is True
    assert row["day_type"] == "trend_up"
    assert row["clv"] >= 0.7 and row["open_loc"] <= 0.3
    assert row["or_break_dir"] == +1
    assert row["or_break_min"] is not None
    # trend day signature: high of day printed near the close
    assert row["hod_min"] > row["lod_min"]


def test_trend_down_day():
    bars = _session(np.linspace(110, 100, ds.FULL_BARS))
    row = ds.compute_session(bars, prev_close=110.0)
    assert row["day_type"] == "trend_down"
    assert row["or_break_dir"] == -1


def test_gap_fade_day():
    # Gap up 2%, then bleed back through more than half the gap.
    closes = np.linspace(102, 100.2, ds.FULL_BARS)
    row = ds.compute_session(_session(closes), prev_close=100.0)
    assert row["gap_pct"] == pytest.approx(2.0, abs=0.01)
    assert row["day_type"] == "gap_fade"


def test_range_day():
    x = np.arange(ds.FULL_BARS)
    closes = 100 + 0.8 * np.sin(x * 2 * np.pi / 15)
    row = ds.compute_session(_session(closes), prev_close=100.0)
    assert row["day_type"] == "range"


def test_locked_session_never_classified():
    """Circuit-locked tape (high == low all day) is labelled, not shaped."""
    bars = _session(np.full(ds.FULL_BARS, 100.0), spread=0.0)
    row = ds.compute_session(bars, prev_close=95.0)
    assert row["day_type"] == "locked"
    assert row["clv"] is None and row["open_loc"] is None


def test_partial_session_flagged_not_classified():
    bars = _session(np.linspace(100, 105, 30))   # only 30 of 75 bars
    row = ds.compute_session(bars, prev_close=100.0)
    assert row["complete"] is False
    assert row["day_type"] == "partial"


def test_vwap_on_flat_tape_is_price():
    bars = _session(np.full(ds.FULL_BARS, 100.0), spread=0.0)
    row = ds.compute_session(bars, prev_close=100.0)
    assert row["vwap"] == pytest.approx(100.0)
    assert row["close_vs_vwap_pct"] == pytest.approx(0.0)


def test_first_session_has_no_gap():
    row = ds.compute_session(_session(np.linspace(100, 104, ds.FULL_BARS)),
                             prev_close=None)
    assert row["gap_pct"] is None and row["ret_cc_pct"] is None


def test_today_ineligible_until_session_closed():
    """Mid-session 'today' must not be eligible for classification."""
    noon = pd.Timestamp("2026-07-24 12:00", tz=ds.IST)
    assert ds._eligible_dates(noon) == noon.normalize()          # today excluded
    late = pd.Timestamp("2026-07-24 15:40", tz=ds.IST)
    assert ds._eligible_dates(late) > late.normalize()           # today included


# ── swing_structure: pivots ─────────────────────────────────────────────────

def test_pivot_confirmation_is_lagged():
    """The lookahead rule: a pivot at i is only knowable at i+W."""
    closes = np.concatenate([np.linspace(100, 110, 11),     # peak at index 10
                             np.linspace(109, 95, 15)])
    df = _daily(closes)
    piv = [p for p in ss.confirmed_pivots(df) if p["kind"] == "high"]
    peak = max(piv, key=lambda p: p["price"])
    assert peak["pivot_date"] == df.index[10]
    assert peak["confirm_date"] == df.index[10 + ss.PIVOT_W]

    # Truncate history to the day BEFORE confirmation: pivot must be invisible.
    early = ss.confirmed_pivots(df.iloc[:10 + ss.PIVOT_W])
    assert all(p["pivot_date"] != df.index[10] for p in early)


def test_breakout_fires_once_per_run():
    closes = np.concatenate([np.full(30, 100.0), np.full(5, 110.0)])
    df = _daily(closes)
    ev = [e for e in ss.detect_events("X", df) if e["event"] == "breakout_20d"]
    assert len(ev) == 1
    assert ev[0]["date"] == str(df.index[30].date())
    # Level excludes the trigger bar: prior 20d high, not today's.
    assert ev[0]["level"] == pytest.approx(100.5)


def test_pivot_event_dated_at_confirmation():
    closes = np.concatenate([np.linspace(100, 110, 11),
                             np.linspace(109, 95, 15)])
    df = _daily(closes)
    ev = [e for e in ss.detect_events("X", df) if e["event"] == "pivot_high"]
    peak = [e for e in ev if e["level"] == pytest.approx(110.5)][0]
    assert peak["date"] == str(df.index[10 + ss.PIVOT_W].date())
    assert peak["pivot_date"] == str(df.index[10].date())


def test_update_is_idempotent(tmp_path, monkeypatch):
    import core.market_state as ms
    closes = np.concatenate([np.full(30, 100.0), np.full(5, 110.0)])
    df = _daily(closes)
    monkeypatch.setattr(ss, "EVENTS_PATH", str(tmp_path / "ev.jsonl"))
    monkeypatch.setattr(ms, "daily_bars", lambda s, **k: df)

    first = ss.update(["X"])
    again = ss.update(["X"])
    assert first["added"] > 0
    assert again["added"] == 0, "second run must add nothing"
    assert again["total"] == first["total"]


def test_analyze_fills_forward_returns(tmp_path, monkeypatch):
    import core.market_state as ms
    closes = np.concatenate([np.full(30, 100.0), np.full(2, 110.0),
                             np.full(10, 121.0)])
    df = _daily(closes)
    monkeypatch.setattr(ss, "EVENTS_PATH", str(tmp_path / "ev.jsonl"))
    monkeypatch.setattr(ms, "daily_bars", lambda s, **k: df)

    ss.update(["X"])
    res = ss.analyze()
    assert res["filled"] > 0

    ev = [e for e in ss._load_events() if e["event"] == "breakout_20d"][0]
    # Event day close 110; 5 bars later close 121 -> +10%.
    assert ev["fwd_5d_pct"] == pytest.approx(10.0, abs=0.01)


def test_swing_state_reports_box_and_distances():
    closes = np.concatenate([np.linspace(90, 100, 40), np.full(20, 100.0)])
    st = ss.swing_state("X", df=_daily(closes, spread=0.4))
    assert st["box"]["in_box"] is True
    assert st["dist_20d_high_pct"] <= 0.5
    assert st["leg"] is not None


def test_recent_sessions_recomputed_despite_existing_file(tmp_path, monkeypatch):
    """A partial session must not freeze: dates in the RECOMPUTE window are
    rebuilt even when their parquet already exists."""
    import core.intraday_capture as ic

    cache = tmp_path / "5m"; out = tmp_path / "ds"
    cache.mkdir(); out.mkdir()
    monkeypatch.setattr(ic, "CACHE_DIR", str(cache))
    monkeypatch.setattr(ds, "OUT_DIR", str(out))

    today = pd.Timestamp.now(tz=ds.IST).normalize()
    day = (today - pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    # First pass: 30-bar partial session on disk.
    _session(np.linspace(100, 105, 30), date=day).to_parquet(cache / "T.parquet")
    ds.build()
    f = out / f"{pd.Timestamp(day).strftime('%Y%m%d')}.parquet"
    assert pd.read_parquet(f)["day_type"].iloc[0] == "partial"

    # Bars complete later; a plain build() (no rebuild flag) must fix the row.
    _session(np.linspace(100, 110, ds.FULL_BARS), date=day).to_parquet(cache / "T.parquet")
    ds.build()
    assert pd.read_parquet(f)["day_type"].iloc[0] == "trend_up"


def test_analyze_handles_tz_aware_bars(tmp_path, monkeypatch):
    """Regression: yfinance-sourced daily frames are tz-aware IST and must
    not crash the forward-fill (bhavcopy frames are naive)."""
    import core.market_state as ms
    closes = np.concatenate([np.full(30, 100.0), np.full(2, 110.0),
                             np.full(10, 121.0)])
    df = _daily(closes)
    df.index = df.index.tz_localize("Asia/Kolkata")
    monkeypatch.setattr(ss, "EVENTS_PATH", str(tmp_path / "ev.jsonl"))
    monkeypatch.setattr(ms, "daily_bars", lambda s, **k: df)

    ss.update(["X"])
    res = ss.analyze()
    assert res["filled"] > 0
    ev = [e for e in ss._load_events() if e["event"] == "breakout_20d"][0]
    assert ev["fwd_5d_pct"] == pytest.approx(10.0, abs=0.01)
