"""
CoordinatorAgent (Nair) — Executive oversight, agent orchestration, conflict resolution.

Inspired by Sudhir Nair (BlackRock tech/operations executive).

Responsibilities:
  1. Agent health monitoring — detect stuck/crashed agents, restart
  2. Consensus enforcement — require 4/5 votes before execution
  3. Conflict resolution — when agents disagree, break tie using macro bias
  4. Priority queue — rank pending signals by urgency + confidence
  5. Post-market trigger — call LearningAgent + InnovationAgent end-of-day analysis
  6. Daily P&L reporting — aggregate and emit summary

Every 30s: check system state, resolve pending decisions.
At 15:30 IST: trigger post-market sequence.
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Agents that must vote for execution (subset - not all agents vote on every signal)
# Swarm agent added: collective intelligence gets a vote
REQUIRED_VOTERS = {"risk", "quant_golub", "compliance_novick", "ops_kapito", "swarm"}
MIN_CONSENSUS = 3  # at least 3 of 5 must approve

# Tiered Authority: S-grade signals need fewer approvals (fast-track)
S_GRADE_VOTERS = {"risk", "quant_golub", "swarm"}  # 3 for Grade S (swarm adds collective wisdom)
S_GRADE_MIN = 2


class CoordinatorAgent(BaseAgent):
    name = "coordinator_nair"
    interval_sec = 30

    def __init__(self, state: SharedState, bus: EventBus,
                 agents: Dict[str, "BaseAgent"] = None):
        super().__init__(state, bus)
        self._agents = agents or {}
        self._pending_symbols: List[str] = []
        self._post_market_done_today = False
        self._last_day = None

        bus.subscribe("EXECUTION_READY", self._on_execution_ready)
        bus.subscribe("SYSTEM_HEALTH", self._on_health)

    def register_agents(self, agents: Dict[str, "BaseAgent"]):
        self._agents = agents

    def run(self) -> None:
        now_ist = datetime.now(_IST).replace(tzinfo=None)
        today = now_ist.date()

        # Day reset
        if self._last_day != today:
            self._last_day = today
            self._post_market_done_today = False
            self.state.set(
                trading_halted=False,
                halt_reason="",
                consecutive_losses=0,
                daily_pnl=0.0,
                total_trades_today=0,
            )
            log.info(f"[Coordinator/Nair] new day {today} - state reset")

        # Post-market trigger at 15:30+
        hour_min = now_ist.hour * 60 + now_ist.minute
        if hour_min >= 15 * 60 + 30 and not self._post_market_done_today:
            self._trigger_post_market()
            self._post_market_done_today = True

        # Resolve pending consensus decisions
        self._resolve_pending()

        # Agent liveness check
        self._check_agent_health()

        # Emit daily summary every loop
        self._emit_summary()

    def _on_execution_ready(self, event: AgentEvent):
        sym = event.payload.get("symbol", "")
        if not sym or sym in self._pending_symbols:
            return

        # Time gate: avoid the midday dead zone, but allow power-hour setups.
        from datetime import datetime, timezone, timedelta
        _ist = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(_ist)
        minutes_now = now_ist.hour * 60 + now_ist.minute
        dead_zone_start = 13 * 60
        dead_zone_end = 14 * 60 + 30
        if dead_zone_start <= minutes_now < dead_zone_end:
            log.info(f"[Coordinator/Nair] BLOCK {sym}: midday dead zone ({now_ist.strftime('%H:%M')})")
            return

        self._pending_symbols.append(sym)
        # Tag grade for tiered authority
        grade = event.payload.get("grade", "A")
        self.state.set(**{f"_pending_grade_{sym}": grade})

    def _on_health(self, event: AgentEvent):
        status = event.payload.get("status", "unknown")
        if status == "critical":
            issues = event.payload.get("issues", [])
            log.critical(f"[Coordinator/Nair] SYSTEM CRITICAL: {issues}")
            # Don't halt trading just for infra issues — architect self-heals

    def _resolve_pending(self):
        resolved = []
        for sym in self._pending_symbols:
            votes = self.state.get_votes(sym)

            # Tiered Authority: S-grade needs only Risk + Quant (fast-track)
            grade = self.state.get(f"_pending_grade_{sym}", "A")
            if grade == "S":
                req_voters = S_GRADE_VOTERS
                min_needed = S_GRADE_MIN
            else:
                req_voters = REQUIRED_VOTERS
                min_needed = MIN_CONSENSUS

            approvals = {a: v for a, v in votes.items()
                         if v.get("approve") and a in req_voters}
            rejections = {a: v for a, v in votes.items()
                          if not v.get("approve") and a in req_voters}

            # Need min_needed approvals from required voters for this tier
            if len(approvals) >= min_needed:
                # Direction must be unanimous among approvers
                dirs = [v["direction"] for v in approvals.values() if v.get("direction")]
                if dirs and len(set(dirs)) == 1:
                    direction = dirs[0]
                    avg_conf = sum(v.get("confidence", 0.5)
                                   for v in approvals.values()) / len(approvals)

                    # Macro bias: SYMMETRIC boost for trend-aligned, penalty for counter-trend
                    macro = self.state.get("macro_bias", "neutral")
                    with_trend = (
                        (macro in ("strong_bull", "bull") and direction == "long") or
                        (macro in ("strong_bear", "bear") and direction == "short")
                    )
                    counter_trend = (
                        (macro in ("strong_bull", "bull") and direction == "short") or
                        (macro in ("strong_bear", "bear") and direction == "long")
                    )
                    if with_trend:
                        avg_conf = min(avg_conf + 0.1, 1.0)
                    elif counter_trend:
                        avg_conf *= 0.7  # reduce, don't block

                    tier_tag = "S-FAST" if grade == "S" else "A-FULL"
                    # Grade-based position sizing: S=100%, A=75%, B=50%, C=30%
                    grade_size = {"S": 1.0, "A": 0.75, "B": 0.5, "C": 0.3}.get(grade, 0.5)
                    self.emit("FINAL_EXECUTE", {
                        "symbol": sym,
                        "direction": direction,
                        "confidence": round(avg_conf, 3),
                        "approvers": list(approvals.keys()),
                        "voters": len(votes),
                        "tier": tier_tag,
                        "grade": grade,
                        "size_mult": grade_size,
                    })
                    log.info(f"[Coordinator/Nair] EXECUTE [{tier_tag}] {sym} {direction} "
                             f"conf={avg_conf:.2f} approvers={list(approvals.keys())}")
                    resolved.append(sym)
                else:
                    # Direction conflict — suppress
                    log.info(f"[Coordinator/Nair] REJECT {sym}: direction conflict {dirs}")
                    resolved.append(sym)

            elif len(rejections) >= 2:
                # 2+ rejections = dead signal
                log.info(f"[Coordinator/Nair] REJECT {sym}: {list(rejections.keys())} rejected")
                resolved.append(sym)

            # Timeout: pending > 60s without resolution → stale, drop
            elif sym in self._pending_symbols:
                vote_times = [v.get("ts", 0) for v in votes.values()]
                if vote_times and (time.time() - max(vote_times)) > 60:
                    log.debug(f"[Coordinator/Nair] TIMEOUT {sym}: stale votes")
                    resolved.append(sym)

        for sym in resolved:
            self._pending_symbols.remove(sym)
            self.state.clear_votes(sym)

    def _check_agent_health(self):
        for name, agent in self._agents.items():
            if hasattr(agent, '_thread') and agent._thread:
                if not agent._thread.is_alive() and agent._running:
                    log.error(f"[Coordinator/Nair] agent {name} DEAD - restarting")
                    try:
                        agent.start()
                    except Exception as e:
                        log.error(f"[Coordinator/Nair] restart {name} failed: {e}")

    def _trigger_post_market(self):
        log.info("[Coordinator/Nair] === POST-MARKET SEQUENCE ===")

        # Learning agent
        learning = self._agents.get("learning")
        if learning and hasattr(learning, "post_market"):
            try:
                learning.post_market()
            except Exception as e:
                log.error(f"[Coordinator/Nair] learning post_market error: {e}")

        # Innovation agent
        innovation = self._agents.get("innovation_ajitsaria")
        if innovation and hasattr(innovation, "post_market_analysis"):
            try:
                result = innovation.post_market_analysis()
                log.info(f"[Coordinator/Nair] innovation report: {result.get('status')}")
            except Exception as e:
                log.error(f"[Coordinator/Nair] innovation post_market error: {e}")

        # Compliance report
        compliance = self._agents.get("compliance_novick")
        if compliance and hasattr(compliance, "generate_daily_report"):
            try:
                report = compliance.generate_daily_report()
                log.info(f"[Coordinator/Nair] compliance: {report.get('trades_executed')} trades, "
                         f"{report.get('violations')} violations")
            except Exception as e:
                log.error(f"[Coordinator/Nair] compliance report error: {e}")

        self.emit("POST_MARKET_DONE", {"date": str(self._last_day)})

    def _emit_summary(self):
        summary = {
            "ts": datetime.now().isoformat(),
            "macro_bias": self.state.get("macro_bias", "unknown"),
            "daily_pnl": self.state.get("daily_pnl", 0),
            "trades_today": self.state.get("total_trades_today", 0),
            "open_positions": len(self.state.get("open_positions", {})),
            "halted": self.state.get("trading_halted", False),
            "pending_signals": len(self._pending_symbols),
            "system_health": self.state.get("system_health", {}).get("status", "unknown"),
        }
        self.state.set(coordinator_summary=summary)
