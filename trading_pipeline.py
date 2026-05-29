import pandas as pd
import numpy as np
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from core.auto_repair import AutoRepairEngine
from core.scanner import LiquidityScanner
from core.signal_engine import SignalEngine, Signal
from core.fake_breakout_filter import FakeBreakoutFilter
from core.order_flow import OrderFlowAnalyzer
from core.strike_selection import StrikeSelector
from core.risk_engine import RiskEngine
from core.execution import ExecutionEngine
from core.ai_filter import AIFilter
from core.volatility_engine import VolatilityEngine, MarketConditionAnalyzer, TradingMode
from core.market_bias import MarketBiasEngine, MarketBias
from core.time_filter import TimeFilter
from core.trade_ranker import TradeRanker
from core.liquidity_intelligence import LiquiditySpikeDetector, LossClusterDetector
from core.execution_refinement import ExecutionRefiner, EntryRefinement
from core.options_greeks import GreeksIntelligence
from core.ml_ai import MLTradingAI
from core.news_filter import NewsEventFilter, MultiTimeframeAnalyzer as NewsMTF
from core.trade_logger import TradeLogger


class TradingPipeline:
    def __init__(self, capital: float = 100000):
        self.capital = capital
        self.scanner = LiquidityScanner()
        self.signal_engine = SignalEngine()
        self.fake_filter = FakeBreakoutFilter()
        self.of_analyzer = OrderFlowAnalyzer()
        self.strike_selector = StrikeSelector()
        self.risk = RiskEngine(capital=capital)
        self.execution = ExecutionEngine(capital=capital)
        self.ai_filter = AIFilter()
        self.volatility_engine = VolatilityEngine()
        self.market_analyzer = MarketConditionAnalyzer()
        self.market_bias_engine = MarketBiasEngine()
        self.time_filter = TimeFilter()
        self.trade_ranker = TradeRanker()
        self.spike_detector = LiquiditySpikeDetector()
        self.loss_cluster = LossClusterDetector()
        self.exec_refiner = ExecutionRefiner()
        self.greeks_intel = GreeksIntelligence()
        self.ml_ai = MLTradingAI()
        self.news_filter = NewsEventFilter()
        self.mtf_news = NewsMTF()
        self.trade_logger = TradeLogger()
        
        self.daily_signals = []
        self.executed_trades = []
        self.market_context = None
        self.time_context = None
        self.repair_engine = AutoRepairEngine()

    def run_scanner(self) -> List[Dict]:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Running liquidity scanner...")
        
        universe = self.scanner.scan_universe()
        
        print(f"  Found {len(universe)} liquid stocks")
        
        return universe

    def run_market_bias_analysis(self) -> Dict:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Analyzing market bias...")
        
        context = self.market_bias_engine.get_market_bias()
        self.market_context = context
        
        sector_rotation = self.market_bias_engine.get_sector_rotation()
        
        print(f"  Market Bias: {context.bias.value}")
        print(f"  Trend: {context.trend}")
        print(f"  Strength: {context.strength:.2f}")
        print(f"  NIFTY: {context.nifty_level:.0f} | BANKNIFTY: {context.banknifty_level:.0f}")
        print(f"  Reason: {context.reason}")
        print(f"  Leading Sector: {sector_rotation.get('leading', 'N/A')}")
        
        return {
            'context': context,
            'sector_rotation': sector_rotation,
        }

    def run_time_filter_check(self) -> Dict:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking trading session...")
        
        self.time_context = self.time_filter.get_time_context()
        
        print(f"  Session: {self.time_context.session.value}")
        print(f"  Trade Mult: {self.time_context.trade_multiplier}x")
        print(f"  Min Confidence: {self.time_context.min_confidence}")
        print(f"  Reason: {self.time_context.reason}")
        
        should_trade, _ = self.time_filter.should_trade(
            signal_strength=0.5, 
            breakout=False
        )
        
        if not should_trade:
            print(f"  [WARN] Not ideal time for new trades")
        
        return {
            'context': self.time_context,
            'should_trade': should_trade,
        }

    def run_signal_generation(self, symbols: List[str]) -> List[Signal]:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Generating signals...")
        
        def get_data(symbol):
            return self.scanner.get_market_data(symbol)
        
        signals = self.signal_engine.scan_symbols(symbols, get_data)

        # Session multiplier: power_hour=1.2x, dead_zone=0.3x, first_hour=1.0x, etc.
        if self.time_context and self.time_context.trade_multiplier != 1.0:
            mult = self.time_context.trade_multiplier
            for sig in signals:
                sig.strength = min(sig.strength * mult, 100.0)
            print(f"  Session multiplier: {mult}x applied to strength")

        print(f"  Generated {len(signals)} raw signals")

        return signals

    def run_volatility_filter(self, signals: List[Signal]) -> List[Dict]:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking volatility regime...")
        filtered = []
        for sig in signals:
            try:
                df = self.scanner.get_market_data(sig.symbol)
                if df is None or df.empty:
                    continue
                analysis = self.volatility_engine.analyze_symbol(sig.symbol, df)
                if analysis['should_trade']:
                    filtered.append({'signal': sig, 'volatility': analysis})
            except Exception:
                continue
        print(f"  Passed volatility filter: {len(filtered)}")
        return filtered

    def run_market_breadth(self) -> Dict:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Analyzing market breadth...")
        
        symbols = [item['symbol'] for item in self.scanner.universe[:15]]
        
        analysis = self.market_analyzer.analyze_market_breadth(symbols, self.scanner.get_market_data)
        rec = self.market_analyzer.get_trading_recommendation(analysis)
        
        print(f"  Market: {analysis['market_condition']} | Sentiment: {analysis['sentiment']}")
        print(f"  Regimes: {analysis['regimes']}")
        print(f"  Action: {rec['action']} ({rec['position_size']}x)")
        
        return {
            'market_analysis': analysis,
            'recommendation': rec,
        }

    def run_filters(self, signals: List[Dict]) -> List[Dict]:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Running filters...")

        def get_data(symbol):
            return self.scanner.get_market_data(symbol)

        filtered = self.fake_filter.filter_signals(signals, get_data)
        print(f"  Passed fake breakout filter: {len(filtered)}")

        if not filtered:
            return []

        ranked = self.of_analyzer.rank_signals(filtered, get_data)
        print(f"  Order flow ranked: {len(ranked)}")

        # Hard-reject absorption (sellers absorbing at highs) and exhaustion (dying momentum)
        from core.order_flow import OrderFlowType
        _bad = (OrderFlowType.ABSORPTION, OrderFlowType.EXHAUSTION)
        pre = len(ranked)
        ranked = [item for item in ranked
                  if item.get('order_flow') is None
                  or item['order_flow'].flow_type not in _bad]
        removed = pre - len(ranked)
        if removed:
            print(f"  Removed {removed} absorption/exhaustion signals")

        return ranked

    def run_strike_selection(self, signals: List[Dict]) -> List[Dict]:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Selecting strikes...")
        
        with_strikes = self.strike_selector.batch_select(signals, get_data_func=self.scanner.get_market_data)
        
        print(f"  Selected strikes: {len(with_strikes)}")
        
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Greeks analysis...")
        expiry = datetime.now() + timedelta(days=7)
        
        for item in with_strikes[:5]:
            signal = item.get('signal')
            strike = item.get('strike')
            
            if signal and strike:
                rec = self.greeks_intel.recommend_strike(
                    signal.entry_price,
                    signal.direction,
                    signal.strength / 100,
                    {'iv_rank': 50, 'interpretation': 'normal_iv'}
                )
                
                item['greeks'] = rec
                
                print(f"    {signal.symbol}: {rec['strike_type']} {rec['option_type']} @ {rec['strike']} | Delta: {rec['delta']:.2f}")
        
        return with_strikes

    def run_ai_filter(self, signals: List[Dict]) -> List[Dict]:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Running AI filter...")
        
        ai_filtered = self.ai_filter.filter_signals(signals)
        
        print(f"  Passed AI filter: {len(ai_filtered)}")
        
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Checking news/events...")
        
        def get_data(symbol):
            return self.scanner.get_market_data(symbol)
        
        news_filtered = []
        for item in ai_filtered:
            signal = item.get('signal')
            if not signal:
                continue
            
            df = get_data(signal.symbol)
            if df is None or df.empty:
                continue
            
            rec = self.news_filter.get_trading_recommendation(df, signal)
            
            item['news_check'] = rec
            
            if rec['recommendation'] == 'PROCEED':
                news_filtered.append(item)
            elif rec['recommendation'] == 'REDUCE_SIZE':
                item['news_reduced'] = True
                news_filtered.append(item)
            else:
                print(f"    {signal.symbol}: {rec['recommendation']} - {rec['reason']}")
        
        print(f"  Passed news/event filter: {len(news_filtered)}")
        
        if news_filtered and self.market_context:
            market_bias = self.market_context.bias.value if hasattr(self.market_context.bias, 'value') else 'neutral'
            
            for item in news_filtered:
                allowed, conf = self.market_bias_engine.filter_signal_by_bias(
                    item.get('signal', type('S', (), {'direction': 'long'})()).direction,
                    self.market_context.bias
                )
                item['bias_confidence'] = conf
                item['bias_allowed'] = allowed
            
            ranked = self.trade_ranker.get_top_signals(
                news_filtered, 
                n=3,
                market_bias_value=market_bias
            )
            
            print(f"  Ranked to top 3: {[s['signal'].symbol for s in ranked]}")
            
            return ranked
        
        return news_filtered if news_filtered else ai_filtered

    def execute_trades(self, signals: List[Dict], capital: float,
                     use_options: bool = False) -> List[Dict]:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Executing trades...")
        
        executed = []
        
        rec = getattr(self, '_market_rec', {})
        base_size_mult = rec.get('position_size', 1.0) if rec else 1.0
        
        for item in signals[:self.risk.config['max_trades_per_day']]:
            signal = item['signal']

            # v4 fix#5: per-symbol re-entry block. If this symbol already hit
            # SL this session, skip — don't chase the same setup that failed.
            if not self.risk.can_trade(symbol=signal.symbol, force_allowed=True):
                print(f"  [SKIP] {signal.symbol}: re-entry blocked (stopped earlier today)")
                continue

            vol_data = item.get('volatility', {})
            vol_risk = vol_data.get('risk_params', {}) if vol_data else {}
            
            ai_pred = item.get('ai_prediction')
            position_size = ai_pred.position_size if ai_pred else "full"

            if position_size == "skip":
                continue

            vol_size_mult = vol_risk.get('position_size_multiplier', 1.0)
            size_multiplier = (1.0 if position_size == "full" else 0.5) * vol_size_mult * base_size_mult
            
            if size_multiplier < 0.25:
                continue
            
            adjusted_capital = capital * size_multiplier

            strike = item.get('strike')

            sl_mult = vol_risk.get('sl_multiplier', 1.0)
            atr_adj = signal.atr * sl_mult

            target = signal.entry_price + (atr_adj * 3) if signal.direction == "long" else signal.entry_price - (atr_adj * 3)

            entry_price = signal.entry_price
            entry_volume = 0.0
            entry_vol_avg = 0.0
            df = self.scanner.get_market_data(signal.symbol)
            if df is not None and not df.empty:
                entry_ctx = self.exec_refiner.refine_entry(df, signal, entry_price)
                if entry_ctx.refinement in (EntryRefinement.AVOID, EntryRefinement.WAIT_VWAP):
                    print(f"  [SKIP] {signal.symbol}: {entry_ctx.reason}")
                    continue
                entry_price = entry_ctx.entry_price
                print(f"  [REFINE] {signal.symbol}: {entry_ctx.refinement.value} @ {entry_price:.2f} | {entry_ctx.reason}")
                entry_volume = float(df['volume'].iloc[-1])
                entry_vol_avg = float(df['volume'].rolling(20).mean().iloc[-1]) if len(df) >= 20 else entry_volume

            result = self.execution.execute_trade(
                symbol=signal.symbol,
                direction=signal.direction,
                capital=adjusted_capital,
                entry_price=entry_price,
                atr=atr_adj,
                strike=strike,
                use_options=use_options,
                entry_volume=entry_volume,
                entry_vol_avg=entry_vol_avg,
            )

            if result and result.success:
                executed.append({
                    'signal': signal,
                    'result': result,
                    'position_size': position_size,
                    'volatility': vol_data,
                    'item': item,
                })

                print(f"\n  {'='*45}")
                print(f"  TRADE EXECUTED")
                print(f"  {'='*45}")
                print(f"  Symbol:      {signal.symbol}")
                print(f"  Direction:   {signal.direction.upper()}")
                print(f"  Entry Price: {signal.entry_price:.2f}")
                print(f"  Stop Loss:   {signal.atr_price:.2f}")
                print(f"  Target:      {target:.2f}")
                print(f"  Quantity:    {result.filled_quantity}")
                print(f"  Order ID:    {result.order_id}")
                
                strike_info = item.get('strike')
                if strike_info:
                    print(f"  Strike Type: {getattr(strike_info, 'strike_type', 'N/A')}")
                    print(f"  Option Type: {getattr(strike_info, 'option_type', 'N/A')}")
                    print(f"  Strike Price:{getattr(strike_info, 'strike_price', 0):.0f}")
                    print(f"  Premium:     {getattr(strike_info, 'premium', 0):.2f}")
                
                greeks = item.get('greeks')
                if greeks:
                    print(f"  Delta:       {greeks.get('delta', 0):.3f}")
                    print(f"  Theta:       {greeks.get('theta', 0):.3f}")
                
                print(f"  Vol Regime:  {vol_data.get('regime', 'NORMAL')}")
                print(f"  Pos Size:    {position_size} ({size_multiplier:.1%})")
                print(f"  Rank:        {item.get('rank', 'N/A')}")
                print(f"  Score:       {item.get('total_score', 0):.2f}")
                print(f"  {'='*45}")
                
                trade_data = {
                    'symbol': signal.symbol,
                    'direction': signal.direction,
                    'entry_price': signal.entry_price,
                    'stop_loss': signal.atr_price,
                    'target': target,
                    'quantity': result.filled_quantity,
                    'order_id': result.order_id,
                    'option_type': getattr(strike, 'option_type', '') if strike else '',
                    'strike_price': getattr(strike, 'strike_price', 0) if strike else 0,
                    'premium': getattr(strike, 'premium', 0) if strike else 0,
                    'delta': greeks.get('delta', 0) if greeks else 0,
                    'theta': greeks.get('theta', 0) if greeks else 0,
                    'volatility_regime': vol_data.get('regime', 'NORMAL') if vol_data else 'NORMAL',
                    'position_size': position_size,
                    'rank': item.get('rank', 0),
                    'total_score': item.get('total_score', 0),
                    'market_bias': self.market_context.bias.value if self.market_context else 'neutral',
                    'session': self.time_context.session.value if self.time_context else 'unknown',
                    'ai_probability': item.get('ai_prediction', {}).win_probability if item.get('ai_prediction') else 0,
                    'status': 'OPEN',
                    'reason': f"{signal.reason}",
                    'pnl': 0,
                    'pnl_percent': 0,
                }
                
                self.trade_logger.log_trade(trade_data)
        
        self.executed_trades.extend(executed)
        
        print(f"\n  Total trades logged: {len(executed)}")
        
        return executed

    def run_full_pipeline(self, capital: float = 100000,
                         use_options: bool = False,
                         use_ai: bool = True) -> Dict:
        print("\n" + "="*50)
        print("STARTING TRADING PIPELINE")
        print("="*50)

        if not self.risk.can_trade(force_allowed=True):
            return {
                'status': 'BLOCKED',
                'reason': 'Risk limits reached',
                'trades_executed': 0
            }

        repair = self.repair_engine

        universe = repair.watched_call('scanner', self.run_scanner)
        if not universe:
            return {
                'status': 'NO_SIGNALS',
                'reason': 'No liquid stocks found',
                'trades_executed': 0
            }

        symbols = [item['symbol'] for item in universe]
        metrics_by_symbol = {item['symbol']: item.get('metrics', {}) for item in universe}

        bias_analysis = repair.watched_call('market_bias', self.run_market_bias_analysis) or {}

        time_check = repair.watched_call('time_filter', self.run_time_filter_check) or {}

        breadth = repair.watched_call('market_breadth', self.run_market_breadth) or {}
        self._market_rec = breadth.get('recommendation', {})

        raw_signals = repair.watched_call('signal_engine', self.run_signal_generation, symbols) or []
        if not raw_signals:
            return {
                'status': 'NO_SIGNALS',
                'reason': 'No raw signals generated',
                'trades_executed': 0
            }

        vol_filtered = repair.watched_call('volatility_filter', self.run_volatility_filter, raw_signals) or []
        for item in vol_filtered:
            sym = item.get('signal') and item['signal'].symbol
            if sym and sym in metrics_by_symbol:
                item['metrics'] = metrics_by_symbol[sym]
        if not vol_filtered:
            return {
                'status': 'NO_SIGNALS',
                'reason': 'Low volatility - no trades',
                'trades_executed': 0
            }

        filtered = repair.watched_call('fake_filter', self.run_filters, vol_filtered) or []
        if not filtered:
            return {
                'status': 'NO_SIGNALS',
                'reason': 'All signals filtered out',
                'trades_executed': 0
            }

        with_strikes = repair.watched_call('strike_selection', self.run_strike_selection, filtered) or []

        if use_ai:
            final_signals = repair.watched_call('ai_filter', self.run_ai_filter, with_strikes) or []
        else:
            final_signals = with_strikes

        if not final_signals:
            return {
                'status': 'NO_SIGNALS',
                'reason': 'AI filter removed all signals',
                'trades_executed': 0
            }

        executed = repair.watched_call('execution', self.execute_trades, final_signals, capital, use_options) or []

        repair.record_cycle(len(raw_signals), len(filtered))
        repair.maybe_adapt()

        print_every = config.AUTO_REPAIR_CONFIG.get('print_health_every', 5)
        if repair._cycle_count % print_every == 0:
            repair.print_health()

        return {
            'status': 'COMPLETE',
            'trades_executed': len(executed),
            'universe_size': len(universe),
            'raw_signals': len(raw_signals),
            'filtered_signals': len(filtered),
            'final_signals': len(final_signals),
            'risk_stats': self.execution.get_risk_stats(),
            'repair_report': repair.health_report(),
        }

    def get_positions(self):
        return self.execution.get_open_positions()

    def manage_positions(self):
        def get_price(symbol):
            df = self.scanner.get_market_data(symbol)
            if df is not None and not df.empty:
                return float(df['close'].iloc[-1])
            return None

        def get_data(symbol):
            return self.scanner.get_market_data(symbol)

        return self.execution.manage_open_positions(get_price, get_data)

    def get_daily_stats(self) -> Dict:
        return self.execution.get_risk_stats()


def run_backtest(scanner, start_date: str, end_date: str) -> pd.DataFrame:
    results = []
    
    return pd.DataFrame(results)


if __name__ == '__main__':
    CAPITAL = 100000
    pipeline = TradingPipeline(capital=CAPITAL)

    result = pipeline.run_full_pipeline(
        capital=CAPITAL,
        use_options=False,
        use_ai=True
    )
    
    print("\n" + "="*50)
    print("PIPELINE RESULT")
    print("="*50)
    print(f"Status: {result['status']}")
    
    if 'reason' in result:
        print(f"Reason: {result['reason']}")
    
    if 'trades_executed' in result:
        print(f"Trades executed: {result['trades_executed']}")
    
    print(f"\nRisk Stats: {pipeline.get_daily_stats()}")