"""
account_task.py - the daily job that makes the account a trader.

WHY A JOB AND NOT A BUTTON
--------------------------
A trading account only means something if it runs whether or not anyone is
watching. If the cycle only fires when someone opens the dashboard, then the
book's exits happen late, positions resolve against whatever bar happened to be
last, and the P&L becomes a record of when the screen was open rather than of
what the market did.

Two passes, in this order, and the order matters:

    1. MARK + EXIT   every open position is marked to the latest daily bar and
                     closed if it hit its stop, its target, or its expiry.
    2. SELECT + OPEN whatever survives the risk caps is funded from cash.

Exits run first so that cash released by a closing position is available to the
trades chosen in the same cycle. Running them the other way round would leave
the account refusing affordable trades because money it no longer needed was
still tied up.

IDEMPOTENT
----------
Re-running in the same session is safe. Exits are resolved from bar data, so a
position that already closed is simply not open any more; entries are capped at
one per symbol, so a repeated cycle does not double a position.

RUN
---
    python account_task.py                 # one cycle: mark, exit, select
    python account_task.py --mark-only     # resolve exits, open nothing
    python account_task.py --status        # print the book, change nothing
    python account_task.py --reset --capital 1000000

SCHEDULING
----------
Run once after the close (>= 16:00 IST), after `capture_task.py`, so the daily
bar the exits resolve against is the finished one. Running intraday marks
against a forming bar, which is the exact corruption the capture layer already
had to defend against.

PAPER ONLY
----------
This places no orders anywhere. config.PAPER_TRADE stays True; the account
moves simulated cash against real prices.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core.trading_account import (
    DEFAULT_CAPITAL, RiskPolicy, _print_summary, load_state, mark_and_exit,
    reset, select_and_open, summary,
)


def _guard_paper_mode() -> None:
    """Refuse to run if someone flipped the system live.

    This account is a simulation. If PAPER_TRADE is ever False the rest of the
    stack may route real orders, and a simulated book running beside a live one
    is a reconciliation nightmare, so stop rather than guess.
    """
    try:
        import config
    except Exception:
        return                      # config is untracked; nothing to contradict
    if getattr(config, "PAPER_TRADE", True) is False:
        raise SystemExit(
            "[account] PAPER_TRADE is False. This book is a simulation and "
            "will not run alongside live execution. Re-enable paper mode or "
            "run the live path deliberately.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Daily funded-account cycle")
    ap.add_argument("--reset", action="store_true",
                    help="archive the existing book and start fresh")
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    ap.add_argument("--risk-per-trade", type=float, default=None,
                    help="fraction of equity risked per trade (default 0.0075)")
    ap.add_argument("--mark-only", action="store_true",
                    help="resolve exits but open nothing")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)

    _guard_paper_mode()

    if a.reset:
        pol = RiskPolicy()
        if a.risk_per_trade is not None:
            pol.risk_per_trade_pct = a.risk_per_trade
        r = reset(a.capital, pol, force=True)
        print(f"[account] reset to Rs {a.capital:,.0f}"
              + (f"; archived {len(r['archived'])} file(s) to {r['archive_dir']}"
                 if r.get("archived") else ""))

    if a.status:
        if a.json:
            print(json.dumps(summary(), indent=2, default=str))
        else:
            _print_summary(summary())
        return 0

    if not load_state():
        print("[account] no account yet. Start one:\n"
              "    python account_task.py --reset --capital 1000000")
        return 1

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    marked = mark_and_exit()
    print(f"[account] {stamp}  exits: {marked.get('n_exits', 0)}")
    if marked.get("no_data"):
        # Never silently treat "no price" as "no move" — say which symbols
        # could not be marked, because their P&L is stale, not flat.
        print(f"[account] WARNING could not mark (stale, not flat): "
              f"{', '.join(marked['no_data'])}")

    if not a.mark_only:
        entered = select_and_open()
        if entered.get("halted"):
            print(f"[account] entries HALTED - {entered.get('reason')}")
        else:
            print(f"[account] opened: {entered.get('n_opened', 0)}")
            for d in entered.get("decisions", []):
                if d.get("taken"):
                    print(f"    TAKE  {d['symbol']:<12} {d['instrument']:<8} "
                          f"qty {d['qty']:>6}  Rs {d['cost_basis']:>11,.0f}  "
                          f"risk Rs {d['risk']:>8,.0f}")

    if a.json:
        print(json.dumps(summary(), indent=2, default=str))
    else:
        _print_summary(summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
