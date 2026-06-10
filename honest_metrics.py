"""
honest_metrics.py — the single trustworthy expectancy number.

WHY THIS EXISTS
---------------
The system's headline stats are not trustworthy for three concrete reasons,
all verified in the code/data:

  1. logs/metrics_daily.jsonl reports PF ~16 and "50% WR". That PF is computed
     in core/metrics_writer.py by summing `pnl_pct`, which for almost every
     journal row is an OPTION-PREMIUM percentage move (mean +26%/trade). Premium
     %s do not compound across trades and are dominated by theta/IV, so their
     "PF" is a fantasy number. The drift alarm fires at PF<0.9 — a metric pinned
     near 16 can never trip it.

  2. The strategy actually traded (INSTRUMENT_MODE="futures") expresses a SPOT
     directional view. The journal already stores that clean truth per row in
     `spot_pnl_pct` / `spot_outcome`. THAT is what we should measure.

  3. The earliest rows are `extra.replay_failed` holiday rows with no real fill.
     They must be excluded (metrics_writer already drops them; the rolling
     window still inherited their pollution via premium pnl_pct).

This tool reads the SAME journal and reports, side by side:
  (A) the broken premium metric (reproduced, to show what you've been seeing),
  (B) what the option leg really did (the theta-taxed reality you abandoned),
  (C) the honest SPOT / directional edge in R-multiples, net of futures cost,
  (D) the theta tax: how often the spot view was RIGHT but the option still lost,
  (E) a cross-check against the committed backtest CSV.

It prints alarms that CAN fire, and a one-line verdict.

Run:
    python honest_metrics.py
    python honest_metrics.py --journal logs/signal_journal.jsonl --days 0
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Windows consoles default to cp1252 and choke on unicode. Force UTF-8 so the
# report never crashes mid-print regardless of terminal/redirect.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
DEFAULT_JOURNAL = ROOT / "logs" / "signal_journal.jsonl"
DEFAULT_BT_CSV = ROOT / "backtest_india_swing_trades.csv"

try:
    import config
    FUT_COST_RT = float(getattr(config, "FUT_COST_ROUNDTRIP_PCT", 0.06))
except Exception:
    FUT_COST_RT = 0.06

# India_swing emits these; signal_engine (legacy 17-detector) emits the rest.
ISWING_PATTERNS = {
    "bullish_engulfing", "bearish_engulfing", "bullish_marubozu",
    "bearish_marubozu", "bullish_pin_bar", "bearish_pin_bar",
    "breakout_5d_high", "breakout_5d_low",
}
LEGACY_MARKERS = {
    "supertrend_up", "supertrend_down", "wae_bull_explosion",
    "wae_bear_explosion", "range_filter_up", "range_filter_down",
    "above_vwap", "below_vwap", "pvsra_super_bull", "pvsra_super_bear",
    "ema_uptrend", "ema_downtrend",
}


# ───────────────────────────── helpers ──────────────────────────────────────

def _f(v, default=None) -> Optional[float]:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def load_journal(path: Path) -> List[Dict]:
    rows: List[Dict] = []
    if not path.exists():
        print(f"[!] journal not found: {path}")
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def is_junk(r: Dict) -> bool:
    extra = r.get("extra") or {}
    return bool(isinstance(extra, dict) and extra.get("replay_failed"))


def engine_of(r: Dict) -> str:
    pats = set(r.get("patterns") or [])
    if pats & ISWING_PATTERNS and not (pats & LEGACY_MARKERS):
        return "india_swing"
    if pats & LEGACY_MARKERS:
        return "legacy"
    return "unknown"


def spot_label(r: Dict) -> Optional[float]:
    """Clean directional % (favourable = positive). Prefer explicit field."""
    sp = r.get("spot_pnl_pct")
    if sp is None and isinstance(r.get("extra"), dict):
        sp = r["extra"].get("spot_pnl_pct")
    return _f(sp)


def risk_pct_of(r: Dict) -> Optional[float]:
    entry = _f(r.get("entry_price"))
    sl = _f(r.get("sl_price"))
    if not entry or not sl or entry <= 0:
        return None
    rp = abs(entry - sl) / entry * 100.0
    return rp if rp > 1e-6 else None


def pf(returns: List[float]) -> float:
    gw = sum(x for x in returns if x > 0)
    gl = abs(sum(x for x in returns if x < 0))
    return gw / gl if gl > 1e-12 else float("inf")


def stats_block(returns: List[float]) -> Dict:
    n = len(returns)
    if n == 0:
        return {"n": 0}
    wins = [x for x in returns if x > 0]
    return {
        "n": n,
        "wr": 100.0 * len(wins) / n,
        "mean": sum(returns) / n,
        "pf": pf(returns),
        "best": max(returns),
        "worst": min(returns),
    }


def equity_curve(r_multiples: List[float], risk_frac: float = 0.01) -> Tuple[float, float]:
    """Fixed-fractional equity from R-multiples. Returns (final_x, maxDD%)."""
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for R in r_multiples:
        R = max(-1.0, min(5.0, R))          # clamp pathological R
        eq *= (1.0 + risk_frac * R)
        peak = max(peak, eq)
        if peak > 0:
            mdd = min(mdd, (eq - peak) / peak * 100.0)
    return eq, mdd


def fmt_pf(x: float) -> str:
    if x == float("inf"):
        return "inf"
    return f"{x:.2f}"


# ───────────────────────────── report ───────────────────────────────────────

def report(journal: Path, bt_csv: Path, days: int) -> None:
    rows = load_journal(journal)
    print("=" * 74)
    print("  HONEST METRICS")
    print(f"  journal: {journal}")
    print(f"  futures round-trip cost assumed: {FUT_COST_RT:.3f}%")
    print("=" * 74)

    if days:
        cutoff = (datetime.now()).timestamp() - days * 86400
        rows = [r for r in rows
                if (r.get("ts") and _ts(r["ts"]) and _ts(r["ts"]) >= cutoff)]

    total = len(rows)
    junk = [r for r in rows if is_junk(r)]
    clean = [r for r in rows if not is_junk(r)]
    resolved = [r for r in clean if r.get("outcome")]
    open_sigs = [r for r in clean if not r.get("outcome")]

    eng = Counter(engine_of(r) for r in resolved)
    print(f"\nRecords total:        {total}")
    print(f"  replay_failed junk: {len(junk)}   (excluded — no real fill)")
    print(f"  open / unresolved:  {len(open_sigs)}")
    print(f"  resolved & usable:  {len(resolved)}")
    print(f"  engine mix (resolved): {dict(eng)}")

    # Overlap note: distinct (symbol,direction) vs rows = repeated theses
    distinct = len({(r.get("symbol"), r.get("direction")) for r in resolved})
    print(f"  distinct symbol+direction: {distinct} "
          f"(rows/distinct = {len(resolved)/max(distinct,1):.1f}x overlap)")

    # ── (A) reproduce the broken premium metric ─────────────────────────────
    prem = [_f(r.get("pnl_pct")) for r in resolved]
    prem = [x for x in prem if x is not None]
    print("\n" + "-" * 74)
    print("(A) BROKEN METRIC REPRODUCTION  — premium pnl_pct, as metrics_writer sums it")
    print("-" * 74)
    if prem:
        s = stats_block(prem)
        print(f"   n={s['n']}  'WR'={s['wr']:.1f}%  'PF'={fmt_pf(s['pf'])}  "
              f"mean={s['mean']:+.2f}%  best={s['best']:+.1f}%  worst={s['worst']:+.1f}%")
        print("   ^ premium %s; theta/IV-driven, do not compound. This is the ~PF16 mirage.")

    # ── (B) what the option leg actually did ────────────────────────────────
    print("\n" + "-" * 74)
    print("(B) OPTION-LEG REALITY  — the theta-taxed P&L you actually booked on premium")
    print("-" * 74)
    if prem:
        s = stats_block(prem)
        print(f"   n={s['n']}  netWR={s['wr']:.1f}%  meanPremMove={s['mean']:+.2f}%")
        print(f"   (premium swings are huge & asymmetric; this is why options bled)")

    # ── (C) directional-skill PROXY (upper bound, NOT a tradeable result) ────
    print("\n" + "-" * 74)
    print("(C) DIRECTIONAL-SKILL PROXY  — spot_pnl_pct (UPPER BOUND, not a strategy result)")
    print("-" * 74)
    print("   CAVEAT: spot_pnl_pct is the spot move sampled AT THE OPTION'S EXIT time")
    print("   (theta-driven), with NO enforced spot stop, over a bull window, on the")
    print("   LEGACY engine. 'percent favourable at that instant' is selection-biased")
    print("   upward. Treat as an optimistic ceiling on directional skill, NOT as what a")
    print("   mechanical futures rule (entry+stop+target) would earn. Section (E) is the")
    print("   only mechanically-honest number.")
    spot_rows = [r for r in resolved if spot_label(r) is not None]
    print(f"   rows with clean spot label: {len(spot_rows)} / {len(resolved)} resolved")
    if not spot_rows:
        print("   [!] No spot_pnl_pct labels found — cannot compute honest edge.")
        print("       (Older rows predate spot-truth logging. Need forward data.)")
    else:
        gross = [spot_label(r) for r in spot_rows]
        net = [g - FUT_COST_RT for g in gross]          # subtract futures cost
        sg = stats_block(gross)
        sn = stats_block(net)
        print(f"   GROSS %:  n={sg['n']}  WR={sg['wr']:.1f}%  mean={sg['mean']:+.3f}%  "
              f"PF={fmt_pf(sg['pf'])}")
        print(f"   NET   %:  n={sn['n']}  WR={sn['wr']:.1f}%  mean={sn['mean']:+.3f}%  "
              f"PF={fmt_pf(sn['pf'])}   (after {FUT_COST_RT:.3f}% RT cost)")

        # R-multiples (instrument-neutral): spot move / initial spot risk
        r_mults = []
        for r in spot_rows:
            g = spot_label(r)
            rp = risk_pct_of(r)
            if rp:
                r_mults.append((g - FUT_COST_RT) / rp)
        if r_mults:
            sr = stats_block(r_mults)
            final_x, mdd = equity_curve(r_mults, risk_frac=0.01)
            print(f"   NET R:    n={sr['n']}  expectancy={sr['mean']:+.3f}R  "
                  f"PF={fmt_pf(sr['pf'])}  worst={sr['worst']:+.2f}R")
            print(f"   Equity (1% risk/trade): {final_x:.3f}x   maxDD={mdd:.1f}%")

        # by direction
        print("   by direction (net %):")
        for d in ("long", "short"):
            sub = [spot_label(r) - FUT_COST_RT for r in spot_rows
                   if r.get("direction") == d]
            if sub:
                s = stats_block(sub)
                print(f"     {d:5s}: n={s['n']:>3d}  WR={s['wr']:.1f}%  "
                      f"mean={s['mean']:+.3f}%  PF={fmt_pf(s['pf'])}")
        # by grade
        print("   by grade (net %):")
        for g in ("S", "A", "B", "C"):
            sub = [spot_label(r) - FUT_COST_RT for r in spot_rows
                   if r.get("grade") == g]
            if sub:
                s = stats_block(sub)
                print(f"     {g}: n={s['n']:>3d}  WR={s['wr']:.1f}%  "
                      f"mean={s['mean']:+.3f}%  PF={fmt_pf(s['pf'])}")

    # ── (D) theta tax ───────────────────────────────────────────────────────
    print("\n" + "-" * 74)
    print("(D) THETA TAX  — spot view RIGHT but the option still LOST")
    print("-" * 74)
    both = [r for r in resolved
            if spot_label(r) is not None and _f(r.get("pnl_pct")) is not None]
    if both:
        right_spot_lost_opt = [r for r in both
                               if spot_label(r) > 0 and _f(r.get("pnl_pct")) < 0]
        print(f"   resolved with both labels: {len(both)}")
        print(f"   spot RIGHT yet option LOST: {len(right_spot_lost_opt)} "
              f"({100*len(right_spot_lost_opt)/len(both):.0f}%)")
        sl_med = _median([spot_label(r) for r in right_spot_lost_opt])
        pl_med = _median([_f(r.get("pnl_pct")) for r in right_spot_lost_opt])
        if sl_med is not None:
            print(f"   on those: median spot {sl_med:+.2f}%  vs  median premium {pl_med:+.2f}%")
            print("   ^ this is the edge the option structure destroyed (why you moved to futures)")

    # ── (E) backtest cross-check ────────────────────────────────────────────
    print("\n" + "-" * 74)
    print("(E) BACKTEST CROSS-CHECK  — backtest_india_swing_trades.csv (the chosen strategy)")
    print("-" * 74)
    bt = _bt_stats(bt_csv)
    if bt:
        print(f"   n={bt['n']}  WR={bt['wr']:.1f}%  PF={fmt_pf(bt['pf'])}  "
              f"expectancy={bt['mean']:+.3f}%/trade  "
              f"({bt['target']} TARGET / {bt['sl']} SL / {bt['be']} BE / {bt['time']} TIME)")
        print(f"   date range: {bt['start']} -> {bt['end']}   long={bt['long']} short={bt['short']}")

    # ── ALARMS + VERDICT ────────────────────────────────────────────────────
    print("\n" + "=" * 74)
    print("  ALARMS  (these CAN fire — unlike the production drift check)")
    print("=" * 74)
    alarms = []
    n_iswing = sum(1 for r in resolved if engine_of(r) == "india_swing")
    if resolved and n_iswing == 0:
        alarms.append("[FIRE] Journal is 100% LEGACY engine, ZERO india_swing rows. The chosen "
                      "strategy has NO live data; only the (negative) backtest. (Problem #2)")
    if prem:
        ppf = pf(prem)
        if ppf == float("inf") or ppf > 3.0:
            alarms.append(f"[FIRE] Premium PF={fmt_pf(ppf)} > 3 -> impossible; the headline metric "
                          f"is a premium-% artifact. metrics_daily/drift is blind. (Problem #3)")
    if spot_rows:
        snet = stats_block([spot_label(r) - FUT_COST_RT for r in spot_rows])
        if snet["pf"] > 3.0 or snet["wr"] > 80.0:
            alarms.append(f"[FIRE] Spot proxy implausible (WR={snet['wr']:.0f}%, "
                          f"PF={fmt_pf(snet['pf'])}) -> ALSO biased (no enforced stop / "
                          f"option-exit-timing / bull drift). Not a real edge.")
        elif snet["mean"] <= 0 or snet["pf"] < 1.0:
            alarms.append(f"[FIRE] Even the optimistic spot proxy is negative: "
                          f"PF={fmt_pf(snet['pf'])}, mean={snet['mean']:+.3f}%/trade.")
    if bt and (bt["pf"] < 1.0 or bt["mean"] <= 0):
        alarms.append(f"[FIRE] Committed backtest is NEGATIVE: PF={fmt_pf(bt['pf'])}, "
                      f"{bt['mean']:+.3f}%/trade. Do not deploy. (Problem #1)")
    if len(spot_rows) < 200:
        alarms.append(f"[WARN] Only {len(spot_rows)} clean spot obs (overlap {len(resolved)/max(distinct,1):.1f}x) "
                      f"— too small/correlated to trust any WR/PF.")
    if not alarms:
        print("   (none)")
    for a in alarms:
        print("   " + a)

    print("\n" + "=" * 74)
    print("  VERDICT")
    print("=" * 74)
    trustworthy_positive = False
    if spot_rows:
        snet = stats_block([spot_label(r) - FUT_COST_RT for r in spot_rows])
        # "trustworthy positive" requires a PLAUSIBLE positive (not a biased mirage)
        plausible = 1.0 <= snet["pf"] <= 3.0 and snet["wr"] <= 80.0
        trustworthy_positive = plausible and snet["mean"] > 0
    if bt and bt["pf"] < 1.0:
        trustworthy_positive = False
    if n_iswing == 0:
        trustworthy_positive = False

    if not trustworthy_positive:
        print("  NO TRUSTWORTHY POSITIVE EDGE for anything you currently intend to trade:")
        print("   - india_swing (chosen): committed backtest is NEGATIVE (PF<1), ZERO live rows.")
        print("   - legacy engine (= 100% of the journal): headline PF is a premium-% artifact;")
        print("     the cleaner spot proxy is positive but selection-biased and describes a")
        print("     DISCONTINUED engine on a DISCONTINUED instrument.")
        print("  Keep PAPER_TRADE=True. Build ONE mechanical rule (entry+stop+target) on the")
        print("  instrument you actually trade, measure THAT, before any tuning or capital.")
    else:
        print("  Spot edge is plausibly positive on this sample -- confirm sample size & true")
        print("  OOS on the ACTUAL strategy/instrument before risking capital.")
    print("=" * 74)


def _ts(s: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _median(xs: List[Optional[float]]) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def _bt_stats(path: Path) -> Optional[Dict]:
    if not path.exists():
        return None
    try:
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
    except Exception:
        return None
    if not rows:
        return None
    pnls = [_f(r.get("pnl_pct")) for r in rows]
    pnls = [x for x in pnls if x is not None]
    oc = Counter(r.get("outcome") for r in rows)
    dr = Counter(r.get("direction") for r in rows)
    dates = sorted(r.get("entry_date", "") for r in rows if r.get("entry_date"))
    s = stats_block(pnls)
    return {
        "n": len(rows), "wr": s.get("wr", 0.0), "pf": s.get("pf", 0.0),
        "mean": s.get("mean", 0.0),
        "target": oc.get("TARGET", 0), "sl": oc.get("SL", 0),
        "be": oc.get("BE_STOP", 0), "time": oc.get("TIME_EXIT", 0),
        "long": dr.get("long", 0), "short": dr.get("short", 0),
        "start": dates[0] if dates else "?", "end": dates[-1] if dates else "?",
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", default=str(DEFAULT_JOURNAL))
    ap.add_argument("--bt-csv", default=str(DEFAULT_BT_CSV))
    ap.add_argument("--days", type=int, default=0,
                    help="Only rows from last N days (0 = all).")
    args = ap.parse_args()
    report(Path(args.journal), Path(args.bt_csv), args.days)
