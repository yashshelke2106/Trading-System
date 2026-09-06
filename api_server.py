"""
FastAPI backend for the Next.js trading dashboard.

Endpoints:
  GET  /api/signals          — Grade A signals from logs/signals.json
  GET  /api/signals/all      — All grades (A/B/C) with by_grade breakdown
  GET  /api/journal?days=30  — signal journal (open + resolved)
  GET  /api/trades           — paper trades from logs/trades.csv
  GET  /api/stats            — win rate, total P&L, grade breakdown
  GET  /api/indices          — Nifty50 / BankNifty / VIX quotes
  GET  /api/status           — market status, IST time, token health
  GET  /api/positions        — live Dhan positions
  GET  /api/volume           — volume analytics (1h + 5m)
  GET  /api/chain?symbol=X   — option chain + PCR + Max Pain
  GET  /api/spike-alerts     — intraday spike alerts
  GET  /api/accuracy         — signal journal stats + live tracking
  GET  /api/intelligence     — RAG market brief
  WS   /ws/signals           — push update whenever signals.json changes

Run:
  python -m uvicorn api_server:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

app = FastAPI(title="F&O Signal API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SIGNALS_FILE = ROOT / "logs" / "signals.json"
JOURNAL_FILE = ROOT / "logs" / "signal_journal.jsonl"

# Sized well above the number of Dhan-dependent endpoints. With 4 workers, a
# single wedged loader (spike-alerts scanning 161 symbols against a dead Dhan)
# starved the pool and every unrelated endpoint — /api/status, /api/verdict —
# hung behind it. Threads cannot be killed in Python, so the defence is
# headroom plus a per-request deadline (see _run).
_executor = ThreadPoolExecutor(max_workers=16)

# No single request may hold a client longer than this. On expiry the caller
# gets a structured timeout instead of hanging; the worker thread is left to
# finish and its result lands in the TTL cache for the next request.
_RUN_DEADLINE_SEC = 12.0

# Must exceed the probe's own network worst-case (connect 3s + read 5s = 8s),
# else a healthy-but-slow Dhan trips this outer deadline before the request
# even finishes and the UI wrongly shows the connection as dead.
_DHAN_PROBE_TIMEOUT_SEC = 10.0
# ── Simple TTL cache ──────────────────────────────────────────────────────────

_CACHE: Dict[str, tuple] = {}  # key → (value, expires_at)

def _cached(key: str, ttl: float, loader):
    now = time.monotonic()
    if key in _CACHE:
        val, exp = _CACHE[key]
        if now < exp:
            return val
    val = loader()
    _CACHE[key] = (val, now + ttl)
    return val


def _json_safe(obj):
    """Recursively replace non-finite floats (NaN/inf) with None.

    FastAPI's JSON encoder raises ValueError("Out of range float values are not
    JSON compliant") on NaN, which turns one empty metric into a 500 and a blank
    panel. pandas is the usual source: `df.where(df.notna(), other=None)` does
    NOT strip NaN from float columns — None is coerced straight back to NaN.
    """
    import math
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


class RunTimeout(Exception):
    """A loader exceeded its deadline. Endpoints turn this into a payload."""


async def _run(fn, *args, timeout: float = _RUN_DEADLINE_SEC):
    """Run a blocking loader off the event loop, under a deadline.

    asyncio.wait_for cancels the *await*, not the thread — the worker keeps
    running and populates the cache, so a slow-but-alive source self-heals on
    the next poll while a dead one stops wedging the UI.
    """
    loop = asyncio.get_event_loop()
    fut = loop.run_in_executor(_executor, fn, *args)
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
    except asyncio.TimeoutError:
        raise RunTimeout(f"loader exceeded {timeout:.0f}s deadline")


# ── File-based helpers ────────────────────────────────────────────────────────

def _read_signals() -> Dict:
    if not SIGNALS_FILE.exists():
        return {"signals": [], "ts": None, "meta": {}, "by_grade": {}}
    try:
        with open(SIGNALS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception:
        return {"signals": [], "ts": None, "meta": {}, "by_grade": {}}


def _filter_grade(data: Dict, grade: Optional[str], top_n: int = 10) -> List[Dict]:
    sigs = data.get("signals", [])
    if grade:
        sigs = [s for s in sigs if s.get("confluence_grade") == grade]
    sigs = sorted(sigs, key=lambda s: float(s.get("confluence_score", 0) or 0), reverse=True)
    return sigs[:top_n] if top_n else sigs


def _read_journal(days: int = 30) -> List[Dict]:
    if not JOURNAL_FILE.exists():
        return []
    cutoff = (datetime.now() - timedelta(days=days)).isoformat() if days else None
    records: List[Dict] = []
    try:
        with open(JOURNAL_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if cutoff is None or r.get("ts", "") >= cutoff:
                        records.append(r)
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return records


def _lot_for(symbol: str) -> int:
    """Contract size, live scrip master first. 1 == unresolved, not "one lot"."""
    try:
        from core.signal_tracker import _lot_for as _lf
        return _lf(symbol)
    except Exception:
        return 1


def _journal_trades(days: int = 30) -> List[Dict]:
    """P&L rows built from the SIGNAL JOURNAL — the source of record.

    The P&L tab used to read logs/trades.csv. That file is a lossy mirror:
    it is pinned to a legacy header, so the writer's premium / option / SL /
    target fields are dropped by `extrasaction="ignore"` on every append. The
    result was a tab that showed 118 trades and Rs 0, with every option column
    blank, while Journal and Verdict — reading the journal — showed real
    numbers for the SAME trades. One book, one set of numbers: read the
    journal here too.

    Two bases travel with every row, never mixed:
      pnl / pnl_percent  premium cash, lot-scaled  (what option buying cost)
      spot_pct           theta/IV-denoised spot move (what the signal was worth)
    `lot_resolved` is False when the contract size is unknown; such a row is
    reported but excluded from the rupee total rather than counted at 1 share.
    """
    rows = _read_journal(days)
    out: List[Dict] = []
    for r in rows:
        if not r.get("outcome"):
            continue
        sym = str(r.get("symbol") or "").upper()
        lot = _lot_for(sym)
        ep, xp = r.get("entry_prem"), r.get("exit_prem")

        # Recompute the rupee P&L at the CORRECT lot rather than trusting the
        # stored pnl_rupees: historical rows were written when the lot lookup
        # silently fell back to 1 share for any symbol missing from the stale
        # static map, which understated the book roughly 5x.
        pnl = None
        if ep is not None and xp is not None and lot > 1:
            pnl = round((float(xp) - float(ep)) * lot, 2)
        elif r.get("pnl_rupees") is not None and lot > 1:
            pnl = float(r["pnl_rupees"])

        pnl_pct = None
        if ep not in (None, 0) and xp is not None:
            try:
                pnl_pct = round((float(xp) / float(ep) - 1.0) * 100.0, 2)
            except (ValueError, TypeError, ZeroDivisionError):
                pnl_pct = None
        if pnl_pct is None and r.get("pnl_pct") is not None:
            pnl_pct = float(r["pnl_pct"])

        outcome = str(r.get("outcome"))
        out.append({
            "trade_id":      r.get("signal_id") or f"{sym}_{r.get('ts','')}",
            "timestamp":     r.get("exit_ts") or r.get("ts") or "",
            "entry_ts":      r.get("ts") or "",
            "symbol":        sym,
            "direction":     str(r.get("direction") or "").upper(),
            "entry_price":   r.get("entry_price"),
            "exit_price":    r.get("exit_price"),
            "sl_price":      r.get("sl_price"),
            "target_price":  r.get("target_price"),
            "quantity":      lot,
            "lot_resolved":  lot > 1,
            "pnl":           pnl,
            "pnl_percent":   pnl_pct,
            "spot_pct":      r.get("spot_pnl_pct"),
            "spot_outcome":  r.get("spot_outcome"),
            "status":        {"TARGET_HIT": "WIN", "SL_HIT": "LOSS"}.get(outcome, "EXPIRED"),
            "exit_reason":   outcome,
            "grade":         r.get("grade"),
            "entry_premium": ep,
            "exit_premium":  xp,
            "option_type":   r.get("option_type"),
            "option_strike": r.get("option_strike"),
            "prem_source":   r.get("prem_source"),
        })
    return out


def _compute_stats(trades: List[Dict]) -> Dict:
    """Two bases, both labelled, never blended.

    CASH  premium rupees at the resolved lot. Only rows whose contract size is
          known contribute; an unknown lot is counted as unscaled, not as 1
          share, because summing 1-share and 2,250-share rows produces a
          number that means nothing.
    SKILL spot %, delegated to core.honest_performance — the same gate the
          Verdict tab reads, so the two tabs cannot disagree.
    """
    empty = {
        "total": 0, "qualified": 0, "wins": 0, "losses": 0, "expired": 0,
        "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
        "best_trade": 0.0, "worst_trade": 0.0,
        "skipped_incomplete": 0, "unresolved_lot": 0,
        "basis": "premium cash (lot-scaled); skill metrics are spot %",
    }
    if not trades:
        return empty

    priced = [t for t in trades if t.get("pnl") is not None]
    unresolved = sum(1 for t in trades if not t.get("lot_resolved"))

    wins    = [t for t in priced if t.get("status") == "WIN"]
    losses  = [t for t in priced if t.get("status") == "LOSS"]
    expired = [t for t in priced if t.get("status") == "EXPIRED"]
    pnls    = [float(t["pnl"]) for t in priced]

    total_pnl = sum(pnls)
    decided = len(wins) + len(losses)
    stats = {
        "total":              len(trades),
        "qualified":          len(priced),
        "skipped_incomplete": len(trades) - len(priced),
        "unresolved_lot":     unresolved,
        "wins":               len(wins),
        "losses":             len(losses),
        "expired":            len(expired),
        "win_rate":           round(len(wins) / decided * 100, 1) if decided else 0.0,
        "total_pnl":          round(total_pnl, 2),
        "avg_pnl":            round(total_pnl / len(priced), 2) if priced else 0.0,
        "best_trade":         round(max(pnls), 2) if pnls else 0.0,
        "worst_trade":        round(min(pnls), 2) if pnls else 0.0,
        "basis":              "premium cash (lot-scaled); skill metrics are spot %",
    }

    # SKILL basis — identical computation to the Verdict tab.
    try:
        from core.honest_performance import honest_performance
        rows = [{"spot_pnl_pct": t.get("spot_pct"), "entry_price": t.get("entry_price"),
                 "exit_price": t.get("exit_price"), "direction": t.get("direction", "").lower()}
                for t in trades]
        stats["honest"] = honest_performance(rows).as_dict()
    except Exception as e:
        stats["honest"] = {"trustworthy": False, "note": f"honest_performance error: {e}"}
    return stats


def _get_dashboard_data():
    from core import dashboard_data as dd
    return dd


def _get_secrets():
    from core import secrets as sec
    return sec


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/api/signals")
def get_signals():
    data = _read_signals()
    return {
        "signals": _filter_grade(data, "A"),
        "ts":      data.get("ts"),
        "meta":    data.get("meta", {}),
        "total_scanned": len(data.get("signals", [])),
    }


@app.get("/api/signals/all")
def get_signals_all():
    data = _read_signals()
    all_sigs = data.get("signals", [])
    by_grade: Dict[str, List] = {"A": [], "B": [], "C": []}
    for s in all_sigs:
        g = s.get("confluence_grade", "C")
        by_grade.setdefault(g, []).append(s)
    return {
        "by_grade": by_grade,
        "ts":       data.get("ts"),
        "meta":     data.get("meta", {}),
        "counts":   {g: len(v) for g, v in by_grade.items()},
    }


@app.get("/api/journal")
def get_journal(days: int = 30):
    records = _read_journal(days)
    open_sigs     = [r for r in records if r.get("outcome") is None]
    resolved_sigs = [r for r in records if r.get("outcome") is not None]
    return {
        "open":     open_sigs,
        "resolved": resolved_sigs,
        "total":    len(records),
    }


@app.get("/api/trades")
def get_trades(days: int = 30):
    trades = _journal_trades(days)
    return {"trades": trades, "stats": _compute_stats(trades),
            "source": "signal_journal.jsonl"}


@app.get("/api/stats")
def get_stats():
    trades = _journal_trades(0)
    stats = _compute_stats(trades)
    # PERMANENT mirage guard: the rupee `pnl` above is option-PREMIUM-polluted
    # (theta/IV), the source of the old PF~16 fantasy. Attach the trustworthy
    # spot-based verdict from the single gate. The UI should PREFER `honest`, and
    # when honest.trustworthy is False, show honest.note — never the premium PF.
    try:
        from core.honest_performance import from_journal
        stats["honest"] = from_journal().as_dict()
    except Exception as e:  # never let the guard break the endpoint
        stats["honest"] = {"trustworthy": False, "note": f"honest_performance error: {e}"}
    return stats


_BOOT_TS = datetime.now().isoformat(timespec="seconds")


@app.get("/api/version")
def version():
    """Build stamp so the UI can prove it's current: git short hash + commit
    subject/date + API boot time. If the hash shown in the browser matches
    `git log -1`, you're looking at up-to-date code."""
    import subprocess
    def _git(args):
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True,
                                  timeout=3, cwd=os.path.dirname(__file__)).stdout.strip()
        except Exception:
            return ""
    return {
        "commit": _git(["rev-parse", "--short", "HEAD"]) or "unknown",
        "subject": _git(["log", "-1", "--pretty=%s"])[:80],
        "commit_date": _git(["log", "-1", "--date=short", "--pretty=%ad"]),
        "api_boot": _BOOT_TS,
        "served": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/health")
def health():
    """Rich health probe — exposes silent failure modes.

    Returns ts, signal freshness, chain success rate, token age, agent count.
    UI/ops can poll this every 30s and alert on degradation.
    """
    import os as _os
    import json as _json
    now = datetime.now()
    payload: dict = {"status": "ok", "ts": now.isoformat(), "checks": {}}

    # 1. signals.json freshness
    try:
        sig_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                 "logs", "signals.json")
        age_sec = (now.timestamp() - _os.path.getmtime(sig_path))
        with open(sig_path) as f:
            d = _json.load(f)
        sigs = d.get("signals", [])
        with_opt = sum(1 for s in sigs if s.get("option_strike"))
        payload["checks"]["signals"] = {
            "age_sec": round(age_sec),
            "count": len(sigs),
            "with_option_data": with_opt,
            "option_pct": round(with_opt / len(sigs) * 100, 1) if sigs else 0.0,
            "stale": age_sec > 300,  # >5min = scanner dead
        }
        if age_sec > 300:
            payload["status"] = "degraded"
            payload["checks"]["signals"]["alert"] = "scanner dead — signals.json stale"
    except Exception as e:
        payload["status"] = "degraded"
        payload["checks"]["signals"] = {"error": str(e)}

    # 2. Dhan token age (proxy for auth health)
    try:
        tok = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                            "dhan_token.txt")
        if _os.path.exists(tok):
            age_h = (now.timestamp() - _os.path.getmtime(tok)) / 3600
            payload["checks"]["dhan_token"] = {
                "age_hours": round(age_h, 1),
                "stale": age_h > 24,
            }
            if age_h > 24:
                payload["status"] = "degraded"
                payload["checks"]["dhan_token"]["alert"] = "token >24h — refresh recommended"
        else:
            payload["status"] = "degraded"
            payload["checks"]["dhan_token"] = {"error": "missing"}
    except Exception as e:
        payload["checks"]["dhan_token"] = {"error": str(e)}

    # 3. Recent option chain success rate (parse last 200 log lines)
    try:
        log_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                 "logs", "agents.log")
        if _os.path.exists(log_path):
            with open(log_path, encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-500:]
            empty = sum(1 for ln in tail if "no option chain" in ln or "option chain empty" in ln)
            persisted = sum(1 for ln in tail if "persisted" in ln and "Signal" in ln)
            total = empty + persisted
            success_rate = (persisted / total) if total > 0 else None
            payload["checks"]["option_chain"] = {
                "recent_drops": empty,
                "recent_persisted": persisted,
                "success_rate": round(success_rate, 3) if success_rate is not None else None,
            }
            if success_rate is not None and success_rate < 0.5:
                payload["status"] = "degraded"
                payload["checks"]["option_chain"]["alert"] = (
                    "<50% chain success — refresh token or check rate limits"
                )
    except Exception as e:
        payload["checks"]["option_chain"] = {"error": str(e)}

    # 4. State snapshot age
    try:
        state_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                   "logs", "state.json")
        if _os.path.exists(state_path):
            age_sec = (now.timestamp() - _os.path.getmtime(state_path))
            payload["checks"]["state"] = {
                "age_sec": round(age_sec),
                "stale": age_sec > 60,
            }
            if age_sec > 60:
                payload["status"] = "degraded"
                payload["checks"]["state"]["alert"] = "aladdin agent loop dead"
    except Exception:
        pass

    return payload


# ── Market status ─────────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    dd  = _get_dashboard_data()
    sec = _get_secrets()

    now     = dd.now_ist()
    elapsed = dd.market_elapsed_minutes(now)
    wait_s  = dd.seconds_to_market_open(now)

    if now.weekday() >= 5:
        mkt_status, mkt_detail = "WEEKEND", "Market closed"
    elif elapsed <= 0 and now.hour * 60 + now.minute < 9 * 60 + 15:
        mkt_status = "PRE-MKT"
        mkt_detail = f"Opens in {wait_s // 3600}h {(wait_s % 3600) // 60}m"
    elif now.hour > 15 or (now.hour == 15 and now.minute >= 30):
        mkt_status = "CLOSED"
        mkt_detail = f"Next open {wait_s // 3600}h {(wait_s % 3600) // 60}m"
    else:
        mkt_status = "LIVE"
        mkt_detail = f"{elapsed // 60}h {elapsed % 60}m elapsed"

    try:
        th = sec.token_health()
        trade_token = {
            "valid":        th.valid,
            "hours_left":   th.hours_left,
            "needs_refresh": th.needs_refresh,
            "message":      th.message,
        }
    except Exception:
        trade_token = {"valid": False, "hours_left": None, "needs_refresh": True, "message": "Error"}

    # Credential PRESENCE is not liveness. This chip read "Active" for weeks
    # off a stored api-key while every /charts/* call returned 401. Prefer the
    # real probe when one is already cached; never trigger a probe from here
    # (this endpoint is polled on a timer and must not block on the network).
    try:
        dh = sec.data_token_health()
        data_api = {"valid": dh.valid, "message": dh.message, "verified": False}
    except Exception:
        data_api = {"valid": False, "message": "Error", "verified": False}

    probe = _CACHE.get("dhanlive", (None, 0))[0]
    if probe is not None:
        data_api = {
            "valid": bool(probe.get("working")),
            "message": probe.get("message") or probe.get("status", ""),
            "status": probe.get("status"),
            "verified": True,
        }

    return {
        "market_status": mkt_status,
        "market_detail": mkt_detail,
        "ist_time":      now.strftime("%H:%M:%S"),
        "ist_date":      now.strftime("%d %b %Y"),
        "is_live":       mkt_status == "LIVE",
        "elapsed_min":   elapsed,
        "trade_token":   trade_token,
        "data_api":      data_api,
    }


# ── Index quotes ──────────────────────────────────────────────────────────────

@app.get("/api/indices")
async def get_indices():
    def _load():
        dd = _get_dashboard_data()
        return _cached("indices", 30, dd.get_index_quotes)

    try:
        quotes = await _run(_load)
        return {"quotes": quotes, "ts": datetime.now().isoformat()}
    except Exception as e:
        return {"quotes": {}, "ts": datetime.now().isoformat(), "error": str(e)}


# ── Live positions ────────────────────────────────────────────────────────────

@app.get("/api/positions")
async def get_positions():
    def _load():
        dd = _get_dashboard_data()
        return _cached("positions", 10, dd.get_live_positions)

    try:
        positions = await _run(_load)
        rows = []
        total_unreal = 0.0
        for pos in (positions or []):
            qty    = float(pos.get("netQty",          pos.get("quantity",     0)) or 0)
            avg_px = float(pos.get("avgCostPrice",    pos.get("averagePrice", 0)) or 0)
            ltp    = float(pos.get("lastTradedPrice", pos.get("ltp",          0)) or 0)
            sym    = pos.get("tradingSymbol", pos.get("symbol", "?"))
            unreal = (ltp - avg_px) * qty if ltp and avg_px else 0.0
            total_unreal += unreal
            rows.append({
                "symbol":     sym,
                "qty":        int(qty),
                "avg_price":  round(avg_px, 2),
                "ltp":        round(ltp, 2),
                "unrealized": round(unreal, 2),
            })
        return {"positions": rows, "total_unrealized": round(total_unreal, 2)}
    except Exception as e:
        return {"positions": [], "total_unrealized": 0.0, "error": str(e)}


# ── Volume analytics ──────────────────────────────────────────────────────────

@app.get("/api/volume")
async def get_volume():
    def _load():
        dd = _get_dashboard_data()
        from core.universe import FO_UNIVERSE
        return _cached("volume", 60, lambda: dd.get_volume_analytics(FO_UNIVERSE))

    try:
        data = await _run(_load)
        return data
    except Exception as e:
        return {"rows_1h": [], "rows_5m": [], "errors": {}, "error": str(e)}


# ── Option chain ──────────────────────────────────────────────────────────────

@app.get("/api/chain")
async def get_chain(symbol: str = "NIFTY"):
    def _load():
        dd = _get_dashboard_data()
        key = f"chain_{symbol}"
        return _cached(key, 30, lambda: dd.get_option_chain_view(symbol))

    try:
        data = await _run(_load)
        return data
    except Exception as e:
        return {"chain": [], "atm_strike": None, "spot": None, "pcr": None,
                "max_pain": None, "error": str(e)}


# ── Spike alerts ──────────────────────────────────────────────────────────────

@app.get("/api/spike-alerts")
async def get_spike_alerts(min_confidence: int = 55):
    """Intraday volume-spike alerts. Dhan-dependent — skipped when Dhan is down.

    Scanning the full F&O universe against a dead Dhan means 161 symbols x
    retry/backoff, which is how this endpoint used to block indefinitely. The
    probe result is already cached, so the short-circuit is free.
    """
    try:
        probe = await _run(_dhan_probe_cached, timeout=_DHAN_PROBE_TIMEOUT_SEC)
    except RunTimeout as e:
        return {"alerts": [], "ts": datetime.now().isoformat(),
                "skipped": True, "timeout": True, "error": str(e),
                "reason": "Dhan connection check timed out"}
    if not probe.get("working"):
        return {"alerts": [], "ts": datetime.now().isoformat(),
                "skipped": True,
                "reason": f"Dhan not available ({probe.get('status')})"}

    def _load():
        dd = _get_dashboard_data()
        from core.universe import FO_UNIVERSE
        key = f"spikes_{min_confidence}"
        return _cached(key, 60, lambda: dd.get_intraday_spike_alerts(
            FO_UNIVERSE, min_confidence=min_confidence
        ))

    try:
        alerts = await _run(_load)
        return {"alerts": alerts or [], "ts": datetime.now().isoformat()}
    except RunTimeout as e:
        return {"alerts": [], "ts": datetime.now().isoformat(),
                "timeout": True, "error": str(e)}
    except Exception as e:
        return {"alerts": [], "ts": datetime.now().isoformat(), "error": str(e)}


# ── Accuracy / P&L report ─────────────────────────────────────────────────────

@app.get("/api/accuracy")
async def get_accuracy():
    def _load():
        dd = _get_dashboard_data()

        # Signal journal as records
        df = dd.load_signal_journal_frame()
        records = []
        if not df.empty:
            records = df.where(df.notna(), other=None).to_dict(orient="records")

        # PnL summary
        pnl_summary = {}
        try:
            pnl_summary = dd.get_signal_pnl_summary()
        except Exception:
            pass

        # Live tracking
        tracking = []
        try:
            tracking = dd.get_live_signal_tracking()
        except Exception:
            pass

        return _json_safe({
            "records":     records,
            "pnl_summary": pnl_summary,
            "tracking":    tracking or [],
        })

    try:
        data = await _run(_load)
        return data
    except Exception as e:
        return {"records": [], "pnl_summary": {}, "tracking": [], "error": str(e)}


# ── Path-#1 allocation (equity-premium strategy) ──────────────────────────────

# Shape written by allocation_task.build_status(). Mirrored here so the key is
# ALWAYS present on the wire: a missing key reads as `undefined` in the UI,
# which is indistinguishable from "fresh" — the exact blind spot this closes.
_DQ_FIELDS = ("checked", "feed_ok", "last_bar", "age_days", "bars_added",
              "stale", "error")


def _normalize_data_quality(status: Dict[str, Any]) -> Dict[str, Any]:
    """Guarantee a full `data_quality` block on an allocation payload.

    Three distinct outcomes the UI must be able to tell apart:
      stale=True   feed is dead or bars are too old -> target is NOT current
      stale=False  verified fresh
      stale=None   never checked (legacy state file, or refresh=False with no
                   cache) -> unverified, which is not the same as fresh
    """
    dq = status.get("data_quality")
    if not isinstance(dq, dict):
        dq = {"checked": False, "error": "state file predates data_quality "
                                        "reporting - freshness unverified"}
    out = {k: dq.get(k) for k in _DQ_FIELDS}
    if not out.get("checked"):
        out["stale"] = None          # unknown, never a bare False
    else:
        out["stale"] = bool(out.get("stale"))
    status["data_quality"] = out
    return out


def _stale_alert_text(dq: Dict[str, Any]) -> str:
    return (f"STALE DATA: NIFTY cache last bar {dq.get('last_bar')} "
            f"({dq.get('age_days')}d old), feed_ok={dq.get('feed_ok')}"
            + (f" - {dq['error']}" if dq.get("error") else "")
            + ". The target shown is computed from OLD prices and is NOT "
              "current. Do NOT rebalance on it; fix the feed and re-run.")


@app.get("/api/allocation")
async def get_allocation(refresh: bool = False):
    """Path-#1 readout: today's target allocation + honest backtest + accuracy
    by holding horizon. Serves logs/allocation_state.json when fresh (written by
    allocation_task.py); recomputes when missing/stale or ?refresh=true.

    `data_quality` rides along on every response — including the error path.
    Note the two independent notions of freshness: the state *file* mtime (is
    the JSON recent?) and `data_quality` (were the PRICES it was computed from
    recent?). A file rewritten a minute ago from a dead feed passes the first
    and fails the second, so the second is what the dashboard must show.
    """
    def _load():
        from allocation_task import STATE_FILE, ALERT_FILE, build_status

        status = None
        if not refresh and os.path.exists(STATE_FILE):
            age_h = (time.time() - os.path.getmtime(STATE_FILE)) / 3600
            if age_h < 24:
                with open(STATE_FILE, encoding="utf-8") as f:
                    status = json.load(f)
        if status is None:
            status = build_status(refresh=refresh)

        dq = _normalize_data_quality(status)

        alert = None
        if os.path.exists(ALERT_FILE):
            with open(ALERT_FILE, encoding="utf-8") as f:
                alert = f.read().strip()
        # allocation_task writes this alert itself, but the API can serve a
        # stale state the task never got to write for (hand-run, crash between
        # state and alert write). Synthesise rather than render an empty banner.
        if dq.get("stale") and not alert:
            alert = _stale_alert_text(dq)
        status["alert"] = alert
        return status

    try:
        return await _run(_load)
    except Exception as e:
        return {"targets": {}, "backtest": {}, "horizon_accuracy": {},
                "alert": None, "error": str(e),
                "data_quality": _normalize_data_quality({})}


# ── Dhan live probe (validity, not presence) ────────────────────────────────

def _dhan_configured() -> Dict[str, Any]:
    """Which credentials are stored right now — a pure local read, no network.

    The config form's (stored)/(missing) labels come from this. It MUST stay
    network-free so the labels are always truthful even when the live probe
    times out; otherwise a slow Dhan makes stored creds look unsaved and the
    user re-enters them in an endless loop.
    """
    from core import secrets as _sec

    def _try(fn):
        try:
            return fn()
        except Exception:
            return None

    trade_token = _try(_sec.get_access_token)
    data_token = _try(_sec.get_data_token)
    data_api_key = _try(_sec.get_data_api_key)
    api_secret = _try(_sec.get_data_api_secret)
    client_id = _try(_sec.get_client_id)

    return {
        "trade_token": bool(trade_token and trade_token.startswith("eyJ")),
        "data_token": bool(data_token),
        "data_api_key": bool(data_api_key),
        "data_api_secret": bool(api_secret),
        "client_id": bool(client_id),
    }


def _dhan_probe() -> Dict[str, Any]:
    """Probe Dhan with stored credentials; report what actually came back.

    Presence of a token file is NOT liveness — the expired subscription kept
    showing "Active" for exactly that reason. One cheap authenticated call:
      200  -> working
      401  -> expired / invalid subscription        (terminal)
      429  -> RATE LIMITED, nothing wrong with you  (transient)
      5xx  -> Dhan side                             (transient)
      else -> auth accepted, request shape rejected (DH-905 class)

    429 used to fall into the catch-all and was reported as "request shape
    rejected (DH-905 class) - subscription alive", which is the opposite
    diagnosis: the request was fine, it was sent too often. That message sent
    people hunting a schema bug while the real cause was the scanner and this
    probe competing for the same endpoint.

    Which is the second fix: the probe used /charts/historical, the exact path
    scan_universe hammers under a 1-req/sec global limit, so a running scan
    reliably 429'd the probe. It now uses /optionchain/expirylist — cheap,
    authenticated, and on a different bucket — and stands down entirely while
    a shared chart/option-chain backoff is in force rather than adding to it.
    """
    import requests as rq
    from core import secrets as _sec

    def _try(fn):
        try:
            return fn()
        except Exception:
            return None

    trade_token = _try(_sec.get_access_token)
    data_token = _try(_sec.get_data_token)
    data_api_key = _try(_sec.get_data_api_key)
    api_secret = _try(_sec.get_data_api_secret)
    client_id = _try(_sec.get_client_id)

    # /charts/* is served against the DATA credential, not the trading
    # token — probing the wrong one reports "unconfigured" while the real
    # blocker is an expired data subscription. get_data_token() already
    # validates the data key (JWT + length) and falls back to the trade JWT,
    # so an 8-char app-id can never become the Access-Token header here.
    token = data_token or trade_token
    used = ("data_token" if data_token else ("trade" if trade_token else None))

    configured = _dhan_configured()
    if not token:
        return {"configured": configured, "working": False,
                "status": "unconfigured", "probed_with": None,
                "message": "no Dhan token stored - paste one below"}

    headers = {"Content-Type": "application/json",
               "Access-Token": token, "client-id": client_id or ""}
    if api_secret:
        headers["api-secret"] = api_secret

    # Do not add load while the scanner is already backing off — probing into
    # an active rate limit guarantees the 429 it is meant to detect.
    try:
        import time as _t
        from core.api_dhan import _shared_backoff_get
        wait = max(_shared_backoff_get("chart"), _shared_backoff_get("oc")) - _t.time()
        if wait > 0:
            return {"configured": configured, "working": False,
                    "status": "rate_limited", "transient": True,
                    "probed_with": used, "retry_after": round(wait),
                    "message": (f"Dhan rate limit active for another {wait:.0f}s "
                                "(scanner is using the quota) - not a credential "
                                "problem, retrying automatically")}
    except Exception:
        pass

    try:
        r = rq.post(
            "https://api.dhan.co/v2/optionchain/expirylist",
            json={"UnderlyingScrip": 13, "UnderlyingSeg": "IDX_I"},
            headers=headers, timeout=(3, 6), verify=False,
        )
        code = r.status_code
    except Exception as e:
        return {"configured": configured, "working": False,
                "status": "unreachable", "transient": True,
                "probed_with": used,
                "message": f"probe failed: {type(e).__name__} - retrying automatically"}

    if code == 200:
        return {"configured": configured, "working": True, "http": code,
                "status": "working", "probed_with": used,
                "message": "Dhan data API responding"}
    if code == 401:
        return {"configured": configured, "working": False, "http": code,
                "status": "expired", "probed_with": used,
                "message": ("Dhan returned 401 - subscription expired or "
                            "token invalid. Live scanning stays idle "
                            "until a valid token is saved.")}
    if code == 429:
        try:
            retry = float(r.headers.get("Retry-After", 30))
        except Exception:
            retry = 30.0
        return {"configured": configured, "working": False, "http": code,
                "status": "rate_limited", "transient": True,
                "probed_with": used, "retry_after": round(retry),
                "message": (f"HTTP 429 - too many requests, retry in {retry:.0f}s. "
                            "Credentials are fine; the scanner and this check "
                            "share one quota.")}
    if code >= 500:
        return {"configured": configured, "working": False, "http": code,
                "status": "dhan_down", "transient": True, "probed_with": used,
                "message": f"HTTP {code} from Dhan - their side, retrying automatically"}
    return {"configured": configured, "working": False, "http": code,
            "status": "auth_ok_shape_issue", "probed_with": used,
            "message": (f"HTTP {code}: auth accepted but request shape "
                        "rejected (DH-905 class) - subscription alive")}


# Last known probe result, kept so the UI is never blanked by one bad call.
_PROBE: Dict[str, Any] = {"result": None, "at": 0.0, "inflight": False,
                          # Last verdict that actually reached Dhan and got a
                          # 200, kept separately from `result`. A transient
                          # answer (429/timeout/5xx) overwrote `result` and so
                          # ERASED a perfectly good verdict: the panel flipped
                          # to "RATE LIMITED / not working" while the token was
                          # valid and the scanner was happily using the very
                          # quota that caused the 429. Keeping the good answer
                          # separately lets a blip degrade the panel instead of
                          # contradicting it.
                          "good": None, "good_at": 0.0}
_PROBE_LOCK = threading.Lock()

# A working probe is good for a while. A 401 is a real answer, so re-check
# occasionally rather than constantly. A 429/timeout says nothing at all, so
# expire it fast — otherwise one rate-limited call made the dashboard look
# broken for the full cache window even after credentials were fixed.
_TTL_OK = 900.0
_TTL_TERMINAL = 120.0
_TTL_TRANSIENT = 20.0


def _ttl_for(result: Dict[str, Any]) -> float:
    if result.get("working"):
        return _TTL_OK
    if result.get("transient"):
        return _TTL_TRANSIENT
    return _TTL_TERMINAL


def _with_last_good(res: Dict[str, Any]) -> Dict[str, Any]:
    """Stop a transient answer from contradicting a recent successful probe.

    429 / timeout / Dhan 5xx say nothing about the credentials. If Dhan
    answered 200 within the last _TTL_OK, that verdict is still the best
    evidence available, so report it and attach the transient condition as
    `degraded`. The panel then reads "working, currently throttled" instead
    of flipping to "not working" because the scanner was using the quota.

    Bounded on purpose: once the good verdict ages past _TTL_OK it stops being
    served, so a genuinely dead connection cannot hide behind it forever.
    """
    if not res.get("transient"):
        return res
    with _PROBE_LOCK:
        good, at = _PROBE["good"], _PROBE["good_at"]
    if not good or (time.monotonic() - at) >= _TTL_OK:
        return res
    return {**good,
            "configured": _dhan_configured(),   # local read, always current
            "working": True,
            "degraded": res.get("status"),
            "degraded_message": res.get("message"),
            "retry_after": res.get("retry_after"),
            "good_age_sec": round(time.monotonic() - at),
            "refreshing": True}


def _probe_now() -> Dict[str, Any]:
    """Run the probe and store it, guarding against duplicate concurrent runs."""
    try:
        res = _dhan_probe()
    except Exception as e:                     # never poison the cache
        res = {"configured": _dhan_configured(), "working": False,
               "status": "error", "transient": True, "message": str(e)}
    with _PROBE_LOCK:
        _PROBE["result"] = res
        _PROBE["at"] = time.monotonic()
        if res.get("working"):
            _PROBE["good"] = res
            _PROBE["good_at"] = time.monotonic()
        _PROBE["inflight"] = False
    return res


def _dhan_probe_cached(refresh: bool = False) -> Dict[str, Any]:
    """Stale-while-revalidate. Returns immediately, always.

    The old version called the network inside the request and cached whatever
    came back for 15 minutes. Two consequences the dashboard actually suffered:
    saving credentials blocked on a live call and showed TIMEOUT when Dhan was
    slow, and a single 429 pinned "broken" for 15 minutes even after the
    credentials were corrected.

    NOTHING here blocks, including the cold start. That last blocking path is
    what still produced a red TIMEOUT after navigating back to the page: with
    an empty cache the request ran a live probe, and a slow or rate-limited
    Dhan tripped the 10s deadline -- so the panel showed TIMEOUT while the
    credentials were stored, the scanner was running and the connection was
    in fact live. A first load now reports "checking" (a transient state) and
    the answer arrives on the next poll a couple of seconds later.
    """
    # _PROBE_LOCK is a plain Lock, so NOTHING inside this block may call
    # _with_last_good — it takes the same lock and would deadlock the request
    # thread. Decide under the lock, then build the response outside it.
    with _PROBE_LOCK:
        cached = _PROBE["result"]
        age = time.monotonic() - _PROBE["at"] if cached else None
        fresh = cached is not None and age < _ttl_for(cached)
        serve_fresh = bool(cached and fresh and not refresh)
        already = _PROBE["inflight"]
        if not serve_fresh and not already:
            _PROBE["inflight"] = True

    if serve_fresh:
        return _with_last_good({**cached, "age_sec": round(age),
                                "stale": False})

    if not already:
        threading.Thread(target=_probe_now, daemon=True,
                         name="dhan-probe").start()

    if cached is None:
        return _with_last_good(
            {"configured": _dhan_configured(), "working": False,
             "status": "checking", "transient": True, "refreshing": True,
             "message": "checking the Dhan connection…"})
    return _with_last_good({**cached, "age_sec": round(age), "stale": True,
                            "refreshing": True})


@app.get("/api/dhan-live-status")
async def dhan_live_status(refresh: bool = False):
    """Live Dhan connection state - probed, never inferred from file presence.

    `configured` (which creds are stored) is a local read and is ALWAYS
    included — even when the live probe times out — so the config form's
    (stored)/(missing) labels stay truthful regardless of Dhan latency.
    """
    def _last_known(fallback_status: str, msg: str) -> Dict[str, Any]:
        """Never throw away a good answer because this one call was slow.

        Reporting a bare TIMEOUT is what let the panel show red while the
        credentials were stored, the scanner was running and the connection
        was live. If a verdict was ever reached, serve it and mark it stale.
        """
        with _PROBE_LOCK:
            cached = _PROBE["result"]
            age = time.monotonic() - _PROBE["at"] if cached else None
        if cached:
            return {**cached, "age_sec": round(age), "stale": True,
                    "refreshing": True}
        return {"configured": _dhan_configured(), "working": False,
                "status": fallback_status, "transient": True,
                "refreshing": True, "message": msg}

    try:
        return await _run(lambda: _dhan_probe_cached(refresh),
                          timeout=_DHAN_PROBE_TIMEOUT_SEC)
    except RunTimeout:
        return _last_known("checking", "still checking the Dhan connection…")
    except Exception as e:
        return _last_known("error", str(e))


@app.get("/api/account")
async def get_account():
    """The funded paper account — one pot of money, marked to market.

    This is the book that actually trades: it selects its own positions, sizes
    them against `risk_per_trade_pct`, debits cash to open and credits it on
    exit. Unlike /api/portfolio (a retrospective replay of a journal) and
    /api/accuracy (per-signal statistics), the number here is a balance: it
    moves only because a position was funded, marked, or closed.

    Read-only by design. Resetting the book and running a cycle are
    side-effectful and stay on the CLI, so a stray GET can never wipe a P&L
    history or open positions.
    """
    def _load():
        from core.trading_account import summary
        return _json_safe(summary())

    try:
        return await _run(lambda: _cached("account", 30, _load))
    except RunTimeout as e:
        return {"ok": False, "reason": str(e)}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@app.get("/api/macro-fund")
async def get_macro_fund():
    """The paper macro fund — the cross-asset trend sleeve as a real book.

    Distinguish it from its neighbours, which answer different questions and
    will not agree:

      /api/signals      what does the equity scanner see right now?
      /api/account      the Rs 10L equity/options book — a cash ledger.
      /api/macro-fund   a USD notional book across 18 global markets.

    This one holds NOTIONAL exposure financed by margin, so gross exposure
    exceeds NAV by design (up to 3x). Its invariant is
    NAV == capital + realised + unrealised, not a cash ledger — see
    core/macro_fund.py for why enforcing the equity book's invariant here
    would be wrong rather than safe.

    `mature` is false until the H-022 pre-registered window closes on
    2027-09-05. Until then every number here is a progress indicator, not a
    verdict, and the UI is expected to say so.

    Read-only. Reset and cycle stay on the CLI (`python macro_task.py`) so a
    stray GET cannot wipe the book.
    """
    def _load():
        from core import macro_fund as mf
        state = mf.load_state()
        if not state:
            return {"ok": False, "reason": "no book yet — run: python macro_task.py"}
        perf = mf.performance(state)
        exp = mf.exposure(state)
        return _json_safe({
            "ok": True,
            "mode": state.get("mode", "PAPER"),
            "hypothesis": state.get("hypothesis", "H-022"),
            "base_currency": state.get("base_currency", "USD"),
            "started": state.get("started"),
            "last_mark": state.get("last_mark"),
            "last_rebalance": state.get("last_rebalance"),
            "performance": perf,
            "exposure": exp,
            "positions": sorted(state.get("positions", []),
                                key=lambda p: -abs(float(p.get("market_value") or 0))),
            "closed": state.get("closed", [])[:40],
            "curve": state.get("curve", [])[-400:],
            "note": state.get("note", ""),
        })

    try:
        return await _run(lambda: _cached("macro_fund", 30, _load))
    except RunTimeout as e:
        return {"ok": False, "reason": str(e)}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@app.get("/api/paper-book")
async def get_paper_book():
    """Forward paper run of the 50/50 swing book.

    Always returns `confidence` alongside P&L. A month of data is n~1 on the
    options sleeve and cannot say whether the system works — the field exists
    so a green month is never read as validation.
    """
    def _load():
        out: Dict[str, Any] = {}
        try:
            from core.paper_book import status as pb_status
            out["status"] = pb_status()
        except Exception as e:
            out["status"] = {"ok": False, "reason": str(e)}
        try:
            from core.swing_book import plan as book_plan
            st = out.get("status") or {}
            cap = st.get("capital") or 100000.0
            out["plan"] = book_plan(float(cap)).to_dict()
        except Exception as e:
            out["plan"] = {"error": str(e)}
        return out

    try:
        return await _run(lambda: _cached("paperbook", 120, _load))
    except RunTimeout as e:
        return {"status": {"ok": False, "reason": str(e)}}
    except Exception as e:
        return {"status": {"ok": False, "reason": str(e)}}


# ── Learning rules (what the system taught itself, and whether it held) ─────

@app.get("/api/learning-rules")
async def get_learning_rules():
    """Active mistake guards + keeper boosts, each with its post-deploy verdict.

    Surfaces the closed learn loop: what was learned, what survived deployment,
    and what was retired. The UNOBSERVABLE verdict is deliberately distinct
    from CONFIRMED — a guard that stopped its own trades produced no evidence,
    and silence must never render as success.
    """
    def _load():
        out: Dict[str, Any] = {}
        try:
            from core.mistake_learner import load_guards
            out["guards"] = load_guards()
        except Exception as e:
            out["guards"] = []
            out["guards_error"] = str(e)
        try:
            from core.keeper_learner import load_boosts
            out["boosts"] = load_boosts()
        except Exception as e:
            out["boosts"] = []
            out["boosts_error"] = str(e)

        # Post-deployment verdicts (read-only here; enforcement runs in EOD).
        try:
            from core.rule_recurrence import evaluate
            out["recurrence"] = [v.to_dict() for v in evaluate()]
        except Exception as e:
            out["recurrence"] = []
            out["recurrence_error"] = str(e)

        # Retirement history — rules the system disproved and dropped.
        retired = []
        rpath = Path("logs/rule_retirements.jsonl")
        if rpath.exists():
            try:
                with open(rpath, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            retired.append(json.loads(line))
            except Exception:
                pass
        out["retired"] = retired[-20:]

        # Ledger tallies: how much was mined vs how little survived.
        def _tally(p: str) -> Dict[str, int]:
            t = {"promote": 0, "reject": 0}
            fp = Path(p)
            if fp.exists():
                try:
                    with open(fp, encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            d = json.loads(line).get("decision")
                            if d in t:
                                t[d] += 1
                except Exception:
                    pass
            return t

        out["ledger"] = {
            "mistake": _tally("logs/mistake_ledger.jsonl"),
            "keeper": _tally("logs/keeper_ledger.jsonl"),
        }
        out["note"] = (
            "Rules are promoted only after a temporal holdout, a recurrence "
            "check and Bonferroni correction — then re-judged on trades "
            "resolved AFTER deployment. UNOBSERVABLE means a guard suppressed "
            "its own evidence: obeyed, not proven."
        )
        return out

    try:
        return await _run(lambda: _cached("learnrules", 120, _load))
    except RunTimeout as e:
        return {"guards": [], "boosts": [], "recurrence": [], "error": str(e)}
    except Exception as e:
        return {"guards": [], "boosts": [], "recurrence": [], "error": str(e)}


# ── Strategy verdict (the "is it good or does it need upgrade" answer) ──────

@app.get("/api/verdict")
async def get_verdict():
    """Aggregated strategy verdict from the honest sources of record.

    Rules are transparent and computed here, not styled client-side:
    a strategy is GOOD only if the honest journal PF > 1 net of the
    exclusions honest_performance applies, or the hypothesis registry
    holds a non-rejected verdict for it. Everything else is evidence
    AGAINST upgrade-by-tinkering — the registry exists precisely to stop
    re-proposing rejected hunts with new parameters.
    """
    def _load():
        out: Dict[str, Any] = {}

        # 1. Honest journal performance (all recorded signals, clean subset).
        try:
            from core.honest_performance import from_journal
            p = from_journal()
            perf = vars(p) if hasattr(p, "__dict__") else dict(p)
            out["journal_perf"] = perf
        except Exception as e:
            out["journal_perf"] = {"error": str(e)}

        # 2. Hypothesis registry — every hunt and its verdict.
        try:
            from core.hypothesis_registry import status as hyp_status
            out["hypotheses"] = hyp_status()
        except Exception as e:
            out["hypotheses"] = [{"error": str(e)}]

        # 3. Swing strategy health (decay monitor) + the standing decision.
        try:
            from core.strategy_health import load_health
            out["swing_health"] = load_health()
        except Exception as e:
            out["swing_health"] = {"error": str(e)}
        out["swing_decision"] = {
            "status": "CLOSED",
            "date": "2026-07-21",
            "detail": ("india_swing permanently dropped - do not re-tune, "
                       "re-backtest, or buy data. Money path is the "
                       "allocation engine (index-core + 200DMA overlay)."),
        }

        # 4. Data-layer truth the panels must not hide.
        # This was hardcoded `expired: True, since 2026-07-24`. The
        # subscription was restored on 2026-08-04, so the page kept showing a
        # red "DHAN DATA API EXPIRED" banner while the header two rows above it
        # reported "DATA API Active" — the dashboard contradicting itself.
        # Read the same live probe the header reads; a banner about a dead feed
        # is only worth anything if it goes away when the feed comes back.
        try:
            probe = _dhan_probe_cached()
            status = probe.get("status")
            # Only a confirmed 401 is "expired". A 429, a 5xx, an unreachable
            # host or a probe still in flight are NOT an expired subscription,
            # and must not raise a banner that tells the user to go and pay.
            expired = status == "expired"
            if expired:
                consequence = probe.get("message") or "subscription expired"
            elif status == "working":
                consequence = "data API responding; charts and history available"
            else:
                consequence = f"probe status: {status or 'unknown'}"
            out["dhan"] = {
                "expired": expired,
                "status": status,
                "since": "2026-07-24" if expired else None,
                "consequence": consequence,
            }
        except Exception as e:
            out["dhan"] = {"expired": False, "status": "unknown",
                           "consequence": f"probe unavailable: {e}"}

        # 5. Rule-based verdicts, one row per strategy lane.
        perf = out.get("journal_perf", {})
        pf = perf.get("profit_factor")
        lanes = []
        lanes.append({
            "lane": "Signal terminal (intraday F&O)",
            "verdict": "REJECT" if (pf is not None and pf < 1.0) else "UNPROVEN",
            "evidence": (f"journal PF {pf}, win {perf.get('win_rate')}, "
                         f"expectancy {perf.get('expectancy_pct')}%/trade "
                         f"over {perf.get('n_clean')} clean trades"),
            "action": "do not size up; do not tinker-upgrade",
        })
        lanes.append({
            "lane": "India swing v3",
            "verdict": "CLOSED",
            "evidence": "decision 2026-07-21 after exhaustive negative",
            "action": "none - decision is permanent",
        })
        # Take the CHIP and the PROSE from the same source. These two strings
        # used to be a frozen snapshot from when the hunt was still open, so
        # once the registry closed H-009 the row rendered a REJECTED chip
        # beside "final statistician gate pending" — the tab telling the user
        # a settled question was still live.
        rsi2 = next((h for h in out.get("hypotheses", [])
                     if "RSI-2" in str(h.get("thesis", ""))), None)
        rsi2_verdict = str((rsi2 or {}).get("verdict", "UNKNOWN")).upper()
        if rsi2_verdict.startswith("REJECT"):
            rsi2_evidence = ("closed: measured trial-Sharpe dispersion sinks the "
                             "Deflated Sharpe gate at every N")
            rsi2_action = "do not re-propose - hunt is closed"
        elif rsi2_verdict == "CONDITIONAL-PASS":
            rsi2_evidence = "power-passing t=4.43; alive at 0.06-0.10% cost only"
            rsi2_action = "final statistician gate at 0.10% pending - not live"
        else:
            rsi2_evidence = f"registry verdict: {rsi2_verdict.lower()}"
            rsi2_action = "see hypothesis registry below"
        lanes.append({
            "lane": "RSI-2 mean-reversion (futures)",
            "verdict": rsi2_verdict,
            "evidence": rsi2_evidence,
            "action": rsi2_action,
        })
        lanes.append({
            "lane": "Allocation engine (index-core + 200DMA)",
            "verdict": "ACTIVE",
            "evidence": "path #1 decision 2026-06-26; earns market, not alpha",
            "action": "deploy capital + time; monitor monthly (/api/allocation)",
        })
        # 6. Claims in the tree that nothing ever verified.
        # The pairs lead read as validated for weeks because its own re-test was
        # written and never run. research_gates, research_integrity and the
        # registry all existed and all worked; nothing forced any of them to
        # fire. Surfacing the audit here is what makes it run without being
        # remembered.
        try:
            from core.claim_audit import unverified
            findings = unverified()
            by_file: Dict[str, Any] = {}
            for f in findings:
                by_file.setdefault(f.path, []).append(
                    {"line": f.line_no, "text": f.line})
            out["claim_audit"] = {
                "unverified": len(findings),
                "files": [{"path": k, "hits": v[:3], "n": len(v)}
                          for k, v in sorted(by_file.items())],
            }
        except Exception as e:
            out["claim_audit"] = {"error": str(e), "unverified": None}

        out["lanes"] = lanes
        out["bottom_line"] = (
            "Upgrade question is answered by evidence, not effort: the "
            "traded strategies are net-negative or closed; the validated "
            "path is the allocation engine. New ideas go through the "
            "hypothesis registry and statistician gate first."
        )
        return out

    try:
        return await _run(lambda: _cached("verdict", 300, _load))
    except Exception as e:
        return {"error": str(e)}


# ── Market capture (point-in-time archives + trend state) ───────────────────

@app.get("/api/portfolio")
async def get_portfolio(equity_capital: float = 500000.0,
                        options_capital: float = 500000.0,
                        position_pct: float = 0.10):
    """Two funded paper books run as an actual ledger.

    Replaces the old "Rs per trade x return" display, which had no pot to draw
    from: nothing was ever debited, so an unaffordable trade counted the same as
    an affordable one and 98 open positions looked like 3. Here capital is a
    real constraint and a skipped signal is reported, not silently paid out.
    """
    def _load():
        from core.paper_portfolio import build_portfolios
        return _json_safe(build_portfolios(
            equity_capital=equity_capital,
            options_capital=options_capital,
            position_pct=position_pct,
        ))
    try:
        return await _run(_load)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/capture")
async def get_capture():
    """Capture-job status and archive health.

    `intraday.worst_stale_days` is the number to watch: yfinance serves only
    ~60 days of 5-minute history, so once a symbol goes staler than that the
    missing sessions are unrecoverable. `unrecoverable: true` means data is
    actively being lost and capture_task.py is not running often enough.
    """
    def _load():
        from capture_task import load_status

        out = {"last_run": load_status()}

        # Survivorship-complete EOD archive.
        try:
            daily_dir = Path("logs/bhavcopy_archive/daily")
            sym_dir = Path("logs/bhavcopy_archive/symbols")
            days = sorted(p.stem for p in daily_dir.glob("*.parquet"))
            out["eod_archive"] = {
                "trading_days": len(days),
                "first": days[0] if days else None,
                "last": days[-1] if days else None,
                "symbols": sum(1 for _ in sym_dir.glob("*.parquet")),
                "survivorship_complete": True,
            }
        except Exception as e:
            out["eod_archive"] = {"error": str(e)}

        # Forward-only intraday archive.
        try:
            from core.intraday_capture import coverage, MAX_LOOKBACK_DAYS
            cov = coverage()
            held = cov[cov["bars"] > 0] if not cov.empty else cov
            if held.empty:
                out["intraday"] = {"symbols": 0}
            else:
                worst = int(held["stale_days"].max())
                out["intraday"] = {
                    "symbols": int(len(held)),
                    "sessions_median": int(held["sessions"].median()),
                    "first": str(held["first"].min()),
                    "last": str(held["last"].max()),
                    "worst_stale_days": worst,
                    "limit_days": MAX_LOOKBACK_DAYS,
                    "unrecoverable": bool(worst > MAX_LOOKBACK_DAYS),
                }
        except Exception as e:
            out["intraday"] = {"error": str(e)}

        return out

    try:
        return await _run(lambda: _cached("capture", 60, _load))
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/market-state")
async def get_market_state(universe: str = "top100", context: bool = False):
    """Trend state per symbol, with the evidence and the gaps in it.

    `unavailable_factors` is always returned. The canonical list of things that
    move a stock is far larger than what this stack can measure — order flow,
    depth, live news, options flow are all absent — and a caller must be able
    to see that rather than infer completeness from a confident label.

    context=true adds sector and market factors (slower: network per call).
    """
    def _load():
        from core import market_state as ms
        from core.universe import FO_UNIVERSE, TOP100_LIQUID

        symbols = list(FO_UNIVERSE if universe == "fo" else TOP100_LIQUID)
        states = ms.classify_many(symbols, with_context=context)

        tally: Dict[str, int] = {}
        for s in states:
            tally[s.direction] = tally.get(s.direction, 0) + 1

        return {
            "universe": universe,
            "with_context": context,
            "tally": tally,
            "states": [s.to_dict() for s in states],
            "unavailable_factors": ms.UNAVAILABLE_FACTORS,
            "limitation": (
                "Labels describe what the tape has done. These indicators "
                "cannot separate a trend from a lucky random walk - see "
                "core/market_state.py KNOWN LIMITATION."
            ),
        }

    try:
        return await _run(lambda: _cached(f"mstate:{universe}:{context}", 300, _load))
    except Exception as e:
        return {"tally": {}, "states": [], "error": str(e)}


# ── Swing framework (no-API strategy: screen + paper journal + health) ───────

@app.get("/api/swing")
async def get_swing():
    """Swing tab payload: last screen output, paper journal (open/resolved),
    strategy health (decay monitor) and learner buckets. All file-based —
    no Dhan credentials involved."""
    def _load():
        out = {"screen": None, "open": [], "resolved": [], "health": None,
               "learner": []}
        p = os.path.join("logs", "swing_screen.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                out["screen"] = json.load(f)
        jf = os.path.join("logs", "swing_paper_journal.jsonl")
        if os.path.exists(jf):
            for line in open(jf, encoding="utf-8"):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                (out["resolved"] if r.get("status") == "resolved"
                 else out["open"]).append(r)
        from core.strategy_health import load_health
        out["health"] = load_health()
        from core.swing_learner import SwingLearner
        L = SwingLearner()   # loads + migrates state itself (pooled buckets)
        for key, b in sorted(L.buckets.items()):
            d, reg = key.split("|")
            sig_mix = ", ".join(f"{s}:{sb['n']}" for s, sb
                                in sorted(b.get("by_signal", {}).items()))
            out["learner"].append({
                "direction": d, "signal": sig_mix or "(pooled)", "regime": reg,
                "n": b["n"], "wins": b["wins"],
                "weight": L.weight(d, "*", reg),
            })
        return out

    try:
        return await _run(_load)
    except Exception as e:
        return {"screen": None, "open": [], "resolved": [], "health": None,
                "learner": [], "error": str(e)}


# ── Intelligence / RAG ────────────────────────────────────────────────────────

@app.get("/api/intelligence")
async def get_intelligence():
    def _load():
        return _cached("intelligence", 120, _build_rag_brief)

    def _build_rag_brief():
        try:
            from core.rag_engine import build_default_rag_engine
            rag = build_default_rag_engine()
            return rag.build_market_brief()
        except Exception as e:
            return {"summary": [], "recommendations": [], "evidence": [], "error": str(e)}

    try:
        data = await _run(_load)
        return data
    except Exception as e:
        return {"summary": [], "recommendations": [], "evidence": [], "error": str(e)}


# ── Adaptive learning ────────────────────────────────────────────────────────

@app.get("/api/learning")
async def get_learning():
    def _load():
        from core.adaptive_learner import get_learner
        learner = get_learner()
        return {
            "param_summary":     learner.get_param_summary(),
            "pattern_stats":     learner.get_pattern_stats(),
            "regime_stats":      learner.get_regime_stats(),
            "feature_importance": learner.get_feature_importance(),
            "recent_changes":    _read_param_changes(limit=20),
        }
    try:
        return await _run(_load)
    except Exception as e:
        return {"error": str(e)}



# ── Mutating-endpoint guard ──────────────────────────────────────────────────
# Six POST endpoints write real state - Dhan credentials among them - and none
# of them checked anything. The saving grace is the loopback bind, so the
# exposure has been "any local process" rather than "the network". That is an
# acceptable trade for a desktop tool and a catastrophic one the moment
# API_HOST is widened, which the launcher makes a one-variable mistake.
#
# So: unchanged on loopback (no token needed, nothing to configure), and
# FAIL CLOSED off-loopback unless an API_TOKEN is set and presented. The
# dangerous configuration now has to be deliberate instead of accidental.
def _is_loopback_bind() -> bool:
    host = os.getenv("API_HOST", "127.0.0.1").strip()
    return host in ("127.0.0.1", "localhost", "::1", "")


def _require_write_auth(request: Request) -> None:
    """Raise 401/403 unless this mutating call is allowed."""
    token = (os.getenv("API_TOKEN") or "").strip()
    if token:
        sent = (request.headers.get("X-API-Token") or "").strip()
        if not sent or not secrets_compare(sent, token):
            raise HTTPException(status_code=401, detail="bad or missing X-API-Token")
        return
    if not _is_loopback_bind():
        raise HTTPException(
            status_code=403,
            detail=("refusing a state-changing call: API_HOST is not loopback "
                    "and no API_TOKEN is set. Set API_TOKEN and send it as "
                    "X-API-Token, or bind to 127.0.0.1."))


def secrets_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


@app.post("/api/learning/trigger")
async def trigger_learning(request: Request):
    _require_write_auth(request)
    def _run_learning():
        from core.adaptive_learner import get_learner
        changes = get_learner().maybe_update(force=True)
        return {"changes": changes, "ts": datetime.now().isoformat()}
    try:
        return await _run(_run_learning)
    except Exception as e:
        return {"error": str(e), "changes": {}}


@app.post("/api/learning/reset")
async def reset_learning(request: Request):
    _require_write_auth(request)
    try:
        from core.adaptive_learner import get_learner
        get_learner().reset_all()
        return {"status": "reset", "ts": datetime.now().isoformat()}
    except Exception as e:
        return {"error": str(e)}


def _read_param_changes(limit: int = 20) -> list:
    import os
    path = os.path.join(os.path.dirname(__file__), "logs", "param_changes.jsonl")
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception:
        pass
    return rows[-limit:]


# ── Agentic RAG — Enhanced intelligence endpoints ────────────────────────────

@app.get("/api/ask")
async def ask_rag(q: str = "", top_k: int = 5):
    """Interactive RAG query: search trade history, signals, system state."""
    if not q.strip():
        return {"error": "Missing query parameter 'q'"}

    def _query():
        from core.agentic_rag import get_agentic_rag
        rag = get_agentic_rag()
        return rag.query(q, top_k=top_k)

    try:
        return await _run(_query)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/insights")
async def get_insights():
    """Auto-generated performance insights + recommendations."""
    def _load():
        return _cached("agentic_insights", 120, _build_insights)

    def _build_insights():
        from core.agentic_rag import get_agentic_rag
        return get_agentic_rag().generate_insights()

    try:
        return await _run(_load)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/analytics")
async def get_analytics(days: int = 90):
    """Structured trade analytics: pattern WR, hour profiling, regime stats."""
    def _load():
        from core.agentic_rag import compute_trade_analytics
        from dataclasses import asdict
        analytics = compute_trade_analytics(days)
        result = asdict(analytics)
        # Convert dataclass lists to dicts for JSON
        result["best_patterns"] = [asdict(p) for p in analytics.best_patterns]
        result["worst_patterns"] = [asdict(p) for p in analytics.worst_patterns]
        result["best_hours"] = [asdict(h) for h in analytics.best_hours]
        result["worst_hours"] = [asdict(h) for h in analytics.worst_hours]
        return result

    try:
        return await _run(_load)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/feedback")
async def submit_feedback(request: Request, body: dict):
    """Operator feedback: blacklist pattern, adjust param, add note.

    Body examples:
      {"action": "blacklist_pattern", "pattern": "rsi_bearish_zone"}
      {"action": "adjust_param", "param": "min_votes", "value": 4}
      {"action": "note", "text": "RELIANCE showing false breakouts today"}
    """
    _require_write_auth(request)
    def _process():
        from core.agentic_rag import get_agentic_rag
        return get_agentic_rag().process_feedback(body)

    try:
        return await _run(_process)
    except Exception as e:
        return {"error": str(e)}


# ── Credentials config ────────────────────────────────────────────────────────

def _invalidate_probe() -> None:
    """Forget the stored verdict and start a fresh probe in the background.

    Saving credentials used to clear a cache and then BLOCK on a live probe,
    so a slow or rate-limited Dhan turned "save" into a 10-second wait ending
    in TIMEOUT -- while the credentials had in fact been written correctly.
    The save now returns as soon as the write succeeds and the UI watches the
    probe state, which is the part that can legitimately take time.
    """
    with _PROBE_LOCK:
        _PROBE["result"] = None
        _PROBE["at"] = 0.0
        # The last-good verdict belonged to the OLD credentials. Keeping it
        # would let a rate-limited probe report the previous token as working
        # after it had been replaced.
        _PROBE["good"] = None
        _PROBE["good_at"] = 0.0
    _CACHE.pop("dhanlive", None)
    threading.Thread(target=_probe_now, daemon=True,
                     name="dhan-probe-after-save").start()


@app.post("/api/config/token")
async def save_token(request: Request, body: dict):
    _require_write_auth(request)
    token = (body.get("token") or "").strip()
    if not token:
        return {"error": "Token is empty"}
    if not token.startswith("eyJ"):
        return {"error": "Expected JWT (starts with eyJ)"}
    try:
        from core import secrets as sec
        sec.save_access_token(token)
        _invalidate_probe()   # new creds: drop the old verdict, re-probe async
        th = sec.token_health(token)
        return {"status": "saved", "hours_left": th.hours_left, "valid": th.valid}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/config/client-id")
async def save_client_id(request: Request, body: dict):
    _require_write_auth(request)
    cid = (body.get("client_id") or "").strip()
    if not cid:
        return {"error": "Client ID is empty"}
    try:
        from core import secrets as sec
        sec.save_client_id(cid)
        _invalidate_probe()   # new creds: drop the old verdict, re-probe async
        return {"status": "saved"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/config/data-key")
async def save_data_key(request: Request, body: dict):
    _require_write_auth(request)
    key = (body.get("api_key") or "").strip()
    if not key:
        return {"error": "API key is empty"}
    try:
        from core import secrets as sec
        sec.save_data_api_key(key)
        _invalidate_probe()   # new creds: drop the old verdict, re-probe async
        return {"status": "saved"}
    except Exception as e:
        return {"error": str(e)}


# ── WebSocket — push on signals.json change ──────────────────────────────────

@app.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket):
    await websocket.accept()
    last_mtime: float = 0.0
    try:
        data = _read_signals()
        await websocket.send_json({
            "type":    "signals",
            "signals": _filter_grade(data, "A"),
            "ts":      data.get("ts"),
            "meta":    data.get("meta", {}),
        })

        while True:
            if SIGNALS_FILE.exists():
                mtime = SIGNALS_FILE.stat().st_mtime
                if mtime != last_mtime:
                    last_mtime = mtime
                    data = _read_signals()
                    await websocket.send_json({
                        "type":    "signals",
                        "signals": _filter_grade(data, "A"),
                        "ts":      data.get("ts"),
                        "meta":    data.get("meta", {}),
                    })
            # 3s poll: scanner writes every 30s anyway, no need to check 30x per write
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] /ws/signals handler error: {e}")


if __name__ == "__main__":
    import uvicorn
    # reload=False — auto-reload adds 100-500ms latency. Enable only for dev.
    # /api/config/* endpoints write Dhan credentials with no auth.
    # Bind loopback by default so LAN devices can't POST tokens.
    # Set API_HOST=0.0.0.0 only after adding auth (or accepting the risk).
    uvicorn.run("api_server:app", host=os.getenv("API_HOST", "127.0.0.1"), port=8000,
                reload=os.getenv("API_RELOAD", "0") == "1",
                workers=1, log_level="warning")
