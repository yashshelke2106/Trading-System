import pandas as pd
import numpy as np
import json
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
import os
import csv
import uuid


@dataclass
class TradeLog:
    trade_id: str
    timestamp: str
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    stop_loss: float
    target: float
    quantity: int
    pnl: float
    pnl_percent: float
    status: str
    reason: str
    order_id: str
    option_type: str
    strike_price: float
    premium: float
    delta: float
    theta: float
    volatility_regime: str
    position_size: str
    rank: int
    total_score: float
    market_bias: str
    session: str
    ai_probability: float
    holding_period: int
    exit_reason: str
    drawdown: float
    max_favorable: float
    
    def to_dict(self) -> Dict:
        return asdict(self)


class TradeLogger:
    def __init__(self):
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        
        self.log_file = os.path.join(log_dir, "trades.csv")
        self.json_log = os.path.join(log_dir, "trades.json")
        self.stats_file = os.path.join(log_dir, "performance.json")
        self.learning_file = os.path.join(log_dir, "learning.json")
        
        self.columns = [
            "trade_id", "timestamp", "symbol", "direction", "entry_price",
            "exit_price", "stop_loss", "target", "quantity", "pnl", "pnl_percent",
            "status", "reason", "order_id", "option_type", "strike_price",
            "premium", "delta", "theta", "volatility_regime", "position_size",
            "rank", "total_score", "market_bias", "session", "ai_probability",
            "holding_period", "exit_reason", "drawdown", "max_favorable"
        ]
        
        if not os.path.exists(self.log_file):
            self._init_csv()
        
        self.trades: List[TradeLog] = []
        self._load_trades()

    def _init_csv(self):
        with open(self.log_file, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=self.columns)
            writer.writeheader()

    def _load_trades(self):
        if os.path.exists(self.log_file):
            try:
                df = pd.read_csv(self.log_file)
                self.trades = df.to_dict('records')
                if self._ensure_unique_trade_ids():
                    pd.DataFrame(self.trades).to_csv(self.log_file, index=False)
            except Exception as e:
                print(f"[WARN] Failed to load trades CSV: {e}")
                self.trades = []

    def _ensure_unique_trade_ids(self) -> bool:
        seen = set()
        changed = False
        for idx, trade in enumerate(self.trades):
            trade_id = str(trade.get('trade_id', '') or '').strip()
            if not trade_id or trade_id in seen:
                base = trade_id or "TRADE"
                trade_id = f"{base}_{idx + 1:04d}_{uuid.uuid4().hex[:6]}"
                trade['trade_id'] = trade_id
                changed = True
            seen.add(trade_id)
        return changed

    def log_trade(self, trade_data: Dict):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        trade_log = TradeLog(
            trade_id=(trade_data.get('trade_id')
                      or f"TRADE_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:6]}"),
            timestamp=timestamp,
            symbol=trade_data.get('symbol', ''),
            direction=trade_data.get('direction', ''),
            entry_price=trade_data.get('entry_price', 0),
            exit_price=trade_data.get('exit_price', 0),
            stop_loss=trade_data.get('stop_loss', 0),
            target=trade_data.get('target', 0),
            quantity=trade_data.get('quantity', 0),
            pnl=trade_data.get('pnl', 0),
            pnl_percent=trade_data.get('pnl_percent', 0),
            status=trade_data.get('status', 'OPEN'),
            reason=trade_data.get('reason', ''),
            order_id=trade_data.get('order_id', ''),
            option_type=trade_data.get('option_type', ''),
            strike_price=trade_data.get('strike_price', 0),
            premium=trade_data.get('premium', 0),
            delta=trade_data.get('delta', 0),
            theta=trade_data.get('theta', 0),
            volatility_regime=trade_data.get('volatility_regime', 'NORMAL'),
            position_size=trade_data.get('position_size', 'full'),
            rank=trade_data.get('rank', 0),
            total_score=trade_data.get('total_score', 0),
            market_bias=trade_data.get('market_bias', ''),
            session=trade_data.get('session', ''),
            ai_probability=trade_data.get('ai_probability', 0),
            holding_period=trade_data.get('holding_period', 0),
            exit_reason=trade_data.get('exit_reason', ''),
            drawdown=trade_data.get('drawdown', 0),
            max_favorable=trade_data.get('max_favorable', 0),
        )
        
        self.trades.append(trade_log.to_dict())

        with open(self.log_file, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=self.columns)
            writer.writerow(trade_log.to_dict())
        
        self._update_stats()
        self._update_learning()

    def update_trade(self, trade_id: str, exit_price: float, status: str, 
                    exit_reason: str, pnl: float, pnl_percent: float,
                    holding_period: int, drawdown: float, max_favorable: float):
        matched = False
        for trade in self.trades:
            if trade.get('trade_id') == trade_id or trade.get('order_id') == trade_id:
                matched = True
                trade['exit_price'] = exit_price
                trade['pnl'] = pnl
                trade['pnl_percent'] = pnl_percent
                trade['status'] = status
                trade['exit_reason'] = exit_reason
                trade['holding_period'] = holding_period
                trade['drawdown'] = drawdown
                trade['max_favorable'] = max_favorable
                break
        if not matched and trade_id:
            # Fallback: match most recent OPEN trade for same symbol
            # trade_id format from Risk.Trade is "SYMBOL_YYYYMMDD_HHMMSS"
            sym = trade_id.split('_')[0] if '_' in trade_id else ''
            for trade in reversed(self.trades):
                if sym and trade.get('symbol') == sym and trade.get('status') == 'OPEN':
                    trade['exit_price'] = exit_price
                    trade['pnl'] = pnl
                    trade['pnl_percent'] = pnl_percent
                    trade['status'] = status
                    trade['exit_reason'] = exit_reason
                    trade['holding_period'] = holding_period
                    trade['drawdown'] = drawdown
                    trade['max_favorable'] = max_favorable
                    break
        
        df = pd.DataFrame(self.trades)
        df.to_csv(self.log_file, index=False)
        
        self._update_stats()

    def _get_attr(self, obj, key, default=None):
        if hasattr(obj, key):
            return getattr(obj, key)
        if isinstance(obj, dict):
            return obj.get(key, default)
        return default

    def get_performance_summary(self) -> Dict:
        if not self.trades:
            return self._empty_stats()
        
        closed_trades = [t for t in self.trades if self._get_attr(t, 'status') in ['WIN', 'LOSS', 'CLOSED']]
        
        if not closed_trades:
            return self._empty_stats()
        
        wins = [t for t in closed_trades if self._get_attr(t, 'status') == 'WIN']
        losses = [t for t in closed_trades if self._get_attr(t, 'status') == 'LOSS']
        
        total_pnl = sum(self._get_attr(t, 'pnl', 0) for t in closed_trades)
        total_pnl_percent = sum(self._get_attr(t, 'pnl_percent', 0) for t in closed_trades)
        
        win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0
        avg_win = sum(self._get_attr(t, 'pnl', 0) for t in wins) / len(wins) if wins else 0
        avg_loss = sum(self._get_attr(t, 'pnl', 0) for t in losses) / len(losses) if losses else 0
        
        max_dd = max([self._get_attr(t, 'drawdown', 0) for t in closed_trades], default=0)
        max_fav = max([self._get_attr(t, 'max_favorable', 0) for t in closed_trades], default=0)
        
        return {
            'total_trades': len(closed_trades),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'total_pnl': total_pnl,
            'avg_pnl_percent': total_pnl_percent / len(closed_trades) if closed_trades else 0,
            'max_drawdown': max_dd,
            'max_favorable': max_fav,
            'profit_factor': abs(avg_win / avg_loss) if avg_loss != 0 else 0,
            'expectancy': (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * abs(avg_loss)),
        }

    def get_stats_by_symbol(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        
        closed = [t for t in self.trades if self._get_attr(t, 'status') in ['WIN', 'LOSS']]
        
        if not closed:
            return pd.DataFrame()
        
        data = []
        for t in closed:
            data.append({
                'symbol': self._get_attr(t, 'symbol', ''),
                'pnl': self._get_attr(t, 'pnl', 0),
                'pnl_percent': self._get_attr(t, 'pnl_percent', 0),
                'status': self._get_attr(t, 'status', ''),
            })
        
        df = pd.DataFrame(data)
        
        stats = df.groupby('symbol').agg({
            'pnl': ['sum', 'count'],
            'pnl_percent': 'mean',
            'status': lambda x: (x == 'WIN').sum() / len(x) * 100
        }).round(2)
        
        stats.columns = ['total_pnl', 'trades', 'avg_pnl_percent', 'win_rate']
        
        return stats.sort_values('total_pnl', ascending=False)

    def get_stats_by_direction(self) -> Dict:
        if not self.trades:
            return {}
        
        closed = [t for t in self.trades if self._get_attr(t, 'status') in ['WIN', 'LOSS']]
        
        stats = {}
        for direction in ['LONG', 'SHORT']:
            dir_trades = [t for t in closed if self._get_attr(t, 'direction') == direction]
            
            if not dir_trades:
                continue
            
            wins = [t for t in dir_trades if self._get_attr(t, 'status') == 'WIN']
            stats[direction] = {
                'trades': len(dir_trades),
                'win_rate': len(wins) / len(dir_trades) * 100,
                'total_pnl': sum(self._get_attr(t, 'pnl', 0) for t in dir_trades),
                'avg_pnl': sum(self._get_attr(t, 'pnl', 0) for t in dir_trades) / len(dir_trades),
            }
        
        return stats

    def get_stats_by_session(self) -> Dict:
        if not self.trades:
            return {}
        
        closed = [t for t in self.trades if self._get_attr(t, 'status') in ['WIN', 'LOSS']]
        
        stats = {}
        for session in ['first_hour', 'power_hour', 'dead_zone', 'open_auction']:
            ses_trades = [t for t in closed if self._get_attr(t, 'session') == session]
            
            if not ses_trades:
                continue
            
            wins = [t for t in ses_trades if self._get_attr(t, 'status') == 'WIN']
            stats[session] = {
                'trades': len(ses_trades),
                'win_rate': len(wins) / len(ses_trades) * 100 if ses_trades else 0,
                'total_pnl': sum(self._get_attr(t, 'pnl', 0) for t in ses_trades),
            }
        
        return stats

    def get_stats_by_regime(self) -> Dict:
        if not self.trades:
            return {}
        
        closed = [t for t in self.trades if self._get_attr(t, 'status') in ['WIN', 'LOSS']]
        
        stats = {}
        for regime in ['LOW', 'NORMAL', 'HIGH']:
            reg_trades = [t for t in closed if self._get_attr(t, 'volatility_regime') == regime]
            
            if not reg_trades:
                continue
            
            wins = [t for t in reg_trades if self._get_attr(t, 'status') == 'WIN']
            stats[regime] = {
                'trades': len(reg_trades),
                'win_rate': len(wins) / len(reg_trades) * 100 if reg_trades else 0,
                'total_pnl': sum(self._get_attr(t, 'pnl', 0) for t in reg_trades),
            }
        
        return stats

    def get_entry_quality_analysis(self) -> Dict:
        if not self.trades:
            return {}
        
        closed = [t for t in self.trades if self._get_attr(t, 'status') in ['WIN', 'LOSS']]
        
        high_score = [t for t in closed if self._get_attr(t, 'total_score', 0) >= 0.7]
        med_score = [t for t in closed if 0.5 <= self._get_attr(t, 'total_score', 0) < 0.7]
        low_score = [t for t in closed if self._get_attr(t, 'total_score', 0) < 0.5]
        
        analysis = {}
        
        for name, trades in [('HIGH', high_score), ('MEDIUM', med_score), ('LOW', low_score)]:
            if not trades:
                continue
            
            wins = [t for t in trades if self._get_attr(t, 'status') == 'WIN']
            analysis[name] = {
                'trades': len(trades),
                'win_rate': len(wins) / len(trades) * 100 if trades else 0,
                'avg_pnl': sum(self._get_attr(t, 'pnl', 0) for t in trades) / len(trades),
            }
        
        return analysis

    def _update_stats(self):
        summary = self.get_performance_summary()
        by_symbol_df = self.get_stats_by_symbol()
        by_symbol = by_symbol_df.to_dict() if not by_symbol_df.empty else {}
        by_direction = self.get_stats_by_direction()
        by_session = self.get_stats_by_session()
        by_regime = self.get_stats_by_regime()
        quality = self.get_entry_quality_analysis()
        
        stats = {
            'timestamp': datetime.now().isoformat(),
            'summary': summary,
            'by_symbol': by_symbol,
            'by_direction': by_direction,
            'by_session': by_session,
            'by_regime': by_regime,
            'entry_quality': quality,
        }
        
        with open(self.stats_file, 'w') as f:
            json.dump(stats, f, indent=2)

    def _update_learning(self):
        learnings = {
            'timestamp': datetime.now().isoformat(),
            'insights': self._generate_insights(),
            'recommendations': self._generate_recommendations(),
        }
        
        with open(self.learning_file, 'w') as f:
            json.dump(learnings, f, indent=2)

    def _generate_insights(self) -> List[str]:
        insights = []
        
        summary = self.get_performance_summary()
        
        if summary['win_rate'] > 60:
            insights.append("Win rate is healthy - system performing well")
        elif summary['win_rate'] < 40:
            insights.append("Win rate needs improvement - review entry criteria")
        
        if summary.get('expectancy', 0) > 0:
            insights.append("Positive expectancy - system has edge")
        else:
            insights.append("Negative expectancy - needs adjustment")
        
        by_dir = self.get_stats_by_direction()
        if 'LONG' in by_dir and 'SHORT' in by_dir:
            long_wr = by_dir['LONG']['win_rate']
            short_wr = by_dir['SHORT']['win_rate']
            
            if abs(long_wr - short_wr) > 20:
                insights.append(f"Bias detected: LONG {long_wr:.0f}% vs SHORT {short_wr:.0f}%")
        
        by_ses = self.get_stats_by_session()
        if by_ses:
            best_session = max(by_ses.items(), key=lambda x: x[1]['win_rate'] if x[1]['trades'] >= 3 else 0)
            if best_session[1]['trades'] >= 3:
                insights.append(f"Best session: {best_session[0]} ({best_session[1]['win_rate']:.0f}% WR)")
        
        return insights

    def _generate_recommendations(self) -> List[str]:
        recommendations = []
        
        summary = self.get_performance_summary()
        
        if summary['win_rate'] < 50:
            recommendations.append("Consider tightening entry filters")
            recommendations.append("Review market bias alignment")
        
        if summary.get('max_drawdown', 0) < -10:
            recommendations.append("Max drawdown exceeded - reduce position size")
        
        quality = self.get_entry_quality_analysis()
        if 'HIGH' in quality and quality['HIGH']['win_rate'] < 50:
            recommendations.append("High-score trades losing - verify scoring weights")
        
        by_dir = self.get_stats_by_direction()
        if 'LONG' in by_dir and by_dir['LONG']['win_rate'] < 40:
            recommendations.append("Long trades underperforming - check market bias filter")
        
        return recommendations

    def _empty_stats(self) -> Dict:
        return {
            'total_trades': 0,
            'wins': 0,
            'losses': 0,
            'win_rate': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'total_pnl': 0,
        }

    def print_summary(self):
        summary = self.get_performance_summary()
        
        print("\n" + "="*50)
        print("TRADE PERFORMANCE SUMMARY")
        print("="*50)
        print(f"Total Trades:     {summary['total_trades']}")
        print(f"Wins:            {summary['wins']}")
        print(f"Losses:          {summary['losses']}")
        print(f"Win Rate:        {summary['win_rate']:.1f}%")
        print(f"Avg Win:         {summary['avg_win']:.2f}")
        print(f"Avg Loss:        {summary['avg_loss']:.2f}")
        print(f"Total P&L:       {summary['total_pnl']:.2f}")
        print(f"Profit Factor:   {summary.get('profit_factor', 0):.2f}")
        print(f"Expectancy:      {summary.get('expectancy', 0):.2f}")
        print(f"Max Drawdown:    {summary.get('max_drawdown', 0):.2f}%")
        
        print("\n" + "-"*50)
        print("BY SYMBOL:")
        print("-"*50)
        stats = self.get_stats_by_symbol()
        if not stats.empty:
            for idx, row in stats.iterrows():
                print(f"{idx:12} | Trades: {row['trades']:2} | P&L: {row['total_pnl']:8.2f} | WR: {row['win_rate']:.0f}%")
        
        print("\n" + "-"*50)
        print("BY DIRECTION:")
        print("-"*50)
        by_dir = self.get_stats_by_direction()
        for direction, stats in by_dir.items():
            print(f"{direction:6} | Trades: {stats['trades']:2} | WR: {stats['win_rate']:.0f}% | P&L: {stats['total_pnl']:.2f}")
        
        print("\n" + "-"*50)
        print("INSIGHTS:")
        print("-"*50)
        insights = self._generate_insights()
        for insight in insights:
            print(f"  - {insight}")
        
        print("\n" + "-"*50)
        print("RECOMMENDATIONS:")
        print("-"*50)
        recs = self._generate_recommendations()
        for rec in recs:
            print(f"  - {rec}")
        
        print("="*50)


if __name__ == '__main__':
    logger = TradeLogger()
    logger.print_summary()
