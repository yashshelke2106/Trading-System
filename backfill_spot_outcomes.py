"""
backfill_spot_outcomes.py — populate clean `spot_outcome` / `spot_pnl_pct` on
journal rows that lack them, by GAP-HONEST replay of SPOT daily bars.

This is the SOURCE fix behind core/honest_performance.py: once spot coverage is
high, the gate can emit a TRUSTWORTHY number (premium pnl_pct stays excluded).

NON-DESTRUCTIVE by default → writes logs/signal_journal.backfilled.jsonl and
prints before/after honest_performance + sanity checks. Only use --inplace (auto
.bak) after you've eyeballed the result.

Replay rules (daily OHLC, NO look-ahead, conservative):
  * Scan bars STRICTLY AFTER the signal date (can't fill on the signal bar and
    also use it to decide).
  * Gap-honest: if a bar OPENS beyond a level, fill at the OPEN (worse than the
    level), not at the level.
  * Long:  low<=sl → SL_HIT ; high>=target → TARGET_HIT. Both in one bar → assume
    SL first (we can't see intrabar order on daily bars; conservative).
  * Short: mirror (high>=sl → SL_HIT ; low<=target → TARGET_HIT).
  * No touch by horizon (option_expiry, else +MAX_HOLD trading bars) → TIME_EXIT
    at that bar's close.
  spot_pnl_pct = dir*(exit/entry - 1)*100   (a real spot %, compounds honestly)
  mfe_pct / mae_pct = best / worst excursion in trade direction over the hold

Run:  .venv/Scripts/python.exe backfill_spot_outcomes.py            # dry, -> new file
      .venv/Scripts/python.exe backfill_spot_outcomes.py --inplace  # after review
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.bar_cache import cached_daily
from core.honest_performance import honest_performance

JOURNAL = "logs/signal_journal.jsonl"
MAX_HOLD = 12          # trading bars if no option_expiry
FETCH_DAYS = 400       # cache window per symbol (covers any trade + horizon)


def _date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "")).date()
    except Exception:
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _has_reliable_spot(row) -> bool:
    so = row.get("spot_outcome")
    sp = row.get("spot_pnl_pct")
    return so in ("SL_HIT", "TARGET_HIT", "TIME_EXIT") and isinstance(sp, (int, float))


STOP_SLIP = 0.001   # stop-market fills ~0.1% WORSE than the stop level (not exactly at it)


def replay(row, bars):
    """Return (spot_outcome, spot_pnl_pct, exit_price) or None if not replayable.

    HONEST entry model: these signals are logged post-market (next-day entries),
    so the cost basis is the NEXT session's OPEN — not the logged close. This
    avoids booking the overnight close->open gap as free PnL (a bull-market
    inflation). Stops fill STOP_SLIP worse than the level; targets fill at the
    level (conservative)."""
    e0 = row.get("entry_price"); sl0 = row.get("sl_price"); tg0 = row.get("target_price")
    if not all(isinstance(v, (int, float)) for v in (e0, sl0, tg0)) or e0 <= 0:
        return None
    long = str(row.get("direction", "long")).lower() == "long"
    edate = _date(row.get("ts"))
    if edate is None:
        return None
    fut = bars[bars.index.map(lambda d: d.date() > edate)]
    if len(fut) == 0:
        return None
    # Cap the walk-forward at the trade's ACTUAL exit when one is recorded.
    # Without this the replay ran to option_expiry or MAX_HOLD - so a position
    # that really closed in 20 hours got its spot label derived from up to ten
    # days of subsequent price action it was never exposed to. Rows resolved
    # live and rows backfilled were then answering different questions, and the
    # backfilled ones were answering a question nobody asked.
    horizon = _date(row.get("exit_ts")) or _date(row.get("option_expiry"))
    if horizon is not None:
        fut = fut[fut.index.map(lambda d: d.date() <= horizon)]
        if len(fut) == 0:
            return None          # exited same session; no daily bar to replay
    else:
        fut = fut.iloc[:MAX_HOLD]
    if len(fut) < 1:
        return None

    # ENTER at next session's open; RE-ANCHOR sl/target to preserve the planned
    # risk%/reward% from the actual fill (what a risk-disciplined trader does on a
    # gapped entry). This fixes the overnight-gap leak without creating sign bugs.
    e = float(fut.iloc[0]["open"])
    if e <= 0:
        return None
    risk_pct = abs(e0 - sl0) / e0
    rew_pct = abs(tg0 - e0) / e0
    if long:
        sl, tg = e * (1 - risk_pct), e * (1 + rew_pct)
    else:
        sl, tg = e * (1 + risk_pct), e * (1 - rew_pct)
    sl_fill = sl * (1 - STOP_SLIP) if long else sl * (1 + STOP_SLIP)
    entry_dt = fut.index[0]

    # Excursions, in trade direction, over the bars actually held. The loop
    # already reads every high and low; it simply threw them away. Persisting
    # them turns "what target would this book have hit" from a full re-replay
    # (which needs bars, a live API and a timezone correction) into a query.
    best = worst = e

    def out(exit_px, label, exit_dt):
        dirn = 1.0 if long else -1.0
        mfe = dirn * (best / e - 1.0) * 100.0
        mae = dirn * (worst / e - 1.0) * 100.0
        return (label, round(dirn * (exit_px / e - 1.0) * 100.0, 3), float(exit_px),
                entry_dt, exit_dt, round(mfe, 3), round(mae, 3))

    for i, (idx, b) in enumerate(fut.iterrows()):
        o, hi, lo = float(b["open"]), float(b["high"]), float(b["low"])
        # favourable = up for a long, down for a short
        best = max(best, hi) if long else min(best, lo)
        worst = min(worst, lo) if long else max(worst, hi)
        if i > 0:                            # gap-through only on bars AFTER entry bar
            if long and o <= sl:
                return out(min(o, sl_fill), "SL_HIT", idx)
            if long and o >= tg:
                return out(o, "TARGET_HIT", idx)
            if not long and o >= sl:
                return out(max(o, sl_fill), "SL_HIT", idx)
            if not long and o <= tg:
                return out(o, "TARGET_HIT", idx)
        if long:
            if lo <= sl:                     # SL checked first (conservative)
                return out(sl_fill, "SL_HIT", idx)
            if hi >= tg:
                return out(tg, "TARGET_HIT", idx)
        else:
            if hi >= sl:
                return out(sl_fill, "SL_HIT", idx)
            if lo <= tg:
                return out(tg, "TARGET_HIT", idx)
    return out(float(fut.iloc[-1]["close"]), "TIME_EXIT", fut.index[-1])


def run_backfill(journal: str = JOURNAL, *, inplace: bool = False,
                 force: bool = False, quiet: bool = False) -> dict:
    """Programmatic entry (also called by the EOD batch). Gap-honest spot replay.
    SAFETY: if the replay sanity check fails (SL moves not negative / target not
    positive), it will NOT overwrite the live journal in-place — it diverts to a
    side file, so a replay bug can never corrupt the journal in an unattended run.
    Returns a summary dict; raises only on unreadable journal."""
    rows = [json.loads(l) for l in open(journal, encoding="utf-8") if l.strip()]
    before = honest_performance(rows)

    bars_cache = {}
    filled = skipped_have = skipped_nodata = 0
    sl_moves, tg_moves = [], []
    for row in rows:
        if _has_reliable_spot(row) and not force:
            skipped_have += 1; continue
        sym = row.get("symbol")
        if sym not in bars_cache:
            try:
                bars_cache[sym] = cached_daily(sym, FETCH_DAYS)
            except Exception:
                bars_cache[sym] = None
        bars = bars_cache[sym]
        if bars is None or len(bars) < 5:
            skipped_nodata += 1; continue
        res = replay(row, bars)
        if res is None:
            skipped_nodata += 1; continue
        so, sp, xp, entry_dt, exit_dt, mfe, mae = res
        row["spot_outcome"] = so
        row["spot_pnl_pct"] = sp
        row["spot_entry_ts"] = str(entry_dt.date())
        row["spot_exit_ts"] = str(exit_dt.date())
        row["mfe_pct"] = mfe
        row["mae_pct"] = mae
        row.setdefault("extra", {})["spot_backfilled"] = True
        filled += 1
        (sl_moves if so == "SL_HIT" else tg_moves if so == "TARGET_HIT" else []).append(sp)

    after = honest_performance(rows)
    sl_bad = sum(1 for m in sl_moves if m > 0)
    tg_bad = sum(1 for m in tg_moves if m < 0)
    sanity_ok = (sl_bad == 0 and tg_bad == 0)

    diverted = inplace and not sanity_ok          # never overwrite on a buggy replay
    out_path = journal if (inplace and sanity_ok) else \
        journal.replace(".jsonl", ".backfilled.jsonl")
    if inplace and sanity_ok:
        bak = journal + ".pre_backfill.bak"
        if not os.path.exists(bak):               # never clobber the pristine original
            shutil.copy2(journal, bak)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, out_path)

    summary = {
        "rows": len(rows), "filled": filled, "skipped_have": skipped_have,
        "skipped_nodata": skipped_nodata, "sanity_ok": sanity_ok,
        "sl_bad": sl_bad, "tg_bad": tg_bad, "diverted": diverted,
        "coverage_before": before.source_counts.get("spot_field", 0),
        "coverage_after": after.source_counts.get("spot_field", 0),
        "trustworthy": after.trustworthy, "win_rate": after.win_rate,
        "profit_factor": after.profit_factor, "expectancy_pct": after.expectancy_pct,
        "out_path": out_path,
    }
    if not quiet:
        print(f"[backfill] {len(rows)} rows. coverage {summary['coverage_before']}"
              f"->{summary['coverage_after']}  filled={filled} "
              f"skipped(had)={skipped_have} skipped(nodata)={skipped_nodata}")
        if sl_moves:
            print(f"[sanity] SL_HIT median {sorted(sl_moves)[len(sl_moves)//2]:+.2f}% "
                  f"({sl_bad} POSITIVE — should be 0)")
        if tg_moves:
            print(f"[sanity] TARGET_HIT median {sorted(tg_moves)[len(tg_moves)//2]:+.2f}% "
                  f"({tg_bad} NEGATIVE — should be 0)")
        print(f"[backfill] AFTER trustworthy={after.trustworthy}: {after.note}")
        if after.trustworthy:
            print(f"           WR {after.win_rate}  PF {after.profit_factor}  "
                  f"exp {after.expectancy_pct}%/tr  (n={after.n_clean})")
        for a in after.alarms:
            print(f"           ALARM: {a}")
        tag = ("  (IN-PLACE, .pre_backfill.bak saved)" if (inplace and sanity_ok)
               else "  (DIVERTED — sanity failed, journal NOT overwritten)" if diverted
               else "  (dry; review then --inplace)")
        print(f"[backfill] wrote {out_path}{tag}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default=JOURNAL)
    ap.add_argument("--inplace", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-replay rows that already have spot fields (overwrite "
                         "premium-timed tracker marks)")
    args = ap.parse_args()
    run_backfill(args.journal, inplace=args.inplace, force=args.force, quiet=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
