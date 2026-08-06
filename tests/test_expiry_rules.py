"""NSE F&O expiry must come from the LISTED contracts, not a weekday rule.

Two bugs sit behind these tests.

1. CLAUDE.md asserted "weekly Thursday (stocks), monthly last Thursday
   (index)" -- the inverse of the real convention.

2. More seriously, the weekday MODEL itself had drifted from the exchange.
   Measured 2026-08-06 against the Dhan scrip master: NIFTY weeklies are all
   TUESDAYS (11/18/25 Aug) while the rule computed Thursday 13 Aug, a contract
   that does not exist; RELIANCE lists both 25 Aug (Tue) and 27 Aug (Thu) with
   the Tuesday nearer. The live option chain agreed with the scrip master. An
   order on an unlisted expiry is simply rejected, so nearest_expiry() now
   reads the contract list and keeps the weekday rule only as a fallback.

The evidence was hidden by a third bug: OPTIDX rows carry an EMPTY
SM_SYMBOL_NAME, and the indexer's guard dropped them before the trading-symbol
fallback ran, so every index option was missing from the lookup.
"""

from datetime import date, datetime

import pytest

from core import scrip_master as sm
from core.nse_calendar import expiry_str_ddmonyy, expiry_str_iso, nearest_expiry

ASOF = date(2026, 8, 6)

INDICES = ["NIFTY", "BANKNIFTY"]
STOCKS = ["RELIANCE", "TATASTEEL", "INFY"]


def _listed(sym):
    return sm.listed_expiries(sym)


def test_index_options_are_indexed_at_all():
    """The regression: OPTIDX rows must not be dropped for an empty
    SM_SYMBOL_NAME. NIFTY alone has ~4,000 option contracts."""
    assert _listed("NIFTY"), "NIFTY options missing from the scrip master index"
    assert _listed("BANKNIFTY")


@pytest.mark.parametrize("sym", INDICES + STOCKS)
def test_resolved_expiry_is_actually_listed(sym):
    """Whatever the calendar returns must be a contract the exchange lists."""
    listed = _listed(sym)
    if not listed:
        pytest.skip(f"no listed contracts for {sym} in this scrip master")
    assert expiry_str_iso(sym, ASOF) in listed


@pytest.mark.parametrize("sym", INDICES + STOCKS)
def test_resolved_expiry_is_not_in_the_past(sym):
    if not _listed(sym):
        pytest.skip("no listed contracts")
    assert nearest_expiry(sym, ASOF) >= ASOF


def test_nifty_weeklies_are_not_assumed_to_be_thursday():
    """Pins the drift that started this: the resolved NIFTY expiry must match
    the listed contract, whichever weekday the exchange has moved to."""
    listed = _listed("NIFTY")
    if not listed:
        pytest.skip("no NIFTY contracts")
    resolved = expiry_str_iso("NIFTY", ASOF)
    assert resolved == min(e for e in listed if e >= ASOF.isoformat())


def test_nearest_is_the_earliest_listed_on_or_after_the_date():
    for sym in INDICES + STOCKS:
        listed = _listed(sym)
        future = [e for e in listed if e >= ASOF.isoformat()]
        if not future:
            continue
        assert expiry_str_iso(sym, ASOF) == min(future), sym


def test_expiry_string_format_is_ddmonyy():
    s = expiry_str_ddmonyy("RELIANCE", ASOF)
    assert len(s) == 7, s                       # e.g. 25AUG26
    assert s[:2].isdigit() and s[-2:].isdigit()
    assert s[2:5].isalpha() and s[2:5].isupper()


def test_ddmonyy_and_iso_agree():
    for sym in INDICES + STOCKS:
        iso = expiry_str_iso(sym, ASOF)
        dd = expiry_str_ddmonyy(sym, ASOF)
        assert datetime.strptime(iso, "%Y-%m-%d").strftime("%d%b%y").upper() == dd


def test_unknown_symbol_still_returns_a_date_via_the_fallback():
    """No listed contracts must degrade to the weekday model, not explode."""
    assert isinstance(nearest_expiry("NOT_A_REAL_SYMBOL_XYZ", ASOF), date)
