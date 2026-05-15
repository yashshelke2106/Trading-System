import logging
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import pickle
import os

log = logging.getLogger(__name__)

try:
    from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
    from sklearn.model_selection import cross_val_score, TimeSeriesSplit
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False


@dataclass
class TradeFeatures:
    symbol: str
    timestamp: datetime
    signal_strength: float
    volume_ratio: float
    volatility_regime: float
    order_flow_strength: float
    breakout_quality: float
    market_bias: float
    time_multiplier: float
    atr_percent: float
    rsi: float
    trend_strength: float
    
    def to_array(self) -> List[float]:
        return [
            self.signal_strength,
            self.volume_ratio,
            self.volatility_regime,
            self.order_flow_strength,
            self.breakout_quality,
            self.market_bias,
            self.time_multiplier,
            self.atr_percent,
            self.rsi,
            self.trend_strength,
        ]
    
    def to_dict(self) -> Dict:
        return {
            'symbol': self.symbol,
            'timestamp': self.timestamp,
            'signal_strength': self.signal_strength,
            'volume_ratio': self.volume_ratio,
            'volatility_regime': self.volatility_regime,
            'order_flow_strength': self.order_flow_strength,
            'breakout_quality': self.breakout_quality,
            'market_bias': self.market_bias,
            'time_multiplier': self.time_multiplier,
            'atr_percent': self.atr_percent,
            'rsi': self.rsi,
            'trend_strength': self.trend_strength,
        }


@dataclass
class ModelPerformance:
    accuracy: float
    precision: float
    recall: float
    f1: float
    win_rate: float
    total_trades: int
    feature_importance: Dict[str, float]


class MLTradingAI:
    def __init__(self):
        self.model = None
        self.scaler = None
        self.is_trained = False
        self.trade_history: List[TradeFeatures] = []
        self.outcomes: List[int] = []
        self.feature_names = [
            'signal_strength', 'volume_ratio', 'volatility_regime',
            'order_flow_strength', 'breakout_quality', 'market_bias',
            'time_multiplier', 'atr_percent', 'rsi', 'trend_strength'
        ]
        self.config = {
            'min_samples': 30,
            'retrain_threshold': 50,
            'model_path': 'models/ml_model.pkl',
        }

    def _create_default_model(self):
        if not SKLEARN_AVAILABLE:
            return None
        
        return GradientBoostingClassifier(
            n_estimators=100,
            learning_rate=0.1,
            max_depth=4,
            min_samples_split=5,
            min_samples_leaf=2,
            subsample=0.8,
            random_state=42
        )

    def _create_rf_model(self):
        if not SKLEARN_AVAILABLE:
            return None
        
        return RandomForestClassifier(
            n_estimators=100,
            max_depth=6,
            min_samples_split=5,
            min_samples_leaf=2,
            random_state=42
        )

    def prepare_features(self, signal, volatility_data=None, 
                       order_flow_data=None, filter_result=None,
                       market_bias_conf=0.5, time_mult=1.0) -> TradeFeatures:
        signal_strength = getattr(signal, 'strength', 50) / 100
        volume_ratio = getattr(signal, 'volume_ratio', 1.0)
        
        if volatility_data:
            vol_regime = 0.5 if volatility_data.get('regime') == 'normal' else (0.8 if volatility_data.get('regime') == 'high' else 0.3)
            atr_pct = volatility_data.get('atr_percent', 2) / 10
        else:
            vol_regime = 0.5
            atr_pct = 0.2
        
        if order_flow_data:
            of_strength = getattr(order_flow_data, 'strength', 0.5)
        else:
            of_strength = 0.5
        
        if filter_result:
            fb_quality = getattr(filter_result, 'confidence', 0.5)
        else:
            fb_quality = 0.5
        
        rsi = 50 + (signal_strength - 0.5) * 20
        
        trend = 0.5
        if hasattr(signal, 'structure'):
            if 'trend' in str(signal.structure).lower():
                trend = 0.7
            elif 'breakout' in str(signal.structure).lower():
                trend = 0.8
        
        return TradeFeatures(
            symbol=getattr(signal, 'symbol', 'UNKNOWN'),
            timestamp=datetime.now(),
            signal_strength=signal_strength,
            volume_ratio=min(volume_ratio / 3, 1.0),
            volatility_regime=vol_regime,
            order_flow_strength=of_strength,
            breakout_quality=fb_quality,
            market_bias=market_bias_conf,
            time_multiplier=time_mult,
            atr_percent=atr_pct,
            rsi=rsi / 100,
            trend_strength=trend,
        )

    def train(self, features: List[TradeFeatures], outcomes: List[int]) -> bool:
        if len(features) < self.config['min_samples']:
            return False
        
        if not SKLEARN_AVAILABLE:
            return False
        
        X = np.array([f.to_array() for f in features])
        y = np.array(outcomes)
        
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)
        
        self.model = self._create_default_model()
        self.model.fit(X_scaled, y)
        
        self.is_trained = True
        
        self.trade_history = features
        self.outcomes = outcomes
        
        return True

    def predict_proba(self, features: TradeFeatures) -> Tuple[float, float]:
        if not self.is_trained or self.model is None:
            return self._default_prediction(features)
        
        try:
            X = np.array([features.to_array()])
            X_scaled = self.scaler.transform(X)
            
            proba = self.model.predict_proba(X_scaled)[0]
            
            win_prob = proba[1] if len(proba) > 1 else proba[0]
            lose_prob = proba[0] if len(proba) > 1 else 1 - proba[0]
            
            return win_prob, lose_prob
            
        except Exception as e:
            return self._default_prediction(features)

    def _default_prediction(self, features: TradeFeatures) -> Tuple[float, float]:
        score = (
            features.signal_strength * 0.25 +
            features.volume_ratio * 0.15 +
            features.volatility_regime * 0.15 +
            features.order_flow_strength * 0.20 +
            features.breakout_quality * 0.15 +
            features.market_bias * 0.10
        )
        
        return min(score, 0.9), max(1 - score, 0.1)

    def evaluate(self) -> Optional[ModelPerformance]:
        if not self.is_trained or len(self.trade_history) < 10:
            return None
        
        if not SKLEARN_AVAILABLE:
            return None
        
        try:
            X = np.array([f.to_array() for f in self.trade_history])
            y = np.array(self.outcomes)
            
            X_scaled = self.scaler.transform(X)
            
            cv_scores = cross_val_score(self.model, X_scaled, y, cv=min(5, len(X) // 5))
            
            predictions = self.model.predict(X_scaled)
            
            tp = sum(1 for p, o in zip(predictions, y) if p == 1 and o == 1)
            fp = sum(1 for p, o in zip(predictions, y) if p == 1 and o == 0)
            tn = sum(1 for p, o in zip(predictions, y) if p == 0 and o == 0)
            fn = sum(1 for p, o in zip(predictions, y) if p == 0 and o == 1)
            
            accuracy = (tp + tn) / len(y) if len(y) > 0 else 0
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
            
            win_rate = sum(self.outcomes) / len(self.outcomes) if self.outcomes else 0
            
            feature_importance = {}
            if hasattr(self.model, 'feature_importances_'):
                for name, imp in zip(self.feature_names, self.model.feature_importances_):
                    feature_importance[name] = float(imp)
            
            return ModelPerformance(
                accuracy=float(accuracy),
                precision=float(precision),
                recall=float(recall),
                f1=float(f1),
                win_rate=float(win_rate),
                total_trades=len(self.trade_history),
                feature_importance=feature_importance,
            )
            
        except Exception as e:
            return None

    def retrain_if_needed(self) -> bool:
        if len(self.trade_history) >= self.config['retrain_threshold']:
            X = np.array([f.to_array() for f in self.trade_history])
            y = np.array(self.outcomes)
            
            self.scaler = StandardScaler()
            X_scaled = self.scaler.fit_transform(X)
            
            self.model = self._create_default_model()
            self.model.fit(X_scaled, y)
            
            self.is_trained = True
            
            return True
        
        return False

    def add_trade(self, features: TradeFeatures, outcome: int):
        self.trade_history.append(features)
        self.outcomes.append(outcome)
        
        if len(self.trade_history) > 500:
            self.trade_history = self.trade_history[-500:]
            self.outcomes = self.outcomes[-500:]
        
        if len(self.trade_history) % 10 == 0:
            self.retrain_if_needed()

    def save_model(self, path: str = None):
        if not self.is_trained:
            return False
        
        path = path or self.config['model_path']
        os.makedirs(os.path.dirname(path), exist_ok=True)
        
        try:
            with open(path, 'wb') as f:
                pickle.dump({
                    'model': self.model,
                    'scaler': self.scaler,
                    'trade_history': self.trade_history,
                    'outcomes': self.outcomes,
                    'feature_names': self.feature_names,
                }, f)
            return True
        except Exception as e:
            log.debug(f"ML model save failed: {e}")
            return False

    def load_model(self, path: str = None):
        path = path or self.config['model_path']

        if not os.path.exists(path):
            return False

        try:
            with open(path, 'rb') as f:
                data = pickle.load(f)

            self.model = data.get('model')
            self.scaler = data.get('scaler')
            self.trade_history = data.get('trade_history', [])
            self.outcomes = data.get('outcomes', [])
            self.feature_names = data.get('feature_names', self.feature_names)

            self.is_trained = self.model is not None

            return True
        except Exception as e:
            log.debug(f"ML model load failed: {e}")
            return False

    def get_position_size(self, features: TradeFeatures) -> str:
        win_prob, _ = self.predict_proba(features)
        
        if win_prob >= 0.70:
            return "full"
        elif win_prob >= 0.55:
            return "half"
        else:
            return "skip"

    def get_feature_importance(self) -> Dict[str, float]:
        if not self.is_trained or not hasattr(self.model, 'feature_importances_'):
            return {name: 0.1 for name in self.feature_names}
        
        return {
            name: float(imp) 
            for name, imp in zip(self.feature_names, self.model.feature_importances_)
        }


if __name__ == '__main__':
    ai = MLTradingAI()
    
    test_features = TradeFeatures(
        symbol="RELIANCE",
        timestamp=datetime.now(),
        signal_strength=0.75,
        volume_ratio=1.5,
        volatility_regime=0.7,
        order_flow_strength=0.8,
        breakout_quality=0.75,
        market_bias=0.8,
        time_multiplier=1.0,
        atr_percent=0.25,
        rsi=60,
        trend_strength=0.8,
    )
    
    print("ML Trading AI Test:")
    print(f"\nFeatures: {test_features.to_dict()}")
    
    prob, _ = ai.predict_proba(test_features)
    print(f"\nPrediction: Win probability = {prob:.2%}")
    print(f"Position Size: {ai.get_position_size(test_features)}")
    
    print("\n\nTraining simulation:")
    np.random.seed(42)
    for i in range(50):
        features = TradeFeatures(
            symbol="TEST",
            timestamp=datetime.now(),
            signal_strength=np.random.uniform(0.3, 0.9),
            volume_ratio=np.random.uniform(0.8, 2.0),
            volatility_regime=np.random.uniform(0.3, 0.8),
            order_flow_strength=np.random.uniform(0.4, 0.9),
            breakout_quality=np.random.uniform(0.4, 0.9),
            market_bias=np.random.uniform(0.4, 0.9),
            time_multiplier=np.random.uniform(0.5, 1.2),
            atr_percent=np.random.uniform(0.1, 0.4),
            rsi=np.random.uniform(40, 70),
            trend_strength=np.random.uniform(0.4, 0.9),
        )
        
        outcome = 1 if features.signal_strength > 0.6 else 0
        ai.add_trade(features, outcome)
    
    ai.train(ai.trade_history, ai.outcomes)
    
    performance = ai.evaluate()
    if performance:
        print(f"\nModel Performance:")
        print(f"  Accuracy: {performance.accuracy:.1%}")
        print(f"  Win Rate: {performance.win_rate:.1%}")
        print(f"  Total Trades: {performance.total_trades}")
        print(f"\nFeature Importance:")
        for feat, imp in sorted(performance.feature_importance.items(), key=lambda x: -x[1])[:5]:
            print(f"  {feat}: {imp:.3f}")