"""
Dhan preflight — one command to confirm the data path works on the real
machine before the forward paper run.

Run:
    python verify_dhan.py

Checks, in order (each prints PASS/FAIL + detail):
  1. Daily bars      — dhan_daily on 3 liquid names
  2. Intraday bars   — dhan_intraday 5m on 1 name
  3. Index (NIFTY)   — dhan_daily('NIFTY') — regime gate depends on it
  4. Regime snapshot — core.regime_filter.get_regime()
  5. Universe filter — which symbols are blocked
  6. Futures leg     — attach_futures_leg builds a clean FUT signal

Exit code 0 = all critical checks passed (ready to backtest + forward-test).
Exit code 1 = a critical check failed (fix before trading).

Nothing here places orders or needs PAPER_TRADE off. Pure data + logic.
"""

from __future__ import annotations

import sys
import warnings
warnings.filterwarnings("ignore")
import logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

PASS = "PASS"
FAIL = "FAIL"
results = []


def _log(name, ok, detail=""):
    tag = PASS if ok else FAIL
    print(f"  [{tag}] {name}{(' — ' + detail) if detail else ''}")
    results.append((name, ok))


def main():
    print("=" * 60)
    print("  DHAN PREFLIGHT — data path verification")
    print("=" * 60)

    # 1. Daily bars
    print("\n[1] Daily bars (dhan_daily)")
    try:
        from core.api_dhan import dhan_daily
        for sym in ("RELIANCE", "TCS", "HDFCBANK"):
            df = dhan_daily(sym, days_back=60)
            n = 0 if df is None else len(df)
            _log(f"daily {sym}", n >= 30, f"{n} bars")
    except Exception as e:
        _log("daily fetch", False, f"exception: {e}")

    # 2. Intraday bars
    print("\n[2] Intraday 5m bars (dhan_intraday)")
    try:
        from core.api_dhan import dhan_intraday
        df = dhan_intraday("RELIANCE", interval_min=5, days_back=5)
        n = 0 if df is None else len(df)
        _log("intraday RELIANCE 5m", n >= 50, f"{n} bars")
    except Exception as e:
        _log("intraday fetch", False, f"exception: {e}")

    # 3. Index
    print("\n[3] Index data (NIFTY) — regime gate depends on this")
    nifty_ok = False
    try:
        from core.api_dhan import dhan_daily
        df = dhan_daily("NIFTY", days_back=300)
        n = 0 if df is None else len(df)
        nifty_ok = n >= 200
        _log("daily NIFTY", nifty_ok, f"{n} bars (need >=200 for ATR percentile)")
    except Exception as e:
        _log("NIFTY fetch", False, f"exception: {e}")

    # 4. Regime
    print("\n[4] Regime snapshot")
    try:
        from core.regime_filter import get_regime, trade_allowed
        snap = get_regime()
        _log("regime computed", snap.valid,
             f"tag={snap.tag} adx={snap.adx} atr_pct={snap.atr_pct}")
        ok, info = trade_allowed("india_swing")
        print(f"        india_swing allowed now: {ok} ({info.get('reason')})")
    except Exception as e:
        _log("regime", False, f"exception: {e}")

    # 5. Universe filter
    print("\n[5] Universe filter (journal-based)")
    try:
        from core.universe_filter import _ensure, blocked_symbols
        st = _ensure()
        blk = blocked_symbols()
        _log("universe loaded", True, f"{len(st)} symbols with history, {len(blk)} blocked")
        if blk:
            print("        blocked:", ", ".join(sorted(blk.keys())))
    except Exception as e:
        _log("universe filter", False, f"exception: {e}")

    # 6. Futures leg
    print("\n[6] Futures leg builder")
    try:
        from core.futures_leg import attach_futures_leg
        sig = {"symbol": "RELIANCE", "direction": "long", "entry_price": 1400.0,
               "sl_price": 1380.0, "target_price": 1440.0, "reason": "preflight"}
        fut = attach_futures_leg(dict(sig))
        ok = bool(fut) and fut.get("instrument") == "FUT" and fut.get("lot_size", 0) > 0
        _log("attach_futures_leg", ok,
             f"lot={fut.get('lot_size')} qty={fut.get('quantity')} RR={fut.get('rr_ratio')}" if fut else "None")
    except Exception as e:
        _log("futures leg", False, f"exception: {e}")

    # Verdict — critical = data checks (1,3) + leg (6). Universe/regime degrade-open.
    print("\n" + "=" * 60)
    crit = {"daily RELIANCE", "daily NIFTY", "attach_futures_leg"}
    crit_ok = all(ok for name, ok in results if name in crit)
    any_data = any(ok for name, ok in results if name.startswith("daily"))
    if crit_ok:
        print("  VERDICT: PASS — data path live. Next: run the backtest below.")
        print("  Windows CMD:")
        print("    set ONLY_LONG=1& set PRECISION_MODE=0& set DISABLE_G9=1&"
              " set DISABLE_G10=1& set DISABLE_REGIME_GATE=1& python backtest_india_swing.py")
        code = 0
    else:
        print("  VERDICT: FAIL — Dhan data not flowing for critical checks.")
        if not any_data:
            print("  No daily bars at all → check API key (must be the JWT, not app-id),")
            print("  Data API subscription active, and security IDs resolve.")
        else:
            print("  Some bars OK but NIFTY/index missing → fix index security-id +")
            print("  IDX_I segment (regime gate + 1h-bias need it).")
        code = 1
    print("=" * 60)
    sys.exit(code)


if __name__ == "__main__":
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
