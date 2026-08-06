"""Tests for the market-capture stack.

Covers core/intraday_capture.py (point-in-time merge semantics),
core/market_state.py (trend classification and the ADX gate), and the two
silent-failure regressions fixed alongside them:

  * core/market_bias.py fabricated random bars on fetch failure and scored
    them as a confident directional bias.
  * core/sector_rotation.py routed to a dead Dhan endpoint, making
    check_sector_alignment() fail open for every symbol.

All tests are offline — no test here may touch the network.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from core import intraday_capture as ic
from core import market_state as ms


# ── Fixtures ────────────────────────────────────────────────────────────────

def _bars(n: int, start: str = "2026-01-01 09:15", freq: str = "5min",
          base: float = 100.0, drift: float = 0.0) -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=n, freq=freq, tz=ic.IST)
    close = base + np.arange(n) * drift
    return pd.DataFrame(
        {
            "open": close, "high": close + 1.0,
            "low": close - 1.0, "close": close,
            "volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def _daily(n: int, drift: float, base: float = 100.0,
           vol_up: float = 1000.0, vol_dn: float = 1000.0,
           noise: float = 0.006, seed: int = 7) -> pd.DataFrame:
    """Daily OHLCV: constant drift plus deterministic noise.

    The noise is not decoration. A perfectly smooth ramp has no confirmed
    swing highs/lows and no down-days at all, so price_structure and
    volume_confirm correctly abstain on it — which is not the tape any real
    classification runs against. `seed` keeps it reproducible.
    """
    idx = pd.date_range(end="2026-07-24", periods=n, freq="B")
    rng = np.random.default_rng(seed)
    steps = np.full(n, drift) + rng.normal(0.0, noise, n)
    close = base * np.exp(np.cumsum(steps))
    ret = np.diff(close, prepend=close[0])
    vol = np.where(ret > 0, vol_up, vol_dn)
    return pd.DataFrame(
        {
            "open": close, "high": close * 1.01,
            "low": close * 0.99, "close": close, "volume": vol,
        },
        index=idx,
    )


def _chop(n: int = 200, base: float = 100.0, phi: float = 0.3,
          sigma: float = 1.0, seed: int = 5) -> pd.DataFrame:
    """Mean-reverting AR(1) chop — frequent reversals, no directional persistence.

    Note what does NOT work as a "sideways" fixture, because both were tried:
      * a driftless random walk wanders and often reads as trending;
      * a smooth sine is locally a very clean trend (ADX ~35-55), since ADX
        measures directional persistence, not boundedness.
    Only a low-persistence process actually produces chop.
    """
    idx = pd.date_range(end="2026-07-24", periods=n, freq="B")
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    e = rng.normal(0.0, sigma, n)
    for t in range(1, n):
        x[t] = phi * x[t - 1] + e[t]
    close = base + x
    prev = np.roll(close, 1)
    r = 0.8 * sigma
    return pd.DataFrame(
        {
            "open": prev,
            "high": np.maximum(close, prev) + r * rng.uniform(0.4, 1.0, n),
            "low": np.minimum(close, prev) - r * rng.uniform(0.4, 1.0, n),
            "close": close,
            "volume": np.full(n, 1000.0),
        },
        index=idx,
    )


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "CACHE_DIR", str(tmp_path))
    return tmp_path


# ── intraday_capture: merge semantics ───────────────────────────────────────

def test_merge_creates_archive_when_absent(tmp_cache):
    st = ic.merge_symbol("TESTSYM", _bars(10))
    assert st["added"] == 10 and st["kept"] == 0 and st["total"] == 10
    assert len(ic.read_symbol("TESTSYM")) == 10


def test_merge_is_idempotent(tmp_cache):
    fresh = _bars(10)
    ic.merge_symbol("TESTSYM", fresh)
    st = ic.merge_symbol("TESTSYM", fresh)
    # Re-running adds nothing and keeps the archive the same size.
    assert st["added"] == 0
    assert st["kept"] == 10
    assert st["total"] == 10


def test_merge_appends_only_new_bars(tmp_cache):
    ic.merge_symbol("TESTSYM", _bars(10))
    # Overlapping window that extends 5 bars further.
    st = ic.merge_symbol("TESTSYM", _bars(15))
    assert st["added"] == 5
    assert st["kept"] == 10
    assert st["total"] == 15


def test_existing_bar_wins_conflict(tmp_cache):
    """Point-in-time rule: the bar already on disk is never overwritten."""
    ic.merge_symbol("TESTSYM", _bars(10, base=100.0))
    restated = _bars(10, base=50.0)          # e.g. a post-split restatement
    st = ic.merge_symbol("TESTSYM", restated)

    assert st["conflicts"] == 10, "restated bars must be counted as conflicts"
    stored = ic.read_symbol("TESTSYM")
    assert float(stored["close"].iloc[0]) == pytest.approx(100.0), \
        "original capture must survive the restatement"


def test_conflict_count_zero_when_values_agree(tmp_cache):
    ic.merge_symbol("TESTSYM", _bars(10))
    st = ic.merge_symbol("TESTSYM", _bars(10))
    assert st["conflicts"] == 0


def test_lookback_is_capped(tmp_cache, monkeypatch):
    """Requesting more than the yfinance 5m limit must not silently over-ask."""
    seen = {}

    class _FakeTicker:
        def __init__(self, t): pass
        def history(self, period, interval):
            seen["period"] = period
            return pd.DataFrame()

    monkeypatch.setitem(sys.modules, "yfinance", type("m", (), {"Ticker": _FakeTicker}))
    ic.fetch_symbol("TESTSYM", days=9999)
    assert seen["period"] == f"{ic.MAX_LOOKBACK_DAYS}d"


def test_yf_ticker_uses_corporate_action_map():
    # TATAMOTORS must resolve to the post-demerger entity, not TATAMOTORS.NS.
    assert ic.yf_ticker("TATAMOTORS") == "TMCV.NS"
    assert ic.yf_ticker("RELIANCE") == "RELIANCE.NS"


# ── market_state: classification ────────────────────────────────────────────

def test_uptrend_classified_up():
    df = _daily(200, drift=0.004, vol_up=2000.0, vol_dn=1000.0)
    st = ms.classify("X", df=df, with_context=False)
    assert st.direction == "up"
    assert st.strength > 0.5


def test_downtrend_classified_down():
    df = _daily(200, drift=-0.004, vol_up=1000.0, vol_dn=2000.0)
    st = ms.classify("X", df=df, with_context=False)
    assert st.direction == "down"


@pytest.mark.parametrize("seed", [5, 11, 17, 23])
def test_single_factor_never_names_a_direction(seed):
    """The guard that matters on noise: one lone factor cannot call a trend.

    On mean-reverting chop, ADX(14) straddles the trend floor, so adx_di
    alone used to return a confident "up" on roughly half of seeds. Whatever
    the label, it must now rest on >= MIN_AGREEING_FACTORS.
    """
    st = ms.classify("X", df=_chop(seed=seed), with_context=False)
    if st.direction in ("up", "down"):
        sign = 1 if st.direction == "up" else -1
        agreeing = sum(1 for f in st.factors if f.available and f.vote == sign)
        assert agreeing >= ms.MIN_AGREEING_FACTORS, (
            f"{st.direction} called on {agreeing} factor(s): "
            + " | ".join(f"{f.name}:{f.vote:+d}" for f in st.factors)
        )


def test_thin_evidence_downgrades_to_sideways(monkeypatch):
    """A lone +1 with everything else abstaining must not become an uptrend."""
    # Strongly trending bars so the ADX gate is cleared and the vote logic —
    # not the gate — decides the outcome.
    df = _daily(200, drift=0.006, noise=0.004)

    monkeypatch.setattr(ms, "_f_price_structure",
                        lambda d: ms.FactorReading("price_structure", 0, "flat"))
    monkeypatch.setattr(ms, "_f_ma_alignment",
                        lambda d: ms.FactorReading("ma_alignment", 0, "flat"))
    monkeypatch.setattr(ms, "_f_volume_confirm",
                        lambda d, **k: ms.FactorReading("volume_confirm", 0, "balanced"))
    monkeypatch.setattr(ms, "_f_adx_di",
                        lambda a, p, m: ms.FactorReading("adx_di", +1, "+DI > -DI"))

    st = ms.classify("X", df=df, with_context=False)
    assert st.adx >= ms.ADX_TREND_FLOOR, "fixture must clear the ADX gate"
    assert st.direction == "sideways"
    assert "need" in st.note


def test_two_agreeing_factors_do_call_a_direction(monkeypatch):
    """The corroboration rule must not deadlock the classifier entirely."""
    df = _daily(200, drift=0.006, noise=0.004)

    monkeypatch.setattr(ms, "_f_price_structure",
                        lambda d: ms.FactorReading("price_structure", +1, "HH+HL"))
    monkeypatch.setattr(ms, "_f_ma_alignment",
                        lambda d: ms.FactorReading("ma_alignment", 0, "flat"))
    monkeypatch.setattr(ms, "_f_volume_confirm",
                        lambda d, **k: ms.FactorReading("volume_confirm", 0, "balanced"))
    monkeypatch.setattr(ms, "_f_adx_di",
                        lambda a, p, m: ms.FactorReading("adx_di", +1, "+DI > -DI"))

    st = ms.classify("X", df=df, with_context=False)
    assert st.direction == "up"


def test_adx_gate_overrides_aligned_factors():
    """Below the ADX floor the call is sideways even if factors point one way."""
    df = _daily(200, drift=0.004, vol_up=2000.0, vol_dn=1000.0)
    monk_floor = ms.ADX_TREND_FLOOR
    try:
        ms.ADX_TREND_FLOOR = 1e9      # nothing can clear this
        st = ms.classify("X", df=df, with_context=False)
        assert st.direction == "sideways"
        assert "below" in st.note.lower()
    finally:
        ms.ADX_TREND_FLOOR = monk_floor


def test_insufficient_history_is_unknown_not_guessed():
    st = ms.classify("X", df=_daily(10, drift=0.01), with_context=False)
    assert st.direction == "unknown"
    assert st.strength == 0.0


def test_unavailable_factors_always_reported():
    """A thin evidence base must never look like a complete one."""
    st = ms.classify("X", df=_daily(200, drift=0.004), with_context=False)
    for key in ("order_flow", "bid_ask_spread", "book_depth", "news_sentiment"):
        assert key in st.unavailable


def test_strength_counts_only_available_factors():
    df = _daily(200, drift=0.004, vol_up=2000.0, vol_dn=1000.0)
    st = ms.classify("X", df=df, with_context=False)
    usable = [f for f in st.factors if f.available]
    assert 0.0 <= st.strength <= 1.0
    assert len(usable) > 0


def test_adx_di_matches_regime_filter_on_shared_input():
    """The ADX half must agree with the existing core.regime_filter helper."""
    from core.regime_filter import _adx as regime_adx
    df = _daily(200, drift=0.003)
    mine, _, _ = ms._adx_di(df, n=14)
    theirs = regime_adx(df, n=14)
    assert mine == pytest.approx(theirs, rel=1e-9)


# ── Regression: fabricated market bias ──────────────────────────────────────

def test_market_bias_forces_neutral_on_synthetic_bars():
    """Regression: a directional bias must never be read off synthetic data."""
    from core.market_bias import MarketBiasEngine, MarketBias

    eng = MarketBiasEngine()
    synth = eng.get_index_data.__wrapped__ if hasattr(eng.get_index_data, "__wrapped__") \
        else None  # not wrapped; call directly below

    # Build the synthetic frame the fallback would produce.
    df = _daily(50, drift=0.01)
    df.attrs["synthetic"] = True

    ctx = eng.get_market_bias(nifty_data=df, banknifty_data=df)
    assert ctx.synthetic is True
    assert ctx.bias == MarketBias.NEUTRAL, \
        "synthetic bars must not produce a directional bias"
    assert ctx.strength == 0.0


def test_market_bias_real_bars_still_classify():
    """The neutral-forcing guard must not disable the normal path."""
    from core.market_bias import MarketBiasEngine, MarketBias

    up = _daily(50, drift=0.01)      # no synthetic attr => real
    ctx = MarketBiasEngine().get_market_bias(nifty_data=up, banknifty_data=up)
    assert ctx.synthetic is False
    assert ctx.bias != MarketBias.NEUTRAL


def test_market_state_ignores_synthetic_bias(monkeypatch):
    """market_state must mark the market factor unavailable, not count it."""
    class _Ctx:
        synthetic = True
        bias = None

    class _Eng:
        def get_market_bias(self): return _Ctx()

    import core.market_bias as mb
    monkeypatch.setattr(mb, "MarketBiasEngine", _Eng)
    f = ms._f_market()
    assert f.available is False
    assert f.vote == 0


# ── Regression: sector gate failing open ────────────────────────────────────

def test_sector_gate_blocks_counter_sector_long(monkeypatch):
    import core.sector_rotation as sr
    monkeypatch.setattr(sr, "sector_bias", lambda s: ("bearish", {"sector": "^X"}))
    ok, info = sr.check_sector_alignment("ANY", "long")
    assert ok is False
    assert info["reason"] == "long_vs_bearish_sector"


def test_sector_fetch_falls_back_to_yfinance(monkeypatch):
    """Regression: Dhan is expired, so the yfinance fallback must engage."""
    import core.sector_rotation as sr
    sr._SECTOR_CACHE.clear()

    frame = _daily(60, drift=0.005).rename(
        columns={"open": "Open", "high": "High", "low": "Low",
                 "close": "Close", "volume": "Volume"}
    )

    class _FakeTicker:
        def __init__(self, t): pass
        def history(self, period, interval): return frame

    monkeypatch.setitem(sys.modules, "yfinance", type("m", (), {"Ticker": _FakeTicker}))
    # Force the Dhan leg to fail the way an expired subscription does.
    import core.api_dhan as ad
    monkeypatch.setattr(ad, "dhan_daily",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("401")),
                        raising=False)

    df = sr._fetch_sector_daily("^TESTIDX")
    assert df is not None, "yfinance fallback must supply bars when Dhan 401s"
    assert "close" in df.columns
    sr._SECTOR_CACHE.clear()


# ── Regression: bars captured while still forming ───────────────────────────

def test_forming_bars_are_dropped():
    """A bar whose interval has not closed must never reach the archive.

    Regression: a run at 11:33 stored the 11:30 bar with partial volume, and
    the existing-wins merge rule then froze that incomplete bar permanently.
    """
    now = pd.Timestamp("2026-07-24 11:33:00", tz=ic.IST)
    idx = pd.DatetimeIndex(
        [pd.Timestamp(f"2026-07-24 {t}", tz=ic.IST)
         for t in ("11:20", "11:25", "11:30")]
    )
    df = pd.DataFrame(
        {"open": [1.0] * 3, "high": [1.0] * 3, "low": [1.0] * 3,
         "close": [1.0] * 3, "volume": [1.0] * 3},
        index=idx,
    )
    kept = ic.drop_forming_bars(df, now=now)
    assert pd.Timestamp("2026-07-24 11:30", tz=ic.IST) not in kept.index, \
        "the 11:30 bar is still forming at 11:33 and must be dropped"
    assert pd.Timestamp("2026-07-24 11:25", tz=ic.IST) in kept.index


def test_completed_bar_at_boundary_is_kept():
    """A bar closed exactly BAR_SECONDS+GRACE ago is complete and must survive."""
    now = pd.Timestamp("2026-07-24 11:36:00", tz=ic.IST)
    idx = pd.DatetimeIndex([pd.Timestamp("2026-07-24 11:30", tz=ic.IST)])
    df = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
        index=idx,
    )
    assert len(ic.drop_forming_bars(df, now=now)) == 1


def test_repair_overwrites_recent_bars(tmp_cache, monkeypatch):
    """repair_symbol must let FRESH win, unlike the normal merge."""
    idx = pd.date_range(end=pd.Timestamp.now(tz=ic.IST).normalize(),
                        periods=3, freq="5min", tz=ic.IST)
    stale = pd.DataFrame(
        {"open": [1.0] * 3, "high": [1.0] * 3, "low": [1.0] * 3,
         "close": [1.0] * 3, "volume": [10.0] * 3},
        index=idx,
    )
    stale.to_parquet(ic._path("TESTSYM"))

    fresh = stale.copy()
    fresh["close"] = 2.0
    fresh["volume"] = 99.0
    monkeypatch.setattr(ic, "fetch_symbol", lambda s, days=60: fresh)

    r = ic.repair_symbol("TESTSYM", days=3)
    assert r["repaired"] == 3
    assert float(ic.read_symbol("TESTSYM")["close"].iloc[0]) == pytest.approx(2.0)


# ── Regression: market_bias uses live data before synthetic ──────────────────

def test_market_bias_falls_to_real_data_not_synthetic(monkeypatch):
    """Dhan failure must reach the yfinance+live tier, not synthetic GBM.

    Regression for the fabricated-bias bug: previously get_index_data went
    Dhan -> synthetic with no real fallback, so an expired Dhan sub produced a
    confident bias from noise.
    """
    from core.market_bias import MarketBiasEngine
    import pandas as pd
    import numpy as np

    eng = MarketBiasEngine()
    eng.cache.clear()

    # Real daily frame (what the yfinance tier returns).
    idx = pd.date_range(end="2026-07-29", periods=250, freq="B")
    close = np.linspace(23000, 24250, 250)
    frame = pd.DataFrame({"open": close, "high": close * 1.005,
                          "low": close * 0.995, "close": close,
                          "volume": np.full(250, 1e6),
                          "date": idx})
    # Force the Dhan tier to fail. Without this the test only passed while Dhan
    # was broken in the environment: a WORKING Dhan short-circuits at the first
    # tier and the fallback this test exists to verify never runs.
    from core.api_dhan import dhan_api
    monkeypatch.setattr(dhan_api, "get_historical_data", lambda *a, **k: None)
    monkeypatch.setattr(eng, "_yf_index_frame", lambda s, d: frame.tail(d))
    # Live patch available and consistent.
    import core.live_quotes as lq
    monkeypatch.setattr(lq, "get_quote",
                        lambda s: lq.Quote(s.upper(), 24249.0, "nse_live"))

    df = eng.get_index_data("NIFTY", 250)
    assert df.attrs.get("synthetic", False) is False
    assert df.attrs.get("live_patched", False) is True
    assert float(df["close"].iloc[-1]) == pytest.approx(24249.0)


def test_live_patch_rejects_absurd_deviation(monkeypatch):
    """A live value >10% off history is a source mismatch, not a move —
    it must NOT overwrite the series."""
    from core.market_bias import MarketBiasEngine
    import pandas as pd
    import numpy as np

    eng = MarketBiasEngine()
    idx = pd.date_range(end="2026-07-29", periods=60, freq="B")
    close = np.full(60, 24000.0)
    df = pd.DataFrame({"open": close, "high": close, "low": close,
                       "close": close, "volume": np.full(60, 1e6), "date": idx})
    import core.live_quotes as lq
    monkeypatch.setattr(lq, "get_quote",
                        lambda s: lq.Quote(s.upper(), 5000.0, "nse_live"))  # absurd
    eng._patch_live_last("NIFTY", df)
    assert float(df["close"].iloc[-1]) == 24000.0          # unchanged
    assert not df.attrs.get("live_patched", False)


# ── paper_book entry logging ─────────────────────────────────────────────────

def test_paper_enter_equity_reduces_cash(tmp_path, monkeypatch):
    import core.paper_book as pb
    monkeypatch.setattr(pb, "STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(pb, "LEDGER_PATH", str(tmp_path / "l.jsonl"))
    monkeypatch.setattr("core.swing_book.plan",
                        lambda cap: type("P", (), {"to_dict": lambda self: {}})())
    pb._save({"started": pb._now(), "capital": 100000.0, "cash": 100000.0,
              "positions": [], "realised_pnl": 0.0})
    r = pb.enter("equity", 90, 277.0)
    assert r["ok"]
    assert pb._load()["cash"] == pytest.approx(100000.0 - 90 * 277.0)


def test_paper_enter_condor_adds_credit(tmp_path, monkeypatch):
    import core.paper_book as pb
    monkeypatch.setattr(pb, "STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(pb, "LEDGER_PATH", str(tmp_path / "l.jsonl"))
    pb._save({"started": pb._now(), "capital": 100000.0, "cash": 50000.0,
              "positions": [], "realised_pnl": 0.0})
    r = pb.enter("condor", 5, 2175.0,
                 {"max_loss_per_lot": 5329.0, "capital_at_risk": 26645.0})
    assert r["ok"]
    assert pb._load()["cash"] == pytest.approx(50000.0 + 5 * 2175.0)


def test_paper_enter_equity_rejects_overspend(tmp_path, monkeypatch):
    import core.paper_book as pb
    monkeypatch.setattr(pb, "STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(pb, "LEDGER_PATH", str(tmp_path / "l.jsonl"))
    pb._save({"started": pb._now(), "capital": 100000.0, "cash": 1000.0,
              "positions": [], "realised_pnl": 0.0})
    r = pb.enter("equity", 90, 277.0)      # needs ~25k, have 1k
    assert not r["ok"] and "insufficient" in r["reason"]


def test_enter_from_plan_no_double_equity(tmp_path, monkeypatch):
    import core.paper_book as pb

    class _Plan:
        equity = {"fundable": True, "units": 90, "price": 277.0,
                  "overlay_state": "risk_off"}
        options = {"fundable": False}
        def to_dict(self): return {}
    monkeypatch.setattr(pb, "STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setattr(pb, "LEDGER_PATH", str(tmp_path / "l.jsonl"))
    monkeypatch.setattr("core.swing_book.plan", lambda cap: _Plan())
    pb._save({"started": pb._now(), "capital": 100000.0, "cash": 100000.0,
              "positions": [], "realised_pnl": 0.0})
    pb.enter_from_plan(equity=True)
    pb.enter_from_plan(equity=True)         # second call must NOT re-enter
    eq = [x for x in pb._load()["positions"] if x["kind"] == "equity"]
    assert len(eq) == 1
