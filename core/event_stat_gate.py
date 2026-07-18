"""
Statistician gate for the corporate-event study.

Takes the ABNORMAL returns from event_study (event excess-vs-NIFTY minus the
stock's own baseline drift) and asks, per event type + horizon: is the mean
abnormal return distinguishable from zero, AFTER

  1. clustering  - bootstrap resamples SYMBOLS, not events, because many events
                   share a stock and overlapping windows are not independent
                   (naive t-tests badly overstate significance here);
  2. multiple testing - Bonferroni across every (event_type x horizon) cell
                   tested, since we are eyeballing ~13 types for the best one;
  3. cost       - the mean must clear a round-trip cost hurdle to be tradeable.

A row PASSES only if the clustered 95% CI excludes 0, it survives the Bonferroni
alpha, and |mean| > cost. Anything else is noise.

RUN:  python -m core.event_stat_gate --types bonus,buyback --horizons 5,20
"""

from __future__ import annotations

import argparse
import json
import os
import random
from collections import defaultdict
from datetime import datetime
from typing import Dict, List

from .event_study import (
    HORIZONS, _INDEX_TICKER, _baseline, _load_events, _load_ohlc,
    _parse_date, _ret, _t0_index,
)

COST_PCT = 0.10   # round-trip hurdle (~futures 0.06, cash ~0.2; 0.10 = middle)
_BOOT = 5000

# One-shot holdout: last HOLDOUT_FRAC of events (by event date) are NEVER seen
# by the discovery gate. A cell that passes discovery gets exactly ONE holdout
# test, recorded in the ledger; any later attempt on the same cell is refused.
HOLDOUT_FRAC = 0.20
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDOUT_LEDGER = os.path.join(_ROOT_DIR, "logs", "holdout_ledger.jsonl")


def _holdout_spent() -> Dict:
    """{(event_type, horizon): verdict} for cells that already consumed their
    single holdout shot. Re-testing a spent cell is refused - no retries."""
    spent = {}
    if os.path.exists(HOLDOUT_LEDGER):
        with open(HOLDOUT_LEDGER, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    spent[(r["event_type"], r["horizon"])] = r["verdict"]
                except Exception:
                    continue
    return spent


def _holdout_record(event_type: str, horizon: int, verdict: str, detail: str) -> None:
    os.makedirs(os.path.dirname(HOLDOUT_LEDGER), exist_ok=True)
    with open(HOLDOUT_LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event_type": event_type, "horizon": horizon,
            "verdict": verdict, "detail": detail}) + "\n")


def _holdout_cutoff(only_types: List[str]):
    """Date splitting events 80/20: events strictly after it are holdout."""
    dates = sorted(
        d for ev in _load_events()
        if (ev.get("event_type") or ev.get("category")) in only_types
        and (d := _parse_date(ev.get("date"))) is not None
    )
    if not dates:
        return None
    return dates[int(len(dates) * (1 - HOLDOUT_FRAC)) - 1]


def _abnormals_by_symbol(only_types: List[str], horizons: List[int],
                         segment: str = "discovery", cutoff=None) -> Dict:
    """{event_type: {horizon: {symbol: [abnormal_ret, ...]}}}
    segment: 'discovery' (events <= cutoff), 'holdout' (> cutoff),
             'all' (no split)."""
    events = _load_events()
    index_df = _load_ohlc(_INDEX_TICKER)
    out: Dict[str, Dict[int, Dict[str, List[float]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list)))

    for ev in events:
        etype = ev.get("event_type") or ev.get("category")
        if etype not in only_types:
            continue
        d = _parse_date(ev.get("date"))
        if not d:
            continue
        if cutoff is not None:
            if segment == "discovery" and d > cutoff:
                continue
            if segment == "holdout" and d <= cutoff:
                continue
        sym = ev.get("symbol")
        df = _load_ohlc(sym)
        if df.empty:
            continue
        i0 = _t0_index(df, d)
        if i0 is None:
            continue
        base = _baseline(sym, df, index_df)
        i0_idx = _t0_index(index_df, df["date"].iloc[i0]) if not index_df.empty else None
        for n in horizons:
            r = _ret(df, i0, n)
            if r is None:
                continue
            m = _ret(index_df, i0_idx, n) if i0_idx is not None else None
            excess = r - m if m is not None else r
            out[etype][n][sym].append(excess - base.get(n, 0.0))
    return out


def _clustered_bootstrap(by_sym: Dict[str, List[float]]):
    """Resample SYMBOLS with replacement (cluster bootstrap). Returns
    (mean, lo95, hi95, p_two_sided, n_events, n_symbols)."""
    syms = list(by_sym)
    all_vals = [v for vs in by_sym.values() for v in vs]
    n_ev = len(all_vals)
    if n_ev == 0 or len(syms) < 2:
        return (float("nan"),) * 4 + (n_ev, len(syms))
    obs_mean = sum(all_vals) / n_ev

    means = []
    for _ in range(_BOOT):
        pool: List[float] = []
        for _ in range(len(syms)):
            pool.extend(by_sym[random.choice(syms)])
        if pool:
            means.append(sum(pool) / len(pool))
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    # two-sided p: how often the bootstrap mean crosses 0
    frac_gt0 = sum(1 for m in means if m > 0) / len(means)
    p = 2 * min(frac_gt0, 1 - frac_gt0)
    return obs_mean, lo, hi, p, n_ev, len(syms)


def run(only_types: List[str], horizons: List[int]) -> None:
    cutoff = _holdout_cutoff(only_types)
    data = _abnormals_by_symbol(only_types, horizons, "discovery", cutoff)
    spent = _holdout_spent()

    # alpha budget: Bonferroni across today's cells AND every hypothesis ever
    # registered (registry seeds with the project's 10 pre-registry hunts).
    n_cells = sum(len(horizons) for _ in only_types)
    try:
        from .hypothesis_registry import trial_count
        n_trials = trial_count()
    except Exception:
        n_trials = 0
    denom = max(n_cells + n_trials, 1)
    alpha = 0.05 / denom

    print("=" * 82)
    print("STATISTICIAN GATE  (discovery segment; clustered bootstrap by symbol)")
    print(f"holdout: last {HOLDOUT_FRAC:.0%} of events (after {cutoff}) reserved, "
          f"one shot per cell")
    print(f"cost hurdle |mean| > {COST_PCT:.2f}%   alpha = 0.05/({n_cells} cells "
          f"+ {n_trials} registered trials) = {alpha:.5f}")
    print("=" * 82)
    print(f"  {'type':12s} {'h':>3s} {'n_ev':>5s} {'n_sym':>5s} {'mean%':>7s} "
          f"{'95% CI':>17s} {'p':>7s}   verdict")
    print("  " + "-" * 78)

    any_pass = False
    for et in only_types:
        for n in horizons:
            by_sym = data.get(et, {}).get(n, {})
            mean, lo, hi, p, n_ev, n_sym = _clustered_bootstrap(by_sym)
            if n_ev == 0:
                print(f"  {et:12s} {n:3d}  (no events)")
                continue
            ci_excl_0 = (lo > 0) or (hi < 0)
            passes = ci_excl_0 and (p < alpha) and (abs(mean) > COST_PCT)
            verdict = "PASS-disc" if passes else (
                "cost" if ci_excl_0 and p < alpha else
                "n.s.")
            print(f"  {et:12s} {n:3d} {n_ev:5d} {n_sym:5d} {mean:+7.2f} "
                  f"[{lo:+6.2f},{hi:+6.2f}] {p:7.4f}   {verdict}")

            # ── one-shot holdout, only for discovery passes ────────────────
            if passes:
                if (et, n) in spent:
                    print(f"  {'':12s}      holdout ALREADY SPENT for this cell "
                          f"({spent[(et, n)]}) - no retry, verdict stands")
                    continue
                h_data = _abnormals_by_symbol([et], [n], "holdout", cutoff)
                h_sym = h_data.get(et, {}).get(n, {})
                hm, hlo, hhi, hp, hn, _ = _clustered_bootstrap(h_sym)
                same_sign = (hm > 0) == (mean > 0)
                h_ok = hn >= 10 and same_sign and ((hlo > 0) or (hhi < 0))
                h_verdict = "CONFIRMED" if h_ok else "FAILED"
                _holdout_record(et, n, h_verdict,
                                f"disc_mean={mean:.2f} hold_mean={hm:.2f} "
                                f"hold_n={hn} hold_p={hp:.4f}")
                any_pass = any_pass or h_ok
                print(f"  {'':12s}      HOLDOUT (one shot, now spent): "
                      f"mean {hm:+.2f} [{hlo:+.2f},{hhi:+.2f}] n={hn} "
                      f"-> {h_verdict}")

    print("  " + "-" * 78)
    print("  PASS-disc = passed discovery; only holdout CONFIRMED is tradeable")
    print("  cost = significant but below cost hurdle | n.s. = not significant")
    if not any_pass:
        print("\n  RESULT: nothing holdout-confirmed. No tradeable event edge -")
        print("  as expected for an efficient large-cap universe. Do not trade.")
    print()


def main(argv):
    p = argparse.ArgumentParser(description="Statistician gate for event study")
    p.add_argument("--types", type=str, default="bonus,buyback,fundraise,acquisition")
    p.add_argument("--horizons", type=str, default="5,20")
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args(argv)
    random.seed(a.seed)
    types = [t.strip() for t in a.types.split(",") if t.strip()]
    hz = [int(h) for h in a.horizons.split(",") if h.strip() and int(h) in HORIZONS]
    run(types, hz)
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
