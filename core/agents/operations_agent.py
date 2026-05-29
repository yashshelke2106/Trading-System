"""
OperationsAgent (Kapito) — Broker integration health, execution quality, fill monitoring.

Inspired by Robert Kapito (BlackRock President, scaling/operations).

Every 60s:
  1. Monitor open position P&L (mark-to-market via LTP)
  2. Track slippage (entry_price vs actual fill)
  3. Broker connectivity health (Dhan API latency)
  4. Auto-tighten SL after 1R profit (trailing stop)
  5. Time-based exit: close positions open > 3 hours (avoid overnight risk)

On TRADE_ENTERED:
  - Record fill quality metrics
  - Start position monitoring

On COMPLIANCE_APPROVED:
  - Final execution gate — check broker is actually reachable before placing
"""

import logging
import time
from datetime import datetime
from typing import Dict, Optional

from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent
import config

log = logging.getLogger(__name__)

MAX_POSITION_AGE_SEC = 3 * 3600  # 3 hours max hold for intraday F&O


class OperationsAgent(BaseAgent):
    name = "ops_kapito"
    interval_sec = 60

    def __init__(self, state: SharedState, bus: EventBus, api, capital: float):
        super().__init__(state, bus)
        self.api = api
        self.capital = capital
        self._position_entries: Dict[str, Dict] = {}  # sym -> {entry_ts, entry_price, ...}
        self._slippage_log: list = []
        self._broker_latency_ms: float = 0

        bus.subscribe("COMPLIANCE_APPROVED", self._on_compliance_approved)
        bus.subscribe("TRADE_ENTERED", self._on_trade_entered)
        bus.subscribe("TRADE_CLOSED", self._on_trade_closed)

    def run(self) -> None:
        self._check_broker_health()
        self._monitor_positions()
        self._check_time_exits()

    def _check_broker_health(self):
        """Check data connectivity. Try Dhan Data API first, fall back to yfinance ping.
        Trading token validity is separate (checked by ArchitectAgent)."""
        try:
            t0 = time.time()
            result = self.api.test_data_api()
            latency = (time.time() - t0) * 1000
            self._broker_latency_ms = latency

            if result.get("ok"):
                self.state.set(broker_healthy=True, broker_latency_ms=round(latency))
                return

            # Dhan-only: no yfinance fallback. If Dhan data API is not active,
            # mark degraded (don't probe yahoo).
            self.state.set(broker_healthy=False, broker_latency_ms=9999)
            log.warning("[Ops/Kapito] Dhan data API not active - data degraded (no yfinance fallback)")
        except Exception as e:
            self.state.set(broker_healthy=False)
            log.error(f"[Ops/Kapito] broker check failed: {e}")

    def _monitor_positions(self):
        positions = self.state.get("open_positions", {})
        if not positions:
            return

        syms = list(positions.keys())
        try:
            quotes = self.api.get_quote(syms)
            if "error" in quotes:
                return

            for item in quotes.get("data", []):
                sym = item.get("symbol", "")
                ltp = item.get("last_price", 0)
                if not (sym and ltp and sym in positions):
                    continue

                pos = positions[sym]
                if not isinstance(pos, dict):
                    continue

                entry = pos.get("entry_price", 0)
                direction = pos.get("direction", "long")
                sl = pos.get("sl_price", 0)

                if entry <= 0:
                    continue

                # Mark-to-market P&L
                if direction == "long":
                    pnl_pct = (ltp - entry) / entry
                else:
                    pnl_pct = (entry - ltp) / entry

                # Trail SL to breakeven after 0.5R profit (more aggressive - protect gains early)
                risk = abs(entry - sl) if sl else entry * 0.01
                if pnl_pct * entry >= risk * 0.5:  # 0.5R instead of 1R
                    new_sl = entry * (1.001 if direction == "long" else 0.999)
                    if (direction == "long" and new_sl > sl) or \
                       (direction == "short" and new_sl < sl):
                        log.info(f"[Ops/Kapito] TRAIL SL {sym}: {sl:.2f} -> {new_sl:.2f} (breakeven)")
                        self.emit("TRAIL_SL", {
                            "symbol": sym,
                            "old_sl": sl,
                            "new_sl": round(new_sl, 2),
                            "reason": "1R_breakeven",
                        })
        except Exception as e:
            log.debug(f"[Ops/Kapito] position monitor error: {e}")

    def _check_time_exits(self):
        positions = self.state.get("open_positions", {})
        now = time.time()

        for sym, meta in self._position_entries.items():
            if sym not in positions:
                continue
            age = now - meta.get("entry_ts", now)
            if age > MAX_POSITION_AGE_SEC:
                log.warning(f"[Ops/Kapito] TIME EXIT {sym}: open {age/3600:.1f}h > 3h limit")
                self.emit("TIME_EXIT", {
                    "symbol": sym,
                    "age_hours": round(age / 3600, 2),
                    "reason": "max_hold_time_exceeded",
                })

    def _on_compliance_approved(self, event: AgentEvent):
        sym = event.payload.get("symbol", "")

        # Final broker check before execution
        if not self.state.get("broker_healthy", True):
            log.warning(f"[Ops/Kapito] BLOCK {sym}: broker unhealthy")
            return

        if self._broker_latency_ms > 10000:
            log.warning(f"[Ops/Kapito] BLOCK {sym}: latency {self._broker_latency_ms:.0f}ms")
            return

        self.state.cast_vote(
            sym, self.name, approve=True,
            direction=event.payload.get("direction", ""),
            confidence=0.85,
        )
        self.emit("EXECUTION_READY", event.payload)

    def _on_trade_entered(self, event: AgentEvent):
        sym = event.payload.get("symbol", "")
        self._position_entries[sym] = {
            "entry_ts": time.time(),
            "entry_price": event.payload.get("entry_price", 0),
            "direction": event.payload.get("direction", ""),
        }

    def _on_trade_closed(self, event: AgentEvent):
        sym = event.payload.get("symbol", "")
        entry_meta = self._position_entries.pop(sym, {})

        if entry_meta:
            hold_time = time.time() - entry_meta.get("entry_ts", time.time())
            self._slippage_log.append({
                "symbol": sym,
                "hold_sec": round(hold_time),
                "pnl": event.payload.get("pnl", 0),
            })
            if len(self._slippage_log) > 100:
                self._slippage_log = self._slippage_log[-100:]
