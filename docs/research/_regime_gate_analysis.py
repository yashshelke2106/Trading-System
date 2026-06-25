"""Regime-gate hypothesis validation. Run for real, no eyeballing."""
import pandas as pd, numpy as np
from scipy import stats

RNG = np.random.default_rng(20260625)

# ---- Load trades ----
T = pd.read_csv(r'C:\Users\yashs\trading_system\backtest_india_swing_trades.csv',
                parse_dates=['entry_date'])
T = T.sort_values('entry_date').reset_index(drop=True)

# ---- Load NIFTY, compute regimes ----
N = pd.read_csv(r'/tmp/nifty_close.csv', parse_dates=['date']).sort_values('date').reset_index(drop=True)
N['sma50']  = N.close.rolling(50).mean()
N['sma200'] = N.close.rolling(200).mean()
N['mom20']  = N.close / N.close.shift(20) - 1.0
N['EXT_UP']   = (N.close > N.sma50) & (N.sma50 > N.sma200)        # primary def
N['ABOVE200'] = (N.close > N.sma200)                              # variant
N['FULLSTACK']= (N.close > N.sma50) & (N.sma50 > N.sma200) & (N.sma200 > N.sma200.shift(20))  # rising 200
N['MOM_UP']   = (N.mom20 > 0.0)                                   # 20d momentum positive

def pit_label(entry_date, col):
    """Strictly point-in-time: last NIFTY bar with date <= entry_date."""
    sub = N[N.date <= entry_date]
    if len(sub) == 0:
        return np.nan
    val = sub.iloc[-1][col]
    return bool(val) if pd.notnull(val) else np.nan

for col in ['EXT_UP','ABOVE200','FULLSTACK','MOM_UP']:
    T[col] = T.entry_date.apply(lambda d: pit_label(d, col))

# verify no lookahead: the bar used must be <= entry_date
def used_bar_date(entry_date):
    sub = N[N.date <= entry_date]
    return sub.iloc[-1].date if len(sub) else pd.NaT
T['nifty_bar'] = T.entry_date.apply(used_bar_date)
assert (T['nifty_bar'] <= T['entry_date']).all(), "LOOKAHEAD DETECTED"
maxgap = (T['entry_date'] - T['nifty_bar']).dt.days.max()
print(f"[PIT CHECK] all nifty bars <= entry_date: OK. max gap days={maxgap}")

def pf(r):
    r = np.asarray(r, float)
    g = r[r > 0].sum()
    l = -r[r < 0].sum()
    if l == 0:
        return np.inf if g > 0 else np.nan
    return g / l

def describe(mask, label):
    r = T.loc[mask, 'R'].values
    print(f"  {label:14s} n={len(r):3d}  meanR={r.mean():+.4f}  sumR={r.sum():+.2f}  "
          f"PF={pf(r):.3f}  win%={(r>0).mean()*100:.1f}")
    return r

print("\n=== PARTITION (primary EXT_UP) ===")
ext  = T['EXT_UP'] == True
nott = T['EXT_UP'] == False
print(f"  n EXT_UP={ext.sum()}  n NOT={nott.sum()}  (nan={T['EXT_UP'].isna().sum()})")
r_ext = describe(ext, 'EXTENDED_UP')
r_not = describe(nott, 'NOT')
pf_gap = pf(r_not) - pf(r_ext)
mean_gap = r_not.mean() - r_ext.mean()
print(f"  PF gap (NOT - EXT) = {pf_gap:+.3f}")
print(f"  meanR gap (NOT - EXT) = {mean_gap:+.4f}")

# ---- TEST 1: Mann-Whitney U + Welch t (naive, trades-as-independent) ----
print("\n=== TEST 1: naive per-trade tests (NOTE: trades overlap -> p invalid, context only) ===")
u, pu = stats.mannwhitneyu(r_not, r_ext, alternative='greater')   # H1: NOT > EXT
print(f"  Mann-Whitney U={u:.1f}  p(one-sided NOT>EXT)={pu:.4f}")
tt, pt = stats.ttest_ind(r_not, r_ext, equal_var=False)
print(f"  Welch t={tt:.3f}  p(two-sided)={pt:.4f}  p(one-sided)={pt/2 if tt>0 else 1-pt/2:.4f}")

# ---- TEST 2: DATE-CLUSTERED PERMUTATION (decisive) ----
# Shuffle EXT_UP label across DISTINCT entry-dates, keeping each date's regime constant
# across all trades that share it, and preserving the #dates labeled EXT_UP.
print("\n=== TEST 2: DATE-CLUSTERED PERMUTATION (decisive) ===")
valid = T[T['EXT_UP'].notna()].copy()
# date-level regime (a date has one regime since it's one nifty bar)
date_reg = valid.groupby('entry_date')['EXT_UP'].agg(lambda s: s.iloc[0])
dates = date_reg.index.values
labels = date_reg.values.astype(bool)
n_ext_dates = labels.sum()
print(f"  distinct dates={len(dates)}  ext_dates={n_ext_dates}  not_dates={len(dates)-n_ext_dates}")

# map each trade to its date index
valid = valid.reset_index(drop=True)
date_to_i = {d:i for i,d in enumerate(dates)}
trade_date_idx = valid['entry_date'].map(date_to_i).values
R = valid['R'].values

def gap_from_datelabels(dlabels):
    trade_ext = dlabels[trade_date_idx]
    r_e = R[trade_ext]; r_n = R[~trade_ext]
    return pf(r_n) - pf(r_e)

obs_gap = gap_from_datelabels(labels)
NREP = 10000
perm = np.empty(NREP)
for i in range(NREP):
    pl = RNG.permutation(labels)
    perm[i] = gap_from_datelabels(pl)
# one-sided: how often shuffled gap >= observed (we predicted NOT has higher PF -> positive gap)
p_clustered = (np.sum(perm >= obs_gap) + 1) / (NREP + 1)
ci = np.nanpercentile(perm, [2.5, 97.5])
print(f"  observed PF gap (NOT-EXT) = {obs_gap:+.4f}")
print(f"  permutation null mean = {np.nanmean(perm):+.4f}  null 95% CI = [{ci[0]:+.3f}, {ci[1]:+.3f}]")
print(f"  CLUSTERED p (one-sided, gap>=obs) = {p_clustered:.4f}")

# also do clustered perm on MEAN-R gap (PF is unstable/inf-prone)
def meangap_from_datelabels(dlabels):
    trade_ext = dlabels[trade_date_idx]
    return R[~trade_ext].mean() - R[trade_ext].mean()
obs_mg = meangap_from_datelabels(labels)
permm = np.array([meangap_from_datelabels(RNG.permutation(labels)) for _ in range(NREP)])
p_mg = (np.sum(permm >= obs_mg) + 1) / (NREP + 1)
cim = np.nanpercentile(permm, [2.5, 97.5])
print(f"  observed meanR gap (NOT-EXT) = {obs_mg:+.4f}")
print(f"  clustered p meanR gap = {p_mg:.4f}  null95CI=[{cim[0]:+.3f},{cim[1]:+.3f}]")

# ---- TEST 3: both-halves stability ----
print("\n=== TEST 3: BOTH-HALVES STABILITY (split ~midpoint) ===")
mid = T['entry_date'].sort_values().iloc[len(T)//2]
print(f"  split date = {mid.date()}")
for name, half in [('H1 (early)', T['entry_date'] <= mid), ('H2 (late)', T['entry_date'] > mid)]:
    sub = T[half]
    re = sub.loc[sub['EXT_UP']==True, 'R'].values
    rn = sub.loc[sub['EXT_UP']==False, 'R'].values
    g = (pf(rn)-pf(re)) if len(re) and len(rn) else float('nan')
    mg = (rn.mean()-re.mean()) if len(re) and len(rn) else float('nan')
    print(f"  {name:11s} nEXT={len(re):2d} PF={pf(re):.2f} | nNOT={len(rn):2d} PF={pf(rn):.2f} "
          f"| PFgap={g:+.3f} meanRgap={mg:+.4f}")

# ---- TEST 4: robustness (drop largest-R, drop largest-R symbol from NOT) ----
print("\n=== TEST 4: ROBUSTNESS ===")
# 4a drop single largest-R trade overall
imax = T['R'].idxmax()
print(f"  largest-R trade: {T.loc[imax,'symbol']} {T.loc[imax,'entry_date'].date()} R={T.loc[imax,'R']:.2f} "
      f"EXT_UP={T.loc[imax,'EXT_UP']}")
T2 = T.drop(index=imax)
re = T2.loc[T2['EXT_UP']==True,'R'].values; rn = T2.loc[T2['EXT_UP']==False,'R'].values
print(f"  after drop-largest: PF_NOT={pf(rn):.3f} PF_EXT={pf(re):.3f} PFgap={pf(rn)-pf(re):+.3f} "
      f"meanRgap={rn.mean()-re.mean():+.4f}")
# 4b drop the largest-R symbol within NOT bucket
not_df = T[T['EXT_UP']==False]
sym_sumR = not_df.groupby('symbol')['R'].sum().sort_values(ascending=False)
top_sym = sym_sumR.index[0]
print(f"  top symbol in NOT by sumR: {top_sym} (sumR={sym_sumR.iloc[0]:.2f}, "
      f"ntrades={ (not_df['symbol']==top_sym).sum() })")
T3 = T[T['symbol'] != top_sym]
re = T3.loc[T3['EXT_UP']==True,'R'].values; rn = T3.loc[T3['EXT_UP']==False,'R'].values
print(f"  after drop-{top_sym}: PF_NOT={pf(rn):.3f} PF_EXT={pf(re):.3f} PFgap={pf(rn)-pf(re):+.3f} "
      f"meanRgap={rn.mean()-re.mean():+.4f}")
# how concentrated is the NOT edge? top-5 trades' contribution
not_pos = not_df.nlargest(5,'R')[['symbol','entry_date','R']]
print("  top-5 R trades in NOT bucket:")
for _,row in not_pos.iterrows():
    print(f"     {row['symbol']:12s} {row['entry_date'].date()} R={row['R']:+.2f}")
print(f"  NOT bucket sumR={not_df['R'].sum():.2f}; top-5 trades sumR={not_pos['R'].sum():.2f}")

# ---- TEST 5: multiple-testing across regime definitions ----
print("\n=== TEST 5: MULTIPLE-TESTING (best-of-N regime defs) ===")
defs = ['EXT_UP','ABOVE200','FULLSTACK','MOM_UP']
raw_ps = {}
for col in defs:
    v = T[T[col].notna()].copy().reset_index(drop=True)
    dr = v.groupby('entry_date')[col].agg(lambda s: bool(s.iloc[0]))
    dd = dr.index.values; ll = dr.values.astype(bool)
    d2i = {d:i for i,d in enumerate(dd)}
    tdi = v['entry_date'].map(d2i).values
    RR = v['R'].values
    def g2(lab):
        te = lab[tdi]; return pf(RR[~te]) - pf(RR[te])
    og = g2(ll)
    pp = np.array([g2(RNG.permutation(ll)) for _ in range(NREP)])
    pv = (np.sum(pp >= og) + 1)/(NREP+1)
    raw_ps[col] = pv
    re = RR[ll==False]; rex = RR[ll==True]
    print(f"  {col:10s} nNOT={(ll==False).sum()*0+ (~ll[tdi]).sum():3d} PFgap={og:+.3f} clustered_p={pv:.4f}")
# Bonferroni + BH on the 4 definitions
ps = np.array([raw_ps[c] for c in defs])
bonf = np.minimum(ps*len(defs), 1.0)
order = np.argsort(ps)
bh = np.empty_like(ps)
m = len(ps)
prev = 1.0
for rank, idx in enumerate(order[::-1]):  # from largest
    k = m - rank
    val = ps[idx]*m/k
    prev = min(prev, val)
    bh[idx] = min(prev,1.0)
print("  --- corrections ---")
for i,c in enumerate(defs):
    print(f"  {c:10s} raw_p={ps[i]:.4f}  Bonferroni={bonf[i]:.4f}  BH_q={bh[i]:.4f}")

# ---- deflated-Sharpe style note: best-of-4 selection inflation on primary ----
print("\n=== SELECTION NOTE ===")
print(f"  primary EXT_UP raw clustered p={raw_ps['EXT_UP']:.4f}; "
      f"Bonferroni(x4)={min(raw_ps['EXT_UP']*4,1):.4f}")
print("DONE")
