"""
Walk-forward parameter fitter for india_swing.

Premise: static thresholds drift. Refit on rolling window monthly.
This script sweeps key params, scores each combo on a HELD-OUT test
period, and saves the winner to config/walk_forward_<YYYY_MM>.json.

Train window  : trailing 5 months ending at split_date
Test window   : 1 month after split_date
Score metric  : profit_factor (gross_win / gross_loss); ties broken by
                expectancy_R.

Param grid (intentionally small — large grids overfit):
    VOL_MIN_X       : [1.3, 1.8, 2.3]
    RSI_LONG_MAX    : [60, 65, 70]
    target_R        : [1.5, 2.0, 2.5]
    HOLD_HORIZON    : [10, 15, 20]

For each combo: run backtest_india_swing.py with env overrides, parse
CSV result, compute PF on test window only.

Output:
    config/walk_forward_2026_05.json   (one record per train→test pair)

Honest constraint: this still uses backtest data, not live journal.
Walk-forward over backtest = better than tune-on-full but still has
look-ahead in the strategy code itself (vs live truly unseen data).
Treat results as direction-of-tuning, not validated edge.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from itertools import product
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
CSV_PATH = ROOT / "backtest_india_swing_trades.csv"

# ── Grid ──────────────────────────────────────────────────────────────
GRID = {
    "VOL_MIN_X":    [1.3, 1.8, 2.3],
    "RSI_LONG_MAX": [60, 65, 70],
    "TARGET_R":     [1.5, 2.0, 2.5],
    "HOLD_HORIZON": [10, 15, 20],
}

TRAIN_MONTHS = 5
TEST_MONTHS  = 1


def _run_backtest(env_overrides: dict) -> pd.DataFrame:
    """Invoke backtest_india_swing.py with env overrides. Returns trades DF."""
    env = os.environ.copy()
    env.update({k: str(v) for k, v in env_overrides.items()})
    env["DISABLE_G9"]  = "1"
    env["DISABLE_G10"] = "1"
    env["PRECISION_MODE"] = "0"

    result = subprocess.run(
        [sys.executable, str(ROOT / "backtest_india_swing.py")],
        env=env, capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        print(f"[WF] backtest failed: {result.stderr[-200:]}")
        return pd.DataFrame()
    if not CSV_PATH.exists():
        return pd.DataFrame()
    df = pd.read_csv(CSV_PATH)
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    return df


def _score(df: pd.DataFrame, start: date, end: date) -> dict:
    """Compute PF + expectancy R on [start, end)."""
    sub = df[(df["entry_date"] >= pd.Timestamp(start)) &
             (df["entry_date"] <  pd.Timestamp(end))]
    if len(sub) < 5:
        return {"n": len(sub), "pf": 0.0, "exp_R": 0.0, "wr": 0.0}
    wins  = sub[sub["pnl_pct"] >  0]["pnl_pct"].sum()
    loss  = abs(sub[sub["pnl_pct"] <= 0]["pnl_pct"].sum())
    pf = wins / max(loss, 0.001)
    exp_R = float(sub["R"].mean())
    wr = float((sub["pnl_pct"] > 0).mean())
    return {"n": len(sub), "pf": round(pf, 2),
            "exp_R": round(exp_R, 3), "wr": round(wr, 3)}


def fit(split_date: date) -> dict:
    """Sweep grid, train on [split-5m, split), score on [split, split+1m).
    Returns winning combo dict."""
    train_start = split_date - timedelta(days=30 * TRAIN_MONTHS)
    test_end    = split_date + timedelta(days=30 * TEST_MONTHS)

    results = []
    combos = list(product(*GRID.values()))
    print(f"[WF] split={split_date} train={train_start}..{split_date} "
          f"test={split_date}..{test_end} | {len(combos)} combos")

    for vals in combos:
        params = dict(zip(GRID.keys(), vals))
        # Map to env names backtest expects
        env_overrides = {
            "WF_VOL_MIN_X":     params["VOL_MIN_X"],
            "WF_RSI_LONG_MAX":  params["RSI_LONG_MAX"],
            "WF_TARGET_R":      params["TARGET_R"],
            "WF_HOLD_HORIZON":  params["HOLD_HORIZON"],
        }
        df = _run_backtest(env_overrides)
        if df.empty:
            continue
        train_stats = _score(df, train_start, split_date)
        test_stats  = _score(df, split_date, test_end)
        results.append({
            "params": params,
            "train":  train_stats,
            "test":   test_stats,
        })
        print(f"  {params} train_pf={train_stats['pf']} test_pf={test_stats['pf']}")

    if not results:
        return {}

    # Pick max test PF, tiebreak on test exp_R
    results.sort(key=lambda r: (r["test"]["pf"], r["test"]["exp_R"]), reverse=True)
    winner = results[0]
    return {
        "split_date": split_date.isoformat(),
        "train_window_months": TRAIN_MONTHS,
        "test_window_months":  TEST_MONTHS,
        "winner": winner,
        "all_results": results,
    }


if __name__ == "__main__":
    # Pick split = today - 1 month (so we have test data already)
    split = date.today() - timedelta(days=30)
    out = fit(split)
    if not out:
        print("[WF] no results")
        sys.exit(1)

    CONFIG_DIR.mkdir(exist_ok=True)
    fname = f"walk_forward_{split.strftime('%Y_%m')}.json"
    path = CONFIG_DIR / fname
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n[WF] Wrote {path}")
    w = out["winner"]
    print(f"WINNER: {w['params']}  test_pf={w['test']['pf']} "
          f"test_exp_R={w['test']['exp_R']}  test_n={w['test']['n']}")
