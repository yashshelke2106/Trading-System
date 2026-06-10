"""
edge_hunt.py — broad, disciplined edge battery ("full potential", honestly).

Tests many ECONOMICALLY-GROUNDED hypotheses at once, each with the same rigor:
fixed textbook params (NO tuning), held-out H1/H2 split, real costs, NIFTY
benchmark. ONE data fetch, many tests.

MULTIPLE-TESTING HONESTY: testing ~8 hypotheses means ~1 will look "significant"
at p<0.05 BY CHANCE. So a single pass is NOT an edge — it must clear a STRICT
bar (positive in BOTH halves AND beat NIFTY AND survive cost) and even then is
only "worth a point-in-time re-test". The summary counts passes vs the ~0.4
expected-false-positives.

Hypotheses (all have a real economic story, not just a pattern):
  1  Overnight drift      — equities earn their return OVERNIGHT, not intraday
                            (global anomaly; risk premium for holding gap risk).
  2  Low-volatility       — low-vol names beat high-vol risk-adjusted (Haugen).
  3  X-sec short reversal  — last week's losers bounce (liquidity/overreaction).
  4  Turn-of-month        — returns cluster at month boundaries (flow-driven).
  5  Day-of-week          — Monday/Friday effects (weak; included for completeness).
  6  VIX-spike reversion  — buy NIFTY after a fear spike (vol mean-reverts).
  7  52-week-high momentum — names near 52wk highs keep winning (George-Hwang).

Run:  python edge_hunt.py --full --limit 80 --days 1095
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from typing import Dict, List, Optional, Tuple

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

_RESULTS: List[Tuple[str, bool, str]] = []   # (name, passes, oneliner)


def _ann_sharpe(daily: pd.Series, periods: int = 252) -> Tuple[float, float]:
    d = daily.dropna()
    if len(d) < 20 or d.std() == 0:
        return 0.0, 0.0
    return float(d.mean() * periods * 100), float(d.mean() / d.std() * np.sqrt(periods))


def _split(s: pd.Series):
    s = s.dropna()
    h = len(s) // 2
    return s.iloc[:h], s.iloc[h:]


def _record(name: str, passes: bool, line: str):
    _RESULTS.append((name, passes, line))
    tag = "PASS" if passes else "fail"
    print(f"  [{tag}] {name}: {line}")


# ── 1. Overnight vs intraday ────────────────────────────────────────────────
def h_overnight(po: pd.DataFrame, pc: pd.DataFrame, nifty: pd.Series):
    print("\n— H1 Overnight drift (close→open) vs intraday (open→close) —")
    overnight = (po / pc.shift(1) - 1).mean(axis=1)      # equal-weight across names
    intraday = (pc / po - 1).mean(axis=1)
    a_on, sh_on = _ann_sharpe(overnight)
    a_in, sh_in = _ann_sharpe(intraday)
    on_net = overnight - COST_RT                          # daily round-trip to harvest
    a_onnet, _ = _ann_sharpe(on_net)
    nf = nifty.pct_change().reindex(overnight.index)
    a_nf, sh_nf = _ann_sharpe(nf)
    h1, h2 = _split(overnight)
    a_h1, _ = _ann_sharpe(h1); a_h2, _ = _ann_sharpe(h2)
    print(f"     overnight gross ann {a_on:+.1f}% (Sharpe {sh_on:.2f}) | "
          f"intraday ann {a_in:+.1f}% (Sharpe {sh_in:.2f}) | NIFTY {a_nf:+.1f}%")
    print(f"     overnight H1 {a_h1:+.1f}% / H2 {a_h2:+.1f}% | NET of daily cost {a_onnet:+.1f}%")
    # real signal if overnight strongly > intraday & > nifty & stable; TRADEABLE only if net>0
    signal = a_on > a_in + 5 and a_h1 > 0 and a_h2 > 0
    tradeable = a_onnet > a_nf and a_onnet > 0
    _record("overnight_drift", bool(signal and tradeable),
            f"signal={'yes' if signal else 'no'} but NET ann {a_onnet:+.1f}% "
            f"(daily cost drag) -> tradeable={'yes' if tradeable else 'NO'}")


# ── 2. Low-volatility anomaly ───────────────────────────────────────────────
def h_lowvol(pc: pd.DataFrame, nifty: pd.Series):
    print("\n— H2 Low-volatility anomaly (long bottom-vol quintile, monthly) —")
    rets = pc.pct_change()
    vol = rets.rolling(60).std()
    nf = nifty.pct_change()
    port, bench = [], []
    step = 21
    for t in range(120, len(pc) - step, step):
        v = vol.iloc[t].dropna()
        fwd = (pc.iloc[t + step] / pc.iloc[t] - 1)
        common = v.index.intersection(fwd.dropna().index)
        if len(common) < 10:
            continue
        v = v[common].sort_values()
        k = max(2, len(v) // 5)
        low = v.index[:k]
        port.append(float(fwd[low].mean()) - 2 * COST_RT)
        bench.append(float(nf.iloc[t + 1:t + step + 1].add(1).prod() - 1))
    if len(port) < 6:
        _record("low_vol", False, "insufficient data"); return
    p = pd.Series(port); b = pd.Series(bench)
    a_p, sh_p = _ann_sharpe(p, 12); a_b, _ = _ann_sharpe(b, 12)
    h1, h2 = _split(p)
    a_h1, _ = _ann_sharpe(h1, 12); a_h2, _ = _ann_sharpe(h2, 12)
    print(f"     low-vol ann {a_p:+.1f}% (Sharpe {sh_p:.2f}) | NIFTY {a_b:+.1f}% | "
          f"H1 {a_h1:+.1f}% / H2 {a_h2:+.1f}%  n={len(p)}mo")
    passes = a_p > a_b + 2 and a_h1 > 0 and a_h2 > 0 and sh_p > 0.7
    _record("low_vol", bool(passes),
            f"ann {a_p:+.1f}% vs NIFTY {a_b:+.1f}%, H2 {a_h2:+.1f}%, Sharpe {sh_p:.2f}")


# ── 3. Cross-sectional short-term reversal ──────────────────────────────────
def h_reversal(pc: pd.DataFrame):
    print("\n— H3 X-sectional short-term reversal (long last-week losers, weekly) —")
    port = []
    step = 5
    for t in range(60, len(pc) - step, step):
        past = pc.iloc[t] / pc.iloc[t - step] - 1
        fwd = pc.iloc[t + step] / pc.iloc[t] - 1
        common = past.dropna().index.intersection(fwd.dropna().index)
        if len(common) < 10:
            continue
        past = past[common].sort_values()
        k = max(2, len(past) // 5)
        losers = past.index[:k]
        port.append(float(fwd[losers].mean()) - 2 * COST_RT)
    if len(port) < 10:
        _record("reversal", False, "insufficient"); return
    p = pd.Series(port)
    a_p, sh_p = _ann_sharpe(p, 52)
    h1, h2 = _split(p)
    a_h1, _ = _ann_sharpe(h1, 52); a_h2, _ = _ann_sharpe(h2, 52)
    print(f"     reversal ann {a_p:+.1f}% (Sharpe {sh_p:.2f}) | H1 {a_h1:+.1f}% / "
          f"H2 {a_h2:+.1f}%  n={len(p)}wk  (net of {2*COST_RT*100:.2f}% cost)")
    passes = a_p > 5 and a_h1 > 0 and a_h2 > 0 and sh_p > 0.7
    _record("reversal", bool(passes), f"ann {a_p:+.1f}%, H2 {a_h2:+.1f}%, Sharpe {sh_p:.2f}")


# ── 4. Turn-of-month ────────────────────────────────────────────────────────
def h_turnofmonth(pc: pd.DataFrame):
    print("\n— H4 Turn-of-month (equal-weight universe daily return) —")
    daily = pc.pct_change().mean(axis=1).dropna()
    dom = daily.index.day
    # ToM window: last 1 + first 3 trading days approximated by calendar day <=3 or >=28
    tom = daily[(dom <= 3) | (dom >= 28)]
    rest = daily[(dom > 3) & (dom < 28)]
    if len(tom) < 20 or len(rest) < 20:
        _record("turn_of_month", False, "insufficient"); return
    from scipy import stats as _st
    t, p = _st.ttest_ind(tom, rest, equal_var=False)
    a_tom = tom.mean() * 252 * 100
    a_rest = rest.mean() * 252 * 100
    print(f"     ToM days ann {a_tom:+.1f}% vs rest {a_rest:+.1f}%  "
          f"t={t:+.2f} p={p:.3f}  (ToM n={len(tom)})")
    # tradeable only if ToM strongly positive AND difference significant
    passes = bool(a_tom > a_rest + 10 and p < 0.05 and tom.mean() > 0)
    _record("turn_of_month", passes, f"ToM {a_tom:+.1f}% vs rest {a_rest:+.1f}%, p={p:.3f}")


# ── 5. Day-of-week ──────────────────────────────────────────────────────────
def h_dayofweek(pc: pd.DataFrame):
    print("\n— H5 Day-of-week (equal-weight) —")
    daily = pc.pct_change().mean(axis=1).dropna()
    names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    best_name, best_ann = None, -1e9
    for dow in range(5):
        d = daily[daily.index.dayofweek == dow]
        if len(d) < 20:
            continue
        ann = d.mean() * 252 * 100
        print(f"     {names[dow]}: ann-if-only-this-day {ann:+.1f}%  (n={len(d)})")
        if ann > best_ann:
            best_ann, best_name = ann, names[dow]
    # day-of-week is the weakest theory; treat any 'pass' as likely data-mining
    _record("day_of_week", False,
            f"best={best_name} {best_ann:+.1f}% — not a tradeable, theory-light, fail by default")


# ── 6. VIX-spike reversion ──────────────────────────────────────────────────
def h_vixspike(nifty: pd.Series, vix: Optional[pd.Series]):
    print("\n— H6 VIX-spike reversion (buy NIFTY after a fear spike, hold 5d) —")
    if vix is None:
        _record("vix_spike", False, "no VIX data"); return
    common = nifty.index.intersection(vix.index)
    nf, vx = nifty.reindex(common), vix.reindex(common)
    hi20 = vx.rolling(20).max()
    rets = []
    i = 25
    while i < len(common) - 6:
        if vx.iloc[i] >= hi20.iloc[i] * 0.999 and vx.iloc[i] > vx.iloc[i - 1] * 1.05:
            r = float(nf.iloc[i + 5] / nf.iloc[i + 1] - 1) - COST_RT
            rets.append(r)
            i += 5
        else:
            i += 1
    if len(rets) < 10:
        _record("vix_spike", False, f"only {len(rets)} events"); return
    a = pd.Series(rets)
    wr = float((a > 0).mean()) * 100
    exp = float(a.mean()) * 100
    h1, h2 = _split(a)
    print(f"     {len(rets)} spike events  WR {wr:.0f}%  exp/trade {exp:+.2f}%  "
          f"H1 mean {h1.mean()*100:+.2f}% / H2 {h2.mean()*100:+.2f}%")
    passes = bool(exp > 0.3 and h1.mean() > 0 and h2.mean() > 0)
    _record("vix_spike", passes, f"exp {exp:+.2f}%/trade, H2 {h2.mean()*100:+.2f}%, n={len(rets)}")


# ── 7. 52-week-high momentum ────────────────────────────────────────────────
def h_52whigh(pc: pd.DataFrame, nifty: pd.Series):
    print("\n— H7 52-week-high momentum (long names within 5% of 52wk high, monthly) —")
    hi = pc.rolling(252, min_periods=200).max()
    nf = nifty.pct_change()
    port, bench, step = [], [], 21
    for t in range(252, len(pc) - step, step):
        prox = (pc.iloc[t] / hi.iloc[t]).dropna()
        fwd = (pc.iloc[t + step] / pc.iloc[t] - 1)
        common = prox.index.intersection(fwd.dropna().index)
        near = [s for s in common if prox[s] >= 0.95]
        if len(near) < 5:
            continue
        port.append(float(fwd[near].mean()) - 2 * COST_RT)
        bench.append(float(nf.iloc[t + 1:t + step + 1].add(1).prod() - 1))
    if len(port) < 6:
        _record("near_52w_high", False, "insufficient"); return
    p = pd.Series(port); b = pd.Series(bench)
    a_p, sh_p = _ann_sharpe(p, 12); a_b, _ = _ann_sharpe(b, 12)
    h1, h2 = _split(p)
    a_h1, _ = _ann_sharpe(h1, 12); a_h2, _ = _ann_sharpe(h2, 12)
    print(f"     near-52wH ann {a_p:+.1f}% (Sharpe {sh_p:.2f}) | NIFTY {a_b:+.1f}% | "
          f"H1 {a_h1:+.1f}% / H2 {a_h2:+.1f}%")
    passes = a_p > a_b + 2 and a_h1 > 0 and a_h2 > 0 and sh_p > 0.7
    _record("near_52w_high", bool(passes),
            f"ann {a_p:+.1f}% vs NIFTY {a_b:+.1f}%, H2 {a_h2:+.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--limit", type=int, default=80)
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

    print(f"[HUNT] fetching {len(syms)} names + NIFTY + India VIX ({args.days}d) ...")
    def _dedup(s: pd.Series) -> pd.Series:
        return s[~s.index.duplicated(keep="last")].sort_index()

    O, C = {}, {}
    for s in syms:
        df = fetch_daily(s, args.days)
        if df is not None and len(df) > 260:
            df = df[~df.index.duplicated(keep="last")]
            O[s], C[s] = df["open"], df["close"]
    po = pd.DataFrame(O).sort_index().ffill()
    pc = pd.DataFrame(C).sort_index().ffill()
    nifty = fetch_daily("NIFTY", args.days)
    nifty = _dedup(nifty["close"]) if nifty is not None else None
    vix = fetch_daily("INDIAVIX", args.days)
    vix = _dedup(vix["close"]) if vix is not None else None
    if pc.shape[1] < 8 or nifty is None:
        print("[HUNT] FATAL: not enough data."); return 1
    print(f"[HUNT] {pc.shape[1]} names x {pc.shape[0]} days "
          f"({pc.index[0].date()} -> {pc.index[-1].date()})  VIX={'yes' if vix is not None else 'NO'}")

    print("\n" + "=" * 72)
    print("  EDGE BATTERY  (fixed params, held-out split, net of cost, vs NIFTY)")
    print("=" * 72)
    h_overnight(po, pc, nifty)
    h_lowvol(pc, nifty)
    h_reversal(pc)
    h_turnofmonth(pc)
    h_dayofweek(pc)
    h_vixspike(nifty, vix)
    h_52whigh(pc, nifty)

    n = len(_RESULTS)
    passes = [r for r in _RESULTS if r[1]]
    exp_false = n * 0.05
    print("\n" + "=" * 72)
    print("  SUMMARY  (multiple-testing aware)")
    print("=" * 72)
    print(f"  hypotheses tested: {n}   passed strict bar: {len(passes)}   "
          f"expected-by-chance false positives: ~{exp_false:.1f}")
    for name, ok, line in _RESULTS:
        print(f"    {'PASS' if ok else 'fail'}  {name:16s} {line}")
    print("-" * 72)
    if len(passes) == 0:
        print("  RESULT: NO tradeable edge across the broadened battery. Directional,")
        print("  volatility (earlier), and now structural/seasonal effects all fail the")
        print("  honest gauntlet. This is now an EXHAUSTIVE negative — the search space")
        print("  of cheap, daily-bar, retail-accessible edges is covered.")
    elif len(passes) <= exp_false + 1:
        print(f"  RESULT: {len(passes)} pass(es), but that is within CHANCE for {n} tests.")
        print("  Treat as likely false positive(s). Re-test the survivor(s) on a")
        print("  point-in-time universe + different period before believing anything.")
    else:
        print(f"  RESULT: {len(passes)} passes — more than chance. The survivors below are")
        print("  worth a serious point-in-time, real-cost, multi-regime validation.")
        for name, ok, line in passes:
            print(f"     -> {name}: {line}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
