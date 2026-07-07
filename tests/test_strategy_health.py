"""Pre-registered decay-monitor rules — transition and persistence tests."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.strategy_health import (
    BACKTEST_BASELINE_BP, MIN_EVAL, RETIRE_PF, REINSTATE_PF, WINDOW,
    compute_health,
)


def _rows(rets, direction="long"):
    return [{"status": "resolved", "direction": direction, "ret_net": r}
            for r in rets]


def test_collecting_below_min_eval():
    h = compute_health(_rows([0.01] * (MIN_EVAL - 1)))
    assert h["status"] == "COLLECTING"


def test_healthy_on_positive_window():
    # 40 wins +2%, 20 losses -1% -> PF = 0.8/0.2 = 4
    h = compute_health(_rows([0.02] * 40 + [-0.01] * 20))
    assert h["status"] == "HEALTHY"
    assert h["funded_side"]["rolling_pf"] == 4.0


def test_warn_between_retire_and_one():
    # PF ~0.95: 19 wins +1%, 20 losses -1% -> 0.19/0.20 = 0.95 (n=39 >= MIN_EVAL,
    # window not full so retire cannot fire)
    h = compute_health(_rows([0.01] * 19 + [-0.01] * 20))
    assert h["status"] == "WARN"


def test_retire_needs_full_window():
    # PF 0.5 but only 40 trades -> WARN, not RETIRED (window must be full)
    h = compute_health(_rows([0.01] * 10 + [-0.01] * 30))
    assert h["status"] == "WARN"
    # full 60-trade window at PF 0.5 -> RETIRED
    h = compute_health(_rows([0.01] * 20 + [-0.01] * 40))
    assert h["funded_side"]["window_n"] == WINDOW
    assert h["status"] == "RETIRED"


def test_reinstate_hysteresis():
    # while RETIRED, PF 1.02 (>= warn bar but < reinstate bar) stays RETIRED
    rets = [0.0102] * 30 + [-0.01] * 30          # PF ~1.02
    h = compute_health(_rows(rets), prev_status="RETIRED")
    assert h["status"] == "RETIRED"
    # PF >= 1.05 on full window -> reinstated HEALTHY
    rets = [0.0106] * 30 + [-0.01] * 30          # PF ~1.06
    h = compute_health(_rows(rets), prev_status="RETIRED")
    assert h["status"] == "HEALTHY"
    assert any("REINSTATED" in r for r in h["reasons"])


def test_persistence_math():
    # avg +18.1bp live == backtest baseline -> persistence 1.0
    base = BACKTEST_BASELINE_BP["long"] / 1e4
    h = compute_health(_rows([base] * 60))
    assert abs(h["funded_side"]["persistence"] - 1.0) < 0.01


def test_shorts_never_drive_status():
    # catastrophic shorts + healthy longs -> status from longs only
    rows = _rows([0.02] * 40 + [-0.01] * 20) + _rows([-0.05] * 100, "short")
    h = compute_health(rows)
    assert h["status"] == "HEALTHY"
    assert h["paper_bench_short"]["n_resolved"] == 100
