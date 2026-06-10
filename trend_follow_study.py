"""
Trend-following / let-winners-run study — does NSE F&O contain a capturable
fat RIGHT TAIL? (User goal 2026-06-10: "capture stock moves that make a fortune".)

This answers a DIFFERENT question than the breakout/factor/VRP tests. Those
measured MEAN return over fixed short windows — which structurally caps the
right tail and drowns the rare huge winner. Fortune-capture is a low-win-rate,
positive-SKEW game: lose small often, win big rarely, let winners run for
months. So here we measure the DISTRIBUTION and TAIL, not win rate.

PRE-REGISTERED design (canonical trend-following, chosen before looking):
  ENTRY : Donchian N=50 breakout (close > prior 50-day high = long; < 50-day low
          = short). The classic Turtle/CTA momentum entry. Enter NEXT open.
  EXIT  : let winners run — trailing stop = max(initial 2.5*ATR stop, 20-day
          Donchian low). NO time limit; hold until the trail is hit. This is
          what gives the strategy its right tail.
  COSTS : 0.20% round-trip per trade.
  JUDGE : market-NEUTRAL per-trade return (stock minus NIFTY over the SAME hold)
          AND raw (what you'd actually feel, beta included). IS/OOS by date.

THE MIRAGE THIS BUSTS — "skew is not edge":
  A trailing stop produces positive skew MECHANICALLY (losses capped, winners
  open-ended) regardless of whether the entry has any signal. So we run a
  MATCHED NULL: identical exit engine, but ENTER ON RANDOM DAYS. If the Donchian
  momentum entry does not beat random entries on mean MN return AND profit
  factor, the 'fortune capture' is just the exit geometry + bull-market beta,
  not a tradeable signal. Reports tail-concentration (share of P&L from the top
  5% of trades) so you can SEE whether a few moves make the fortune — and
  whether the null makes them too.

Run:
    python trend_follow_study.py                 # top 100
    python trend_follow_study.py --all           # full 153 universe
    python trend_follow_study.py --perms 300
"""
from __future__ import annotations

import argparse, sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import warnings; warnings.filterwarnings("ignore")
import logging
for n in ("core.api_dhan", "core.bar_cache"):
    logging.getLogger(n).setLevel(logging.CRITICAL)
import numpy as np
import pandas as pd

N_ENTRY  = 50
M_EXIT   = 20
ATR_MULT = 2.5
COST_RT  = 0.0020
RNG      = np.random.default_rng(11)


def atr_arr(h, l, c, n=14):
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    tr = np.concatenate([[np.nan], tr])
    return pd.Series(tr).rolling(n).mean().values


def simulate(o, h, l, c, dates, nifty_on_date, entry_idx, direction):
    """Run the trailing-exit engine from a given set of entry bar indices.
    Returns list of (entry_date, exit_date, hold, raw_ret, mn_ret)."""
    a = atr_arr(h, l, c)
    if direction == "long":
        trail_base = pd.Series(l).rolling(M_EXIT).min().shift(1).values   # 20d low
    else:
        trail_base = pd.Series(h).rolling(M_EXIT).max().shift(1).values   # 20d high
    n = len(c)
    out = []
    entry_set = sorted(set(int(i) for i in entry_idx if 0 < i < n - 2))
    busy_until = -1
    for i in entry_idx_sorted(entry_set):
        if i <= busy_until:                       # no overlapping positions per symbol
            continue
        ei = i + 1                                # enter next open
        if ei >= n - 1 or not np.isfinite(o[ei]) or o[ei] <= 0 or not np.isfinite(a[i]):
            continue
        entry = o[ei]
        init_stop = entry - ATR_MULT * a[i] if direction == "long" else entry + ATR_MULT * a[i]
        xi = None
        for j in range(ei, n - 1):
            tb = trail_base[j]
            if direction == "long":
                stop = init_stop if not np.isfinite(tb) else max(init_stop, tb)
                if c[j] < stop:
                    xi = j + 1; break
            else:
                stop = init_stop if not np.isfinite(tb) else min(init_stop, tb)
                if c[j] > stop:
                    xi = j + 1; break
        if xi is None:
            xi = n - 1
        exit_px = o[xi] if np.isfinite(o[xi]) and o[xi] > 0 else c[xi - 1]
        raw = (exit_px / entry - 1.0) if direction == "long" else (entry / exit_px - 1.0)
        n0, n1 = nifty_on_date(dates[ei]), nifty_on_date(dates[xi])
        nret = (n1 / n0 - 1.0) if (n0 and n1 and n0 > 0) else 0.0
        mn = raw - (nret if direction == "long" else -nret)
        out.append((dates[ei], dates[xi], xi - ei, raw, mn))
        busy_until = xi
    return out


def entry_idx_sorted(s):
    return s


def donchian_entries(h, l, c, direction):
    if direction == "long":
        lvl = pd.Series(h).rolling(N_ENTRY).max().shift(1).values
        sig = (c > lvl) & np.concatenate([[False], c[:-1] <= lvl[1:]])
    else:
        lvl = pd.Series(l).rolling(N_ENTRY).min().shift(1).values
        sig = (c < lvl) & np.concatenate([[False], c[:-1] >= lvl[1:]])
    return np.where(sig)[0]


# ── stats on a trade table ───────────────────────────────────────────────────
def describe(trades, label, cost=COST_RT):
    if not trades:
        print(f"  {label}: no trades"); return None
    df = pd.DataFrame(trades, columns=["entry", "exit", "hold", "raw", "mn"])
    fin = np.isfinite(df["mn"].values) & np.isfinite(df["raw"].values)
    df = df[fin]
    if df.empty:
        print(f"  {label}: no valid trades"); return None
    mn = df["mn"].values - cost
    raw = df["raw"].values - cost
    win = (mn > 0).mean() * 100
    gw, gl = mn[mn > 0].sum(), -mn[mn < 0].sum()
    pf = gw / gl if gl > 0 else float("inf")
    # tail concentration: share of GROSS positive P&L from top 5% of winners
    wins = np.sort(mn[mn > 0])[::-1]
    top5 = wins[:max(1, int(len(wins) * 0.05))].sum() / wins.sum() * 100 if len(wins) else 0
    print(f"  {label}")
    print(f"     n={len(df):4d}  win%={win:4.0f}  mean_mn={mn.mean()*100:+.2f}%  "
          f"median={np.median(mn)*100:+.2f}%  PF={pf:.2f}")
    print(f"     mean_raw={raw.mean()*100:+.2f}%  skew={pd.Series(mn).skew():+.2f}  "
          f"max_win={mn.max()*100:+.1f}%  avg_hold={df['hold'].mean():.0f}d")
    print(f"     top-5% of winners = {top5:.0f}% of all winning P&L  "
          f"(↑ = a few moves make the fortune)")
    return {"n": len(df), "mean_mn": mn.mean(), "pf": pf, "mean_raw": raw.mean(),
            "skew": pd.Series(mn).skew(), "top5": top5}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--days", type=int, default=2900)
    ap.add_argument("--perms", type=int, default=300)
    args = ap.parse_args()

    from core.bar_cache import cached_daily
    from core.universe import FO_UNIVERSE, TOP100_FO
    universe = FO_UNIVERSE if args.all else TOP100_FO

    nf = cached_daily("NIFTY", args.days)
    if nf is None or nf.empty:
        print("no NIFTY"); return
    nser = nf["close"]; nser = nser[~nser.index.duplicated(keep="last")].sort_index()
    nmap = {pd.Timestamp(d).normalize(): float(v) for d, v in nser.items()}
    def nifty_on(d): return nmap.get(pd.Timestamp(d).normalize(), np.nan)

    print(f"[TF] {len(universe)} symbols, Donchian {N_ENTRY}/{M_EXIT}, trail {ATR_MULT}ATR ...")
    real = {"long": {}, "short": {}}               # sym -> list of trades
    sym_data = {}
    for j, sym in enumerate(universe):
        try:
            df = cached_daily(sym, args.days)
            if df is None or len(df) < N_ENTRY + 60:
                continue
            df = df[~df.index.duplicated(keep="last")].sort_index()
            o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
            dts = list(df.index)
            valid = np.arange(N_ENTRY + 2, len(df) - 2)
            sym_data[sym] = (o, h, l, c, dts, valid)
            for d in ("long", "short"):
                ent = donchian_entries(h, l, c, d)
                real[d][sym] = simulate(o, h, l, c, dts, nifty_on, ent, d)
        except Exception:
            continue
        if (j + 1) % 25 == 0:
            print(f"  ... {j+1}/{len(universe)}")

    flat = lambda dd: [t for s in dd for t in dd[s]]
    long_all, short_all = flat(real["long"]), flat(real["short"])
    alltr = long_all + short_all
    if not alltr:
        print("no trades"); return
    split = pd.DataFrame(alltr, columns=["entry","exit","hold","raw","mn"])["entry"].quantile(0.70)
    def seg(trs, oos): return [t for t in trs if (t[0] >= split) == oos]

    print("\n" + "=" * 74)
    print(f"  TREND-FOLLOWING — real Donchian entries   (OOS split {str(split)[:10]})")
    print("=" * 74)
    for d in ("long", "short"):
        print(f"\n  [{d.upper()}]")
        describe(seg(flat(real[d]), False), "IS ")
        describe(seg(flat(real[d]), True),  "OOS")
    print("\n  [LONG+SHORT — DESCRIPTIVE ONLY, non-realizable (overlapping L/S per name)]")
    describe(seg(alltr, True), "OOS")

    # ── MATCHED NULL on the FORTUNE LENS: LONG-ONLY, RAW, OOS-count matched ──
    # (Codex fix: judge what the user actually wants — absolute right tail of
    #  long breakouts — with the null matched on OOS-EXECUTED trade count per
    #  symbol and drawn only from OOS-window entry dates. Also test the tail
    #  magnitude p95 directly, not just the mean.)
    print("\n" + "-" * 74)
    print(f"  MATCHED NULL — LONG-ONLY, RAW returns, OOS-count matched ({args.perms} draws)")
    print("  Q: does the momentum entry catch BIGGER/MORE fortunes than random entry?")
    print("-" * 74)
    def lstats(trs):
        if not trs:
            return None
        raw = pd.DataFrame(trs, columns=["entry","exit","hold","raw","mn"])["raw"].values - COST_RT
        raw = raw[np.isfinite(raw)]
        if len(raw) == 0:
            return None
        gw, gl = raw[raw>0].sum(), -raw[raw<0].sum()
        return {"n": len(raw), "mean": raw.mean(), "pf": (gw/gl if gl>0 else np.nan),
                "p95": np.percentile(raw, 95), "max": raw.max()}
    real_long_oos = seg(flat(real["long"]), True)
    R = lstats(real_long_oos)
    # per-symbol OOS real-long count + OOS-restricted valid entry indices
    oos_valid = {s: [i for i in vd[5] if vd[4][i] >= split] for s, vd in sym_data.items()}
    real_oos_cnt = {s: sum(1 for t in real["long"][s] if t[0] >= split) for s in real["long"]}
    nm, npf, np95 = [], [], []
    for _ in range(args.perms):
        nt = []
        for s, vd in sym_data.items():
            cnt = real_oos_cnt.get(s, 0); pool = oos_valid.get(s, [])
            if cnt <= 0 or len(pool) == 0:
                continue
            o, h, l, c, dts, _ = vd
            ridx = RNG.choice(pool, size=min(cnt, len(pool)), replace=False)
            nt.extend(simulate(o, h, l, c, dts, nifty_on, ridx, "long"))
        S = lstats(nt)
        if S:
            nm.append(S["mean"]); npf.append(S["pf"]); np95.append(S["p95"])
    nm, npf, np95 = np.array(nm), np.array(npf), np.array(np95)
    p_mean = (np.sum(nm >= R["mean"]) + 1) / (len(nm) + 1)
    p_pf   = (np.nansum(npf >= R["pf"]) + 1) / (np.sum(np.isfinite(npf)) + 1)
    p_p95  = (np.sum(np95 >= R["p95"]) + 1) / (len(np95) + 1)
    print(f"  real LONG OOS:  n={R['n']}  mean_raw {R['mean']*100:+.2f}%  PF {R['pf']:.2f}  "
          f"p95 winner {R['p95']*100:+.1f}%  max {R['max']*100:+.0f}%")
    print(f"  random null  :  mean {nm.mean()*100:+.2f}% (p95band {np.percentile(nm,95)*100:+.2f}%)  "
          f"PF {np.nanmean(npf):.2f}  p95 {np.mean(np95)*100:+.1f}%")
    print(f"  p-values     :  mean p={p_mean:.3f}   PF p={p_pf:.3f}   tail(p95) p={p_p95:.3f}")
    beats = sum(p < 0.05 for p in (p_mean, p_pf, p_p95))
    print(f"\n  VERDICT (long, raw): momentum entry beats random on "
          f"{beats}/3 metrics (mean/PF/tail).")
    if R["mean"] <= 0:
        print(f"  Even ignoring the null, real long raw mean is {R['mean']*100:+.2f}% after cost "
              f"→ not profitable in this OOS window regardless.")
    if beats == 0:
        print("  → on THIS implementation/period, the entry adds nothing the random null doesn't.")
    print("\n  SCOPE (do not over-generalize): one param set (50/20/2.5), one daily-bar")
    print("  single-equity-market implementation, no pyramiding, no cross-asset diversification.")


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
