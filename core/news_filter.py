import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from enum import Enum


class EventType(Enum):
    EARNINGS = "earnings"
    BOARD_MEETING = "board_meeting"
    DIVIDEND = "dividend"
    FNO_EXPIRY = "fno_expiry"
    INDEX_REBALANCE = "index_rebalance"
    REGULATORY = "regulatory"
    GLOBAL = "global"
    NEWS = "news"
    UNKNOWN = "unknown"


@dataclass
class EventAlert:
    event_type: EventType
    symbol: str
    event_date: datetime
    severity: str
    description: str
    impact_prediction: str
    confidence: float


class EventCalendar:
    def __init__(self):
        self.events: List[EventAlert] = []
        self.fno_expiry_dates = self._get_fno_expiry_dates()

    def _get_fno_expiry_dates(self) -> List[datetime]:
        try:
            from .nse_calendar import fno_monthly_expiry_dates
            return [
                datetime(d.year, d.month, d.day, 15, 30)
                for d in fno_monthly_expiry_dates(months_ahead=3)
            ]
        except Exception:
            # Fallback: raw last-Thursday-of-month
            dates = []
            current = datetime.now()
            for i in range(3):
                expiry = current.replace(day=25) + timedelta(days=30 * i)
                while expiry.weekday() != 3:
                    expiry += timedelta(days=1)
                dates.append(expiry.replace(hour=15, minute=30))
            return dates

    # Stocks where monthly SIAM sales data (released 1st–5th of month) is a catalyst
    AUTO_SALES_SYMBOLS = {
        'BAJAJ-AUTO', 'MARUTI', 'TATAMOTORS', 'HEROMOTOCO', 'EICHERMOT',
        'FORCEMOT', 'BAJAJ-AUTO', 'MAHINDRA',
    }

    # Quarterly results seasons: (start, end)
    RESULTS_SEASONS = {
        'Q1FY26': (date(2025, 7, 15), date(2025, 8, 30)),
        'Q4FY25': (date(2025, 4, 15), date(2025, 5, 30)),
        'Q3FY25': (date(2025, 1, 15), date(2025, 2, 28)),
        'Q2FY25': (date(2024, 10, 15), date(2024, 11, 30)),
    }

    def add_event(self, event: EventAlert):
        self.events.append(event)

    def is_auto_sales_week(self) -> bool:
        """True on 1st–5th of month when SIAM releases auto sales data."""
        return datetime.now().day <= 5

    def is_results_season(self) -> Tuple[bool, str]:
        today = date.today()
        for quarter, (start, end) in self.RESULTS_SEASONS.items():
            if start <= today <= end:
                return True, quarter
        return False, ""

    def get_event_context(self, symbol: str) -> Dict:
        in_results, quarter = self.is_results_season()
        auto_catalyst = symbol in self.AUTO_SALES_SYMBOLS and self.is_auto_sales_week()
        return {
            'auto_sales_week': auto_catalyst,
            'results_season': in_results,
            'results_quarter': quarter,
            'tighten_stop': in_results,          # gap risk during results
            'boost_auto_confidence': auto_catalyst,
        }

    def get_upcoming_events(self, symbol: str, days: int = 7) -> List[EventAlert]:
        now = datetime.now()
        cutoff = now + timedelta(days=days)
        
        return [
            e for e in self.events
            if e.symbol == symbol and e.event_date <= cutoff
        ]

    def is_fno_expiry_week(self) -> bool:
        now = datetime.now()
        
        for expiry in self.fno_expiry_dates:
            days_diff = (expiry - now).days
            
            if 0 <= days_diff <= 5:
                return True
        
        return False

    def get_days_to_expiry(self) -> int:
        now = datetime.now()
        min_days = 999
        
        for expiry in self.fno_expiry_dates:
            if expiry > now:
                days_diff = (expiry - now).days
                min_days = min(min_days, days_diff)
        
        return min_days if min_days < 999 else 0


class NewsEventFilter:
    def __init__(self):
        self.event_calendar = EventCalendar()
        self.config = {
            'news_volume_threshold': 2.5,
            'price_spike_threshold': 0.02,
            'confirmation_candles': 2,
        }

    def detect_unusual_activity(self, df: pd.DataFrame) -> Tuple[bool, str]:
        if len(df) < 10:
            return False, ""
        
        recent = df.tail(5)
        
        vol_spike = recent['volume'].iloc[-1] > recent['volume'].mean() * self.config['news_volume_threshold']
        
        price_change = abs(recent['close'].iloc[-1] - recent['close'].iloc[-3]) / recent['close'].iloc[-3]
        price_spike = price_change > self.config['price_spike_threshold']
        
        candle_range = (recent['high'] - recent['low']).mean() / recent['close'].mean()
        range_spike = candle_range > 0.02
        
        if vol_spike and price_spike:
            return True, "news_driven"
        elif vol_spike and range_spike:
            return True, "volatility_event"
        elif price_spike and not self._has_technical_reason(df):
            return True, "news_move"
        
        return False, ""

    def _has_technical_reason(self, df: pd.DataFrame) -> bool:
        if len(df) < 20:
            return True
        
        recent = df.tail(10)
        
        sma20 = recent['close'].rolling(5).mean()
        sma50 = recent['close'].rolling(10).mean()
        
        price_above_sma = recent['close'].iloc[-1] > sma20.iloc[-1] > sma50.iloc[-1]
        price_below_sma = recent['close'].iloc[-1] < sma20.iloc[-1] < sma50.iloc[-1]
        
        if price_above_sma or price_below_sma:
            return True
        
        if self._is_near_support_resistance(df):
            return True
        
        return False

    def _is_near_support_resistance(self, df: pd.DataFrame) -> bool:
        if len(df) < 20:
            return False
        
        recent_high = df['high'].tail(20).max()
        recent_low = df['low'].tail(20).min()
        current = df['close'].iloc[-1]
        
        upper_zone = recent_high * 0.98
        lower_zone = recent_low * 1.02
        
        return current >= lower_zone and current <= upper_zone

    def detect_news_move(self, df: pd.DataFrame) -> Dict:
        unusual, move_type = self.detect_unusual_activity(df)
        
        if not unusual:
            return {
                'is_news_move': False,
                'confidence': 0,
                'move_type': 'technical',
                'wait_for_confirmation': False,
            }
        
        volume_spike = df['volume'].iloc[-1] > df['volume'].rolling(20).mean().iloc[-1] * 2
        
        price_change = (df['close'].iloc[-1] - df['open'].iloc[-1]) / df['open'].iloc[-1]
        
        if volume_spike and abs(price_change) > 0.01:
            confirmation = False
        else:
            confirmation = True
        
        if move_type == "news_driven":
            confidence = 0.9
        elif move_type == "volatility_event":
            confidence = 0.7
        else:
            confidence = 0.5
        
        return {
            'is_news_move': unusual,
            'confidence': confidence,
            'move_type': move_type,
            'wait_for_confirmation': not confirmation,
            'volume_spike': volume_spike,
            'price_change': price_change,
        }

    def check_event_calendar(self, symbol: str) -> List[EventAlert]:
        return self.event_calendar.get_upcoming_events(symbol)

    def is_high_risk_event(self, symbol: str) -> Tuple[bool, str]:
        events = self.check_event_calendar(symbol)
        
        for event in events:
            if event.severity in ['high', 'critical']:
                return True, f"{event.event_type.value}: {event.description}"
        
        return False, ""

    def should_wait_confirmation(self, df: pd.DataFrame, signal_strength: float) -> Tuple[bool, str]:
        news_analysis = self.detect_news_move(df)
        
        if not news_analysis['is_news_move']:
            return False, ""
        
        if news_analysis['wait_for_confirmation'] and signal_strength < 0.8:
            return True, "Wait for candle confirmation on news move"
        
        if news_analysis['confidence'] > 0.8 and signal_strength < 0.9:
            return True, "High confidence news move - verify with second candle"
        
        return False, ""

    def fetch_news_sentiment(self, symbol: str) -> Dict:
        """Fetch Google News RSS and score headlines. Returns score, signal, headlines."""
        try:
            import feedparser
        except ImportError:
            return {'score': 0, 'signal': 'neutral', 'headlines': [], 'error': 'feedparser not installed'}

        POSITIVE = [
            'order win', 'order received', 'contract', 'results beat', 'record profit',
            'record revenue', 'upgrade', 'buyback', 'dividend', 'capex', 'expansion',
            'joint venture', 'stake acquisition', 'profit up', 'revenue up',
        ]
        NEGATIVE = [
            'tariff', 'probe', 'penalty', 'loss', 'downgrade', 'stake sale',
            'pledge increase', 'default', 'fir', 'sebi notice', 'debt concern',
            'profit down', 'revenue miss', 'warning', 'fraud', 'write-off',
        ]

        try:
            url = (
                f"https://news.google.com/rss/search"
                f"?q={symbol}+NSE+India&hl=en-IN&gl=IN&ceid=IN:en"
            )
            feed = feedparser.parse(url)
            cutoff = datetime.now() - timedelta(hours=36)
            score = 0
            headlines = []

            for entry in feed.entries[:15]:
                try:
                    published = datetime(*entry.published_parsed[:6])
                except Exception:
                    published = datetime.now()

                if published < cutoff:
                    continue

                title = entry.title.lower()
                headlines.append(entry.title)
                for kw in POSITIVE:
                    if kw in title:
                        score += 1
                for kw in NEGATIVE:
                    if kw in title:
                        score -= 1

            signal = 'bullish' if score > 1 else 'bearish' if score < -1 else 'neutral'
            return {
                'score': score,
                'signal': signal,
                'headlines': headlines[:3],
                'confidence_modifier': 1.15 if signal == 'bullish' else 0.75 if signal == 'bearish' else 1.0,
            }
        except Exception as e:
            return {'score': 0, 'signal': 'neutral', 'headlines': [], 'error': str(e)}

    def get_trading_recommendation(self, df: pd.DataFrame, signal: any) -> Dict:
        news = self.detect_news_move(df)
        wait, reason = self.should_wait_confirmation(df, getattr(signal, 'strength', 50) / 100)
        
        expiry_days = self.event_calendar.get_days_to_expiry()
        
        recommendation = "PROCEED"
        
        if wait:
            recommendation = "WAIT"
        elif news['is_news_move'] and news['confidence'] > 0.8:
            recommendation = "REDUCE_SIZE"
        elif expiry_days <= 3:
            recommendation = "EXPIRY_CAUTION"
        
        return {
            'recommendation': recommendation,
            'reason': reason if reason else "Normal conditions",
            'is_news_move': news['is_news_move'],
            'news_confidence': news['confidence'],
            'move_type': news['move_type'],
            'days_to_expiry': expiry_days,
            'is_expiry_week': self.event_calendar.is_fno_expiry_week(),
        }


class MultiTimeframeAnalyzer:
    def __init__(self):
        self.config = {
            'daily_trend_period': 20,
            'weekly_trend_period': 10,
        }

    def get_daily_trend(self, df: pd.DataFrame) -> Tuple[str, float]:
        if len(df) < self.config['daily_trend_period']:
            return "neutral", 0.5
        
        sma20 = df['close'].rolling(20).mean().iloc[-1]
        sma50 = df['close'].rolling(50).mean().iloc[-1]
        current = df['close'].iloc[-1]
        
        returns = df['close'].pct_change().tail(10)
        momentum = returns.mean() / returns.std() * 100 if returns.std() > 0 else 0
        
        if current > sma20 > sma50:
            trend = "strong_up"
            strength = min(abs(momentum) / 10, 1.0)
        elif current > sma20:
            trend = "up"
            strength = 0.6
        elif current < sma20 < sma50:
            trend = "strong_down"
            strength = min(abs(momentum) / 10, 1.0)
        elif current < sma20:
            trend = "down"
            strength = 0.6
        else:
            trend = "neutral"
            strength = 0.3
        
        return trend, strength

    def get_weekly_context(self, weekly_df: pd.DataFrame) -> Tuple[str, float]:
        if weekly_df.empty or len(weekly_df) < 5:
            return "neutral", 0.5
        
        sma10 = weekly_df['close'].rolling(5).mean().iloc[-1]
        current = weekly_df['close'].iloc[-1]
        
        if current > sma10:
            return "up", 0.7
        elif current < sma10:
            return "down", 0.7
        else:
            return "neutral", 0.5

    def get_trend_alignment(self, intraday_trend: str, daily_trend: str) -> Tuple[bool, float]:
        if intraday_trend == daily_trend:
            return True, 1.0
        
        if intraday_trend == "neutral":
            return False, 0.5
        
        if daily_trend == "neutral":
            return True, 0.7
        
        if (intraday_trend in ["up", "strong_up"] and daily_trend in ["down", "strong_down"]) or \
           (intraday_trend in ["down", "strong_down"] and daily_trend in ["up", "strong_up"]):
            return False, 0.3
        
        return False, 0.6

    def align_signal_with_htf(self, signal_direction: str, 
                              htf_trend: str) -> Tuple[bool, float]:
        direction_map = {'long': 'up', 'short': 'down'}
        
        signal_trend = direction_map.get(signal_direction, 'neutral')
        
        aligned, confidence = self.get_trend_alignment(signal_trend, htf_trend)
        
        return aligned, confidence

    def get_multi_tf_summary(self, intraday_df: pd.DataFrame, 
                           daily_df: pd.DataFrame) -> Dict:
        intraday_trend, intraday_strength = self.get_daily_trend(intraday_df)
        daily_trend, daily_strength = self.get_daily_trend(daily_df)
        
        aligned, alignment_conf = self.get_trend_alignment(intraday_trend, daily_trend)
        
        return {
            'intraday_trend': intraday_trend,
            'intraday_strength': intraday_strength,
            'daily_trend': daily_trend,
            'daily_strength': daily_strength,
            'aligned': aligned,
            'alignment_confidence': alignment_conf,
            'action': 'trade' if aligned else 'caution',
            'reason': 'Trends aligned' if aligned else 'Trend divergence detected',
        }


if __name__ == '__main__':
    from core.scanner import LiquidityScanner
    
    scanner = LiquidityScanner()
    news_filter = NewsEventFilter()
    mtf = MultiTimeframeAnalyzer()
    
    scanner.scan_universe()
    
    print("News/Event Filter Analysis:")
    for item in scanner.universe[:3]:
        df = scanner.get_market_data(item['symbol'])
        
        news_analysis = news_filter.detect_news_move(df)
        
        print(f"\n{item['symbol']}:")
        print(f"  Is News Move: {news_analysis['is_news_move']}")
        print(f"  Confidence: {news_analysis['confidence']:.0%}")
        print(f"  Move Type: {news_analysis['move_type']}")
        print(f"  Wait for Confirmation: {news_analysis['wait_for_confirmation']}")
    
    print(f"\n\nFNO Expiry Analysis:")
    print(f"  Is Expiry Week: {news_filter.event_calendar.is_fno_expiry_week()}")
    print(f"  Days to Expiry: {news_filter.event_calendar.get_days_to_expiry()}")
    
    print("\n\nMulti-Timeframe Analysis:")
    for item in scanner.universe[:2]:
        df = scanner.get_market_data(item['symbol'])
        
        summary = mtf.get_multi_tf_summary(df, df)
        
        print(f"\n{item['symbol']}:")
        print(f"  Intraday: {summary['intraday_trend']} ({summary['intraday_strength']:.2f})")
        print(f"  Daily: {summary['daily_trend']} ({summary['daily_strength']:.2f})")
        print(f"  Aligned: {summary['aligned']}")
        print(f"  Action: {summary['action']}")