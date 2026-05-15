import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass
from enum import Enum
import config


class VolatilityRegime(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class TradingMode(Enum):
    AVOID = "avoid"
    NORMAL = "normal"
    AGGRESSIVE = "aggressive"
    CAUTIOUS = "cautious"


@dataclass
class VolatilityState:
    regime: VolatilityRegime
    mode: TradingMode
    atr: float
    atr_percent: float
    avg_atr: float
    regime_score: float
    reason: str


class VolatilityEngine:
    def __init__(self):
        vc = getattr(config, 'VOLATILITY_CONFIG', {})
        self.config = {
            'atr_period': config.SIGNAL_CONFIG.get('atr_period', 14),
            'low_thresh': vc.get('low_thresh', 0.7),
            'high_thresh': vc.get('high_thresh', 1.5),
            'atr_multiplier': config.SIGNAL_CONFIG.get('atr_multiplier', 1.5),
        }

    def calculate_atr(self, df: pd.DataFrame, period: int = None) -> pd.Series:
        period = period or self.config['atr_period']
        
        if df.empty or len(df) < period + 1:
            return pd.Series([0] * len(df))
        
        high = df['high']
        low = df['low']
        close = df['close']
        
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        
        return atr

    def calculate_atr_percent(self, df: pd.DataFrame) -> pd.Series:
        atr = self.calculate_atr(df)
        close = df['close']
        
        atr_pct = (atr / close) * 100
        return atr_pct

    def calculate_volatility_metrics(self, df: pd.DataFrame) -> Dict:
        if df.empty or len(df) < 30:
            return {
                'atr': 0,
                'atr_percent': 0,
                'avg_atr': 0,
                'avg_atr_percent': 0,
                'regime_score': 0.5,
            }
        
        atr = self.calculate_atr(df)
        atr_percent = self.calculate_atr_percent(df)
        
        current_atr = atr.iloc[-1]
        current_atr_pct = atr_percent.iloc[-1]
        
        avg_atr = atr.median()
        avg_atr_pct = atr_percent.median()
        
        regime_score = current_atr / avg_atr if avg_atr > 0 else 1
        
        return {
            'atr': current_atr,
            'atr_percent': current_atr_pct,
            'avg_atr': avg_atr,
            'avg_atr_percent': avg_atr_pct,
            'regime_score': regime_score,
        }

    def classify_regime(self, df: pd.DataFrame) -> VolatilityState:
        metrics = self.calculate_volatility_metrics(df)
        
        regime_score = metrics['regime_score']
        atr_pct = metrics['atr_percent']
        avg_atr_pct = metrics['avg_atr_percent']
        
        if regime_score < self.config['low_thresh']:
            regime = VolatilityRegime.LOW
            mode = TradingMode.AVOID
            reason = f"ATR below {self.config['low_thresh']}x average ({regime_score:.2f}x)"
            
        elif regime_score > self.config['high_thresh']:
            regime = VolatilityRegime.HIGH
            mode = TradingMode.CAUTIOUS
            reason = f"ATR above {self.config['high_thresh']}x average ({regime_score:.2f}x)"
            
        else:
            regime = VolatilityRegime.NORMAL
            mode = TradingMode.NORMAL
            reason = f"ATR within normal range ({regime_score:.2f}x)"
        
        return VolatilityState(
            regime=regime,
            mode=mode,
            atr=metrics['atr'],
            atr_percent=atr_pct,
            avg_atr=metrics['avg_atr'],
            regime_score=regime_score,
            reason=reason
        )

    def should_trade(self, state: VolatilityState) -> Tuple[bool, str]:
        if state.mode == TradingMode.AVOID:
            return False, state.reason
        
        return True, state.reason

    def get_risk_parameters(self, state: VolatilityState) -> Dict:
        if state.regime == VolatilityRegime.LOW:
            return {
                'position_size_multiplier': 0,
                'sl_multiplier': 1.0,
                'max_trades': 0,
                'use_options': False,
                'strike_type': 'ATM',
            }
        
        elif state.regime == VolatilityRegime.HIGH:
            return {
                'position_size_multiplier': 0.5,
                'sl_multiplier': 1.3,
                'max_trades': 2,
                'use_options': True,
                'strike_type': 'ITM',
            }
        
        else:
            return {
                'position_size_multiplier': 1.0,
                'sl_multiplier': 1.0,
                'max_trades': 5,
                'use_options': False,
                'strike_type': 'ATM',
            }

    def get_strike_adjustment(self, spot_price: float, regime: VolatilityRegime,
                      direction: str) -> float:
        step = getattr(config, 'VOLATILITY_CONFIG', {}).get('nse_strike_step', 50)
        
        if regime == VolatilityRegime.HIGH:
            if direction == "long":
                return spot_price - step
            else:
                return spot_price + step
        
        elif regime == VolatilityRegime.LOW:
            return spot_price
        
        else:
            return round(spot_price / step) * step

    def analyze_symbol(self, symbol: str, df: pd.DataFrame) -> Dict:
        state = self.classify_regime(df)
        should_trade, reason = self.should_trade(state)
        risk_params = self.get_risk_parameters(state)
        
        return {
            'symbol': symbol,
            'regime': state.regime.value,
            'mode': state.mode.value,
            'should_trade': should_trade,
            'atr': state.atr,
            'atr_percent': state.atr_percent,
            'avg_atr': state.avg_atr,
            'regime_score': state.regime_score,
            'reason': state.reason,
            'risk_params': risk_params,
        }

    def filter_signals(self, signals: List, get_data_func) -> List[Dict]:
        filtered = []
        
        for item in signals:
            signal = item if hasattr(item, 'symbol') else item.get('signal')
            if not signal or not hasattr(signal, 'symbol'):
                continue
            
            try:
                df = get_data_func(signal.symbol)
                
                if df is None or df.empty:
                    continue
                
                analysis = self.analyze_symbol(signal.symbol, df)
                
                if analysis['should_trade']:
                    if hasattr(item, '__setitem__'):
                        item['volatility'] = analysis
                    filtered.append(item)
                    
            except Exception as e:
                print(f"    Error: {e}")
                continue
        
        return filtered


class MarketConditionAnalyzer:
    def __init__(self):
        self.vol_engine = VolatilityEngine()

    def analyze_market_breadth(self, symbols: List[str], get_data_func) -> Dict:
        regimes = {
            'low': 0,
            'normal': 0,
            'high': 0,
        }
        
        for symbol in symbols:
            try:
                df = get_data_func(symbol)
                
                if df is None or df.empty:
                    continue
                
                state = self.vol_engine.classify_regime(df)
                regimes[state.regime.value] += 1
                
            except Exception:
                continue
        
        total = sum(regimes.values())
        
        if total == 0:
            return {
                'market_condition': 'unknown',
                'sentiment': 'neutral',
                'regimes': regimes,
            }
        
        if regimes['low'] > total * 0.5:
            return {
                'market_condition': 'low_vol',
                'sentiment': 'cautious',
                'regimes': regimes,
            }
        
        if regimes['high'] > total * 0.4:
            return {
                'market_condition': 'high_vol',
                'sentiment': 'volatile',
                'regimes': regimes,
            }
        
        return {
            'market_condition': 'normal',
            'sentiment': 'positive',
            'regimes': regimes,
        }

    def get_trading_recommendation(self, market_analysis: Dict) -> Dict:
        condition = market_analysis.get('market_condition', 'unknown')
        
        if condition == 'low_vol':
            return {
                'action': 'reduce',
                'position_size': 0.5,
                'reason': 'Low volatility environment'
            }
        
        if condition == 'high_vol':
            return {
                'action': 'cautious',
                'position_size': 0.7,
                'reason': 'High volatility - use tight stops'
            }
        
        return {
            'action': 'normal',
            'position_size': 1.0,
            'reason': 'Normal volatility conditions'
        }


if __name__ == '__main__':
    from scanner import LiquidityScanner
    
    scanner = LiquidityScanner()
    vol_engine = VolatilityEngine()
    market_analyzer = MarketConditionAnalyzer()
    
    scanner.scan_universe()
    symbols = [item['symbol'] for item in scanner.universe[:10]]
    
    print("\nMarket Breadth Analysis:")
    analysis = market_analyzer.analyze_market_breadth(symbols, scanner.get_market_data)
    print(f"Condition: {analysis['market_condition']}")
    print(f"Sentiment: {analysis['sentiment']}")
    print(f"Regimes: {analysis['regimes']}")
    
    rec = market_analyzer.get_trading_recommendation(analysis)
    print(f"\nRecommendation: {rec['action']} (size: {rec['position_size']}x)")
    
    print("\n\nSymbol-level Analysis:")
    for symbol in symbols[:5]:
        df = scanner.get_market_data(symbol)
        result = vol_engine.analyze_symbol(symbol, df)
        trade, reason = vol_engine.should_trade(vol_engine.classify_regime(df))
        
        print(f"\n{symbol}: {result['regime']} | Trade: {trade}")
        print(f"  {result['reason']}")
        print(f"  Risk: {result['risk_params']}")