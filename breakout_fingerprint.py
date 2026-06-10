"""
Breakout fingerprint study — does a daily swing-high/low breakout's FOLLOW-THROUGH
depend on the indicator state at the breakout?  (User hypothesis, 2026-06-10.)

WHY THIS IS DANGEROUS (read before trusting any output)
--------------------------------------------------------
The naive form of this idea — "snapshot ALL indicators at every breakout and find
the values that recur in winners" — is the single most overfitting-prone procedure
in quant, and it already burned this project once (FINDINGS_FACTORS.md Round 2:
RSI/pin-bar 'edges' that cross-validated, then reversed on the full universe).
So this harness is built to FALSIFY, not to flatter:

  PRE-REGISTERED (chosen BEFORE looking at outcomes; no mining):
    * Breakout = daily close crosses the most-recent CONFIRMED swing pivot
      (3-bar fractal, same as strategy_india_swing). Entry = NEXT day's OPEN
      (no overnight-gap lookahead). Confirmation lag = no future leak.
    * Outcome = MARKET-NEUTRAL forward return (stock minus NIFTY over the same
      window) at h=5 and h=10 trading days. Market-neutral because the journal
      window taught us raw direction is just the index moving.
    * EXACTLY FOUR mechanism-backed conditioners, fixed in advance:
        1. vol_confirm   volume/SMA20 >= 1.5      (real breakouts have participation)
        2. trend_align   close vs EMA50            (don't fade the larger trend)
        3. tight_base    ATR% < its 100d median    (coiled spring -> expansion)
        4. not_extended  (close-level)/ATR < 1.0   (didn't already run away)
    * TIME split: fit/look only inside the IS window; the OOS window (last 30% of
      dates) is the judge. A conditioner only counts if it helps OOS.
    * MATCHED NULL: permutation test — shuffle each conditioner's labels among OOS
      breakouts N times; p = P(random label split beats the real one). Family-wise
      max-statistic correction across the 4 conditioners (you searched 4, pay for 4).
    * COSTS: round-trip cost applied before any 'tradeable' claim.

  EXPLORATORY (the user's 'fingerprint of all indicators') is printed too, but
  LABELLED NOT-A-FACT — descriptive means of winners vs losers, no inference.

The base-rate GATE runs first: if unconditional breakouts have ~0 market-neutral
expectancy OOS, then 'which indicators separate winners' is the already-falsified
factor question, and no conditioner can be trusted to fix a zero base.

Run:
    python breakout_fingerprint.py                 # top 100, h=5 & 10
    python breakout_fingerprint.py --all           # full 153 F&O universe
    python breakout_fingerprint.py --perms 2000    # heavier null
"""
from __future__ import annotations

import argparse
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import warnings
warnings.filterwarnings("ignore")
import logging
logging.getLogger("core.api_dhan").setLevel(logging.CRITICAL)
logging.getLogger("core.bar_cache").setLevel(logging.CRITICAL)

import numpy as np
import pandas as pd

PIVOT_N      = 3        # fractal half-width (matches strategy_india_swing)
HORIZONS     = (5, 10)  # forward trading days
OOS_FRACTION = 0.30
COST_RT      = 0.0020   # round-trip cost fraction (STT+slippage+brokerage, swing)
RNG          = np.random.default_rng(7)


# ── indicators (all causal: value at t uses only data <= t) ──────────────────
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def confirmed_pivot_level(series: pd.Series, n: int, kind: str) -> pd.Series:
    """Most-recent CONFIRMED fractal pivot level known as of each day t.

    A pivot at index p (high>n neighbours each side / low<) is only CONFIRMED at
    p+n. We publish its level at p+n and forward-fill, so the level at t never
    uses information past t. No lookahead.
    """
    v = series.values
    out = np.full(len(v), np.nan)
    for p in range(n, len(v) - n):
        left = v[p - n:p]; right = v[p + 1:p + 1 + n]
        if kind == "high" and v[p] > left.max() and v[p] > right.max():
            if p + n < len(v):
                out[p + n] = v[p]
        elif kind == "low" and v[p] < left.min() and v[p] < right.min():
            if p + n < len(v):
                out[p + n] = v[p]
    return pd.Series(out, index=series.index).ffill()


def events_for_symbol(sym: str, df: pd.DataFrame, nifty: pd.Series) -> list[dict]:
    """Emit one record per swing breakout (long & short) with causal indicator
    snapshot + market-neutral forward returns. Entry = next day's open."""
    if df is None or len(df) < 260:
        return []
    df = df.copy()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    c, h, l, vol = df["close"], df["high"], df["low"], df["volume"]
    ema50 = c.ewm(span=50, adjust=False).mean()
    r = rsi(c); a = atr(df); atrp = a / c
    volsma = vol.rolling(20).mean()
    atrp_med = atrp.rolling(100).median()
    hi_level = confirmed_pivot_level(h, PIVOT_N, "high")
    lo_level = confirmed_pivot_level(l, PIVOT_N, "low")
    nifty = nifty.reindex(df.index).ffill()

    idx = df.index
    recs = []
    for i in range(120, len(df) - max(HORIZONS) - 1):
        t = idx[i]
        entry_i = i + 1                      # enter NEXT open (no gap leak)
        entry = df["open"].iloc[entry_i]
        if not np.isfinite(entry) or entry <= 0:
            continue
        for direction, level in (("long", hi_level.iloc[i]), ("short", lo_level.iloc[i])):
            if not np.isfinite(level):
                continue
            broke = (c.iloc[i] > level and c.iloc[i - 1] <= level) if direction == "long" \
                else (c.iloc[i] < level and c.iloc[i - 1] >= level)
            if not broke:
                continue
            rec = {"symbol": sym, "date": t, "direction": direction,
                   "rsi": r.iloc[i], "atrp": atrp.iloc[i],
                   "vol_ratio": (vol.iloc[i] / volsma.iloc[i]) if volsma.iloc[i] else np.nan,
                   "dist_ema50": (c.iloc[i] - ema50.iloc[i]) / c.iloc[i],
                   "extension": (c.iloc[i] - level) / a.iloc[i] if a.iloc[i] else np.nan,
                   "close": c.iloc[i], "level": level}
            # pre-registered conditioners (binary, causal)
            rec["vol_confirm"]  = bool(rec["vol_ratio"] is not np.nan and rec["vol_ratio"] >= 1.5)
            rec["trend_align"]  = bool((c.iloc[i] > ema50.iloc[i]) if direction == "long"
                                       else (c.iloc[i] < ema50.iloc[i]))
            rec["tight_base"]   = bool(np.isfinite(atrp_med.iloc[i]) and atrp.iloc[i] < atrp_med.iloc[i])
            ext = rec["extension"]
            rec["not_extended"] = bool(np.isfinite(ext) and abs(ext) < 1.0)
            # market-neutral forward returns
            ok = True
            for hh in HORIZONS:
                ex = df["close"].iloc[entry_i + hh - 1]
                stk = (ex / entry - 1.0) if direction == "long" else (entry / ex - 1.0)
                n0, n1 = nifty.iloc[entry_i], nifty.iloc[entry_i + hh - 1]
                nret = (n1 / n0 - 1.0) if (np.isfinite(n0) and n0 > 0) else 0.0
                mn = stk - (nret if direction == "long" else -nret)
                if not np.isfinite(mn):
                    ok = False; break
                rec[f"mn{hh}"] = mn
                rec[f"raw{hh}"] = stk
            if ok:
                recs.append(rec)
    return recs


# ── stats helpers ────────────────────────────────────────────────────────────
def tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3 or x.std() == 0:
        return 0.0
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))

def line(label, x):
    x = x[np.isfinite(x)]
    if len(x) == 0:
        print(f"  {label:22} n=0"); return
    print(f"  {label:22} n={len(x):5d}  mean {x.mean()*100:+6.3f}%  "
          f"t={tstat(x):+5.2f}  win% {(x>0).mean()*100:4.0f}")


def perm_pvalue(vals: np.ndarray, mask: np.ndarray, perms: int) -> tuple[float, float]:
    """Improvement of conditioner-true subset over the whole OOS set, and a
    permutation p-value: shuffle the mask, how often does a random split of the
    SAME size beat the real subset mean? Returns (improvement, p)."""
    vals = vals.astype(float)
    base = np.nanmean(vals)
    real = np.nanmean(vals[mask]) - base
    k = int(mask.sum())
    if k < 10 or k == len(vals):
        return real, 1.0
    cnt = 0
    for _ in range(perms):
        samp = RNG.choice(vals, size=k, replace=False)
        if (np.nanmean(samp) - base) >= real:
            cnt += 1
    return real, (cnt + 1) / (perms + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="full 153 F&O universe (default top 100)")
    ap.add_argument("--days", type=int, default=2600)
    ap.add_argument("--perms", type=int, default=1000)
    args = ap.parse_args()

    from core.bar_cache import cached_daily
    from core.universe import FO_UNIVERSE, TOP100_FO
    universe = FO_UNIVERSE if args.all else TOP100_FO

    print(f"[BFP] loading NIFTY + {len(universe)} symbols (cache) ...")
    nifty_df = cached_daily("NIFTY", args.days)
    if nifty_df is None or nifty_df.empty:
        print("[BFP] FAIL — no NIFTY bars."); return
    nifty = nifty_df["close"]; nifty = nifty[~nifty.index.duplicated(keep="last")].sort_index()

    all_recs, loaded, failed = [], 0, 0
    for j, sym in enumerate(universe):
        try:
            df = cached_daily(sym, args.days)
            recs = events_for_symbol(sym, df, nifty)
            all_recs.extend(recs); loaded += 1
        except Exception:
            failed += 1
        if (j + 1) % 25 == 0:
            print(f"  ... {j+1}/{len(universe)} symbols, {len(all_recs)} breakouts so far")
    print(f"[BFP] {loaded} symbols loaded ({failed} failed), {len(all_recs)} breakout events\n")
    if len(all_recs) < 200:
        print("[BFP] too few events."); return

    D = pd.DataFrame(all_recs).sort_values("date").reset_index(drop=True)
    split_date = D["date"].quantile(0.70)
    IS = D[D["date"] < split_date]; OOS = D[D["date"] >= split_date]
    print("=" * 74)
    print(f"  BREAKOUT FOLLOW-THROUGH — {len(D)} events  "
          f"({D['date'].min().date()} → {D['date'].max().date()})")
    print(f"  IS {len(IS)} (<{str(split_date)[:10]})   OOS {len(OOS)} (≥)")
    print("=" * 74)

    # ── BASE-RATE GATE ───────────────────────────────────────────────────────
    for hh in HORIZONS:
        print(f"\n  BASE RATE — market-neutral {hh}d forward return (cost not yet applied)")
        for name, seg in (("IS  all", IS), ("OOS all", OOS),
                          ("OOS long", OOS[OOS.direction == "long"]),
                          ("OOS short", OOS[OOS.direction == "short"])):
            line(name, seg[f"mn{hh}"].values)
        net = OOS[f"mn{hh}"].values - COST_RT
        line(f"OOS all NET (−{COST_RT*100:.2f}%)", net)

    # ── PRE-REGISTERED CONDITIONERS (judged OOS, matched-null) ───────────────
    H = HORIZONS[0]
    conds = ["vol_confirm", "trend_align", "tight_base", "not_extended"]
    print(f"\n  CONDITIONERS — OOS market-neutral {H}d, improvement vs OOS base, "
          f"matched-null p ({args.perms} perms)")
    base = np.nanmean(OOS[f"mn{H}"].values)
    print(f"  (OOS base mean = {base*100:+.3f}%)")
    results = []
    vals = OOS[f"mn{H}"].values
    for cnd in conds:
        mask = OOS[cnd].values.astype(bool)
        imp, p = perm_pvalue(vals, mask, args.perms)
        results.append((cnd, mask.sum(), imp, p))
        print(f"  {cnd:14} true={mask.sum():4d}  cond_mean {np.nanmean(vals[mask])*100:+.3f}%  "
              f"impr {imp*100:+.3f}%  p={p:.3f}")
    # family-wise: Bonferroni on the 4 searched
    best = min(results, key=lambda r: r[3])
    print(f"\n  best conditioner: {best[0]} p={best[3]:.3f}  "
          f"→ Bonferroni×4 = {min(best[3]*4,1.0):.3f}  "
          f"({'SURVIVES' if best[3]*4 < 0.05 else 'FAILS'} family-wise α=0.05)")

    # ── EXPLORATORY FINGERPRINT (NOT A FACT) ─────────────────────────────────
    print("\n  ── EXPLORATORY fingerprint (DESCRIPTIVE ONLY — NOT predictive) ──")
    win = OOS[OOS[f"mn{H}"] > 0]; los = OOS[OOS[f"mn{H}"] <= 0]
    print(f"  {'indicator':12} {'winners(mean)':>14} {'losers(mean)':>14}")
    for f in ("rsi", "vol_ratio", "atrp", "dist_ema50", "extension"):
        print(f"  {f:12} {win[f].mean():>14.3f} {los[f].mean():>14.3f}")
    print("  (if these columns look different it is HINDSIGHT on OOS — the matched-null")
    print("   test above is the only thing that says whether it is real.)")


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
