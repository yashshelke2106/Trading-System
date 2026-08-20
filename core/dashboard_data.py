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


def _journal_as_trades_frame() -> pd.DataFrame:
    """The signal journal, wearing the legacy trades.csv column names.

    logs/trades.csv is a lossy mirror: it is pinned to a legacy header, so
    _write_paper_trade's premium / option / SL / target fields are dropped by
    extrasaction="ignore" on every append. `stop_loss` and `target` are blank in
    every row, and the option columns never arrive. Anything reading it is
    reading a strictly worse copy of the journal.

    Emitting the same column names keeps every existing consumer (rag_engine,
    streamlit_app, today_trades_frame) working unchanged while the data
    underneath becomes complete. Rupee P&L is recomputed at the LIVE lot rather
    than trusted from the file, for the same reason as everywhere else: the
    stored figure was written when a missing symbol silently sized at 1 share.
    """
    rows = []
    try:
        with open(JOURNAL_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        return pd.DataFrame()

    try:
        from core.futures_leg import lot_size_for
    except Exception:                                   # pragma: no cover
        def lot_size_for(_s):
            return 1

    out = []
    for r in rows:
        if not r.get("outcome"):
            continue
        sym = str(r.get("symbol") or "").upper()
        lot = max(1, int(lot_size_for(sym)))
        ep, xp = r.get("entry_prem"), r.get("exit_prem")
        pnl = None
        if ep is not None and xp is not None and lot > 1:
            pnl = (float(xp) - float(ep)) * lot
        elif r.get("pnl_rupees") is not None and lot > 1:
            pnl = float(r["pnl_rupees"])
        oc = str(r.get("outcome"))
        out.append({
            "trade_id": r.get("signal_id"),
            "timestamp": r.get("exit_ts") or r.get("ts"),
            "symbol": sym,
            "direction": str(r.get("direction") or "").upper(),
            "entry_price": r.get("entry_price"),
            "exit_price": r.get("exit_price"),
            "stop_loss": r.get("sl_price"),
            "target": r.get("target_price"),
            "quantity": lot,
            "pnl": pnl,
            "pnl_percent": r.get("pnl_pct"),
            "spot_pnl_pct": r.get("spot_pnl_pct"),
            "status": {"TARGET_HIT": "WIN", "SL_HIT": "LOSS"}.get(oc, "EXPIRED"),
            "exit_reason": oc,
            "grade": r.get("grade"),
            "option_type": r.get("option_type"),
            "strike_price": r.get("option_strike"),
            "premium": ep,
            "rank": r.get("rank"),
            "total_score": r.get("score"),
            "market_bias": r.get("market_bias"),
            "session": r.get("session"),
            "ai_probability": r.get("ai_prob"),
        })
    return pd.DataFrame(out)


def load_trades_frame() -> pd.DataFrame:
    def loader() -> pd.DataFrame:
        df = _journal_as_trades_frame()
        if df.empty:
            # Legacy fallback only - see _journal_as_trades_frame for why the
            # CSV can never be complete.
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
    """P&L summary for the Accuracy tab.

    Emits BOTH key spellings on purpose. The tab renders a fixed tile list and
    hides any tile whose key is missing, so when this returned `win_rate` /
    `wins` / `losses` while the tab asked for `win_rate_pct` / `target_hit` /
    `sl_hit` / `closed_signals` / `avg_pnl_rupees`, five of the seven tiles
    silently vanished — the page looked "fine", just mostly empty, and the one
    number left (total P&L) had no win rate beside it to give it meaning.

    Rupees are recomputed at the LIVE lot size. The stored pnl_rupees was
    written with a lookup that fell back to 1 share for any symbol missing
    from the stale static map, which mixed 1-share and 2,250-share positions
    in one column and understated the book roughly 5x.
    """
    df = load_signal_journal_frame()
    decided_raw = df[df["outcome"].isin(["WIN", "LOSS"])] if not df.empty else df
    # Quality filter — a settled trade needs real entry and exit prices. SL and
    # target are ENTRY-PLAN fields; demanding them to report a REALISED P&L is
    # what disqualified every row on the P&L tab.
    decided = decided_raw.copy() if not decided_raw.empty else decided_raw
    for col in ("entry_price", "exit_price"):
        if col in decided.columns:
            decided = decided[pd.to_numeric(decided[col], errors="coerce") > 0]

    empty = {
        "total_pnl_rupees": 0.0, "total_signals": 0, "closed_signals": 0,
        "wins": 0, "losses": 0, "target_hit": 0, "sl_hit": 0,
        "win_rate": 0.0, "win_rate_pct": 0.0,
        "avg_pnl_rupees": 0.0, "avg_win_rupees": 0.0, "avg_loss_rupees": 0.0,
        "profit_factor": 0.0, "unresolved_lot": 0,
        "basis": "premium cash, lot-scaled",
    }
    if decided.empty:
        return empty

    try:
        from core.futures_leg import lot_size_for
    except Exception:                                    # pragma: no cover
        def lot_size_for(_sym):                          # type: ignore
            return 1

    rupees, unresolved = [], 0
    for _, row in decided.iterrows():
        lot = 1
        try:
            lot = max(1, int(lot_size_for(str(row.get("symbol", "")))))
        except Exception:
            lot = 1
        ep, xp = row.get("entry_prem"), row.get("exit_prem")
        if lot <= 1:
            unresolved += 1
            rupees.append(None)
            continue
        try:
            if pd.notna(ep) and pd.notna(xp):
                rupees.append((float(xp) - float(ep)) * lot)
            else:
                rupees.append(float(row.get("pnl_rupees") or 0.0))
        except (ValueError, TypeError):
            rupees.append(None)

    decided = decided.assign(_pnl_lot=rupees)
    priced = decided[decided["_pnl_lot"].notna()]
    if priced.empty:
        out = dict(empty)
        out.update({"total_signals": int(len(decided)),
                    "unresolved_lot": unresolved})
        return out

    wins   = priced[priced["outcome"] == "WIN"]
    losses = priced[priced["outcome"] == "LOSS"]
    total_pnl = float(priced["_pnl_lot"].sum())
    avg_win   = float(wins["_pnl_lot"].mean())   if len(wins)   else 0.0
    avg_loss  = float(losses["_pnl_lot"].mean()) if len(losses) else 0.0
    gross_p   = float(wins["_pnl_lot"].sum())    if len(wins)   else 0.0
    gross_l   = -float(losses["_pnl_lot"].sum()) if len(losses) else 0.0
    pf        = (gross_p / gross_l) if gross_l > 0 else 0.0
    win_rate  = len(wins) / len(priced) * 100 if len(priced) else 0.0

    return {
        "total_pnl_rupees": round(total_pnl, 2),
        "total_signals":    int(len(decided)),
        "closed_signals":   int(len(priced)),
        "wins":             int(len(wins)),
        "losses":           int(len(losses)),
        "target_hit":       int(len(wins)),
        "sl_hit":           int(len(losses)),
        "win_rate":         round(win_rate, 1),
        "win_rate_pct":     round(win_rate, 1),
        "avg_pnl_rupees":   round(total_pnl / len(priced), 2),
        "avg_win_rupees":   round(avg_win, 2),
        "avg_loss_rupees":  round(avg_loss, 2),
        "profit_factor":    round(pf, 2),
        "unresolved_lot":   unresolved,
        "basis":            "premium cash, lot-scaled",
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
            # LIVE scrip master first. Reading config.NSE_LOT_SIZES directly
            # sized 51 of the journal's 70 symbols at ONE SHARE and got 16 more
            # wrong by up to 6x, which is what made the rupee column
            # incomparable across rows in the first place.
            try:
                from core.futures_leg import lot_size_for
                return max(1, int(lot_size_for(sym)))
            except Exception:
                return max(1, int(_cfg.NSE_LOT_SIZES.get(str(sym).upper(), 1)))

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

        try:
            from core.futures_leg import lot_size_for
            lot_size = max(1, int(lot_size_for(sym)))
        except Exception:
            lot_size = max(1, int(_cfg.NSE_LOT_SIZES.get(sym.upper(), 1)))
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
    """Nifty50 / BankNifty / FinNifty / India VIX — {ltp, chg, pct} each.

    NSE's public feed carries last, previousClose and percentChange, which is
    everything this returns — so the normal path costs ZERO Dhan /charts/*
    calls. That matters: this is polled by both the legacy Streamlit dashboard
    and /api/indices, and /charts/* is a 1 req/sec budget shared cross-process
    with the live scanner. Routing this through get_index_panel (which fetches
    bars) put three pollers on that budget at once and drew Dhan 429s.

    Bars are only consulted if NSE is unreachable. get_index_panel is the one
    that legitimately needs them, for the intraday path it draws.

    This does NOT use daily bars: they do not carry today's forming session,
    so during market hours the old version reported the PREVIOUS session's
    move (on 2026-08-11 at 09:34 IST, Monday's close against Friday's) while
    presenting it as the current quote.
    """
    def loader() -> Dict[str, Dict]:
        try:
            from core.live_quotes import get_index_quotes as _live
            live = _live()
        except Exception:
            live = {}

        if not live:
            # NSE down — fall back to the bar-backed panel rather than report
            # nothing. Costs chart calls, which is why it is not the default.
            try:
                panel = get_index_panel()
            except Exception:
                return {}
            return {k: {"ltp": e["ltp"], "chg": e["chg"], "pct": e["pct"]}
                    for k, e in panel.items()}

        out: Dict[str, Dict] = {}
        for key, _dhan_sym, live_key, _name in INDEX_PANEL:
            q = live.get(live_key)
            ltp = float(getattr(q, "last", 0) or 0) if q is not None else 0.0
            prev = float(getattr(q, "prev_close", 0) or 0) if q is not None else 0.0
            chg = ltp - prev if (ltp > 0 and prev > 0) else 0.0
            out[key] = {
                "ltp": round(ltp, 2),
                "chg": round(chg, 2),
                "pct": round(chg / prev * 100, 2) if (chg and prev > 0) else 0.0,
            }
        return out

    return dict(_cached("index:quotes", 20, loader))


# ── Index strip: intraday path + live price ──────────────────────────────────
#
# Dashboard label → (Dhan chart symbol, live_quotes key, display name).
# MIDCAP100 is deliberately absent: NSE quotes it but Dhan has no security id
# for it, so it would render as a price with no intraday path behind it.
INDEX_PANEL: Tuple[Tuple[str, str, str, str], ...] = (
    ("NIFTY50",   "NIFTY",     "NIFTY",     "Nifty 50"),
    ("BANKNIFTY", "BANKNIFTY", "BANKNIFTY", "Bank Nifty"),
    ("FINNIFTY",  "FINNIFTY",  "FINNIFTY",  "Fin Nifty"),
    ("INDIAVIX",  "INDIAVIX",  "INDIAVIX",  "India VIX"),
)

# Dhan returns bar timestamps as epoch seconds for the IST wall clock, which
# pandas reads back as UTC — every bar lands 5h30 early. Undo that before
# splitting into sessions, or 09:15 IST is filed under the previous day.
_IST_OFFSET = timedelta(hours=5, minutes=30)
_CHART_INTERVALS = (1, 5, 15, 25, 60)


def _empty_index_entry(name: str, error: str = "") -> Dict:
    entry = {
        "name": name, "ltp": 0.0, "prev_close": 0.0, "chg": 0.0, "pct": 0.0,
        "open": 0.0, "high": 0.0, "low": 0.0,
        "bars": [], "session_date": "", "is_today": False,
        "last_bar": "", "quote_ts": "", "source": "",
    }
    if error:
        entry["error"] = error
    return entry


def get_index_panel(interval_min: int = 5) -> Dict[str, Dict]:
    """Today's intraday path plus the live price for each dashboard index.

    Two sources, each used for what it is actually best at:

      * the SHAPE of the day comes from Dhan intraday bars. Those are cached
        60s in api_dhan and paced by a 1 req/sec chart throttle shared with
        the live scanner, so this must not be polled harder than that;
      * the LIVE PRICE comes from NSE's public allIndices feed — seconds
        fresh, and it costs none of that chart budget.

    The two agree: NSE's previousClose and the previous session's last Dhan
    bar were both 24583.80 for NIFTY on 2026-08-11, which is what lets the
    previous close be read off the bars instead of a second historical call.

    Never fabricates. A source that fails leaves its fields at 0/[] and sets
    "error" — the strip then shows the gap instead of a plausible number.
    """
    interval = int(interval_min) if int(interval_min) in _CHART_INTERVALS else 5

    def loader() -> Dict[str, Dict]:
        from core.api_dhan import dhan_intraday

        # One NSE call covers every index; failure here is survivable because
        # the bars still carry a (slightly older) price.
        try:
            from core.live_quotes import get_index_quotes as _live
            live = _live()
        except Exception:
            live = {}

        out: Dict[str, Dict] = {}
        for key, dhan_sym, live_key, name in INDEX_PANEL:
            entry = _empty_index_entry(name)
            try:
                # 5 calendar days back so the previous session is still in
                # range after a long weekend; Dhan caps 1-min history at 5.
                df = dhan_intraday(dhan_sym, interval_min=interval, days_back=5)
            except Exception as exc:
                df = pd.DataFrame()
                entry["error"] = f"bars: {exc}"

            if df is not None and not df.empty and "date" in df.columns:
                ist = pd.to_datetime(df["date"]) + _IST_OFFSET
                df = df.assign(_ist=ist, _day=ist.dt.date).sort_values("_ist")
                days = list(dict.fromkeys(df["_day"].tolist()))
                session = df[df["_day"] == days[-1]]

                entry["session_date"] = str(days[-1])
                entry["is_today"] = days[-1] == datetime.now().date()
                entry["bars"] = [
                    {"t": t.strftime("%H:%M"), "c": round(float(c), 2)}
                    for t, c in zip(session["_ist"], session["close"])
                ]
                entry["last_bar"] = session["_ist"].iloc[-1].strftime("%H:%M")
                entry["ltp"] = round(float(session["close"].iloc[-1]), 2)
                entry["open"] = round(float(session["open"].iloc[0]), 2)
                entry["high"] = round(float(session["high"].max()), 2)
                entry["low"] = round(float(session["low"].min()), 2)
                entry["source"] = "dhan_bars"
                if len(days) >= 2:
                    prev = df[df["_day"] == days[-2]]
                    entry["prev_close"] = round(float(prev["close"].iloc[-1]), 2)

            # Live price wins when NSE answered — it is seconds fresh where the
            # last bar can be a full interval old.
            q = live.get(live_key)
            if q is not None and getattr(q, "last", 0):
                entry["ltp"] = round(float(q.last), 2)
                entry["quote_ts"] = q.ts or ""
                entry["source"] = q.source
                if getattr(q, "prev_close", None):
                    entry["prev_close"] = round(float(q.prev_close), 2)

            prev_close, ltp = entry["prev_close"], entry["ltp"]
            if prev_close > 0 and ltp > 0:
                entry["chg"] = round(ltp - prev_close, 2)
                entry["pct"] = round((ltp - prev_close) / prev_close * 100, 2)

            if not entry["bars"] and not entry.get("error"):
                entry["error"] = "no intraday bars"
            out[key] = entry
        return out

    # 15s: short enough that a new NSE price surfaces quickly, long enough that
    # several open browser tabs collapse into one upstream fetch.
    return dict(_cached(f"index:panel:{interval}", 15, loader))


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
