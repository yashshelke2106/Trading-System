"""
ScannerAgent — scans F&O universe every 3 min for volume surges.

For each symbol with projected daily vol >= entry_min_vol_ratio * 20d avg:
  1. Casts 'scanner' vote with approval
  2. Publishes SURGE_DETECTED event

Cache: baseline average volumes refreshed once per hour.
"""

import json
import os
import time
import logging
from typing import Dict, List
import pandas as pd

import config
from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus

log = logging.getLogger(__name__)

_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "scanner_vol_cache.json")

MARKET_TOTAL_MIN = 375   # 9:15 to 15:30

# NSE intraday volume curve (U-shaped): cumulative % of daily volume by hour
# Empirical: ~25% by 10:15, ~40% by 11:15, ~55% by 12:15, ~65% by 13:15, ~80% by 14:15, 100% by 15:30
_VOL_CURVE = {
    60:  0.25,   # 1h elapsed (10:15)
    120: 0.40,   # 2h
    180: 0.55,   # 3h
    240: 0.65,   # 4h
    300: 0.80,   # 5h
    375: 1.00,   # full day
}

def _volume_fraction(elapsed_min: float) -> float:
    """Return expected fraction of daily volume completed by elapsed_min."""
    prev_min, prev_frac = 0, 0.0
    for t_min, frac in sorted(_VOL_CURVE.items()):
        if elapsed_min <= t_min:
            # Linear interpolation between curve points
            ratio = (elapsed_min - prev_min) / max(t_min - prev_min, 1)
            return prev_frac + ratio * (frac - prev_frac)
        prev_min, prev_frac = t_min, frac
    return 1.0


class ScannerAgent(BaseAgent):
    name = "scanner"
    interval_sec = 90   # 1.5 minutes — faster signal detection

    CACHE_TTL = 3600     # 1 hour

    def __init__(self, state: SharedState, bus: EventBus, scanner, universe: List[str], feed=None):
        super().__init__(state, bus)
        self.scanner = scanner
        self.universe = universe
        self.feed = feed   # DhanMarketFeed — live volume when available
        self._avg_cache: Dict[str, float] = {}
        self._cache_ts: float = 0.0
        self._load_cache_disk()

    def _load_cache_disk(self) -> None:
        """Restore volume cache from disk on startup — survives restarts."""
        try:
            if os.path.exists(_CACHE_FILE):
                with open(_CACHE_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                age = time.time() - d.get("ts", 0)
                if age < 86400:  # accept up to 24h old
                    self._avg_cache = {k: float(v) for k, v in d.get("cache", {}).items()}
                    self._cache_ts = d.get("ts", 0)
                    log.info(f"[Scanner] loaded {len(self._avg_cache)} symbols from disk cache (age={age/60:.0f}m)")
        except Exception as e:
            log.warning(f"[Scanner] disk cache load failed: {e}")

    def _save_cache_disk(self) -> None:
        try:
            os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
            with open(_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"ts": self._cache_ts, "cache": self._avg_cache}, f)
        except Exception as e:
            log.debug(f"[Scanner] disk cache save failed: {e}")

    def run(self) -> None:
        # Refresh if TTL expired OR cache empty (don't get stuck on empty cache)
        if not self._avg_cache or time.time() - self._cache_ts > self.CACHE_TTL:
            self._refresh_cache()

        surges = self._detect_surges()
        self.state.set(volume_surges=surges, last_scan_ts=time.time())

        for sym, vol_ratio in surges.items():
            self.state.cast_vote(sym, self.name, True, direction="", confidence=min(vol_ratio / 4.0, 1.0))
            self.emit("SURGE_DETECTED", {"symbol": sym, "vol_ratio": vol_ratio})

        if surges:
            log.info(f"[Scanner] {len(surges)} surges: {list(surges.items())[:5]}")

    def _refresh_cache(self) -> None:
        log.info("[Scanner] refreshing volume cache...")
        new_cache = dict(self._avg_cache)  # preserve existing on partial failure
        fetched = 0
        failed = 0
        for sym in self.universe:
            try:
                df = self.scanner.get_daily_data(sym, 25)
                if df is not None and len(df) >= 20:
                    new_cache[sym] = float(df['volume'].rolling(20).mean().iloc[-1])
                    fetched += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        self._avg_cache = new_cache
        # Only mark TTL as fresh if we actually got new data — else retry next tick
        if fetched > 0:
            self._cache_ts = time.time()
            self._save_cache_disk()
        log.info(f"[Scanner] cache: {len(self._avg_cache)} symbols (this refresh: +{fetched}, fail={failed})")

    def _minutes_elapsed(self) -> float:
        from datetime import datetime
        now = datetime.now()
        open_dt = now.replace(hour=9, minute=15, second=0)
        return max((now - open_dt).total_seconds() / 60.0, 1.0)

    @staticmethod
    def _session_frame(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty or 'date' not in df.columns:
            return pd.DataFrame()
        try:
            dt = pd.to_datetime(df['date'], errors='coerce')
            if dt.isna().all():
                return pd.DataFrame()
            latest_day = dt.dt.date.max()
            return df.loc[dt.dt.date == latest_day].reset_index(drop=True)
        except Exception:
            return pd.DataFrame()

    @classmethod
    def _session_volume(cls, df: pd.DataFrame) -> float:
        session_df = cls._session_frame(df)
        if session_df.empty:
            return 0.0
        if len(session_df) == 1:
            return float(session_df['volume'].iloc[-1])
        return float(session_df['volume'].sum())

    @classmethod
    def _session_move_pct(cls, df: pd.DataFrame) -> float:
        session_df = cls._session_frame(df)
        if session_df.empty:
            return 0.0
        day_open = float(session_df['open'].iloc[0])
        cur = float(session_df['close'].iloc[-1])
        return abs((cur - day_open) / day_open * 100) if day_open > 0 else 0.0

    def _detect_surges(self) -> Dict[str, float]:
        min_ratio = config.VOLUME_EXIT_CONFIG["entry_min_vol_ratio"]
        elapsed = self._minutes_elapsed()

        # Session-aware: higher bar during midday chop (11:30-13:30)
        from datetime import datetime
        hour_now = datetime.now().hour
        if 11 <= hour_now <= 13:
            min_ratio *= 1.3  # 30% higher threshold during midday lull

        surges: Dict[str, float] = {}

        for sym in self.universe:
            avg = self._avg_cache.get(sym, 0)
            # If no baseline (cache cold), fall through to top-mover-only check
            no_baseline = avg <= 0
            try:
                df = self.scanner.get_intraday_data(sym, interval=5, days_back=5)
                if df is None or df.empty:
                    continue
                session_move = self._session_move_pct(df)
                if no_baseline:
                    # Top-mover-only path: catch big % movers w/o needing volume baseline
                    try:
                        if session_move == 0.0:
                            continue
                        if session_move >= 1.5:  # lowered to catch more movers
                            surges[sym] = round(session_move / 1.5, 2)  # synthetic ratio
                            log.info(f"[Scanner] NO-BASELINE MOVER {sym} {session_move:+.1f}%")
                    except Exception:
                        pass
                    continue
                # U-shaped volume curve projection (not linear)
                vol_frac = _volume_fraction(elapsed)
                live_vol = self.feed.get_volume(sym) if self.feed else None
                if live_vol is not None and live_vol > 0:
                    session_volume = float(live_vol)
                else:
                    session_volume = self._session_volume(df)
                    if session_volume <= 0:
                        continue
                projected = session_volume / max(vol_frac, 0.05)
                ratio = projected / avg

                if ratio >= min_ratio:
                    surges[sym] = round(ratio, 2)
                    continue

                # Top mover fallback: stocks moving > 2% intraday with > 1.2x vol get caught
                # even if projection-based ratio doesn't trigger surge threshold
                try:
                    if session_move >= 1.5 and ratio >= 1.0:
                        surges[sym] = round(ratio, 2)
                        log.info(f"[Scanner] TOP MOVER {sym} {session_move:+.1f}% vol={ratio:.1f}x")
                except Exception:
                    pass
            except Exception:
                pass
        return surges
