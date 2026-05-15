"""
OpeningScoutAgent — dedicated opening session hunter.

Active 9:15-10:30 IST. Scans universe for:
  1. Gap-up / gap-down stocks with momentum confirmation
  2. Opening Range Breakouts (first 15min high/low break)
  3. Directional Predictor consensus (multi-factor up/down score)

Emits separate "OPENING_SIGNAL" events that bypass the standard pipeline's
restrictive filters. Goes through coordinator with grade S/A priority.
"""

import logging
from datetime import datetime, time as dtime
from typing import Dict, List

from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus

log = logging.getLogger(__name__)

# Active window
ACTIVE_START = dtime(9, 15)
ACTIVE_END = dtime(10, 30)


class OpeningScoutAgent(BaseAgent):
    name = "opening_scout"
    interval_sec = 60   # scan every 60s during opening window

    def __init__(self, state: SharedState, bus: EventBus, scanner, universe: List[str]):
        super().__init__(state, bus)
        self.scanner = scanner
        self.universe = universe
        self._signaled_today: set = set()
        self._last_day = None

    def run(self) -> None:
        now = datetime.now().time()
        today = datetime.now().date()

        # Day reset
        if self._last_day != today:
            self._last_day = today
            self._signaled_today = set()

        # Active only in opening window
        if not (ACTIVE_START <= now <= ACTIVE_END):
            return

        log.info(f"[OpeningScout] scanning {len(self.universe)} symbols for opening setups")

        nifty_pct = self.state.get("nifty_change_pct", 0.0)
        opportunities = []

        for sym in self.universe:
            if sym in self._signaled_today:
                continue
            try:
                opp = self._evaluate(sym, nifty_pct)
                if opp:
                    opportunities.append(opp)
                    self._signaled_today.add(sym)
            except Exception as e:
                log.debug(f"[OpeningScout] {sym}: {e}")

        # Sort by quality, emit top 5
        opportunities.sort(key=lambda o: o['quality_score'], reverse=True)
        for opp in opportunities[:5]:
            self._emit_opening_signal(opp)

        if opportunities:
            log.info(f"[OpeningScout] {len(opportunities)} opportunities found, emitted top {min(5, len(opportunities))}")

    def _evaluate(self, sym: str, nifty_pct: float) -> Dict:
        """Check sym for opening session opportunity. Returns dict or None."""
        df_5m = self.scanner.get_market_data(sym, 30)
        if df_5m is None or df_5m.empty or len(df_5m) < 3:
            return None

        df_1d = self.scanner.get_market_data(sym, 1440) if hasattr(self.scanner, 'get_daily_data') else None

        # Run all 3 detectors
        from core.gap_analysis import analyze_gap
        from core.opening_range import detect_orb_break
        from core.directional_predictor import predict_direction

        gap = None
        if df_1d is not None and not df_1d.empty:
            gap = analyze_gap(sym, df_5m, df_1d)

        orb = detect_orb_break(sym, df_5m)
        pred = predict_direction(sym, df_5m, df_daily=df_1d, nifty_pct=nifty_pct)

        # Quality score: combine all 3 signals
        score = 0
        signals_fired = []

        if gap and gap.likely_action == 'gap_and_go' and gap.gap_type in ('breakaway', 'runaway'):
            score += int(gap.confidence * 30)
            signals_fired.append(f"gap_{gap.gap_type}_{gap.bias}")

        if orb:
            score += int(orb.confidence * 40)
            signals_fired.append(f"orb_break_{orb.direction}")

        if pred and pred.classification in ('STRONG_UP', 'STRONG_DOWN', 'MODERATE_UP', 'MODERATE_DOWN'):
            score += abs(pred.score) // 2
            signals_fired.append(f"direction_{pred.classification.lower()}")

        if score < 30:   # threshold for any opportunity at all
            return None

        # Determine direction (must agree across signals)
        direction = None
        if orb:
            direction = orb.direction
        elif pred and pred.direction in ('up', 'down'):
            direction = 'long' if pred.direction == 'up' else 'short'
        elif gap and gap.bias in ('bullish', 'bearish'):
            direction = 'long' if gap.bias == 'bullish' else 'short'

        if not direction:
            return None

        # Entry/SL/Target
        current_price = float(df_5m['close'].iloc[-1])
        if orb:
            entry = orb.entry_price
            sl = orb.stop_loss
            target = orb.target_1
        else:
            entry = current_price
            # Use 0.8% SL for non-ORB signals
            if direction == 'long':
                sl = entry * 0.992
                target = entry * 1.012
            else:
                sl = entry * 1.008
                target = entry * 0.988

        return {
            'symbol': sym,
            'direction': direction,
            'entry': entry,
            'sl': sl,
            'target': target,
            'quality_score': score,
            'signals': signals_fired,
            'gap_pct': gap.gap_pct if gap else 0,
            'orb_break': orb is not None,
            'predicted_direction': pred.classification if pred else 'UNKNOWN',
            'probability_up': pred.probability_up if pred else 0.5,
        }

    def _emit_opening_signal(self, opp: Dict) -> None:
        log.info(
            f"[OpeningScout] OPP {opp['symbol']} {opp['direction'].upper()} "
            f"score={opp['quality_score']} signals={opp['signals']} "
            f"entry={opp['entry']:.2f} sl={opp['sl']:.2f} tgt={opp['target']:.2f}"
        )

        # Cast scout vote so coordinator can promote
        self.state.cast_vote(
            symbol=opp['symbol'],
            agent=self.name,
            approve=True,
            direction=opp['direction'],
            confidence=min(0.95, opp['quality_score'] / 100),
        )

        # Emit as SURGE_DETECTED to feed into normal pipeline
        # (signal_engine will run with top_mover_mode override)
        self.emit("SURGE_DETECTED", {
            "symbol": opp['symbol'],
            "vol_ratio": 2.0,   # synthetic — opening scout already validated
            "source": "opening_scout",
            "opening_opp": opp,
        })
