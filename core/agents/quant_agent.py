"""
QuantAgent (Golub) — Portfolio optimization, Kelly criterion sizing, correlation guard.

Inspired by Bennett Golub (BlackRock CRO / risk quant).

On RISK_APPROVED:
  1. Check sector concentration (max 40% one sector)
  2. Kelly criterion position size (based on journal WR + avg W/L)
  3. Correlation guard — reject if existing position in same sector + direction
  4. Publish QUANT_SIZED with optimal position size

Every 5 min:
  - Recompute portfolio Greeks / heat map
  - Update Kelly edge from rolling journal stats
"""

import json
import logging
import math
import os
from typing import Dict, Optional

import config
from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent

log = logging.getLogger(__name__)

JOURNAL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                            "logs", "signal_journal.jsonl")
SECTOR_MAP = getattr(config, "STOCK_SECTOR_MAP", {})


class QuantAgent(BaseAgent):
    name = "quant_golub"
    interval_sec = 300

    MAX_SECTOR_PCT = 0.30          # Reduced from 40% - less sector concentration
    MIN_KELLY_EDGE = 0.08          # Raised from 0.05 - need real edge before sizing up
    KELLY_FRACTION = 0.15          # Reduced from 0.25 - more conservative until WR improves
    ROLLING_WINDOW = 50

    def __init__(self, state: SharedState, bus: EventBus, capital: float):
        super().__init__(state, bus)
        self.capital = capital
        self._kelly_fraction = self.KELLY_FRACTION
        self._win_rate = 0.0
        self._avg_win = 0.0
        self._avg_loss = 0.0
        bus.subscribe("RISK_APPROVED", self._on_risk_approved)

    def run(self) -> None:
        self._update_kelly_stats()

    def _load_journal_outcomes(self):
        wins, losses = [], []
        try:
            if not os.path.exists(JOURNAL_PATH):
                return wins, losses
            with open(JOURNAL_PATH) as f:
                lines = f.readlines()
            for line in lines[-200:]:
                try:
                    o = json.loads(line)
                    oc = o.get("outcome")
                    ep = o.get("entry_price", 0)
                    xp = o.get("exit_price", 0)
                    if not (oc and ep and xp):
                        continue
                    pnl_pct = abs(xp - ep) / ep if ep > 0 else 0
                    if oc == "TARGET_HIT":
                        wins.append(pnl_pct)
                    elif oc == "SL_HIT":
                        losses.append(pnl_pct)
                except Exception:
                    continue
        except Exception:
            pass
        return wins, losses

    def _update_kelly_stats(self):
        wins, losses = self._load_journal_outcomes()
        total = len(wins) + len(losses)
        if total < 10:
            self._kelly_fraction = self.KELLY_FRACTION
            return

        self._win_rate = len(wins) / total
        self._avg_win = sum(wins) / len(wins) if wins else 0
        self._avg_loss = sum(losses) / len(losses) if losses else 0.01

        if self._avg_loss <= 0:
            self._kelly_fraction = self.KELLY_FRACTION
            return

        # Kelly: f* = (p * b - q) / b where b = avg_win/avg_loss
        b = self._avg_win / self._avg_loss
        p = self._win_rate
        q = 1 - p
        kelly_full = (p * b - q) / b if b > 0 else 0

        if kelly_full < self.MIN_KELLY_EDGE:
            self._kelly_fraction = 0.005  # minimal size — no edge
            log.info(f"[Quant/Golub] negative edge kelly={kelly_full:.3f} -> min size")
        else:
            self._kelly_fraction = kelly_full * 0.25  # quarter-Kelly
            log.info(f"[Quant/Golub] kelly={kelly_full:.2f} -> f={self._kelly_fraction:.3f} "
                     f"WR={p:.1%} b={b:.2f}")

    def _sector_exposure(self, direction: str) -> Dict[str, float]:
        positions = self.state.get("open_positions", {})
        sector_risk: Dict[str, float] = {}
        for sym, pos in positions.items():
            if isinstance(pos, dict) and pos.get("direction") == direction:
                sector = SECTOR_MAP.get(sym, "OTHER")
                risk = abs(pos.get("entry_price", 0) * pos.get("quantity", 0))
                sector_risk[sector] = sector_risk.get(sector, 0) + risk
        return sector_risk

    def _on_risk_approved(self, event: AgentEvent):
        sym = event.payload["symbol"]
        direction = event.payload["direction"]
        entry = event.payload.get("entry_price", 0)
        atr = event.payload.get("atr", 0)

        if entry <= 0:
            return

        # Sector concentration check
        sector = SECTOR_MAP.get(sym, "OTHER")
        exposure = self._sector_exposure(direction)
        sector_total = exposure.get(sector, 0)
        if sector_total / max(self.capital, 1) > self.MAX_SECTOR_PCT:
            log.warning(f"[Quant/Golub] BLOCK {sym}: sector {sector} at "
                        f"{sector_total/self.capital:.0%} > {self.MAX_SECTOR_PCT:.0%}")
            return

        # Correlation guard — no duplicate sector+direction
        positions = self.state.get("open_positions", {})
        for s, pos in positions.items():
            if (isinstance(pos, dict)
                    and SECTOR_MAP.get(s, "X") == sector
                    and pos.get("direction") == direction
                    and s != sym):
                log.info(f"[Quant/Golub] BLOCK {sym}: correlated with {s} in {sector}")
                return

        # Kelly-optimal position size
        risk_pct = min(self._kelly_fraction, config.RISK_CONFIG.get("max_risk_per_trade", 0.02))

        # AI sizing adjustment: half position if AI says "half"
        ai_size = event.payload.get("ai_size", "full")
        if ai_size == "half":
            risk_pct *= 0.5

        risk_amount = self.capital * risk_pct
        sl_dist = atr * config.RISK_CONFIG.get("atr_sl_multiplier", 1.5) if atr > 0 else entry * 0.02
        if sl_dist <= 0:
            sl_dist = entry * 0.01
        optimal_qty = max(1, int(risk_amount / sl_dist))

        # Lot size rounding
        try:
            from core.futures_leg import lot_size_for
            lot = max(1, int(lot_size_for(sym)))
        except Exception:
            lot = max(1, int(config.NSE_LOT_SIZES.get(sym, 1)))
        if lot > 1:
            optimal_qty = max(lot, (optimal_qty // lot) * lot)

        self.state.cast_vote(
            symbol=sym, agent=self.name,
            approve=True, direction=direction,
            confidence=min(self._kelly_fraction * 10, 1.0),
        )

        self.emit("QUANT_SIZED", {
            "symbol": sym,
            "direction": direction,
            "entry_price": entry,
            "optimal_qty": optimal_qty,
            "risk_pct": round(risk_pct, 4),
            "kelly_f": round(self._kelly_fraction, 4),
            "sector": sector,
            "size_mult": event.payload.get("size_mult", 1.0),
            "grade": event.payload.get("grade", "A"),
        })
        log.info(f"[Quant/Golub] SIZED {sym} qty={optimal_qty} kelly_f={self._kelly_fraction:.3f} "
                 f"sector={sector}")
