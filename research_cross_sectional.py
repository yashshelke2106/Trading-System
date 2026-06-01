"""
Cross-sectional factor research — hunt a REAL edge, the right way.

Everything tested so far was single-name ABSOLUTE timing ("will THIS stock go
up?"). On NSE F&O daily bars that has no edge after costs (proven: india_swing
PF 0.72; MR2/breakout/gap all fail OOS). The market is efficient at that game.

This tests something categorically different and academically robust across
markets INCLUDING India: CROSS-SECTIONAL factors — "which stocks outperform the
OTHERS?". You rank the whole universe each month by a factor, hold the top
basket, and measure return RELATIVE TO the equal-weight universe. The benchmark
is the universe itself, so a rising market cancels out — what's left is true
alpha, not beta.

Factors tested (each a number computed per stock at each rebalance, no
lookahead):
  mom_12_1   12-month return skipping the last month (classic momentum — the
             single most documented anomaly in finance; the skip avoids the
             short-term reversal contamination).
  mom_6_1    6-month momentum (faster).
  rev_1w     short-term reversal: last-5-day return, go LONG the biggest
             LOSERS (they bounce). Cross-sectional, unlike the single-name
             reversal that failed.
  lowvol     long the lowest 20-day realized volatility (low-vol anomaly).
  hi52       proximity to 52-week high (52-week-high momentum).

Method per factor:
  - Monthly rebalance (21 trading days), long the TOP quintile (~20%).
  - Forward return = next-21-day return of that basket, equal weight.
  - EXCESS = basket return - universe (all-stock equal-weight) return.
  - Report mean monthly excess, % months positive, annualised, and an
    IN-SAMPLE vs OUT-OF-SAMPLE split. A real factor has POSITIVE excess in
    BOTH halves. Costs modelled as turnover * per-trade cost.

Run (real Dhan, your machine):
    python research_cross_sectional.py            # 30 names quick
    python research_cross_sectional.py --full     # full ~150 F&O universe
"""

from __future__ import annotations

import argparse
import warnings
warnings.filterwarnings("ignore")
import logging
logging.getLogger("core.api_dhan").setLevel(logging.CRITICAL)

import numpy as np
import pandas as pd

DAYS = 730
REBAL = 21            # trading-day rebalance (~monthly)
HOLD = 21            # holding period = rebalance
TOP_QUANTILE = 0.20   # long top 20%
COST_PCT = 0.06       # per-trade round-trip %; applied on turnover
OOS_FRACTION = 0.30

DEFAULT_UNIVERSE = [
    "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","SBIN","BHARTIARTL","KOTAKBANK",
    "BAJFINANCE","HINDUNILVR","ITC","LT","AXISBANK","MARUTI","ASIANPAINT","WIPRO",
    "HCLTECH","TECHM","SUNPHARMA","DRREDDY","CIPLA","DIVISLAB","TATAMOTORS","M&M",
    "BAJAJ-AUTO","ULTRACEMCO","POWERGRID","NTPC","ONGC","NESTLEIND",
]


def build_panel(universe):
    """Return a close-price panel: DataFrame indexed by date, columns=symbols."""
    from core.api_dhan import dhan_daily
    print(f"[XS] fetching {len(universe)} symbols ({DAYS}d) ...")
    cols = {}
    for s in universe:
        d = dhan_daily(s, days_back=DAYS)
        if d is None or d.empty:
            continue
        d = d.copy(); d.columns = [c.lower() for c in d.columns]
        d["date"] = pd.to_datetime(d["date"]).dt.normalize()
        ser = d.set_index("date")["close"]
        # Dhan occasionally returns duplicate dates for a symbol — keep the
        # last and sort, else the panel build fails on duplicate index labels.
        ser = ser[~ser.index.duplicated(keep="last")].sort_index()
        cols[s] = ser
    # Build on the UNION of all (deduped) dates, aligning each symbol to it.
    panel = pd.concat(cols, axis=1).sort_index()
    panel = panel.dropna(how="all")
    print(f"[XS] panel: {panel.shape[0]} dates x {panel.shape[1]} symbols\n")
    return panel


def factor_value(name, panel, t_idx):
    """Compute factor for every symbol at date-index t_idx (uses only data
    up to t_idx). Returns a Series symbol->value (NaN where insufficient)."""
    p = panel
    c = p.iloc[t_idx]
    if name == "mom_12_1":
        if t_idx < 252: return None
        return p.iloc[t_idx-21] / p.iloc[t_idx-252] - 1
    if name == "mom_6_1":
        if t_idx < 126: return None
        return p.iloc[t_idx-21] / p.iloc[t_idx-126] - 1
    if name == "rev_1w":
        if t_idx < 6: return None
        # biggest LOSERS rank highest -> long them
        return -(c / p.iloc[t_idx-5] - 1)
    if name == "lowvol":
        if t_idx < 21: return None
        rets = p.iloc[t_idx-20:t_idx+1].pct_change()
        return -rets.std()          # lowest vol ranks highest
    if name == "hi52":
        if t_idx < 252: return None
        hi = p.iloc[t_idx-251:t_idx+1].max()
        return c / hi               # closeness to 52w high
    return None


def run_factor(name, panel):
    dates = panel.index
    n = len(dates)
    periods = []  # (date, excess_return)
    prev_basket = set()
    for t in range(252, n - HOLD, REBAL):
        f = factor_value(name, panel, t)
        if f is None:
            continue
        c_now = panel.iloc[t]
        c_fwd = panel.iloc[t + HOLD]
        valid = f.dropna().index
        # need price now and forward
        valid = [s for s in valid if not pd.isna(c_now[s]) and not pd.isna(c_fwd[s]) and c_now[s] > 0]
        if len(valid) < 10:
            continue
        f = f[valid].sort_values(ascending=False)
        k = max(3, int(len(valid) * TOP_QUANTILE))
        basket = list(f.index[:k])
        # forward equal-weight returns
        fwd = {s: (c_fwd[s] - c_now[s]) / c_now[s] for s in valid}
        basket_ret = np.mean([fwd[s] for s in basket])
        univ_ret = np.mean([fwd[s] for s in valid])
        # turnover cost: fraction of basket replaced * round-trip cost
        turn = 1.0 - (len(prev_basket & set(basket)) / max(len(basket), 1)) if prev_basket else 1.0
        cost = turn * (COST_PCT / 100)
        excess = (basket_ret - univ_ret) - cost
        periods.append((dates[t], excess))
        prev_basket = set(basket)
    return periods


def summ(periods):
    if not periods:
        return None
    ex = np.array([e for _, e in periods])
    pos = (ex > 0).mean() * 100
    mean_m = ex.mean() * 100
    ann = ((1 + ex.mean()) ** 12 - 1) * 100   # ~12 rebalances/yr
    t_stat = ex.mean() / (ex.std() / np.sqrt(len(ex))) if ex.std() > 0 else 0
    return {"n": len(ex), "mean_m": mean_m, "pos": pos, "ann": ann, "t": t_stat}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()
    if args.full:
        try:
            from core.universe import FO_UNIVERSE
            universe = list(dict.fromkeys(FO_UNIVERSE))
        except Exception:
            universe = DEFAULT_UNIVERSE
    else:
        universe = DEFAULT_UNIVERSE

    panel = build_panel(universe)
    if panel.shape[1] < 15:
        print("Need >=15 symbols for a cross-section. Check Dhan."); return

    split_i = int(len(panel) * (1 - OOS_FRACTION))
    split_date = panel.index[split_i]
    print(f"OOS split: {str(split_date)[:10]}   (excess return measured vs equal-weight universe)\n")

    FACTORS = ["mom_12_1", "mom_6_1", "rev_1w", "lowvol", "hi52"]
    print(f"{'factor':10} | {'IS  n':>5} {'exc/mo':>7} {'pos%':>5} {'ann%':>6} {'t':>5} | "
          f"{'OOS n':>5} {'exc/mo':>7} {'pos%':>5} {'ann%':>6} {'t':>5} | verdict")
    print("-" * 104)
    for fac in FACTORS:
        periods = run_factor(fac, panel)
        is_p = [(d, e) for d, e in periods if d < split_date]
        oos_p = [(d, e) for d, e in periods if d >= split_date]
        si, so = summ(is_p), summ(oos_p)
        def fmt(x):
            return (f"{x['n']:>5} {x['mean_m']:>+6.2f}% {x['pos']:>4.0f}% {x['ann']:>+5.1f}% {x['t']:>5.2f}"
                    if x else f"{'-':>5} {'-':>7} {'-':>5} {'-':>6} {'-':>5}")
        verdict = "thin"
        if si and so and si["n"] >= 6 and so["n"] >= 6:
            if si["mean_m"] > 0 and so["mean_m"] > 0:
                verdict = ">>> EDGE (alpha both halves)"
                if so["t"] >= 1.5: verdict = ">>> STRONG EDGE (alpha both, t>1.5)"
            elif si["mean_m"] > 0.3 and so["mean_m"] <= 0:
                verdict = "fades OOS"
            else:
                verdict = "no alpha"
        print(f"{fac:10} | {fmt(si)} | {fmt(so)} | {verdict}")

    print("\nExcess = top-quintile basket return MINUS equal-weight universe (beta")
    print("removed). A REAL factor shows POSITIVE excess in BOTH IS and OOS. 't'")
    print("is the t-stat of monthly excess; |t|>1.5 is suggestive, >2 is solid.")


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
