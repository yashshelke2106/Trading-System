"""
pairs_test.py — statistical arbitrage / pairs trading. A genuinely NEW, untested
category for this system: MARKET-NEUTRAL relative value, not directional.

THE IDEA (real economic reason): two same-sector names (HDFCBANK/ICICIBANK)
share risk factors, so their price RATIO mean-reverts. Trade the spread, not the
market. Documented edge (Gatev, Goetzmann, Rouwenhorst 2006) — decayed since it
got crowded, but never tested here.

THE DISCIPLINE (this is what makes it honest, not data-mining):
  * Pairs are SELECTED using ONLY the first ~55% of history (in-sample): same
    sector + return-correlation + a mean-reverting spread (OU half-life 2-30d).
  * Those EXACT pairs are then traded BLIND on the held-out last ~45% (OOS).
    The OOS result is the only one that counts. Selecting pairs on data you then
    trade is the classic look-ahead — we don't do that.
  * Fixed textbook entry/exit (z>2 in, z<0.5 out, z>3.5 stop, 20d max). No tuning.
  * Net of costs (2 instruments per trade); cost sweep shown.

Run:  python pairs_test.py --full --limit 60 --days 1460
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from itertools import combinations
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest_live_pipeline import fetch_daily, DEFAULT_UNIVERSE
try:
    import config
    COST_RT = float(getattr(config, "FUT_COST_ROUNDTRIP_PCT", 0.06)) / 100.0
except Exception:
    COST_RT = 0.0006
# Use the RICHER sector map (signal_finalize, ~90 names) for more candidate pairs;
# fall back to config's. Normalise to a single case so pairs compare cleanly.
SECTOR: Dict[str, str] = {}
try:
    from core.signal_finalize import SECTOR_MAP as _SM
    SECTOR.update({k.upper(): str(v).lower() for k, v in _SM.items()})
except Exception:
    pass
try:
    for k, v in getattr(config, "STOCK_SECTOR_MAP", {}).items():
        SECTOR.setdefault(k.upper(), str(v).lower())
except Exception:
    pass

# Fixed textbook params
Z_ENTRY, Z_EXIT, Z_STOP, MAX_HOLD = 2.0, 0.5, 3.5, 20
ROLL = 30                 # rolling window for OOS z-score (causal)
IS_FRAC = 0.55
MIN_CORR = 0.7
HL_MIN, HL_MAX = 2.0, 30.0   # acceptable OU half-life (days)


def half_life(spread: pd.Series) -> float:
    s = spread.dropna()
    if len(s) < 40:
        return np.inf
    ds = s.diff().dropna()
    lag = s.shift(1).dropna()
    idx = ds.index.intersection(lag.index)
    if len(idx) < 30:
        return np.inf
    beta = np.polyfit(lag.loc[idx].values, ds.loc[idx].values, 1)[0]
    if beta >= 0:
        return np.inf
    return float(-np.log(2) / beta)


def select_pairs(pc_is: pd.DataFrame) -> List[Tuple[str, str]]:
    """Pick same-sector, correlated, mean-reverting pairs using IN-SAMPLE only."""
    cols = [c for c in pc_is.columns if c in SECTOR]
    rets = pc_is[cols].pct_change()
    chosen = []
    for a, b in combinations(cols, 2):
        if SECTOR.get(a) != SECTOR.get(b):
            continue
        r = rets[[a, b]].dropna()
        if len(r) < 60:
            continue
        if r[a].corr(r[b]) < MIN_CORR:
            continue
        spread = np.log(pc_is[a]) - np.log(pc_is[b])
        hl = half_life(spread)
        if HL_MIN <= hl <= HL_MAX:
            chosen.append((a, b))
    return chosen


def trade_oos(pc_oos: pd.DataFrame, pair: Tuple[str, str], cost: float) -> List[float]:
    """Trade ONE pair on OOS data with rolling z-score. Returns list of net %s."""
    a, b = pair
    spread = (np.log(pc_oos[a]) - np.log(pc_oos[b])).dropna()
    if len(spread) < ROLL + 5:
        return []
    mu = spread.rolling(ROLL).mean()
    sd = spread.rolling(ROLL).std()
    z = (spread - mu) / sd
    trades = []
    pos = 0          # +1 long-spread (long A short B), -1 short-spread
    entry_spread = 0.0
    held = 0
    for i in range(ROLL, len(spread)):
        zi = z.iloc[i]
        if pos == 0:
            if zi >= Z_ENTRY:
                pos, entry_spread, held = -1, spread.iloc[i], 0     # spread rich -> short it
            elif zi <= -Z_ENTRY:
                pos, entry_spread, held = +1, spread.iloc[i], 0
        else:
            held += 1
            exit_now = (abs(zi) <= Z_EXIT or abs(zi) >= Z_STOP or held >= MAX_HOLD)
            if exit_now:
                move = spread.iloc[i] - entry_spread          # log-spread change
                pnl = (pos * move) - cost                      # pos=+1 profits if spread rises
                trades.append(float(pnl))
                pos = 0
    return trades


def _stats(r: np.ndarray) -> Dict:
    if len(r) == 0:
        return {"n": 0}
    w, l = r[r > 0], r[r < 0]
    pf = w.sum() / -l.sum() if l.size else float("inf")
    sh = (r.mean() / r.std() * np.sqrt(len(r))) if r.std() > 0 else 0.0  # per-trade Sharpe-ish
    return {"n": len(r), "wr": 100 * len(w) / len(r), "pf": pf,
            "exp": float(r.mean()) * 100, "sharpe": sh}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1460)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()
    if args.full:
        try:
            from core.universe import FO_UNIVERSE
            syms = list(dict.fromkeys(FO_UNIVERSE))
        except Exception:
            syms = DEFAULT_UNIVERSE
    else:
        syms = DEFAULT_UNIVERSE
    syms = syms[:args.limit] if args.limit else syms

    print(f"[PAIRS] fetching {len(syms)} names ({args.days}d) ...")
    C = {}
    for s in syms:
        df = fetch_daily(s, args.days)
        if df is not None and len(df) > 300:
            df = df[~df.index.duplicated(keep="last")]
            C[s] = df["close"]
    pc = pd.DataFrame(C).sort_index().ffill().dropna(how="all")
    if pc.shape[1] < 6:
        print("[PAIRS] FATAL: not enough names."); return 1
    n = len(pc)
    cut = int(n * IS_FRAC)
    pc_is, pc_oos = pc.iloc[:cut], pc.iloc[cut:]
    print(f"[PAIRS] {pc.shape[1]} names x {n} days. "
          f"IS {pc.index[0].date()}->{pc.index[cut-1].date()}  "
          f"OOS {pc.index[cut].date()}->{pc.index[-1].date()}")

    pairs = select_pairs(pc_is)
    print(f"[PAIRS] selected {len(pairs)} same-sector mean-reverting pairs IN-SAMPLE:")
    for a, b in pairs[:20]:
        print(f"   {a}-{b} ({SECTOR.get(a)})")
    if not pairs:
        print("[PAIRS] no qualifying pairs."); return 0

    print("\n" + "=" * 64)
    print("  OUT-OF-SAMPLE pairs trading (pairs picked on IS, traded blind on OOS)")
    print("=" * 64)
    for cost in (0.0012, 0.0020, 0.0030):     # 2-instrument round trip cost levels
        allt = []
        for p in pairs:
            allt.extend(trade_oos(pc_oos, p, cost))
        r = np.array(allt)
        s = _stats(r)
        if s["n"] == 0:
            print(f"  cost {cost*100:.2f}%: no trades"); continue
        half = len(r) // 2
        h2 = _stats(r[half:])
        pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        print(f"  cost {cost*100:.2f}%/trade:  n={s['n']:>4}  WR {s['wr']:.0f}%  "
              f"PF {pf}  exp {s['exp']:+.3f}%/trade  perTradeSharpe {s['sharpe']:.2f}")
    print("=" * 64)
    # verdict at a realistic 0.20% (two liquid legs, in+out)
    allt = [t for p in pairs for t in trade_oos(pc_oos, p, 0.0020)]
    r = np.array(allt); s = _stats(r) if len(allt) else {"n": 0}
    print("\n  VERDICT (@0.20%/trade realistic, OOS only):")
    if s.get("n", 0) >= 30 and s["pf"] > 1.2 and s["exp"] > 0:
        print(f"  PASSES OOS: PF {s['pf']:.2f}, exp {s['exp']:+.3f}%/trade, n={s['n']}.")
        print("  REAL LEAD — market-neutral, survived held-out. Next: more pairs, more")
        print("  history through a crash, real borrow/short costs, point-in-time sectors.")
    else:
        pfv = s.get("pf", 0)
        print(f"  Does not clear the bar (PF {pfv if isinstance(pfv,str) else round(pfv,2)}, "
              f"exp {s.get('exp',0):+.3f}%, n={s.get('n',0)}). Pairs reversion didn't")
        print("  survive OOS + costs here either — consistent with a decayed, crowded edge.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
