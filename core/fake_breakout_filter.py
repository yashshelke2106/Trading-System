import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass
from enum import Enum
import config
from .signal_engine import Signal, MarketStructure, VolatilityRegime


class BreakoutQuality(Enum):
    REAL = "real"
    FAKE = "fake"
    WEAK = "weak"


@dataclass
class FilterResult:
    quality: BreakoutQuality
    is_valid: bool
    confidence: float
    reasons: list


class FakeBreakoutFilter:
    def __init__(self):
        self.config = config.FILTER_CONFIG

    def check_candle_strength(self, df: pd.DataFrame) -> Tuple[bool, float]:
        if len(df) < 3:
            return False, 0
        current = df.iloc[-1]
        body = abs(current['close'] - current['open'])
        total_range = current['high'] - current['low']
        if total_range == 0:
            return False, 0

        body_ratio = body / total_range
        upper_wick = current['high'] - max(current['open'], current['close'])
        lower_wick = min(current['open'], current['close']) - current['low']
        wick_top_ratio = upper_wick / body if body > 0 else 1
        wick_bot_ratio = lower_wick / body if body > 0 else 1

        avg_range = (df['high'] - df['low']).rolling(5).mean().iloc[-1]
        size_vs_avg = total_range / avg_range if avg_range > 0 else 0

        score = 0.0
        if body_ratio > 0.65:
            score += 0.40
        elif body_ratio > 0.45:
            score += 0.20

        if wick_top_ratio < self.config['rejection_wick_ratio']:
            score += 0.20
        if wick_bot_ratio < self.config['rejection_wick_ratio']:
            score += 0.15

        if size_vs_avg >= 1.5:
            score += 0.20
        elif size_vs_avg >= 1.0:
            score += 0.10
        elif size_vs_avg >= self.config['min_candle_size']:
            score += 0.03

        return score >= 0.55, min(score, 1.0)

    def check_follow_through(self, df: pd.DataFrame) -> Tuple[bool, float]:
        if len(df) < 5:
            return False, 0

        current = df.iloc[-1]
        current_direction = 1 if current['close'] > current['open'] else -1
        prev_candles = df.iloc[-4:-1]

        consecutive = 0
        for i in range(len(prev_candles) - 1, -1, -1):
            c_dir = 1 if prev_candles.iloc[i]['close'] > prev_candles.iloc[i]['open'] else -1
            if c_dir == current_direction:
                consecutive += 1
            else:
                break

        avg_volume = df['volume'].iloc[:-1].rolling(5).mean().iloc[-1]
        current_volume = df['volume'].iloc[-1]
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

        # Volume should be INCREASING during follow-through (confirms momentum)
        vol_increasing = volume_ratio > 1.0

        follow_score = consecutive * 0.25
        follow_score += min(volume_ratio / 2.5, 0.4)
        if vol_increasing and consecutive >= 1:
            follow_score += 0.15

        return follow_score >= 0.30, min(follow_score, 1.0)

    def check_rejection(self, df: pd.DataFrame) -> Tuple[bool, float]:
        if len(df) < 10:
            return True, 0.5

        recent = df.tail(10)
        rejections = 0

        for i in range(2, len(recent)):
            candle = recent.iloc[-i]
            body = abs(candle['close'] - candle['open'])
            total_range = candle['high'] - candle['low']
            if total_range == 0:
                continue
            wick_to_body = (
                (candle['high'] - max(candle['open'], candle['close'])) +
                (min(candle['open'], candle['close']) - candle['low'])
            ) / body if body > 0 else 2

            if wick_to_body > 1.5:
                rejections += 1

        # Fewer rejections = cleaner breakout
        if rejections <= 1:
            score = 0.9
        elif rejections <= 3:
            score = 0.7
        elif rejections <= 5:
            score = 0.5
        else:
            score = 0.3

        # Volume declining recently is a caution
        recent_vol = df['volume'].tail(5).mean()
        older_vol = df['volume'].tail(10).mean()
        if recent_vol < older_vol * 0.8:
            score -= 0.1

        return rejections <= 4, max(0.0, min(score, 1.0))

    def check_volume_profile(self, df: pd.DataFrame) -> Tuple[bool, float]:
        if len(df) < 20:
            return True, 0.5

        current_vol = df['volume'].iloc[-1]
        avg_vol_20 = df['volume'].rolling(20).mean().iloc[-1]
        avg_vol_5 = df['volume'].rolling(5).mean().iloc[-1]

        vol_ratio = current_vol / avg_vol_20 if avg_vol_20 > 0 else 0
        vol_momentum = avg_vol_5 / avg_vol_20 if avg_vol_20 > 0 else 0

        score = 0.0
        if vol_ratio >= 2.0:     # 2x+ = confirmed inflow surge
            score += 0.55
        elif vol_ratio > 1.5:
            score += 0.35
        elif vol_ratio > 1.2:
            score += 0.15
        elif vol_ratio > 1.0:
            score += 0.05

        if vol_momentum > 1.15:
            score += 0.25
        elif vol_momentum > 0.95:
            score += 0.10

        # Volume building in last 3 bars = breakout accumulation
        recent = df.tail(3)
        vol_building = all(
            recent.iloc[i]['volume'] >= recent.iloc[i-1]['volume']
            for i in range(1, len(recent))
        )
        if vol_building:
            score += 0.20

        return score >= 0.40, min(score, 1.0)

    def check_vwap_position(self, df: pd.DataFrame, direction: str) -> Tuple[bool, float]:
        """Breakouts in VWAP direction are more reliable."""
        if len(df) < 5:
            return True, 0.5

        # Intraday-aware: use only current day's bars for VWAP
        if hasattr(df.index, 'date'):
            day_series = pd.Series([d.date() for d in df.index], index=df.index)
        elif 'date' in df.columns:
            day_series = pd.to_datetime(df['date']).dt.date
            day_series.index = df.index
        else:
            day_series = None

        if day_series is not None and day_series.nunique() > 1:
            today = day_series.iloc[-1]
            df = df[day_series == today]
            if len(df) < 2:
                return True, 0.5

        tp = (df['high'] + df['low'] + df['close']) / 3
        total_vol = df['volume'].sum()
        if total_vol == 0:
            return True, 0.5
        vwap = float((tp * df['volume']).sum() / total_vol)
        current_price = float(df['close'].iloc[-1])
        deviation = (current_price - vwap) / vwap * 100

        if direction == "long":
            if deviation > 0.5:
                return True, 0.9
            elif deviation > -0.3:
                return True, 0.7
            else:
                # Price well below VWAP = weak long setup
                return False, 0.4
        else:
            if deviation < -0.5:
                return True, 0.9
            elif deviation < 0.3:
                return True, 0.7
            else:
                # Price well above VWAP = weak short setup
                return False, 0.4

    def check_volume_dry_up(self, df: pd.DataFrame) -> Tuple[bool, float]:
        """Real breakouts often have volume compression before explosive expansion.
        Look for 2-3 low-volume bars followed by high-volume breakout bar."""
        if len(df) < 6:
            return False, 0.5

        avg_vol = df['volume'].rolling(20).mean().iloc[-1]
        if avg_vol == 0:
            return False, 0.5

        # Check pre-breakout volume compression (bars -4 to -2)
        pre_vols = df['volume'].iloc[-4:-1].values
        breakout_vol = df['volume'].iloc[-1]

        pre_ratio = np.mean(pre_vols) / avg_vol
        breakout_ratio = breakout_vol / avg_vol

        # Pattern: pre-breakout volume < 80% of avg, then explosion >= 1.5x
        dry_up_present = pre_ratio < 0.85
        vol_expansion = breakout_ratio >= 1.5

        if dry_up_present and vol_expansion:
            return True, 0.95
        elif vol_expansion:
            return True, 0.75
        else:
            return False, 0.40

    def check_stop_hunt(self, df: pd.DataFrame, direction: str) -> Tuple[bool, float]:
        """Detect stop-hunt pattern: quick spike through key level then reversal.
        For longs: spike below recent support that quickly recovers above.
        Penalize this as it may be a false break."""
        if len(df) < 5:
            return False, 0.5

        recent_low = df['low'].iloc[-5:-1].min()
        recent_high = df['high'].iloc[-5:-1].max()
        current = df.iloc[-1]

        if direction == "long":
            # Did last bar spike below the recent 5-bar low then close above?
            spike_below = current['low'] < recent_low
            recovered = current['close'] > recent_low
            if spike_below and recovered:
                # This is actually a stop-hunt and bullish spring pattern
                # — it's a BUY signal, not a penalty
                return True, 0.85
        else:
            spike_above = current['high'] > recent_high
            recovered = current['close'] < recent_high
            if spike_above and recovered:
                return True, 0.85

        return False, 0.50

    def analyze(self, df: pd.DataFrame, signal: Signal) -> FilterResult:
        reasons = []
        direction = getattr(signal, 'direction', 'long')

        # Hard gate: require 2x average volume at entry (no fake breakouts)
        min_vol_ratio = config.VOLUME_EXIT_CONFIG.get('entry_min_vol_ratio', 2.0) if hasattr(config, 'VOLUME_EXIT_CONFIG') else 2.0
        if len(df) >= 20:
            avg_vol_20 = float(df['volume'].rolling(20).mean().iloc[-1])
            current_vol = float(df['volume'].iloc[-1])
            hard_vol_ratio = current_vol / avg_vol_20 if avg_vol_20 > 0 else 0.0
        else:
            hard_vol_ratio = 0.0

        if hard_vol_ratio < min_vol_ratio:
            return FilterResult(
                quality=BreakoutQuality.FAKE,
                is_valid=False,
                confidence=0.0,
                reasons=[f"Volume insufficient: {hard_vol_ratio:.1f}x < {min_vol_ratio}x required"],
            )

        candle_valid, candle_score = self.check_candle_strength(df)
        follow_valid, follow_score = self.check_follow_through(df)
        rejection_valid, rejection_score = self.check_rejection(df)
        volume_valid, volume_score = self.check_volume_profile(df)
        vwap_valid, vwap_score = self.check_vwap_position(df, direction)
        dry_up_valid, dry_up_score = self.check_volume_dry_up(df)
        stop_hunt, sh_score = self.check_stop_hunt(df, direction)

        if not candle_valid:
            reasons.append(f"Weak candle body ({candle_score:.2f})")
        if not follow_valid:
            reasons.append(f"No follow-through ({follow_score:.2f})")
        if not rejection_valid:
            reasons.append(f"Many wicks/rejections ({rejection_score:.2f})")
        if not volume_valid:
            reasons.append(f"Volume not confirming ({volume_score:.2f})")
        if not vwap_valid:
            reasons.append(f"Price against VWAP ({vwap_score:.2f})")

        dry_boost = 0.05 if dry_up_valid else 0
        sh_boost = 0.03 if stop_hunt else 0

        avg_score = (candle_score * 0.30 +
                     follow_score * 0.25 +
                     rejection_score * 0.15 +
                     volume_score * 0.20 +
                     vwap_score * 0.10) + dry_boost + sh_boost

        # Stricter gate — both follow-through AND volume must confirm (was OR).
        # Journal showed losers had vol_ratio 1.66x; volume alone insufficient.
        if avg_score >= 0.70 and candle_valid and follow_valid and volume_valid and vwap_valid:
            quality = BreakoutQuality.REAL
            is_valid = True
        else:
            quality = BreakoutQuality.FAKE
            is_valid = False
            reasons.append(f"Score {avg_score:.2f} < 0.70 or required check failed")

        return FilterResult(
            quality=quality,
            is_valid=is_valid,
            confidence=min(avg_score, 1.0),
            reasons=reasons if reasons else ["Passed all filters"],
        )

    def filter_signals(self, signals: list, get_data_func) -> list:
        filtered = []
        for item in signals:
            is_dict = isinstance(item, dict)
            signal = item['signal'] if is_dict else item
            try:
                df = get_data_func(signal.symbol)
                if df is None or df.empty:
                    continue
                result = self.analyze(df, signal)
                if result.is_valid:
                    if is_dict:
                        filtered.append({**item, 'filter_result': result})
                    else:
                        filtered.append({'signal': signal, 'filter_result': result})
            except Exception:
                continue
        return filtered


if __name__ == '__main__':
    from scanner import LiquidityScanner
    from signal_engine import SignalEngine

    scanner = LiquidityScanner()
    engine = SignalEngine()
    filter_obj = FakeBreakoutFilter()

    scanner.scan_universe()

    print("\nApplying fake breakout filter...")
    for item in scanner.universe[:10]:
        signal = engine.generate_signal(item['symbol'], scanner.get_market_data(item['symbol']))
        if signal:
            result = filter_obj.analyze(scanner.get_market_data(item['symbol']), signal)
            status = "PASS" if result.is_valid else "FAIL"
            print(f"\n{signal.symbol}: {status} | Quality:{result.quality.value} | Conf:{result.confidence:.0%}")
            print(f"  Reasons: {', '.join(result.reasons)}")
