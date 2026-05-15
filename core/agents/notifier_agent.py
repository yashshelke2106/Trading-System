"""
NotifierAgent — trade/regime alerts to Telegram + notifications.json.

Event-driven:
  TRADE_ENTERED   → immediate entry alert
  TRADE_CLOSED    → P&L result (✅ profit / ❌ loss)
  REGIME_CHANGED  → regime shift

Periodic (every 5 min via interval_sec=300):
  Open position heartbeat → current price, unrealized P&L, % vs entry
  Threshold alerts (once per trade per threshold):
    +1% / +2% / +3% profit milestone
    -5% / -8% warning (SL at -10%)
    Price within 2% of SL → "SL NEAR" warning
  Trading halt transition
"""

import logging
from typing import Dict, Set
from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent
from core.notifier import TelegramNotifier

log = logging.getLogger(__name__)

REGIME_ICON = {"bull": "🐂", "bear": "🐻", "volatile": "⚡", "neutral": "⚪"}

PROFIT_MILESTONES = [1.0, 2.0, 3.0]   # % profit to alert once each
LOSS_WARNINGS     = [-5.0, -8.0]       # % loss to alert once each
SL_NEAR_PCT       = 2.0                # alert when this % away from SL


class NotifierAgent(BaseAgent):
    name = "notifier"
    interval_sec = 300   # 5-min periodic heartbeat for active trades

    def __init__(self, state: SharedState, bus: EventBus,
                 notifier: TelegramNotifier, feed=None):
        super().__init__(state, bus)
        self.notifier = notifier
        self.feed = feed

        # Per-symbol alert tracking — reset when position closes
        self._milestones: Dict[str, Set[float]] = {}   # sym → set of alerted % levels
        self._sl_warned:  Dict[str, bool]       = {}   # sym → True if SL-near sent
        self._last_halt = False

        bus.subscribe("TRADE_ENTERED",  self._on_entered)
        bus.subscribe("TRADE_CLOSED",   self._on_closed)
        bus.subscribe("REGIME_CHANGED", self._on_regime)
        bus.subscribe("SWARM_EVOLVED",  self._on_swarm_evolved)

    # ── Periodic run — active trade heartbeat ─────────────────────────────────

    def run(self) -> None:
        self._active_trade_update()
        self._check_halt()

    def _active_trade_update(self) -> None:
        positions = self.state.get("open_positions", {})
        if not positions:
            return

        lines = []
        for sym, pos in positions.items():
            entry     = pos.get("entry", 0.0)
            qty       = pos.get("qty", 0)
            direction = pos.get("direction", "long")
            sl_price  = pos.get("sl_price", 0.0)
            target    = pos.get("target_price", 0.0)

            # Get live price — WebSocket preferred, else skip if unavailable
            current = self.feed.get_ltp(sym) if self.feed else None
            if not current or not entry:
                continue

            # Unrealized P&L
            if direction == "long":
                upnl     = (current - entry) * qty
                pct      = (current - entry) / entry * 100
                sl_dist  = (current - sl_price) / entry * 100 if sl_price else None
            else:
                upnl     = (entry - current) * qty
                pct      = (entry - current) / entry * 100
                sl_dist  = (sl_price - current) / entry * 100 if sl_price else None

            sign = "+" if upnl >= 0 else ""
            bar  = "📈" if pct >= 0 else "📉"
            lines.append(
                f"{bar} <b>{sym}</b> {direction.upper()}  "
                f"₹{current:,.2f}  P&L: ₹{sign}{upnl:,.0f} ({pct:+.1f}%)"
                + (f"  SL dist: {sl_dist:.1f}%" if sl_dist is not None else "")
            )

            # ── Threshold alerts (once per level per trade) ──────────────
            alerted = self._milestones.setdefault(sym, set())

            for milestone in PROFIT_MILESTONES:
                if pct >= milestone and milestone not in alerted:
                    alerted.add(milestone)
                    self.notifier.notify(
                        "PROFIT_MILESTONE",
                        f"🎯 <b>{sym}</b> hit +{milestone:.0f}%!\n"
                        f"Entry ₹{entry:,.2f} → Now ₹{current:,.2f}\n"
                        f"Unrealized P&L: ₹{sign}{upnl:,.0f}",
                        symbol=sym, pnl=upnl,
                    )

            for warn_pct in LOSS_WARNINGS:
                if pct <= warn_pct and warn_pct not in alerted:
                    alerted.add(warn_pct)
                    self.notifier.notify(
                        "LOSS_WARNING",
                        f"⚠️ <b>{sym}</b> at {pct:.1f}%\n"
                        f"Entry ₹{entry:,.2f} → Now ₹{current:,.2f}\n"
                        f"Unrealized P&L: ₹{upnl:,.0f}",
                        symbol=sym, pnl=upnl,
                    )

            if (sl_dist is not None
                    and 0 < sl_dist <= SL_NEAR_PCT
                    and not self._sl_warned.get(sym)):
                self._sl_warned[sym] = True
                self.notifier.notify(
                    "SL_NEAR",
                    f"🚨 <b>SL NEAR</b> — {sym}\n"
                    f"Price ₹{current:,.2f}  SL ₹{sl_price:,.2f}  "
                    f"({sl_dist:.1f}% away)",
                    symbol=sym, pnl=upnl,
                )

        if lines:
            summary = "📊 <b>Active Positions</b>\n" + "\n".join(lines)
            log.info(f"[Notifier] position heartbeat: {len(lines)} open")
            self.notifier.notify("POSITION_UPDATE", summary)

    def _check_halt(self) -> None:
        halted = self.state.get("trading_halted", False)
        if halted and not self._last_halt:
            reason = self.state.get("halt_reason", "")
            self.notifier.notify(
                "HALT",
                f"🚫 <b>TRADING HALTED</b>\nReason: {reason}",
            )
        self._last_halt = halted

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_entered(self, event: AgentEvent) -> None:
        sym       = event.payload.get("symbol", "")
        direction = event.payload.get("direction", "").upper()
        price     = event.payload.get("price", 0.0)
        qty       = event.payload.get("qty", 0)
        conf      = event.payload.get("confidence", 0.0)

        # Seed threshold tracker for this trade
        self._milestones[sym] = set()
        self._sl_warned[sym]  = False

        # Fetch SL + target from state (written by ExecutionAgent)
        pos = self.state.get("open_positions", {}).get(sym, {})
        sl_price = pos.get("sl_price", 0.0)
        target   = pos.get("target_price", 0.0)

        icon = "🟢" if direction == "LONG" else "🔴"
        sl_line = f"\nSL      : ₹{sl_price:,.2f}" if sl_price else ""
        tgt_line = f"\nTarget  : ₹{target:,.2f}"  if target   else ""

        msg = (
            f"{icon} <b>TRADE ENTERED</b>\n"
            f"Symbol   : {sym}\n"
            f"Direction: {direction}\n"
            f"Price    : ₹{price:,.2f}   Qty: {qty}\n"
            f"Confidence: {conf:.0%}"
            f"{sl_line}{tgt_line}"
        )
        log.info(f"[Notifier] ENTERED {sym} {direction} @ {price:.2f}")
        self.notifier.notify("TRADE_ENTERED", msg, symbol=sym)

    def _on_closed(self, event: AgentEvent) -> None:
        sym    = event.payload.get("symbol", "")
        pnl    = event.payload.get("pnl", 0.0)
        reason = event.payload.get("reason", "")
        icon   = "✅" if pnl >= 0 else "❌"
        sign   = "+" if pnl >= 0 else ""

        msg = (
            f"{icon} <b>TRADE CLOSED</b>\n"
            f"Symbol: {sym}\n"
            f"P&L   : ₹{sign}{pnl:,.0f}\n"
            f"Reason: {reason}"
        )
        log.info(f"[Notifier] CLOSED {sym} pnl={pnl:+.0f} reason={reason}")
        self.notifier.notify("TRADE_CLOSED", msg, symbol=sym, pnl=pnl)

        # Clear threshold tracking for closed trade
        self._milestones.pop(sym, None)
        self._sl_warned.pop(sym, None)

    def _on_regime(self, event: AgentEvent) -> None:
        regime    = event.payload.get("regime", "")
        nifty_pct = event.payload.get("nifty_pct", 0.0)
        vix       = event.payload.get("vix", 0.0)
        icon      = REGIME_ICON.get(regime, "⚠️")
        msg = (
            f"{icon} <b>REGIME CHANGE</b>: {regime.upper()}\n"
            f"NIFTY: {nifty_pct:+.2f}%   VIX~{vix:.1f}"
        )
        log.info(f"[Notifier] REGIME -> {regime}")
        self.notifier.notify("REGIME_CHANGED", msg)

    def _on_swarm_evolved(self, event: AgentEvent) -> None:
        stage = event.payload.get("new_stage", "")
        trade_num = event.payload.get("trade_num", 0)
        gen = event.payload.get("generation", 0)
        stage_icons = {
            "LEARNING": "📘", "COMPETENT": "📗",
            "ADVANCED": "📙", "ELITE": "📕"
        }
        icon = stage_icons.get(stage, "📈")
        msg = (
            f"{icon} <b>SWARM EVOLVED</b>\n"
            f"Stage: {stage}\n"
            f"Trade #: {trade_num}\n"
            f"Generation: {gen}"
        )
        log.info(f"[Notifier] SWARM EVOLVED -> {stage}")
        self.notifier.notify("SWARM_EVOLVED", msg)
