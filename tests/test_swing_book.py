"""Tests for core/swing_book.py — the 50/50 equity + options swing book.

What must hold for this to be safe to act on:
  - the options sleeve REFUSES to propose a position it cannot fund;
  - concentration is computed and surfaced, never hidden;
  - the narrowest fundable wing is chosen (a wider one means no position);
  - capital scales the ladder correctly;
  - the plan always carries the "sell premium, don't buy it" warning when the
    options sleeve cannot be funded.

Offline — the options sleeve is pure arithmetic and needs no network.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core import swing_book as sb


# ── options sleeve sizing ───────────────────────────────────────────────────

def test_ladder_max_loss_matches_formula():
    """max loss = (wing - credit) * lot, credit = CREDIT_FRAC * wing."""
    o = sb.plan_options(100_000)
    for row in o["ladder"]:
        w = row["wing_points"]
        expect = (w - w * sb.CREDIT_FRAC) * sb.NIFTY_LOT
        assert row["max_loss_per_lot"] == pytest.approx(expect, abs=0.01)


def test_max_loss_is_monotone_in_wing_width():
    o = sb.plan_options(100_000)
    losses = [r["max_loss_per_lot"] for r in o["ladder"]]
    assert losses == sorted(losses), "wider wings must risk more, not less"


def test_narrowest_fundable_wing_is_chosen():
    """A wider wing on a small sleeve means NO position — pick the narrowest."""
    # Rs30k book -> Rs15k options sleeve. 100pt risks 5250, 200pt risks 10500.
    o = sb.plan_options(30_000)
    assert o["fundable"]
    assert o["chosen"]["wing_points"] == min(
        r["wing_points"] for r in o["ladder"] if r["lots_affordable"] >= 1)


def test_unfundable_sleeve_refuses_and_says_what_is_needed():
    o = sb.plan_options(2_000)          # Rs1,000 options sleeve — far too small
    assert o["fundable"] is False
    assert "reason" in o
    assert o["capital_needed_for_one_lot"] > 2_000


def test_concentration_is_reported():
    o = sb.plan_options(15_000)          # Rs7,500 sleeve, one 100pt condor
    assert o["fundable"]
    assert 0 < o["sleeve_concentration"] <= 1.0
    # 5250 / 7500 = 0.70
    assert o["sleeve_concentration"] == pytest.approx(0.70, abs=0.01)


def test_single_lot_triggers_lumpiness_warning():
    o = sb.plan_options(15_000)
    assert o["lots"] == 1
    assert any("ONE lot" in w for w in o["warnings"])


def test_larger_capital_affords_more_lots():
    small = sb.plan_options(15_000)["lots"]
    big = sb.plan_options(150_000)["lots"]
    assert big > small


def test_lot_size_drives_affordability():
    """A smaller lot (e.g. a different index) should make more structures fit."""
    big_lot = sb.plan_options(15_000, lot=75)
    small_lot = sb.plan_options(15_000, lot=15)
    n_big = sum(1 for r in big_lot["ladder"] if r["lots_affordable"] >= 1)
    n_small = sum(1 for r in small_lot["ladder"] if r["lots_affordable"] >= 1)
    assert n_small > n_big


# ── book assembly ───────────────────────────────────────────────────────────

def test_sleeve_fractions_sum_to_one():
    assert sb.EQUITY_FRAC + sb.OPTIONS_FRAC == pytest.approx(1.0)


def test_unfundable_options_warns_against_buying_instead(monkeypatch):
    """The failure mode to prevent: filling the gap with BOUGHT options,
    which this system measured at PF 0.44."""
    monkeypatch.setattr(sb, "plan_equity",
                        lambda cap: {"target_frac": 0.5, "sleeve_capital": cap * 0.5,
                                     "fundable": True, "units": 1, "price": 100.0,
                                     "deployed": 100.0, "cash_in_sleeve": 0.0,
                                     "overlay_state": "core_only",
                                     "overlay_equity_weight": 1.0})
    p = sb.plan(2_000)
    assert any("NOT FUNDABLE" in w for w in p.warnings_)
    assert any("BOUGHT options" in w for w in p.warnings_)


def test_plan_serialises(monkeypatch):
    monkeypatch.setattr(sb, "plan_equity",
                        lambda cap: {"target_frac": 0.5, "sleeve_capital": cap * 0.5,
                                     "fundable": True, "units": 10, "price": 275.0,
                                     "deployed": 2750.0, "cash_in_sleeve": 4750.0,
                                     "overlay_state": "risk_off",
                                     "overlay_equity_weight": 0.5})
    d = sb.plan(15_000).to_dict()
    for k in ("capital", "asof", "equity", "options", "warnings", "note"):
        assert k in d
    import json
    json.dumps(d, default=str)          # must be serialisable


def test_note_states_the_options_edge_is_unproven(monkeypatch):
    monkeypatch.setattr(sb, "plan_equity",
                        lambda cap: {"target_frac": 0.5, "sleeve_capital": cap * 0.5,
                                     "fundable": True, "units": 10, "price": 275.0,
                                     "deployed": 2750.0, "cash_in_sleeve": 4750.0,
                                     "overlay_state": "risk_off",
                                     "overlay_equity_weight": 0.5})
    note = sb.plan(15_000).note
    assert "unproven" in note.lower()
    assert "H-014" in note
