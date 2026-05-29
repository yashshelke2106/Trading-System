"""
Monte Carlo price simulation for F&O signal validation.

Given a signal (entry, SL, target, direction), simulate N future price
paths using the stock's EMPIRICAL return distribution (not Gaussian —
real markets have fat tails, skew, and serial correlation).

Returns:
  - P(target_hit): probability price reaches target before SL
  - P(sl_hit): probability SL is hit first
  - P(sideways): neither hit within holding period
  - expected_pnl: probability-weighted expected spot P&L
  - edge_score: (P(target) × target_dist - P(sl) × sl_dist) / sl_dist

Usage in scanner:
  from core.monte_carlo import simulate_signal
  result = simulate_signal(symbol, entry, sl, target, direction, api)
  if result['p_target'] < 0.15:  # < 15% chance of hitting target
      skip signal

Design choices:
  1. Empirical bootstrap (not parametric) — preserves fat tails, skew,
     vol clustering without assuming a distribution
  2. Block bootstrap (blocks of 3-5 bars) — preserves serial correlation
     (momentum/mean-reversion structure in 5m bars)
  3. ADAPTIVE horizon — calculates required bars from target distance
     and per-bar volatility. Typical: 75 bars (1 day) for 2% target,
     225 bars (3 days) for 6% target. Clamped to [30, 450] (min 2.5h,
     max 5 trading days).
  4. Uses regime-adjusted volatility: scales returns by current_atr/historical_atr
     so simulation reflects TODAY's volatility, not last month's average
"""

import logging
import numpy as np
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── Defaults ─────────────────────────────────────────────────────────────
N_SIMULATIONS = 2000        # paths per signal (2k = fast + stable)
HOLDING_BARS_MIN = 30       # floor: 2.5 hours (minimum meaningful window)
HOLDING_BARS_MAX = 450      # ceiling: 5 trading days (75 bars/day × 6)
HOLDING_BARS_DEFAULT = 75   # fallback if adaptive calc fails (1 trading day)
BLOCK_SIZE = 4              # bootstrap block size (preserves momentum)
LOOKBACK_DAYS = 10          # days of 5m data for return distribution
MIN_BARS_REQUIRED = 100     # need at least this many bars for reliable sim
BARS_PER_DAY = 75           # 5m bars in one NSE trading day (9:15-15:30 = 6.25h)


def _fetch_returns(symbol: str, api=None) -> Optional[np.ndarray]:
    """Fetch recent 5m log-returns for symbol. Returns array or None."""
    try:
        from .api_dhan import dhan_intraday
        df = dhan_intraday(symbol, 5, LOOKBACK_DAYS)
        if df is None or len(df) < MIN_BARS_REQUIRED:
            # Try Dhan API
            if api:
                df = api.get_intraday_data(symbol, interval="5", days=LOOKBACK_DAYS)
            if df is None or len(df) < MIN_BARS_REQUIRED:
                return None

        closes = df["close"].values.astype(float)
        # Log returns (handles compounding correctly)
        log_returns = np.diff(np.log(closes))
        # Remove any NaN/inf
        log_returns = log_returns[np.isfinite(log_returns)]
        if len(log_returns) < MIN_BARS_REQUIRED - 1:
            return None
        return log_returns
    except Exception as e:
        log.debug(f"MC fetch_returns {symbol}: {e}")
        return None


def _current_vol_ratio(returns: np.ndarray, recent_window: int = 20) -> float:
    """Ratio of recent volatility to full-sample volatility.

    If today is volatile (ratio > 1), simulation paths will be wider.
    If today is quiet (ratio < 1), paths will be tighter.
    Clamped to [0.5, 2.5] to prevent extreme scaling.
    """
    if len(returns) < recent_window * 2:
        return 1.0
    full_vol = np.std(returns)
    recent_vol = np.std(returns[-recent_window:])
    if full_vol <= 0:
        return 1.0
    ratio = recent_vol / full_vol
    return float(np.clip(ratio, 0.5, 2.5))


def _adaptive_holding_bars(returns: np.ndarray, entry: float,
                           target: float, vol_scale: float = 1.0) -> int:
    """Calculate how many 5m bars needed for price to plausibly reach target.

    Logic: target_dist% / (per_bar_std × vol_scale × sqrt(n)) = ~1.5 sigma.
    Solve for n: n = (target_dist / (1.5 × per_bar_std × vol_scale))^2

    Clamp to [HOLDING_BARS_MIN, HOLDING_BARS_MAX].
    This ensures 6% targets get ~225 bars (3 days), not 60 bars (impossible).
    """
    per_bar_std = np.std(returns)
    if per_bar_std <= 0 or entry <= 0:
        return HOLDING_BARS_DEFAULT

    target_dist_pct = abs(target - entry) / entry
    scaled_std = per_bar_std * vol_scale

    # At how many bars does random walk reach target_dist at 1.5 sigma?
    # target_dist = 1.5 * scaled_std * sqrt(n) => n = (target_dist / (1.5*std))^2
    sigma_target = 1.5
    n_bars = (target_dist_pct / (sigma_target * scaled_std)) ** 2
    n_bars = int(np.clip(n_bars, HOLDING_BARS_MIN, HOLDING_BARS_MAX))

    log.debug(f"MC adaptive bars: target_dist={target_dist_pct:.3%} "
              f"per_bar_std={scaled_std:.5f} -> {n_bars} bars "
              f"({n_bars/BARS_PER_DAY:.1f} days)")
    return n_bars


def _block_bootstrap(returns: np.ndarray, n_bars: int,
                     block_size: int = BLOCK_SIZE) -> np.ndarray:
    """Generate one simulated return path via block bootstrap.

    Block bootstrap preserves short-term serial correlation
    (momentum within blocks) while randomizing across blocks.
    """
    path = []
    n = len(returns)
    while len(path) < n_bars:
        # Random start index for a block
        start = np.random.randint(0, n - block_size)
        block = returns[start:start + block_size]
        path.extend(block.tolist())
    return np.array(path[:n_bars])


def _simulate_paths(returns: np.ndarray, entry: float,
                    n_sims: int = N_SIMULATIONS,
                    n_bars: int = HOLDING_BARS_DEFAULT,
                    vol_scale: float = 1.0) -> np.ndarray:
    """Simulate n_sims price paths from entry using block bootstrap.

    Returns: (n_sims, n_bars+1) array of prices. First column = entry.
    """
    paths = np.zeros((n_sims, n_bars + 1))
    paths[:, 0] = entry

    for i in range(n_sims):
        sim_returns = _block_bootstrap(returns, n_bars)
        # Scale by current volatility regime
        sim_returns = sim_returns * vol_scale
        # Convert log-returns back to prices
        cum_returns = np.cumsum(sim_returns)
        paths[i, 1:] = entry * np.exp(cum_returns)

    return paths


def _evaluate_paths(paths: np.ndarray, entry: float, sl: float,
                    target: float, direction: str) -> Dict:
    """Walk each path forward, check if target or SL hit first.

    Returns dict with probabilities and stats.
    """
    n_sims = paths.shape[0]
    n_bars = paths.shape[1] - 1

    target_hits = 0
    sl_hits = 0
    sideways = 0
    final_pnls = []
    bars_to_target = []
    bars_to_sl = []
    max_favorable_list = []
    max_adverse_list = []

    for i in range(n_sims):
        path = paths[i]
        hit_target = False
        hit_sl = False
        target_bar = n_bars
        sl_bar = n_bars
        max_fav = 0.0
        max_adv = 0.0

        for j in range(1, n_bars + 1):
            price = path[j]

            if direction == "long":
                favorable = price - entry
                adverse = entry - price
                if price >= target and not hit_target:
                    hit_target = True
                    target_bar = j
                if price <= sl and not hit_sl:
                    hit_sl = True
                    sl_bar = j
            else:  # short
                favorable = entry - price
                adverse = price - entry
                if price <= target and not hit_target:
                    hit_target = True
                    target_bar = j
                if price >= sl and not hit_sl:
                    hit_sl = True
                    sl_bar = j

            max_fav = max(max_fav, favorable)
            max_adv = max(max_adv, adverse)

        max_favorable_list.append(max_fav / entry * 100)
        max_adverse_list.append(max_adv / entry * 100)

        # Determine outcome: whichever hit FIRST wins
        if hit_target and hit_sl:
            if target_bar <= sl_bar:
                target_hits += 1
                bars_to_target.append(target_bar)
            else:
                sl_hits += 1
                bars_to_sl.append(sl_bar)
        elif hit_target:
            target_hits += 1
            bars_to_target.append(target_bar)
        elif hit_sl:
            sl_hits += 1
            bars_to_sl.append(sl_bar)
        else:
            sideways += 1

        # Final P&L (at end of holding period or at hit point)
        if hit_target and (not hit_sl or target_bar <= sl_bar):
            pnl = abs(target - entry) / entry * 100
        elif hit_sl and (not hit_target or sl_bar < target_bar):
            pnl = -abs(sl - entry) / entry * 100
        else:
            # Time exit at final price
            final_price = path[-1]
            if direction == "long":
                pnl = (final_price - entry) / entry * 100
            else:
                pnl = (entry - final_price) / entry * 100
        final_pnls.append(pnl)

    p_target = target_hits / n_sims
    p_sl = sl_hits / n_sims
    p_sideways = sideways / n_sims

    sl_dist = abs(entry - sl) / entry * 100
    tgt_dist = abs(target - entry) / entry * 100

    # Edge score: expected value normalized by risk
    # Positive = statistical edge, negative = house wins
    edge = (p_target * tgt_dist - p_sl * sl_dist) / sl_dist if sl_dist > 0 else 0

    return {
        "p_target": round(p_target, 4),
        "p_sl": round(p_sl, 4),
        "p_sideways": round(p_sideways, 4),
        "expected_pnl": round(float(np.mean(final_pnls)), 3),
        "median_pnl": round(float(np.median(final_pnls)), 3),
        "edge_score": round(edge, 3),
        "avg_mfe": round(float(np.mean(max_favorable_list)), 3),
        "avg_mae": round(float(np.mean(max_adverse_list)), 3),
        "avg_bars_to_target": round(float(np.mean(bars_to_target)), 1) if bars_to_target else None,
        "avg_bars_to_sl": round(float(np.mean(bars_to_sl)), 1) if bars_to_sl else None,
        "n_simulations": n_sims,
        "n_bars": n_bars,
        # Distribution percentiles
        "pnl_p10": round(float(np.percentile(final_pnls, 10)), 3),
        "pnl_p25": round(float(np.percentile(final_pnls, 25)), 3),
        "pnl_p75": round(float(np.percentile(final_pnls, 75)), 3),
        "pnl_p90": round(float(np.percentile(final_pnls, 90)), 3),
    }


def simulate_signal(symbol: str, entry: float, sl: float, target: float,
                    direction: str, api=None,
                    n_sims: int = N_SIMULATIONS,
                    n_bars: int = 0) -> Optional[Dict]:
    """Main entry point: simulate a signal and return probability estimates.

    Args:
        symbol: Stock symbol (e.g., 'RELIANCE')
        entry: Entry price
        sl: Stop-loss price
        target: Target price
        direction: 'long' or 'short'
        api: Optional DhanAPI instance for data fetching
        n_sims: Number of Monte Carlo paths (default 2000)
        n_bars: Holding period in 5m bars. 0 = auto-calculate from
                target distance and empirical volatility.

    Returns:
        Dict with p_target, p_sl, p_sideways, expected_pnl, edge_score, etc.
        None if insufficient data for simulation.
    """
    if entry <= 0 or sl <= 0 or target <= 0:
        return None

    # Validate direction vs levels
    if direction == "long" and (target <= entry or sl >= entry):
        log.debug(f"MC {symbol}: long but target <= entry or sl >= entry")
        return None
    if direction == "short" and (target >= entry or sl <= entry):
        log.debug(f"MC {symbol}: short but target >= entry or sl <= entry")
        return None

    # Fetch empirical returns
    returns = _fetch_returns(symbol, api)
    if returns is None:
        log.debug(f"MC {symbol}: insufficient data for simulation")
        return None

    # Adjust for current volatility regime
    vol_scale = _current_vol_ratio(returns)

    # Adaptive holding period: auto-calculate from target distance + vol
    if n_bars <= 0:
        n_bars = _adaptive_holding_bars(returns, entry, target, vol_scale)

    # Run simulation
    paths = _simulate_paths(returns, entry, n_sims, n_bars, vol_scale)

    # Evaluate outcomes
    result = _evaluate_paths(paths, entry, sl, target, direction)
    result["symbol"] = symbol
    result["direction"] = direction
    result["vol_scale"] = round(vol_scale, 3)
    result["holding_bars"] = n_bars
    result["holding_days"] = round(n_bars / BARS_PER_DAY, 1)

    log.info(
        f"MC {symbol} {direction}: P(tgt)={result['p_target']:.0%} "
        f"P(sl)={result['p_sl']:.0%} P(side)={result['p_sideways']:.0%} "
        f"E[pnl]={result['expected_pnl']:+.2f}% edge={result['edge_score']:+.2f} "
        f"bars={n_bars} ({n_bars/BARS_PER_DAY:.1f}d)"
    )

    return result


def simulate_batch(signals: List[Dict], api=None,
                   n_sims: int = N_SIMULATIONS) -> List[Dict]:
    """Run MC simulation on a batch of signals. Adds mc_* fields to each signal.

    Signals without sufficient data get mc_edge=0 (neutral, don't block).
    """
    for sig in signals:
        sym = sig.get("symbol", "")
        entry = float(sig.get("entry_price", 0) or 0)
        sl = float(sig.get("sl_price", 0) or 0)
        tgt = float(sig.get("target_price", 0) or 0)
        direction = sig.get("direction", "long")

        result = simulate_signal(sym, entry, sl, tgt, direction, api, n_sims)

        if result:
            sig["mc_p_target"] = result["p_target"]
            sig["mc_p_sl"] = result["p_sl"]
            sig["mc_p_sideways"] = result["p_sideways"]
            sig["mc_expected_pnl"] = result["expected_pnl"]
            sig["mc_edge"] = result["edge_score"]
            sig["mc_avg_mfe"] = result["avg_mfe"]
            sig["mc_avg_mae"] = result["avg_mae"]
            sig["mc_vol_scale"] = result["vol_scale"]
        else:
            # No data — neutral score (don't penalize)
            sig["mc_p_target"] = None
            sig["mc_p_sl"] = None
            sig["mc_edge"] = 0.0

    return signals


# ── CLI for standalone testing ───────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Monte Carlo signal simulator")
    parser.add_argument("--symbol", required=True, help="Stock symbol")
    parser.add_argument("--entry", type=float, required=True)
    parser.add_argument("--sl", type=float, required=True)
    parser.add_argument("--target", type=float, required=True)
    parser.add_argument("--direction", default="long", choices=["long", "short"])
    parser.add_argument("--sims", type=int, default=N_SIMULATIONS)
    parser.add_argument("--bars", type=int, default=0, help="0=auto-calc from target dist")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    result = simulate_signal(
        args.symbol, args.entry, args.sl, args.target,
        args.direction, n_sims=args.sims, n_bars=args.bars,
    )

    if result:
        print(json.dumps(result, indent=2))
    else:
        print("Insufficient data for simulation")
