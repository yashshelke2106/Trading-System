"""Selection by measurement, and the six research gates.

Two pieces of the playbook made executable:

  core/selection.py      the universe is a liquidity measurement, not a
                         hand-maintained list that rots (two of its names were
                         delisted and silently failed every fetch)
  core/research_gates.py the six checks that overturned six readings in a
                         single day. A checklist in a document does not run.
"""

import numpy as np
import pytest

from core import selection as sel
from core.research_gates import BASELINE_3D_PCT, GateReport


# ── selection ────────────────────────────────────────────────────────────

def test_tiers_are_nested_and_ordered():
    """Stricter liquidity floors must yield subsets, never disjoint sets."""
    opts = set(sel.universe("options").symbols)
    futs = set(sel.universe("futures").symbols)
    res = set(sel.universe("research").symbols)
    assert opts <= futs <= res
    assert len(opts) <= len(futs) <= len(res)


def test_delisted_names_never_appear():
    """LTIM and GUJGASLTD have no successor and fail every fetch."""
    for tier in ("options", "futures", "research"):
        syms = sel.universe(tier).symbols
        for dead in sel.DEAD:
            assert dead not in syms, f"{dead} leaked into {tier}"


def test_unknown_tier_is_rejected():
    with pytest.raises(ValueError):
        sel.universe("penny-stocks")


def test_source_is_declared_not_silent():
    """A caller must be able to tell a measured universe from a fallback."""
    u = sel.universe("futures")
    assert u.source in ("measured", "fallback:universe.py")


def test_universe_behaves_like_a_collection():
    u = sel.universe("research")
    assert len(u) == len(u.symbols)
    assert list(iter(u)) == u.symbols
    if u.symbols:
        assert u.symbols[0] in u


def test_is_tradeable_respects_the_tier():
    """A name can clear the research floor and fail the options floor."""
    res = sel.universe("research").symbols
    opts = sel.universe("options").symbols
    only_research = [s for s in res if s not in opts]
    if only_research:
        s = only_research[0]
        assert sel.is_tradeable(s, "research")
        assert not sel.is_tradeable(s, "options")


# ── research gates ───────────────────────────────────────────────────────

def test_numpy_bool_does_not_hide_a_failed_gate():
    """Regression: numpy comparisons return np.bool_, and
    `np.bool_(False) is False` is False -- which dropped failed gates out of
    failures() while they still printed as FAIL."""
    r = GateReport("x").spread_t(np.zeros(10) + 0.0001)
    assert r.failures(), "a failing numpy-derived gate must be reported"
    assert all(isinstance(g.passed, bool) for g in r.gates)


def test_baseline_gate_uses_the_measured_default():
    r = GateReport("x").baseline(strategy_ret=0.001)   # below +0.186%
    assert r.failures()
    r2 = GateReport("x").baseline(strategy_ret=BASELINE_3D_PCT / 100 + 0.01)
    assert not r2.failures()


def test_spread_t_requires_positive_significance():
    rng = np.random.default_rng(7)
    strong = rng.normal(0.5, 1.0, 400)
    assert not GateReport("x").spread_t(strong).failures()
    negative = rng.normal(-0.5, 1.0, 400)          # significant but WRONG WAY
    assert GateReport("x").spread_t(negative).failures()


def test_cost_sweep_fails_when_the_sign_flips():
    flips = GateReport("x").cost_sweep({10: 0.15, 20: 0.02, 30: -0.12})
    assert flips.failures()
    holds = GateReport("x").cost_sweep({10: 0.15, 20: 0.09, 30: 0.04})
    assert not holds.failures()


def test_cost_sweep_needs_more_than_one_point():
    assert GateReport("x").cost_sweep({10: 0.15}).failures()


def test_shuffle_gate_rejects_a_below_null_result():
    """The real case: best real cell +12.07% vs a null whose bests average
    +18.15% -- the 0th percentile."""
    null = np.full(200, 18.15)
    assert GateReport("x").shuffle(12.07, null).failures()
    assert not GateReport("x").shuffle(25.0, null).failures()


def test_an_unrun_gate_is_not_a_pass():
    r = GateReport("x").baseline(0.05).point_in_time(True)
    assert not r.passed()
    assert "2 SPREAD_T" in r.missing()
    assert "INCOMPLETE" in r.verdict()


def test_not_applicable_satisfies_a_gate_explicitly():
    r = (GateReport("x")
         .baseline(0.05)
         .spread_t(np.random.default_rng(1).normal(0.5, 1.0, 400))
         .point_in_time(True).corp_actions(True)
         .cost_sweep({10: 0.05, 30: 0.02})
         .not_applicable("6 SHUFFLE", "single parameterisation")
         .fundability(capital=500_000, margin_per_unit=50_000, units_required=4))
    assert r.passed(), r.verdict()
    assert "all gates cleared" in r.verdict()


def test_re_running_a_gate_replaces_rather_than_duplicates():
    r = GateReport("x").corp_actions(False).corp_actions(True)
    names = [g.name for g in r.gates]
    assert names.count("4 CORP_ACTIONS") == 1
    assert not r.failures()


def test_verdict_names_every_failing_gate():
    r = (GateReport("x").baseline(0.0001)
         .cost_sweep({10: 0.1, 30: -0.1}))
    v = r.verdict()
    assert "1 BASELINE" in v and "5 COST_SWEEP" in v
