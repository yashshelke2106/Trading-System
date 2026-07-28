"""Tests for core/keeper_learner.py — the positive dual of the mistake learner.

Properties that make it a validated booster, not a curve-fitter:
  - a genuine recurring WIN-signature is promoted with a boost > 1.0;
  - an in-sample-only winning fluke is NOT (holdout kills it);
  - boosts are bounded and gentle (never a bet-the-book multiplier);
  - overlapping keepers take the max boost, not the product;
  - SPOT outcome labels (shared with mistake_learner).

Offline, deterministic.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core import keeper_learner as kl

_BASE = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)


def _row(i, won, direction="long", regime="neutral"):
    ts = (_BASE + _dt.timedelta(minutes=13 * i)).isoformat()
    return {
        "ts": ts, "direction": direction, "market_bias": regime, "grade": "B",
        "symbol": "TESTSYM", "rsi": 50, "volume_ratio": 1.2, "vote_margin": 3,
        "spot_outcome": "TARGET_HIT" if won else "SL_HIT",
        "spot_pnl_pct": 1.5 if won else -1.0,
    }


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    j = str(tmp_path / "journal.jsonl")
    b = str(tmp_path / "boosts.json")
    l = str(tmp_path / "ledger.jsonl")
    # load_decided lives in mistake_learner; patch its JOURNAL too.
    import core.mistake_learner as ml
    monkeypatch.setattr(ml, "JOURNAL", j)
    monkeypatch.setattr(kl, "BOOSTS_PATH", b)
    monkeypatch.setattr(kl, "LEDGER_PATH", l)
    return j, b, l


def test_boost_band_monotone_and_clamped():
    assert kl._boost_from_edge(0.0) == 1.0
    assert kl._boost_from_edge(0.20) == kl.MAX_BOOST
    assert kl._boost_from_edge(1.0) == kl.MAX_BOOST      # clamped
    assert kl._boost_from_edge(0.10) > kl._boost_from_edge(0.05)


def test_recurring_win_signature_is_boosted(paths):
    j, _, _ = paths
    rows = []
    # Interleave both types across the WHOLE timeline so train and holdout each
    # hold a mix (otherwise the cell == the whole holdout and its expectancy
    # gain vs the rest collapses to ~0).
    for i in range(240):
        if i % 3 == 0:
            # short+bearish WINS ~85%
            rows.append(_row(i, won=(i % 20) != 0, direction="short", regime="bearish"))
        else:
            # baseline longs ~45%
            rows.append(_row(i, won=(i % 20) >= 11, direction="long"))
    _write(j, rows)

    res = kl.learn(verbose=False)
    assert res["ok"]
    assert any("short" in k or "bearish" in k for k in res["boosts"]), res["boosts"]
    mult, why = kl.confidence_boost({
        "direction": "short", "market_bias": "bearish", "grade": "B",
        "symbol": "X", "rsi": 50, "volume_ratio": 1.2, "vote_margin": 3,
        "ts": "2026-07-01T05:00:00+00:00"})
    assert mult > 1.0 and "keeper" in why
    assert mult <= kl.MAX_BOOST


def test_in_sample_only_winning_fluke_not_boosted(paths):
    j, _, _ = paths
    rows = []
    # short+bearish wins only in TRAIN, reverts in holdout -> no boost.
    for i in range(140):
        train_window = i < 98
        if i % 5 == 0:
            won = True if train_window else False
            rows.append(_row(i, won=won, direction="short", regime="bearish"))
        else:
            rows.append(_row(i, won=(i % 20) >= 11, direction="long"))
    _write(j, rows)
    res = kl.learn(verbose=False)
    assert not any("bearish" in k for k in res["boosts"])


def test_no_boosts_means_neutral_multiplier(paths):
    mult, why = kl.confidence_boost({"direction": "long"})
    assert mult == 1.0 and why == ""


def test_overlapping_keepers_take_max_not_product(paths, monkeypatch):
    # Two matching rules -> multiplier is the larger, not boost1*boost2.
    monkeypatch.setattr(kl, "load_boosts", lambda path=None: [
        {"feature": "direction", "value": "short", "boost": 1.3,
         "holdout_winrate": 0.7, "baseline": 0.45},
        {"feature": "regime", "value": "bearish", "boost": 1.4,
         "holdout_winrate": 0.72, "baseline": 0.45},
    ])
    mult, _ = kl.confidence_boost({"direction": "short", "market_bias": "bearish"})
    assert mult == 1.4


def test_too_few_trades_graceful(paths):
    j, _, _ = paths
    _write(j, [_row(i, won=True) for i in range(9)])
    res = kl.learn(verbose=False)
    assert not res["ok"]
