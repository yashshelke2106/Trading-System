"""NSE F&O expiry: stocks are MONTHLY, indices are WEEKLY.

CLAUDE.md asserted the inverse ("weekly Thursday (stocks), monthly last
Thursday (index)") while core/nse_calendar.py implemented it correctly. A
future reader trusting the doc would have "fixed" working code and put every
single-stock option on a contract that does not exist. These tests pin the
real rule so the code cannot drift toward the stale doc.
"""

from datetime import date

from core.nse_calendar import (expiry_str_ddmonyy, nearest_monthly_expiry,
                               nearest_weekly_expiry)

# A Thursday, mid-month, so weekly and monthly are unambiguously different.
ASOF = date(2026, 8, 6)

INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
STOCKS = ["RELIANCE", "TATASTEEL", "INFY", "SBIN", "KOTAKBANK"]


def test_single_stocks_get_the_monthly_expiry():
    """There are no weekly single-stock contracts on NSE."""
    monthly = nearest_monthly_expiry(ASOF)
    for sym in STOCKS:
        assert expiry_str_ddmonyy(sym, ASOF) == \
            expiry_str_ddmonyy("__MONTHLY_PROBE__", ASOF), sym
    assert monthly.day >= 22, "monthly expiry is the LAST Thursday"


def test_indices_get_the_weekly_expiry():
    weekly = nearest_weekly_expiry(ASOF)
    monthly = nearest_monthly_expiry(ASOF)
    assert weekly < monthly, "mid-month weekly must precede the monthly"
    for sym in INDICES:
        assert expiry_str_ddmonyy(sym, ASOF) != expiry_str_ddmonyy(
            "RELIANCE", ASOF), f"{sym} must not use the stock monthly"


def test_stock_and_index_expiries_actually_differ_midmonth():
    """The regression that matters: if these ever collapse to one value, the
    weekly/monthly dispatch has been broken."""
    assert expiry_str_ddmonyy("NIFTY", ASOF) != expiry_str_ddmonyy("RELIANCE", ASOF)


def test_monthly_expiry_is_the_last_thursday_or_earlier_for_holidays():
    exp = nearest_monthly_expiry(ASOF)
    # Thursday==3; a holiday shift moves it EARLIER, never later.
    assert exp.weekday() <= 3
    assert exp.month == ASOF.month or exp.month == ASOF.month + 1


def test_weekly_expiry_is_a_thursday_or_earlier():
    exp = nearest_weekly_expiry(ASOF)
    assert exp.weekday() <= 3
    assert (exp - ASOF).days <= 7


def test_expiry_string_format_is_ddmonyy():
    s = expiry_str_ddmonyy("RELIANCE", ASOF)
    assert len(s) == 7, s          # e.g. 27AUG26
    assert s[:2].isdigit() and s[-2:].isdigit()
    assert s[2:5].isalpha() and s[2:5].isupper()
