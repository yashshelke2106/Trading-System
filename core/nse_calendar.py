"""
NSE trading calendar and F&O expiry resolver.

NSE rule: if expiry Thursday is a holiday, expiry moves to the immediately
preceding trading day (which itself must not be a holiday).

Primary source of expiry truth: Dhan API get_option_expiry_list().
This module is the fallback when Dhan API is unavailable.

Confirmed 2026 expiry shifts (user-verified):
  April 2026 stock monthly: Apr 28 (Tue) — Apr 29+30 are NSE holidays
  May 2026 stock monthly:   May 26 (Tue) — May 27+28 are NSE holidays
"""

from __future__ import annotations

from datetime import date, timedelta, timezone, timedelta as _td
from typing import List, Optional, Set
import datetime as _dt

_IST = timezone(_td(hours=5, minutes=30))


def _now_ist() -> _dt.datetime:
    return _dt.datetime.now(_IST).replace(tzinfo=None)


# ─── NSE Trading Holidays 2026 ────────────────────────────────────────────────
# Source: NSE circular + user-confirmed dates.
# Weekend closures (Sat/Sun) are handled separately via .weekday() check.
NSE_HOLIDAYS_2026: Set[date] = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 26),   # Maha Shivratri
    date(2026, 3, 25),   # Holi
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 4, 29),   # (confirmed: shifted Apr expiry to Apr 28)
    date(2026, 4, 30),   # (confirmed: shifted Apr expiry to Apr 28)
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 27),   # (confirmed: shifted May expiry to May 26)
    date(2026, 5, 28),   # Buddha Purnima (confirmed: shifted May expiry to May 26)
    date(2026, 6, 16),   # Eid ul-Adha (tentative)
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 17),   # Parsi New Year (tentative)
    date(2026, 9, 2),    # Ganesh Chaturthi (tentative)
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 20),  # Diwali Laxmi Puja (tentative)
    date(2026, 10, 21),  # Diwali Balipratipada (tentative)
    date(2026, 11, 4),   # Guru Nanak Jayanti (tentative)
    date(2026, 12, 25),  # Christmas
}


def is_trading_day(d: date, holidays: Optional[Set[date]] = None) -> bool:
    """Return True if d is a NSE trading day (weekday + not holiday)."""
    if holidays is None:
        holidays = NSE_HOLIDAYS_2026
    return d.weekday() < 5 and d not in holidays


def last_trading_day_on_or_before(d: date, holidays: Optional[Set[date]] = None) -> date:
    """Return d if trading day, else walk back until trading day found."""
    while not is_trading_day(d, holidays):
        d -= timedelta(days=1)
    return d


def nearest_monthly_expiry(from_date: Optional[date] = None,
                            holidays: Optional[Set[date]] = None) -> date:
    """
    NSE stock F&O monthly expiry = last Thursday of month, holiday-adjusted.
    Expiry must be >= from_date. If current month's expiry already passed,
    return next month's expiry.
    """
    d = from_date or _now_ist().date()

    def _last_thursday_of_month(year: int, month: int) -> date:
        # Start from last day of month, walk back to Thursday
        if month == 12:
            last_day = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(year, month + 1, 1) - timedelta(days=1)
        while last_day.weekday() != 3:  # 3 = Thursday
            last_day -= timedelta(days=1)
        return last_day

    def _adjusted_expiry(year: int, month: int) -> date:
        raw = _last_thursday_of_month(year, month)
        # Walk back if holiday (NSE rule: preceding trading day)
        return last_trading_day_on_or_before(raw, holidays)

    candidate = _adjusted_expiry(d.year, d.month)
    if candidate >= d:
        return candidate

    # Current month expired — return next month
    if d.month == 12:
        return _adjusted_expiry(d.year + 1, 1)
    return _adjusted_expiry(d.year, d.month + 1)


def nearest_weekly_expiry(from_date: Optional[date] = None,
                           holidays: Optional[Set[date]] = None) -> date:
    """
    NSE index (NIFTY/BANKNIFTY) weekly expiry = nearest Thursday, holiday-adjusted.
    Same-day Thursday valid until 15:30 IST; after that, roll to next week.
    """
    d = from_date or _now_ist().date()

    days_ahead = (3 - d.weekday()) % 7  # 3 = Thursday
    if days_ahead == 0:
        now = _now_ist()
        if now.date() == d and now.time() >= _dt.time(15, 30):
            days_ahead = 7

    candidate = d + timedelta(days=days_ahead)
    candidate = last_trading_day_on_or_before(candidate, holidays)

    # If adjusted candidate fell before today, take next week's Thursday
    if candidate < d:
        candidate = last_trading_day_on_or_before(d + timedelta(days=7), holidays)

    return candidate


def nearest_expiry(symbol: str, from_date: Optional[date] = None,
                   holidays: Optional[Set[date]] = None) -> date:
    """
    Dispatch to weekly (indices) or monthly (stocks) expiry.
    Indices: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY → weekly Thursday
    All other F&O stocks → last Thursday of month (monthly)
    """
    if symbol.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYIT"):
        return nearest_weekly_expiry(from_date, holidays)
    return nearest_monthly_expiry(from_date, holidays)


def expiry_str_ddmonyy(symbol: str, from_date: Optional[date] = None) -> str:
    """Return expiry as 'DDMONYY' string used in NSE option symbols (e.g. '28APR26')."""
    d = nearest_expiry(symbol, from_date)
    return d.strftime("%d%b%y").upper()


def expiry_str_iso(symbol: str, from_date: Optional[date] = None) -> str:
    """Return expiry as 'YYYY-MM-DD' string."""
    return nearest_expiry(symbol, from_date).strftime("%Y-%m-%d")


def days_to_next_expiry(symbol: str = "NIFTY",
                        from_date: Optional[date] = None) -> int:
    d = from_date or _now_ist().date()
    return max((nearest_expiry(symbol, d) - d).days, 0)


def is_expiry_week(symbol: str = "NIFTY") -> bool:
    return days_to_next_expiry(symbol) <= 5


def fno_monthly_expiry_dates(months_ahead: int = 3,
                              from_date: Optional[date] = None) -> List[date]:
    """Return next N monthly expiry dates (holiday-adjusted)."""
    d = from_date or _now_ist().date()
    results: List[date] = []
    for _ in range(months_ahead):
        exp = nearest_monthly_expiry(d)
        results.append(exp)
        # Advance past this expiry
        if exp.month == 12:
            d = date(exp.year + 1, 1, 1)
        else:
            d = date(exp.year, exp.month + 1, 1)
    return results
