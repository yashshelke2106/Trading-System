"""
Swarm Intelligence Engine — Collective learning from every trade outcome.

Evolution stages:
  NOOB       (0-20 trades)   : Fixed conservative params, no adaptation
  LEARNING   (20-50 trades)  : Begin adapting weights, cautious moves
  COMPETENT  (50-100 trades) : Full adaptation, pattern memory active
  ADVANCED   (100-200 trades): Multi-factor optimization, combo discovery
  ELITE      (200+ trades)   : Predictive filtering, self-tuning risk, auto-prune

Each trade teaches the swarm:
  - Which patterns led to wins vs losses (pattern DNA)
  - Which market conditions (RSI, vol, time, regime) favor success
  - Which symbols perform best (symbol fitness)
  - Optimal SL/target distances for each context
  - Which agent combinations produce best consensus

The swarm memory persists in logs/swarm_memory.json and evolves continuously.
"""

import json
import logging
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import config

log = logging.getLogger(__name__)

MEMORY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "swarm_memory.json")
JOURNAL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "signal_journal.jsonl")

# Evolution stages
STAGES = {
    "NOOB": (0, 20),
    "LEARNING": (20, 50),
    "COMPETENT": (50, 100),
    "ADVANCED": (100, 200),
    "ELITE": (200, float("inf")),
}


class SwarmMemory:
    """Persistent collective memory of the swarm."""

    def __init__(self):
        self.pattern_dna: Dict[str, Dict] = {}       # pattern -> {wins, losses, wr, weight, contexts}
        self.symbol_fitness: Dict[str, Dict] = {}    # symbol -> {wins, losses, wr, avg_pnl_pct}
        self.time_memory: Dict[int, Dict] = {}       # hour -> {wins, losses, wr}
        self.rsi_zones: Dict[str, Dict] = {}         # "50-60" -> {wins, losses, wr}
        self.sl_memory: Dict[str, Dict] = {}         # "0.5-0.8" -> {wins, losses, wr}
        self.regime_memory: Dict[str, Dict] = {}     # regime -> {wins, losses, wr}
        self.combo_memory: Dict[str, Dict] = {}      # "pat1+pat2" -> {wins, losses, wr}
        self.agent_consensus: Dict[str, Dict] = {}   # "agent_set" -> {wins, losses}
        self.evolution_stage: str = "NOOB"
        self.total_trades: int = 0
        self.generation: int = 1                     # increments each adaptation cycle
        self.adaptations_log: List[Dict] = []        # history of param changes
        self.optimal_params: Dict = {}               # swarm-derived optimal parameters
        self.last_adapted: str = ""
        self.streak: int = 0                         # current win/loss streak
        self.max_streak: int = 0
        self.cumulative_pnl_pct: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "pattern_dna": self.pattern_dna,
            "symbol_fitness": self.symbol_fitness,
            "time_memory": {str(k): v for k, v in self.time_memory.items()},
            "rsi_zones": self.rsi_zones,
            "sl_memory": self.sl_memory,
            "regime_memory": self.regime_memory,
            "combo_memory": self.combo_memory,
            "agent_consensus": self.agent_consensus,
            "evolution_stage": self.evolution_stage,
            "total_trades": self.total_trades,
            "generation": self.generation,
            "adaptations_log": self.adaptations_log[-50:],  # keep last 50
            "optimal_params": self.optimal_params,
            "last_adapted": self.last_adapted,
            "streak": self.streak,
            "max_streak": self.max_streak,
            "cumulative_pnl_pct": self.cumulative_pnl_pct,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "SwarmMemory":
        m = cls()
        m.pattern_dna = d.get("pattern_dna", {})
        m.symbol_fitness = d.get("symbol_fitness", {})
        m.time_memory = {int(k): v for k, v in d.get("time_memory", {}).items()}
        m.rsi_zones = d.get("rsi_zones", {})
        m.sl_memory = d.get("sl_memory", {})
        m.regime_memory = d.get("regime_memory", {})
        m.combo_memory = d.get("combo_memory", {})
        m.agent_consensus = d.get("agent_consensus", {})
        m.evolution_stage = d.get("evolution_stage", "NOOB")
        m.total_trades = d.get("total_trades", 0)
        m.generation = d.get("generation", 1)
        m.adaptations_log = d.get("adaptations_log", [])
        m.optimal_params = d.get("optimal_params", {})
        m.last_adapted = d.get("last_adapted", "")
        m.streak = d.get("streak", 0)
        m.max_streak = d.get("max_streak", 0)
        m.cumulative_pnl_pct = d.get("cumulative_pnl_pct", 0.0)
        return m


class SwarmIntelligence:
    """
    Collective intelligence engine. Every trade outcome feeds the swarm.
    Agents query swarm for approval/rejection signals.
    System evolves from NOOB to ELITE automatically.
    """

    def __init__(self):
        self.memory = self._load_memory()
        self._dirty = False

    def _load_memory(self) -> SwarmMemory:
        try:
            if os.path.exists(MEMORY_PATH):
                with open(MEMORY_PATH) as f:
                    return SwarmMemory.from_dict(json.load(f))
        except Exception as e:
            log.warning(f"[Swarm] memory load failed: {e}")
        return SwarmMemory()

    def save(self):
        if not self._dirty:
            return
        try:
            os.makedirs(os.path.dirname(MEMORY_PATH), exist_ok=True)
            with open(MEMORY_PATH, "w") as f:
                json.dump(self.memory.to_dict(), f, indent=2)
            self._dirty = False
        except Exception as e:
            log.error(f"[Swarm] save failed: {e}")

    @property
    def stage(self) -> str:
        return self.memory.evolution_stage

    @property
    def generation(self) -> int:
        return self.memory.generation

    # ══════════════════════════════════════════════════════════════════════
    # LEARNING: Absorb trade outcome into collective memory
    # ══════════════════════════════════════════════════════════════════════

    def learn_from_trade(self, trade: Dict) -> Dict:
        """
        Core learning function. Called on every TRADE_CLOSED event.
        Absorbs ALL context into swarm memory.

        Returns dict of insights/adaptations made.
        """
        outcome = trade.get("outcome", "")
        if outcome not in ("TARGET_HIT", "SL_HIT", "EXPIRED", "VOLUME_EXIT", "PARTIAL_T1"):
            return {}

        # EXPIRED/VOLUME_EXIT = lost opportunity, treat as mild negative
        won = outcome == "TARGET_HIT"
        if outcome in ("EXPIRED", "VOLUME_EXIT"):
            won = False  # didn't hit target = pattern wasn't strong enough
        sym = trade.get("symbol", "")
        direction = trade.get("direction", "long")
        patterns = trade.get("patterns", [])
        rsi = trade.get("rsi", 0)
        vol_ratio = trade.get("volume_ratio", 0)
        entry = trade.get("entry_price", 0)
        sl = trade.get("sl_price", 0)
        target = trade.get("target_price", 0)
        regime = trade.get("market_bias", "neutral")
        hour = self._extract_hour(trade.get("ts", ""))
        pnl_pct = trade.get("pnl_pct", 0)

        insights = {"trade_num": self.memory.total_trades + 1, "won": won}
        self.memory.total_trades += 1
        self._dirty = True

        # Streak tracking
        if won:
            self.memory.streak = max(0, self.memory.streak) + 1
        else:
            self.memory.streak = min(0, self.memory.streak) - 1
        self.memory.max_streak = max(self.memory.max_streak, abs(self.memory.streak))
        self.memory.cumulative_pnl_pct += pnl_pct

        # 1. Pattern DNA — each pattern strengthens/weakens with time-decay
        # Recent trades weighted 2x more than trades > 30 days old
        for pat in patterns:
            key = f"{direction}:{pat}"
            if key not in self.memory.pattern_dna:
                self.memory.pattern_dna[key] = {
                    "wins": 0, "losses": 0, "weight": 1.0, "contexts": [],
                    "weighted_wins": 0.0, "weighted_losses": 0.0,
                }
            dna = self.memory.pattern_dna[key]

            # Raw counts (always increment for backwards-compat)
            if won:
                dna["wins"] += 1
                dna["weighted_wins"] = dna.get("weighted_wins", 0.0) + 1.0
            else:
                dna["losses"] += 1
                dna["weighted_losses"] = dna.get("weighted_losses", 0.0) + 1.0

            # Decay older outcomes (each call adds 1, decays existing 1% per trade)
            # After ~70 trades, original weight halved. Adapts to changing markets.
            decay = 0.99
            dna["weighted_wins"] = dna["weighted_wins"] * decay
            dna["weighted_losses"] = dna["weighted_losses"] * decay

            total = dna["wins"] + dna["losses"]
            dna["wr"] = dna["wins"] / total
            wt_total = dna["weighted_wins"] + dna["weighted_losses"]
            wt_wr = dna["weighted_wins"] / wt_total if wt_total > 0 else 0.5
            dna["weighted_wr"] = round(wt_wr, 3)

            # Adaptive weight: now uses time-decayed WR (recent matters more)
            if total >= 3:
                # Sigmoid-smoothed: maps WR to 0.2-2.0 range
                dna["weight"] = round(0.2 + 1.8 / (1 + math.exp(-8 * (wt_wr - 0.35))), 3)

            # Store recent contexts (last 10) for this pattern
            ctx = {"rsi": rsi, "vol": vol_ratio, "hour": hour, "won": won}
            dna["contexts"] = (dna.get("contexts", []) + [ctx])[-10:]

        # 2. Symbol fitness
        if sym:
            if sym not in self.memory.symbol_fitness:
                self.memory.symbol_fitness[sym] = {"wins": 0, "losses": 0, "pnl_sum": 0}
            sf = self.memory.symbol_fitness[sym]
            if won:
                sf["wins"] += 1
            else:
                sf["losses"] += 1
            sf["pnl_sum"] = sf.get("pnl_sum", 0) + pnl_pct
            total = sf["wins"] + sf["losses"]
            sf["wr"] = sf["wins"] / total
            sf["avg_pnl_pct"] = sf["pnl_sum"] / total

        # 3. Time memory
        if hour is not None:
            if hour not in self.memory.time_memory:
                self.memory.time_memory[hour] = {"wins": 0, "losses": 0}
            tm = self.memory.time_memory[hour]
            if won:
                tm["wins"] += 1
            else:
                tm["losses"] += 1
            tm["wr"] = tm["wins"] / (tm["wins"] + tm["losses"])

        # 4. RSI zone memory
        if rsi:
            zone = self._rsi_zone(rsi)
            if zone not in self.memory.rsi_zones:
                self.memory.rsi_zones[zone] = {"wins": 0, "losses": 0}
            rz = self.memory.rsi_zones[zone]
            if won:
                rz["wins"] += 1
            else:
                rz["losses"] += 1
            rz["wr"] = rz["wins"] / (rz["wins"] + rz["losses"])

        # 5. SL distance memory
        if entry and sl:
            sl_pct = abs(entry - sl) / entry * 100
            sl_band = self._sl_band(sl_pct)
            if sl_band not in self.memory.sl_memory:
                self.memory.sl_memory[sl_band] = {"wins": 0, "losses": 0}
            sm = self.memory.sl_memory[sl_band]
            if won:
                sm["wins"] += 1
            else:
                sm["losses"] += 1
            sm["wr"] = sm["wins"] / (sm["wins"] + sm["losses"])

        # 6. Regime memory
        if regime:
            if regime not in self.memory.regime_memory:
                self.memory.regime_memory[regime] = {"wins": 0, "losses": 0}
            rm = self.memory.regime_memory[regime]
            if won:
                rm["wins"] += 1
            else:
                rm["losses"] += 1
            rm["wr"] = rm["wins"] / (rm["wins"] + rm["losses"])

        # 7. Pattern combo memory (2-pattern combos)
        sorted_pats = sorted(set(patterns))
        for i in range(len(sorted_pats)):
            for j in range(i + 1, len(sorted_pats)):
                combo = f"{sorted_pats[i]}+{sorted_pats[j]}"
                if combo not in self.memory.combo_memory:
                    self.memory.combo_memory[combo] = {"wins": 0, "losses": 0}
                cm = self.memory.combo_memory[combo]
                if won:
                    cm["wins"] += 1
                else:
                    cm["losses"] += 1
                total = cm["wins"] + cm["losses"]
                cm["wr"] = cm["wins"] / total

        # Update evolution stage
        old_stage = self.memory.evolution_stage
        self._update_stage()
        if self.memory.evolution_stage != old_stage:
            insights["stage_up"] = self.memory.evolution_stage
            log.info(f"[Swarm] EVOLUTION: {old_stage} -> {self.memory.evolution_stage} "
                     f"(trade #{self.memory.total_trades})")

        # Run adaptation if ready
        adaptations = self._maybe_adapt()
        if adaptations:
            insights["adaptations"] = adaptations

        self.save()
        return insights

    # ══════════════════════════════════════════════════════════════════════
    # QUERY: Agents ask swarm for trade approval signal
    # ══════════════════════════════════════════════════════════════════════

    def evaluate_signal(self, symbol: str, direction: str, patterns: List[str],
                        rsi: float = 0, volume_ratio: float = 0,
                        hour: int = None, regime: str = "neutral") -> Dict:
        """
        Swarm collective evaluation of a potential trade.
        Returns: {approve: bool, confidence: float, reasons: [], kill_patterns: []}
        """
        if self.memory.evolution_stage == "NOOB":
            # Not enough data to have opinion - pass through
            return {"approve": True, "confidence": 0.5, "reasons": ["noob_stage"], "score": 0}

        score = 0.0
        reasons = []
        kill_patterns = []

        # 1. Pattern DNA score
        pat_scores = []
        for pat in patterns:
            key = f"{direction}:{pat}"
            dna = self.memory.pattern_dna.get(key, {})
            if dna.get("wins", 0) + dna.get("losses", 0) >= 3:
                w = dna.get("weight", 1.0)
                wr = dna.get("wr", 0.5)
                pat_scores.append(w)
                if wr <= 0.15 and dna["wins"] + dna["losses"] >= 5:
                    kill_patterns.append(key)
                    reasons.append(f"kill:{pat}(WR={wr:.0%})")

        if pat_scores:
            avg_pat_weight = sum(pat_scores) / len(pat_scores)
            score += (avg_pat_weight - 1.0) * 30  # -24 to +54 range
            if avg_pat_weight > 1.3:
                reasons.append(f"strong_patterns({avg_pat_weight:.2f})")
            elif avg_pat_weight < 0.7:
                reasons.append(f"weak_patterns({avg_pat_weight:.2f})")

        # 2. Symbol fitness
        sf = self.memory.symbol_fitness.get(symbol, {})
        if sf.get("wins", 0) + sf.get("losses", 0) >= 5:
            sym_wr = sf.get("wr", 0.5)
            score += (sym_wr - 0.35) * 20  # base expectation 35%
            if sym_wr >= 0.50:
                reasons.append(f"good_symbol({sym_wr:.0%})")
            elif sym_wr <= 0.20:
                reasons.append(f"bad_symbol({sym_wr:.0%})")

        # 3. Time fitness
        if hour is not None and hour in self.memory.time_memory:
            tm = self.memory.time_memory[hour]
            if tm["wins"] + tm["losses"] >= 5:
                t_wr = tm["wr"]
                score += (t_wr - 0.35) * 15
                if t_wr <= 0.15:
                    reasons.append(f"bad_hour({hour}:00={t_wr:.0%})")
                elif t_wr >= 0.50:
                    reasons.append(f"good_hour({hour}:00={t_wr:.0%})")

        # 4. RSI zone fitness
        if rsi:
            zone = self._rsi_zone(rsi)
            rz = self.memory.rsi_zones.get(zone, {})
            if rz.get("wins", 0) + rz.get("losses", 0) >= 5:
                r_wr = rz["wr"]
                score += (r_wr - 0.35) * 15
                if r_wr <= 0.20:
                    reasons.append(f"bad_rsi_zone({zone}={r_wr:.0%})")

        # 5. Regime fitness
        if regime:
            rm = self.memory.regime_memory.get(regime, {})
            if rm.get("wins", 0) + rm.get("losses", 0) >= 5:
                rg_wr = rm["wr"]
                score += (rg_wr - 0.35) * 10

        # 6. Combo bonus/penalty (ADVANCED+ only)
        if self.memory.evolution_stage in ("ADVANCED", "ELITE"):
            sorted_pats = sorted(set(patterns))
            for i in range(len(sorted_pats)):
                for j in range(i + 1, len(sorted_pats)):
                    combo = f"{sorted_pats[i]}+{sorted_pats[j]}"
                    cm = self.memory.combo_memory.get(combo, {})
                    if cm.get("wins", 0) + cm.get("losses", 0) >= 5:
                        c_wr = cm.get("wr", 0.5)
                        if c_wr >= 0.55:
                            score += 10
                            reasons.append(f"winning_combo({combo}={c_wr:.0%})")
                        elif c_wr <= 0.15:
                            score -= 15
                            reasons.append(f"losing_combo({combo}={c_wr:.0%})")

        # 7. Streak awareness (ELITE only)
        if self.memory.evolution_stage == "ELITE":
            if self.memory.streak <= -2:
                score -= 10  # cautious after losses
                reasons.append("loss_streak_caution")
            elif self.memory.streak >= 3:
                score += 5  # system is hot
                reasons.append("hot_streak")

        # Decision threshold scales with evolution
        thresholds = {
            "LEARNING": -10,     # very permissive
            "COMPETENT": -5,     # slightly filter
            "ADVANCED": 0,       # neutral baseline
            "ELITE": 5,          # only positive-EV trades
        }
        threshold = thresholds.get(self.memory.evolution_stage, -10)

        approve = score >= threshold and len(kill_patterns) == 0
        confidence = min(max((score + 30) / 60, 0.1), 0.99)  # normalize to 0.1-0.99

        return {
            "approve": approve,
            "confidence": round(confidence, 3),
            "score": round(score, 1),
            "reasons": reasons,
            "kill_patterns": kill_patterns,
            "stage": self.memory.evolution_stage,
            "generation": self.memory.generation,
        }

    # ══════════════════════════════════════════════════════════════════════
    # ADAPTATION: Auto-tune system parameters based on swarm knowledge
    # ══════════════════════════════════════════════════════════════════════

    def _maybe_adapt(self) -> List[Dict]:
        """Run adaptation every 10 trades (or 5 in ELITE)."""
        interval = 5 if self.memory.evolution_stage == "ELITE" else 10
        if self.memory.total_trades % interval != 0:
            return []

        if self.memory.evolution_stage == "NOOB":
            return []  # not enough data

        adaptations = []
        self.memory.generation += 1

        # Adaptation 1: Optimal RSI floor from zone data
        best_rsi_zones = sorted(
            [(z, d) for z, d in self.memory.rsi_zones.items()
             if d["wins"] + d["losses"] >= 5],
            key=lambda x: -x[1]["wr"]
        )
        if best_rsi_zones:
            # Find lowest RSI zone with WR >= 35%
            good_zones = [z for z, d in best_rsi_zones if d["wr"] >= 0.35]
            if good_zones:
                # Extract lower bound of best zone
                try:
                    optimal_rsi_floor = int(good_zones[-1].split("-")[0])
                    if optimal_rsi_floor != self.memory.optimal_params.get("rsi_floor"):
                        adaptations.append({
                            "param": "rsi_long_momentum_min",
                            "old": self.memory.optimal_params.get("rsi_floor", 62),
                            "new": optimal_rsi_floor,
                            "reason": f"zone {good_zones[-1]} WR >= 35%",
                        })
                        self.memory.optimal_params["rsi_floor"] = optimal_rsi_floor
                except (ValueError, IndexError):
                    pass

        # Adaptation 2: Optimal time window
        good_hours = [h for h, d in self.memory.time_memory.items()
                      if d["wins"] + d["losses"] >= 5 and d["wr"] >= 0.35]
        bad_hours = [h for h, d in self.memory.time_memory.items()
                     if d["wins"] + d["losses"] >= 5 and d["wr"] <= 0.15]
        if bad_hours:
            earliest_bad = min(bad_hours)
            if earliest_bad != self.memory.optimal_params.get("block_after_hour"):
                adaptations.append({
                    "param": "block_after_hour",
                    "old": self.memory.optimal_params.get("block_after_hour", 13),
                    "new": earliest_bad,
                    "reason": f"hours {bad_hours} WR <= 15%",
                })
                self.memory.optimal_params["block_after_hour"] = earliest_bad

        # Adaptation 3: Optimal SL band
        best_sl = sorted(
            [(b, d) for b, d in self.memory.sl_memory.items()
             if d["wins"] + d["losses"] >= 5],
            key=lambda x: -x[1]["wr"]
        )
        if best_sl:
            best_band = best_sl[0][0]
            try:
                parts = best_band.split("-")
                optimal_min_sl = float(parts[0]) / 100
                optimal_max_sl = float(parts[1]) / 100
                if optimal_min_sl != self.memory.optimal_params.get("min_sl_pct"):
                    adaptations.append({
                        "param": "sl_band",
                        "old": f"{self.memory.optimal_params.get('min_sl_pct', 0.006)*100:.1f}-{self.memory.optimal_params.get('max_sl_pct', 0.012)*100:.1f}%",
                        "new": f"{optimal_min_sl*100:.1f}-{optimal_max_sl*100:.1f}%",
                        "reason": f"band {best_band} WR={best_sl[0][1]['wr']:.0%}",
                    })
                    self.memory.optimal_params["min_sl_pct"] = optimal_min_sl
                    self.memory.optimal_params["max_sl_pct"] = optimal_max_sl
            except (ValueError, IndexError):
                pass

        # Adaptation 4: Pattern weight update (COMPETENT+)
        if self.memory.evolution_stage in ("COMPETENT", "ADVANCED", "ELITE"):
            weight_changes = []
            for key, dna in self.memory.pattern_dna.items():
                if dna["wins"] + dna["losses"] >= 5:
                    old_w = dna.get("prev_weight", 1.0)
                    new_w = dna["weight"]
                    if abs(new_w - old_w) > 0.1:
                        weight_changes.append({"pattern": key, "old": old_w, "new": new_w})
                        dna["prev_weight"] = new_w
            if weight_changes:
                adaptations.append({
                    "param": "pattern_weights",
                    "changes": weight_changes[:10],
                    "reason": "Bayesian weight update",
                })

        # Adaptation 5: Min votes adjustment (ADVANCED+)
        if self.memory.evolution_stage in ("ADVANCED", "ELITE"):
            # If overall WR improving, can loosen slightly; if declining, tighten
            recent = list(self.memory.pattern_dna.values())
            overall_wr = sum(d["wins"] for d in recent) / max(sum(d["wins"] + d["losses"] for d in recent), 1)
            if overall_wr >= 0.45:
                optimal_votes = 4  # can afford to be slightly less strict
            elif overall_wr >= 0.35:
                optimal_votes = 5
            else:
                optimal_votes = 6  # very strict when losing
            if optimal_votes != self.memory.optimal_params.get("min_votes"):
                adaptations.append({
                    "param": "min_votes",
                    "old": self.memory.optimal_params.get("min_votes", 5),
                    "new": optimal_votes,
                    "reason": f"overall_wr={overall_wr:.0%}",
                })
                self.memory.optimal_params["min_votes"] = optimal_votes

        # Log adaptations
        if adaptations:
            self.memory.adaptations_log.append({
                "generation": self.memory.generation,
                "stage": self.memory.evolution_stage,
                "ts": datetime.now().isoformat(),
                "total_trades": self.memory.total_trades,
                "adaptations": adaptations,
            })
            self.memory.last_adapted = datetime.now().isoformat()
            log.info(f"[Swarm] GEN {self.memory.generation} | {len(adaptations)} adaptations | "
                     f"stage={self.memory.evolution_stage}")
            for a in adaptations:
                if "changes" not in a:
                    log.info(f"  {a['param']}: {a.get('old')} -> {a.get('new')} ({a['reason']})")

        return adaptations

    def get_optimal_params(self) -> Dict:
        """Return swarm-optimized parameters for config override."""
        return self.memory.optimal_params

    # ══════════════════════════════════════════════════════════════════════
    # COLLECTIVE QUERIES
    # ══════════════════════════════════════════════════════════════════════

    def get_pattern_weights(self) -> Dict[str, float]:
        """Return current pattern DNA weights for signal scoring."""
        return {k: v["weight"] for k, v in self.memory.pattern_dna.items()
                if v.get("wins", 0) + v.get("losses", 0) >= 3}

    def get_blacklisted_patterns(self) -> List[str]:
        """Patterns with WR <= 15% and n >= 5."""
        return [k for k, v in self.memory.pattern_dna.items()
                if v.get("wr", 1) <= 0.15 and v["wins"] + v["losses"] >= 5]

    def get_whitelisted_patterns(self) -> List[str]:
        """Patterns with WR >= 50% and n >= 5."""
        return [k for k, v in self.memory.pattern_dna.items()
                if v.get("wr", 0) >= 0.50 and v["wins"] + v["losses"] >= 5]

    def get_symbol_blacklist(self) -> List[str]:
        """Symbols with WR <= 15% and n >= 5."""
        return [s for s, v in self.memory.symbol_fitness.items()
                if v.get("wr", 1) <= 0.15 and v["wins"] + v["losses"] >= 5]

    def get_symbol_whitelist(self) -> List[str]:
        """Symbols with WR >= 50% and n >= 5."""
        return [s for s, v in self.memory.symbol_fitness.items()
                if v.get("wr", 0) >= 0.50 and v["wins"] + v["losses"] >= 5]

    def get_best_hours(self) -> List[int]:
        """Hours with WR >= 40%."""
        return [h for h, d in self.memory.time_memory.items()
                if d["wins"] + d["losses"] >= 5 and d.get("wr", 0) >= 0.40]

    def get_worst_hours(self) -> List[int]:
        """Hours with WR <= 20%."""
        return [h for h, d in self.memory.time_memory.items()
                if d["wins"] + d["losses"] >= 5 and d.get("wr", 0) <= 0.20]

    def get_status(self) -> Dict:
        """Full swarm status for dashboard/monitoring."""
        return {
            "stage": self.memory.evolution_stage,
            "generation": self.memory.generation,
            "total_trades": self.memory.total_trades,
            "streak": self.memory.streak,
            "max_streak": self.memory.max_streak,
            "cumulative_pnl_pct": round(self.memory.cumulative_pnl_pct, 2),
            "patterns_tracked": len(self.memory.pattern_dna),
            "symbols_tracked": len(self.memory.symbol_fitness),
            "last_adapted": self.memory.last_adapted,
            "best_patterns": sorted(
                [(k, v["wr"]) for k, v in self.memory.pattern_dna.items()
                 if v["wins"] + v["losses"] >= 5],
                key=lambda x: -x[1]
            )[:5],
            "worst_patterns": sorted(
                [(k, v["wr"]) for k, v in self.memory.pattern_dna.items()
                 if v["wins"] + v["losses"] >= 5],
                key=lambda x: x[1]
            )[:5],
        }

    # ══════════════════════════════════════════════════════════════════════
    # BOOTSTRAP: Initialize from existing journal
    # ══════════════════════════════════════════════════════════════════════

    def bootstrap_from_journal(self):
        """Load all historical trades and learn from them. Run once on first start."""
        if self.memory.total_trades > 0:
            return  # already bootstrapped

        try:
            if not os.path.exists(JOURNAL_PATH):
                return
            with open(JOURNAL_PATH) as f:
                for line in f:
                    try:
                        trade = json.loads(line)
                        if trade.get("outcome") in ("TARGET_HIT", "SL_HIT"):
                            self.learn_from_trade(trade)
                    except Exception:
                        continue
            log.info(f"[Swarm] bootstrapped from journal: {self.memory.total_trades} trades, "
                     f"stage={self.memory.evolution_stage}")
        except Exception as e:
            log.error(f"[Swarm] bootstrap failed: {e}")

    # ══════════════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════════════

    def _update_stage(self):
        n = self.memory.total_trades
        for stage, (lo, hi) in STAGES.items():
            if lo <= n < hi:
                self.memory.evolution_stage = stage
                break

    def _extract_hour(self, ts: str) -> Optional[int]:
        try:
            if len(ts) >= 13:
                return int(ts[11:13])
        except (ValueError, TypeError):
            pass
        return None

    def _rsi_zone(self, rsi: float) -> str:
        if rsi < 30:
            return "0-30"
        elif rsi < 40:
            return "30-40"
        elif rsi < 50:
            return "40-50"
        elif rsi < 60:
            return "50-60"
        elif rsi < 70:
            return "60-70"
        elif rsi < 80:
            return "70-80"
        else:
            return "80-100"

    def _sl_band(self, sl_pct: float) -> str:
        if sl_pct < 0.3:
            return "0-0.3"
        elif sl_pct < 0.5:
            return "0.3-0.5"
        elif sl_pct < 0.8:
            return "0.5-0.8"
        elif sl_pct < 1.0:
            return "0.8-1.0"
        elif sl_pct < 1.5:
            return "1.0-1.5"
        else:
            return "1.5+"


# Singleton instance
_swarm: Optional[SwarmIntelligence] = None


def get_swarm() -> SwarmIntelligence:
    global _swarm
    if _swarm is None:
        _swarm = SwarmIntelligence()
        _swarm.bootstrap_from_journal()
    return _swarm
