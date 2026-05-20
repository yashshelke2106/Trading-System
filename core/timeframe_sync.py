"""
Multi-timeframe confluence engine — 1D bias + 15m setup + 5m entry.

Role per timeframe:
  1D  — trend direction; daily breakout/compression (NR7, Inside Day, Outside Day);
         multi-bar candlestick patterns (next-candle bias: morning star, evening star,
         engulfing, three soldiers/crows, hammer, shooting star, marubozu, doji);
         RVOL and volume dry-up/surge classification
  15m — entry zone identification; structure-based SL (swing extreme of last 5 bars,
         capped at 2×ATR_15m); T1=1R partial profit and T2=2R main target
  5m  — precise entry trigger (candle-close confirmation); tight candle SL
         (entry bar low/high + 0.05% buffer) for aggressive sizing

Scoring (max ~155):
  1D confirms:       +35 | 1D neutral: +10 | 1D opposes: suppress (return None)
  1D candle bias:    +10 (strong aligned) / +5 (moderate) / −5 (opposing candle)
  1D volume:         +10 (surge_breakout) / +8 (compression→surge) / +5 (surge)
  1D daily pattern:  +8 (NR7 or Inside Day)
  15m confirms:      +25 | 15m neutral: +8 | 15m opposes: 0
  Volume cascade:    +20 (15m≥1.2x AND 5m≥1.5x) | 5m-only: +10
  15m structure:     +15
  EMA stack 3-TF:    +15 | 5m+15m: +8
  Fresh 5m entry:    +10 (≤2 bars since breakout)
  VWAP aligned:      +5

Grade thresholds: A≥80  B≥55  C≥30  D suppressed
SL:      15m swing structure, capped at 2×ATR_15m
sl_tight: 5m entry-candle extreme (aggressive entry, tighter RR)
target_1: entry ± 1×risk  (T1 — scale out 50% here, move SL to breakeven)
target_price / T2: entry ± 4×risk  (T2 — runner target, default 4.0R)
"""
import time, threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Tuple
import pandas as pd
from core.signal_engine import SignalEngine, Signal, MarketStructure

_GOOD = {"trend_up", "trend_down", "breakout", "nr7_breakout",
         "inside_bar_breakout", "vwap_breakout", "ema_crossover", "ema_pullback"}
_SEM  = threading.Semaphore(4)  # cap concurrent yfinance calls; ~3 req/s per worker

# Calibration + selective-fire now live in core.signal_finalize (applied
# post-OI-enrichment in scan_only_v2). scan_universe stays candidate-only.


@dataclass
class SyncedSignal:
    symbol: str
    direction: str
    confluence_grade: str
    confluence_score: int
    entry_price: float
    sl_price: float          # structure SL (15m swing, reliable)
    sl_tight: float          # candle SL (5m entry bar, aggressive)
    target_1: float          # T1: 1R — scale out 50% here
    target_price: float      # T2: 2R — main target
    signal_5m: Optional[Signal]
    signal_15m: Optional[Signal]
    signal_1d: Optional[Signal]
    volume_cascade: bool
    ema_stack_aligned: bool
    vwap_synced: bool
    patterns_combined: List[str]
    reason: str
    ts: str = field(default_factory=lambda: datetime.now().isoformat())
    calibrated_prob: float = 0.0   # P(win) from core.calibrator (0 = not scored)

    def to_dict(self):
        def _s(s):
            if s is None:
                return None
            return {"strength": s.strength, "volume_ratio": s.volume_ratio,
                    "structure": s.structure.value, "patterns": s.patterns,
                    "rsi": s.rsi, "ema9": s.ema9, "ema21": s.ema21, "vwap": s.vwap}
        sl_dist  = abs(self.entry_price - self.sl_price)
        tgt_dist = abs(self.target_price - self.entry_price)
        rr = round(tgt_dist / sl_dist, 2) if sl_dist > 0 else 0.0
        per_tf: Dict = {}
        if self.signal_5m:
            per_tf["5m"]  = {"present": True,
                             "vol_ratio": self.signal_5m.volume_ratio,
                             "direction": self.signal_5m.direction,
                             "strength": self.signal_5m.strength}
        if self.signal_15m:
            per_tf["15m"] = {"present": True,
                             "vol_ratio": self.signal_15m.volume_ratio,
                             "direction": self.signal_15m.direction,
                             "strength": self.signal_15m.strength}
        if self.signal_1d:
            per_tf["1d"]  = {"present": True,
                             "vol_ratio": self.signal_1d.volume_ratio,
                             "direction": self.signal_1d.direction,
                             "strength": self.signal_1d.strength}
        s5 = self.signal_5m
        return {
            "symbol":            self.symbol,
            "direction":         self.direction,
            "confluence_grade":  self.confluence_grade,
            "confluence_score":  self.confluence_score,
            "entry_price":       self.entry_price,
            "sl_price":          self.sl_price,
            "sl_tight":          self.sl_tight,
            "target_1":          self.target_1,
            "target_price":      self.target_price,
            "rr_ratio":          rr,
            "calibrated_prob":   round(self.calibrated_prob, 4),
            "volume_cascade":    self.volume_cascade,
            "ema_stack_aligned": self.ema_stack_aligned,
            "vwap_synced":       self.vwap_synced,
            "patterns_combined": self.patterns_combined,
            "patterns":          self.patterns_combined,
            "per_tf":            per_tf,
            "reason":            self.reason,
            "ts":                self.ts,
            "rsi":               round(s5.rsi, 1) if s5 else 0.0,
            "volume_ratio":      round(s5.volume_ratio, 2) if s5 else 0.0,
            "vote_margin":       abs(s5.long_votes - s5.short_votes) if s5 else 0,
            "signal_5m":         _s(self.signal_5m),
            "signal_15m":        _s(self.signal_15m),
            "signal_1d":         _s(self.signal_1d),
        }


class TimeframeSyncEngine:
    def __init__(self):
        self.engine = SignalEngine()

    # ── Data fetchers ────────────────────────────────────────────────────

    def _fetch(self, api, sym, interval: int, days_back: int = 5) -> pd.DataFrame:
        """Fetch intraday bars (5m or 15m) with rate-limit throttle."""
        with _SEM:
            time.sleep(0.35)
            return api.get_intraday_data(sym, interval=interval, days_back=days_back)

    def _fetch_daily(self, api, sym, days_back: int = 60) -> pd.DataFrame:
        """Fetch daily OHLCV bars — 60 trading days gives enough indicator warmup."""
        with _SEM:
            time.sleep(0.35)
            return api.get_historical_data(sym, from_date=days_back)

    @staticmethod
    def _resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
        """Daily → weekly OHLCV (NSE week ends Friday). For swing trend TF."""
        if df is None or df.empty or "date" not in df.columns:
            return pd.DataFrame()
        try:
            d = df.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            agg = {"open": "first", "high": "max", "low": "min",
                   "close": "last", "volume": "sum"}
            use = {c: a for c, a in agg.items() if c in d.columns}
            w = d.resample("W-FRI").agg(use).dropna(how="any").reset_index()
            return w
        except Exception:
            return pd.DataFrame()

    def _fetch_timeframes(self, api, sym):
        """Return (df_exec, df_setup, df_hbias, df_trend) per the active
        trade mode. Intraday: 5m/15m/1H/1D. Swing: 1D/1D/1D/1W. Distinct
        sources fetched once and reused so swing doesn't triple-hit the
        daily endpoint. Slot variable names are kept downstream."""
        from core.trade_mode import get_mode
        m = get_mode()
        cache: Dict = {}

        def _one(spec):
            key = tuple(spec)
            if key in cache:
                return cache[key]
            kind = spec[0]
            if kind == "intraday":
                df = self._fetch(api, sym, spec[1],
                                 days_back=m.intraday_days_back)
                df = (self._recent(df, m.recent_trim_days)
                      if m.recent_trim_days else df)
            elif kind == "daily":
                df = self._fetch_daily(api, sym,
                                       days_back=m.daily_days_back)
            elif kind == "weekly":
                df = self._resample_weekly(
                    self._fetch_daily(api, sym, days_back=m.daily_days_back))
            else:
                df = pd.DataFrame()
            cache[key] = df
            return df

        return (_one(m.exec_tf), _one(m.setup_tf),
                _one(m.hbias_tf), _one(m.trend_tf))

    @staticmethod
    def _today(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        today = datetime.now().date()
        try:    mask = df["date"].dt.date == today
        except: mask = pd.to_datetime(df["date"]).dt.date == today
        return df[mask].reset_index(drop=True)

    @staticmethod
    def _recent(df: pd.DataFrame, keep_days: int = 10) -> pd.DataFrame:
        """Keep last N unique trading days — avoids indicator starvation on today-only data."""
        if df is None or df.empty:
            return pd.DataFrame()
        try:
            dates = pd.to_datetime(df["date"]).dt.date
        except Exception:
            return df
        unique_days = sorted(dates.unique())[-keep_days:]
        return df[dates.isin(unique_days)].reset_index(drop=True)

    # ── Daily-specific analysis ───────────────────────────────────────────

    @staticmethod
    def _analyze_daily(df1d: pd.DataFrame, direction: str) -> Tuple[int, List[str]]:
        """
        Analyze last 3 closed daily bars for candle bias, volume, and compression.

        Returns (bonus_score, reason_labels).
        bonus_score: +ve when daily confirms direction, −ve when opposing.
        """
        if len(df1d) < 7:
            return 0, []

        lng = direction == "long"
        bonus = 0
        labels: List[str] = []

        # Use last 4 bars; index -1 may be today's partial bar.
        # Candle pattern on the last 3 CLOSED bars (−4, −3, −2).
        c3 = df1d.iloc[-4]
        c2 = df1d.iloc[-3]
        c1 = df1d.iloc[-2]   # last confirmed closed daily bar

        def _rng(c):
            return float(c["high"]) - float(c["low"])

        def _body(c):
            return abs(float(c["close"]) - float(c["open"]))

        r1, r2, r3 = _rng(c1), _rng(c2), _rng(c3)
        b1, b2, b3 = _body(c1), _body(c2), _body(c3)

        candle_bull = 0
        candle_bear = 0

        # ── 3-bar patterns ───────────────────────────────────────────────
        # Three White Soldiers: 3 consecutive bullish, each closing higher
        if (c3["close"] > c3["open"] and c2["close"] > c2["open"] and c1["close"] > c1["open"]
                and float(c2["close"]) > float(c3["close"])
                and float(c1["close"]) > float(c2["close"])):
            candle_bull += 3
            labels.append("1D:3_soldiers")

        # Three Black Crows: 3 consecutive bearish, each closing lower
        if (c3["close"] < c3["open"] and c2["close"] < c2["open"] and c1["close"] < c1["open"]
                and float(c2["close"]) < float(c3["close"])
                and float(c1["close"]) < float(c2["close"])):
            candle_bear += 3
            labels.append("1D:3_crows")

        # Morning Star: big bear → small star → big bull closing above c3 midpoint
        c3_big_bear = c3["close"] < c3["open"] and r3 > 0 and b3 / r3 > 0.5
        c2_star     = r2 > 0 and b2 / r2 < 0.30
        c1_big_bull = (c1["close"] > c1["open"]
                       and float(c1["close"]) > (float(c3["open"]) + float(c3["close"])) / 2)
        if c3_big_bear and c2_star and c1_big_bull:
            candle_bull += 3
            labels.append("1D:morning_star")

        # Evening Star: big bull → small star → big bear closing below c3 midpoint
        c3_big_bull = c3["close"] > c3["open"] and r3 > 0 and b3 / r3 > 0.5
        c1_big_bear = (c1["close"] < c1["open"]
                       and float(c1["close"]) < (float(c3["open"]) + float(c3["close"])) / 2)
        if c3_big_bull and c2_star and c1_big_bear:
            candle_bear += 3
            labels.append("1D:evening_star")

        # ── 2-bar patterns (c2 → c1) ─────────────────────────────────────
        # Bullish engulfing: c2 bear, c1 bull covering c2 body
        if (c2["close"] < c2["open"] and c1["close"] > c1["open"]
                and float(c1["open"]) <= float(c2["close"])
                and float(c1["close"]) >= float(c2["open"])
                and b1 > b2):
            candle_bull += 2
            labels.append("1D:bull_engulf")

        # Bearish engulfing
        if (c2["close"] > c2["open"] and c1["close"] < c1["open"]
                and float(c1["open"]) >= float(c2["close"])
                and float(c1["close"]) <= float(c2["open"])
                and b1 > b2):
            candle_bear += 2
            labels.append("1D:bear_engulf")

        # ── 1-bar patterns (c1) ──────────────────────────────────────────
        if r1 > 0:
            lower_wick = min(float(c1["open"]), float(c1["close"])) - float(c1["low"])
            upper_wick = float(c1["high"]) - max(float(c1["open"]), float(c1["close"]))

            # Hammer: lower wick > 55% of range, body < 35%, lower wick > 2× upper wick
            if lower_wick / r1 > 0.55 and b1 / r1 < 0.35 and lower_wick > 2 * upper_wick:
                candle_bull += 2
                labels.append("1D:hammer")

            # Shooting star: upper wick > 55%, body < 35%, upper wick > 2× lower wick
            if upper_wick / r1 > 0.55 and b1 / r1 < 0.35 and upper_wick > 2 * lower_wick:
                candle_bear += 2
                labels.append("1D:shooting_star")

            # Marubozu: body > 80% of range — strong continuation
            if b1 / r1 > 0.80:
                if c1["close"] > c1["open"]:
                    candle_bull += 1
                    labels.append("1D:bull_marubozu")
                else:
                    candle_bear += 1
                    labels.append("1D:bear_marubozu")

            # Doji: indecision — zero out candle signals
            if b1 / r1 < 0.08:
                candle_bull = 0
                candle_bear = 0
                labels.append("1D:doji")

        # Score candle bias relative to trade direction
        if lng:
            if candle_bull >= 3:   bonus += 10; labels.append("1D:strong_bull")
            elif candle_bull >= 2: bonus += 5;  labels.append("1D:bull_bias")
            elif candle_bear >= 2: bonus -= 5;  labels.append("1D:bear_vs_long")
        else:
            if candle_bear >= 3:   bonus += 10; labels.append("1D:strong_bear")
            elif candle_bear >= 2: bonus += 5;  labels.append("1D:bear_bias")
            elif candle_bull >= 2: bonus -= 5;  labels.append("1D:bull_vs_short")

        # ── Volume analysis (use most recent bar including today partial) ──
        curr_vol_row = df1d.iloc[-1]
        if len(df1d) >= 21:
            avg_20 = float(df1d["volume"].rolling(20).mean().iloc[-1])
            curr_vol = float(curr_vol_row["volume"])
            rvol = curr_vol / avg_20 if avg_20 > 0 else 1.0

            # Compression: last 3 complete bars all below 70% of 20d avg
            recent_vols = df1d["volume"].iloc[-4:-1]
            compression = bool((recent_vols < avg_20 * 0.70).all())

            if rvol >= 2.0 and not compression:
                # Strong surge — new demand/supply entering
                bonus += 10
                labels.append(f"1D:vol_surge({rvol:.1f}x)")
            elif compression and rvol >= 1.3:
                # Compressed then exploded — coiled spring triggering
                bonus += 8
                labels.append(f"1D:coiled_spring({rvol:.1f}x)")
            elif rvol >= 1.5:
                bonus += 5
                labels.append(f"1D:vol_above_avg({rvol:.1f}x)")
            elif compression:
                bonus += 3
                labels.append("1D:vol_compressed")

        # ── Range compression patterns ───────────────────────────────────
        curr = df1d.iloc[-1]
        prev = df1d.iloc[-2]

        # Inside Day: today's range entirely within yesterday's range
        if (float(curr["high"]) <= float(prev["high"])
                and float(curr["low"]) >= float(prev["low"])):
            bonus += 8
            labels.append("1D:inside_day")

        # NR7: today's range is narrowest of last 7 bars and below mean
        if len(df1d) >= 7:
            ranges = (df1d["high"] - df1d["low"]).tail(7)
            curr_range = float(curr["high"]) - float(curr["low"])
            if (curr_range <= float(ranges.min()) * 1.0001
                    and curr_range < float(ranges.mean()) * 0.75):
                bonus += 8
                labels.append("1D:NR7")

        return bonus, labels

    # ── Level calculation ─────────────────────────────────────────────────

    @staticmethod
    def _calc_levels(df15: pd.DataFrame, df5: pd.DataFrame,
                     direction: str, entry: float,
                     atr_15m: float) -> Dict:
        """
        Structure-based SL and 1R/2R targets.

        SL:      15m swing extreme of last 5 bars, capped at 2×ATR_15m
        sl_tight: 5m entry-candle extreme (for aggressive entry sizing)
        t1:      entry ± 1×risk  (scale out 50% here, move SL to breakeven)
        t2:      entry ± 2×risk  (let remaining run to T2)
        """
        lng = direction == "long"

        # Structure SL from 15m swing extreme (last 5 bars)
        if not df15.empty and len(df15) >= 1:
            n = min(5, len(df15))
            recent = df15.tail(n)
            swing_sl = float(recent["low"].min()) if lng else float(recent["high"].max())
            # Cap: never wider than 2×ATR_15m (avoids mega-wide SL on large swings)
            cap_sl = entry - atr_15m * 2.0 if lng else entry + atr_15m * 2.0
            sl = max(swing_sl, cap_sl) if lng else min(swing_sl, cap_sl)
        else:
            sl = entry - atr_15m * 1.5 if lng else entry + atr_15m * 1.5

        # Guard: SL must be on the correct side of entry
        if lng and sl >= entry:
            sl = entry - atr_15m * 1.0
        elif not lng and sl <= entry:
            sl = entry + atr_15m * 1.0

        # Tight SL: 5m entry-candle extreme + small buffer
        sl_tight = sl
        if len(df5) >= 1:
            last5 = df5.iloc[-1]
            if lng:
                cl = float(last5["low"]) * (1 - 0.0005)
                if entry > cl > 0:
                    sl_tight = max(cl, sl)   # take whichever is higher (tighter for long)
            else:
                ch = float(last5["high"]) * (1 + 0.0005)
                if ch > entry:
                    sl_tight = min(ch, sl)   # take whichever is lower (tighter for short)

        risk = abs(entry - sl)
        if risk <= 0:
            risk = atr_15m * 1.0

        try:
            from core.adaptive_learner import get_learned_config as _glc_rr
            rr_ratio = float(_glc_rr().get("SIGNAL_CONFIG", {}).get("rr_ratio", 4.0))
        except Exception:
            rr_ratio = 4.0

        t1 = (entry + risk)            if lng else (entry - risk)             # 1:1 partial
        t2 = (entry + risk * rr_ratio) if lng else (entry - risk * rr_ratio)  # adaptive main target

        return {
            "sl":       round(sl, 2),
            "sl_tight": round(sl_tight, 2),
            "t1":       round(t1, 2),
            "t2":       round(t2, 2),
            "risk":     round(risk, 2),
        }

    # ── Daily structural breakout detection ─────────────────────────────

    @staticmethod
    def _detect_daily_breakout(df1d: pd.DataFrame, direction: str) -> Tuple[bool, int, List[str]]:
        """Detect REAL structural breakout on daily chart — not just candle patterns.

        Checks:
          1. 20-day high/low breakout (price clearing prior resistance/support)
          2. Range contraction → expansion (NR4-NR7 → big bar)
          3. Prior resistance/support level clear with volume
          4. EMA 9/21 stack alignment on daily

        Returns (is_breakout, bonus_score, labels).
        """
        if df1d is None or len(df1d) < 21:
            return False, 0, []

        lng = direction == "long"
        bonus = 0
        labels = []
        is_breakout = False

        curr = df1d.iloc[-1]   # today (may be partial)
        prev = df1d.iloc[-2]   # last closed bar
        curr_close = float(curr["close"])
        curr_high = float(curr["high"])
        curr_low = float(curr["low"])
        curr_vol = float(curr["volume"]) if "volume" in curr.index else 0

        # 20-day lookback (excluding today)
        lookback = df1d.iloc[-21:-1]
        high_20d = float(lookback["high"].max())
        low_20d = float(lookback["low"].min())
        avg_vol_20d = float(lookback["volume"].mean()) if "volume" in lookback.columns else 0

        # Volume confirmation: breakout bar should have above-avg volume
        vol_confirm = (avg_vol_20d > 0 and curr_vol > avg_vol_20d * 1.2)

        # ── 1. 20-day high/low breakout ──────────────────────────────
        if lng and curr_close > high_20d:
            is_breakout = True
            bonus += 20
            labels.append(f"1D:20d_high_break({curr_close:.0f}>{high_20d:.0f})")
            if vol_confirm:
                bonus += 10
                labels.append("1D:vol_confirmed_break")
        elif not lng and curr_close < low_20d:
            is_breakout = True
            bonus += 20
            labels.append(f"1D:20d_low_break({curr_close:.0f}<{low_20d:.0f})")
            if vol_confirm:
                bonus += 10
                labels.append("1D:vol_confirmed_break")

        # ── 2. Range contraction → expansion ─────────────────────────
        # Last 5 bars narrowing, then today's range > avg
        if len(df1d) >= 7:
            ranges = (df1d["high"] - df1d["low"]).astype(float)
            last5_ranges = ranges.iloc[-6:-1]
            curr_range = float(curr_high - curr_low)
            avg_range_5 = float(last5_ranges.mean())
            is_contracting = all(float(last5_ranges.iloc[i]) >= float(last5_ranges.iloc[i+1])
                                 for i in range(min(3, len(last5_ranges)-1)))
            if is_contracting and avg_range_5 > 0 and curr_range > avg_range_5 * 1.5:
                is_breakout = True
                bonus += 15
                labels.append(f"1D:range_expand({curr_range/avg_range_5:.1f}x)")

        # ── 3. Horizontal resistance/support clear ───────────────────
        # Check if price closed above highest close of last 10 bars (resistance)
        if len(df1d) >= 11:
            closes_10 = df1d["close"].astype(float).iloc[-11:-1]
            max_close_10 = float(closes_10.max())
            min_close_10 = float(closes_10.min())
            if lng and curr_close > max_close_10 * 1.002:  # 0.2% above prior max close
                bonus += 10
                labels.append("1D:resistance_cleared")
                is_breakout = True
            elif not lng and curr_close < min_close_10 * 0.998:
                bonus += 10
                labels.append("1D:support_broken")
                is_breakout = True

        # ── 4. Daily EMA stack ───────────────────────────────────────
        if len(df1d) >= 21:
            ema9 = float(df1d["close"].ewm(span=9).mean().iloc[-1])
            ema21 = float(df1d["close"].ewm(span=21).mean().iloc[-1])
            if lng and ema9 > ema21 and curr_close > ema9:
                bonus += 5
                labels.append("1D:ema_bull_stack")
            elif not lng and ema9 < ema21 and curr_close < ema9:
                bonus += 5
                labels.append("1D:ema_bear_stack")
            elif lng and ema9 < ema21:
                bonus -= 10
                labels.append("1D:ema_against_long")
            elif not lng and ema9 > ema21:
                bonus -= 10
                labels.append("1D:ema_against_short")

        return is_breakout, bonus, labels

    # ── Hourly (1H) analysis ─────────────────────────────────────────────

    @staticmethod
    def _analyze_hourly(df1h: pd.DataFrame, direction: str) -> Tuple[int, List[str]]:
        """1H timeframe: bridge between daily breakout and 5m entry.

        Confirms intraday momentum direction. If 1H trend opposes 5m entry,
        signal is likely counter-trend scalp → lower score.
        """
        if df1h is None or len(df1h) < 10:
            return 0, []

        lng = direction == "long"
        bonus = 0
        labels = []

        # 1H EMA trend
        ema9 = df1h["close"].ewm(span=9).mean()
        ema21 = df1h["close"].ewm(span=21).mean()
        curr_close = float(df1h["close"].iloc[-1])
        ema9_val = float(ema9.iloc[-1])
        ema21_val = float(ema21.iloc[-1])

        ema_aligned = (lng and ema9_val > ema21_val) or (not lng and ema9_val < ema21_val)
        price_above_ema = (lng and curr_close > ema9_val) or (not lng and curr_close < ema9_val)

        if ema_aligned and price_above_ema:
            bonus += 12
            labels.append("1H:trend_aligned")
        elif ema_aligned:
            bonus += 6
            labels.append("1H:ema_ok")
        elif not ema_aligned:
            bonus -= 8
            labels.append("1H:trend_opposing")

        # 1H volume confirmation: last bar volume vs 10-bar avg
        if "volume" in df1h.columns and len(df1h) >= 10:
            avg_vol = float(df1h["volume"].iloc[-10:].mean())
            curr_vol = float(df1h["volume"].iloc[-1])
            if avg_vol > 0:
                vol_ratio = curr_vol / avg_vol
                if vol_ratio >= 1.5:
                    bonus += 8
                    labels.append(f"1H:vol_surge({vol_ratio:.1f}x)")
                elif vol_ratio < 0.5:
                    bonus -= 5
                    labels.append("1H:vol_dry")

        # 1H higher-high / lower-low structure (last 3 bars)
        if len(df1h) >= 3:
            h1, h2, h3 = (float(df1h["high"].iloc[-1]), float(df1h["high"].iloc[-2]),
                           float(df1h["high"].iloc[-3]))
            l1, l2, l3 = (float(df1h["low"].iloc[-1]), float(df1h["low"].iloc[-2]),
                           float(df1h["low"].iloc[-3]))
            if lng and h1 > h2 > h3 and l1 > l2:
                bonus += 8
                labels.append("1H:HH_HL")
            elif not lng and l1 < l2 < l3 and h1 < h2:
                bonus += 8
                labels.append("1H:LL_LH")

        return bonus, labels

    # ── Main analysis pipeline ────────────────────────────────────────────

    @staticmethod
    def _in_chop_window() -> bool:
        """Last 30 min of NSE = squaring zone. Skip.
        Opening 5 min (9:15-9:20) too noisy, but 9:20+ OK — opening breakouts strongest.
        Swing trades the daily close → no intraday chop window applies."""
        try:
            from core.trade_mode import get_mode as _gm
            if _gm().bypass_intraday_time_gates:
                return False
        except Exception:
            pass
        n = datetime.now()
        cur = n.hour * 60 + n.minute
        return (cur < 9*60 + 20) or (cur >= 15*60)

    def _is_top_mover(self, df5: pd.DataFrame, df1d: pd.DataFrame) -> tuple:
        """Returns (is_mover, classification). Cheap shortcut to top_mover_mode."""
        try:
            from core.top_mover_mode import detect_top_mover
            is_m, cls, pct, vol = detect_top_mover(df5, df1d)
            return is_m, cls
        except Exception:
            return False, 'normal'

    def analyze(self, api, sym):
        import logging as _log
        _logger = _log.getLogger(__name__)
        try:
            in_chop = self._in_chop_window()
            # Timeframe quartet per active trade mode (swing = daily/weekly).
            # Slot names kept; downstream logic unchanged.
            df5, df15, df1h, df1d = self._fetch_timeframes(api, sym)

            if len(df5) < 10:
                return None

            # Top mover override for chop window — opening gaps are the alpha
            if in_chop:
                try:
                    from core.top_mover_mode import detect_top_mover, should_bypass_chop_window
                    is_m, cls, _, _ = detect_top_mover(df5, df1d)
                    if not (is_m and should_bypass_chop_window(cls)):
                        return None
                except Exception:
                    return None

            s5  = self.engine.generate_signal(sym, df5)
            s15 = self.engine.generate_signal(sym, df15) if len(df15) >= 25 else None
            s1d = self.engine.generate_signal(sym, df1d) if len(df1d) >= 25 else None

            if s5 is None:
                return None
            s5.timeframe  = "5m"
            if s15: s15.timeframe = "15m"
            if s1d: s1d.timeframe = "1d"

            result = self._grade(sym, s5, s15, s1d, df5, df15, df1d, df1h)
            # Diagnostic: log near-miss signals (had 5m signal but grade killed it)
            if result is None and s5 is not None:
                _logger.debug(
                    f"[TFSync] NEAR-MISS {sym} dir={s5.direction} "
                    f"str={s5.strength:.0f} rsi={s5.rsi:.0f} "
                    f"votes={s5.long_votes}L/{s5.short_votes}S "
                    f"15m={'YES' if s15 else 'NO'} "
                    f"1d={'YES' if s1d else 'NO'} "
                    f"1h={len(df1h) if df1h is not None else 0}bars"
                )
            return result
        except Exception as e:
            _logger.debug("analyze %s failed: %s", sym, e)
            return None

    def _grade(self, sym: str,
               s5: Signal, s15: Optional[Signal], s1d: Optional[Signal],
               df5: pd.DataFrame, df15: pd.DataFrame, df1d: pd.DataFrame,
               df1h: pd.DataFrame = None):
        score = 0
        reasons: List[str] = []
        lng = s5.direction == "long"
        d5  = s5.direction

        # Load adaptive scoring weights (falls back to defaults if learner unavailable)
        try:
            from core.adaptive_learner import get_learned_config
            _sw = get_learned_config().get("SCORING_WEIGHTS", {})
        except Exception:
            _sw = {}
        W_1D_CONFIRM    = int(_sw.get("score_1d_confirm",    35))
        W_15M_CONFIRM   = int(_sw.get("score_15m_confirm",   25))
        W_VOL_CASCADE   = int(_sw.get("score_vol_cascade",   20))
        W_EMA_STACK     = int(_sw.get("score_ema_stack",     15))
        W_FRESH_ENTRY   = int(_sw.get("score_fresh_entry",   10))
        W_VWAP          = int(_sw.get("score_vwap_align",     5))

        # ── Symmetric extreme-RSI guard ─────────────────────────────────────
        # Block shorts only at EXTREME oversold (< rsi_short_floor) — bounce risk.
        # Block longs at EXTREME overbought (> 100 - rsi_short_floor) — top risk.
        # rsi_short_floor default is 30 → mirror ceiling is 70.
        try:
            _rsi_sf = float(_sw.get("rsi_short_floor")
                            or get_learned_config().get("SIGNAL_CONFIG", {}).get("rsi_short_floor", 30))
        except Exception:
            _rsi_sf = 30.0
        _rsi_lc = 100.0 - _rsi_sf  # symmetric long ceiling
        if not lng and s5.rsi < _rsi_sf:
            return None
        if lng and s5.rsi > _rsi_lc:
            return None

        # ── Daily structural breakout (NEW — real breakout, not just candle) ──
        daily_breakout = False
        if df1d is not None and len(df1d) >= 21:
            daily_breakout, brk_bonus, brk_labels = self._detect_daily_breakout(df1d, d5)
            score += brk_bonus
            reasons.extend(brk_labels)

        # ── 1H timeframe analysis (NEW — bridge between 1D and 5m) ────────
        if df1h is not None and len(df1h) >= 10:
            h_bonus, h_labels = self._analyze_hourly(df1h, d5)
            score += h_bonus
            reasons.extend(h_labels)

        # ── 1D: primary direction filter ─────────────────────────────────
        if s1d:
            if s1d.direction == d5:
                score += W_1D_CONFIRM
                reasons.append("1D confirms")
            else:
                # Daily opposes BUT daily has structural breakout in our dir → override
                if daily_breakout:
                    score += 10
                    reasons.append("1D:signal_opposes_but_struct_break")
                else:
                    return None   # daily chart signals opposite, no breakout — suppress
        else:
            # No 1D signal — require EITHER daily breakout OR 1H confirmation
            if daily_breakout:
                score += W_1D_CONFIRM - 5  # nearly as good as 1D signal confirm
                reasons.append("1D:no_signal_but_struct_break")
            elif df1h is not None and len(df1h) >= 10:
                # Check if 1H at least aligns
                h_ema9 = float(df1h["close"].ewm(span=9).mean().iloc[-1])
                h_ema21 = float(df1h["close"].ewm(span=21).mean().iloc[-1])
                h_aligned = (lng and h_ema9 > h_ema21) or (not lng and h_ema9 < h_ema21)
                if h_aligned:
                    score += 10
                    reasons.append("1D:neutral+1H_aligned")
                else:
                    score += 3
                    reasons.append("1D:neutral+1H:opposing")
            else:
                score += 5
                reasons.append("1D neutral")

        # ── 1D: candle bias + volume + compression (daily-specific) ───────
        daily_bonus, daily_labels = self._analyze_daily(df1d, d5)
        score += daily_bonus
        reasons.extend(daily_labels)

        # ── 15m: intermediate setup confirmation ─────────────────────────
        # Journal data: with15m=41% WR vs no15m=20% WR.
        # Hard-kill was preventing valid early-morning signals. Now: penalty instead.
        if s15 and s15.direction == d5:
            score += W_15M_CONFIRM
            reasons.append("15m confirms")
        elif s15 and s15.direction != d5:
            score -= 15  # opposing 15m = big penalty but not instant death
            reasons.append("15m opposes(-15)")
        else:
            score += 5   # no 15m data = small neutral bonus (early morning)
            reasons.append("15m neutral")

        # ── Volume cascade (15m volume escalating into 5m) ───────────────
        v5  = s5.volume_ratio
        v15 = s15.volume_ratio if s15 else 0.0
        cascade = v15 >= 1.2 and v5 >= 1.5
        if cascade:
            score += W_VOL_CASCADE
            reasons.append(f"vol {v15:.1f}x->{v5:.1f}x")
        elif v5 >= 1.5:
            score += W_VOL_CASCADE // 2
            reasons.append(f"5m vol {v5:.1f}x")

        # ── 15m structure quality ─────────────────────────────────────────
        if s15 and s15.structure.value in _GOOD:
            score += 15
            reasons.append(f"15m {s15.structure.value}")
        elif s15 and s15.structure == MarketStructure.CONSOLIDATION:
            score += 4
            reasons.append("15m consol")

        # ── EMA stack: all 3 timeframes aligned ──────────────────────────
        e5ok  = (s5.ema9  > s5.ema21)  if lng else (s5.ema9  < s5.ema21)
        e15ok = ((s15.ema9 > s15.ema21) if lng else (s15.ema9 < s15.ema21)) \
                if (s15 and s15.ema9) else True
        e1dok = ((s1d.ema9 > s1d.ema21) if lng else (s1d.ema9 < s1d.ema21)) \
                if (s1d and s1d.ema9) else True
        ema_ok = e5ok and e15ok and e1dok
        if ema_ok:
            score += W_EMA_STACK
            reasons.append("EMA stack")
        elif e5ok and e15ok:
            score += W_EMA_STACK // 2
            reasons.append("EMA 5m+15m")

        # ── Fresh 5m entry timing ─────────────────────────────────────────
        if s5.candles_since_breakout <= 2:
            score += W_FRESH_ENTRY
            reasons.append(f"fresh({s5.candles_since_breakout}b)")

        # ── VWAP context (intraday bias from 5m) ─────────────────────────
        vwap_ok = s5.vwap > 0 and (
            (lng and s5.entry_price > s5.vwap) or
            (not lng and s5.entry_price < s5.vwap)
        )
        if vwap_ok:
            score += W_VWAP
            reasons.append("VWAP ok")

        # ── Symbol tier adjustment (from historical scan) ────────────────
        # Historical WR: A-tier >= 40%, C-tier < 20% across 45 days/153 stocks.
        try:
            _tiers = get_learned_config().get("SYMBOL_TIERS", {})
            if sym in _tiers.get("A", []):
                score += 8
                reasons.append("sym_A-tier")
            elif sym in _tiers.get("C", []):
                score -= 10
                reasons.append("sym_C-tier(-10)")
        except Exception:
            pass

        # ── Pattern weight reinforcement ──────────────────────────────────
        # Learned from journal: patterns in TARGET_HIT → weight > 1 (boost),
        # patterns in SL_HIT → weight < 1 (dampen). Applied before grade cutoff
        # so winning patterns can push a borderline signal into Grade A, and
        # losing patterns can pull a mediocre signal back out.
        all_patterns = list(set(
            s5.patterns +
            (s15.patterns if s15 else []) +
            (s1d.patterns if s1d else [])
        ))
        try:
            pw = _sw.get("__pattern_weights__") or {}  # try cached
            if not pw:
                from core.adaptive_learner import get_learned_config as _glc
                pw = _glc().get("PATTERN_WEIGHTS", {})
            if pw and all_patterns:
                import math as _math
                weights = [pw.get(f"{d5}:{p.lower()}", pw.get(p.lower(), 1.0)) for p in all_patterns]
                geo = _math.exp(
                    sum(_math.log(max(w, 0.05)) for w in weights) / len(weights)
                )
                geo = max(0.5, min(2.0, geo))   # hard bounds: never halve or double
                score = round(score * geo)
        except Exception:
            pass

        # ── Breakout quality: multi-TF confirmation + fake breakout check ──
        # Breakout patterns have higher fake-out risk — validate them harder.
        _BREAKOUT_PATS = frozenset({
            'consol_breakout_up', 'consol_breakout_down',
            'bull_flag_breakout', 'bear_flag_breakout',
            'horizontal_breakout_up', 'horizontal_breakout_down',
            'nr7_breakout_up', 'nr7_breakout_down',
            'vwap_breakout_up', 'vwap_breakout_down',
        })
        has_breakout = bool(set(all_patterns) & _BREAKOUT_PATS)
        if has_breakout:
            # ── 15m timeframe confirmation ─────────────────────────────
            if s15:
                if s15.direction == d5:
                    score += 15         # 15m agrees → real breakout probability up
                    reasons.append("15m+5m breakout aligned")
                elif s15.direction not in (None, "neutral", d5):
                    score -= 15         # 15m opposes → likely fake breakout
                    reasons.append("15m opposes breakout")

            # ── Volume vs consolidation (pre-breakout bars on 5m) ──────
            try:
                if len(df5) >= 7:
                    consol_vol = float(df5['volume'].iloc[-6:-1].mean())
                    brk_vol    = float(df5['volume'].iloc[-1])
                    vol_exp    = brk_vol / consol_vol if consol_vol > 0 else 0
                    if vol_exp >= 2.5:
                        score += 15
                        reasons.append(f"vol_exp={vol_exp:.1f}x")
                    elif vol_exp >= 1.5:
                        score += 8
                        reasons.append(f"vol_exp={vol_exp:.1f}x")
                    elif vol_exp < 1.0:
                        score -= 12     # volume contracting on break = fake
                        reasons.append(f"low_vol_break({vol_exp:.1f}x)")
            except Exception:
                pass

            # ── Candle body check: real breaks close near high/low ─────
            try:
                c = df5.iloc[-1]
                body   = abs(float(c['close']) - float(c['open']))
                rng    = float(c['high']) - float(c['low'])
                if rng > 0:
                    body_ratio = body / rng
                    if body_ratio < 0.40:
                        score -= 10     # wick-heavy candle = rejection / fake
                        reasons.append(f"weak_body({body_ratio:.0%})")
                    elif body_ratio >= 0.65:
                        score += 5
                        reasons.append("strong_body")
            except Exception:
                pass

        # ── Grade S: ultra-high conviction ───────────────────────────────
        # Requires A-tier symbol + 15m confirmation + score >= 95.
        # Historical: A-tier + 15m = 60% WR at T2. Using T1 (1:1) → ~75-80% WR.
        _tiers_check = {}
        try:
            _tiers_check = get_learned_config().get("SYMBOL_TIERS", {})
        except Exception:
            pass
        is_a_tier    = sym in _tiers_check.get("A", [])
        is_c_tier    = sym in _tiers_check.get("C", [])
        tf_all3      = (s15 is not None and s15.direction == d5 and
                        s1d is not None and s1d.direction == d5)

        # Top mover override: bypass C-tier reject + lower grade threshold
        is_top_mover, mover_class = self._is_top_mover(df5, df1d)

        # Thresholds tuned to actually fire signals in normal markets.
        # Old (S=110/A=85/B=65/C=50) produced zero signals on 153-symbol scans.
        if score >= 95 and is_a_tier:
            g = "S"   # Super — A-tier symbol + score 95+ + 15m confirmed
        elif score >= 70:
            g = "A"   # Strong setup
        elif score >= 50:
            g = "B"   # Decent setup (smaller position via grade-based sizing)
        elif is_top_mover and score >= 35:
            g = "C"   # Top mover — even mediocre score gets grade if intraday move strong
        else:
            return None

        # C-tier reject UNLESS this is today's top mover (yesterday's loser != today's loser)
        if is_c_tier and g not in ("S", "A") and not is_top_mover:
            return None

        # ── Precise SL + T1/T2 from 15m structure ────────────────────────
        atr_15m = (s15.atr if s15 and s15.atr else 0) or s5.atr
        levels  = self._calc_levels(df15, df5, d5, s5.entry_price, atr_15m)

        # SL band: ATR-aware floor + pct ceiling. High-volatility stocks need
        # room to breathe (1.5x 15m ATR), low-volatility get the 1.0% floor.
        entry = s5.entry_price
        atr_floor = (atr_15m * 1.5) if atr_15m and atr_15m > 0 else 0.0
        min_sl_dist = max(entry * 0.010, atr_floor)
        # Read max_sl_pct + rr_t2 from learned/regime config so top movers + volatile
        # regimes can opt-in to wider stops + bigger runners.
        try:
            _learned_sig = get_learned_config().get("SIGNAL_CONFIG", {})
            _learned_risk = get_learned_config().get("RISK_CONFIG", {})
        except Exception:
            _learned_sig, _learned_risk = {}, {}
        max_sl_pct = float(_learned_risk.get("max_sl_pct", 0.025))
        # Top movers earn wider stops + bigger runners — signal_engine applies
        # these in its own path, but timeframe_sync builds the actual T1/T2 so
        # the override must be applied HERE too or movers get capped at 3.0R.
        if is_top_mover and mover_class != 'normal':
            try:
                from core.top_mover_mode import get_override_config
                _ov = get_override_config(mover_class)
                if "max_sl_pct" in _ov:
                    max_sl_pct = float(_ov["max_sl_pct"])
                if "rr_ratio" in _ov:
                    _learned_sig = {**_learned_sig, "rr_ratio": float(_ov["rr_ratio"])}
            except Exception:
                pass
        # Cap min_sl_dist below max so clamp logic stays sane
        max_sl_dist = entry * max_sl_pct
        if min_sl_dist > max_sl_dist:
            min_sl_dist = max_sl_dist
        sl = levels["sl"]
        sl_dist_raw = abs(entry - sl)
        if sl_dist_raw < min_sl_dist:
            sl_dist_raw = min_sl_dist
        elif sl_dist_raw > max_sl_dist:
            sl_dist_raw = max_sl_dist
        if d5 == "long":
            sl = entry - sl_dist_raw
        else:
            sl = entry + sl_dist_raw
        levels["sl"] = round(sl, 2)
        risk = sl_dist_raw

        # Multi-target — T1 close so it actually fills,
        # T2 runner from learned config (regime/top-mover overrides apply):
        #   T1 = 1.5R → partial exit, move SL to breakeven
        #   T2 = rr_ratio (default 4.0R, up to 5.0R for extreme movers)
        rr_t1 = 1.5
        rr_t2 = float(_learned_sig.get("rr_ratio", 4.0))
        levels["t1"] = round(entry + risk * rr_t1, 2) if d5 == "long" else round(entry - risk * rr_t1, 2)
        levels["t2"] = round(entry + risk * rr_t2, 2) if d5 == "long" else round(entry - risk * rr_t2, 2)

        # Primary target = T2 (4.0R) — runners capture real moves
        # Partial exit at T1 locks 1.5R profit, runner aims for 4.0R+
        primary_target = levels["t2"]

        return SyncedSignal(
            symbol           = sym,
            direction        = d5,
            confluence_grade = g,
            confluence_score = score,
            entry_price      = s5.entry_price,
            sl_price         = levels["sl"],
            sl_tight         = levels["sl_tight"],
            target_1         = levels["t1"],
            target_price     = primary_target,
            signal_5m        = s5,
            signal_15m       = s15,
            signal_1d        = s1d,
            volume_cascade   = cascade,
            ema_stack_aligned= ema_ok,
            vwap_synced      = vwap_ok,
            patterns_combined= all_patterns,
            reason           = ", ".join(reasons),
        )

    def scan_universe(self, api, symbols: List[str], max_workers: int = 8) -> List[Dict]:
        out = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for sig in pool.map(lambda sym: self.analyze(api, sym), symbols):
                if sig:
                    out.append(sig)

        # Generate ranked CANDIDATES only. Calibration + selective-fire is
        # applied later by core.signal_finalize, AFTER option-leg + OI
        # enrichment, so the gate sees the OI-adjusted score (running it
        # here would kill candidates before the OI edge could speak).
        out.sort(key=lambda s: ({"S": 0, "A": 1, "B": 2, "C": 3}.get(s.confluence_grade, 9),
                                 -s.confluence_score))
        return [s.to_dict() for s in out]
