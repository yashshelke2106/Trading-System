"""Tests for core/midcap_universe.py — endogenous point-in-time universe.

Uses a synthetic bhavcopy archive (deterministic) so it does not depend on the
real archive's build state.
"""

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from core.midcap_universe import build_universe
from core.universe import FO_UNIVERSE

# a real F&O name we expect to be excluded
_FO_NAME = FO_UNIVERSE[0]


def _write_symbol(archive_dir, sym, close, volume, n=70, start="2026-01-01",
                  volume_tail=None):
    """Write a synthetic per-symbol parquet. If volume_tail given, the last
    n//2 bars use volume_tail (to test point-in-time turnover changes)."""
    d = os.path.join(archive_dir, "symbols")
    os.makedirs(d, exist_ok=True)
    dates = pd.date_range(start, periods=n, freq="D")
    vols = [volume] * n
    if volume_tail is not None:
        vols = [volume] * (n // 2) + [volume_tail] * (n - n // 2)
    df = pd.DataFrame({"date": dates, "close": [close] * n, "volume": vols})
    df.to_parquet(os.path.join(d, f"{sym}.parquet"))


def _archive(tmp_path):
    a = str(tmp_path / "arch")
    # turnover_Cr = close*volume/1e7
    _write_symbol(a, _FO_NAME, close=100, volume=10_000_000)   # 100 Cr, F&O → excluded
    _write_symbol(a, "MIDA", close=100, volume=5_000_000)      # 50 Cr
    _write_symbol(a, "MIDB", close=100, volume=3_000_000)      # 30 Cr
    _write_symbol(a, "MIDC", close=100, volume=1_000_000)      # 10 Cr
    _write_symbol(a, "TINY", close=100, volume=100_000)        # 1 Cr → below min
    return a


def test_excludes_fo_and_ranks_by_turnover(tmp_path):
    a = _archive(tmp_path)
    snap = build_universe("2026-03-01", archive_dir=a, band_lo=1, band_hi=150,
                          min_turnover_cr=5.0)
    assert _FO_NAME not in snap.symbols          # F&O largecap excluded
    assert "TINY" not in snap.symbols            # below min turnover
    assert snap.symbols == ["MIDA", "MIDB", "MIDC"]  # ranked desc by turnover


def test_band_slice(tmp_path):
    a = _archive(tmp_path)
    snap = build_universe("2026-03-01", archive_dir=a, band_lo=2, band_hi=2,
                          min_turnover_cr=5.0)
    assert snap.symbols == ["MIDB"]              # the 2nd-ranked name only


def test_min_turnover_filter(tmp_path):
    a = _archive(tmp_path)
    snap = build_universe("2026-03-01", archive_dir=a, band_lo=1, band_hi=150,
                          min_turnover_cr=20.0)   # only MIDA(50) & MIDB(30) clear
    assert snap.symbols == ["MIDA", "MIDB"]


def test_point_in_time_no_lookahead(tmp_path):
    a = str(tmp_path / "arch2")
    # NEWLIQ is illiquid early (1 Cr), liquid late (40 Cr)
    _write_symbol(a, "NEWLIQ", close=100, volume=100_000, volume_tail=4_000_000)
    _write_symbol(a, "MIDA", close=100, volume=5_000_000)
    # asof early: NEWLIQ's later liquidity must NOT count → excluded by min
    early = build_universe("2026-01-20", archive_dir=a, min_turnover_cr=5.0)
    # asof late: NEWLIQ now liquid → included
    late = build_universe("2026-03-10", archive_dir=a, min_turnover_cr=5.0)
    assert "NEWLIQ" not in early.symbols
    assert "NEWLIQ" in late.symbols


def test_empty_archive(tmp_path):
    snap = build_universe("2026-03-01", archive_dir=str(tmp_path / "nope"))
    assert snap.symbols == []
    assert snap.n_candidates == 0
