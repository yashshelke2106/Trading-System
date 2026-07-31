"""Tests for core/proof.py — pre-registered paper-book proof.

The guarantees that make this an honest proof, not a rationalisation:
  - too-small a sample returns TOO EARLY, never a conclusion;
  - the benchmark is NIFTY buy-and-hold over the SAME window (green in a bull
    market is not "proof");
  - a wild capture ratio flags INVESTIGATE (mechanics bug), not a view;
  - the options EDGE is never claimed proven (needs ~907 trades).

Offline — state file and live NIFTY are monkeypatched.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core import proof


def _write_state(tmp_path, monkeypatch, *, days_ago, nifty_start,
                 realised=0.0, equity_unrealised=0.0, capital=100000.0,
                 closed_condors=0):
    started = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    positions = [{"kind": "condor", "closed": "x"} for _ in range(closed_condors)]
    state = {"started": started, "capital": capital, "realised_pnl": realised,
             "equity_unrealised": equity_unrealised, "positions": positions,
             "plan_at_start": {"equity": {"nifty": nifty_start}}}
    p = tmp_path / "paper_book_state.json"
    p.write_text(json.dumps(state))
    monkeypatch.setattr(proof, "STATE", str(p))


def test_too_early_never_concludes(tmp_path, monkeypatch):
    _write_state(tmp_path, monkeypatch, days_ago=3, nifty_start=24000)
    monkeypatch.setattr(proof, "_nifty_now", lambda: 24500)
    s = proof.evaluate()
    assert s.verdict == "TOO EARLY"


def test_benchmark_is_same_window_nifty(tmp_path, monkeypatch):
    _write_state(tmp_path, monkeypatch, days_ago=30, nifty_start=24000,
                 equity_unrealised=1200.0)   # +1.2% book
    monkeypatch.setattr(proof, "_nifty_now", lambda: 24720)   # +3.0% NIFTY
    s = proof.evaluate()
    assert s.benchmark_return_pct == pytest.approx(3.0, abs=0.01)
    assert s.book_return_pct == pytest.approx(1.2, abs=0.01)
    assert s.excess_pct == pytest.approx(-1.8, abs=0.01)
    assert s.capture_ratio == pytest.approx(0.4, abs=0.01)


def test_sensible_capture_is_on_track(tmp_path, monkeypatch):
    # book captured 40% of a +3% NIFTY move -> within [0.10, 0.90] band
    _write_state(tmp_path, monkeypatch, days_ago=30, nifty_start=24000,
                 equity_unrealised=1200.0)
    monkeypatch.setattr(proof, "_nifty_now", lambda: 24720)
    s = proof.evaluate()
    assert "mechanics" in s.verdict.lower()


def test_wild_capture_flags_investigate(tmp_path, monkeypatch):
    # book +8% while NIFTY +3% -> capture 2.67, impossible for a half-weight
    # book -> mechanics bug, must flag INVESTIGATE not celebrate
    _write_state(tmp_path, monkeypatch, days_ago=30, nifty_start=24000,
                 equity_unrealised=8000.0)
    monkeypatch.setattr(proof, "_nifty_now", lambda: 24720)
    s = proof.evaluate()
    assert s.verdict == "INVESTIGATE"


def test_never_claims_options_edge_proven(tmp_path, monkeypatch):
    _write_state(tmp_path, monkeypatch, days_ago=40, nifty_start=24000,
                 equity_unrealised=1000.0, closed_condors=5)
    monkeypatch.setattr(proof, "_nifty_now", lambda: 24600)
    s = proof.evaluate()
    assert "NOT concludable" in s.detail
    assert "PROVEN" not in s.verdict.upper()


def test_no_book_is_honest(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "STATE", str(tmp_path / "missing.json"))
    s = proof.evaluate()
    assert s.verdict == "NO BOOK"


def test_checkpoint_advances_with_days(tmp_path, monkeypatch):
    monkeypatch.setattr(proof, "_nifty_now", lambda: 24000)
    for days, cp in [(5, "PRE-MECHANICS"), (25, "MECHANICS"),
                     (70, "CONSISTENCY"), (300, "DIRECTION")]:
        _write_state(tmp_path, monkeypatch, days_ago=days, nifty_start=24000)
        assert proof.evaluate().checkpoint == cp
