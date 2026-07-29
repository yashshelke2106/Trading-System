"""Tests for core/strategy_search.py.

The agent's value is entirely in its refusals. What must hold:
  - the random-walk null is computed correctly (S/(S+T));
  - the intrabar blend sits between the pessimistic and optimistic bounds;
  - a config with too few trades is not evaluable (no n=12 "winners");
  - hit rate and expectancy are computed consistently with the R model;
  - pure noise does NOT produce a validated survivor, no matter how many
    configurations are searched.

Offline, deterministic.
"""
from __future__ import annotations

import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from core import strategy_search as ss


def _table(n_rows=2000, hit_p=0.5, seed=0, key="1.0_3.0_5"):
    """Minimal table with one (tgt,stop,hold) outcome column set."""
    rng = np.random.default_rng(seed)
    f = pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n_rows, freq="h"),
        "symbol": "X",
        "above_ma200": rng.random(n_rows) > 0.5,
        "above_ma50": rng.random(n_rows) > 0.5,
        "rsi2": rng.uniform(0, 100, n_rows),
        "rsi14": rng.uniform(0, 100, n_rows),
        "vol_ratio": rng.uniform(0.3, 3.0, n_rows),
        "gap_pct": rng.normal(0, 1, n_rows),
        "dist_20d_high": rng.normal(-3, 3, n_rows),
        "atr_pct": rng.uniform(0.5, 4.0, n_rows),
        "price": 100.0,
    })
    out = (rng.random(n_rows) < hit_p).astype(np.int8)
    f[f"p_{key}"] = out
    f[f"o_{key}"] = out
    f[f"v_{key}"] = True
    return f


def test_null_is_stop_over_stop_plus_target():
    tbl = _table()
    cfg = ss.Config("none", "any", 1.0, 3.0, 5)
    r = ss.evaluate(tbl, np.ones(len(tbl), bool), cfg, cost_pct=0.0, target_wr=0.7)
    assert r is not None
    assert r.p_null == pytest.approx(3.0 / 4.0)      # S/(S+T)


def test_intrabar_blend_between_bounds():
    """hit must lie between the pessimistic and optimistic outcome means."""
    tbl = _table(key="1.0_3.0_5")
    # Make pess and opt differ: pess all 0, opt all 1 -> blend == P_TARGET_FIRST
    tbl["p_1.0_3.0_5"] = np.zeros(len(tbl), dtype=np.int8)
    tbl["o_1.0_3.0_5"] = np.ones(len(tbl), dtype=np.int8)
    cfg = ss.Config("none", "any", 1.0, 3.0, 5)
    r = ss.evaluate(tbl, np.ones(len(tbl), bool), cfg, 0.0, 0.7)
    assert r.hit_rate == pytest.approx(ss.P_TARGET_FIRST, abs=1e-6)
    assert 0.0 <= r.hit_rate <= 1.0


def test_too_few_trades_is_not_evaluable():
    """No config may be scored on a handful of trades."""
    tbl = _table(n_rows=50)
    cfg = ss.Config("none", "any", 1.0, 3.0, 5)
    assert ss.evaluate(tbl, np.ones(len(tbl), bool), cfg, 0.2, 0.7) is None


def test_expectancy_matches_R_model():
    tbl = _table(hit_p=1.0)          # every trade a winner
    tbl["p_1.0_3.0_5"] = 1
    tbl["o_1.0_3.0_5"] = 1
    cfg = ss.Config("none", "any", 1.0, 3.0, 5)
    r = ss.evaluate(tbl, np.ones(len(tbl), bool), cfg, cost_pct=0.0, target_wr=0.7)
    # hit=1 -> exp = 1*rr - 0 = rr. Tolerance covers the stored fields' own
    # rounding (exp_R at 4dp, rr at 3dp), not a modelling slack.
    assert r.exp_R == pytest.approx(r.rr, abs=1e-3)


def test_cost_is_charged_in_R_units():
    tbl = _table(hit_p=1.0)
    tbl["p_1.0_3.0_5"] = 1
    tbl["o_1.0_3.0_5"] = 1
    cfg = ss.Config("none", "any", 1.0, 3.0, 5)
    free = ss.evaluate(tbl, np.ones(len(tbl), bool), cfg, 0.0, 0.7)
    paid = ss.evaluate(tbl, np.ones(len(tbl), bool), cfg, 0.3, 0.7)
    # cost_R = cost_pct / stop_pct = 0.3/3.0 = 0.1
    assert free.exp_R - paid.exp_R == pytest.approx(0.1, abs=1e-6)


def test_hit_target_and_profitable_flags():
    tbl = _table()
    tbl["p_1.0_3.0_5"] = 1
    tbl["o_1.0_3.0_5"] = 1
    cfg = ss.Config("none", "any", 1.0, 3.0, 5)
    r = ss.evaluate(tbl, np.ones(len(tbl), bool), cfg, 0.0, 0.7)
    assert r.hits_target_wr is True and r.profitable is True

    tbl2 = _table()
    tbl2["p_1.0_3.0_5"] = 0
    tbl2["o_1.0_3.0_5"] = 0
    r2 = ss.evaluate(tbl2, np.ones(len(tbl2), bool), cfg, 0.0, 0.7)
    assert r2.hits_target_wr is False and r2.profitable is False


def test_noise_search_yields_no_validated_survivor():
    """The core guarantee: searching many configs over PURE NOISE must not
    produce a deflation-surviving winner. This is the anti-overfit contract."""
    from core.deflated_sharpe import evaluate as dsr_eval
    rng = np.random.default_rng(7)
    n_trials = 900          # a big search
    best_dsr_pass = False
    for _ in range(40):
        # A "winner" from noise: hit rate drawn around the null, sample ~800.
        hit = rng.normal(0.75, 0.02)
        rr, T = 1.0 / 3.0, 800
        mean_r = hit * rr - (1 - hit)
        var = hit * (rr - mean_r) ** 2 + (1 - hit) * (-1 - mean_r) ** 2
        sr = mean_r / np.sqrt(var) if var > 0 else 0.0
        v = dsr_eval(sr_hat=float(sr), T=T, skew=0.0, kurt=3.0,
                     n_trials=n_trials, sr_trial_sd=None)
        best_dsr_pass = best_dsr_pass or v.passes
    assert not best_dsr_pass, "noise produced a 'validated' strategy — gate broken"


def test_config_key_is_stable_and_unique():
    a = ss.Config("uptrend", "mkt_up", 1.0, 3.0, 5)
    b = ss.Config("uptrend", "mkt_up", 1.0, 3.0, 10)
    assert a.key() != b.key()
    assert a.key() == ss.Config("uptrend", "mkt_up", 1.0, 3.0, 5).key()


def test_search_space_is_fully_enumerated():
    expected = len(ss.FILTERS) * len(ss.REGIMES) * len(ss.TARGETS) * \
        len(ss.STOPS) * len(ss.HOLDS)
    combos = set()
    for f, rg in itertools.product(ss.FILTERS, ss.REGIMES):
        for t, s, h in itertools.product(ss.TARGETS, ss.STOPS, ss.HOLDS):
            combos.add(ss.Config(f, rg, t, s, h).key())
    assert len(combos) == expected


def test_all_filters_return_boolean_masks():
    tbl = _table()
    for name, fn in ss.FILTERS.items():
        m = fn(tbl)
        assert m.dtype == bool, f"{name} did not return a boolean mask"
        assert len(m) == len(tbl)
