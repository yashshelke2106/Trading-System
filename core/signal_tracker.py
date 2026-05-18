"""
Signal tracker — auto-resolves open signals by monitoring prices/premiums,
then writes a paper trade record to trades.csv for P&L tracking.

Called periodically from scan_only_v2.py after each scan cycle.

Mode A — new signals (have entry_prem from option chain enrichment):
  Fetches current CE/PE LTP from Dhan option chain per symbol.
  current_prem >= target_prem → TARGET_HIT  (exit at target_prem)
  current_prem <= sl_prem     → SL_HIT      (exit at sl_prem)
  If chain row unavailable, falls back to: entry_prem + delta × spot_move.
  age > MAX_SIGNAL_AGE_HOURS  → EXPIRED     (exit at current_prem)
  P&L = (exit_prem - entry_prem) × lot_size  (option buyer P&L)

Mode B — legacy signals (no entry_prem, spot-based):
  Fetches current spot price via yfinance.
  long:  price >= target_price → TARGET_HIT  (exit at target_price)
         price <= sl_price     → SL_HIT      (exit at sl_price)
  short: price <= target_price → TARGET_HIT
         price >= sl_price     → SL_HIT
  age > MAX_SIGNAL_AGE_HOURS   → EXPIRED     (exit at current_price)
  P&L = (exit_spot - entry_spot) × lot_size

Paper trade P&L uses exact target/sl levels as exit so P&L reflects the
signal's promise, not the price at scan time.
1 lot per signal. No real broker interaction.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)

# Hold horizon from the active trade mode: intraday 6.5h (one session),
# swing 10 calendar days. A signal older than this resolves as EXPIRED.
try:
    from core.trade_mode import get_mode as _get_mode
    MAX_SIGNAL_AGE_HOURS = float(_get_mode().hold_horizon_hours)
except Exception:
    MAX_SIGNAL_AGE_HOURS = 6.5

# ── Honest-fill cost model ───────────────────────────────────────────────
# A delta-mapped gross exit premium is fiction until you pay the market its
# tax. Two unavoidable costs for an option BUYER on real fills:
#   1. Spread — you buy at ask, sell at bid. Liquid F&O option round-trip
#      ~5-7% of premium; we use a conservative flat fraction of entry prem.
#   2. Theta  — every hour held bleeds premium even if spot is flat. ATM
#      intraday option loses ~7-9% of premium over a full session.
# Costs only ever REDUCE the exit (shave wins, deepen losses) and never
# push premium below the 5% theta-worst floor. This is the single change
# that turns a synthetic 62% WR into an honest ~48% — and an honest label
# is the only thing calibration (F2) can legitimately learn from.
COST_SPREAD_RT_PCT   = 0.06    # round-trip spread = 6% of entry premium
COST_THETA_PCT_PER_H = 0.012   # 1.2% of entry premium bled per hour held
COST_FLOOR_PCT       = 0.05    # premium can't go below 5% of entry (theta cap)


def _apply_fill_costs(entry_prem: float, gross_exit_prem: float,
                      hours_held: float) -> float:
    """Net exit premium after spread + theta. Buyer-side, costs subtract only."""
    if entry_prem <= 0:
        return gross_exit_prem
    spread = entry_prem * COST_SPREAD_RT_PCT
    theta  = entry_prem * COST_THETA_PCT_PER_H * max(hours_held, 0.0)
    net    = gross_exit_prem - spread - theta
    return max(net, entry_prem * COST_FLOOR_PCT)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TRADES_CSV   = os.path.join(_PROJECT_ROOT, "logs", "trades.csv")

_PAPER_COLS = [
    "trade_id", "timestamp", "symbol", "direction",
    "entry_price", "exit_price", "quantity", "pnl", "pnl_percent",
    "status", "exit_reason", "session", "market_bias",
    "patterns_combined", "confluence_score", "ai_probability", "grade",
    "option_strike", "option_expiry", "option_type", "entry_premium", "exit_premium",
]


def _write_paper_trade(sig: Dict, outcome: str, exit_price: float, lot_size: int,
                       exit_prem: Optional[float] = None) -> None:
    """Append a paper trade record to trades.csv for the resolved signal."""
    sym       = sig.get("symbol", "")
    direction = sig.get("direction", "long").lower()
    entry     = float(sig.get("entry_price", 0))
    entry_prem = sig.get("entry_prem")

    if entry_prem and exit_prem is not None:
        # Option P&L: premium move × lot_size (buyer perspective)
        ep = float(entry_prem)
        pnl = (exit_prem - ep) * lot_size
        pnl_pct = (pnl / (ep * lot_size) * 100) if ep > 0 and lot_size > 0 else 0.0
    else:
        # Spot P&L (legacy)
        if direction == "long":
            pnl = (exit_price - entry) * lot_size
        else:
            pnl = (entry - exit_price) * lot_size
        pnl_pct = (pnl / (entry * lot_size) * 100) if entry > 0 and lot_size > 0 else 0.0

    patterns = sig.get("patterns", [])
    patterns_str = " | ".join(patterns) if isinstance(patterns, list) else str(patterns)

    trade_id = f"PAPER_{sig.get('signal_id', sym + '_' + datetime.now().strftime('%H%M%S'))}"

    # Guard: don't double-write same paper trade
    if os.path.exists(_TRADES_CSV):
        try:
            with open(_TRADES_CSV, encoding="utf-8") as f:
                if trade_id in f.read():
                    return
        except Exception:
            pass

    os.makedirs(os.path.dirname(_TRADES_CSV), exist_ok=True)
    write_header = not os.path.exists(_TRADES_CSV) or os.path.getsize(_TRADES_CSV) == 0

    row = {
        "trade_id":          trade_id,
        "timestamp":         sig.get("ts", datetime.now().isoformat()),
        "symbol":            sym.upper(),
        "direction":         direction.upper(),
        "entry_price":       round(entry, 2),
        "exit_price":        round(exit_price, 2),
        "quantity":          lot_size,
        "pnl":               round(pnl, 2),
        "pnl_percent":       round(pnl_pct, 2),
        "status":            "WIN" if outcome == "TARGET_HIT" else ("LOSS" if outcome == "SL_HIT" else "EXPIRED"),
        "exit_reason":       outcome,
        "session":           sig.get("session", ""),
        "market_bias":       sig.get("market_bias", ""),
        "patterns_combined": patterns_str,
        "confluence_score":  sig.get("score", 0),
        "ai_probability":    sig.get("ai_prob", 0),
        "grade":             sig.get("grade", "C"),
        "option_strike":     sig.get("option_strike", ""),
        "option_expiry":     sig.get("option_expiry", ""),
        "option_type":       sig.get("option_type", ""),
        "entry_premium":     round(float(entry_prem or 0), 2),
        "exit_premium":      round(float(exit_prem or 0), 2),
    }

    # Detect actual column order from existing file header so rows align correctly.
    try:
        if os.path.exists(_TRADES_CSV) and os.path.getsize(_TRADES_CSV) > 0:
            with open(_TRADES_CSV, encoding="utf-8") as f:
                existing_cols = f.readline().strip().split(",")
            fieldnames = existing_cols if existing_cols and existing_cols[0] else _PAPER_COLS
            write_header = False
        else:
            fieldnames = _PAPER_COLS

        with open(_TRADES_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        log.info(f"[Tracker] paper trade: {sym} {outcome} pnl=₹{pnl:+.0f}")
    except Exception as e:
        log.warning(f"[Tracker] paper trade write error: {e}")


def _fetch_prices(symbols: List[str]) -> Dict[str, float]:
    """Fetch current/last price for each symbol via yfinance."""
    prices: Dict[str, float] = {}
    if not symbols:
        return prices
    try:
        import yfinance as yf
        tickers = [s + ".NS" for s in symbols]
        data = yf.download(
            tickers,
            period="1d",
            interval="5m",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        for sym, ticker in zip(symbols, tickers):
            try:
                if len(symbols) == 1:
                    close_col = data["Close"] if "Close" in data.columns else None
                    if close_col is not None and not close_col.dropna().empty:
                        prices[sym] = float(close_col.dropna().iloc[-1])
                else:
                    if ticker in data.columns.get_level_values(0):
                        col = data[ticker]["Close"].dropna()
                        if not col.empty:
                            prices[sym] = float(col.iloc[-1])
            except Exception:
                pass
    except Exception as e:
        log.warning(f"[Tracker] price fetch error: {e}")
    return prices


def _fetch_option_chains(symbols: List[str]) -> Dict[str, List[Dict]]:
    """Fetch live option chains from Dhan for the given symbols."""
    chains: Dict[str, List[Dict]] = {}
    if not symbols:
        return chains
    try:
        from core.dashboard_data import get_option_chain
        for sym in symbols:
            try:
                chain = get_option_chain(sym) or []
                if chain:
                    chains[sym] = chain
            except Exception as e:
                log.debug(f"[Tracker] chain fetch {sym}: {e}")
    except Exception as e:
        log.warning(f"[Tracker] option chain import error: {e}")
    return chains


def _current_prem_from_chain(chain: List[Dict], option_strike: float,
                              option_type: str) -> float:
    """Look up current LTP for a given strike from the chain."""
    if not chain or not option_strike:
        return 0.0
    row = min(chain, key=lambda r: abs(r["strike"] - option_strike), default=None)
    if row is None:
        return 0.0
    if option_type == "PE":
        return float(row.get("pe_ltp", 0) or 0)
    return float(row.get("ce_ltp", 0) or 0)


def check_outcomes(lot_sizes: Dict[str, int] = None) -> Tuple[int, int, int]:
    """
    Resolve all open signals whose price/premium crossed SL or target.
    Returns (target_hits, sl_hits, expired).
    """
    from core.signal_journal import get_open_signals, resolve_signal
    import config

    lot_sizes = lot_sizes or getattr(config, "NSE_LOT_SIZES", {})
    open_sigs = get_open_signals()
    if not open_sigs:
        return 0, 0, 0

    now = datetime.now()
    symbols = list({s["symbol"] for s in open_sigs})

    # Fetch spot prices for all symbols (needed for Mode B + delta fallback in Mode A)
    prices = _fetch_prices(symbols)

    # Fetch option chains for Mode A signals only
    mode_a_syms = [s["symbol"] for s in open_sigs if s.get("entry_prem")]
    chains = _fetch_option_chains(list(set(mode_a_syms)))

    target_hits = sl_hits = expired = 0

    for sig in open_sigs:
        sym = sig["symbol"]
        direction = sig.get("direction", "long").lower()
        ts_str = sig.get("ts", "")
        lot = lot_sizes.get(sym, 1)

        try:
            sig_age_h = (now - datetime.fromisoformat(ts_str)).total_seconds() / 3600.0
        except Exception:
            sig_age_h = 0.0

        entry_prem = sig.get("entry_prem")

        if entry_prem:
            # ── Mode A: option premium tracking ──────────────────────────────
            entry_prem_f  = float(entry_prem)
            target_prem   = float(sig.get("target_prem", 0) or 0)
            sl_prem_level = float(sig.get("sl_prem", 0) or 0)
            delta         = float(sig.get("delta", 0) or 0)
            entry_spot    = float(sig.get("entry_price", 0))
            option_type   = sig.get("option_type", "CE")
            option_strike = float(sig.get("option_strike", 0) or 0)

            # Current premium: live Dhan chain first
            current_prem = _current_prem_from_chain(
                chains.get(sym, []), option_strike, option_type
            )

            # Delta-approximation fallback when chain row missing or LTP=0
            if current_prem <= 0:
                current_spot = prices.get(sym, 0)
                if current_spot > 0 and abs(delta) > 0 and entry_spot > 0:
                    spot_move = (current_spot - entry_spot) if option_type == "CE" \
                                else (entry_spot - current_spot)
                    current_prem = entry_prem_f + abs(delta) * spot_move
                    current_prem = max(current_prem, entry_prem_f * 0.05)

            if sig_age_h > MAX_SIGNAL_AGE_HOURS:
                # Walk SPOT forward for the REAL outcome, then map to premium
                # via delta. Old stub used last-known/entry premium → pnl=0
                # garbage that poisoned the adaptive learner.
                try:
                    from core.exit_replay import replay_exit
                    res = replay_exit(sig)
                    real_outcome = res["outcome"]
                    exit_spot = float(res["exit_price"])
                    # Favorable spot move in the option's direction
                    if option_type == "CE":
                        spot_move = exit_spot - entry_spot
                    else:
                        spot_move = entry_spot - exit_spot
                    if abs(delta) > 0:
                        exit_p = entry_prem_f + abs(delta) * spot_move
                    else:
                        exit_p = current_prem if current_prem > 0 else entry_prem_f
                    # Premium can't go below ~5% of entry (theta worst case)
                    exit_p = max(exit_p, entry_prem_f * 0.05)
                    # Honest fill: pay spread + theta for time actually held.
                    _bars = res.get("bars_held") or 0
                    _hrs  = (_bars * 5.0 / 60.0) if _bars else min(
                        sig_age_h, MAX_SIGNAL_AGE_HOURS)
                    _gross = exit_p
                    exit_p = _apply_fill_costs(entry_prem_f, exit_p, _hrs)
                    # Costs can flip a marginal target into a real loss — honest.
                    if real_outcome == "TARGET_HIT" and exit_p <= entry_prem_f:
                        real_outcome = "SL_HIT"

                    if real_outcome == "TARGET_HIT":
                        oc = "TARGET_HIT"; target_hits += 1
                    elif real_outcome == "SL_HIT":
                        oc = "SL_HIT"; sl_hits += 1
                    else:  # TIME_EXIT / NO_DATA
                        oc = "EXPIRED"; expired += 1
                    # CLEAN spot label = the raw spot-path result BEFORE the
                    # theta/IV cost reclassification (signal-skill, denoised).
                    _clean = {
                        "spot_outcome": res.get("outcome"),
                        "spot_pnl_pct": res.get("pnl_pct"),
                        "mfe_pct":      res.get("mfe_pct"),
                        "mae_pct":      res.get("mae_pct"),
                        "exit_reason":  res.get("exit_reason"),
                    }
                    resolve_signal(sig["signal_id"], oc,
                                   float(sig.get("entry_price", 0)),
                                   lot_size=lot, exit_prem=exit_p,
                                   extra=_clean)
                    _write_paper_trade(sig, oc,
                                       float(sig.get("entry_price", 0)),
                                       lot, exit_prem=exit_p)
                    log.info(f"[Tracker] REPLAY {oc} {sym} {option_type} "
                             f"spot={exit_spot:.2f} prem={exit_p:.2f} "
                             f"mfe={res['mfe_pct']:+.2f}%")
                except Exception as e:
                    log.warning(f"[Tracker] Mode-A exit_replay failed {sym}: {e} — stub")
                    exit_p = current_prem if current_prem > 0 else entry_prem_f
                    resolve_signal(sig["signal_id"], "EXPIRED",
                                   float(sig.get("entry_price", 0)), lot_size=lot, exit_prem=exit_p)
                    _write_paper_trade(sig, "EXPIRED",
                                       float(sig.get("entry_price", 0)), lot, exit_prem=exit_p)
                    expired += 1
                continue

            if current_prem <= 0:
                continue  # Can't determine premium; skip until next cycle

            if target_prem > 0 and current_prem >= target_prem:
                net = _apply_fill_costs(entry_prem_f, target_prem, sig_age_h)
                # Honest: if spread+theta ate the whole edge it's not a win.
                oc = "TARGET_HIT" if net > entry_prem_f else "SL_HIT"
                resolve_signal(sig["signal_id"], oc,
                               option_strike, lot_size=lot, exit_prem=net)
                _write_paper_trade(sig, oc,
                                   option_strike, lot, exit_prem=net)
                if oc == "TARGET_HIT":
                    target_hits += 1
                else:
                    sl_hits += 1
                log.info(f"[Tracker] {oc} {sym} {option_type} "
                         f"gross={target_prem:.2f} net={net:.2f}")
            elif sl_prem_level > 0 and current_prem <= sl_prem_level:
                net = _apply_fill_costs(entry_prem_f, sl_prem_level, sig_age_h)
                resolve_signal(sig["signal_id"], "SL_HIT",
                               option_strike, lot_size=lot, exit_prem=net)
                _write_paper_trade(sig, "SL_HIT",
                                   option_strike, lot, exit_prem=net)
                sl_hits += 1
                log.info(f"[Tracker] SL_HIT {sym} {option_type} "
                         f"gross={sl_prem_level:.2f} net={net:.2f}")

        else:
            # ── Mode B: legacy spot tracking ─────────────────────────────────
            entry  = float(sig.get("entry_price", 0))
            sl     = float(sig.get("sl_price", 0))
            target = float(sig.get("target_price", 0))

            if sig_age_h > MAX_SIGNAL_AGE_HOURS:
                # Walk forward through 5m bars to find the REAL outcome instead
                # of stamping EXPIRED with exit==entry (pnl=0 garbage).
                try:
                    from core.exit_replay import replay_exit
                    res = replay_exit(sig)
                    real_outcome = res["outcome"]
                    real_exit = float(res["exit_price"])
                    # Mode B is spot tracking — outcome already IS the clean
                    # spot label; still persist magnitude for graded learning.
                    _clean = {
                        "spot_outcome": res.get("outcome"),
                        "spot_pnl_pct": res.get("pnl_pct"),
                        "mfe_pct":      res.get("mfe_pct"),
                        "mae_pct":      res.get("mae_pct"),
                        "exit_reason":  res.get("exit_reason"),
                    }
                    if real_outcome == "TARGET_HIT":
                        resolve_signal(sig["signal_id"], "TARGET_HIT", real_exit,
                                       lot_size=lot, extra=_clean)
                        _write_paper_trade(sig, "TARGET_HIT", real_exit, lot)
                        target_hits += 1
                        log.info(f"[Tracker] REPLAY TARGET_HIT {sym} @ {real_exit:.2f} "
                                 f"(mfe={res['mfe_pct']:+.2f}%)")
                    elif real_outcome == "SL_HIT":
                        resolve_signal(sig["signal_id"], "SL_HIT", real_exit,
                                       lot_size=lot, extra=_clean)
                        _write_paper_trade(sig, "SL_HIT", real_exit, lot)
                        sl_hits += 1
                        log.info(f"[Tracker] REPLAY SL_HIT {sym} @ {real_exit:.2f} "
                                 f"(mfe={res['mfe_pct']:+.2f}%)")
                    else:
                        # TIME_EXIT or NO_DATA — still EXPIRED but with real exit price
                        resolve_signal(sig["signal_id"], "EXPIRED", real_exit,
                                       lot_size=lot, extra=_clean)
                        _write_paper_trade(sig, "EXPIRED", real_exit, lot)
                        expired += 1
                        log.info(f"[Tracker] REPLAY TIME_EXIT {sym} @ {real_exit:.2f} "
                                 f"pnl={res['pnl_pct']:+.2f}% mfe={res['mfe_pct']:+.2f}%")
                except Exception as e:
                    # Fallback to legacy stub if replay fails
                    log.warning(f"[Tracker] exit_replay failed {sym}: {e} — using stub")
                    resolve_signal(sig["signal_id"], "EXPIRED", entry, lot_size=lot)
                    _write_paper_trade(sig, "EXPIRED", entry, lot)
                    expired += 1
                continue

            current_price = prices.get(sym)
            if current_price is None or current_price <= 0:
                continue

            if direction == "long":
                if target > 0 and current_price >= target:
                    resolve_signal(sig["signal_id"], "TARGET_HIT", target, lot_size=lot)
                    _write_paper_trade(sig, "TARGET_HIT", target, lot)
                    target_hits += 1
                    log.info(f"[Tracker] TARGET_HIT {sym} @ {target:.2f}")
                elif sl > 0 and current_price <= sl:
                    resolve_signal(sig["signal_id"], "SL_HIT", sl, lot_size=lot)
                    _write_paper_trade(sig, "SL_HIT", sl, lot)
                    sl_hits += 1
                    log.info(f"[Tracker] SL_HIT {sym} @ {sl:.2f}")
            else:
                if target > 0 and current_price <= target:
                    resolve_signal(sig["signal_id"], "TARGET_HIT", target, lot_size=lot)
                    _write_paper_trade(sig, "TARGET_HIT", target, lot)
                    target_hits += 1
                    log.info(f"[Tracker] TARGET_HIT {sym} @ {target:.2f}")
                elif sl > 0 and current_price >= sl:
                    resolve_signal(sig["signal_id"], "SL_HIT", sl, lot_size=lot)
                    _write_paper_trade(sig, "SL_HIT", sl, lot)
                    sl_hits += 1
                    log.info(f"[Tracker] SL_HIT {sym} @ {sl:.2f}")

    if target_hits or sl_hits or expired:
        log.info(f"[Tracker] resolved: TARGET={target_hits} SL={sl_hits} EXPIRED={expired}")

    return target_hits, sl_hits, expired
