"""
swing_screen.py — daily EOD swing screen (7-gate framework), DIRECTION-
SYMMETRIC, with a reinforcement-style learner re-ranking candidates.

LONGS : NIFTY > 200-DMA, stock above its rising 200-DMA, fresh pullback
        (2% under 5-DMA / 3 down closes / RSI-2 < 10).
SHORTS: exact mirror — NIFTY < 200-DMA, stock below its falling 200-DMA,
        fresh rally (2% over 5-DMA / 3 up closes / RSI-2 > 90).
        NOTE: Indian cash market allows no overnight retail shorts —
        short swings execute via STOCK FUTURES (cost ~0.10% RT, lot sizes).

Geometry both ways: enter NEXT open, target 2xATR, stop 2xATR, 10-session
time exit. Learner (core/swing_learner.py) multiplies each candidate's rank
by its bucket weight — buckets earn/lose trust from resolved outcomes only.

    python swing_screen.py                # screen + ranked candidates
    python swing_screen.py --json         # also write logs/swing_screen.json
    python swing_screen.py --journal      # also journal today's tradeable
                                          # candidates for paper tracking
    python swing_screen.py --learner      # print learner bucket report
"""

from __future__ import annotations

import argparse
import json
import os
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from core.swing_learner import SwingLearner
from core.universe import TOP100_LIQUID

YMAP = {"MCDOWELL-N": "UNITDSPR"}
MIN_TARGET_PCT = 2.0
JOURNAL_FILE = os.path.join("logs", "swing_paper_journal.jsonl")

# ── trade geometry (ATR multiples) ───────────────────────────────────────
# TARGET is informational under exit_style "C": swing_tracker guards every
# target check with `if not style_c`, so the reward leg is the 5-DMA momentum
# exit, not this level. It still drives MIN_TARGET_PCT and the rank, so it
# stays at 2x.
#
# STOP widened 2x -> 4x on 2026-09-04. Measured on 17,313 long signals across
# the 52 symbols with an unambiguous price history (2017-2026), re-resolved
# under the SHIPPING exit (style-C momentum exit for winners). PF is monotone
# in stop depth:  2x 1.138 | 3x 1.218 | 4x 1.274 | 5x 1.329 | 6x 1.343.
# The 2x stop fired on 42.6% of trades and cost +1.19% on every trade it
# touched; 22% of stopped-out positions were profitable 20 sessions later.
# A stop set inside the noise of a mean-reversion entry liquidates exactly
# when the thesis is strongest. Stopping at 4x rather than 6x is deliberate:
# past 4x the PF curve is nearly flat while the worst single trade keeps
# growing (-19.2% at 2x, -31.4% at 4x, -35.5% at 6x).
TARGET_ATR_MULT = 2.0
STOP_ATR_MULT = 4.0

# ── portfolio caps ───────────────────────────────────────────────────────
# Nothing counted open positions before 2026-09-04; the paper book peaked at
# 78 concurrent rows and held 7 Adani-group names on the day it lost 60.7
# points. The portfolio arithmetic behind the 4x stop assumes 10 positions.
MAX_CONCURRENT = 10      # total funded positions held at once
MAX_PER_GROUP = 2        # per correlation group (core/symbol_groups.py)


def _rsi(series: pd.Series, n: int = 2) -> pd.Series:
    d = series.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def fetch(period: str = "2y") -> dict:
    import yfinance as yf
    tickers = {s: YMAP.get(s, s) + ".NS" for s in TOP100_LIQUID}
    raw = yf.download(list(tickers.values()), period=period, group_by="ticker",
                      auto_adjust=True, threads=True, progress=False)
    out = {}
    for sym, tk in tickers.items():
        try:
            d = raw[tk][["Open", "High", "Low", "Close", "Volume"]].dropna()
            if len(d) >= 260:
                d.columns = ["open", "high", "low", "close", "volume"]
                out[sym] = d
        except Exception:
            pass
    return out


def regime() -> tuple:
    import yfinance as yf
    n = yf.download("^NSEI", period="2y", auto_adjust=True, progress=False)["Close"]
    if isinstance(n, pd.DataFrame):
        n = n.iloc[:, 0]
    ma = n.rolling(200).mean()
    return float(n.iloc[-1]), float(ma.iloc[-1])


def screen(data: dict, learner: SwingLearner = None, regime_state: str = "risk_on") -> list:
    """Symmetric candidate scan. Returns BOTH directions; caller applies the
    regime gate. Rank = target% x learner bucket weight."""
    learner = learner or SwingLearner()
    cands = []
    for sym, df in data.items():
        tr = np.maximum(df.high - df.low,
             np.maximum((df.high - df.close.shift()).abs(),
                        (df.low - df.close.shift()).abs()))
        atr = tr.rolling(14).mean()
        ma200 = df.close.rolling(200).mean()
        ma5 = df.close.rolling(5).mean()
        rsi2 = _rsi(df.close, 2)
        dchg = df.close.diff()
        dn3 = (dchg < 0).rolling(3).sum() == 3
        up3 = (dchg > 0).rolling(3).sum() == 3
        c, a = df.close.iloc[-1], atr.iloc[-1]
        m2, m5v = ma200.iloc[-1], ma5.iloc[-1]
        if pd.isna(a) or pd.isna(m2):
            continue
        tgt_pct = TARGET_ATR_MULT * a / c * 100
        if tgt_pct < MIN_TARGET_PCT:
            continue

        # identical logic, mirrored — no direction is privileged
        setups = []
        if c > m2 and m2 > ma200.iloc[-21]:            # own uptrend
            if c < m5v * 0.98: setups.append(("long", "2pct_under_5dma"))
            if bool(dn3.iloc[-1]): setups.append(("long", "3_down_days"))
            if rsi2.iloc[-1] < 10: setups.append(("long", "rsi2_oversold"))
        if c < m2 and m2 < ma200.iloc[-21]:            # own downtrend
            if c > m5v * 1.02: setups.append(("short", "2pct_over_5dma"))
            if bool(up3.iloc[-1]): setups.append(("short", "3_up_days"))
            if rsi2.iloc[-1] > 90: setups.append(("short", "rsi2_overbought"))

        for direction, signal in setups[:1]:           # one candidate per symbol
            sign = 1 if direction == "long" else -1
            w = learner.weight(direction, signal, regime_state)
            cands.append({
                "symbol": sym, "direction": direction, "signal": signal,
                "close": round(float(c), 2),
                "target": round(float(c + sign * TARGET_ATR_MULT * a), 2),
                "stop": round(float(c - sign * STOP_ATR_MULT * a), 2),
                "target_pct": round(float(tgt_pct), 2),
                "weight": w,
                "rank": round(float(tgt_pct) * w, 2),
                "bar": str(df.index[-1].date()),
            })
    cands.sort(key=lambda x: -x["rank"])
    return cands


def open_funded_symbols() -> list:
    """Symbols already held on the FUNDED side, from the paper journal.

    Bench rows are excluded on purpose: they consume no capital, so capping
    against them would starve the funded book of the very trades the caps are
    meant to protect."""
    if not os.path.exists(JOURNAL_FILE):
        return []
    out = []
    for line in open(JOURNAL_FILE, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("status") == "open" and r.get("fundable"):
            out.append(r["symbol"])
    return out


def apply_portfolio_caps(cands: list, held: list) -> list:
    """Demote fundable candidates that breach a portfolio cap.

    Three caps, applied to candidates in rank order (highest rank keeps the
    slot). Each is a distinct failure seen in the 2026-08-31 book:

      one per symbol  -- that book held six symbols twice, entered from two
                         different signals, and stopped out of both legs.
      MAX_PER_GROUP   -- it also held seven Adani-group names, which is one
                         bet wearing seven tickets (core/symbol_groups.py).
      MAX_CONCURRENT  -- and peaked at 78 concurrent rows, against portfolio
                         arithmetic that assumes 10.

    Capped candidates are set fundable=False with a `cap_reason`, so they
    still reach the paper bench and the learner. Nothing is silently dropped,
    and the reason is journaled so the cap's own cost stays measurable.
    Returns the surviving funded list; mutates candidates in place."""
    from core.symbol_groups import group_of

    slots = MAX_CONCURRENT - len(held)
    per_group: dict = {}
    for s in held:
        per_group[group_of(s)] = per_group.get(group_of(s), 0) + 1
    seen = set(held)

    kept = []
    for x in cands:
        if not x.get("fundable"):
            continue
        sym, grp = x["symbol"], group_of(x["symbol"])
        if sym in seen:
            reason = "already held"
        elif per_group.get(grp, 0) >= MAX_PER_GROUP:
            reason = f"group {grp} at cap {MAX_PER_GROUP}"
        elif len(kept) >= slots:
            reason = f"book at cap {MAX_CONCURRENT} ({len(held)} held)"
        else:
            seen.add(sym)
            per_group[grp] = per_group.get(grp, 0) + 1
            kept.append(x)
            continue
        x["fundable"] = False
        x["cap_reason"] = reason
    return kept


def journalable(cands: list, regime_state: str) -> list:
    """Which candidates enter the paper journal: ALL of them, both directions,
    every regime. Journaling is paper-only data collection for the learner —
    funding is a separate decision (the `fundable` flag). Journaling only the
    regime side starved the journal of longs during risk-off weeks (22/22
    short rows by 2026-07-15), which read as short bias and left the learner
    with zero long-side evidence."""
    return list(cands)


def journal_candidates(cands: list, regime_state: str) -> int:
    """Append today's regime-allowed candidates as OPEN paper trades. The
    tracker resolves them later and feeds the learner — win AND loss alike."""
    os.makedirs("logs", exist_ok=True)
    seen = set()
    if os.path.exists(JOURNAL_FILE):
        for line in open(JOURNAL_FILE, encoding="utf-8"):
            try:
                r = json.loads(line)
                seen.add((r["symbol"], r["signal_date"]))
            except Exception:
                pass
    n = 0
    with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
        for x in cands:
            key = (x["symbol"], x["bar"])
            if key in seen:
                continue
            f.write(json.dumps({**x, "signal_date": x["bar"],
                                "regime": regime_state, "status": "open",
                                "exit_style": "C",   # adopted 2026-07-15: winners ride until close crosses 5DMA
                                "journaled": datetime.now().isoformat(timespec="seconds")}) + "\n")
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--journal", action="store_true",
                    help="journal today's tradeable candidates (paper)")
    ap.add_argument("--learner", action="store_true",
                    help="print learner bucket report and exit")
    args = ap.parse_args()

    learner = SwingLearner()
    if args.learner:
        print(learner.report())
        return 0

    nifty, ma = regime()
    dist = (nifty / ma - 1) * 100
    regime_state = "risk_on" if nifty > ma else "risk_off"
    allowed = "long" if regime_state == "risk_on" else "short"

    print("=" * 78)
    print("SWING SCREEN - symmetric 7-gate framework + outcome learner")
    print("=" * 78)

    # decay monitor (stage 7): if the strategy is RETIRED, nothing is funded
    from core.strategy_health import health_line, load_health
    health = load_health()
    print(health_line(health))
    retired = bool(health and health.get("status") == "RETIRED")
    if retired:
        print("*** STRATEGY RETIRED by pre-registered decay rule (rolling PF < 0.90).")
        print("*** Candidates below are PAPER/BENCH ONLY. Fund nothing until the")
        print("*** monitor reinstates (rolling PF >= 1.05). Core stays in allocation.")
    print(f"Gate 1  NIFTY {nifty:.0f} vs 200DMA {ma:.0f} ({dist:+.2f}%) -> "
          f"{regime_state.upper()}")

    data = fetch()
    cands = screen(data, learner, regime_state)

    # ── FUNDING POLICY (structural, single source of truth) ──────────────
    # Shorts are NEVER a funded recommendation: 15y evidence (2,816 trades)
    # = PF 0.65, -125bp/trade net. They stay on the paper bench so the
    # learner can falsify/confirm; docs/research/short_side_policy.md holds
    # the unlock condition. Risk-off therefore means STAND ASIDE, not short.
    for x in cands:
        x["fundable"] = (x["direction"] == "long"
                         and regime_state == "risk_on" and not retired)
    # Portfolio caps run AFTER the direction/regime gate and BEFORE anything
    # is called funded: one per symbol, MAX_PER_GROUP per correlation group,
    # MAX_CONCURRENT in the book. Added 2026-09-04 — see apply_portfolio_caps.
    held = open_funded_symbols()
    funded = apply_portfolio_caps(cands, held)
    capped = [x for x in cands if x.get("cap_reason")]
    bench = [x for x in cands if not x["fundable"]]
    funded_action = ("long" if funded else "stand_aside")

    hdr = (f"{'#':>2} {'SYMBOL':<13}{'DIR':<6}{'SIGNAL':<18}"
           f"{'CLOSE':>9}{'TARGET':>9}{'STOP':>9}{'MOVE%':>7}{'W':>6}")

    if funded:
        print(f"\nFUNDED CANDIDATES (long): {len(funded)}")
        print(hdr); print("-" * len(hdr))
        for i, x in enumerate(funded[:15], 1):
            print(f"{i:>2} {x['symbol']:<13}{x['direction']:<6}{x['signal']:<18}"
                  f"{x['close']:>9}{x['target']:>9}{x['stop']:>9}"
                  f"{x['target_pct']:>6.1f}%{x['weight']:>6.2f}")
    else:
        why = ("strategy RETIRED" if retired else
               "risk-off regime - shorts are net-negative over 15y, not funded")
        print(f"\nFUNDED ACTION: NONE - STAND ASIDE ({why}).")
        print("Cash sits per the allocation engine. No funded swing trades today.")

    if capped:
        print(f"\nBLOCKED BY PORTFOLIO CAP: {len(capped)} setups "
              f"({len(held)} funded positions already open, cap {MAX_CONCURRENT})")
        for i, x in enumerate(capped[:10], 1):
            print(f"{i:>2} {x['symbol']:<13}{x['direction']:<6}"
                  f"{x['signal']:<18}-> {x['cap_reason']}")

    if bench:
        print(f"\nPAPER BENCH (learner only - DO NOT FUND): {len(bench)} setups")
        print(hdr); print("-" * len(hdr))
        for i, x in enumerate(bench[:10], 1):
            print(f"{i:>2} {x['symbol']:<13}{x['direction']:<6}{x['signal']:<18}"
                  f"{x['close']:>9}{x['target']:>9}{x['stop']:>9}"
                  f"{x['target_pct']:>6.1f}%{x['weight']:>6.2f}")

    if args.journal:
        # journal BOTH sides, every regime — paper data collection for the
        # learner. Funding remains a separate decision (fundable flag).
        to_journal = journalable(cands, regime_state)
        if to_journal:
            n = journal_candidates(to_journal, regime_state)
            print(f"\njournaled {n} new paper trades -> {JOURNAL_FILE} "
                  f"({sum(1 for x in to_journal if x['direction']=='long')} long / "
                  f"{sum(1 for x in to_journal if x['direction']=='short')} short)")
            print("resolve + learn with: python swing_tracker.py")

    if args.json:
        with open(os.path.join("logs", "swing_screen.json"), "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.now().isoformat(timespec="seconds"),
                       "regime": regime_state, "nifty": round(nifty, 1),
                       "ma200": round(ma, 1),
                       "funded_action": funded_action,
                       "candidates": cands}, f, indent=2)
        print("written -> logs/swing_screen.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
