"""
ML probability filter for india_swing signals.

A simple LogisticRegression trained on closed-trade outcomes
(backtest CSV + signal journal). Used as the FINAL gate: only emit signal
if model P(win) ≥ ML_THRESHOLD.

Design principles (intentionally conservative):
  - Simple model (LogReg, not xgboost) — interpretable, fewer params to
    overfit on small datasets (~200-1000 trades).
  - Standardised numeric features + binary pattern flags. No look-ahead
    features.
  - Cold start safe: if model file missing or sklearn unavailable, the
    filter passes the signal through (returns ok=True with reason).
  - Feature schema versioned. Mismatch → pass through (log warning).
  - Training is OFFLINE: call `train_and_save()` from a script. Inference
    is read-only on the saved model.

Save location: logs/ml_filter_model.pkl  (pickle: dict with model, scaler,
feature_names, schema_version, training_meta).
"""

from __future__ import annotations

import json
import logging
import os
import pickle
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

MODEL_PATH = Path("logs/ml_filter_model.pkl")
ML_THRESHOLD = float(os.environ.get("ML_THRESHOLD", "0.55"))  # P(win) ≥ this
SCHEMA_VERSION = 2   # bumped: dropped vol_ratio (train/serve skew); real temporal split
MIN_TRAINING_SAMPLES = 50   # under this, model unreliable; pass through

# Feature schema — MUST match between training and inference
NUMERIC_FEATURES = [
    "rsi",            # 0..100
    "rs_vs_nifty",    # ratio, ~0.5..2.0
    "score",          # confluence 0..100
    # NOTE: vol_ratio removed — the training CSV lacked it (defaulted to a
    # constant 1.0) while live passes real values, creating a train/serve skew.
]
ORDINAL_FEATURES = {
    "grade": {"S": 3, "A": 2, "B": 1, "C": 0},
    "direction": {"long": 1, "short": 0},
}
BINARY_PATTERN_FEATURES = [
    "bullish_engulfing", "bearish_engulfing",
    "bullish_marubozu",  "bearish_marubozu",
    "bullish_pin_bar",   "bearish_pin_bar",
    "breakout_5d_high",  "breakout_5d_low",
]
EXTRA_FEATURES = ["near_52wh", "near_52wl"]


def _feature_names() -> List[str]:
    return (NUMERIC_FEATURES
            + list(ORDINAL_FEATURES.keys())
            + [f"has_{p}" for p in BINARY_PATTERN_FEATURES]
            + EXTRA_FEATURES)


def _extract_features(signal: Dict) -> Optional[np.ndarray]:
    """
    Build the feature vector in the EXACT order returned by _feature_names().
    Returns None if any critical numeric is missing (no NaN-impute on entry).
    """
    try:
        # numerics
        row = []
        for f in NUMERIC_FEATURES:
            v = signal.get(f)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return None
            row.append(float(v))
        # ordinals
        for f, mapping in ORDINAL_FEATURES.items():
            v = signal.get(f)
            row.append(float(mapping.get(v, 0)))
        # binary patterns from comma string or list
        pats = signal.get("patterns") or signal.get("patterns_combined") or []
        if isinstance(pats, str):
            pats_set = set(p.strip() for p in pats.split(",") if p.strip())
        else:
            pats_set = set(pats)
        for p in BINARY_PATTERN_FEATURES:
            row.append(1.0 if p in pats_set else 0.0)
        # extras
        for f in EXTRA_FEATURES:
            v = signal.get(f, False)
            row.append(1.0 if v else 0.0)
        return np.array(row, dtype=float)
    except Exception as e:
        log.debug(f"[ML] feature extract error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────
# TRAINING
# ─────────────────────────────────────────────────────────────────────────

def _load_training_rows(csv_path: str = "backtest_india_swing_trades.csv"
                        ) -> List[Dict]:
    """Load closed trades from backtest CSV. Skips TIME_EXIT (neither win nor loss)."""
    import csv
    if not Path(csv_path).exists():
        log.warning(f"[ML] training CSV not found: {csv_path}")
        return []
    rows: List[Dict] = []
    try:
        with open(csv_path) as f:
            for r in csv.DictReader(f):
                outcome = r.get("outcome", "").strip()
                if outcome not in ("TARGET", "SL", "BE_STOP"):
                    continue
                # Cast types
                try:
                    r["rsi"] = float(r.get("rsi", 0) or 0)
                    r["rs_vs_nifty"] = float(r.get("rs_vs_nifty", 1) or 1)
                    r["score"] = float(r.get("score", 0) or 0)
                    r["near_52wh"] = (r.get("near_52wh", "False") == "True")
                    r["near_52wl"] = (r.get("near_52wl", "False") == "True")
                    # Target label: TARGET = 1, SL/BE_STOP = 0 (BE_STOP is "saved loss")
                    r["__label__"] = 1 if outcome == "TARGET" else 0
                    rows.append(r)
                except Exception:
                    continue
    except Exception as e:
        log.warning(f"[ML] CSV read failed: {e}")
    return rows


def train_and_save(csv_path: str = "backtest_india_swing_trades.csv",
                   model_path: Path = MODEL_PATH) -> Dict:
    """
    Train LogReg on closed-trade outcomes from CSV. Save model + scaler +
    feature_names + meta to pickle. Returns training summary dict.
    """
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError as e:
        log.error(f"[ML] sklearn not available: {e}")
        return {"trained": False, "reason": "sklearn_missing"}

    rows = _load_training_rows(csv_path)
    if len(rows) < MIN_TRAINING_SAMPLES:
        return {"trained": False, "reason": f"too_few_samples_{len(rows)}",
                "min_required": MIN_TRAINING_SAMPLES}
    # Real temporal order so the 70/30 split is time-based (not file/symbol order).
    rows.sort(key=lambda r: str(r.get("entry_date", "")))

    # Build feature matrix
    X_list: List[np.ndarray] = []
    y_list: List[int] = []
    skipped = 0
    for r in rows:
        feat = _extract_features(r)
        if feat is None:
            skipped += 1
            continue
        X_list.append(feat)
        y_list.append(int(r["__label__"]))
    if len(X_list) < MIN_TRAINING_SAMPLES:
        return {"trained": False, "reason": "too_few_after_feature_extract",
                "skipped": skipped}

    X = np.vstack(X_list)
    y = np.array(y_list, dtype=int)

    # Time-sorted split (last 30% = held-out test)
    split = int(0.7 * len(X))
    X_train, y_train = X[:split], y[:split]
    X_test, y_test = X[split:], y[split:]

    if len(np.unique(y_train)) < 2:
        return {"trained": False, "reason": "single_class_in_train",
                "y_train_classes": np.unique(y_train).tolist()}

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test) if len(X_test) else X_test

    # LogReg with mild regularization. class_weight balanced — small wins set.
    clf = LogisticRegression(
        C=1.0, max_iter=1000, class_weight="balanced",
        solver="lbfgs", random_state=42,
    )
    clf.fit(X_train_s, y_train)

    train_acc = float(clf.score(X_train_s, y_train))
    test_acc = float(clf.score(X_test_s, y_test)) if len(X_test) else None

    # Probability calibration check on test
    test_pos_rate = float(np.mean(y_test)) if len(y_test) else None
    train_pos_rate = float(np.mean(y_train))

    # Save bundle
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "feature_names": _feature_names(),
        "scaler": scaler,
        "model": clf,
        "training_meta": {
            "trained_at": datetime.now().isoformat(),
            "n_train": int(len(X_train)),
            "n_test": int(len(X_test)),
            "train_pos_rate": round(train_pos_rate, 3),
            "test_pos_rate": round(test_pos_rate, 3) if test_pos_rate is not None else None,
            "train_acc": round(train_acc, 3),
            "test_acc": round(test_acc, 3) if test_acc is not None else None,
            "coef": dict(zip(_feature_names(), clf.coef_[0].tolist())),
            "skipped_rows": int(skipped),
        },
    }
    model_path.parent.mkdir(exist_ok=True)
    with open(model_path, "wb") as f:
        pickle.dump(bundle, f)

    log.info(f"[ML] trained, saved to {model_path}: "
             f"n={len(X_train)}+{len(X_test)} "
             f"train_acc={train_acc:.2f} test_acc={(test_acc or 0):.2f}")
    return {"trained": True, **bundle["training_meta"]}


# ─────────────────────────────────────────────────────────────────────────
# INFERENCE
# ─────────────────────────────────────────────────────────────────────────

_cached_bundle: Optional[Dict] = None


# QUARANTINE, 2026-08-06. Measured on the live CSV, this model selects the
# WORST trades: -3.04%/trade against a -1.20% baseline, accuracy 0.474 against
# 0.868 for "accept everything". It was taken out of service by RENAMING the
# artifact - which is not a quarantine, it is a filename. Retraining writes a
# fresh logs/ml_filter_model.pkl and silently re-arms a gate measured to be
# actively harmful, and no launcher sets DISABLE_G10 to stop it.
#
# So the load path now refuses by default. Set ENABLE_ML_FILTER=1 to opt back
# in, which is a deliberate act that leaves a trace, rather than a side effect
# of running the trainer.
ML_FILTER_ENABLED = os.environ.get("ENABLE_ML_FILTER", "0") == "1"


def _load_model() -> Optional[Dict]:
    global _cached_bundle
    if not ML_FILTER_ENABLED:
        return None
    if _cached_bundle is not None:
        return _cached_bundle
    if not MODEL_PATH.exists():
        return None
    try:
        with open(MODEL_PATH, "rb") as f:
            bundle = pickle.load(f)
        if bundle.get("schema_version") != SCHEMA_VERSION:
            log.warning(f"[ML] schema mismatch: file v{bundle.get('schema_version')} "
                        f"vs code v{SCHEMA_VERSION} — ignoring model")
            return None
        _cached_bundle = bundle
        return bundle
    except Exception as e:
        log.warning(f"[ML] model load failed: {e}")
        return None


def predict_win_probability(signal: Dict) -> Optional[float]:
    """
    Return P(win) for this signal, or None if model unavailable or
    features can't be built. Caller decides whether to emit.
    """
    bundle = _load_model()
    if bundle is None:
        return None
    feat = _extract_features(signal)
    if feat is None:
        return None
    try:
        X = bundle["scaler"].transform(feat.reshape(1, -1))
        proba = float(bundle["model"].predict_proba(X)[0][1])  # P(class=1)
        return proba
    except Exception as e:
        log.debug(f"[ML] inference err: {e}")
        return None


def check_ml_filter(signal: Dict, threshold: float = None) -> Tuple[bool, Dict]:
    """
    Gate function. Returns (ok, info).

    ok=True if either:
      - Model unavailable (cold start) → pass through
      - P(win) >= threshold
    ok=False if model available AND P(win) < threshold.
    """
    th = threshold if threshold is not None else ML_THRESHOLD
    proba = predict_win_probability(signal)
    if proba is None:
        return True, {"reason": "model_unavailable_or_features_missing",
                      "ml_prob": None, "threshold": th}
    info = {"ml_prob": round(proba, 3), "threshold": th}
    if proba < th:
        return False, {**info, "reason": f"p_win_{proba:.2f}_below_{th:.2f}"}
    return True, {**info, "reason": "ok"}


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "train":
        csv = sys.argv[2] if len(sys.argv) > 2 else "backtest_india_swing_trades.csv"
        result = train_and_save(csv)
        print(json.dumps(result, indent=2, default=str))
    else:
        # Default: inspect model + sample inference
        bundle = _load_model()
        if bundle is None:
            print("No model. Train: python -m core.ml_filter train [csv_path]")
            sys.exit(0)
        print("Loaded model:")
        print(json.dumps(bundle["training_meta"], indent=2, default=str))
        sample = {
            "rsi": 60, "rs_vs_nifty": 1.08, "score": 80, "vol_ratio": 2.1,
            "grade": "A", "direction": "long",
            "patterns": "bullish_marubozu", "near_52wh": False, "near_52wl": False,
        }
        ok, info = check_ml_filter(sample)
        print(f"\nSample check: ok={ok}  {info}")
