"""macro_screen.py — daily cross-asset opportunity scan (PAPER ONLY).

Scans 18 markets across equity, rates, credit, FX, commodities and crypto for
time-series-momentum opportunities, sizes them by inverse trailing volatility,
and journals the resulting target book so forward evidence accumulates
honestly. This is the macro sleeve described in core/multi_asset.py.

    python macro_screen.py                 # scan and print today's book
    python macro_screen.py --journal       # also append to the paper journal
    python macro_screen.py --json          # also write logs/macro_screen.json
    python macro_screen.py --report        # forward P&L of the paper journal

NOTHING HERE PLACES AN ORDER. The journal is a research ledger, exactly like
logs/swing_paper_journal.jsonl — and read it the same way: an un-weighted
research record, not a portfolio and not rupees.

The in-sample result is in core/multi_asset.py. It is NOT evidence that this
will work forward; it is the reason the forward test was registered (H-022).
Judge it on the journal, after the pre-registered window.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

JOURNAL = os.path.join("logs", "macro_paper_journal.jsonl")
SNAPSHOT = os.path.join("logs", "macro_screen.json")


def _journaled_bars() -> set:
    if not os.path.exists(JOURNAL):
        return set()
    out = set()
    for line in open(JOURNAL, encoding="utf-8"):
        try:
            out.add(json.loads(line)["bar"])
        except Exception:
            pass
    return out


def journal(rows: list, summary: dict) -> int:
    """Append today's book as one row per market. Idempotent per bar date."""
    if not rows:
        return 0
    bar = rows[0]["bar"]
    if bar in _journaled_bars():
        return 0
    os.makedirs("logs", exist_ok=True)
    stamped = datetime.now().isoformat(timespec="seconds")
    with open(JOURNAL, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({**r, "gross_exposure": summary["gross_exposure"],
                                "journaled": stamped, "status": "open"}) + "\n")
    return len(rows)


def report() -> None:
    """Forward P&L of journaled books, marked against later closes."""
    import pandas as pd
    import yfinance as yf

    if not os.path.exists(JOURNAL):
        print("no macro journal yet — run with --journal after a scan")
        return
    rows = [json.loads(l) for l in open(JOURNAL, encoding="utf-8") if l.strip()]
    bars = sorted({r["bar"] for r in rows})
    print(f"macro paper journal: {len(rows)} rows across {len(bars)} books "
          f"({bars[0]} to {bars[-1]})")
    if len(bars) < 2:
        print("need at least two journaled books before a forward mark means anything.")
        return

    tickers = sorted({r["ticker"] for r in rows})
    px = yf.download(tickers, period="2y", interval="1d", auto_adjust=True,
                     progress=False, threads=True)["Close"].ffill()
    cum = 0.0
    print(f"\n  {'book':12}{'gross':>7}{'fwd ret':>10}{'cum':>9}")
    for i, b in enumerate(bars[:-1]):
        nxt = bars[i + 1]
        book = [r for r in rows if r["bar"] == b]
        pnl = 0.0
        for r in book:
            try:
                a = float(px[r["ticker"]].loc[:b].iloc[-1])
                z = float(px[r["ticker"]].loc[:nxt].iloc[-1])
                pnl += r["weight"] * (z / a - 1)
            except Exception:
                continue
        cum += pnl
        print(f"  {b:12}{book[0].get('gross_exposure', 0):7.2f}"
              f"{pnl*100:9.2f}%{cum*100:8.2f}%")
    print(f"\n  cumulative (un-compounded, gross of costs): {cum*100:+.2f}%")
    print("  NOT a portfolio return — costs and rebalancing are not applied here.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", action="store_true", help="append today's book")
    ap.add_argument("--json", action="store_true", help="write snapshot json")
    ap.add_argument("--report", action="store_true", help="forward P&L and exit")
    args = ap.parse_args()

    if args.report:
        report()
        return 0

    from core.multi_asset import book_summary, fetch, signals

    px = fetch()
    rows = signals(px)
    s = book_summary(rows)

    print("=" * 74)
    print("MACRO SCREEN — cross-asset trend (PAPER ONLY, no orders)")
    print("=" * 74)
    print(f"bar {rows[0]['bar']}   {len(rows)} markets   "
          f"gross {s['gross_exposure']:.2f}x   net {s['net_exposure']:+.2f}x   "
          f"{s['n_long']}L / {s['n_short']}S / {s['n_flat']}F")
    print()
    hdr = (f"{'#':>2} {'MARKET':<16}{'CLASS':<11}{'DIR':<6}{'MOM252':>9}"
           f"{'VOL':>8}{'WEIGHT':>9}  IN")
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(rows, 1):
        if r["direction"] == "flat":
            continue
        print(f"{i:>2} {r['name']:<16}{r['asset_class']:<11}{r['direction']:<6}"
              f"{r['mom_252d_pct']:>8.1f}%{r['ann_vol_pct']:>7.1f}%"
              f"{r['weight']:>9.3f}  {'Y' if r['india_accessible'] else '-'}")

    print()
    print("net exposure by asset class:")
    for k, v in s["net_by_class"].items():
        print(f"  {k:<11}{v:+.3f}")

    n_ind = sum(1 for r in rows if r["india_accessible"] and r["direction"] != "flat")
    print()
    print(f"ACCESS: {n_ind} of {s['n_long'] + s['n_short']} active positions are "
          f"tradeable on an Indian domestic exchange.")
    print("The India-only subset measured Sharpe 0.00 / CAGR -1.8% excluding 2025")
    print("(core/multi_asset.py). This book needs offshore access to be real.")

    if args.json:
        os.makedirs("logs", exist_ok=True)
        with open(SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.now().isoformat(timespec="seconds"),
                       "summary": s, "rows": rows}, f, indent=2)
        print(f"\nwrote {SNAPSHOT}")

    if args.journal:
        n = journal(rows, s)
        print(f"journaled {n} rows -> {JOURNAL}"
              if n else f"\nbar {rows[0]['bar']} already journaled — nothing appended")
        print("mark it forward with: python macro_screen.py --report")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
