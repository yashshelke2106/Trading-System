"""Evals for the journal-balance fix (2026-07-15): the paper journal must
collect BOTH directions every day, and strategy health must count only the
truly funded population (long + risk_on)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.strategy_health import compute_health
from swing_screen import journalable


def _cands():
    return [
        {"symbol": "AAA", "direction": "long", "signal": "rsi2_oversold"},
        {"symbol": "BBB", "direction": "short", "signal": "3_up_days"},
    ]


def test_journalable_includes_both_directions_any_regime():
    # The learner needs data on BOTH sides regardless of regime — journaling
    # only the regime side starved the journal of longs for weeks (22/22
    # short rows) and read as system bias.
    for regime in ("risk_on", "risk_off"):
        dirs = {c["direction"] for c in journalable(_cands(), regime)}
        assert dirs == {"long", "short"}, f"regime={regime} lost a side: {dirs}"


def _row(direction, ret, regime=None):
    r = {"status": "resolved", "direction": direction, "ret_net": ret}
    if regime is not None:
        r["regime"] = regime
    return r


def test_health_funded_side_excludes_riskoff_longs():
    # Funded population = longs taken under risk_on (pre-registered intent).
    # Bench longs journaled during risk_off must not contaminate the window.
    rows = ([_row("long", 0.02, "risk_on")] * 30
            + [_row("long", -0.05, "risk_off")] * 40)
    h = compute_health(rows)
    assert h["funded_side"]["n_resolved"] == 30
    assert h["funded_side"]["rolling_avg_bp"] > 0


def test_health_legacy_rows_without_regime_count_as_funded():
    # Old journal rows predate the regime field — they were funded-era longs.
    rows = [_row("long", 0.02)] * 35
    h = compute_health(rows)
    assert h["funded_side"]["n_resolved"] == 35
