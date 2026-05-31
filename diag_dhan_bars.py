"""
Dhan data-quality diagnostic.

The Dhan backtest gave far fewer signals + 0 wins vs the yahoo run on the
SAME code. That points at a DATA difference, not the strategy. This script
inspects Dhan daily + intraday bars for the usual culprits:

  1. OHLC sanity     — high >= max(open,close) >= min(open,close) >= low?
  2. Daily range     — is daily (high-low) a real session range, or compressed
                       (e.g. close-only / last-price highs)? Compare to the
                       range implied by that day's intraday 5m bars.
  3. Split/bonus gaps — overnight gaps > 15% suggest UNADJUSTED data (a split
                       shows as a fake 50%/100% gap).
  4. Volume scale    — are volumes plausible (lakhs–crores), or tiny/zero?

Run:
    python diag_dhan_bars.py            # default RELIANCE
    python diag_dhan_bars.py TCS INFY   # specific symbols
"""

from __future__ import annotations

import sys
import warnings
warnings.filterwarnings("ignore")
import logging
logging.getLogger("core.api_dhan").setLevel(logging.CRITICAL)

import pandas as pd


def diag(symbol: str):
    from core.api_dhan import dhan_daily, dhan_intraday
    print("=" * 60)
    print(f"  {symbol}")
    print("=" * 60)

    d = dhan_daily(symbol, days_back=60)
    if d is None or d.empty:
        print("  daily: NO DATA")
        return
    d = d.copy()
    d.columns = [c.lower() for c in d.columns]
    print(f"  daily bars: {len(d)}   range {d['date'].min()} -> {d['date'].max()}")
    last = d.tail(3)
    print("  last 3 daily bars:")
    for _, r in last.iterrows():
        rng_pct = (r['high'] - r['low']) / r['close'] * 100 if r['close'] else 0
        print(f"    {str(r['date'])[:10]}  O={r['open']:.1f} H={r['high']:.1f} "
              f"L={r['low']:.1f} C={r['close']:.1f}  range={rng_pct:.2f}%  vol={int(r['volume']):,}")

    # 1. OHLC sanity
    bad = d[(d['high'] < d[['open', 'close']].max(axis=1)) |
            (d['low'] > d[['open', 'close']].min(axis=1))]
    print(f"  [OHLC sanity] {'OK' if bad.empty else f'{len(bad)} BAD bars (high<body or low>body)'}")

    # 2. Daily range vs intraday-implied range (today/last session)
    avg_daily_rng = ((d['high'] - d['low']) / d['close'] * 100).mean()
    print(f"  [daily range] avg {avg_daily_rng:.2f}% of close "
          f"({'looks real' if avg_daily_rng > 0.8 else 'SUSPICIOUSLY TIGHT — maybe close-only highs'})")
    try:
        intr = dhan_intraday(symbol, interval_min=5, days_back=2)
        if intr is not None and not intr.empty:
            intr = intr.copy(); intr.columns = [c.lower() for c in intr.columns]
            intr['day'] = pd.to_datetime(intr['date']).dt.date
            lastday = intr['day'].max()
            sub = intr[intr['day'] == lastday]
            if not sub.empty:
                ihi, ilo = sub['high'].max(), sub['low'].min()
                irng = (ihi - ilo) / sub['close'].iloc[-1] * 100
                # match same day in daily
                drow = d[pd.to_datetime(d['date']).dt.date == lastday]
                if not drow.empty:
                    dr = drow.iloc[-1]
                    drng = (dr['high'] - dr['low']) / dr['close'] * 100
                    print(f"  [range cross-check {lastday}] daily={drng:.2f}%  intraday-implied={irng:.2f}%  "
                          f"{'MATCH' if abs(drng - irng) < 0.5 else 'MISMATCH — daily high/low not the true session range!'}")
    except Exception as e:
        print(f"  [range cross-check] intraday compare failed: {e}")

    # 3. Split/bonus gaps
    d = d.sort_values('date')
    d['prev_close'] = d['close'].shift(1)
    d['gap_pct'] = (d['open'] - d['prev_close']).abs() / d['prev_close'] * 100
    big = d[d['gap_pct'] > 15]
    if big.empty:
        print("  [split/bonus] no >15% overnight gaps — data looks adjusted")
    else:
        print(f"  [split/bonus] {len(big)} gap(s) >15% — likely UNADJUSTED (split/bonus shows as fake gap):")
        for _, r in big.iterrows():
            print(f"    {str(r['date'])[:10]}  gap {r['gap_pct']:.0f}%  prev_close={r['prev_close']:.1f} open={r['open']:.1f}")

    # 4. Volume
    vmin, vmax, vmean = int(d['volume'].min()), int(d['volume'].max()), int(d['volume'].mean())
    zero = int((d['volume'] <= 0).sum())
    print(f"  [volume] min={vmin:,} mean={vmean:,} max={vmax:,}  zero-vol bars={zero} "
          f"{'(zero-vol bars will break the volume gate)' if zero else ''}")


def main():
    syms = [s.upper() for s in sys.argv[1:]] or ["RELIANCE", "SBIN", "INFY"]
    for s in syms:
        diag(s)
        print()
    print("Read the flags above. MISMATCH on range or >15% gaps = the data")
    print("issue behind the 0-win backtest. Paste this output back.")


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
