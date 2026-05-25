"""
Entry guard — pre-entry sanity checks to block bad trades.

Solves 87% of historical losses (209/241) by blocking known bias modes:
  1. Dead volume entry (48% of losses)
  2. Long overbought / Short oversold chase (44% combined)
  3. Counter-VWAP entries (5%)
  4. Counter-trend entries (6%)
  5. Conflicting patterns (5%)

Returns (ok: bool, reason: str). If ok=False, signal is killed.
Setup-matched signals bypass all guards (the combo IS the edge).
"""

from __future__ import annotations

import logging
from typing import Dict, Tuple

log = logging.getLogger(__name__)

# ── Thresholds (data-driven from journal precision analysis) ──────────
# DEAD_VOL filter at 0.7 had 29% block-zone WR (same as baseline) — too aggressive.
# Tightened to 0.4 (extreme dead tape only). Most wins happen in 0.4-1.0 vol.
# Short RSI<30: 0% WR (3 trades). Long RSI>78 chase: 0% WR.
MIN_VOLUME_RATIO     = 0.40   # only EXTREME dead tape blocks (was 0.70, blocked too many wins)
LONG_RSI_CEILING     = 78     # 0% WR above 78 in journal
SHORT_RSI_FLOOR      = 30     # 0% WR below 30 in journal
DEEP_OB_RSI          = 82     # absolute long kill above this (18% WR)
DEEP_OS_RSI          = 22     # absolute short kill below this (0% WR)

# Conflicting pattern sets — block if BOTH present
WAE_CONFLICT_PATTERNS = ({"wae_bull_explosion"}, {"wae_bear_explosion"})
PVSRA_CONFLICT_PATTERNS = ({"pvsra_super_bull"}, {"pvsra_super_bear"})


def _parse_patterns(signal: Dict) -> set:
    """Extract pattern set from signal."""
    p = signal.get("patterns_combined", "") or signal.get("patterns", "") or ""
    if isinstance(p, list):
        return set(str(x).strip() for x in p if str(x).strip())
    return set(x.strip() for x in str(p).split(",") if x.strip())


def _safe_float(v, default=0.0) -> float:
    """Robust float parse — handles string, None, empty."""
    if v is None or v == "":
        return default
    try:
        f = float(v)
        return f if not (f != f) else default  # NaN check
    except (ValueError, TypeError):
        return default


def check_entry(signal: Dict) -> Tuple[bool, str]:
    """
    Run all entry guards. Returns (ok, reason).
    Setup-matched signals bypass guards (return True with reason="setup_bypass").
    """
    # Setup bypass — mega winners and high-WR combos override sanity checks
    if signal.get("setup_name") and signal.get("setup_type") in ("mega_winner", "high_wr"):
        return True, "setup_bypass"

    direction = signal.get("direction", "long").lower()
    patterns = _parse_patterns(signal)
    sym = signal.get("symbol", "?")

    # ── Guard 1: Volume floor ──────────────────────────────────────────
    # Try BOTH field names + robust parsing
    vol = _safe_float(signal.get("volume_ratio")) or _safe_float(signal.get("vol_ratio"))
    if vol > 0 and vol < MIN_VOLUME_RATIO:
        return False, f"DEAD_VOL_{vol:.2f}x"

    # ── Guard 2: RSI extremes ──────────────────────────────────────────
    rsi = _safe_float(signal.get("rsi"), 50.0)
    if direction == "long":
        if rsi > DEEP_OB_RSI:
            return False, f"DEEP_OVERBOUGHT_{rsi:.0f}"
        if rsi > LONG_RSI_CEILING and "ema_bullish_cross" not in patterns:
            # RSI > 72 = late chase, unless fresh momentum (EMA cross)
            return False, f"LONG_CHASE_RSI_{rsi:.0f}"
    else:  # short
        if rsi < DEEP_OS_RSI:
            return False, f"DEEP_OVERSOLD_{rsi:.0f}"
        if rsi < SHORT_RSI_FLOOR and "ema_bearish_cross" not in patterns:
            # RSI < 28 = bounce zone, unless fresh breakdown
            return False, f"SHORT_CHASE_RSI_{rsi:.0f}"

    # ── Guard 3: Deep-zone warning patterns ────────────────────────────
    # Only rsi_deep_overbought blocks longs (0% WR). rsi_deep_bearish removed —
    # had 50% block-zone WR (kills profitable shorts in strong downtrend).
    if direction == "long" and "rsi_deep_overbought" in patterns:
        return False, "PATTERN_DEEP_OVERBOUGHT"

    # ── Guard 4: Counter-VWAP entries ──────────────────────────────────
    # below_vwap + long = buying when intraday sellers control
    # above_vwap + short = shorting when intraday buyers control
    # EXCEPTION: fresh trend reversal (EMA cross) — VWAP lags new trend
    if direction == "long" and "below_vwap" in patterns:
        if "ema_bullish_cross" not in patterns and "ema8_fresh_cross_up" not in patterns:
            return False, "LONG_BELOW_VWAP"
    if direction == "short" and "above_vwap" in patterns:
        if "ema_bearish_cross" not in patterns and "ema8_fresh_cross_dn" not in patterns:
            return False, "SHORT_ABOVE_VWAP"

    # ── Guard 5: Counter-trend (EMA direction vs signal) ───────────────
    if direction == "long" and "ema_downtrend" in patterns:
        # Allow only if signal is a trend-change setup
        if "ema_bullish_cross" not in patterns and "ema8_fresh_cross_up" not in patterns:
            return False, "LONG_IN_DOWNTREND"
    if direction == "short" and "ema_uptrend" in patterns:
        if "ema_bearish_cross" not in patterns and "ema8_fresh_cross_dn" not in patterns:
            return False, "SHORT_IN_UPTREND"

    # ── Guard 6: Conflicting indicator patterns ────────────────────────
    wae_bull, wae_bear = WAE_CONFLICT_PATTERNS
    if wae_bull.issubset(patterns) and wae_bear.issubset(patterns):
        return False, "WAE_CONFLICT"

    pvsra_bull, pvsra_bear = PVSRA_CONFLICT_PATTERNS
    if pvsra_bull.issubset(patterns) and pvsra_bear.issubset(patterns):
        return False, "PVSRA_CONFLICT"

    # ── Guard 7: OI opposition ─────────────────────────────────────────
    if direction == "long" and "oi_opposes_long" in patterns:
        return False, "OI_OPPOSES_LONG"
    if direction == "short" and "oi_opposes_short" in patterns:
        return False, "OI_OPPOSES_SHORT"

    # ── Guard 8: Demand/supply zone conflict ───────────────────────────
    if direction == "long" and "demand_zone_bos_down" in patterns:
        return False, "DEMAND_BOS_DOWN_CONFLICT"
    if direction == "short" and "supply_zone_bos_up" in patterns:
        return False, "SUPPLY_BOS_UP_CONFLICT"

    # All guards passed
    return True, "ok"


def apply_guards(signals: list) -> list:
    """Filter a list of signals through all entry guards. Logs killed signals."""
    out = []
    kill_reasons = {}
    for s in signals:
        ok, reason = check_entry(s)
        if ok:
            out.append(s)
        else:
            kill_reasons[reason] = kill_reasons.get(reason, 0) + 1
            log.info(f"[EntryGuard] KILL {s.get('symbol')} {s.get('direction')} — {reason}")

    if kill_reasons:
        summary = ", ".join(f"{r}={n}" for r, n in sorted(kill_reasons.items(),
                                                          key=lambda x: -x[1]))
        log.info(f"[EntryGuard] killed {sum(kill_reasons.values())} signals: {summary}")
    return out


if __name__ == "__main__":
    # Self-test with historical loss patterns
    test_cases = [
        # Should KILL
        ({"symbol": "X", "direction": "long", "rsi": 70, "volume_ratio": 0.3,
          "patterns_combined": "ema_uptrend"}, False),
        ({"symbol": "X", "direction": "long", "rsi": 75, "volume_ratio": 1.5,
          "patterns_combined": "ema_uptrend"}, False),
        ({"symbol": "X", "direction": "short", "rsi": 30, "volume_ratio": 1.5,
          "patterns_combined": "ema_downtrend"}, False),
        ({"symbol": "X", "direction": "long", "volume_ratio": 1.0, "rsi": 55,
          "patterns_combined": "below_vwap, ema_uptrend"}, False),
        ({"symbol": "X", "direction": "long", "volume_ratio": 1.0, "rsi": 55,
          "patterns_combined": "wae_bull_explosion, wae_bear_explosion"}, False),
        # Should PASS
        ({"symbol": "X", "direction": "long", "rsi": 60, "volume_ratio": 1.5,
          "patterns_combined": "ema_uptrend, supertrend_up, above_vwap"}, True),
        ({"symbol": "X", "direction": "long", "rsi": 75, "volume_ratio": 1.5,
          "patterns_combined": "ema_bullish_cross, above_vwap"}, True),  # fresh cross OK
        # Setup bypass
        ({"symbol": "X", "direction": "long", "rsi": 80, "volume_ratio": 0.1,
          "setup_name": "trend_pullback_explosion", "setup_type": "mega_winner"}, True),
    ]

    for sig, expected in test_cases:
        ok, reason = check_entry(sig)
        status = "OK" if ok == expected else "FAIL"
        print(f"[{status}] {sig.get('symbol')} {sig.get('direction')} "
              f"RSI={sig.get('rsi','?')} vol={sig.get('volume_ratio','?')} "
              f"-> ok={ok} reason={reason}")
