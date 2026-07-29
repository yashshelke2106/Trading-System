"""
strategy_search.py — an automated strategy-search agent that cannot fool itself.

WHAT THIS IS
------------
An agent that systematically generates, tests and ranks thousands of strategy
configurations (entry filter x target x stop x hold), hunting for one that hits
a target win rate — 7-in-10 by default — AND is profitable net of costs.

WHY THE OBVIOUS VERSION IS A TRAP
---------------------------------
"Keep trying strategies until one succeeds" is, unguarded, a machine for
manufacturing false positives. Search 5,000 configurations against one dataset
and you WILL find several with 70%+ hit rates and positive backtest expectancy
— by luck alone, with no predictive content whatsoever. The more the agent
"tries harder", the more confidently wrong it becomes. This is the single
easiest way to lose real money with real code.

So this agent is built around the correction rather than bolting it on:

  1. TRIAL COUNTING     every configuration evaluated increments a counter. The
                        winner is judged against the best-of-N null, not zero.
  2. LOCKED HOLDOUT     the final slice of history is split off BEFORE the
                        search starts and is never read during ranking. Only
                        the finalists ever touch it, once.
  3. DEFLATED SHARPE    the survivor's Sharpe must beat what the best of N
                        random trials would produce (core.deflated_sharpe).
  4. NULL BENCHMARK     every config is scored against S/(S+T), the driftless
                        random-walk touch probability, because level placement
                        alone moves hit rate without creating any profit.
  5. HONEST REPORTING   if nothing survives, the agent says nothing survived.
                        It is not permitted to return a "best available" config
                        as though it were validated.

INTRABAR RESOLUTION — MEASURED, NOT ASSUMED
-------------------------------------------
When a daily bar contains both the target and the stop, their order is unknown.
Assuming target-first inflates results; assuming stop-first deflates them. Both
outcomes are computed per row and blended with P_TARGET_FIRST, which was
MEASURED at 0.904 on real 5-minute bars (261 ambiguous bars, 151 symbols).
That measurement came from a 60-day, possibly bull-skewed window, so it is
treated as an optimistic-leaning estimate and is configurable.

RUN
---
    python -m core.strategy_search --search
    python -m core.strategy_search --search --target-wr 0.70 --cost 0.20
    python -m core.strategy_search --report
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys
import time
import warnings
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYMBOL_DIR = os.path.join(_ROOT, "logs", "bhavcopy_archive", "symbols")
RESULTS_PATH = os.path.join(_ROOT, "logs", "strategy_search_results.json")
LEDGER_PATH = os.path.join(_ROOT, "logs", "strategy_search_ledger.jsonl")

# Measured on real 5m bars: of daily bars containing BOTH levels, this fraction
# touched the TARGET first. See docs/research/FINAL_hitrate_verdict.md.
P_TARGET_FIRST = 0.904

MIN_PRICE = 50.0
HOLDOUT_FRAC = 0.30      # final 30% of dates, locked away during search
MIN_TRADES = 300         # a config with fewer entries is not evaluable


# ── Search space ────────────────────────────────────────────────────────────

TARGETS = [0.5, 1.0, 1.5, 2.0, 3.0]
STOPS = [1.0, 2.0, 3.0, 5.0]
HOLDS = [3, 5, 10]

# Entry filters. Each is (name, predicate over the feature frame). Written as
# vectorised boolean masks so thousands of combinations stay cheap.
FILTERS: Dict[str, Callable[[pd.DataFrame], np.ndarray]] = {
    "none":          lambda f: np.ones(len(f), dtype=bool),
    "uptrend":       lambda f: f["above_ma200"].values,
    "downtrend":     lambda f: ~f["above_ma200"].values,
    "ma50_above":    lambda f: f["above_ma50"].values,
    "rsi2_low":      lambda f: f["rsi2"].values < 20,
    "rsi2_high":     lambda f: f["rsi2"].values > 80,
    "rsi14_mid":     lambda f: (f["rsi14"].values > 45) & (f["rsi14"].values < 60),
    "vol_surge":     lambda f: f["vol_ratio"].values > 1.5,
    "vol_quiet":     lambda f: f["vol_ratio"].values < 0.8,
    "gap_up":        lambda f: f["gap_pct"].values > 0.5,
    "gap_down":      lambda f: f["gap_pct"].values < -0.5,
    "near_20d_high": lambda f: f["dist_20d_high"].values > -2.0,
    "off_20d_high":  lambda f: f["dist_20d_high"].values < -5.0,
    "low_atr":       lambda f: f["atr_pct"].values < 1.5,
    "high_atr":      lambda f: f["atr_pct"].values > 2.5,
}
# Market-regime overlay applied on top of the stock filter.
REGIMES = ["any", "mkt_up", "mkt_down"]


# ── Feature + outcome precomputation ────────────────────────────────────────

def _rsi(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    ru = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    rd = (-d).clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + ru / rd.replace(0, np.nan))).fillna(50.0)


def load_symbol(sym: str) -> Optional[pd.DataFrame]:
    p = os.path.join(SYMBOL_DIR, f"{sym}.parquet")
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
        df.index = pd.to_datetime(df.index)
        need = ("open", "high", "low", "close", "volume")
        if not all(c in df.columns for c in need):
            return None
        return df[list(need)].sort_index()
    except Exception:
        return None


def build_table(symbols: List[str], verbose: bool = True) -> pd.DataFrame:
    """One row per (symbol, day): features + per-(target,stop,hold) outcomes.

    Outcomes are precomputed ONCE for every level pair, so evaluating a filter
    later is a boolean mask rather than a re-simulation. This is what makes a
    multi-thousand-config search tractable.
    """
    rows = []
    for i, sym in enumerate(symbols, 1):
        if verbose and i % 40 == 0:
            print(f"  ...{i}/{len(symbols)}")
        df = load_symbol(sym)
        if df is None or len(df) < 260:
            continue

        c, o, h, l = df["close"], df["open"], df["high"], df["low"]
        f = pd.DataFrame(index=df.index)
        f["symbol"] = sym
        f["above_ma200"] = c > c.rolling(200).mean()
        f["above_ma50"] = c > c.rolling(50).mean()
        f["rsi2"] = _rsi(c, 2)
        f["rsi14"] = _rsi(c, 14)
        f["vol_ratio"] = df["volume"] / df["volume"].rolling(20).mean()
        f["gap_pct"] = (o / c.shift(1) - 1) * 100
        f["dist_20d_high"] = (c / h.rolling(20).max().shift(1) - 1) * 100
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()],
                       axis=1).max(axis=1)
        f["atr_pct"] = (tr.rolling(14).mean() / c) * 100
        f["price"] = c

        ov, hv, lv = o.values, h.values, l.values
        n = len(df)

        # Outcome per (target, stop, hold): pessimistic and optimistic variants.
        for tgt, stp, hold in itertools.product(TARGETS, STOPS, HOLDS):
            pess = np.zeros(n, dtype=np.int8)
            opt = np.zeros(n, dtype=np.int8)
            valid = np.zeros(n, dtype=bool)
            for i0 in range(n - hold - 1):
                e = ov[i0 + 1]                    # next-open entry
                if not np.isfinite(e) or e < MIN_PRICE:
                    continue
                T = e * (1 + tgt / 100)
                S = e * (1 - stp / 100)
                valid[i0] = True
                rp = ro = 0
                for k in range(i0 + 1, min(i0 + 1 + hold, n)):
                    ht, hs = hv[k] >= T, lv[k] <= S
                    if hs and ht:                 # ambiguous bar
                        rp, ro = 0, 1
                        break
                    if hs:
                        rp = ro = 0
                        break
                    if ht:
                        rp = ro = 1
                        break
                pess[i0], opt[i0] = rp, ro
            key = f"{tgt}_{stp}_{hold}"
            f[f"p_{key}"] = pess
            f[f"o_{key}"] = opt
            f[f"v_{key}"] = valid

        rows.append(f)

    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows)
    out.index.name = "date"
    return out.reset_index()


# ── Evaluation ──────────────────────────────────────────────────────────────

@dataclass
class Config:
    filt: str
    regime: str
    target_pct: float
    stop_pct: float
    hold: int

    def key(self) -> str:
        return f"{self.filt}|{self.regime}|{self.target_pct}|{self.stop_pct}|{self.hold}"


@dataclass
class Result:
    config: Dict
    n: int
    hit_rate: float
    p_null: float
    edge: float
    exp_R: float
    rr: float
    hits_target_wr: bool
    profitable: bool
    segment: str = "search"

    def to_dict(self) -> Dict:
        return asdict(self)


def evaluate(tbl: pd.DataFrame, mask: np.ndarray, cfg: Config,
             cost_pct: float, target_wr: float,
             p_target_first: float = P_TARGET_FIRST) -> Optional[Result]:
    key = f"{cfg.target_pct}_{cfg.stop_pct}_{cfg.hold}"
    valid = tbl[f"v_{key}"].values
    m = mask & valid
    n = int(m.sum())
    if n < MIN_TRADES:
        return None

    pess = tbl[f"p_{key}"].values[m].mean()
    opt = tbl[f"o_{key}"].values[m].mean()
    hit = pess + p_target_first * (opt - pess)      # measured blend

    rr = cfg.target_pct / cfg.stop_pct
    p_null = cfg.stop_pct / (cfg.stop_pct + cfg.target_pct)
    cost_r = cost_pct / cfg.stop_pct
    exp_r = hit * rr - (1 - hit) * 1.0 - cost_r

    return Result(
        config=asdict(cfg), n=n, hit_rate=round(float(hit), 4),
        p_null=round(p_null, 4), edge=round(float(hit - p_null), 4),
        exp_R=round(float(exp_r), 4), rr=round(rr, 3),
        hits_target_wr=bool(hit >= target_wr), profitable=bool(exp_r > 0),
    )


def search(target_wr: float = 0.70, cost_pct: float = 0.20,
           verbose: bool = True) -> Dict:
    """Run the full search. Returns a report; never returns an unvalidated winner."""
    from core.universe import FO_UNIVERSE
    syms = [s.upper() for s in FO_UNIVERSE]

    t0 = time.time()
    if verbose:
        print("=== STRATEGY SEARCH AGENT ===")
        print(f"goal: hit rate >= {target_wr:.0%} AND positive expectancy "
              f"net of {cost_pct:.2f}% round-trip")
        print(f"intrabar blend: P(target first) = {P_TARGET_FIRST} (measured on 5m bars)")
        print("\nbuilding feature/outcome table...")

    tbl = build_table(syms, verbose=verbose)
    if tbl.empty:
        return {"ok": False, "reason": "no data"}

    # Lock the holdout BEFORE any ranking happens.
    dates = np.sort(tbl["date"].unique())
    cut = dates[int(len(dates) * (1 - HOLDOUT_FRAC))]
    is_search = tbl["date"].values < cut
    if verbose:
        print(f"\nrows {len(tbl):,} | search < {pd.Timestamp(cut).date()} "
              f"| holdout >= {pd.Timestamp(cut).date()} (locked)")

    # Market regime from the table itself (breadth of above_ma200).
    breadth = tbl.groupby("date")["above_ma200"].mean()
    mkt_up = tbl["date"].map(breadth > 0.5).values

    search_tbl = tbl[is_search].reset_index(drop=True)
    hold_tbl = tbl[~is_search].reset_index(drop=True)
    s_mkt_up = mkt_up[is_search]
    h_mkt_up = mkt_up[~is_search]

    results: List[Result] = []
    n_trials = 0
    if verbose:
        print(f"\nsearching {len(FILTERS)}x{len(REGIMES)}x{len(TARGETS)}"
              f"x{len(STOPS)}x{len(HOLDS)} configurations...")

    for fname, fn in FILTERS.items():
        base = fn(search_tbl)
        for regime in REGIMES:
            if regime == "mkt_up":
                m0 = base & s_mkt_up
            elif regime == "mkt_down":
                m0 = base & ~s_mkt_up
            else:
                m0 = base
            for tgt, stp, hold in itertools.product(TARGETS, STOPS, HOLDS):
                cfg = Config(fname, regime, tgt, stp, hold)
                n_trials += 1
                r = evaluate(search_tbl, m0, cfg, cost_pct, target_wr)
                if r:
                    results.append(r)

    if verbose:
        print(f"evaluated {n_trials} configurations, {len(results)} had enough trades")

    # Finalists: meet BOTH criteria in the search segment.
    finalists = [r for r in results if r.hits_target_wr and r.profitable]
    finalists.sort(key=lambda r: r.exp_R, reverse=True)

    report = {
        "ok": True,
        "target_wr": target_wr, "cost_pct": cost_pct,
        "n_trials": n_trials, "n_evaluable": len(results),
        "n_finalists_search": len(finalists),
        "elapsed_s": round(time.time() - t0, 1),
        "search_end": str(pd.Timestamp(cut).date()),
    }

    # Empirical trial-Sharpe dispersion across everything searched.
    # This is the input the Deflated Sharpe gate needs and never had: the DSR
    # analysis of RSI-2 (docs/research/deflated_sharpe_rsi2.md) showed its
    # survival hinges entirely on the SD of Sharpes ACROSS TRIALS, which had to
    # be guessed. A 2,700-config sweep over the same universe is a legitimate
    # empirical estimate of "how dispersed are strategies someone might try".
    trial_sharpes = []
    for r in results:
        p, rr_ = r.hit_rate, r.rr
        mean_r = p * rr_ - (1 - p) * 1.0
        var_r = p * (rr_ - mean_r) ** 2 + (1 - p) * (-1.0 - mean_r) ** 2
        if var_r > 0:
            trial_sharpes.append(mean_r / math.sqrt(var_r))
    if trial_sharpes:
        arr = np.array(trial_sharpes)
        report["trial_sharpe_dist"] = {
            "n": int(arr.size),
            "sd": round(float(arr.std(ddof=1)), 5),
            "mean": round(float(arr.mean()), 5),
            "p05": round(float(np.percentile(arr, 5)), 5),
            "p95": round(float(np.percentile(arr, 95)), 5),
            "max": round(float(arr.max()), 5),
        }

    # Best-effort context even when nothing qualifies.
    hit_only = [r for r in results if r.hits_target_wr]
    prof_only = [r for r in results if r.profitable]
    report["n_hit_target_wr"] = len(hit_only)
    report["n_profitable"] = len(prof_only)
    if hit_only:
        b = max(hit_only, key=lambda r: r.exp_R)
        report["best_by_hitrate"] = b.to_dict()
    if prof_only:
        b = max(prof_only, key=lambda r: r.hit_rate)
        report["best_by_profit"] = b.to_dict()

    # HOLDOUT: only finalists are allowed to touch it, once.
    confirmed = []
    for r in finalists[:10]:
        cfg = Config(**r.config)
        base = FILTERS[cfg.filt](hold_tbl)
        if cfg.regime == "mkt_up":
            m = base & h_mkt_up
        elif cfg.regime == "mkt_down":
            m = base & ~h_mkt_up
        else:
            m = base
        hr = evaluate(hold_tbl, m, cfg, cost_pct, target_wr)
        if hr:
            hr.segment = "holdout"
            confirmed.append({"search": r.to_dict(), "holdout": hr.to_dict(),
                              "survived": hr.hits_target_wr and hr.profitable})
    report["holdout_tested"] = confirmed
    report["n_survived_holdout"] = sum(1 for c in confirmed if c["survived"])

    # Deflated Sharpe on any holdout survivor.
    survivors = [c for c in confirmed if c["survived"]]
    deflated = []
    for c in survivors:
        h = c["holdout"]
        rr = h["rr"]
        p = h["hit_rate"]
        # Per-trade return in R; Sharpe from the two-point Bernoulli outcome.
        mean_r = p * rr - (1 - p) * 1.0
        var_r = p * (rr - mean_r) ** 2 + (1 - p) * (-1.0 - mean_r) ** 2
        sd_r = math.sqrt(var_r) if var_r > 0 else 0.0
        sr = mean_r / sd_r if sd_r > 0 else 0.0
        try:
            from core.deflated_sharpe import evaluate as dsr_eval
            v = dsr_eval(sr_hat=sr, T=h["n"], skew=0.0, kurt=3.0,
                         n_trials=n_trials, sr_trial_sd=None)
            deflated.append({"config": h["config"], "sr": round(sr, 4),
                             "dsr": round(v.dsr, 4), "sr0": round(v.sr0_per_obs, 4),
                             "passes": v.passes, "note": v.note})
        except Exception as exc:
            deflated.append({"config": h["config"], "error": str(exc)})
    report["deflated"] = deflated
    report["n_survived_deflation"] = sum(1 for d in deflated if d.get("passes"))

    _write(report)
    if verbose:
        _print(report)
    return report


# ── Persistence + reporting ─────────────────────────────────────────────────

def _write(report: Dict) -> None:
    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "n_trials": report.get("n_trials"),
            "n_finalists_search": report.get("n_finalists_search"),
            "n_survived_holdout": report.get("n_survived_holdout"),
            "n_survived_deflation": report.get("n_survived_deflation"),
        }) + "\n")


def _print(r: Dict) -> None:
    print("\n" + "=" * 74)
    print(f"configurations evaluated        : {r['n_trials']}")
    print(f"  reaching {r['target_wr']:.0%} hit rate         : {r.get('n_hit_target_wr', 0)}")
    print(f"  profitable                    : {r.get('n_profitable', 0)}")
    print(f"  BOTH (search segment)         : {r['n_finalists_search']}")
    print(f"  survived locked holdout       : {r.get('n_survived_holdout', 0)}")
    print(f"  survived selection deflation  : {r.get('n_survived_deflation', 0)}")

    if r.get("best_by_hitrate"):
        b = r["best_by_hitrate"]
        c = b["config"]
        print(f"\nhighest-expectancy config that reaches the hit-rate goal:")
        print(f"  {c['filt']}/{c['regime']} tgt {c['target_pct']}% stop "
              f"{c['stop_pct']}% hold {c['hold']}d")
        print(f"  hit {b['hit_rate']*100:.1f}% (null {b['p_null']*100:.1f}%, "
              f"edge {b['edge']*100:+.1f}%)  exp {b['exp_R']:+.3f}R  n={b['n']}")

    if r.get("n_survived_deflation"):
        print("\n*** VALIDATED SURVIVOR(S) ***")
        for d in r["deflated"]:
            if d.get("passes"):
                print(f"  {d['config']}  SR={d['sr']} DSR={d['dsr']} — {d['note']}")
    else:
        print("\nNo configuration survived the full gauntlet.")
        print("The agent is not permitted to return a best-available config as")
        print("though it were validated — that is how search becomes overfitting.")
    print("=" * 74)


def main() -> int:
    ap = argparse.ArgumentParser(description="Automated strategy search with anti-overfit gates.")
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--target-wr", type=float, default=0.70)
    ap.add_argument("--cost", type=float, default=0.20)
    args = ap.parse_args()

    if args.search:
        search(target_wr=args.target_wr, cost_pct=args.cost)
        return 0
    if args.report:
        if not os.path.exists(RESULTS_PATH):
            print("no search run yet — use --search")
            return 1
        with open(RESULTS_PATH, encoding="utf-8") as fh:
            _print(json.load(fh))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
