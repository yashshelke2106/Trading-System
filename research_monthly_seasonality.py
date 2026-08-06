"""
research_monthly_seasonality.py — which of the top-50 names have momentum in
which calendar month, and does any of it survive the multiple-testing null?

WHAT IS BEING ASKED
-------------------
"Find which stock has good momentum in which month, sort them, then wait for
breakouts in those names during those months."

WHY THIS NEEDS A NULL, NOT JUST A RANKING
-----------------------------------------
50 symbols x 12 months = 600 cells, and 10 years of history gives about 10
observations per cell. Ranking 600 noisy averages ALWAYS produces a
spectacular top of the table -- at p<0.05 roughly 30 cells clear by chance
alone. The ranking is not the finding; whether the ranking beats a shuffle is.

Three checks are applied, in increasing severity:

  1. RAW      : mean monthly return per (symbol, month), ranked
  2. BONFERRONI: |t| needed for 600 simultaneous tests, not one
  3. SHUFFLE  : month labels permuted within each symbol 200 times. This
                preserves each symbol's own return distribution and destroys
                only the calendar linkage, so the best cell from a shuffled
                world is exactly what "no seasonality" looks like. If the real
                best cell is not clearly outside that distribution, the pattern
                is noise wearing a calendar.

A SPLIT-SAMPLE test follows: does a month that worked in the first half of the
history keep working in the second? A seasonal effect that does not persist
cannot be traded, because you can only ever act on the earlier half.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(ROOT, "data", "history")
TOP50 = os.path.join(ROOT, "data", "top50", "_ranking.csv")

N_SHUFFLE = 200
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def load_monthly() -> pd.DataFrame:
    """Monthly returns for the top-50 names, corporate-action cleaned."""
    from core.corporate_actions import clean_returns

    top = pd.read_csv(TOP50)
    symbols = list(top.iloc[:, 0])

    rows = []
    for sym in symbols:
        p = os.path.join(HIST, f"{sym}.csv")
        if not os.path.exists(p):
            continue
        d = pd.read_csv(p, usecols=["date", "close"])
        d["date"] = pd.to_datetime(d["date"], errors="coerce")
        d = d.dropna(subset=["date"]).sort_values("date").set_index("date")
        # Daily returns with split/bonus days neutralised, then compounded.
        r = clean_returns(d["close"]).fillna(0.0)
        m = (1 + r).resample("ME").prod() - 1
        for dt, val in m.items():
            if np.isfinite(val):
                rows.append({"symbol": sym, "date": dt, "month": dt.month,
                             "year": dt.year, "ret": val * 100})
    return pd.DataFrame(rows)


def cell_table(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(["symbol", "month"])["ret"]
    t = pd.DataFrame({"n": g.size(), "mean": g.mean(), "std": g.std()})
    t["t"] = t["mean"] / (t["std"] / np.sqrt(t["n"]))
    return t.reset_index().dropna(subset=["t"])


def shuffle_null(df: pd.DataFrame, n_iter: int = N_SHUFFLE) -> np.ndarray:
    """Best cell mean from worlds where the calendar carries no information."""
    rng = np.random.default_rng(42)
    best = []
    arrs = {s: g["ret"].to_numpy() for s, g in df.groupby("symbol")}
    months = {s: g["month"].to_numpy() for s, g in df.groupby("symbol")}
    for _ in range(n_iter):
        rows = []
        for s, vals in arrs.items():
            mm = rng.permutation(months[s])       # break the calendar link only
            rows.append(pd.DataFrame({"symbol": s, "month": mm, "ret": vals}))
        sh = pd.concat(rows, ignore_index=True)
        cm = sh.groupby(["symbol", "month"])["ret"].mean()
        best.append(cm.max())
    return np.array(best)


def main() -> None:
    df = load_monthly()
    print(f"monthly observations: {len(df):,} across "
          f"{df['symbol'].nunique()} symbols "
          f"({df['year'].min()} .. {df['year'].max()})")

    t = cell_table(df)
    n_cells = len(t)
    print(f"cells (symbol x month): {n_cells}   "
          f"median obs/cell: {t['n'].median():.0f}\n")

    top = t.sort_values("mean", ascending=False).head(20)
    print("TOP 20 (symbol, month) BY MEAN MONTHLY RETURN")
    print(f"{'symbol':<13s} {'month':>5s} {'mean%':>8s} {'t':>6s} {'n':>4s}")
    print("-" * 40)
    for _, r in top.iterrows():
        print(f"{r['symbol']:<13s} {MONTHS[int(r['month'])-1]:>5s} "
              f"{r['mean']:8.2f} {r['t']:6.2f} {int(r['n']):4d}")

    # --- Bonferroni ---
    from scipy import stats as sps
    crit = abs(sps.norm.ppf(0.025 / n_cells))
    survivors = t[t["t"].abs() >= crit]
    print(f"\nBONFERRONI for {n_cells} tests: need |t| >= {crit:.2f}")
    print(f"  cells surviving: {len(survivors)}")
    if len(survivors):
        for _, r in survivors.sort_values("mean", ascending=False).iterrows():
            print(f"    {r['symbol']:<13s} {MONTHS[int(r['month'])-1]:>5s} "
                  f"mean {r['mean']:+.2f}%  t {r['t']:.2f}")

    # --- Shuffle null ---
    print(f"\nSHUFFLE NULL ({N_SHUFFLE} permutations of the calendar) ...",
          flush=True)
    null = shuffle_null(df)
    real_best = t["mean"].max()
    pctile = float((null < real_best).mean() * 100)
    print(f"  best REAL cell     : {real_best:+.2f}%")
    print(f"  best SHUFFLED cell : mean {null.mean():+.2f}%  "
          f"p95 {np.percentile(null, 95):+.2f}%  max {null.max():+.2f}%")
    print(f"  the real best sits at the {pctile:.0f}th percentile of the null")
    if pctile < 95:
        print("  => indistinguishable from a world with NO seasonality")
    else:
        print("  => exceeds the shuffled null; worth a persistence check")

    # --- Split-sample persistence ---
    mid = int(df["year"].median())
    a = cell_table(df[df["year"] <= mid])[["symbol", "month", "mean"]]
    b = cell_table(df[df["year"] > mid])[["symbol", "month", "mean"]]
    j = a.merge(b, on=["symbol", "month"], suffixes=("_early", "_late"))
    if len(j) > 30:
        rho = j["mean_early"].corr(j["mean_late"], method="spearman")
        top_early = j.nlargest(50, "mean_early")
        print(f"\nPERSISTENCE (<= {mid} vs > {mid}), {len(j)} paired cells")
        print(f"  rank correlation early->late : {rho:+.3f}")
        print(f"  top-50 early cells, later mean: "
              f"{top_early['mean_late'].mean():+.2f}%  "
              f"(all cells: {j['mean_late'].mean():+.2f}%)")
        print("  A seasonal edge you can trade must show a clearly positive")
        print("  correlation here -- you can only ever act on the early half.")


if __name__ == "__main__":
    main()
