"""
core/margin.py — can this F&O position actually be funded?

WHY THIS EXISTS
---------------
Nothing in the system modelled margin. Position sizing asked only "how many
lots fit my risk budget", never "can I fund a single lot", so it happily sized
trades the account cannot open. Measured 2026-08-06 at the default Rs 1,00,000
capital: one INFY futures lot needs ~Rs 93,200 of margin (93% of the account),
and KOTAKBANK (~Rs 1.58L) and BAJFINANCE (~Rs 1.73L) cannot be funded at all.
A broker rejects those orders outright; the system emitted signals for them.

Refusing an unfundable order is not a restriction bolted on -- it IS proper
F&O behaviour, and it is the difference between a signal you can act on and
one that dies at the order window.

WHAT THIS IS AND IS NOT
-----------------------
This is an ESTIMATOR, not the exchange's number. Real initial margin is SPAN
(a portfolio risk scenario calculation run by the exchange) plus an exposure
margin, it changes intraday with volatility, and brokers add their own buffer.
The only authoritative figure is the broker's margin calculator or API.

The estimate here is deliberately CONSERVATIVE (it errs high), because the
failure mode it prevents -- sizing a position you cannot open -- is worse than
occasionally declaring a fundable trade unfundable.

THE ONE STRUCTURAL FACT THAT MATTERS MOST
-----------------------------------------
Long options cost PREMIUM ONLY -- no SPAN margin. Short options and futures
need full margin. That asymmetry is precisely why a small account can trade
options but not stock futures, and it is the single most important thing this
module encodes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

try:
    import config as _cfg
except Exception:  # pragma: no cover
    _cfg = None

Instrument = Literal["futures", "option_long", "option_short"]

# Fractions of NOTIONAL. Stock futures SPAN typically lands 10-14% with a 3-5%
# exposure margin on top; volatile names run higher. Index futures are lower.
DEFAULT_MARGIN = {
    "stock_futures_span": 0.13,
    "stock_futures_exposure": 0.05,
    "index_futures_span": 0.09,
    "index_futures_exposure": 0.03,
    # Short options are margined like futures on the UNDERLYING notional, plus
    # the premium received is credited. Modelled as futures-equivalent here.
    "short_option_multiplier": 1.0,
    # Broker buffer over the exchange requirement — brokers block at their own
    # threshold, not the exchange's, so ignoring this over-reports capacity.
    "broker_buffer": 0.10,
}

_INDEX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYIT", "INDIAVIX"}


@dataclass
class MarginEstimate:
    instrument: str
    symbol: str
    lots: int
    lot_size: int
    notional: float
    span: float
    exposure: float
    buffer: float
    total: float
    affordable: bool
    capital: float
    shortfall: float
    note: str

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["pct_of_capital"] = (round(self.total / self.capital, 4)
                               if self.capital else None)
        return d


def _rates() -> dict:
    override = getattr(_cfg, "MARGIN_RATES", None) if _cfg else None
    return {**DEFAULT_MARGIN, **override} if isinstance(override, dict) else DEFAULT_MARGIN


def is_index(symbol: str) -> bool:
    return str(symbol).upper() in _INDEX


def estimate(symbol: str, price: float, lots: int = 1,
             instrument: Instrument = "futures",
             capital: float = 0.0, lot_size: Optional[int] = None,
             premium: Optional[float] = None) -> MarginEstimate:
    """Estimated initial margin to OPEN this position, and whether it fits.

    price   : underlying price (used for notional)
    premium : option premium per unit — REQUIRED for option_long, which costs
              premium only and carries no SPAN margin.
    """
    if price <= 0 or lots <= 0:
        raise ValueError("price and lots must be positive")

    sym = str(symbol).upper()
    if lot_size is None:
        try:
            from core.futures_leg import lot_size_for
            lot_size = int(lot_size_for(sym))
        except Exception:
            lot_size = 1
    lot_size = max(int(lot_size), 1)

    r = _rates()
    qty = lots * lot_size
    notional = price * qty

    if instrument == "option_long":
        # Premium only. No SPAN: max loss is the premium already paid.
        if premium is None or premium < 0:
            raise ValueError("option_long requires a non-negative premium")
        span = exposure = 0.0
        total = premium * qty
        note = "long option: premium only, no SPAN margin"
    else:
        idx = is_index(sym)
        span_pct = r["index_futures_span"] if idx else r["stock_futures_span"]
        exp_pct = r["index_futures_exposure"] if idx else r["stock_futures_exposure"]
        if instrument == "option_short":
            span_pct *= r["short_option_multiplier"]
            exp_pct *= r["short_option_multiplier"]
            note = "short option: margined like futures on underlying notional"
        else:
            note = "index futures" if idx else "stock futures"
        span = notional * span_pct
        exposure = notional * exp_pct
        total = span + exposure

    buffer = total * r["broker_buffer"]
    total_with_buffer = total + buffer

    affordable = bool(capital) and total_with_buffer <= capital
    shortfall = max(0.0, total_with_buffer - capital) if capital else 0.0

    return MarginEstimate(
        instrument=instrument, symbol=sym, lots=int(lots), lot_size=lot_size,
        notional=round(notional, 2), span=round(span, 2),
        exposure=round(exposure, 2), buffer=round(buffer, 2),
        total=round(total_with_buffer, 2), affordable=affordable,
        capital=round(capital, 2), shortfall=round(shortfall, 2), note=note,
    )


def max_affordable_lots(symbol: str, price: float, capital: float,
                        instrument: Instrument = "futures",
                        lot_size: Optional[int] = None,
                        premium: Optional[float] = None,
                        max_capital_fraction: float = 1.0) -> int:
    """Largest lot count fundable from `capital`. 0 means not even one lot.

    max_capital_fraction caps how much of the account a single position may
    consume: one lot eating 93% of the account is technically fundable and
    still not a position any risk policy should allow.
    """
    if price <= 0 or capital <= 0:
        return 0
    budget = capital * max(0.0, min(1.0, max_capital_fraction))
    one = estimate(symbol, price, 1, instrument, capital,
                   lot_size, premium).total
    if one <= 0:
        return 0
    return int(budget // one)
