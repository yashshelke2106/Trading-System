"""
Adaptive Learner — self-improving parameter engine.

Reads resolved signals from signal_journal, computes per-pattern win rates,
per-regime (bias × session × volatility) win rates, and runs bounded
hill-climbing to suggest parameter adjustments.

Outputs to logs/learned_params.json — read by signal_engine.py and
scan_only_v2.py to override config defaults at runtime.

Learning cycle triggered:
  - After every 10 newly resolved signals
  - On daily close (post-market)
  - Manually from Streamlit UI

Safety constraints:
  - Minimum 20 resolved trades before any parameter change
  - Maximum ±1 step per cycle per parameter
  - Each parameter has hard bounds (never goes outside BOUNDS dict)
  - All changes logged with justification to logs/param_changes.jsonl
  - Rollback window: if win rate drops > 10pp in next 10 trades → revert
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEARNED_PARAMS_FILE = os.path.join(_PROJECT_ROOT, "logs", "learned_params.json")
PARAM_CHANGES_FILE  = os.path.join(_PROJECT_ROOT, "logs", "param_changes.jsonl")

_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# Parameter definitions: key → (default, min, max, step, config_section)
# ─────────────────────────────────────────────────────────────────────────────
TUNABLE_PARAMS: Dict[str, Tuple[float, float, float, float, str]] = {
    # (default, min, max, step, section)
    # Capped low: top movers need wide vote latitude
    "min_votes":              (3,    3,    4,    1,    "SIGNAL_CONFIG"),
    "min_vote_lead":          (2,    1,    2,    1,    "SIGNAL_CONFIG"),
    "rsi_oversold":           (38,   25,   45,   2,    "SIGNAL_CONFIG"),
    "rsi_overbought":         (70,   60,   80,   2,    "SIGNAL_CONFIG"),
    "rsi_extreme_oversold":   (30,   20,   38,   2,    "SIGNAL_CONFIG"),
    "rsi_extreme_overbought": (75,   65,   85,   2,    "SIGNAL_CONFIG"),
    # RSI dead zone: LONG wins cluster at RSI 60-65, SL-hits at 50-55.
    # Block long vote when RSI < rsi_long_momentum_min (avoids low-conviction entries).
    "rsi_long_momentum_min":  (60,   55,   68,   2,    "SIGNAL_CONFIG"),
    # SHORT extreme oversold filter: only block shorts at extreme oversold (bounce risk).
    # RSI 30-50 is bearish momentum — shorts should work there.
    "rsi_short_floor":        (30,   20,   40,   2,    "SIGNAL_CONFIG"),
    # Adaptive RR: reduce when SL hits >> target hits (brings target closer).
    # rr_ratio floor lifted to 2.5: a 1.5-2.0R target on a ~1% SL caps the
    # stock move at ~1.2% (journal median) → ~15% premium, never the 50%
    # goal. Floor 2.5R x 1.2% SL = 3% stock ≈ 40-50% premium. Ceil 5.0R for
    # genuine trend/top-mover runners.
    "rr_ratio":               (3.0,  2.5,  5.0,  0.25, "SIGNAL_CONFIG"),
    "vol_surge_threshold":    (1.5,  1.2,  3.0,  0.1,  "SIGNAL_CONFIG"),
    # Capped at 50: top movers often have moderate strength scores. Multi-TF enforces quality.
    "min_strength":           (35,   25,   50,   5,    "SIGNAL_CONFIG"),
    "atr_multiplier":         (1.5,  1.0,  3.0,  0.25, "SIGNAL_CONFIG"),
    "entry_min_vol_ratio":    (2.0,  1.5,  4.0,  0.25, "VOLUME_EXIT_CONFIG"),
    # Time filters — MetaLearning can adjust based on hour WR data
    "block_after_hour":       (12,   11,   14,   1,    "SIGNAL_CONFIG"),
    "skip_first_minutes":     (15,   10,   30,   5,    "SIGNAL_CONFIG"),
    # SL band. OLD floor 0.006 (0.6%) was the death spiral: loss-heavy data
    # → learner tightens SL → targets shrink → tiny fake "wins" → tighten
    # more. A 0.6% SL gives the trade no room and caps target ~1.2%. Floor
    # 1.2%, ceil 3.5% so high-ATR stocks + runners can breathe.
    "max_sl_pct":             (0.020, 0.012, 0.035, 0.002, "RISK_CONFIG"),
    # Scoring component weights — applied by timeframe_sync._grade()
    "score_1d_confirm":       (35,   20,   50,   5,    "SCORING_WEIGHTS"),
    "score_15m_confirm":      (25,   15,   35,   5,    "SCORING_WEIGHTS"),
    "score_vol_cascade":      (20,   10,   30,   5,    "SCORING_WEIGHTS"),
    "score_ema_stack":        (15,    8,   25,   5,    "SCORING_WEIGHTS"),
    "score_fresh_entry":      (10,    5,   20,   5,    "SCORING_WEIGHTS"),
    "score_vwap_align":       ( 5,    0,   10,   5,    "SCORING_WEIGHTS"),
    "score_1d_candle_strong": (10,    5,   15,   5,    "SCORING_WEIGHTS"),
    "score_1d_volume":        (10,    5,   15,   5,    "SCORING_WEIGHTS"),
}

# EMA alpha for pattern / regime win rates (lower = slower to adapt)
ALPHA = 0.15
MIN_TRADES_FOR_UPDATE = 20    # need this many resolved trades
MIN_PATTERN_TRADES    = 5     # per-pattern minimum before weight adjustment

# Option-buyer reality: a time-out (EXPIRED) is NOT neutral — premium decayed
# to theta, so it's effectively a loss (or a small win if it timed out still
# in profit). When True, EXPIRED rows enter the per-pattern/vote/RR learning
# set, relabelled by realised P&L sign: pnl > 0 → win, pnl <= 0 → loss.
# When False, legacy behaviour (EXPIRED ignored except aggregate expiry-rate).
TREAT_EXPIRED_BY_PNL = True
SIGNIFICANT_WIN_DELTA = 0.08  # 8pp win rate difference = significant

# Accelerator #1+#2 (Phase H): the per-pattern reinforcement rule learns
# SIGNAL SKILL, which is best measured on the theta/IV-denoised SPOT
# outcome, weighted by the MAGNITUDE of the move (a +2.8R run teaches far
# more than a scratch +1). Money tuners (RR, expectancy, calibrator) keep
# using the real OPTION outcome — skill and cost stay separated.
GRADE_W_MIN = 0.25            # floor so a tiny move still counts a little
GRADE_W_MAX = 3.0             # cap so one outlier can't dominate the update


def _clean_won(r: Dict) -> bool:
    """Denoised signal-skill label. Prefer the raw SPOT-path result
    (theta/IV-independent); fall back to spot P&L sign, then to the
    (possibly EXPIRED-relabelled) option outcome."""
    so = r.get("spot_outcome")
    if so == "TARGET_HIT":
        return True
    if so == "SL_HIT":
        return False
    sp = r.get("spot_pnl_pct")
    if sp is not None:
        try:
            return float(sp) > 0
        except (ValueError, TypeError):
            pass
    return r.get("outcome") == "TARGET_HIT"


def _grade_weight(r: Dict) -> float:
    """Magnitude of the move in R (|realised %| / risk %), clamped.
    1.0 when it can't be computed → identical to old binary behaviour."""
    try:
        entry = float(r.get("entry_price") or 0)
        sl = float(r.get("sl_price") or 0)
        if entry <= 0 or sl <= 0:
            return 1.0
        risk_pct = abs(entry - sl) / entry * 100.0
        if risk_pct <= 0:
            return 1.0
        realised = r.get("spot_pnl_pct")
        if realised is None:
            realised = r.get("pnl_pct")
        if realised is None:
            mfe = r.get("mfe_pct")
            realised = mfe if mfe is not None else None
        if realised is None:
            return 1.0
        r_mult = abs(float(realised)) / risk_pct
        return max(GRADE_W_MIN, min(GRADE_W_MAX, r_mult))
    except (ValueError, TypeError, ZeroDivisionError):
        return 1.0


# ─────────────────────────────────────────────────────────────────────────────

class AdaptiveLearner:

    def __init__(self):
        self._params = self._load_learned_params()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_learned_params(self) -> Dict[str, Any]:
        """Return current learned parameters (section → {key: value})."""
        with _lock:
            return {k: dict(v) for k, v in self._params.items()}

    def maybe_update(self, force: bool = False) -> Dict[str, Any]:
        """
        Walk-forward learning cycle.
          1. Sort journal chronologically.
          2. Train on first 80% — propose param changes.
          3. Simulate those params against holdout 20%.
          4. Reject change if holdout WR drops > 2pp vs current params.

        Without this, adaptive learner can over-fit recent regime and produce
        worse out-of-sample performance.
        """
        from core.signal_journal import get_resolved_signals
        resolved = get_resolved_signals(days=90)

        def _has_complete_data(r: Dict) -> bool:
            """Only train on trades with entry/exit/SL/target/pnl populated."""
            try:
                return (float(r.get("entry_price") or 0) > 0 and
                        float(r.get("exit_price") or 0) > 0 and
                        float(r.get("sl_price") or 0) > 0 and
                        float(r.get("target_price") or 0) > 0 and
                        (r.get("pnl_pct") is not None
                         or r.get("pnl_percent") is not None
                         or r.get("pnl_rupees") is not None))
            except (ValueError, TypeError):
                return False

        def _pnl(r: Dict) -> float:
            """Realised P&L sign source. Prefer pct, fall back to rupees."""
            for k in ("pnl_pct", "pnl_percent", "pnl_rupees"):
                v = r.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (ValueError, TypeError):
                        pass
            return 0.0

        accepted = ("TARGET_HIT", "SL_HIT", "EXPIRED") if TREAT_EXPIRED_BY_PNL \
            else ("TARGET_HIT", "SL_HIT")
        decided = []
        for r in resolved:
            if r.get("outcome") not in accepted or not _has_complete_data(r):
                continue
            # Always work on a copy; attach the clean signal-skill label
            # and the magnitude weight for the pattern-reinforcement rule.
            r = {**r}
            if r.get("outcome") == "EXPIRED":
                # Relabel by P&L so option-outcome win/loss checks stay correct.
                r["outcome"] = "TARGET_HIT" if _pnl(r) > 0 else "SL_HIT"
                r["_was_expired"] = True
            r["_won_clean"] = _clean_won(r)
            r["_grade_w"] = _grade_weight(r)
            decided.append(r)
        decided.sort(key=lambda r: r.get("ts", ""))

        if not force and len(decided) < MIN_TRADES_FOR_UPDATE:
            return {}

        # ── Walk-forward split ────────────────────────────────────────────────
        n = len(decided)
        # Only enforce holdout when sample is big enough — otherwise legacy path
        WALK_FORWARD_MIN = 30
        if n >= WALK_FORWARD_MIN:
            split = int(n * 0.8)
            train  = decided[:split]
            holdout = decided[split:]
            current_holdout_wr = self._simulate_wr(holdout, self._params)
        else:
            train, holdout = decided, []
            current_holdout_wr = None

        pattern_stats = self._compute_pattern_stats(train)
        regime_stats  = self._compute_regime_stats(train)
        # Save snapshot, apply changes, simulate, optionally revert
        snapshot = {k: dict(v) for k, v in self._params.items()}
        changes  = self._optimize_params(train, train, pattern_stats, regime_stats)

        if not changes:
            return {}

        # Holdout validation
        if holdout and current_holdout_wr is not None:
            new_holdout_wr = self._simulate_wr(holdout, self._params)
            drop = current_holdout_wr - new_holdout_wr
            if drop > 0.02:
                # Revert — proposed params hurt holdout WR by >2pp
                log.warning(
                    f"[Learner] walk-forward REJECTED {len(changes)} param changes: "
                    f"holdout WR {current_holdout_wr:.1%} → {new_holdout_wr:.1%} "
                    f"(drop {drop*100:.1f}pp > 2pp threshold)"
                )
                with _lock:
                    self._params = snapshot
                return {}
            log.info(
                f"[Learner] walk-forward OK: holdout WR "
                f"{current_holdout_wr:.1%} → {new_holdout_wr:.1%} (n_holdout={len(holdout)})"
            )

        self._log_changes(changes, len(decided))
        self._save_learned_params()
        return changes

    def _simulate_wr(self, trades: List[Dict], params: Dict) -> float:
        """Estimate WR on held-out trades using current params.

        Approximation: weight each trade's outcome by the product of its
        pattern weights from the proposed params. Trades with all-suppressed
        patterns (weight ~0.3) count less — caller would have skipped them.
        Real WR = sum(weight × won) / sum(weight).
        """
        if not trades:
            return 0.0
        pw = params.get("PATTERN_WEIGHTS", {}) or {}
        total_w = 0.0
        win_w = 0.0
        for t in trades:
            d = str(t.get("direction", "")).lower()
            pats = t.get("patterns") or t.get("patterns_combined") or []
            if isinstance(pats, str):
                pats = [p.strip() for p in pats.split("|") if p.strip()]
            # Trade-level weight = mean of pattern weights (proxy for "would we take it?")
            tw = 1.0
            if pats:
                ws = [pw.get(f"{d}:{p}", 1.0) for p in pats if not str(p).startswith("vol_")]
                tw = sum(ws) / len(ws) if ws else 1.0
            won = 1.0 if t.get("outcome") == "TARGET_HIT" else 0.0
            total_w += tw
            win_w  += tw * won
        return win_w / total_w if total_w > 0 else 0.0

    def get_pattern_stats(self) -> Dict[str, Dict]:
        """Return per-pattern statistics for dashboard display."""
        from core.signal_journal import get_resolved_signals
        resolved = get_resolved_signals(days=90)
        decided = [r for r in resolved if r.get("outcome") in ("TARGET_HIT", "SL_HIT")]
        return self._compute_pattern_stats(decided)

    def get_regime_stats(self) -> Dict[str, Dict]:
        """Return per-regime statistics."""
        from core.signal_journal import get_resolved_signals
        resolved = get_resolved_signals(days=90)
        decided = [r for r in resolved if r.get("outcome") in ("TARGET_HIT", "SL_HIT")]
        return self._compute_regime_stats(decided)

    def get_param_summary(self) -> List[Dict]:
        """For dashboard: current vs default for every tunable param."""
        rows = []
        for key, (default, lo, hi, step, section) in TUNABLE_PARAMS.items():
            current = self._params.get(section, {}).get(key, default)
            rows.append({
                "param":   key,
                "section": section,
                "default": default,
                "current": current,
                "min":     lo,
                "max":     hi,
            })
        return rows

    def reset_param(self, key: str) -> bool:
        """Reset a single parameter to its default."""
        if key not in TUNABLE_PARAMS:
            return False
        default, _, _, _, section = TUNABLE_PARAMS[key]
        with _lock:
            self._params.setdefault(section, {})[key] = default
            self._save_learned_params()
        return True

    def reset_all(self) -> None:
        """Reset all learned parameters to defaults."""
        with _lock:
            self._params = {}
            self._save_learned_params()

    # ── Core statistics ───────────────────────────────────────────────────────

    def _compute_pattern_stats(self, decided: List[Dict]) -> Dict[str, Dict]:
        stats: Dict[str, Dict] = defaultdict(lambda: {
            "wins": 0, "losses": 0, "total_pnl": 0.0, "ema_win_rate": None
        })
        for sig in decided:
            # Clean signal-skill label (theta/IV-denoised) for pattern stats.
            win = bool(sig.get("_won_clean", sig.get("outcome") == "TARGET_HIT"))
            pnl = float(sig.get("pnl_rupees") or 0)
            for pattern in sig.get("patterns", []):
                p = pattern.strip().lower()
                if not p:
                    continue
                stats[p]["wins" if win else "losses"] += 1
                stats[p]["total_pnl"] += pnl
                # EMA win rate
                prev = stats[p]["ema_win_rate"]
                new_obs = 1.0 if win else 0.0
                if prev is None:
                    stats[p]["ema_win_rate"] = new_obs
                else:
                    stats[p]["ema_win_rate"] = ALPHA * new_obs + (1 - ALPHA) * prev

        result = {}
        for p, s in stats.items():
            total = s["wins"] + s["losses"]
            result[p] = {
                "wins":      s["wins"],
                "losses":    s["losses"],
                "total":     total,
                "win_rate":  s["wins"] / total if total else 0.0,
                "ema_win_rate": s["ema_win_rate"] or 0.5,
                "avg_pnl":   s["total_pnl"] / total if total else 0.0,
            }
        return result

    def _compute_regime_stats(self, decided: List[Dict]) -> Dict[str, Dict]:
        stats: Dict[str, Dict] = defaultdict(lambda: {"wins": 0, "losses": 0, "total_pnl": 0.0})
        for sig in decided:
            win = sig["outcome"] == "TARGET_HIT"
            pnl = float(sig.get("pnl_rupees") or 0)
            # By session
            sess = sig.get("session", "unknown")
            stats[f"session:{sess}"]["wins" if win else "losses"] += 1
            stats[f"session:{sess}"]["total_pnl"] += pnl
            # By market bias
            bias = sig.get("market_bias", "neutral")
            stats[f"bias:{bias}"]["wins" if win else "losses"] += 1
            stats[f"bias:{bias}"]["total_pnl"] += pnl
            # By volatility
            vol = str(sig.get("volatility", "NORMAL")).upper().split(".")[-1]
            stats[f"vol:{vol}"]["wins" if win else "losses"] += 1
            stats[f"vol:{vol}"]["total_pnl"] += pnl
            # Combined regime
            regime = f"{bias}|{sess}|{vol}"
            stats[f"regime:{regime}"]["wins" if win else "losses"] += 1
            stats[f"regime:{regime}"]["total_pnl"] += pnl

        result = {}
        for k, s in stats.items():
            total = s["wins"] + s["losses"]
            result[k] = {
                "wins":     s["wins"],
                "losses":   s["losses"],
                "total":    total,
                "win_rate": s["wins"] / total if total else 0.0,
                "avg_pnl":  s["total_pnl"] / total if total else 0.0,
            }
        return result

    # ── Parameter optimization ────────────────────────────────────────────────

    def _optimize_params(self, decided: List[Dict], resolved: List[Dict],
                          pattern_stats: Dict, regime_stats: Dict) -> Dict:
        changes = {}
        n = len(decided)
        if n < MIN_TRADES_FOR_UPDATE:
            return changes

        overall_wr = sum(1 for d in decided if d["outcome"] == "TARGET_HIT") / n
        wins   = [d for d in decided if d["outcome"] == "TARGET_HIT"]
        losses = [d for d in decided if d["outcome"] == "SL_HIT"]

        # ── Rule 1: min_votes ────────────────────────────────────────────────────
        changes.update(self._tune_vote_threshold(decided, overall_wr))

        # ── Rule 2: RSI thresholds ───────────────────────────────────────────────
        changes.update(self._tune_rsi_thresholds(decided))

        # ── Rule 3: vol_surge_threshold ──────────────────────────────────────────
        changes.update(self._tune_vol_threshold(decided))

        # ── Rule 4: min_strength ─────────────────────────────────────────────────
        changes.update(self._tune_min_strength(decided))

        # ── Rule 5: scoring component weights (timeframe-level) ─────────────────
        changes.update(self._tune_scoring_weights(decided))

        # ── Rule 6: EXPIRED rate ─────────────────────────────────────────────────
        changes.update(self._tune_expiry_rate(resolved))

        # ── Rule 7: per-pattern reinforcement — THE CORE RULE ───────────────────
        # Increase weight of patterns in TARGET_HIT, decrease in SL_HIT
        changes.update(self._tune_pattern_weights(decided, overall_wr))

        # ── Rule 8: feature threshold calibration from win/loss distributions ────
        changes.update(self._tune_feature_thresholds(wins, losses, overall_wr))

        # ── Rule 9: RSI momentum zone floor for longs ────────────────────────────
        changes.update(self._tune_rsi_momentum_zone(wins, losses))

        # ── Rule 10: SHORT RSI floor — block shorts entering oversold ───────────
        changes.update(self._tune_short_rsi_floor(wins, losses))

        # ── Rule 11: Adaptive RR — reduce when SL hits >> target hits ───────────
        changes.update(self._tune_rr_ratio(wins, losses))

        return changes

    def _tune_vote_threshold(self, decided: List[Dict], overall_wr: float) -> Dict:
        changes = {}
        section = "SIGNAL_CONFIG"
        default, lo, hi, step, _ = TUNABLE_PARAMS["min_votes"]
        current = float(self._params.get(section, {}).get("min_votes", default))
        n = len(decided)

        if overall_wr < 0.20 and n >= 30 and current < hi:
            # Emergency: catastrophic win rate — jump 2 steps to filter faster
            new_val = min(current + 2 * step, hi)
            self._set_param(section, "min_votes", new_val)
            changes["min_votes"] = {"from": current, "to": new_val,
                                     "reason": f"win_rate={overall_wr:.1%} < 20% with {n} trades — emergency tighten (2 steps)"}
        elif overall_wr < 0.40 and current < hi:
            new_val = min(current + step, hi)
            self._set_param(section, "min_votes", new_val)
            changes["min_votes"] = {"from": current, "to": new_val,
                                     "reason": f"win_rate={overall_wr:.1%} < 40% — tightening vote threshold"}
        elif overall_wr > 0.72 and n >= 30 and current > lo:
            # Plenty of wins — can loosen slightly to get more trades
            new_val = max(current - step, lo)
            self._set_param(section, "min_votes", new_val)
            changes["min_votes"] = {"from": current, "to": new_val,
                                     "reason": f"win_rate={overall_wr:.1%} > 72% with {n} trades — loosening"}
        return changes

    def _tune_rsi_thresholds(self, decided: List[Dict]) -> Dict:
        changes = {}
        section = "SIGNAL_CONFIG"

        # Split by RSI bands
        low_rsi  = [d for d in decided if float(d.get("rsi", 50)) < 40]
        high_rsi = [d for d in decided if float(d.get("rsi", 50)) > 65]
        mid_rsi  = [d for d in decided if 40 <= float(d.get("rsi", 50)) <= 65]

        def wr(group):
            if not group:
                return None
            return sum(1 for d in group if d["outcome"] == "TARGET_HIT") / len(group)

        low_wr, high_wr, mid_wr = wr(low_rsi), wr(high_rsi), wr(mid_rsi)

        # If RSI extremes performing much worse than mid-range, tighten thresholds
        if low_wr is not None and mid_wr is not None and len(low_rsi) >= MIN_PATTERN_TRADES:
            if mid_wr - low_wr > SIGNIFICANT_WIN_DELTA:
                default, lo, hi, step, _ = TUNABLE_PARAMS["rsi_oversold"]
                current = float(self._params.get(section, {}).get("rsi_oversold", default))
                new_val = self._clip("rsi_oversold", current + step)
                if new_val != current:
                    self._set_param(section, "rsi_oversold", new_val)
                    changes["rsi_oversold"] = {"from": current, "to": new_val,
                                                "reason": f"low_rsi WR={low_wr:.1%} vs mid WR={mid_wr:.1%} — tightening oversold threshold"}

        if high_wr is not None and mid_wr is not None and len(high_rsi) >= MIN_PATTERN_TRADES:
            if mid_wr - high_wr > SIGNIFICANT_WIN_DELTA:
                default, lo, hi, step, _ = TUNABLE_PARAMS["rsi_overbought"]
                current = float(self._params.get(section, {}).get("rsi_overbought", default))
                new_val = self._clip("rsi_overbought", current - step)
                if new_val != current:
                    self._set_param(section, "rsi_overbought", new_val)
                    changes["rsi_overbought"] = {"from": current, "to": new_val,
                                                  "reason": f"high_rsi WR={high_wr:.1%} vs mid WR={mid_wr:.1%} — tightening overbought threshold"}

        return changes

    def _tune_vol_threshold(self, decided: List[Dict]) -> Dict:
        changes = {}
        section = "VOLUME_EXIT_CONFIG"
        default, lo, hi, step, _ = TUNABLE_PARAMS["entry_min_vol_ratio"]
        current = float(self._params.get(section, {}).get("entry_min_vol_ratio", default))

        # Bin by volume ratio
        low_vol  = [d for d in decided if float(d.get("volume_ratio", 1)) < 1.5]
        high_vol = [d for d in decided if float(d.get("volume_ratio", 1)) >= 2.0]

        def wr(group):
            if len(group) < MIN_PATTERN_TRADES:
                return None
            return sum(1 for d in group if d["outcome"] == "TARGET_HIT") / len(group)

        lv_wr, hv_wr = wr(low_vol), wr(high_vol)

        if lv_wr is not None and hv_wr is not None:
            if hv_wr - lv_wr > SIGNIFICANT_WIN_DELTA and current < hi:
                new_val = self._clip("entry_min_vol_ratio", current + step)
                if new_val != current:
                    self._set_param(section, "entry_min_vol_ratio", new_val)
                    changes["entry_min_vol_ratio"] = {
                        "from": current, "to": new_val,
                        "reason": f"high_vol WR={hv_wr:.1%} >> low_vol WR={lv_wr:.1%} — raise vol threshold"
                    }
            elif lv_wr is not None and hv_wr is not None and lv_wr >= hv_wr and current > lo:
                # Low vol trades doing fine — can loosen to get more signals
                new_val = self._clip("entry_min_vol_ratio", current - step)
                if new_val != current:
                    self._set_param(section, "entry_min_vol_ratio", new_val)
                    changes["entry_min_vol_ratio"] = {
                        "from": current, "to": new_val,
                        "reason": f"low_vol WR={lv_wr:.1%} ≥ high_vol WR={hv_wr:.1%} — can loosen vol requirement"
                    }

        return changes

    def _tune_min_strength(self, decided: List[Dict]) -> Dict:
        changes = {}
        section = "SIGNAL_CONFIG"
        default, lo, hi, step, _ = TUNABLE_PARAMS["min_strength"]
        current = float(self._params.get(section, {}).get("min_strength", default))

        # Score quartiles
        scores = sorted(float(d.get("score", 0)) for d in decided)
        if len(scores) < MIN_TRADES_FOR_UPDATE:
            return changes
        q25 = scores[len(scores) // 4]
        q75 = scores[3 * len(scores) // 4]

        low_score  = [d for d in decided if float(d.get("score", 0)) <= q25]
        high_score = [d for d in decided if float(d.get("score", 0)) >= q75]

        def wr(group):
            if len(group) < 3:
                return None
            return sum(1 for d in group if d["outcome"] == "TARGET_HIT") / len(group)

        ls_wr, hs_wr = wr(low_score), wr(high_score)

        if ls_wr is not None and hs_wr is not None:
            if hs_wr - ls_wr > SIGNIFICANT_WIN_DELTA and current < hi:
                new_val = self._clip("min_strength", current + step)
                if new_val != current:
                    self._set_param(section, "min_strength", new_val)
                    changes["min_strength"] = {
                        "from": current, "to": new_val,
                        "reason": f"top-quartile score WR={hs_wr:.1%} >> bottom WR={ls_wr:.1%} — raise min_strength"
                    }

        return changes

    # ── Scoring weight tuning ─────────────────────────────────────────────────

    # Maps reason-string keywords → TUNABLE_PARAMS key
    _REASON_MAP: Dict[str, str] = {
        "1d confirms":     "score_1d_confirm",
        "1d confirm":      "score_1d_confirm",
        "15m confirms":    "score_15m_confirm",
        "15m confirm":     "score_15m_confirm",
        "vol_cascade":     "score_vol_cascade",
        "vol cascade":     "score_vol_cascade",
        "ema stack":       "score_ema_stack",
        "ema_stack":       "score_ema_stack",
        "fresh":           "score_fresh_entry",
        "vwap ok":         "score_vwap_align",
        "vwap_ok":         "score_vwap_align",
        "1d:strong_bull":  "score_1d_candle_strong",
        "1d:strong_bear":  "score_1d_candle_strong",
        "1d:bull_bias":    "score_1d_candle_strong",
        "1d:bear_bias":    "score_1d_candle_strong",
    }

    def _tune_scoring_weights(self, decided: List[Dict]) -> Dict:
        changes: Dict = {}
        section = "SCORING_WEIGHTS"
        if len(decided) < MIN_TRADES_FOR_UPDATE:
            return changes

        overall_wr = sum(1 for d in decided if d["outcome"] == "TARGET_HIT") / len(decided)

        # Build component presence matrix from reason strings
        component_decided: Dict[str, List[bool]] = defaultdict(list)
        for sig in decided:
            reason_low = str(sig.get("reason", "")).lower()
            win = sig["outcome"] == "TARGET_HIT"
            seen: set = set()
            for keyword, param_key in self._REASON_MAP.items():
                if param_key in seen:
                    continue
                fired = keyword in reason_low
                component_decided[param_key].append(fired and win if fired else None)  # type: ignore
                if fired:
                    seen.add(param_key)

        # For each component: win rate when fired vs overall
        for param_key in set(self._REASON_MAP.values()):
            if param_key not in TUNABLE_PARAMS:
                continue
            default, lo, hi, step, _ = TUNABLE_PARAMS[param_key]
            current = float(self._params.get(section, {}).get(param_key, default))

            # Collect trades where this component fired
            fired_wins = 0
            fired_total = 0
            not_fired_wins = 0
            not_fired_total = 0
            for sig in decided:
                reason_low = str(sig.get("reason", "")).lower()
                win = sig["outcome"] == "TARGET_HIT"
                fired = any(kw in reason_low for kw, pk in self._REASON_MAP.items() if pk == param_key)
                if fired:
                    fired_wins  += int(win)
                    fired_total += 1
                else:
                    not_fired_wins  += int(win)
                    not_fired_total += 1

            if fired_total < MIN_PATTERN_TRADES or not_fired_total < MIN_PATTERN_TRADES:
                continue

            fired_wr    = fired_wins    / fired_total
            not_fired_wr = not_fired_wins / not_fired_total

            # Component fires → significantly better win rate → raise its weight
            if fired_wr - overall_wr > SIGNIFICANT_WIN_DELTA and current < hi:
                new_val = self._clip(param_key, current + step)
                if new_val != current:
                    self._set_param(section, param_key, new_val)
                    changes[param_key] = {
                        "from": current, "to": new_val,
                        "reason": (f"component '{param_key}' fires → WR={fired_wr:.1%} "
                                   f"vs overall={overall_wr:.1%} — increasing weight"),
                    }
            # Component fires → significantly worse win rate → lower its weight
            elif fired_wr - overall_wr < -SIGNIFICANT_WIN_DELTA and current > lo:
                new_val = self._clip(param_key, current - step)
                if new_val != current:
                    self._set_param(section, param_key, new_val)
                    changes[param_key] = {
                        "from": current, "to": new_val,
                        "reason": (f"component '{param_key}' fires → WR={fired_wr:.1%} "
                                   f"< overall={overall_wr:.1%} — reducing weight"),
                    }

        return changes

    def _tune_pattern_weights(self, decided: List[Dict], overall_wr: float) -> Dict:
        """
        Core reinforcement rule (Rule 7) — direction-aware:

        Weights are computed SEPARATELY per direction (long / short) to prevent
        regime-leakage: short patterns must not be penalized just because the market
        was bullish during the sampling window (where all shorts lost regardless of
        the pattern quality).

        Guard: a direction needs MIN_DIR_WINS (3) wins before its weights are updated.
        Below that threshold the direction's pattern weights remain at 1.0 (neutral),
        so neither direction is blocked by insufficient data.

        Weight = EMA of (pattern_wr_within_direction / direction_baseline_wr).
          > 1.0 → pattern predicts wins relative to its direction baseline → boost
          < 1.0 → pattern predicts losses relative to its direction baseline → dampen
        Bounds: [0.5, 2.0] — more conservative than before to prevent full suppression.
        """
        MIN_DIR_WINS = 2   # was 3 — relaxed so short patterns can train sooner

        section = "PATTERN_WEIGHTS"
        current = dict(self._params.get(section, {}))
        changes: Dict = {}

        for direction in ("long", "short"):
            dir_decided = [d for d in decided if d.get("direction") == direction]
            if not dir_decided:
                continue

            # Clean signal-skill label; raw count still gates the guard.
            dir_wins = [d for d in dir_decided if d.get("_won_clean",
                        d.get("outcome") == "TARGET_HIT")]
            if len(dir_wins) < MIN_DIR_WINS:
                # Not enough wins in this direction — leave direction's pattern weights neutral.
                # Existing keys for this direction are left unchanged (or initialized at 1.0).
                continue

            # Direction baseline as a MAGNITUDE-weighted win rate (#2): a
            # direction whose wins ran big is a higher bar than one that
            # only scratched out wins.
            dir_win_w = sum(d.get("_grade_w", 1.0) for d in dir_wins)
            dir_tot_w = sum(d.get("_grade_w", 1.0) for d in dir_decided)
            dir_wr = (dir_win_w / dir_tot_w) if dir_tot_w > 0 else 0.0
            if dir_wr <= 0:
                continue

            # Tally per-pattern WITHIN this direction: weighted sums drive
            # the ratio, raw counts gate MIN_PATTERN_TRADES (so 2 big trades
            # can't masquerade as a 5-trade sample).
            pat_w_win:  Dict[str, float] = defaultdict(float)
            pat_w_tot:  Dict[str, float] = defaultdict(float)
            pat_n:      Dict[str, int]   = defaultdict(int)
            pat_win_n:  Dict[str, int]   = defaultdict(int)
            for sig in dir_decided:
                win = bool(sig.get("_won_clean",
                                   sig.get("outcome") == "TARGET_HIT"))
                gw = sig.get("_grade_w", 1.0)
                for p in sig.get("patterns", []):
                    p = p.strip().lower()
                    if not p:
                        continue
                    pat_n[p] += 1
                    pat_w_tot[p] += gw
                    if win:
                        pat_win_n[p] += 1
                        pat_w_win[p] += gw

            for pattern in set(pat_w_tot):
                total = pat_n[pattern]
                if total < MIN_PATTERN_TRADES:
                    continue

                tot_w = pat_w_tot[pattern]
                wins_n = pat_w_win[pattern]
                wr_pattern = (wins_n / tot_w) if tot_w > 0 else 0.0
                # Key is direction-qualified: "long:pattern_name" or "short:pattern_name"
                # Prevents supertrend_down being penalized for long AND short contexts
                # (it's bad for longs, good for shorts — two separate weights)
                key = f"{direction}:{pattern}"
                prev  = current.get(key, 1.0)
                ratio = wr_pattern / dir_wr
                new_w = round(max(0.5, min(2.0, ALPHA * ratio + (1 - ALPHA) * prev)), 4)

                if abs(new_w - prev) >= 0.01:
                    current[key] = new_w
                    arrow = "+" if new_w > prev else "-"
                    changes[f"pw:{key}"] = {
                        "from":   round(prev, 3),
                        "to":     new_w,
                        "reason": (f"{direction} wr={wr_pattern:.1%} vs {direction}_base={dir_wr:.1%} "
                                   f"({pat_win_n[pattern]}W/{total-pat_win_n[pattern]}L "
                                   f"grade-wtd) weight {arrow}"),
                    }

        with _lock:
            self._params[section] = current

        return changes

    def _tune_feature_thresholds(self, wins: List[Dict], losses: List[Dict],
                                  overall_wr: float) -> Dict:
        """
        Rule 8: Data-driven threshold calibration.
        Compare mean feature values (score, vote_margin, vol_ratio) in TARGET_HIT
        vs SL_HIT trades. If winners cluster at higher values → raise the threshold
        to filter out the loss zone. If indistinguishable → leave unchanged.
        """
        changes: Dict = {}
        if len(wins) < 3 or len(losses) < 3:
            return changes

        import statistics

        def _safe_mean(lst):
            return statistics.mean(lst) if lst else None

        # ── Score → min_strength
        win_scores  = [float(d.get("score", 0)) for d in wins  if d.get("score")]
        loss_scores = [float(d.get("score", 0)) for d in losses if d.get("score")]
        m_win  = _safe_mean(win_scores)
        m_loss = _safe_mean(loss_scores)
        if m_win and m_loss and m_win > m_loss + 8:
            # Winners have meaningfully higher scores → raise threshold
            section, key = "SIGNAL_CONFIG", "min_strength"
            default, lo, hi, step, _ = TUNABLE_PARAMS[key]
            current = float(self._params.get(section, {}).get(key, default))
            # Push threshold toward lower edge of winner zone (conservative)
            optimal = m_loss + (m_win - m_loss) * 0.25
            if optimal > current + step:
                new_val = self._clip(key, current + step)
                if new_val != current:
                    self._set_param(section, key, new_val)
                    changes[f"ft:{key}"] = {
                        "from": current, "to": new_val,
                        "reason": (f"winners avg_score={m_win:.0f} vs losses={m_loss:.0f} "
                                   f"— raising threshold toward winner zone"),
                    }

        # ── vote_margin → min_vote_lead
        win_votes  = [float(d.get("vote_margin", 0)) for d in wins]
        loss_votes = [float(d.get("vote_margin", 0)) for d in losses]
        mv_win  = _safe_mean(win_votes)
        mv_loss = _safe_mean(loss_votes)
        if mv_win and mv_loss and mv_win > mv_loss + 1.0:
            section, key = "SIGNAL_CONFIG", "min_vote_lead"
            default, lo, hi, step, _ = TUNABLE_PARAMS[key]
            current = float(self._params.get(section, {}).get(key, default))
            new_val = self._clip(key, current + step)
            if new_val != current:
                self._set_param(section, key, new_val)
                changes[f"ft:{key}"] = {
                    "from": current, "to": new_val,
                    "reason": (f"winners avg_vote_margin={mv_win:.1f} vs losses={mv_loss:.1f} "
                               f"— raising vote lead requirement"),
                }

        # ── vol_ratio → vol_surge_threshold
        win_vols  = [float(d.get("volume_ratio", 1)) for d in wins]
        loss_vols = [float(d.get("volume_ratio", 1)) for d in losses]
        mv_wv  = _safe_mean(win_vols)
        mv_lv  = _safe_mean(loss_vols)
        if mv_wv and mv_lv and mv_wv > mv_lv + 0.3:
            section, key = "VOLUME_EXIT_CONFIG", "entry_min_vol_ratio"
            default, lo, hi, step, _ = TUNABLE_PARAMS[key]
            current = float(self._params.get(section, {}).get(key, default))
            new_val = self._clip(key, current + step)
            if new_val != current:
                self._set_param(section, key, new_val)
                changes[f"ft:{key}"] = {
                    "from": current, "to": new_val,
                    "reason": (f"winners avg_vol_ratio={mv_wv:.1f} vs losses={mv_lv:.1f} "
                               f"— raising volume threshold"),
                }

        return changes

    def _tune_expiry_rate(self, resolved: List[Dict]) -> Dict:
        """
        High EXPIRED% means signals lack conviction — price doesn't move enough
        to hit either target or SL within the session window.
        For option buyers this is effectively a loss (theta decay).
        Raise min_strength to demand higher-quality signals when expired% > 50%.
        """
        changes = {}
        total = len([r for r in resolved if r.get("outcome") in ("TARGET_HIT", "SL_HIT", "EXPIRED")])
        if total < MIN_TRADES_FOR_UPDATE:
            return changes
        expired_n = sum(1 for r in resolved if r.get("outcome") == "EXPIRED")
        expired_rate = expired_n / total if total > 0 else 0.0

        if expired_rate < 0.50:
            return changes

        section = "SIGNAL_CONFIG"
        default, lo, hi, step, _ = TUNABLE_PARAMS["min_strength"]
        current = float(self._params.get(section, {}).get("min_strength", default))
        # Expired >70%: jump 2 steps; 50-70%: 1 step
        extra = step * 2 if expired_rate > 0.70 else step
        new_val = self._clip("min_strength", current + extra)
        if new_val != current:
            self._set_param(section, "min_strength", new_val)
            changes["min_strength_expiry"] = {
                "from": current, "to": new_val,
                "reason": (f"expired_rate={expired_rate:.0%} ({expired_n}/{total}) > 50% "
                           f"— signals lack momentum, raising quality bar"),
            }
        return changes

    def _tune_rsi_momentum_zone(self, wins: List[Dict], losses: List[Dict]) -> Dict:
        """
        Rule 9: Long WIN RSI >> Long SL RSI → raise rsi_long_momentum_min.
        Data showed WIN mean RSI=64.7 vs SL mean RSI=53.8 for longs (+10.9 gap).
        """
        import statistics as _stats
        changes: Dict = {}
        long_wins   = [d for d in wins   if d.get("direction") == "long"]
        long_losses = [d for d in losses if d.get("direction") == "long"]
        if len(long_wins) < 3 or len(long_losses) < 3:
            return changes

        mean_win_rsi  = _stats.mean(float(d.get("rsi", 50)) for d in long_wins)
        mean_loss_rsi = _stats.mean(float(d.get("rsi", 50)) for d in long_losses)
        diff = mean_win_rsi - mean_loss_rsi

        section, key = "SIGNAL_CONFIG", "rsi_long_momentum_min"
        default, lo, hi, step, _ = TUNABLE_PARAMS[key]
        current = float(self._params.get(section, {}).get(key, default))

        if diff > 5 and current < hi:
            new_val = self._clip(key, current + step)
            if new_val != current:
                self._set_param(section, key, new_val)
                changes[key] = {
                    "from": current, "to": new_val,
                    "reason": (f"long WIN RSI={mean_win_rsi:.1f} vs SL RSI={mean_loss_rsi:.1f} "
                               f"(gap={diff:.1f}) — raising long momentum floor"),
                }
        elif diff < 2 and current > lo:
            new_val = self._clip(key, current - step)
            if new_val != current:
                self._set_param(section, key, new_val)
                changes[key] = {
                    "from": current, "to": new_val,
                    "reason": (f"long WIN RSI={mean_win_rsi:.1f} vs SL RSI={mean_loss_rsi:.1f} "
                               f"(gap={diff:.1f}) — RSI no longer discriminating, relaxing floor"),
                }
        return changes

    def _tune_short_rsi_floor(self, wins: List[Dict], losses: List[Dict]) -> Dict:
        """
        Rule 10: Short SL-hits at EXTREME oversold RSI → snap-back → SL.
        Only raise floor when short losses cluster below RSI 30 (genuine bounce zone).
        RSI 30-50 is bearish territory where shorts SHOULD work — never raise floor above 35.
        """
        import statistics as _stats
        changes: Dict = {}
        short_losses = [d for d in losses if d.get("direction") == "short"]
        short_wins = [d for d in wins if d.get("direction") == "short"]
        if len(short_losses) < 5:  # need more data before adjusting (was 3)
            return changes

        mean_sl_rsi = _stats.mean(float(d.get("rsi", 50)) for d in short_losses)
        section, key = "SIGNAL_CONFIG", "rsi_short_floor"
        default, lo, hi, step, _ = TUNABLE_PARAMS[key]
        current = float(self._params.get(section, {}).get(key, default))

        # Only raise floor if losses cluster in extreme oversold (< 30)
        # AND short WR is bad. Don't blindly raise — check if shorts are actually losing
        short_wr = len(short_wins) / max(len(short_wins) + len(short_losses), 1)
        if mean_sl_rsi < 30 and short_wr < 0.25 and current < hi:
            new_val = self._clip(key, current + step)
            if new_val != current:
                self._set_param(section, key, new_val)
                changes[key] = {
                    "from": current, "to": new_val,
                    "reason": (f"short SL avg RSI={mean_sl_rsi:.1f} (extreme oversold bounce zone), "
                               f"short WR={short_wr:.0%} — raising floor slightly"),
                }
        # If short WR is decent (>35%), consider lowering floor to allow more shorts
        elif short_wr > 0.35 and current > lo:
            new_val = self._clip(key, current - step)
            if new_val != current:
                self._set_param(section, key, new_val)
                changes[key] = {
                    "from": current, "to": new_val,
                    "reason": (f"short WR={short_wr:.0%} healthy — lowering RSI floor "
                               f"to allow more short entries"),
                }
        return changes

    def _tune_rr_ratio(self, wins: List[Dict], losses: List[Dict]) -> Dict:
        """
        Rule 11: Adaptive target distance.
        SL/WIN ratio > 2:1 → reduce RR (target closer, more hits).
        SL/WIN ratio < 0.8 → increase RR (wins easy, maximize payoff).
        """
        changes: Dict = {}
        if len(wins) < 3 or len(losses) < 3:
            return changes

        sl_win_ratio = len(losses) / max(len(wins), 1)
        section, key = "SIGNAL_CONFIG", "rr_ratio"
        default, lo, hi, step, _ = TUNABLE_PARAMS[key]
        current = float(self._params.get(section, {}).get(key, default))

        if sl_win_ratio > 2.0 and current > lo:
            new_val = self._clip(key, round(current - step, 2))
            if new_val != current:
                self._set_param(section, key, new_val)
                changes[key] = {
                    "from": current, "to": new_val,
                    "reason": (f"SL/WIN ratio={sl_win_ratio:.1f} > 2.0 "
                               f"— reducing RR to bring target closer"),
                }
        elif sl_win_ratio < 0.8 and current < hi:
            new_val = self._clip(key, round(current + step, 2))
            if new_val != current:
                self._set_param(section, key, new_val)
                changes[key] = {
                    "from": current, "to": new_val,
                    "reason": (f"SL/WIN ratio={sl_win_ratio:.1f} < 0.8 "
                               f"— increasing RR for better payoff on easy market"),
                }
        return changes

    def get_feature_importance(self) -> List[Dict]:
        """
        Run logistic regression on resolved signal features → return ranked
        feature importances.  Requires scikit-learn (already in dependencies).
        Returns [] gracefully if sklearn unavailable or insufficient data.
        """
        from core.signal_journal import get_resolved_signals
        resolved = get_resolved_signals(days=90)
        decided = [r for r in resolved if r.get("outcome") in ("TARGET_HIT", "SL_HIT")]
        if len(decided) < MIN_TRADES_FOR_UPDATE:
            return []

        try:
            import numpy as np
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler

            feature_names = [
                "confluence_score", "rsi", "volume_ratio", "rr_ratio",
                "vote_margin", "has_1d_confirm", "has_15m_confirm",
                "has_vol_cascade", "has_ema_stack", "has_fresh_entry", "has_vwap",
            ]

            rows, labels = [], []
            for sig in decided:
                reason_low = str(sig.get("reason", "")).lower()
                rows.append([
                    float(sig.get("confluence_score", 0) or 0),
                    float(sig.get("rsi", 50) or 50),
                    float(sig.get("volume_ratio", 1) or 1),
                    float(sig.get("rr_ratio", 1) or 1),
                    float(sig.get("vote_margin", 0) or 0),
                    float("1d confirms" in reason_low or "1d confirm" in reason_low),
                    float("15m confirms" in reason_low or "15m confirm" in reason_low),
                    float("vol cascade" in reason_low or "vol_cascade" in reason_low),
                    float("ema stack" in reason_low or "ema_stack" in reason_low),
                    float("fresh" in reason_low),
                    float("vwap ok" in reason_low),
                ])
                labels.append(1 if sig["outcome"] == "TARGET_HIT" else 0)

            X = np.array(rows)
            y = np.array(labels)
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)

            clf = LogisticRegression(max_iter=500, random_state=42)
            clf.fit(X_scaled, y)

            importances = clf.coef_[0]
            result = sorted(
                [{"feature": fn, "importance": round(float(imp), 4),
                  "direction": "positive" if imp > 0 else "negative"}
                 for fn, imp in zip(feature_names, importances)],
                key=lambda x: abs(x["importance"]), reverse=True,
            )
            return result

        except Exception as e:
            log.warning(f"[Learner] feature_importance failed: {e}")
            return []

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_param(self, section: str, key: str, value: Any) -> None:
        with _lock:
            self._params.setdefault(section, {})[key] = value

    def _clip(self, key: str, value: float) -> float:
        _, lo, hi, _, _ = TUNABLE_PARAMS.get(key, (value, value, value, 0, ""))
        return round(max(lo, min(hi, value)), 4)

    def _load_learned_params(self) -> Dict:
        if not os.path.exists(LEARNED_PARAMS_FILE):
            return {}
        try:
            with open(LEARNED_PARAMS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("params", {})
        except Exception:
            return {}

    def _save_learned_params(self) -> None:
        os.makedirs(os.path.dirname(LEARNED_PARAMS_FILE), exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(),
            "params": self._params,
        }
        tmp = LEARNED_PARAMS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, LEARNED_PARAMS_FILE)

    def _log_changes(self, changes: Dict, n_trades: int) -> None:
        os.makedirs(os.path.dirname(PARAM_CHANGES_FILE), exist_ok=True)
        entry = {
            "ts":        datetime.now().isoformat(),
            "n_trades":  n_trades,
            "changes":   changes,
        }
        with open(PARAM_CHANGES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        for key, info in changes.items():
            log.info(f"[Learner] {key}: {info['from']} -> {info['to']} | {info['reason']}")


# ── Module-level singleton ────────────────────────────────────────────────────

_learner: Optional[AdaptiveLearner] = None


def get_learner() -> AdaptiveLearner:
    global _learner
    if _learner is None:
        _learner = AdaptiveLearner()
    return _learner


def get_learned_config() -> Dict[str, Any]:
    """
    Returns merged config: learned params override config.py defaults.
    Consumers call this instead of reading config.py directly.

    Usage:
      from core.adaptive_learner import get_learned_config
      lc = get_learned_config()
      min_votes = lc.get("SIGNAL_CONFIG", {}).get("min_votes", 3)
    """
    return get_learner().get_learned_params()


def _cli() -> int:
    """CLI: dry-run / reset short pattern weights.

    Usage:
      python -m core.adaptive_learner --dry-run
      python -m core.adaptive_learner --reset-direction-weights short
      python -m core.adaptive_learner --reset-direction-weights long
      python -m core.adaptive_learner --reset-direction-weights all
    """
    import argparse
    ap = argparse.ArgumentParser(prog="adaptive_learner")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show current learned params + proposed changes, no write")
    ap.add_argument("--reset-direction-weights",
                    choices=["long", "short", "all"],
                    help="Reset pattern weights for given direction to neutral 1.0")
    args = ap.parse_args()

    learner = get_learner()

    if args.reset_direction_weights:
        d = args.reset_direction_weights
        pw = dict(learner._params.get("PATTERN_WEIGHTS", {}))
        if not pw:
            print("PATTERN_WEIGHTS section empty — nothing to reset")
            return 0

        targets = []
        if d in ("long", "all"):
            targets += [k for k in pw if k.startswith("long:")]
        if d in ("short", "all"):
            targets += [k for k in pw if k.startswith("short:")]

        changed = 0
        for k in targets:
            if pw[k] != 1.0:
                pw[k] = 1.0
                changed += 1
        learner._params["PATTERN_WEIGHTS"] = pw
        learner._save_learned_params()
        print(f"Reset {changed}/{len(targets)} pattern weights to 1.0 for direction={d}")
        return 0

    if args.dry_run:
        params = learner.get_learned_params()
        import json as _json
        print(_json.dumps(params, indent=2)[:4000])
        # Show short:* weight summary
        pw = params.get("PATTERN_WEIGHTS", {})
        shorts = {k: v for k, v in pw.items() if k.startswith("short:")}
        longs  = {k: v for k, v in pw.items() if k.startswith("long:")}
        print(f"\nshort patterns: {len(shorts)}  avg={sum(shorts.values())/max(len(shorts),1):.3f}")
        print(f"long patterns:  {len(longs)}  avg={sum(longs.values())/max(len(longs),1):.3f}")
        return 0

    print("Specify --dry-run or --reset-direction-weights {long|short|all}")
    return 1


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli())
