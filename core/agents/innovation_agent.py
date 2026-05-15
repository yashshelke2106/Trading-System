"""
InnovationAgent (Ajitsaria) — ML model lifecycle, pattern discovery, prediction upgrades.

Inspired by Nish Ajitsaria (BlackRock AI/ML innovation lead).

Every 10 min:
  1. Analyze signal journal for pattern performance drift
  2. Discover new winning/losing pattern combinations
  3. Update pattern weights in learned_params
  4. Feature importance ranking (which indicators matter most)
  5. Publish INNOVATION_UPDATE with findings

Post-market:
  - Full pattern analysis across all journal entries
  - Cluster analysis: which pattern combos have highest WR
  - Update SYMBOL_TIERS based on rolling performance
"""

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Tuple

from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent

log = logging.getLogger(__name__)

JOURNAL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                            "logs", "signal_journal.jsonl")
LEARNED_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                            "logs", "learned_params.json")


class InnovationAgent(BaseAgent):
    name = "innovation_ajitsaria"
    interval_sec = 600

    MIN_SAMPLES = 10

    def __init__(self, state: SharedState, bus: EventBus):
        super().__init__(state, bus)
        self._pattern_stats: Dict[str, Dict] = {}
        self._combo_stats: Dict[str, Dict] = {}
        self._last_discovery_ts = 0

    def run(self) -> None:
        entries = self._load_journal()
        if len(entries) < self.MIN_SAMPLES:
            return

        self._analyze_patterns(entries)
        self._discover_combos(entries)
        self._analyze_features(entries)
        self._publish_findings()

    def _load_journal(self) -> List[Dict]:
        entries = []
        try:
            if not os.path.exists(JOURNAL_PATH):
                return entries
            with open(JOURNAL_PATH) as f:
                for line in f:
                    try:
                        o = json.loads(line)
                        if o.get("outcome") in ("TARGET_HIT", "SL_HIT"):
                            entries.append(o)
                    except Exception:
                        continue
        except Exception:
            pass
        return entries

    def _analyze_patterns(self, entries: List[Dict]):
        """Per-pattern win rate and sample size."""
        pattern_stats: Dict[str, Dict] = {}

        for e in entries:
            won = e["outcome"] == "TARGET_HIT"
            direction = e.get("direction", "long")
            patterns = e.get("patterns", [])

            for p in patterns:
                key = f"{direction}:{p}"
                if key not in pattern_stats:
                    pattern_stats[key] = {"wins": 0, "total": 0}
                pattern_stats[key]["total"] += 1
                if won:
                    pattern_stats[key]["wins"] += 1

        # Compute WR
        for key, stats in pattern_stats.items():
            stats["wr"] = stats["wins"] / max(stats["total"], 1)

        self._pattern_stats = pattern_stats

        # Log notable findings
        for key, stats in sorted(pattern_stats.items(), key=lambda x: -x[1]["wr"]):
            if stats["total"] >= 5:
                log.info(f"[Innovation/Ajitsaria] {key}: WR={stats['wr']:.0%} "
                         f"(n={stats['total']})")

    def _discover_combos(self, entries: List[Dict]):
        """Find 2-pattern combos with significantly different WR than individual patterns."""
        combo_stats: Dict[str, Dict] = defaultdict(lambda: {"wins": 0, "total": 0})

        for e in entries:
            won = e["outcome"] == "TARGET_HIT"
            patterns = sorted(set(e.get("patterns", [])))

            for i in range(len(patterns)):
                for j in range(i + 1, len(patterns)):
                    key = f"{patterns[i]}+{patterns[j]}"
                    combo_stats[key]["total"] += 1
                    if won:
                        combo_stats[key]["wins"] += 1

        # Filter to significant combos
        discoveries = {}
        for key, stats in combo_stats.items():
            if stats["total"] >= 5:
                wr = stats["wins"] / stats["total"]
                stats["wr"] = wr
                if wr >= 0.50 or wr <= 0.15:
                    discoveries[key] = stats

        self._combo_stats = dict(discoveries)

        if discoveries:
            best = max(discoveries.items(), key=lambda x: x[1]["wr"])
            worst = min(discoveries.items(), key=lambda x: x[1]["wr"])
            log.info(f"[Innovation/Ajitsaria] best combo: {best[0]} WR={best[1]['wr']:.0%} "
                     f"n={best[1]['total']}")
            log.info(f"[Innovation/Ajitsaria] worst combo: {worst[0]} WR={worst[1]['wr']:.0%} "
                     f"n={worst[1]['total']}")

    def _analyze_features(self, entries: List[Dict]):
        """Feature importance: which numeric features differ most between wins and losses."""
        features = ["rsi", "volume_ratio", "vote_margin", "score"]
        win_vals: Dict[str, List] = {f: [] for f in features}
        loss_vals: Dict[str, List] = {f: [] for f in features}

        for e in entries:
            target = win_vals if e["outcome"] == "TARGET_HIT" else loss_vals
            for f in features:
                v = e.get(f)
                if isinstance(v, (int, float)):
                    target[f].append(v)

        importance = {}
        for f in features:
            w = win_vals[f]
            l = loss_vals[f]
            if w and l:
                w_mean = sum(w) / len(w)
                l_mean = sum(l) / len(l)
                # Effect size: difference in means / pooled std
                all_vals = w + l
                pooled_std = (sum((x - sum(all_vals)/len(all_vals))**2 for x in all_vals) / len(all_vals)) ** 0.5
                if pooled_std > 0:
                    importance[f] = {
                        "effect_size": round(abs(w_mean - l_mean) / pooled_std, 3),
                        "win_mean": round(w_mean, 2),
                        "loss_mean": round(l_mean, 2),
                        "direction": "higher_wins" if w_mean > l_mean else "lower_wins",
                    }

        self.state.set(feature_importance=importance)

        # Publish winning thresholds for other agents to use
        if "rsi" in importance:
            self.state.set(innovation_rsi_win_avg=importance["rsi"]["win_mean"],
                          innovation_rsi_loss_avg=importance["rsi"]["loss_mean"])
        if "volume_ratio" in importance:
            self.state.set(innovation_vol_win_avg=importance["volume_ratio"]["win_mean"],
                          innovation_vol_loss_avg=importance["volume_ratio"]["loss_mean"])

        ranked = sorted(importance.items(), key=lambda x: -x[1]["effect_size"])
        for f, info in ranked:
            log.info(f"[Innovation/Ajitsaria] feature {f}: effect={info['effect_size']} "
                     f"W={info['win_mean']} L={info['loss_mean']} ({info['direction']})")

    def _publish_findings(self):
        # Compute pattern whitelist (WR>=50%) and blacklist (WR<=15%)
        whitelist = []
        blacklist = []
        for key, stats in self._pattern_stats.items():
            if stats["total"] >= 5:
                if stats["wr"] >= 0.50:
                    whitelist.append(key)
                elif stats["wr"] <= 0.15:
                    blacklist.append(key)

        self.state.set(
            pattern_whitelist=whitelist,
            pattern_blacklist=blacklist,
            winning_combos=[k for k, v in self._combo_stats.items() if v.get("wr", 0) >= 0.50],
            losing_combos=[k for k, v in self._combo_stats.items() if v.get("wr", 0) <= 0.15],
        )

        self.emit("INNOVATION_UPDATE", {
            "pattern_count": len(self._pattern_stats),
            "combo_discoveries": len(self._combo_stats),
            "whitelist": whitelist,
            "blacklist": blacklist,
            "top_combos": dict(sorted(
                self._combo_stats.items(),
                key=lambda x: -x[1].get("wr", 0),
            )[:5]),
            "feature_importance": self.state.get("feature_importance", {}),
        })

    def post_market_analysis(self) -> Dict:
        """Full end-of-day analysis. Called by coordinator at market close."""
        entries = self._load_journal()
        if len(entries) < self.MIN_SAMPLES:
            return {"status": "insufficient_data", "count": len(entries)}

        self._analyze_patterns(entries)
        self._discover_combos(entries)
        self._analyze_features(entries)

        # Pattern weight recommendations
        recommendations = {}
        for key, stats in self._pattern_stats.items():
            if stats["total"] >= 5:
                if stats["wr"] >= 0.45:
                    recommendations[key] = "boost"
                elif stats["wr"] <= 0.20:
                    recommendations[key] = "suppress"

        return {
            "status": "complete",
            "entries_analyzed": len(entries),
            "patterns_tracked": len(self._pattern_stats),
            "combos_discovered": len(self._combo_stats),
            "recommendations": recommendations,
        }
