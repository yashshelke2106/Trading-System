from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as _cfg

from . import secrets as sec
from .api_dhan import DhanAPI, get_security_id

log = logging.getLogger(__name__)
from .signal_writer import read_signals
from .universe import FO_UNIVERSE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
JOURNAL_FILE = os.path.join(LOG_DIR, "signal_journal.jsonl")
LEARNING_FILE = os.path.join(LOG_DIR, "learning.json")
PERFORMANCE_FILE = os.path.join(LOG_DIR, "performance.json")
STATE_FILE = os.path.join(LOG_DIR, "state.json")
STRATEGY_FILE = os.path.join(PROJECT_ROOT, "STRATEGIES.md")

MARKET_OPEN_MIN = 9 * 60 + 15
MARKET_TOTAL_MIN = 375

_API_LOCK = threading.Lock()
_API: Optional[DhanAPI] = None
_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Dict[str, object]] = {}


def get_api() -> DhanAPI:
    global _API
    with _API_LOCK:
        if _API is None:
            _API = DhanAPI()
        return _API


def refresh_api() -> DhanAPI:
    global _API
    with _API_LOCK:
        _API = DhanAPI()
        return _API


def _cached(key: str, ttl_seconds: float, loader):
    now = time.time()
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if item and now - float(item["ts"]) < ttl_seconds:
            return item["value"]
    value = loader()
    with _CACHE_LOCK:
        _CACHE[key] = {"ts": now, "value": value}
    return value


def clear_cache(prefix: str | None = None) -> None:
    with _CACHE_LOCK:
        if prefix is None:
            _CACHE.clear()
            return
        for key in list(_CACHE):
            if key.startswith(prefix):
                _CACHE.pop(key, None)


def now_ist() -> datetime:
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).replace(tzinfo=None)


def market_elapsed_minutes(now: Optional[datetime] = None) -> int:
    current = now or now_ist()
    elapsed = current.hour * 60 + current.minute - MARKET_OPEN_MIN
    return max(elapsed, 0)


def seconds_to_market_open(now: Optional[datetime] = None) -> int:
    current = now or now_ist()
    open_dt = current.replace(hour=9, minute=15, second=0, microsecond=0)
    if current < open_dt and current.weekday() < 5:
        return int((open_dt - current).total_seconds())

    next_open = open_dt + timedelta(days=1)
    while next_open.weekday() >= 5:
        next_open += timedelta(days=1)
    return int((next_open - current).total_seconds())


def read_json_file(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def read_text_file(path: str) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return ""


def get_signal_payload() -> Dict:
    return _cached("signals:payload", 2, lambda: read_signals() or {})


def get_live_positions() -> List[Dict]:
    def loader() -> List[Dict]:
        try:
            return get_api().get_positions() or []
        except Exception:
            return []

    return _cached("positions:live", 10, loader)


def _normalize_outcome(row: pd.Series) -> str:
    exit_reason = str(row.get("exit_reason", "") or "").strip().upper()
    status = str(row.get("status", "") or "").strip().upper()
    if exit_reason in {"TARGET_HIT", "SL_HIT", "CLOSED", "OPEN"}:
        return exit_reason
    if status == "WIN":
        return "TARGET_HIT"
    if status == "LOSS":
        return "SL_HIT"
    if status == "OPEN":
        return "OPEN"
    if status == "CLOSED":
        return "CLOSED"
    return exit_reason or status


def load_trades_frame() -> pd.DataFrame:
    def loader() -> pd.DataFrame:
        if not os.path.exists(TRADES_CSV):
            return pd.DataFrame()
        try:
            df = pd.read_csv(TRADES_CSV)
        except Exception:
            return pd.DataFrame()
        if df.empty:
            return df

        for col in ("entry_price", "exit_price", "stop_loss", "target",
                    "quantity", "pnl", "pnl_percent", "rank", "total_score"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype(str).str.upper()
        if "direction" in df.columns:
            df["direction"] = df["direction"].astype(str).str.upper()
        if "status" in df.columns:
            df["status"] = df["status"].astype(str).str.upper()

        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        else:
            df["timestamp"] = pd.NaT

        df["outcome"] = df.apply(_normalize_outcome, axis=1)
        df["date"] = df["timestamp"].dt.date
        return df

    return _cached("trades:frame", 5, loader).copy()


def get_trade_summary() -> Dict:
    df = load_trades_frame()
    if df.empty:
        return {
            "summary": {
                "total_pnl": 0.0,
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "open_positions": 0,
                "win_rate": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "profit_factor": 0.0,
                "expectancy": 0.0,
            },
            "recent_trades": [],
        }

    closed_raw = df[df["outcome"].isin(["TARGET_HIT", "SL_HIT", "CLOSED"])]
    # Quality filter — only trades with complete entry/exit/SL/target/pnl details
    closed = closed_raw.copy()
    for col in ("entry_price", "exit_price", "sl_price", "target_price"):
        if col in closed.columns:
            closed = closed[pd.to_numeric(closed[col], errors="coerce") > 0]
    if "pnl_percent" in closed.columns:
        closed = closed[pd.to_numeric(closed["pnl_percent"], errors="coerce").notna()]

    wins = closed[closed["outcome"] == "TARGET_HIT"]
    losses = closed[closed["outcome"] == "SL_HIT"]
    open_t = df[df["outcome"] == "OPEN"]

    total_pnl = float(closed["pnl"].sum()) if not closed.empty else 0.0
    win_rate = float(len(wins) / len(closed) * 100) if len(closed) else 0.0
    avg_win = float(wins["pnl"].mean()) if len(wins) else 0.0
    avg_loss = float(losses["pnl"].mean()) if len(losses) else 0.0
    profit_factor = float(abs(avg_win / avg_loss)) if avg_loss else 0.0
    expectancy = float((win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss))

    recent_cols = [
        col for col in [
            "timestamp", "symbol", "direction", "entry_price", "exit_price",
            "quantity", "pnl", "pnl_percent", "outcome", "status", "exit_reason",
        ] if col in df.columns
    ]
    recent = (
        df.sort_values("timestamp", ascending=False)[recent_cols]
        .head(25)
        .copy()
    )
    if "timestamp" in recent.columns:
        recent["timestamp"] = recent["timestamp"].astype(str)

    return {
        "summary": {
            "total_pnl": total_pnl,
            "total_trades": int(len(closed)),
            "wins": int(len(wins)),
            "losses": int(len(losses)),
            "open_positions": int(len(open_t)),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "expectancy": expectancy,
        },
        "recent_trades": recent.to_dict("records"),
    }


def get_signal_pnl_summary() -> Dict:
    """₹ P&L summary from signal journal using 1 lot per signal from NSE_LOT_SIZES."""
    df = load_signal_journal_frame()
    decided_raw = df[df["outcome"].isin(["WIN", "LOSS"])] if not df.empty else df
    # Quality filter — full entry/exit/SL/target details required for P&L counting
    decided = decided_raw.copy() if not decided_raw.empty else decided_raw
    for col in ("entry_price", "exit_price", "sl_price", "target_price"):
        if col in decided.columns:
            decided = decided[pd.to_numeric(decided[col], errors="coerce") > 0]
    if "pnl_pct" in decided.columns:
        decided = decided[pd.to_numeric(decided["pnl_pct"], errors="coerce").notna()]
    if decided.empty:
        return {
            "total_pnl_rupees": 0.0,
            "total_signals": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_win_rupees": 0.0,
            "avg_loss_rupees": 0.0,
            "profit_factor": 0.0,
        }
    wins   = decided[decided["outcome"] == "WIN"]
    losses = decided[decided["outcome"] == "LOSS"]
    total_pnl   = float(decided["pnl_rupees"].sum())
    avg_win     = float(wins["pnl_rupees"].mean())   if len(wins)   else 0.0
    avg_loss    = float(losses["pnl_rupees"].mean()) if len(losses) else 0.0
    pf          = abs(avg_win / avg_loss) if avg_loss else 0.0
    win_rate    = len(wins) / len(decided) * 100 if len(decided) else 0.0
    return {
        "total_pnl_rupees": round(total_pnl, 2),
        "total_signals":    int(len(decided)),
        "wins":             int(len(wins)),
        "losses":           int(len(losses)),
        "win_rate":         round(win_rate, 1),
        "avg_win_rupees":   round(avg_win, 2),
        "avg_loss_rupees":  round(avg_loss, 2),
        "profit_factor":    round(pf, 2),
    }


def load_signal_journal_frame() -> pd.DataFrame:
    def loader() -> pd.DataFrame:
        if not os.path.exists(JOURNAL_FILE):
            return pd.DataFrame()
        rows: List[Dict] = []
        try:
            with open(JOURNAL_FILE, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return pd.DataFrame()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)

        # ── Timestamp normalisation ────────────────────────────────────────
        # New schema uses exit_ts; old schema used ts_outcome. Support both.
        if "ts_outcome" not in df.columns:
            df["ts_outcome"] = df.get("exit_ts", pd.NaT)
        df["ts_outcome"] = pd.to_datetime(df["ts_outcome"], errors="coerce")

        # ts for signal generation (entry time)
        if "ts" in df.columns:
            df["ts_entry"] = pd.to_datetime(df["ts"], errors="coerce")
        else:
            df["ts_entry"] = pd.NaT

        # date = outcome date if resolved, else entry date
        df["date"] = df["ts_outcome"].dt.date.where(
            df["ts_outcome"].notna(), df["ts_entry"].dt.date
        )

        # ── Outcome normalisation ─────────────────────────────────────────
        # New journal: TARGET_HIT / SL_HIT / EXPIRED / null
        # Old journal: WIN / LOSS / TIMEOUT
        # Normalise to old schema so all downstream consumers work unchanged.
        _outcome_map = {
            "TARGET_HIT": "WIN",
            "SL_HIT":     "LOSS",
            "EXPIRED":    "TIMEOUT",
        }
        if "outcome" in df.columns:
            df["outcome"] = (
                df["outcome"]
                .map(lambda x: _outcome_map.get(str(x), x) if x is not None else "OPEN")
                .fillna("OPEN")
            )
        else:
            df["outcome"] = "OPEN"

        # ── Lot size ──────────────────────────────────────────────────────
        def _lot(sym: str) -> int:
            return int(_cfg.NSE_LOT_SIZES.get(str(sym).upper(), 1))

        df["lot_size"] = df["symbol"].apply(_lot) if "symbol" in df.columns else 1

        # ── ₹ P&L ─────────────────────────────────────────────────────────
        # New schema stores pnl_rupees directly; old schema used pnl_pct.
        # Prefer stored pnl_rupees → derive pnl_pct. Fall back to pnl_pct → derive pnl_rupees.
        entry_px = pd.to_numeric(df.get("entry_price", 0), errors="coerce").fillna(0)

        if "pnl_rupees" in df.columns:
            df["pnl_rupees"] = pd.to_numeric(df["pnl_rupees"], errors="coerce").fillna(0)
            # Derive pnl_pct for compatibility with existing render code
            denom = entry_px * df["lot_size"]
            df["pnl_pct"] = (df["pnl_rupees"] / denom.replace(0, float("nan")) * 100).fillna(0).round(4)
        elif "pnl_pct" in df.columns:
            df["pnl_pct"]    = pd.to_numeric(df["pnl_pct"], errors="coerce").fillna(0)
            df["pnl_rupees"] = (df["pnl_pct"] / 100.0 * entry_px * df["lot_size"]).round(2)
        else:
            df["pnl_rupees"] = 0.0
            df["pnl_pct"]    = 0.0

        return df

    return _cached("journal:frame", 15, loader).copy()


def get_learning_data() -> Dict:
    return _cached("learning:file", 15, lambda: read_json_file(LEARNING_FILE, {}))


def get_performance_data() -> Dict:
    return _cached("performance:file", 15, lambda: read_json_file(PERFORMANCE_FILE, {}))


def get_runtime_state() -> Dict:
    return _cached("state:file", 10, lambda: read_json_file(STATE_FILE, {}))


def _today_filter(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df.iloc[0:0].copy()
    return df[df["date"] == now_ist().date()].reset_index(drop=True)


def get_baseline_volumes(symbols: Iterable[str]) -> Dict[str, float]:
    symbols_tuple = tuple(dict.fromkeys(sym.upper() for sym in symbols))

    def loader() -> Dict[str, float]:
        api = get_api()

        def fetch(sym: str) -> Tuple[str, float]:
            try:
                df = api.get_historical_data(sym, from_date=25)
                if df is not None and len(df) >= 20:
                    avg = float(df["volume"].rolling(20).mean().iloc[-1])
                    return sym, avg
            except Exception:
                pass
            return sym, 0.0

        out: Dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for sym, avg in pool.map(fetch, symbols_tuple):
                out[sym] = avg
        return out

    cache_key = f"baseline:{','.join(symbols_tuple)}"
    return dict(_cached(cache_key, 1800, loader))


def get_intraday_snapshots(symbols: Iterable[str]) -> Tuple[Dict[str, Dict], Dict[str, str]]:
    symbols_tuple = tuple(dict.fromkeys(sym.upper() for sym in symbols))

    def loader() -> Tuple[Dict[str, Dict], Dict[str, str]]:
        api = get_api()
        today = now_ist().date()

        def fetch(sym: str) -> Tuple[str, Optional[Dict], Optional[str]]:
            try:
                df = api.get_intraday_data(sym, interval=5, days_back=1)
                if df is None or getattr(df, "empty", True):
                    return sym, None, "no data"
                if "date" in df.columns:
                    df = df[pd.to_datetime(df["date"]).dt.date == today].reset_index(drop=True)
                if df.empty:
                    return sym, None, "no bars for today"

                vols = df["volume"].values
                highs = df["high"].values
                lows = df["low"].values
                bars = len(vols)

                return sym, {
                    "price": float(df["close"].iloc[-1]),
                    "vol_today": int(vols.sum()),
                    "vol_1h": int(vols[-12:].sum()) if bars >= 12 else int(vols.sum()),
                    "vol_last5": int(vols[-1]),
                    "vol_prev5": int(vols[-2]) if bars >= 2 else 0,
                    "day_high": float(highs.max()),
                    "day_low": float(lows.min()),
                    "last30_high": float(highs[-6:].max()) if bars >= 6 else float(highs.max()),
                    "last30_low": float(lows[-6:].min()) if bars >= 6 else float(lows.min()),
                    "bars": bars,
                }, None
            except Exception as exc:
                return sym, None, f"{type(exc).__name__}: {exc}"

        out: Dict[str, Dict] = {}
        errs: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=10) as pool:
            for sym, data, err in pool.map(fetch, symbols_tuple):
                if data:
                    out[sym] = data
                elif err:
                    errs[sym] = err
        return out, errs

    cache_key = f"intraday:{','.join(symbols_tuple)}"
    cached = _cached(cache_key, 20, loader)
    snaps, errs = cached
    # Evict cache immediately when ALL symbols failed so next call retries
    if errs and not snaps:
        with _CACHE_LOCK:
            _CACHE.pop(cache_key, None)
    return dict(snaps), dict(errs)


def get_live_ltp(symbols: Iterable[str]) -> Dict[str, float]:
    """Fetch real-time LTP via market quote API. Short 5s cache for ATM accuracy."""
    symbols_tuple = tuple(dict.fromkeys(sym.upper() for sym in symbols))

    def loader() -> Dict[str, float]:
        try:
            api = get_api()
            result = api.get_quote(list(symbols_tuple))
            items = result.get("data", []) if isinstance(result, dict) else []
            out: Dict[str, float] = {}
            for item in (items if isinstance(items, list) else []):
                sym = (item.get("symbol") or item.get("tradingSymbol", "")).upper()
                ltp = float(item.get("last_price", 0) or item.get("lastTradedPrice", 0) or 0)
                if sym and ltp > 0:
                    out[sym] = ltp
            return out
        except Exception:
            return {}

    cache_key = f"ltp:{','.join(symbols_tuple)}"
    return dict(_cached(cache_key, 5, loader))


def get_spot_prices(symbols: Iterable[str]) -> Dict[str, float]:
    """Live LTP first (5s cache), fallback to last intraday bar close."""
    live = get_live_ltp(symbols)
    if live:
        return live
    snaps, _ = get_intraday_snapshots(symbols)
    return {sym: float(data.get("price", 0) or 0) for sym, data in snaps.items()}


def get_intraday_spike_alerts(symbols: Iterable[str], min_confidence: int = 55) -> List[Dict]:
    """Detect high-confidence intraday volume spikes across universe. 60s cache."""
    from .volume_spike_detector import VolumeSpikeDetector

    symbols_list = [sym.upper() for sym in symbols]
    detector = VolumeSpikeDetector()
    api = get_api()
    today = now_ist().date()

    def fetch_and_detect(sym: str) -> Optional[Dict]:
        try:
            df = api.get_intraday_data(sym, interval=5, days_back=1)
            if df is None or getattr(df, "empty", True):
                return None
            if "date" in df.columns:
                df = df[pd.to_datetime(df["date"]).dt.date == today].reset_index(drop=True)
            if df.empty:
                return None
            alert = detector.detect_from_df(df, sym)
            if alert and alert.confidence >= min_confidence:
                return {
                    "symbol":         alert.symbol,
                    "direction":      alert.direction,
                    "confidence":     alert.confidence,
                    "vol_ratio":      alert.vol_ratio,
                    "price":          alert.current_price,
                    "vwap":           alert.vwap,
                    "price_move_pct": alert.price_move_pct,
                    "is_breakout":    alert.is_breakout,
                    "in_prime":       alert.in_prime_time,
                    "reason":         alert.reason,
                    "timestamp":      alert.timestamp,
                }
        except Exception:
            pass
        return None

    def loader() -> List[Dict]:
        results: List[Dict] = []
        with ThreadPoolExecutor(max_workers=12) as pool:
            for result in pool.map(fetch_and_detect, symbols_list):
                if result:
                    results.append(result)
        return sorted(results, key=lambda x: x["confidence"], reverse=True)

    cache_key = f"spikes:{','.join(symbols_list)}"
    return list(_cached(cache_key, 60, loader))


def get_live_signal_tracking() -> List[Dict]:
    """Fetch current prices for active signals and compute target/SL distance tracking."""
    payload = get_signal_payload()
    if not payload:
        return []
    signals = payload.get("signals", []) or []
    if not signals:
        return []

    symbols = [s.get("symbol", "") for s in signals if s.get("symbol")]
    spot_map = get_spot_prices(symbols) if symbols else {}

    now = datetime.now()
    tracking: List[Dict] = []

    for sig in signals:
        sym    = sig.get("symbol", "")
        entry  = float(sig.get("entry_price", 0) or 0)
        sl     = float(sig.get("sl_price", 0) or 0)
        target = float(sig.get("target_price", 0) or 0)
        lng    = sig.get("direction", "long") == "long"

        if entry <= 0 or sl <= 0 or target <= 0:
            continue

        current = float(spot_map.get(sym.upper(), 0) or 0) or entry

        if lng:
            pct_from_entry = (current - entry) / entry * 100
            pct_to_target  = (target - current) / current * 100
            pct_to_sl      = (current - sl) / current * 100
        else:
            pct_from_entry = (entry - current) / entry * 100
            pct_to_target  = (current - target) / current * 100
            pct_to_sl      = (sl - current) / current * 100

        total_range = abs(target - sl)
        raw_progress = ((current - sl) / total_range * 100) if total_range > 0 else 50.0
        progress = max(0.0, min(100.0, raw_progress if lng else 100.0 - raw_progress))

        if pct_to_target <= 0.5:
            status = "AT_TARGET"
        elif pct_to_target <= 2.0:
            status = "NEAR_TARGET"
        elif pct_to_sl <= 0.5:
            status = "AT_SL"
        elif pct_to_sl <= 2.0:
            status = "NEAR_SL"
        else:
            status = "RUNNING"

        try:
            ts = datetime.fromisoformat(sig.get("ts", now.isoformat()))
            age_min = int((now - ts).total_seconds() / 60)
        except Exception:
            age_min = 0

        lot_size = _cfg.NSE_LOT_SIZES.get(sym.upper(), 1)
        unrealized = pct_from_entry / 100.0 * entry * lot_size

        tracking.append({
            "symbol":          sym,
            "direction":       sig.get("direction", "long"),
            "grade":           sig.get("confluence_grade", "?"),
            "score":           float(sig.get("confluence_score", 0) or 0),
            "entry":           round(entry, 2),
            "current":         round(current, 2),
            "sl":              round(sl, 2),
            "target":          round(target, 2),
            "pct_from_entry":  round(pct_from_entry, 2),
            "pct_to_target":   round(pct_to_target, 2),
            "pct_to_sl":       round(pct_to_sl, 2),
            "progress":        round(progress, 1),
            "status":          status,
            "lot_size":        lot_size,
            "unrealized_pnl":  round(unrealized, 0),
            "age_min":         age_min,
            "rr_ratio":        sig.get("rr_ratio", 0),
            "reason":          sig.get("reason", ""),
        })

    return sorted(tracking, key=lambda x: x["score"], reverse=True)


def get_volume_analytics(symbols: Iterable[str]) -> Dict:
    symbols_list = [sym.upper() for sym in symbols]
    baseline = get_baseline_volumes(symbols_list)
    snaps, errs = get_intraday_snapshots(symbols_list)

    rows_1h: List[Dict] = []
    rows_5m: List[Dict] = []
    for sym in symbols_list:
        snap = snaps.get(sym)
        if not snap:
            continue
        avg_daily = baseline.get(sym, 0.0)

        expected_1h = avg_daily * 60 / MARKET_TOTAL_MIN if avg_daily else 0.0
        ratio_1h = round(snap["vol_1h"] / expected_1h, 2) if expected_1h > 0 else 0.0
        rows_1h.append({
            "symbol": sym,
            "price": round(float(snap["price"]), 2),
            "vol_1h": int(snap["vol_1h"]),
            "expected_1h": int(expected_1h),
            "ratio": ratio_1h,
            "flag": "surge" if ratio_1h >= 2.0 else ("watch" if ratio_1h >= 1.5 else ""),
        })

        prev5 = int(snap["vol_prev5"])
        ratio_5m = round(snap["vol_last5"] / prev5, 2) if prev5 > 0 else 0.0
        rows_5m.append({
            "symbol": sym,
            "price": round(float(snap["price"]), 2),
            "vol_last5": int(snap["vol_last5"]),
            "vol_prev5": prev5,
            "ratio": ratio_5m,
            "flag": "surge" if ratio_5m >= 2.0 else ("watch" if ratio_5m >= 1.5 else ""),
        })

    rows_1h.sort(key=lambda item: item["ratio"], reverse=True)
    rows_5m.sort(key=lambda item: item["ratio"], reverse=True)

    return {
        "generated_at": now_ist().isoformat(),
        "baseline": baseline,
        "snapshots": snaps,
        "errors": errs,
        "rows_1h": rows_1h,
        "rows_5m": rows_5m,
    }


def get_option_chain(symbol: str) -> List[Dict]:
    """Fetch live option chain: Dhan first, NSE public API as fallback.

    Cache TTL = 90s. Premiums tick fast but signal enrichment runs per scan,
    so refreshing every 5s only buys us 429 rate limits from Dhan. 90s is
    plenty fresh for entry/SL/target calc at signal birth.
    """
    symbol = symbol.upper()

    def loader() -> List[Dict]:
        # 1. Try Dhan Data API first (requires paid subscription)
        api = get_api()
        try:
            chain = api.get_option_chain(symbol)
            # Validation: require AT LEAST ONE strike with live ce_ltp OR pe_ltp.
            # Old code checked chain[0].ce_ltp — first strike is deep ITM/OTM
            # edge with ce_ltp=0 for most stocks. That rejected valid chains.
            if chain:
                has_live = any(
                    (float(r.get("ce_ltp", 0) or 0) > 0 or
                     float(r.get("pe_ltp", 0) or 0) > 0)
                    for r in chain
                )
                if has_live:
                    log.debug(f"option chain {symbol}: Dhan returned {len(chain)} strikes")
                    return chain
                log.debug(f"option chain {symbol}: Dhan {len(chain)} strikes but no LTP yet")
        except Exception as exc:
            log.debug(f"Dhan option chain {symbol} failed: {exc}")

        # 2. Fallback to NSE public option chain API (free, no auth)
        try:
            from core.nse_option_chain import fetch_option_chain as nse_fetch
            chain = nse_fetch(symbol)
            if chain:
                log.info(f"option chain {symbol}: NSE fallback returned {len(chain)} strikes")
                return chain
        except Exception as exc:
            log.warning(f"NSE option chain {symbol} failed: {exc}")

        log.warning(f"option chain empty for {symbol} - both Dhan + NSE failed")
        return []

    # 90s TTL — drastically reduces Dhan 429 errors during 153-symbol scans
    return list(_cached(f"option-chain:{symbol}", 90, loader))


def get_option_chain_view(symbol: str) -> Dict:
    symbol = symbol.upper()
    chain = get_option_chain(symbol)

    # Prefer spot price embedded in chain response (Dhan last_price of underlying)
    chain_spot = 0.0
    expiry_used = ""
    if chain:
        chain_spot = float(chain[0].get("_spot", 0) or 0)
        expiry_used = str(chain[0].get("_expiry", "") or "")

    # Fall back to live LTP quote, then intraday bar
    if chain_spot <= 0:
        spot_map = get_spot_prices([symbol])
        chain_spot = float(spot_map.get(symbol, 0) or 0)

    spot = chain_spot
    if spot <= 0 and chain:
        strikes = sorted(row["strike"] for row in chain)
        spot = strikes[len(strikes) // 2]

    # Strip internal fields before returning to UI
    clean_chain = [
        {k: v for k, v in row.items() if not k.startswith("_")}
        for row in chain
    ]

    atm = None
    if clean_chain:
        atm = min(clean_chain, key=lambda row: abs(row["strike"] - spot))

    display_chain: List[Dict] = []
    if clean_chain:
        strikes_sorted = sorted(clean_chain, key=lambda row: row["strike"])
        if spot > 0:
            atm_idx = min(
                range(len(strikes_sorted)),
                key=lambda idx: abs(strikes_sorted[idx]["strike"] - spot),
            )
            lo = max(0, atm_idx - 5)
            hi = min(len(strikes_sorted), atm_idx + 6)
            display_chain = strikes_sorted[lo:hi]
        else:
            display_chain = strikes_sorted

    return {
        "generated_at": now_ist().isoformat(),
        "symbol": symbol,
        "spot": spot,
        "expiry": expiry_used,
        "atm": atm,
        "rows": display_chain,
    }


def get_dashboard_snapshot() -> Dict:
    signals = get_signal_payload()
    trades = get_trade_summary()
    performance = get_performance_data()
    learning = get_learning_data()
    state = get_runtime_state()
    return {
        "generated_at": now_ist().isoformat(),
        "signals": signals,
        "positions": get_live_positions(),
        "trades": trades,
        "performance": performance,
        "learning": learning,
        "state": state,
        "token_health": {
            "trading": sec.token_health().__dict__,
            "data": sec.data_token_health().__dict__,
        },
    }


def universe_subset(limit: int) -> List[str]:
    return FO_UNIVERSE[:max(limit, 1)]


def today_trades_frame() -> pd.DataFrame:
    return _today_filter(load_trades_frame())


def get_index_quotes() -> Dict[str, Dict]:
    """Fetch Nifty50, BankNifty, India VIX via yfinance intraday bars (30s cache)."""
    def loader() -> Dict[str, Dict]:
        try:
            import yfinance as yf
            _SYM = {"NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK", "INDIAVIX": "^INDIAVIX"}
            out: Dict[str, Dict] = {}
            for name, sym in _SYM.items():
                try:
                    df = yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=True)
                    if df.empty:
                        out[name] = {"ltp": 0, "chg": 0, "pct": 0}
                        continue
                    ltp  = float(df["Close"].iloc[-1])
                    prev = float(df["Close"].iloc[-2]) if len(df) >= 2 else ltp
                    chg  = ltp - prev
                    pct  = chg / prev * 100 if prev > 0 else 0.0
                    out[name] = {"ltp": round(ltp, 2), "chg": round(chg, 2), "pct": round(pct, 2)}
                except Exception:
                    out[name] = {"ltp": 0, "chg": 0, "pct": 0}
            return out
        except ImportError:
            return {}
    return dict(_cached("index:quotes", 30, loader))


def calc_chain_analytics(chain: List[Dict]) -> Dict:
    """PCR and Max Pain from full option chain data."""
    total_ce_oi = sum(int(r.get("ce_oi", 0) or 0) for r in chain)
    total_pe_oi = sum(int(r.get("pe_oi", 0) or 0) for r in chain)
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0.0

    max_pain_strike: Optional[float] = None
    if chain:
        strikes = sorted(float(r["strike"]) for r in chain)
        min_pain: Optional[float] = None
        for exp_price in strikes:
            pain = 0.0
            for row in chain:
                k = float(row["strike"])
                ce_oi = int(row.get("ce_oi", 0) or 0)
                pe_oi = int(row.get("pe_oi", 0) or 0)
                pain += max(k - exp_price, 0) * ce_oi
                pain += max(exp_price - k, 0) * pe_oi
            if min_pain is None or pain < min_pain:
                min_pain = pain
                max_pain_strike = exp_price

    return {
        "pcr": pcr,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "max_pain": max_pain_strike,
    }
