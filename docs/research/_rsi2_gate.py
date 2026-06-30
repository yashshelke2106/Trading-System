"""
RSI-2 mean-reversion final gate (main-thread, after subagent drops).
Signal: RSI(2)<10 & close>SMA200; entry next-day open; exit first close>SMA5
within 10 bars else 10th-bar close. Universe: logs/bar_cache (153 names).
Decision cost: 0.10% round-trip (liquid-name futures). Independence unit = entry DATE.
"""
import glob, os, numpy as np, pandas as pd

COST = 0.0010  # 0.10% round-trip (decision cost)
RNG = np.random.default_rng(7)

def rsi(close, n=2):
    d = close.diff()
    up = d.clip(lower=0.0); dn = (-d).clip(lower=0.0)
    ru = up.ewm(alpha=1/n, adjust=False).mean()
    rd = dn.ewm(alpha=1/n, adjust=False).mean()
    rs = ru / rd.replace(0, np.nan)
    return (100 - 100/(1+rs)).fillna(100)

def trades_for(df):
    df = df.sort_index()
    c = df['close']; o = df['open']
    r2 = rsi(c, 2); s200 = c.rolling(200).mean(); s5 = c.rolling(5).mean()
    sig = (r2 < 10) & (c > s200)
    co = c.values; op = o.values; s5v = s5.values; sg = sig.values; dates = df.index.values
    turnover = float((c*df['volume']).tail(252).mean())  # recent avg daily turnover
    n = len(df); out = []
    i = 200
    while i < n-1:
        if sg[i]:
            entry = op[i+1]; e_date = dates[i+1]; exit_px = None
            kmax = min(i+1+10, n)
            for k in range(i+1, kmax):
                if not np.isnan(s5v[k]) and co[k] > s5v[k]:
                    exit_px = co[k]; ei = k; break
            if exit_px is None:
                ei = min(i+10, n-1); exit_px = co[ei]
            if entry and entry > 0:
                ret = exit_px/entry - 1.0
                out.append((e_date, ret))
            i = ei + 1
        else:
            i += 1
    return out, turnover

# ---- build trades across the universe ----
rows = []; turn = {}
for f in glob.glob('logs/bar_cache/*.parquet'):
    sym = os.path.splitext(os.path.basename(f))[0]
    if sym.upper() == 'NIFTY': continue
    df = pd.read_parquet(f)
    if 'close' not in df.columns or len(df) < 250: continue
    tr, tv = trades_for(df); turn[sym] = tv
    for d, r in tr: rows.append({'date': pd.Timestamp(d).normalize(), 'symbol': sym, 'gross': r})

T = pd.DataFrame(rows)
T['net'] = T['gross'] - COST

def stats(df, label):
    if len(df) == 0: return None
    per_date = df.groupby('date')['net'].mean()
    D = len(per_date); obs = per_date.mean()
    t = obs / (per_date.std(ddof=1)/np.sqrt(D)) if D > 1 else np.nan
    pf = df.loc[df.net>0,'net'].sum() / abs(df.loc[df.net<0,'net'].sum()) if (df.net<0).any() else np.inf
    # sign-flip permutation on per-date means
    pdv = per_date.values; nperm = 10000
    perm = (RNG.choice([-1,1], size=(nperm, D)) * pdv).mean(axis=1)
    p = (np.sum(perm >= obs) + 1) / (nperm + 1)
    return dict(label=label, trades=len(df), dates=D, exp=obs, t=t, pf=pf, perm_p=p)

def show(s):
    if s is None: print("  (no trades)"); return
    print(f"  {s['label']:26} trades={s['trades']:5d} dates={s['dates']:4d} "
          f"exp/trade={s['exp']*100:+.3f}% PF={s['pf']:.3f} t={s['t']:.2f} perm_p={s['perm_p']:.4f}")

print(f"=== RSI-2 GATE @ {COST*100:.2f}% round-trip cost ===")
full = stats(T, "FULL universe")
show(full)

# both-halves
mid = T['date'].sort_values().iloc[len(T)//2]
h1 = stats(T[T.date <= mid], "H1 (first half)")
h2 = stats(T[T.date >  mid], "H2 (second half)")
print("BOTH-HALVES (both must be net-positive & ideally significant):"); show(h1); show(h2)

# robustness: drop largest-contribution date and symbol
date_contrib = T.groupby('date')['net'].sum(); worst_date = date_contrib.idxmax()
sym_contrib = T.groupby('symbol')['net'].sum(); worst_sym = sym_contrib.idxmax()
show(stats(T[T.date != worst_date], f"drop top date {pd.Timestamp(worst_date).date()}"))
show(stats(T[T.symbol != worst_sym], f"drop top symbol {worst_sym}"))

# top-50 most liquid (where 0.10% is realistic)
top50 = set(pd.Series(turn).sort_values(ascending=False).head(50).index)
show(stats(T[T.symbol.isin(top50)], "top-50 liquid only"))

# context: other cost levels on full
for c in (0.0006, 0.0025):
    Tc = T.copy(); Tc['net'] = Tc['gross'] - c
    s = stats(Tc, f"FULL @ {c*100:.2f}%"); show(s)
