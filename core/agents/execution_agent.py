"""
ExecutionAgent — trade entry on RISK_APPROVED consensus + position management loop.

Entry: checks consensus (3/4 votes, unanimous direction) then calls ExecutionEngine.
Loop (60s): manages open positions — SL, volume exit, trailing SL.

Also computes portfolio heat after each change and writes to SharedState.
"""

import logging
from typing import Optional

import config
from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent
from core.execution import ExecutionEngine
from core.execution_refinement import ExecutionRefiner, EntryRefinement
from core.trade_logger import TradeLogger

log = logging.getLogger(__name__)


class ExecutionAgent(BaseAgent):
    name = "execution"
    interval_sec = 60    # manage positions every 60s

    def __init__(self, state: SharedState, bus: EventBus, scanner, capital: float, feed=None):
        super().__init__(state, bus)
        self.scanner = scanner
        self.capital = capital
        self.feed = feed   # DhanMarketFeed — live price for SL checks
        self.exec_engine = ExecutionEngine(capital=capital)
        self.refiner = ExecutionRefiner()
        self.trade_logger = TradeLogger()
        # Listen to FINAL_EXECUTE from Coordinator (full Aladdin pipeline)
        # NOT RISK_APPROVED (which bypasses Quant/Compliance/Ops/Coordinator)
        bus.subscribe("FINAL_EXECUTE", self._on_final_execute)

    def run(self) -> None:
        """Called every 60s — manage all open positions."""
        self._manage_positions()
        self._sync_open_positions()
        self._update_portfolio_heat()

    # ── Trade entry (from Coordinator FINAL_EXECUTE) ────────────────────

    def _on_final_execute(self, event: AgentEvent) -> None:
        sym = event.payload["symbol"]
        direction = event.payload["direction"]
        conf = event.payload.get("confidence", 0.5)
        size_mult = event.payload.get("size_mult", 1.0)
        grade = event.payload.get("grade", "A")
        cc_variant = event.payload.get("cc_variant", "champion")
        regime = event.payload.get("regime", self.state.get("market_regime", "neutral"))

        # Prevent duplicate entry
        if sym in self.state.get("open_positions", {}):
            return

        with self.state._lock:
            signal = self.state.signals.get(sym)
        if signal is None:
            return

        try:
            df = self.scanner.get_intraday_data(sym, interval=5, days_back=5)
            if df is None or df.empty:
                return

            # Entry refinement (VWAP / pullback)
            entry_ctx = self.refiner.refine_entry(df, signal, signal.entry_price)
            if entry_ctx.refinement == EntryRefinement.AVOID:
                log.info(f"[Exec] SKIP {sym}: entry refiner says AVOID")
                self.state.clear_votes(sym)
                return

            entry_price = entry_ctx.entry_price
            entry_vol   = float(df['volume'].iloc[-1])
            entry_va    = float(df['volume'].rolling(20).mean().iloc[-1]) if len(df) >= 20 else entry_vol

            result = self.exec_engine.execute_trade(
                symbol       = sym,
                direction    = direction,
                capital      = self.capital * size_mult,
                entry_price  = entry_price,
                atr          = signal.atr,
                strike       = None,
                use_options  = False,
                entry_volume = entry_vol,
                entry_vol_avg= entry_va,
            )

            if result and result.success:
                # Fetch SL + target from ExecutionEngine ground truth
                engine_pos = next(
                    (p for p in self.exec_engine.get_open_positions() if p.symbol == sym),
                    None,
                )
                sl_price     = engine_pos.sl_price     if engine_pos else 0.0
                target_price = engine_pos.target_price if engine_pos else 0.0

                # BUG 2 FIX: Standardize keys — use entry_price, quantity everywhere
                with self.state._lock:
                    self.state.open_positions[sym] = {
                        "entry":        result.filled_price,   # legacy key (NotifierAgent)
                        "entry_price":  result.filled_price,   # standard key (OpsAgent, QuantAgent)
                        "qty":          result.filled_quantity, # legacy key (NotifierAgent)
                        "quantity":     result.filled_quantity, # standard key (QuantAgent)
                        "direction":    direction,
                        "order_id":     result.order_id,
                        "trade_id":     result.order_id,
                        "confidence":   conf,
                        "grade":        grade,
                        "sl_price":     sl_price,
                        "target_price": target_price,
                        "rsi":          getattr(signal, 'rsi', 0),
                        "patterns":     getattr(signal, 'patterns', []),
                        "volume_ratio": getattr(signal, 'volume_ratio', 0),
                        "votes":        self.state.get_votes(sym),
                        "cc_variant":   cc_variant,
                        "regime":       regime,
                    }
                    self.state.total_trades_today += 1

                self._update_portfolio_heat()
                self.trade_logger.log_trade({
                    "trade_id":          result.order_id,
                    "symbol":            sym,
                    "direction":         direction.upper(),
                    "entry_price":       result.filled_price,
                    "exit_price":        0,
                    "stop_loss":         sl_price,
                    "target":            target_price,
                    "quantity":          result.filled_quantity,
                    "pnl":               0,
                    "pnl_percent":       0,
                    "status":            "OPEN",
                    "reason":            getattr(signal, "reason", ""),
                    "order_id":          result.order_id,
                    "volatility_regime": str(getattr(signal, "volatility", "")),
                    "position_size":     "full",
                    "rank":              0,
                    "total_score":       getattr(signal, "strength", 0),
                    "session":           "",
                })
                log.info(f"[Exec] TRADE [{grade}] {sym} {direction.upper()} "
                         f"@ {result.filled_price:.2f} qty={result.filled_quantity} "
                         f"conf={conf:.0%}")
                self.emit("TRADE_ENTERED", {
                    "symbol": sym, "direction": direction,
                    "price": result.filled_price, "qty": result.filled_quantity,
                    "confidence": conf, "entry_price": result.filled_price,
                    "sl_price": sl_price, "target_price": target_price,
                    "grade": grade,
                })
            else:
                log.warning(f"[Exec] order failed for {sym}")

        except Exception as e:
            log.error(f"[Exec] {sym}: {e}", exc_info=True)

    # ── Position management ───────────────────────────────────────────────

    def _manage_positions(self) -> None:
        def get_price(s):
            # Prefer live WebSocket LTP (sub-second latency for SL accuracy)
            if self.feed:
                ltp = self.feed.get_ltp(s)
                if ltp:
                    return ltp
            df = self.scanner.get_intraday_data(s, interval=5, days_back=2)
            return float(df['close'].iloc[-1]) if df is not None and not df.empty else None

        def get_data(s):
            return self.scanner.get_intraday_data(s, interval=5, days_back=2)

        closed = self.exec_engine.manage_open_positions(get_price, get_data)

        for item in closed:
            sym   = item["symbol"]
            trade = item.get("trade")
            reason = item["reason"]
            pnl    = trade.pnl if trade else 0.0

            # Grab position metadata before removing (for swarm learning)
            pos_meta = {}
            with self.state._lock:
                pos_meta = dict(self.state.open_positions.get(sym, {}))
                self.state.open_positions.pop(sym, None)

                # BUG 3 FIX: Update daily_pnl on every trade close
                self.state.daily_pnl = getattr(self.state, 'daily_pnl', 0.0) + pnl

                if trade:
                    self.state.today_trades.append({
                        "symbol": sym,
                        "direction": trade.direction,
                        "pnl": pnl,
                        "reason": reason,
                    })
                    if pnl > 0:
                        self.state.consecutive_losses = 0
                        self.state.win_streak += 1
                    else:
                        self.state.consecutive_losses += 1
                        self.state.win_streak = 0

            # BUG 4 FIX: Enrich TRADE_CLOSED with full context for swarm learning
            entry_price = pos_meta.get("entry_price", pos_meta.get("entry", 0))
            quantity = pos_meta.get("quantity", pos_meta.get("qty", 1))
            pnl_pct = (pnl / (entry_price * quantity) * 100) if entry_price and quantity else 0
            trade_id = pos_meta.get("trade_id") or pos_meta.get("order_id") or getattr(trade, "id", "")

            if trade:
                self.trade_logger.update_trade(
                    trade_id=trade_id,
                    exit_price=getattr(trade, "exit_price", 0.0),
                    status="WIN" if pnl > 0 else "LOSS",
                    exit_reason=reason,
                    pnl=pnl,
                    pnl_percent=round(pnl_pct, 2),
                    holding_period=getattr(trade, "holding_days", 0),
                    drawdown=0.0,
                    max_favorable=0.0,
                )

            log.info(f"[Exec] CLOSED {sym} reason={reason} pnl={pnl:+.0f} daily={self.state.daily_pnl:+.0f}")
            self.emit("TRADE_CLOSED", {
                "symbol": sym,
                "pnl": pnl,
                "pnl_pct": round(pnl_pct, 2),
                "reason": reason,
                "outcome": "TARGET_HIT" if pnl > 0 else ("EXPIRED" if reason == "EXPIRED" else "SL_HIT"),
                "direction": pos_meta.get("direction", ""),
                "entry_price": entry_price,
                "sl_price": pos_meta.get("sl_price", 0),
                "target_price": pos_meta.get("target_price", 0),
                "patterns": pos_meta.get("patterns", []),
                "rsi": pos_meta.get("rsi", 0),
                "volume_ratio": pos_meta.get("volume_ratio", 0),
                "market_bias": pos_meta.get("regime") or self.state.get("market_regime", "neutral"),
                "grade": pos_meta.get("grade", ""),
                "confidence": pos_meta.get("confidence", 0.5),
                "cc_variant": pos_meta.get("cc_variant", "champion"),
            })

    def _sync_open_positions(self) -> None:
        """Sync SharedState.open_positions from ExecutionEngine ground truth."""
        live = {p.symbol: p for p in self.exec_engine.get_open_positions()}
        with self.state._lock:
            synced = {}
            for sym, p in live.items():
                existing = self.state.open_positions.get(sym, {})
                synced[sym] = {
                    **existing,
                    "entry":        p.entry_price,
                    "entry_price":  p.entry_price,
                    "qty":          p.quantity,
                    "quantity":     p.quantity,
                    "direction":    p.direction,
                    "trade_id":     existing.get("trade_id") or existing.get("order_id", ""),
                    "order_id":     existing.get("order_id", ""),
                    "sl_price":     p.sl_price,
                    "target_price": p.target_price,
                }
            self.state.open_positions = synced

    def _update_portfolio_heat(self) -> None:
        """Compute total capital at risk across all open positions."""
        positions = self.exec_engine.get_open_positions()
        heat = 0.0
        for pos in positions:
            at_risk = abs(pos.entry_price - pos.sl_price) * pos.quantity
            heat += at_risk / max(self.capital, 1)
        self.state.set(portfolio_heat=heat)
