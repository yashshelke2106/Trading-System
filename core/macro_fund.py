"""core/macro_fund.py — the paper hedge fund book (H-022 macro sleeve).

This is the fund, not the screen. macro_screen.py answers "what does the model
want to hold"; this answers "starting from a pot of capital, am I up or down".
Same distinction the equity side draws between /api/signals and /api/account.

WHY THE LEDGER MATH DIFFERS FROM core/trading_account.py

The equity/options book pays cash for a position: you debit the account, hold
the lot, credit it back on exit, and every rupee is either in cash or in a
position. A managed-futures book does not work that way. It holds NOTIONAL
exposure financed by margin, so gross exposure routinely exceeds NAV (this
book runs up to 3x) while the cash never leaves. Enforcing
`cash + committed == capital` here would be wrong, not safe.

The right invariant for a notional book is therefore:

        NAV == capital + realised_pnl + unrealised_pnl

with realised_pnl already NET of costs. It is asserted on every write and it
RAISES, for the same reason the equity book's does: a P&L number from a
leaking ledger is worthless, and every prior money-view in this repo failed by
reporting one anyway.

HOW A CYCLE WORKS

  mark      — every open position is re-priced daily; unrealised moves, NAV
              moves, nothing is booked.
  rebalance — on the first business day of a month: mark, REALISE everything,
              charge cost on turnover notional, then re-establish at the new
              target weights. This mirrors the backtest exactly (monthly
              rebalance, cost on turnover) rather than approximating it.

Positions are sized from the vol-targeted weights in core.multi_asset, on the
ETF universe the fund would really trade (FUND_UNIVERSE), so signal and
execution share an instrument and no tracking error is assumed away.

PAPER ONLY. Nothing here places an order, and there is no broker binding.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Dict, List, Optional

STATE_PATH = os.path.join("logs", "macro_fund_state.json")
LEDGER_PATH = os.path.join("logs", "macro_fund_ledger.jsonl")

# USD. The measured floor for the 18-sleeve book: below ~USD 25k position
# granularity breaks down, and fixed commissions eat the diversification
# benefit (see the feasibility note, 2026-09-05).
DEFAULT_CAPITAL = 100_000.0
COST_BPS = 10.0          # round trip charged on turnover notional


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> date:
    return date.today()


def _f(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if f == f else default          # NaN guard
    except (TypeError, ValueError):
        return default


# ── state ────────────────────────────────────────────────────────────────
def _default_state(capital: float) -> Dict:
    return {
        "started": _now(),
        "mode": "PAPER",
        "base_currency": "USD",
        "capital": float(capital),
        "realised_pnl": 0.0,
        "unrealised_pnl": 0.0,
        "total_costs": 0.0,
        "nav": float(capital),
        "positions": [],          # open notional exposures
        "closed": [],             # realised legs, most recent first
        "curve": [{"date": str(_today()), "nav": float(capital),
                   "gross": 0.0, "net": 0.0}],
        "last_mark": None,
        "last_rebalance": None,
        "hypothesis": "H-022",
        "note": ("Paper macro fund. Notional book financed by margin, so gross "
                 "exposure exceeds NAV by design; the invariant is "
                 "NAV == capital + realised + unrealised, not a cash ledger."),
    }


def load_state() -> Dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def check_invariant(state: Dict) -> None:
    """NAV == capital + realised + unrealised. Raises, never warns."""
    lhs = _f(state.get("nav"))
    rhs = (_f(state.get("capital")) + _f(state.get("realised_pnl"))
           + _f(state.get("unrealised_pnl")))
    if abs(lhs - rhs) > 0.01:
        raise AssertionError(
            f"macro fund ledger broken: nav={lhs:.2f} vs "
            f"capital+realised+unrealised={rhs:.2f} (diff {lhs - rhs:.4f})")


def save_state(state: Dict) -> None:
    check_invariant(state)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)
    os.replace(tmp, STATE_PATH)


def append_ledger(entry: Dict) -> None:
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _now(), **entry}, default=str) + "\n")


def reset(capital: float = DEFAULT_CAPITAL) -> Dict:
    """Archive any existing book and start a fresh one."""
    if os.path.exists(STATE_PATH):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.replace(STATE_PATH, f"{STATE_PATH}.archived_{stamp}")
    state = _default_state(capital)
    save_state(state)
    append_ledger({"event": "reset", "capital": capital})
    return state


# ── valuation ────────────────────────────────────────────────────────────
def position_pnl(p: Dict, price: float) -> float:
    """Mark-to-market of one notional leg. units is signed."""
    return _f(p.get("units")) * (price - _f(p.get("entry_price")))


def mark(state: Dict, prices: Dict[str, float]) -> Dict:
    """Re-price every open leg. Moves unrealised and NAV; books nothing."""
    unreal = 0.0
    for p in state.get("positions", []):
        px = prices.get(p["ticker"])
        if px is None or not _f(px):
            continue                                  # stale: keep last mark
        p["last_price"] = round(float(px), 6)
        p["pnl"] = round(position_pnl(p, float(px)), 2)
        p["market_value"] = round(_f(p.get("units")) * float(px), 2)
        unreal += p["pnl"]
    state["unrealised_pnl"] = round(unreal, 2)
    state["nav"] = round(_f(state["capital"]) + _f(state["realised_pnl"])
                         + unreal, 2)
    state["last_mark"] = str(_today())
    return state


def exposure(state: Dict) -> Dict:
    """Gross / net / per-class exposure as a multiple of NAV."""
    nav = _f(state.get("nav")) or 1.0
    gross = net = 0.0
    by_class: Dict[str, float] = {}
    for p in state.get("positions", []):
        mv = _f(p.get("market_value"))
        gross += abs(mv)
        net += mv
        by_class[p["asset_class"]] = by_class.get(p["asset_class"], 0.0) + mv
    return {
        "gross": round(gross / nav, 3),
        "net": round(net / nav, 3),
        "gross_usd": round(gross, 2),
        "net_usd": round(net, 2),
        "by_class": {k: round(v / nav, 3) for k, v in sorted(by_class.items())},
        "n_long": sum(1 for p in state.get("positions", []) if _f(p.get("units")) > 0),
        "n_short": sum(1 for p in state.get("positions", []) if _f(p.get("units")) < 0),
    }


# ── rebalance ────────────────────────────────────────────────────────────
def is_rebalance_day(state: Dict, today: Optional[date] = None) -> bool:
    """Monthly. True when we have not yet rebalanced in the current month."""
    today = today or _today()
    last = state.get("last_rebalance")
    if not last:
        return True                       # never rebalanced — not an error path
    try:
        d = date.fromisoformat(str(last)[:10])
    except ValueError as e:
        # Fail CLOSED. Treating a corrupt stamp as "due" would rebalance on
        # every single cycle and pay turnover cost each time — a slow money
        # leak that looks like normal operation. Surface it instead.
        raise ValueError(
            f"last_rebalance is corrupt ({last!r}); refusing to guess. "
            "Fix the book or start a fresh one with "
            "`python macro_task.py --reset`.") from e
    return (d.year, d.month) != (today.year, today.month)


def rebalance(state: Dict, targets: List[Dict],
              prices: Dict[str, float]) -> Dict:
    """Realise every open leg, charge costs on turnover, re-establish.

    `targets` is the output of core.multi_asset.signals(): rows carrying
    ticker / name / asset_class / weight. Weights are fractions of NAV.
    """
    state = mark(state, prices)
    nav_before = _f(state["nav"])
    today = str(_today())

    # 1. realise everything currently open
    closed_notional = 0.0
    for p in state.get("positions", []):
        px = prices.get(p["ticker"])
        if px is None or not _f(px):
            continue
        pnl = position_pnl(p, float(px))
        closed_notional += abs(_f(p.get("units")) * float(px))
        rec = {**p, "exit_price": round(float(px), 6), "exit_date": today,
               "realised_pnl": round(pnl, 2)}
        state.setdefault("closed", []).insert(0, rec)
        state["realised_pnl"] = round(_f(state["realised_pnl"]) + pnl, 2)
    del state["closed"][400:]                      # keep the file bounded
    state["positions"] = []
    state["unrealised_pnl"] = 0.0
    state["nav"] = round(_f(state["capital"]) + _f(state["realised_pnl"]), 2)

    # 2. open the new book at current prices
    nav = _f(state["nav"])
    opened_notional = 0.0
    for t in targets:
        w = _f(t.get("weight"))
        px = prices.get(t["ticker"])
        if not w or px is None or not _f(px):
            continue
        notional = w * nav
        units = notional / float(px)
        opened_notional += abs(notional)
        state["positions"].append({
            "ticker": t["ticker"], "name": t["name"],
            "asset_class": t["asset_class"],
            "direction": "long" if units > 0 else "short",
            "weight": round(w, 4),
            "units": round(units, 6),
            "entry_price": round(float(px), 6),
            "entry_date": today,
            "last_price": round(float(px), 6),
            "market_value": round(notional, 2),
            "pnl": 0.0,
        })

    # 3. cost on turnover — both sides of the switch pay
    turnover = closed_notional + opened_notional
    cost = turnover * COST_BPS / 1e4
    state["realised_pnl"] = round(_f(state["realised_pnl"]) - cost, 2)
    state["total_costs"] = round(_f(state["total_costs"]) + cost, 2)
    state["nav"] = round(_f(state["capital"]) + _f(state["realised_pnl"]), 2)

    # positions were sized off the pre-cost NAV; re-mark so the book is
    # internally consistent before the invariant is checked
    state = mark(state, prices)
    state["last_rebalance"] = today

    exp = exposure(state)
    append_ledger({"event": "rebalance", "nav_before": round(nav_before, 2),
                   "nav_after": _f(state["nav"]), "turnover": round(turnover, 2),
                   "cost": round(cost, 2), "n_positions": len(state["positions"]),
                   "gross": exp["gross"], "net": exp["net"]})
    return state


def record_curve(state: Dict) -> Dict:
    """Append today's NAV point, replacing an existing one for the same day."""
    exp = exposure(state)
    pt = {"date": str(_today()), "nav": _f(state["nav"]),
          "gross": exp["gross"], "net": exp["net"]}
    curve = [c for c in state.get("curve", []) if c.get("date") != pt["date"]]
    curve.append(pt)
    state["curve"] = curve[-2000:]
    return state


# ── reporting ────────────────────────────────────────────────────────────
def performance(state: Dict) -> Dict:
    """Headline numbers. Returns are on capital, not time-weighted — this is a
    single-pot book with no external flows, so the two coincide."""
    import math

    cap = _f(state.get("capital")) or 1.0
    nav = _f(state.get("nav"))
    curve = state.get("curve", [])
    navs = [_f(c.get("nav")) for c in curve if _f(c.get("nav"))]

    peak, maxdd = 0.0, 0.0
    for v in navs:
        peak = max(peak, v)
        if peak:
            maxdd = min(maxdd, v / peak - 1)

    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))
            if navs[i - 1]]
    sharpe = 0.0
    if len(rets) > 20:
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var)
        if sd:
            sharpe = m / sd * math.sqrt(252)

    days = 0
    if len(curve) > 1:
        try:
            days = (date.fromisoformat(curve[-1]["date"])
                    - date.fromisoformat(curve[0]["date"])).days
        except Exception:
            days = 0

    return {
        "nav": round(nav, 2),
        "capital": round(cap, 2),
        "total_return_pct": round((nav / cap - 1) * 100, 3),
        "realised_pnl": round(_f(state.get("realised_pnl")), 2),
        "unrealised_pnl": round(_f(state.get("unrealised_pnl")), 2),
        "total_costs": round(_f(state.get("total_costs")), 2),
        "max_drawdown_pct": round(maxdd * 100, 2),
        "sharpe_annualised": round(sharpe, 2),
        "days_live": days,
        "n_marks": len(curve),
        # H-022 pre-registers a 12-month window. Anything read before that is
        # a progress indicator, not a verdict, and the UI must say so.
        "mature": days >= 365,
        "evaluate_after": "2027-09-05",
    }
