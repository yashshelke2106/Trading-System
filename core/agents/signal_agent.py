"""
SignalAgent — generates technical signals on SURGE_DETECTED.

On SURGE_DETECTED:
  1. Checks market regime (skip volatile)
  2. Runs SignalEngine.generate_signal()
  3. Casts 'signal' vote with direction + strength
  4. Publishes SIGNAL_GENERATED
"""

import logging
import threading
from datetime import datetime
from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent
from core.signal_engine import SignalEngine
from core.signal_writer import write_signals

log = logging.getLogger(__name__)

# Lock for thread-safe signal accumulation across concurrent SURGE_DETECTED events
_signals_lock = threading.Lock()
_pending_signals: list = []


class SignalAgent(BaseAgent):
    name = "signal"
    interval_sec = 0   # event-driven

    def __init__(self, state: SharedState, bus: EventBus, scanner):
        super().__init__(state, bus)
        self.scanner = scanner
        self.engine = SignalEngine()
        bus.subscribe("SURGE_DETECTED", self._on_surge)
        bus.subscribe("TRADE_CLOSED", self._on_trade_closed)

        # Signal memory: feedback loop from trade outcomes
        from core.signal_memory import get_signal_memory
        self.memory = get_signal_memory()

    def run(self) -> None:
        stats = self.memory.get_stats()
        if stats["patterns_tracked"] > 0:
            log.info(f"[Signal] memory loaded: {stats['patterns_tracked']} patterns, "
                     f"{stats['combos_tracked']} combos tracked")
            if stats.get("best_pattern"):
                log.info(f"[Signal] best pattern: {stats['best_pattern']}")
            if stats.get("worst_pattern"):
                log.info(f"[Signal] worst pattern: {stats['worst_pattern']}")

    def _on_trade_closed(self, event: AgentEvent) -> None:
        """Feedback loop: record trade outcome in signal memory."""
        try:
            p = event.payload
            symbol = p.get("symbol", "")
            direction = p.get("direction", "long")
            patterns = p.get("patterns", [])
            entry_hour = p.get("entry_hour", 10)
            pnl_pct = float(p.get("pnl_pct", 0))
            entry_price = float(p.get("entry_price", 0))
            sl_price = float(p.get("sl_price", 0))
            exit_price = float(p.get("exit_price", 0))

            won = pnl_pct > 0
            # R-multiple: profit / risk
            risk = abs(entry_price - sl_price) if sl_price and entry_price else entry_price * 0.01
            if risk > 0:
                if direction == "long":
                    r_mult = (exit_price - entry_price) / risk
                else:
                    r_mult = (entry_price - exit_price) / risk
            else:
                r_mult = 0.0

            self.memory.record_outcome(
                symbol=symbol, direction=direction, patterns=patterns,
                entry_hour=entry_hour, won=won, r_multiple=round(r_mult, 2),
                pnl_pct=round(pnl_pct, 4),
            )
        except Exception as e:
            log.debug(f"[Signal] feedback record error: {e}")

    def _on_surge(self, event: AgentEvent) -> None:
        sym = event.payload["symbol"]
        vol_ratio = event.payload.get("vol_ratio", 0.0)

        # Regime gate
        regime = self.state.get("market_regime", "neutral")
        if regime == "volatile":
            # Volatile: still allow signals but use volatile-specific params
            pass  # handled by _apply_learned_params(regime="volatile")

        # Per-symbol blacklist check (top movers bypass — they often are recent losers turning around)
        try:
            from core.symbol_memory import get_symbol_memory
            sm = get_symbol_memory()
            if sm.is_blacklisted(sym):
                # Check if top mover today — if yes, override blacklist
                try:
                    from core.top_mover_mode import detect_top_mover, should_bypass_symbol_blacklist
                    df_check = self.scanner.get_market_data(sym, 30)
                    if df_check is not None and not df_check.empty:
                        is_m, cls, _, _ = detect_top_mover(df_check)
                        if not (is_m and should_bypass_symbol_blacklist(cls)):
                            log.info(f"[Signal] SKIP {sym}: symbol blacklisted (WR<20%)")
                            return
                        else:
                            log.info(f"[Signal] OVERRIDE {sym}: top mover bypasses blacklist")
                    else:
                        log.info(f"[Signal] SKIP {sym}: symbol blacklisted (WR<20%)")
                        return
                except Exception:
                    log.info(f"[Signal] SKIP {sym}: symbol blacklisted (WR<20%)")
                    return
        except Exception:
            pass

        # Champion/Challenger: pick variant for this signal
        cc_variant = "champion"
        try:
            from core.champion_challenger import get_cc
            cc_variant = get_cc().pick_variant().name
        except Exception:
            pass

        # Re-apply layered params with current regime + variant
        try:
            self.engine.config = dict(__import__("config").SIGNAL_CONFIG)
            self.engine._apply_learned_params(regime=regime, variant=cc_variant)
        except Exception:
            pass

        try:
            df = self.scanner.get_intraday_data(sym, interval=5, days_back=5)
            if df is None or len(df) < 25:
                return

            signal = self.engine.generate_signal(sym, df)
            if signal is None:
                return

            # Per-symbol RSI band check (if symbol has enough trade history)
            try:
                rsi_band = sm.get_optimal_rsi_band(sym)
                if rsi_band:
                    low, high = rsi_band
                    if not (low <= signal.rsi <= high + 5):  # 5pt grace
                        log.info(f"[Signal] SKIP {sym}: RSI={signal.rsi:.0f} outside winning band [{low:.0f}-{high:.0f}]")
                        return
            except Exception:
                pass

            # ── Multi-timeframe confluence (user workflow: 1H bias → 15M entry) ──
            # 1H: direction bias via EMA8/21 + WAE + S/D zones + range filter
            # 15M: entry confirmation via same indicators + PVSRA volume
            # 5M signal must agree with BOTH timeframes to get full bonus.
            # Opposing 1H = hard -15 penalty (likely counter-trend trade).
            # Opposing 15M = soft -8 penalty (entry timing off).
            htf_penalty = 0
            htf_bonus   = 0
            htf_pats: list = []

            # ── Fetch both timeframes once ────────────────────────────────
            df_15m = None
            df_1h  = None
            try:
                df_15m = self.scanner.get_intraday_data(sym, interval=15, days_back=5)
            except Exception:
                pass
            try:
                df_1h = self.scanner.get_intraday_data(sym, interval=60, days_back=10)
            except Exception:
                pass

            # ── 1H BIAS: EMA8/21 + WAE + S/D + Range Filter ─────────────
            if df_1h is not None and len(df_1h) >= 30:
                try:
                    ema8_1h  = df_1h['close'].ewm(span=8,  adjust=False).mean()
                    ema21_1h = df_1h['close'].ewm(span=21, adjust=False).mean()

                    h1_ema_bull = ema8_1h.iloc[-1] > ema21_1h.iloc[-1]
                    h1_ema_bear = ema8_1h.iloc[-1] < ema21_1h.iloc[-1]

                    # Fresh 1H EMA cross (most powerful 1H signal)
                    h1_cross_bull = (ema8_1h.iloc[-2] <= ema21_1h.iloc[-2] and
                                     ema8_1h.iloc[-1]  > ema21_1h.iloc[-1])
                    h1_cross_bear = (ema8_1h.iloc[-2] >= ema21_1h.iloc[-2] and
                                     ema8_1h.iloc[-1]  < ema21_1h.iloc[-1])

                    # Run full indicator stack on 1H
                    lv_sd,  sv_sd,  _ = self.engine.detect_supply_demand_zones(df_1h)
                    lv_wae, sv_wae, _ = self.engine.detect_wae(df_1h)
                    lv_rf,  sv_rf,  _ = self.engine.detect_range_filter(df_1h)
                    lv_pv,  sv_pv,  _ = self.engine.detect_pvsra(df_1h)

                    # HH/HL or LL/LH structure (keep from original)
                    h1, h2 = float(df_1h['high'].iloc[-1]), float(df_1h['high'].iloc[-2])
                    l1, l2 = float(df_1h['low'].iloc[-1]),  float(df_1h['low'].iloc[-2])
                    h1_hh_hl = h1 > h2 and l1 > l2   # higher high + higher low
                    h1_ll_lh = l1 < l2 and h1 < h2   # lower low  + lower high

                    h1_long_score  = (int(h1_ema_bull) + int(h1_cross_bull) * 2
                                      + lv_sd + lv_wae + lv_rf + lv_pv
                                      + int(h1_hh_hl))
                    h1_short_score = (int(h1_ema_bear) + int(h1_cross_bear) * 2
                                      + sv_sd + sv_wae + sv_rf + sv_pv
                                      + int(h1_ll_lh))

                    # Volume confirmation of the 1H EMA-cross trend.
                    # EMA cross works most of the time; its rare failures show
                    # up FIRST as fading volume while price still pushes. Treat
                    # a fresh cross on shrinking volume as suspect, not strong.
                    vc_ok, vc_fade, vc_pats = self.engine.detect_ema_volume_confirmation(
                        df_1h, signal.direction)

                    if signal.direction == "long":
                        if h1_long_score >= h1_short_score:
                            b = min(h1_long_score * 2, 15)
                            htf_bonus += b
                            htf_pats.append('1h_bias_bull')
                            if h1_cross_bull:
                                if vc_fade:
                                    # Cross firing but volume drying up → likely
                                    # to fail. Skip the fresh-cross bonus and
                                    # flag the reversal risk instead.
                                    htf_penalty += 6
                                    htf_pats.append('1h_cross_vol_fading')
                                    log.info(f"[Signal] CAUTION {sym}: 1H bull cross on FADING volume "
                                             f"— pattern-change risk, no fresh-cross bonus")
                                else:
                                    htf_bonus += 5
                                    htf_pats.append('1h_ema_fresh_cross_bull')
                                    if vc_ok:
                                        htf_bonus += 4
                                        htf_pats.append('1h_cross_vol_confirmed')
                            log.debug(f"[Signal] BONUS {sym}: 1H bull bias score={h1_long_score} +{b}")
                        else:
                            htf_penalty += 15
                            htf_pats.append('1h_bias_bear_conflict')
                            log.info(f"[Signal] PENALTY {sym}: 1H bearish (score {h1_short_score}>{h1_long_score}) vs long signal")
                    else:  # short
                        if h1_short_score >= h1_long_score:
                            b = min(h1_short_score * 2, 15)
                            htf_bonus += b
                            htf_pats.append('1h_bias_bear')
                            if h1_cross_bear:
                                if vc_fade:
                                    htf_penalty += 6
                                    htf_pats.append('1h_cross_vol_fading')
                                    log.info(f"[Signal] CAUTION {sym}: 1H bear cross on FADING volume "
                                             f"— pattern-change risk, no fresh-cross bonus")
                                else:
                                    htf_bonus += 5
                                    htf_pats.append('1h_ema_fresh_cross_bear')
                                    if vc_ok:
                                        htf_bonus += 4
                                        htf_pats.append('1h_cross_vol_confirmed')
                            log.debug(f"[Signal] BONUS {sym}: 1H bear bias score={h1_short_score} +{b}")
                        else:
                            htf_penalty += 15
                            htf_pats.append('1h_bias_bull_conflict')
                            log.info(f"[Signal] PENALTY {sym}: 1H bullish (score {h1_long_score}>{h1_short_score}) vs short signal")
                except Exception as e:
                    log.debug(f"[Signal] 1H analysis error {sym}: {e}")

            # ── 15M ENTRY: EMA8/21 + PVSRA + S/D + WAE ──────────────────
            if df_15m is not None and len(df_15m) >= 25:
                try:
                    ema8_15  = df_15m['close'].ewm(span=8,  adjust=False).mean()
                    ema21_15 = df_15m['close'].ewm(span=21, adjust=False).mean()

                    m15_ema_bull = ema8_15.iloc[-1] > ema21_15.iloc[-1]
                    m15_ema_bear = ema8_15.iloc[-1] < ema21_15.iloc[-1]

                    m15_cross_bull = (ema8_15.iloc[-2] <= ema21_15.iloc[-2] and
                                      ema8_15.iloc[-1]  > ema21_15.iloc[-1])
                    m15_cross_bear = (ema8_15.iloc[-2] >= ema21_15.iloc[-2] and
                                      ema8_15.iloc[-1]  < ema21_15.iloc[-1])

                    # Full indicator stack on 15M
                    lv_pv,  sv_pv,  _ = self.engine.detect_pvsra(df_15m)
                    lv_sd,  sv_sd,  _ = self.engine.detect_supply_demand_zones(df_15m)
                    lv_wae, sv_wae, _ = self.engine.detect_wae(df_15m)

                    # Volume surge on 15M (original check kept)
                    if len(df_15m) >= 20:
                        avg_v15 = float(df_15m['volume'].rolling(20).mean().iloc[-1])
                        cur_v15 = float(df_15m['volume'].iloc[-1])
                        vol_surge_15 = avg_v15 > 0 and cur_v15 > avg_v15 * 1.5
                    else:
                        vol_surge_15 = False

                    m15_long_score  = (int(m15_ema_bull) + int(m15_cross_bull) * 2
                                       + lv_pv + lv_sd + lv_wae + int(vol_surge_15))
                    m15_short_score = (int(m15_ema_bear) + int(m15_cross_bear) * 2
                                       + sv_pv + sv_sd + sv_wae + int(vol_surge_15))

                    if signal.direction == "long":
                        if m15_long_score > 0:
                            b = min(m15_long_score * 2, 10)
                            htf_bonus += b
                            htf_pats.append('15m_entry_bull')
                            if m15_cross_bull:
                                htf_bonus += 3
                                htf_pats.append('15m_ema_cross_bull')
                            if vol_surge_15:
                                htf_pats.append('15m_vol_surge')
                            log.debug(f"[Signal] BONUS {sym}: 15M bull entry score={m15_long_score} +{b}")
                        elif m15_short_score > m15_long_score:
                            htf_penalty += 8
                            htf_pats.append('15m_entry_bear_conflict')
                            log.debug(f"[Signal] PENALTY {sym}: 15M bearish vs long entry")
                    else:  # short
                        if m15_short_score > 0:
                            b = min(m15_short_score * 2, 10)
                            htf_bonus += b
                            htf_pats.append('15m_entry_bear')
                            if m15_cross_bear:
                                htf_bonus += 3
                                htf_pats.append('15m_ema_cross_bear')
                            if vol_surge_15:
                                htf_pats.append('15m_vol_surge')
                            log.debug(f"[Signal] BONUS {sym}: 15M bear entry score={m15_short_score} +{b}")
                        elif m15_long_score > m15_short_score:
                            htf_penalty += 8
                            htf_pats.append('15m_entry_bull_conflict')
                            log.debug(f"[Signal] PENALTY {sym}: 15M bullish vs short entry")
                except Exception as e:
                    log.debug(f"[Signal] 15M analysis error {sym}: {e}")

            # ── Broken zone retest check (overrides conflict penalty) ────
            # Scenario A: 1H breaks BELOW demand → old support = resistance
            #   5M bounces UP toward zone (ltf_bouncing_up) → 5M generates "long" signal
            #   TRUE trade = SHORT (selling the retest of resistance)
            #   → when signal="long" + broken_demand_retest_short: SUPPRESS the long
            #   → when signal="short" (rare: 5M already rejected): BONUS
            #
            # Scenario B: 1H breaks ABOVE supply → old resistance = support
            #   5M pulls back DOWN toward zone (ltf_pulling_back) → 5M generates "short"
            #   TRUE trade = LONG (buying the retest of support)
            #   → when signal="short" + broken_supply_retest_long: SUPPRESS the short
            #   → when signal="long" (rare: 5M already bounced): BONUS
            try:
                df_5m = self.scanner.get_intraday_data(sym, interval=5, days_back=5)
                if df_5m is None:
                    df_5m = df  # fallback to signal df
                bz_lv, bz_sv, bz_pats = self.engine.detect_broken_zone_retest(df_1h, df_5m)
                if bz_pats:
                    if 'broken_demand_retest_short' in bz_pats:
                        if signal.direction == "long":
                            # 5M is bouncing UP into broken demand (resistance).
                            # This looks bullish on 5M but it's a HTF SHORT setup.
                            # Suppress long — wait for 5M rejection to fire the real SHORT.
                            log.info(
                                f"[Signal] {sym}: SUPPRESS long — broken demand zone retest "
                                f"(HTF bearish, 5M bounce = resistance test). "
                                f"Await SHORT entry on 5M rejection."
                            )
                            return  # ← discard this long signal entirely
                        else:  # signal.direction == "short" (5M already rejecting)
                            old_penalty = htf_penalty
                            htf_penalty = max(htf_penalty - 15, 0)
                            htf_bonus += bz_sv * 3
                            htf_pats.extend(bz_pats)
                            log.info(
                                f"[Signal] {sym}: BROKEN DEMAND RETEST SHORT confirmed "
                                f"penalty {old_penalty}→{htf_penalty} +{bz_sv * 3} bonus"
                            )

                    elif 'broken_supply_retest_long' in bz_pats:
                        if signal.direction == "short":
                            # 5M is pulling back DOWN into broken supply (support).
                            # This looks bearish on 5M but it's a HTF LONG setup.
                            # Suppress short — wait for 5M bounce to fire the real LONG.
                            log.info(
                                f"[Signal] {sym}: SUPPRESS short — broken supply zone retest "
                                f"(HTF bullish, 5M pullback = support test). "
                                f"Await LONG entry on 5M bounce."
                            )
                            return  # ← discard this short signal entirely
                        else:  # signal.direction == "long" (5M already bouncing off zone)
                            old_penalty = htf_penalty
                            htf_penalty = max(htf_penalty - 15, 0)
                            htf_bonus += bz_lv * 3
                            htf_pats.extend(bz_pats)
                            log.info(
                                f"[Signal] {sym}: BROKEN SUPPLY RETEST LONG confirmed "
                                f"penalty {old_penalty}→{htf_penalty} +{bz_lv * 3} bonus"
                            )
            except Exception as e:
                log.debug(f"[Signal] broken zone retest check error {sym}: {e}")

            # ── Net HTF adjustment ────────────────────────────────────────
            effective_strength = max(signal.strength + htf_bonus - htf_penalty, 0)
            if htf_bonus > 0 or htf_penalty > 0:
                log.info(f"[Signal] {sym}: MTF adj str={signal.strength:.0f}→{effective_strength:.0f} "
                         f"(+{htf_bonus}/-{htf_penalty}) pats={htf_pats}")

            # ── Signal Memory feedback: skip bad combos, boost good ones ────
            entry_hour = datetime.now().hour
            skip, skip_reason = self.memory.should_skip_signal(
                signal.direction, signal.patterns or [], sym, entry_hour)
            if skip:
                log.info(f"[Signal] SKIP {sym}: memory says {skip_reason}")
                return

            # Adjust confidence from historical performance
            mem_adj = self.memory.confidence_adjustment(
                signal.direction, signal.patterns or [], sym, entry_hour)
            if mem_adj != 1.0:
                old_str = effective_strength
                effective_strength = max(effective_strength * mem_adj, 0)
                log.info(f"[Signal] {sym}: memory adj {old_str:.0f}→{effective_strength:.0f} "
                         f"(mult={mem_adj:.2f})")

            # ── Per-stock momentum fingerprint scoring ────────────────────
            # If this stock has a learned indicator fingerprint, check how
            # well current patterns match. Boost/penalize accordingly.
            try:
                from core.momentum_profiler import get_momentum_profiler
                mp = get_momentum_profiler()
                fit_score = mp.score_signal_fit(
                    sym, signal.direction, signal.patterns or [])
                if fit_score is not None:
                    # fit_score 0.0-1.0: 0.7+ = strong match, 0.3- = anti-match
                    if fit_score >= 0.7:
                        bonus = int((fit_score - 0.5) * 20)  # up to +10
                        effective_strength += bonus
                        log.info(f"[Signal] {sym}: momentum fingerprint MATCH "
                                 f"score={fit_score:.2f} str+={bonus}")
                    elif fit_score < 0.3:
                        penalty = int((0.5 - fit_score) * 20)  # up to -10
                        effective_strength = max(effective_strength - penalty, 0)
                        log.info(f"[Signal] {sym}: momentum fingerprint MISMATCH "
                                 f"score={fit_score:.2f} str-={penalty}")
            except Exception as e:
                log.debug(f"[Signal] momentum profiler err {sym}: {e}")

            # ── Per-stock breakout profile scoring ────────────────────────
            # Check if current indicators match this stock's historical
            # breakout fingerprint (RSI range, EMA state, volume, supertrend).
            try:
                from core.breakout_study import score_current_vs_profile
                pats = signal.patterns or []
                bp_result = score_current_vs_profile(
                    sym, signal.direction,
                    rsi=signal.rsi,
                    volume_ratio=signal.volume_ratio,
                    ema9_above_21=(signal.ema9 > signal.ema21),
                    supertrend_up=("supertrend_up" in pats),
                    above_vwap=(signal.entry_price > signal.vwap if signal.vwap > 0 else True),
                )
                if bp_result is not None:
                    ms = bp_result["match_score"]
                    if ms >= 0.7:
                        bonus = int((ms - 0.5) * 30)  # up to +15
                        effective_strength += bonus
                        log.info(f"[Signal] {sym}: breakout profile MATCH "
                                 f"{ms:.0%} str+={bonus} | {bp_result['checks']}")
                    elif ms < 0.3:
                        penalty = int((0.5 - ms) * 30)  # up to -15
                        effective_strength = max(effective_strength - penalty, 0)
                        log.info(f"[Signal] {sym}: breakout profile MISMATCH "
                                 f"{ms:.0%} str-={penalty} | {bp_result['checks']}")
            except Exception as e:
                log.debug(f"[Signal] breakout profile err {sym}: {e}")

            # Store signal for downstream agents
            with self.state._lock:
                self.state.signals[sym] = signal
                # Clear stale filter results from previous scan
                self.state.filter_results.pop(sym, None)
                self.state.order_flow.pop(sym, None)

            self.state.cast_vote(
                symbol=sym, agent=self.name,
                approve=True,
                direction=signal.direction,
                confidence=effective_strength / 100.0,
            )

            # Synthetic grade for tiered authority (mirrors timeframe_sync thresholds)
            if effective_strength >= 80 and vol_ratio >= 2.0:
                grade = "S"
            elif effective_strength >= 65:
                grade = "A"
            elif effective_strength >= 45:
                grade = "B"
            else:
                grade = "C"

            self.emit("SIGNAL_GENERATED", {
                "symbol": sym,
                "direction": signal.direction,
                "strength": effective_strength,
                "entry_price": signal.entry_price,
                "atr": signal.atr,
                "patterns": signal.patterns,
                "vol_ratio": vol_ratio,
                "rsi": signal.rsi,
                "grade": grade,
                "cc_variant": cc_variant,
                "regime": regime,
            })
            log.info(f"[Signal] {sym} {signal.direction.upper()} str={effective_strength:.0f} "
                     f"vol={vol_ratio:.1f}x grade={grade} pat={signal.patterns[:2]}")

            # ── Bridge to Streamlit: persist signal to logs/signals.json ──
            self._persist_signal(signal, grade, effective_strength, vol_ratio)

        except Exception as e:
            log.error(f"[Signal] {sym}: {e}")

    def _persist_signal(self, signal, grade: str, strength: float, vol_ratio: float) -> None:
        """Convert Signal to dashboard dict format and write to signals.json.

        HARD GATE: only persist if stock has live option chain (F&O eligible).
        Non-F&O stocks are dropped — no signal shown in UI.
        """
        try:
            # ── HARD F&O gate (2-stage) ──────────────────────────────────
            # Stage 1: symbol must be in NSE F&O segment (static authoritative
            #          set — works even when NSE/Dhan APIs are down).
            # Stage 2: live option chain must exist.
            from core.dashboard_data import get_option_chain
            from core.option_translator import get_option_rec
            from core.nse_option_chain import is_fno_symbol

            if not is_fno_symbol(signal.symbol):
                log.info(f"[Signal] DROP {signal.symbol}: not in NSE F&O segment")
                return

            chain = get_option_chain(signal.symbol)
            if not chain:
                log.info(f"[Signal] DROP {signal.symbol}: no option chain (not F&O / data down)")
                return

            entry = signal.entry_price
            atr = signal.atr or (entry * 0.01)  # fallback 1% ATR
            sl_dist = atr * 1.5   # 1.5x ATR for wider room
            # Clamp SL to 1.0-2.5% band — wider SL = bigger target = 50%+ premium gain
            sl_dist = max(sl_dist, entry * 0.010)
            sl_dist = min(sl_dist, entry * 0.025)
            sl = round(entry - sl_dist, 2) if signal.direction == "long" else round(entry + sl_dist, 2)
            # Multi-target: T1=1.5R partial, T2=rr_ratio runner (from learned config)
            try:
                from core.adaptive_learner import get_learned_config as _glc
                _rr = float(_glc().get("SIGNAL_CONFIG", {}).get("rr_ratio", 4.0))
            except Exception:
                _rr = 4.0
            t1 = round(entry + sl_dist * 1.5, 2) if signal.direction == "long" else round(entry - sl_dist * 1.5, 2)
            t2 = round(entry + sl_dist * _rr, 2) if signal.direction == "long" else round(entry - sl_dist * _rr, 2)
            rr_ratio = round(abs(t2 - entry) / abs(entry - sl), 2) if abs(entry - sl) > 0 else 0

            sig_dict = {
                "symbol":            signal.symbol,
                "direction":         signal.direction,
                "confluence_grade":  grade,
                "confluence_score":  int(strength),
                "entry_price":       entry,
                "sl_price":          sl,
                "sl_tight":          sl,
                "target_1":          t1,
                "target_price":      t2,
                "rr_ratio":          rr_ratio,
                "volume_cascade":    vol_ratio >= 2.0,
                "ema_stack_aligned": True,
                "vwap_synced":       True,
                "patterns_combined": signal.patterns or [],
                "patterns":          signal.patterns or [],
                "reason":            signal.reason or "",
                "ts":                datetime.now().isoformat(),
                "rsi":               round(signal.rsi, 1) if signal.rsi else 0.0,
                "volume_ratio":      round(signal.volume_ratio, 2) if signal.volume_ratio else 0.0,
                "vote_margin":       abs(signal.long_votes - signal.short_votes) if hasattr(signal, 'long_votes') else 0,
                "per_tf":            {"5m": {"present": True, "vol_ratio": signal.volume_ratio, "direction": signal.direction, "strength": strength}},
            }

            # ── Option enrichment with live chain data ───────────────────
            # chain is guaranteed non-empty here (hard gate above)
            rec = get_option_rec(
                symbol=signal.symbol, direction=signal.direction,
                spot=entry, entry=entry, sl=sl, target=t1,
                chain_data=chain,
            )
            if not rec:
                log.info(f"[Signal] DROP {signal.symbol}: no option rec (illiquid chain)")
                return
            if rec:
                # Sanity: SL/target within 3x of entry premium
                ep = rec["entry_prem"]
                if ep > 0:
                    sl_r = abs(ep - rec["sl_prem"]) / ep
                    tgt_r = abs(rec["target_prem"] - ep) / ep
                    if sl_r > 3.0 or tgt_r > 3.0:
                        log.warning(f"[Signal] {signal.symbol}: unrealistic premiums, skipping option data")
                    else:
                        sig_dict.update({
                            "option_strike": rec["strike"],
                            "option_expiry": rec["expiry"],
                            "option_type":   rec["option_type"],
                            "entry_prem":    rec["entry_prem"],
                            "sl_prem":       rec["sl_prem"],
                            "target_prem":   rec["target_prem"],
                            "delta":         rec["delta"],
                            "iv_pct":        rec["iv_pct"],
                            "prem_source":   rec["source"],
                        })

            with _signals_lock:
                _pending_signals.append(sig_dict)
                # Flush all pending signals in one atomic write
                to_write = list(_pending_signals)
                _pending_signals.clear()

            write_signals(to_write, meta={
                "source": "aladdin_runner",
                "elapsed_sec": 0,
                "universe_size": len(self.scanner.get_symbols() if hasattr(self.scanner, 'get_symbols') else []),
            })
            log.info(f"[Signal] persisted {signal.symbol} to signals.json (grade={grade})")
        except Exception as e:
            log.error(f"[Signal] persist failed for {signal.symbol}: {e}")
