"""
core/smallcap_costs.py — turnover-tiered transaction-cost model for the
small/mid-cap (sub-F&O) universe used by the PEAD / path-#2 research.

WHY THIS EXISTS
---------------
Transaction cost is the binding constraint on small/mid-cap anomalies (see
brain/concepts/Transaction Costs.md: "cost decides who has the edge on
identical trades" — RSI-2 PF 1.13 @0.06% vs 0.99 @0.20%). Large-cap futures
cost ~0.06% round-trip; cash-equity midcaps ~0.30%; illiquid smallcaps run far
higher once half-spread + market impact are included. A PEAD-style edge of
3-5% per event survives on a liquid midcap but is eaten alive on an illiquid
smallcap. Any backtest on this universe MUST net these costs or it reproduces
the survivors-only / gross-return mirage already documented for the F&O book.

EMPIRICAL BASIS for the tier values below
-----------------------------------------
- brain/concepts/Transaction Costs.md: 0.06% (liquid futures) .. 0.30% (midcap)
  round-trip, all-in (STT + exchange + GST + stamp + SEBI + brokerage + spread).
- data-engineer measurement (2026-06-25): half-spread ~0.75-1.5%/side and
  impact 0.3-0.7% for sub-Midcap-150 names with <INR 15 Cr daily turnover →
  ~1-2% ONE-WAY → ~2-4% round-trip for the illiquid tail; ~0.7% round-trip for
  DIXON-tier liquid midcaps.

Tiers are turnover-driven (avg daily traded value in INR Cr) because turnover is
the cleanest available liquidity proxy and is computable from bhavcopy/bar_cache
bars. Values are conservative round-trip fractions and are CONFIG CONSTANTS so
they can be tuned in one place.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Cost tiers — round-trip cost as a FRACTION of notional, keyed by the LOWER
# bound of avg daily turnover in INR Cr. Ordered high-liquidity → low.
# A trade in a name with avg daily turnover T pays the cost of the first tier
# whose threshold it clears. Below the smallest threshold = the illiquid tail.
# ─────────────────────────────────────────────────────────────────────────────
COST_TIERS_CR = [
    # (min_turnover_cr, roundtrip_cost_fraction, label)
    (1000.0, 0.0020, "largecap_liquid"),   # ~0.20% — F&O-grade liquidity
    (300.0,  0.0050, "midcap_liquid"),     # ~0.50% — DIXON-tier
    (100.0,  0.0100, "midcap"),            # ~1.00%
    (30.0,   0.0200, "smallmid_illiquid"), # ~2.00%
]
# Below the smallest tier threshold (turnover < 30 Cr): very illiquid smallcap.
ILLIQUID_TAIL_COST = 0.0400  # ~4.00% round-trip (≈2% one-way + fees/impact)

# Absolute floor: even a very liquid name pays at least this (min brokerage +
# fees + one tick of spread). Guards against a 0% cost on a thin-but-high-priced
# name with a freak turnover reading.
MIN_ROUNDTRIP_COST = 0.0015  # 0.15%

# Turnover lookback for the liquidity estimate.
DEFAULT_TURNOVER_WINDOW = 60


def estimate_roundtrip_cost(
    symbol: str,
    avg_daily_turnover_cr: float,
    price: Optional[float] = None,
) -> float:
    """Estimate round-trip transaction cost (as a fraction of notional) for a
    trade in `symbol`, given its average daily turnover in INR Cr.

    `price` is accepted for future tick-size refinement (very low-priced stocks
    pay a larger relative tick) but is not required; the turnover tier dominates.

    Returns a fraction, e.g. 0.005 == 0.5% round-trip. Higher for less liquid.
    """
    if avg_daily_turnover_cr is None or avg_daily_turnover_cr <= 0:
        # No / unknown liquidity → treat as the most illiquid (most conservative).
        return ILLIQUID_TAIL_COST

    cost = ILLIQUID_TAIL_COST
    for min_cr, tier_cost, _label in COST_TIERS_CR:
        if avg_daily_turnover_cr >= min_cr:
            cost = tier_cost
            break

    return max(cost, MIN_ROUNDTRIP_COST)


def cost_tier_label(avg_daily_turnover_cr: float) -> str:
    """Human-readable liquidity tier for reporting/diagnostics."""
    if avg_daily_turnover_cr is None or avg_daily_turnover_cr <= 0:
        return "unknown_illiquid"
    for min_cr, _cost, label in COST_TIERS_CR:
        if avg_daily_turnover_cr >= min_cr:
            return label
    return "very_illiquid_smallcap"


def compute_avg_daily_turnover_cr(
    bars: pd.DataFrame,
    window: int = DEFAULT_TURNOVER_WINDOW,
    asof: Optional[pd.Timestamp] = None,
) -> float:
    """Average daily traded value in INR Cr over the trailing `window` bars.

    Turnover per bar = close * volume (rupees); /1e7 → Cr. Point-in-time: if
    `asof` is given, only bars on/before it are used (no lookahead). Expects
    columns 'close' and 'volume' (bhavcopy / bar_cache schema).
    """
    if bars is None or len(bars) == 0:
        return 0.0
    df = bars
    if asof is not None:
        idx = df.index if df.index.name == "date" or "date" not in df.columns else df["date"]
        df = df[pd.to_datetime(idx) <= pd.to_datetime(asof)]
    if len(df) == 0:
        return 0.0
    tail = df.tail(window)
    turnover_rupees = (tail["close"] * tail["volume"]).mean()
    if pd.isna(turnover_rupees):
        return 0.0
    return float(turnover_rupees) / 1e7


def net_edge_after_cost(
    gross_edge: float,
    avg_daily_turnover_cr: float,
    price: Optional[float] = None,
) -> float:
    """Gross per-trade edge (fraction) minus round-trip cost for that name's
    liquidity. Convenience for the backtest: positive => survives cost."""
    return gross_edge - estimate_roundtrip_cost("", avg_daily_turnover_cr, price)
