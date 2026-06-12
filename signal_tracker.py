"""
Signal outcome tracker — runs alongside scan_only_v2.py.

Monitors every Grade A/B signal. When price hits target → WIN.
When price hits SL → LOSS. EOD without hit → TIMEOUT.
Writes each outcome to logs/signal_journal.jsonl for accuracy analysis.

Run:
    python signal_tracker.py
"""

import os
import sys
import signal as _signal
import json
import time
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:                                  # Windows cp1252 chokes on the ✅/❌/⏱ icons
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import config
except Exception:
    config = None

LOG_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
SIGNALS_FILE  = os.path.join(LOG_DIR, "signals.json")
TRACKED_FILE  = os.path.join(LOG_DIR, "tracked_signals.json")
JOURNAL_FILE  = os.path.join(LOG_DIR, "signal_journal.jsonl")
CHECK_SEC     = 60     # price check interval
GRADES_TRACK  = {"A", "B"}   # which grades to track

os.makedirs(LOG_DIR, exist_ok=True)

_running = True


def _stop(sig, frame):
    global _running
    _running = False
    print("\nTracker stopping...")


# ─────────────────────────────────────────────────────────────────────────────
# Price fetch
# ─────────────────────────────────────────────────────────────────────────────

def _get_price(symbol: str) -> float:
    try:
        from core.api_dhan import dhan_intraday
        df = dhan_intraday(symbol, interval_min=5, days_back=1)
        if df is None or df.empty:
            return 0.0
        return float(df["close"].iloc[-1])
    except Exception:
        return 0.0


def _get_prices_batch(symbols: list) -> dict:
    """Fetch latest price for multiple symbols from Dhan (per-symbol)."""
    result = {}
    if not symbols:
        return result
    for sym in symbols:
        p = _get_price(sym)
        if p > 0:
            result[sym] = p
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Tracked state
# ─────────────────────────────────────────────────────────────────────────────

def _load_tracked() -> dict:
    if os.path.exists(TRACKED_FILE):
        try:
            with open(TRACKED_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_tracked(tracked: dict) -> None:
    with open(TRACKED_FILE, "w", encoding="utf-8") as f:
        json.dump(tracked, f, indent=2, default=str)


def _append_journal(entry: dict) -> None:
    with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Core logic
# ─────────────────────────────────────────────────────────────────────────────

def _ingest_new_signals(tracked: dict) -> int:
    """Read signals.json; add unseen Grade A/B signals to tracked dict."""
    if not os.path.exists(SIGNALS_FILE):
        return 0
    try:
        with open(SIGNALS_FILE, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return 0

    added = 0
    for s in payload.get("signals", []):
        if s.get("confluence_grade") not in GRADES_TRACK:
            continue
        key = f"{s['symbol']}_{s.get('ts', '')}"
        if key in tracked:
            continue
        entry_p = float(s.get("entry_price") or 0)
        sl_p    = float(s.get("sl_price") or 0)
        tgt_p   = float(s.get("target_price") or 0)
        if entry_p <= 0 or sl_p <= 0 or tgt_p <= 0:
            continue
        tracked[key] = {
            "symbol":      s["symbol"],
            "direction":   s.get("direction", "long"),
            "grade":       s["confluence_grade"],
            "score":       s.get("confluence_score", 0),
            "entry":       entry_p,
            "sl":          sl_p,
            "target":      tgt_p,
            "patterns":    s.get("patterns_combined") or [],
            "reason":      s.get("reason", ""),
            "ts_signal":   s.get("ts", datetime.now().isoformat()),
            "signal_date": str(date.today()),
        }
        added += 1
    return added


def _check_outcomes(tracked: dict) -> list:
    """
    For each tracked signal compare current price to target / SL.
    Returns list of keys that are now closed (outcome recorded).
    """
    if not tracked:
        return []

    symbols = list({v["symbol"] for v in tracked.values()})
    prices  = _get_prices_batch(symbols)

    now  = datetime.now()
    eod  = now.hour >= 15 and now.minute >= 20   # 15:20 — 10 min before close

    closed_keys = []
    for key, t in tracked.items():
        sym   = t["symbol"]
        cur   = prices.get(sym, 0.0)
        if cur <= 0:
            continue

        lng    = t["direction"].lower() == "long"
        entry  = t["entry"]
        sl     = t["sl"]
        target = t["target"]

        # ── Breakeven trail at 0.7R (match backtest exit logic) ──────────
        # Once price moves 0.7R in favour, ratchet the stop to entry. This is
        # exactly what backtest_india_swing.simulate_one_signal does, so the
        # forward results are comparable to the PF 1.17 backtest. A stop that
        # fires AFTER the move is a BE_STOP (scratch), not a full LOSS.
        sl0 = float(t.get("sl0", sl))          # original stop (immutable)
        t.setdefault("sl0", sl0)
        eff_sl = float(t.get("sl_eff", sl0))   # current (possibly trailed) stop
        init_risk = abs(entry - sl0)
        if init_risk > 0 and not t.get("be_moved"):
            be_threshold = entry + 0.7 * init_risk if lng else entry - 0.7 * init_risk
            reached = (cur >= be_threshold) if lng else (cur <= be_threshold)
            if reached:
                eff_sl = entry
                t["sl_eff"] = entry
                t["be_moved"] = True

        outcome    = None
        exit_price = cur

        if lng:
            if cur >= target:
                outcome = "WIN"
            elif cur <= eff_sl:
                outcome = "BE_STOP" if t.get("be_moved") else "LOSS"
                exit_price = eff_sl
        else:
            if cur <= target:
                outcome = "WIN"
            elif cur >= eff_sl:
                outcome = "BE_STOP" if t.get("be_moved") else "LOSS"
                exit_price = eff_sl

        if outcome is None and eod and t.get("signal_date") == str(date.today()):
            outcome = "TIMEOUT"

        if outcome is None:
            continue

        gross_pct = ((exit_price - entry) / entry * 100) if lng else ((entry - exit_price) / entry * 100)
        # Subtract realistic round-trip cost so the journal P&L is NET, not a
        # fantasy gross. Futures all-in (STT sell-side + exchange txn + GST +
        # stamp + SEBI + brokerage) plus half-spread slippage each way is
        # ~0.06% of notional round-trip on liquid names. Configurable via
        # config.FUT_COST_ROUNDTRIP_PCT. Metrics built on this are honest.
        cost_pct = float(getattr(config, "FUT_COST_ROUNDTRIP_PCT", 0.06))
        pnl_pct = gross_pct - cost_pct

        record = {
            "ts_signal":    t["ts_signal"],
            "ts_outcome":   now.isoformat(),
            "symbol":       sym,
            "direction":    t["direction"],
            "grade":        t["grade"],
            "score":        t["score"],
            "entry_price":  entry,
            "sl_price":     sl,
            "target_price": target,
            "exit_price":   round(exit_price, 2),
            "patterns":     t.get("patterns", []),
            "reason":       t.get("reason", ""),
            "outcome":      outcome,
            "instrument":   t.get("instrument", "FUT"),
            "gross_pct":    round(gross_pct, 3),
            "cost_pct":     round(cost_pct, 3),
            "pnl_pct":      round(pnl_pct, 3),
        }
        _append_journal(record)
        closed_keys.append(key)

        icon = "✅" if outcome == "WIN" else ("❌" if outcome == "LOSS" else "⏱")
        print(f"[Tracker] {icon} {sym} {outcome} @ ₹{exit_price:.2f}  "
              f"entry={entry:.2f}  tgt={target:.2f}  sl={sl:.2f}  "
              f"pnl={pnl_pct:+.2f}%")

    return closed_keys


def _market_open() -> bool:
    n = datetime.now()
    t = n.hour * 60 + n.minute
    return 9 * 60 + 15 <= t <= 15 * 60 + 30


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

def main():
    _signal.signal(_signal.SIGINT, _stop)

    print("F&O Signal Tracker started.")
    print(f"Tracking grades: {sorted(GRADES_TRACK)}")
    print(f"Journal: {JOURNAL_FILE}")
    print(f"Check interval: {CHECK_SEC}s\n")

    # Show existing journal stats on startup
    if os.path.exists(JOURNAL_FILE):
        lines = []
        with open(JOURNAL_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    lines.append(json.loads(line.strip()))
                except Exception:
                    pass
        if lines:
            total = len(lines)
            wins  = sum(1 for r in lines if r.get("outcome") == "WIN")
            print(f"Journal: {total} recorded outcomes, {wins}/{total} wins "
                  f"({wins/total*100:.1f}% win rate)\n")

    while _running:
        if not _market_open():
            n = datetime.now()
            if n.hour >= 15 and n.minute >= 31:
                # After close: flush EOD timeouts then stop
                tracked = _load_tracked()
                if tracked:
                    closed = _check_outcomes(tracked)
                    for k in closed:
                        tracked.pop(k, None)
                    _save_tracked(tracked)
                # Daily metrics rollup — tracker is the natural once-a-day
                # trigger (runs every market day, fires once at close). Writes
                # one line to logs/metrics_daily.jsonl with rolling WR/PF/DD +
                # drift/redesign alerts. Failure here must not block shutdown.
                try:
                    from core.metrics_writer import write_metrics
                    rec = write_metrics()
                    r30 = rec.get("rolling_30d", {})
                    print(f"[Metrics] {rec['date']} written: "
                          f"30d WR {r30.get('wr')} PF {r30.get('pf')} "
                          f"DD {r30.get('max_dd_pct')}% "
                          f"| drift={rec.get('drift_alert')} "
                          f"redesign={rec.get('redesign_alert')}")
                    if rec.get("drift_alert"):
                        print("  [!] DRIFT ALERT — 30d PF<0.9 or DD>8%. Review before next session.")
                    if rec.get("redesign_alert"):
                        print("  [!!] REDESIGN ALERT — 60+ days of 90d WR<35%. Strategy edge gone.")
                except Exception as e:
                    print(f"[Metrics] rollup failed (non-fatal): {e}")
                print("Market closed. Tracker done for today.")
                break
            time.sleep(30)
            continue

        tracked = _load_tracked()
        added   = _ingest_new_signals(tracked)
        if added:
            print(f"[Tracker] +{added} new signal(s) added. "
                  f"Monitoring {len(tracked)} total.")

        closed = _check_outcomes(tracked)
        for k in closed:
            tracked.pop(k, None)

        _save_tracked(tracked)

        for _ in range(CHECK_SEC):
            if not _running:
                break
            time.sleep(1)

    print("Tracker stopped.")


if __name__ == "__main__":
    main()
