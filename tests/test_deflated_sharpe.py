"""Tests for core/deflated_sharpe.py — selection-bias correction.

The properties that must hold for this to be a trustworthy gate:
  - more trials => higher bar (SR0 rises with N);
  - a genuine strong edge passes; noise does not;
  - fat tails / negative skew make the SAME Sharpe less significant;
  - the normal helpers match known values (no scipy).

All offline, deterministic.
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
import pytest

from core import deflated_sharpe as dsr


# ── normal helpers ──────────────────────────────────────────────────────────

def test_norm_cdf_known_points():
    assert dsr._norm_cdf(0.0) == pytest.approx(0.5, abs=1e-9)
    assert dsr._norm_cdf(1.96) == pytest.approx(0.9750, abs=1e-3)
    assert dsr._norm_cdf(-1.96) == pytest.approx(0.0250, abs=1e-3)


def test_norm_ppf_inverts_cdf():
    for p in (0.05, 0.5, 0.95, 0.975, 0.999):
        assert dsr._norm_cdf(dsr._norm_ppf(p)) == pytest.approx(p, abs=1e-6)


def test_norm_ppf_known():
    assert dsr._norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-4)


# ── expected max sharpe rises with trial count ──────────────────────────────

def test_more_trials_raise_the_bar():
    prev = -1.0
    for n in (1, 2, 5, 20, 100, 1000):
        sr0 = dsr.expected_max_sharpe(n, sr_trial_sd=0.1)
        assert sr0 >= prev, "SR0 must be monotone non-decreasing in N"
        prev = sr0
    assert dsr.expected_max_sharpe(1, 0.1) == 0.0


def test_sr0_scales_with_trial_dispersion():
    lo = dsr.expected_max_sharpe(50, 0.05)
    hi = dsr.expected_max_sharpe(50, 0.20)
    assert hi > lo


# ── DSR discriminates edge from noise ───────────────────────────────────────

def test_strong_edge_passes_even_under_many_trials():
    # Long series with a clear positive per-obs Sharpe. sr_trial_sd is the
    # dispersion of Sharpes ACROSS trials — realistically far smaller than the
    # winner, not equal to it (that conservative default is tested separately).
    rng = random.Random(1)
    rets = [rng.gauss(0.10, 1.0) for _ in range(2000)]  # SR ~ 0.10/obs
    v = dsr.evaluate(rets, n_trials=20, sr_trial_sd=0.03)
    assert v.sr_hat_per_obs > 0.05
    assert v.dsr > 0.95
    assert v.passes


def test_conservative_default_is_punitive():
    """With sr_trial_sd defaulting to sr_hat, even a decent Sharpe can fail —
    the default assumes trials were as dispersed as the winner is large. This
    is intended: absent a real trial-dispersion estimate, the gate errs strict."""
    rng = random.Random(11)
    rets = [rng.gauss(0.10, 1.0) for _ in range(2000)]
    v = dsr.evaluate(rets, n_trials=20)          # no sr_trial_sd -> = sr_hat
    assert v.sr0_per_obs > v.sr_hat_per_obs
    assert not v.passes


def test_pure_noise_does_not_pass():
    rng = random.Random(2)
    rets = [rng.gauss(0.0, 1.0) for _ in range(2000)]   # zero-mean noise
    v = dsr.evaluate(rets, n_trials=20)
    assert not v.passes
    assert v.dsr < 0.95


def test_marginal_edge_killed_by_trial_count():
    """A borderline Sharpe that would pass at N=1 fails once selection over
    many trials is priced in — the whole point of the gate."""
    rng = random.Random(3)
    rets = [rng.gauss(0.045, 1.0) for _ in range(1500)]
    solo = dsr.evaluate(rets, n_trials=1)
    many = dsr.evaluate(rets, n_trials=200)
    assert many.sr0_per_obs > solo.sr0_per_obs
    assert many.dsr < solo.dsr


# ── non-normality penalises significance ────────────────────────────────────

# Non-normality is evaluated in the meaningful regime (SR̂ above the null
# benchmark); with a small realistic sr_trial_sd, sr_hat=0.10 clears SR0.
def test_fat_tails_reduce_dsr_at_same_sharpe():
    base = dsr.evaluate(sr_hat=0.10, T=1500, skew=0.0, kurt=3.0, n_trials=5, sr_trial_sd=0.02)
    fat  = dsr.evaluate(sr_hat=0.10, T=1500, skew=0.0, kurt=9.0, n_trials=5, sr_trial_sd=0.02)
    assert base.sr_hat_per_obs > base.sr0_per_obs, "must be above benchmark"
    assert fat.dsr < base.dsr


def test_negative_skew_reduces_dsr():
    sym = dsr.evaluate(sr_hat=0.10, T=1500, skew=0.0,  kurt=5.0, n_trials=5, sr_trial_sd=0.02)
    neg = dsr.evaluate(sr_hat=0.10, T=1500, skew=-1.0, kurt=5.0, n_trials=5, sr_trial_sd=0.02)
    assert sym.sr_hat_per_obs > sym.sr0_per_obs
    assert neg.dsr < sym.dsr


# ── minimum track record length ─────────────────────────────────────────────

def test_min_trl_infinite_when_below_benchmark():
    assert dsr.min_track_record_length(0.02, 0.0, 3.0, sr_benchmark=0.05) == float("inf")


def test_min_trl_finite_and_positive_for_real_edge():
    trl = dsr.min_track_record_length(0.12, 0.0, 3.0, sr_benchmark=0.0)
    assert 0 < trl < 1000


# ── Benjamini–Hochberg ──────────────────────────────────────────────────────

def test_bh_all_null_none_survive():
    ps = [0.6, 0.7, 0.8, 0.4, 0.55]
    r = dsr.benjamini_hochberg(ps, fdr=0.10)
    assert r["n_significant"] == 0


def test_bh_one_strong_survives():
    ps = [1e-6, 0.6, 0.7, 0.8, 0.9]
    r = dsr.benjamini_hochberg(ps, fdr=0.10)
    assert r["n_significant"] >= 1
    assert 0 in r["survivors"]


def test_bh_monotone_in_fdr():
    ps = [0.001, 0.02, 0.03, 0.2, 0.5]
    lo = dsr.benjamini_hochberg(ps, fdr=0.05)["n_significant"]
    hi = dsr.benjamini_hochberg(ps, fdr=0.25)["n_significant"]
    assert hi >= lo
