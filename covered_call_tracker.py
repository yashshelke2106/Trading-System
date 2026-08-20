"""
covered_call_tracker.py — PAPER forward test of the validated covered-call sleeve.

Purpose: covered_call_test.py passed the honest backtest (CC 2.5% OTM beat
buy&hold Sharpe in BOTH halves, 2019-2026), BUT its income is model-priced
(BS @ India VIX) and likely OPTIMISTIC vs real call quotes (skew). This tracker
closes that gap by paper-trading the exact rule forward and recording the
REALIZED market premium vs the MODEL premium each cycle.

THE RULE (fixed — the validated variant; do not tune):
  * Hold NIFTY (ETF proxy). Each cycle: sell 1 call at strike = spot * 1.025
    rounded to the 50-point NIFTY grid, MONTHLY expiry (holiday-adjusted last
    Thursday via core.nse_calendar). Hold to expiry, settle vs NIFTY close.
  * Credit booked = market LTP if a live quote exists, else model premium —
    both recorded, minus 5% premium cost either way.

WHAT IT WRITES:
  logs/cc_paper_state.json    — the one open cycle (idempotent: never two)
  logs/cc_paper_journal.jsonl — one line per settled cycle:
      model vs market premium (the validation metric), payoff, overlay return,
      buy&hold return, covered-call return.

DECISION RULE after 2-3 settled cycles:
  premium_ratio = market_ltp / model_prem_same_expiry, averaged.
  ~>= 0.8  -> model income roughly honest -> the MODEL is usable.
              (A calibration threshold, not a verdict on the sleeve: no
               hypothesis has been registered or closed for covered calls,
               so nothing here has cleared the statistician gate.)
  ~<  0.6  -> skew eats the edge -> stick to plain index holding.

Run daily (or whenever):  python covered_call_tracker.py
  --status    just show state + journal summary
  --selftest  verify open/settle math offline (no network)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from covered_call_test import bs_call, RISK_FREE  # reuse the validated pricing

OTM_PCT = 0.025          # the variant that passed (2.5% OTM)
STRIKE_STEP = 50         # NIFTY strike grid
COST_FRAC = 0.05         # 5% of premium (slippage+fees), as validated
MIN_DAYS_TO_EXPIRY = 7   # if monthly expiry is closer than this, roll to next month

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "logs" / "cc_paper_state.json"
JOURNAL_FILE = ROOT / "logs" / "cc_paper_journal.jsonl"


# ── pure logic (selftest-able, no I/O) ───────────────────────────────────────

def pick_strike(spot: float, otm: float = OTM_PCT, step: int = STRIKE_STEP) -> float:
    return round(spot * (1 + otm) / step) * step


def open_position(spot: float, iv: float, today: date, expiry: date,
                  chain_rows: Optional[List[Dict]] = None,
                  chain_expiry: Optional[date] = None) -> Dict:
    """Build the open-cycle state. Records model premium at the MONTHLY expiry
    (the position) AND, if a live chain is supplied, the market LTP at our
    strike with the model re-priced at the CHAIN's expiry (apples-to-apples)."""
    strike = pick_strike(spot)
    t_pos = max((expiry - today).days, 1) / 365.0
    model_prem = bs_call(spot, strike, iv, t_pos)

    market_ltp = None
    model_at_chain_exp = None
    premium_ratio = None
    if chain_rows and chain_expiry:
        for row in chain_rows:
            try:
                if abs(float(row.get("strike", 0)) - strike) < 0.01:
                    ltp = float(row.get("ce_ltp", 0) or 0)
                    if ltp > 0:
                        market_ltp = ltp
                    break
            except Exception:
                continue
        if market_ltp is not None:
            t_chain = max((chain_expiry - today).days, 1) / 365.0
            model_at_chain_exp = bs_call(spot, strike, iv, t_chain)
            if model_at_chain_exp > 0:
                premium_ratio = round(market_ltp / model_at_chain_exp, 3)

    return {
        "status": "open",
        "entry_date": today.isoformat(),
        "spot_entry": round(spot, 2),
        "strike": strike,
        "expiry": expiry.isoformat(),
        "iv_used": round(iv, 4),
        "model_prem": round(model_prem, 2),
        "market_ltp": market_ltp,
        "chain_expiry": chain_expiry.isoformat() if chain_expiry else None,
        "model_prem_at_chain_expiry": (round(model_at_chain_exp, 2)
                                       if model_at_chain_exp else None),
        "premium_ratio": premium_ratio,   # market/model — THE validation metric
        "otm_pct": OTM_PCT,
    }


def settle(state: Dict, expiry_close: float) -> Dict:
    """Settle an open cycle against the NIFTY close at expiry."""
    spot0 = float(state["spot_entry"])
    strike = float(state["strike"])
    # Credit: prefer the real quote (scaled is wrong — different expiry), so the
    # booked credit uses the MODEL monthly premium; the market quote is kept as
    # the validation metric, not the booked credit, unless chain expiry == expiry.
    credit_gross = float(state["model_prem"])
    if state.get("market_ltp") and state.get("chain_expiry") == state.get("expiry"):
        credit_gross = float(state["market_ltp"])
    credit = credit_gross * (1 - COST_FRAC)
    payoff = max(expiry_close - strike, 0.0)
    overlay_ret = (credit - payoff) / spot0
    bh_ret = (expiry_close - spot0) / spot0
    return {
        **state,
        "status": "settled",
        "settle_date": date.today().isoformat(),
        "spot_expiry": round(expiry_close, 2),
        "called_away": payoff > 0,
        "credit_booked": round(credit, 2),
        "payoff_owed": round(payoff, 2),
        "overlay_ret_pct": round(overlay_ret * 100, 3),
        "bh_ret_pct": round(bh_ret * 100, 3),
        "cc_ret_pct": round((bh_ret + overlay_ret) * 100, 3),
    }


# ── I/O helpers ──────────────────────────────────────────────────────────────

def _load_state() -> Optional[Dict]:
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            return s if s.get("status") == "open" else None
        except Exception:
            return None
    return None


def _save_state(state: Optional[Dict]) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state or {}, indent=2))


def _append_journal(rec: Dict) -> None:
    JOURNAL_FILE.parent.mkdir(exist_ok=True)
    with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def _journal_rows() -> List[Dict]:
    if not JOURNAL_FILE.exists():
        return []
    rows = []
    for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def print_summary() -> None:
    state = _load_state()
    rows = _journal_rows()
    print("=" * 64)
    print("  COVERED-CALL PAPER TRACKER — status")
    print("=" * 64)
    if state:
        print(f"  OPEN: sold {state['strike']} CE exp {state['expiry']} "
              f"(spot {state['spot_entry']}, model ₹{state['model_prem']}"
              + (f", market ₹{state['market_ltp']}, ratio {state['premium_ratio']}"
                 if state.get("market_ltp") else ", no live quote") + ")")
    else:
        print("  no open cycle")
    if rows:
        ratios = [r["premium_ratio"] for r in rows if r.get("premium_ratio")]
        cc = [r["cc_ret_pct"] for r in rows]
        bh = [r["bh_ret_pct"] for r in rows]
        print(f"  settled cycles: {len(rows)}")
        print(f"  CC mean {sum(cc)/len(cc):+.2f}%/cycle vs B&H {sum(bh)/len(bh):+.2f}%")
        if ratios:
            avg = sum(ratios) / len(ratios)
            print(f"  premium_ratio (market/model) avg: {avg:.2f} "
                  f"({'income model roughly honest' if avg >= 0.8 else 'model OVERSTATES income — discount the sleeve' if avg < 0.6 else 'borderline — keep collecting'})")
        else:
            print("  premium_ratio: no live quotes captured yet")
    else:
        print("  settled cycles: 0 (decision needs 2-3)")
    print("=" * 64)


# ── live cycle ───────────────────────────────────────────────────────────────

def run_cycle() -> int:
    from backtest_live_pipeline import fetch_daily
    today = date.today()
    state = _load_state()

    # 1) settle if expired
    if state:
        expiry = date.fromisoformat(state["expiry"])
        if today > expiry:
            nf = fetch_daily("NIFTY", 60)
            if nf is None or nf.empty:
                print("[CC] cannot settle — NIFTY data unavailable. Retry later.")
                return 1
            nf = nf[~nf.index.duplicated(keep="last")].sort_index()
            upto = nf[nf.index.date <= expiry] if hasattr(nf.index, "date") else nf
            upto = upto[upto.index.map(lambda x: x.date() <= expiry)]
            if upto.empty:
                print("[CC] no close on/before expiry yet. Retry later.")
                return 1
            rec = settle(state, float(upto["close"].iloc[-1]))
            _append_journal(rec)
            _save_state(None)
            print(f"[CC] SETTLED {rec['strike']} CE: spot {rec['spot_expiry']} "
                  f"{'CALLED' if rec['called_away'] else 'expired worthless'} | "
                  f"CC {rec['cc_ret_pct']:+.2f}% vs B&H {rec['bh_ret_pct']:+.2f}%")
            state = None
        else:
            print(f"[CC] cycle open until {expiry} — nothing to do.")
            print_summary()
            return 0

    # 2) open a new cycle
    nf = fetch_daily("NIFTY", 30)
    vx = fetch_daily("INDIAVIX", 30)
    if nf is None or nf.empty or vx is None or vx.empty:
        print("[CC] cannot open — NIFTY/VIX unavailable.")
        return 1
    spot = float(nf["close"].iloc[-1])
    iv = float(vx["close"].iloc[-1]) / 100.0

    try:
        from core.nse_calendar import nearest_monthly_expiry
        expiry = nearest_monthly_expiry(today)
        if (expiry - today).days < MIN_DAYS_TO_EXPIRY:
            expiry = nearest_monthly_expiry(expiry + timedelta(days=5))
    except Exception:
        # fallback: last Thursday of next month-ish
        nxt = (today.replace(day=1) + timedelta(days=62)).replace(day=1)
        d = nxt - timedelta(days=1)
        while d.weekday() != 3:
            d -= timedelta(days=1)
        expiry = d

    chain_rows, chain_expiry = None, None
    try:
        from core.dashboard_data import get_option_chain
        chain = get_option_chain("NIFTY")
        if chain:
            chain_rows = chain
            ce = str(chain[0].get("_expiry", "") or "")
            if ce:
                chain_expiry = date.fromisoformat(ce[:10])
    except Exception as e:
        print(f"[CC] option chain unavailable ({e}) — opening model-only cycle.")

    state = open_position(spot, iv, today, expiry, chain_rows, chain_expiry)
    _save_state(state)
    print(f"[CC] OPENED paper cycle: sell {state['strike']} CE exp {state['expiry']} "
          f"| spot {spot:.0f} VIX {iv*100:.1f}% | model ₹{state['model_prem']}"
          + (f" | market ₹{state['market_ltp']} (ratio {state['premium_ratio']})"
             if state.get("market_ltp") else " | no live quote"))
    print_summary()
    return 0


# ── selftest (offline) ───────────────────────────────────────────────────────

def selftest() -> int:
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        if not cond:
            fails += 1

    today = date(2026, 6, 10)
    expiry = date(2026, 6, 25)
    # strike rounding: 25000*1.025=25625 -> nearest 50 grid
    check("strike on 50-grid", pick_strike(25000) % 50 == 0)
    check("strike ~2.5% OTM", abs(pick_strike(25000) - 25625) <= 25)

    st = open_position(25000, 0.14, today, expiry)
    check("opens with positive model premium", st["model_prem"] > 0)
    check("no market quote -> ratio None", st["premium_ratio"] is None)

    # market quote path: chain at same strike, weekly expiry
    chain = [{"strike": pick_strike(25000), "ce_ltp": 80.0}]
    st2 = open_position(25000, 0.14, today, expiry, chain, date(2026, 6, 12))
    check("captures market LTP", st2["market_ltp"] == 80.0)
    check("ratio computed vs chain-expiry model",
          st2["premium_ratio"] is not None and st2["premium_ratio"] > 0)

    # settle below strike: keep credit, not called
    r1 = settle(st, 25100.0)
    check("expired worthless -> not called", r1["called_away"] is False)
    check("overlay positive when not called", r1["overlay_ret_pct"] > 0)
    check("cc = bh + overlay",
          abs(r1["cc_ret_pct"] - (r1["bh_ret_pct"] + r1["overlay_ret_pct"])) < 0.02)

    # settle far above strike: called away, upside capped vs B&H
    r2 = settle(st, 27000.0)
    check("called away above strike", r2["called_away"] is True)
    check("CC underperforms B&H in a melt-up", r2["cc_ret_pct"] < r2["bh_ret_pct"])
    # capped: CC return ~ (strike-spot+credit)/spot
    cap = (float(st["strike"]) - 25000 + r2["credit_booked"]) / 25000 * 100
    check("upside capped at strike+credit", abs(r2["cc_ret_pct"] - cap) < 0.05)

    print(f"\n  selftest: {'ALL PASS' if fails == 0 else str(fails) + ' FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    if args.status:
        print_summary()
        sys.exit(0)
    sys.exit(run_cycle())
