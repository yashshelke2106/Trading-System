"""Exit style C (winners ride until close crosses 5DMA) — resolver tests."""
import os, sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from swing_tracker import _resolve


def _bars(rows):
    """rows: list of (o,h,l,c) starting the day AFTER signal; prepends 5
    warmup bars at 100 so the 5-DMA is defined."""
    warm = [(100, 101, 99, 100)] * 5
    data = warm + rows
    idx = pd.bdate_range("2026-01-01", periods=len(data))
    return pd.DataFrame(data, columns=["open", "high", "low", "close"], index=idx)


def _row(**kw):
    base = {"symbol": "T", "direction": "long", "signal": "s",
            "signal_date": "2026-01-07",   # after the 5 warmup bars
            "close": 100.0, "target": 110.0, "stop": 95.0, "exit_style": "C"}
    base.update(kw)
    return base


def test_c_winner_rides_then_exits_on_momentum_break():
    # rallies well past the old fixed target, stays above 5DMA, then one
    # close below 5DMA while in profit -> exit NEXT open (MOM_EXIT)
    rows = [(100, 101, 99, 101), (102, 106, 101, 105), (106, 112, 105, 111),
            (112, 118, 111, 117), (117, 119, 115, 118),
            (117, 117, 108, 109),                 # close 109 < 5DMA(~112) & in profit
            (108, 109, 106, 107)]                 # exit here at open 108
    res = _resolve(_row(), _bars(rows))
    assert res["outcome"] == "MOM_EXIT"
    assert res["exit_px"] == 108.0
    assert res["ret_gross"] > 0.07                # rode past the old 10% cap? 8%
    # crucially: NOT capped at the fixed target
    assert res["outcome"] != "TARGET_HIT"


def test_c_loser_still_dies_at_stop():
    rows = [(100, 101, 99, 100), (99, 100, 94, 95)]   # low 94 <= stop 95
    res = _resolve(_row(), _bars(rows))
    assert res["outcome"] == "SL_HIT"
    assert res["exit_px"] == 95.0


def test_c_no_exit_below_entry_even_under_5dma():
    # under water AND below 5DMA -> no momentum exit (winners-only rule);
    # survives until stop or time cap
    rows = [(100, 101, 96, 97)] * 3
    res = _resolve(_row(), _bars(rows))
    assert res is None or res["outcome"] in ("SL_HIT", "TIME_EXIT")
    if res is not None:
        assert res["outcome"] != "MOM_EXIT"


def test_legacy_rows_keep_fixed_target():
    rows = [(100, 101, 99, 100), (101, 111, 100, 110)]  # high 111 >= target 110
    res = _resolve(_row(exit_style=None), _bars(rows))
    assert res["outcome"] == "TARGET_HIT"
    assert res["exit_px"] == 110.0
