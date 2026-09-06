"""Tests for the paper macro fund ledger (H-022).

The invariant is the point of this module, so most of these exist to make it
impossible to quietly break: a P&L number from a leaking ledger is worthless.
No network — every test drives the book with hand-built prices.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import macro_fund as mf


def _targets():
    return [
        {"ticker": "SPY", "name": "EQ US", "asset_class": "equity", "weight": 0.5},
        {"ticker": "TLT", "name": "RATES US20y+", "asset_class": "rates", "weight": -0.3},
        {"ticker": "GLD", "name": "CMDY Gold", "asset_class": "commodity", "weight": 0.2},
    ]


def _prices(spy=100.0, tlt=100.0, gld=100.0):
    return {"SPY": spy, "TLT": tlt, "GLD": gld}


def _book(capital=100_000.0):
    """A fresh in-memory book — never touches logs/."""
    return mf._default_state(capital)


# ── the invariant ────────────────────────────────────────────────────────
def test_invariant_holds_on_a_fresh_book():
    mf.check_invariant(_book())


def test_invariant_holds_through_rebalance_and_marks():
    s = _book()
    s = mf.rebalance(s, _targets(), _prices())
    mf.check_invariant(s)
    s = mf.mark(s, _prices(spy=110, tlt=95, gld=105))
    mf.check_invariant(s)
    s = mf.rebalance(s, _targets(), _prices(spy=110, tlt=95, gld=105))
    mf.check_invariant(s)


def test_invariant_raises_when_nav_is_tampered_with():
    """It must RAISE, not warn — the whole reason it exists."""
    s = _book()
    s = mf.rebalance(s, _targets(), _prices())
    s["nav"] += 5_000.0
    with pytest.raises(AssertionError):
        mf.check_invariant(s)


def test_save_state_refuses_a_broken_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(mf, "STATE_PATH", str(tmp_path / "s.json"))
    s = _book()
    s["realised_pnl"] = 1234.0          # nav not updated to match
    with pytest.raises(AssertionError):
        mf.save_state(s)
    assert not os.path.exists(mf.STATE_PATH), "a broken book must not be persisted"


# ── P&L arithmetic ───────────────────────────────────────────────────────
def test_long_position_gains_when_price_rises():
    s = mf.rebalance(_book(), _targets(), _prices())
    s = mf.mark(s, _prices(spy=110))
    spy = next(p for p in s["positions"] if p["ticker"] == "SPY")
    # 0.5 * ~100k / 100 = ~500 units, +10 each
    assert spy["pnl"] == pytest.approx(4985.0, rel=0.02)


def test_short_position_gains_when_price_falls():
    s = mf.rebalance(_book(), _targets(), _prices())
    s = mf.mark(s, _prices(tlt=90))
    tlt = next(p for p in s["positions"] if p["ticker"] == "TLT")
    assert tlt["units"] < 0
    assert tlt["pnl"] > 0


def test_short_position_loses_when_price_rises():
    s = mf.rebalance(_book(), _targets(), _prices())
    s = mf.mark(s, _prices(tlt=110))
    tlt = next(p for p in s["positions"] if p["ticker"] == "TLT")
    assert tlt["pnl"] < 0


def test_costs_are_charged_on_turnover_both_ways():
    """Entry-only: gross 1.0x of 100k = 100k notional at 10bp = 100."""
    s = mf.rebalance(_book(), _targets(), _prices())
    assert s["total_costs"] == pytest.approx(100.0, rel=0.01)
    assert s["nav"] == pytest.approx(99_900.0, rel=0.001)


def test_second_rebalance_pays_to_close_and_to_open():
    s = mf.rebalance(_book(), _targets(), _prices())
    first = s["total_costs"]
    s = mf.rebalance(s, _targets(), _prices())
    # closes ~100k and opens ~100k => roughly double the one-way cost
    assert s["total_costs"] - first == pytest.approx(2 * first, rel=0.05)


def test_unrealised_becomes_realised_on_rebalance():
    s = mf.rebalance(_book(), _targets(), _prices())
    s = mf.mark(s, _prices(spy=110))
    unreal = s["unrealised_pnl"]
    assert unreal > 0
    s = mf.rebalance(s, _targets(), _prices(spy=110))
    assert s["unrealised_pnl"] == pytest.approx(0.0, abs=0.01)
    assert s["realised_pnl"] > unreal - 500      # net of the switch cost


def test_a_stale_price_keeps_the_last_mark_instead_of_zeroing():
    """A missing quote must not be read as a price of zero."""
    s = mf.rebalance(_book(), _targets(), _prices())
    s = mf.mark(s, _prices(spy=110))
    spy_before = next(p for p in s["positions"] if p["ticker"] == "SPY")["pnl"]
    s = mf.mark(s, {"TLT": 100.0, "GLD": 100.0})     # SPY absent
    spy_after = next(p for p in s["positions"] if p["ticker"] == "SPY")["pnl"]
    assert spy_after == spy_before
    mf.check_invariant(s)


# ── exposure ─────────────────────────────────────────────────────────────
def test_exposure_reports_gross_net_and_class():
    s = mf.rebalance(_book(), _targets(), _prices())
    e = mf.exposure(s)
    assert e["gross"] == pytest.approx(1.0, rel=0.02)
    assert e["net"] == pytest.approx(0.4, rel=0.05)
    assert e["n_long"] == 2 and e["n_short"] == 1
    assert e["by_class"]["rates"] < 0 < e["by_class"]["equity"]


def test_gross_exposure_above_one_is_allowed():
    """A notional book is not a cash book — 3x gross is the design."""
    big = [{**t, "weight": t["weight"] * 3} for t in _targets()]
    s = mf.rebalance(_book(), big, _prices())
    assert mf.exposure(s)["gross"] > 2.5
    mf.check_invariant(s)


# ── rebalance scheduling ─────────────────────────────────────────────────
def test_rebalance_is_due_on_a_fresh_book():
    assert mf.is_rebalance_day(_book()) is True


def test_rebalance_not_due_twice_in_one_month():
    from datetime import date
    s = _book()
    s["last_rebalance"] = str(date(2026, 9, 3))
    assert mf.is_rebalance_day(s, today=date(2026, 9, 20)) is False
    assert mf.is_rebalance_day(s, today=date(2026, 10, 1)) is True


def test_corrupt_last_rebalance_fails_closed():
    """Must RAISE, not default to 'due'. Defaulting to due would rebalance on
    every cycle and pay turnover cost each time — a leak that looks like
    normal operation. Caught by the repo's own fail-open audit."""
    s = _book()
    s["last_rebalance"] = "not-a-date"
    with pytest.raises(ValueError, match="corrupt"):
        mf.is_rebalance_day(s)


# ── reporting ────────────────────────────────────────────────────────────
def test_performance_is_not_marked_mature_before_the_registered_window():
    """H-022 evaluates no earlier than 2027-09-05. A book read before that must
    not present itself as a verdict."""
    p = mf.performance(_book())
    assert p["mature"] is False
    assert p["evaluate_after"] == "2027-09-05"


def test_performance_drawdown_tracks_the_curve():
    s = _book()
    s["curve"] = [{"date": "2026-09-01", "nav": 100_000.0},
                  {"date": "2026-09-02", "nav": 110_000.0},
                  {"date": "2026-09-03", "nav": 99_000.0}]
    p = mf.performance(s)
    assert p["max_drawdown_pct"] == pytest.approx(-10.0, rel=0.01)


def test_record_curve_replaces_same_day_point():
    s = _book()
    s = mf.record_curve(s)
    s = mf.record_curve(s)
    today = str(mf._today())
    assert sum(1 for c in s["curve"] if c["date"] == today) == 1
