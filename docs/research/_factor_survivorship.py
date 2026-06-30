"""
Factor sleeve survivorship-deflation test (path #1, item #2).
Runs momentum (6-1) and low-vol (60d) top-30 on the bhavcopy archive twice:
survivorship-complete (full universe incl. delisted names) vs survivors-only
(restricted to names still in logs/bar_cache today). The gap = survivorship
inflation. Run on the FULL 2018-present archive for the clean multi-regime verdict.

    python docs/research/_factor_survivorship.py
"""
import glob, os, numpy as np, pandas as pd

RF = 0.065
COST_MONTHLY_RT = 0.0025  # ~0.25% delivery round-trip


def load_panel(archive='logs/bhavcopy_archive/daily'):
    frames = [pd.read_parquet(f)[['symbol', 'date', 'close', 'volume']]
              for f in sorted(glob.glob(f'{archive}/*.parquet'))]
    long = pd.concat(frames, ignore_index=True)
    long['date'] = pd.to_datetime(long['date'])
    close = long.pivot_table(index='date', columns='symbol', values='close').sort_index()
    vol = long.pivot_table(index='date', columns='symbol', values='volume').sort_index()
    return close, close * vol


def run(close, turn, survivors, universe='complete'):
    dret = close.pct_change(fill_method=None)
    mom = close.pct_change(126, fill_method=None)
    lv = dret.rolling(60).std()
    avgturn = turn.rolling(60).mean()
    mret = close.resample('ME').last().pct_change(fill_method=None)
    mdates = mret.index
    w_mom = pd.DataFrame(0.0, index=mdates, columns=close.columns)
    w_lv = pd.DataFrame(0.0, index=mdates, columns=close.columns)
    for d in mdates:
        at = avgturn.loc[:d]
        if at.empty:
            continue
        liq = at.iloc[-1].dropna()
        if universe == 'survivors':
            liq = liq[[c for c in liq.index if c in survivors]]
        uni = liq.sort_values(ascending=False).head(120).index
        ms = mom.loc[:d].iloc[-1].reindex(uni).dropna()
        vs = lv.loc[:d].iloc[-1].reindex(uni).dropna()
        if len(ms) >= 30:
            w_mom.loc[d, ms.sort_values(ascending=False).head(30).index] = 1/30
        if len(vs) >= 30:
            w_lv.loc[d, vs.sort_values(ascending=True).head(30).index] = 1/30
    return _stat(w_mom, mret), _stat(w_lv, mret)


def _stat(w, mret):
    port = (w.shift(1) * mret).sum(1).dropna()
    if len(port) < 3:
        return None
    drag = w.diff().abs().sum(1).mean() * COST_MONTHLY_RT * 12
    yrs = (port.index[-1] - port.index[0]).days / 365.25
    cagr = (1 + port).prod() ** (1/yrs) - 1 - drag
    vol = port.std() * np.sqrt(12)
    eq = (1 + port).cumprod()
    return dict(cagr=cagr, sharpe=(cagr-RF)/vol if vol else 0,
                max_dd=(eq/eq.cummax()-1).min())


if __name__ == "__main__":
    survivors = {os.path.splitext(os.path.basename(f))[0]
                 for f in glob.glob('logs/bar_cache/*.parquet')}
    close, turn = load_panel()
    print(f"panel {close.shape[0]}d x {close.shape[1]} symbols, "
          f"{close.index.min().date()}..{close.index.max().date()}")
    for uni in ('complete', 'survivors'):
        m, l = run(close, turn, survivors, uni)
        print(f"[{uni}]")
        for nm, s in [('Momentum', m), ('Low-vol', l)]:
            if s:
                print(f"  {nm:9} CAGR={s['cagr']*100:7.2f}%  Sharpe={s['sharpe']:5.2f}  "
                      f"maxDD={s['max_dd']*100:6.1f}%")
