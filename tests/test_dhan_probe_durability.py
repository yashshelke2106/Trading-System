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

import asyncio
import time

import pytest

import api_server as A


@pytest.fixture(autouse=True)
def _clean_probe_state(monkeypatch):
    with A._PROBE_LOCK:
        A._PROBE["result"] = None
        A._PROBE["at"] = 0.0
        A._PROBE["inflight"] = False
        # last-known-good must be cleared too, or a working verdict from an
        # earlier test masks the transient result the next one asserts on
        A._PROBE["good"] = None
        A._PROBE["good_at"] = 0.0
    A._CACHE.pop("dhanlive", None)

    # These tests mock rq.post to drive _dhan_probe's HTTP classification, but
    # the probe short-circuits to "rate_limited" while the SCANNER holds a
    # shared backoff — so with a live scanner running, four of them failed
    # before the mock was ever reached. Neutralise the real backoff so the
    # tests exercise classification, which is what they are about.
    monkeypatch.setattr("core.api_dhan._shared_backoff_get", lambda kind: 0.0)
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
    # A cold start no longer blocks: it returns "checking" and probes in the
    # background, so prime the cache directly to test the WARM path.
    A._probe_now()
    assert calls["n"] == 1

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


# ── cold start must not block, and TIMEOUT must be unreachable ────────────

def test_cold_start_returns_immediately_against_a_slow_dhan(monkeypatch):
    """The remaining symptom: navigate away, come back, and the panel showed
    a red TIMEOUT while the credentials were stored, the scanner was running
    and the connection was live. An empty cache used to run a live probe
    inside the request and trip the 10s deadline."""
    def _very_slow():
        time.sleep(30)
        return _probe(working=True)

    monkeypatch.setattr(A, "_dhan_probe", _very_slow)
    t0 = time.time()
    out = A._dhan_probe_cached()
    assert time.time() - t0 < 1.0, "a cold start must not block the request"
    assert out["status"] == "checking"
    assert out["transient"] is True and out["refreshing"] is True
    assert out["configured"] is not None


def test_cold_start_never_reports_timeout(monkeypatch):
    def _very_slow():
        time.sleep(30)
        return _probe(working=True)
    monkeypatch.setattr(A, "_dhan_probe", _very_slow)
    assert A._dhan_probe_cached()["status"] != "timeout"


def test_endpoint_serves_last_known_good_instead_of_a_timeout(monkeypatch):
    """A slow call must never throw away a verdict already reached."""
    monkeypatch.setattr(A, "_dhan_probe",
                        lambda: _probe(working=True, status="working"))
    A._dhan_probe_cached()

    async def _boom(*a, **k):
        raise A.RunTimeout("loader exceeded 10s deadline")
    monkeypatch.setattr(A, "_run", _boom)

    out = asyncio.run(A.dhan_live_status())
    assert out["status"] == "working", "last known good must survive a timeout"
    assert out["stale"] is True and out["working"] is True


def test_endpoint_reports_checking_when_nothing_is_known_yet(monkeypatch):
    async def _boom(*a, **k):
        raise A.RunTimeout("loader exceeded 10s deadline")
    monkeypatch.setattr(A, "_run", _boom)
    out = asyncio.run(A.dhan_live_status())
    assert out["status"] == "checking" and out["transient"] is True
    assert "configured" in out


# ── a throttle must not read as a broken connection ──────────────────────
#
# Reported from the live dashboard 2026-08-11: the panel showed
# "RATE LIMITED ... Live scanning idle until the probe goes green" while the
# token was valid and the scanner was running — in fact it was the scanner's
# own quota use that produced the 429. A transient answer overwrote the
# cached good verdict, so a blip erased knowledge instead of qualifying it.

def test_a_429_does_not_erase_a_recent_working_verdict(monkeypatch):
    monkeypatch.setattr(A, "_dhan_probe",
                        lambda: _probe(working=True, status="working"))
    A._probe_now()
    assert A._dhan_probe_cached()["working"] is True

    # _probe_now runs the probe SYNCHRONOUSLY; _dhan_probe_cached(refresh=True)
    # would only queue it in the background and hand back the stale value.
    monkeypatch.setattr(A, "_dhan_probe", lambda: _probe(
        status="rate_limited", transient=True, retry_after=28))
    A._probe_now()
    out = A._dhan_probe_cached()

    assert out["working"] is True, "a throttle is not a broken connection"
    assert out["degraded"] == "rate_limited"
    assert out["retry_after"] == 28


def test_a_401_is_never_masked_by_a_good_verdict(monkeypatch):
    """The safety valve: only TRANSIENT states may be softened."""
    monkeypatch.setattr(A, "_dhan_probe",
                        lambda: _probe(working=True, status="working"))
    A._probe_now()

    monkeypatch.setattr(A, "_dhan_probe",
                        lambda: _probe(status="expired", http=401))
    A._probe_now()
    out = A._dhan_probe_cached()
    assert out["working"] is False and out["status"] == "expired"
    assert "degraded" not in out


def test_a_stale_good_verdict_stops_covering_for_a_throttle(monkeypatch):
    """Bounded: a dead connection cannot hide behind an old success."""
    monkeypatch.setattr(A, "_dhan_probe",
                        lambda: _probe(working=True, status="working"))
    A._probe_now()
    with A._PROBE_LOCK:
        A._PROBE["good_at"] = time.monotonic() - (A._TTL_OK + 1)

    monkeypatch.setattr(A, "_dhan_probe", lambda: _probe(
        status="rate_limited", transient=True))
    A._probe_now()
    out = A._dhan_probe_cached()
    assert out["working"] is False and out["status"] == "rate_limited"


def test_saving_credentials_drops_the_good_verdict(monkeypatch):
    """A new token must not inherit the old token's success."""
    monkeypatch.setattr(A, "_dhan_probe",
                        lambda: _probe(working=True, status="working"))
    A._probe_now()
    monkeypatch.setattr(A.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    A._invalidate_probe()
    with A._PROBE_LOCK:
        assert A._PROBE["good"] is None
