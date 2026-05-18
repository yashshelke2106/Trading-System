"""
End-of-day learning batch — the post-market auto-improvement job.

Why this exists
---------------
Learning was in-loop, every ~10 scans, *during* market hours. That fits
an intraday system where trades resolve within the session. It does NOT
fit SWING: swing signals resolve over DAYS, so at any given close there
is little-to-nothing new to learn from in-session, and both runners go
quiet after close (scan_only_v2 exits, aladdin idles). Net: a swing
system that never deliberately consolidates what matured.

run_eod() is that deliberate consolidation, run once after the bell:

  1. check_outcomes()          resolve every signal that has matured
                               (Mode A/B, walk-forward replay, HONEST
                               spread+theta costs, clean spot label —
                               all the machinery built in C/F/H/J)
  2. adaptive_learner          retune pattern weights / params from the
                               freshly resolved set (gated: ≥20)
  3. calibrator.fit            refit score→P(win) on resolved current-
                               engine trades (gated: ≥MIN_CALIB)
  4. report                    append a timestamped summary to
                               logs/eod_learn.log and print it

Idempotent and safe: resolve_signal only touches outcome=None rows, the
learner/calibrator gates no-op on thin data, every step is wrapped so a
failure in one never blocks the others or shutdown. PAPER_TRADE and live
order paths are never touched.

Run:
  python -m core.eod_learn            # full batch (writes params/calib)
  python -m core.eod_learn --quiet    # log only, no stdout
Auto-runs when scan_only_v2 sees the market close (unless --no-eod).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EOD_LOG = os.path.join(_PROJECT_ROOT, "logs", "eod_learn.log")


def _journal_snapshot() -> dict:
    """Honest current-engine accuracy from the journal (post-resolve)."""
    out = {"closed": 0, "open": 0, "target": 0, "sl": 0, "time": 0}
    try:
        from core.signal_journal import get_all_signals, ENGINE_VERSION
        for r in get_all_signals(days=120):
            if r.get("engine_version") != ENGINE_VERSION:
                continue
            oc = (r.get("outcome") or "").upper()
            if oc == "TARGET_HIT":
                out["closed"] += 1; out["target"] += 1
            elif oc == "SL_HIT":
                out["closed"] += 1; out["sl"] += 1
            elif oc in ("EXPIRED", "TIME_EXIT"):
                out["closed"] += 1; out["time"] += 1
            elif r.get("outcome") is None:
                out["open"] += 1
    except Exception as e:
        log.debug(f"[EOD] snapshot failed: {e}")
    return out


def run_eod(quiet: bool = False) -> dict:
    """Run the post-market consolidation. Returns a summary dict.
    Never raises — each stage is isolated so shutdown is never blocked."""
    started = datetime.now()
    summary: dict = {"ts": started.isoformat(timespec="seconds")}

    # ── 1. Resolve everything that matured ───────────────────────────
    try:
        from core.signal_tracker import check_outcomes
        th, sl, ex = check_outcomes()
        summary["resolved"] = {"target": th, "sl": sl, "expired": ex}
    except Exception as e:
        summary["resolved"] = {"error": str(e)}
        log.warning(f"[EOD] check_outcomes failed: {e}")

    # ── 2. Adaptive learner (gated on ≥20 resolved current-engine) ────
    try:
        from core.adaptive_learner import get_learner
        changes = get_learner().maybe_update()
        summary["learner"] = {"params_changed": len(changes or {}),
                              "keys": sorted((changes or {}).keys())[:12]}
    except Exception as e:
        summary["learner"] = {"error": str(e)}
        log.warning(f"[EOD] learner failed: {e}")

    # ── 3. Recalibrate score→P(win) on the resolved set ──────────────
    try:
        from core.calibrator import get_calibrator
        cal = get_calibrator()
        fit = cal.fit_from_journal()
        summary["calibrator"] = {"fitted": fit.get("fitted", False),
                                 "n": fit.get("n"),
                                 "mode": cal.status().get("mode")}
    except Exception as e:
        summary["calibrator"] = {"error": str(e)}
        log.warning(f"[EOD] calibrator failed: {e}")

    # ── 4. Honest accuracy snapshot ──────────────────────────────────
    summary["accuracy"] = _journal_snapshot()
    summary["elapsed_s"] = round((datetime.now() - started).total_seconds(), 1)

    _emit(summary, quiet)
    return summary


def _emit(s: dict, quiet: bool) -> None:
    acc = s.get("accuracy", {})
    res = s.get("resolved", {})
    lrn = s.get("learner", {})
    cal = s.get("calibrator", {})
    line = (
        f"[EOD {s['ts']}] resolved T{res.get('target','?')}/"
        f"S{res.get('sl','?')}/X{res.get('expired','?')} | "
        f"closed={acc.get('closed',0)} open={acc.get('open',0)} "
        f"(T{acc.get('target',0)}/S{acc.get('sl',0)}/X{acc.get('time',0)}) | "
        f"learner d{lrn.get('params_changed',0)} | "
        f"calib {cal.get('mode','?')} n={cal.get('n','?')} | "
        f"{s.get('elapsed_s','?')}s"
    )
    try:
        os.makedirs(os.path.dirname(_EOD_LOG), exist_ok=True)
        with open(_EOD_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        log.debug(f"[EOD] log write failed: {e}")
    if not quiet:
        print(line)
        c = acc.get("closed", 0)
        if c == 0:
            print("  note: 0 resolved on current engine - swing trades "
                  "mature over days; learning begins once >=20 resolve. "
                  "Nothing to improve yet is the correct state, not a bug.")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="eod_learn")
    ap.add_argument("--quiet", action="store_true", help="log only, no stdout")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    run_eod(quiet=args.quiet)
