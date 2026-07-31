"""Tests for harvest.py — the daily premium-harvest driver.

The behaviours that matter:
  - flags an action ONLY when the overlay actually flips (a quiet day = no action);
  - persists overlay state so the flip is detected across runs;
  - never runs the edge-hunt scanner (posture guarantee).

Offline — book/plan calls are monkeypatched.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import harvest


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(harvest, "_STATE", str(tmp_path / "harvest_state.json"))
    # stub the heavy live calls
    class _Plan:
        def __init__(self, overlay):
            self._o = overlay
            self.equity = {"overlay_state": overlay, "overlay_equity_weight": 0.5,
                           "fundable": True, "units": 90, "deployed": 24948.0,
                           "cash_in_sleeve": 25052.0, "nifty": 24229, "ma200": 24788}
            self.options = {"fundable": True, "lots": 5,
                            "chosen": {"wing_points": 100},
                            "capital_at_risk": 26660.0,
                            "ladder_tranches": [2, 2, 1]}
        def to_dict(self):
            return {"equity": self.equity, "options": self.options}
    monkeypatch.setattr("core.swing_book.plan", lambda cap: _Plan(_tmp_state.overlay))
    monkeypatch.setattr("core.paper_book.mark", lambda: {"ok": True})
    monkeypatch.setattr("core.paper_book.status",
                        lambda: {"ok": True, "total_pnl": 0.0, "return_pct": 0.0,
                                 "days_running": 1, "confidence": "MECHANICS ONLY"})
    _tmp_state.overlay = "risk_off"
    return _tmp_state


def test_quiet_day_no_action(_tmp_state):
    harvest.harvest(100000)                 # first run: seeds state, no prior
    o = harvest.harvest(100000)             # second run, same overlay
    assert o["actions"] == []


def test_overlay_flip_raises_action(_tmp_state):
    harvest.harvest(100000)                 # seed risk_off
    _tmp_state.overlay = "risk_on"          # market flips
    o = harvest.harvest(100000)
    assert any("OVERLAY FLIP" in a for a in o["actions"])
    assert "risk_off -> risk_on" in o["actions"][0]


def test_state_persists_between_runs(_tmp_state):
    harvest.harvest(100000)
    assert os.path.exists(harvest._STATE)
    assert harvest._prev_overlay() == "risk_off"


def test_first_run_does_not_false_flag(_tmp_state):
    # No prior state file -> must NOT report a flip on the very first run.
    o = harvest.harvest(100000)
    assert o["actions"] == []


def test_condor_ladder_surfaced(_tmp_state):
    o = harvest.harvest(100000)
    assert o.get("condor_ladder") == [2, 2, 1]


def test_harvest_does_not_import_scanner(monkeypatch, _tmp_state):
    """Posture guarantee: the harvest path must never pull in scan_only_v2."""
    import sys as _s
    _s.modules.pop("scan_only_v2", None)
    harvest.harvest(100000)
    assert "scan_only_v2" not in _s.modules, "harvest must not run the edge scanner"
