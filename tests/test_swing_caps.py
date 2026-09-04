"""Regression tests for the 2026-09-04 swing repair.

Each test pins one of the three defects found in the 2026-08-31 post-mortem,
so a future edit that reintroduces one fails loudly rather than quietly
costing money.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import swing_screen as S
from core.swing_learner import SwingLearner, _wilson
from core.symbol_groups import group_of, groups_present


# ── geometry ─────────────────────────────────────────────────────────────
def test_stop_is_wider_than_target():
    """The defect: target and stop were both 2xATR, giving R:R 1.00 on every
    row, while exit_style C disabled the target and kept the stop."""
    assert S.STOP_ATR_MULT > S.TARGET_ATR_MULT
    assert S.STOP_ATR_MULT == 4.0


def test_screen_emits_asymmetric_geometry():
    """A long candidate must sit closer to its target than to its stop."""
    import numpy as np
    import pandas as pd

    n = 400
    idx = pd.bdate_range("2024-01-01", periods=n)
    # gently rising series so the 200-DMA slopes up, with a 3-day dip at the
    # end to trip the pullback setup
    close = np.linspace(100, 160, n)
    close[-3:] = [close[-4] * 0.99, close[-4] * 0.975, close[-4] * 0.96]
    df = pd.DataFrame({"open": close, "high": close * 1.02,
                       "low": close * 0.98, "close": close,
                       "volume": np.full(n, 1e6)}, index=idx)

    cands = S.screen({"TESTCO": df}, SwingLearner(state_file="/nonexistent"),
                     "risk_on")
    if not cands:
        pytest.skip("synthetic series did not trip a setup")
    c = cands[0]
    assert c["direction"] == "long"
    reward = c["target"] - c["close"]
    risk = c["close"] - c["stop"]
    assert risk > reward
    assert risk / reward == pytest.approx(S.STOP_ATR_MULT / S.TARGET_ATR_MULT,
                                          rel=0.02)


# ── learner ──────────────────────────────────────────────────────────────
def test_short_weight_is_frozen_even_on_a_winning_bucket():
    """The defect: 28 short trades at a 71% win rate were promoting the short
    bucket, against 6,188 backtest trades that say PF 0.68-0.83."""
    L = SwingLearner(state_file="/nonexistent")
    L.buckets["short|risk_off"] = {"wins": 20, "losses": 8, "sum_ret": 0.23,
                                   "n": 28, "by_signal": {}}
    # the posterior really would have promoted it, absent the freeze
    lo, _ = _wilson(20, 28)
    assert lo > 0.5, "fixture no longer represents a promotable bucket"
    assert L.weight("short", "rsi2_overbought", "risk_off") == 1.0


def test_long_weights_still_move():
    """The freeze must not neuter the learner on the funded side."""
    L = SwingLearner(state_file="/nonexistent")
    L.buckets["long|risk_on"] = {"wins": 200, "losses": 100, "sum_ret": 1.0,
                                 "n": 300, "by_signal": {}}
    assert L.weight("long", "rsi2_oversold", "risk_on") > 1.0


def test_shorts_are_still_recorded(tmp_path):
    """Freezing the weight must not blind the paper bench — the falsification
    evidence is the reason shorts are journaled at all."""
    state = tmp_path / "learner.json"
    L = SwingLearner(state_file=str(state))
    cwd = os.getcwd()
    os.chdir(tmp_path)                      # AUDIT_FILE is a relative path
    try:
        L.record("short", "3_up_days", "risk_off", won=False, ret_net=-0.02)
    finally:
        os.chdir(cwd)
    assert L.buckets["short|risk_off"]["n"] == 1
    assert L.buckets["short|risk_off"]["losses"] == 1


# ── correlation groups ───────────────────────────────────────────────────
def test_adani_names_share_a_group():
    """The 2026-08-31 book held seven of these at once."""
    adani = ["ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER"]
    assert len({group_of(s) for s in adani}) == 1


def test_promoter_beats_sector_for_tcs():
    assert group_of("TCS") == "TATA"
    assert group_of("INFY") == "IT"


def test_unmapped_symbols_are_singletons():
    assert group_of("POLYCAB") != group_of("LAURUSLABS")
    assert groups_present(["POLYCAB", "LAURUSLABS"]).keys().__len__() == 2


# ── portfolio caps ───────────────────────────────────────────────────────
def _cand(sym, rank=1.0, fundable=True):
    return {"symbol": sym, "direction": "long", "signal": "rsi2_oversold",
            "rank": rank, "fundable": fundable}


def test_group_cap_blocks_the_adani_pileup():
    cands = [_cand(s) for s in ["ADANIENT", "ADANIGREEN", "ADANIPORTS",
                                "ADANIPOWER"]]
    kept = S.apply_portfolio_caps(cands, held=[])
    assert len(kept) == S.MAX_PER_GROUP
    blocked = [c for c in cands if c.get("cap_reason")]
    assert len(blocked) == 2
    assert all("group ADANI" in c["cap_reason"] for c in blocked)


def test_duplicate_symbol_is_blocked():
    cands = [_cand("SBIN"), _cand("SBIN")]
    kept = S.apply_portfolio_caps(cands, held=[])
    assert len(kept) == 1
    assert cands[1]["cap_reason"] == "already held"


def test_already_held_symbol_is_blocked():
    cands = [_cand("POLYCAB")]
    kept = S.apply_portfolio_caps(cands, held=["POLYCAB"])
    assert kept == []
    assert cands[0]["cap_reason"] == "already held"


def test_book_cap_counts_existing_positions():
    held = [f"SOLO{i}" for i in range(S.MAX_CONCURRENT - 1)]
    cands = [_cand("POLYCAB"), _cand("NAUKRI")]
    kept = S.apply_portfolio_caps(cands, held=held)
    assert len(kept) == 1, "only one slot should remain"
    assert "book at cap" in cands[1]["cap_reason"]


def test_caps_never_promote_an_unfundable_candidate():
    cands = [_cand("SBIN", fundable=False)]
    assert S.apply_portfolio_caps(cands, held=[]) == []
    assert cands[0]["fundable"] is False


def test_capped_candidates_stay_on_the_bench():
    """Capped setups must remain visible to the learner, not vanish."""
    cands = [_cand(s) for s in ["ADANIENT", "ADANIGREEN", "ADANIPORTS"]]
    S.apply_portfolio_caps(cands, held=[])
    bench = [c for c in cands if not c["fundable"]]
    assert len(bench) == 1
    assert bench[0]["cap_reason"]


def test_rank_order_wins_the_slot():
    """apply_portfolio_caps consumes candidates in the order given, and
    screen() sorts by rank descending — so the best-ranked name keeps it."""
    cands = [_cand("ADANIENT", rank=9.0), _cand("ADANIGREEN", rank=8.0),
             _cand("ADANIPORTS", rank=7.0)]
    kept = S.apply_portfolio_caps(cands, held=[])
    assert [c["symbol"] for c in kept] == ["ADANIENT", "ADANIGREEN"]
