import pandas as pd
import numpy as np
from typing import Dict, Optional, List
from dataclasses import dataclass
from enum import Enum
import config


class StrikeType(Enum):
    ITM = "itm"
    ATM = "atm"
    OTM = "otm"


@dataclass
class StrikeRecommendation:
    strike_type: StrikeType
    strike_price: float
    option_type: str  # "CE" or "PE"
    premium: float
    delta: float
    risk_reward: float
    reasoning: str


class StrikeSelector:
    def __init__(self):
        self.config = config.STRIKE_CONFIG

    def get_option_chain(self, symbol: str) -> List[Dict]:
        # In production, fetch from Dhan/NSE option chain API
        # Returns list of strikes with bid/ask/delta
        
        # Mock data for development
        if symbol not in self._option_cache:
            spot = 2500 + np.random.randint(-200, 200)
            
            strikes = []
            for i in range(-10, 11):
                strike = spot + (i * 50)
                
                otype = "CE" if i <= 0 else "PE"
                
                intrinsic = max(0, abs(spot - strike)) if (i <= 0 and otype == "CE") or (i >= 0 and otype == "PE") else 0
                time_value = 50 + np.random.randint(-20, 20)
                premium = intrinsic + time_value
                
                delta = self._calculate_delta(spot, strike, otype, premium)
                
                strikes.append({
                    'strike': strike,
                    'type': otype,
                    'premium': premium,
                    'delta': delta,
                    'bid': premium - 2,
                    'ask': premium + 2,
                })
            
            self._option_cache[symbol] = strikes
        
        return self._option_cache[symbol]

    _option_cache = {}

    def _calculate_delta(self, spot: float, strike: float, option_type: str, premium: float) -> float:
        if option_type == "CE":
            moneyness = spot / strike
            base_delta = min(max(moneyness - 0.5, 0), 1) * 0.5 + 0.5
        else:
            moneyness = strike / spot
            base_delta = min(max(moneyness - 0.5, 0), 1) * 0.5 + 0.5
        
        base_delta += (premium / spot) * 2
        
        return min(max(base_delta, 0.01), 0.99)

    def analyze_volatility(self, df: pd.DataFrame) -> str:
        if len(df) < 14:
            return "normal"
        
        returns = df['close'].pct_change().dropna()
        
        volatility = returns.std() * np.sqrt(252)
        
        if volatility < 0.15:
            return "low"
        elif volatility < 0.30:
            return "normal"
        else:
            return "high"

    def determine_strike_type(self, signal_strength: float, volatility: str, 
                           volume_ratio: float) -> StrikeType:
        score = signal_strength / 100
        
        if volatility == "high" or volume_ratio > 2.0:
            return StrikeType.OTM
        elif volatility == "low" or volume_ratio < 1.0:
            return StrikeType.ITM
        elif score > 0.7:
            return StrikeType.ITM
        elif score < 0.5:
            return StrikeType.OTM
        else:
            return StrikeType.ATM

    def select_strike(self, symbol: str, spot_price: float, signal_strength: float,
                   volume_ratio: float, direction: str = "long",
                   df: pd.DataFrame = None) -> StrikeRecommendation:
        chain = self.get_option_chain(symbol)

        if not chain:
            return None

        volatility = self.analyze_volatility(df if df is not None else pd.DataFrame())
        
        strike_type = self.determine_strike_type(signal_strength, volatility, volume_ratio)
        
        option_type = "CE" if direction == "long" else "PE"
        
        target_delta = self._get_target_delta(strike_type)
        
        suitable_strikes = [s for s in chain if s['type'] == option_type]
        
        if not suitable_strikes:
            return None
        
        suitable_strikes.sort(key=lambda x: abs(x['delta'] - target_delta))
        
        selected = suitable_strikes[0]
        
        atr_mult = 2 if strike_type == StrikeType.ITM else (1.5 if strike_type == StrikeType.ATM else 1)
        risk_points = spot_price * 0.02 * atr_mult
        
        if direction == "long":
            reward_points = risk_points * 3
            rr_ratio = reward_points / risk_points
        else:
            rr_ratio = 3
        
        reasoning = f"{strike_type.value} strike for {volatility} volatility and {signal_strength:.0f}% strength"
        
        return StrikeRecommendation(
            strike_type=strike_type,
            strike_price=selected['strike'],
            option_type=option_type,
            premium=selected['premium'],
            delta=selected['delta'],
            risk_reward=rr_ratio,
            reasoning=reasoning
        )

    def _get_target_delta(self, strike_type: StrikeType) -> float:
        if strike_type == StrikeType.ITM:
            return self.config['itm_delta']
        elif strike_type == StrikeType.ATM:
            return 0.5
        else:
            return self.config['otm_delta']

    def batch_select(self, signals: List[Dict], get_data_func=None) -> List[Dict]:
        results = []

        for item in signals:
            signal = item.get('signal')
            if not signal:
                continue

            df = None
            if get_data_func is not None:
                try:
                    df = get_data_func(signal.symbol)
                except Exception:
                    df = None

            rec = self.select_strike(
                signal.symbol,
                signal.entry_price,
                signal.strength,
                signal.volume_ratio,
                signal.direction,
                df=df,
            )
            
            if rec:
                item['strike'] = rec
                results.append(item)
        
        return results


if __name__ == '__main__':
    from scanner import LiquidityScanner
    from signal_engine import SignalEngine
    from fake_breakout_filter import FakeBreakoutFilter
    from order_flow import OrderFlowAnalyzer
    
    scanner = LiquidityScanner()
    engine = SignalEngine()
    fb_filter = FakeBreakoutFilter()
    of_analyzer = OrderFlowAnalyzer()
    strike_selector = StrikeSelector()
    
    scanner.scan_universe()
    
    print("\nStrike Selection...")
    for item in scanner.universe[:5]:
        signal = engine.generate_signal(item['symbol'], scanner.get_market_data(item['symbol']))
        
        if signal:
            df = scanner.get_market_data(item['symbol'])
            fb_result = fb_filter.analyze(df, signal)
            of_result = of_analyzer.analyze(df, signal.direction)
            
            if fb_result.is_valid and of_result.flow_type.value != "absorption":
                rec = strike_selector.select_strike(
                    item['symbol'],
                    item['last_price'],
                    signal.strength,
                    signal.volume_ratio,
                    signal.direction
                )
                
                if rec:
                    print(f"\n{item['symbol']}: {rec.strike_type.value} {rec.option_type} @ ₹{rec.strike_price}")
                    print(f"  Premium: ₹{rec.premium} | Delta: {rec.delta:.2f} | R:R: {rec.risk_reward:.1f}R")