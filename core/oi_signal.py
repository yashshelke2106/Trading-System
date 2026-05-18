"""
OI signal — turn ΔOI + ΔPrice into a direction-aware edge.

This is the orthogonal input the calibration curve has been starving
for. Everything else in the engine is a price→price transform; OI is
*committed positioning*. The read is the classic intraday map of how
total option open-interest moved against price between two snapshots:

  price ↑ & PE-OI ↑   put writers confident   LONG buildup    (confirm long, strong)
  price ↓ & CE-OI ↑   call writers confident  SHORT buildup   (confirm short, strong)
  price ↑ & CE-OI ↓   call short-covering     LONG but fading (weak, fade soon)
  price ↓ & PE-OI ↓   put unwinding           SHORT but fading (weak)

Plus two structural reads from the same chain, free:
  • OI walls — strike with max CE OI = hard resistance; max PE OI =
    hard support. A long pressing into the CE wall (or short into the
    PE wall) is a low-quality trade → wall_block.
  • PCR regime — total PE/CE OI as a slow tilt, contrarian at extremes.

Honest / graceful: needs a prior snapshot. Cold start (first sight of a
symbol, or unusable chain) → neutral, zero score impact, never blocks.
All magnitudes are bounded and modest — OI informs, it does not
dominate the 17-vote engine.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# A change in total OI is only "real" above this fraction of the prior
# level — below it is noise / stale-feed jitter.
SIG_DOI_FRAC = 0.03      # 3% of prior total OI

# Bounded score contributions (the engine scores ~0..155; OI is a
# strong-but-not-dominant source, comparable to one good vote group).
SD_CONFIRM_STRONG = 8
SD_CONFIRM_WEAK   = 3
SD_OPPOSE_STRONG  = -8
SD_OPPOSE_WEAK    = -3
SD_PCR_TILT       = 2

# Wall proximity: within this fraction of the OI wall = pressing into it.
WALL_PROX_FRAC = 0.004   # 0.4%


def _pcr_regime(pcr: float) -> str:
    if pcr <= 0:
        return "neutral"
    if pcr >= 1.8:
        return "contrarian_bearish"   # extreme put-heavy → exhaustion
    if pcr >= 1.3:
        return "supportive_long"
    if pcr <= 0.5:
        return "contrarian_bullish"   # extreme call-heavy → exhaustion
    if pcr <= 0.7:
        return "supportive_short"
    return "neutral"


def compute_oi_features(prev: Optional[Dict], cur: Optional[Dict],
                        direction: str) -> Dict:
    """Pure ΔOI logic. Returns a direction-aware feature dict.

    direction: the signal's intended side ("long" / "short").
    """
    out: Dict = {
        "quadrant": "cold_start",
        "pcr": float(cur.get("pcr", 0.0)) if cur else 0.0,
        "pcr_regime": "neutral",
        "wall_block": False,
        "wall_reason": "",
        "score_delta": 0,
        "patterns": [],
        "cold_start": True,
    }
    if not cur:
        return out

    out["pcr_regime"] = _pcr_regime(out["pcr"])
    lng = direction == "long"

    # ── Structural: OI walls (works on a single snapshot) ───────────────
    spot   = float(cur.get("spot", 0) or 0)
    ce_wall = float(cur.get("max_ce_oi_strike", 0) or 0)   # resistance
    pe_wall = float(cur.get("max_pe_oi_strike", 0) or 0)   # support
    if spot > 0:
        if lng and ce_wall > 0 and abs(ce_wall - spot) / spot <= WALL_PROX_FRAC \
                and ce_wall >= spot:
            out["wall_block"] = True
            out["wall_reason"] = f"long into CE OI wall @ {ce_wall:.0f}"
        elif (not lng) and pe_wall > 0 \
                and abs(spot - pe_wall) / spot <= WALL_PROX_FRAC \
                and pe_wall <= spot:
            out["wall_block"] = True
            out["wall_reason"] = f"short into PE OI wall @ {pe_wall:.0f}"

    # ── PCR regime tilt (slow, modest) ──────────────────────────────────
    reg = out["pcr_regime"]
    score = 0
    if reg == "supportive_long":
        score += SD_PCR_TILT if lng else -SD_PCR_TILT
    elif reg == "supportive_short":
        score += SD_PCR_TILT if not lng else -SD_PCR_TILT
    elif reg == "contrarian_bullish":
        score += SD_PCR_TILT if lng else -SD_PCR_TILT
    elif reg == "contrarian_bearish":
        score += SD_PCR_TILT if not lng else -SD_PCR_TILT

    # ── ΔOI 4-quadrant (needs a prior snapshot) ─────────────────────────
    if prev:
        p_ce = int(prev.get("total_ce_oi", 0) or 0)
        p_pe = int(prev.get("total_pe_oi", 0) or 0)
        c_ce = int(cur.get("total_ce_oi", 0) or 0)
        c_pe = int(cur.get("total_pe_oi", 0) or 0)
        d_ce = c_ce - p_ce
        d_pe = c_pe - p_pe
        d_spot = spot - float(prev.get("spot", spot) or spot)

        ce_sig = p_ce > 0 and abs(d_ce) / p_ce >= SIG_DOI_FRAC
        pe_sig = p_pe > 0 and abs(d_pe) / p_pe >= SIG_DOI_FRAC
        price_up = d_spot > 0

        quadrant = "neutral"
        bias = 0          # +1 bullish, -1 bearish
        strong = False

        if price_up and pe_sig and d_pe > 0:
            quadrant, bias, strong = "long_buildup", +1, True
        elif (not price_up) and ce_sig and d_ce > 0:
            quadrant, bias, strong = "short_buildup", -1, True
        elif price_up and ce_sig and d_ce < 0:
            quadrant, bias, strong = "long_short_covering", +1, False
        elif (not price_up) and pe_sig and d_pe < 0:
            quadrant, bias, strong = "short_put_unwinding", -1, False

        out["quadrant"] = quadrant
        out["cold_start"] = False

        if bias != 0:
            sig_dir = +1 if lng else -1
            if bias == sig_dir:
                score += SD_CONFIRM_STRONG if strong else SD_CONFIRM_WEAK
                out["patterns"].append(
                    f"oi_{quadrant}" if strong else f"oi_{quadrant}_fade")
            else:
                score += SD_OPPOSE_STRONG if strong else SD_OPPOSE_WEAK
                out["patterns"].append(f"oi_opposes_{direction}")

    out["score_delta"] = int(max(-12, min(12, score)))
    return out


def oi_features(symbol: str, chain: List[Dict], direction: str) -> Dict:
    """Store-backed wrapper: read prior snapshot, record current, diff.

    Reads prev BEFORE recording so the diff is prev→current. Any failure
    degrades to cold-start neutral (never raises into the scan loop).
    """
    try:
        from core.oi_store import get_oi_store
        store = get_oi_store()
        prev = store.prev(symbol)
        cur = store.record(symbol, chain)
        return compute_oi_features(prev, cur, direction)
    except Exception as e:
        log.debug(f"[OISignal] {symbol} degraded to cold-start: {e}")
        return compute_oi_features(None, None, direction)


if __name__ == "__main__":
    # Self-check the 4-quadrant truth table.
    prev = {"total_ce_oi": 100000, "total_pe_oi": 100000, "spot": 100.0,
            "pcr": 1.0, "max_ce_oi_strike": 105, "max_pe_oi_strike": 95}
    cases = [
        ("long",  102.0, 100000, 110000, "long_buildup confirm long"),
        ("short",  98.0, 110000, 100000, "short_buildup confirm short"),
        ("long",  102.0,  90000, 100000, "covering weak long"),
        ("short",  98.0, 100000,  90000, "put unwinding weak short"),
        ("short", 102.0, 100000, 110000, "long_buildup OPPOSES short"),
    ]
    for d, sp, ce, pe, label in cases:
        cur = {"total_ce_oi": ce, "total_pe_oi": pe, "spot": sp, "pcr": 1.0,
               "max_ce_oi_strike": 105, "max_pe_oi_strike": 95}
        f = compute_oi_features(prev, cur, d)
        print(f"{label:38s} -> q={f['quadrant']:<20s} "
              f"sd={f['score_delta']:+d} pats={f['patterns']}")
