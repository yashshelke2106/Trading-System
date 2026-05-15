"""
AlphaAgent (Fink) — Macro regime, sector rotation, capital allocation.

Inspired by Larry Fink (BlackRock CEO) — macro-level strategic direction.

Every 5 min:
  1. Track Nifty/BankNifty momentum (EMA slope)
  2. Sector rotation scoring (which sectors leading/lagging)
  3. Set macro_bias in SharedState (strong_bull / bull / neutral / bear / strong_bear)
  4. Publish ALPHA_REGIME with sector rankings + allocation weights

On SURGE_DETECTED:
  - Check if symbol's sector is in favorable rotation → boost/suppress
"""

import logging
from typing import Dict, List, Optional, Tuple
import pandas as pd

from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent
import config

log = logging.getLogger(__name__)

SECTOR_MAP = getattr(config, "STOCK_SECTOR_MAP", {})

# Reverse: sector → symbols
_SECTOR_SYMBOLS: Dict[str, List[str]] = {}
for _sym, _sec in SECTOR_MAP.items():
    _SECTOR_SYMBOLS.setdefault(_sec, []).append(_sym)


class AlphaAgent(BaseAgent):
    name = "alpha_fink"
    interval_sec = 300

    def __init__(self, state: SharedState, bus: EventBus, api):
        super().__init__(state, bus)
        self.api = api
        self._sector_scores: Dict[str, float] = {}
        self._macro_bias = "neutral"
        self._favored_sectors: List[str] = []
        bus.subscribe("SURGE_DETECTED", self._on_surge)

    def run(self) -> None:
        self._update_macro_bias()
        self._score_sectors()
        self._publish_regime()

    def _update_macro_bias(self):
        try:
            nifty_change = self.state.get("nifty_change_pct", 0.0)
            bnifty_change = self.state.get("banknifty_change_pct", 0.0)
            vix = self.state.get("vix_proxy", 15.0)

            # Composite: weighted Nifty + BankNifty + inverse VIX
            composite = nifty_change * 0.5 + bnifty_change * 0.3 - (vix - 15) * 0.1

            if composite > 0.8:
                bias = "strong_bull"
            elif composite > 0.3:
                bias = "bull"
            elif composite < -0.8:
                bias = "strong_bear"
            elif composite < -0.3:
                bias = "bear"
            else:
                bias = "neutral"

            self._macro_bias = bias
            self.state.set(macro_bias=bias, macro_composite=round(composite, 3))
            log.info(f"[Alpha/Fink] macro={bias} composite={composite:.3f} "
                     f"nifty={nifty_change:+.2f}% vix={vix:.1f}")
        except Exception as e:
            log.debug(f"[Alpha/Fink] macro update failed: {e}")

    def _score_sectors(self):
        scores: Dict[str, float] = {}

        for sector, symbols in _SECTOR_SYMBOLS.items():
            sector_scores = []
            for sym in symbols[:3]:  # top 3 per sector for speed
                try:
                    df = self.api.get_intraday_data(sym, interval=15, days_back=3)
                    if df is None or len(df) < 10:
                        continue
                    closes = df["close"]
                    # EMA momentum: slope of 9EMA over last 5 bars
                    ema9 = closes.ewm(span=9, adjust=False).mean()
                    if len(ema9) >= 5:
                        slope = (float(ema9.iloc[-1]) - float(ema9.iloc[-5])) / float(ema9.iloc[-5]) * 100
                        sector_scores.append(slope)
                except Exception:
                    continue

            if sector_scores:
                scores[sector] = sum(sector_scores) / len(sector_scores)

        self._sector_scores = scores
        # Top sectors (positive momentum)
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        self._favored_sectors = [s for s, sc in ranked if sc > 0.1][:3]

        self.state.set(
            sector_scores=scores,
            favored_sectors=self._favored_sectors,
        )

        if ranked:
            top3 = ", ".join(f"{s}({sc:+.2f}%)" for s, sc in ranked[:3])
            bot3 = ", ".join(f"{s}({sc:+.2f}%)" for s, sc in ranked[-3:])
            log.info(f"[Alpha/Fink] sectors top=[{top3}] bot=[{bot3}]")

    def _publish_regime(self):
        self.emit("ALPHA_REGIME", {
            "macro_bias": self._macro_bias,
            "sector_scores": self._sector_scores,
            "favored_sectors": self._favored_sectors,
        })

    def _on_surge(self, event: AgentEvent):
        sym = event.payload.get("symbol", "")
        sector = SECTOR_MAP.get(sym, "OTHER")

        # Boost confidence if sector is favored, suppress if lagging
        sector_score = self._sector_scores.get(sector, 0)
        if sector in self._favored_sectors:
            boost = 0.15
            log.info(f"[Alpha/Fink] {sym} in favored sector {sector} (+{boost})")
        elif sector_score < -0.3:
            boost = -0.20
            log.info(f"[Alpha/Fink] {sym} in weak sector {sector} ({boost})")
        else:
            boost = 0.0

        if boost != 0:
            self.state.set(**{f"alpha_boost_{sym}": boost})
