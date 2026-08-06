"""
research_iv_rank_premium.py — does selling premium pay WHEN IT IS EXPENSIVE?

THE REFINEMENT UNDER TEST
-------------------------
Selling OTM strangles unconditionally came out at exactly zero: 73.6% wins,
mean -0.011% of spot, t=-0.25 (research_short_premium.py). That is what a
fairly-priced option market looks like on average.

But "on average" is the wrong question if the premium is sometimes rich and
sometimes cheap. The standard practitioner rule is to sell only when implied
volatility is high relative to its own history -- IV rank -- and stand aside
otherwise. If the volatility risk premium is real but concentrated, the
unconditional zero would be a rich half and a poor half cancelling out, and
splitting by richness should separate them.

If instead every richness bucket sits near zero, the premium is fairly priced
in every state, which is a much stronger and more final conclusion than the
unconditional test could give.

RICHNESS MEASURE
----------------
Rather than invert Black-Scholes on stale end-of-day marks (which produces
garbage IV on illiquid strikes), richness is measured directly as what the
seller is actually paid:

    richness = (call premium + put premium) / spot,  normalised by sqrt(DTE)

then RANKED WITHIN EACH SYMBOL against its own history, so a structurally
volatile name is not permanently classed as "expensive". That per-symbol rank
IS an IV rank in everything but name, and it is computed only from data
available at entry.

EXPANDING-WINDOW RANK, NOT FULL-SAMPLE
--------------------------------------
The rank for a trade uses only that symbol's PRIOR observations. Ranking
against the full sample would leak the future into the entry decision and is
the most common way this exact study gets faked.

RESULT (2026-08-06): THE PRACTITIONER RULE IS INVERTED, AND THE INVERSION IS
ALSO NOT TRADEABLE
---------------------------------------------------------------------------
    band                    n   win%   mean%      t   RoM/yr%
    LOW iv_rank (cheap) 1,776   79.1  +0.151   2.55     +9.6
    MID                 1,139   75.3  +0.111   1.22     +7.1
    HIGH (rich)         1,897   67.4  -0.275  -3.17    -17.5
    TOP decile          911     66.5  -0.329  -2.54    -20.9

Monotone, and backwards from "sell when IV is high". The economics make sense:
IV is high precisely when a large move is coming, and it is broadly RIGHT
about that, so rich premium is compensation for real risk rather than a
mispricing to harvest.

But the cheap-premium leg does NOT survive scrutiny, in three separate ways:

  by year     2024 -0.167% (t=-1.33) | 2025 +0.369% (t=5.74) | 2026 -0.240%
              (t=-1.23)  -- the entire result is one year
  costs       10% of premium +0.151% (t=2.55) | 20% +0.016% (t=0.28)
              | 30% -0.118% (t=-2.00)  -- and 20-30% is realistic for OTM
              single-stock spreads, so the 10% default was optimistic
  regime      market-up cycles +0.495% (t=6.77) | market-down -0.161%
              (t=-1.79)  -- it earns only when the market rises, i.e. it is
              short-vol beta wearing a strategy costume

So: no edge selling rich premium, and no edge selling cheap premium either.
The volatility risk premium in this market is not harvestable in either
direction, which closes the options sleeve for good.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import research_short_premium as SP

COST_MULT = SP.COST_PER_LEG_PCT


def add_richness(t: pd.DataFrame) -> pd.DataFrame:
    """Per-symbol expanding-window rank of premium richness at entry."""
    t = t.sort_values("entry").copy()
    t["dte"] = (t["expiry"] - t["entry"]).dt.days.clip(lower=1)
    t["richness"] = (t["premium"] / t["spot"]) / np.sqrt(t["dte"])

    # Expanding rank within symbol: strictly prior observations only.
    def _rank(g: pd.Series) -> pd.Series:
        return g.expanding().apply(
            lambda w: (w[:-1] < w[-1]).mean() if len(w) > 1 else np.nan,
            raw=True)

    t["iv_rank"] = t.groupby("symbol")["richness"].transform(_rank)
    return t


def report_band(g: pd.DataFrame, label: str) -> dict:
    p = g["pnl_pct_spot"]
    if len(p) < 100:
        return {}
    t_stat = p.mean() / (p.std() / np.sqrt(len(p))) if p.std() > 0 else 0.0
    margin_frac = 0.198
    ror = p.mean() / (margin_frac * 100) * 100
    return {"band": label, "n": len(p), "win": (p > 0).mean() * 100,
            "mean": p.mean(), "median": p.median(), "t": t_stat,
            "worst": p.min(), "p1": p.quantile(0.01),
            "avg_win": p[p > 0].mean(), "avg_loss": p[p < 0].mean(),
            "ror_yr": ror * (252 / SP.ENTRY_DTE)}


def main() -> None:
    df = SP.load_options()
    print(f"option archive: {len(df):,} rows")
    t = SP.build_trades(df)
    t = add_richness(t).dropna(subset=["iv_rank"])
    print(f"trades with a prior-history rank: {len(t):,} "
          f"({t['symbol'].nunique()} symbols)\n")

    bands = [
        ("LOW iv_rank (cheap)", t["iv_rank"] <= 0.33),
        ("MID", (t["iv_rank"] > 0.33) & (t["iv_rank"] <= 0.66)),
        ("HIGH iv_rank (rich)", t["iv_rank"] > 0.66),
        ("TOP decile (richest)", t["iv_rank"] > 0.90),
    ]

    hdr = (f"{'band':22s} {'n':>6s} {'win%':>6s} {'mean%':>7s} {'med%':>7s} "
           f"{'t':>6s} {'p1%':>7s} {'worst%':>8s} {'RoM/yr%':>8s}")
    print(hdr); print("-" * len(hdr))
    rows = []
    for label, mask in bands:
        r = report_band(t[mask], label)
        if not r:
            continue
        rows.append(r)
        print(f"{r['band']:22s} {r['n']:6,} {r['win']:6.1f} {r['mean']:+7.3f} "
              f"{r['median']:+7.3f} {r['t']:6.2f} {r['p1']:+7.2f} "
              f"{r['worst']:+8.2f} {r['ror_yr']:+8.1f}")

    res = pd.DataFrame(rows)
    if res.empty:
        return

    hi = res[res.band.str.startswith("HIGH")]
    lo = res[res.band.str.startswith("LOW")]
    if not hi.empty and not lo.empty:
        spread = hi["mean"].iloc[0] - lo["mean"].iloc[0]
        print(f"\nRICH minus CHEAP: {spread:+.3f}% of spot per trade")
        print("If the volatility risk premium were real and concentrated, this")
        print("would be clearly positive. Near zero means the premium is fairly")
        print("priced in EVERY state, not just on average.")

    print("\nTail check — selling rich premium should still be judged on the "
          "left tail, since that is where short options pay for their wins.")
    for r in rows:
        print(f"  {r['band']:22s} avg win {r['avg_win']:+.3f}%  "
              f"avg loss {r['avg_loss']:+.3f}%  "
              f"ratio {abs(r['avg_loss']/r['avg_win']):.2f}x")


if __name__ == "__main__":
    main()
