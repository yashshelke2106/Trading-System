"""
trading_account.py - the funded paper book that actually trades.

WHAT WAS MISSING
----------------
The system could already do three things: generate signals (scan_only_v2 ->
logs/signals.json), resolve whether a signal's levels were hit (signal_tracker),
and replay a journal against a notional pot after the fact (paper_portfolio).

None of those is a trader. A trader holds a pot of money, decides which of
today's candidates to take, sizes each one against the risk it carries, pays
cash out to open and takes cash back on exit, and ends every day with a
balance that is higher or lower than yesterday's for reasons it can name.

This module is that layer:

    capital in -> the account SELECTS trades -> sizing by risk
        -> cash debited -> levels resolve -> cash credited -> balance moves

TWO PROPERTIES THIS ENFORCES, BECAUSE THE OLD VIEWS DID NOT
-----------------------------------------------------------
1. Money is finite. A signal the account cannot fund is NOT taken; it is
   counted in `skipped_no_cash`. The book earns what the money could actually
   capture, never what every signal would have paid.
2. Risk is the sizing unit, not rupees. Position size falls out of
   `risk_per_trade_pct` divided by the distance to the stop, so a wide-stop
   trade gets fewer shares and every open position risks about the same amount.

INSTRUMENT ROUTING ("both equity and options, as per risk")
------------------------------------------------------------
A direction can be expressed two ways, and the account picks per signal:

    long   -> cash equity (delivery)   |  long CE
    short  -> stock futures            |  long PE

Default preference is the DELTA-ONE leg (cash equity / futures), with options
used only when delta-one will not fit inside the risk caps. That ordering is
not taste: this repo measured long-option buying at PF 0.44, and
core/option_buy_eval.py derives why - the drift required to overcome theta
(0.100%/day) exceeds all the drift available (0.0982%/day). Options are
therefore the LEVERAGE fallback for the case that actually justifies paying
theta: a delta-one position too large for the pot. Set
`RiskPolicy.prefer="cheapest"` to invert the ordering.

Long options are additionally capped by `options_sleeve_max_pct`, so a sleeve
with a measured negative expectancy can never sink the whole book.

HONESTY RULES BAKED IN
----------------------
* No lookahead: exits resolve only against bars dated AFTER the entry bar.
* Same-bar ambiguity resolves PESSIMISTICALLY - if one bar spans both the stop
  and the target, the stop is taken.
* Expired option contracts settle at INTRINSIC value on their own expiry date.
  They are never marked against the next expiry's chain (that bug manufactured
  phantom +200% "EXPIRED wins" in the journal, audited 2026-07-04).
* Costs are charged explicitly and reported on their own line, never folded
  silently into P&L.
* `cash + committed == capital + realised_pnl` is asserted on every write.

This is a PAPER book. It places no orders anywhere.

RUN
---
    python -m core.trading_account --reset --capital 1000000
    python -m core.trading_account --run          # mark, exit, then select
    python -m core.trading_account --status
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import warnings
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional, Tuple, Any

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(_ROOT, "logs", "trading_account_state.json")
LEDGER_PATH = os.path.join(_ROOT, "logs", "trading_account_ledger.jsonl")
SIGNALS_PATH = os.path.join(_ROOT, "logs", "signals.json")

DEFAULT_CAPITAL = 1_000_000.0

# Cost model. Measured in this repo: cash delivery 0.20-0.25% round trip vs
# futures 0.06%. Delta-one costs are charged half on entry and half on exit so
# an open position is never flattered by unpaid exit costs.
EQUITY_COST_BPS_RT = 25.0
FUTURES_COST_BPS_RT = 6.0
OPTION_SPREAD_PCT_PER_LEG = 0.005
OPTION_BROKERAGE_PER_LEG = 20.0

_GRADE_RANK = {"S": 4, "A": 3, "B": 2, "C": 1}


# --------------------------------------------------------------------------
# Risk policy
# --------------------------------------------------------------------------

@dataclass
class RiskPolicy:
    """Every number that decides how much money a trade gets.

    risk_per_trade_pct is the real sizing knob: the fraction of CURRENT equity
    the account accepts losing if the stop fills exactly. Everything else is a
    cap, and a cap can only ever make a position smaller.
    """
    risk_per_trade_pct: float = 0.0075     # 0.75% of equity risked per trade
    max_portfolio_risk_pct: float = 0.06   # 6% at risk across all open trades
    max_position_pct: float = 0.15         # no single position over 15% of equity
    max_gross_deployed_pct: float = 0.90   # keep dry powder; never fully invested
    max_open_positions: int = 12
    max_per_symbol: int = 1
    options_sleeve_max_pct: float = 0.30   # cap on capital in long options
    daily_loss_stop_pct: float = 0.03      # stop opening after -3% on the day
    min_risk_fill: float = 0.5             # a leg must carry >=50% of the
                                           # intended risk to be preferred
    min_rr: float = 1.5
    min_grade: str = "B"
    max_hold_days: int = 10
    signal_max_age_hours: float = 48.0
    prefer: str = "delta_one"              # or "cheapest"

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict]) -> "RiskPolicy":
        d = d or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------------
# Position
# --------------------------------------------------------------------------

@dataclass
class Position:
    id: str
    symbol: str
    direction: str                 # "long" | "short"
    instrument: str                # "equity" | "futures" | "option"
    opened: str                    # ISO timestamp
    opened_date: str               # YYYY-MM-DD; exits resolve strictly AFTER
    qty: int                       # total units (lots * lot_size)
    lots: int
    lot_size: int
    entry: float                   # per-unit price PAID (premium for options)
    entry_spot: float              # underlying at entry (== entry for delta-one)
    sl: float                      # underlying stop level
    target: float                  # underlying target level
    cost_basis: float              # cash debited (equity/option) or blocked (futures)
    risk_rupees: float             # planned loss if the stop fills exactly
    grade: str = ""
    score: float = 0.0
    option_strike: Optional[float] = None
    option_type: Optional[str] = None      # "CE" | "PE"
    option_expiry: Optional[str] = None
    sl_prem: Optional[float] = None
    target_prem: Optional[float] = None
    delta: Optional[float] = None
    mark: Optional[float] = None           # last per-unit mark
    mark_spot: Optional[float] = None
    mark_method: str = ""
    unrealised: float = 0.0
    closed: Optional[str] = None
    exit: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    costs: float = 0.0
    held_days: Optional[int] = None
    sleeve: str = "equity"                 # "equity" | "options"

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "Position":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------------
# Costs / small helpers
# --------------------------------------------------------------------------

def _entry_costs(instrument: str, qty: int, price: float) -> float:
    if instrument == "option":
        return qty * price * OPTION_SPREAD_PCT_PER_LEG + OPTION_BROKERAGE_PER_LEG
    bps = EQUITY_COST_BPS_RT if instrument == "equity" else FUTURES_COST_BPS_RT
    return qty * price * (bps / 10_000.0) / 2.0


def _exit_costs(instrument: str, qty: int, price: float) -> float:
    if instrument == "option":
        return qty * price * OPTION_SPREAD_PCT_PER_LEG + OPTION_BROKERAGE_PER_LEG
    bps = EQUITY_COST_BPS_RT if instrument == "equity" else FUTURES_COST_BPS_RT
    return qty * price * (bps / 10_000.0) / 2.0


def _lot_size_for(symbol: str) -> int:
    """Live scrip master first. config.NSE_LOT_SIZES was measured correct for
    only 3 of 70 traded symbols, so it is never read directly."""
    try:
        from core.futures_leg import lot_size_for
        v = int(lot_size_for(symbol))
        if v > 0:
            return v
    except Exception:
        pass
    try:
        from core.scrip_master import lot_size as _live
        v = _live(str(symbol).upper())
        if v and int(v) > 0:
            return int(v)
    except Exception:
        pass
    return 1


def _intrinsic(spot: float, strike: float, option_type: str) -> float:
    """The only thing an expired contract is worth."""
    if spot <= 0 or strike <= 0:
        return 0.0
    return max(0.0, spot - strike) if str(option_type).upper() == "CE" \
        else max(0.0, strike - spot)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> date:
    return datetime.now().date()


def _parse_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _f(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Price data
# --------------------------------------------------------------------------

def daily_bars(symbol: str, days_back: int = 120):
    """Daily OHLC for `symbol`. Dhan first, yfinance as fallback.

    Returns a list of dicts [{date, open, high, low, close}] sorted ascending,
    or [] when neither source answers. Callers must treat [] as "cannot mark"
    and leave the position alone - never as "no move".
    """
    rows: List[Dict] = []
    try:
        from core.api_dhan import dhan_daily
        df = dhan_daily(symbol, days_back=days_back)
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                d = _parse_date(r.get("date"))
                if d is None:
                    continue
                rows.append({"date": d, "open": _f(r.get("open")),
                             "high": _f(r.get("high")), "low": _f(r.get("low")),
                             "close": _f(r.get("close"))})
    except Exception:
        rows = []

    if not rows:
        try:
            import yfinance as yf
            from core.api_dhan import _YF_TICKER_MAP
            tkr = _YF_TICKER_MAP.get(str(symbol).upper(), f"{str(symbol).upper()}.NS")
            df = yf.download(tkr, period=f"{max(days_back, 5)}d",
                             interval="1d", progress=False, auto_adjust=False)
            if df is not None and not df.empty:
                if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                    df.columns = df.columns.get_level_values(0)
                for idx, r in df.iterrows():
                    d = _parse_date(idx)
                    if d is None:
                        continue
                    rows.append({"date": d, "open": _f(r.get("Open")),
                                 "high": _f(r.get("High")), "low": _f(r.get("Low")),
                                 "close": _f(r.get("Close"))})
        except Exception:
            pass

    rows = [r for r in rows if r["close"] > 0]
    rows.sort(key=lambda r: r["date"])
    return rows


# --------------------------------------------------------------------------
# Sizing
# --------------------------------------------------------------------------

@dataclass
class Sizing:
    """One fundable way to express a signal, or a reason it is not fundable."""
    ok: bool
    instrument: str = ""
    reason: str = ""
    qty: int = 0
    lots: int = 0
    lot_size: int = 1
    entry: float = 0.0
    cost_basis: float = 0.0
    risk_rupees: float = 0.0
    risk_fill: float = 0.0     # risk_rupees / risk_budget: how much of the
                               # intended risk this leg actually carries

    def to_dict(self) -> Dict:
        return asdict(self)


def size_equity(entry: float, sl: float, risk_budget: float,
                equity: float, cash: float, policy: RiskPolicy) -> Sizing:
    """Cash-equity long. Cash debited = qty * entry."""
    risk_unit = entry - sl
    if entry <= 0 or risk_unit <= 0:
        return Sizing(False, "equity", "stop is not below entry")
    qty = int(risk_budget // risk_unit)
    if qty < 1:
        return Sizing(False, "equity", "one share exceeds the risk budget")
    qty = min(qty, int((equity * policy.max_position_pct) // entry))
    qty = min(qty, int(cash // entry))
    if qty < 1:
        return Sizing(False, "equity", "not fundable inside position/cash caps")
    return Sizing(True, "equity", "cash delivery long", qty=qty, lots=qty,
                  lot_size=1, entry=entry, cost_basis=qty * entry,
                  risk_rupees=qty * risk_unit)


def size_futures(symbol: str, entry: float, sl: float, risk_budget: float,
                 equity: float, cash: float, policy: RiskPolicy) -> Sizing:
    """Stock-futures short. Cash BLOCKED = span + exposure + broker buffer.

    Shorting cash equity for a multi-day hold is not a thing on NSE, so a short
    signal is expressed through the future or not at all.
    """
    risk_unit = sl - entry
    if entry <= 0 or risk_unit <= 0:
        return Sizing(False, "futures", "stop is not above entry")
    lot = _lot_size_for(symbol)
    risk_per_lot = risk_unit * lot
    if risk_per_lot <= 0:
        return Sizing(False, "futures", "degenerate lot risk")
    lots = int(risk_budget // risk_per_lot)
    if lots < 1:
        return Sizing(False, "futures", "one lot exceeds the risk budget")
    try:
        from core.margin import estimate as margin_estimate
        margin_per_lot = float(margin_estimate(symbol, entry, 1, "futures",
                                               capital=cash, lot_size=lot).total)
    except Exception:
        # Conservative fallback: 20% of notional if the margin model is absent.
        margin_per_lot = entry * lot * 0.20
    if margin_per_lot <= 0:
        return Sizing(False, "futures", "no margin estimate")
    lots = min(lots, int((equity * policy.max_position_pct) // margin_per_lot))
    lots = min(lots, int(cash // margin_per_lot))
    if lots < 1:
        return Sizing(False, "futures", "margin for one lot exceeds position/cash caps")
    return Sizing(True, "futures", "stock futures short", qty=lots * lot, lots=lots,
                  lot_size=lot, entry=entry, cost_basis=lots * margin_per_lot,
                  risk_rupees=lots * risk_per_lot)


def size_option(symbol: str, prem: float, sl_prem: float, risk_budget: float,
                equity: float, cash: float, options_room: float,
                policy: RiskPolicy) -> Sizing:
    """Long CE/PE. Cash debited = lots * lot_size * premium.

    Risk is capped at the premium paid: a long option cannot lose more than it
    cost, whatever the stop level says.
    """
    if prem <= 0:
        return Sizing(False, "option", "no premium")
    if sl_prem is None or sl_prem < 0 or sl_prem >= prem:
        return Sizing(False, "option", "premium stop is not below premium entry")
    lot = _lot_size_for(symbol)
    risk_unit = prem - sl_prem
    risk_per_lot = risk_unit * lot
    if risk_per_lot <= 0:
        return Sizing(False, "option", "degenerate lot risk")
    lots = int(risk_budget // risk_per_lot)
    if lots < 1:
        return Sizing(False, "option", "one lot exceeds the risk budget")
    outlay_per_lot = prem * lot
    lots = min(lots, int((equity * policy.max_position_pct) // outlay_per_lot))
    lots = min(lots, int(cash // outlay_per_lot))
    lots = min(lots, int(max(0.0, options_room) // outlay_per_lot))
    if lots < 1:
        return Sizing(False, "option", "not fundable inside position/cash/sleeve caps")
    qty = lots * lot
    cost_basis = qty * prem
    return Sizing(True, "option", "long option", qty=qty, lots=lots, lot_size=lot,
                  entry=prem, cost_basis=cost_basis,
                  risk_rupees=min(qty * risk_unit, cost_basis))


def _option_leg_usable(sig: Dict, today: Optional[date] = None) -> Tuple[bool, str]:
    """Is there a real, tradeable option leg on this signal?

    Rejects an expiry that is today or past. Entering on expiry day is the
    gamma trap this repo already paid for (INDUSTOWER PE, 2026-05-26).
    """
    today = today or _today()
    prem = _f(sig.get("entry_prem"))
    if prem <= 0:
        return False, "no premium on the signal"
    if not sig.get("option_strike") or not sig.get("option_type"):
        return False, "no strike/type"
    exp = _parse_date(sig.get("option_expiry"))
    if exp is None:
        return False, "no expiry"
    if exp <= today:
        return False, f"expiry {exp} is today or past"
    return True, ""


def choose_sizing(sig: Dict, equity: float, cash: float, options_room: float,
                  policy: RiskPolicy, today: Optional[date] = None) -> Sizing:
    """Pick the instrument and the size for one signal.

    Builds every fundable expression of the signal and returns one, ordered by
    `policy.prefer`. Returns an `ok=False` Sizing carrying the reason when no
    expression fits - the caller records that reason rather than dropping it.
    """
    today = today or _today()
    direction = str(sig.get("direction") or "").lower()
    symbol = str(sig.get("symbol") or "").upper()
    entry = _f(sig.get("entry_price"))
    sl = _f(sig.get("sl_price"))
    risk_budget = equity * policy.risk_per_trade_pct

    candidates: List[Sizing] = []
    rejects: List[str] = []

    if direction == "long":
        s = size_equity(entry, sl, risk_budget, equity, cash, policy)
    else:
        s = size_futures(symbol, entry, sl, risk_budget, equity, cash, policy)
    (candidates if s.ok else rejects).append(s if s.ok else f"{s.instrument}: {s.reason}")

    usable, why = _option_leg_usable(sig, today)
    if usable:
        o = size_option(symbol, _f(sig.get("entry_prem")), _f(sig.get("sl_prem")),
                        risk_budget, equity, cash, options_room, policy)
        (candidates if o.ok else rejects).append(o if o.ok else f"option: {o.reason}")
    else:
        rejects.append(f"option: {why}")

    if not candidates:
        return Sizing(False, "", "; ".join(str(r) for r in rejects))

    for c in candidates:
        c.risk_fill = (c.risk_rupees / risk_budget) if risk_budget > 0 else 0.0

    if policy.prefer == "cheapest":
        candidates.sort(key=lambda c: c.cost_basis)
        return candidates[0]

    # Delta-one preferred, but only when it actually EXPRESSES the trade.
    #
    # Cash equity is infinitely divisible, so it can always be scaled down to
    # whatever cash is left - which means "does it fit" is the wrong question
    # and would route every long to equity. A 30-share position carrying 10% of
    # the intended risk is a token, not the trade. So delta-one wins only if it
    # delivers at least `min_risk_fill` of the risk budget; otherwise the leg
    # that carries the most risk per rupee of capital wins, which is precisely
    # the case where paying theta is justified.
    delta_one = [c for c in candidates if c.instrument != "option"]
    good = [c for c in delta_one if c.risk_fill >= policy.min_risk_fill]
    if good:
        good.sort(key=lambda c: c.cost_basis)
        return good[0]
    candidates.sort(key=lambda c: (-round(c.risk_fill, 4), c.cost_basis))
    return candidates[0]


# --------------------------------------------------------------------------
# Account state
# --------------------------------------------------------------------------

def _default_state(capital: float, policy: RiskPolicy) -> Dict:
    return {
        "started": _now(),
        "capital": float(capital),
        "cash": float(capital),
        "realised_pnl": 0.0,
        "total_costs": 0.0,
        "policy": policy.to_dict(),
        "open": [],
        "closed": [],
        "curve": [{"ts": _now(), "equity": float(capital), "cash": float(capital)}],
        "counters": {"signals_seen": 0, "taken": 0, "skipped_no_cash": 0,
                     "skipped_risk_cap": 0, "skipped_filter": 0,
                     "skipped_duplicate": 0, "skipped_no_data": 0},
        "day": {"date": str(_today()), "start_equity": float(capital),
                "halted": False, "halt_reason": ""},
        "mode": "PAPER",
        "note": ("Funded paper book. Capital is a real constraint; an "
                 "unaffordable signal is skipped and counted, never paid out."),
    }


def load_state() -> Dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def committed(state: Dict) -> float:
    """Cash currently tied up in open positions (paid or blocked as margin)."""
    return sum(_f(p.get("cost_basis")) for p in state.get("open", []))


def check_invariant(state: Dict) -> None:
    """cash + committed == capital + realised_pnl.

    Every rupee is either in the account or in a position. If this ever fails,
    a P&L number from this book is meaningless, so it raises rather than warns.
    """
    lhs = _f(state.get("cash")) + committed(state)
    rhs = _f(state.get("capital")) + _f(state.get("realised_pnl"))
    if abs(lhs - rhs) > 1.0:
        raise AssertionError(
            f"cash ledger broken: cash+committed={lhs:.2f} vs "
            f"capital+realised={rhs:.2f} (diff {lhs - rhs:.2f})")


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


def market_value(p: Dict) -> float:
    """What the position would return to cash if closed at the last mark.

    For futures the blocked margin comes back plus the mark-to-market move, so
    market value is cost_basis + unrealised, not qty * price.
    """
    if p.get("instrument") == "futures":
        return _f(p.get("cost_basis")) + _f(p.get("unrealised"))
    mark = p.get("mark")
    if mark is None:
        return _f(p.get("cost_basis"))
    return _f(p.get("qty")) * _f(mark)


def equity_of(state: Dict) -> float:
    """Cash plus the marked value of everything open."""
    return _f(state.get("cash")) + sum(market_value(p) for p in state.get("open", []))


def options_deployed(state: Dict) -> float:
    return sum(_f(p.get("cost_basis")) for p in state.get("open", [])
               if p.get("instrument") == "option")


def open_risk(state: Dict) -> float:
    return sum(_f(p.get("risk_rupees")) for p in state.get("open", []))


# --------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------

# Other books whose P&L a "reset everything" is expected to clear too. The
# account's own files are archived from STATE_PATH / LEDGER_PATH, so pointing
# those elsewhere (tests, a second book) archives the right files rather than
# whatever happens to sit in logs/.
_LEGACY_BOOKS = ("paper_book_state.json", "paper_book_ledger.jsonl",
                 "cc_paper_state.json")


def reset(capital: float = DEFAULT_CAPITAL, policy: Optional[RiskPolicy] = None,
          force: bool = False) -> Dict:
    """Archive whatever P&L exists and open a fresh book at `capital`.

    Archives, never deletes. logs/signal_journal.jsonl in particular is the
    evidence base that established option BUYING at PF 0.44 - the measurement
    that forced the current design - and is left untouched where it is.
    """
    state = load_state()
    if state and not force and not state.get("_resettable", True):
        return {"ok": False, "reason": "a book is open; pass force=True"}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = os.path.join(os.path.dirname(STATE_PATH),
                               f"archive_account_{stamp}")
    own = [STATE_PATH, LEDGER_PATH]
    legacy = [os.path.join(_ROOT, "logs", n) for n in _LEGACY_BOOKS]
    archived: List[str] = []
    for src in own + legacy:
        if os.path.exists(src):
            os.makedirs(archive_dir, exist_ok=True)
            shutil.copy2(src, os.path.join(archive_dir, os.path.basename(src)))
            archived.append(os.path.basename(src))
    # Only the account's own files are cleared. The legacy books are copied and
    # left in place: signal_journal.jsonl and friends are the evidence base that
    # measured option BUYING at PF 0.44, and nothing here may destroy them.
    for p in own:
        if os.path.exists(p):
            os.remove(p)

    policy = policy or RiskPolicy()
    fresh = _default_state(capital, policy)
    save_state(fresh)
    append_ledger({"event": "reset", "capital": capital,
                   "archived_to": archive_dir if archived else None,
                   "archived": archived, "policy": policy.to_dict()})
    return {"ok": True, "capital": capital, "archived": archived,
            "archive_dir": archive_dir if archived else None,
            "policy": policy.to_dict()}


# --------------------------------------------------------------------------
# Marking and exits
# --------------------------------------------------------------------------

def _resolve_exit(p: Dict, bars: List[Dict], policy: RiskPolicy,
                  today: Optional[date] = None) -> Optional[Dict]:
    """Walk the bars AFTER entry and return the first exit, or None.

    Pessimistic on ambiguity: a bar that spans both the stop and the target is
    recorded as a stop. Intrabar order is unknowable from daily data, and
    assuming the good fill is how a backtest manufactures an edge it does not
    have.
    """
    today = today or _today()
    entry_date = _parse_date(p.get("opened_date"))
    if entry_date is None:
        return None
    direction = p.get("direction")
    sl, target = _f(p.get("sl")), _f(p.get("target"))
    is_option = p.get("instrument") == "option"
    expiry = _parse_date(p.get("option_expiry")) if is_option else None

    forward = [b for b in bars if b["date"] > entry_date]
    for i, b in enumerate(forward, start=1):
        # An option cannot be held past its own expiry. Settle at intrinsic on
        # the expiry date and never look at a later bar for that contract.
        if expiry is not None and b["date"] > expiry:
            settle_bar = next((x for x in reversed(forward)
                               if x["date"] <= expiry), None)
            spot = _f(settle_bar["close"]) if settle_bar else _f(b["open"])
            return {"exit": _intrinsic(spot, _f(p.get("option_strike")),
                                       p.get("option_type") or "CE"),
                    "exit_spot": spot, "reason": "EXPIRED",
                    "date": expiry, "held_days": max(1, i - 1)}

        hit_sl = (b["low"] <= sl) if direction == "long" else (b["high"] >= sl)
        hit_tgt = (b["high"] >= target) if direction == "long" else (b["low"] <= target)

        if hit_sl:
            px = _f(p.get("sl_prem")) if is_option else sl
            return {"exit": px, "exit_spot": sl, "reason": "SL_HIT",
                    "date": b["date"], "held_days": i,
                    "ambiguous": bool(hit_tgt)}
        if hit_tgt:
            px = _f(p.get("target_prem")) if is_option else target
            return {"exit": px, "exit_spot": target, "reason": "TARGET_HIT",
                    "date": b["date"], "held_days": i}

        # Time exit applies to delta-one only. An option has no defined premium
        # at an arbitrary date, and settling it on a delta approximation would
        # put an estimate into REALISED P&L. Expiry already bounds the hold, so
        # options run to a level or to intrinsic value.
        if i >= policy.max_hold_days and not is_option:
            spot = _f(b["close"])
            return {"exit": spot, "exit_spot": spot, "reason": "TIME_EXIT",
                    "date": b["date"], "held_days": i}
    return None


def _mark_option(p: Dict, spot: float, on: Optional[date] = None) -> float:
    """Approximate a long option's value from the underlying.

    delta * spot move, minus theta already burned. This is an APPROXIMATION and
    every caller labels it as one: the journal measured option premium losing
    -9.56% on a -0.60% spot move, so a pure delta mark reads too kind. Marks
    are for the unrealised line only - realised P&L always uses the signal's
    own premium levels or intrinsic value at expiry.
    """
    entry_prem = _f(p.get("entry"))
    entry_spot = _f(p.get("entry_spot"))
    delta = _f(p.get("delta"))
    strike = _f(p.get("option_strike"))
    otype = p.get("option_type") or "CE"
    if spot <= 0:
        return entry_prem
    if entry_spot <= 0 or delta <= 0:
        return max(_intrinsic(spot, strike, otype), 0.0) or entry_prem
    move = (spot - entry_spot) if otype.upper() == "CE" else (entry_spot - spot)
    val = entry_prem + delta * abs(move) * (1 if move >= 0 else -1)
    return max(0.0, max(val, _intrinsic(spot, strike, otype)))


def _close_position(state: Dict, p: Dict, exit_px: float, reason: str,
                    held_days: int, when: Optional[date] = None) -> Dict:
    """Realise a position: credit cash, book P&L, move it to `closed`."""
    qty = int(_f(p.get("qty")))
    instrument = p.get("instrument")
    entry = _f(p.get("entry"))
    ecost = _exit_costs(instrument, qty, exit_px)
    open_cost = _f(p.get("costs"))

    if instrument == "futures":
        gross = qty * (entry - exit_px) if p.get("direction") == "short" \
            else qty * (exit_px - entry)
    else:
        gross = qty * (exit_px - entry)

    pnl = gross - ecost - open_cost
    p["closed"] = _now()
    p["closed_date"] = str(when or _today())
    p["exit"] = round(exit_px, 4)
    p["exit_reason"] = reason
    p["held_days"] = held_days
    p["costs"] = round(open_cost + ecost, 2)
    p["gross_pnl"] = round(gross, 2)
    p["pnl"] = round(pnl, 2)
    p["pnl_pct"] = round(100.0 * pnl / _f(p.get("cost_basis"), 1.0), 3) \
        if _f(p.get("cost_basis")) > 0 else 0.0
    p["unrealised"] = 0.0

    state["cash"] = _f(state.get("cash")) + _f(p.get("cost_basis")) + pnl
    state["realised_pnl"] = _f(state.get("realised_pnl")) + pnl
    state["total_costs"] = _f(state.get("total_costs")) + open_cost + ecost
    state["open"] = [x for x in state.get("open", []) if x.get("id") != p.get("id")]
    state.setdefault("closed", []).append(p)
    append_ledger({"event": "close", "id": p.get("id"), "symbol": p.get("symbol"),
                   "instrument": instrument, "reason": reason, "qty": qty,
                   "entry": entry, "exit": exit_px, "pnl": round(pnl, 2),
                   "costs": round(open_cost + ecost, 2),
                   "cash_after": round(_f(state["cash"]), 2)})
    return p


def mark_and_exit(state: Optional[Dict] = None,
                  today: Optional[date] = None) -> Dict:
    """Mark every open position to market and close the ones that resolved.

    This is where money is actually won and lost: a target that fills credits
    cash, a stop that fills debits it.
    """
    state = state if state is not None else load_state()
    if not state:
        return {"ok": False, "reason": "no account; run reset first"}
    policy = RiskPolicy.from_dict(state.get("policy"))
    today = today or _today()

    exits: List[Dict] = []
    no_data: List[str] = []
    bars_cache: Dict[str, List[Dict]] = {}

    for p in list(state.get("open", [])):
        sym = p.get("symbol")
        if sym not in bars_cache:
            bars_cache[sym] = daily_bars(sym, days_back=120)
        bars = bars_cache[sym]
        if not bars:
            no_data.append(sym)
            continue

        ex = _resolve_exit(p, bars, policy, today)
        if ex:
            exits.append(_close_position(state, p, _f(ex["exit"]), ex["reason"],
                                         int(ex["held_days"]), ex.get("date")))
            continue

        last = bars[-1]
        spot = _f(last["close"])
        p["mark_spot"] = spot
        if p.get("instrument") == "option":
            p["mark"] = round(_mark_option(p, spot, last["date"]), 4)
            p["mark_method"] = "delta-approx (unrealised only)"
            p["unrealised"] = round(_f(p["qty"]) * (_f(p["mark"]) - _f(p["entry"]))
                                    - _f(p.get("costs")), 2)
        else:
            p["mark"] = round(spot, 4)
            p["mark_method"] = "last daily close"
            sign = 1.0 if p.get("direction") == "long" else -1.0
            p["unrealised"] = round(sign * _f(p["qty"]) * (spot - _f(p["entry"]))
                                    - _f(p.get("costs")), 2)

    _roll_day(state, today)
    save_state(state)
    return {"ok": True, "exits": [e.get("id") for e in exits],
            "n_exits": len(exits), "no_data": sorted(set(no_data)),
            "realised_pnl": round(_f(state.get("realised_pnl")), 2),
            "equity": round(equity_of(state), 2)}


def _roll_day(state: Dict, today: date) -> None:
    """Start a new trading day, and enforce the daily loss stop within one."""
    day = state.setdefault("day", {})
    if day.get("date") != str(today):
        day["date"] = str(today)
        day["start_equity"] = equity_of(state)
        day["halted"] = False
        day["halt_reason"] = ""
        state.setdefault("curve", []).append(
            {"ts": _now(), "date": str(today), "equity": round(equity_of(state), 2),
             "cash": round(_f(state.get("cash")), 2)})
        return
    start = _f(day.get("start_equity"), equity_of(state))
    if start > 0:
        dd = (equity_of(state) - start) / start
        policy = RiskPolicy.from_dict(state.get("policy"))
        if dd <= -abs(policy.daily_loss_stop_pct):
            day["halted"] = True
            day["halt_reason"] = (f"daily loss stop: {dd * 100:.2f}% vs limit "
                                  f"-{policy.daily_loss_stop_pct * 100:.2f}%")


# --------------------------------------------------------------------------
# Selection and entry
# --------------------------------------------------------------------------

def _load_signals() -> List[Dict]:
    if not os.path.exists(SIGNALS_PATH):
        return []
    try:
        with open(SIGNALS_PATH, encoding="utf-8") as fh:
            blob = json.load(fh)
    except Exception:
        return []
    sigs = blob.get("signals") if isinstance(blob, dict) else blob
    return list(sigs or [])


def _passes_filters(sig: Dict, policy: RiskPolicy,
                    now: Optional[datetime] = None) -> Tuple[bool, str]:
    now = now or datetime.now()
    grade = str(sig.get("confluence_grade") or "").upper()
    if _GRADE_RANK.get(grade, 0) < _GRADE_RANK.get(policy.min_grade.upper(), 0):
        return False, f"grade {grade or '?'} below {policy.min_grade}"
    entry, sl = _f(sig.get("entry_price")), _f(sig.get("sl_price"))
    target = _f(sig.get("target_price"))
    if entry <= 0 or sl <= 0 or target <= 0:
        return False, "incomplete levels"
    direction = str(sig.get("direction") or "").lower()
    if direction not in ("long", "short"):
        return False, "no direction"
    if direction == "long" and not (sl < entry < target):
        return False, "levels inconsistent for a long"
    if direction == "short" and not (target < entry < sl):
        return False, "levels inconsistent for a short"
    risk = abs(entry - sl)
    rr = abs(target - entry) / risk if risk > 0 else 0.0
    if rr < policy.min_rr:
        return False, f"rr {rr:.2f} below {policy.min_rr}"
    ts = sig.get("ts")
    if ts:
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", ""))
            age_h = (now - t).total_seconds() / 3600.0
            if age_h > policy.signal_max_age_hours:
                return False, f"stale by {age_h:.0f}h"
        except Exception:
            pass
    return True, ""


def select_and_open(state: Optional[Dict] = None, signals: Optional[List[Dict]] = None,
                    today: Optional[date] = None,
                    now: Optional[datetime] = None) -> Dict:
    """Choose today's trades and fund them. This is the account deciding.

    Candidates are ranked by grade then confluence score, then taken in order
    until a cap binds. Every rejection is recorded with its reason: a book that
    silently drops what it could not afford is the exact mirage this replaces.
    """
    state = state if state is not None else load_state()
    if not state:
        return {"ok": False, "reason": "no account; run reset first"}
    policy = RiskPolicy.from_dict(state.get("policy"))
    today = today or _today()
    now = now or datetime.now()
    _roll_day(state, today)

    sigs = signals if signals is not None else _load_signals()
    counters = state.setdefault("counters", {})
    counters["signals_seen"] = int(counters.get("signals_seen", 0)) + len(sigs)

    decisions: List[Dict] = []
    if state.get("day", {}).get("halted"):
        save_state(state)
        # Same shape as the normal return: a caller must never have to know
        # which branch produced the result to read n_opened.
        return {"ok": True, "opened": [], "n_opened": 0, "halted": True,
                "reason": state["day"].get("halt_reason"), "decisions": [],
                "cash": round(_f(state.get("cash")), 2),
                "equity": round(equity_of(state), 2)}

    ranked = sorted(
        sigs,
        key=lambda s: (_GRADE_RANK.get(str(s.get("confluence_grade") or "").upper(), 0),
                       _f(s.get("confluence_score"))),
        reverse=True)

    opened: List[Dict] = []
    for sig in ranked:
        sym = str(sig.get("symbol") or "").upper()
        equity = equity_of(state)
        cash = _f(state.get("cash"))

        ok, why = _passes_filters(sig, policy, now)
        if not ok:
            counters["skipped_filter"] = int(counters.get("skipped_filter", 0)) + 1
            decisions.append({"symbol": sym, "taken": False, "reason": why})
            continue

        held = sum(1 for p in state.get("open", []) if p.get("symbol") == sym)
        if held >= policy.max_per_symbol:
            counters["skipped_duplicate"] = int(counters.get("skipped_duplicate", 0)) + 1
            decisions.append({"symbol": sym, "taken": False,
                              "reason": f"already holding {held}"})
            continue
        if len(state.get("open", [])) >= policy.max_open_positions:
            decisions.append({"symbol": sym, "taken": False,
                              "reason": f"at max {policy.max_open_positions} positions"})
            continue

        risk_budget = equity * policy.risk_per_trade_pct
        room = equity * policy.max_portfolio_risk_pct - open_risk(state)
        if room < risk_budget:
            counters["skipped_risk_cap"] = int(counters.get("skipped_risk_cap", 0)) + 1
            decisions.append({"symbol": sym, "taken": False,
                              "reason": f"portfolio risk cap: Rs{room:,.0f} of "
                                        f"Rs{risk_budget:,.0f} left"})
            continue

        # Spendable is capped below cash so the book is never fully invested.
        # A fully deployed account cannot take tomorrow's better signal and has
        # no buffer for a futures margin call.
        spendable = min(cash, equity * policy.max_gross_deployed_pct - committed(state))
        if spendable <= 0:
            counters["skipped_no_cash"] = int(counters.get("skipped_no_cash", 0)) + 1
            decisions.append({"symbol": sym, "taken": False,
                              "reason": f"gross deployment cap "
                                        f"({policy.max_gross_deployed_pct:.0%}) reached"})
            continue

        options_room = equity * policy.options_sleeve_max_pct - options_deployed(state)
        sizing = choose_sizing(sig, equity, spendable, options_room, policy, today)
        if not sizing.ok:
            counters["skipped_no_cash"] = int(counters.get("skipped_no_cash", 0)) + 1
            decisions.append({"symbol": sym, "taken": False, "reason": sizing.reason})
            continue
        if sizing.risk_rupees > room:
            counters["skipped_risk_cap"] = int(counters.get("skipped_risk_cap", 0)) + 1
            decisions.append({"symbol": sym, "taken": False,
                              "reason": "sized risk exceeds remaining portfolio risk"})
            continue

        p = _open_position(state, sig, sizing, today)
        opened.append(p)
        counters["taken"] = int(counters.get("taken", 0)) + 1
        decisions.append({"symbol": sym, "taken": True,
                          "instrument": sizing.instrument, "qty": sizing.qty,
                          "cost_basis": round(sizing.cost_basis, 2),
                          "risk": round(sizing.risk_rupees, 2)})

    save_state(state)
    return {"ok": True, "opened": [p["id"] for p in opened], "n_opened": len(opened),
            "halted": False, "decisions": decisions,
            "cash": round(_f(state.get("cash")), 2),
            "equity": round(equity_of(state), 2)}


def _open_position(state: Dict, sig: Dict, sizing: Sizing, today: date) -> Dict:
    """Debit the cash and record the position."""
    sym = str(sig.get("symbol") or "").upper()
    direction = str(sig.get("direction") or "").lower()
    is_option = sizing.instrument == "option"
    entry_spot = _f(sig.get("entry_price"))
    cost = _entry_costs(sizing.instrument, sizing.qty, sizing.entry)

    pid = f"{sym}-{sizing.instrument}-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:18]}"
    p = Position(
        id=pid, symbol=sym, direction=direction, instrument=sizing.instrument,
        opened=_now(), opened_date=str(today), qty=sizing.qty, lots=sizing.lots,
        lot_size=sizing.lot_size, entry=round(sizing.entry, 4),
        entry_spot=entry_spot, sl=_f(sig.get("sl_price")),
        target=_f(sig.get("target_price")),
        cost_basis=round(sizing.cost_basis, 2),
        risk_rupees=round(sizing.risk_rupees, 2),
        grade=str(sig.get("confluence_grade") or ""),
        score=_f(sig.get("confluence_score")),
        option_strike=_f(sig.get("option_strike")) if is_option else None,
        option_type=(str(sig.get("option_type")).upper() if is_option else None),
        option_expiry=(str(sig.get("option_expiry"))[:10] if is_option else None),
        sl_prem=_f(sig.get("sl_prem")) if is_option else None,
        target_prem=_f(sig.get("target_prem")) if is_option else None,
        delta=_f(sig.get("delta")) if is_option else None,
        mark=round(sizing.entry, 4), mark_spot=entry_spot,
        mark_method="entry", unrealised=round(-cost, 2),
        costs=round(cost, 2),
        sleeve=("options" if is_option else "equity"),
    ).to_dict()

    state["cash"] = _f(state.get("cash")) - sizing.cost_basis
    state.setdefault("open", []).append(p)
    append_ledger({"event": "open", "id": pid, "symbol": sym,
                   "instrument": sizing.instrument, "direction": direction,
                   "qty": sizing.qty, "lots": sizing.lots, "entry": sizing.entry,
                   "cost_basis": round(sizing.cost_basis, 2),
                   "risk": round(sizing.risk_rupees, 2),
                   "cash_after": round(_f(state["cash"]), 2)})
    return p


# --------------------------------------------------------------------------
# Cycle + reporting
# --------------------------------------------------------------------------

def run_cycle(today: Optional[date] = None) -> Dict:
    """One full pass: resolve what closed, then decide what to open.

    Exits run FIRST so cash released by a closing position is available to the
    trades chosen in the same cycle.
    """
    state = load_state()
    if not state:
        return {"ok": False, "reason": "no account; run reset first"}
    marked = mark_and_exit(state, today)
    state = load_state()
    entered = select_and_open(state, today=today)
    return {"ok": True, "marked": marked, "entered": entered,
            "account": summary(load_state())}


def summary(state: Optional[Dict] = None) -> Dict:
    """The account, as a trader would read it."""
    state = state if state is not None else load_state()
    if not state:
        return {"ok": False, "reason": "no account; run reset first"}

    capital = _f(state.get("capital"))
    cash = _f(state.get("cash"))
    realised = _f(state.get("realised_pnl"))
    open_positions = state.get("open", [])
    closed = state.get("closed", [])
    unreal = sum(_f(p.get("unrealised")) for p in open_positions)
    eq = equity_of(state)

    wins = [c for c in closed if _f(c.get("pnl")) > 0]
    losses = [c for c in closed if _f(c.get("pnl")) <= 0]
    gross_win = sum(_f(c.get("pnl")) for c in wins)
    gross_loss = abs(sum(_f(c.get("pnl")) for c in losses))

    by_sleeve: Dict[str, Dict] = {}
    for c in closed:
        s = by_sleeve.setdefault(c.get("sleeve") or "equity",
                                 {"trades": 0, "pnl": 0.0, "wins": 0})
        s["trades"] += 1
        s["pnl"] = round(s["pnl"] + _f(c.get("pnl")), 2)
        s["wins"] += 1 if _f(c.get("pnl")) > 0 else 0
    for s in by_sleeve.values():
        s["win_rate_pct"] = round(100.0 * s["wins"] / s["trades"], 1) if s["trades"] else None

    peak = capital
    trough_dd = 0.0
    for pt in state.get("curve", []):
        e = _f(pt.get("equity"))
        peak = max(peak, e)
        if peak > 0:
            trough_dd = min(trough_dd, (e - peak) / peak)

    return {
        "ok": True,
        "mode": state.get("mode", "PAPER"),
        "started": state.get("started"),
        "capital": round(capital, 2),
        "cash": round(cash, 2),
        "committed": round(committed(state), 2),
        "equity": round(eq, 2),
        "realised_pnl": round(realised, 2),
        "unrealised_pnl": round(unreal, 2),
        "total_pnl": round(realised + unreal, 2),
        "return_pct": round(100.0 * (eq - capital) / capital, 3) if capital else 0.0,
        # Costs already incurred, including the entry side of positions still
        # open. Reporting only realised costs made an open book look free.
        "total_costs": round(_f(state.get("total_costs"))
                             + sum(_f(p.get("costs")) for p in open_positions), 2),
        "costs_realised": round(_f(state.get("total_costs")), 2),
        "open_positions": len(open_positions),
        "open_risk": round(open_risk(state), 2),
        "open_risk_pct": round(100.0 * open_risk(state) / eq, 2) if eq else 0.0,
        "options_deployed": round(options_deployed(state), 2),
        "options_deployed_pct": round(100.0 * options_deployed(state) / eq, 2) if eq else 0.0,
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(closed), 1) if closed else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "max_drawdown_pct": round(100.0 * trough_dd, 2),
        "by_sleeve": by_sleeve,
        "counters": state.get("counters", {}),
        "day": state.get("day", {}),
        "policy": state.get("policy", {}),
        "positions": open_positions,
        "recent_closed": closed[-20:],
        "confidence": _confidence(len(closed)),
    }


def _confidence(n_closed: int) -> Dict:
    """What this book's P&L is and is not evidence for.

    Stated alongside every number so a green month is never read as validation.
    Every strategy this repo has tested landed at or below zero; a funded book
    measures whether the MACHINERY is right, and needs a large sample before it
    says anything about edge.
    """
    if n_closed < 30:
        verdict = "MECHANICS ONLY - too few trades to say anything about edge"
    elif n_closed < 200:
        verdict = "DIRECTIONAL HINT - still inside the noise band"
    else:
        verdict = "SAMPLE BUILDING - compare against the pre-registered null"
    return {"closed_trades": n_closed, "verdict": verdict,
            "note": ("P&L here is the account's own realised cash. It is not a "
                     "validated edge: no strategy in this repo has cleared its "
                     "holdout gate.")}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_summary(s: Dict) -> None:
    if not s.get("ok"):
        print(f"[account] {s.get('reason')}")
        return
    print(f"\n=== FUNDED PAPER ACCOUNT ({s['mode']}) ===")
    print(f"  capital        Rs {s['capital']:>14,.2f}")
    print(f"  cash           Rs {s['cash']:>14,.2f}")
    print(f"  committed      Rs {s['committed']:>14,.2f}")
    print(f"  equity         Rs {s['equity']:>14,.2f}   ({s['return_pct']:+.2f}%)")
    print(f"  realised P&L   Rs {s['realised_pnl']:>14,.2f}")
    print(f"  unrealised     Rs {s['unrealised_pnl']:>14,.2f}")
    print(f"  costs paid     Rs {s['total_costs']:>14,.2f}")
    print(f"\n  open {s['open_positions']} | risk at work {s['open_risk_pct']:.2f}% "
          f"| options sleeve {s['options_deployed_pct']:.1f}%")
    print(f"  closed {s['closed_trades']} | win rate "
          f"{s['win_rate_pct'] if s['win_rate_pct'] is not None else '-'} "
          f"| PF {s['profit_factor'] if s['profit_factor'] is not None else '-'}")
    for name, sl in (s.get("by_sleeve") or {}).items():
        print(f"    {name:<8} {sl['trades']:>3} trades  Rs {sl['pnl']:>12,.2f}  "
              f"win {sl['win_rate_pct']}%")
    for p in s.get("positions", []):
        print(f"    [{p['instrument']:<8}] {p['symbol']:<12} {p['direction']:<5} "
              f"qty {p['qty']:>6}  cost Rs {p['cost_basis']:>11,.0f}  "
              f"unreal Rs {p['unrealised']:>10,.0f}")
    c = s.get("confidence", {})
    print(f"\n  confidence: {c.get('verdict')}")
    print(f"  {c.get('note')}\n")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Funded paper trading account")
    ap.add_argument("--reset", action="store_true", help="archive P&L, open a fresh book")
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    ap.add_argument("--risk-per-trade", type=float, default=None,
                    help="fraction of equity risked per trade, e.g. 0.0075")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--run", action="store_true", help="mark, exit, then select")
    ap.add_argument("--mark", action="store_true", help="mark and exit only")
    ap.add_argument("--select", action="store_true", help="select and open only")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)

    if a.reset:
        pol = RiskPolicy()
        if a.risk_per_trade is not None:
            pol.risk_per_trade_pct = a.risk_per_trade
        r = reset(a.capital, pol, force=True if a.force else True)
        print(f"[account] reset to Rs {a.capital:,.0f}"
              + (f"; archived {len(r.get('archived', []))} prior file(s) to "
                 f"{r.get('archive_dir')}" if r.get("archived") else ""))
    if a.mark:
        print(json.dumps(mark_and_exit(), indent=2, default=str))
    if a.select:
        print(json.dumps(select_and_open(), indent=2, default=str))
    if a.run:
        r = run_cycle()
        if not r.get("ok"):
            print(f"[account] {r.get('reason')}")
            return 1
        m, e = r["marked"], r["entered"]
        print(f"[account] exits {m.get('n_exits', 0)} | opened {e.get('n_opened', 0)}")
        for d in e.get("decisions", [])[:20]:
            flag = "TAKE" if d.get("taken") else "skip"
            extra = (f"{d.get('instrument')} qty {d.get('qty')} "
                     f"Rs{d.get('cost_basis', 0):,.0f}") if d.get("taken") \
                else d.get("reason")
            print(f"    {flag}  {d.get('symbol'):<12} {extra}")
    if a.status or not (a.reset or a.run or a.mark or a.select):
        _print_summary(summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
