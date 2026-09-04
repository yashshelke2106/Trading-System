"""Evals for bucket consolidation (2026-07-16): learner pools trust at
direction x regime (the 3 entry signals were proven statistically
indistinguishable — deep-entry gate, paired t=-0.01), tripling data
concentration. Per-signal counts stay recorded for a future re-split."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.swing_learner import MIN_N, SwingLearner, _wilson

SIGNALS = ["rsi2_oversold", "3_down_days", "2pct_under_5dma"]


def _fresh(path):
    if os.path.exists(path):
        os.remove(path)
    return SwingLearner(state_file=path)


def test_pooling_reaches_significance_across_signals():
    # 7 wins from each of 3 signals: no single signal reaches MIN_N, but the
    # pooled bucket (n=21) does — this is the consolidation speed-up.
    sf = os.path.join(tempfile.gettempdir(), "_lc1.json")
    L = _fresh(sf)
    for sig in SIGNALS:
        for _ in range(7):
            L.record("long", sig, "risk_on", True, 0.01)
    ws = {sig: L.weight("long", sig, "risk_on") for sig in SIGNALS}
    assert len(set(ws.values())) == 1, f"signals must share the pooled weight: {ws}"
    assert list(ws.values())[0] > 1.0, "pooled n=21 all-wins must lift the weight"
    if os.path.exists(sf): os.remove(sf)


def test_below_gate_stays_neutral():
    sf = os.path.join(tempfile.gettempdir(), "_lc2.json")
    L = _fresh(sf)
    for i in range(MIN_N - 1):
        L.record("long", SIGNALS[i % 3], "risk_on", True, 0.01)
    assert L.weight("long", "rsi2_oversold", "risk_on") == 1.0
    if os.path.exists(sf): os.remove(sf)


def test_posterior_arithmetic_is_direction_symmetric():
    """The original invariant, narrowed on 2026-09-04.

    Identical evidence must still produce an identical POSTERIOR for a long
    and a short bucket — no direction gets a different prior, update rule, or
    Wilson bound. What changed is downstream: the short side's rank WEIGHT is
    pinned (see test_short_weight_is_frozen below), so the learner still
    measures shorts honestly and simply declines to act on the measurement."""
    sf = os.path.join(tempfile.gettempdir(), "_lc3.json")
    L = _fresh(sf)
    for _ in range(30):
        L.record("long", "rsi2_oversold", "risk_on", True, 0.01)
        L.record("short", "rsi2_overbought", "risk_off", True, 0.01)
    lb = L.buckets["long|risk_on"]
    sb = L.buckets["short|risk_off"]
    for field in ("wins", "losses", "n", "sum_ret"):
        assert lb[field] == sb[field], f"{field} diverged by direction"
    assert _wilson(lb["wins"], lb["n"]) == _wilson(sb["wins"], sb["n"])
    if os.path.exists(sf): os.remove(sf)


def test_short_weight_is_frozen_and_long_is_not():
    """The 2026-09-04 asymmetry, asserted explicitly so it stays deliberate.

    Rationale (short_side_policy.md): 6,188 short signals re-resolved under
    ten exit policies came out negative in every one, PF 0.68-0.83. The live
    bucket meanwhile read 71% wins on 28 trades and was boosting short ranks —
    weight() scores win rate, which is exactly what a low-payoff short book
    flatters. If this test ever fails, the freeze was removed; re-read the
    evidence before accepting that."""
    sf = os.path.join(tempfile.gettempdir(), "_lc3b.json")
    L = _fresh(sf)
    for _ in range(30):
        L.record("long", "rsi2_oversold", "risk_on", True, 0.01)
        L.record("short", "rsi2_overbought", "risk_off", True, 0.01)
    assert L.weight("long", "rsi2_oversold", "risk_on") > 1.0
    assert L.weight("short", "rsi2_overbought", "risk_off") == 1.0
    if os.path.exists(sf): os.remove(sf)


def test_migration_from_per_signal_state():
    # Old state files keyed direction|signal|regime must fold into the pooled
    # bucket with per-signal counts preserved.
    sf = os.path.join(tempfile.gettempdir(), "_lc4.json")
    old = {"buckets": {
        "long|rsi2_oversold|risk_on": {"wins": 3, "losses": 2, "sum_ret": 0.01, "n": 5},
        "long|3_down_days|risk_on":   {"wins": 4, "losses": 2, "sum_ret": 0.02, "n": 6},
        "short|3_up_days|risk_off":   {"wins": 1, "losses": 2, "sum_ret": -0.02, "n": 3},
    }}
    with open(sf, "w", encoding="utf-8") as f:
        json.dump(old, f)
    L = SwingLearner(state_file=sf)
    b = L.buckets["long|risk_on"]
    assert b["n"] == 11 and b["wins"] == 7 and b["losses"] == 4
    assert b["by_signal"]["rsi2_oversold"]["n"] == 5
    assert b["by_signal"]["3_down_days"]["n"] == 6
    assert L.buckets["short|risk_off"]["n"] == 3
    assert abs(b["sum_ret"] - 0.03) < 1e-9
    if os.path.exists(sf): os.remove(sf)
