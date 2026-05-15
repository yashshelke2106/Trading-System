import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass
from enum import Enum
import config


class OrderFlowType(Enum):
    AGGRESSION = "aggression"
    ABSORPTION = "absorption"
    EXHAUSTION = "exhaustion"
    NEUTRAL = "neutral"


@dataclass
class OrderFlowAnalysis:
    flow_type: OrderFlowType
    strength: float
    absorption_count: int
    exhaustion_indicators: List[str]
    recommendation: str


class OrderFlowAnalyzer:
    def __init__(self):
        self.config = config.ORDER_FLOW_CONFIG

    def calculate_buy_sell_pressure(self, df: pd.DataFrame) -> Tuple[float, float]:
        if len(df) < 5:
            return 0.5, 0.5

        recent = df.tail(10)
        buy_pressure, sell_pressure = 0.0, 0.0

        for _, row in recent.iterrows():
            body = row['close'] - row['open']
            volume = row['volume']
            range_size = row['high'] - row['low']

            if range_size == 0:
                continue

            position = body / range_size
            if body > 0:
                buy_pressure += volume * position
            else:
                sell_pressure += volume * abs(position)

        total = buy_pressure + sell_pressure
        if total == 0:
            return 0.5, 0.5

        return buy_pressure / total, sell_pressure / total

    def detect_absorption(self, df: pd.DataFrame, direction: str = "long") -> Tuple[int, bool]:
        """Detect absorption — direction-aware.

        For longs:  sellers absorbing at HIGHS  → upper wick rejection = caution (top)
        For shorts: buyers absorbing at LOWS   → lower wick rejection = caution (bottom)

        Returns (rejection_count, is_absorbing).
        """
        if len(df) < 10:
            return 0, False

        absorption_count = 0

        for i in range(len(df) - 1, max(len(df) - 10, 0), -1):
            current = df.iloc[i]

            upper_wick = current['high'] - max(current['open'], current['close'])
            lower_wick = min(current['open'], current['close']) - current['low']
            body = abs(current['close'] - current['open'])
            total_range = current['high'] - current['low']

            if total_range == 0:
                continue

            wick_ratio_threshold = self.config.get('absorption_wick_ratio', 3)

            if direction == "long":
                # Sellers absorbing buy orders at resistance → caution for longs
                wick = upper_wick
                close_lt_open = current['close'] < current['open']
                close_le_open = current['close'] <= current['open']
            else:
                # Buyers absorbing sell orders at support → caution for shorts
                wick = lower_wick
                close_lt_open = current['close'] > current['open']
                close_le_open = current['close'] >= current['open']

            if body > 0:
                is_rejection = (wick / body > wick_ratio_threshold and close_lt_open)
            else:
                is_rejection = (wick / total_range > 0.40 and close_le_open)
            if is_rejection:
                absorption_count += 1

        min_count = self.config.get('min_absorption_count', 3)
        return absorption_count, absorption_count >= min_count

    def detect_exhaustion(self, df: pd.DataFrame, direction: str = "long") -> List[str]:
        """Detect exhaustion — direction-aware.

        Returns indicators that the signal direction is about to fail.
          - For longs:  bearish exhaustion (rally fading)
          - For shorts: bullish exhaustion (decline fading)
        """
        indicators = []

        if len(df) < 10:
            return indicators

        # 1. Volume spike without meaningful price progress — applies to BOTH directions
        recent_vol = df['volume'].tail(5).mean()
        older_vol = df['volume'].rolling(10).mean().iloc[-1]
        recent_price_change = abs(df['close'].iloc[-1] - df['close'].iloc[-5]) / df['close'].iloc[-5]

        if (recent_vol > older_vol * self.config['exhaustion_volume_ratio']
                and recent_price_change < 0.005):
            indicators.append("High volume without price progress")

        # 2. Compressed price range (indecision / balance) — direction-neutral
        last_3 = df.tail(3)
        price_range = (last_3['high'].max() - last_3['low'].min()) / df['close'].iloc[-1]
        if price_range < 0.008:
            indicators.append("Compressed price range")

        # 3. Extreme ONE-SIDED pressure — symmetric (each direction flags pressure
        #    that contradicts its own thesis)
        buy_ratio, sell_ratio = self.calculate_buy_sell_pressure(df)
        if direction == "long" and sell_ratio > 0.85:
            indicators.append("Overwhelming sell pressure (rally exhausting)")
        elif direction == "short" and buy_ratio > 0.85:
            indicators.append("Overwhelming buy pressure (decline exhausting)")

        # 4. Price-volume DIVERGENCE — symmetric
        recent = df.tail(5)
        closes = recent['close'].values
        volumes = recent['volume'].values

        consecutive_up = all(closes[i] >= closes[i-1] for i in range(1, len(closes)))
        consecutive_down = all(closes[i] <= closes[i-1] for i in range(1, len(closes)))
        volume_declining = all(volumes[i] <= volumes[i-1] for i in range(1, len(volumes)))

        if direction == "long" and consecutive_up and volume_declining:
            indicators.append("Bearish divergence: up closes with declining volume")
        elif direction == "short" and consecutive_down and volume_declining:
            indicators.append("Bullish divergence: down closes with declining volume")

        return indicators

    def analyze(self, df: pd.DataFrame, direction: str = "long") -> OrderFlowAnalysis:
        buy_ratio, sell_ratio = self.calculate_buy_sell_pressure(df)
        absorption_count, is_absorbing = self.detect_absorption(df, direction)
        exhaustion_indicators = self.detect_exhaustion(df, direction)

        if direction == "long":
            pressure_ratio = buy_ratio
        else:
            pressure_ratio = sell_ratio

        # Strong absorption is a caution signal — direction-aware (top for longs, bottom for shorts)
        if absorption_count >= self.config.get('min_absorption_count', 3):
            flow_type = OrderFlowType.ABSORPTION
            strength = min(absorption_count / 10, 1.0)
            loc = "highs" if direction == "long" else "lows"
            recommendation = f"Caution - absorption at {loc} detected"

        elif len(exhaustion_indicators) >= 2:
            flow_type = OrderFlowType.EXHAUSTION
            strength = len(exhaustion_indicators) / 5
            recommendation = "Avoid - exhaustion: " + "; ".join(exhaustion_indicators[:2])

        elif pressure_ratio > 0.62:
            flow_type = OrderFlowType.AGGRESSION
            strength = pressure_ratio
            recommendation = f"Trade {'LONG' if direction == 'long' else 'SHORT'} - aggressive flow"

        else:
            flow_type = OrderFlowType.NEUTRAL
            strength = 0.5
            recommendation = "Neutral - wait for clarity"

        return OrderFlowAnalysis(
            flow_type=flow_type,
            strength=strength,
            absorption_count=absorption_count,
            exhaustion_indicators=exhaustion_indicators,
            recommendation=recommendation,
        )

    def rank_signals(self, signals: List, get_data_func) -> List[Dict]:
        ranked = []

        for item in signals:
            signal = item.get('signal')
            if not signal:
                continue

            df = get_data_func(signal.symbol)
            if df is None or df.empty:
                continue

            analysis = self.analyze(df, signal.direction)
            item['order_flow'] = analysis
            ranked.append(item)

        priority = {
            OrderFlowType.AGGRESSION: 3,
            OrderFlowType.NEUTRAL: 2,
            OrderFlowType.EXHAUSTION: 1,
            OrderFlowType.ABSORPTION: 0,
        }
        ranked.sort(key=lambda x: priority.get(
            x.get('order_flow', OrderFlowAnalysis(OrderFlowType.NEUTRAL, 0.5, 0, [], '')).flow_type,
            0), reverse=True)

        return ranked


if __name__ == '__main__':
    from scanner import LiquidityScanner
    from signal_engine import SignalEngine
    from fake_breakout_filter import FakeBreakoutFilter

    scanner = LiquidityScanner()
    engine = SignalEngine()
    fb_filter = FakeBreakoutFilter()
    of_analyzer = OrderFlowAnalyzer()

    scanner.scan_universe()

    print("\nOrder Flow Analysis...")
    for item in scanner.universe[:10]:
        signal = engine.generate_signal(item['symbol'], scanner.get_market_data(item['symbol']))
        if signal:
            df = scanner.get_market_data(item['symbol'])
            of_result = of_analyzer.analyze(df, signal.direction)
            print(f"\n{signal.symbol}: {of_result.flow_type.value}")
            print(f"  Recommendation: {of_result.recommendation}")
            if of_result.exhaustion_indicators:
                print(f"  Warnings: {of_result.exhaustion_indicators}")
