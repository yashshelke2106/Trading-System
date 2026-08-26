"""
research_participant_flow.py -- H-021 test harness for NSCCL participant flow.

QUESTION (pre-registered as H-021, superseding H-020, before any result existed):

    Does day-on-day CHANGE in a participant's INDEX FUTURES net position, joined
    at `available_from` (T+1), predict next-day NIFTY direction with net-of-cost
    expectancy > 0?

DISCIPLINE (matches the registered kill criteria):

  * IN-SAMPLE 2016-01-01 .. 2021-12-31 -- signal SEARCH happens here (which
    participant among {FII, DII, Pro, Client}, which threshold above zero).
  * HOLDOUT 2022-01-01 .. today -- LOCKED. One touch, no retry, no going back.
  * Signal is TAKEN on `available_from`: NSCCL publishes day T's positioning
    after T's close, so acting on T is lookahead. The loader already attaches
    this; we join on it and never on `trade_date`.
  * Archive-hole guard: `fut_idx_net_chg` is already NaN across gaps > 10
    sessions (MAX_SESSION_GAP_DAYS in flow_capture.add_derived) so a
    trans-holiday "next row" cannot be traded as if it were a day-on-day change.

TARGET: Dhan NIFTY daily close -- the real index, available 2015-09+. The
bhavcopy archive cannot serve this: it starts 2019 and carries only the CM
(equity) segment, so it has no index level at all. H-020 was superseded
precisely because its 2012 window had no priceable target.

KILL CRITERIA (any one triggers REJECT):
  1. IS |t| < 2.5                                       -- too weak to survive OOS
  2. LOYO year contributes > 50% of the IS effect       -- concentration
  3. Holdout Sharpe sign flip vs IS                     -- fragile
  4. Expectancy dies at 6 bp round-trip futures cost    -- unfundable

NULL:
  Best-of-4 shuffle-null on the IS window. Randomly permute the signal LABEL
  500 times, take the best-of-4-participants Sharpe each shuffle, and compare
  the real best to that distribution. Reason: with 4 candidates a naive
  Bonferroni is loose; a shuffle null measures the actual best-of-4 draw.

STRATEGY (fixed textbook, no tuning):
  * Enter tomorrow (t = available_from) long if signal_chg > 0, short if < 0,
    flat if == 0. Exit at the following session's close (1-day hold).
  * PnL uses NIFTY close-to-close; costs applied at the top level as a
    round-trip rate.

USAGE:
    python research_participant_flow.py --search   # IS-only search + null
    python research_participant_flow.py --lock     # touch the holdout ONCE
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.flow_capture import load_panel

# Frozen boundaries. Do not change these once a holdout touch has happened;
# moving them retroactively is exactly the retry the registration bans.
IS_START = "2016-01-01"
IS_END = "2021-12-31"
OOS_START = "2022-01-01"

PARTICIPANTS = ("FII", "DII", "Pro", "Client")
COST_BP_ROUND_TRIP = 6.0                # NSE futures, per H-020 registration
N_SHUFFLE = 500

_ROOT = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(_ROOT, "logs", "h021_ledger.json")


# ── Data ─────────────────────────────────────────────────────────────────────

def _load_nifty() -> pd.DataFrame:
    """Real NIFTY index daily closes, from Dhan.

    Deliberately has NO synthetic fallback. An earlier draft averaged the
    top-100 closes when the index was missing, which would have quietly scored
    this hypothesis against a basket that is not the thing the participants are
    positioned in -- a wrong answer is worse than a missing one, so this raises
    instead. The bhavcopy archive cannot serve as a source here: it begins 2019
    and holds only the CM (equity) segment, with no index level.
    """
    from core.api_dhan import dhan_daily
    df = dhan_daily("NIFTY", days_back=4200)
    if df is None or getattr(df, "empty", True):
        raise RuntimeError(
            "NIFTY index history unavailable from Dhan -- refusing to score "
            "H-021 against a proxy. Fix the feed and re-run.")
    df = df.copy()
    ts = pd.to_datetime(df["date"] if "date" in df.columns else df.index)

    # Dhan stamps daily bars IST-read-as-UTC, so a session shows up as the
    # PRIOR day at 18:30 (18:30Z + 5:30 = 00:00 IST next day). Joining that
    # against midnight-normalised signal dates matches nothing at all and
    # silently yields n=0 -- which reads as "no data" rather than "bad join".
    # Shift then normalise so a bar lands on the session it actually is.
    if (ts.dt.hour != 0).any():
        ts = ts + pd.Timedelta(hours=5, minutes=30)
    df["date"] = ts.dt.normalize()
    return (df[["date", "close"]].dropna()
            .sort_values("date").reset_index(drop=True))


def _next_returns(nifty: pd.DataFrame) -> pd.DataFrame:
    """Next-day close-to-close return, indexed by the day the signal is taken
    (`available_from`). r[t] = close[t+1] / close[t] - 1, so a signal on t
    captures the move from t's close to the following session's close."""
    n = nifty.copy()
    n["ret"] = n["close"].pct_change().shift(-1)
    return n.dropna(subset=["ret"]).set_index("date")[["ret"]]


def _build_frame(participant: str) -> pd.DataFrame:
    """Signal + realised next-day return, joined at available_from."""
    sig = load_panel(kind="oi")
    if sig.empty:
        raise RuntimeError("participant OI archive is empty -- run the flow layer first")
    sig = sig[sig["participant"] == participant].dropna(subset=["available_from",
                                                                  "fut_idx_net_chg"])
    sig = sig.set_index("available_from")[["fut_idx_net_chg"]].sort_index()

    nifty = _load_nifty()
    if nifty.empty:
        raise RuntimeError("NIFTY history not available")
    rets = _next_returns(nifty)

    df = sig.join(rets, how="inner").dropna()
    df.index = pd.to_datetime(df.index)
    return df


# ── Metrics ──────────────────────────────────────────────────────────────────

def _sharpe(returns: np.ndarray) -> float:
    if len(returns) < 2 or returns.std(ddof=1) == 0:
        return 0.0
    return float(returns.mean() / returns.std(ddof=1) * math.sqrt(252))


def _t_stat(returns: np.ndarray) -> float:
    if len(returns) < 2 or returns.std(ddof=1) == 0:
        return 0.0
    return float(returns.mean() / (returns.std(ddof=1) / math.sqrt(len(returns))))


def _run_strategy(df: pd.DataFrame, cost_bp: float = 0.0) -> Dict:
    """Sign-of-change strategy. Cost applied whenever the SIGN changes (a
    position flip is a round trip; keeping the sign is free carry)."""
    if df.empty:
        return {"n": 0, "sharpe": 0.0, "t": 0.0, "expectancy": 0.0}
    sign = np.sign(df["fut_idx_net_chg"].to_numpy())
    ret = df["ret"].to_numpy()
    pnl = sign * ret
    if cost_bp > 0:
        flips = np.concatenate([[0.0], np.abs(np.diff(sign)) / 2.0])
        pnl = pnl - flips * (cost_bp / 10_000.0)
    return {
        "n": int(len(pnl)),
        "sharpe": _sharpe(pnl),
        "t": _t_stat(pnl),
        "expectancy": float(pnl.mean()),
        "long_frac": float((sign > 0).mean()),
        "flip_frac": float(np.abs(np.diff(sign)).mean() / 2.0) if len(sign) > 1 else 0.0,
    }


# ── Kill checks ──────────────────────────────────────────────────────────────

def _loyo(df: pd.DataFrame) -> Dict[int, float]:
    """Leave-One-Year-Out: each year's contribution to the mean daily PnL of
    the sign-of-change strategy. Concentration >50% is a REJECT under H-020."""
    if df.empty:
        return {}
    sign = np.sign(df["fut_idx_net_chg"].to_numpy())
    pnl = pd.Series(sign * df["ret"].to_numpy(), index=df.index)
    total = pnl.sum()
    out = {}
    for y in sorted(set(df.index.year)):
        yr = pnl.loc[str(y)].sum()
        out[int(y)] = float(yr / total) if total else 0.0
    return out


def _shuffle_null(df: pd.DataFrame, n: int = N_SHUFFLE, seed: int = 7) -> np.ndarray:
    """Best-of-4 draw from randomly permuting the SIGNAL labels."""
    rng = np.random.default_rng(seed)
    sig = df["fut_idx_net_chg"].to_numpy()
    ret = df["ret"].to_numpy()
    if len(sig) < 10:
        return np.array([])
    bests = np.empty(n)
    for i in range(n):
        best = -np.inf
        for _ in range(len(PARTICIPANTS)):
            shuffled = rng.permutation(sig)
            s = _sharpe(np.sign(shuffled) * ret)
            if s > best:
                best = s
        bests[i] = best
    return bests


# ── The registered protocol ──────────────────────────────────────────────────

def search_in_sample() -> Dict:
    """Signal SEARCH. Runs the strategy for each of {FII, DII, Pro, Client}
    over IS only. Does NOT touch the holdout."""
    is_ = {}
    for p in PARTICIPANTS:
        try:
            df = _build_frame(p)
        except Exception as e:
            is_[p] = {"error": str(e)}
            continue
        window = df.loc[IS_START:IS_END]
        is_[p] = _run_strategy(window)
        is_[p]["loyo_max_share"] = max(_loyo(window).values(),
                                       default=0.0) if not window.empty else 0.0

    ranked = sorted(
        [(p, s) for p, s in is_.items() if isinstance(s, dict) and "sharpe" in s],
        key=lambda kv: kv[1]["sharpe"], reverse=True)
    winner = ranked[0][0] if ranked else None

    null_dist = np.array([])
    if winner and "error" not in is_[winner]:
        winner_df = _build_frame(winner).loc[IS_START:IS_END]
        null_dist = _shuffle_null(winner_df)

    out = {
        "is_by_participant": is_,
        "winner": winner,
        "null": {
            "n": int(null_dist.size),
            "mean": float(null_dist.mean()) if null_dist.size else None,
            "p95": float(np.percentile(null_dist, 95)) if null_dist.size else None,
            "p_value": (float((null_dist >= is_[winner]["sharpe"]).mean())
                        if winner and null_dist.size else None),
        },
    }

    # Auto-verdict: kill criteria 1 and 2 are checkable in-sample.
    kills = []
    if winner:
        w = is_[winner]
        if abs(w["t"]) < 2.5:
            kills.append(f"IS |t| = {w['t']:.2f} < 2.5")
        if w.get("loyo_max_share", 0) > 0.5:
            kills.append(f"one year contributes {w['loyo_max_share']*100:.0f}% > 50%")
        null_p = out["null"]["p_value"]
        if null_p is not None and null_p > 0.05:
            kills.append(f"p = {null_p:.3f} > 0.05 vs matched null")
    out["is_verdict"] = "PROCEED to holdout" if not kills else f"REJECT: {'; '.join(kills)}"
    return out


def touch_holdout(participant: str) -> Dict:
    """ONE-SHOT. Records the touch to the ledger to enforce no-retry."""
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            log = json.load(f)
        if log.get("touched"):
            raise RuntimeError(
                f"Holdout ALREADY touched on {log['touched_at']} for "
                f"{log['participant']}. H-020 does not permit a second touch.")
    df = _build_frame(participant)
    is_win = df.loc[IS_START:IS_END]
    oos = df.loc[OOS_START:]
    is_res = _run_strategy(is_win)
    oos_gross = _run_strategy(oos, cost_bp=0.0)
    oos_net = _run_strategy(oos, cost_bp=COST_BP_ROUND_TRIP)

    kills = []
    if is_res["sharpe"] != 0 and oos_gross["sharpe"] != 0:
        if math.copysign(1, is_res["sharpe"]) != math.copysign(1, oos_gross["sharpe"]):
            kills.append("holdout sign flip vs IS")
    if oos_net["expectancy"] <= 0:
        kills.append(f"expectancy = {oos_net['expectancy']*100:.4f}% <= 0 at "
                     f"{COST_BP_ROUND_TRIP} bp cost")

    result = {
        "participant": participant,
        "is": is_res,
        "oos_gross": oos_gross,
        "oos_net_at_6bp": oos_net,
        "verdict": "PASS" if not kills else f"REJECT: {'; '.join(kills)}",
        "touched_at": datetime.now().isoformat(timespec="seconds"),
    }
    log = {"touched": True, "touched_at": result["touched_at"],
           "participant": participant, "result": result}
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, default=str)
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print(x: Dict) -> None:
    print(json.dumps(x, indent=2, default=str))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--search", action="store_true",
                    help="IS-only search + shuffle null (holdout untouched)")
    ap.add_argument("--lock", metavar="PARTICIPANT",
                    help="ONE-SHOT holdout evaluation (records to ledger)")
    args = ap.parse_args(argv)

    if args.search:
        _print(search_in_sample()); return 0
    if args.lock:
        _print(touch_holdout(args.lock)); return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
