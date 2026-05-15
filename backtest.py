"""
Backtest engine for the Indian F&O trading system.

Bar-by-bar daily simulation:
  - Signal on bar t close -> fill at bar t+1 open
  - SL/target checked bar-by-bar on subsequent OHLC
  - Commission: 0.1% round trip | Slippage: 0.05% per side
  - Position sizing: 2% risk per trade, 1.5x ATR stop, 3x ATR target
  - Max 5 concurrent positions
  - Warmup: 60 bars before first trade
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import config
from core.signal_engine import SignalEngine
from core.fake_breakout_filter import FakeBreakoutFilter
from core.order_flow import OrderFlowAnalyzer
from core.trade_ranker import TradeRanker
from core.execution_refinement import ExecutionRefiner, EntryRefinement
from core.scanner import LiquidityScanner


COMMISSION_RT   = 0.001   # 0.1% round trip (0.05% each leg)
SLIPPAGE_ENTRY  = 0.0005  # 0.05% unfavourable
SLIPPAGE_EXIT   = 0.0005  # 0.05% unfavourable
WARMUP_BARS     = 60
MAX_POSITIONS   = 5
MAX_POSITION_PCT = 0.20   # max 20% of capital per single trade
DATA_DAYS       = 365     # fetch 1 year OHLCV per symbol
SL_PCT          = 0.10    # 10% price-based stop loss
VOL_EXIT_THRESHOLD = config.VOLUME_EXIT_CONFIG['exit_vol_threshold']
MIN_PROFIT_PCT     = config.VOLUME_EXIT_CONFIG['profit_required_pct']

DEFAULT_SYMBOLS = [
    'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK',
    'SBIN', 'BHARTIARTL', 'KOTAKBANK', 'BAJFINANCE', 'HINDUNILVR',
    'ITC', 'LT', 'AXISBANK', 'MARUTI', 'ASIANPAINT',
]


@dataclass
class BtPosition:
    symbol:        str
    direction:     str
    entry_bar:     int
    entry_date:    date
    fill_price:    float
    sl_price:      float
    target_price:  float
    quantity:      int
    atr:           float
    entry_volume:  float = 0.0
    entry_vol_avg: float = 0.0
    patterns:      List[str] = field(default_factory=list)
    signal_strength: float = 0.0


@dataclass
class BtTrade:
    symbol:       str
    direction:    str
    entry_date:   date
    exit_date:    date
    entry_price:  float
    exit_price:   float
    quantity:     int
    pnl:          float
    pnl_pct:      float
    commission:   float
    holding_days: int
    exit_reason:  str
    patterns:     List[str] = field(default_factory=list)
    signal_strength: float = 0.0


class BacktestEngine:
    def __init__(
        self,
        symbols: List[str] = None,
        capital: float = 100_000,
        verbose: bool = True,
    ):
        self.symbols = symbols or DEFAULT_SYMBOLS
        self.initial_capital = capital
        self.capital = capital
        self.verbose = verbose

        self.signal_engine  = SignalEngine()
        self.fake_filter    = FakeBreakoutFilter()
        self.of_analyzer    = OrderFlowAnalyzer()
        self.trade_ranker   = TradeRanker()
        self.exec_refiner   = ExecutionRefiner()

        self.data:    Dict[str, pd.DataFrame] = {}
        self.dates:   List[date] = []

        self.positions: List[BtPosition] = []
        self.trades:    List[BtTrade]    = []
        self.equity_curve: List[float]   = []
        self.daily_returns: List[float]  = []

    # ─────────────────────────────────────────────────────────────────────
    # Data loading
    # ─────────────────────────────────────────────────────────────────────

    def load_data(self) -> None:
        print(f"\nLoading {DATA_DAYS} days of historical data for {len(self.symbols)} symbols...")
        scanner = LiquidityScanner()

        loaded = 0
        for sym in self.symbols:
            try:
                df = scanner.get_market_data(sym, DATA_DAYS)
                if df.empty or len(df) < WARMUP_BARS + 10:
                    print(f"  [SKIP] {sym}: insufficient data ({len(df)} bars)")
                    continue
                df = df.sort_values('date').reset_index(drop=True)
                self.data[sym] = df
                loaded += 1
                print(f"  {sym:<14} {len(df)} bars  "
                      f"{df['date'].iloc[0].strftime('%Y-%m-%d')} -> "
                      f"{df['date'].iloc[-1].strftime('%Y-%m-%d')}")
            except Exception as e:
                print(f"  [SKIP] {sym}: {e}")

        print(f"\nLoaded: {loaded}/{len(self.symbols)} symbols")

        # Build common date index (union of all dates)
        all_dates = set()
        for df in self.data.values():
            all_dates.update(df['date'].dt.date.tolist())
        self.dates = sorted(all_dates)

    # ─────────────────────────────────────────────────────────────────────
    # Position helpers
    # ─────────────────────────────────────────────────────────────────────

    def _in_position(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.positions)

    def _qty(self, fill_price: float, sl_price: float) -> int:
        risk_amount = self.capital * config.RISK_CONFIG['max_risk_per_trade']
        risk_per_share = abs(fill_price - sl_price)
        if risk_per_share < 0.001 or fill_price < 0.001:
            return 0
        qty_by_risk = int(risk_amount / risk_per_share)
        # Hard cap: no single position > MAX_POSITION_PCT of capital
        max_qty_by_capital = int(self.capital * MAX_POSITION_PCT / fill_price)
        return max(1, min(qty_by_risk, max_qty_by_capital))

    def _fill_cost(self, price: float, qty: int) -> float:
        return price * qty * COMMISSION_RT

    # ─────────────────────────────────────────────────────────────────────
    # Position management on each new bar
    # ─────────────────────────────────────────────────────────────────────

    def _check_positions(self, bar_idx: int) -> None:
        closed = []

        for pos in self.positions:
            sym_df = self.data.get(pos.symbol)
            if sym_df is None or bar_idx >= len(sym_df):
                continue

            sym_dates = sym_df['date'].dt.date.tolist()
            current_date = self.dates[bar_idx]

            # Find this symbol's bar for the current date
            if current_date not in sym_dates:
                continue

            bar_loc = sym_dates.index(current_date)
            bar = sym_df.iloc[bar_loc]

            open_p  = float(bar['open'])
            high_p  = float(bar['high'])
            low_p   = float(bar['low'])
            close_p = float(bar['close'])

            exit_price  = None
            exit_reason = None

            if pos.direction == "long":
                # Gap-down below SL at open
                if open_p <= pos.sl_price:
                    exit_price  = open_p
                    exit_reason = "GAP_SL"
                # SL hit intrabar
                elif low_p <= pos.sl_price:
                    exit_price  = pos.sl_price * (1 - SLIPPAGE_EXIT)
                    exit_reason = "SL_HIT"
                # Target hit intrabar
                elif high_p >= pos.target_price:
                    exit_price  = pos.target_price * (1 - SLIPPAGE_EXIT)
                    exit_reason = "TARGET_HIT"
            else:  # short
                if open_p >= pos.sl_price:
                    exit_price  = open_p
                    exit_reason = "GAP_SL"
                elif high_p >= pos.sl_price:
                    exit_price  = pos.sl_price * (1 + SLIPPAGE_EXIT)
                    exit_reason = "SL_HIT"
                elif low_p <= pos.target_price:
                    exit_price  = pos.target_price * (1 + SLIPPAGE_EXIT)
                    exit_reason = "TARGET_HIT"

            if exit_price is None:
                # Volume exhaustion exit: book profit when inflow dries up
                avg_vol = float(sym_df['volume'].iloc[max(0, bar_loc - 20):bar_loc].mean()) if bar_loc > 0 else 0
                current_vol = float(bar['volume'])
                vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0

                was_vol_surge = (pos.entry_vol_avg > 0 and
                                 pos.entry_volume > pos.entry_vol_avg * 1.5)
                vol_drying = vol_ratio < VOL_EXIT_THRESHOLD

                if pos.direction == "long":
                    in_profit = close_p > pos.fill_price * (1 + MIN_PROFIT_PCT)
                else:
                    in_profit = close_p < pos.fill_price * (1 - MIN_PROFIT_PCT)

                if was_vol_surge and vol_drying and in_profit:
                    exit_price  = close_p * (1 - SLIPPAGE_EXIT) if pos.direction == "long" else close_p * (1 + SLIPPAGE_EXIT)
                    exit_reason = "VOLUME_EXIT"

            if exit_price is not None:
                self._close_position(pos, exit_price, exit_reason, current_date)
                closed.append(pos)
            else:
                self._trail_sl(pos, close_p)

        for pos in closed:
            self.positions.remove(pos)

    def _trail_sl(self, pos: BtPosition, current_price: float) -> None:
        atr = pos.atr
        if pos.direction == "long":
            profit = current_price - pos.fill_price
            if profit > atr * 2:
                new_sl = pos.fill_price + atr * 0.5
                pos.sl_price = max(new_sl, pos.sl_price)
        else:
            profit = pos.fill_price - current_price
            if profit > atr * 2:
                new_sl = pos.fill_price - atr * 0.5
                pos.sl_price = min(new_sl, pos.sl_price)

    def _close_position(
        self,
        pos: BtPosition,
        exit_price: float,
        reason: str,
        exit_date: date,
    ) -> None:
        if pos.direction == "long":
            pnl = (exit_price - pos.fill_price) * pos.quantity
        else:
            pnl = (pos.fill_price - exit_price) * pos.quantity

        commission = self._fill_cost(pos.fill_price, pos.quantity) + self._fill_cost(exit_price, pos.quantity)
        pnl -= commission
        self.capital += pnl

        holding = (exit_date - pos.entry_date).days

        trade = BtTrade(
            symbol        = pos.symbol,
            direction     = pos.direction,
            entry_date    = pos.entry_date,
            exit_date     = exit_date,
            entry_price   = pos.fill_price,
            exit_price    = exit_price,
            quantity      = pos.quantity,
            pnl           = pnl,
            pnl_pct       = pnl / (pos.fill_price * pos.quantity) * 100,
            commission    = commission,
            holding_days  = holding,
            exit_reason   = reason,
            patterns      = pos.patterns,
            signal_strength = pos.signal_strength,
        )
        self.trades.append(trade)

    # ─────────────────────────────────────────────────────────────────────
    # Signal generation and entry
    # ─────────────────────────────────────────────────────────────────────

    def _generate_signals(self, bar_idx: int) -> List[dict]:
        current_date = self.dates[bar_idx]
        signals = []

        for sym in self.data:
            if self._in_position(sym):
                continue

            sym_df = self.data[sym]
            sym_dates = sym_df['date'].dt.date.tolist()

            if current_date not in sym_dates:
                continue

            bar_loc = sym_dates.index(current_date)
            if bar_loc < WARMUP_BARS:
                continue

            df_slice = sym_df.iloc[:bar_loc + 1].copy()

            try:
                signal = self.signal_engine.generate_signal(sym, df_slice)
                if signal is None:
                    continue

                # Fake breakout filter
                filter_result = self.fake_filter.analyze(df_slice, signal)
                if not filter_result.is_valid:
                    continue

                # Entry refinement (VWAP/pullback)
                entry_ctx = self.exec_refiner.refine_entry(df_slice, signal, signal.entry_price)
                if entry_ctx.refinement == EntryRefinement.AVOID:
                    continue

                signals.append({
                    'signal': signal,
                    'filter_result': filter_result,
                    'df_slice': df_slice,
                    'bar_loc': bar_loc,
                })
            except Exception as e:
                if self.verbose:
                    print(f"    [WARN] Signal error {sym}: {e}")

        return signals

    def _enter_trades(self, signal_items: List[dict], bar_idx: int) -> None:
        if not signal_items or len(self.positions) >= MAX_POSITIONS:
            return

        # Order flow ranking
        def get_data(sym):
            items = [it for it in signal_items if it['signal'].symbol == sym]
            if items:
                return items[0]['df_slice']
            return pd.DataFrame()

        try:
            of_ranked = self.of_analyzer.rank_signals(signal_items, get_data)
        except Exception:
            of_ranked = signal_items

        # Trade ranker
        try:
            top = self.trade_ranker.get_top_signals(
                of_ranked,
                n=MAX_POSITIONS - len(self.positions),
            )
        except Exception:
            top = of_ranked[:MAX_POSITIONS - len(self.positions)]

        # Fill at next bar's open
        next_idx = bar_idx + 1
        if next_idx >= len(self.dates):
            return

        next_date = self.dates[next_idx]

        for item in top:
            if len(self.positions) >= MAX_POSITIONS:
                break

            signal = item.get('signal') if isinstance(item, dict) else item
            if signal is None:
                continue

            sym = signal.symbol
            if self._in_position(sym):
                continue

            sym_df   = self.data.get(sym)
            if sym_df is None:
                continue

            sym_dates = sym_df['date'].dt.date.tolist()
            if next_date not in sym_dates:
                continue

            next_loc = sym_dates.index(next_date)
            next_bar = sym_df.iloc[next_loc]
            next_open = float(next_bar['open'])

            # Slippage-adjusted fill
            if signal.direction == "long":
                fill = next_open * (1 + SLIPPAGE_ENTRY)
                sl   = fill * (1 - SL_PCT)
                tp   = fill * (1 + SL_PCT * 3)   # far target; volume exit handles actual exit
            else:
                fill = next_open * (1 - SLIPPAGE_ENTRY)
                sl   = fill * (1 + SL_PCT)
                tp   = fill * (1 - SL_PCT * 3)

            qty = self._qty(fill, sl)
            if qty <= 0:
                continue

            # Get volume data at signal bar for volume-exit tracking
            item_data = next((it for it in signal_items if it.get('signal') is signal or
                              (isinstance(it, dict) and it.get('signal') == signal)), None)
            sig_bar_loc = item_data.get('bar_loc', 0) if isinstance(item_data, dict) else 0
            entry_volume = float(sym_df.iloc[sig_bar_loc]['volume']) if sig_bar_loc > 0 else 0.0
            entry_vol_avg = float(sym_df['volume'].iloc[max(0, sig_bar_loc-20):sig_bar_loc].mean()) if sig_bar_loc > 20 else entry_volume

            pos = BtPosition(
                symbol          = sym,
                direction       = signal.direction,
                entry_bar       = next_idx,
                entry_date      = next_date,
                fill_price      = fill,
                sl_price        = sl,
                target_price    = tp,
                quantity        = qty,
                atr             = signal.atr,
                entry_volume    = entry_volume,
                entry_vol_avg   = entry_vol_avg,
                patterns        = list(signal.patterns),
                signal_strength = signal.strength,
            )
            self.positions.append(pos)

            if self.verbose:
                print(f"  ENTER {sym:<12} {signal.direction.upper():<5}  "
                      f"fill={fill:.2f}  sl={sl:.2f}  tp={tp:.2f}  qty={qty}  "
                      f"str={signal.strength:.0f}  {signal.patterns[:3]}")

    # ─────────────────────────────────────────────────────────────────────
    # Force-close all positions at end of test
    # ─────────────────────────────────────────────────────────────────────

    def _force_close_all(self) -> None:
        last_date = self.dates[-1]
        for pos in list(self.positions):
            sym_df = self.data.get(pos.symbol)
            if sym_df is None:
                continue
            last_close = float(sym_df['close'].iloc[-1])
            self._close_position(pos, last_close, "END_OF_TEST", last_date)
        self.positions.clear()

    # ─────────────────────────────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────────────────────────────

    def run(self) -> Dict:
        if not self.data:
            self.load_data()

        if not self.data:
            print("No data loaded. Abort.")
            return {}

        n_bars = len(self.dates)
        print(f"\nRunning backtest over {n_bars} dates "
              f"({self.dates[0]} -> {self.dates[-1]}) "
              f"with {len(self.data)} symbols\n")

        prev_capital = self.initial_capital

        for bar_idx in range(n_bars):
            current_date = self.dates[bar_idx]

            # Skip weekends (shouldn't exist but just in case)
            if hasattr(current_date, 'weekday') and current_date.weekday() >= 5:
                continue

            if self.verbose and bar_idx % 20 == 0:
                print(f"[{current_date}]  capital={self.capital:,.0f}  "
                      f"open_pos={len(self.positions)}  trades={len(self.trades)}")

            # 1. Check existing positions first
            self._check_positions(bar_idx)

            # 2. Generate new signals (only if warmup done)
            if bar_idx >= WARMUP_BARS:
                signal_items = self._generate_signals(bar_idx)

                # 3. Enter best signals (fill next bar)
                if signal_items:
                    self._enter_trades(signal_items, bar_idx)

            # 4. Mark-to-market equity
            unrealized = 0.0
            for pos in self.positions:
                sym_df = self.data.get(pos.symbol)
                if sym_df is None:
                    continue
                sym_dates = sym_df['date'].dt.date.tolist()
                if current_date in sym_dates:
                    loc = sym_dates.index(current_date)
                    close_p = float(sym_df.iloc[loc]['close'])
                    if pos.direction == "long":
                        unrealized += (close_p - pos.fill_price) * pos.quantity
                    else:
                        unrealized += (pos.fill_price - close_p) * pos.quantity

            equity = self.capital + unrealized
            self.equity_curve.append(equity)
            self.daily_returns.append((equity - prev_capital) / prev_capital)
            prev_capital = equity

        # Close any remaining open positions
        self._force_close_all()

        print(f"\nBacktest complete. Trades: {len(self.trades)}")
        return self.compute_metrics()

    # ─────────────────────────────────────────────────────────────────────
    # Metrics
    # ─────────────────────────────────────────────────────────────────────

    def compute_metrics(self) -> Dict:
        trades = self.trades
        equity = self.equity_curve

        if not trades:
            print("No trades executed.")
            return {}

        wins   = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]

        total_return = (self.capital - self.initial_capital) / self.initial_capital * 100
        win_rate     = len(wins) / len(trades) * 100 if trades else 0

        avg_win  = sum(t.pnl for t in wins)  / len(wins)  if wins   else 0
        avg_loss = sum(t.pnl for t in losses)/ len(losses) if losses else 0

        gross_profit = sum(t.pnl for t in wins)
        gross_loss   = abs(sum(t.pnl for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        expectancy = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)

        # Max drawdown
        peak = self.initial_capital
        max_dd = 0.0
        for eq in equity:
            peak = max(peak, eq)
            dd = (peak - eq) / peak * 100
            max_dd = max(max_dd, dd)

        # Sharpe (annualized, daily returns)
        daily = np.array(self.daily_returns)
        sharpe = (
            float(np.mean(daily) / np.std(daily) * np.sqrt(252))
            if np.std(daily) > 0 else 0.0
        )

        # CAGR
        n_days  = max((self.dates[-1] - self.dates[0]).days, 1) if self.dates else 1
        n_years = n_days / 365.25
        cagr    = ((self.capital / self.initial_capital) ** (1 / n_years) - 1) * 100

        avg_hold = sum(t.holding_days for t in trades) / len(trades) if trades else 0

        total_commission = sum(t.commission for t in trades)

        best_trade  = max(trades, key=lambda t: t.pnl)
        worst_trade = min(trades, key=lambda t: t.pnl)

        # Exit breakdown
        exit_counts: Dict[str, int] = {}
        for t in trades:
            exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1

        # Consecutive max win/loss streaks
        results = [1 if t.pnl > 0 else 0 for t in trades]
        max_win_streak  = _max_streak(results, 1)
        max_loss_streak = _max_streak(results, 0)

        metrics = {
            'total_return_pct':   round(total_return, 2),
            'cagr_pct':           round(cagr, 2),
            'sharpe':             round(sharpe, 2),
            'max_drawdown_pct':   round(max_dd, 2),
            'profit_factor':      round(profit_factor, 2),
            'total_trades':       len(trades),
            'win_rate_pct':       round(win_rate, 1),
            'avg_win_inr':        round(avg_win, 0),
            'avg_loss_inr':       round(avg_loss, 0),
            'expectancy_inr':     round(expectancy, 0),
            'avg_holding_days':   round(avg_hold, 1),
            'max_win_streak':     max_win_streak,
            'max_loss_streak':    max_loss_streak,
            'total_commission':   round(total_commission, 0),
            'final_capital':      round(self.capital, 0),
            'initial_capital':    self.initial_capital,
            'exit_breakdown':     exit_counts,
            'best_trade':         f"{best_trade.symbol} +{best_trade.pnl:.0f}",
            'worst_trade':        f"{worst_trade.symbol} {worst_trade.pnl:.0f}",
        }

        self._print_report(metrics)
        return metrics

    def _print_report(self, m: Dict) -> None:
        print("\n" + "=" * 55)
        print("  BACKTEST RESULTS")
        print("=" * 55)
        print(f"  Capital:        {m['initial_capital']:>10,.0f} -> {m['final_capital']:>10,.0f}")
        print(f"  Total Return:   {m['total_return_pct']:>+10.2f}%")
        print(f"  CAGR:           {m['cagr_pct']:>+10.2f}%")
        print(f"  Sharpe Ratio:   {m['sharpe']:>10.2f}")
        print(f"  Max Drawdown:   {m['max_drawdown_pct']:>10.2f}%")
        print(f"  Profit Factor:  {m['profit_factor']:>10.2f}")
        print("-" * 55)
        print(f"  Total Trades:   {m['total_trades']:>10}")
        print(f"  Win Rate:       {m['win_rate_pct']:>10.1f}%")
        print(f"  Avg Win:        Rs{m['avg_win_inr']:>9,.0f}")
        print(f"  Avg Loss:       Rs{m['avg_loss_inr']:>9,.0f}")
        print(f"  Expectancy:     Rs{m['expectancy_inr']:>9,.0f}")
        print(f"  Avg Hold:       {m['avg_holding_days']:>9.1f} days")
        print(f"  Max Win Streak: {m['max_win_streak']:>10}")
        print(f"  Max Loss Streak:{m['max_loss_streak']:>10}")
        print("-" * 55)
        print(f"  Commission Paid:Rs{m['total_commission']:>9,.0f}")
        print(f"  Best Trade:     {m['best_trade']}")
        print(f"  Worst Trade:    {m['worst_trade']}")
        print(f"  Exit Breakdown: {m['exit_breakdown']}")
        print("=" * 55)

    def save_trades_csv(self, path: str = "backtest_trades.csv") -> None:
        if not self.trades:
            return
        rows = []
        for t in self.trades:
            rows.append({
                'symbol':          t.symbol,
                'direction':       t.direction,
                'entry_date':      t.entry_date,
                'exit_date':       t.exit_date,
                'entry_price':     round(t.entry_price, 2),
                'exit_price':      round(t.exit_price, 2),
                'quantity':        t.quantity,
                'pnl':             round(t.pnl, 2),
                'pnl_pct':         round(t.pnl_pct, 2),
                'commission':      round(t.commission, 2),
                'holding_days':    t.holding_days,
                'exit_reason':     t.exit_reason,
                'signal_strength': round(t.signal_strength, 1),
                'patterns':        ','.join(t.patterns[:5]),
            })
        pd.DataFrame(rows).to_csv(path, index=False)
        print(f"\nTrades saved to: {path}")

    def save_equity_csv(self, path: str = "backtest_equity.csv") -> None:
        if not self.equity_curve or not self.dates:
            return
        n = min(len(self.equity_curve), len(self.dates))
        pd.DataFrame({
            'date':   self.dates[:n],
            'equity': [round(e, 2) for e in self.equity_curve[:n]],
        }).to_csv(path, index=False)
        print(f"Equity curve saved to: {path}")


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _max_streak(results: List[int], target: int) -> int:
    max_s = cur = 0
    for r in results:
        if r == target:
            cur += 1
            max_s = max(max_s, cur)
        else:
            cur = 0
    return max_s


# ─────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    bt = BacktestEngine(
        symbols=DEFAULT_SYMBOLS,
        capital=100_000,
        verbose=True,
    )
    bt.load_data()
    metrics = bt.run()
    bt.save_trades_csv("backtest_trades.csv")
    bt.save_equity_csv("backtest_equity.csv")
