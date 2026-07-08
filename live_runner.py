"""
Live trading runner  - NSE F&O market.

Features:
  - Scans full F&O universe every 3 minutes for volume surges
  - Runs fake-breakout filter + risk-aware execution on candidates
  - Manages open positions (volume-exit + trailing SL + hard 10% stop)
  - Post-market: analyzes all trades, runs backtest, auto-tunes parameters
  - Config overrides persist to logs/config_override.json (survives restarts)

Commands:
  python live_runner.py                    # full live session
  python live_runner.py --capital 200000   # custom capital
  python live_runner.py --mode analyze     # post-market analysis only
  python live_runner.py --mode tune        # force auto-tune
  python live_runner.py --mode backtest    # run backtest only
"""

import sys
import os
import json
import time
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional, Tuple

_IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> datetime:
    return datetime.now(_IST).replace(tzinfo=None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np

import config
from core.scanner import LiquidityScanner
from core.signal_engine import SignalEngine
from core.fake_breakout_filter import FakeBreakoutFilter
from core.order_flow import OrderFlowAnalyzer
from core.trade_ranker import TradeRanker
from core.execution import ExecutionEngine
from core.risk_engine import RiskEngine
from core.execution_refinement import ExecutionRefiner, EntryRefinement
from core.trade_logger import TradeLogger
from core.volatility_engine import VolatilityEngine
from core.market_bias import MarketBiasEngine
from core.auto_repair import AutoRepairEngine
from core.universe import FO_UNIVERSE as SHARED_FO_UNIVERSE


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SCAN_INTERVAL_SEC   = 30      # 30 seconds
MARKET_OPEN_HOUR    = 9
MARKET_OPEN_MIN     = 15
MARKET_CLOSE_HOUR   = 15
MARKET_CLOSE_MIN    = 30
MARKET_TOTAL_MIN    = 375     # 9:15 to 15:30

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
OVERRIDE_FILE = os.path.join(LOG_DIR, "config_override.json")
TUNER_FILE    = os.path.join(LOG_DIR, "tuner_state.json")   # AutoTuner owns this; TradeLogger owns performance.json

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "live_runner.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Full F&O universe (extended vs default 15-stock list)
# ─────────────────────────────────────────────────────────────────────────────

FO_UNIVERSE = [
    # ── Nifty 50 ──────────────────────────────────────────────────────────────
    'RELIANCE',   'TCS',        'INFY',       'HDFCBANK',   'ICICIBANK',
    'SBIN',       'BHARTIARTL', 'KOTAKBANK',  'BAJFINANCE', 'HINDUNILVR',
    'ITC',        'LT',         'AXISBANK',   'MARUTI',     'ASIANPAINT',
    'WIPRO',      'HCLTECH',    'TITAN',      'SUNPHARMA',  'TATAMOTORS',
    'ADANIENT',   'NTPC',       'POWERGRID',  'ULTRACEMCO', 'JSWSTEEL',
    'ONGC',       'COALINDIA',  'BPCL',       'HEROMOTOCO', 'EICHERMOT',
    'TATASTEEL',  'HINDALCO',   'GRASIM',     'CIPLA',      'DIVISLAB',
    'DRREDDY',    'BRITANNIA',  'NESTLEIND',  'TATACONSUM', 'M&M',
    'BAJAJ-AUTO', 'INDUSINDBK', 'TECHM',      'ADANIPORTS', 'BAJAJFINSV',
    'HDFCLIFE',   'LTIM',       'SHRIRAMFIN', 'TRENT',      'APOLLOHOSP',
    # ── Nifty Next 50 / active F&O ────────────────────────────────────────────
    'ABB',        'ALKEM',      'AMBUJACEM',  'APOLLOTYRE', 'AUROPHARMA',
    'BANDHANBNK', 'BANKBARODA', 'BEL',        'BERGEPAINT', 'BHARATFORG',
    'BHEL',       'BIOCON',     'BOSCHLTD',   'CANBK',      'CEATLTD',
    'CHOLAFIN',   'COLPAL',     'CONCOR',     'COROMANDEL', 'DABUR',
    'DLF',        'DMART',      'ESCORTS',    'EXIDEIND',   'FEDERALBNK',
    'GAIL',       'GLENMARK',   'GODREJCP',   'GODREJPROP', 'GRANULES',
    'GUJGASLTD',  'HAVELLS',    'HINDPETRO',  'ICICIPRULI', 'ICICIGI',
    'IDFCFIRSTB', 'IGL',        'INDHOTEL',   'INDIGO',     'INDUSTOWER',
    'IPCALAB',    'IRCTC',      'IRFC',       'JINDALSTEL', 'JIOFIN',
    'LALPATHLAB', 'LAURUSLABS', 'LICHSGFIN',  'LICI',
    'LUPIN',      'M&MFIN',     'MANAPPURAM', 'MARICO',     'MAXHEALTH',
    'MCX',        'METROPOLIS', 'MCDOWELL-N', 'MOTHERSON',  'MPHASIS',
    'MUTHOOTFIN', 'NATIONALUM', 'NAUKRI',     'NCC',        'NHPC',
    'NMDC',       'NYKAA',      'OBEROIRLTY', 'OFSS',       'OLECTRA',
    'PAGEIND',    'PAYTM',      'PFC',        'PIIND',
    'PNB',        'POLYCAB',    'PVRINOX',    'RAMCOCEM',   'RECLTD',
    'SAIL',       'SBICARD',    'SBILIFE',    'SHREECEM',   'SIEMENS',
    'SUNTV',      'TATACOMM',   'TATACHEM',   'TATAELXSI',  'TATAPOWER',
    'TVSMOTOR',   'UBL',        'UPL',        'VEDL',       'VOLTAS',
    'ZOMATO',     'ZYDUSLIFE',  'ADANIGREEN', 'ADANIPOWER',
    # ── Others already tracked ────────────────────────────────────────────────
    'ATUL',       'BALKRISIND', 'DEEPAKNT',   'NAVINFLUOR', 'PERSISTENT',
    'PIDILITIND',
]

FO_UNIVERSE = SHARED_FO_UNIVERSE


# ─────────────────────────────────────────────────────────────────────────────
# Config override system
# ─────────────────────────────────────────────────────────────────────────────

def load_config_overrides() -> Dict:
    if os.path.exists(OVERRIDE_FILE):
        try:
            with open(OVERRIDE_FILE) as f:
                overrides = json.load(f)
            log.info(f"Loaded config overrides: {overrides}")
            return overrides
        except Exception as e:
            log.warning(f"Could not load overrides: {e}")
    return {}


def apply_config_overrides(overrides: Dict) -> None:
    """Apply persisted auto-tuned parameters to live config dicts."""
    for key, val in overrides.get("VOLUME_EXIT_CONFIG", {}).items():
        if key in config.VOLUME_EXIT_CONFIG:
            config.VOLUME_EXIT_CONFIG[key] = val
    for key, val in overrides.get("RISK_CONFIG", {}).items():
        if key in config.RISK_CONFIG:
            config.RISK_CONFIG[key] = val
    for key, val in overrides.get("SIGNAL_CONFIG", {}).items():
        if key in config.SIGNAL_CONFIG:
            config.SIGNAL_CONFIG[key] = val


def save_config_overrides() -> None:
    overrides = {
        "updated_at": datetime.now().isoformat(),
        "VOLUME_EXIT_CONFIG": {
            "entry_min_vol_ratio": config.VOLUME_EXIT_CONFIG["entry_min_vol_ratio"],
            "exit_vol_threshold":  config.VOLUME_EXIT_CONFIG["exit_vol_threshold"],
            "profit_required_pct": config.VOLUME_EXIT_CONFIG["profit_required_pct"],
        },
        "RISK_CONFIG": {
            "max_risk_per_trade": config.RISK_CONFIG["max_risk_per_trade"],
            "sl_pct":             config.RISK_CONFIG["sl_pct"],
            "max_daily_loss":     config.RISK_CONFIG["max_daily_loss"],
        },
        "SIGNAL_CONFIG": {
            "min_votes":      config.SIGNAL_CONFIG["min_votes"],
            "min_vote_lead":  config.SIGNAL_CONFIG["min_vote_lead"],
        },
    }
    with open(OVERRIDE_FILE, "w") as f:
        json.dump(overrides, f, indent=2)
    log.info(f"Config overrides saved to {OVERRIDE_FILE}")


# ─────────────────────────────────────────────────────────────────────────────
# Auto-tuner
# ─────────────────────────────────────────────────────────────────────────────

class AutoTuner:
    """
    Adapts config parameters based on rolling trade outcomes.

    Rules (applied to last N closed trades):
      win_rate < 35%   -> tighten entry vol threshold, add vote
      win_rate > 85% and trades_per_week < 5  -> loosen vol threshold slightly
      expectancy < 0  -> raise volume exit threshold (book earlier)
      max_drawdown > 7%  -> reduce risk per trade
      consecutive_losses >= 4  -> pause trading flag (halt next trade)
    """

    WINDOW = 20      # rolling window for metrics
    MIN_TRADES = 5   # minimum trades before tuning kicks in

    BOUNDS = {
        "entry_min_vol_ratio": (1.5,  4.0),
        "exit_vol_threshold":  (0.55, 0.92),
        "max_risk_per_trade":  (0.005, 0.05),
        "min_votes":           (2,    7),
    }

    def __init__(self):
        self.trade_history: List[Dict] = self._load_history()

    def _load_history(self) -> List[Dict]:
        if os.path.exists(TUNER_FILE):
            try:
                with open(TUNER_FILE) as f:
                    data = json.load(f)
                return data.get("trade_history", [])
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"tuner history unreadable ({e}) — starting empty")
        return []

    def record_trade(self, symbol: str, pnl: float, pnl_pct: float,
                     exit_reason: str, signal_strength: float) -> None:
        self.trade_history.append({
            "date":            date.today().isoformat(),
            "symbol":          symbol,
            "pnl":             pnl,
            "pnl_pct":         pnl_pct,
            "exit_reason":     exit_reason,
            "signal_strength": signal_strength,
            "won":             pnl > 0,
            "entry_vol_ratio": config.VOLUME_EXIT_CONFIG["entry_min_vol_ratio"],
        })
        self._persist()

    def _persist(self) -> None:
        with open(TUNER_FILE, "w") as f:
            json.dump({"trade_history": self.trade_history[-200:]}, f, indent=2)

    def _clip(self, key: str, val: float) -> float:
        lo, hi = self.BOUNDS.get(key, (val, val))
        return round(max(lo, min(hi, val)), 4)

    def tune(self) -> Dict:
        recent = self.trade_history[-self.WINDOW:]
        if len(recent) < self.MIN_TRADES:
            log.info(f"AutoTuner: only {len(recent)} trades, need {self.MIN_TRADES}  - skipping")
            return {}

        wins   = sum(1 for t in recent if t["won"])
        losses = len(recent) - wins
        win_rate = wins / len(recent)

        pnl_list = [t["pnl"] for t in recent]
        avg_pnl  = sum(pnl_list) / len(pnl_list)
        max_dd   = self._rolling_drawdown(pnl_list)

        changes = {}

        # ── Entry filter adjustment ──────────────────────────────────────
        evr = config.VOLUME_EXIT_CONFIG["entry_min_vol_ratio"]
        if win_rate < 0.35:
            evr = self._clip("entry_min_vol_ratio", evr + 0.3)
            config.VOLUME_EXIT_CONFIG["entry_min_vol_ratio"] = evr
            changes["entry_min_vol_ratio"] = evr
            # min_votes owned by AutoRepairEngine.ParameterAdaptor  - do not mutate here
            log.info(f"AutoTuner: win_rate={win_rate:.0%} low  - tightened entry_vol to {evr}")

        elif win_rate > 0.82 and len(recent) < 8:
            evr = self._clip("entry_min_vol_ratio", evr - 0.15)
            config.VOLUME_EXIT_CONFIG["entry_min_vol_ratio"] = evr
            changes["entry_min_vol_ratio"] = evr
            log.info(f"AutoTuner: high WR but few trades  - loosened entry_vol to {evr}")

        # ── Exit threshold adjustment ────────────────────────────────────
        evt = config.VOLUME_EXIT_CONFIG["exit_vol_threshold"]
        if avg_pnl < 0 and wins > losses:
            # Win but avg pnl still negative = exiting too early on losses, not on winners
            evt = self._clip("exit_vol_threshold", evt + 0.05)
            config.VOLUME_EXIT_CONFIG["exit_vol_threshold"] = evt
            changes["exit_vol_threshold"] = evt
            log.info(f"AutoTuner: avg P&L negative despite wins  - raised exit threshold to {evt}")

        # ── Risk per trade ───────────────────────────────────────────────
        rpt = config.RISK_CONFIG["max_risk_per_trade"]
        if max_dd > 0.07:
            rpt = self._clip("max_risk_per_trade", rpt - 0.003)
            config.RISK_CONFIG["max_risk_per_trade"] = rpt
            changes["max_risk_per_trade"] = rpt
            log.info(f"AutoTuner: drawdown {max_dd:.1%} > 7%  - reduced risk_per_trade to {rpt}")
        elif max_dd < 0.02 and win_rate > 0.65:
            rpt = self._clip("max_risk_per_trade", rpt + 0.002)
            config.RISK_CONFIG["max_risk_per_trade"] = rpt
            changes["max_risk_per_trade"] = rpt
            log.info(f"AutoTuner: low drawdown + good WR  - raised risk_per_trade to {rpt}")

        if changes:
            save_config_overrides()

        return changes

    @staticmethod
    def _rolling_drawdown(pnl_list: List[float]) -> float:
        peak = 0.0
        max_dd = 0.0
        running = 0.0
        for p in pnl_list:
            running += p
            peak = max(peak, running)
            max_dd = max(max_dd, (peak - running) / max(peak, 1))
        return max_dd


# ─────────────────────────────────────────────────────────────────────────────
# Post-market analyzer
# ─────────────────────────────────────────────────────────────────────────────

class PostMarketAnalyzer:

    def __init__(self, capital: float, tuner: AutoTuner):
        self.capital = capital
        self.tuner   = tuner
        self.logger  = TradeLogger()

    def run(self) -> None:
        log.info("\n" + "=" * 60)
        log.info("  POST-MARKET ANALYSIS")
        log.info("=" * 60)

        self.logger.print_summary()

        changes = self.tuner.tune()
        if changes:
            log.info(f"AutoTuner applied changes: {changes}")
        else:
            log.info("AutoTuner: no parameter changes needed")

        self._run_backtest()
        self._save_daily_report()

    def _run_backtest(self) -> None:
        log.info("\nRunning post-market backtest...")
        try:
            from backtest import BacktestEngine
            bt = BacktestEngine(
                symbols=FO_UNIVERSE[:20],
                capital=self.capital,
                verbose=False,
            )
            bt.load_data()
            metrics = bt.run()
            if metrics:
                log.info(f"Backtest result: return={metrics.get('total_return_pct',0):+.1f}% "
                         f"WR={metrics.get('win_rate_pct',0):.1f}% "
                         f"PF={metrics.get('profit_factor',0):.2f} "
                         f"MaxDD={metrics.get('max_drawdown_pct',0):.1f}%")
                bt.save_trades_csv(os.path.join(LOG_DIR, f"backtest_trades_{date.today()}.csv"))
        except Exception as e:
            log.error(f"Backtest failed: {e}")

    def _save_daily_report(self) -> None:
        report_path = os.path.join(LOG_DIR, f"daily_report_{date.today()}.txt")
        summary = self.logger.get_performance_summary()
        by_sym  = self.logger.get_stats_by_symbol()
        by_dir  = self.logger.get_stats_by_direction()

        lines = [
            f"Daily Report  - {date.today()}",
            "=" * 50,
            f"Trades:       {summary['total_trades']}",
            f"Win Rate:     {summary['win_rate']:.1f}%",
            f"Total P&L:    {summary['total_pnl']:.2f}",
            f"Profit Factor:{summary.get('profit_factor', 0):.2f}",
            f"Expectancy:   {summary.get('expectancy', 0):.2f}",
            "",
            "Config at EOD:",
            f"  entry_min_vol_ratio = {config.VOLUME_EXIT_CONFIG['entry_min_vol_ratio']}",
            f"  exit_vol_threshold  = {config.VOLUME_EXIT_CONFIG['exit_vol_threshold']}",
            f"  sl_pct              = {config.RISK_CONFIG['sl_pct']}",
            f"  max_risk_per_trade  = {config.RISK_CONFIG['max_risk_per_trade']}",
            f"  min_votes           = {config.SIGNAL_CONFIG['min_votes']}",
        ]
        try:
            with open(report_path, "w") as f:
                f.write("\n".join(lines))
            log.info(f"Daily report: {report_path}")
        except Exception as e:
            log.warning(f"Could not save daily report: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Volume surge fast-scanner
# ─────────────────────────────────────────────────────────────────────────────

class VolumeSurgeScanner:
    """
    Fast first-pass filter: find symbols with current projected daily volume
    exceeding entry_min_vol_ratio * 20-day average.

    Caches avg volumes to avoid re-fetching every tick.
    Cache refreshed once at open and every 60 minutes.
    """

    CACHE_TTL_SEC = 3600

    def __init__(self, scanner: LiquidityScanner):
        self.scanner     = scanner
        self.avg_cache:  Dict[str, float] = {}
        self.cache_time: float = 0.0

    def _refresh_avg_cache(self) -> None:
        log.info("Refreshing volume baseline cache...")
        def fetch_avg(sym: str) -> Tuple[str, float]:
            try:
                df = self.scanner.get_market_data(sym, 25)
                if df is not None and len(df) >= 20:
                    avg = float(df["volume"].rolling(20).mean().iloc[-1])
                    return sym, avg
            except Exception as e:
                log.debug(f"vol-avg fetch failed {sym}: {e}")
            return sym, 0.0

        refreshed: Dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for sym, avg in pool.map(fetch_avg, FO_UNIVERSE):
                if avg > 0:
                    refreshed[sym] = avg
        self.avg_cache = refreshed
        self.cache_time = time.time()
        log.info(f"Volume cache built for {len(self.avg_cache)} symbols")

    def _minutes_since_open(self) -> float:
        now = _now_ist()
        open_dt = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0)
        elapsed = (now - open_dt).total_seconds() / 60.0
        # Floor at 5 min: projections at <5 min are too noisy (375x multiplier at 1 min)
        return max(elapsed, 5.0)

    def scan(self) -> List[str]:
        """Return symbols with volume surges (fast quote-based check)."""
        if time.time() - self.cache_time > self.CACHE_TTL_SEC:
            self._refresh_avg_cache()

        min_vol_ratio = config.VOLUME_EXIT_CONFIG["entry_min_vol_ratio"]
        surges: List[str] = []
        elapsed_min = self._minutes_since_open()

        # Try batch quote (Dhan API)
        dhan_api = getattr(self.scanner, 'dhan_api', None)
        if dhan_api and not config.USE_MOCK_DATA:
            surges = self._scan_via_quotes(dhan_api, min_vol_ratio, elapsed_min)

        # Fallback: fetch latest bar for each symbol
        if not surges:
            surges = self._scan_via_latest_bar(min_vol_ratio, elapsed_min)

        log.info(f"Volume surge scan: {len(surges)} candidates  - {surges[:10]}")
        return surges

    def _scan_via_quotes(self, dhan_api, min_vol_ratio: float, elapsed_min: float) -> List[str]:
        surges = []
        try:
            from core.api_dhan import SECURITY_ID_MAP
            known_symbols = [s for s in FO_UNIVERSE if s in SECURITY_ID_MAP and s in self.avg_cache]
            if not known_symbols:
                return []

            result = dhan_api.get_quote(known_symbols)
            if not isinstance(result, (dict, list)):
                return []

            data_list = result if isinstance(result, list) else result.get("data", [result])

            for item in data_list:
                if not isinstance(item, dict):
                    continue
                sym = item.get("symbol") or item.get("tradingSymbol", "")
                current_vol = float(item.get("volume", 0) or item.get("totalTradedVolume", 0))
                avg_vol = self.avg_cache.get(sym, 0)

                if avg_vol <= 0 or current_vol <= 0:
                    continue

                projected = current_vol * (MARKET_TOTAL_MIN / elapsed_min)
                if projected >= avg_vol * min_vol_ratio:
                    surges.append(sym)
        except Exception as e:
            log.debug(f"Quote scan error: {e}")
        return surges

    def _scan_via_latest_bar(self, min_vol_ratio: float, elapsed_min: float) -> List[str]:
        """Check projected daily volume (today's accumulated volume) vs 20d daily avg."""
        surges = []
        today = _now_ist().date()
        for sym in FO_UNIVERSE:
            avg_vol_daily = self.avg_cache.get(sym, 0)
            if avg_vol_daily <= 0:
                continue
            try:
                df = self.scanner.get_market_data(sym, 5)
                if df is None or df.empty:
                    continue
                # Sum today's accumulated volume (all intraday bars, not just latest bar)
                try:
                    dates = pd.to_datetime(df['date']).dt.date
                    today_vol = float(df.loc[dates == today, 'volume'].sum())
                except Exception:
                    today_vol = float(df['volume'].iloc[-1])
                if today_vol <= 0:
                    continue
                # Project accumulated volume to full day (floor at 5 min to avoid noisy projections)
                projected_daily = today_vol * (MARKET_TOTAL_MIN / max(elapsed_min, 5.0))
                if projected_daily >= avg_vol_daily * min_vol_ratio:
                    surges.append(sym)
            except Exception:
                pass
        return surges


# ─────────────────────────────────────────────────────────────────────────────
# Core pipeline wrapper (tight integration of all filters)
# ─────────────────────────────────────────────────────────────────────────────

class IntegratedPipeline:
    """
    Combines FakeBreakoutFilter + SignalEngine + OrderFlow + Risk + Execution
    in a single tightly-coupled flow designed for 3-min tick execution.
    """

    def __init__(self, capital: float):
        self.capital       = capital
        self.scanner       = LiquidityScanner()
        self.signal_engine = SignalEngine()
        self.fake_filter   = FakeBreakoutFilter()
        self.of_analyzer   = OrderFlowAnalyzer()
        self.trade_ranker  = TradeRanker()
        self.exec_refiner  = ExecutionRefiner()
        self.execution     = ExecutionEngine(capital=capital)
        self.vol_engine    = VolatilityEngine()
        self.bias_engine   = MarketBiasEngine()
        self.trade_logger  = TradeLogger()
        self.tuner         = AutoTuner()
        self.surge_scanner = VolumeSurgeScanner(self.scanner)
        self.repair_engine = AutoRepairEngine()  # error catch + param adaptation

    # ─────────────────────────────────────────────────────────────────────
    # Market hours check
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def is_market_open() -> bool:
        from datetime import time as dtime
        now = _now_ist().time()
        return dtime(MARKET_OPEN_HOUR, MARKET_OPEN_MIN) <= now <= dtime(MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN)

    @staticmethod
    def market_just_closed() -> bool:
        from datetime import time as dtime
        now = _now_ist().time()
        return dtime(MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN) <= now <= dtime(16, 30)

    # ─────────────────────────────────────────────────────────────────────
    # 3-minute tick
    # ─────────────────────────────────────────────────────────────────────

    def tick(self) -> None:
        tick_time = _now_ist().strftime("%H:%M:%S")
        log.info(f"\n[TICK {tick_time}] --- 3-min scan start ---")

        # 1. Manage existing positions first (SL/volume-exit/trailing)
        closed = self._manage_positions()
        for item in closed:
            trade = item.get('trade')
            if trade:
                log.info(f"  CLOSED {item['symbol']} | reason={item['reason']} | pnl={trade.pnl:.0f}")
                self.trade_logger.update_trade(
                    trade_id=getattr(trade, 'id', ''),
                    exit_price=getattr(trade, 'exit_price', 0.0),
                    status='WIN' if trade.pnl > 0 else 'LOSS',
                    exit_reason=item['reason'],
                    pnl=trade.pnl,
                    pnl_percent=getattr(trade, 'pnl_percent', 0.0),
                    holding_period=getattr(trade, 'holding_days', 0),
                    drawdown=0.0,
                    max_favorable=0.0,
                )
                self.tuner.record_trade(
                    symbol=item['symbol'],
                    pnl=trade.pnl,
                    pnl_pct=getattr(trade, 'pnl_percent', 0.0),
                    exit_reason=item['reason'],
                    signal_strength=0.0,
                )
            else:
                log.info(f"  CLOSED {item}")

        # 2. Fast-scan for volume surges
        candidates = self.surge_scanner.scan()
        if not candidates:
            log.info("  No volume surges detected")
            return

        # 3. Run full pipeline on candidates
        new_trades = self._run_pipeline(candidates)
        log.info(f"  Executed {len(new_trades)} new trades")

    # ─────────────────────────────────────────────────────────────────────
    # Position management
    # ─────────────────────────────────────────────────────────────────────

    def _manage_positions(self) -> List[Dict]:
        def get_price(sym):
            # days=5  -> intraday 5-min bars (below scanner threshold 15)
            df = self.scanner.get_market_data(sym, 5)
            if df is not None and not df.empty:
                return float(df['close'].iloc[-1])
            return None

        def get_data(sym):
            return self.scanner.get_market_data(sym, 5)

        return self.execution.manage_open_positions(get_price, get_data)

    # ─────────────────────────────────────────────────────────────────────
    # Full pipeline on candidate symbols
    # ─────────────────────────────────────────────────────────────────────

    def _run_pipeline(self, candidates: List[str]) -> List[Dict]:
        # GAP #4: check can_trade() once at the top of the pipeline cycle.
        # If the kill gate fires (daily-loss, drawdown-halt, consecutive-loss
        # limit, trades-per-day), skip the entire candidate batch — no point
        # running expensive signal generation if we cannot open anything.
        # We build live mark prices here (last close of each open position) so
        # the MTM-aware daily-loss and drawdown checks (GAP #2) are fed real data.
        open_positions = self.execution.get_open_positions()
        live_marks: Dict[str, float] = {}
        for pos in open_positions:
            df = self.scanner.get_market_data(pos.symbol, 5)
            if df is not None and not df.empty:
                live_marks[pos.symbol] = float(df['close'].iloc[-1])

        risk_engine = self.execution.risk
        if not risk_engine.can_trade(mark_prices=live_marks or None):
            log.info("  [KILL GATE] can_trade() blocked — daily-loss/drawdown/limits hit; "
                     "skipping new entries this tick")
            return []

        open_syms = {p.symbol for p in open_positions}
        executed: List[Dict] = []
        signal_items: List[Dict] = []

        for sym in candidates:
            if sym in open_syms:
                continue

            # days=10  -> triggers 5-min intraday bars (scanner threshold=15)
            # 10 days × 78 bars/day = ~780 bars  - correct for intraday signal detection
            df = self.scanner.get_market_data(sym, 10)
            if df is None or len(df) < 25:
                continue

            # Signal generation
            signal = self.signal_engine.generate_signal(sym, df)
            if signal is None:
                continue

            # Fake-breakout filter (hard 2x volume gate inside)
            filter_result = self.fake_filter.analyze(df, signal)
            if not filter_result.is_valid:
                log.debug(f"  SKIP {sym}: fake-breakout {filter_result.reasons}")
                continue

            # Entry refinement (VWAP/pullback)
            entry_ctx = self.exec_refiner.refine_entry(df, signal, signal.entry_price)
            if entry_ctx.refinement in (EntryRefinement.AVOID, EntryRefinement.WAIT_VWAP):
                log.debug(f"  SKIP {sym}: bad VWAP entry")
                continue

            # Volatility regime
            vol_analysis = self.vol_engine.analyze_symbol(sym, df)
            vol_risk = vol_analysis.get('risk_params', {}) if vol_analysis else {}

            entry_volume  = float(df['volume'].iloc[-1])
            entry_vol_avg = float(df['volume'].rolling(20).mean().iloc[-1]) if len(df) >= 20 else entry_volume

            signal_items.append({
                'signal':         signal,
                'filter_result':  filter_result,
                'df':             df,
                'entry_price':    entry_ctx.entry_price,
                'entry_volume':   entry_volume,
                'entry_vol_avg':  entry_vol_avg,
                'vol_risk':       vol_risk,
            })

        if not signal_items:
            return []

        # Order flow ranking
        def get_df(sym):
            for it in signal_items:
                if it['signal'].symbol == sym:
                    return it['df']
            return pd.DataFrame()

        try:
            of_ranked = self.of_analyzer.rank_signals(signal_items, get_df)
        except Exception:
            of_ranked = signal_items

        # Trade ranking
        try:
            top = self.trade_ranker.get_top_signals(
                of_ranked,
                n=config.RISK_CONFIG['max_trades_per_day'],
            )
        except Exception:
            top = of_ranked[:config.RISK_CONFIG['max_trades_per_day']]

        # Execute top signals
        for item in top:
            signal      = item.get('signal') if isinstance(item, dict) else item
            entry_price = item.get('entry_price', signal.entry_price) if isinstance(item, dict) else signal.entry_price
            entry_vol   = item.get('entry_volume', 0.0) if isinstance(item, dict) else 0.0
            entry_va    = item.get('entry_vol_avg', 0.0) if isinstance(item, dict) else 0.0
            vol_risk    = item.get('vol_risk', {}) if isinstance(item, dict) else {}

            sl_mult = vol_risk.get('sl_multiplier', 1.0)
            atr_adj = signal.atr * sl_mult

            result = self.execution.execute_trade(
                symbol       = signal.symbol,
                direction    = signal.direction,
                capital      = self.capital,
                entry_price  = entry_price,
                atr          = atr_adj,
                strike       = None,
                use_options  = False,
                entry_volume = entry_vol,
                entry_vol_avg= entry_va,
                mark_prices  = live_marks or None,  # GAP #2: feed open-MTM into entry gate
            )

            if result and result.success:
                log.info(f"  TRADE {signal.symbol} {signal.direction.upper()} "
                         f"@ {result.filled_price:.2f} | qty={result.filled_quantity} "
                         f"| str={signal.strength:.0f} | {signal.patterns[:3]}")
                executed.append({'signal': signal, 'result': result})
                position = next(
                    (pos for pos in self.execution.get_open_positions() if pos.symbol == signal.symbol),
                    None,
                )
                if position is not None:
                    stop_loss = position.sl_price
                    target_price = position.target_price
                else:
                    sl_pct = config.RISK_CONFIG["sl_pct"]
                    stop_loss = (
                        result.filled_price * (1 - sl_pct)
                        if signal.direction == "long"
                        else result.filled_price * (1 + sl_pct)
                    )
                    risk_per_share = abs(result.filled_price - stop_loss)
                    target_price = (
                        result.filled_price + (risk_per_share * config.RISK_CONFIG["min_risk_reward"])
                        if signal.direction == "long"
                        else result.filled_price - (risk_per_share * config.RISK_CONFIG["min_risk_reward"])
                    )

                # Log trade
                self.trade_logger.log_trade({
                    'symbol':        signal.symbol,
                    'direction':     signal.direction.upper(),
                    'entry_price':   result.filled_price,
                    'exit_price':    0,
                    'stop_loss':     stop_loss,
                    'target':        target_price,
                    'quantity':      result.filled_quantity,
                    'pnl':           0,
                    'pnl_percent':   0,
                    'status':        'OPEN',
                    'reason':        signal.reason,
                    'order_id':      result.order_id,
                    'volatility_regime': str(signal.volatility),
                    'position_size': 'full',
                    'rank':          item.get('rank', 0) if isinstance(item, dict) else 0,
                    'total_score':   item.get('total_score', 0) if isinstance(item, dict) else 0,
                    'session':       self._current_session(),
                })

        return executed

    def _current_session(self) -> str:
        now = datetime.now().time()
        from datetime import time as dtime
        if dtime(9, 15) <= now <= dtime(10, 15):
            return "first_hour"
        elif dtime(14, 30) <= now <= dtime(15, 30):
            return "power_hour"
        elif dtime(12, 0) <= now <= dtime(13, 30):
            return "dead_zone"
        else:
            return "mid_session"

    # ─────────────────────────────────────────────────────────────────────
    # Record closed trades to auto-tuner
    # ─────────────────────────────────────────────────────────────────────

    def record_closed_trade(self, symbol: str, pnl: float, pnl_pct: float,
                             exit_reason: str, signal_strength: float = 0.0) -> None:
        self.tuner.record_trade(symbol, pnl, pnl_pct, exit_reason, signal_strength)


# ─────────────────────────────────────────────────────────────────────────────
# Live Runner
# ─────────────────────────────────────────────────────────────────────────────

class LiveRunner:

    def __init__(self, capital: float = 100_000):
        self.capital   = capital
        self.pipeline  = IntegratedPipeline(capital)
        self.analyzer  = PostMarketAnalyzer(capital, self.pipeline.tuner)
        self._running  = False

    @staticmethod
    def startup_check() -> bool:
        """Validate parse health, credentials, log writability before main loop."""
        ok = True

        # Credentials
        from core import secrets as _sec
        cid = _sec.get_client_id()
        tok = _sec.get_access_token()
        if not cid or not tok:
            log.error("STARTUP: Dhan credentials missing  - run secrets.py to configure")
            ok = False
        else:
            h = _sec.token_health(tok)
            if not h.valid:
                log.error(f"STARTUP: Token invalid  - {h.message}")
                ok = False
            else:
                log.info(f"STARTUP: Token OK  - {h.message}")

        # Log path writable
        try:
            test_path = os.path.join(LOG_DIR, ".startup_probe")
            with open(test_path, "w") as f:
                f.write("ok")
            os.remove(test_path)
            log.info("STARTUP: Log dir writable")
        except Exception as e:
            log.error(f"STARTUP: Log dir not writable  - {e}")
            ok = False

        # Symbol coverage
        try:
            from core.api_dhan import SECURITY_ID_MAP
            mapped = [s for s in FO_UNIVERSE if s in SECURITY_ID_MAP]
            missing = [s for s in FO_UNIVERSE if s not in SECURITY_ID_MAP]
            log.info(f"STARTUP: {len(mapped)}/{len(FO_UNIVERSE)} symbols have security IDs")
            if missing:
                log.warning(f"STARTUP: No security_id for: {missing[:10]}")
        except Exception as e:
            log.warning(f"STARTUP: Symbol coverage check failed  - {e}")

        # NSE API parse health
        try:
            from core.api_nse import nse_api  # noqa  - import verifies no SyntaxError
            log.info("STARTUP: api_nse parsed OK")
        except SyntaxError as e:
            log.error(f"STARTUP: api_nse SyntaxError  - {e}")
            ok = False
        except Exception:
            pass  # import errors (missing deps) are non-fatal at startup

        return ok

    def run(self) -> None:
        log.info(f"\n{'='*60}")
        log.info(f"  LIVE RUNNER STARTED  capital=Rs {self.capital:,.0f}")
        log.info(f"  Scan interval: {SCAN_INTERVAL_SEC}s  Vol threshold: "
                 f"{config.VOLUME_EXIT_CONFIG['entry_min_vol_ratio']}x")
        log.info(f"{'='*60}\n")

        if not self.startup_check():
            log.error("Startup checks failed  - aborting. Fix errors above and retry.")
            return

        self._running = True

        while self._running:
            now = _now_ist()

            if self.pipeline.is_market_open():
                self.pipeline.tick()
                self._sleep_until_next_tick()

            elif self.pipeline.market_just_closed():
                log.info("Market closed  - running post-market analysis...")
                self.analyzer.run()
                self._running = False

            else:
                wait = self._seconds_to_market_open()
                log.info(f"Market not open yet. Opening in {wait//60:.0f}m {wait%60:.0f}s")
                time.sleep(min(60, wait))

    def _sleep_until_next_tick(self) -> None:
        time.sleep(SCAN_INTERVAL_SEC)

    def _seconds_to_market_open(self) -> int:
        now = _now_ist()
        open_dt = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0, microsecond=0)
        if now < open_dt and now.weekday() < 5:
            return int((open_dt - now).total_seconds())

        next_open = open_dt + timedelta(days=1)
        while next_open.weekday() >= 5:
            next_open += timedelta(days=1)
        return int((next_open - now).total_seconds())


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Live trading runner")
    parser.add_argument("--capital", type=float, default=100_000, help="Trading capital in INR")
    parser.add_argument("--mode", choices=["live", "analyze", "tune", "backtest"],
                        default="live", help="Run mode")
    args = parser.parse_args()

    # Load and apply persisted config overrides
    overrides = load_config_overrides()
    apply_config_overrides(overrides)

    if args.mode == "live":
        runner = LiveRunner(capital=args.capital)
        runner.run()

    elif args.mode == "analyze":
        tuner    = AutoTuner()
        analyzer = PostMarketAnalyzer(args.capital, tuner)
        analyzer.run()

    elif args.mode == "tune":
        tuner = AutoTuner()
        changes = tuner.tune()
        print(f"Auto-tune changes: {changes if changes else 'none'}")

    elif args.mode == "backtest":
        from backtest import BacktestEngine
        bt = BacktestEngine(symbols=FO_UNIVERSE[:20], capital=args.capital, verbose=True)
        bt.load_data()
        metrics = bt.run()
        bt.save_trades_csv()
        bt.save_equity_csv()


if __name__ == "__main__":
    main()
