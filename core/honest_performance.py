"""
honest_performance.py — the ONE trustworthy performance gate.

PERMANENT FIX for the recurring mirage (PF ~16, +26000% "returns"). The journal
stores an option-PREMIUM `pnl_pct` (theta/IV-polluted, mean +26%/trade, never
compounds) AND a clean spot move. Every dashboard that read `pnl_pct` showed a
fantasy. This module is the single source of truth: it computes headline metrics
ONLY from trustworthy per-row signals, NEVER falls back to premium, and — the key
guarantee — REFUSES to emit a number it can't trust (returns trustworthy=False
with a reason instead of a mirage). Wire every stats surface through this.

Trust priority per row:
  1. `spot_pnl_pct`            — the theta/IV-denoised real spot/futures move (best)
  2. derived spot move         — dir*(exit_price/entry-1) if a real fill exists
  3. EXCLUDED                  — replay_failed, premium-only, or implausible gap
Premium `pnl_pct` is NEVER used for headline PF/expectancy.

    from core.honest_performance import honest_performance
    p = honest_performance(rows)              # rows = parsed signal_journal dicts
    if p.trustworthy: show(p.profit_factor, p.expectancy_pct, ...)
    else:             show(p.note)            # e.g. "insufficient clean data (n=20<30)"

Self-check on the live journal:
    .venv/Scripts/python.exe -m core.honest_performance
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

MAX_PLAUSIBLE_MOVE = 50.0   # % single-trade spot move above this = corporate-action artifact
MIN_CLEAN_DEFAULT = 30      # below this, no headline is trustworthy
PF_IMPLAUSIBLE = 3.0        # daily-bar retail PF above this is almost always an artifact


@dataclass
class Perf:
    n_total: int = 0
    n_clean: int = 0
    n_excluded: int = 0
    source_counts: Dict[str, int] = field(default_factory=dict)  # spot_field / derived / excluded
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    expectancy_pct: Optional[float] = None
    median_pct: Optional[float] = None
    total_pct: Optional[float] = None
    trustworthy: bool = False
    alarms: List[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> Dict:
        return {
            "trustworthy": self.trustworthy, "note": self.note,
            "n_clean": self.n_clean, "n_excluded": self.n_excluded,
            "win_rate": self.win_rate, "profit_factor": self.profit_factor,
            "expectancy_pct": self.expectancy_pct, "median_pct": self.median_pct,
            "total_pct": self.total_pct, "alarms": self.alarms,
            "source_counts": self.source_counts,
        }


def _num(v) -> Optional[float]:
    return v if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)) else None


def _clean_move_pct(row: Dict) -> "tuple[Optional[float], str]":
    """Return (clean_spot_%_move, source). source in {spot_field, derived, excluded}.
    NEVER returns the premium pnl_pct."""
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    if extra.get("replay_failed"):
        return None, "excluded"
    sp = _num(row.get("spot_pnl_pct"))
    if sp is None:
        sp = _num(extra.get("spot_pnl_pct"))
    if sp is not None and abs(sp) <= MAX_PLAUSIBLE_MOVE:
        return sp, "spot_field"
    # derive from a real fill: dir * (exit/entry - 1)
    e, x = _num(row.get("entry_price")), _num(row.get("exit_price"))
    if e and x and e > 0 and x != e:
        dirn = 1.0 if str(row.get("direction", "long")).lower() == "long" else -1.0
        mv = dirn * (x / e - 1.0) * 100.0
        if abs(mv) <= MAX_PLAUSIBLE_MOVE:
            return mv, "derived"
    return None, "excluded"


def honest_performance(rows: Sequence[Dict], *, min_clean: int = MIN_CLEAN_DEFAULT,
                       market_return_pct: Optional[float] = None) -> Perf:
    p = Perf(n_total=len(rows))
    src = {"spot_field": 0, "derived": 0, "excluded": 0}
    clean: List[float] = []
    for r in rows:
        mv, source = _clean_move_pct(r)
        src[source] += 1
        if mv is not None:
            clean.append(mv)
    p.source_counts = src
    p.n_clean = len(clean)
    p.n_excluded = src["excluded"]

    if p.n_clean < min_clean:
        p.trustworthy = False
        p.note = (f"insufficient clean data (n={p.n_clean} < {min_clean}); "
                  f"{p.n_excluded} rows excluded as premium-only/replay-failed. "
                  "Refusing to emit a headline rather than show a mirage.")
        return p

    wins = [x for x in clean if x > 0]
    losses = [x for x in clean if x < 0]
    p.win_rate = round(len(wins) / len(clean), 3)
    gp, gl = sum(wins), -sum(losses)
    p.profit_factor = round(gp / gl, 2) if gl > 0 else float("inf")
    p.expectancy_pct = round(sum(clean) / len(clean), 3)
    p.median_pct = round(sorted(clean)[len(clean) // 2], 3)
    p.total_pct = round(sum(clean), 1)

    # mirage gate — these signatures fooled us before, so they REVOKE the headline
    # (better to say "not trustworthy yet" than to show a soft mirage with a warning).
    suspect = False
    if src["spot_field"] < min_clean and src["derived"] > src["spot_field"]:
        p.alarms.append(f"LOW SPOT COVERAGE: only {src['spot_field']} rows have clean "
                        f"spot_pnl_pct; {src['derived']} derived from (unreliable) fills. "
                        "Run the spot-outcome backfill before trusting this.")
        suspect = True
    pf = p.profit_factor
    if isinstance(pf, float) and pf != float("inf") and pf > PF_IMPLAUSIBLE:
        p.alarms.append(f"PF {pf} > {PF_IMPLAUSIBLE} is implausible for daily-bar retail — "
                        "almost always premium leak, beta/regime, or bad exit_price.")
        suspect = True
    if isinstance(pf, float) and pf == float("inf"):
        p.alarms.append("PF = inf (no losing trades) — sample is not real/complete.")
        suspect = True
    if market_return_pct is not None:
        p.alarms.append(f"vs market: per-trade market drift ~"
                        f"{market_return_pct/max(p.n_clean,1):+.3f}% — beta in a bull is not alpha.")

    p.trustworthy = not suspect
    if suspect:
        p.note = (f"NOT trustworthy yet: clean spot coverage {src['spot_field']}/{p.n_total} "
                  "and/or implausible PF. Fix the data (backfill spot outcomes), don't display this.")
    else:
        p.note = (f"clean n={p.n_clean} ({src['spot_field']} spot, {src['derived']} derived); "
                  f"premium pnl_pct excluded by design.")
    return p


def from_journal(path: str = "logs/signal_journal.jsonl",
                 since: Optional[str] = None, **kw) -> Perf:
    """Honest performance from the journal. `since` (ISO date/datetime string)
    filters to signals timestamped >= since — used for a clean FORWARD paper test
    so new signals are scored on their own, not blended into historical rows."""
    rows = []
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    if since:
        rows = [r for r in rows if str(r.get("ts", "")) >= since]
    return honest_performance(rows, **kw)


if __name__ == "__main__":
    p = from_journal()
    print("[honest_performance] live journal")
    for k, v in p.as_dict().items():
        print(f"  {k}: {v}")
    print(f"\n  => {'TRUSTWORTHY' if p.trustworthy else 'NOT TRUSTWORTHY'}: {p.note}")
    if p.alarms:
        print("  ALARMS:")
        for a in p.alarms:
            print(f"    - {a}")
