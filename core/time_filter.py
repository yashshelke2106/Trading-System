from typing import Dict, Tuple, Optional
from datetime import datetime, time
from dataclasses import dataclass
from enum import Enum


class SessionType(Enum):
    OPEN_AUCTION = "open_auction"
    FIRST_HOUR = "first_hour"
    DEAD_ZONE = "dead_zone"
    POWER_HOUR = "power_hour"
    CLOSING_HOUR = "closing_hour"
    AFTER_HOURS = "after_hours"


@dataclass
class TimeContext:
    session: SessionType
    time_obj: datetime
    trade_multiplier: float
    breakout_allowance: float
    min_confidence: float
    reason: str


class TimeFilter:
    def __init__(self):
        self.sessions = {
            SessionType.OPEN_AUCTION: {
                'start': time(9, 15),
                'end': time(9, 30),
                'trade_mult': 0.5,
                'breakout_mult': 0.7,
                'min_conf': 0.7,
                'description': 'High volatility open - reduce size'
            },
            SessionType.FIRST_HOUR: {
                'start': time(9, 30),
                'end': time(10, 30),
                'trade_mult': 1.0,
                'breakout_mult': 1.0,
                'min_conf': 0.5,
                'description': 'Best trading hour - full system'
            },
            SessionType.DEAD_ZONE: {
                # 10:30 → 13:30. Previously started at 11:30, leaving a 1-hour
                # gap (10:30–11:30) silently classified as AFTER_HOURS.
                'start': time(10, 30),
                'end': time(13, 30),
                'trade_mult': 0.3,
                'breakout_mult': 0.5,
                'min_conf': 0.7,
                'description': 'Low movement - avoid new trades'
            },
            SessionType.POWER_HOUR: {
                'start': time(14, 30),
                'end': time(15, 15),
                'trade_mult': 1.2,
                'breakout_mult': 1.2,
                'min_conf': 0.4,
                'description': 'Best breakout hour - increase exposure'
            },
            SessionType.CLOSING_HOUR: {
                'start': time(15, 15),
                'end': time(15, 30),
                'trade_mult': 0.5,
                'breakout_mult': 0.8,
                'min_conf': 0.6,
                'description': 'Close only - avoid new entries'
            },
            SessionType.AFTER_HOURS: {
                'start': time(15, 30),
                'end': time(23, 59),
                'trade_mult': 0,
                'breakout_mult': 0,
                'min_conf': 1.0,
                'description': 'Market closed - no trades'
            }
        }
        
        self.weekdays = [0, 1, 2, 3, 4]

    def get_session(self, current_time: datetime = None) -> SessionType:
        if current_time is None:
            current_time = datetime.now()
        
        if current_time.weekday() not in self.weekdays:
            return SessionType.AFTER_HOURS
        
        time_obj = current_time.time()
        
        for session, config in self.sessions.items():
            if config['start'] <= time_obj < config['end']:
                return session
        
        return SessionType.AFTER_HOURS

    def get_time_context(self, current_time: datetime = None) -> TimeContext:
        if current_time is None:
            current_time = datetime.now()
        
        session = self.get_session(current_time)
        config = self.sessions[session]
        
        time_remaining = self._get_time_remaining(session, current_time)
        
        if session == SessionType.FIRST_HOUR and time_remaining < 15:
            trade_mult = 0.8
            reason = "First hour ending soon"
        elif session == SessionType.POWER_HOUR and time_remaining > 20:
            trade_mult = config['trade_mult'] * 1.1
            reason = config['description']
        elif session == SessionType.DEAD_ZONE:
            trade_mult = config['trade_mult']
            if time_remaining < 30:
                trade_mult = 0.5
            reason = config['description']
        else:
            trade_mult = config['trade_mult']
            reason = config['description']
        
        return TimeContext(
            session=session,
            time_obj=current_time,
            trade_multiplier=trade_mult,
            breakout_allowance=config['breakout_mult'],
            min_confidence=config['min_conf'],
            reason=reason
        )

    def _get_time_remaining(self, session: SessionType, current_time: datetime) -> int:
        config = self.sessions[session]
        end_time = datetime.combine(current_time.date(), config['end'])
        remaining = (end_time - current_time).total_seconds() / 60
        return max(0, int(remaining))

    def should_trade(self, signal_strength: float = 0.5,
                    breakout: bool = False,
                    current_time: datetime = None) -> Tuple[bool, TimeContext]:
        context = self.get_time_context(current_time)
        
        if context.session == SessionType.AFTER_HOURS:
            return False, context
        
        if context.session == SessionType.DEAD_ZONE:
            if breakout and signal_strength > 0.8:
                return True, context
            return False, context
        
        if context.session == SessionType.CLOSING_HOUR:
            if breakout and signal_strength > 0.7:
                return True, context
            return False, context
        
        if context.session == SessionType.OPEN_AUCTION:
            if signal_strength > context.min_confidence:
                return True, context
            return False, context
        
        adjusted_strength = signal_strength * context.trade_multiplier
        
        if breakout:
            adjusted_strength *= context.breakout_allowance
        
        if adjusted_strength >= context.min_confidence:
            return True, context
        
        return False, context

    def get_trade_multiplier(self, current_time: datetime = None) -> float:
        context = self.get_time_context(current_time)
        return context.trade_multiplier

    def get_breakout_multiplier(self, current_time: datetime = None) -> float:
        context = self.get_time_context(current_time)
        return context.breakout_allowance

    def is_best_session(self, current_time: datetime = None) -> bool:
        session = self.get_session(current_time)
        return session == SessionType.POWER_HOUR or session == SessionType.FIRST_HOUR

    def get_session_stats(self, current_time: datetime = None) -> Dict:
        if current_time is None:
            current_time = datetime.now()
        
        session = self.get_session(current_time)
        config = self.sessions[session]
        
        return {
            'current_session': session.value,
            'trade_multiplier': config['trade_mult'],
            'breakout_multiplier': config['breakout_mult'],
            'min_confidence': config['min_conf'],
            'description': config['description'],
            'is_trading_hours': session != SessionType.AFTER_HOURS,
            'is_best_hour': session in [SessionType.FIRST_HOUR, SessionType.POWER_HOUR],
            'is_avoid_hour': session in [SessionType.DEAD_ZONE, SessionType.AFTER_HOURS],
        }


if __name__ == '__main__':
    tf = TimeFilter()
    
    print("Time Filter Analysis:")
    
    test_times = [
        datetime(2026, 4, 22, 9, 20),
        datetime(2026, 4, 22, 10, 0),
        datetime(2026, 4, 22, 12, 0),
        datetime(2026, 4, 22, 14, 45),
        datetime(2026, 4, 22, 15, 20),
    ]
    
    for t in test_times:
        context = tf.get_time_context(t)
        should_trade, _ = tf.should_trade(0.6, False, t)
        
        print(f"\n{t.strftime('%H:%M')} - {context.session.value}")
        print(f"  Trade Mult: {context.trade_multiplier}x")
        print(f"  Breakout Mult: {context.breakout_allowance}x")
        print(f"  Should Trade: {should_trade}")
        print(f"  Reason: {context.reason}")