"""Tests for core/rule_recurrence.py — post-deployment rule validation.

The properties that make this honest rather than self-congratulatory:
  - a rule whose effect persisted is CONFIRMED;
  - a rule whose effect vanished is REVERTED and actually retired;
  - a guard with no post-deploy trades is UNOBSERVABLE, never "confirmed"
    (silence is obedience, not proof);
  - too little post-deploy data is UNPROVEN and is NOT retired;
  - retired rules are written to the learner's ledger as rejects so they
    are never re-proposed.

Offline, deterministic.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core import rule_recurrence as rr

_BASE = _dt.datetime(2026, 6, 1, tzinfo=_dt.timezone.utc)
DEPLOY = (_BASE + _dt.timedelta(days=10)).isoformat()


def _row(offset_days, won, symbol="TESTSYM", direction="long"):
    ts = (_BASE + _dt.timedelta(days=offset_days)).isoformat()
    return {
        "ts": ts, "symbol": symbol, "direction": direction,
        "market_bias": "neutral", "grade": "B", "rsi": 50,
        "volume_ratio": 1.2, "vote_margin": 3,
        "_won": won, "_pnl": 1.5 if won else -1.0,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated guard/boost/ledger files."""
    g = str(tmp_path / "guards.json")
    b = str(tmp_path / "boosts.json")
    ml_led = str(tmp_path / "mistake_ledger.jsonl")
    kl_led = str(tmp_path / "keeper_ledger.jsonl")
    ret = str(tmp_path / "retire.jsonl")

    monkeypatch.setattr(rr, "GUARDS_PATH", g)
    monkeypatch.setattr(rr, "BOOSTS_PATH", b)
    monkeypatch.setattr(rr, "MISTAKE_LEDGER", ml_led)
    monkeypatch.setattr(rr, "KEEPER_LEDGER", kl_led)
    monkeypatch.setattr(rr, "RETIREMENT_PATH", ret)

    # load_guards / load_boosts read their own module paths
    import core.mistake_learner as ml
    import core.keeper_learner as kl
    monkeypatch.setattr(ml, "GUARDS_PATH", g)
    monkeypatch.setattr(kl, "BOOSTS_PATH", b)
    return g, b, ml_led, kl_led, ret


def _put_guard(path, feature="symbol", value="BADSYM", holdout_loss=1.0):
    json.dump({"updated": "x", "rules": [{
        "feature": feature, "value": value, "train_lossrate": 1.0,
        "holdout_lossrate": holdout_loss, "baseline": 0.5,
        "p_bonferroni": 0.01, "expectancy_gain": 0.5}]},
        open(path, "w", encoding="utf-8"))


def _put_boost(path, feature="symbol", value="GOODSYM", holdout_win=0.9):
    json.dump({"updated": "x", "rules": [{
        "feature": feature, "value": value, "boost": 1.4,
        "train_winrate": 0.85, "holdout_winrate": holdout_win,
        "baseline": 0.5, "p_bonferroni": 0.01, "expectancy_gain": 0.5}]},
        open(path, "w", encoding="utf-8"))


def _put_promote(ledger, key):
    with open(ledger, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": DEPLOY, "key": key,
                             "decision": "promote", "reason": "x"}) + "\n")


# ── guards ──────────────────────────────────────────────────────────────────

def test_guard_with_no_posttrades_is_unobservable_not_confirmed(env):
    g, _, ml_led, _, _ = env
    _put_guard(g)
    _put_promote(ml_led, "symbol=BADSYM")
    # Only pre-deploy trades exist for the guarded symbol.
    decided = [_row(1, False, symbol="BADSYM") for _ in range(5)]
    decided += [_row(20, True) for _ in range(10)]
    v = [x for x in rr.evaluate(decided) if x.kind == "guard"][0]
    assert v.verdict == rr.UNOBSERVABLE
    assert "not the same as being proven right" in v.detail


def test_guard_still_losing_is_confirmed(env):
    g, _, ml_led, _, _ = env
    _put_guard(g)
    _put_promote(ml_led, "symbol=BADSYM")
    decided = [_row(20 + i, False, symbol="BADSYM") for i in range(10)]  # all lose
    decided += [_row(20 + i, True) for i in range(10)]                    # baseline 50%
    v = [x for x in rr.evaluate(decided) if x.kind == "guard"][0]
    assert v.verdict == rr.CONFIRMED
    assert v.post_n == 10


def test_guard_whose_effect_vanished_is_reverted_and_retired(env):
    g, _, ml_led, _, ret = env
    _put_guard(g)
    _put_promote(ml_led, "symbol=BADSYM")
    # Post-deploy the guarded symbol wins as often as everything else.
    decided = [_row(20 + i, i % 2 == 0, symbol="BADSYM") for i in range(10)]
    decided += [_row(20 + i, i % 2 == 0) for i in range(10)]
    verdicts = rr.evaluate(decided)
    v = [x for x in verdicts if x.kind == "guard"][0]
    assert v.verdict == rr.REVERTED

    res = rr.enforce(verdicts)
    assert res["retired_guards"] == 1
    # active file no longer contains it
    assert json.load(open(g, encoding="utf-8"))["rules"] == []
    # retirement logged
    assert os.path.exists(ret) and "BADSYM" in open(ret, encoding="utf-8").read()
    # ledger got a reject so it is never re-proposed
    assert "RETIRED by recurrence check" in open(ml_led, encoding="utf-8").read()


def test_too_few_posttrades_is_unproven_and_not_retired(env):
    g, _, ml_led, _, _ = env
    _put_guard(g)
    _put_promote(ml_led, "symbol=BADSYM")
    decided = [_row(20 + i, True, symbol="BADSYM") for i in range(3)]
    decided += [_row(20 + i, i % 2 == 0) for i in range(10)]
    verdicts = rr.evaluate(decided)
    v = [x for x in verdicts if x.kind == "guard"][0]
    assert v.verdict == rr.UNPROVEN
    res = rr.enforce(verdicts)
    assert res["retired_guards"] == 0
    assert len(json.load(open(g, encoding="utf-8"))["rules"]) == 1


# ── keepers ─────────────────────────────────────────────────────────────────

def test_keeper_still_winning_is_confirmed(env):
    _, b, _, kl_led, _ = env
    _put_boost(b)
    _put_promote(kl_led, "symbol=GOODSYM")
    decided = [_row(20 + i, True, symbol="GOODSYM") for i in range(10)]
    decided += [_row(20 + i, i % 2 == 0) for i in range(10)]
    v = [x for x in rr.evaluate(decided) if x.kind == "keeper"][0]
    assert v.verdict == rr.CONFIRMED


def test_keeper_that_stopped_winning_is_reverted_and_retired(env):
    _, b, _, kl_led, _ = env
    _put_boost(b)
    _put_promote(kl_led, "symbol=GOODSYM")
    # Post-deploy it wins no more than baseline.
    decided = [_row(20 + i, i % 2 == 0, symbol="GOODSYM") for i in range(10)]
    decided += [_row(20 + i, i % 2 == 0) for i in range(10)]
    verdicts = rr.evaluate(decided)
    v = [x for x in verdicts if x.kind == "keeper"][0]
    assert v.verdict == rr.REVERTED
    res = rr.enforce(verdicts)
    assert res["retired_keepers"] == 1
    assert json.load(open(b, encoding="utf-8"))["rules"] == []
    assert "RETIRED by recurrence check" in open(kl_led, encoding="utf-8").read()


# ── general ─────────────────────────────────────────────────────────────────

def test_no_rules_is_graceful(env):
    assert rr.evaluate([_row(1, True)]) == []


def test_enforce_is_idempotent(env):
    g, _, ml_led, _, _ = env
    _put_guard(g)
    _put_promote(ml_led, "symbol=BADSYM")
    decided = [_row(20 + i, i % 2 == 0, symbol="BADSYM") for i in range(10)]
    decided += [_row(20 + i, i % 2 == 0) for i in range(10)]
    rr.enforce(rr.evaluate(decided))
    second = rr.enforce(rr.evaluate(decided))     # nothing left to retire
    assert second["retired_guards"] == 0
