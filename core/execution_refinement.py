import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
from enum import Enum


class EntryRefinement(Enum):
    DIRECT = "direct"
    PULLBACK = "pullback"
    WAIT_VWAP = "wait_vwap"
    AVOID = "avoid"


@dataclass
class EntryContext:
    refinement: EntryRefinement
    entry_price: float
    wait_candles: int
    confidence: float
    reason: str


class ExecutionRefiner:
    def __init__(self):
        self.config = {
            'pullback_threshold': 0.9,
            'vwap_tolerance': 0.002,
            'retest_candles': 3,
        }

    def calculate_vwap(self, df: pd.DataFrame) -> float:
        if len(df) < 5:
            return df['close'].iloc[-1]

        # Intraday-aware: if multiple trading days present, use only today's bars
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
                return df['close'].iloc[-1]

        typical_price = (df['high'] + df['low'] + df['close']) / 3
        volume = df['volume']

        vwap = (typical_price * volume).sum() / volume.sum()

        return vwap

    def detect_pullback(self, df: pd.DataFrame, direction: str) -> Tuple[bool, float]:
        if len(df) < 5:
            return False, 0
        
        atr = self._calculate_atr(df)
        threshold = atr * self.config['pullback_threshold']
        
        if direction == "long":
            recent_high = df['high'].iloc[-1]
            prev_high = df['high'].iloc[-2]
            
            if df['close'].iloc[-1] < df['close'].iloc[-2]:
                pullback_size = prev_high - df['low'].iloc[-1]
                
                if pullback_size < threshold and pullback_size > 0:
                    return True, pullback_size / atr
        else:
            recent_low = df['low'].iloc[-1]
            prev_low = df['low'].iloc[-2]
            
            if df['close'].iloc[-1] > df['close'].iloc[-2]:
                pullback_size = df['high'].iloc[-1] - prev_low
                
                if pullback_size < threshold and pullback_size > 0:
                    return True, pullback_size / atr
        
        return False, 0

    def check_vwap_alignment(self, df: pd.DataFrame, direction: str) -> Tuple[bool, float]:
        if len(df) < 5:
            return False, 0.5
        
        vwap = self.calculate_vwap(df)
        current = df['close'].iloc[-1]
        
        vwap_distance = abs(current - vwap) / current
        
        if direction == "long":
            above_vwap = current > vwap
            close_to_vwap = vwap_distance < self.config['vwap_tolerance']
            
            return above_vwap or close_to_vwap, vwap_distance
        else:
            below_vwap = current < vwap
            close_to_vwap = vwap_distance < self.config['vwap_tolerance']
            
            return below_vwap or close_to_vwap, vwap_distance

    def detect_retest(self, df: pd.DataFrame, level: float) -> int:
        retests = 0
        
        for i in range(len(df) - 1, max(0, len(df) - self.config['retest_candles'] - 1), -1):
            if df['high'].iloc[i] >= level >= df['low'].iloc[i]:
                retests += 1
            elif abs(df['close'].iloc[i] - level) / level < 0.001:
                retests += 1
        
        return retests

    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        if len(df) < period + 1:
            return df['close'].iloc[-1] * 0.02
        
        high = df['high']
        low = df['low']
        close = df['close']
        
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        
        return atr if not pd.isna(atr) else df['close'].iloc[-1] * 0.02

    def refine_entry(self, df: pd.DataFrame, signal, base_entry: float) -> EntryContext:
        direction = signal.direction
        
        vwap_ok, vwap_dist = self.check_vwap_alignment(df, direction)
        
        pullback, pullback_strength = self.detect_pullback(df, direction)
        
        current = df['close'].iloc[-1]
        atr = self._calculate_atr(df)
        
        if not vwap_ok:
            return EntryContext(
                refinement=EntryRefinement.WAIT_VWAP,
                entry_price=base_entry,
                wait_candles=2,
                confidence=0.5,
                reason=f"Price {vwap_dist*100:.2f}% from VWAP - wait"
            )
        
        if pullback and vwap_ok:
            pullback_entry = current
            
            return EntryContext(
                refinement=EntryRefinement.PULLBACK,
                entry_price=pullback_entry,
                wait_candles=1,
                confidence=0.8,
                reason=f"Pullback entry ({pullback_strength:.2f} ATR)"
            )
        
        if vwap_ok:
            return EntryContext(
                refinement=EntryRefinement.DIRECT,
                entry_price=current,
                wait_candles=0,
                confidence=0.9,
                reason="Clean VWAP alignment - direct entry"
            )
        
        return EntryContext(
            refinement=EntryRefinement.AVOID,
            entry_price=base_entry,
            wait_candles=0,
            confidence=0,
            reason="No valid entry setup"
        )

    def get_entry_multiplier(self, refinement: EntryRefinement) -> float:
        multipliers = {
            EntryRefinement.DIRECT: 1.0,
            EntryRefinement.PULLBACK: 1.1,
            EntryRefinement.WAIT_VWAP: 0.9,
            EntryRefinement.AVOID: 0.0,
        }
        return multipliers.get(refinement, 0.5)

    def should_wait_for_entry(self, refinement: EntryRefinement) -> bool:
        return refinement in [EntryRefinement.WAIT_VWAP, EntryRefinement.AVOID]


class MultiTimeframeAnalyzer:
    def __init__(self):
        self.config = {
            'daily_trend_period': 20,
            'swing_trend_period': 50,
        }

    def get_daily_trend(self, df: pd.DataFrame) -> str:
        if len(df) < self.config['daily_trend_period']:
            return "neutral"
        
        sma20 = df['close'].rolling(20).mean().iloc[-1]
        sma50 = df['close'].rolling(50).mean().iloc[-1]
        current = df['close'].iloc[-1]
        
        if current > sma20 > sma50:
            return "strong_up"
        elif current > sma20:
            return "up"
        elif current < sma20 < sma50:
            return "strong_down"
        elif current < sma20:
            return "down"
        else:
            return "neutral"

    def get_trend_alignment(self, intraday_df: pd.DataFrame,
                           daily_df: pd.DataFrame) -> Tuple[str, float]:
        intraday_trend = self.get_daily_trend(intraday_df)
        daily_trend = self.get_daily_trend(daily_df)

        aligned_scores = {
            ('strong_up', 'strong_up'): 1.0,
            ('strong_up', 'up'): 0.9,
            ('up', 'strong_up'): 0.8,
            ('up', 'up'): 0.75,
            ('strong_down', 'strong_down'): 1.0,
            ('strong_down', 'down'): 0.9,
            ('down', 'strong_down'): 0.8,
            ('down', 'down'): 0.75,
        }

        if (intraday_trend, daily_trend) in aligned_scores:
            return intraday_trend, aligned_scores[(intraday_trend, daily_trend)]

        if intraday_trend == 'neutral' or daily_trend == 'neutral':
            return intraday_trend, 0.5

        return intraday_trend, 0.5

    def adjust_for_htf(self, signal_direction: str, htf_trend: str) -> Tuple[bool, float]:
        direction_map = {'long': 1, 'short': -1, 'up': 1, 'down': -1}

        signal_sign = direction_map.get(signal_direction, 0)
        trend_sign = direction_map.get(htf_trend, 0)

        if signal_sign == 0 or trend_sign == 0:
            return True, 0.5

        if signal_sign == trend_sign:
            return True, 1.0

        return False, 0.3


if __name__ == '__main__':
    import sys
    sys.path.insert(0, '.')
    from core.scanner import LiquidityScanner
    from core.signal_engine import SignalEngine
    
    scanner = LiquidityScanner()
    engine = SignalEngine()
    refiner = ExecutionRefiner()
    mtf = MultiTimeframeAnalyzer()
    
    scanner.scan_universe()
    
    print("Execution Refinement Analysis:")
    for item in scanner.universe[:3]:
        df = scanner.get_market_data(item['symbol'])
        signal = engine.generate_signal(item['symbol'], df)
        
        if signal:
            context = refiner.refine_entry(df, signal, signal.entry_price)
            
            print(f"\n{item['symbol']}: {signal.direction}")
            print(f"  Refinement: {context.refinement.value}")
            print(f"  Entry: {context.entry_price:.2f}")
            print(f"  Wait Candles: {context.wait_candles}")
            print(f"  Confidence: {context.confidence:.0%}")
            print(f"  Reason: {context.reason}")
            
            vwap = refiner.calculate_vwap(df)
            print(f"  VWAP: {vwap:.2f}")