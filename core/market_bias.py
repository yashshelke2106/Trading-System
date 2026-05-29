import time
import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
from enum import Enum

_INDEX_CACHE_TTL = 600  # 10 minutes — refresh index data during live session


class MarketBias(Enum):
    STRONG_LONG = "strong_long"
    LONG_BIAS = "long_bias"
    NEUTRAL = "neutral"
    SHORT_BIAS = "short_bias"
    STRONG_SHORT = "strong_short"


@dataclass
class MarketContext:
    bias: MarketBias
    trend: str
    strength: float
    nifty_level: float
    banknifty_level: float
    reason: str


class MarketBiasEngine:
    def __init__(self):
        self.index_symbol = "NIFTY"
        self.bank_index = "BANKNIFTY"
        self.cache = {}  # symbol -> (timestamp, df)

    def get_index_data(self, symbol: str = "NIFTY", days: int = 50):
        entry = self.cache.get(symbol)
        if entry and time.time() - entry[0] < _INDEX_CACHE_TTL:
            return entry[1]

        # Try Dhan API first
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import config as cfg
            if not cfg.USE_MOCK_DATA and cfg.DHAN_CLIENT_ID != "your_dhan_client_id":
                from .api_dhan import dhan_api
                df = dhan_api.get_historical_data(symbol, from_date=days)
                if df is not None and not df.empty:
                    self.cache[symbol] = (time.time(), df)
                    return df
        except Exception:
            pass

        # Fresh mock data each call (no fixed seed) to avoid stale analysis
        rng = np.random.default_rng(int(pd.Timestamp.now().timestamp()) % 100000)
        dates = pd.date_range(end=pd.Timestamp.now(), periods=days, freq='D')
        base = 22500 if symbol == "NIFTY" else 49000

        daily_returns = rng.normal(0.0003, 0.012, days)
        closes = base * np.exp(np.cumsum(daily_returns))

        df = pd.DataFrame({
            'date': dates,
            'open': closes * (1 + rng.uniform(-0.003, 0.003, days)),
            'high': closes * (1 + rng.uniform(0.002, 0.012, days)),
            'low': closes * (1 - rng.uniform(0.002, 0.012, days)),
            'close': closes,
            'volume': rng.integers(50_000_000, 200_000_000, days),
        })

        self.cache[symbol] = (time.time(), df)
        return df

    def calculate_trend(self, df: pd.DataFrame) -> Tuple[str, float]:
        if len(df) < 50:
            return "unknown", 0
        
        sma20 = df['close'].rolling(20).mean().iloc[-1]
        sma50 = df['close'].rolling(50).mean().iloc[-1]
        current = df['close'].iloc[-1]
        
        above_sma = current > sma20 > sma50
        
        returns = df['close'].pct_change().tail(20)
        momentum = returns.mean() / returns.std() * 100
        
        if above_sma and momentum > 1:
            trend = "STRONG_UP"
            strength = min(abs(momentum) / 10, 1)
        elif above_sma:
            trend = "UP"
            strength = 0.6
        elif current < sma20 < sma50 and momentum < -1:
            trend = "STRONG_DOWN"
            strength = min(abs(momentum) / 10, 1)
        elif current < sma20:
            trend = "DOWN"
            strength = 0.6
        else:
            trend = "NEUTRAL"
            strength = 0.3
        
        return trend, strength

    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        if len(df) < period + 1:
            return 0
        
        tr1 = df['high'] - df['low']
        tr2 = (df['high'] - df['close'].shift(1)).abs()
        tr3 = (df['low'] - df['close'].shift(1)).abs()
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        
        return atr if not pd.isna(atr) else 0

    def get_market_bias(self, nifty_data: pd.DataFrame = None, 
                       banknifty_data: pd.DataFrame = None) -> MarketContext:
        if nifty_data is None:
            nifty_data = self.get_index_data("NIFTY", 50)
        if banknifty_data is None:
            banknifty_data = self.get_index_data("BANKNIFTY", 50)
        
        nifty_trend, nifty_strength = self.calculate_trend(nifty_data)
        bank_trend, bank_strength = self.calculate_trend(banknifty_data)
        
        avg_strength = (nifty_strength + bank_strength) / 2
        
        nifty_atr = self.calculate_atr(nifty_data)
        nifty_current = nifty_data['close'].iloc[-1]
        nifty_atr_pct = (nifty_atr / nifty_current) * 100
        
        bank_atr = self.calculate_atr(banknifty_data)
        bank_current = banknifty_data['close'].iloc[-1]
        bank_atr_pct = (bank_atr / bank_current) * 100
        
        avg_vol = nifty_data['volume'].tail(5).mean() / nifty_data['volume'].mean()
        
        if nifty_trend == "STRONG_UP" and bank_trend == "STRONG_UP":
            bias = MarketBias.STRONG_LONG
            reason = f"Both indices in strong uptrend (N: {nifty_trend}, B: {bank_trend})"
        elif nifty_trend in ["STRONG_UP", "UP"] and nifty_strength > 0.5:
            bias = MarketBias.LONG_BIAS
            reason = f"Nifty leading uptrend with {nifty_strength:.2f} strength"
        elif nifty_trend == "STRONG_DOWN" and bank_trend == "STRONG_DOWN":
            bias = MarketBias.STRONG_SHORT
            reason = f"Both indices in strong downtrend (N: {nifty_trend}, B: {bank_trend})"
        elif nifty_trend in ["STRONG_DOWN", "DOWN"] and nifty_strength > 0.5:
            bias = MarketBias.SHORT_BIAS
            reason = f"Nifty leading downtrend with {nifty_strength:.2f} strength"
        else:
            bias = MarketBias.NEUTRAL
            reason = f"Mixed signals (N: {nifty_trend}, B: {bank_trend})"
        
        if avg_vol > 1.3:
            reason += " | High volume day"
        
        return MarketContext(
            bias=bias,
            trend=nifty_trend,
            strength=avg_strength,
            nifty_level=nifty_current,
            banknifty_level=bank_current,
            reason=reason
        )

    def filter_signal_by_bias(self, signal_direction: str, 
                             market_bias: MarketBias) -> Tuple[bool, float]:
        direction_map = {
            "long": 1,
            "short": -1,
        }
        
        # Symmetric bias scores: both directions get equal treatment at equivalent bias levels
        bias_score = {
            MarketBias.STRONG_LONG: 1.0,
            MarketBias.LONG_BIAS: 0.8,
            MarketBias.NEUTRAL: 0.5,
            MarketBias.SHORT_BIAS: 0.8,
            MarketBias.STRONG_SHORT: 1.0,
        }
        
        signal_sign = direction_map.get(signal_direction, 0)
        
        if market_bias in [MarketBias.STRONG_LONG, MarketBias.STRONG_SHORT]:
            favorable = (signal_sign == 1 and market_bias == MarketBias.STRONG_LONG) or \
                       (signal_sign == -1 and market_bias == MarketBias.STRONG_SHORT)
            
            if favorable:
                return True, bias_score[market_bias]
            else:
                return False, 0
        
        confidence = bias_score.get(market_bias, 0.5)
        
        if signal_sign == 1:
            adjusted_confidence = confidence if market_bias in [MarketBias.LONG_BIAS, MarketBias.NEUTRAL] else confidence * 0.5
        else:
            adjusted_confidence = confidence if market_bias in [MarketBias.SHORT_BIAS, MarketBias.NEUTRAL] else confidence * 0.5
        
        return True, adjusted_confidence

    def get_sector_rotation(self) -> Dict:
        sectors = {
            'IT':        ['INFY', 'TCS', 'WIPRO', 'HCLTECH'],
            'BANK':      ['HDFCBANK', 'ICICIBANK', 'KOTAKBANK', 'SBIN', 'AXISBANK'],
            'AUTO':      ['MARUTI', 'TATAMOTORS', 'BAJAJ-AUTO'],
            'FMCG':      ['HINDUNILVR', 'ITC', 'ASIANPAINT'],
            'ENERGY':    ['RELIANCE', 'ONGC', 'BPCL'],
            'METAL':     ['NALCO', 'VEDANTA', 'HINDALCO', 'JSWSTEEL'],  # NALCO→NATIONALUM.NS, VEDANTA→VEDL.NS via _YF_TICKER_MAP
            'RENEWABLE': ['ADANIGREEN', 'TATAPOWER', 'WAAREE'],         # WAAREE→WAAREEENER.NS
            'PSU_CAPEX': ['BHEL', 'BEL', 'HAL', 'NTPC'],
            'NBFC':      ['BAJFINANCE', 'CHOLAFIN', 'MUTHOOTFIN'],
            'AUTO_TECH': ['KPIT', 'BOSCH', 'MOTHERSON'],                # KPIT→KPITTECH.NS, BOSCH→BOSCHLTD.NS
            'BROKING':   ['ANGELONE', 'MOTILALOFS', 'ICICISEC'],        # ICICISEC→ISEC.NS
        }

        rotation = {}
        for sector, symbols in sectors.items():
            returns = []
            for sym in symbols[:2]:
                df = self.get_index_data(sym, days=5)
                if df is not None and not df.empty and len(df) >= 2:
                    ret = (df['close'].iloc[-1] - df['close'].iloc[0]) / df['close'].iloc[0] * 100
                    returns.append(ret)
            rotation[sector] = float(np.mean(returns)) if returns else 0.0

        sorted_sectors = sorted(rotation.items(), key=lambda x: x[1], reverse=True)

        return {
            'leading': sorted_sectors[0][0] if sorted_sectors else None,
            'lagging': sorted_sectors[-1][0] if len(sorted_sectors) > 1 else None,
            'all_sectors': rotation,
        }

    def get_global_cue_score(self) -> Dict:
        """Pre-market global cue: US markets, DXY, crude → composite risk-on/off score."""
        try:
            # Global pre-market cues (US/DXY/crude) are NOT available on Dhan
            # (Indian-exchange only). yfinance permanently removed -> this cue
            # is disabled and returns neutral. Re-enable only with a dedicated
            # global-data provider, never yfinance.
            us_ret = dxy_ret = crude_ret = 0.0

            # Composite: US market positive → risk on; strong USD → EM headwind; crude context
            composite = us_ret * 0.5 - dxy_ret * 0.3 + crude_ret * 0.1

            return {
                'us_market_ret': round(us_ret, 3),
                'dxy_change': round(dxy_ret, 3),
                'crude_change': round(crude_ret, 3),
                'composite_score': round(composite, 3),
                'bias': 'RISK_ON' if composite > 0.5 else 'RISK_OFF' if composite < -0.5 else 'NEUTRAL',
                'metal_headwind': dxy_ret > 0.5,       # strong USD → bad for metals/EM
                'energy_tailwind': crude_ret > 1.0,
                'us_market_weak': us_ret < -0.8,
            }
        except Exception as e:
            return {
                'us_market_ret': 0.0, 'dxy_change': 0.0, 'crude_change': 0.0,
                'composite_score': 0.0, 'bias': 'NEUTRAL',
                'metal_headwind': False, 'energy_tailwind': False,
                'us_market_weak': False, 'error': str(e),
            }


if __name__ == '__main__':
    engine = MarketBiasEngine()
    
    context = engine.get_market_bias()
    print(f"Market Bias: {context.bias.value}")
    print(f"Trend: {context.trend}")
    print(f"Strength: {context.strength:.2f}")
    print(f"NIFTY: {context.nifty_level:.2f}")
    print(f"BANKNIFTY: {context.banknifty_level:.2f}")
    print(f"Reason: {context.reason}")
    
    print("\nSignal Filtering:")
    for direction in ['long', 'short']:
        allowed, conf = engine.filter_signal_by_bias(direction, context.bias)
        print(f"  {direction.upper()}: {'ALLOWED' if allowed else 'AVOID'} (conf: {conf:.2f})")
    
    print("\nSector Rotation:")
    rotation = engine.get_sector_rotation()
    print(f"  Leading: {rotation['leading']}")
    print(f"  Lagging: {rotation['lagging']}")