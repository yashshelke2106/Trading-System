"""
core/charges.py — statutory + brokerage charges on an Indian trade.

WHY THIS EXISTS
---------------
Paper P&L was computed GROSS: `(exit - entry) * qty`. The only cost modelled
anywhere was the paper FILL (5 bps/side of spread + slippage in
execution._paper_fill), which is a market-microstructure cost, not a charge.
STT, exchange transaction fees, GST, the SEBI turnover fee, stamp duty and
brokerage were never deducted, so every reported P&L was optimistic by the
full charge stack and no number in the system matched what a broker contract
note would actually say.

A discount broker (Groww, Angel One, Zerodha) itemises all of these on every
trade. Matching them is the difference between a P&L you can reconcile and one
you cannot.

RATES CHANGE. These are the schedules as configured, not immutable law — SEBI
and the exchanges revise them, and brokerage is per-broker. Verify against a
real contract note before trusting absolute rupee figures, and override via
config.CHARGE_RATES if your broker differs. The structure (which leg each
charge applies to) is the part that is stable.

SIDE CONVENTIONS THAT ARE EASY TO GET WRONG
-------------------------------------------
  - STT on equity FUTURES and OPTIONS is charged on the SELL leg only.
  - STT on equity DELIVERY is charged on BOTH legs.
  - Stamp duty is charged on the BUY leg only.
  - Options STT/exchange/stamp are on PREMIUM turnover, not notional.
  - GST applies to brokerage + exchange txn + SEBI fee (not to STT/stamp).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Literal, Optional

try:
    import config as _cfg
except Exception:  # pragma: no cover - config always present in the app
    _cfg = None

Segment = Literal["futures", "options", "delivery", "intraday"]

# Rates as fractions of turnover unless named _flat (absolute rupees).
# Sourced from the standard NSE/SEBI schedule used by Indian discount brokers.
DEFAULT_RATES: Dict[str, Dict[str, float]] = {
    "futures": {
        "brokerage_flat": 20.0,      # per executed order, discount-broker norm
        "brokerage_pct": 0.0003,     # or 0.03%, whichever is LOWER
        "stt_sell": 0.0002,          # 0.02% on sell turnover
        "exchange": 0.0000173,       # ~0.00173% per side (NSE futures)
        "sebi": 0.000001,            # Rs 10 per crore
        "stamp_buy": 0.00002,        # 0.002% on buy turnover
        "gst": 0.18,                 # on brokerage + exchange + sebi
    },
    "options": {
        "brokerage_flat": 20.0,
        "brokerage_pct": 0.0,        # flat only, no percentage tier
        "stt_sell": 0.001,           # 0.1% on sell PREMIUM
        "exchange": 0.0003503,       # 0.03503% of PREMIUM per side
        "sebi": 0.000001,
        "stamp_buy": 0.00003,        # 0.003% on buy premium
        "gst": 0.18,
    },
    "delivery": {
        "brokerage_flat": 0.0,       # most discount brokers: free delivery
        "brokerage_pct": 0.0,
        "stt_sell": 0.001,           # 0.1% BOTH legs - see stt_buy below
        "stt_buy": 0.001,
        "exchange": 0.0000297,       # 0.00297% per side
        "sebi": 0.000001,
        "stamp_buy": 0.00015,        # 0.015% on buy turnover
        "gst": 0.18,
    },
    "intraday": {
        "brokerage_flat": 20.0,
        "brokerage_pct": 0.0003,
        "stt_sell": 0.00025,         # 0.025% on sell turnover
        "exchange": 0.0000297,
        "sebi": 0.000001,
        "stamp_buy": 0.00003,        # 0.003% on buy turnover
        "gst": 0.18,
    },
}


@dataclass
class Charges:
    """Itemised charges for ONE round trip (buy leg + sell leg)."""
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    total: float
    buy_turnover: float
    sell_turnover: float

    @property
    def pct_of_turnover(self) -> float:
        """Round-trip charges as a fraction of the BUY turnover (0.0006 = 6bps)."""
        return self.total / self.buy_turnover if self.buy_turnover else 0.0

    def to_dict(self) -> Dict[str, float]:
        d = asdict(self)
        d["pct_of_turnover"] = round(self.pct_of_turnover, 8)
        return d


def _rates(segment: str) -> Dict[str, float]:
    table = DEFAULT_RATES
    override = getattr(_cfg, "CHARGE_RATES", None) if _cfg else None
    if isinstance(override, dict):
        table = {**DEFAULT_RATES, **override}
    seg = table.get(segment)
    if seg is None:
        raise ValueError(f"unknown segment {segment!r}; "
                         f"expected one of {sorted(DEFAULT_RATES)}")
    return seg


def _brokerage_one_leg(turnover: float, r: Dict[str, float]) -> float:
    """Flat-or-percentage, whichever is LOWER — the discount-broker rule.
    A zero flat means free (delivery); a zero pct means flat-only (options)."""
    flat = r.get("brokerage_flat", 0.0)
    pct = r.get("brokerage_pct", 0.0)
    if flat and pct:
        return min(flat, turnover * pct)
    if pct:
        return turnover * pct
    return flat


def round_trip(buy_price: float, sell_price: float, quantity: float,
               segment: Segment = "futures") -> Charges:
    """Itemised charges for a full round trip.

    For options pass the PREMIUM as the price — STT, exchange and stamp are
    levied on premium turnover, not on the notional.
    """
    if quantity <= 0 or buy_price < 0 or sell_price < 0:
        raise ValueError("quantity must be > 0 and prices non-negative")

    r = _rates(segment)
    buy_to = buy_price * quantity
    sell_to = sell_price * quantity

    brokerage = _brokerage_one_leg(buy_to, r) + _brokerage_one_leg(sell_to, r)
    # STT: sell leg always; buy leg too only where the schedule says so (delivery).
    stt = sell_to * r.get("stt_sell", 0.0) + buy_to * r.get("stt_buy", 0.0)
    exchange = (buy_to + sell_to) * r.get("exchange", 0.0)
    sebi = (buy_to + sell_to) * r.get("sebi", 0.0)
    stamp = buy_to * r.get("stamp_buy", 0.0)
    gst = (brokerage + exchange + sebi) * r.get("gst", 0.0)

    # Round each line item FIRST, then total the rounded items. Totalling the
    # raw values and rounding once leaves the itemisation off by a paisa, and a
    # contract note that does not add up is not reconcilable.
    items = {
        "brokerage": round(brokerage, 2), "stt": round(stt, 2),
        "exchange": round(exchange, 2), "sebi": round(sebi, 2),
        "stamp": round(stamp, 2), "gst": round(gst, 2),
    }
    return Charges(
        **items,
        total=round(sum(items.values()), 2),
        buy_turnover=round(buy_to, 2), sell_turnover=round(sell_to, 2),
    )


def net_pnl(entry_price: float, exit_price: float, quantity: float,
            direction: str = "long", segment: Segment = "futures"):
    """Gross P&L, charges, and NET P&L for one round trip.

    Returns (net_pnl, gross_pnl, Charges). Direction decides which price is the
    buy leg: a short sells first, so the charge legs swap even though the
    arithmetic of gross P&L is mirrored.
    """
    is_long = str(direction).lower() in ("long", "buy")
    gross = ((exit_price - entry_price) if is_long
             else (entry_price - exit_price)) * quantity
    buy_px, sell_px = ((entry_price, exit_price) if is_long
                       else (exit_price, entry_price))
    ch = round_trip(buy_px, sell_px, quantity, segment)
    return round(gross - ch.total, 2), round(gross, 2), ch
