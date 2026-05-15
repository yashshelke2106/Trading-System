"""
FilterAgent — fake-breakout filter + order flow analysis on SIGNAL_GENERATED.

Runs FakeBreakoutFilter (hard 2x vol gate + candle quality) and
OrderFlowAnalyzer (buy/sell pressure + absorption detection).

Both must pass for 'filter' vote to approve.
Publishes SIGNAL_FILTERED on approval.
"""

import logging
from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent
from core.fake_breakout_filter import FakeBreakoutFilter
from core.order_flow import OrderFlowAnalyzer, OrderFlowType
from core.ai_filter import AIFilter
from core.trade_ranker import TradeRanker

log = logging.getLogger(__name__)

# Minimum AI win probability to pass (skip = too low edge)
MIN_AI_WIN_PROB = 0.45
# Minimum ranker composite score
MIN_RANKER_SCORE = 0.40


class FilterAgent(BaseAgent):
    name = "filter"
    interval_sec = 0

    def __init__(self, state: SharedState, bus: EventBus, scanner):
        super().__init__(state, bus)
        self.scanner = scanner
        self.fb_filter = FakeBreakoutFilter()
        self.of_analyzer = OrderFlowAnalyzer()
        self.ai_filter = AIFilter()
        self.ranker = TradeRanker()
        bus.subscribe("SIGNAL_GENERATED", self._on_signal)

    def run(self) -> None:
        pass

    def _on_signal(self, event: AgentEvent) -> None:
        sym = event.payload["symbol"]
        direction = event.payload.get("direction", "long")

        with self.state._lock:
            signal = self.state.signals.get(sym)
        if signal is None:
            return

        # Innovation agent pattern blacklist check
        blacklist = self.state.get("pattern_blacklist", [])
        if blacklist:
            sig_patterns = getattr(signal, 'patterns', []) or []
            for pat in sig_patterns:
                pat_key = f"{direction}:{pat}"
                if pat_key in blacklist:
                    log.info(f"[Filter] BLOCK {sym}: pattern {pat} blacklisted (WR<=15%)")
                    self.state.cast_vote(sym, self.name, approve=False, direction=direction)
                    return

        try:
            df = self.scanner.get_intraday_data(sym, interval=5, days_back=5)
            if df is None or len(df) < 25:
                return

            fb_result = self.fb_filter.analyze(df, signal)
            of_result = self.of_analyzer.analyze(df, direction)

            # Order flow must not be exhaustion or absorption
            of_ok = of_result.flow_type in (OrderFlowType.AGGRESSION, OrderFlowType.NEUTRAL)
            approve = fb_result.is_valid and of_ok

            # --- AI Filter (ML probability from old system) ---
            ai_prob = 0.5
            ai_size = "full"
            try:
                ai_pred = self.ai_filter.predict(signal, fb_result, of_result)
                ai_prob = ai_pred.win_probability
                ai_size = ai_pred.position_size
                if ai_size == "skip":
                    approve = False
                    log.info(f"[Filter] AI REJECT {sym}: win_prob={ai_prob:.2f} too low")
            except Exception as e:
                log.debug(f"[Filter] AI filter unavailable: {e}")

            # --- Trade Ranker (composite score from old system) ---
            ranker_score = 0.5
            try:
                trade_data = {
                    "symbol": sym,
                    "signal": signal,
                    "filter_result": fb_result,
                    "order_flow": of_result,
                    "bias_confidence": 0.5,
                    "time_mult": 1.0,
                }
                macro = self.state.get("macro_bias", "neutral")
                score_result = self.ranker.score_trade(trade_data, market_bias_value=macro)
                ranker_score = score_result.total_score
                if ranker_score < MIN_RANKER_SCORE:
                    approve = False
                    log.info(f"[Filter] RANKER REJECT {sym}: score={ranker_score:.2f} < {MIN_RANKER_SCORE}")
            except Exception as e:
                log.debug(f"[Filter] Ranker unavailable: {e}")

            # Blended confidence: FB 35% + OF 25% + AI 25% + Ranker 15%
            confidence = (fb_result.confidence * 0.35
                          + of_result.strength * 0.25
                          + ai_prob * 0.25
                          + ranker_score * 0.15)

            with self.state._lock:
                self.state.filter_results[sym] = fb_result
                self.state.order_flow[sym] = of_result

            self.state.cast_vote(
                symbol=sym, agent=self.name,
                approve=approve,
                direction=direction,
                confidence=confidence,
            )

            if approve:
                self.emit("SIGNAL_FILTERED", {
                    "symbol": sym,
                    "direction": direction,
                    "entry_price": event.payload.get("entry_price", 0),
                    "atr": event.payload.get("atr", 0),
                    "rsi": event.payload.get("rsi", 0),
                    "fb_confidence": fb_result.confidence,
                    "of_type": of_result.flow_type.value,
                    "of_strength": of_result.strength,
                    "ai_prob": round(ai_prob, 3),
                    "ai_size": ai_size,
                    "ranker_score": round(ranker_score, 3),
                    "grade": event.payload.get("grade", "A"),
                })
                log.info(f"[Filter] PASS {sym} fb={fb_result.confidence:.2f} "
                         f"of={of_result.flow_type.value}({of_result.strength:.2f}) "
                         f"ai={ai_prob:.2f} rank={ranker_score:.2f}")
            else:
                reasons = fb_result.reasons + (["of_rejected"] if not of_ok else [])
                log.debug(f"[Filter] REJECT {sym}: {reasons[:3]}")

        except Exception as e:
            log.error(f"[Filter] {sym}: {e}")
