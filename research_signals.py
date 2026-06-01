"""
Signal research harness — find an edge empirically, don't guess one.

india_swing (trend pullback continuation, long) tested at PF 0.72 / 23% WR on
146 trades across 152 stocks / 2yr — no edge, no factor discriminates. Tuning
a dead signal = curve-fitting. This harness instead tests several MECHANICALLY
DISTINCT candidate edges head-to-head on the full Dhan universe and reports
which (if any) actually works.

Built-in overfit protection: every signal is scored separately on an
IN-SAMPLE period (first 70% of dates) and an OUT-OF-SAMPLE period (last 30%).
A real edge holds OOS. A curve-fit shows IS>>OOS (or OOS PF<1). We only trust
signals that work OOS — that is exactly the check that would have caught the
RSI/pin-bar overfit.

Candidate signals (each a SIMPLE pure function of OHLCV — simple = hard to
overfit):
  MR2_long   Connors RSI(2) mean-reversion: RSI2<10 AND close>EMA200 -> buy,
             exit RSI2>60 or 5 bars. (The classic retail mean-reversion edge,
             OPPOSITE of trend-chasing.)
  MR2_tight  Same but RSI2<5 (deeper oversold).
  BB_revert  Close below lower Bollinger(20,2) AND close>EMA200 -> buy, exit
             at mid-band or 5 bars.
  Donchian   20-day high breakout -> buy, 10-day-low trailing stop (trend
             following — the honest control; if even this is flat, direction
             prediction is dead here).
  Gap_fade   Open gaps down >2% vs prev close, in uptrend -> buy, exit next
             close. (Overreaction fade.)
  RSI2_short Mirror short: RSI2>90 AND close<EMA200 -> short.

Exit model: each signal defines its own exit; all use a hard ATR stop and a
max-hold cap. Costs: FUT_COST_ROUNDTRIP_PCT (default 0.06%) per trade.

Run (real Dhan data, your machine):
    python research_signals.py             # 30-name quick look
    python research_signals.py --full      # full ~150 F&O universe
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
ATR_STOP_MULT = 2.0
MAX_HOLD = 10
COST_PCT = 0.06           # round-trip % (matches config.FUT_COST_ROUNDTRIP_PCT)
OOS_FRACTION = 0.30       # last 30% of the period is out-of-sample

DEFAULT_UNIVERSE = [
    "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","SBIN","BHARTIARTL","KOTAKBANK",
    "BAJFINANCE","HINDUNILVR","ITC","LT","AXISBANK","MARUTI","ASIANPAINT","WIPRO",
    "HCLTECH","TECHM","SUNPHARMA","DRREDDY","CIPLA","DIVISLAB","TATAMOTORS","M&M",
    "BAJAJ-AUTO","ULTRACEMCO","POWERGRID","NTPC","ONGC","NESTLEIND",
]


# ── indicators ─────────────────────────────────────────────────────────
def rsi(series: pd.Series, n: int) -> pd.Series:
    d = series.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    df = df.sort_index()
    df["rsi2"] = rsi(df["close"], 2)
    df["rsi14"] = rsi(df["close"], 14)
    df["ema200"] = ema(df["close"], 200)
    df["atr"] = atr(df)
    df["donch_hi20"] = df["high"].rolling(20).max()
    df["donch_lo10"] = df["low"].rolling(10).min()
    m = df["close"].rolling(20).mean(); sd = df["close"].rolling(20).std()
    df["bb_lo"] = m - 2 * sd; df["bb_mid"] = m
    df["prev_close"] = df["close"].shift(1)
    return df


# ── signal entry rules: return (enter_bool, direction, exit_fn_name) ─────
def signals_for_bar(df, i):
    """Yield (name, direction) for every candidate firing at bar i (entry next open)."""
    r = df.iloc[i]
    out = []
    if i < 200 or any(pd.isna(r[k]) for k in ("rsi2","ema200","atr","bb_lo")):
        return out
    up = r["close"] > r["ema200"]
    dn = r["close"] < r["ema200"]
    # mean reversion longs
    if up and r["rsi2"] < 10: out.append(("MR2_long","long"))
    if up and r["rsi2"] < 5:  out.append(("MR2_tight","long"))
    if up and r["close"] < r["bb_lo"]: out.append(("BB_revert","long"))
    # trend following (control)
    if r["close"] >= r["donch_hi20"]: out.append(("Donchian","long"))
    # mirror short (gap-fade handled separately in main loop)
    if dn and r["rsi2"] > 90: out.append(("RSI2_short","short"))
    return out


def gap_fade_entry(df, i):
    r = df.iloc[i]
    if i < 200 or pd.isna(r["ema200"]): return False
    prev = df["close"].iloc[i-1]
    return (r["close"] > r["ema200"]) and prev > 0 and (df["open"].iloc[i] - prev)/prev <= -0.02


# ── exit rules per signal ───────────────────────────────────────────────
def exit_reached(name, df, j, entry_px, direction, bars_held):
    """True when this signal's profit-target exit condition is met at bar j."""
    r = df.iloc[j]
    if name in ("MR2_long","MR2_tight"):
        return r["rsi2"] > 60
    if name == "BB_revert":
        return r["close"] >= r["bb_mid"]
    if name == "Donchian":
        return r["close"] <= r["donch_lo10"]      # trailing-stop style exit
    if name == "Gap_fade":
        return bars_held >= 1                      # exit next close
    if name == "RSI2_short":
        return r["rsi2"] < 40
    return False


# ── simulate one trade from entry at bar i (fill next open) ─────────────
def simulate(name, df, i, direction):
    n = len(df)
    if i + 1 >= n: return None
    fill = float(df["open"].iloc[i+1])
    a = float(df["atr"].iloc[i])
    if not (fill > 0 and a > 0): return None
    sl = fill - ATR_STOP_MULT*a if direction=="long" else fill + ATR_STOP_MULT*a
    for k in range(1, MAX_HOLD+1):
        j = i + 1 + k
        if j >= n:
            j = n-1
            px = float(df["close"].iloc[j]); break
        hi, lo, cl = (float(df["high"].iloc[j]), float(df["low"].iloc[j]), float(df["close"].iloc[j]))
        if direction=="long" and lo <= sl: px = sl; break
        if direction=="short" and hi >= sl: px = sl; break
        if exit_reached(name, df, j, fill, direction, k): px = cl; break
    else:
        px = float(df["close"].iloc[min(i+1+MAX_HOLD, n-1)])
    ret = (px-fill)/fill*100 if direction=="long" else (fill-px)/fill*100
    return ret - COST_PCT, df.index[i+1]


def backtest_symbol(name_filter, df):
    trades = []
    n = len(df)
    cool = -999
    for i in range(200, n-1):
        if i < cool: continue
        fired = signals_for_bar(df, i)
        if gap_fade_entry(df, i): fired.append(("Gap_fade","long"))
        for nm, d in fired:
            if name_filter and nm != name_filter: continue
            res = simulate(nm, df, i, d)
            if res:
                ret, dt = res
                trades.append({"signal":nm, "date":dt, "ret":ret})
        if fired: cool = i + 1   # one position at a time per symbol
    return trades


def stats(trades):
    if not trades: return None
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]; losses = [r for r in rets if r <= 0]
    pf = sum(wins)/abs(sum(losses)) if losses and sum(losses)!=0 else float("inf")
    return {
        "n": len(rets), "wr": len(wins)/len(rets)*100,
        "exp": float(np.mean(rets)), "pf": pf,
    }


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

    from core.api_dhan import dhan_daily
    print(f"[RESEARCH] fetching {len(universe)} symbols ({DAYS}d) from Dhan ...")
    data = {}
    for s in universe:
        d = dhan_daily(s, days_back=DAYS)
        if d is not None and not d.empty and len(d) >= 220:
            data[s] = prep(d)
    print(f"[RESEARCH] {len(data)} symbols usable\n")
    if not data:
        print("No data. Check Dhan."); return

    # global OOS split date
    all_dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    split = all_dates[int(len(all_dates)*(1-OOS_FRACTION))]
    print(f"OOS split date: {str(split)[:10]}  (IS before, OOS after)\n")

    SIGNALS = ["MR2_long","MR2_tight","BB_revert","Donchian","Gap_fade","RSI2_short"]
    print(f"{'signal':12} | {'IS  n':>6} {'WR':>4} {'PF':>5} {'expR%':>6} | "
          f"{'OOS n':>6} {'WR':>4} {'PF':>5} {'expR%':>6} | verdict")
    print("-"*92)
    for sig in SIGNALS:
        all_tr = []
        for s, df in data.items():
            all_tr.extend(backtest_symbol(sig, df))
        is_tr = [t for t in all_tr if t["date"] < split]
        oos_tr = [t for t in all_tr if t["date"] >= split]
        si, so = stats(is_tr), stats(oos_tr)
        if not si and not so:
            print(f"{sig:12} | (no trades)"); continue
        def fmt(x):
            return f"{x['n']:>6} {x['wr']:>3.0f}% {x['pf']:>5.2f} {x['exp']:>+5.2f}" if x else f"{'-':>6} {'-':>4} {'-':>5} {'-':>6}"
        # verdict: real edge = OOS PF>1.1 AND OOS n>=20
        verdict = "no data"
        if so:
            if so["pf"] >= 1.2 and so["n"] >= 20: verdict = ">>> EDGE (holds OOS)"
            elif so["pf"] >= 1.0 and so["n"] >= 20: verdict = "marginal"
            else: verdict = "no edge"
            if si and si["pf"] >= 1.3 and so["pf"] < 1.0: verdict = "OVERFIT (IS only)"
        print(f"{sig:12} | {fmt(si)} | {fmt(so)} | {verdict}")

    print("\nTrust ONLY signals marked EDGE (OOS PF>=1.2, n>=20). 'OVERFIT' = looked")
    print("good in-sample, failed out-of-sample — exactly the trap to avoid.")


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
