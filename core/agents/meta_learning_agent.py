"""
MetaLearningAgent — Self-improving parameter tuner using trade analytics.

Runs every 10 minutes during market hours + once post-market.
Analyzes trade outcomes to auto-tune system parameters.

Learning loop:
  1. Compute trade analytics (pattern WR, hour profiling, RSI zones)
  2. Compare current params against optimal from data
  3. Propose bounded adjustments (max ±1 step per cycle)
  4. Write to learned_params.json (consumed by SignalEngine on next reload)
  5. Publish insights to SharedState (consumed by Streamlit / Next.js)

Safety:
  - Minimum 20 trades before any adaptation
  - Max ±1 step per parameter per cycle
  - Hard bounds from TUNABLE_PARAMS
  - All changes logged to param_changes.jsonl
  - Rollback: if win rate drops >10pp in 10 trades, revert last batch
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent
from core.agentic_rag import (
    AgenticRAG,
    TradeAnalytics,
    compute_trade_analytics,
    get_agentic_rag,
    _load_trades_df,
)

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEARNED_PARAMS_FILE = os.path.join(_PROJECT_ROOT, "logs", "learned_params.json")
PARAM_CHANGES_FILE = os.path.join(_PROJECT_ROOT, "logs", "param_changes.jsonl")

MIN_TRADES_FOR_TUNING = 20
MAX_STEPS_PER_CYCLE = 1


class MetaLearningAgent(BaseAgent):
    name = "meta_learning"
    interval_sec = 600  # 10 minutes

    def __init__(self, state: SharedState, bus: EventBus):
        super().__init__(state, bus)
        self.rag = get_agentic_rag()
        self._last_trade_count = 0
        self._last_wr = None
        self._changes_this_session: List[Dict] = []

        bus.subscribe("TRADE_CLOSED", self._on_trade_closed)
        bus.subscribe("POST_MARKET_DONE", self._on_post_market)

    def run(self) -> None:
        """Periodic: compute analytics, publish insights, tune params."""
        analytics = self.rag.get_analytics(days=30)

        # Publish insights to SharedState for UI consumption
        self._publish_insights(analytics)

        # Auto-tune if enough trades
        if analytics.total_trades >= MIN_TRADES_FOR_TUNING:
            self._auto_tune(analytics)

        # Rollback check
        self._check_rollback(analytics)

    def _on_trade_closed(self, event: AgentEvent) -> None:
        """React to each trade close — check if new analytics needed."""
        # Invalidate analytics cache so next run() picks up fresh data
        self.rag._analytics_cache = None

    def _on_post_market(self, event: AgentEvent) -> None:
        """Post-market: deep analysis + tune."""
        log.info("[MetaLearning] Post-market deep analysis...")
        # Force fresh analytics
        self.rag._analytics_cache = None
        analytics = compute_trade_analytics(days=90)
        self._publish_insights(analytics)

        if analytics.total_trades >= MIN_TRADES_FOR_TUNING:
            self._auto_tune(analytics)

        # Generate full insights report
        insights = self.rag.generate_insights()
        self.state.set(meta_insights=insights)
        log.info(f"[MetaLearning] Post-market report: "
                 f"trades={analytics.total_trades} WR={analytics.win_rate:.1f}% "
                 f"PF={analytics.profit_factor:.2f}")

    def _publish_insights(self, analytics: TradeAnalytics) -> None:
        """Push analytics to SharedState for UI."""
        self.state.set(
            meta_win_rate=round(analytics.win_rate, 1),
            meta_total_pnl=round(analytics.total_pnl, 2),
            meta_profit_factor=round(analytics.profit_factor, 2),
            meta_expectancy=round(analytics.expectancy, 2),
            meta_total_trades=analytics.total_trades,
            meta_streak=f"{analytics.streak_type}{analytics.recent_streak}",
            meta_recommendations=analytics.recommendations[:5],
            meta_best_patterns=[
                {"pattern": p.pattern, "wr": round(p.win_rate, 1), "n": p.total}
                for p in analytics.best_patterns[:5]
            ],
            meta_worst_patterns=[
                {"pattern": p.pattern, "wr": round(p.win_rate, 1), "n": p.total}
                for p in analytics.worst_patterns[:5]
            ],
            meta_best_hours=[
                {"hour": h.hour, "wr": round(h.win_rate, 1), "n": h.total}
                for h in analytics.best_hours[:3]
            ],
        )

    def _auto_tune(self, analytics: TradeAnalytics) -> None:
        """RAG-powered parameter tuning.

        Uses AgenticRAG to retrieve relevant trade context, then makes
        bounded hill-climbing decisions based on structured analytics +
        semantic retrieval evidence.

        Tuning dimensions:
          1. RSI floors (long + short, per-direction from RAG)
          2. Time filters (block_after_hour from hour WR)
          3. Vote thresholds (min_votes when WR low)
          4. SL band (max_sl_pct when loss >> win)
          5. Pattern weights (per-direction from RAG trade retrieval)
        """
        from core.adaptive_learner import TUNABLE_PARAMS
        import config as cfg

        changes = []

        # ── RAG retrieval: get direction-specific insights ───────────────
        long_evidence = self.rag.search("long trades win rate patterns", top_k=10)
        short_evidence = self.rag.search("short trades win rate patterns", top_k=10)
        rsi_evidence = self.rag.search("RSI zone win rate momentum oversold overbought", top_k=5)
        time_evidence = self.rag.search("trading hour performance worst hours", top_k=5)

        # ── Per-direction analytics from trades ──────────────────────────
        df = _load_trades_df()
        dir_stats = {"long": {"wins": 0, "total": 0}, "short": {"wins": 0, "total": 0}}
        if not df.empty and "direction" in df.columns:
            for _, row in df.iterrows():
                d = str(row.get("direction", "")).lower()
                if d in dir_stats:
                    dir_stats[d]["total"] += 1
                    if row.get("status") == "WIN":
                        dir_stats[d]["wins"] += 1

        long_wr = (dir_stats["long"]["wins"] / dir_stats["long"]["total"] * 100
                   if dir_stats["long"]["total"] >= 5 else None)
        short_wr = (dir_stats["short"]["wins"] / dir_stats["short"]["total"] * 100
                    if dir_stats["short"]["total"] >= 5 else None)

        # Publish per-direction WR to SharedState for UI
        self.state.set(
            meta_long_wr=round(long_wr, 1) if long_wr is not None else None,
            meta_short_wr=round(short_wr, 1) if short_wr is not None else None,
            meta_long_trades=dir_stats["long"]["total"],
            meta_short_trades=dir_stats["short"]["total"],
        )

        # ── 1. RSI long momentum floor (RAG-informed) ───────────────────
        if not df.empty and "patterns_combined" in df.columns:
            rsi_data = {"high_wr": 0, "low_wr": 0, "high_n": 0, "low_n": 0}
            for _, row in df.iterrows():
                pats = str(row.get("patterns_combined", ""))
                status = row.get("status", "")
                if "rsi_momentum_zone" in pats or "rsi_bullish_zone" in pats:
                    rsi_data["high_n"] += 1
                    if status == "WIN":
                        rsi_data["high_wr"] += 1
                elif "rsi_bearish_zone" in pats or "rsi_oversold" in pats:
                    rsi_data["low_n"] += 1
                    if status == "WIN":
                        rsi_data["low_wr"] += 1

            if rsi_data["high_n"] >= 5 and rsi_data["low_n"] >= 5:
                high_wr = rsi_data["high_wr"] / rsi_data["high_n"] * 100
                low_wr = rsi_data["low_wr"] / rsi_data["low_n"] * 100
                current_floor = cfg.SIGNAL_CONFIG.get("rsi_long_momentum_min", 60)
                _, lo, hi, step, section = TUNABLE_PARAMS.get(
                    "rsi_long_momentum_min", (60, 55, 68, 2, "SIGNAL_CONFIG")
                )
                rag_context = f" (RAG: {len(rsi_evidence)} docs retrieved)"
                if high_wr > low_wr + 15 and current_floor < hi:
                    new_val = min(current_floor + step, hi)
                    changes.append(("rsi_long_momentum_min", current_floor, new_val, section,
                                    f"high RSI WR={high_wr:.0f}% >> low RSI WR={low_wr:.0f}%{rag_context}"))
                elif low_wr > high_wr + 10 and current_floor > lo:
                    new_val = max(current_floor - step, lo)
                    changes.append(("rsi_long_momentum_min", current_floor, new_val, section,
                                    f"low RSI WR={low_wr:.0f}% > high RSI WR={high_wr:.0f}%{rag_context}"))

        # ── 2. RSI short floor (symmetric, RAG-informed) ────────────────
        if not df.empty and "patterns_combined" in df.columns and "rsi_short_floor" in TUNABLE_PARAMS:
            short_rsi_data = {"high_wr": 0, "low_wr": 0, "high_n": 0, "low_n": 0}
            for _, row in df.iterrows():
                if str(row.get("direction", "")).lower() != "short":
                    continue
                rsi_val = float(row.get("rsi", 50) or 50)
                status = row.get("status", "")
                if rsi_val >= 55:
                    short_rsi_data["high_n"] += 1
                    if status == "WIN":
                        short_rsi_data["high_wr"] += 1
                elif rsi_val < 45:
                    short_rsi_data["low_n"] += 1
                    if status == "WIN":
                        short_rsi_data["low_wr"] += 1

            if short_rsi_data["high_n"] >= 3 and short_rsi_data["low_n"] >= 3:
                s_high_wr = short_rsi_data["high_wr"] / short_rsi_data["high_n"] * 100
                s_low_wr = short_rsi_data["low_wr"] / short_rsi_data["low_n"] * 100
                current_sf = cfg.SIGNAL_CONFIG.get("rsi_short_floor", 50)
                _, lo, hi, step, section = TUNABLE_PARAMS["rsi_short_floor"]
                # If shorting oversold loses more → raise floor
                if s_low_wr < s_high_wr - 15 and current_sf < hi:
                    new_val = min(current_sf + step, hi)
                    changes.append(("rsi_short_floor", current_sf, new_val, section,
                                    f"short oversold WR={s_low_wr:.0f}% << high RSI WR={s_high_wr:.0f}%"))

        # ── 3. Block after hour (RAG time evidence) ─────────────────────
        if analytics.worst_hours:
            worst = analytics.worst_hours[0]
            if worst.win_rate < 15 and worst.total >= 5:
                current_block = cfg.SIGNAL_CONFIG.get("block_after_hour", 12)
                if worst.hour < current_block and "block_after_hour" in TUNABLE_PARAMS:
                    _, lo, hi, step, section = TUNABLE_PARAMS["block_after_hour"]
                    new_val = max(worst.hour, lo)
                    if new_val != current_block:
                        rag_ctx = f" (RAG: {len(time_evidence)} time docs)"
                        changes.append(("block_after_hour", current_block, new_val, section,
                                        f"hour {worst.hour} WR={worst.win_rate:.0f}% (n={worst.total}){rag_ctx}"))

        # ── 4. Min votes (when WR too low) ──────────────────────────────
        if analytics.win_rate < 30 and analytics.total_trades >= 30:
            current_votes = cfg.SIGNAL_CONFIG.get("min_votes", 5)
            _, lo, hi, step, section = TUNABLE_PARAMS.get(
                "min_votes", (3, 3, 4, 1, "SIGNAL_CONFIG")
            )
            if current_votes < hi:
                changes.append(("min_votes", current_votes, current_votes + step, section,
                                f"WR={analytics.win_rate:.0f}% too low, raising filter"))

        # ── 5. SL band (loss magnitude analysis) ────────────────────────
        if analytics.avg_loss != 0 and analytics.avg_win != 0:
            loss_mag = abs(analytics.avg_loss)
            win_mag = abs(analytics.avg_win)
            if loss_mag > win_mag * 2:
                current_max_sl = cfg.RISK_CONFIG.get("max_sl_pct", 0.012)
                if "max_sl_pct" in TUNABLE_PARAMS:
                    _, lo, hi, step, section = TUNABLE_PARAMS["max_sl_pct"]
                    if current_max_sl > lo:
                        new_val = max(current_max_sl - step, lo)
                        changes.append(("max_sl_pct", current_max_sl, new_val, section,
                                        f"avg loss ₹{loss_mag:.0f} >> avg win ₹{win_mag:.0f}"))

        # ── 6. Pattern weight learning via RAG ──────────────────────────
        self._learn_pattern_weights(df, long_evidence, short_evidence)

        # Apply changes (max 3 per cycle)
        if changes:
            self._apply_changes(changes[:3])

    def _learn_pattern_weights(self, df, long_evidence, short_evidence) -> None:
        """Use RAG retrieval to learn per-direction pattern weights.

        Reads trade outcomes, computes per-pattern win rate per direction,
        writes to PATTERN_WEIGHTS in learned_params.json.
        SignalEngine._grade() reads these weights to boost/dampen signals.
        """
        if df.empty or "patterns_combined" not in df.columns:
            return

        pattern_dir_stats = {}  # "long:pattern" → {wins, total}
        for _, row in df.iterrows():
            d = str(row.get("direction", "")).lower()
            if d not in ("long", "short"):
                continue
            pats_raw = str(row.get("patterns_combined", ""))
            pats = [p.strip() for p in pats_raw.split("|") if p.strip()]
            status = str(row.get("status", "")).upper()
            for pat in pats:
                if pat.startswith("vol_"):
                    continue
                key = f"{d}:{pat}"
                if key not in pattern_dir_stats:
                    pattern_dir_stats[key] = {"wins": 0, "total": 0}
                pattern_dir_stats[key]["total"] += 1
                if status == "WIN":
                    pattern_dir_stats[key]["wins"] += 1

        # Per-direction baseline — prevents long-tilted dataset from collapsing
        # short pattern weights. Each direction normalized against ITS OWN base WR.
        long_df  = df[df["direction"].str.lower() == "long"]  if "direction" in df.columns else df.iloc[0:0]
        short_df = df[df["direction"].str.lower() == "short"] if "direction" in df.columns else df.iloc[0:0]
        long_wins  = len(long_df[long_df["status"] == "WIN"])  if "status" in long_df.columns  and len(long_df)  else 0
        short_wins = len(short_df[short_df["status"] == "WIN"]) if "status" in short_df.columns and len(short_df) else 0
        long_base  = long_wins  / len(long_df)  if len(long_df)  else 0.5
        short_base = short_wins / len(short_df) if len(short_df) else 0.5

        # Skip whole-direction update when win sample too thin (mirrors
        # adaptive_learner MIN_DIR_WINS) — leaves existing weights untouched.
        MIN_DIR_WINS = 2
        long_ok  = long_wins  >= MIN_DIR_WINS
        short_ok = short_wins >= MIN_DIR_WINS

        weights = {}
        for key, stats in pattern_dir_stats.items():
            if stats["total"] < 3:
                continue
            direction = key.split(":", 1)[0]
            if direction == "long" and not long_ok:
                continue
            if direction == "short" and not short_ok:
                continue
            base = long_base if direction == "long" else short_base
            if base <= 0:
                continue
            pat_wr = stats["wins"] / stats["total"]
            w = pat_wr / base
            # Floor 0.5 (was 0.3): full suppression below 0.3 is too aggressive
            # given small short-direction samples.
            weights[key] = round(max(0.5, min(2.0, w)), 3)

        if not weights:
            return

        # Write to learned_params.json under PATTERN_WEIGHTS
        # Must use {"updated_at": ..., "params": {...}} format (see _apply_changes docstring)
        try:
            data = {}
            if os.path.exists(LEARNED_PARAMS_FILE):
                with open(LEARNED_PARAMS_FILE, encoding="utf-8") as f:
                    data = json.load(f)

            if "params" not in data:
                params = {k: v for k, v in data.items()
                          if k not in ("updated_at",) and isinstance(v, dict)}
                data = {"params": params}

            # MERGE (don't replace) — preserves weights for patterns not seen
            # in this batch (e.g., short patterns with no recent trades).
            existing_pw = data["params"].get("PATTERN_WEIGHTS", {}) or {}
            existing_pw.update(weights)
            data["params"]["PATTERN_WEIGHTS"] = existing_pw
            data["updated_at"] = datetime.now().isoformat()

            with open(LEARNED_PARAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            log.info(f"[MetaLearning] Updated {len(weights)} pattern weights via RAG")

            # Publish top/bottom for UI
            sorted_w = sorted(weights.items(), key=lambda x: x[1], reverse=True)
            self.state.set(
                meta_top_patterns=[{"key": k, "weight": v} for k, v in sorted_w[:5]],
                meta_bottom_patterns=[{"key": k, "weight": v} for k, v in sorted_w[-5:]],
            )
        except Exception as e:
            log.error(f"[MetaLearning] pattern weight write failed: {e}")

    def _apply_changes(self, changes: List) -> None:
        """Write parameter changes to learned_params.json + log.

        CRITICAL: Must use {"updated_at": ..., "params": {...}} format
        because AdaptiveLearner._load_learned_params() reads data.get("params", {}).
        Writing raw sections at top level = AdaptiveLearner reads empty dict = params lost.
        """
        try:
            data = {}
            if os.path.exists(LEARNED_PARAMS_FILE):
                with open(LEARNED_PARAMS_FILE, encoding="utf-8") as f:
                    data = json.load(f)

            # Handle legacy format: if "params" key missing, wrap existing data
            if "params" not in data:
                # Old format: sections at top level. Migrate.
                params = {k: v for k, v in data.items()
                          if k not in ("updated_at",) and isinstance(v, dict)}
                data = {"params": params}
            params = data["params"]

            applied = []
            for param, old_val, new_val, section, reason in changes:
                if section not in params:
                    params[section] = {}
                params[section][param] = new_val
                applied.append({
                    "param": param,
                    "old": old_val,
                    "new": new_val,
                    "section": section,
                    "reason": reason,
                    "ts": datetime.now().isoformat(),
                })
                log.info(f"[MetaLearning] TUNE {param}: {old_val} → {new_val} ({reason})")

            data["updated_at"] = datetime.now().isoformat()

            # Write with correct format
            os.makedirs(os.path.dirname(LEARNED_PARAMS_FILE), exist_ok=True)
            with open(LEARNED_PARAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            # Append to change log
            with open(PARAM_CHANGES_FILE, "a", encoding="utf-8") as f:
                for entry in applied:
                    f.write(json.dumps(entry) + "\n")

            self._changes_this_session.extend(applied)

            # Emit event so other agents can react
            self.emit("META_TUNED", {
                "changes": applied,
                "total_changes_session": len(self._changes_this_session),
            })

        except Exception as e:
            log.error(f"[MetaLearning] apply changes failed: {e}")

    def _check_rollback(self, analytics: TradeAnalytics) -> None:
        """If win rate dropped >10pp since last tune, revert."""
        if self._last_wr is None:
            self._last_wr = analytics.win_rate
            return

        if not self._changes_this_session:
            self._last_wr = analytics.win_rate
            return

        drop = self._last_wr - analytics.win_rate
        if drop > 10 and analytics.total_trades - self._last_trade_count >= 10:
            log.warning(f"[MetaLearning] ROLLBACK: WR dropped {drop:.0f}pp "
                        f"({self._last_wr:.0f}% → {analytics.win_rate:.0f}%)")

            # Revert last batch of changes
            try:
                data = {}
                if os.path.exists(LEARNED_PARAMS_FILE):
                    with open(LEARNED_PARAMS_FILE, encoding="utf-8") as f:
                        data = json.load(f)

                params = data.get("params", data)  # handle both formats

                for change in self._changes_this_session[-3:]:
                    section = change["section"]
                    param = change["param"]
                    if section in params and param in params[section]:
                        params[section][param] = change["old"]
                        log.info(f"[MetaLearning] REVERTED {param}: {change['new']} → {change['old']}")

                data["params"] = params
                data["updated_at"] = datetime.now().isoformat()
                with open(LEARNED_PARAMS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)

                self._changes_this_session.clear()
                self.emit("META_ROLLBACK", {
                    "reason": f"WR dropped {drop:.0f}pp",
                    "reverted_params": len(self._changes_this_session),
                })

            except Exception as e:
                log.error(f"[MetaLearning] rollback failed: {e}")

        self._last_wr = analytics.win_rate
        self._last_trade_count = analytics.total_trades

    def post_market_analysis(self) -> Dict:
        """Called by CoordinatorAgent during post-market sequence."""
        analytics = compute_trade_analytics(days=30)
        insights = self.rag.generate_insights()
        return {
            "status": "ok",
            "analytics": {
                "total_trades": analytics.total_trades,
                "win_rate": round(analytics.win_rate, 1),
                "profit_factor": round(analytics.profit_factor, 2),
                "recommendations": analytics.recommendations,
            },
            "insights": insights,
        }
