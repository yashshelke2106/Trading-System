"""
research_midcap_meanrev.py — H-012: RSI-2 mean-reversion in the midcap band.

THE QUESTION (registered BEFORE running, as H-012)
--------------------------------------------------
RSI-2 mean-reversion is the programme's one CONDITIONAL-PASS, validated on the
F&O large-cap universe via futures, where it is alive at 0.06-0.10% round-trip
and DEAD at 0.25%. Midcap round-trip costs start at 0.50% (best tier) and rise
to 1.00%+.

So the naive question ("does mean-reversion exist in midcaps?") is not the
interesting one — it almost certainly does, gross. The decisive question is:

    does the GROSS edge scale with illiquidity faster than the COST does?

Limits-to-arbitrage says the edge should be larger where arbitrage capital is
scarcer. The cost model says the bill grows too. Only one of those wins, and
this script measures which — per liquidity tier, so the answer is not averaged
into mush.

REGISTERED PRIOR: likely REJECT on cost. Recording that up front so a null
result cannot be quietly re-spun as a surprise, and a positive result faces the
same scepticism it would have faced beforehand.

METHOD (every choice here is a bias guard, not a preference)
------------------------------------------------------------
  universe    point-in-time midcap band from the survivorship-complete bhavcopy
              archive (core.midcap_universe) — includes names that later
              delisted, rebuilt at each rebalance, no lookahead.
  signal      RSI(2) < RSI_ENTRY on close of day t.
  entry       NEXT DAY'S OPEN. Never the signal bar's close — that fill does
              not exist, and it is the single most common way a mean-reversion
              backtest manufactures a mirage.
  exit        first close with RSI(2) > RSI_EXIT, else forced exit after
              MAX_HOLD days. Exit at the CLOSE of the exit day.
  costs       per-trade, from that symbol's own trailing turnover tier
              (core.smallcap_costs) — an illiquid name is charged illiquid
              costs, not a flat blended rate.
  stats       returns clustered BY DATE (many symbols fire the same day; naive
              per-trade t-stats treat those as independent and inflate t by
              sqrt(names-per-day)). Holdout is 70/30 temporal. Bonferroni uses
              the live registry trial count. Deflated Sharpe applied last.

RUN
---
    python research_midcap_meanrev.py                # full study
    python research_midcap_meanrev.py --quick        # 2019-2021 only
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import warnings
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# Windows consoles default to cp1252 and crash on the box-drawing characters
# used in this report. Force UTF-8 on stdout so the study can print its
# results anywhere.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.smallcap_costs import (
    estimate_roundtrip_cost, cost_tier_label, compute_avg_daily_turnover_cr,
)

ARCHIVE = os.path.join("logs", "bhavcopy_archive")
SYMBOL_DIR = os.path.join(ARCHIVE, "symbols")

# ── Signal parameters (fixed BEFORE the run; not swept) ─────────────────────
RSI_LEN = 2
RSI_ENTRY = 10.0     # oversold trigger
RSI_EXIT = 70.0      # mean-reverted exit
MAX_HOLD = 5         # forced exit (trading days)
MIN_PRICE = 20.0     # skip penny names: their % moves are quote noise
TURNOVER_WINDOW = 60
MIN_TURNOVER_CR = 5.0   # below this, not tradeable at any size

# Universe band: liquidity rank among NON-F&O names.
BAND_LO, BAND_HI = 1, 150
REBALANCE_MONTHS = 6


def rsi(close: pd.Series, n: int = RSI_LEN) -> pd.Series:
    """Wilder RSI. Short n makes this very reactive — intended for RSI(2)."""
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    ru = up.ewm(alpha=1.0 / n, adjust=False).mean()
    rd = dn.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = ru / rd.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


# ── Universe ────────────────────────────────────────────────────────────────

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


def build_bands(start: str, end: str) -> Dict[pd.Timestamp, List[str]]:
    """Point-in-time midcap membership at each rebalance date."""
    from core.midcap_universe import build_universe
    dates = pd.date_range(start, end, freq=f"{REBALANCE_MONTHS}MS")
    bands: Dict[pd.Timestamp, List[str]] = {}
    for d in dates:
        try:
            snap = build_universe(
                asof=d.date(), band_lo=BAND_LO, band_hi=BAND_HI,
                turnover_window=TURNOVER_WINDOW,
                min_turnover_cr=MIN_TURNOVER_CR)
            syms = getattr(snap, "symbols", None) or []
            if syms:
                bands[d] = list(syms)
                print(f"  {d.date()}  band size {len(syms)}")
        except Exception as exc:
            print(f"  {d.date()}  universe build failed: {exc}")
    return bands


def band_for(bands: Dict[pd.Timestamp, List[str]], ts: pd.Timestamp) -> List[str]:
    """Membership in force on `ts` — the most recent rebalance at or before it."""
    keys = [k for k in bands if k <= ts]
    return bands[max(keys)] if keys else []


# ── Backtest ────────────────────────────────────────────────────────────────

def run_trades(bands: Dict[pd.Timestamp, List[str]]) -> pd.DataFrame:
    """Generate every RSI-2 trade with its own liquidity-tiered cost."""
    all_syms = sorted({s for v in bands.values() for s in v})
    print(f"\nscanning {len(all_syms)} distinct symbols across rebalances…")

    trades = []
    for i, sym in enumerate(all_syms, 1):
        if i % 50 == 0:
            print(f"  ...{i}/{len(all_syms)}")
        df = load_symbol(sym)
        if df is None or len(df) < 120:
            continue
        df = df[df["close"] >= MIN_PRICE]
        if len(df) < 120:
            continue

        r = rsi(df["close"])
        turnover_cr = (df["close"] * df["volume"] / 1e7).rolling(
            TURNOVER_WINDOW).mean()

        closes = df["close"].values
        opens = df["open"].values
        idx = df.index
        rv = r.values
        tv = turnover_cr.values

        j = 0
        n = len(df)
        while j < n - 2:
            # Signal on close of j; must be a member of the band on that date.
            if rv[j] >= RSI_ENTRY or not np.isfinite(tv[j]) or tv[j] < MIN_TURNOVER_CR:
                j += 1
                continue
            if sym not in band_for(bands, idx[j]):
                j += 1
                continue

            entry_i = j + 1                      # NEXT day's open
            entry_px = opens[entry_i]
            if not np.isfinite(entry_px) or entry_px <= 0:
                j += 1
                continue

            exit_i = None
            for k in range(entry_i, min(entry_i + MAX_HOLD, n)):
                if rv[k] > RSI_EXIT:
                    exit_i = k
                    break
            if exit_i is None:
                exit_i = min(entry_i + MAX_HOLD - 1, n - 1)

            exit_px = closes[exit_i]
            gross = (exit_px - entry_px) / entry_px
            cost = estimate_roundtrip_cost(sym, float(tv[j]))
            trades.append({
                "symbol": sym,
                "signal_date": idx[j],
                "entry_date": idx[entry_i],
                "exit_date": idx[exit_i],
                "hold_days": exit_i - entry_i,
                "turnover_cr": float(tv[j]),
                "tier": cost_tier_label(float(tv[j])),
                "gross_ret": gross,
                "cost": cost,
                "net_ret": gross - cost,
            })
            j = exit_i + 1                       # no overlapping positions

    return pd.DataFrame(trades)


# ── Statistics ──────────────────────────────────────────────────────────────

def date_clustered_t(df: pd.DataFrame, col: str) -> Tuple[float, float, int]:
    """t-stat on DAILY MEAN returns — the honest unit of independence.

    Many symbols fire the same day on a market-wide dip; treating each as an
    independent observation inflates t by ~sqrt(names per day).
    """
    daily = df.groupby("entry_date")[col].mean()
    x = daily.dropna().values
    n = len(x)
    if n < 3:
        return float("nan"), float("nan"), n
    m = x.mean()
    s = x.std(ddof=1)
    if s == 0:
        return float("nan"), m, n
    return m / (s / math.sqrt(n)), m, n


def summarize(df: pd.DataFrame, label: str) -> Dict:
    if df.empty:
        return {"label": label, "n": 0}
    t_g, m_g, nd = date_clustered_t(df, "gross_ret")
    t_n, m_n, _ = date_clustered_t(df, "net_ret")
    wins = (df["net_ret"] > 0).sum()
    gross_wins = df.loc[df["gross_ret"] > 0, "gross_ret"].sum()
    gross_loss = -df.loc[df["gross_ret"] < 0, "gross_ret"].sum()
    net_wins = df.loc[df["net_ret"] > 0, "net_ret"].sum()
    net_loss = -df.loc[df["net_ret"] < 0, "net_ret"].sum()
    return {
        "label": label,
        "n": len(df),
        "indep_dates": nd,
        "avg_cost_pct": round(df["cost"].mean() * 100, 3),
        "gross_mean_pct": round(m_g * 100, 4),
        "net_mean_pct": round(m_n * 100, 4),
        "gross_t": round(t_g, 3) if t_g == t_g else None,
        "net_t": round(t_n, 3) if t_n == t_n else None,
        "win_rate": round(wins / len(df), 3),
        "gross_pf": round(gross_wins / gross_loss, 3) if gross_loss > 0 else None,
        "net_pf": round(net_wins / net_loss, 3) if net_loss > 0 else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="2019-2021 only")
    args = ap.parse_args()

    start, end = ("2019-01-01", "2021-12-31") if args.quick else ("2019-01-01", "2026-07-01")
    print(f"=== H-012: midcap RSI-2 mean-reversion  ({start} .. {end}) ===")
    print("registered prior: likely REJECT on cost\n")
    print("building point-in-time midcap bands...")
    bands = build_bands(start, end)
    if not bands:
        print("no universe bands built — is the bhavcopy archive present?")
        return 1

    trades = run_trades(bands)
    if trades.empty:
        print("no trades generated.")
        return 1

    trades = trades.sort_values("entry_date").reset_index(drop=True)
    print(f"\ntotal trades: {len(trades)}")

    print("\n── OVERALL ──")
    overall = summarize(trades, "all")
    for k, v in overall.items():
        print(f"  {k:16s} {v}")

    print("\n── BY LIQUIDITY TIER (the decisive fork) ──")
    tier_rows = []
    for tier, g in trades.groupby("tier"):
        s = summarize(g, tier)
        tier_rows.append(s)
        print(f"  {tier:20s} n={s['n']:<6d} cost={s['avg_cost_pct']}%  "
              f"gross={s['gross_mean_pct']}% (t={s['gross_t']})  "
              f"net={s['net_mean_pct']}% (t={s['net_t']})  netPF={s['net_pf']}")

    # Temporal holdout
    split = int(len(trades) * 0.7)
    tr, ho = trades.iloc[:split], trades.iloc[split:]
    print("\n── TEMPORAL HOLDOUT ──")
    for lbl, part in (("train(70%)", tr), ("holdout(30%)", ho)):
        s = summarize(part, lbl)
        print(f"  {lbl:14s} n={s['n']:<6d} net={s['net_mean_pct']}% "
              f"(t={s['net_t']})  netPF={s['net_pf']}")

    # Selection correction
    try:
        from core.hypothesis_registry import trial_count
        n_trials = trial_count()
    except Exception:
        n_trials = 12
    t_n, m_n, nd = date_clustered_t(trades, "net_ret")
    if t_n == t_n and nd > 2:
        from math import erfc, sqrt
        p_raw = erfc(abs(t_n) / sqrt(2))          # two-sided normal approx
        p_bonf = min(1.0, p_raw * n_trials)
        print(f"\n── SELECTION CORRECTION ──")
        print(f"  net t={t_n:.3f} over {nd} independent dates")
        print(f"  p_raw={p_raw:.5f}   Bonferroni x{n_trials} = {p_bonf:.5f}")

        from core.deflated_sharpe import evaluate as dsr_eval
        daily = trades.groupby("entry_date")["net_ret"].mean().dropna()
        v = dsr_eval(list(daily.values), n_trials=n_trials, sr_trial_sd=0.03)
        print(f"  Deflated Sharpe: SR={v.sr_hat_per_obs:.4f} vs "
              f"best-of-{n_trials} null SR0={v.sr0_per_obs:.4f} -> "
              f"DSR={v.dsr:.3f}  {'PASS' if v.passes else 'FAIL'}")
        print(f"  {v.note}")

    out = {"overall": overall, "tiers": tier_rows,
           "trades": len(trades), "window": [start, end]}
    os.makedirs("docs/research", exist_ok=True)
    with open("docs/research/midcap_meanrev_H012.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\nwrote docs/research/midcap_meanrev_H012.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
