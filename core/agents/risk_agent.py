"""
RiskAgent — portfolio heat + regime-aware sizing on SIGNAL_FILTERED.

Gates:
  - trading_halted flag (consecutive losses)
  - max open positions
  - portfolio heat >= 25% capital
  - regime: counter-trend trades get REDUCED size, not blocked (unbiased)

Casts 'risk' vote with size_multiplier from regime.
Publishes RISK_APPROVED.
"""

import logging
import config
from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent

log = logging.getLogger(__name__)


class RiskAgent(BaseAgent):
    name = "risk"
    interval_sec = 0

    MAX_HEAT       = 0.25   # 25% capital at risk simultaneously
    MAX_POSITIONS  = 5

    def __init__(self, state: SharedState, bus: EventBus, capital: float):
        super().__init__(state, bus)
        self.capital = capital
        bus.subscribe("SIGNAL_FILTERED", self._on_filtered)

    def run(self) -> None:
        pass

    def _on_filtered(self, event: AgentEvent) -> None:
        sym = event.payload["symbol"]
        direction = event.payload["direction"]

        # Hard halt gate
        if self.state.get("trading_halted", False):
            log.debug(f"[Risk] HALT {sym}: {self.state.get('halt_reason')}")
            return

        # Consecutive loss gate
        consec = self.state.get("consecutive_losses", 0)
        max_consec = config.RISK_CONFIG.get("max_consecutive_losses", 4)
        if consec >= max_consec:
            self.state.set(trading_halted=True, halt_reason=f"{consec} consecutive losses")
            log.warning(f"[Risk] HALTING: {consec} consecutive losses")
            return

        # Position count gate
        open_positions = self.state.get("open_positions", {})
        if len(open_positions) >= self.MAX_POSITIONS:
            log.debug(f"[Risk] BLOCK {sym}: max positions ({self.MAX_POSITIONS})")
            return

        # Portfolio heat gate
        heat = self.state.get("portfolio_heat", 0.0)
        if heat >= self.MAX_HEAT:
            log.debug(f"[Risk] BLOCK {sym}: portfolio heat {heat:.1%}")
            return

        # Daily trade count gate
        trades_today = self.state.get("total_trades_today", 0)
        max_trades = config.RISK_CONFIG.get("max_trades_per_day", 3)
        if trades_today >= max_trades:
            log.info(f"[Risk] BLOCK {sym}: daily limit {trades_today}/{max_trades}")
            return

        # Regime-aware sizing (NOT blocking — both directions always allowed)
        regime = self.state.get("market_regime", "neutral")
        if regime == "volatile":
            log.debug(f"[Risk] BLOCK {sym}: volatile regime")
            return

        # Counter-trend = reduced size, NOT blocked. System stays unbiased.
        counter_trend = (
            (regime in ("bear", "strong_bear") and direction == "long") or
            (regime in ("bull", "strong_bull") and direction == "short")
        )
        if counter_trend:
            log.info(f"[Risk] COUNTER-TREND {sym}: {direction} in {regime} regime → reduced size")

        # Innovation-informed RSI gate: applies symmetrically to both directions
        rsi_win_avg = self.state.get("innovation_rsi_win_avg", 64.7)
        rsi_loss_avg = self.state.get("innovation_rsi_loss_avg", 53.8)
        signal_rsi = event.payload.get("rsi", 0)
        if signal_rsi:
            midpoint = (rsi_win_avg + rsi_loss_avg) / 2
            # Symmetric RSI gate:
            # Longs: RSI should be above midpoint (momentum)
            # Shorts: RSI should be below midpoint (bearish momentum). RSI > 70 = IDEAL short (exhaustion)
            rsi_unfavorable = (
                (direction == "long" and signal_rsi < midpoint) or
                (direction == "short" and signal_rsi > midpoint and signal_rsi < 65)  # shorts in 50-65 = weak conviction
            )
            if rsi_unfavorable:
                log.info(f"[Risk] RSI_PENALTY {sym}: RSI={signal_rsi:.0f} unfavorable for {direction}")

        # Size multiplier: base from regime + counter-trend penalty
        base_mult = {"bull": 1.0, "bear": 1.0, "neutral": 0.85,
                     "strong_bull": 1.0, "strong_bear": 1.0}.get(regime, 0.85)
        size_mult = base_mult * (0.6 if counter_trend else 1.0)  # 40% reduction, not block

        # Confidence from upstream filter scores
        fb_conf = event.payload.get("fb_confidence", 0.5)
        confidence = fb_conf * size_mult

        self.state.cast_vote(
            symbol=sym, agent=self.name,
            approve=True,
            direction=direction,
            confidence=confidence,
        )

        self.emit("RISK_APPROVED", {
            "symbol": sym,
            "direction": direction,
            "entry_price": event.payload.get("entry_price", 0),
            "atr": event.payload.get("atr", 0),
            "rsi": event.payload.get("rsi", 0),
            "size_mult": size_mult,
            "fb_confidence": fb_conf,
            "grade": event.payload.get("grade", "A"),
        })
        log.info(f"[Risk] APPROVE {sym} {direction} heat={heat:.1%} size={size_mult:.2f}")
