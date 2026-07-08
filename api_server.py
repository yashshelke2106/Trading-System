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
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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
TRADES_FILE  = ROOT / "logs" / "trades.csv"

_executor = ThreadPoolExecutor(max_workers=4)

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


async def _run(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, fn, *args)


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


def _read_trades() -> List[Dict]:
    if not TRADES_FILE.exists():
        return []
    trades: List[Dict] = []
    try:
        with open(TRADES_FILE, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(dict(row))
    except Exception:
        pass
    return trades


def _has_complete_trade_data(t: Dict) -> bool:
    """A trade qualifies for P&L ONLY when all 4 price levels are present
    and the pnl is computed. No stubs, no None, no 0-entry.

    Required:
      entry_price > 0
      exit_price  > 0
      sl_price    > 0
      target_price > 0  (NSE_LOT_SIZES default 0 is fine; this is trade target)
      pnl_percent  is not None / not nan
    """
    try:
        ep = float(t.get("entry_price") or 0)
        xp = float(t.get("exit_price") or 0)
        sl = float(t.get("sl_price") or 0)
        tg = float(t.get("target_price") or 0)
        pn = t.get("pnl_percent")
        if pn is None or pn == "":
            return False
        float(pn)  # raises if nan-as-string
        return ep > 0 and xp > 0 and sl > 0 and tg > 0
    except (ValueError, TypeError):
        return False


def _compute_stats(trades: List[Dict]) -> Dict:
    if not trades:
        return {"total": 0, "qualified": 0, "wins": 0, "losses": 0, "expired": 0,
                "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
                "best_trade": 0.0, "worst_trade": 0.0,
                "skipped_incomplete": 0}

    # FILTER: only trades with complete entry/exit/SL/target data count toward P&L.
    # Incomplete rows are reported separately for transparency but excluded from WR/PnL.
    qualified = [t for t in trades if _has_complete_trade_data(t)]
    skipped = len(trades) - len(qualified)

    if not qualified:
        return {"total": len(trades), "qualified": 0, "wins": 0, "losses": 0,
                "expired": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
                "best_trade": 0.0, "worst_trade": 0.0,
                "skipped_incomplete": skipped}

    wins    = [t for t in qualified if t.get("status") == "WIN"]
    losses  = [t for t in qualified if t.get("status") == "LOSS"]
    expired = [t for t in qualified if t.get("status") == "EXPIRED"]

    def _pnl(t):
        try:
            return float(t.get("pnl", 0))
        except (ValueError, TypeError):
            return 0.0

    pnls = [_pnl(t) for t in qualified]
    total_pnl = sum(pnls)
    decided = len(wins) + len(losses)   # WR denominator excludes EXPIRED
    return {
        "total":              len(trades),
        "qualified":          len(qualified),
        "skipped_incomplete": skipped,
        "wins":               len(wins),
        "losses":             len(losses),
        "expired":            len(expired),
        "win_rate":           round(len(wins) / decided * 100, 1) if decided else 0.0,
        "total_pnl":          round(total_pnl, 2),
        "avg_pnl":            round(total_pnl / len(qualified), 2) if qualified else 0.0,
        "best_trade":         round(max(pnls), 2) if pnls else 0.0,
        "worst_trade":        round(min(pnls), 2) if pnls else 0.0,
    }


# ── Dashboard data helpers (lazy import to avoid startup crash) ───────────────

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
    trades = _read_trades()
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        trades = [t for t in trades if t.get("timestamp", "") >= cutoff]
    return {"trades": trades, "stats": _compute_stats(trades)}


@app.get("/api/stats")
def get_stats():
    trades = _read_trades()
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

    try:
        dh = sec.data_token_health()
        data_api = {"valid": dh.valid, "message": dh.message}
    except Exception:
        data_api = {"valid": False, "message": "Error"}

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

        return {
            "records":     records,
            "pnl_summary": pnl_summary,
            "tracking":    tracking or [],
        }

    try:
        data = await _run(_load)
        return data
    except Exception as e:
        return {"records": [], "pnl_summary": {}, "tracking": [], "error": str(e)}


# ── Path-#1 allocation (equity-premium strategy) ──────────────────────────────

@app.get("/api/allocation")
async def get_allocation(refresh: bool = False):
    """Path-#1 readout: today's target allocation + honest backtest + accuracy
    by holding horizon. Serves logs/allocation_state.json when fresh (written by
    allocation_task.py); recomputes when missing/stale or ?refresh=true."""
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

        alert = None
        if os.path.exists(ALERT_FILE):
            with open(ALERT_FILE, encoding="utf-8") as f:
                alert = f.read().strip()
        status["alert"] = alert
        return status

    try:
        return await _run(_load)
    except Exception as e:
        return {"targets": {}, "backtest": {}, "horizon_accuracy": {},
                "alert": None, "error": str(e)}


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
        lf = os.path.join("logs", "swing_learner.json")
        if os.path.exists(lf):
            with open(lf, encoding="utf-8") as f:
                buckets = json.load(f).get("buckets", {})
            from core.swing_learner import SwingLearner
            L = SwingLearner()
            for key, b in sorted(buckets.items()):
                d, s, reg = key.split("|")
                out["learner"].append({
                    "direction": d, "signal": s, "regime": reg,
                    "n": b["n"], "wins": b["wins"],
                    "weight": L.weight(d, s, reg),
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


@app.post("/api/learning/trigger")
async def trigger_learning():
    def _run_learning():
        from core.adaptive_learner import get_learner
        changes = get_learner().maybe_update(force=True)
        return {"changes": changes, "ts": datetime.now().isoformat()}
    try:
        return await _run(_run_learning)
    except Exception as e:
        return {"error": str(e), "changes": {}}


@app.post("/api/learning/reset")
async def reset_learning():
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
async def submit_feedback(body: dict):
    """Operator feedback: blacklist pattern, adjust param, add note.

    Body examples:
      {"action": "blacklist_pattern", "pattern": "rsi_bearish_zone"}
      {"action": "adjust_param", "param": "min_votes", "value": 4}
      {"action": "note", "text": "RELIANCE showing false breakouts today"}
    """
    def _process():
        from core.agentic_rag import get_agentic_rag
        return get_agentic_rag().process_feedback(body)

    try:
        return await _run(_process)
    except Exception as e:
        return {"error": str(e)}


# ── Credentials config ────────────────────────────────────────────────────────

@app.post("/api/config/token")
async def save_token(body: dict):
    token = (body.get("token") or "").strip()
    if not token:
        return {"error": "Token is empty"}
    if not token.startswith("eyJ"):
        return {"error": "Expected JWT (starts with eyJ)"}
    try:
        from core import secrets as sec
        sec.save_access_token(token)
        th = sec.token_health(token)
        return {"status": "saved", "hours_left": th.hours_left, "valid": th.valid}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/config/client-id")
async def save_client_id(body: dict):
    cid = (body.get("client_id") or "").strip()
    if not cid:
        return {"error": "Client ID is empty"}
    try:
        from core import secrets as sec
        sec.save_client_id(cid)
        return {"status": "saved"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/config/data-key")
async def save_data_key(body: dict):
    key = (body.get("api_key") or "").strip()
    if not key:
        return {"error": "API key is empty"}
    try:
        from core import secrets as sec
        sec.save_data_api_key(key)
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
