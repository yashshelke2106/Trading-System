"""
Option auto-translation: equity signal → option leg recommendation.

Since yfinance doesn't support NSE option chains, we use Black-Scholes
to compute a theoretical ATM premium, then translate equity entry/SL/target
to option premium levels via delta.

For a SHORT signal (e.g. ITC 438.5 → target 452.3 / SL 432):
  → Recommend ITC 440 PE
  → entry_prem  = BSM put price at ATM
  → target_prem = entry_prem + |δ| × (entry_spot − target_spot)
  → sl_prem     = entry_prem − |δ| × (sl_spot   − entry_spot)

For LONG:
  → Recommend ATM CE, same logic flipped.

If Dhan chain data is passed in (list of {strike, ce_ltp, pe_ltp, ce_iv, pe_iv}),
real LTP is used instead of BSM price; IV from chain replaces assumed IV.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

_DEFAULT_IV = 0.30          # 30% assumed annualized IV (used only if no historical IV)
_DAYS_TO_EXPIRY_DEFAULT = 7  # assume ~1 week to nearest expiry

# Per-symbol IV estimates from historical realized vol (better than fixed 30%)
# Updated whenever real chain data IS available, used as fallback for BSM
_SYMBOL_IV_CACHE: dict = {}

# Per-symbol IV history for IV rank computation
# {symbol: [(timestamp, iv), ...]} — last 252 trading days (~1y)
_IV_HISTORY: dict = {}
_IV_HISTORY_MAX = 252


def _compute_iv_rank(symbol: str, current_iv: float) -> Optional[float]:
    """IV rank = where current IV sits in past-year IV range.

    Returns 0-100 percentile or None if insufficient history (<10 samples).

    Side effect: appends current_iv to history.
    """
    if current_iv <= 0:
        return None
    import time as _t
    hist = _IV_HISTORY.setdefault(symbol, [])
    # Append (dedupe within same hour)
    now = _t.time()
    if not hist or (now - hist[-1][0]) > 3600:
        hist.append((now, current_iv))
    if len(hist) > _IV_HISTORY_MAX:
        hist[:] = hist[-_IV_HISTORY_MAX:]
    if len(hist) < 10:
        return None
    ivs = [iv for _, iv in hist]
    lo, hi = min(ivs), max(ivs)
    if hi <= lo:
        return None
    return (current_iv - lo) / (hi - lo) * 100


def _estimate_iv(symbol: str, df_daily=None) -> float:
    """Estimate annualized IV for a symbol.

    Priority:
      1. Cached IV from last successful chain fetch
      2. Historical realized vol from daily data (close-to-close, 20-day)
      3. Default 30%
    """
    if symbol in _SYMBOL_IV_CACHE:
        cached = _SYMBOL_IV_CACHE[symbol]
        if cached.get("ts", 0) > 0:
            import time as _t
            # Use cached IV if < 24h old
            if _t.time() - cached["ts"] < 86400:
                return cached["iv"]

    # Compute realized vol if daily data passed
    if df_daily is not None and len(df_daily) >= 20:
        try:
            import numpy as np
            returns = df_daily['close'].pct_change().dropna().tail(20)
            daily_vol = returns.std()
            annual_vol = daily_vol * (252 ** 0.5)
            # IV usually 1.1-1.3x realized vol for stocks
            implied = min(max(annual_vol * 1.2, 0.15), 0.80)
            return implied
        except Exception:
            pass

    return _DEFAULT_IV


def _cache_iv(symbol: str, iv: float) -> None:
    """Cache realized IV when we got real data."""
    import time as _t
    _SYMBOL_IV_CACHE[symbol] = {"iv": iv, "ts": _t.time()}


# NSE-confirmed strike intervals for heavily traded stocks.
# Price-band fallback used for everything else.
_STEP_OVERRIDE: dict[str, int] = {
    # ₹50-step large-caps (spot ~1000–3000, NSE uses 50 not 25)
    "RELIANCE": 50, "HDFCBANK": 50, "INFY": 50, "BHARTIARTL": 50,
    "KOTAKBANK": 50, "BAJAJFINSV": 50, "WIPRO": 10, "HINDUNILVR": 50,
    "AXISBANK":  25, "ICICIBANK":  25, "SBIN": 20, "INDUSINDBK": 25,
    # Index options
    "NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25,
}


def _strike_step(spot: float, symbol: str = "") -> int:
    if symbol and symbol.upper() in _STEP_OVERRIDE:
        return _STEP_OVERRIDE[symbol.upper()]
    # Price-band fallback (conservative — rounds to nearest valid strike)
    if spot < 200:    return 5
    if spot < 500:    return 10
    if spot < 1000:   return 20
    if spot < 2500:   return 50   # most NSE large-caps in ₹1000–₹2500 use 50
    if spot < 5000:   return 50
    if spot < 10000:  return 100
    return 200


def _atm_strike(spot: float, step: int) -> float:
    return round(spot / step) * step


def _nearest_expiry(from_date: date | None = None, symbol: str = "") -> date:
    """Return nearest valid NSE weekly expiry.

    NSE schedule (2024+): stocks + NIFTY = Tuesday, BANKNIFTY = Wednesday.
    """
    try:
        from .nse_calendar import nearest_weekly_expiry
        return nearest_weekly_expiry(from_date)
    except Exception:
        d = from_date or date.today()
        # Stock + NIFTY = Tuesday (1), BANKNIFTY = Wednesday (2)
        target = 2 if symbol.upper() == "BANKNIFTY" else 1  # Tuesday
        days_ahead = (target - d.weekday()) % 7
        if days_ahead == 0:
            now = datetime.now()
            if d != now.date() or now.time() <= dtime(15, 30):
                return d
            days_ahead = 7
        return d + timedelta(days=days_ahead)


def _chain_lookup_premium(chain: List[Dict], strike: float,
                          option_type: str) -> float:
    """Get real LTP for a specific strike from chain data. Returns 0 if not found."""
    key = "pe_ltp" if option_type == "PE" else "ce_ltp"
    row = min(chain, key=lambda r: abs(r["strike"] - strike), default=None)
    if row and abs(row["strike"] - strike) < 1:  # exact match
        return float(row.get(key, 0) or 0)
    return 0.0


def _estimate_premium_at_spot(chain: List[Dict], option_type: str,
                              entry_strike: float, entry_spot: float,
                              target_spot: float) -> float:
    """Estimate option premium at a different spot level using chain's strike ladder.

    Core idea: the chain has real premiums at many strikes NOW.
    When spot moves from 2800→2850 (+50), an ATM 2800 CE behaves like
    what a 2750 CE is worth now (same moneyness displacement).

    This gives us REAL market prices instead of delta-linear math.
    Falls back to interpolation between two nearest strikes if exact not found.
    """
    spot_delta = target_spot - entry_spot  # positive = spot going up
    key = "ce_ltp" if option_type == "CE" else "pe_ltp"

    # Moneyness displacement: when spot moves, the option's premium
    # resembles a different-strike option at CURRENT spot.
    #
    # CE: spot up by +X  → CE deeper ITM → like (strike - X) CE now.
    # PE: spot down by -X (spot_delta<0) → PE deeper ITM → like
    #     (strike - |spot_delta|) PE = (strike - spot_delta) PE now.
    #     i.e. the SAME formula as CE: proxy = entry_strike - spot_delta.
    #
    # Both option types use the same sign because moneyness for CE
    # increases when strike decreases, and for PE it increases when
    # strike increases — but spot_delta's sign already encodes the
    # direction, so the subtraction is correct for both.
    proxy_strike = entry_strike - spot_delta

    # Find two nearest strikes for interpolation
    sorted_chain = sorted(chain, key=lambda r: r["strike"])
    strikes = [r["strike"] for r in sorted_chain]
    ltps = [float(r.get(key, 0) or 0) for r in sorted_chain]

    if not strikes:
        return 0.0

    # Exact match
    for i, s in enumerate(strikes):
        if abs(s - proxy_strike) < 1 and ltps[i] > 0:
            return ltps[i]

    # Interpolate between two nearest strikes with valid LTP
    below_idx = above_idx = None
    for i, s in enumerate(strikes):
        if s <= proxy_strike and ltps[i] > 0:
            below_idx = i
        if s >= proxy_strike and ltps[i] > 0 and above_idx is None:
            above_idx = i

    if below_idx is not None and above_idx is not None and below_idx != above_idx:
        s_lo, s_hi = strikes[below_idx], strikes[above_idx]
        p_lo, p_hi = ltps[below_idx], ltps[above_idx]
        frac = (proxy_strike - s_lo) / (s_hi - s_lo) if s_hi != s_lo else 0.5
        return p_lo + frac * (p_hi - p_lo)

    # Edge: proxy strike beyond chain range — use nearest available
    if below_idx is not None:
        return ltps[below_idx]
    if above_idx is not None:
        return ltps[above_idx]

    return 0.0


def get_option_rec(
    symbol: str,
    direction: str,
    spot: float,
    entry: float,
    sl: float,
    target: float,
    chain_data: Optional[List[Dict]] = None,
    assumed_iv: float = _DEFAULT_IV,
    days_to_expiry: int = _DAYS_TO_EXPIRY_DEFAULT,
) -> Optional[Dict]:
    """Translate equity signal → option leg with REAL chain premiums.

    HARD RULE: no chain data = no recommendation. Never use BSM-only pricing.

    Entry premium: real LTP from chain (ATM strike).
    Target/SL premiums: estimated via chain strike ladder (real market prices
    at adjacent strikes used as proxy for spot movement). Much more accurate
    than delta-linear approximation.
    """
    if spot <= 0 or entry <= 0:
        return None
    if not chain_data:
        log.debug(f"option_rec {symbol}: no chain data — refusing to guess")
        return None

    try:
        from .options_greeks import BlackScholesModel
        bsm = BlackScholesModel()

        step = _strike_step(spot, symbol)
        atm = _atm_strike(spot, step)
        option_type = "CE" if direction == "long" else "PE"

        # ── Strike selection: 1-step OTM for cheaper premium → higher % gain ─
        # ATM delta ~0.50, 1-OTM delta ~0.40-0.45 but premium 25-35% cheaper
        # Same stock move = bigger % premium gain on cheaper option
        if direction == "long":
            otm_strike = atm + step   # 1 step above for CE
        else:
            otm_strike = atm - step   # 1 step below for PE

        # ── Expiry from chain (real, not guessed) ────────────────────────────
        expiry_date = _nearest_expiry(symbol=symbol)
        chain_expiry_str = str(chain_data[0].get("_expiry", "") or "") if chain_data else ""
        if chain_expiry_str:
            try:
                expiry_date = date.fromisoformat(chain_expiry_str)
            except Exception:
                pass
        expiry_days = max((expiry_date - date.today()).days, 0)
        tte = max((datetime.combine(expiry_date, datetime.min.time()) - datetime.now()
                   ).total_seconds() / (365 * 24 * 3600), 1 / 365)

        # ── Entry premium: REAL LTP from chain ──────────────────────────────
        ltp_key = "pe_ltp" if option_type == "PE" else "ce_ltp"
        iv_key = "pe_iv" if option_type == "PE" else "ce_iv"

        # Try 1-OTM first; fall back to ATM if OTM has no liquidity
        otm_row = min(chain_data, key=lambda r: abs(r["strike"] - otm_strike), default=None)
        atm_row = min(chain_data, key=lambda r: abs(r["strike"] - atm), default=None)
        if not atm_row:
            return None

        # Use OTM if it has decent liquidity (LTP > 0 and OI > 0)
        use_otm = (otm_row and
                   float(otm_row.get(ltp_key, 0) or 0) > 0 and
                   abs(otm_row["strike"] - otm_strike) < step * 0.5)
        chosen_row = otm_row if use_otm else atm_row

        strike = float(chosen_row["strike"])
        ltp = float(chosen_row.get(ltp_key, 0) or 0)
        chain_iv = float(chosen_row.get(iv_key, 0) or 0) / 100
        iv_used = chain_iv if chain_iv > 0 else assumed_iv

        if chain_iv > 0:
            _cache_iv(symbol, chain_iv)

        source = "live"

        # ── Bid-Ask spread filter ─────────────────────────────────────────────
        # Illiquid strikes have wide spreads → real entry price >> LTP → slippage
        # kills the trade before it starts. Drop if spread > 5% of mid.
        bid_key = "pe_bid" if option_type == "PE" else "ce_bid"
        ask_key = "pe_ask" if option_type == "PE" else "ce_ask"
        bid = float(chosen_row.get(bid_key, 0) or 0)
        ask = float(chosen_row.get(ask_key, 0) or 0)
        spread_pct = 0.0
        if bid > 0 and ask > 0 and ask > bid:
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid
            if spread_pct > 0.05:   # >5% spread = too illiquid
                log.info(
                    f"option_rec {symbol} {strike}{option_type}: "
                    f"spread {spread_pct*100:.1f}% > 5% — skip (illiquid)"
                )
                return None

        # ── IV rank gate ──────────────────────────────────────────────────────
        # IV rank > 80 = premium expensive vs 252-day range → buying = bad.
        # IV rank < 30 = premium cheap → good for long option positions.
        # Only block long entries when IV is too rich (selling not implemented).
        try:
            iv_rank = _compute_iv_rank(symbol, chain_iv if chain_iv > 0 else iv_used)
            if iv_rank is not None and iv_rank > 80:
                log.info(
                    f"option_rec {symbol} {strike}{option_type}: "
                    f"IV rank {iv_rank:.0f} > 80 — premium too rich, skip"
                )
                return None
        except Exception:
            iv_rank = None

        # If chain LTP is 0, try Dhan live quote as fallback
        if ltp <= 0:
            try:
                from .api_dhan import DhanAPI
                api = DhanAPI()
                expiry_iso = expiry_date.strftime("%Y-%m-%d")
                live_ltp = api.get_option_quote(symbol, expiry_iso, strike, option_type)
                if live_ltp and live_ltp > 0:
                    ltp = live_ltp
                    source = "live_quote"
            except Exception as e:
                log.debug(f"live option quote failed for {symbol}: {e}")

        if ltp <= 0:
            log.debug(f"option_rec {symbol}: zero LTP even with chain — skip")
            return None

        # ── Target/SL premiums: chain-based lookup (REAL data) ───────────────
        # Uses adjacent strikes in chain as proxy for spot movement.
        # Way more accurate than delta × dS (which ignores gamma/skew).
        target_prem = _estimate_premium_at_spot(
            chain_data, option_type, strike, spot, target)
        sl_prem = _estimate_premium_at_spot(
            chain_data, option_type, strike, spot, sl)

        # If chain lookup failed (strike range too narrow), fall back to
        # gamma-adjusted delta: dP ≈ δ·dS + ½·γ·dS²  (still better than linear)
        greeks = bsm.calculate_greeks(spot, strike, tte, iv_used, option_type)
        abs_delta = abs(greeks.delta)
        gamma = abs(greeks.gamma) if hasattr(greeks, 'gamma') else 0.0

        if target_prem <= 0:
            if option_type == "CE":
                ds = target - entry
                target_prem = ltp + abs_delta * ds + 0.5 * gamma * ds * ds
            else:
                ds = entry - target
                target_prem = ltp + abs_delta * ds + 0.5 * gamma * ds * ds
            source = "gamma_adj"

        if sl_prem <= 0:
            if option_type == "CE":
                ds = entry - sl
                sl_prem = ltp - abs_delta * ds + 0.5 * gamma * ds * ds
            else:
                ds = sl - entry
                sl_prem = ltp - abs_delta * ds + 0.5 * gamma * ds * ds
            source = "gamma_adj"

        # ── Minimal sanity (no artificial bounds — use real data as-is) ──────
        # Only enforce: SL premium > 0 and < entry, target > entry
        sl_prem = max(sl_prem, 0.05)  # can't go below ₹0.05
        sl_prem = min(sl_prem, ltp * 0.98)  # must be below entry
        target_prem = max(target_prem, ltp * 1.05)  # must be above entry

        rr = (target_prem - ltp) / (ltp - sl_prem) if ltp > sl_prem else 0.0

        return {
            "strike":      strike,
            "expiry":      expiry_date.strftime("%Y-%m-%d"),
            "expiry_str":  expiry_date.strftime("%d %b"),
            "expiry_days": expiry_days,
            "option_type": option_type,
            "entry_prem":  round(ltp, 2),
            "target_prem": round(target_prem, 2),
            "sl_prem":     round(sl_prem, 2),
            "delta":       round(greeks.delta, 3),
            "abs_delta":   round(abs_delta, 3),
            "gamma":       round(gamma, 5) if gamma else None,
            "theta":       round(greeks.theta, 3) if hasattr(greeks, 'theta') else None,
            "iv_pct":      round(iv_used * 100, 1),
            "iv_rank":     round(iv_rank, 1) if iv_rank is not None else None,
            "rr":          round(rr, 2),
            "bid":         round(bid, 2) if bid > 0 else None,
            "ask":         round(ask, 2) if ask > 0 else None,
            "spread_pct":  round(spread_pct, 4) if spread_pct > 0 else None,
            "source":      source,
        }

    except Exception as exc:
        log.debug("option_rec %s failed: %s", symbol, exc)
        return None
