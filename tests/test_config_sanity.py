"""Config/invariant guards — cheap tests that catch dangerous regressions
(PAPER_TRADE flipped, ranker weights drifting, audit safety flags removed)."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.trade_ranker import TradeRanker


def test_paper_trade_is_on():
    assert config.PAPER_TRADE is True


def test_ranker_weights_sum_to_one():
    w = TradeRanker().weights
    assert abs(sum(w.values()) - 1.0) < 1e-6


def test_lot_sizes_positive_and_nifty_current():
    assert all(v > 0 for v in config.NSE_LOT_SIZES.values())
    assert config.NSE_LOT_SIZES["NIFTY"] == 75   # 2024+ value, not the stale 50


def test_risk_config_sane():
    rc = config.RISK_CONFIG
    assert 0 < rc["max_risk_per_trade"] < 0.05
    assert 0 < rc["max_daily_loss"] <= 0.10
    assert rc["max_per_sector"] >= 1
    assert rc["min_risk_reward"] >= 1.0


def test_audit_safety_flags_present():
    assert config.LEARNING_ENABLED is False          # in-session overfitting frozen
    assert hasattr(config, "GATES_FAIL_CLOSED")
    assert hasattr(config, "REQUIRE_EARNINGS_DATA")
    assert hasattr(config, "ISWING_DECISION_TIME")
