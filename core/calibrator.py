"""
Score calibrator — turns the raw additive ``confluence_score`` into an
HONEST probability of the trade hitting target before SL.

Why this exists
---------------
``confluence_score`` is an unbounded integer pile of bonuses. A "72" does
not mean "wins 72% of the time" — it means nothing until mapped to
realised outcomes. The trade ranker treated the raw score as quality and
fired the top N. That sprays trades across a flat ~42% base rate.

A genius option buyer does the opposite: estimate P(win) per setup,
fire ONLY the top-decile confidence, skip the rest. Same engine, same
signals — 42% spray becomes a 60%+ sniper, at the cost of trade count.
That selectivity is the only honest path to a 60-65% *traded* hit rate
(theta forbids it as a blanket number).

Method
------
Isotonic regression (Pool-Adjacent-Violators). It is *monotone by
construction* — higher score never maps to lower P(win) — so it cannot
invert the engine's own ordering, and it has no free weights to overfit
(the monotonicity constraint IS the regularisation). Fit on resolved,
current-engine journal rows only; legacy garbage excluded.

Honest-by-design
----------------
* Labels use the SAME win/loss convention as adaptive_learner:
  TARGET_HIT = win; SL_HIT = loss; EXPIRED relabelled by realised P&L
  sign (timeout with premium decayed = loss for an option buyer).
* Until ``MIN_CALIB`` real outcomes exist, ``predict`` returns a gentle
  monotone PRIOR (never blocks the pipeline, still ranks by score).
* Persisted to logs/calibration.json — inspectable, cheap to reload,
  stamped with engine_version + sample size so a stale fit is obvious.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALIB_FILE = os.path.join(_PROJECT_ROOT, "logs", "calibration.json")

# Need at least this many resolved current-engine trades before a fitted
# curve is trusted. Below it → prior. Isotonic on < ~30 points overfits
# every bump, so the gate is deliberately conservative.
MIN_CALIB = 30

# Prior mapping params (used until MIN_CALIB real outcomes exist).
# Monotone logistic in score: p = lo + (hi-lo) / (1 + exp(-(s-mid)/scale)).
# Centred near a typical B-grade score; gentle slope so nothing is hard
# blocked pre-data but higher score still ⇒ higher P.
_PRIOR_MID   = 55.0
_PRIOR_SCALE = 18.0
_PRIOR_LO    = 0.20
_PRIOR_HI    = 0.62


def _prior(score: float) -> float:
    import math
    z = (float(score) - _PRIOR_MID) / _PRIOR_SCALE
    return _PRIOR_LO + (_PRIOR_HI - _PRIOR_LO) / (1.0 + math.exp(-z))


def _won(row: Dict) -> Optional[bool]:
    """TARGET_HIT=win, SL_HIT=loss, EXPIRED by realised P&L sign.
    None = not a usable outcome (open / NO_DATA-only)."""
    oc = row.get("outcome")
    if oc == "TARGET_HIT":
        return True
    if oc == "SL_HIT":
        return False
    if oc == "EXPIRED":
        for k in ("pnl_pct", "pnl_percent", "pnl_rupees"):
            v = row.get(k)
            if v is not None:
                try:
                    return float(v) > 0
                except (ValueError, TypeError):
                    pass
        return False  # timed out, no P&L recorded = decayed = loss
    return None


class Calibrator:
    """Monotone score→P(win) map. Thread-safe singleton via get_calibrator()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._x: List[float] = []      # sorted score knots
        self._p: List[float] = []      # monotone P(win) at knots
        self._n: int = 0               # sample size behind the fit
        self._fitted: bool = False
        self._fitted_at: Optional[str] = None
        self._load()

    # ── persistence ──────────────────────────────────────────────────
    def _load(self) -> None:
        if not os.path.exists(CALIB_FILE):
            return
        try:
            with open(CALIB_FILE) as f:
                d = json.load(f)
            self._x = [float(v) for v in d.get("knots_x", [])]
            self._p = [float(v) for v in d.get("knots_p", [])]
            self._n = int(d.get("n", 0))
            self._fitted = bool(self._x) and self._n >= MIN_CALIB
            self._fitted_at = d.get("fitted_at")
        except Exception as e:
            log.warning(f"[Calib] load failed: {e}")

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(CALIB_FILE), exist_ok=True)
            tmp = CALIB_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "knots_x": self._x,
                    "knots_p": self._p,
                    "n": self._n,
                    "fitted": self._fitted,
                    "fitted_at": self._fitted_at,
                    "min_calib": MIN_CALIB,
                }, f, indent=2)
            os.replace(tmp, CALIB_FILE)
        except Exception as e:
            log.error(f"[Calib] save failed: {e}")

    # ── fit ──────────────────────────────────────────────────────────
    def fit(self, samples: List[Tuple[float, bool]]) -> Dict:
        """Fit isotonic P(win) on (score, won) pairs. Returns a summary."""
        usable = [(float(s), 1.0 if w else 0.0) for s, w in samples]
        info: Dict = {"n": len(usable), "fitted": False}
        if len(usable) < MIN_CALIB:
            with self._lock:
                self._n = len(usable)
                self._fitted = False
                self._save()
            info["reason"] = f"need {MIN_CALIB}, have {len(usable)} — using prior"
            return info
        try:
            import numpy as np
            from sklearn.isotonic import IsotonicRegression
            xs = np.array([s for s, _ in usable], dtype=float)
            ys = np.array([y for _, y in usable], dtype=float)
            ir = IsotonicRegression(out_of_bounds="clip", y_min=0.02, y_max=0.95)
            ir.fit(xs, ys)
            # Sample the fitted curve at the unique observed scores → compact,
            # inspectable knot table (predict = linear interp between knots).
            knots = sorted(set(round(float(s)) for s in xs))
            preds = ir.predict(np.array(knots, dtype=float))
            with self._lock:
                self._x = [float(k) for k in knots]
                self._p = [float(round(p, 4)) for p in preds]
                self._n = len(usable)
                self._fitted = True
                self._fitted_at = datetime.now().isoformat(timespec="seconds")
                self._save()
            base = float(ys.mean())
            info.update({"fitted": True, "knots": len(knots),
                         "base_rate": round(base, 4),
                         "p_min": min(self._p), "p_max": max(self._p)})
            log.info(f"[Calib] fitted n={len(usable)} base={base:.1%} "
                     f"knots={len(knots)} p=[{min(self._p):.2f},{max(self._p):.2f}]")
        except Exception as e:
            log.error(f"[Calib] fit failed, keeping prior: {e}")
            info["reason"] = f"fit error: {e}"
        return info

    def fit_from_journal(self, engine_version: Optional[str] = None) -> Dict:
        """Pull resolved current-engine rows from the journal and fit."""
        from core.signal_journal import get_resolved_signals, ENGINE_VERSION
        ev = engine_version or ENGINE_VERSION
        rows = get_resolved_signals(days=120)
        samples: List[Tuple[float, bool]] = []
        for r in rows:
            if r.get("engine_version") != ev:
                continue
            w = _won(r)
            if w is None:
                continue
            try:
                s = float(r.get("score", r.get("confluence_score", 0)))
            except (ValueError, TypeError):
                continue
            samples.append((s, w))
        return self.fit(samples)

    # ── predict ──────────────────────────────────────────────────────
    def predict(self, score: float) -> float:
        """Calibrated P(win) in [0,1]. Prior when not yet fitted."""
        try:
            s = float(score)
        except (ValueError, TypeError):
            return _prior(0.0)
        with self._lock:
            if not self._fitted or not self._x:
                return round(_prior(s), 4)
            x, p = self._x, self._p
            if s <= x[0]:
                return p[0]
            if s >= x[-1]:
                return p[-1]
            # linear interp between surrounding knots (monotone by fit)
            lo = 0
            for i in range(1, len(x)):
                if s <= x[i]:
                    lo = i - 1
                    break
            x0, x1 = x[lo], x[lo + 1]
            p0, p1 = p[lo], p[lo + 1]
            if x1 == x0:
                return round(p1, 4)
            return round(p0 + (p1 - p0) * (s - x0) / (x1 - x0), 4)

    def status(self) -> Dict:
        return {
            "fitted": self._fitted,
            "n": self._n,
            "min_calib": MIN_CALIB,
            "fitted_at": self._fitted_at,
            "knots": len(self._x),
            "mode": "isotonic" if self._fitted else "prior",
        }


_cal: Optional[Calibrator] = None
_cal_lock = threading.Lock()


def get_calibrator() -> Calibrator:
    global _cal
    if _cal is None:
        with _cal_lock:
            if _cal is None:
                _cal = Calibrator()
    return _cal


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="calibrator")
    ap.add_argument("--fit", action="store_true",
                    help="Refit from journal (current engine) and persist")
    ap.add_argument("--status", action="store_true",
                    help="Show calibration status")
    ap.add_argument("--curve", action="store_true",
                    help="Print P(win) at scores 0..120 step 10")
    args = ap.parse_args()
    c = get_calibrator()
    if args.fit:
        print(json.dumps(c.fit_from_journal(), indent=2))
    if args.status or not (args.fit or args.curve):
        print(json.dumps(c.status(), indent=2))
    if args.curve:
        for s in range(0, 121, 10):
            print(f"  score {s:>3} -> P(win) {c.predict(s):.3f}")
