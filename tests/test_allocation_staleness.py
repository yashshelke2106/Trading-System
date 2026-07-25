"""Staleness reporting for the NIFTY allocation feed.

Regression cover for 2026-07-24: `refresh_nifty_cache` swallowed a yfinance
failure and returned the same bare date string it returns on success, and
`allocation_task.build_status` discarded that value entirely. The daily job then
printed a normal-looking `risk_off 50% equity` line computed from 3-day-old
prices, with no warning — on the one strategy earmarked for real capital.
"""

import pandas as pd
import pytest

from core.allocation import MAX_CACHE_AGE_DAYS, CacheRefresh, refresh_nifty_cache

TODAY = pd.Timestamp("2026-07-24")


def _mk(last_date, ok=True, added=0, error=""):
    return CacheRefresh(last_date=last_date, ok=ok, added=added, error=error)


# ── age ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("last,expected", [
    ("2026-07-24", 0),
    ("2026-07-23", 1),
    ("2026-07-21", 3),
])
def test_age_days_counts_calendar_days(last, expected):
    assert _mk(last).age_days(TODAY) == expected


# ── the actual regression ───────────────────────────────────────────────────

def test_failed_fetch_is_stale_even_when_cache_date_is_recent():
    """The 07-24 bug: fetch died, cache date looked fine, nothing flagged it."""
    rc = _mk("2026-07-24", ok=False, error="TypeError: 'NoneType' not subscriptable")
    assert rc.is_stale(TODAY) is True


def test_failed_fetch_preserves_the_error_text():
    rc = _mk("2026-07-21", ok=False, error="TypeError: boom")
    assert "TypeError" in rc.error


def test_success_and_failure_are_distinguishable():
    """The root cause: both paths used to return an indistinguishable str."""
    good = _mk("2026-07-24", ok=True)
    bad = _mk("2026-07-24", ok=False, error="down")
    assert good.ok != bad.ok
    assert good.is_stale(TODAY) != bad.is_stale(TODAY)


# ── age-based staleness ─────────────────────────────────────────────────────

def test_fresh_cache_is_not_stale():
    assert _mk("2026-07-24", ok=True).is_stale(TODAY) is False


def test_weekend_gap_is_not_stale():
    """Fri->Mon must not false-positive; the exchange being shut isn't a fault."""
    monday = pd.Timestamp("2026-07-27")
    assert _mk("2026-07-24", ok=True).is_stale(monday) is False


def test_cache_older_than_threshold_is_stale_despite_ok_feed():
    """A feed frozen at an old date still reports ok=True — age must catch it."""
    old = (TODAY - pd.Timedelta(days=MAX_CACHE_AGE_DAYS + 1)).strftime("%Y-%m-%d")
    assert _mk(old, ok=True).is_stale(TODAY) is True


def test_threshold_boundary_is_inclusive():
    at = (TODAY - pd.Timedelta(days=MAX_CACHE_AGE_DAYS)).strftime("%Y-%m-%d")
    assert _mk(at, ok=True).is_stale(TODAY) is False


# ── refresh_nifty_cache contract ────────────────────────────────────────────

def test_refresh_returns_cacherefresh_not_str(tmp_path, monkeypatch):
    """Guards the return type — a bare str is what made the bug invisible."""
    p = tmp_path / "NIFTY.parquet"
    idx = pd.to_datetime(["2026-07-22", "2026-07-23"])
    pd.DataFrame({"open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
                  "close": [1.0, 1.0], "volume": [1, 1]}, index=idx).to_parquet(p)

    import yfinance
    monkeypatch.setattr(yfinance, "download",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("feed down")))

    rc = refresh_nifty_cache(str(p))
    assert isinstance(rc, CacheRefresh)
    assert rc.ok is False
    assert "feed down" in rc.error
    assert rc.last_date == "2026-07-23"   # stale cache still stands
    assert rc.is_stale(TODAY) is True
