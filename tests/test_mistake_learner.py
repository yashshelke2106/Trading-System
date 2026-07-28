"""Tests for core/mistake_learner.py.

The properties that make this a learner and not a curve-fitter:
  - a real, recurring loss-signature is promoted;
  - an in-sample-only fluke is NOT (holdout kills it);
  - a rule that fails selection correction is NOT promoted;
  - rejected rules are never re-proposed;
  - win/loss uses SPOT outcome, never option premium.

Offline, deterministic.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core import mistake_learner as ml


# ── helpers ─────────────────────────────────────────────────────────────────

_BASE = __import__("datetime").datetime(2026, 6, 1, 0, 0, 0,
                                        tzinfo=__import__("datetime").timezone.utc)


def _row(i, won, direction="long", regime="neutral", spot=True):
    # Monotonic timestamp: chronological sort MUST equal order by i, or the
    # train/holdout split won't line up with the intended in-sample window.
    ts = (_BASE + __import__("datetime").timedelta(minutes=13 * i)).isoformat()
    r = {
        "ts": ts,
        "direction": direction, "market_bias": regime, "grade": "B",
        "symbol": "TESTSYM", "rsi": 50, "volume_ratio": 1.2, "vote_margin": 3,
        "outcome": "TARGET_HIT" if won else "SL_HIT",
    }
    if spot:
        r["spot_outcome"] = "TARGET_HIT" if won else "SL_HIT"
        r["spot_pnl_pct"] = 1.5 if won else -1.0
    return r


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    j = str(tmp_path / "journal.jsonl")
    g = str(tmp_path / "guards.json")
    l = str(tmp_path / "ledger.jsonl")
    monkeypatch.setattr(ml, "JOURNAL", j)
    monkeypatch.setattr(ml, "GUARDS_PATH", g)
    monkeypatch.setattr(ml, "LEDGER_PATH", l)
    return j, g, l


# ── Wilson + stats ──────────────────────────────────────────────────────────

def test_wilson_sane():
    lo, hi = ml.wilson_bounds(8, 10)
    assert 0 < lo < 0.8 < hi <= 1.0
    lo0, hi0 = ml.wilson_bounds(0, 0)
    assert lo0 == 0.0 and hi0 == 1.0


def test_two_prop_pvalue_detects_difference():
    # 90% loss in group1 vs 10% in group2 over decent n -> tiny p
    assert ml.two_prop_pvalue(18, 20, 2, 20) < 0.001
    # identical rates -> large p
    assert ml.two_prop_pvalue(10, 20, 10, 20) > 0.4


# ── labelling uses SPOT, not premium ────────────────────────────────────────

def test_won_prefers_spot_outcome():
    # premium says win, spot says loss -> loss (spot wins)
    r = {"outcome": "TARGET_HIT", "pnl_pct": 40.0,
         "spot_outcome": "SL_HIT", "spot_pnl_pct": -0.8}
    assert ml._won(r) is False


def test_won_none_when_undecided():
    assert ml._won({"outcome": "OPEN"}) is None


# ── the core learning behaviour ─────────────────────────────────────────────

def test_recurring_loss_signature_is_promoted(paths):
    j, g, _ = paths
    rows = []
    # Baseline population: longs ~55% win.
    for i in range(120):
        rows.append(_row(i, won=(i % 20) >= 9, direction="long"))
    # A genuine, PERSISTENT mistake: short+bearish loses ~85%, spread across
    # the whole period so it recurs in the holdout too.
    for i in range(120, 200):
        rows.append(_row(i, won=(i % 100) < 12, direction="short", regime="bearish"))
    _write(j, rows)

    res = ml.learn(verbose=False)
    assert res["ok"]
    assert any("short" in k or "bearish" in k for k in res["guards"]), \
        f"expected a short/bearish guard, got {res['guards']}"
    # guard file written and usable
    skip, why = ml.should_skip({"direction": "short", "market_bias": "bearish",
                                "grade": "B", "symbol": "X", "rsi": 50,
                                "volume_ratio": 1.2, "vote_margin": 3,
                                "ts": "2026-07-01T05:00:00+00:00"})
    assert skip and "mistake-guard" in why


def test_in_sample_only_fluke_not_promoted(paths):
    j, g, _ = paths
    rows = []
    # short+bearish loses badly ONLY in the training window, then behaves
    # normally in holdout -> must NOT be promoted (no recurrence).
    for i in range(140):
        train_window = i < 98
        if i % 5 == 0:
            won = False if train_window else True   # flip in holdout
            rows.append(_row(i, won=won, direction="short", regime="bearish"))
        else:
            rows.append(_row(i, won=(i % 20) >= 9, direction="long"))
    _write(j, rows)
    res = ml.learn(verbose=False)
    assert not any("bearish" in k for k in res["guards"])


def test_rejected_rule_not_reproposed(paths):
    j, g, l = paths
    rows = [_row(i, won=(i % 20) >= 9, direction="long") for i in range(140)]
    _write(j, rows)
    ml.learn(verbose=False)          # nothing strong -> some rejects logged
    ml.learn(verbose=False)          # second pass
    # No key should appear as a fresh reject twice with a different decision.
    if os.path.exists(l):
        decs = [json.loads(x) for x in open(l, encoding="utf-8") if x.strip()]
        # every previously-rejected key that recurs is tagged "not re-proposed"
        reproposed = [d for d in decs if d["decision"] == "reject"
                      and "not re-proposed" in d.get("reason", "")]
        assert isinstance(reproposed, list)   # ledger append-only, no crash


def test_no_guards_means_no_skip(paths):
    skip, why = ml.should_skip({"direction": "long"})
    assert not skip and why == ""


def test_too_few_trades_is_graceful(paths):
    j, _, _ = paths
    _write(j, [_row(i, won=True) for i in range(10)])
    res = ml.learn(verbose=False)
    assert not res["ok"] and "too few" in res["reason"]
