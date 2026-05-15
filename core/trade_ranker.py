import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as cfg


@dataclass
class TradeScore:
    symbol: str
    total_score: float
    components: Dict[str, float]
    rank: int
    verdict: str
    reasons: List[str]


class TradeRanker:
    def __init__(self):
        # Weights sum to 1.0; sector_momentum absorbed from liquidity/order_flow
        self.weights = {
            'signal_strength':  0.23,
            'order_flow':       0.18,
            'breakout_quality': 0.16,
            'liquidity':        0.13,
            'market_bias':      0.10,
            'sector_momentum':  0.10,
            'volatility':       0.05,
            'time_filter':      0.05,
        }

        self.thresholds = {
            'excellent': 0.72,
            'good': 0.55,
            'average': 0.40,
            'poor': 0.25,
        }

    def score_liquidity(self, metrics: Dict) -> float:
        # Keys match LiquidityScanner.raw_metrics: adv, avg_delivery, volume_trend
        volume_score   = min(metrics.get('adv', 0) / 10_000_000, 1.0)
        delivery_score = min(metrics.get('avg_delivery', 0) / 30, 1.0)
        trend_score    = min(metrics.get('volume_trend', 1.0) / 2.0, 1.0)
        return volume_score * 0.4 + delivery_score * 0.3 + trend_score * 0.3

    def score_signal_strength(self, signal) -> float:
        strength = getattr(signal, 'strength', 0) / 100

        structure_mult = 1.0
        if hasattr(signal, 'structure'):
            struct_str = str(signal.structure).lower()
            if 'nr7' in struct_str or 'inside_bar' in struct_str:
                structure_mult = 1.25
            elif 'breakout' in struct_str or 'vwap' in struct_str:
                structure_mult = 1.20
            elif 'pullback' in struct_str or 'crossover' in struct_str:
                structure_mult = 1.15
            elif 'trend' in struct_str:
                structure_mult = 1.10

        # Multi-pattern confluence bonus
        patterns = getattr(signal, 'patterns', [])
        long_votes = getattr(signal, 'long_votes', 0)
        short_votes = getattr(signal, 'short_votes', 0)
        max_votes = max(long_votes, short_votes)
        confluence_bonus = min((max_votes - 3) / 10.0, 1.0) * 0.20

        return min(strength * structure_mult + confluence_bonus, 1.0)

    def score_volatility(self, vol_data: Dict) -> float:
        if not vol_data:
            return 0.5
        
        regime = vol_data.get('regime', 'normal')
        
        if regime == 'low':
            return 0.3
        elif regime == 'high':
            return 0.7
        else:
            return 1.0

    def score_order_flow(self, of_data) -> float:
        if of_data is None:
            return 0.5
        
        flow_type = getattr(of_data, 'flow_type', None)
        
        if flow_type and hasattr(flow_type, 'value'):
            type_scores = {
                'aggression': 1.0,
                'neutral': 0.6,
                'exhaustion': 0.3,
                'absorption': 0.2,
            }
            base_score = type_scores.get(flow_type.value, 0.5)
        else:
            base_score = 0.5
        
        strength = getattr(of_data, 'strength', 0.5)
        
        return base_score * strength

    def score_breakout_quality(self, fb_data) -> float:
        if fb_data is None:
            return 0.5
        
        quality = getattr(fb_data, 'quality', None)
        confidence = getattr(fb_data, 'confidence', 0.5)
        
        if quality and hasattr(quality, 'value'):
            quality_scores = {
                'real': 1.0,
                'weak': 0.6,
                'fake': 0.2,
            }
            base_score = quality_scores.get(quality.value, 0.5)
        else:
            base_score = 0.5
        
        return base_score * confidence

    def score_market_bias(self, bias_confidence: float, signal_direction: str,
                         market_bias) -> float:
        if market_bias is None:
            return 0.5
        
        direction_map = {'long': 1, 'short': -1}
        signal_sign = direction_map.get(signal_direction, 0)
        
        bias_sign_map = {
            'strong_long': 1,
            'long_bias': 1,
            'neutral': 0,
            'short_bias': -1,
            'strong_short': -1,
        }
        
        bias_value = market_bias if isinstance(market_bias, str) else getattr(market_bias, 'value', 'neutral')
        bias_sign = bias_sign_map.get(bias_value, 0)
        
        alignment = signal_sign * bias_sign
        
        if alignment > 0:
            return bias_confidence
        elif alignment < 0:
            return 0.2
        else:
            return 0.5

    def score_sector_momentum(self, symbol: str, sector_rotation: Optional[Dict],
                              signal_direction: str) -> float:
        """Score based on sector tailwind/headwind. Uses STOCK_SECTOR_MAP from config."""
        if not sector_rotation:
            return 0.5

        sector = cfg.STOCK_SECTOR_MAP.get(symbol)
        if not sector:
            return 0.5

        all_sectors = sector_rotation.get('all_sectors', {})
        sector_ret = all_sectors.get(sector, 0.0)

        # Sector return → 0..1 score for long direction (inverted for short)
        if signal_direction == 'long':
            if sector_ret > 2.0:    return 0.95
            elif sector_ret > 1.0:  return 0.80
            elif sector_ret > 0.3:  return 0.65
            elif sector_ret > -0.3: return 0.50
            elif sector_ret > -1.0: return 0.35
            else:                   return 0.20
        else:  # short — inverted
            if sector_ret < -2.0:   return 0.95
            elif sector_ret < -1.0: return 0.80
            elif sector_ret < -0.3: return 0.65
            elif sector_ret < 0.3:  return 0.50
            elif sector_ret < 1.0:  return 0.35
            else:                   return 0.20

    def score_time_filter(self, time_mult: float) -> float:
        return time_mult

    def calculate_total_score(self, components: Dict[str, float]) -> float:
        total = 0
        for key, weight in self.weights.items():
            total += components.get(key, 0) * weight
        
        return total

    def score_trade(self, trade_data: Dict, market_bias_value=None,
                    sector_rotation: Optional[Dict] = None) -> TradeScore:
        symbol = trade_data.get('symbol', 'UNKNOWN')
        components = {}
        reasons = []

        if 'signal' in trade_data:
            signal = trade_data['signal']
            components['signal_strength'] = self.score_signal_strength(signal)
            signal_dir = getattr(signal, 'direction', 'long')
        else:
            signal_dir = 'long'
            components['signal_strength'] = 0.5

        if 'metrics' in trade_data:
            components['liquidity'] = self.score_liquidity(trade_data['metrics'])
        else:
            components['liquidity'] = 0.5

        if 'volatility' in trade_data:
            components['volatility'] = self.score_volatility(trade_data['volatility'])
        else:
            components['volatility'] = 0.5

        if 'order_flow' in trade_data:
            components['order_flow'] = self.score_order_flow(trade_data['order_flow'])
        else:
            components['order_flow'] = 0.5

        if 'filter_result' in trade_data:
            components['breakout_quality'] = self.score_breakout_quality(trade_data['filter_result'])
        else:
            components['breakout_quality'] = 0.5

        components['market_bias'] = self.score_market_bias(
            trade_data.get('bias_confidence', 0.5),
            signal_dir,
            market_bias_value
        )

        components['time_filter'] = self.score_time_filter(
            trade_data.get('time_mult', 1.0)
        )

        # Sector momentum — uses sector_rotation from MarketBiasEngine.get_sector_rotation()
        components['sector_momentum'] = self.score_sector_momentum(
            symbol, sector_rotation, signal_dir
        )

        total = self.calculate_total_score(components)

        # Apply commodity modifier if available (multiplicative, outside weights)
        commodity_data = trade_data.get('commodity')
        if commodity_data and commodity_data.get('applicable'):
            total = min(total * commodity_data.get('confidence_modifier', 1.0), 1.0)
            if commodity_data.get('tailwind'):
                reasons.append(f"Commodity tailwind: {commodity_data.get('reason', '')}")
            else:
                reasons.append(f"Commodity headwind: {commodity_data.get('reason', '')}")

        # Apply news sentiment modifier if available (multiplicative)
        news_data = trade_data.get('news_sentiment')
        if news_data and news_data.get('signal') != 'neutral':
            total = min(total * news_data.get('confidence_modifier', 1.0), 1.0)
            reasons.append(f"News: {news_data.get('signal')} (score={news_data.get('score', 0)})")

        if total >= self.thresholds['excellent']:
            verdict = "EXCELLENT"
            reasons.append("Top-tier trade opportunity")
        elif total >= self.thresholds['good']:
            verdict = "GOOD"
            reasons.append("Solid trade setup")
        elif total >= self.thresholds['average']:
            verdict = "AVERAGE"
            reasons.append("Acceptable with minor concerns")
        else:
            verdict = "AVOID"
            reasons.append("Below quality threshold")

        if components.get('volatility', 0.5) < 0.4:
            reasons.append("Low volatility - lower probability")

        if components.get('order_flow', 0.5) < 0.4:
            reasons.append("Weak order flow signal")

        if components.get('breakout_quality', 0.5) < 0.4:
            reasons.append("Questionable breakout quality")

        if components.get('sector_momentum', 0.5) < 0.3:
            reasons.append(f"Sector headwind for {cfg.STOCK_SECTOR_MAP.get(symbol, 'unknown sector')}")

        return TradeScore(
            symbol=symbol,
            total_score=total,
            components=components,
            rank=0,
            verdict=verdict,
            reasons=reasons
        )

    def rank_signals(self, signals: List[Dict], max_trades: int = 5,
                    market_bias_value=None,
                    sector_rotation: Optional[Dict] = None) -> List[Tuple[Dict, TradeScore]]:
        scored = []

        for signal_data in signals:
            score = self.score_trade(signal_data, market_bias_value, sector_rotation)
            scored.append((signal_data, score))

        scored.sort(key=lambda x: x[1].total_score, reverse=True)

        for i, (_, score) in enumerate(scored):
            score.rank = i + 1

        return scored[:max_trades]

    def get_top_signals(self, signals: List[Dict], n: int = 5,
                       market_bias_value=None,
                       sector_rotation: Optional[Dict] = None) -> List[Dict]:
        ranked = self.rank_signals(signals, n, market_bias_value, sector_rotation)
        
        results = []
        for signal_data, score in ranked:
            signal_data['rank'] = score.rank
            signal_data['total_score'] = score.total_score
            signal_data['verdict'] = score.verdict
            signal_data['score_components'] = score.components
            signal_data['reasons'] = score.reasons
            results.append(signal_data)
        
        return results


if __name__ == '__main__':
    ranker = TradeRanker()
    
    test_trades = [
        {
            'symbol': 'RELIANCE',
            'metrics': {'adv': 15000000, 'avg_delivery': 25, 'volume_trend': 1.5},
            'signal': type('Signal', (), {'strength': 75, 'direction': 'long', 'structure': 'breakout'})(),
            'volatility': {'regime': 'normal'},
            'order_flow': type('OF', (), {'flow_type': type('FT', (), {'value': 'aggression'})(), 'strength': 0.8})(),
            'filter_result': type('FB', (), {'quality': type('Q', (), {'value': 'real'})(), 'confidence': 0.8})(),
            'bias_confidence': 0.8,
            'time_mult': 1.0,
        },
        {
            'symbol': 'TCS',
            'metrics': {'adv': 8000000, 'avg_delivery': 20, 'volume_trend': 1.2},
            'signal': type('Signal', (), {'strength': 60, 'direction': 'long', 'structure': 'trend'})(),
            'volatility': {'regime': 'high'},
            'order_flow': type('OF', (), {'flow_type': type('FT', (), {'value': 'neutral'})(), 'strength': 0.5})(),
            'filter_result': type('FB', (), {'quality': type('Q', (), {'value': 'weak'})(), 'confidence': 0.6})(),
            'bias_confidence': 0.6,
            'time_mult': 0.8,
        },
    ]
    
    print("Trade Scoring Results:")
    for trade in test_trades:
        score = ranker.score_trade(trade, 'neutral')
        print(f"\n{score.symbol}: {score.total_score:.2f} ({score.verdict})")
        print(f"  Components: {score.components}")
        print(f"  Reasons: {score.reasons}")