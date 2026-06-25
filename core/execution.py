import pandas as pd
import numpy as np
import time
from typing import Dict, Optional, List
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
import config
from .risk_engine import Position, RiskEngine, Trade
from .strike_selection import StrikeRecommendation

_IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class OrderResult:
    success: bool
    order_id: str
    filled_price: float
    filled_quantity: int
    message: str
    retry_count: int


class ExecutionEngine:
    def __init__(self, capital: float = 100000):
        self.config = config.EXECUTION_CONFIG
        self.risk = RiskEngine(capital=capital)
        self.pending_orders = []
        self.filled_orders = []
        self.order_id_counter = 0
        self.dhan_api = None
        self.nse_api = None
        self._init_apis()

    def _init_apis(self):
        if not config.USE_MOCK_DATA:
            try:
                from .api_dhan import dhan_api
                self.dhan_api = dhan_api
            except Exception as e:
                print(f"[WARN] Dhan API init failed: {e}")

            try:
                from .api_nse import nse_api
                self.nse_api = nse_api
            except Exception as e:
                print(f"[WARN] NSE API init failed: {e}")

    def generate_order_id(self) -> str:
        self.order_id_counter += 1
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        return f"ORD_{stamp}_{self.order_id_counter:04d}"

    def place_order(self, symbol: str, direction: str, quantity: int,
                  order_type: str = "LIMIT", price: float = None,
                  is_option: bool = False, option_strike: float = None,
                  option_type: str = None, is_futures: bool = False) -> OrderResult:

        for attempt in range(self.config['retry_attempts']):
            try:
                if is_option:
                    order_result = self._execute_option_order(
                        symbol, direction, quantity, order_type, price,
                        option_strike, option_type
                    )
                elif is_futures:
                    order_result = self._execute_futures_order(
                        symbol, direction, quantity, order_type, price
                    )
                else:
                    order_result = self._execute_equity_order(
                        symbol, direction, quantity, order_type, price
                    )

                if order_result.success:
                    return order_result

                time.sleep(self.config['retry_delay'])

            except Exception as e:
                print(f"[ERROR] Order attempt {attempt+1} failed for {symbol}: {e}")
                time.sleep(self.config['retry_delay'])
        
        return OrderResult(
            success=False,
            order_id=self.generate_order_id(),
            filled_price=0,
            filled_quantity=0,
            message=f"Order failed after {self.config['retry_attempts']} attempts",
            retry_count=self.config['retry_attempts']
        )

    def _execute_equity_order(self, symbol: str, direction: str, quantity: int,
                           order_type: str, price: float) -> OrderResult:
        _tx = {"long": "BUY", "short": "SELL"}.get(direction.lower(), direction.upper())

        if config.USE_MOCK_DATA or getattr(config, 'PAPER_TRADE', False) or not self.dhan_api:
            filled_price = price or 2500
            filled_quantity = quantity
            mode = "PAPER" if getattr(config, 'PAPER_TRADE', False) and not config.USE_MOCK_DATA else "MOCK"
            return OrderResult(
                success=True,
                order_id=self.generate_order_id(),
                filled_price=filled_price,
                filled_quantity=filled_quantity,
                message=f"[{mode}] {_tx} {quantity} {symbol} @ {filled_price}",
                retry_count=0
            )

        if self.dhan_api:
            try:
                result = self.dhan_api.place_order(
                    symbol=symbol,
                    exchange="NSE_EQ",
                    transaction_type=_tx,
                    quantity=quantity,
                    order_type=order_type.upper(),
                    price=price
                )

                # Dhan v2 uses "orderId"; v1 compat uses "order_id"
                oid = result.get("orderId") or result.get("order_id")
                if oid:
                    filled_price = (result.get("tradedPrice") or result.get("traded_price")
                                    or result.get("price") or price)
                    return OrderResult(
                        success=True,
                        order_id=str(oid),
                        filled_price=filled_price,
                        filled_quantity=quantity,
                        message=f"{direction.upper()} {quantity} {symbol} @ {filled_price}",
                        retry_count=0
                    )
            except Exception as e:
                print(f"[ERROR] Equity order API call failed for {symbol}: {e}")
        
        return OrderResult(
            success=False,
            order_id=self.generate_order_id(),
            filled_price=0,
            filled_quantity=0,
            message="API order failed",
            retry_count=1
        )

    def _execute_option_order(self, symbol: str, direction: str, quantity: int,
                              order_type: str, price: float, option_strike: float,
                              option_type: str) -> OrderResult:
        _tx = {"long": "BUY", "short": "SELL"}.get(direction.lower(), direction.upper())

        if config.USE_MOCK_DATA or getattr(config, 'PAPER_TRADE', False):
            filled_price = price or 50
            mode = "PAPER" if getattr(config, 'PAPER_TRADE', False) and not config.USE_MOCK_DATA else "MOCK"
            return OrderResult(
                success=True,
                order_id=self.generate_order_id(),
                filled_price=filled_price,
                filled_quantity=quantity,
                message=f"[{mode}] {_tx} {quantity} {symbol} {option_strike}{option_type} @ {filled_price}",
                retry_count=0
            )

        if self.dhan_api:
            try:
                strike_int = int(option_strike)
                expiry = self._next_nse_expiry(symbol)
                option_symbol = f"{symbol}{expiry}{option_type}{strike_int}"

                result = self.dhan_api.place_order(
                    symbol=option_symbol,
                    exchange="NSE_FO",
                    transaction_type=_tx,
                    quantity=quantity,
                    order_type=order_type.upper(),
                    price=price
                )

                oid = result.get("orderId") or result.get("order_id")
                if oid:
                    filled_price = (result.get("tradedPrice") or result.get("traded_price")
                                    or result.get("price") or price)
                    return OrderResult(
                        success=True,
                        order_id=str(oid),
                        filled_price=filled_price,
                        filled_quantity=quantity,
                        message=f"{_tx} {quantity} {option_symbol} @ {filled_price}",
                        retry_count=0
                    )
            except Exception as e:
                print(f"[ERROR] Options order API call failed for {symbol}: {e}")
        
        return OrderResult(
            success=False,
            order_id=self.generate_order_id(),
            filled_price=0,
            filled_quantity=0,
            message="Options order failed",
            retry_count=1
        )

    def _execute_futures_order(self, symbol: str, direction: str, quantity: int,
                               order_type: str, price: float) -> OrderResult:
        """Place a STOCK FUTURES order.

        Unlike options, the future is delta~1 with no theta/IV — direction maps
        straight to BUY/SELL. Quantity is already a lot multiple from the
        futures leg. Live path uses Dhan NSE_FNO segment + the current-month
        futures trading symbol + MARGIN product type.
        """
        _tx = {"long": "BUY", "short": "SELL"}.get(direction.lower(), direction.upper())

        # PAPER / MOCK: simulate the fill (no real order)
        if config.USE_MOCK_DATA or getattr(config, 'PAPER_TRADE', False) or not self.dhan_api:
            filled_price = price or 0
            mode = "PAPER" if getattr(config, 'PAPER_TRADE', False) and not config.USE_MOCK_DATA else "MOCK"
            return OrderResult(
                success=True,
                order_id=self.generate_order_id(),
                filled_price=filled_price,
                filled_quantity=quantity,
                message=f"[{mode}] {_tx} {quantity} {symbol}-FUT @ {filled_price}",
                retry_count=0,
            )

        # LIVE: real Dhan futures order
        if self.dhan_api:
            try:
                expiry = self._next_nse_expiry(symbol)
                fut_symbol = f"{symbol}{expiry}FUT"
                result = self.dhan_api.place_order(
                    symbol=fut_symbol,
                    exchange="NSE_FNO",          # stock futures segment
                    transaction_type=_tx,
                    quantity=quantity,
                    order_type=order_type.upper(),
                    price=price,
                    product_type="MARGIN",       # carry-forward futures (not INTRADAY)
                )
                oid = result.get("orderId") or result.get("order_id")
                if oid:
                    filled_price = (result.get("tradedPrice") or result.get("traded_price")
                                    or result.get("price") or price)
                    return OrderResult(
                        success=True,
                        order_id=str(oid),
                        filled_price=filled_price,
                        filled_quantity=quantity,
                        message=f"{_tx} {quantity} {fut_symbol} @ {filled_price}",
                        retry_count=0,
                    )
            except Exception as e:
                print(f"[ERROR] Futures order API call failed for {symbol}: {e}")

        return OrderResult(
            success=False,
            order_id=self.generate_order_id(),
            filled_price=0,
            filled_quantity=0,
            message="Futures order failed",
            retry_count=1,
        )

    @staticmethod
    def _next_nse_expiry(symbol: str = "") -> str:
        """Return nearest NSE F&O expiry as DDMONYY string, holiday-adjusted."""
        from .nse_calendar import expiry_str_ddmonyy
        return expiry_str_ddmonyy(symbol or "NIFTY")

    def calculate_slippage(self, price: float, direction: str) -> float:
        slippage = price * self.config['slippage_tolerance']
        
        if direction == "long":
            return price + slippage
        else:
            return price - slippage

    def execute_trade(self, symbol: str, direction: str, capital: float,
                   entry_price: float, atr: float, strike: Optional[StrikeRecommendation] = None,
                   use_options: bool = False, use_futures: bool = None,
                   entry_volume: float = 0.0, entry_vol_avg: float = 0.0,
                   mark_prices: Optional[Dict[str, float]] = None,
                   option_expiry: str = "") -> Optional[OrderResult]:
        # GAP #3: option_expiry (YYYY-MM-DD) is carried from the signal dict by
        # the caller (live_runner / aladdin_runner) so the risk engine can enforce
        # the per-expiry concentration cap. Defaults to "" (non-option / unknown)
        # which the expiry cap degrades gracefully to pass-through.
        # GAP #2: mark_prices should be the live last-price of each open position.
        # The caller (live_runner._run_pipeline) is responsible for supplying these.
        # If not supplied, validate_trade falls back to realized-only (legacy safe).
        if use_options and strike is None:
            return None
        # Default instrument from config when caller doesn't specify. Futures is
        # the default expression of a directional swing (no theta/IV tax).
        if use_futures is None:
            use_futures = (getattr(config, "INSTRUMENT_MODE", "futures") == "futures"
                           and not use_options)
        # ATR-based SL (adapts to stock volatility); fallback to % if ATR unavailable
        if atr and atr > 0:
            atr_mult = config.SIGNAL_CONFIG.get('atr_multiplier', 1.5)
            sl_distance = atr * atr_mult
        else:
            sl_pct = config.RISK_CONFIG.get('sl_pct', 0.10)
            sl_distance = entry_price * sl_pct
        sl_price = entry_price - sl_distance if direction == "long" else entry_price + sl_distance
        min_rr = config.RISK_CONFIG.get('min_risk_reward', 1.5)
        target_price = (entry_price + sl_distance * min_rr if direction == "long"
                        else entry_price - sl_distance * min_rr)

        if use_options:
            quantity = self._calculate_option_quantity(capital, strike.premium, symbol)
            option_strike = strike.strike_price
            option_type = strike.option_type
            entry_with_slip = entry_price
            premium = strike.premium
        elif use_futures:
            # Trade in lot multiples; risk-based lots if available, else 1 lot.
            quantity = self._calculate_futures_quantity(capital, entry_price, sl_price, symbol)
            option_strike = None
            option_type = None
            entry_with_slip = self.calculate_slippage(entry_price, direction)
            premium = 0
        else:
            quantity = self.risk.calculate_quantity(capital, entry_price, sl_price, symbol=symbol)
            option_strike = None
            option_type = None
            entry_with_slip = self.calculate_slippage(entry_price, direction)
            premium = 0

        if quantity <= 0:
            return None

        position = Position(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            quantity=quantity,
            sl_price=sl_price,
            target_price=target_price,
            premium=premium,
            option_strike=option_strike or 0,
            option_type=option_type or "",
            entry_atr=atr,
            entry_volume=entry_volume,
            entry_vol_avg=entry_vol_avg,
            option_expiry=option_expiry,   # GAP #3: for expiry concentration cap
        )

        validation = self.risk.validate_trade(position, entry_price, force_allowed=True,
                                              mark_prices=mark_prices)

        if not validation['is_valid']:
            return None

        order_type = self.config['order_type'].lower()

        if use_options:
            result = self.place_order(
                symbol, direction, quantity, order_type, strike.premium,
                is_option=True, option_strike=option_strike, option_type=option_type
            )
        elif use_futures:
            result = self.place_order(
                symbol, direction, quantity, order_type, entry_with_slip,
                is_futures=True
            )
        else:
            result = self.place_order(
                symbol, direction, quantity, order_type, entry_with_slip
            )

        if result.success:
            self.risk.open_position(position, force_allowed=True)

        return result

    def _calculate_futures_quantity(self, capital: float, entry: float,
                                    sl: float, symbol: str = '') -> int:
        """Lots of stock futures sized by fixed-fractional risk.

        Risk a constant % of capital per trade (RISK_CONFIG.max_risk_per_trade).
        lots = floor( (capital * risk%) / (per-unit risk * lot_size) ), min 1 lot.
        This gives EVEN rupee risk per trade instead of a flat 1-lot everywhere.
        """
        lot_size = int(getattr(config, 'NSE_LOT_SIZES', {}).get(symbol.upper(), 1))
        lot_size = max(lot_size, 1)
        risk_per_unit = abs(entry - sl)
        if risk_per_unit <= 0:
            return lot_size  # 1 lot fallback
        risk_pct = config.RISK_CONFIG.get('max_risk_per_trade', 0.01)
        risk_budget = capital * risk_pct
        risk_per_lot = risk_per_unit * lot_size
        if risk_per_lot <= 0:
            return lot_size
        lots = int(risk_budget // risk_per_lot)
        return max(1, lots) * lot_size

    def _calculate_option_quantity(self, capital: float, premium: float,
                                    symbol: str = '') -> int:
        if premium <= 0:
            return 0
        risk_pct = config.RISK_CONFIG['max_risk_per_trade']
        raw = int(capital * risk_pct / premium)
        lot_size = getattr(config, 'NSE_LOT_SIZES', {}).get(symbol, 1)
        if lot_size <= 1:
            return max(1, raw)
        return max(lot_size, (raw // lot_size) * lot_size)

    def close_position(self, symbol: str, exit_price: float,
                    reason: str = "signal") -> Optional[Trade]:
        return self.risk.close_position(symbol, exit_price, reason)

    def trail_stop_loss(self, position: Position, current_price: float,
                     atr: float, direction: str) -> float:
        """Progressive trailing SL — activates early, tightens as profit grows.

        Old: waited for 2 ATR profit → 82% trades expired before trail kicked in.
        New: 3-tier system:
          0.5 ATR profit → trail 1.5 ATR behind (lock ~breakeven)
          1.0 ATR profit → trail 1.0 ATR behind (lock partial gain)
          1.5 ATR profit → trail 0.7 ATR behind (tight, capture most of move)
        """
        if atr <= 0:
            return position.sl_price

        if direction == "long":
            profit_atr = (current_price - position.entry_price) / atr
            if profit_atr >= 1.5:
                new_sl = current_price - atr * 0.7
            elif profit_atr >= 1.0:
                new_sl = current_price - atr * 1.0
            elif profit_atr >= 0.5:
                new_sl = current_price - atr * 1.5
            else:
                return position.sl_price
            return max(new_sl, position.sl_price)
        else:
            profit_atr = (position.entry_price - current_price) / atr
            if profit_atr >= 1.5:
                new_sl = current_price + atr * 0.7
            elif profit_atr >= 1.0:
                new_sl = current_price + atr * 1.0
            elif profit_atr >= 0.5:
                new_sl = current_price + atr * 1.5
            else:
                return position.sl_price
            return min(new_sl, position.sl_price)

    def _check_volume_exit(self, position: Position, df, current_price: float) -> bool:
        """Exit when volume dries up after inflow surge (while in profit)."""
        vec = config.VOLUME_EXIT_CONFIG
        if df is None or len(df) < vec['vol_surge_lookback']:
            return False

        avg_vol = df['volume'].rolling(vec['vol_surge_lookback']).mean().iloc[-1]
        current_vol = float(df['volume'].iloc[-1])
        if avg_vol <= 0:
            return False

        vol_ratio = current_vol / avg_vol

        min_profit = vec['profit_required_pct']
        if position.direction == "long":
            in_profit = current_price > position.entry_price * (1 + min_profit)
        else:
            in_profit = current_price < position.entry_price * (1 - min_profit)

        if not in_profit:
            return False

        was_vol_surge = (position.entry_vol_avg > 0 and
                         position.entry_volume > position.entry_vol_avg * 1.5)
        vol_drying = vol_ratio < vec['exit_vol_threshold']

        return was_vol_surge and vol_drying

    def _check_time_exit(self, position: Position, current_price: float) -> Optional[str]:
        """Time/scratch exits — kill the 46% TIME_EXIT bleed.

        Journal data (367 trades): 46% of trades neither hit target nor SL,
        they chop sideways while option theta decays. Losers showed +0.26%
        peak then reversed to −0.94% SL. Winners ran fast (+1.25% MFE,
        −0.32% MAE). So:

          1. EOD exit (15:10 IST) — no overnight F&O risk
          2. FAST time-stop: >35min AND below +0.3R → trade isn't working,
             cut before theta + reversal eat it (was 60min/−0.7%)
          3. SCRATCH exit: trade went green (peak ≥ +0.4%) then faded back
             to ≤ +0.05% → exit flat instead of riding to SL
          4. Profit time exit: >75min, >1% profit, partial booked
          5. Stale: >120min and going nowhere (was 180min)
        """
        now = datetime.now(_IST)
        elapsed = (now - position.entry_time.replace(tzinfo=_IST)).total_seconds()
        elapsed_min = elapsed / 60.0

        # EOD forced exit — no overnight F&O risk
        if now.hour >= 15 and now.minute >= 10:
            return "EOD_EXIT"

        if position.direction == "long":
            pnl_pct = (current_price - position.entry_price) / position.entry_price
        else:
            pnl_pct = (position.entry_price - current_price) / position.entry_price

        # Risk unit (R) — prefer entry SL distance, fallback ATR, then 1%
        risk = 0.0
        if getattr(position, "entry_sl", 0):
            risk = abs(position.entry_price - position.entry_sl) / position.entry_price
        if risk <= 0 and getattr(position, "entry_atr", 0):
            risk = position.entry_atr / position.entry_price
        if risk <= 0:
            risk = 0.01
        r_mult = pnl_pct / risk if risk > 0 else 0.0

        # Track peak favorable pnl for scratch logic
        peak = getattr(position, "_peak_pnl_pct", 0.0)
        if pnl_pct > peak:
            peak = pnl_pct
            try:
                position._peak_pnl_pct = peak
            except Exception:
                pass

        # ── 2. FAST time-stop — not working within 35min ─────────────────
        # Most real breakouts move within 2-3 bars (10-15min). If after
        # 35min we're below +0.3R, the thesis failed — cut now.
        if elapsed_min >= 35 and r_mult < 0.3 and not getattr(position, "partial_booked", False):
            return "FAST_TIME_EXIT"

        # ── 3. SCRATCH exit — gave back the gain ─────────────────────────
        # Went green ≥ +0.4% then collapsed back to flat. Don't donate it
        # back to the SL. Exit at scratch.
        if peak >= 0.004 and pnl_pct <= 0.0005 and not getattr(position, "partial_booked", False):
            return "SCRATCH_EXIT"

        # ── 4. Profitable but stagnating (partial booked already) ────────
        if elapsed_min >= 75 and pnl_pct > 0.010 and getattr(position, 'partial_booked', False):
            return "TIME_PROFIT_EXIT"

        # ── 5. Stale — 2hrs+ and going nowhere ───────────────────────────
        if elapsed_min >= 120 and pnl_pct < 0.005:
            return "STALE_EXIT"

        return None

    def manage_open_positions(self, get_price_func, get_data_func=None) -> List[Dict]:
        closed = []

        positions = list(self.risk.get_open_positions())

        for position in positions:
            current_price = get_price_func(position.symbol)

            if current_price is None:
                continue

            # Priority 1: SL hit — always exit
            if self.risk.check_sl_hit(position, current_price):
                result = self.close_position(position.symbol, current_price, "SL_HIT")
                closed.append({
                    'symbol': position.symbol,
                    'action': 'CLOSED',
                    'reason': 'SL_HIT',
                    'trade': result
                })

            # Priority 2: Target hit — partial at T1, runner aims for T2
            elif self.risk.check_target_hit(position, current_price):
                if not position.partial_booked and position.quantity > 1:
                    # T1 hit: book 50%, move SL to breakeven, let runner aim for T2
                    half_qty = position.quantity // 2
                    position.quantity -= half_qty
                    position.partial_booked = True
                    position.sl_price = position.entry_price  # breakeven SL = risk-free runner

                    # Runner target: entry + 2.5R (not current + 1.2R)
                    # This lets runner capture the full move, not just next tiny leg
                    risk = abs(position.entry_price - position.entry_sl) if hasattr(position, 'entry_sl') else (position.entry_atr or position.entry_price * 0.01)
                    if position.direction == "long":
                        position.target_price = position.entry_price + risk * 2.5
                    else:
                        position.target_price = position.entry_price - risk * 2.5
                    closed.append({
                        'symbol': position.symbol,
                        'action': 'PARTIAL',
                        'reason': 'PARTIAL_T1',
                        'trade': None,
                        'partial_qty': half_qty,
                        'partial_price': current_price,
                    })
                else:
                    result = self.close_position(position.symbol, current_price, "TARGET_HIT")
                    closed.append({
                        'symbol': position.symbol,
                        'action': 'CLOSED',
                        'reason': 'TARGET_HIT',
                        'trade': result
                    })

            else:
                # Priority 3: Time-based exit — prevents 82% EXPIRED trades
                time_reason = self._check_time_exit(position, current_price)
                if time_reason:
                    result = self.close_position(position.symbol, current_price, time_reason)
                    closed.append({
                        'symbol': position.symbol,
                        'action': 'CLOSED',
                        'reason': time_reason,
                        'trade': result
                    })
                    continue

                # Priority 4: Volume dry-up exit
                df = get_data_func(position.symbol) if get_data_func else None
                if df is not None and self._check_volume_exit(position, df, current_price):
                    result = self.close_position(position.symbol, current_price, "VOLUME_EXIT")
                    closed.append({
                        'symbol': position.symbol,
                        'action': 'CLOSED',
                        'reason': 'VOLUME_EXIT',
                        'trade': result
                    })
                else:
                    # Priority 5: Trail SL (now activates at 0.5 ATR, not 2 ATR)
                    atr = position.entry_atr if position.entry_atr > 0 else position.entry_price * 0.02
                    position.sl_price = self.trail_stop_loss(position, current_price, atr, position.direction)

        return closed

    def get_open_positions(self) -> List[Position]:
        return self.risk.get_open_positions()

    def get_risk_stats(self) -> Dict:
        return self.risk.get_stats()


if __name__ == '__main__':
    exec_engine = ExecutionEngine()
    
    print("Testing execution engine...")
    
    strike = StrikeRecommendation(
        strike_type="ATM",
        strike_price=2500,
        option_type="CE",
        premium=50,
        delta=0.5,
        risk_reward=3,
        reasoning="Test"
    )
    
    result = exec_engine.execute_trade(
        symbol="RELIANCE",
        direction="long",
        capital=100000,
        entry_price=2500,
        atr=50,
        strike=strike,
        use_options=False
    )
    
    if result:
        print(f"Order result: {result.message}")
    else:
        print("Trade not executed")
    
    print(f"\nRisk stats: {exec_engine.get_risk_stats()}")
