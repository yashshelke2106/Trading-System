import pandas as pd
import numpy as np
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from datetime import datetime, time, date, timezone, timedelta
import config

_IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> datetime:
    return datetime.now(_IST).replace(tzinfo=None)


@dataclass
class Position:
    symbol: str
    direction: str
    entry_price: float
    quantity: int
    sl_price: float
    target_price: float
    premium: float = 0
    option_strike: float = 0
    option_type: str = ""
    entry_atr: float = 0.0
    entry_volume: float = 0.0
    entry_vol_avg: float = 0.0
    entry_time: datetime = field(default_factory=datetime.now)
    partial_booked: bool = False   # True after 50% booked at T1


@dataclass
class Trade:
    id: str
    timestamp: datetime
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_percent: float
    status: str
    reason: str
    holding_days: int = 0


class RiskEngine:
    def __init__(self, capital: float = 100000):
        self.config = config.RISK_CONFIG
        self.capital = capital          # Track capital for % comparisons
        self.positions: List[Position] = []
        self.trades: List[Trade] = []
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.trades_today = 0
        self.last_reset = date.today()
        # v4 fix#5: per-symbol same-day re-entry block.
        # When a symbol hits SL, no further trades in that symbol until next session.
        # Prevents revenge entries / chasing the same setup that just failed.
        self.symbol_stops_today: set = set()

    def check_market_hours(self, force_allowed: bool = False) -> bool:
        if force_allowed:
            return True
            
        now = _now_ist().time()

        market_open = time(config.MARKET_OPEN_HOUR, config.MARKET_OPEN_MINUTE)
        market_close = time(config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE)

        return market_open <= now <= market_close

    def unrealized_pnl(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        """Open-position MTM at the given marks. 0.0 if no marks supplied."""
        if not mark_prices:
            return 0.0
        total = 0.0
        for pos in self.positions:
            px = mark_prices.get(pos.symbol)
            if px is None:
                continue
            if pos.direction == "long":
                total += (px - pos.entry_price) * pos.quantity
            else:
                total += (pos.entry_price - px) * pos.quantity
        return total

    def check_daily_loss(self, mark_prices: Optional[Dict[str, float]] = None) -> bool:
        # FIX (audit #18): the daily kill-switch must see OPEN losses too — a book
        # bleeding on open positions used to keep opening new trades. When marks
        # are supplied we include unrealized MTM; otherwise realized-only (legacy).
        total_pnl = self.daily_pnl + self.unrealized_pnl(mark_prices)
        loss = max(-total_pnl, 0.0)
        daily_loss_pct = loss / self.capital if self.capital > 0 else 0
        return daily_loss_pct < self.config['max_daily_loss']

    def check_correlation(self, symbol: str,
                          sector_map: Optional[Dict[str, str]] = None) -> bool:
        """FIX (audit #17): cap simultaneous correlated exposure. True if opening
        `symbol` would NOT exceed max_per_sector open positions in its sector.
        Degrades open when sector is unknown."""
        if not symbol:
            return True
        max_per_sector = int(self.config.get('max_per_sector', 2))
        if max_per_sector <= 0:
            return True
        sector_map = sector_map or getattr(config, 'STOCK_SECTOR_MAP', {})
        sec = sector_map.get(symbol.upper())
        if not sec:
            return True  # unmapped → don't block
        open_in_sector = sum(1 for p in self.positions
                             if sector_map.get(p.symbol.upper()) == sec)
        return open_in_sector < max_per_sector

    def check_consecutive_losses(self) -> bool:
        return self.consecutive_losses < self.config['max_consecutive_losses']

    def check_trades_limit(self) -> bool:
        return self.trades_today < self.config['max_trades_per_day']

    def check_symbol_not_stopped(self, symbol: str) -> bool:
        """v4 fix#5: True if symbol hasn't already hit SL this session."""
        return symbol.upper() not in self.symbol_stops_today

    def record_symbol_stop(self, symbol: str) -> None:
        """v4 fix#5: call when a position closes via SL. Blocks re-entry for
        the rest of the session."""
        self.symbol_stops_today.add(symbol.upper())

    def _auto_reset_if_new_day(self) -> None:
        today = _now_ist().date()
        if self.last_reset != today:
            self.reset_daily()
            self.symbol_stops_today.clear()   # v4 fix#5: fresh slate
            self.last_reset = today

    def can_trade(self, symbol: str = "", force_allowed: bool = False,
                  mark_prices: Optional[Dict[str, float]] = None) -> bool:
        self._auto_reset_if_new_day()
        if not self.check_market_hours(force_allowed):
            return False
        if not self.check_daily_loss(mark_prices):   # audit #18: includes open MTM
            return False
        if not self.check_consecutive_losses():
            return False
        if not self.check_trades_limit():
            return False
        # v4 fix#5: per-symbol re-entry block (only enforced when symbol given)
        if symbol and not self.check_symbol_not_stopped(symbol):
            return False
        # audit #17: correlation/concentration cap (only enforced when symbol given)
        if symbol and not self.check_correlation(symbol):
            return False
        return True

    def calculate_quantity(self, capital: float, entry: float,
                           stop_loss: float, risk_percent: float = None,
                           symbol: str = '') -> int:
        risk_pct = risk_percent or self.config['max_risk_per_trade']
        risk_amount = capital * risk_pct
        risk_per_share = abs(entry - stop_loss)
        if risk_per_share == 0:
            return 0
        raw_qty = int(risk_amount / risk_per_share)
        # Prefer the LIVE lot size (scrip master) over the stale static map.
        try:
            from core.futures_leg import lot_size_for as _lsf
            lot_size = _lsf(symbol) if symbol else 1
        except Exception:
            lot_size = getattr(config, 'NSE_LOT_SIZES', {}).get(symbol, 1)
        if lot_size <= 1:
            return max(1, raw_qty)
        # round down to nearest lot; ensure at least 1 lot
        return max(lot_size, (raw_qty // lot_size) * lot_size)

    def calculate_stop_loss(self, entry: float, direction: str, atr: float = None) -> float:
        # ATR-based SL when available; bounded by min/max % of entry to beat noise / cap risk.
        atr_mult = self.config.get('atr_sl_multiplier', 1.5)
        min_pct  = self.config.get('min_sl_pct', 0.004)
        max_pct  = self.config.get('max_sl_pct', 0.025)
        if atr and atr > 0:
            sl_distance = atr * atr_mult
        else:
            sl_distance = entry * self.config.get('sl_pct', 0.05)
        sl_distance = max(entry * min_pct, min(sl_distance, entry * max_pct))
        if direction == "long":
            return entry - sl_distance
        else:
            return entry + sl_distance

    def calculate_target(self, entry: float, direction: str, sl: float,
                      min_rr: float = None) -> float:
        rr = min_rr or self.config['min_risk_reward']
        
        risk = abs(entry - sl)
        reward = risk * rr
        
        if direction == "long":
            return entry + reward
        else:
            return entry - reward

    def validate_trade(self, position: Position, current_price: float, force_allowed: bool = False) -> Dict:
        reasons = []
        
        if not self.check_market_hours(force_allowed):
            reasons.append("Outside market hours")
        
        if not self.check_daily_loss():
            reasons.append("Daily loss limit reached")
        
        if not self.check_consecutive_losses():
            reasons.append("Max consecutive losses reached")
        
        if not self.check_trades_limit():
            reasons.append("Daily trades limit reached")
        
        if position.quantity <= 0:
            reasons.append("Invalid quantity")

        # Use abs so the check works for short positions too (sl > entry).
        sl_dist = abs(position.entry_price - position.sl_price)
        if sl_dist == 0:
            reasons.append("SL equals entry price")
            return {'is_valid': False, 'reasons': reasons, 'can_trade': False}

        rr = abs(position.target_price - position.entry_price) / sl_dist
        
        if rr < self.config['min_risk_reward']:
            reasons.append(f"R:R below {self.config['min_risk_reward']}")
        
        is_valid = len(reasons) == 0
        
        return {
            'is_valid': is_valid,
            'reasons': reasons,
            'can_trade': self.can_trade(force_allowed=force_allowed)
        }

    def open_position(self, position: Position, force_allowed: bool = False) -> bool:
        validation = self.validate_trade(position, position.entry_price, force_allowed=force_allowed)

        if not validation['is_valid']:
            return False

        self.positions.append(position)
        self.trades_today += 1

        return True

    def close_position(self, symbol: str, exit_price: float, reason: str = "signal") -> Optional[Trade]:
        position = None
        
        for pos in self.positions:
            if pos.symbol == symbol:
                position = pos
                break
        
        if not position:
            return None
        
        if position.direction == "long":
            pnl = (exit_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - exit_price) * position.quantity
        
        pnl_percent = (pnl / (position.entry_price * position.quantity)) * 100
        
        status = "WIN" if pnl > 0 else "LOSS"
        elapsed = datetime.now() - position.entry_time
        holding_days = max(1, int(elapsed.total_seconds() / 86400)) if elapsed.total_seconds() >= 60 else 0

        trade = Trade(
            id=f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            timestamp=datetime.now(),
            symbol=symbol,
            direction=position.direction,
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            pnl=pnl,
            pnl_percent=pnl_percent,
            status=status,
            reason=reason,
            holding_days=holding_days,
        )
        
        self.trades.append(trade)
        self.daily_pnl += pnl

        if pnl < 0:
            self.consecutive_losses += 1
            # v4 fix#5: SL/stop exit → block re-entry in this symbol today.
            # Only stop-driven exits arm the block; a signal/target exit does not.
            if reason and ("SL" in reason.upper() or "STOP" in reason.upper()):
                self.record_symbol_stop(symbol)
        else:
            self.consecutive_losses = 0
        
        self.positions = [p for p in self.positions if p.symbol != symbol]
        
        return trade

    def get_open_positions(self) -> List[Position]:
        return self.positions

    def get_stats(self) -> Dict:
        if not self.trades:
            return {
                'total_trades': 0,
                'win_rate': 0,
                'avg_win': 0,
                'avg_loss': 0,
                'daily_pnl': self.daily_pnl,
            }
        
        wins = [t for t in self.trades if t.status == "WIN"]
        losses = [t for t in self.trades if t.status == "LOSS"]
        
        return {
            'total_trades': len(self.trades),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': len(wins) / len(self.trades) * 100,
            'avg_win': sum(t.pnl for t in wins) / len(wins) if wins else 0,
            'avg_loss': sum(t.pnl for t in losses) / len(losses) if losses else 0,
            'daily_pnl': self.daily_pnl,
            'consecutive_losses': self.consecutive_losses,
            'trades_today': self.trades_today,
        }

    def reset_daily(self):
        self.daily_pnl = 0
        self.trades_today = 0

    def check_sl_hit(self, position: Position, current_price: float) -> bool:
        if position.direction == "long":
            return current_price <= position.sl_price
        else:
            return current_price >= position.sl_price

    def check_target_hit(self, position: Position, current_price: float) -> bool:
        if position.direction == "long":
            return current_price >= position.target_price
        else:
            return current_price <= position.target_price


if __name__ == '__main__':
    risk = RiskEngine()
    
    print(f"Can trade: {risk.can_trade()}")
    print(f"Stats: {risk.get_stats()}")
    
    pos = Position(
        symbol="RELIANCE",
        direction="long",
        entry_price=2500,
        quantity=10,
        sl_price=2450,
        target_price=2650
    )
    
    result = risk.validate_trade(pos, 2500)
    print(f"\nTrade validation: {result}")
    
    if result['is_valid']:
        risk.open_position(pos)
        print("Position opened")
    
    print(f"Open positions: {len(risk.get_open_positions())}")
