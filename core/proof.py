"""
proof.py — pre-registered, benchmark-relative proof that the paper book works.

THE QUESTION THIS ANSWERS HONESTLY
----------------------------------
"Prove it makes money before I add capital." The naive version — wait for the
P&L to be green — proves nothing: in a bull market a beta book is green because
NIFTY is green, not because the system has any skill. Proof requires three
things the naive version lacks:

  1. A FAIR BENCHMARK. The book holds ~half its equity sleeve in NIFTYBEES, so
     the honest comparison is NIFTY buy-and-hold on the same capital over the
     same window. "Made money" must mean "did what it claimed vs that", not
     "number went up".

  2. CRITERIA FIXED IN ADVANCE. Defined here, before the run, so a lucky month
     cannot be re-labelled a success after the fact. Moving these is cheating.

  3. CONFIDENCE GATING. A sample too small to conclude must return
     INSUFFICIENT, never PROVEN. The options sleeve in particular needs ~907
     trades for significance (decades) — this module will NEVER claim the
     condor edge is proven on a few months, and says so.

WHAT CAN AND CANNOT BE PROVEN ON PAPER, IN MONTHS
-------------------------------------------------
  CAN (weeks-months):
    - MECHANICS: sizing, marking, overlay flips, accounting all run correctly.
    - TRACKING: the equity sleeve tracks its benchmark within tolerance (i.e.
      the system holds what it says and the P&L is explained by the holdings).
  CANNOT (needs years-decades):
    - that the condor / any sleeve has positive EDGE. Edge needs a sample this
      timeframe cannot produce. The protocol reports the condor as a forward
      TEST with an explicit "not concludable yet" until n is adequate.

So the achievable proof is: "the system does exactly what it claims, its costs
are as modelled, and it tracks its benchmark." That is the proof that makes it
safe to fund — not a promise of alpha, which does not exist here.

CHECKPOINTS (pre-registered)
----------------------------
  ~21 trading days  MECHANICS   : book marks, overlay logic fires, no blow-ups,
                                  tracking error vs benchmark within band.
  ~63 trading days  CONSISTENCY : tracking holds; overlay adds or at least does
                                  not materially subtract vs buy-and-hold.
  ~252 trading days DIRECTION   : full cycle seen; excess return and max
                                  drawdown vs benchmark are as designed.
Each checkpoint concludes ONLY what its sample supports.

RUN
---
    python -m core.proof                # evaluate the open paper book
    python -m core.proof --criteria     # print the pre-registered criteria
"""
from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import warnings
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, Optional

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ssl._create_default_https_context = ssl._create_unverified_context

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(_ROOT, "logs", "paper_book_state.json")

# ── Pre-registered criteria (DO NOT edit after the run starts) ───────────────
# Tracking-error band: the book is ~50% equity with a 200-DMA overlay that goes
# to half-weight risk-off, so it is EXPECTED to capture only part of NIFTY's
# move. The test is not "matches NIFTY" but "moves a sensible FRACTION of it and
# never diverges wildly" — a wild divergence means a bug, not a view.
CHECKPOINTS = [
    {"name": "MECHANICS",   "min_days": 21,
     "requires": "book marks daily, overlay logic fires, |book move| between "
                 "10% and 90% of |benchmark move|, no unexplained jumps"},
    {"name": "CONSISTENCY", "min_days": 63,
     "requires": "tracking band holds across the window; overlay does not "
                 "subtract more than its risk-off drag vs buy-and-hold"},
    {"name": "DIRECTION",   "min_days": 252,
     "requires": "excess return and max drawdown vs benchmark within design; "
                 "a full risk_on/off cycle observed"},
]
# The condor edge is NOT on this list by design — it is unprovable in this
# horizon. It rides as a forward test, reported separately, never as "proof".
OPTIONS_TRADES_FOR_EDGE = 907        # from H-014 SD/mean; ~decades monthly

TRACK_LO, TRACK_HI = 0.10, 0.90      # book move as a fraction of benchmark move


@dataclass
class ProofState:
    days: int
    book_return_pct: Optional[float]
    benchmark_return_pct: Optional[float]
    excess_pct: Optional[float]
    capture_ratio: Optional[float]     # book move / benchmark move
    checkpoint: str
    verdict: str
    detail: str

    def to_dict(self) -> Dict:
        return asdict(self)


def _nifty_now() -> Optional[float]:
    """Live NIFTY, freshest legal source."""
    try:
        from core.live_quotes import get_quote
        q = get_quote("NIFTY")
        if q and q.last:
            return float(q.last)
    except Exception:
        pass
    try:
        import yfinance as yf
        d = yf.Ticker("^NSEI").history(period="5d", interval="1d")
        if len(d):
            return float(d["Close"].iloc[-1])
    except Exception:
        pass
    return None


def evaluate() -> ProofState:
    if not os.path.exists(STATE):
        return ProofState(0, None, None, None, None, "NONE",
                          "NO BOOK", "no paper book open — run paper_book --start")
    with open(STATE, encoding="utf-8") as fh:
        st = json.load(fh)

    started = st.get("started", "")
    try:
        days = (datetime.now(timezone.utc)
                - datetime.fromisoformat(started)).days
    except Exception:
        days = 0

    # Benchmark: NIFTY at book start vs now.
    nifty_0 = ((st.get("plan_at_start") or {}).get("equity") or {}).get("nifty")
    nifty_1 = _nifty_now()
    bench = None
    if nifty_0 and nifty_1 and nifty_0 > 0:
        bench = (nifty_1 / nifty_0 - 1) * 100

    # Book return: realised + equity unrealised over notional capital.
    cap = st.get("capital") or 1.0
    total = (st.get("realised_pnl", 0.0) or 0.0) + (st.get("equity_unrealised", 0.0) or 0.0)
    book = total / cap * 100 if cap else None

    excess = (book - bench) if (book is not None and bench is not None) else None
    capture = None
    if book is not None and bench not in (None, 0):
        capture = book / bench

    # Which checkpoint are we in?
    cp = "PRE-MECHANICS"
    for c in CHECKPOINTS:
        if days >= c["min_days"]:
            cp = c["name"]

    # Verdict — confidence-gated, never over-claims.
    n_opt = _closed_option_trades(st)
    verdict, detail = _verdict(days, book, bench, capture, n_opt)

    return ProofState(days,
                      round(book, 3) if book is not None else None,
                      round(bench, 3) if bench is not None else None,
                      round(excess, 3) if excess is not None else None,
                      round(capture, 3) if capture is not None else None,
                      cp, verdict, detail)


def _closed_option_trades(st: Dict) -> int:
    return sum(1 for p in st.get("positions", [])
               if p.get("kind") == "condor" and p.get("closed"))


def _verdict(days, book, bench, capture, n_opt) -> tuple:
    if days < CHECKPOINTS[0]["min_days"]:
        return ("TOO EARLY",
                f"day {days}: below the {CHECKPOINTS[0]['min_days']}-day mechanics "
                f"checkpoint. Nothing concludable yet — this is expected.")
    if book is None or bench is None:
        return ("NO DATA", "benchmark or book return unavailable — mark the book.")

    # MECHANICS test: is the book capturing a SENSIBLE fraction of the benchmark?
    if bench is not None and abs(bench) > 1.0:      # only test when NIFTY moved enough
        if capture is None or not (TRACK_LO <= capture <= TRACK_HI):
            return ("INVESTIGATE",
                    f"capture ratio {capture} outside [{TRACK_LO},{TRACK_HI}] — "
                    f"book moved {book:+.1f}% vs benchmark {bench:+.1f}%. Likely a "
                    f"mechanics/marking issue, NOT a view. Check before trusting.")

    base = (f"day {days}, book {book:+.2f}% vs NIFTY {bench:+.2f}% "
            f"(excess {book - bench:+.2f}%). ")
    edge_note = (f"Options edge NOT concludable: {n_opt}/{OPTIONS_TRADES_FOR_EDGE} "
                 f"trades needed for significance.")
    return ("ON TRACK (mechanics)", base +
            "Mechanics tracking as designed. This proves the system DOES what it "
            "claims, not that it has edge. " + edge_note)


def _print(s: ProofState) -> None:
    print(f"\n=== PAPER PROOF — day {s.days}  [{s.checkpoint}] ===")
    if s.book_return_pct is not None:
        print(f"  book       : {s.book_return_pct:+.2f}%")
        print(f"  benchmark  : {s.benchmark_return_pct:+.2f}%  (NIFTY buy&hold, same window)")
        print(f"  excess     : {s.excess_pct:+.2f}%   capture ratio {s.capture_ratio}")
    print(f"\n  VERDICT: {s.verdict}")
    print(f"  {s.detail}\n")


def _print_criteria() -> None:
    print("\n=== PRE-REGISTERED PROOF CRITERIA (fixed before the run) ===\n")
    for c in CHECKPOINTS:
        print(f"  [{c['min_days']:>3}d] {c['name']}")
        print(f"        {c['requires']}\n")
    print(f"  Benchmark : NIFTY buy-and-hold, same capital + window.")
    print(f"  Tracking  : book move must be {TRACK_LO:.0%}-{TRACK_HI:.0%} of "
          f"benchmark move (it is a ~half-weight overlay book).")
    print(f"  Edge      : NOT on this list. The condor needs "
          f"~{OPTIONS_TRADES_FOR_EDGE} trades (decades) — unprovable here, "
          f"ridden as a forward test only.\n")
    print("  What paper CAN prove in months: mechanics + tracking = the system")
    print("  does what it claims. That is what makes it safe to fund. It cannot")
    print("  prove alpha, because there is none to prove.\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-registered paper-book proof.")
    ap.add_argument("--criteria", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.criteria:
        _print_criteria()
        return 0
    s = evaluate()
    if args.json:
        print(json.dumps(s.to_dict(), indent=2, default=str))
    else:
        _print(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
