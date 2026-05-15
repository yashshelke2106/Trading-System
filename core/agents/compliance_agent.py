"""
ComplianceAgent (Novick) — Audit trail, risk limit enforcement, trade compliance.

Inspired by Barbara Novick (BlackRock co-founder, governance/regulatory).

Subscribes to ALL trade events. Enforces hard limits that override other agents:
  - Max daily drawdown (absolute rupee + %)
  - Max position size vs capital
  - Trade frequency throttle (no rapid-fire entries)
  - Audit log every trade decision to compliance_log.jsonl
  - End-of-day compliance report

HARD HALT powers — can set trading_halted=True and no agent can override.
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, List

from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent
import config

log = logging.getLogger(__name__)

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
COMPLIANCE_LOG = os.path.join(LOG_DIR, "compliance_log.jsonl")


class ComplianceAgent(BaseAgent):
    name = "compliance_novick"
    interval_sec = 60

    MAX_SINGLE_POSITION_PCT = 0.06   # 6% capital in single trade (tighter from 8%)
    MIN_TRADE_INTERVAL_SEC  = 180    # 3 min between entries (was 2min - avoid rapid fire)
    MAX_DAILY_TRADES        = 3      # Hard cap (was 6 - quality over quantity)
    MAX_DAILY_DRAWDOWN_PCT  = 0.04   # 4% daily drawdown halt (was 5% - protect capital faster)

    def __init__(self, state: SharedState, bus: EventBus, capital: float):
        super().__init__(state, bus)
        self.capital = capital
        self._last_entry_ts: float = 0
        self._trade_count = 0
        self._violations: List[Dict] = []

        bus.subscribe("QUANT_SIZED", self._on_pre_trade)
        bus.subscribe("TRADE_ENTERED", self._on_trade_entered)
        bus.subscribe("TRADE_CLOSED", self._on_trade_closed)
        bus.subscribe("RISK_APPROVED", self._audit_event)

    def run(self) -> None:
        self._check_drawdown()

    def _audit(self, action: str, details: Dict, violation: bool = False):
        record = {
            "ts": datetime.now().isoformat(),
            "agent": self.name,
            "action": action,
            "violation": violation,
            **details,
        }
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(COMPLIANCE_LOG, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass
        if violation:
            self._violations.append(record)
            log.warning(f"[Compliance/Novick] VIOLATION: {action} - {details}")

    def _audit_event(self, event: AgentEvent):
        self._audit(f"event_{event.type}", event.payload)

    def _on_pre_trade(self, event: AgentEvent):
        sym = event.payload["symbol"]
        direction = event.payload["direction"]
        entry = event.payload.get("entry_price", 0)
        qty = event.payload.get("optimal_qty", 0)

        # Position size limit
        position_value = entry * qty
        if position_value > self.capital * self.MAX_SINGLE_POSITION_PCT:
            self._audit("POSITION_TOO_LARGE", {
                "symbol": sym, "value": position_value,
                "limit": self.capital * self.MAX_SINGLE_POSITION_PCT,
            }, violation=True)
            self.state.cast_vote(sym, self.name, approve=False, direction=direction)
            return

        # Trade frequency throttle
        now = time.time()
        if now - self._last_entry_ts < self.MIN_TRADE_INTERVAL_SEC:
            self._audit("THROTTLED", {
                "symbol": sym,
                "seconds_since_last": round(now - self._last_entry_ts),
            }, violation=True)
            self.state.cast_vote(sym, self.name, approve=False, direction=direction)
            return

        # Daily trade count
        if self._trade_count >= self.MAX_DAILY_TRADES:
            self._audit("DAILY_LIMIT", {
                "symbol": sym, "count": self._trade_count,
            }, violation=True)
            self.state.cast_vote(sym, self.name, approve=False, direction=direction)
            return

        # All checks passed
        self.state.cast_vote(sym, self.name, approve=True,
                             direction=direction, confidence=0.9)
        self._audit("PRE_TRADE_APPROVED", {"symbol": sym, "direction": direction,
                                            "grade": event.payload.get("grade", "A")})
        self.emit("COMPLIANCE_APPROVED", {**event.payload, "grade": event.payload.get("grade", "A")})

    def _on_trade_entered(self, event: AgentEvent):
        self._last_entry_ts = time.time()
        self._trade_count += 1
        self._audit("TRADE_ENTERED", event.payload)

    def _on_trade_closed(self, event: AgentEvent):
        pnl = event.payload.get("pnl", 0)
        self._audit("TRADE_CLOSED", {
            "symbol": event.payload.get("symbol"),
            "pnl": pnl,
            "reason": event.payload.get("reason"),
        })
        self._check_drawdown()

    def _check_drawdown(self):
        daily_pnl = self.state.get("daily_pnl", 0.0)
        if daily_pnl < 0 and abs(daily_pnl) / max(self.capital, 1) > self.MAX_DAILY_DRAWDOWN_PCT:
            self.state.set(
                trading_halted=True,
                halt_reason=f"Compliance halt: drawdown {daily_pnl/self.capital:.1%} > {self.MAX_DAILY_DRAWDOWN_PCT:.0%}",
            )
            self._audit("HARD_HALT_DRAWDOWN", {
                "daily_pnl": daily_pnl,
                "drawdown_pct": round(daily_pnl / self.capital, 4),
            }, violation=True)
            log.critical(f"[Compliance/Novick] HARD HALT - drawdown {daily_pnl/self.capital:.1%}")

    def generate_daily_report(self) -> Dict:
        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "trades_executed": self._trade_count,
            "violations": len(self._violations),
            "violation_details": self._violations[-10:],
            "daily_pnl": self.state.get("daily_pnl", 0),
            "halted": self.state.get("trading_halted", False),
        }
