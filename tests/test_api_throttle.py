"""Tests for the Dhan 429 mitigation layer: session-aware daily cache expiry
and the cross-process shared backoff file (logs/dhan_backoff.json)."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import api_dhan as ad


def test_daily_cache_expiry_is_future_and_bounded():
    exp = ad._daily_cache_expiry_epoch()
    hrs = (exp - time.time()) / 3600
    assert 0 < hrs <= 24


def _force_reread():
    ad._backoff_read_cache["ts"] = 0


def test_shared_backoff_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_BACKOFF_FILE", str(tmp_path / "bk.json"))
    until = time.time() + 30
    ad._shared_backoff_set("chart", until)
    _force_reread()
    assert abs(ad._shared_backoff_get("chart") - until) < 1


def test_shared_backoff_never_downgrades(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_BACKOFF_FILE", str(tmp_path / "bk.json"))
    later = time.time() + 60
    ad._shared_backoff_set("chart", later)
    ad._shared_backoff_set("chart", later - 50)   # earlier value must NOT win
    _force_reread()
    assert abs(ad._shared_backoff_get("chart") - later) < 1


def test_shared_backoff_missing_file_is_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_BACKOFF_FILE", str(tmp_path / "nope.json"))
    _force_reread()
    assert ad._shared_backoff_get("chart") == 0.0


def test_shared_backoff_corrupt_file_is_zero(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    monkeypatch.setattr(ad, "_BACKOFF_FILE", str(p))
    _force_reread()
    assert ad._shared_backoff_get("oc") == 0.0
