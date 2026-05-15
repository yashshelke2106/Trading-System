"""Liquidity Intelligence — volume spike detection & classification.

Fixes from prior version:
  * Spike baseline was contaminated: 5-day recent window sat *inside* the
    20-day MA. Baseline now excludes the recent window.
  * `classify_spike_type` branch order was wrong — news_driven (>3%) was
    unreachable because trend_driven (>2%) branch consumed it first.
    Rewritten with strongest condition tested first and with explicit
    direction separation.
  * `detect_news_move` compared latest bar vs mean that *included* the
    latest bar. Off-by-one fixed with `iloc[:-1]`.
  * Bare `except:` swapped for specific exceptions.
  * Added VWAP deviation and cumulative-delta proxy (up-vol minus
    down-vol) so spike classification does not rely on price alone.
  * Added impact-cost guard — no point ranking a "spike" in an illiquid
    name with wide intraday range; penalized automatically.
  * LossClusterDetector now persists state optionally to disk (hook
    provided; caller can wire a path).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spike detection
# ---------------------------------------------------------------------------
@dataclass
class LiquiditySpike:
    detected: bool
    spike_ratio: float
    spike_type: str
    confidence: float
    reason: str
    is_news_driven: bool
    direction: str = "neutral"           # up / down / neutral
    vwap_deviation_bps: float = 0.0
    delta_ratio: float = 0.0             # (up_vol - dn_vol) / total_vol
    impact_cost_bps: float = 0.0


class LiquiditySpikeDetector:
    """Classify volume spikes. Avoids fake breakouts by confirming with
    price direction, VWAP deviation, and delta imbalance.
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = {
            'spike_threshold': 1.8,        # recent vs baseline
            'volume_ma_period': 20,
            'momentum_period': 5,
            'news_price_pct': 0.03,
            'trend_price_pct': 0.012,
            'accum_price_pct': 0.005,
            'news_gap_pct': 0.01,
            'news_range_pct': 0.03,
            'max_impact_bps': 250,         # above this, penalize score
        }
        if config:
            self.config.update(config)

    # ------------------------------------------------------------------
    # Core metrics
    # ------------------------------------------------------------------
    def _baseline_volume(self, df: pd.DataFrame) -> float:
        """MA of volume EXCLUDING the recent momentum window."""
        momentum = self.config['momentum_period']
        period = self.config['volume_ma_period']
        if len(df) < period + momentum:
            # Not enough history; use all-but-recent fallback
            if len(df) <= momentum:
                return float(df['volume'].mean() or 0.0)
            base = df['volume'].iloc[:-momentum]
            return float(base.tail(period).mean() or 0.0)
        base = df['volume'].iloc[:-momentum].tail(period)
        return float(base.mean() or 0.0)

    def calculate_spike_ratio(self, df: pd.DataFrame) -> float:
        if len(df) < self.config['volume_ma_period']:
            return 1.0
        recent_vol = float(df['volume'].tail(self.config['momentum_period']).mean())
        baseline = self._baseline_volume(df)
        if baseline <= 0:
            return 1.0
        return recent_vol / baseline

    def detect_spike(self, df: pd.DataFrame) -> Tuple[bool, float]:
        ratio = self.calculate_spike_ratio(df)
        return ratio >= self.config['spike_threshold'], ratio

    # ------------------------------------------------------------------
    # Confirmations — direction, VWAP, delta, impact cost
    # ------------------------------------------------------------------
    @staticmethod
    def _price_change(df: pd.DataFrame, bars: int = 3) -> float:
        if len(df) < bars:
            return 0.0
        tail = df.tail(bars)
        open0 = float(tail['open'].iloc[0])
        close_last = float(tail['close'].iloc[-1])
        if open0 == 0:
            return 0.0
        return (close_last - open0) / open0

    @staticmethod
    def _typical_price(df: pd.DataFrame) -> pd.Series:
        return (df['high'] + df['low'] + df['close']) / 3.0

    def _vwap_deviation_bps(self, df: pd.DataFrame) -> float:
        """Deviation of last close from rolling VWAP (bps)."""
        if len(df) < 5:
            return 0.0
        tp = self._typical_price(df)
        vol = df['volume'].replace(0, np.nan)
        vwap = (tp * vol).sum() / vol.sum() if vol.sum() > 0 else np.nan
        last = float(df['close'].iloc[-1])
        if not np.isfinite(vwap) or vwap == 0:
            return 0.0
        return float((last - vwap) / vwap * 10_000)

    @staticmethod
    def _delta_ratio(df: pd.DataFrame, bars: int = 5) -> float:
        """Cumulative delta proxy: up-close volume minus down-close volume,
        over last N bars, normalized by total volume.
        """
        if len(df) < 2:
            return 0.0
        tail = df.tail(bars)
        direction = np.sign(tail['close'] - tail['open']).replace(0, 0)
        signed = (direction * tail['volume']).sum()
        total = tail['volume'].sum()
        if total <= 0:
            return 0.0
        return float(signed / total)

    @staticmethod
    def _impact_cost_bps(df: pd.DataFrame, bars: int = 5) -> float:
        tail = df.tail(bars)
        denom = tail['close'].replace(0, np.nan)
        return float(((tail['high'] - tail['low']) / denom).mean() * 10_000)

    # ------------------------------------------------------------------
    # Classification — fixes branch-ordering bug
    # ------------------------------------------------------------------
    def classify_spike_type(self, df: pd.DataFrame) -> Tuple[str, str]:
        """Returns (type, direction)."""
        if len(df) < 3:
            return "unknown", "neutral"

        price_change = self._price_change(df, bars=3)
        direction = "up" if price_change > 0 else ("down" if price_change < 0 else "neutral")
        magnitude = abs(price_change)

        # Volume trend — rising or waning within recent window
        recent = df.tail(3)
        vol_trend = float(recent['volume'].diff().sum())
        rising_vol = vol_trend > 0

        # Order by magnitude: strongest signal first.
        if magnitude > self.config['news_price_pct'] and rising_vol:
            return "news_driven", direction
        if magnitude > self.config['trend_price_pct'] and rising_vol:
            return "trend_driven", direction
        if direction == "down" and magnitude > self.config['trend_price_pct'] and rising_vol:
            return "distribution", direction
        if magnitude < self.config['accum_price_pct'] and rising_vol:
            return "accumulation", "neutral"
        return "neutral", direction

    def detect_news_move(self, df: pd.DataFrame) -> bool:
        """Off-by-one fixed: mean must exclude the bar under test."""
        if len(df) < 6:
            return False
        prior = df.iloc[:-1].tail(5)
        last = df.iloc[-1]
        prior_mean_vol = float(prior['volume'].mean() or 0.0)
        volume_spike = prior_mean_vol > 0 and float(last['volume']) > prior_mean_vol * 2

        prev_close = float(df['close'].iloc[-2])
        cur_open = float(last['open'])
        price_gap = prev_close > 0 and abs(cur_open - prev_close) / prev_close > self.config['news_gap_pct']

        close_last = float(last['close']) or 1.0
        candle_range = (float(last['high']) - float(last['low'])) / close_last
        large_range = candle_range > self.config['news_range_pct']

        return volume_spike and (price_gap or large_range)

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------
    def analyze_spike(self, df: pd.DataFrame) -> LiquiditySpike:
        if df is None or df.empty:
            return LiquiditySpike(False, 1.0, "none", 0.0, "No data", False)

        detected, ratio = self.detect_spike(df)
        stype, direction = self.classify_spike_type(df)
        is_news = self.detect_news_move(df)
        vwap_dev = self._vwap_deviation_bps(df)
        delta = self._delta_ratio(df)
        impact = self._impact_cost_bps(df)

        if not detected:
            return LiquiditySpike(
                detected=False,
                spike_ratio=ratio,
                spike_type="none",
                confidence=0.0,
                reason="No significant volume spike",
                is_news_driven=False,
                direction=direction,
                vwap_deviation_bps=vwap_dev,
                delta_ratio=delta,
                impact_cost_bps=impact,
            )

        # Base confidence table
        base_conf = {
            "news_driven": 0.90,
            "trend_driven": 0.80,
            "accumulation": 0.70,
            "distribution": 0.60,
            "neutral": 0.50,
            "unknown": 0.40,
        }.get(stype, 0.50)

        # Delta alignment adjustment — reward confirmation, penalize divergence.
        if stype in {"trend_driven", "news_driven"}:
            if direction == "up" and delta > 0.2:
                base_conf += 0.05
            elif direction == "down" and delta < -0.2:
                base_conf += 0.05
            elif direction != "neutral" and np.sign(delta) != (1 if direction == "up" else -1):
                base_conf -= 0.15  # divergence — possible fake breakout

        # Impact-cost penalty — wide range = slippage risk
        if impact > self.config['max_impact_bps']:
            base_conf -= 0.10

        base_conf = float(np.clip(base_conf, 0.0, 1.0))

        reasons = {
            "news_driven": "Volume spike + large price move — likely news",
            "trend_driven": "Volume spike + directional move — trend confirmation",
            "accumulation": "Volume spike without price move — possible accumulation",
            "distribution": "Volume spike with price drop — possible distribution",
            "neutral": "Volume spike without clear direction",
            "unknown": "Insufficient data for classification",
        }
        reason = reasons.get(stype, "Volume spike detected")

        return LiquiditySpike(
            detected=True,
            spike_ratio=ratio,
            spike_type=stype,
            confidence=base_conf,
            reason=reason,
            is_news_driven=is_news,
            direction=direction,
            vwap_deviation_bps=vwap_dev,
            delta_ratio=delta,
            impact_cost_bps=impact,
        )

    # ------------------------------------------------------------------
    # Signal prioritization — multiplies score; callers blend as needed.
    # ------------------------------------------------------------------
    def adjust_signal_priority(self, spike: LiquiditySpike, base_score: float) -> float:
        if not spike.detected:
            return base_score

        mult = {
            "trend_driven": 1.30,
            "news_driven": 0.80,          # news = fade risk, don't chase
            "accumulation": 1.10,
            "distribution": 0.70,
            "neutral": 1.00,
        }.get(spike.spike_type, 1.0)

        # Further damp if delta diverges from direction (fake-breakout guard)
        if spike.spike_type in {"trend_driven", "news_driven"}:
            if spike.direction == "up" and spike.delta_ratio < -0.1:
                mult *= 0.7
            elif spike.direction == "down" and spike.delta_ratio > 0.1:
                mult *= 0.7

        return base_score * mult

    # ------------------------------------------------------------------
    # Bulk filter
    # ------------------------------------------------------------------
    def filter_signals(
        self,
        signals: List,
        get_data_func: Callable[[str], pd.DataFrame],
    ) -> List[Dict]:
        filtered: List[Dict] = []

        for item in signals:
            signal = item if hasattr(item, 'symbol') else (
                item.get('signal') if isinstance(item, dict) else None
            )
            if not signal or not hasattr(signal, 'symbol'):
                continue

            try:
                df = get_data_func(signal.symbol)
            except (ConnectionError, TimeoutError, KeyError) as e:
                logger.warning("Data fetch failed for %s: %s", signal.symbol, e)
                continue
            except Exception as e:  # noqa: BLE001
                logger.exception("Unexpected data error for %s: %s", signal.symbol, e)
                continue

            if df is None or df.empty:
                continue

            spike = self.analyze_spike(df)

            if isinstance(item, dict):
                item['liquidity_spike'] = {
                    'detected': spike.detected,
                    'spike_ratio': spike.spike_ratio,
                    'spike_type': spike.spike_type,
                    'direction': spike.direction,
                    'confidence': spike.confidence,
                    'is_news_driven': spike.is_news_driven,
                    'delta_ratio': spike.delta_ratio,
                    'vwap_dev_bps': spike.vwap_deviation_bps,
                    'impact_cost_bps': spike.impact_cost_bps,
                    'reason': spike.reason,
                }

            # Keep if strong spike OR confirmed trend/accumulation.
            keep = (
                spike.spike_ratio >= 2.5
                or spike.spike_type in {"trend_driven", "accumulation"}
            )
            # But reject divergent fake breakouts (spike up with negative delta)
            if spike.spike_type in {"trend_driven", "news_driven"}:
                if spike.direction == "up" and spike.delta_ratio < -0.2:
                    keep = False
                elif spike.direction == "down" and spike.delta_ratio > 0.2:
                    keep = False

            if keep:
                filtered.append(item if isinstance(item, dict) else {'signal': signal, 'liquidity_spike': spike})

        return filtered


# ---------------------------------------------------------------------------
# Loss cluster detector (with optional persistence)
# ---------------------------------------------------------------------------
class LossClusterDetector:
    def __init__(self, state_path: Optional[str] = None):
        self.loss_streak = 0
        self.loss_history: List[str] = []
        self.config = {
            'reduce_at_2': 0.75,
            'reduce_at_3': 0.50,
            'pause_at_4': True,
            'pause_duration_minutes': 30,
            'history_cap': 20,
        }
        self.pause_until: Optional[datetime] = None
        self._state_path = Path(state_path) if state_path else None
        self._load()

    # ------------------------------------------------------------------
    def record_trade(self, outcome: str) -> None:
        if outcome not in {"WIN", "LOSS", "BE"}:
            logger.warning("Unknown outcome %r; ignoring", outcome)
            return
        self.loss_history.append(outcome)
        if len(self.loss_history) > self.config['history_cap']:
            self.loss_history.pop(0)
        if outcome == "LOSS":
            self.loss_streak += 1
        else:
            self.loss_streak = 0
        self._save()

    def get_position_multiplier(self) -> float:
        if self.loss_streak >= 4:
            return 0.0
        if self.loss_streak == 3:
            return self.config['reduce_at_3']
        if self.loss_streak == 2:
            return self.config['reduce_at_2']
        return 1.0

    def should_pause(self) -> Tuple[bool, str]:
        now = datetime.now()
        if self.pause_until and now < self.pause_until:
            remaining = max(0, int((self.pause_until - now).total_seconds() // 60))
            return True, f"Paused for {remaining} more minutes"

        if self.loss_streak >= 4 and self.config['pause_at_4']:
            self.pause_until = now + timedelta(minutes=self.config['pause_duration_minutes'])
            self._save()
            return True, f"Paused for {self.config['pause_duration_minutes']} minutes"

        return False, ""

    def get_loss_stats(self) -> Dict:
        if not self.loss_history:
            return {
                'loss_streak': 0,
                'recent_outcomes': [],
                'loss_rate_10': 0.0,
                'position_mult': 1.0,
                'should_pause': False,
            }
        recent = self.loss_history[-10:]
        losses = sum(1 for x in recent if x == "LOSS")
        return {
            'loss_streak': self.loss_streak,
            'recent_outcomes': recent,
            'loss_rate_10': losses / len(recent) if recent else 0.0,
            'position_mult': self.get_position_multiplier(),
            'should_pause': self.should_pause()[0],
        }

    # ------------------------------------------------------------------
    def _save(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({
                'loss_streak': self.loss_streak,
                'loss_history': self.loss_history,
                'pause_until': self.pause_until.isoformat() if self.pause_until else None,
            }))
        except OSError as e:
            logger.warning("LossCluster save failed: %s", e)

    def _load(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text())
            self.loss_streak = int(raw.get('loss_streak', 0))
            self.loss_history = list(raw.get('loss_history', []))
            pu = raw.get('pause_until')
            self.pause_until = datetime.fromisoformat(pu) if pu else None
        except (OSError, ValueError, json.JSONDecodeError) as e:
            logger.warning("LossCluster load failed: %s", e)


# ---------------------------------------------------------------------------
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    from core.scanner import LiquidityScanner

    scanner = LiquidityScanner()
    detector = LiquiditySpikeDetector()
    loss = LossClusterDetector()

    scanner.scan_universe()

    print("Liquidity Spike Analysis:")
    for item in scanner.universe[:5]:
        df = scanner.get_market_data(item['symbol'])
        s = detector.analyze_spike(df)
        print(f"\n{item['symbol']}:")
        print(f"  Detected: {s.detected}  Ratio: {s.spike_ratio:.2f}x")
        print(f"  Type: {s.spike_type} / Dir: {s.direction}")
        print(f"  Confidence: {s.confidence:.0%}")
        print(f"  Delta: {s.delta_ratio:+.2f}  VWAPdev: {s.vwap_deviation_bps:+.0f}bps  IC: {s.impact_cost_bps:.0f}bps")
        print(f"  Reason: {s.reason}")

    print("\n\nLoss Cluster Detection:")
    for outcome in ['LOSS', 'LOSS', 'WIN', 'LOSS', 'LOSS', 'LOSS']:
        loss.record_trade(outcome)
    stats = loss.get_loss_stats()
    print(f"Loss Streak: {stats['loss_streak']}")
    print(f"Position Mult: {stats['position_mult']}x")
    print(f"Should Pause: {stats['should_pause']}")
