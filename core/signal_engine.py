import logging
import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass, field
from enum import Enum
import config

_log = logging.getLogger(__name__)


class MarketStructure(Enum):
    BREAKOUT = "breakout"
    RANGE = "range"
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    CONSOLIDATION = "consolidation"
    UNKNOWN = "unknown"
    NR7_BREAKOUT = "nr7_breakout"
    INSIDE_BAR_BREAKOUT = "inside_bar_breakout"
    EMA_CROSSOVER = "ema_crossover"
    VWAP_BREAKOUT = "vwap_breakout"
    EMA_PULLBACK = "ema_pullback"
    RSI_REVERSAL = "rsi_reversal"


class VolatilityRegime(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


@dataclass
class Signal:
    symbol: str
    direction: str       # "long" or "short"
    structure: MarketStructure
    volatility: VolatilityRegime
    entry_price: float
    atr: float
    atr_price: float
    volume_ratio: float
    strength: float      # 0-100
    candles_since_breakout: int
    reason: str
    rsi: float = 50.0
    ema9: float = 0.0
    ema21: float = 0.0
    vwap: float = 0.0
    patterns: List[str] = field(default_factory=list)
    long_votes: int = 0
    short_votes: int = 0
    # Timeframe label set by callers — "3m" / "5m" / "10m" / "" (legacy daily/intraday)
    timeframe: str = ""


class SignalEngine:
    def __init__(self):
        self.config = dict(config.SIGNAL_CONFIG)  # mutable copy
        self._apply_learned_params()
        self.cache = {}

    def _apply_learned_params(self, regime: str = "neutral",
                              variant: str = "champion") -> None:
        """Merge learned params: defaults <- learned <- regime <- A/B variant.

        Layered override (each higher-priority layer wins):
          1. config.SIGNAL_CONFIG (defaults)
          2. learned_params.json (adaptive_learner overall tune)
          3. regime_params.json[regime] (regime-specific overrides)
          4. champion_challenger active variant (A/B test)
        """
        try:
            from core.adaptive_learner import get_learned_config
            learned = get_learned_config().get("SIGNAL_CONFIG", {})
            self.config.update(learned)
        except Exception:
            pass

        # Regime-specific override
        try:
            from core.regime_params import get_regime_params
            regime_p = get_regime_params().get_params(regime)
            if regime_p:
                self.config.update(regime_p)
        except Exception:
            pass

        # Champion/Challenger override (highest priority)
        try:
            from core.champion_challenger import get_cc
            variant_p = get_cc().get_active_params(variant)
            if variant_p:
                self.config.update(variant_p)
        except Exception:
            pass

        self._active_variant = variant

    def reload_learned_params(self) -> None:
        """Call this to pick up parameter updates mid-session."""
        self.config = dict(config.SIGNAL_CONFIG)
        self._apply_learned_params()

    # ──────────────────────────── Indicators ────────────────────────────

    def calculate_atr(self, df: pd.DataFrame, period: int = None) -> float:
        period = period or self.config['atr_period']
        if len(df) < period + 1:
            return 0.0
        high, low, close = df['high'], df['low'], df['close']
        tr = pd.concat([high - low,
                        (high - close.shift(1)).abs(),
                        (low - close.shift(1)).abs()], axis=1).max(axis=1)
        val = tr.rolling(period).mean().iloc[-1]
        return float(val) if not pd.isna(val) else 0.0

    def calculate_rsi(self, df: pd.DataFrame, period: int = 14) -> float:
        closes = df['close']
        if len(closes) < period + 1:
            return 50.0
        delta = closes.diff()
        gain = delta.where(delta > 0, 0.0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))
        val = rsi.iloc[-1]
        return float(val) if not pd.isna(val) else 50.0

    def calculate_ema(self, df: pd.DataFrame, period: int) -> pd.Series:
        return df['close'].ewm(span=period, adjust=False).mean()

    def calculate_vwap(self, df: pd.DataFrame) -> float:
        """VWAP — auto-resets to current session when intraday bars detected."""
        work = df
        if 'date' in df.columns and len(df) > 10:
            try:
                dt = pd.to_datetime(df['date'])
                unique_days = dt.dt.date.nunique()
                # Intraday: rows >> unique dates (multiple bars per day)
                if unique_days > 1 and len(df) / unique_days >= 5:
                    latest_day = dt.dt.date.max()
                    mask = dt.dt.date == latest_day
                    if mask.sum() >= 1:
                        work = df[mask]
            except Exception:
                pass
        tp = (work['high'] + work['low'] + work['close']) / 3
        total_vol = work['volume'].sum()
        if total_vol == 0:
            return float(work['close'].iloc[-1])
        return float((tp * work['volume']).sum() / total_vol)

    def calculate_supertrend(self, df: pd.DataFrame,
                              period: int = 10, multiplier: float = 3.0) -> Tuple[bool, float]:
        """Returns (in_uptrend, supertrend_support_level)."""
        if len(df) < period + 2:
            return True, float(df['close'].iloc[-1])

        high, low, close = df['high'].values, df['low'].values, df['close'].values
        atr_period = period

        tr = np.maximum(high - low,
               np.maximum(np.abs(high - np.roll(close, 1)),
                          np.abs(low - np.roll(close, 1))))
        tr[0] = high[0] - low[0]

        # Wilder smoothing for ATR
        atr = np.zeros(len(tr))
        atr[:atr_period] = tr[:atr_period].mean()
        for i in range(atr_period, len(tr)):
            atr[i] = (atr[i-1] * (atr_period - 1) + tr[i]) / atr_period

        hl2 = (high + low) / 2
        upper_basic = hl2 + multiplier * atr
        lower_basic = hl2 - multiplier * atr

        final_upper = upper_basic.copy()
        final_lower = lower_basic.copy()

        for i in range(1, len(df)):
            final_upper[i] = min(upper_basic[i], final_upper[i-1]) if close[i-1] <= final_upper[i-1] else upper_basic[i]
            final_lower[i] = max(lower_basic[i], final_lower[i-1]) if close[i-1] >= final_lower[i-1] else lower_basic[i]

        # Determine current trend
        in_uptrend = True
        st_level = final_lower[-1]
        for i in range(atr_period, len(df)):
            if close[i] > final_upper[i]:
                in_uptrend = True
            elif close[i] < final_lower[i]:
                in_uptrend = False
            st_level = final_lower[i] if in_uptrend else final_upper[i]

        return in_uptrend, float(st_level)

    # ──────────────────────────── Pattern Detectors ─────────────────────

    def detect_candlestick_patterns(self, df: pd.DataFrame) -> Tuple[int, int, List[str]]:
        """Pin bars and engulfing patterns — high-precision reversal/continuation signals."""
        lv, sv, patterns = 0, 0, []
        if len(df) < 2:
            return lv, sv, patterns

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        o  = float(curr['open'])
        h  = float(curr['high'])
        l  = float(curr['low'])
        c  = float(curr['close'])
        body       = abs(c - o)
        total_range = h - l

        if total_range < 1e-9:
            return lv, sv, patterns

        upper_wick  = h - max(o, c)
        lower_wick  = min(o, c) - l
        body_pct    = body / total_range

        # ── Pin bars: rejection at extreme, body < 40% of range ───────────
        # Bullish: long lower wick (rejection of sellers), body in upper 55%+
        if lower_wick > 2 * max(body, total_range * 0.01) and body_pct < 0.40:
            lower_shadow_pct = lower_wick / total_range
            if lower_shadow_pct > 0.55:
                lv += 2
                patterns.append('bullish_pin_bar')

        # Bearish: long upper wick (rejection of buyers), body in lower 55%+
        if upper_wick > 2 * max(body, total_range * 0.01) and body_pct < 0.40:
            upper_shadow_pct = upper_wick / total_range
            if upper_shadow_pct > 0.55:
                sv += 2
                patterns.append('bearish_pin_bar')

        # ── Engulfing: current candle body fully covers previous body ──────
        prev_o = float(prev['open'])
        prev_c = float(prev['close'])
        prev_bullish = prev_c > prev_o
        curr_bullish = c > o

        if not prev_bullish and curr_bullish:
            # Bullish engulfing: curr open <= prev close AND curr close >= prev open
            if o <= prev_c and c >= prev_o and body > abs(prev_c - prev_o):
                lv += 2
                patterns.append('bullish_engulfing')

        if prev_bullish and not curr_bullish:
            # Bearish engulfing: curr open >= prev close AND curr close <= prev open
            if o >= prev_c and c <= prev_o and body > abs(prev_c - prev_o):
                sv += 2
                patterns.append('bearish_engulfing')

        return lv, sv, patterns

    def detect_nr7(self, df: pd.DataFrame) -> bool:
        """Narrowest range candle in last 7 bars = coiled spring."""
        if len(df) < 7:
            return False
        ranges = (df['high'] - df['low']).tail(7)
        # Use tolerance (1e-9) to avoid float precision miss on equal ranges
        return bool(ranges.iloc[-1] <= ranges.min() * 1.0001 and
                    ranges.iloc[-1] < ranges.mean() * 0.75)

    def detect_inside_bar_breakout(self, df: pd.DataFrame) -> Tuple[bool, str]:
        """Previous bar was inside bar; current bar breaks out."""
        if len(df) < 3:
            return False, "none"
        prev2, prev1, curr = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        is_inside = prev1['high'] <= prev2['high'] and prev1['low'] >= prev2['low']
        if is_inside:
            if curr['close'] > prev2['high']:
                return True, "long"
            if curr['close'] < prev2['low']:
                return True, "short"
        return False, "none"

    def detect_ema_signal(self, df: pd.DataFrame,
                          ema9: pd.Series, ema21: pd.Series) -> Tuple[int, int, List[str]]:
        """EMA9/21 cross + EMA8/21 stack alignment (Pine: Enhanced EMA Crossover).

        EMA8 (fast) fires 1-2 bars earlier than EMA9.
        When EMA8 and EMA9 BOTH cross EMA21 on the same bar → ema_stack_aligned (+1 bonus).
        This confirms the cross is genuine, not a whipsaw.
        """
        lv, sv, patterns = 0, 0, []
        if len(df) < 25:
            return lv, sv, patterns

        ema8 = df['close'].ewm(span=8, adjust=False).mean()

        cross_up = ema9.iloc[-2] <= ema21.iloc[-2] and ema9.iloc[-1] > ema21.iloc[-1]
        cross_dn = ema9.iloc[-2] >= ema21.iloc[-2] and ema9.iloc[-1] < ema21.iloc[-1]

        # EMA8 alignment: is EMA8 also on same side of EMA21?
        ema8_bullish = ema8.iloc[-1] > ema21.iloc[-1]
        ema8_bearish = ema8.iloc[-1] < ema21.iloc[-1]
        # EMA8 fresh cross (1-2 bars recent)
        ema8_cross_up = any(
            ema8.iloc[i-1] <= ema21.iloc[i-1] and ema8.iloc[i] > ema21.iloc[i]
            for i in range(-3, 0)
        )
        ema8_cross_dn = any(
            ema8.iloc[i-1] >= ema21.iloc[i-1] and ema8.iloc[i] < ema21.iloc[i]
            for i in range(-3, 0)
        )

        if cross_up:
            lv += 3
            patterns.append('ema_bullish_cross')
            # EMA8 already above EMA21 = stack aligned, cross confirmed
            if ema8_bullish:
                lv += 1
                patterns.append('ema_stack_aligned_bull')
        elif cross_dn:
            sv += 3
            patterns.append('ema_bearish_cross')
            if ema8_bearish:
                sv += 1
                patterns.append('ema_stack_aligned_bear')
        elif ema9.iloc[-1] > ema21.iloc[-1]:
            lv += 1
            patterns.append('ema_uptrend')
            # EMA8 crossed up recently while EMA9 already bullish = fresh momentum
            if ema8_cross_up and ema8_bullish:
                lv += 1
                patterns.append('ema8_fresh_cross_up')
        else:
            sv += 1
            patterns.append('ema_downtrend')
            if ema8_cross_dn and ema8_bearish:
                sv += 1
                patterns.append('ema8_fresh_cross_dn')

        return lv, sv, patterns

    def detect_ema_volume_confirmation(self, df: pd.DataFrame, direction: str,
                                       lookback: int = 10
                                       ) -> Tuple[bool, bool, List[str]]:
        """Volume confirmation of an EMA-crossover trend (trader methodology).

        The EMA9/21 cross (best on the 1H bias timeframe) is a
        high-probability entry trigger — it works most of the time. Its rare
        failures are foreshadowed by FADING volume: when price keeps pushing
        in the trend direction but each successive bar trades on lower
        volume, participation is drying up and the odds of the pattern
        flipping rise sharply. Steady or rising volume means the move still
        has fuel and the cross can be trusted.

        Returns (confirmed, fading, patterns):
          • confirmed → 'ema_cross_vol_confirmed' (volume backs the move)
          • fading    → 'ema_cross_vol_fading'   (exhaustion warning)
        Both False = inconclusive (no clear leg / flat volume).
        """
        pats: List[str] = []
        if df is None or len(df) < 25 or 'volume' not in df.columns:
            return False, False, pats

        closes = df['close'].astype(float)
        vols = df['volume'].astype(float)
        ema9 = closes.ewm(span=9, adjust=False).mean()
        ema21 = closes.ewm(span=21, adjust=False).mean()

        # Find the most recent cross in the trend direction within the window
        win = min(lookback * 2, len(df) - 2)
        cross_idx = None
        for k in range(1, win + 1):
            i = -k
            up = (ema9.iloc[i - 1] <= ema21.iloc[i - 1]
                  and ema9.iloc[i] > ema21.iloc[i])
            dn = (ema9.iloc[i - 1] >= ema21.iloc[i - 1]
                  and ema9.iloc[i] < ema21.iloc[i])
            if (direction == 'long' and up) or (direction == 'short' and dn):
                cross_idx = len(df) + i
                break

        # Volume + price of the post-cross leg (or the standing trend if no
        # fresh cross — the rule still applies to an ongoing trend's fuel)
        if cross_idx is not None and cross_idx < len(df) - 2:
            seg_v = vols.iloc[cross_idx:].values
            seg_c = closes.iloc[cross_idx:].values
        else:
            seg_v = vols.iloc[-lookback:].values
            seg_c = closes.iloc[-lookback:].values

        if len(seg_v) < 3:
            return False, False, pats
        vmean = float(np.nanmean(seg_v))
        if not np.isfinite(vmean) or vmean <= 0:
            return False, False, pats

        # Fractional volume change per bar across the leg (trend slope)
        x = np.arange(len(seg_v), dtype=float)
        try:
            vslope = float(np.polyfit(x, seg_v, 1)[0]) / vmean
        except Exception:
            return False, False, pats

        price_prog = (seg_c[-1] - seg_c[0]) / seg_c[0] if seg_c[0] else 0.0
        moved = ((direction == 'long' and price_prog > 0)
                 or (direction == 'short' and price_prog < 0))

        confirmed = fading = False
        if moved:
            if vslope <= -0.04:      # volume shrinking ≥4%/bar while price extends
                fading = True
                pats.append('ema_cross_vol_fading')
            elif vslope >= 0.0:      # volume holding or rising with the move
                confirmed = True
                pats.append('ema_cross_vol_confirmed')
        return confirmed, fading, pats

    def detect_vwap_signal(self, df: pd.DataFrame, vwap: float) -> Tuple[int, int, List[str]]:
        """VWAP-based signals."""
        lv, sv, patterns = 0, 0, []
        if len(df) < 2:
            return lv, sv, patterns

        curr = df['close'].iloc[-1]
        prev = df['close'].iloc[-2]
        deviation = (curr - vwap) / vwap * 100
        prev_dev = (prev - vwap) / vwap * 100

        # Fresh VWAP break is strongest signal
        if prev_dev <= 0.1 and deviation > 0.4:
            lv += 3
            patterns.append('vwap_breakout_up')
        elif prev_dev >= -0.1 and deviation < -0.4:
            sv += 3
            patterns.append('vwap_breakout_down')
        elif deviation > 0.6:
            lv += 1
            patterns.append('above_vwap')
        elif deviation < -0.6:
            sv += 1
            patterns.append('below_vwap')

        return lv, sv, patterns

    def detect_rsi_signal(self, rsi: float, vol_surge: bool) -> Tuple[int, int, List[str]]:
        """RSI-based signals — symmetric for longs and shorts.

        RSI ladder (symmetric by design):
          <25 + vol_surge  → lv+3 oversold_vol_surge (extreme reversal)
          <30              → context-dependent: reversal vs continuation
          25-38            → sv+1 short momentum zone (bearish continuation)
          38-50            → sv+1 bearish zone
          50-55            → dead zone (no conviction either way)
          55-62            → dead zone for longs (weak momentum)
          62-70            → lv+1 bullish zone
          70+              → lv+1 momentum zone (bullish continuation)
          >75 + vol_surge  → sv+2 overbought exhaustion
        """
        lv, sv, patterns = 0, 0, []
        oversold = self.config.get('rsi_oversold', 38)
        overbought = self.config.get('rsi_overbought', 70)
        extreme_oversold = self.config.get('rsi_extreme_oversold', 30)
        extreme_overbought = self.config.get('rsi_extreme_overbought', 75)
        rsi_long_momentum_min = self.config.get('rsi_long_momentum_min', 62)

        if rsi < extreme_oversold and vol_surge:
            # Extreme oversold + volume = capitulation reversal (long)
            lv += 3
            patterns.append('rsi_oversold_vol_surge')
        elif rsi > extreme_overbought and vol_surge:
            # Extreme overbought + volume = distribution exhaustion (short)
            sv += 3
            patterns.append('rsi_overbought_vol_surge')
        elif rsi < extreme_oversold:
            # Deep oversold without vol surge = bearish continuation (was giving long vote — wrong!)
            sv += 1
            patterns.append('rsi_deep_bearish')
        elif extreme_oversold <= rsi < oversold:
            # Oversold zone: bearish momentum, short continuation
            sv += 1
            patterns.append('rsi_short_momentum_zone')
        elif oversold <= rsi < 50:
            # Mild bearish zone
            sv += 1
            patterns.append('rsi_bearish_zone')
        elif rsi > extreme_overbought:
            # Deep overbought without vol surge = exhaustion, short opportunity
            sv += 1
            patterns.append('rsi_deep_overbought')
        elif overbought < rsi <= extreme_overbought:
            # Above overbought = strong bullish momentum continuation
            lv += 1
            patterns.append('rsi_momentum_zone')
        elif rsi_long_momentum_min < rsi <= overbought:
            # Clear bullish momentum zone
            lv += 1
            patterns.append('rsi_bullish_zone')
        elif 50 <= rsi <= rsi_long_momentum_min:
            # Dead zone: 50-62 — no directional conviction, no vote
            pass
        return lv, sv, patterns

    def detect_ema_pullback(self, df: pd.DataFrame,
                            ema9: pd.Series, ema21: pd.Series) -> Tuple[int, int, List[str]]:
        """EMA21 pullback in trend direction."""
        lv, sv, patterns = 0, 0, []
        if len(df) < 3:
            return lv, sv, patterns

        in_uptrend = ema9.iloc[-1] > ema21.iloc[-1]
        ema21_val = ema21.iloc[-1]
        curr_low = df['low'].iloc[-1]
        curr_close = df['close'].iloc[-1]
        prev_low = df['low'].iloc[-2]

        if in_uptrend:
            touched = (curr_low <= ema21_val * 1.008 or prev_low <= ema21_val * 1.008)
            bounced = curr_close > ema21_val
            if touched and bounced:
                lv += 3
                patterns.append('ema21_pullback_long')

        in_downtrend = ema9.iloc[-1] < ema21.iloc[-1]
        curr_high = df['high'].iloc[-1]
        if in_downtrend:
            touched = curr_high >= ema21_val * 0.992
            rejected = curr_close < ema21_val
            if touched and rejected:
                sv += 3
                patterns.append('ema21_pullback_short')

        return lv, sv, patterns

    def detect_structure(self, df: pd.DataFrame) -> Tuple[MarketStructure, float]:
        if df.empty or len(df) < self.config['range_period']:
            return MarketStructure.UNKNOWN, 0

        atr = self.calculate_atr(df)
        current_price = df['close'].iloc[-1]
        recent = df.tail(self.config['range_period'])
        high = recent['high'].max()
        low = recent['low'].min()
        range_size = high - low

        avg_range = atr * self.config['range_period'] ** 0.5

        if range_size < avg_range * 0.5:
            return MarketStructure.RANGE, range_size

        if df['close'].iloc[-1] >= high - atr * 0.2:
            direction = "up"
        elif df['close'].iloc[-1] <= low + atr * 0.2:
            direction = "down"
        else:
            return MarketStructure.CONSOLIDATION, range_size

        sma20 = df['close'].rolling(20).mean().iloc[-1]
        sma50 = df['close'].rolling(50).mean().iloc[-1] if len(df) >= 50 else sma20

        if direction == "up" and sma20 > sma50:
            return MarketStructure.TREND_UP, range_size
        elif direction == "down" and sma20 < sma50:
            return MarketStructure.TREND_DOWN, range_size
        elif direction == "up":
            return MarketStructure.BREAKOUT, range_size
        else:
            return MarketStructure.BREAKOUT, range_size

    def detect_volatility(self, df: pd.DataFrame) -> VolatilityRegime:
        atr = self.calculate_atr(df)
        current_price = df['close'].iloc[-1]
        atr_pct = (atr / current_price) * 100
        avg_vol = df['volume'].rolling(20).mean().iloc[-1]
        current_vol = df['volume'].iloc[-1]
        vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1
        score = atr_pct * 10 + vol_ratio

        if score < 2:
            return VolatilityRegime.LOW
        elif score < 5:
            return VolatilityRegime.NORMAL
        else:
            return VolatilityRegime.HIGH

    def calculate_volume_profile(self, df: pd.DataFrame) -> float:
        avg = df['volume'].rolling(20).mean().iloc[-1]
        curr = df['volume'].iloc[-1]
        return float(curr / avg) if avg > 0 else 0.0

    def find_candles_since_breakout(self, df: pd.DataFrame) -> int:
        if len(df) < 5:
            return 0
        current_price = df['close'].iloc[-1]
        recent_high = df['high'].iloc[-5:-1].max()
        if current_price > recent_high:
            for i in range(len(df) - 2, max(len(df) - 10, 0), -1):
                if df['close'].iloc[i] <= recent_high:
                    return len(df) - i - 1
            return 5
        return 0

    def detect_consolidation_breakout(self, df: pd.DataFrame) -> Tuple[int, int, List[str]]:
        """
        Tight N-bar range (≤1.2 ATR) followed by close outside range with volume expansion
        vs the consolidation average. More robust than NR7 — requires sustained compression.
        """
        CONSOL_N = 5
        if len(df) < CONSOL_N + 3:
            return 0, 0, []

        consol = df.iloc[-(CONSOL_N + 1):-1]
        curr   = df.iloc[-1]

        atr = float((df['high'] - df['low']).rolling(14).mean().iloc[-2])
        if atr <= 0:
            return 0, 0, []

        consol_high  = float(consol['high'].max())
        consol_low   = float(consol['low'].min())
        consol_range = consol_high - consol_low

        if consol_range > atr * 1.2:
            return 0, 0, []  # range too wide — not a tight consolidation

        consol_vol = float(consol['volume'].mean())
        curr_vol   = float(curr['volume'])
        vol_exp    = curr_vol / consol_vol if consol_vol > 0 else 0

        if vol_exp < 1.3:
            return 0, 0, []  # no volume expansion = fake break risk

        lv, sv, pats = 0, 0, []
        if float(curr['close']) > consol_high:
            lv += 3
            pats.append('consol_breakout_up')
            if vol_exp >= 2.0:
                lv += 1  # extra vote: strong volume confirms real break
        elif float(curr['close']) < consol_low:
            sv += 3
            pats.append('consol_breakout_down')
            if vol_exp >= 2.0:
                sv += 1
        return lv, sv, pats

    def detect_flag_breakout(self, df: pd.DataFrame) -> Tuple[int, int, List[str]]:
        """
        Bull/Bear flag: strong impulse (5 bars, ≥1.5 ATR net move)
        → flag with declining volume (avg < 85% of impulse avg)
        → breakout in impulse direction with volume expansion.
        Volume dry-up in flag = institutional accumulation/distribution.
        """
        IMPULSE_N = 5
        FLAG_N    = 5
        TOTAL     = IMPULSE_N + FLAG_N + 2
        if len(df) < TOTAL:
            return 0, 0, []

        curr    = df.iloc[-1]
        flag    = df.iloc[-(FLAG_N + 1):-1]
        impulse = df.iloc[-(IMPULSE_N + FLAG_N + 1):-(FLAG_N + 1)]

        atr          = float((df['high'] - df['low']).rolling(14).mean().iloc[-2])
        impulse_move = float(impulse['close'].iloc[-1] - impulse['close'].iloc[0])

        if abs(impulse_move) < atr * 1.5 or atr <= 0:
            return 0, 0, []

        bull_impulse = impulse_move > 0
        flag_vol     = float(flag['volume'].mean())
        impulse_vol  = float(impulse['volume'].mean())

        # Flag must have declining volume (compression before breakout)
        if flag_vol >= impulse_vol * 0.85 or impulse_vol <= 0:
            return 0, 0, []

        flag_high = float(flag['high'].max())
        flag_low  = float(flag['low'].min())
        curr_vol  = float(curr['volume'])
        vol_exp   = curr_vol / flag_vol if flag_vol > 0 else 0

        if vol_exp < 1.3:
            return 0, 0, []

        lv, sv, pats = 0, 0, []
        if bull_impulse and float(curr['close']) > flag_high:
            lv += 4
            pats.append('bull_flag_breakout')
        elif not bull_impulse and float(curr['close']) < flag_low:
            sv += 4
            pats.append('bear_flag_breakout')
        return lv, sv, pats

    def detect_horizontal_breakout(self, df: pd.DataFrame) -> Tuple[int, int, List[str]]:
        """
        Price tests same resistance or support level ≥2 times in last 20 bars
        (within 0.3% of the level), then current bar closes through it with volume.
        Multi-touch levels are the strongest breakout candidates.
        """
        if len(df) < 20:
            return 0, 0, []

        curr = df.iloc[-1]
        past = df.iloc[-20:-1]

        resistance = float(past['high'].max())
        support    = float(past['low'].min())
        avg_vol    = float(past['volume'].mean())
        curr_vol   = float(curr['volume'])
        vol_ratio  = curr_vol / avg_vol if avg_vol > 0 else 0

        if vol_ratio < 1.3:
            return 0, 0, []

        TOL = 0.003  # 0.3% tolerance for "touch"
        res_tests = sum(
            1 for i in range(len(past))
            if abs(float(past.iloc[i]['high']) - resistance) / max(resistance, 1) < TOL
        )
        sup_tests = sum(
            1 for i in range(len(past))
            if abs(float(past.iloc[i]['low']) - support) / max(support, 1) < TOL
        )

        lv, sv, pats = 0, 0, []
        curr_close = float(curr['close'])
        if curr_close > resistance and res_tests >= 2:
            lv += 3
            pats.append('horizontal_breakout_up')
        elif curr_close < support and sup_tests >= 2:
            sv += 3
            pats.append('horizontal_breakout_down')
        return lv, sv, pats

    def detect_supply_demand_zones(self, df: pd.DataFrame) -> Tuple[int, int, List[str]]:
        """Supply/Demand zone detection (Pine: DIY Custom Strategy Builder).

        Swing-high pivot → supply zone (top = swing high, bottom = top - ATR buffer).
        Swing-low pivot  → demand zone (bottom = swing low, top = bottom + ATR buffer).

        Long vote:  price inside / bouncing from demand zone (close > prev close).
        Short vote: price inside / rejecting from supply zone (close < prev close).
        Bonus vote: price breaking THROUGH zone = BOS (break-of-structure) in that direction.
        """
        SWING_LEN = 10   # bars on each side of pivot (Pine default)
        BOX_WIDTH = 2.5  # ATR multiplier for zone thickness
        LOOKBACK  = 60   # search last N bars for pivots
        MIN_BARS  = SWING_LEN * 2 + 5

        if len(df) < MIN_BARS:
            return 0, 0, []

        highs  = df['high'].values
        lows   = df['low'].values
        closes = df['close'].values

        # ATR-50 for zone thickness
        tr = np.maximum(highs - lows,
                        np.maximum(np.abs(highs - np.roll(closes, 1)),
                                   np.abs(lows  - np.roll(closes, 1))))
        tr[0] = highs[0] - lows[0]
        n_atr = min(50, len(tr))
        atr_val   = float(np.mean(tr[-n_atr:]))
        zone_w    = atr_val * (BOX_WIDTH / 10.0)

        curr_price = closes[-1]
        prev_price = closes[-2]

        supply_zones: List[Tuple[float, float]] = []
        demand_zones: List[Tuple[float, float]] = []

        lb = min(LOOKBACK, len(df) - SWING_LEN - 1)
        for i in range(SWING_LEN, SWING_LEN + lb):
            idx = len(df) - 1 - i
            if idx < SWING_LEN or idx + SWING_LEN >= len(df):
                continue
            win_h = highs[idx - SWING_LEN : idx + SWING_LEN + 1]
            win_l = lows[idx - SWING_LEN : idx + SWING_LEN + 1]
            if highs[idx] >= win_h.max() * 0.9999:
                supply_zones.append((highs[idx], highs[idx] - zone_w))
            if lows[idx] <= win_l.min() * 1.0001:
                demand_zones.append((lows[idx] + zone_w, lows[idx]))

        lv, sv, pats = 0, 0, []

        # ── Demand zone checks ──────────────────────────────────────────
        for z_top, z_bot in demand_zones[:5]:
            if z_bot <= curr_price <= z_top * 1.005:
                # Price inside demand zone — bounce confirmation (long)
                if curr_price >= prev_price:
                    lv += 2
                    pats.append('demand_zone_bounce')
                break
            elif prev_price >= z_bot > curr_price:
                # BOS: price broke BELOW demand zone → demand zone flips to resistance
                # This is the bearish break — strong short signal
                sv += 2
                pats.append('demand_zone_bos_down')
                break
            elif curr_price > z_top and prev_price <= z_top:
                # Price bouncing UP into the BOTTOM of a previously broken demand zone
                # = retest-as-resistance → short entry (see detect_broken_zone_retest)
                sv += 1
                pats.append('broken_demand_retest_short')
                break

        # ── Supply zone checks ──────────────────────────────────────────
        for z_top, z_bot in supply_zones[:5]:
            if z_bot * 0.995 <= curr_price <= z_top:
                # Price inside supply zone — rejection confirmation (short)
                if curr_price <= prev_price:
                    sv += 2
                    pats.append('supply_zone_rejection')
                break
            elif prev_price <= z_top < curr_price:
                # BOS: price broke ABOVE supply zone → supply zone flips to support
                # Bullish breakout — strong long signal
                lv += 2
                pats.append('supply_zone_bos_up')
                break
            elif curr_price < z_bot and prev_price >= z_bot:
                # Price pulling back DOWN into the TOP of a previously broken supply zone
                # = retest-as-support → long entry
                lv += 1
                pats.append('broken_supply_retest_long')
                break

        return lv, sv, pats

    def detect_broken_zone_retest(self, df_htf: pd.DataFrame,
                                   df_ltf: pd.DataFrame) -> Tuple[int, int, List[str]]:
        """Broken zone retest detector — the highest-probability MTF setup.

        Scenario A (SHORT):
          HTF (1H) breaks BELOW demand zone  →  old support becomes resistance
          LTF (5M/15M) shows bullish bounce  →  price retesting broken demand from below
          Action: SHORT at resistance (retest top of broken demand zone)

        Scenario B (LONG):
          HTF (1H) breaks ABOVE supply zone  →  old resistance becomes support
          LTF shows bearish pullback          →  price retesting broken supply from above
          Action: LONG at support (retest bottom of broken supply zone)

        This is NOT a conflict — the HTF bearish + LTF bullish combination IS the setup.
        Returns extra short/long votes to confirm the retest direction.
        """
        if df_htf is None or df_ltf is None:
            return 0, 0, []
        if len(df_htf) < 25 or len(df_ltf) < 25:
            return 0, 0, []

        lv, sv, pats = 0, 0, []

        try:
            # Get HTF demand zones
            htf_highs  = df_htf['high'].values
            htf_lows   = df_htf['low'].values
            htf_closes = df_htf['close'].values
            tr_h = np.maximum(htf_highs - htf_lows,
                              np.maximum(np.abs(htf_highs - np.roll(htf_closes, 1)),
                                         np.abs(htf_lows  - np.roll(htf_closes, 1))))
            tr_h[0] = htf_highs[0] - htf_lows[0]
            atr_htf   = float(np.mean(tr_h[-min(50, len(tr_h)):]))
            zone_w    = atr_htf * 0.25

            SWING     = min(10, len(df_htf) // 4)
            htf_demand_zones: List[Tuple[float, float]] = []
            htf_supply_zones: List[Tuple[float, float]] = []

            lb = min(40, len(df_htf) - SWING - 1)
            for i in range(SWING, SWING + lb):
                idx = len(df_htf) - 1 - i
                if idx < SWING or idx + SWING >= len(df_htf):
                    continue
                wh = htf_highs[idx - SWING : idx + SWING + 1]
                wl = htf_lows[idx - SWING  : idx + SWING + 1]
                if htf_lows[idx]  <= wl.min() * 1.0001:
                    htf_demand_zones.append((htf_lows[idx] + zone_w, htf_lows[idx]))
                if htf_highs[idx] >= wh.max() * 0.9999:
                    htf_supply_zones.append((htf_highs[idx], htf_highs[idx] - zone_w))

            ltf_curr  = float(df_ltf['close'].iloc[-1])
            ltf_prev  = float(df_ltf['close'].iloc[-2])
            htf_curr  = float(df_htf['close'].iloc[-1])
            htf_prev  = float(df_htf['close'].iloc[-2])

            # ── Scenario A: HTF broke below demand → retest from below → SHORT ──
            for z_top, z_bot in htf_demand_zones[:5]:
                zone_mid = (z_top + z_bot) / 2.0
                htf_broke_below = htf_curr < z_bot  # HTF price is now BELOW the zone
                # LTF is bouncing upward TOWARD the broken zone (retest)
                ltf_bouncing_up = ltf_curr > ltf_prev
                # LTF price approaching OR touching broken zone (within 2× ATR below zone)
                ltf_near_zone   = ltf_curr >= z_bot - atr_htf * 2 and ltf_curr <= z_top * 1.01

                if htf_broke_below and ltf_bouncing_up and ltf_near_zone:
                    sv += 3
                    pats.append('broken_demand_retest_short')
                    _log.debug(f"[SE] BROKEN DEMAND RETEST SHORT: zone={z_bot:.2f}-{z_top:.2f} "
                               f"htf={htf_curr:.2f} ltf={ltf_curr:.2f}")
                    break

            # ── Scenario B: HTF broke above supply → retest from above → LONG ──
            for z_top, z_bot in htf_supply_zones[:5]:
                htf_broke_above = htf_curr > z_top  # HTF price now ABOVE the zone
                # LTF is pulling back DOWN toward the broken zone (retest)
                ltf_pulling_back = ltf_curr < ltf_prev
                # LTF price approaching or touching the broken zone top (within 2× ATR)
                ltf_near_zone   = ltf_curr >= z_bot - atr_htf * 2 and ltf_curr <= z_top * 1.01

                if htf_broke_above and ltf_pulling_back and ltf_near_zone:
                    lv += 3
                    pats.append('broken_supply_retest_long')
                    _log.debug(f"[SE] BROKEN SUPPLY RETEST LONG: zone={z_bot:.2f}-{z_top:.2f} "
                               f"htf={htf_curr:.2f} ltf={ltf_curr:.2f}")
                    break

        except Exception as e:
            _log.debug(f"[SE] broken zone retest error: {e}")

        return lv, sv, pats

    def detect_pvsra(self, df: pd.DataFrame) -> Tuple[int, int, List[str]]:
        """Price Volume Spread Analysis (PVSRA — Pine: DIY Custom Strategy Builder).

        Super bar: volume >= 2× 10-bar avg  OR  volume×range = 10-bar high.
        High-vol bar: volume >= 1.5× avg.

        Super bullish (close > open) = institutional accumulation → long vote.
        Super bearish (close < open) = institutional distribution → short vote.
        """
        if len(df) < 12 or 'volume' not in df.columns:
            return 0, 0, []

        tail = df.tail(11)
        curr = tail.iloc[-1]
        hist = tail.iloc[:-1]   # previous 10 bars

        avg_vol = float(hist['volume'].mean())
        if avg_vol <= 0:
            return 0, 0, []

        curr_vol = float(curr['volume'])
        curr_rng = float(curr['high']) - float(curr['low'])
        vol_spread = curr_vol * curr_rng

        hist_vol_spread = float(
            (hist['volume'] * (hist['high'] - hist['low'])).max()
        )

        is_bull  = float(curr['close']) > float(curr['open'])
        is_super = curr_vol >= avg_vol * 2.0 or (
            hist_vol_spread > 0 and vol_spread >= hist_vol_spread
        )
        is_high  = curr_vol >= avg_vol * 1.5

        lv, sv, pats = 0, 0, []
        if is_super and is_bull:
            lv += 3; pats.append('pvsra_super_bull')
        elif is_super and not is_bull:
            sv += 3; pats.append('pvsra_super_bear')
        elif is_high and is_bull:
            lv += 1; pats.append('pvsra_high_vol_bull')
        elif is_high and not is_bull:
            sv += 1; pats.append('pvsra_high_vol_bear')
        return lv, sv, pats

    def detect_wae(self, df: pd.DataFrame) -> Tuple[int, int, List[str]]:
        """Waddah Attar Explosion (Pine: DIY Custom Strategy Builder).

        Fires when directional MACD momentum > Bollinger Band channel width AND > deadzone.
        Catches explosive directional moves that simple RSI/EMA miss.
        """
        if len(df) < 42:
            return 0, 0, []

        SENSITIVITY = 150
        FAST, SLOW   = 20, 40
        BB_LEN, BB_M = 20, 2.0

        closes = df['close'].values

        ema_fast = pd.Series(closes, dtype=float).ewm(span=FAST, adjust=False).mean().values
        ema_slow = pd.Series(closes, dtype=float).ewm(span=SLOW, adjust=False).mean().values
        macd     = ema_fast - ema_slow

        t1 = (macd[-1] - macd[-2]) * SENSITIVITY   # momentum delta

        sma_bb = float(np.mean(closes[-BB_LEN:]))
        std_bb = float(np.std(closes[-BB_LEN:], ddof=1))
        e1     = 2 * BB_M * std_bb                 # BB width

        # Deadzone: EMA of ATR(1) × 3.7 over 100 bars
        tr1 = np.abs(np.diff(closes))
        n_dz = min(100, len(tr1))
        deadzone = float(np.mean(tr1[-n_dz:])) * 3.7

        trend_up   = t1 if t1 >= 0 else 0.0
        trend_down = (-t1) if t1 < 0 else 0.0

        lv, sv, pats = 0, 0, []
        if trend_up > 0 and trend_up > e1 and e1 > deadzone and trend_up > deadzone:
            lv += 2; pats.append('wae_bull_explosion')
        elif trend_down > 0 and trend_down > e1 and e1 > deadzone and trend_down > deadzone:
            sv += 2; pats.append('wae_bear_explosion')
        return lv, sv, pats

    def detect_range_filter(self, df: pd.DataFrame) -> Tuple[int, int, List[str]]:
        """Smooth Range Filter trend direction (Pine: DIY Custom Strategy Builder).

        Builds a staircase price filter using smoothed ATR range.
        Long: price above rising filter.  Short: price below falling filter.
        """
        if len(df) < 30:
            return 0, 0, []

        PERIOD = 20   # tuned for 5m intraday (Pine default 100 for crypto — shorter here)
        MULT   = 2.5

        closes = df['close'].values
        diff   = np.abs(np.diff(closes))
        diff   = np.concatenate([[diff[0]], diff])

        # Conditional EMA of absolute change
        ema_ac  = pd.Series(diff, dtype=float).ewm(span=PERIOD, adjust=False).mean().values
        wper    = PERIOD * 2 - 1
        smrng   = pd.Series(ema_ac, dtype=float).ewm(span=wper, adjust=False).mean().values * MULT

        # Range filter staircase
        filt = np.zeros(len(closes), dtype=float)
        filt[0] = closes[0]
        for i in range(1, len(closes)):
            hi = closes[i] - smrng[i]
            lo = closes[i] + smrng[i]
            if hi > filt[i - 1]:
                filt[i] = hi
            elif lo < filt[i - 1]:
                filt[i] = lo
            else:
                filt[i] = filt[i - 1]

        curr = closes[-1]
        rf_up   = curr > filt[-1] and filt[-1] > filt[-2]
        rf_down = curr < filt[-1] and filt[-1] < filt[-2]

        lv, sv, pats = 0, 0, []
        if rf_up:
            lv += 1; pats.append('range_filter_up')
        elif rf_down:
            sv += 1; pats.append('range_filter_down')
        return lv, sv, pats

    # ──────────────────────────── Main Signal Generator ─────────────────

    def generate_signal(self, symbol: str, df: pd.DataFrame) -> Optional[Signal]:
        if df.empty or len(df) < 25:
            return None

        atr = self.calculate_atr(df)
        current_price = float(df['close'].iloc[-1])
        rsi = self.calculate_rsi(df)
        vwap = self.calculate_vwap(df)

        ema9 = self.calculate_ema(df, 9)
        ema21 = self.calculate_ema(df, 21)
        ema9_val = float(ema9.iloc[-1])
        ema21_val = float(ema21.iloc[-1])

        avg_vol = df['volume'].rolling(20).mean().iloc[-1]
        curr_vol = df['volume'].iloc[-1]
        # Guard against NaN (rolling on <20 bars) — fall back to neutral 1.0
        if pd.isna(avg_vol) or pd.isna(curr_vol) or avg_vol <= 0:
            vol_ratio = 1.0
        else:
            vol_ratio = float(curr_vol / avg_vol)
        vol_surge = vol_ratio >= self.config.get('vol_surge_threshold', 1.5)

        structure, range_size = self.detect_structure(df)
        volatility = self.detect_volatility(df)

        long_votes, short_votes = 0, 0
        all_patterns: List[str] = []

        # ── 1. Classic market structure ──────────────────────────────────
        if structure == MarketStructure.TREND_UP:
            long_votes += 2
            all_patterns.append('trend_up')
        elif structure == MarketStructure.TREND_DOWN:
            short_votes += 2
            all_patterns.append('trend_down')
        elif structure == MarketStructure.BREAKOUT:
            sma20 = df['close'].rolling(20).mean().iloc[-1]
            if current_price >= sma20:
                long_votes += 2
                all_patterns.append('price_breakout_up')
            else:
                short_votes += 2
                all_patterns.append('price_breakout_down')

        # ── 2. EMA signals ───────────────────────────────────────────────
        lv, sv, pats = self.detect_ema_signal(df, ema9, ema21)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── 3. VWAP signals ──────────────────────────────────────────────
        lv, sv, pats = self.detect_vwap_signal(df, vwap)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── 4. RSI signals ───────────────────────────────────────────────
        lv, sv, pats = self.detect_rsi_signal(rsi, vol_surge)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── 5. NR7 breakout ──────────────────────────────────────────────
        if self.detect_nr7(df) and vol_surge:
            if current_price > float(df['close'].iloc[-2]):
                long_votes += 3
                all_patterns.append('nr7_breakout_up')
            else:
                short_votes += 3
                all_patterns.append('nr7_breakout_down')

        # ── 6. Inside-bar breakout ───────────────────────────────────────
        ib_break, ib_dir = self.detect_inside_bar_breakout(df)
        if ib_break:
            if ib_dir == 'long':
                long_votes += 2; all_patterns.append('inside_bar_long')
            else:
                short_votes += 2; all_patterns.append('inside_bar_short')

        # ── 7. EMA21 pullback ────────────────────────────────────────────
        lv, sv, pats = self.detect_ema_pullback(df, ema9, ema21)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── 8. Supertrend ────────────────────────────────────────────────
        st_uptrend, st_level = self.calculate_supertrend(df)
        if st_uptrend:
            long_votes += 1; all_patterns.append('supertrend_up')
        else:
            short_votes += 1; all_patterns.append('supertrend_down')

        # ── 9. Candlestick patterns (pin bar / engulfing) ────────────────
        lv, sv, pats = self.detect_candlestick_patterns(df)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── 10. Volume surge bonus ───────────────────────────────────────
        if vol_surge:
            all_patterns.append(f'vol_{vol_ratio:.1f}x')

        # ── 11. Consolidation breakout (tight range → volume expansion) ──
        lv, sv, pats = self.detect_consolidation_breakout(df)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── 12. Flag/pennant breakout ─────────────────────────────────────
        lv, sv, pats = self.detect_flag_breakout(df)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── 13. Horizontal level breakout (multi-test → clean break) ─────
        lv, sv, pats = self.detect_horizontal_breakout(df)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── 14. Supply/Demand zones (swing pivot + ATR buffer) ────────────
        lv, sv, pats = self.detect_supply_demand_zones(df)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── 15. PVSRA: institutional volume bars ──────────────────────────
        lv, sv, pats = self.detect_pvsra(df)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── 16. Waddah Attar Explosion: momentum > BB channel > deadzone ──
        lv, sv, pats = self.detect_wae(df)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── 17. Range Filter: smoothed ATR trend direction ────────────────
        lv, sv, pats = self.detect_range_filter(df)
        long_votes += lv; short_votes += sv; all_patterns.extend(pats)

        # ── Top Mover detection: override filters for clear day's movers ──
        try:
            from core.top_mover_mode import (
                detect_top_mover, get_override_config,
                should_bypass_afternoon_block,
            )
            is_mover, mover_class, intraday_pct, day_vol_ratio = detect_top_mover(df)
            if is_mover:
                overrides = get_override_config(mover_class)
                # Temporarily merge overrides into config (this call only)
                effective_config = {**self.config, **overrides}
                _log.info(f"[SE] {symbol} TOP_MOVER detected: class={mover_class} "
                          f"day_pct={intraday_pct:+.1f}% day_vol={day_vol_ratio:.1f}x")
            else:
                effective_config = self.config
                mover_class = 'normal'
        except Exception as e:
            _log.debug(f"top_mover detect err: {e}")
            effective_config = self.config
            mover_class = 'normal'
            is_mover = False

        # ── Decision: need minimum confluence ───────────────────────────
        MIN_VOTES = int(effective_config.get('min_votes', 3))
        MIN_LEAD = int(effective_config.get('min_vote_lead', 2))

        disable_shorts = bool(effective_config.get('disable_shorts', False))
        rsi_long_floor = float(effective_config.get('rsi_long_momentum_min', 58))

        # Time-based filter: block late-day + opening noise
        # Skip entirely in replay mode (testing with historical data)
        from datetime import datetime, timezone, timedelta
        _ist = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(_ist)
        current_hour = now_ist.hour
        current_minute = now_ist.minute

        import os
        if os.environ.get("REPLAY_MODE") == "1":
            current_hour = 10  # simulate best trading hour
            current_minute = 30

        # Swing trades on the daily close — intraday clock gates (skip the
        # open, block the afternoon) are meaningless and would wrongly kill
        # every signal generated after noon. Bypass them in swing mode.
        try:
            from core.trade_mode import get_mode as _gm
            _bypass_clock = bool(_gm().bypass_intraday_time_gates)
        except Exception:
            _bypass_clock = False

        if not _bypass_clock:
            # Skip first N minutes (09:15-09:30 opening candle = false breakouts)
            skip_mins = int(effective_config.get('skip_first_minutes', 15))
            if current_hour == 9 and current_minute < (15 + skip_mins):
                _log.debug(f"[SE] {symbol} KILL: opening noise ({current_hour}:{current_minute:02d})")
                return None

            block_after = int(effective_config.get('block_after_hour', 12))
            if current_hour >= block_after:
                # Top mover override: allow extreme/top movers through afternoon block
                try:
                    if is_mover and should_bypass_afternoon_block(mover_class, current_hour):
                        _log.info(f"[SE] {symbol} BYPASS afternoon block (top mover at {current_hour}:00)")
                    else:
                        _log.debug(f"[SE] {symbol} KILL: hour={current_hour} >= {block_after}")
                        return None
                except Exception:
                    _log.debug(f"[SE] {symbol} KILL: hour={current_hour} >= {block_after}")
                    return None

        # Volume cap: wins avg vol_ratio=0.96, losses=1.70. Excessive volume = chasing
        # Top movers HAVE high vol — bypass via effective_config override
        max_vol_ratio = float(effective_config.get('max_volume_ratio', 3.0))
        if vol_ratio > max_vol_ratio:
            _log.debug(f"[SE] {symbol} KILL: vol_ratio={vol_ratio:.1f} > {max_vol_ratio}")
            return None

        # Pattern blacklist: 0% WR patterns from journal — kill signal if dominant
        blacklist = effective_config.get('blacklisted_patterns', [])
        if blacklist:
            blacklisted_count = sum(1 for p in all_patterns if p in blacklist)
            clean_count = len(all_patterns) - blacklisted_count
            if blacklisted_count > 0 and clean_count < MIN_VOTES:
                _log.debug(f"[SE] {symbol} KILL: {blacklisted_count} blacklisted patterns, "
                           f"only {clean_count} clean (need {MIN_VOTES})")
                return None

        rsi_short_ceil = float(effective_config.get('rsi_short_floor', 50))

        if long_votes >= MIN_VOTES and long_votes >= short_votes + MIN_LEAD:
            # Skip mid-RSI longs — journal: losers avg RSI 53.8, winners 64.7
            if rsi < rsi_long_floor and 'rsi_oversold_vol_surge' not in all_patterns:
                _log.debug(f"[SE] {symbol} KILL: RSI={rsi:.0f} < {rsi_long_floor} (votes={long_votes}L/{short_votes}S)")
                return None
            direction = "long"
            dominant_votes = long_votes
        elif short_votes >= MIN_VOTES and short_votes >= long_votes + MIN_LEAD:
            if disable_shorts:
                _log.debug(f"[SE] {symbol} KILL: shorts disabled (votes={long_votes}L/{short_votes}S)")
                return None
            # Only block shorts at extreme oversold (genuine bounce risk at RSI < 30)
            # RSI 30-50 is BEARISH territory — shorts should work there
            if rsi < rsi_short_ceil and 'rsi_overbought_vol_surge' not in all_patterns:
                _log.debug(f"[SE] {symbol} KILL short: RSI={rsi:.0f} < {rsi_short_ceil} (extreme oversold bounce risk)")
                return None
            direction = "short"
            dominant_votes = short_votes
        else:
            # Log near-miss: had votes but not enough
            if long_votes >= 3 or short_votes >= 3:
                _log.debug(f"[SE] {symbol} KILL: votes={long_votes}L/{short_votes}S need={MIN_VOTES}/{MIN_LEAD}lead pat={all_patterns[:4]}")
            return None

        # ── Zone-location gate: PROPER entries only ──────────────────────
        # The ICICIPRULI/UPL problem: EMA-cross + supertrend + range-filter +
        # WAE can stack 6+ votes while price chops in no-man's-land between
        # zones. Those indicators confirm DIRECTION but not LOCATION — in a
        # range they whipsaw and every entry is a loss.
        #
        # Fix: a signal must be anchored to a structural location —
        #   • at a demand/supply zone (bounce / rejection)
        #   • a break of structure THROUGH a zone (BOS)
        #   • a retest of a broken zone
        #   • a trend pullback to EMA21
        #   • a clean horizontal / consolidation / flag / NR7 break
        # Pure indicator confluence with NO location anchor = kill.
        # Top movers bypass (momentum days run without retesting zones).
        require_zone = bool(effective_config.get('require_zone_anchor', True))
        if require_zone and mover_class == 'normal':
            long_anchors = {
                'demand_zone_bounce', 'supply_zone_bos_up',
                'broken_supply_retest_long', 'ema21_pullback_long',
                'trend_up', 'horizontal_breakout_up', 'consol_breakout_up',
                'bull_flag_breakout', 'nr7_breakout_up', 'price_breakout_up',
                # Breakout indicators the engine actually emits:
                'vwap_breakout_up', 'wae_bull_explosion', 'range_filter_up',
                'ema_bullish_cross', 'ema_stack_aligned_bull',
            }
            short_anchors = {
                'supply_zone_rejection', 'demand_zone_bos_down',
                'broken_demand_retest_short', 'ema21_pullback_short',
                'trend_down', 'horizontal_breakout_down', 'consol_breakout_down',
                'bear_flag_breakout', 'nr7_breakout_down', 'price_breakout_down',
                # Breakout indicators the engine actually emits:
                'vwap_breakout_down', 'wae_bear_explosion', 'range_filter_down',
                'ema_bearish_cross', 'ema_stack_aligned_bear',
            }
            anchors = long_anchors if direction == 'long' else short_anchors
            if not (anchors & set(all_patterns)):
                _log.debug(
                    f"[SE] {symbol} KILL: no zone/structure anchor for {direction} "
                    f"(votes={long_votes}L/{short_votes}S, mid-range chop) "
                    f"pat={all_patterns[:5]}"
                )
                return None

        # Pick primary structure label
        primary = structure
        for check, struct in [
            ('nr7_breakout', MarketStructure.NR7_BREAKOUT),
            ('inside_bar', MarketStructure.INSIDE_BAR_BREAKOUT),
            ('ema21_pullback', MarketStructure.EMA_PULLBACK),
            ('vwap_breakout', MarketStructure.VWAP_BREAKOUT),
            ('ema_bullish_cross', MarketStructure.EMA_CROSSOVER),
            ('ema_bearish_cross', MarketStructure.EMA_CROSSOVER),
            ('rsi_oversold', MarketStructure.RSI_REVERSAL),
        ]:
            if any(check in p for p in all_patterns):
                primary = struct
                break
        if primary in (MarketStructure.UNKNOWN, MarketStructure.RANGE, MarketStructure.CONSOLIDATION):
            primary = MarketStructure.BREAKOUT

        # ATR stop — use effective_config so top-mover override applies here too.
        atr_mult = effective_config.get('atr_multiplier', 1.5)
        if direction == "long":
            atr_price = current_price - atr * atr_mult
        else:
            atr_price = current_price + atr * atr_mult

        # Strength: base + confluence bonus
        # candle_str capped at 2.0 ATRs (beyond that, outlier candle not more meaningful)
        candle_str = min(abs(df['close'].iloc[-1] - df['open'].iloc[-1]) / atr, 2.0) if atr > 0 else 0
        vol_score = min(vol_ratio / self.config.get('volume_multiplier', 1.5), 1.0)
        struct_score = {
            MarketStructure.BREAKOUT: 0.90, MarketStructure.TREND_UP: 0.80,
            MarketStructure.TREND_DOWN: 0.80, MarketStructure.NR7_BREAKOUT: 0.88,
            MarketStructure.INSIDE_BAR_BREAKOUT: 0.82, MarketStructure.EMA_PULLBACK: 0.78,
            MarketStructure.VWAP_BREAKOUT: 0.82, MarketStructure.EMA_CROSSOVER: 0.76,
            MarketStructure.RSI_REVERSAL: 0.72,
        }.get(primary, 0.50)

        # Scale 0→1 as votes go from MIN_VOTES to MIN_VOTES+10 (was capped at 0.18, had <3% impact)
        confluence_bonus = min((dominant_votes - MIN_VOTES) / 10.0, 1.0)
        strength = min(
            (candle_str * 0.20 + vol_score * 0.25 + struct_score * 0.20 + confluence_bonus * 0.35) * 100,
            100
        )

        min_strength = effective_config.get('min_strength', 35)
        if strength < min_strength:
            _log.debug(f"[SE] {symbol} KILL: strength={strength:.0f} < {min_strength}")
            return None

        reason = (f"{primary.value} | patterns=[{','.join(all_patterns[:4])}] "
                  f"RSI:{rsi:.0f} Vol:{vol_ratio:.1f}x Votes:{long_votes}L/{short_votes}S")

        return Signal(
            symbol=symbol,
            direction=direction,
            structure=primary,
            volatility=volatility,
            entry_price=current_price,
            atr=atr,
            atr_price=atr_price,
            volume_ratio=vol_ratio,
            strength=strength,
            candles_since_breakout=self.find_candles_since_breakout(df),
            reason=reason,
            rsi=rsi,
            ema9=ema9_val,
            ema21=ema21_val,
            vwap=vwap,
            patterns=all_patterns,
            long_votes=long_votes,
            short_votes=short_votes,
        )

    def scan_symbols(self, symbols: List[str], get_data_func) -> List[Signal]:
        signals = []
        for symbol in symbols:
            try:
                df = get_data_func(symbol)
                if df is None or df.empty:
                    continue
                signal = self.generate_signal(symbol, df)
                if signal:
                    signals.append(signal)
            except Exception:
                continue

        signals.sort(key=lambda x: x.strength, reverse=True)
        return signals


if __name__ == '__main__':
    # dev smoke block — see git history for previous content
    pass
