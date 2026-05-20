import bisect
import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass
from scipy.stats import norm
from datetime import datetime, timedelta
import config


@dataclass
class OptionGreeks:
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float
    theoretical_price: float


class BlackScholesModel:
    def __init__(self):
        self.risk_free_rate = getattr(config, 'OPTIONS_CONFIG', {}).get('risk_free_rate', 0.065)

    def calculate_d1_d2(self, spot: float, strike: float, 
                       time_to_expiry: float, iv: float) -> Tuple[float, float]:
        d1 = (np.log(spot / strike) + (self.risk_free_rate + 0.5 * iv ** 2) * time_to_expiry) / (iv * np.sqrt(time_to_expiry))
        d2 = d1 - iv * np.sqrt(time_to_expiry)
        return d1, d2

    def call_price(self, spot: float, strike: float, 
                  time_to_expiry: float, iv: float) -> float:
        if time_to_expiry <= 0:
            return max(0, spot - strike)
        
        d1, d2 = self.calculate_d1_d2(spot, strike, time_to_expiry, iv)
        
        call = spot * norm.cdf(d1) - strike * np.exp(-self.risk_free_rate * time_to_expiry) * norm.cdf(d2)
        return max(0, call)

    def put_price(self, spot: float, strike: float, 
                 time_to_expiry: float, iv: float) -> float:
        if time_to_expiry <= 0:
            return max(0, strike - spot)
        
        d1, d2 = self.calculate_d1_d2(spot, strike, time_to_expiry, iv)
        
        put = strike * np.exp(-self.risk_free_rate * time_to_expiry) * norm.cdf(-d2) - spot * norm.cdf(-d1)
        return max(0, put)

    def calculate_greeks(self, spot: float, strike: float, 
                       time_to_expiry: float, iv: float,
                       option_type: str = "CE") -> OptionGreeks:
        if time_to_expiry <= 0:
            if option_type == "CE":
                delta_at_exp = 1.0 if spot > strike else 0.0
            else:
                delta_at_exp = -1.0 if strike > spot else 0.0
            return OptionGreeks(
                delta=delta_at_exp,
                gamma=0,
                theta=0,
                vega=0,
                iv=iv,
                theoretical_price=max(0, spot - strike) if option_type == "CE" else max(0, strike - spot)
            )
        
        d1, d2 = self.calculate_d1_d2(spot, strike, time_to_expiry, iv)
        
        if option_type == "CE":
            delta = norm.cdf(d1)
            theta = (-spot * norm.pdf(d1) * iv / (2 * np.sqrt(time_to_expiry))
                    - self.risk_free_rate * strike * np.exp(-self.risk_free_rate * time_to_expiry) * norm.cdf(d2))
        else:
            delta = norm.cdf(d1) - 1
            theta = (-spot * norm.pdf(d1) * iv / (2 * np.sqrt(time_to_expiry))
                    + self.risk_free_rate * strike * np.exp(-self.risk_free_rate * time_to_expiry) * norm.cdf(-d2))
        
        gamma = norm.pdf(d1) / (spot * iv * np.sqrt(time_to_expiry))
        vega = spot * norm.pdf(d1) * np.sqrt(time_to_expiry) / 100
        
        theta = theta / 365
        
        if option_type == "CE":
            theoretical_price = self.call_price(spot, strike, time_to_expiry, iv)
        else:
            theoretical_price = self.put_price(spot, strike, time_to_expiry, iv)
        
        return OptionGreeks(
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            iv=iv,
            theoretical_price=theoretical_price
        )


class GreeksIntelligence:
    def __init__(self):
        self.bsm = BlackScholesModel()
        self.config = {
            'iv_percentile_threshold': 75,
            'iv_rank_threshold': 30,
            'min_days_to_expiry': 3,
            'max_days_to_expiry': 10,
        }

    def estimate_iv(self, spot: float, strike: float, 
                   premium: float, time_to_expiry: float) -> float:
        if time_to_expiry <= 0 or premium <= 0:
            return 0.20
        
        intrinsic = max(0, spot - strike)
        time_value = premium - intrinsic
        
        if time_value <= 0:
            return 0.15
        
        base_iv = 0.20
        max_attempts = 50
        
        for _ in range(max_attempts):
            theoretical = self.bsm.call_price(spot, strike, time_to_expiry, base_iv)
            
            if abs(theoretical - premium) < 0.01:
                break
            
            if theoretical < premium:
                base_iv += 0.01
            else:
                base_iv -= 0.01
            
            base_iv = max(0.05, min(2.0, base_iv))
        
        return base_iv

    def get_option_chain_greeks(self, spot: float, strikes: List[float],
                               expiry_date: datetime,
                               option_type: str = "CE") -> List[Dict]:
        now = datetime.now()
        time_to_expiry = max(0.001, (expiry_date - now).total_seconds() / (365 * 24 * 3600))
        
        results = []
        
        for strike in strikes:
            premium_estimate = self._estimate_premium(spot, strike, time_to_expiry)
            
            iv = self.estimate_iv(spot, strike, premium_estimate, time_to_expiry)
            
            greeks = self.bsm.calculate_greeks(spot, strike, time_to_expiry, iv, option_type)
            
            results.append({
                'strike': strike,
                'type': option_type,
                'premium': greeks.theoretical_price,
                'iv': iv,
                'delta': greeks.delta,
                'gamma': greeks.gamma,
                'theta': greeks.theta,
                'vega': greeks.vega,
                'intrinsic': max(0, spot - strike) if option_type == "CE" else max(0, strike - spot),
                'time_value': greeks.theoretical_price - (max(0, spot - strike) if option_type == "CE" else max(0, strike - spot)),
            })
        
        return results

    def _estimate_premium(self, spot: float, strike: float, time_to_expiry: float) -> float:
        atm_distance = abs(spot - strike) / spot
        base_premium = spot * 0.01
        
        if time_to_expiry < 0.01:
            return max(0, spot - strike) if strike < spot else 0
        
        premium = base_premium * (1 + atm_distance * 5) * (1 + time_to_expiry * 2)
        
        if strike > spot:
            premium *= 0.8
        else:
            premium *= 1.2
        
        return max(10, premium)

    def analyze_iv_rank(self, current_iv: float, iv_history: List[float]) -> Dict:
        if not iv_history:
            return {'iv_rank': 50, 'iv_percentile': 50, 'interpretation': 'normal'}
        
        min_iv = min(iv_history)
        max_iv = max(iv_history)
        avg_iv = np.mean(iv_history)
        
        iv_range = max_iv - min_iv
        iv_rank = ((current_iv - min_iv) / iv_range * 100) if iv_range > 0 else 50
        
        sorted_iv = sorted(iv_history)
        percentile = bisect.bisect_left(sorted_iv, current_iv) / len(sorted_iv) * 100

        oc = getattr(config, 'OPTIONS_CONFIG', {})
        iv_hi = oc.get('iv_high_threshold', 1.3)
        iv_lo = oc.get('iv_low_threshold', 0.7)
        if current_iv > avg_iv * iv_hi:
            interpretation = 'high_iv'
        elif current_iv < avg_iv * iv_lo:
            interpretation = 'low_iv'
        else:
            interpretation = 'normal_iv'
        
        return {
            'iv_rank': iv_rank,
            'iv_percentile': percentile,
            'interpretation': interpretation,
            'avg_iv': avg_iv,
            'iv_direction': 'expanding' if current_iv > avg_iv else 'contracting',
        }

    def recommend_strike(self, spot: float, direction: str,
                        signal_strength: float,
                        iv_analysis: Dict) -> Dict:
        step = getattr(config, 'VOLATILITY_CONFIG', {}).get('nse_strike_step', 50)
        atm_strike = round(spot / step) * step
        
        iv_rank = iv_analysis.get('iv_rank', 50)
        iv_interp = iv_analysis.get('interpretation', 'normal_iv')
        
        if direction == "long":
            if iv_interp == 'high_iv':
                strike_type = "ITM"
                strike = atm_strike - step
            elif signal_strength > 0.7:
                strike_type = "ITM"
                strike = atm_strike - step
            else:
                strike_type = "ATM"
                strike = atm_strike
        else:
            if iv_interp == 'high_iv':
                strike_type = "ITM"
                strike = atm_strike + step
            elif signal_strength > 0.7:
                strike_type = "ITM"
                strike = atm_strike + step
            else:
                strike_type = "ATM"
                strike = atm_strike
        
        time_to_expiry = 7 / 365
        option_type = "CE" if direction == "long" else "PE"
        
        greeks = self.bsm.calculate_greeks(spot, strike, time_to_expiry, 0.25, option_type)
        
        return {
            'strike_type': strike_type,
            'strike': strike,
            'option_type': option_type,
            'delta': greeks.delta,
            'theta': greeks.theta,
            'vega': greeks.vega,
            'iv_interpretation': iv_interp,
            'iv_rank': iv_rank,
        }

    def filter_by_greeks(self, option_data: Dict, direction: str) -> Tuple[bool, str]:
        delta = option_data.get('delta', 0.5)
        theta = option_data.get('theta', 0)
        iv = option_data.get('iv', 0.25)
        
        if direction == "long":
            if delta < 0.2:
                return False, "Delta too low for long"
            if theta < -5:
                return False, "Theta too negative"
        
        if direction == "short":
            if delta > -0.2:
                return False, "Delta too high for short"
            if abs(theta) > 10:
                return False, "Theta too high risk"
        
        if iv > 0.5:
            return False, "IV too high"
        
        return True, "Pass"

    def calculate_position_delta(self, spot: float, quantity: int,
                                direction: str, delta: float) -> float:
        if direction == "long":
            return quantity * delta
        else:
            return -quantity * delta

    def hedge_delta(self, position_delta: float, target_delta: float = 0) -> Tuple[str, int]:
        needed_delta = target_delta - position_delta
        
        if abs(needed_delta) < 0.05:
            return "no_hedge", 0
        
        if needed_delta > 0:
            return "buy_hedge", int(needed_delta * 100)
        else:
            return "sell_hedge", int(abs(needed_delta) * 100)


if __name__ == '__main__':
    gi = GreeksIntelligence()
    
    spot = 25000
    strikes = [24500, 24750, 25000, 25250, 25500]
    expiry = datetime.now() + timedelta(days=7)
    
    print("NIFTY Options Greeks Analysis:")
    print(f"Spot: {spot}\n")
    
    ce_chain = gi.get_option_chain_greeks(spot, strikes, expiry, "CE")
    
    print("CE Chain:")
    print(f"{'Strike':<8} {'Premium':<10} {'IV':<8} {'Delta':<8} {'Theta':<10} {'Vega':<8}")
    print("-" * 60)
    for opt in ce_chain:
        print(f"{opt['strike']:<8} {opt['premium']:<10.2f} {opt['iv']:<8.2%} {opt['delta']:<8.3f} {opt['theta']:<10.3f} {opt['vega']:<8.3f}")
    
    print("\n\nIV Analysis:")
    iv_hist = [0.20, 0.22, 0.25, 0.23, 0.28, 0.30, 0.35, 0.32, 0.30, 0.28]
    analysis = gi.analyze_iv_rank(0.32, iv_hist)
    print(f"Current IV: 0.32")
    print(f"IV Rank: {analysis['iv_rank']:.0f}%")
    print(f"IV Percentile: {analysis['iv_percentile']:.0f}%")
    print(f"Interpretation: {analysis['interpretation']}")
    
    print("\n\nStrike Recommendation:")
    rec = gi.recommend_strike(spot, "long", 0.75, analysis)
    print(f"Recommendation: {rec['strike_type']} {rec['option_type']} @ {rec['strike']}")
    print(f"Delta: {rec['delta']:.3f}")
    print(f"Theta: {rec['theta']:.3f}")
    print(f"Vega: {rec['vega']:.3f}")