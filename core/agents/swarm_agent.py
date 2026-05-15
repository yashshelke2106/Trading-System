"""
SwarmAgent — Collective intelligence that learns from every trade.

Evolves through stages: NOOB -> LEARNING -> COMPETENT -> ADVANCED -> ELITE

Subscribes to:
  SIGNAL_GENERATED  - Evaluate signal against swarm memory (pre-filter)
  TRADE_CLOSED      - Learn from outcome, update all DNA

Periodic (60s):
  - Publish swarm status to SharedState
  - Apply optimal params if stage >= COMPETENT

The swarm is the system's long-term memory. Individual agents handle
real-time decisions; the swarm handles pattern recognition across
hundreds of trades.
"""

import logging
import time
from typing import Dict

from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent
from core.swarm_intelligence import get_swarm

log = logging.getLogger(__name__)


class SwarmAgent(BaseAgent):
    name = "swarm"
    interval_sec = 60

    def __init__(self, state: SharedState, bus: EventBus):
        super().__init__(state, bus)
        self.swarm = get_swarm()
        self._last_publish = 0

        bus.subscribe("SIGNAL_GENERATED", self._on_signal)
        bus.subscribe("TRADE_CLOSED", self._on_trade_closed)
        bus.subscribe("INNOVATION_UPDATE", self._on_innovation)

    def run(self) -> None:
        """Periodic: publish swarm state + apply adaptations."""
        status = self.swarm.get_status()
        self.state.set(
            swarm_stage=status["stage"],
            swarm_generation=status["generation"],
            swarm_total_trades=status["total_trades"],
            swarm_streak=status["streak"],
            swarm_best_patterns=status["best_patterns"],
            swarm_worst_patterns=status["worst_patterns"],
            pattern_blacklist=self.swarm.get_blacklisted_patterns(),
            pattern_whitelist=self.swarm.get_whitelisted_patterns(),
            symbol_blacklist=self.swarm.get_symbol_blacklist(),
            symbol_whitelist=self.swarm.get_symbol_whitelist(),
            swarm_best_hours=self.swarm.get_best_hours(),
            swarm_worst_hours=self.swarm.get_worst_hours(),
        )

        # Apply optimal params to config (COMPETENT+ only)
        if status["stage"] in ("COMPETENT", "ADVANCED", "ELITE"):
            self._apply_optimal_params()

        # Log status every 5 min
        now = time.time()
        if now - self._last_publish > 300:
            log.info(f"[Swarm] stage={status['stage']} gen={status['generation']} "
                     f"trades={status['total_trades']} streak={status['streak']} "
                     f"pnl={status['cumulative_pnl_pct']:+.1f}%")
            self._last_publish = now

    def _on_signal(self, event: AgentEvent) -> None:
        """Evaluate signal against swarm collective memory."""
        sym = event.payload.get("symbol", "")
        direction = event.payload.get("direction", "long")
        patterns = event.payload.get("patterns", [])
        rsi = event.payload.get("rsi", 0)
        vol_ratio = event.payload.get("vol_ratio", 0)
        regime = self.state.get("market_regime", "neutral")

        from datetime import datetime, timezone, timedelta
        _ist = timezone(timedelta(hours=5, minutes=30))
        hour = datetime.now(_ist).hour

        evaluation = self.swarm.evaluate_signal(
            symbol=sym,
            direction=direction,
            patterns=patterns,
            rsi=rsi,
            volume_ratio=vol_ratio,
            hour=hour,
            regime=regime,
        )

        # Cast vote based on swarm evaluation
        self.state.cast_vote(
            symbol=sym,
            agent=self.name,
            approve=evaluation["approve"],
            direction=direction,
            confidence=evaluation["confidence"],
        )

        if not evaluation["approve"]:
            reasons_str = ", ".join(evaluation["reasons"][:3])
            log.info(f"[Swarm] BLOCK {sym}: score={evaluation['score']:.0f} [{reasons_str}]")
        elif evaluation["score"] > 10:
            log.info(f"[Swarm] STRONG {sym}: score={evaluation['score']:.0f} "
                     f"conf={evaluation['confidence']:.2f}")

    def _on_trade_closed(self, event: AgentEvent) -> None:
        """Learn from every trade outcome."""
        trade = event.payload
        if not trade.get("outcome"):
            # Compute outcome from pnl if not set
            pnl = trade.get("pnl", 0)
            if pnl > 0:
                trade["outcome"] = "TARGET_HIT"
            elif pnl < 0:
                trade["outcome"] = "SL_HIT"
            else:
                return

        # Compute pnl_pct if not present
        if "pnl_pct" not in trade:
            entry = trade.get("entry_price", 0)
            pnl = trade.get("pnl", 0)
            qty = trade.get("qty", 1)
            if entry and qty:
                trade["pnl_pct"] = pnl / (entry * qty) * 100

        insights = self.swarm.learn_from_trade(trade)

        if insights.get("stage_up"):
            self.emit("SWARM_EVOLVED", {
                "new_stage": insights["stage_up"],
                "trade_num": insights["trade_num"],
                "generation": self.swarm.generation,
            })
            log.info(f"[Swarm] EVOLVED to {insights['stage_up']}!")

        if insights.get("adaptations"):
            self.emit("SWARM_ADAPTED", {
                "generation": self.swarm.generation,
                "adaptations": insights["adaptations"],
                "stage": self.swarm.stage,
            })

    def _on_innovation(self, event: AgentEvent) -> None:
        """Sync innovation agent findings with swarm memory."""
        # Innovation agent discovers combos - swarm can use these
        pass  # Swarm already tracks combos independently

    def _apply_optimal_params(self):
        """Apply swarm-derived optimal params to live config."""
        import config as cfg
        params = self.swarm.get_optimal_params()

        if "rsi_floor" in params:
            cfg.SIGNAL_CONFIG["rsi_long_momentum_min"] = params["rsi_floor"]
        if "block_after_hour" in params:
            cfg.SIGNAL_CONFIG["block_after_hour"] = params["block_after_hour"]
        if "min_sl_pct" in params:
            cfg.RISK_CONFIG["min_sl_pct"] = params["min_sl_pct"]
        if "max_sl_pct" in params:
            cfg.RISK_CONFIG["max_sl_pct"] = params["max_sl_pct"]
        if "min_votes" in params:
            cfg.SIGNAL_CONFIG["min_votes"] = params["min_votes"]
