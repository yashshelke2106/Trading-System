"""
LearningAgent — central feedback hub. Records trades to all learning subsystems.

On TRADE_CLOSED (SL_HIT / TARGET_HIT / VOLUME_EXIT):
  1. AutoTuner — adaptive params
  2. SymbolMemory — per-stock learning
  3. RegimeParams — per-regime stats
  4. ChampionChallenger — A/B test outcome tagging
  5. AutoRollback — degradation detection
  6. CalibrationTracker — predicted vs actual WR
  7. Strategy validation — early warning

post_market() — at 15:30: full tune + analyzer + cross-system reports.
"""

import logging
from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent

log = logging.getLogger(__name__)

RECENT_WINDOW = 5   # trades to check for intra-day early warning


class LearningAgent(BaseAgent):
    name = "learning"
    interval_sec = 0   # event-driven

    def __init__(self, state: SharedState, bus: EventBus, tuner, analyzer=None):
        super().__init__(state, bus)
        self.tuner    = tuner
        self.analyzer = analyzer   # PostMarketAnalyzer — injected by orchestrator
        bus.subscribe("TRADE_CLOSED", self._on_trade_closed)

    def run(self) -> None:
        pass

    # ── On every SL/target/volume-exit ───────────────────────────────────

    def _on_trade_closed(self, event: AgentEvent) -> None:
        sym       = event.payload["symbol"]
        pnl       = event.payload.get("pnl", 0.0)
        reason    = event.payload.get("reason", "")
        outcome   = event.payload.get("outcome", "TARGET_HIT" if pnl > 0 else "SL_HIT")
        rsi       = float(event.payload.get("rsi", 50))
        patterns  = event.payload.get("patterns", []) or []
        entry_p   = float(event.payload.get("entry_price", 0))
        sl_p      = float(event.payload.get("sl_price", 0))
        sl_pct    = abs(entry_p - sl_p) / entry_p * 100 if entry_p > 0 else 1.0
        regime    = event.payload.get("market_bias") or self.state.get("market_regime", "neutral")
        confidence = float(event.payload.get("confidence", 0.5))
        variant   = event.payload.get("cc_variant", "champion")

        try:
            # Hour for time-of-day learning
            from datetime import datetime
            hour = datetime.now().hour

            # 1. AutoTuner (existing)
            self.tuner.record_trade(
                symbol=sym, pnl=pnl, pnl_pct=0.0,
                exit_reason=reason, signal_strength=0.0,
            )

            # 2. SymbolMemory — per-stock learning
            try:
                from core.symbol_memory import get_symbol_memory
                get_symbol_memory().record_trade(
                    sym=sym, outcome=outcome, rsi=rsi, hour=hour,
                    sl_pct=sl_pct, patterns=patterns, pnl=pnl,
                )
            except Exception as e:
                log.debug(f"[Learning] SymMem err: {e}")

            # 2b. MomentumProfiler — per-stock indicator fingerprinting
            try:
                from core.momentum_profiler import record_from_journal_entry
                record_from_journal_entry(data)
            except Exception as e:
                log.debug(f"[Learning] MomProf err: {e}")

            # 3. RegimeParams — per-regime stats
            try:
                from core.regime_params import get_regime_params
                get_regime_params().record_trade(regime=regime, outcome=outcome, pnl=pnl, sym=sym)
            except Exception as e:
                log.debug(f"[Learning] Regime err: {e}")

            # 4. ChampionChallenger — A/B outcome tagging
            try:
                from core.champion_challenger import get_cc
                get_cc().record_outcome(variant_name=variant, outcome=outcome, pnl=pnl, sym=sym)
            except Exception as e:
                log.debug(f"[Learning] CC err: {e}")

            # 5. AutoRollback — degradation watch
            try:
                from core.auto_rollback import get_rollback
                get_rollback().record_trade(outcome=outcome, pnl=pnl)
            except Exception as e:
                log.debug(f"[Learning] Rollback err: {e}")

            # 6. CalibrationTracker — predicted vs actual
            try:
                from core.calibration import get_calibration
                get_calibration().record(predicted_prob=confidence, outcome=outcome, sym=sym)
            except Exception as e:
                log.debug(f"[Learning] Calib err: {e}")

            log.info(f"[Learning] trade recorded: {sym} pnl={pnl:+.0f} "
                     f"reason={reason} regime={regime} variant={variant}")

            # Immediate strategy validation after every close
            self._validate_strategy()

        except Exception as e:
            log.error(f"[Learning] record error: {e}")

    def _validate_strategy(self) -> None:
        """
        Rolling-window check after every trade close.
        AutoTuner.tune() has MIN_TRADES=5 guard — no-op if insufficient history.
        If recent 5 trades all losses → update SharedState consecutive_losses.
        """
        try:
            changes = self.tuner.tune()
            if changes:
                log.warning(f"[Learning] INTRA-DAY param adjustment: {changes}")

            # Early-warning: last RECENT_WINDOW trades
            recent = self.tuner.trade_history[-RECENT_WINDOW:]
            if len(recent) >= RECENT_WINDOW:
                wins = sum(1 for t in recent if t["won"])
                wr   = wins / len(recent)
                losses_run = len([t for t in reversed(recent) if not t["won"]])
                # Count from end until we hit a win
                run = 0
                for t in reversed(recent):
                    if not t["won"]:
                        run += 1
                    else:
                        break

                log.info(f"[Learning] strategy check: last {RECENT_WINDOW} WR={wr:.0%} "
                         f"loss_run={run}")

                # Sync loss streak to SharedState so RiskAgent can halt if needed
                if run > 0:
                    self.state.set(consecutive_losses=run)

        except Exception as e:
            log.error(f"[Learning] validate error: {e}")

    # ── Post-market (auto-triggered at 15:30 by orchestrator) ─────────────

    def post_market(self) -> None:
        """
        Full end-of-day sequence (no manual command needed):
          1. Final rolling-window tune
          2. PostMarketAnalyzer: summary + full 365-day backtest + save daily report
          3. Champion/Challenger evaluation + new challenger deployment
          4. Calibration drift report
          5. Per-regime + per-symbol stats logged
        """
        log.info("[Learning] === POST-MARKET SEQUENCE START ===")

        try:
            changes = self.tuner.tune()
            log.info(f"[Learning] final tune: {changes if changes else 'no changes'}")
        except Exception as e:
            log.error(f"[Learning] final tune error: {e}")

        if self.analyzer:
            try:
                self.analyzer.run()
            except Exception as e:
                log.error(f"[Learning] post-market analysis error: {e}")

        # Deploy new challenger if no current challenger and we have data
        try:
            from core.champion_challenger import get_cc
            cc = get_cc()
            stats = cc.stats()
            if stats["challenger"] is None and stats["champion"]["n"] >= 30:
                # Generate new challenger params: small variation on champion
                self._deploy_new_challenger(cc, stats["champion"]["params"])
            log.info(f"[Learning] CC stats: {stats}")
        except Exception as e:
            log.error(f"[Learning] CC error: {e}")

        # Calibration drift check
        try:
            from core.calibration import get_calibration
            calib = get_calibration()
            bs = calib.brier_score()
            if bs is not None:
                log.info(f"[Learning] Brier score={bs:.3f} (lower=better; >0.30 = drifting)")
                if calib.is_drifting():
                    log.warning("[Learning] CALIBRATION DRIFTING — predictions unreliable")
        except Exception as e:
            log.debug(f"[Learning] calib err: {e}")

        # Regime stats
        try:
            from core.regime_params import get_regime_params
            stats = get_regime_params().stats()
            for regime, s in stats.items():
                if s["n"] > 0:
                    log.info(f"[Learning] regime[{regime}] n={s['n']} wr={s['wr']:.1%} pnl={s['total_pnl']:+.0f}")
        except Exception as e:
            log.debug(f"[Learning] regime err: {e}")

        log.info("[Learning] === POST-MARKET SEQUENCE DONE ===")

    def _deploy_new_challenger(self, cc, champion_params: dict) -> None:
        """Generate slight variation on champion to test."""
        import random as _r
        # Try 3 variations: looser votes, tighter strength, lower RSI floor
        variations = [
            {**champion_params, "min_votes": max(3, champion_params.get("min_votes", 4) - 1)},
            {**champion_params, "min_strength": min(70, champion_params.get("min_strength", 50) + 5)},
            {**champion_params, "rsi_long_momentum_min": max(50, champion_params.get("rsi_long_momentum_min", 55) - 3)},
        ]
        new_challenger = _r.choice(variations)
        cc.deploy_challenger(new_challenger)
        log.info(f"[Learning] new challenger deployed: {new_challenger}")
