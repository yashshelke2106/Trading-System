"""
paper_book.py — forward paper run of the 50/50 swing book.

WHAT THIS IS
------------
A clean, append-only ledger for the NEW system (core.swing_book), started from
a stated notional capital with zero inherited P&L. Its job is to answer one
question honestly over the next month:

    do the MECHANICS work end to end — sizing, entry, exit, accounting?

WHAT IT CANNOT ANSWER, AND WHY THAT IS NOT A DEFECT
---------------------------------------------------
It cannot tell you whether the options sleeve has an edge. H-014 measured the
condor at OOS +2.2% per trade with SD 33.1%; reaching t=2 needs ~907 trades,
which is 76 years at monthly cadence. A one-month result is n≈1 on the options
sleeve. Any P&L number it produces is dominated by noise.

So this ledger deliberately reports CONFIDENCE alongside P&L, and refuses to
express a month of data as a verdict. A green month is not evidence the system
works; a red month is not evidence it is broken. The thing being tested is
whether the machinery runs, not whether it wins.

THE LEGACY JOURNAL IS ARCHIVED, NOT DELETED
-------------------------------------------
logs/signal_journal.jsonl (1,085 resolved trades) is the evidence base that
established option BUYING at PF 0.44 — the finding that forced this system to
sell premium instead of buying it. It is copied to logs/archive_<ts>/ before
any reset. Never delete it: it is the reason the current design exists.

RUN
---
    python -m core.paper_book --start --capital 100000
    python -m core.paper_book --mark            # mark positions to market
    python -m core.paper_book --status
"""
from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import warnings
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ssl._create_default_https_context = ssl._create_unverified_context

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(_ROOT, "logs", "paper_book_state.json")
LEDGER_PATH = os.path.join(_ROOT, "logs", "paper_book_ledger.jsonl")

# Below this many resolved options trades, no P&L statement about the OPTIONS
# sleeve is meaningful. Derived from H-014: SD/mean = 33.1/2.2 => ~907 trades
# for t=2. Anything short of that is mechanics-testing, and the report says so.
MEANINGFUL_OPTION_TRADES = 907


@dataclass
class Position:
    kind: str                 # "equity" | "condor"
    opened: str
    qty: float
    entry: float              # equity: price/unit. condor: credit received/lot
    detail: Dict = field(default_factory=dict)
    closed: Optional[str] = None
    exit: Optional[float] = None
    pnl: Optional[float] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> Dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(state: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)


def _append(entry: Dict) -> None:
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({**entry, "ts": _now()}, default=str) + "\n")


# ── Start ───────────────────────────────────────────────────────────────────

def start(capital: float, force: bool = False) -> Dict:
    """Open a fresh paper book at `capital`. Refuses to clobber a live run."""
    existing = _load()
    if existing and not force:
        return {"ok": False,
                "reason": (f"a paper book is already open "
                           f"(started {existing.get('started')}, "
                           f"capital Rs{existing.get('capital', 0):,.0f}). "
                           f"Use --force to replace it.")}

    from core.swing_book import plan as book_plan
    p = book_plan(capital)

    state = {
        "started": _now(),
        "capital": capital,
        "cash": capital,
        "positions": [],
        "realised_pnl": 0.0,
        "plan_at_start": p.to_dict(),
        "note": ("Paper run of the 50/50 swing book. Tests MECHANICS, not "
                 "edge — a month is n~1 on the options sleeve."),
    }
    _save(state)
    _append({"event": "book_started", "capital": capital})
    return {"ok": True, "state": state, "plan": p.to_dict()}


# ── Entry ─────────────────────────────────────────────────────────────────

def enter(kind: str, qty: float, entry: float, detail: Optional[Dict] = None) -> Dict:
    """Log ONE position into the open book. kind: 'equity' | 'condor'.

    For equity, `entry` is price/unit and cash is reduced by qty*entry.
    For a condor, `entry` is the net credit RECEIVED per lot; cash rises by the
    credit and the capital-at-risk (max loss) is tracked in detail for honest
    exposure accounting.
    """
    state = _load()
    if not state:
        return {"ok": False, "reason": "no paper book open — run --start first"}
    detail = detail or {}

    pos = {"kind": kind, "opened": _now(), "qty": float(qty),
           "entry": float(entry), "detail": detail,
           "closed": None, "exit": None, "pnl": None}

    if kind == "equity":
        cost = qty * entry
        if cost > state.get("cash", 0) + 1e-6:
            return {"ok": False, "reason": (f"insufficient cash: need "
                    f"Rs{cost:,.0f}, have Rs{state.get('cash',0):,.0f}")}
        state["cash"] -= cost
    elif kind == "condor":
        credit = qty * entry                 # entry = credit points * lot value
        state["cash"] = state.get("cash", 0) + credit
    else:
        return {"ok": False, "reason": f"unknown kind '{kind}'"}

    state.setdefault("positions", []).append(pos)
    _save(state)
    _append({"event": "enter", "kind": kind, "qty": qty, "entry": entry,
             "detail": detail})
    return {"ok": True, "position": pos, "cash": state["cash"]}


def enter_from_plan(capital: Optional[float] = None,
                    equity: bool = True, condor_lots: int = 0) -> Dict:
    """Log the entries the swing_book currently prescribes — the honest way to
    start the proof from what the system actually says to hold today.

    equity: log the full equity-sleeve holding at the live price.
    condor_lots: log THIS MANY condor lots now (the ladder is entered in
      tranches over days, so you call this once per tranche, not all at once).
    """
    state = _load()
    if not state:
        return {"ok": False, "reason": "no paper book open — run --start first"}
    cap = capital or state.get("capital", 100000.0)

    from core.swing_book import plan as book_plan
    p = book_plan(cap)
    logged = []

    if equity:
        eq = p.equity
        if eq.get("fundable") and eq.get("units", 0) >= 1:
            # Skip if an equity position is already open (avoid double-entry).
            if not any(x["kind"] == "equity" and not x.get("closed")
                       for x in state.get("positions", [])):
                r = enter("equity", eq["units"], eq["price"],
                          {"instrument": "NIFTYBEES",
                           "overlay_state": eq.get("overlay_state")})
                if r.get("ok"):
                    logged.append(f"equity: {eq['units']} NIFTYBEES @ {eq['price']}")
                state = _load()

    if condor_lots > 0:
        op = p.options
        if op.get("fundable"):
            c = op["chosen"]
            credit_per_lot = c["credit_points"] * op["lot_size"]
            max_loss_per_lot = c["max_loss_per_lot"]
            r = enter("condor", condor_lots, credit_per_lot,
                      {"wing_points": c["wing_points"], "lot_size": op["lot_size"],
                       "max_loss_per_lot": max_loss_per_lot,
                       "capital_at_risk": max_loss_per_lot * condor_lots,
                       "spot": op.get("spot"), "vix": op.get("vix"),
                       "expiry_dte": op.get("dte")})
            if r.get("ok"):
                logged.append(f"condor: {condor_lots} lot(s) {c['wing_points']}pt "
                              f"wings, credit Rs{credit_per_lot*condor_lots:,.0f}, "
                              f"risk Rs{max_loss_per_lot*condor_lots:,.0f}")

    return {"ok": True, "logged": logged or ["nothing new to enter"],
            "cash": _load().get("cash")}


# ── Marking ─────────────────────────────────────────────────────────────────

def _nifty_bees() -> Optional[float]:
    try:
        import yfinance as yf
        d = yf.Ticker("NIFTYBEES.NS").history(period="5d", interval="1d")
        if d is not None and len(d):
            return float(d["Close"].iloc[-1])
    except Exception:
        pass
    return None


def mark() -> Dict:
    """Mark open positions to market. Equity marks live; condors mark at
    credit until expiry (a mid-life condor mark needs an option chain we do
    not have — reported as UNMARKED rather than guessed)."""
    state = _load()
    if not state:
        return {"ok": False, "reason": "no paper book open — use --start"}

    px = _nifty_bees()
    equity_mtm = 0.0
    equity_cost = 0.0
    unmarked = 0

    for pos in state.get("positions", []):
        if pos.get("closed"):
            continue
        if pos["kind"] == "equity" and px:
            equity_cost += pos["qty"] * pos["entry"]
            equity_mtm += pos["qty"] * px
        elif pos["kind"] == "condor":
            unmarked += 1

    state["marked_at"] = _now()
    state["equity_mtm"] = round(equity_mtm, 2)
    state["equity_cost"] = round(equity_cost, 2)
    state["equity_unrealised"] = round(equity_mtm - equity_cost, 2)
    state["condors_unmarked"] = unmarked
    _save(state)
    _append({"event": "marked", "equity_unrealised": state["equity_unrealised"],
             "condors_unmarked": unmarked})
    return {"ok": True, "state": state}


# ── Status ──────────────────────────────────────────────────────────────────

def status() -> Dict:
    state = _load()
    if not state:
        return {"ok": False, "reason": "no paper book open — use --start"}

    opened = state.get("started")
    days = None
    try:
        days = (datetime.now(timezone.utc)
                - datetime.fromisoformat(opened)).days
    except Exception:
        pass

    closed_options = sum(1 for p in state.get("positions", [])
                         if p["kind"] == "condor" and p.get("closed"))
    total = (state.get("realised_pnl", 0.0)
             + state.get("equity_unrealised", 0.0))

    return {
        "ok": True,
        "capital": state.get("capital"),
        "started": opened,
        "days_running": days,
        "realised_pnl": round(state.get("realised_pnl", 0.0), 2),
        "equity_unrealised": state.get("equity_unrealised"),
        "total_pnl": round(total, 2),
        "return_pct": round(total / state["capital"] * 100, 3)
        if state.get("capital") else None,
        "options_trades_closed": closed_options,
        "options_trades_for_significance": MEANINGFUL_OPTION_TRADES,
        "confidence": _confidence(closed_options),
        "open_positions": sum(1 for p in state.get("positions", [])
                              if not p.get("closed")),
    }


def _confidence(n_options: int) -> str:
    """Never let a small sample read as a verdict."""
    if n_options == 0:
        return ("NO OPTIONS EVIDENCE YET — P&L so far is equity beta plus "
                "noise. Nothing here says the system works or fails.")
    if n_options < 30:
        return (f"MECHANICS ONLY (n={n_options}). Far below the ~{MEANINGFUL_OPTION_TRADES} "
                f"trades needed for statistical significance. Treat P&L as a "
                f"plumbing check, not a result.")
    if n_options < MEANINGFUL_OPTION_TRADES:
        return (f"STILL INDICATIVE ONLY (n={n_options} of ~{MEANINGFUL_OPTION_TRADES}). "
                f"Direction may be informative; magnitude is not.")
    return f"statistically meaningful sample reached (n={n_options})"


def _print_status(s: Dict) -> None:
    if not s.get("ok"):
        print(s.get("reason"))
        return
    print(f"\n=== PAPER BOOK — Rs{s['capital']:,.0f} notional ===")
    print(f"  started        : {s['started'][:10]}  ({s['days_running']} days)")
    print(f"  realised P&L   : Rs{s['realised_pnl']:,.2f}")
    if s.get("equity_unrealised") is not None:
        print(f"  equity unreal. : Rs{s['equity_unrealised']:,.2f}")
    print(f"  TOTAL P&L      : Rs{s['total_pnl']:,.2f}  ({s['return_pct']:+.3f}%)")
    print(f"  open positions : {s['open_positions']}")
    print(f"  options closed : {s['options_trades_closed']}")
    print(f"\n  CONFIDENCE: {s['confidence']}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Paper run of the 50/50 swing book.")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--force", action="store_true",
                    help="replace an existing open paper book")
    ap.add_argument("--mark", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--enter-equity", action="store_true",
                    help="log the equity-sleeve holding the book prescribes now")
    ap.add_argument("--enter-condor", type=int, metavar="LOTS", default=0,
                    help="log THIS MANY condor lots now (one ladder tranche)")
    args = ap.parse_args()

    if args.start:
        r = start(args.capital, force=args.force)
        if not r.get("ok"):
            print(r["reason"])
            return 1
        print(f"paper book opened at Rs{args.capital:,.0f}")
        _print_status(status())
        return 0
    if args.enter_equity or args.enter_condor:
        r = enter_from_plan(equity=args.enter_equity,
                            condor_lots=args.enter_condor)
        if not r.get("ok"):
            print(r["reason"])
            return 1
        for line in r["logged"]:
            print(f"  logged: {line}")
        print(f"  cash now: Rs{r.get('cash', 0):,.2f}")
        return 0
    if args.mark:
        r = mark()
        print(r.get("reason") if not r.get("ok") else "marked to market")
        return 0 if r.get("ok") else 1
    if args.status:
        _print_status(status())
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
