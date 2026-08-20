"""
paper_portfolio.py — two funded paper books, run as an actual book.

WHY THIS EXISTS
---------------
The dashboard used to show "Rs per trade = 50,000" and multiply every trade's
return by it. That answers "what would one trade have paid on 50k", which is a
per-trade statistic wearing a rupee sign. It cannot answer the only question
that matters to a trader:

    starting from a pot of money, am I up or down?

It has no pot. Nothing is ever debited, so a book with 98 open positions looks
identical to a book with 3, and a trade the account could never have afforded
counts the same as one it could. This module replaces that with a real ledger:

    capital in  ->  positions cost cash  ->  exits return cash  ->  balance out

TWO SLEEVES, RUN SEPARATELY
---------------------------
    equity   Rs 5,00,000   swing book, sized in rupees per position
    options  Rs 5,00,000   F&O journal, sized in whole lots

They are deliberately NOT pooled. The sleeves trade different instruments with
different cost structures and different failure modes; averaging them hides
which one is bleeding. The combined view is a sum, never a blend.

CAPITAL IS A REAL CONSTRAINT
----------------------------
A signal the sleeve cannot afford is NOT taken, and is counted in
`skipped_no_cash`. This is the honesty that a per-trade multiplier throws away:
if the system fires more signals than the money supports, the book earns what
the money could actually capture, not what every signal would have paid.

COSTS
-----
Equity uses the swing book's own `ret_net`, which already carries its round-trip
(25bp delivery / 10bp futures) -- charging again would double-count. Options
premiums are recorded raw, so a cost model is applied here and reported as a
separate line rather than silently folded into the P&L.

    python -m core.paper_portfolio
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_FILE = os.path.join(_ROOT, "logs", "signal_journal.jsonl")
SWING_FILE = os.path.join(_ROOT, "logs", "swing_paper_journal.jsonl")

# ── Sleeve configuration ─────────────────────────────────────────────────────
EQUITY_CAPITAL = 500_000.0
OPTIONS_CAPITAL = 500_000.0

# Rupees committed per equity position, as a fraction of STARTING capital.
# 10% of 5L = Rs 50,000 -- the size the dashboard already implied, now actually
# drawn from a pot. Fixed against starting capital rather than running equity so
# the sizing rule doesn't quietly compound (or shrink) the test.
EQUITY_POSITION_PCT = 0.10

# Option round trip. Premiums in the journal come from a live quote, so the
# bid/ask is not in them; brokerage and STT are not either.
OPTION_SPREAD_PCT_PER_LEG = 0.005     # 0.5% of premium, each way
OPTION_BROKERAGE_PER_LEG = 20.0       # flat, each way


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass
class BookTrade:
    symbol: str
    direction: str
    entry_date: str
    exit_date: str
    quantity: int
    entry_price: float
    exit_price: float
    cost_basis: float          # cash the position tied up
    pnl: float                 # realised, net of costs
    pnl_pct: float             # on the cost basis
    costs: float
    outcome: str
    instrument: str            # "equity" | "option"
    detail: str = ""           # e.g. "2300 CE x 375"


@dataclass
class Sleeve:
    name: str
    starting_capital: float
    cash: float = 0.0
    realized_pnl: float = 0.0
    total_costs: float = 0.0

    n_signals: int = 0
    n_taken: int = 0
    skipped_no_cash: int = 0
    skipped_no_data: int = 0

    wins: int = 0
    losses: int = 0

    peak_equity: float = 0.0
    max_drawdown_pct: float = 0.0
    peak_deployed: float = 0.0

    curve: List[Dict] = field(default_factory=list)
    trades: List[BookTrade] = field(default_factory=list)

    # ---- derived ----
    @property
    def equity(self) -> float:
        return self.starting_capital + self.realized_pnl

    @property
    def return_pct(self) -> float:
        return self.realized_pnl / self.starting_capital * 100 if self.starting_capital else 0.0

    @property
    def win_rate(self) -> float:
        d = self.wins + self.losses
        return self.wins / d * 100 if d else 0.0

    @property
    def profit_factor(self) -> float:
        gp = sum(t.pnl for t in self.trades if t.pnl > 0)
        gl = -sum(t.pnl for t in self.trades if t.pnl < 0)
        return round(gp / gl, 3) if gl > 0 else 0.0

    def as_dict(self) -> Dict:
        gp = sum(t.pnl for t in self.trades if t.pnl > 0)
        gl = -sum(t.pnl for t in self.trades if t.pnl < 0)
        return {
            "name": self.name,
            "starting_capital": round(self.starting_capital, 2),
            "equity": round(self.equity, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "return_pct": round(self.return_pct, 3),
            "total_costs": round(self.total_costs, 2),
            "n_signals": self.n_signals,
            "n_taken": self.n_taken,
            "skipped_no_cash": self.skipped_no_cash,
            "skipped_no_data": self.skipped_no_data,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 1),
            "profit_factor": self.profit_factor,
            "gross_profit": round(gp, 2),
            "gross_loss": round(gl, 2),
            "avg_win": round(gp / self.wins, 2) if self.wins else 0.0,
            "avg_loss": round(-gl / self.losses, 2) if self.losses else 0.0,
            "avg_trade": round(self.realized_pnl / self.n_taken, 2) if self.n_taken else 0.0,
            "peak_equity": round(self.peak_equity, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "peak_deployed": round(self.peak_deployed, 2),
            "peak_deployed_pct": round(self.peak_deployed / self.starting_capital * 100, 1)
                                 if self.starting_capital else 0.0,
            "curve": self.curve,
            "trades": [asdict(t) for t in self.trades],
        }


# ── Simulation core ──────────────────────────────────────────────────────────

def _run(sleeve: Sleeve, orders: List[Dict]) -> Sleeve:
    """Walk orders in date order, honouring cash.

    `orders` are dicts with: entry_date, exit_date, cost_basis, pnl, costs, and
    the display fields for BookTrade. Cost basis is deducted on entry and
    returned (plus P&L) on exit, so two positions cannot spend the same rupee.
    """
    sleeve.cash = sleeve.starting_capital
    sleeve.peak_equity = sleeve.starting_capital

    events = []
    for i, o in enumerate(orders):
        events.append((o["entry_date"], 1, i, "OPEN"))   # 1 -> opens after closes
        events.append((o["exit_date"], 0, i, "CLOSE"))
    # Same day: free capital before spending it, which is what a real desk does.
    events.sort(key=lambda e: (e[0], e[1]))

    taken: Dict[int, Dict] = {}
    deployed = 0.0

    for when, _, idx, kind in events:
        o = orders[idx]
        if kind == "OPEN":
            sleeve.n_signals += 1
            cost = o["cost_basis"]
            if cost <= 0:
                sleeve.skipped_no_data += 1
                continue
            if cost > sleeve.cash + 1e-9:
                sleeve.skipped_no_cash += 1
                continue
            sleeve.cash -= cost
            deployed += cost
            taken[idx] = o
            sleeve.peak_deployed = max(sleeve.peak_deployed, deployed)
        else:
            if idx not in taken:
                continue                      # never opened -> nothing to close
            cost = o["cost_basis"]
            sleeve.cash += cost + o["pnl"]
            deployed -= cost
            sleeve.realized_pnl += o["pnl"]
            sleeve.total_costs += o["costs"]
            sleeve.n_taken += 1
            if o["pnl"] > 0:
                sleeve.wins += 1
            else:
                sleeve.losses += 1
            sleeve.trades.append(BookTrade(
                symbol=o["symbol"], direction=o["direction"],
                entry_date=o["entry_date"], exit_date=o["exit_date"],
                quantity=o["quantity"], entry_price=o["entry_price"],
                exit_price=o["exit_price"], cost_basis=round(cost, 2),
                pnl=round(o["pnl"], 2),
                pnl_pct=round(o["pnl"] / cost * 100, 3) if cost else 0.0,
                costs=round(o["costs"], 2), outcome=o["outcome"],
                instrument=o["instrument"], detail=o.get("detail", ""),
            ))

        equity = sleeve.starting_capital + sleeve.realized_pnl
        sleeve.peak_equity = max(sleeve.peak_equity, equity)
        dd = (equity - sleeve.peak_equity) / sleeve.peak_equity * 100 if sleeve.peak_equity else 0.0
        sleeve.max_drawdown_pct = min(sleeve.max_drawdown_pct, dd)
        sleeve.curve.append({
            "date": when, "equity": round(equity, 2), "cash": round(sleeve.cash, 2),
            "deployed": round(deployed, 2), "realized": round(sleeve.realized_pnl, 2),
        })

    # n_signals counts OPEN events; skipped_no_data rows never had a cost basis
    return sleeve


def _d(v) -> str:
    """Normalise anything date-ish to YYYY-MM-DD for ordering."""
    if not v:
        return ""
    s = str(v)
    if "T" in s:
        s = s.split("T")[0]
    return s[:10]


# ── Equity sleeve ────────────────────────────────────────────────────────────

def build_equity_orders(resolved: List[Dict], capital: float = EQUITY_CAPITAL,
                        position_pct: float = EQUITY_POSITION_PCT) -> List[Dict]:
    """Swing rows -> orders. Uses the book's own `ret_net`, which already
    carries its round-trip cost; charging again here would double-count."""
    slot = capital * position_pct
    out = []
    for r in resolved:
        entry = r.get("entry_px")
        exit_ = r.get("exit_px")
        ret_net = r.get("ret_net")
        if not entry or entry <= 0 or ret_net is None:
            continue
        qty = int(slot // float(entry))
        if qty < 1:
            continue                       # one share costs more than the slot
        basis = qty * float(entry)
        gross = basis * float(r.get("ret_gross") or ret_net)
        pnl = basis * float(ret_net)
        out.append({
            "symbol": r.get("symbol", ""), "direction": (r.get("direction") or "").upper(),
            "entry_date": _d(r.get("signal_date") or r.get("bar")),
            "exit_date": _d(r.get("exit_date")),
            "quantity": qty, "entry_price": float(entry),
            "exit_price": float(exit_) if exit_ else 0.0,
            "cost_basis": basis, "pnl": pnl, "costs": gross - pnl,
            "outcome": r.get("outcome", ""), "instrument": "equity",
            "detail": f"{qty} sh",
        })
    return out


# ── Options sleeve ───────────────────────────────────────────────────────────

def _lot_for(symbol: str) -> int:
    try:
        from core.signal_tracker import _lot_for as _lf
        return _lf(symbol)
    except Exception:
        return 1


def build_option_orders(rows: List[Dict], lots: int = 1) -> List[Dict]:
    """Journal rows -> orders. One lot per signal by default; premiums are raw,
    so spread and brokerage are applied here and reported separately."""
    out = []
    for r in rows:
        if not r.get("outcome"):
            continue
        ep, xp = r.get("entry_prem"), r.get("exit_prem")
        if ep is None or xp is None:
            continue
        sym = str(r.get("symbol") or "").upper()
        lot = _lot_for(sym)
        if lot <= 1:
            continue                       # contract size unknown -> not sizeable
        qty = lot * lots
        basis = float(ep) * qty
        gross = (float(xp) - float(ep)) * qty
        costs = (float(ep) + float(xp)) * qty * OPTION_SPREAD_PCT_PER_LEG \
                + 2 * OPTION_BROKERAGE_PER_LEG
        strike, otype = r.get("option_strike"), r.get("option_type") or ""
        out.append({
            "symbol": sym, "direction": (r.get("direction") or "").upper(),
            "entry_date": _d(r.get("ts")), "exit_date": _d(r.get("exit_ts")),
            "quantity": qty, "entry_price": float(ep), "exit_price": float(xp),
            "cost_basis": basis, "pnl": gross - costs, "costs": costs,
            "outcome": str(r.get("outcome")), "instrument": "option",
            "detail": f"{strike:g} {otype} x{qty}" if strike else f"x{qty}",
        })
    return out


# ── Loaders ──────────────────────────────────────────────────────────────────

def _load_journal() -> List[Dict]:
    if not os.path.exists(JOURNAL_FILE):
        return []
    rows = []
    with open(JOURNAL_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def _load_swing_resolved() -> List[Dict]:
    """Resolved rows from the swing paper journal — the same file /api/swing
    reads, so the Portfolio tab and the Overview tab describe one book."""
    if not os.path.exists(SWING_FILE):
        return []
    rows = []
    with open(SWING_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("status") == "resolved":
                rows.append(r)
    return rows


# ── Public entry point ───────────────────────────────────────────────────────

def build_portfolios(equity_capital: float = EQUITY_CAPITAL,
                     options_capital: float = OPTIONS_CAPITAL,
                     position_pct: float = EQUITY_POSITION_PCT,
                     swing_resolved: Optional[List[Dict]] = None,
                     journal_rows: Optional[List[Dict]] = None) -> Dict:
    """Both sleeves plus a combined roll-up. The combined figure is a SUM of two
    independent books, never a blended return."""
    if swing_resolved is None:
        swing_resolved = _load_swing_resolved()
    if journal_rows is None:
        journal_rows = _load_journal()

    eq = _run(Sleeve("equity", equity_capital),
              build_equity_orders(swing_resolved, equity_capital, position_pct))
    op = _run(Sleeve("options", options_capital),
              build_option_orders(journal_rows))

    total_start = equity_capital + options_capital
    total_pnl = eq.realized_pnl + op.realized_pnl
    return {
        "equity": eq.as_dict(),
        "options": op.as_dict(),
        "combined": {
            "starting_capital": round(total_start, 2),
            "equity": round(total_start + total_pnl, 2),
            "realized_pnl": round(total_pnl, 2),
            "return_pct": round(total_pnl / total_start * 100, 3) if total_start else 0.0,
            "n_taken": eq.n_taken + op.n_taken,
            "skipped_no_cash": eq.skipped_no_cash + op.skipped_no_cash,
            "total_costs": round(eq.total_costs + op.total_costs, 2),
        },
        "config": {
            "equity_capital": equity_capital,
            "options_capital": options_capital,
            "equity_position_pct": position_pct,
            "equity_slot_rupees": round(equity_capital * position_pct, 2),
            "option_lots_per_signal": 1,
            "option_spread_pct_per_leg": OPTION_SPREAD_PCT_PER_LEG,
            "option_brokerage_per_leg": OPTION_BROKERAGE_PER_LEG,
            "equity_costs_note": "ret_net already includes the swing book's round trip",
        },
    }


if __name__ == "__main__":
    p = build_portfolios()
    for k in ("equity", "options"):
        s = p[k]
        print(f"\n=== {k.upper()} SLEEVE ===")
        print(f"  start Rs {s['starting_capital']:>12,.0f}   ->   now Rs {s['equity']:>12,.0f}"
              f"   ({s['return_pct']:+.2f}%)")
        print(f"  realised P&L      Rs {s['realized_pnl']:>12,.0f}")
        print(f"  costs paid        Rs {s['total_costs']:>12,.0f}")
        print(f"  trades taken      {s['n_taken']:>5d}   skipped (no cash) {s['skipped_no_cash']}")
        print(f"  win rate          {s['win_rate']:>5.1f}%   PF {s['profit_factor']}")
        print(f"  max drawdown      {s['max_drawdown_pct']:>5.2f}%")
        print(f"  peak deployed     Rs {s['peak_deployed']:>12,.0f} "
              f"({s['peak_deployed_pct']}% of sleeve)")
    c = p["combined"]
    print(f"\n=== COMBINED ===")
    print(f"  Rs {c['starting_capital']:,.0f} -> Rs {c['equity']:,.0f}  ({c['return_pct']:+.2f}%)")
