"""
Scan F&O universe for liquidity + volume spikes. No execution, no capital.

Usage:
    python scan_only.py              # single scan, print table
    python scan_only.py --loop 60    # repeat every 60s
    python scan_only.py --top 20     # show top 20 results
"""

import sys
import os
import time
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from core.scanner import LiquidityScanner
from core.api_dhan import check_token_health
from core.universe import FO_UNIVERSE

MARKET_TOTAL_MIN = 375  # 9:15 to 15:30


def minutes_elapsed() -> float:
    now = datetime.now()
    open_dt = now.replace(hour=9, minute=15, second=0, microsecond=0)
    return max((now - open_dt).total_seconds() / 60.0, 1.0)


def get_vol_ratio(scanner: LiquidityScanner, sym: str, avg_cache: dict) -> float:
    """Projected daily volume vs 20d average."""
    avg = avg_cache.get(sym, 0)
    if avg <= 0:
        return 0.0
    try:
        df = scanner.get_market_data(sym, 5)
        if df is None or df.empty:
            return 0.0
        latest_vol = float(df['volume'].iloc[-1])
        elapsed = minutes_elapsed()
        projected = latest_vol * (MARKET_TOTAL_MIN / elapsed)
        return round(projected / avg, 2)
    except Exception:
        return 0.0


def build_avg_cache(scanner: LiquidityScanner, symbols: list) -> dict:
    cache = {}
    def fetch(sym):
        try:
            df = scanner.get_market_data(sym, 25)
            if df is not None and len(df) >= 20:
                return sym, float(df['volume'].rolling(20).mean().iloc[-1])
        except Exception:
            pass
        return sym, 0.0

    with ThreadPoolExecutor(max_workers=8) as pool:
        for sym, avg in pool.map(fetch, symbols):
            cache[sym] = avg
    return cache


def scan(scanner: LiquidityScanner, avg_cache: dict, top_n: int, min_vol_ratio: float):
    print(f"\n{'='*65}")
    print(f"  F&O VOLUME + LIQUIDITY SCAN   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Min vol ratio: {min_vol_ratio}x   Universe: {len(FO_UNIVERSE)} symbols")
    print(f"{'='*65}")

    # Build vol ratios in parallel
    results = []
    def fetch_ratio(sym):
        return sym, get_vol_ratio(scanner, sym, avg_cache)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for sym, ratio in pool.map(fetch_ratio, FO_UNIVERSE):
            results.append((sym, ratio))

    # Also run liquidity scan to get composite scores + delivery %
    liq_universe = scanner.scan_universe(FO_UNIVERSE)
    liq_map = {item['symbol']: item for item in liq_universe}

    # Merge: vol ratio + liquidity score
    merged = []
    for sym, vol_ratio in results:
        liq = liq_map.get(sym, {})
        metrics = liq.get('metrics', {})
        merged.append({
            'symbol':     sym,
            'vol_ratio':  vol_ratio,
            'liq_score':  round(liq.get('composite_score', 0), 1),
            'last_price': round(liq.get('last_price', 0), 2),
            'adv':        int(metrics.get('adv', 0)),
            'delivery':   round(metrics.get('avg_delivery', 0), 1),
            'vol_trend':  round(metrics.get('volume_trend', 1.0), 2),
            'spike':      vol_ratio >= min_vol_ratio,
        })

    # Sort: spike first, then by vol_ratio desc
    merged.sort(key=lambda x: (x['spike'], x['vol_ratio']), reverse=True)
    top = merged[:top_n]

    # Print header
    print(f"\n{'Sym':<14} {'VolRatio':>8} {'LiqScore':>9} {'LastPx':>9} {'ADV(M)':>8} {'Dlvry%':>7} {'Trend':>6}  {'Status'}")
    print(f"{'-'*14} {'-'*8} {'-'*9} {'-'*9} {'-'*8} {'-'*7} {'-'*6}  {'-'*12}")

    spikes = 0
    for r in top:
        flag = '*** SPIKE' if r['spike'] else ''
        if r['spike']:
            spikes += 1
        adv_m = r['adv'] / 1_000_000
        print(
            f"{r['symbol']:<14} {r['vol_ratio']:>8.2f}x {r['liq_score']:>9.1f} "
            f"{r['last_price']:>9.2f} {adv_m:>8.2f} {r['delivery']:>7.1f} "
            f"{r['vol_trend']:>6.2f}  {flag}"
        )

    print(f"\n  Spikes found: {spikes}  |  Min ratio: {min_vol_ratio}x")
    print(f"{'='*65}\n")
    return spikes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--loop',    type=int,   default=0,    help='Repeat interval seconds (0 = run once)')
    parser.add_argument('--top',     type=int,   default=15,   help='Rows to display')
    parser.add_argument('--minvol',  type=float, default=config.VOLUME_EXIT_CONFIG.get('entry_min_vol_ratio', 2.0),
                        help='Min projected vol ratio to flag as spike')
    args = parser.parse_args()

    if not config.USE_MOCK_DATA:
        check_token_health()

    scanner = LiquidityScanner()

    print("Building 20d volume baseline cache...")
    avg_cache = build_avg_cache(scanner, FO_UNIVERSE)
    print(f"Cache ready: {sum(1 for v in avg_cache.values() if v > 0)}/{len(FO_UNIVERSE)} symbols")

    if args.loop > 0:
        last_token_check = time.time()
        while True:
            scan(scanner, avg_cache, args.top, args.minvol)
            # Re-check token health every 60 min
            if not config.USE_MOCK_DATA and time.time() - last_token_check > 3600:
                check_token_health()
                last_token_check = time.time()
            print(f"Next scan in {args.loop}s  (Ctrl+C to stop)")
            time.sleep(args.loop)
    else:
        scan(scanner, avg_cache, args.top, args.minvol)


if __name__ == '__main__':
    main()
