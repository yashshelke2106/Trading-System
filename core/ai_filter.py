import logging
import pandas as pd
import numpy as np
from typing import Dict, Optional, List
from dataclasses import dataclass
import config
import pickle
import os

log = logging.getLogger(__name__)


@dataclass
class ProbabilityPrediction:
    win_probability: float
    position_size: str  # "full", "half", "skip"
    confidence: float
    features: Dict


class AIFilter:
    def __init__(self):
        self.config = config.AI_CONFIG
        self.model = None
        self.is_loaded = False

    def load_model(self) -> bool:
        model_path = self.config['model_path']
        
        if not os.path.exists(model_path):
            self._create_default_model()
            return False
        
        try:
            with open(model_path, 'rb') as f:
                self.model = pickle.load(f)
            self.is_loaded = True
            return True
        except Exception as e:
            log.debug(f"AI model load failed: {e}")
            self._create_default_model()
            return False

    def _create_default_model(self):
        self.model = {
            'weights': {
                'structure':  0.22,   # signal strength
                'breakout':   0.18,   # fake_breakout filter confidence
                'volume':     0.18,   # volume ratio
                'order_flow': 0.22,   # order flow strength
                'volatility': 0.10,   # volatility fit
                'strike':     0.10,   # delta quality
            },
            'thresholds': {
                'high': self.config['high_prob_threshold'],
                'medium': self.config['medium_prob_threshold'],
            }
        }
        self.is_loaded = True

    def extract_features(self, signal, filter_result, order_flow_result,
                        strike_result) -> Dict:
        features = {
            'strength': signal.strength / 100,
            'volume_ratio': min(signal.volume_ratio / 3, 1),
            'volatility': 0.5 if signal.volatility.value == "normal" else (0.3 if signal.volatility.value == "low" else 0.8),
            'order_flow_strength': order_flow_result.strength if order_flow_result else 0.5,
            'filter_confidence': filter_result.confidence if filter_result else 0.5,
            'strike_delta': strike_result.delta if strike_result else 0.5,
            'risk_reward': strike_result.risk_reward / 5 if strike_result else 0.6,
        }
        
        return features

    def calculate_probability(self, features: Dict) -> float:
        if not self.model:
            return 0.5
        w = self.model.get('weights', {})
        score = (
            features.get('strength', 0)            * w.get('structure',  0.22) +
            features.get('filter_confidence', 0)   * w.get('breakout',   0.18) +
            features.get('volume_ratio', 0)        * w.get('volume',     0.18) +
            features.get('order_flow_strength', 0) * w.get('order_flow', 0.22) +
            features.get('volatility', 0)          * w.get('volatility', 0.10) +
            features.get('strike_delta', 0)        * w.get('strike',     0.10)
        )
        return min(max(score, 0), 1)

    def predict(self, signal, filter_result=None, order_flow_result=None,
              strike_result=None) -> ProbabilityPrediction:
        
        features = self.extract_features(
            signal, filter_result, order_flow_result, strike_result
        )
        
        probability = self.calculate_probability(features)
        
        if probability >= self.config['high_prob_threshold']:
            position_size = "full"
        elif probability >= self.config['medium_prob_threshold']:
            position_size = "half"
        else:
            position_size = "skip"
        
        return ProbabilityPrediction(
            win_probability=probability,
            position_size=position_size,
            confidence=probability,
            features=features
        )

    def filter_signals(self, signals: List[Dict]) -> List[Dict]:
        if not self.is_loaded:
            self.load_model()
        
        filtered = []
        
        for item in signals:
            signal = item.get('signal')
            if not signal:
                continue
            
            filter_result = item.get('filter_result')
            order_flow_result = item.get('order_flow')
            strike_result = item.get('strike')
            
            prediction = self.predict(
                signal, filter_result, order_flow_result, strike_result
            )
            
            if prediction.position_size != "skip":
                item['ai_prediction'] = prediction
                filtered.append(item)
        
        filtered.sort(key=lambda x: x['ai_prediction'].win_probability, reverse=True)
        
        return filtered

    def train_model(self, trades: List[Dict], outcomes: List[int]) -> bool:
        if len(trades) != len(outcomes):
            return False
        
        features_list = []
        
        for trade in trades:
            features = self.extract_features(
                trade.get('signal'),
                trade.get('filter_result'),
                trade.get('order_flow'),
                trade.get('strike')
            )
            features_list.append(features)
        
        self._update_weights(features_list, outcomes)
        
        return True

    def _update_weights(self, features_list: List[Dict], outcomes: List[int]):
        if not features_list:
            return
        # Map model weight keys to actual feature keys in extracted feature dicts
        KEY_MAP = {
            'structure':  'strength',
            'breakout':   'filter_confidence',
            'volume':     'volume_ratio',
            'order_flow': 'order_flow_strength',
            'volatility': 'volatility',
            'strike':     'strike_delta',
        }
        feature_importance = {k: 0.0 for k in KEY_MAP}
        win_features  = [f for f, o in zip(features_list, outcomes) if o == 1]
        loss_features = [f for f, o in zip(features_list, outcomes) if o == 0]
        if win_features and loss_features:
            for model_key, feat_key in KEY_MAP.items():
                avg_win  = sum(f.get(feat_key, 0) for f in win_features)  / len(win_features)
                avg_loss = sum(f.get(feat_key, 0) for f in loss_features) / len(loss_features)
                if avg_win > avg_loss:
                    feature_importance[model_key] = (avg_win - avg_loss) * 10
        total = sum(feature_importance.values())
        if total > 0:
            for key in feature_importance:
                feature_importance[key] /= total
            if self.model:
                self.model['weights'] = feature_importance

    def save_model(self, path: str = None) -> bool:
        path = path or self.config['model_path']
        
        os.makedirs(os.path.dirname(path), exist_ok=True)
        
        try:
            with open(path, 'wb') as f:
                pickle.dump(self.model, f)
            return True
        except Exception as e:
            log.debug(f"AI model save failed: {e}")
            return False


if __name__ == '__main__':
    ai_filter = AIFilter()
    ai_filter.load_model()
    
    print("Testing AI filter...")
    print(f"Model loaded: {ai_filter.is_loaded}")
    
    test_signal = type('Signal', (), {
        'symbol': 'RELIANCE',
        'direction': 'long',
        'strength': 75,
        'volume_ratio': 1.5,
        'volatility': type('Vol', (), {'value': 'normal'})(),
    })()
    
    prediction = ai_filter.predict(test_signal)
    print(f"Prediction: {prediction.win_probability:.2f} - {prediction.position_size}")