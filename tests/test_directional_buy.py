"""Tests for core/directional_buy.py — mechanics spec + requirement gate.

What must hold:
  - the gate BLOCKS on the measured (failing) inputs, and would ALLOW if the
    edge inputs were genuinely met — so it is a real test, not a rubber stamp;
  - strike selection lands near the target delta on the real strike grid;
  - a deeper-ITM strike has higher delta (sanity on the pricer);
  - the plan's stop/target/time-stop follow the spec exactly.
Offline — live data monkeypatched.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core import directional_buy as db


# ── the gate ────────────────────────────────────────────────────────────────

def test_gate_blocks_on_measured_reality():
    g = db.check_requirements()          # defaults = this system's measurements
    assert g.allowed is False
    assert "BLOCKED" in g.summary
    failed = [r.name for r in g.requirements if not r.passed]
    assert any("accuracy" in f.lower() for f in failed)
    assert any("winning moves" in f.lower() for f in failed)


def test_gate_is_not_a_rubber_stamp():
    """If the edge inputs were genuinely met, the substantive requirements
    must PASS — otherwise the gate proves nothing."""
    g = db.check_requirements(accuracy=0.58, mfe_p90=4.0, iv_percentile=20)
    subst = [r for r in g.requirements
             if "accuracy" in r.name.lower() or "winning moves" in r.name.lower()]
    assert all(r.passed for r in subst), "gate must pass when edge is real"


def test_low_accuracy_alone_fails():
    g = db.check_requirements(accuracy=0.20, mfe_p90=5.0)
    assert not g.allowed


def test_small_moves_alone_fails():
    g = db.check_requirements(accuracy=0.60, mfe_p90=0.5)
    assert not g.allowed
    moves = [r for r in g.requirements if "winning moves" in r.name.lower()][0]
    assert not moves.passed


def test_high_iv_fails_the_iv_requirement():
    g = db.check_requirements(iv_percentile=85)
    iv = [r for r in g.requirements if r.name.lower().startswith("enter at low iv")][0]
    assert not iv.passed


# ── mechanics ───────────────────────────────────────────────────────────────

def test_strike_lands_near_target_delta():
    S, iv, dte = 24000.0, 0.12, 35
    K = db.strike_for_delta(S, iv, dte, db.TARGET_DELTA, "up")
    d = db._delta_call(S, K, dte / 365.0, iv)
    assert abs(d - db.TARGET_DELTA) < 0.08
    assert K < S, "delta 0.65 call must be ITM (strike below spot)"


def test_strike_on_listed_grid():
    K = db.strike_for_delta(24000.0, 0.12, 35, 0.65, "up")
    assert K % 50 == 0, "must land on a tradeable 50-point NIFTY strike"


def test_deeper_itm_has_higher_delta():
    S, iv, T = 24000.0, 0.12, 35 / 365.0
    assert db._delta_call(S, 23000, T, iv) > db._delta_call(S, 24000, T, iv)
    assert db._delta_call(S, 24000, T, iv) > db._delta_call(S, 25000, T, iv)


def test_put_view_picks_strike_above_spot():
    K = db.strike_for_delta(24000.0, 0.12, 35, db.TARGET_DELTA, "down")
    assert K > 24000.0, "delta-0.65 put must be ITM (strike above spot)"


def test_plan_follows_spec(monkeypatch):
    monkeypatch.setattr(db, "_live_spot_vix", lambda: (24000.0, 12.0))
    p = db.build_plan("NIFTY", "up", dte=35)
    assert p is not None
    assert db.DTE_MIN <= p.dte <= db.DTE_MAX
    assert p.exit_by_dte == db.EXIT_DTE
    assert p.stop_premium == pytest.approx(p.premium * 0.5, rel=1e-3)
    assert p.target_premium == pytest.approx(p.premium * 4.0, rel=1e-3)  # +3R
    assert p.option_type == "CALL"


def test_dte_is_clamped_away_from_theta_cliff(monkeypatch):
    monkeypatch.setattr(db, "_live_spot_vix", lambda: (24000.0, 12.0))
    assert db.build_plan("NIFTY", "up", dte=7).dte == db.DTE_MIN
    assert db.build_plan("NIFTY", "up", dte=90).dte == db.DTE_MAX


def test_plan_none_when_no_live_data(monkeypatch):
    monkeypatch.setattr(db, "_live_spot_vix", lambda: None)
    assert db.build_plan("NIFTY", "up") is None
