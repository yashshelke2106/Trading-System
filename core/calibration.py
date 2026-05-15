"""
Confidence calibration: compare predicted win prob vs actual outcomes.

Buckets predictions by confidence band (e.g. 50-60%, 60-70%, etc.).
For each band, computes actual WR. Should match prediction.

Mismatch = drift = system's predictions are unreliable.
Triggers alert if Brier score > 0.30 (poor calibration).

Brier score = mean((prediction - actual)^2). 0 = perfect, 0.25 = random.
"""

import json
import logging
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

CALIB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "logs", "calibration.json")

BUCKETS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)]
MIN_PER_BUCKET = 10
ALERT_BRIER = 0.30


class CalibrationTracker:
    """Predicted vs actual WR tracking."""

    def __init__(self):
        self._predictions: List[Dict] = []   # [{predicted, actual, ts}]
        self._load()

    def _load(self) -> None:
        if os.path.exists(CALIB_FILE):
            try:
                with open(CALIB_FILE) as f:
                    self._predictions = json.load(f).get("predictions", [])
            except Exception as e:
                log.warning(f"[Calib] load failed: {e}")

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(CALIB_FILE), exist_ok=True)
            tmp = CALIB_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "predictions": self._predictions[-500:],  # cap
                    "updated": time.time(),
                }, f, default=str)
            os.replace(tmp, CALIB_FILE)
        except Exception as e:
            log.error(f"[Calib] save failed: {e}")

    def record(self, predicted_prob: float, outcome: str, sym: str = "") -> None:
        actual = 1.0 if outcome == "TARGET_HIT" else 0.0
        self._predictions.append({
            "predicted": predicted_prob,
            "actual": actual,
            "symbol": sym,
            "ts": time.time(),
        })
        self._predictions = self._predictions[-500:]
        self._save()

    def brier_score(self, n_recent: int = 100) -> Optional[float]:
        """Brier score over recent N predictions. Lower = better calibration."""
        recent = self._predictions[-n_recent:]
        if len(recent) < 20:
            return None
        return sum((p["predicted"] - p["actual"]) ** 2 for p in recent) / len(recent)

    def get_buckets(self) -> Dict[tuple, Dict]:
        """Per-bucket: predicted vs actual WR."""
        buckets: Dict[tuple, List] = defaultdict(list)
        for p in self._predictions:
            for low, high in BUCKETS:
                if low <= p["predicted"] < high:
                    buckets[(low, high)].append(p)
                    break

        result = {}
        for bucket, preds in buckets.items():
            if len(preds) < MIN_PER_BUCKET:
                continue
            avg_pred = sum(p["predicted"] for p in preds) / len(preds)
            actual_wr = sum(p["actual"] for p in preds) / len(preds)
            result[bucket] = {
                "n": len(preds),
                "predicted_wr": avg_pred,
                "actual_wr": actual_wr,
                "calibration_error": abs(avg_pred - actual_wr),
            }
        return result

    def is_drifting(self) -> bool:
        bs = self.brier_score()
        if bs is None:
            return False
        return bs > ALERT_BRIER


_calib: Optional[CalibrationTracker] = None


def get_calibration() -> CalibrationTracker:
    global _calib
    if _calib is None:
        _calib = CalibrationTracker()
    return _calib
