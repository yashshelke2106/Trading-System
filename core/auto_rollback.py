"""
Auto-rollback: revert param changes if they hurt performance.

Maintains snapshot history of learned_params.json. Each time adaptive_learner
makes a change, we record a snapshot. If next 20 trades show WR drop > 15%
from rolling baseline, revert to previous snapshot.

Stops the auto-tuner death spiral observed: tuner over-tightens, signals dry up,
sample size drops, more over-tightening.
"""

import json
import logging
import os
import time
from collections import deque
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

ROLLBACK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "logs", "rollback_snapshots.jsonl")
LEARNED_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "logs", "learned_params.json")

EVAL_WINDOW = 20      # trades to evaluate after a change
BAD_WR_DELTA = 0.15   # 15% WR drop triggers rollback
MAX_SNAPSHOTS = 30    # keep last 30 snapshots


class AutoRollback:
    """Snapshot-and-revert system for learned params."""

    def __init__(self):
        self._snapshots: deque = deque(maxlen=MAX_SNAPSHOTS)
        self._post_change_trades: List[Dict] = []
        self._baseline_wr: Optional[float] = None
        self._load()

    def _load(self) -> None:
        if not os.path.exists(ROLLBACK_FILE):
            return
        try:
            with open(ROLLBACK_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._snapshots.append(json.loads(line))
        except Exception as e:
            log.warning(f"[Rollback] load failed: {e}")

    def _save_snapshot(self, snap: Dict) -> None:
        try:
            os.makedirs(os.path.dirname(ROLLBACK_FILE), exist_ok=True)
            with open(ROLLBACK_FILE, "a") as f:
                f.write(json.dumps(snap, default=str) + "\n")
        except Exception as e:
            log.error(f"[Rollback] save failed: {e}")

    def snapshot_before_change(self, current_params: Dict, change_reason: str = "",
                                baseline_wr: float = 0.0) -> None:
        """Call BEFORE applying a change. Saves current state for potential revert."""
        snap = {
            "ts": time.time(),
            "params": json.loads(json.dumps(current_params, default=str)),
            "reason": change_reason,
            "baseline_wr_before": baseline_wr,
        }
        self._snapshots.append(snap)
        self._save_snapshot(snap)
        self._post_change_trades = []   # reset evaluation window
        self._baseline_wr = baseline_wr
        log.info(f"[Rollback] snapshot saved before change: {change_reason}")

    def record_trade(self, outcome: str, pnl: float) -> None:
        """Record trade outcome AFTER a recent change. Triggers rollback check."""
        if not self._snapshots:
            return
        self._post_change_trades.append({
            "outcome": outcome,
            "pnl": pnl,
            "ts": time.time(),
        })

        if len(self._post_change_trades) >= EVAL_WINDOW:
            self._check_for_rollback()

    def _check_for_rollback(self) -> bool:
        """Evaluate post-change WR. Roll back if degraded."""
        if not self._post_change_trades or self._baseline_wr is None:
            return False

        wins = sum(1 for t in self._post_change_trades if t["outcome"] == "TARGET_HIT")
        wr = wins / len(self._post_change_trades)

        if wr < self._baseline_wr - BAD_WR_DELTA:
            log.warning(
                f"[Rollback] WR DEGRADED: post-change={wr:.1%} vs baseline={self._baseline_wr:.1%} "
                f"(drop={(self._baseline_wr - wr):.1%}). Reverting params."
            )
            return self._do_rollback()
        else:
            log.info(
                f"[Rollback] post-change WR={wr:.1%} OK vs baseline={self._baseline_wr:.1%}. "
                "Keeping new params."
            )
            self._post_change_trades = []  # accept new baseline
            return False

    def _do_rollback(self) -> bool:
        """Restore most recent snapshot."""
        if not self._snapshots:
            return False
        snap = self._snapshots[-1]
        try:
            tmp = LEARNED_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "params": snap["params"],
                    "rolled_back_from": snap.get("reason", ""),
                }, f, indent=2, default=str)
            os.replace(tmp, LEARNED_FILE)
            log.warning(f"[Rollback] reverted to snapshot from ts={snap['ts']}")
            self._post_change_trades = []
            return True
        except Exception as e:
            log.error(f"[Rollback] revert failed: {e}")
            return False


_rollback: Optional[AutoRollback] = None


def get_rollback() -> AutoRollback:
    global _rollback
    if _rollback is None:
        _rollback = AutoRollback()
    return _rollback
