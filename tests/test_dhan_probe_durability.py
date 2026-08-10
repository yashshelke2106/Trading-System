"""The Dhan connection panel must stay honest and stay up.

Three reported symptoms, three distinct bugs:

  "it times out when I give new credentials after leaving the page"
      saving awaited a live probe, so a slow Dhan turned a SUCCESSFUL write
      into a TIMEOUT in the UI

  "HTTP 429: auth accepted but request shape rejected (DH-905 class)"
      429 fell into the catch-all branch and was reported as a schema
      problem. It is a RATE LIMIT -- the request was fine, it was sent too
      often -- and it sent people hunting a bug that did not exist

  "it isn't durable"
      any failure was cached for 15 minutes, so one rate-limited call kept
      the panel broken long after the credentials were fixed
"""

import time

import pytest

import api_server as A


@pytest.fixture(autouse=True)
def _clean_probe_state():
    with A._PROBE_LOCK:
        A._PROBE["result"] = None
        A._PROBE["at"] = 0.0
        A._PROBE["inflight"] = False
    A._CACHE.pop("dhanlive", None)
    yield


def _probe(**over):
    base = {"configured": {}, "working": False, "status": "x"}
    base.update(over)
    return base


# ── classification ───────────────────────────────────────────────────────

def test_429_is_a_rate_limit_not_a_schema_problem(monkeypatch):
    class R:
        status_code = 429
        headers = {"Retry-After": "42"}

    monkeypatch.setattr(A, "_dhan_configured", lambda: {"trade_token": True})
    import requests as rq
    monkeypatch.setattr(rq, "post", lambda *a, **k: R())
    monkeypatch.setattr("core.secrets.get_data_token", lambda: "eyJ" + "x" * 300)

    out = A._dhan_probe()
    assert out["status"] == "rate_limited"
    assert out["transient"] is True
    assert out["retry_after"] == 42
    assert "DH-905" not in out["message"]
    assert "shape" not in out["message"].lower()


def test_500_is_marked_transient(monkeypatch):
    class R:
        status_code = 503
        headers = {}
    import requests as rq
    monkeypatch.setattr(A, "_dhan_configured", lambda: {})
    monkeypatch.setattr(rq, "post", lambda *a, **k: R())
    monkeypatch.setattr("core.secrets.get_data_token", lambda: "eyJ" + "x" * 300)
    out = A._dhan_probe()
    assert out["status"] == "dhan_down" and out["transient"] is True


def test_401_stays_terminal_and_is_not_transient(monkeypatch):
    class R:
        status_code = 401
        headers = {}
    import requests as rq
    monkeypatch.setattr(A, "_dhan_configured", lambda: {})
    monkeypatch.setattr(rq, "post", lambda *a, **k: R())
    monkeypatch.setattr("core.secrets.get_data_token", lambda: "eyJ" + "x" * 300)
    out = A._dhan_probe()
    assert out["status"] == "expired"
    assert not out.get("transient")


def test_a_genuine_shape_error_still_reports_dh905(monkeypatch):
    class R:
        status_code = 400
        headers = {}
    import requests as rq
    monkeypatch.setattr(A, "_dhan_configured", lambda: {})
    monkeypatch.setattr(rq, "post", lambda *a, **k: R())
    monkeypatch.setattr("core.secrets.get_data_token", lambda: "eyJ" + "x" * 300)
    out = A._dhan_probe()
    assert out["status"] == "auth_ok_shape_issue"
    assert "DH-905" in out["message"]


# ── TTL: transient failures must expire fast ─────────────────────────────

def test_transient_failure_expires_far_sooner_than_success():
    assert A._ttl_for(_probe(working=True)) == A._TTL_OK
    assert A._ttl_for(_probe(transient=True)) == A._TTL_TRANSIENT
    assert A._ttl_for(_probe(status="expired")) == A._TTL_TERMINAL
    assert A._TTL_TRANSIENT < A._TTL_TERMINAL < A._TTL_OK


def test_a_rate_limit_does_not_pin_the_panel_for_the_full_window():
    """One 429 used to keep the dashboard 'broken' for 15 minutes."""
    assert A._ttl_for(_probe(transient=True)) <= 30


# ── stale-while-revalidate ───────────────────────────────────────────────

def test_warm_reads_are_served_from_memory(monkeypatch):
    calls = {"n": 0}

    def _slow():
        calls["n"] += 1
        return _probe(working=True, status="working")

    monkeypatch.setattr(A, "_dhan_probe", _slow)
    first = A._dhan_probe_cached()
    assert first["working"] and calls["n"] == 1

    t0 = time.time()
    second = A._dhan_probe_cached()
    assert time.time() - t0 < 0.5           # no network on the warm path
    assert calls["n"] == 1
    assert second["stale"] is False and "age_sec" in second


def test_stale_result_is_returned_immediately_not_awaited(monkeypatch):
    monkeypatch.setattr(A, "_dhan_probe",
                        lambda: _probe(working=True, status="working"))
    A._dhan_probe_cached()
    with A._PROBE_LOCK:                     # force staleness
        A._PROBE["at"] = time.monotonic() - (A._TTL_OK + 10)

    def _hang():
        time.sleep(5)
        return _probe(working=True)

    monkeypatch.setattr(A, "_dhan_probe", _hang)
    t0 = time.time()
    out = A._dhan_probe_cached()
    assert time.time() - t0 < 1.0, "a stale read must not wait on the network"
    assert out["stale"] is True and out["refreshing"] is True
    assert out["working"] is True           # last known good still shown


def test_probe_never_returns_none_configured_even_on_error(monkeypatch):
    """The config form reads `configured`; dropping it made stored
    credentials render as (missing) and invited endless re-entry."""
    def _boom():
        raise RuntimeError("network gone")
    monkeypatch.setattr(A, "_dhan_probe", _boom)
    out = A._dhan_probe_cached()
    assert "configured" in out and isinstance(out["configured"], dict)
    assert out.get("transient") is True


# ── saving credentials must not block ────────────────────────────────────

def test_invalidate_probe_returns_immediately(monkeypatch):
    def _slow():
        time.sleep(3)
        return _probe(working=True)
    monkeypatch.setattr(A, "_dhan_probe", _slow)
    t0 = time.time()
    A._invalidate_probe()
    assert time.time() - t0 < 0.5, "saving credentials must not await a probe"


def test_invalidate_probe_drops_the_previous_verdict(monkeypatch):
    monkeypatch.setattr(A, "_dhan_probe",
                        lambda: _probe(working=True, status="working"))
    A._dhan_probe_cached()
    monkeypatch.setattr(A, "_dhan_probe", lambda: _probe(status="expired"))
    A._invalidate_probe()
    time.sleep(0.4)
    assert A._dhan_probe_cached()["status"] == "expired"
