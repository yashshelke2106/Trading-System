"""
capture_task.py — the daily market-capture job.

WHY A SCHEDULED JOB AND NOT A SCRIPT YOU REMEMBER TO RUN
--------------------------------------------------------
Every capture layer in this repo was built, verified, run once for whichever
research question prompted it, and then abandoned:

  logs/bhavcopy_archive/  populated 2019-01 .. 2020-05 for a survivorship
                          check, then untouched for six years
  logs/intraday_5m/       populated 2026-04 .. 2026-06 for the gap-fade probe,
                          then untouched

Nothing accrued, so every later study started data-poor again. This task
exists so capture is a standing process rather than a remembered errand.

The asymmetry that justifies it: EOD bhavcopy is archival and can be
back-filled years later, but yfinance serves only ~60 days of 5-minute
history. Intraday sessions missed beyond that window are unrecoverable at any
price. Running late costs nothing on the EOD layer and costs data permanently
on the intraday layer.

WHAT IT DOES
------------
  eod       append today's NSE bhavcopy to the survivorship-complete archive
  intraday  merge the last 60d of 5m bars for the configured universe
  state     classify trend state for the universe and snapshot it
  flow      participant-wise OI/volume (FII/DII/Pro/Client) + FII/DII cash

All three are idempotent — re-running merges rather than duplicates.

RUN
---
    python capture_task.py                  # all layers, top100 universe
    python capture_task.py --layers eod     # EOD only (fast, ~1s)
    python capture_task.py --universe fo    # full F&O universe
    python capture_task.py --status         # print last run summary

SCHEDULING
----------
Run once after the close (>= 16:00 IST). The EOD bhavcopy for day D is
published by NSE after the session ends; running before that simply finds no
file for today and reports eod.appended = 0, which is harmless.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

STATE_FILE = os.path.join("logs", "capture_state.json")

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(IST)


# ── Layer: EOD bhavcopy ─────────────────────────────────────────────────────

def run_eod(days_back: int = 5) -> dict:
    """Append recent bhavcopy days. Small window because gaps are back-fillable.

    days_back covers weekends, holidays and a missed run or two. `--resume`
    semantics mean already-captured days are skipped for free.
    """
    out = {"layer": "eod", "ok": False}
    try:
        import bhavcopy_archive as ba
        end = date.today()
        start = end - timedelta(days=days_back)
        res = ba.ingest_range(start=start, end=end, resume=True,
                              pace_sec=0.25, build_symbols=False)
        out.update({
            "ok": True,
            "appended": res["trading_days"],
            "skipped": res["skipped"],
            "rows": res["total_rows"],
            "window": f"{start} .. {end}",
        })
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["trace"] = traceback.format_exc()[-800:]
    return out


# ── Layer: intraday 5m ──────────────────────────────────────────────────────

def run_intraday(universe: str = "top100", pace: float = 0.4) -> dict:
    out = {"layer": "intraday", "ok": False}
    try:
        from core import intraday_capture as ic
        symbols = list(ic._resolve_universe(universe))

        # REFRESH WHAT YOU ALREADY HOLD. The universe is top100 but the archive
        # had grown to 151 symbols, so 51 of them were never refreshed and aged
        # toward the 60-day cliff - past which 5m history is UNRECOVERABLE, not
        # merely late. Coverage counted them while the refresh did not, so the
        # health metric reported 151 symbols and a worst-stale that no
        # successful run could ever move.
        try:
            import glob
            import os as _os
            held = {_os.path.basename(f).rsplit(".", 1)[0]
                    for f in glob.glob(_os.path.join("logs", "intraday_5m", "*"))}
            orphans = sorted(held - set(symbols))
            if orphans:
                log.info("[capture] +%d archived symbols outside %s "
                         "(they age out at 60d if never refreshed)",
                         len(orphans), universe)
                symbols += orphans
                out["orphans_refreshed"] = len(orphans)
        except Exception as exc:                      # never block the main leg
            log.warning("[capture] orphan sweep failed: %s", exc)

        st = ic.capture(symbols, pace_sec=pace)
        out.update({
            "ok": True,
            "symbols": st["symbols"],
            "succeeded": st["ok"],
            "failed": st["failed"],
            "bars_added": st["bars_added"],
            "conflicts": st["conflicts"],
            "failures": st["failures"][:20],
        })
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["trace"] = traceback.format_exc()[-800:]
    return out


# ── Layer: day movement structure ───────────────────────────────────────────

def run_day_structure() -> dict:
    """Per-session shape descriptors from the 5m archive. Derived layer —
    recomputable as long as the underlying bars were captured, so a missed
    run costs nothing here (it costs on the intraday layer)."""
    out = {"layer": "day_structure", "ok": False}
    try:
        from core import day_structure as dstru
        res = dstru.build()
        out.update({"ok": True, **res})
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["trace"] = traceback.format_exc()[-800:]
    return out


# ── Layer: swing events ─────────────────────────────────────────────────────

def run_swing(universe: str = "top100") -> dict:
    """Point-in-time swing events + forward-slot fills. Uses daily bars via
    the bhavcopy archive, which lags one session (published post-close), so
    today's breakout lands on tomorrow's run — recorded, never lost."""
    out = {"layer": "swing", "ok": False}
    try:
        from core import swing_structure as ss
        from core.universe import FO_UNIVERSE, TOP100_LIQUID
        symbols = list(FO_UNIVERSE if universe == "fo" else TOP100_LIQUID)
        upd = ss.update(symbols)
        ana = ss.analyze()
        out.update({
            "ok": True,
            "symbols": upd["symbols"],
            "events_added": upd["added"],
            "events_total": upd["total"],
            "fwd_filled": ana["filled"],
            "failed": upd["failed"],
        })
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["trace"] = traceback.format_exc()[-800:]
    return out


# ── Layer: trend state snapshot ─────────────────────────────────────────────

def run_state(universe: str = "top100", with_context: bool = False) -> dict:
    out = {"layer": "state", "ok": False}
    try:
        from core import market_state as ms
        from core.universe import FO_UNIVERSE, TOP100_LIQUID
        symbols = list(FO_UNIVERSE if universe == "fo" else TOP100_LIQUID)
        states = ms.classify_many(symbols, with_context=with_context)

        tally: dict = {}
        for s in states:
            tally[s.direction] = tally.get(s.direction, 0) + 1

        out.update({
            "ok": True,
            "symbols": len(states),
            "tally": tally,
            "states": [
                {"symbol": s.symbol, "direction": s.direction,
                 "strength": s.strength, "adx": (s.adx if s.adx == s.adx else None),
                 "asof": s.asof}
                for s in states
            ],
            # Carried on every snapshot so a reader of the stored JSON cannot
            # mistake this for a full-factor picture.
            "unavailable_factors": ms.UNAVAILABLE_FACTORS,
        })
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["trace"] = traceback.format_exc()[-800:]
    return out


# ── Layer: institutional flow ───────────────────────────────────────────────

def run_flow(days_back: int = 7) -> dict:
    """Participant-wise OI/volume + FII/DII cash.

    Asymmetric like the intraday layer, and for the same reason: the NSCCL
    participant archive is permanent and back-fillable, but the FII/DII cash
    endpoint serves ONLY the current day and has no archive behind it. A day
    this job does not run is a cash-flow observation that can never be
    recovered, so cash failure is reported but does not fail the layer -- the
    positioning half is what a study depends on.
    """
    out = {"layer": "flow", "ok": False}
    try:
        from core import flow_capture as fx
        end = date.today()
        res = fx.build(end - timedelta(days=days_back), end,
                       sleep=0.25, verbose=False)
        out.update({"ok": True, "oi_days_written": res["written"],
                    "skipped": res["skipped"], "missing": res["missing"]})
        try:
            rows = fx.fetch_cash_flow(fx._get_session())
            out["cash_rows"] = fx.append_cash(rows) if rows else 0
            if not rows:
                out["cash_error"] = "fetch returned nothing — day lost, unrecoverable"
        except Exception as exc:
            out["cash_error"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["trace"] = traceback.format_exc()[-800:]
    return out


# ── Orchestration ───────────────────────────────────────────────────────────

ALL_LAYERS = ("eod", "intraday", "day_structure", "swing", "state", "flow")


def run(layers=ALL_LAYERS, universe: str = "top100",
        with_context: bool = False, pace: float = 0.4) -> dict:
    started = time.time()
    status = {
        "run_at": _now_ist().isoformat(),
        "universe": universe,
        "layers": {},
    }

    if "eod" in layers:
        status["layers"]["eod"] = run_eod()
    if "intraday" in layers:
        status["layers"]["intraday"] = run_intraday(universe, pace=pace)
    if "day_structure" in layers:
        status["layers"]["day_structure"] = run_day_structure()
    if "swing" in layers:
        status["layers"]["swing"] = run_swing(universe)
    if "state" in layers:
        status["layers"]["state"] = run_state(universe, with_context=with_context)
    if "flow" in layers:
        status["layers"]["flow"] = run_flow()

    status["elapsed_sec"] = round(time.time() - started, 1)
    status["ok"] = all(v.get("ok") for v in status["layers"].values())

    # Intraday staleness is the number that actually decays — surface it at the
    # top level so a monitor does not have to know the archive layout.
    try:
        from core import intraday_capture as ic
        cov = ic.coverage()
        held = cov[cov["bars"] > 0] if not cov.empty else cov
        if not held.empty:
            worst = int(held["stale_days"].max())
            status["intraday_coverage"] = {
                "symbols": int(len(held)),
                "worst_stale_days": worst,
                "unrecoverable": bool(worst > ic.MAX_LOOKBACK_DAYS),
                "limit_days": ic.MAX_LOOKBACK_DAYS,
            }
    except Exception as exc:
        status["intraday_coverage"] = {"error": str(exc)}

    os.makedirs("logs", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, default=str)

    # A run that reports "OK" can still mean the data behind it is decaying:
    # this scheduled run succeeding says nothing about how stale the archive
    # was BEFORE it ran. 2026-08-20: the EOD archive was 6 days stale and one
    # intraday symbol had drifted to 27 of a 60-day recovery window, entirely
    # unnoticed, because nothing was watching. Never let a monitoring failure
    # block the capture run it is monitoring.
    try:
        from core.freshness_alarm import check_and_notify
        check_and_notify(quiet=True)
    except Exception as exc:
        status.setdefault("warnings", []).append(f"freshness_alarm failed: {exc}")

    return status


def load_status() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"error": "no capture run recorded yet"}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        st["age_hours"] = round((time.time() - os.path.getmtime(STATE_FILE)) / 3600, 1)
        return st
    except Exception as exc:
        return {"error": f"unreadable state file: {exc}"}


# ── CLI ─────────────────────────────────────────────────────────────────────

def _print_summary(st: dict) -> None:
    print(f"\n=== CAPTURE RUN {st.get('run_at','?')} "
          f"({st.get('elapsed_sec','?')}s) ===")
    for name, lay in st.get("layers", {}).items():
        mark = "OK  " if lay.get("ok") else "FAIL"
        print(f"  [{mark}] {name}")
        for k, v in lay.items():
            if k in ("layer", "ok", "trace"):
                continue
            if k == "states":
                continue
            print(f"           {k}: {v}")
    cov = st.get("intraday_coverage")
    if cov and "error" not in cov:
        warn = "  <-- DATA BEING LOST" if cov.get("unrecoverable") else ""
        print(f"  intraday coverage: {cov['symbols']} symbols, "
              f"worst stale {cov['worst_stale_days']}d "
              f"(limit {cov['limit_days']}d){warn}")
    print(f"  overall: {'OK' if st.get('ok') else 'DEGRADED'}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily market-capture job.")
    ap.add_argument("--layers", nargs="*", default=list(ALL_LAYERS),
                    choices=list(ALL_LAYERS))
    ap.add_argument("--universe", default="top100", choices=["fo", "top100"])
    ap.add_argument("--pace", type=float, default=0.4)
    ap.add_argument("--with-context", action="store_true",
                    help="Include sector/market factors in the state snapshot")
    ap.add_argument("--status", action="store_true", help="Print last run and exit")
    args = ap.parse_args()

    if args.status:
        st = load_status()
        if "error" in st:
            print(st["error"])
            return 1
        _print_summary(st)
        return 0

    st = run(layers=tuple(args.layers), universe=args.universe,
             with_context=args.with_context, pace=args.pace)
    _print_summary(st)
    return 0 if st.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
