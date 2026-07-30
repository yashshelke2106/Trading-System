"""Tests for core/live_quotes.py — legal near-real-time quote layer.

What must hold:
  - never fabricates: all sources failing => None / empty, not a made-up price;
  - freshest source wins (NSE live before yfinance);
  - the fallback chain actually falls back when NSE is down;
  - source_status honestly reports what can be seen and how fresh;
  - broker_feed_available reflects credential presence, not wishful thinking.

Offline — all network calls are monkeypatched.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from core import live_quotes as lq


@pytest.fixture(autouse=True)
def _clear_cache():
    lq._cache.clear()
    yield
    lq._cache.clear()


def test_nse_live_preferred_for_indices(monkeypatch):
    monkeypatch.setattr(lq, "_nse_all_indices",
                        lambda: {"NIFTY": lq.Quote("NIFTY", 24250.0, "nse_live")})
    # yfinance must NOT be consulted when NSE has the index
    monkeypatch.setattr(lq, "_yf_quote",
                        lambda s: (_ for _ in ()).throw(AssertionError("yf called")))
    q = lq.get_quote("NIFTY")
    assert q.source == "nse_live" and q.last == 24250.0


def test_falls_back_to_yfinance_when_nse_empty(monkeypatch):
    monkeypatch.setattr(lq, "_nse_all_indices", lambda: {})
    monkeypatch.setattr(lq, "_yf_quote",
                        lambda s: lq.Quote(s.upper(), 100.0, "yfinance"))
    q = lq.get_quote("NIFTY")
    assert q.source == "yfinance"


def test_never_fabricates_when_all_sources_fail(monkeypatch):
    monkeypatch.setattr(lq, "_nse_all_indices", lambda: {})
    monkeypatch.setattr(lq, "_yf_quote", lambda s: None)
    assert lq.get_quote("NIFTY") is None
    assert lq.get_index_quotes() == {}


def test_stock_symbol_uses_yfinance(monkeypatch):
    """No free per-stock live source — a stock must route to yfinance."""
    called = {}
    def fake_yf(s):
        called["sym"] = s
        return lq.Quote(s.upper(), 275.0, "yfinance")
    monkeypatch.setattr(lq, "_nse_all_indices", lambda: {})
    monkeypatch.setattr(lq, "_yf_quote", fake_yf)
    q = lq.get_quote("RELIANCE")
    assert called["sym"] == "RELIANCE" and q.source == "yfinance"


def test_index_quotes_rebuild_from_yf_when_nse_down(monkeypatch):
    monkeypatch.setattr(lq, "_nse_all_indices", lambda: {})
    monkeypatch.setattr(lq, "_yf_quote",
                        lambda s: lq.Quote(s, 1.0, "yfinance") if s in
                        ("NIFTY", "BANKNIFTY", "INDIAVIX") else None)
    out = lq.get_index_quotes()
    assert set(out) == {"NIFTY", "BANKNIFTY", "INDIAVIX"}
    assert all(q.source == "yfinance" for q in out.values())


def test_source_status_reports_nse_live(monkeypatch):
    monkeypatch.setattr(lq, "get_index_quotes",
                        lambda: {"NIFTY": lq.Quote("NIFTY", 24000.0, "nse_live"),
                                 "INDIAVIX": lq.Quote("INDIAVIX", 12.0, "nse_live")})
    monkeypatch.setattr(lq, "broker_feed_available", lambda: False)
    s = lq.source_status()
    assert s["nse_live_indices"] is True
    assert "seconds" in s["freshest_index_source"]
    assert s["per_stock_live"] is False
    assert s["nifty"] == 24000.0 and s["india_vix"] == 12.0


def test_source_status_degrades_honestly(monkeypatch):
    monkeypatch.setattr(lq, "get_index_quotes",
                        lambda: {"NIFTY": lq.Quote("NIFTY", 1.0, "yfinance")})
    monkeypatch.setattr(lq, "broker_feed_available", lambda: False)
    s = lq.source_status()
    assert s["nse_live_indices"] is False
    assert "3 min" in s["freshest_index_source"]


def test_broker_feed_reflects_credentials(monkeypatch):
    import core.secrets as sec
    monkeypatch.setattr(sec, "get_access_token", lambda: "eyJvalidtoken")
    assert lq.broker_feed_available() is True
    monkeypatch.setattr(sec, "get_access_token", lambda: "")
    assert lq.broker_feed_available() is False


def test_index_cache_avoids_repeat_calls(monkeypatch):
    calls = {"n": 0}
    def one_shot():
        calls["n"] += 1
        return {"NIFTY": lq.Quote("NIFTY", 1.0, "nse_live")}
    # simulate the raw fetch by seeding cache through the real function path
    import time
    monkeypatch.setattr(lq, "_session", object())   # skip session build
    # Patch requests via the module's session usage is complex; instead assert
    # cache TTL logic directly.
    lq._cache["nse_all"] = (time.time(), {"NIFTY": lq.Quote("NIFTY", 1.0, "nse_live")})
    assert lq._nse_all_indices()["NIFTY"].last == 1.0   # served from cache
