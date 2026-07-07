"""
core/swing_learner.py — reinforcement-style outcome learner for the swing
framework. Bandit with Wilson-bound shrinkage, DIRECTION-SYMMETRIC by
construction.

How it learns (honestly):
  - Every resolved trade (win or loss) updates the posterior of its bucket
    (direction x signal x regime). Rewards and penalties are symmetric.
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

State: logs/swing_learner.json (atomic writes). Audit: every update appended
to logs/swing_learner_log.jsonl.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from typing import Dict, Optional

STATE_FILE = os.path.join("logs", "swing_learner.json")
AUDIT_FILE = os.path.join("logs", "swing_learner_log.jsonl")

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
                    self.buckets = json.load(f).get("buckets", {})
            except Exception:
                self.buckets = {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.now().isoformat(timespec="seconds"),
                       "buckets": self.buckets}, f, indent=2)
        os.replace(tmp, self.state_file)

    # ── core API ──────────────────────────────────────────────────────────
    @staticmethod
    def bucket_key(direction: str, signal: str, regime: str) -> str:
        return f"{direction}|{signal}|{regime}"

    def record(self, direction: str, signal: str, regime: str,
               won: bool, ret_net: float, symbol: str = "",
               ts: Optional[str] = None) -> None:
        """Update from ONE resolved trade — win or loss, long or short,
        the exact same arithmetic."""
        key = self.bucket_key(direction, signal, regime)
        b = self.buckets.setdefault(key, {"wins": 0, "losses": 0,
                                          "sum_ret": 0.0, "n": 0})
        b["wins" if won else "losses"] += 1
        b["n"] += 1
        b["sum_ret"] += float(ret_net)
        os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts or datetime.now().isoformat(timespec="seconds"),
                                "bucket": key, "symbol": symbol, "won": won,
                                "ret_net": round(float(ret_net), 5)}) + "\n")

    def weight(self, direction: str, signal: str, regime: str) -> float:
        """Rank multiplier for a candidate. 1.0 = neutral / not enough data."""
        b = self.buckets.get(self.bucket_key(direction, signal, regime))
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
            d, s, r = key.split("|")
            lo, hi = _wilson(b["wins"], b["n"])
            avg = b["sum_ret"] / b["n"] * 1e4 if b["n"] else 0.0
            rows.append(f"  {d:5} | {s:24} | {r:8} | n={b['n']:4} "
                        f"wr={b['wins']/max(b['n'],1)*100:4.1f}% "
                        f"wilson=[{lo*100:.0f},{hi*100:.0f}]% "
                        f"avg={avg:+6.1f}bp weight={self.weight(d, s, r):.2f}")
        return ("SwingLearner buckets (weight moves off 1.0 only past "
                f"n>={MIN_N} + Wilson clears 50%):\n" + "\n".join(rows)
                if rows else "SwingLearner: no resolved trades yet.")
