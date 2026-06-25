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
    # GAP #3: expiry concentration cap. Carry the option expiry date (YYYY-MM-DD
    # string) so the risk engine can count how many open positions share the same
    # weekly/monthly expiry. Empty string = unknown/non-option (never blocked).
    option_expiry: str = ""


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
        # GAP #4: intraday peak-to-trough drawdown halt.
        # _session_peak tracks the highest (realized + unrealized) equity seen
        # today. When the drop from that peak exceeds max_drawdown_halt * capital,
        # can_trade() returns False for the rest of the session.
        self._session_peak_equity: float = 0.0

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

    def _update_session_peak(self, mark_prices: Optional[Dict[str, float]] = None) -> None:
        """Advance the intraday equity high-water mark. Call before any halt check."""
        current_equity = self.daily_pnl + self.unrealized_pnl(mark_prices)
        if current_equity > self._session_peak_equity:
            self._session_peak_equity = current_equity

    def check_drawdown_halt(self, mark_prices: Optional[Dict[str, float]] = None) -> bool:
        """GAP #4: peak-to-trough intraday drawdown guard.

        Returns True (trading allowed) when the drop from today's equity high-water
        mark is within the configured threshold. Returns False (halt) when it
        exceeds max_drawdown_halt * capital.

        Includes unrealized MTM when marks are supplied (same rule as
        check_daily_loss). Falls back to realized-only when marks are absent.
        Disabled (always True) when max_drawdown_halt is 0 or missing.
        """
        halt_ratio = self.config.get('max_drawdown_halt', 0.08)
        if halt_ratio <= 0 or self.capital <= 0:
            return True  # guard disabled
        current_equity = self.daily_pnl + self.unrealized_pnl(mark_prices)
        self._update_session_peak(mark_prices)
        drawdown = self._session_peak_equity - current_equity
        drawdown_pct = drawdown / self.capital
        return drawdown_pct < halt_ratio

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

    def check_concurrent_positions(self) -> bool:
        """GAP #3: global ceiling on simultaneous open positions.
        Prevents the sector cap (max_per_sector) from being silently bypassed
        across many sectors — e.g. 2 BANK + 2 IT + 2 METAL + ... = 16 concurrent.
        Returns True (entry allowed) when opening one more would NOT exceed the cap.
        Disabled (always True) when max_concurrent_positions is 0 or missing."""
        cap = int(self.config.get('max_concurrent_positions', 5))
        if cap <= 0:
            return True
        return len(self.positions) < cap

    def check_expiry_concentration(self, expiry: str) -> bool:
        """GAP #3: cap N open positions that all expire on the same date.
        Options sharing one expiry are jointly exposed to a single gap/pin event;
        this limits that correlated tail risk. Returns True (entry allowed) when
        opening one more position for `expiry` would NOT exceed the cap.
        Disabled (always True) when max_per_expiry is 0 or missing, or when
        `expiry` is empty/unknown (non-option or expiry not available)."""
        if not expiry:
            return True  # unknown expiry → don't block; degrade gracefully
        cap = int(self.config.get('max_per_expiry', 3))
        if cap <= 0:
            return True
        count = sum(1 for p in self.positions if p.option_expiry == expiry)
        return count < cap

    def _auto_reset_if_new_day(self) -> None:
        today = _now_ist().date()
        if self.last_reset != today:
            self.reset_daily()
            self.symbol_stops_today.clear()   # v4 fix#5: fresh slate
            self.last_reset = today

    def can_trade(self, symbol: str = "", force_allowed: bool = False,
                  mark_prices: Optional[Dict[str, float]] = None,
                  expiry: str = "") -> bool:
        self._auto_reset_if_new_day()
        if not self.check_market_hours(force_allowed):
            return False
        if not self.check_daily_loss(mark_prices):   # audit #18: includes open MTM
            return False
        # GAP #4: peak-to-trough intraday drawdown halt. Checked after daily-loss
        # so a severe single-trade blow-up is caught by daily-loss first; this gate
        # catches a slower bleed across multiple trades. force_allowed does NOT
        # bypass this — it is a capital-safety rule, not a market-hours override.
        if not self.check_drawdown_halt(mark_prices):
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
        # GAP #3: global concurrent-positions ceiling (symbol-independent)
        if not self.check_concurrent_positions():
            return False
        # GAP #3: per-expiry concentration cap (only enforced when expiry given)
        if expiry and not self.check_expiry_concentration(expiry):
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

    def validate_trade(self, position: Position, current_price: float,
                       force_allowed: bool = False,
                       mark_prices: Optional[Dict[str, float]] = None) -> Dict:
        # GAP #2: accept mark_prices so the daily-loss and drawdown checks see
        # open-position MTM, not just realized P&L. Callers that don't supply
        # marks get the realized-only (legacy) behaviour — no crash, no change.
        reasons = []

        if not self.check_market_hours(force_allowed):
            reasons.append("Outside market hours")

        if not self.check_daily_loss(mark_prices):
            reasons.append("Daily loss limit reached")

        if not self.check_drawdown_halt(mark_prices):
            reasons.append("Intraday drawdown halt triggered")

        if not self.check_consecutive_losses():
            reasons.append("Max consecutive losses reached")

        if not self.check_trades_limit():
            reasons.append("Daily trades limit reached")

        # GAP #3: global concurrent-positions ceiling
        if not self.check_concurrent_positions():
            reasons.append("Max concurrent positions reached")

        # GAP #3: per-expiry concentration cap (uses expiry from the candidate position)
        if not self.check_expiry_concentration(position.option_expiry):
            reasons.append(
                f"Max positions for expiry {position.option_expiry} reached"
            )

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
            'can_trade': self.can_trade(force_allowed=force_allowed,
                                        mark_prices=mark_prices,
                                        expiry=position.option_expiry)
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
        self._session_peak_equity = 0.0   # GAP #4: reset peak on new session

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
