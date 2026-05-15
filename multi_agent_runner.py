"""
Multi-agent trading runner for NSE F&O.

7 specialized agents share state and vote on every trade:

  MarketContextAgent  [5 min]   - NIFTY/VIX regime classification
  ScannerAgent        [3 min]   - volume surge detection across 50 symbols
  SignalAgent         [event]   - technical signal generation
  FilterAgent         [event]   - fake-breakout + order-flow filter
  RiskAgent           [event]   - portfolio heat + regime direction guard
  ExecutionAgent      [60s+ev]  - consensus entry + position management
  LearningAgent       [event]   - trade recording + auto-tune trigger

Trade entry requires 3/4 approving votes AND unanimous direction.

Commands:
  python multi_agent_runner.py                   # full live session
  python multi_agent_runner.py --capital 200000  # custom capital
  python multi_agent_runner.py --mode analyze    # post-market only
  python multi_agent_runner.py --mode backtest   # run backtest
  python multi_agent_runner.py --mode status     # print shared state snapshot
"""

import sys
import os
import time
import json
import logging
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from core.agent_bus import SharedState, EventBus
from core.agents.market_context_agent import MarketContextAgent
from core.agents.scanner_agent import ScannerAgent
from core.agents.signal_agent import SignalAgent
from core.agents.filter_agent import FilterAgent
from core.agents.risk_agent import RiskAgent
from core.agents.execution_agent import ExecutionAgent
from core.agents.learning_agent import LearningAgent
from core.agents.notifier_agent import NotifierAgent
from core.scanner import LiquidityScanner
from core.market_feed import DhanMarketFeed
from core.api_dhan import SECURITY_ID_MAP
from core.notifier import TelegramNotifier
from live_runner import (
    FO_UNIVERSE, AutoTuner, PostMarketAnalyzer,
    load_config_overrides, apply_config_overrides,
    LOG_DIR, MARKET_OPEN_HOUR, MARKET_OPEN_MIN,
    MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "agents.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class MultiAgentOrchestrator:
    """
    Wires all agents together, starts/stops them, and drives the main loop.
    All inter-agent communication happens via EventBus + SharedState  -
    the orchestrator itself never touches signal logic.
    """

    def __init__(self, capital: float = 100_000):
        self.capital = capital
        self.state   = SharedState()
        self.bus     = EventBus()
        self.tuner   = AutoTuner()

        # Shared data source
        self.scanner = LiquidityScanner()

        # Live WebSocket feed  - provides real-time LTP + volume to all agents
        self.feed = DhanMarketFeed()

        # Telegram + dashboard notifications
        self.notifier = TelegramNotifier()

        # Build agent team (feed injected into agents that need real-time prices)
        self.ctx_agent    = MarketContextAgent(self.state, self.bus, self.scanner, feed=self.feed)
        self.scan_agent   = ScannerAgent(self.state, self.bus, self.scanner, FO_UNIVERSE, feed=self.feed)
        self.sig_agent    = SignalAgent(self.state, self.bus, self.scanner)
        self.filt_agent   = FilterAgent(self.state, self.bus, self.scanner)
        self.risk_agent   = RiskAgent(self.state, self.bus, capital)
        self.exec_agent   = ExecutionAgent(self.state, self.bus, self.scanner, capital, feed=self.feed)
        self.analyzer     = PostMarketAnalyzer(capital, self.tuner)
        # Pass analyzer into LearningAgent so post_market() owns the full sequence
        self.learn_agent  = LearningAgent(self.state, self.bus, self.tuner, self.analyzer)
        self.notify_agent = NotifierAgent(self.state, self.bus, self.notifier, feed=self.feed)

        self._agents = [
            self.ctx_agent,
            self.scan_agent,
            self.sig_agent,
            self.filt_agent,
            self.risk_agent,
            self.exec_agent,
            self.learn_agent,
            self.notify_agent,
        ]

        self._post_market_done = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        log.info("\n" + "=" * 65)
        log.info(f"  MULTI-AGENT RUNNER   capital=Rs {self.capital:,.0f}")
        log.info(f"  Agents  : {[a.name for a in self._agents]}")
        log.info(f"  Universe: {len(FO_UNIVERSE)} symbols")
        log.info(f"  Vol gate: {config.VOLUME_EXIT_CONFIG['entry_min_vol_ratio']}x  "
                 f"SL: {config.RISK_CONFIG['sl_pct']:.0%}  "
                 f"Consensus: {self.exec_agent.REQUIRED_VOTES}/4 votes")
        log.info("=" * 65 + "\n")

        # Start WebSocket feed for all universe symbols + indices
        feed_map = {sym: sid for sym, sid in SECURITY_ID_MAP.items()
                    if sym in set(FO_UNIVERSE) | {"NIFTY", "BANKNIFTY"}}
        self.feed.start(feed_map)
        log.info(f"[Feed] subscribed to {len(feed_map)} symbols via WebSocket")

        for agent in self._agents:
            agent.start()

    def stop(self) -> None:
        self.feed.stop()
        for agent in self._agents:
            agent.stop()
        log.info("All agents stopped")

    # ── Main loop ─────────────────────────────────────────────────────────

    def run(self) -> None:
        self.start()
        try:
            while True:
                if self._is_market_open():
                    time.sleep(10)
                    self._heartbeat()

                elif self._market_just_closed() and not self._post_market_done:
                    # learn_agent.post_market() owns full sequence:
                    # tune()  -> analyzer.run() (summary + backtest + daily report)
                    log.info("Market closed  - auto post-market sequence starting...")
                    self.learn_agent.post_market()
                    self._post_market_done = True
                    self._send_daily_summary()
                    break

                else:
                    secs = self._secs_to_open()
                    log.info(f"Market opens in {secs//60:.0f}m {secs%60:.0f}s")
                    time.sleep(min(60, max(secs, 1)))

        except KeyboardInterrupt:
            log.info("Interrupted")
        finally:
            self.stop()

    # ── Status helpers ────────────────────────────────────────────────────

    def _heartbeat(self) -> None:
        s = self.state
        feed_status = "WS:live" if self.feed.connected else "WS:poll"
        log.info(
            f"[HB] regime={s.get('market_regime','?')} "
            f"surges={len(s.get('volume_surges',{}))} "
            f"positions={len(s.get('open_positions',{}))} "
            f"trades={s.get('total_trades_today',0)} "
            f"heat={s.get('portfolio_heat',0):.1%} "
            f"losses={s.get('consecutive_losses',0)} "
            f"{feed_status}"
        )
        self._export_state()

    def _send_daily_summary(self) -> None:
        """Send end-of-day P&L summary to Telegram."""
        s = self.state
        trades = s.get("today_trades", [])
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        wins  = sum(1 for t in trades if t.get("pnl", 0) > 0)
        total = len(trades)
        wr    = (wins / total * 100) if total else 0
        icon  = "📈" if total_pnl >= 0 else "📉"
        sign  = "+" if total_pnl >= 0 else ""

        msg = (
            f"{icon} <b>DAILY SUMMARY</b>\n"
            f"Date    : {datetime.now().strftime('%d %b %Y')}\n"
            f"Trades  : {total}  (W:{wins} L:{total - wins}  WR:{wr:.0f}%)\n"
            f"Total P&L: ₹{sign}{total_pnl:,.0f}\n"
            f"Regime  : {s.get('market_regime', '?').upper()}"
        )
        self.notifier.notify("DAILY_SUMMARY", msg, pnl=total_pnl)
        log.info(f"[Daily] summary sent: trades={total} pnl={total_pnl:+.0f}")

    def _enrich_positions(self, positions: dict) -> dict:
        """Add live price, unrealized P&L, % change, SL distance to each position."""
        enriched = {}
        for sym, pos in positions.items():
            entry     = pos.get("entry", 0.0)
            qty       = pos.get("qty", 0)
            direction = pos.get("direction", "long")
            sl_price  = pos.get("sl_price", 0.0)
            target    = pos.get("target_price", 0.0)

            current = self.feed.get_ltp(sym) if self.feed else None

            upnl = pct = sl_dist_pct = None
            if current and entry:
                if direction == "long":
                    upnl = (current - entry) * qty
                    pct  = (current - entry) / entry * 100
                    sl_dist_pct = (current - sl_price) / entry * 100 if sl_price else None
                else:
                    upnl = (entry - current) * qty
                    pct  = (entry - current) / entry * 100
                    sl_dist_pct = (sl_price - current) / entry * 100 if sl_price else None

            enriched[sym] = {
                "entry":        entry,
                "qty":          qty,
                "direction":    direction,
                "confidence":   round(pos.get("confidence", 0), 3),
                "sl_price":     sl_price,
                "target_price": target,
                "current_price": round(current, 2) if current else None,
                "upnl":         round(upnl, 2) if upnl is not None else None,
                "pct":          round(pct, 2)  if pct  is not None else None,
                "sl_dist_pct":  round(sl_dist_pct, 2) if sl_dist_pct is not None else None,
            }
        return enriched

    def _export_state(self) -> None:
        """Write SharedState snapshot to logs/state.json for dashboard."""
        try:
            s = self.state
            snapshot = {
                "ts": datetime.now().isoformat(),
                "market_regime": s.get("market_regime", "unknown"),
                "nifty_change_pct": s.get("nifty_change_pct", 0.0),
                "banknifty_change_pct": s.get("banknifty_change_pct", 0.0),
                "vix_proxy": s.get("vix_proxy", 15.0),
                "portfolio_heat": s.get("portfolio_heat", 0.0),
                "daily_pnl": s.get("daily_pnl", 0.0),
                "total_trades_today": s.get("total_trades_today", 0),
                "consecutive_losses": s.get("consecutive_losses", 0),
                "win_streak": s.get("win_streak", 0),
                "trading_halted": s.get("trading_halted", False),
                "halt_reason": s.get("halt_reason", ""),
                "volume_surges": {
                    sym: round(ratio, 2)
                    for sym, ratio in s.get("volume_surges", {}).items()
                },
                "open_positions": self._enrich_positions(s.get("open_positions", {})),
                "today_trades": s.get("today_trades", []),
                "agents": [a.name for a in self._agents],
            }
            state_file = os.path.join(LOG_DIR, "state.json")
            with open(state_file, "w") as f:
                json.dump(snapshot, f, indent=2, default=str)
        except Exception as e:
            log.debug(f"[HB] state export error: {e}")

    def print_status(self) -> None:
        s = self.state
        print("\n" + "=" * 55)
        print("  SHARED STATE SNAPSHOT")
        print("=" * 55)
        print(f"  Market regime     : {s.market_regime}")
        print(f"  NIFTY change      : {s.nifty_change_pct:+.2f}%")
        print(f"  VIX proxy         : {s.vix_proxy:.1f}")
        print(f"  Volume surges     : {len(s.volume_surges)} symbols")
        print(f"  Open positions    : {len(s.open_positions)}")
        print(f"  Portfolio heat    : {s.portfolio_heat:.1%}")
        print(f"  Daily P&L         : {s.daily_pnl:+.0f}")
        print(f"  Trades today      : {s.total_trades_today}")
        print(f"  Win streak        : {s.win_streak}")
        print(f"  Consecutive losses: {s.consecutive_losses}")
        print(f"  Trading halted    : {s.trading_halted} ({s.halt_reason})")
        if s.open_positions:
            print("\n  Open positions:")
            for sym, pos in s.open_positions.items():
                print(f"    {sym}: {pos.get('direction','?').upper()} "
                      f"@ {pos.get('entry', 0):.2f} "
                      f"qty={pos.get('qty', 0)} "
                      f"conf={pos.get('confidence', 0):.0%}")
        print("=" * 55)

    @staticmethod
    def _is_market_open() -> bool:
        from datetime import time as dtime
        now = datetime.now().time()
        return dtime(MARKET_OPEN_HOUR, MARKET_OPEN_MIN) <= now <= dtime(MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN)

    @staticmethod
    def _market_just_closed() -> bool:
        from datetime import time as dtime
        now = datetime.now().time()
        return dtime(MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN) <= now <= dtime(16, 30)

    @staticmethod
    def _secs_to_open() -> int:
        now = datetime.now()
        open_dt = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0, microsecond=0)
        return max(0, int((open_dt - now).total_seconds()))


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-agent F&O trading runner")
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--mode", choices=["live", "analyze", "backtest", "status"],
                        default="live")
    args = parser.parse_args()

    # Load persisted auto-tuned parameters
    overrides = load_config_overrides()
    apply_config_overrides(overrides)

    if args.mode == "live":
        orch = MultiAgentOrchestrator(capital=args.capital)
        orch.run()

    elif args.mode == "analyze":
        tuner = AutoTuner()
        PostMarketAnalyzer(args.capital, tuner).run()

    elif args.mode == "backtest":
        from backtest import BacktestEngine
        bt = BacktestEngine(symbols=FO_UNIVERSE[:20], capital=args.capital, verbose=True)
        bt.load_data()
        metrics = bt.run()
        bt.save_trades_csv()
        bt.save_equity_csv()

    elif args.mode == "status":
        # Snapshot without running market
        orch = MultiAgentOrchestrator(capital=args.capital)
        orch.print_status()


if __name__ == "__main__":
    main()
