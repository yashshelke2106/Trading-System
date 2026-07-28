"""
Signal finalisation — the ONE authoritative calibrate + selective-fire
gate, applied AFTER option-leg + OI enrichment.

Why it lives here (not in scan_universe)
----------------------------------------
The gate must see the OI-adjusted confluence_score. If it ran inside
scan_universe (pre-OI) it would kill negative-expectancy candidates
before the OI footprint could rescue or condemn them — defeating the
whole point of the OI edge. So scan_universe just generates ranked
candidates; this runs last, on the fully-enriched dicts, as the single
chokepoint before signals.json.

What it does
------------
1. Map each signal's (OI-adjusted) confluence_score → calibrated P(win).
2. Keep only signals whose expectancy  p·rr − (1−p)  clears a margin.
3. Rank best-edge-first, cap the count. Fewer, better — the only
   honest path to a high *traded* hit rate for an option buyer.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

SELECTIVE_FIRE      = True
MIN_EXPECTANCY_R    = 0.15   # need p·rr − (1−p) ≥ this (positive w/ margin)
SELECTIVE_FIRE_KEEP = 12     # hard cap per scan (sniper, not spray)
MIN_VOLUME_RATIO    = 0.70   # vol < 0.7x avg = dead tape, skip

# ── Hour-based filter (Bug #4) ─────────────────────────────────────────
# Journal data: Hour 12 = 15% WR (death zone). Hour 14 = 23%.
# Hours 09-11 and 13 are 30%+ WR.
DEATH_HOURS = {12, 14}       # hard-block these hours (setup-matched exempt)
WEAK_HOURS  = {15}            # penalty only, not kill (last 30min)

# ── Sector correlation cap (Bug #8) ────────────────────────────────────
# Max signals from same sector per scan. Prevents correlated blowups.
MAX_PER_SECTOR = 2
SECTOR_MAP = {
    # Banks
    "AXISBANK": "bank", "BANDHANBNK": "bank", "BANKBARODA": "bank",
    "CANBK": "bank", "HDFCBANK": "bank", "ICICIBANK": "bank",
    "IDFCFIRSTB": "bank", "INDUSINDBK": "bank", "KOTAKBANK": "bank",
    "PNB": "bank", "SBIN": "bank", "FEDERALBNK": "bank",
    "AUBANK": "bank", "MANAPPURAM": "nbfc", "BAJFINANCE": "nbfc",
    "BAJAJFINSV": "nbfc", "CHOLAFIN": "nbfc", "MUTHOOTFIN": "nbfc",
    "PFC": "nbfc", "RECLTD": "nbfc", "SHRIRAMFIN": "nbfc",
    "LICHSGFIN": "nbfc", "JIOFIN": "nbfc",
    # IT
    "INFY": "it", "TCS": "it", "WIPRO": "it", "HCLTECH": "it",
    "TECHM": "it", "LTIM": "it", "PERSISTENT": "it", "COFORGE": "it",
    "MPHASIS": "it", "TATAELXSI": "it", "OFSS": "it", "NAUKRI": "it",
    # Auto
    "MARUTI": "auto", "TATAMOTORS": "auto", "M&M": "auto",
    "BAJAJ-AUTO": "auto", "HEROMOTOCO": "auto", "EICHERMOT": "auto",
    "ASHOKLEY": "auto", "ESCORTS": "auto", "MOTHERSON": "auto",
    "APOLLOTYRE": "auto", "MRF": "auto", "BALKRISIND": "auto",
    "BHARATFORG": "auto",
    # Pharma
    "SUNPHARMA": "pharma", "DRREDDY": "pharma", "CIPLA": "pharma",
    "DIVISLAB": "pharma", "LUPIN": "pharma", "AUROPHARMA": "pharma",
    "BIOCON": "pharma", "ALKEM": "pharma", "LAURUSLABS": "pharma",
    "TORNTPHARM": "pharma",
    # Metal
    "TATASTEEL": "metal", "JSWSTEEL": "metal", "HINDALCO": "metal",
    "VEDL": "metal", "NATIONALUM": "metal", "SAIL": "metal",
    "NMDC": "metal", "COALINDIA": "metal",
    # Oil & Gas
    "RELIANCE": "oilgas", "ONGC": "oilgas", "BPCL": "oilgas",
    "IOC": "oilgas", "GAIL": "oilgas", "PETRONET": "oilgas",
    "GUJGASLTD": "oilgas", "IGL": "oilgas",
    # FMCG
    "HINDUNILVR": "fmcg", "ITC": "fmcg", "BRITANNIA": "fmcg",
    "NESTLEIND": "fmcg", "TATACONSUM": "fmcg", "DABUR": "fmcg",
    "MARICO": "fmcg", "COLPAL": "fmcg", "GODREJCP": "fmcg",
    # Infra/Cement
    "ULTRACEMCO": "infra", "SHREECEM": "infra", "AMBUJACEM": "infra",
    "ACC": "infra", "RAMCOCEM": "infra", "GRASIM": "infra",
    "DLF": "infra", "GODREJPROP": "infra", "OBEROIRLTY": "infra",
    "NCC": "infra", "LT": "infra",
    # Adani group
    "ADANIENT": "adani", "ADANIPORTS": "adani", "ADANIGREEN": "adani",
    "ADANIPOWER": "adani",
    # Power/Utilities
    "NTPC": "power", "POWERGRID": "power", "TATAPOWER": "power",
    "INDUSTOWER": "power",
}

# Pattern conflict sets — if signal has patterns from OPPOSING set, block.
# Empirical: all 3 SL_HITs this week had opposing HTF patterns.
LONG_OPPOSING = {"ema_downtrend", "supertrend_down", "ema_stack_aligned_bear",
                 "ema_bearish_cross", "lower_high_lower_low"}
SHORT_OPPOSING = {"ema_uptrend", "supertrend_up", "ema_stack_aligned_bull",
                  "ema_bullish_cross", "higher_high_higher_low"}


def _has_pattern_conflict(signal: Dict) -> bool:
    """Check if signal has 2+ opposing HTF patterns. Strong SL predictor."""
    direction = signal.get("direction", "long")
    patterns_str = signal.get("patterns_combined", "") or signal.get("patterns", "")
    if not patterns_str:
        return False
    patterns = set(p.strip() for p in patterns_str.split(",") if p.strip())
    opposing = LONG_OPPOSING if direction == "long" else SHORT_OPPOSING
    conflicts = patterns & opposing
    if len(conflicts) >= 2:
        log.info(f"[Conflict] {signal.get('symbol')} {direction} has "
                 f"{len(conflicts)} opposing patterns: {conflicts}")
        return True
    return False


def _get_market_regime() -> str:
    """Detect current market regime from NIFTY 50 data.

    Returns: "trending_up" | "trending_down" | "choppy" | "unknown"

    Uses NIFTY daily EMA9 vs EMA21 + recent ADX-like volatility measure.
    Trend-following signals in chop = losses. Regime filter blocks them.
    """
    try:
        import numpy as np
        from core.api_dhan import dhan_daily
        df = dhan_daily("NIFTY", days_back=30)
        if df is None or len(df) < 21:
            return "unknown"

        close = df["close"].values
        # EMA9 vs EMA21
        ema9 = _ema(close, 9)
        ema21 = _ema(close, 21)

        # Recent direction: last 5 bars trend
        recent_change = (close[-1] - close[-5]) / close[-5] * 100

        # Range-bound detection: if 5-day range < 2% = chop
        hi5 = max(close[-5:])
        lo5 = min(close[-5:])
        range_pct = (hi5 - lo5) / lo5 * 100

        if range_pct < 1.5:
            return "choppy"
        elif ema9 > ema21 and recent_change > 0.5:
            return "trending_up"
        elif ema9 < ema21 and recent_change < -0.5:
            return "trending_down"
        elif range_pct < 3.0:
            return "choppy"
        else:
            return "trending_up" if ema9 > ema21 else "trending_down"
    except Exception as e:
        log.debug(f"[Regime] detection failed: {e}")
        return "unknown"


def _ema(data, period: int):
    """Simple EMA calculation."""
    import numpy as np
    multiplier = 2 / (period + 1)
    ema = [float(data[0])]
    for price in data[1:]:
        ema.append((float(price) - ema[-1]) * multiplier + ema[-1])
    return ema[-1]


# Cache regime for the scan cycle (don't re-fetch NIFTY per signal)
_regime_cache: Dict = {"regime": "unknown", "ts": 0.0}
_REGIME_TTL = 300  # 5 minutes


def _get_cached_regime() -> str:
    """Get market regime with 5-minute cache."""
    import time
    now = time.time()
    if now - _regime_cache["ts"] > _REGIME_TTL:
        _regime_cache["regime"] = _get_market_regime()
        _regime_cache["ts"] = now
        log.info(f"[Regime] detected: {_regime_cache['regime']}")
    return _regime_cache["regime"]


def finalize_and_select(signals: List[Dict]) -> List[Dict]:
    """Calibrate + expectancy-gate a list of enriched signal dicts."""
    if not signals:
        return signals

    # Detect market regime once per scan
    regime = _get_cached_regime()

    # Pre-filter: pattern conflict + volume floor + hour block + regime
    pre_count = len(signals)
    filtered = []
    hour_blocked = 0
    regime_blocked = 0
    mistake_blocked = 0
    now_hour = datetime.now().hour

    # Validated mistake guards (core.mistake_learner): holdout- and
    # selection-tested loss-signatures. Advisory, loaded once per scan. Unlike
    # raw win-rate skips, each rule survived a temporal holdout + Bonferroni,
    # so this cannot curve-fit the last few losers.
    try:
        from core.mistake_learner import load_guards as _load_mistake_guards
        _mistake_guards = _load_mistake_guards()
    except Exception:
        _mistake_guards = []

    for s in signals:
        is_setup = bool(s.get("setup_name") and
                        s.get("setup_type") in ("mega_winner", "high_wr"))

        # Mistake guard: skip signals matching a validated loss-signature
        # (setup-matched signals are exempt — they have their own evidence).
        if _mistake_guards and not is_setup:
            from core.mistake_learner import should_skip as _mistake_skip
            skip, why = _mistake_skip(s)
            if skip:
                mistake_blocked += 1
                log.debug(f"[finalize] mistake-guard skip {s.get('symbol')}: {why}")
                continue

        # Hour block: death hours (12, 14) unless setup-matched
        if now_hour in DEATH_HOURS and not is_setup:
            hour_blocked += 1
            continue

        # Regime filter: block trend signals in chop, block counter-trend in trends
        if regime == "choppy" and not is_setup:
            # In chop, only allow signals with strong volume (breakout potential)
            vol = float(s.get("volume_ratio", 0) or s.get("vol_ratio", 0) or 0)
            if vol < 1.5:
                regime_blocked += 1
                continue
        elif regime == "trending_up":
            # In uptrend, penalize shorts (don't kill — shorts at resistance still valid)
            if s.get("direction") == "short" and not is_setup:
                try:
                    s["confluence_score"] = int(float(s.get("confluence_score", 0) or 0)) - 15
                except (ValueError, TypeError):
                    pass
        elif regime == "trending_down":
            # In downtrend, penalize longs
            if s.get("direction") == "long" and not is_setup:
                try:
                    s["confluence_score"] = int(float(s.get("confluence_score", 0) or 0)) - 15
                except (ValueError, TypeError):
                    pass

        # Pattern conflict: 2+ opposing HTF patterns = strong SL predictor
        if _has_pattern_conflict(s):
            continue
        # Volume floor: all SL_HITs had vol < 0.7x, all winners > 1.0x
        vol = s.get("volume_ratio") or s.get("vol_ratio")
        if vol is not None:
            try:
                vol_f = float(vol)
                if vol_f < MIN_VOLUME_RATIO:
                    log.info(f"[VolFloor] {s.get('symbol')} vol={vol_f:.2f} < {MIN_VOLUME_RATIO}")
                    continue
            except (ValueError, TypeError):
                pass
        filtered.append(s)

    if hour_blocked:
        log.info(f"[HourBlock] {hour_blocked} signals blocked (death hour {now_hour})")
    if regime_blocked:
        log.info(f"[Regime] {regime_blocked} signals blocked in {regime} regime")
    if mistake_blocked:
        log.info(f"[MistakeGuard] {mistake_blocked} signals blocked (validated loss-signatures)")
    if pre_count > len(filtered):
        log.info(f"[PreFilter] {pre_count} -> {len(filtered)} "
                 f"(conflict/vol/hour/regime dropped {pre_count - len(filtered)})")
    signals = filtered

    try:
        from core.calibrator import get_calibrator
        cal = get_calibrator()
    except Exception:
        cal = None

    scored = []
    for s in signals:
        try:
            raw = float(s.get("confluence_score", 0) or 0)
        except (ValueError, TypeError):
            raw = 0.0
        p = 0.0
        if cal is not None:
            try:
                p = float(cal.predict(raw))
            except Exception:
                p = 0.0
        s["calibrated_prob"] = round(p, 4)

        entry = float(s.get("entry_price", 0) or 0)
        sl    = float(s.get("sl_price", 0) or 0)
        tgt   = float(s.get("target_price", 0) or 0)
        sl_d  = abs(entry - sl)
        tgt_d = abs(tgt - entry)
        rr    = (tgt_d / sl_d) if sl_d > 0 else 0.0
        exp_r = p * rr - (1.0 - p) * 1.0
        s["expectancy_r"] = round(exp_r, 4)
        scored.append((s, exp_r))

    if not SELECTIVE_FIRE:
        return [s for s, _ in scored]

    kept = []
    setup_bypass = 0
    for s, e in scored:
        # Setup-matched signals bypass expectancy gate — the combo IS the edge
        if s.get("setup_name") and s.get("setup_type") in ("mega_winner", "high_wr"):
            kept.append((s, max(e, 1.0)))  # force positive expectancy
            setup_bypass += 1
            continue
        if e >= MIN_EXPECTANCY_R:
            kept.append((s, e))

    # Keeper boosts (core.keeper_learner): validated WIN-signatures earn a
    # gentle size/rank lean. Applied ONLY here, AFTER the expectancy gate — a
    # boost must never push a signal past the gate, only reorder/size ones that
    # already passed. That firewall is what keeps "size up what works" from
    # becoming "lower the bar".
    try:
        from core.keeper_learner import confidence_boost as _keeper_boost
    except Exception:
        _keeper_boost = None

    boosted = 0
    ranked = []
    for s, e in kept:
        mult, why = (_keeper_boost(s) if _keeper_boost else (1.0, ""))
        s["keeper_boost"] = mult          # consumed by the sizing layer
        rank_val = e * mult               # ranking preference only
        tag = ""
        if mult > 1.0:
            boosted += 1
            tag = f" | keeper x{mult:.2f}"
        s["reason"] = (f"{s.get('reason','')} | E={e:+.2f}R "
                       f"p={s['calibrated_prob']:.0%}{tag}")
        s["regime"] = regime
        ranked.append((s, rank_val))
    if boosted:
        log.info(f"[Keeper] {boosted} passed signals boosted (rank/size lean)")
    kept = ranked
    kept.sort(key=lambda t: t[1], reverse=True)

    # Sector correlation cap: max MAX_PER_SECTOR signals from same sector
    sector_count: Dict[str, int] = defaultdict(int)
    sector_capped = []
    sector_dropped = 0
    for s, e in kept[:SELECTIVE_FIRE_KEEP * 2]:  # scan wider, then cap
        sym = s.get("symbol", "")
        sector = SECTOR_MAP.get(sym, sym)  # unmapped = own sector
        if sector_count[sector] >= MAX_PER_SECTOR:
            sector_dropped += 1
            log.info(f"[SectorCap] {sym} dropped (sector={sector}, "
                     f"already {MAX_PER_SECTOR} from same sector)")
            continue
        sector_count[sector] += 1
        sector_capped.append(s)
        if len(sector_capped) >= SELECTIVE_FIRE_KEEP:
            break

    out = sector_capped
    if sector_dropped:
        log.info(f"[SectorCap] {sector_dropped} signals capped by sector limit")
    if setup_bypass:
        log.info(f"[Finalize] {setup_bypass} setup-matched signals bypassed expectancy gate")
    log.info(f"[Finalize] {len(signals)} candidates -> {len(out)} fired "
             f"(expectancy >= {MIN_EXPECTANCY_R}R, regime={regime})")
    return out
