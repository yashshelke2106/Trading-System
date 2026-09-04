"""
core/swing_learner.py — reinforcement-style outcome learner for the swing
framework. Bandit with Wilson-bound shrinkage, DIRECTION-SYMMETRIC by
construction.

How it learns (honestly):
  - Every resolved trade (win or loss) updates the posterior of its bucket.
    Rewards and penalties are symmetric.
  - Buckets are CONSOLIDATED at direction x regime (2026-07-16): the three
    entry signals were proven statistically indistinguishable over 15 years
    (docs/research/deep_entry_gate.md, paired t=-0.01), so per-signal buckets
    tripled time-to-significance while encoding a non-difference. Pooling
    concentrates ~3x more trades per bucket — faster learning WITHOUT
    lowering the evidence bar. Per-signal counts are still recorded under
    by_signal for a future re-split if volume ever justifies one.
  - A bucket's rank weight moves away from 1.0 ONLY when its Wilson 95%
    lower/upper bound clears the 50% baseline — i.e. when the evidence is
    strong, not when 7 trades got lucky. Below MIN_N the weight stays 1.0.
  - Weights are clamped to [0.5, 1.5]: the learner can NEVER fully mute or
    mania-boost a bucket. It re-ranks; it doesn't hallucinate new signals.

Anti-bias guarantees:
  - Identical priors, identical update rule, identical clamps for long and
    short buckets. No direction is privileged in code.
  - NOTE the honest asymmetry that remains: the MARKET drifts up (equity
    premium), so long buckets will earn better posteriors over time. That is
    evidence, not bias. Forcing 50/50 long/short would itself be a bias.

State: logs/swing_learner.json (atomic writes; old per-signal state files
migrate automatically on load). Audit: every update appended to
logs/swing_learner_log.jsonl.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from typing import Dict, Optional

STATE_FILE = os.path.join("logs", "swing_learner.json")
AUDIT_FILE = os.path.join("logs", "swing_learner_log.jsonl")

# Directions whose rank weight is pinned to 1.0 regardless of posterior.
# This IS an asymmetry, and the module above otherwise promises none -- so
# state the evidence rather than bury it. Re-resolving 6,188 short signals
# (2017-2026, point-in-time archive) under ten different exit policies put the
# short side under water in EVERY one: PF 0.68-0.83, t = -4.8 to -15.8.
# Against that, the live bucket read short|risk_off at PF 3.19 on 28 trades
# and was handing shorts a 1.06x rank boost. Twenty-eight observations cannot
# overturn 6,188, and weight() cannot tell the difference because it scores
# WIN RATE -- which is precisely the statistic the short book flatters
# (71% wins at PF 0.72).
#
# Shorts are still RECORDED: falsification evidence is the point of the paper
# bench, and freezing the weight keeps that evidence out of the ranker without
# blinding the journal. Funding is blocked separately in swing_screen.py.
# The unlock condition is unchanged: docs/research/short_side_policy.md.
WEIGHT_FROZEN_DIRECTIONS = frozenset({"short"})

MIN_N = 20          # observations before a weight may leave 1.0
W_MIN, W_MAX = 0.5, 1.5
BASELINE = 0.5      # coin-flip win-rate baseline
Z = 1.96            # Wilson 95%


def _wilson(wins: int, n: int) -> tuple:
    """95% Wilson score interval for a win rate."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + Z * Z / n
    centre = p + Z * Z / (2 * n)
    margin = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n))
    return (centre - margin) / denom, (centre + margin) / denom


class SwingLearner:
    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self.buckets: Dict[str, Dict] = {}
        self._load()

    # ── persistence ───────────────────────────────────────────────────────
    def _load(self) -> None:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, encoding="utf-8") as f:
                    raw = json.load(f).get("buckets", {})
            except Exception:
                raw = {}
            self.buckets = self._migrate(raw)

    @staticmethod
    def _migrate(raw: Dict) -> Dict:
        """Fold legacy per-signal keys (direction|signal|regime) into pooled
        direction|regime buckets, preserving per-signal counts."""
        out: Dict[str, Dict] = {}
        for key, b in raw.items():
            parts = key.split("|")
            if len(parts) == 2:                      # already pooled
                out.setdefault(key, {"wins": 0, "losses": 0, "sum_ret": 0.0,
                                     "n": 0, "by_signal": {}})
                dst = out[key]
                for f in ("wins", "losses", "n"):
                    dst[f] += b.get(f, 0)
                dst["sum_ret"] += b.get("sum_ret", 0.0)
                for sig, sb in b.get("by_signal", {}).items():
                    d = dst["by_signal"].setdefault(sig, {"wins": 0, "losses": 0, "n": 0})
                    for f in ("wins", "losses", "n"):
                        d[f] += sb.get(f, 0)
                continue
            direction, signal, regime = parts        # legacy 3-part key
            pooled = f"{direction}|{regime}"
            dst = out.setdefault(pooled, {"wins": 0, "losses": 0, "sum_ret": 0.0,
                                          "n": 0, "by_signal": {}})
            for f in ("wins", "losses", "n"):
                dst[f] += b.get(f, 0)
            dst["sum_ret"] += b.get("sum_ret", 0.0)
            sb = dst["by_signal"].setdefault(signal, {"wins": 0, "losses": 0, "n": 0})
            for f in ("wins", "losses", "n"):
                sb[f] += b.get(f, 0)
        return out

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.now().isoformat(timespec="seconds"),
                       "buckets": self.buckets}, f, indent=2)
        os.replace(tmp, self.state_file)

    # ── core API ──────────────────────────────────────────────────────────
    @staticmethod
    def bucket_key(direction: str, regime: str) -> str:
        """Pooled trust bucket. Signal is deliberately NOT part of the key
        (indistinguishable over 15y — deep_entry_gate.md)."""
        return f"{direction}|{regime}"

    def record(self, direction: str, signal: str, regime: str,
               won: bool, ret_net: float, symbol: str = "",
               ts: Optional[str] = None) -> None:
        """Update from ONE resolved trade — win or loss, long or short,
        the exact same arithmetic. Pooled bucket + per-signal sub-count."""
        key = self.bucket_key(direction, regime)
        b = self.buckets.setdefault(key, {"wins": 0, "losses": 0,
                                          "sum_ret": 0.0, "n": 0,
                                          "by_signal": {}})
        b["wins" if won else "losses"] += 1
        b["n"] += 1
        b["sum_ret"] += float(ret_net)
        sb = b.setdefault("by_signal", {}).setdefault(
            signal, {"wins": 0, "losses": 0, "n": 0})
        sb["wins" if won else "losses"] += 1
        sb["n"] += 1
        os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts or datetime.now().isoformat(timespec="seconds"),
                                "bucket": key, "signal": signal, "symbol": symbol,
                                "won": won,
                                "ret_net": round(float(ret_net), 5)}) + "\n")

    def weight(self, direction: str, signal: str, regime: str) -> float:
        """Rank multiplier for a candidate. Signature keeps the signal arg so
        callers are unchanged, but trust is pooled per direction x regime.
        1.0 = neutral / not enough data."""
        if direction in WEIGHT_FROZEN_DIRECTIONS:
            return 1.0
        b = self.buckets.get(self.bucket_key(direction, regime))
        if not b or b["n"] < MIN_N:
            return 1.0
        lo, hi = _wilson(b["wins"], b["n"])
        if lo > BASELINE:            # evidence the bucket wins MORE than chance
            w = 1.0 + min((lo - BASELINE) * 2.0, W_MAX - 1.0)
        elif hi < BASELINE:          # evidence it wins LESS than chance
            w = 1.0 - min((BASELINE - hi) * 2.0, 1.0 - W_MIN)
        else:                        # interval straddles 50% -> no verdict yet
            w = 1.0
        return round(max(W_MIN, min(W_MAX, w)), 3)

    def report(self) -> str:
        rows = []
        for key, b in sorted(self.buckets.items()):
            d, r = key.split("|")
            lo, hi = _wilson(b["wins"], b["n"])
            avg = b["sum_ret"] / b["n"] * 1e4 if b["n"] else 0.0
            rows.append(f"  {d:5} | {r:8} | n={b['n']:4} "
                        f"wr={b['wins']/max(b['n'],1)*100:4.1f}% "
                        f"wilson=[{lo*100:.0f},{hi*100:.0f}]% "
                        f"avg={avg:+6.1f}bp weight={self.weight(d, '*', r):.2f}")
            for sig, sb in sorted(b.get("by_signal", {}).items()):
                rows.append(f"          - {sig:<22} n={sb['n']:3} "
                            f"wr={sb['wins']/max(sb['n'],1)*100:4.1f}%")
        return ("SwingLearner buckets (pooled direction x regime; weight moves "
                f"off 1.0 only past n>={MIN_N} + Wilson clears 50%):\n"
                + "\n".join(rows)
                if rows else "SwingLearner: no resolved trades yet.")
