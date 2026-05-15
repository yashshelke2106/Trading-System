"""
AgenticRAG — Enhanced RAG engine with semantic search + structured trade analytics.

Layers:
  1. Semantic corpus: signals, trades, state, strategy docs
  2. Structured analytics: pattern WR, hour profiling, RSI zones, regime stats
  3. Query engine: TF-IDF (default) or sentence-transformers (if installed)
  4. Insight generator: auto-generates actionable recommendations from data

Used by:
  - FastAPI /api/ask (interactive query)
  - FastAPI /api/insights (auto-brief)
  - MetaLearningAgent (periodic self-analysis)
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_CSV = os.path.join(_PROJECT_ROOT, "logs", "trades.csv")
SIGNALS_JSON = os.path.join(_PROJECT_ROOT, "logs", "signals.json")
JOURNAL_FILE = os.path.join(_PROJECT_ROOT, "logs", "signal_journal.jsonl")
LEARNED_PARAMS = os.path.join(_PROJECT_ROOT, "logs", "learned_params.json")
SWARM_MEMORY = os.path.join(_PROJECT_ROOT, "logs", "swarm_memory.json")
STATE_FILE = os.path.join(_PROJECT_ROOT, "logs", "state.json")

# Lazy embedder — only loaded if sentence-transformers available
_EMBEDDER = None
_EMBEDDER_CHECKED = False


def _get_embedder():
    """Load sentence-transformer model once. Returns None if unavailable."""
    global _EMBEDDER, _EMBEDDER_CHECKED
    if _EMBEDDER_CHECKED:
        return _EMBEDDER
    _EMBEDDER_CHECKED = True
    try:
        from sentence_transformers import SentenceTransformer
        _EMBEDDER = SentenceTransformer("all-MiniLM-L6-v2")
        log.info("[AgenticRAG] Semantic embedder loaded (all-MiniLM-L6-v2)")
    except ImportError:
        log.info("[AgenticRAG] sentence-transformers not installed — using TF-IDF fallback")
    except Exception as e:
        log.warning(f"[AgenticRAG] embedder load failed: {e}")
    return _EMBEDDER


# ── Data loading helpers ────────────────────────────────────────────────────

def _load_trades_df() -> pd.DataFrame:
    if not os.path.exists(TRADES_CSV):
        return pd.DataFrame()
    try:
        df = pd.read_csv(TRADES_CSV)
        if df.empty:
            return df
        for col in ("entry_price", "exit_price", "pnl", "pnl_percent", "quantity"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        if "status" in df.columns:
            df["status"] = df["status"].astype(str).str.upper()
        if "direction" in df.columns:
            df["direction"] = df["direction"].astype(str).str.upper()
        return df
    except Exception:
        return pd.DataFrame()


def _load_signals() -> List[Dict]:
    if not os.path.exists(SIGNALS_JSON):
        return []
    try:
        with open(SIGNALS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("signals", [])
    except Exception:
        return []


def _load_journal(days: int = 90) -> List[Dict]:
    if not os.path.exists(JOURNAL_FILE):
        return []
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    records = []
    try:
        with open(JOURNAL_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("ts", "") >= cutoff:
                        records.append(r)
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return records


def _load_json(path: str) -> Dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ── Structured Analytics ────────────────────────────────────────────────────

@dataclass
class PatternStats:
    pattern: str
    total: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_pnl_pct: float = 0.0
    best_pnl: float = 0.0
    worst_pnl: float = 0.0


@dataclass
class HourStats:
    hour: int
    total: int = 0
    wins: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0


@dataclass
class TradeAnalytics:
    """Full structured analysis of trade history."""
    total_trades: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    best_patterns: List[PatternStats] = field(default_factory=list)
    worst_patterns: List[PatternStats] = field(default_factory=list)
    best_hours: List[HourStats] = field(default_factory=list)
    worst_hours: List[HourStats] = field(default_factory=list)
    regime_stats: Dict[str, Dict] = field(default_factory=dict)
    rsi_zones: Dict[str, Dict] = field(default_factory=dict)
    recent_streak: int = 0
    streak_type: str = ""
    recommendations: List[str] = field(default_factory=list)


def compute_trade_analytics(days: int = 90) -> TradeAnalytics:
    """Compute structured analytics from trade history."""
    df = _load_trades_df()
    if df.empty:
        return TradeAnalytics()

    # Filter by time
    if "timestamp" in df.columns and days:
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
        df = df[df["timestamp"] >= cutoff]

    if df.empty:
        return TradeAnalytics()

    result = TradeAnalytics()
    result.total_trades = len(df)
    result.total_pnl = float(df["pnl"].sum())

    wins = df[df["status"] == "WIN"]
    losses = df[df["status"] == "LOSS"]

    result.win_rate = len(wins) / len(df) * 100 if len(df) > 0 else 0
    result.avg_win = float(wins["pnl"].mean()) if not wins.empty else 0
    result.avg_loss = float(losses["pnl"].mean()) if not losses.empty else 0

    gross_win = float(wins["pnl"].sum()) if not wins.empty else 0
    gross_loss = abs(float(losses["pnl"].sum())) if not losses.empty else 0
    result.profit_factor = gross_win / gross_loss if gross_loss > 0 else 0
    result.expectancy = result.total_pnl / result.total_trades if result.total_trades > 0 else 0

    # Pattern analysis — extract from patterns_combined column
    pattern_data: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "pnls": []}
    )
    if "patterns_combined" in df.columns:
        for _, row in df.iterrows():
            pats_raw = str(row.get("patterns_combined", ""))
            pats = [p.strip() for p in pats_raw.split("|") if p.strip()]
            status = row.get("status", "")
            pnl_pct = float(row.get("pnl_percent", 0) or 0)
            for pat in pats:
                # Skip vol_Xx patterns (noise)
                if pat.startswith("vol_"):
                    continue
                if status == "WIN":
                    pattern_data[pat]["wins"] += 1
                elif status == "LOSS":
                    pattern_data[pat]["losses"] += 1
                pattern_data[pat]["pnls"].append(pnl_pct)

    all_patterns = []
    for pat, data in pattern_data.items():
        total = data["wins"] + data["losses"]
        if total < 3:
            continue
        pnls = data["pnls"]
        ps = PatternStats(
            pattern=pat,
            total=total,
            wins=data["wins"],
            losses=data["losses"],
            win_rate=data["wins"] / total * 100 if total > 0 else 0,
            avg_pnl_pct=float(np.mean(pnls)) if pnls else 0,
            best_pnl=float(max(pnls)) if pnls else 0,
            worst_pnl=float(min(pnls)) if pnls else 0,
        )
        all_patterns.append(ps)

    all_patterns.sort(key=lambda x: x.win_rate, reverse=True)
    result.best_patterns = all_patterns[:10]
    result.worst_patterns = sorted(all_patterns, key=lambda x: x.win_rate)[:10]

    # Hour analysis
    if "timestamp" in df.columns:
        df["_hour"] = df["timestamp"].dt.hour
        hour_data: Dict[int, Dict] = defaultdict(lambda: {"wins": 0, "total": 0, "pnls": []})
        for _, row in df.iterrows():
            h = int(row.get("_hour", 0) or 0)
            hour_data[h]["total"] += 1
            if row.get("status") == "WIN":
                hour_data[h]["wins"] += 1
            hour_data[h]["pnls"].append(float(row.get("pnl", 0) or 0))

        hour_stats = []
        for h, data in sorted(hour_data.items()):
            if data["total"] >= 3:
                hour_stats.append(HourStats(
                    hour=h,
                    total=data["total"],
                    wins=data["wins"],
                    win_rate=data["wins"] / data["total"] * 100,
                    avg_pnl=float(np.mean(data["pnls"])),
                ))
        hour_stats.sort(key=lambda x: x.win_rate, reverse=True)
        result.best_hours = hour_stats[:3]
        result.worst_hours = sorted(hour_stats, key=lambda x: x.win_rate)[:3]

    # Per-direction stats (critical for unbiased tuning)
    if "direction" in df.columns:
        for d in ("LONG", "SHORT"):
            ddf = df[df["direction"] == d]
            if len(ddf) >= 3:
                dw = ddf[ddf["status"] == "WIN"]
                result.regime_stats[f"dir_{d.lower()}"] = {
                    "total": len(ddf),
                    "wins": len(dw),
                    "win_rate": len(dw) / len(ddf) * 100 if len(ddf) > 0 else 0,
                    "avg_pnl": float(ddf["pnl"].mean()),
                }

    # Regime stats
    if "market_bias" in df.columns:
        for regime in df["market_bias"].dropna().unique():
            rdf = df[df["market_bias"] == regime]
            rw = rdf[rdf["status"] == "WIN"]
            result.regime_stats[str(regime)] = {
                "total": len(rdf),
                "wins": len(rw),
                "win_rate": len(rw) / len(rdf) * 100 if len(rdf) > 0 else 0,
                "avg_pnl": float(rdf["pnl"].mean()),
            }

    # Streak
    if "timestamp" in df.columns and not df.empty:
        sorted_df = df.sort_values("timestamp")
        outcomes = sorted_df["status"].tolist()
        streak_n, last_type = 0, None
        for o in reversed(outcomes):
            cur = "W" if o == "WIN" else "L"
            if last_type is None:
                last_type = cur
            if cur == last_type:
                streak_n += 1
            else:
                break
        result.recent_streak = streak_n
        result.streak_type = last_type or ""

    # Auto-recommendations
    result.recommendations = _generate_recommendations(result)

    return result


def _generate_recommendations(analytics: TradeAnalytics) -> List[str]:
    """Generate actionable recommendations from trade analytics."""
    recs = []

    if analytics.total_trades < 10:
        recs.append("Insufficient trade history for robust analysis. Need 10+ trades.")
        return recs

    # Win rate based
    if analytics.win_rate < 30:
        recs.append(
            f"Win rate critically low ({analytics.win_rate:.0f}%). "
            "Consider tightening entry filters (raise min_votes, raise rsi_long_momentum_min)."
        )
    elif analytics.win_rate < 45:
        recs.append(
            f"Win rate below target ({analytics.win_rate:.0f}%). "
            "Review worst patterns and consider blacklisting consistently losing setups."
        )

    # Profit factor
    if analytics.profit_factor < 1.0 and analytics.profit_factor > 0:
        recs.append(
            f"Profit factor {analytics.profit_factor:.2f} < 1.0 — system losing money. "
            "Reduce position size or tighten SL band."
        )

    # Pattern recs
    if analytics.worst_patterns:
        worst = [p for p in analytics.worst_patterns if p.win_rate < 25 and p.total >= 5]
        if worst:
            names = ", ".join(p.pattern for p in worst[:3])
            recs.append(f"Consider blacklisting patterns with <25% WR: {names}")

    if analytics.best_patterns:
        best = [p for p in analytics.best_patterns if p.win_rate > 60 and p.total >= 5]
        if best:
            names = ", ".join(p.pattern for p in best[:3])
            recs.append(f"High-WR patterns to favor: {names}")

    # Hour recs
    if analytics.worst_hours:
        bad_hours = [h for h in analytics.worst_hours if h.win_rate < 25 and h.total >= 5]
        if bad_hours:
            hrs = ", ".join(f"{h.hour}:00" for h in bad_hours)
            recs.append(f"Avoid trading at: {hrs} (win rate <25%)")

    if analytics.best_hours:
        good_hours = [h for h in analytics.best_hours if h.win_rate > 50 and h.total >= 5]
        if good_hours:
            hrs = ", ".join(f"{h.hour}:00" for h in good_hours)
            recs.append(f"Best trading hours: {hrs}")

    # Streak warning
    if analytics.streak_type == "L" and analytics.recent_streak >= 3:
        recs.append(
            f"On {analytics.recent_streak}-trade losing streak. "
            "Consider reducing position size by 50% until streak breaks."
        )

    # Expectancy
    if analytics.expectancy < 0:
        recs.append(
            f"Negative expectancy (₹{analytics.expectancy:,.0f}/trade). "
            "System is net-negative — focus on reducing loss size or increasing win rate."
        )

    if not recs:
        recs.append("System performing within acceptable parameters. No urgent changes needed.")

    return recs


# ── Semantic Search ─────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    score: float
    source: str
    title: str
    text: str
    metadata: Dict = field(default_factory=dict)


class AgenticRAG:
    """Enhanced RAG with semantic search + trade analytics."""

    def __init__(self):
        self.config = getattr(config, "RAG_CONFIG", {}) or {}
        self.top_k = int(self.config.get("top_k", 5))
        self._corpus_cache: Optional[Tuple[float, List[Dict]]] = None
        self._corpus_ttl = 120  # 2 min cache
        self._analytics_cache: Optional[Tuple[float, TradeAnalytics]] = None
        self._analytics_ttl = 300  # 5 min cache

    def get_analytics(self, days: int = 90) -> TradeAnalytics:
        """Cached trade analytics."""
        import time
        now = time.time()
        if self._analytics_cache and (now - self._analytics_cache[0]) < self._analytics_ttl:
            return self._analytics_cache[1]
        a = compute_trade_analytics(days)
        self._analytics_cache = (now, a)
        return a

    def _build_corpus(self) -> List[Dict]:
        """Build searchable corpus from all data sources."""
        import time
        now = time.time()
        if self._corpus_cache and (now - self._corpus_cache[0]) < self._corpus_ttl:
            return self._corpus_cache[1]

        docs = []

        # Signals
        for sig in _load_signals()[:25]:
            sym = sig.get("symbol", "?")
            direction = str(sig.get("direction", "")).upper()
            grade = sig.get("confluence_grade", "?")
            pats = sig.get("patterns_combined", "")
            if isinstance(pats, list):
                pats = " | ".join(pats)
            docs.append({
                "source": "signals",
                "title": f"{sym} {direction} signal (grade {grade})",
                "text": (
                    f"{sym} {direction} grade {grade}. "
                    f"Entry {sig.get('entry_price', 0)}, SL {sig.get('sl_price', 0)}, "
                    f"target {sig.get('target_price', 0)}. "
                    f"Patterns: {pats}. Reason: {sig.get('reason', '')}"
                ),
                "metadata": {"symbol": sym, "grade": grade},
            })

        # Trades (last 100)
        df = _load_trades_df()
        if not df.empty:
            if "timestamp" in df.columns:
                df = df.sort_values("timestamp", ascending=False)
            for _, row in df.head(100).iterrows():
                sym = row.get("symbol", "?")
                status = row.get("status", "?")
                pnl = row.get("pnl", 0)
                docs.append({
                    "source": "trades",
                    "title": f"{sym} {status} trade",
                    "text": (
                        f"Trade {sym} {row.get('direction', '')} outcome {status}. "
                        f"PnL: {pnl}, PnL%: {row.get('pnl_percent', 0)}. "
                        f"Patterns: {row.get('patterns_combined', '')}. "
                        f"Exit reason: {row.get('exit_reason', '')}."
                    ),
                    "metadata": {"symbol": sym, "status": status, "pnl": pnl},
                })

        # Analytics summary as document
        analytics = self.get_analytics()
        if analytics.total_trades > 0:
            docs.append({
                "source": "analytics",
                "title": "Trade performance analytics",
                "text": (
                    f"Total trades: {analytics.total_trades}. Win rate: {analytics.win_rate:.1f}%. "
                    f"Total PnL: {analytics.total_pnl:+.0f}. Profit factor: {analytics.profit_factor:.2f}. "
                    f"Expectancy: {analytics.expectancy:+.0f}/trade. "
                    f"Best patterns: {', '.join(p.pattern for p in analytics.best_patterns[:5])}. "
                    f"Worst patterns: {', '.join(p.pattern for p in analytics.worst_patterns[:5])}. "
                    f"Recommendations: {'; '.join(analytics.recommendations[:3])}"
                ),
                "metadata": {"type": "analytics_summary"},
            })

            # Per-direction analytics (enables RAG to answer "why shorts losing?")
            for dir_key in ("dir_long", "dir_short"):
                if dir_key in analytics.regime_stats:
                    ds = analytics.regime_stats[dir_key]
                    d_label = dir_key.replace("dir_", "").upper()
                    docs.append({
                        "source": "analytics",
                        "title": f"{d_label} direction performance",
                        "text": (
                            f"{d_label} trades: {ds['total']}. Win rate: {ds['win_rate']:.1f}%. "
                            f"Avg PnL: {ds['avg_pnl']:+.0f}. Wins: {ds['wins']}."
                        ),
                        "metadata": {"type": "direction_analytics", "direction": d_label},
                    })

            # Pattern weights document (enables RAG to answer "which patterns work?")
            learned = _load_json(LEARNED_PARAMS)
            pw = learned.get("PATTERN_WEIGHTS", {})
            if pw:
                top5 = sorted(pw.items(), key=lambda x: x[1], reverse=True)[:5]
                bot5 = sorted(pw.items(), key=lambda x: x[1])[:5]
                docs.append({
                    "source": "learned_params",
                    "title": "Learned pattern weights",
                    "text": (
                        f"Top patterns: {', '.join(f'{k}={v}' for k, v in top5)}. "
                        f"Bottom patterns: {', '.join(f'{k}={v}' for k, v in bot5)}. "
                        f"Total patterns tracked: {len(pw)}."
                    ),
                    "metadata": {"type": "pattern_weights"},
                })

        # Runtime state
        state = _load_json(STATE_FILE)
        if state:
            docs.append({
                "source": "state",
                "title": "Current system state",
                "text": json.dumps(state, default=str),
                "metadata": {"type": "runtime_state"},
            })

        # Swarm memory
        swarm = _load_json(SWARM_MEMORY)
        if swarm:
            stage = swarm.get("evolution_stage", "NOOB")
            gen = swarm.get("generation", 0)
            docs.append({
                "source": "swarm",
                "title": f"Swarm memory (stage={stage}, gen={gen})",
                "text": (
                    f"Swarm stage {stage}, generation {gen}. "
                    f"Total trades tracked: {swarm.get('total_trades', 0)}. "
                    f"Cumulative PnL%: {swarm.get('cumulative_pnl_pct', 0):+.1f}%. "
                    f"Streak: {swarm.get('streak', 0)}."
                ),
                "metadata": {"type": "swarm"},
            })

        self._corpus_cache = (now, docs)
        return docs

    def search(self, query: str, top_k: int = None) -> List[SearchResult]:
        """Semantic search over corpus."""
        docs = self._build_corpus()
        if not docs:
            return []

        limit = top_k or self.top_k
        texts = [d["text"] for d in docs]
        embedder = _get_embedder()

        if embedder is not None:
            # Semantic similarity via sentence-transformers
            try:
                query_emb = embedder.encode([query], convert_to_numpy=True)
                doc_embs = embedder.encode(texts, convert_to_numpy=True)
                scores = (doc_embs @ query_emb.T).ravel()
            except Exception:
                scores = self._tfidf_scores(query, texts)
        else:
            scores = self._tfidf_scores(query, texts)

        ranked = sorted(
            zip(docs, scores),
            key=lambda x: x[1],
            reverse=True,
        )

        results = []
        for doc, score in ranked[:limit]:
            if score <= 0:
                continue
            results.append(SearchResult(
                score=float(score),
                source=doc["source"],
                title=doc["title"],
                text=doc["text"][:300],
                metadata=doc.get("metadata", {}),
            ))
        return results

    def _tfidf_scores(self, query: str, texts: List[str]) -> List[float]:
        """TF-IDF fallback when sentence-transformers not available."""
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            if len(texts) < 2:
                return [0.5] * len(texts)
            vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2))
            matrix = vectorizer.fit_transform(texts)
            qvec = vectorizer.transform([query])
            return (matrix @ qvec.T).toarray().ravel().tolist()
        except ImportError:
            # Pure keyword matching fallback
            words = {w for w in re.findall(r"[a-zA-Z0-9_]+", query.lower()) if len(w) > 2}
            return [
                sum(1 for w in words if w in text.lower()) / max(len(words), 1)
                for text in texts
            ]

    def query(self, question: str, top_k: int = None) -> Dict:
        """Full query: search + analytics + recommendations."""
        results = self.search(question, top_k)
        analytics = self.get_analytics()

        # Build structured answer
        matches = []
        for r in results:
            matches.append({
                "score": round(r.score, 4),
                "source": r.source,
                "title": r.title,
                "snippet": r.text[:220],
                "metadata": r.metadata,
            })

        answer_parts = []
        if matches:
            answer_parts.append(f"Top evidence for '{question}':")
            for m in matches[:3]:
                answer_parts.append(f"  - [{m['source']}] {m['title']}: {m['snippet']}")

        if analytics.recommendations:
            answer_parts.append("\nRecommendations:")
            for rec in analytics.recommendations[:3]:
                answer_parts.append(f"  - {rec}")

        return {
            "question": question,
            "generated_at": datetime.now().isoformat(),
            "answer": "\n".join(answer_parts) if answer_parts else f"No strong matches for '{question}'.",
            "matches": matches,
            "analytics_summary": {
                "total_trades": analytics.total_trades,
                "win_rate": round(analytics.win_rate, 1),
                "total_pnl": round(analytics.total_pnl, 2),
                "profit_factor": round(analytics.profit_factor, 2),
                "expectancy": round(analytics.expectancy, 2),
                "streak": f"{analytics.streak_type}{analytics.recent_streak}",
            },
            "recommendations": analytics.recommendations,
        }

    def generate_insights(self) -> Dict:
        """Auto-generate full market + performance insights."""
        analytics = self.get_analytics()
        signals = _load_signals()
        state = _load_json(STATE_FILE)

        # Current signal landscape
        signal_summary = []
        if signals:
            by_grade = defaultdict(list)
            for s in signals:
                g = s.get("confluence_grade", "C")
                by_grade[g].append(s.get("symbol", "?"))
            for grade in ["S", "A", "B", "C"]:
                if grade in by_grade:
                    signal_summary.append(f"Grade {grade}: {', '.join(by_grade[grade][:5])}")

        # Performance summary
        perf = {
            "total_trades": analytics.total_trades,
            "win_rate": round(analytics.win_rate, 1),
            "total_pnl": round(analytics.total_pnl, 2),
            "profit_factor": round(analytics.profit_factor, 2),
            "expectancy": round(analytics.expectancy, 2),
            "avg_win": round(analytics.avg_win, 2),
            "avg_loss": round(analytics.avg_loss, 2),
            "streak": f"{analytics.streak_type}{analytics.recent_streak}",
        }

        # Pattern leaderboard
        pattern_board = []
        for p in analytics.best_patterns[:5]:
            pattern_board.append({
                "pattern": p.pattern,
                "trades": p.total,
                "win_rate": round(p.win_rate, 1),
                "avg_pnl_pct": round(p.avg_pnl_pct, 2),
            })

        # Danger patterns
        danger_patterns = []
        for p in analytics.worst_patterns[:5]:
            if p.win_rate < 40:
                danger_patterns.append({
                    "pattern": p.pattern,
                    "trades": p.total,
                    "win_rate": round(p.win_rate, 1),
                    "avg_pnl_pct": round(p.avg_pnl_pct, 2),
                })

        # Hour heatmap
        hour_map = {}
        for h in (analytics.best_hours + analytics.worst_hours):
            hour_map[h.hour] = {
                "trades": h.total,
                "win_rate": round(h.win_rate, 1),
                "avg_pnl": round(h.avg_pnl, 2),
            }

        return {
            "generated_at": datetime.now().isoformat(),
            "system_state": {
                "regime": state.get("market_regime", "unknown"),
                "macro_bias": state.get("macro_bias", "unknown"),
                "daily_pnl": state.get("daily_pnl", 0),
                "trades_today": state.get("total_trades_today", 0),
                "open_positions": len(state.get("open_positions", {})),
                "halted": state.get("trading_halted", False),
            },
            "signals": signal_summary,
            "performance": perf,
            "pattern_leaderboard": pattern_board,
            "danger_patterns": danger_patterns,
            "hour_heatmap": hour_map,
            "regime_stats": analytics.regime_stats,
            "recommendations": analytics.recommendations,
        }

    def process_feedback(self, feedback: Dict) -> Dict:
        """Process operator feedback to improve system.

        Feedback types:
          - blacklist_pattern: mark pattern as avoid
          - whitelist_pattern: mark pattern as preferred
          - adjust_param: suggest parameter change
          - note: free-form note for future reference
        """
        action = feedback.get("action", "")
        result = {"status": "ok", "action": action, "applied": False}

        if action == "blacklist_pattern":
            pattern = feedback.get("pattern", "")
            if pattern:
                try:
                    from core.swarm_intelligence import get_swarm
                    swarm = get_swarm()
                    if hasattr(swarm, "memory") and hasattr(swarm.memory, "pattern_dna"):
                        if pattern in swarm.memory.pattern_dna:
                            swarm.memory.pattern_dna[pattern]["blacklisted"] = True
                            swarm.save_memory()
                            result["applied"] = True
                            result["message"] = f"Pattern '{pattern}' blacklisted in swarm memory"
                        else:
                            result["message"] = f"Pattern '{pattern}' not found in swarm memory"
                    else:
                        result["message"] = "Swarm memory not available"
                except Exception as e:
                    result["message"] = f"Error: {e}"

        elif action == "adjust_param":
            param = feedback.get("param", "")
            value = feedback.get("value")
            if param and value is not None:
                # Write to learned_params.json
                try:
                    params = _load_json(LEARNED_PARAMS)
                    # Determine config section
                    from core.adaptive_learner import TUNABLE_PARAMS
                    if param in TUNABLE_PARAMS:
                        _, lo, hi, _, section = TUNABLE_PARAMS[param]
                        value = max(lo, min(float(value), hi))
                        if section not in params:
                            params[section] = {}
                        params[section][param] = value
                        with open(LEARNED_PARAMS, "w", encoding="utf-8") as f:
                            json.dump(params, f, indent=2)
                        result["applied"] = True
                        result["message"] = f"Set {param}={value} in {section}"
                    else:
                        result["message"] = f"Unknown tunable param: {param}"
                except Exception as e:
                    result["message"] = f"Error: {e}"

        elif action == "note":
            note = feedback.get("text", "")
            if note:
                # Append to feedback log
                fb_file = os.path.join(_PROJECT_ROOT, "logs", "operator_feedback.jsonl")
                try:
                    with open(fb_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "ts": datetime.now().isoformat(),
                            "note": note,
                        }) + "\n")
                    result["applied"] = True
                    result["message"] = "Note recorded"
                except Exception as e:
                    result["message"] = f"Error: {e}"
        else:
            result["message"] = f"Unknown action: {action}"

        return result


# Module-level singleton
_RAG_INSTANCE: Optional[AgenticRAG] = None


def get_agentic_rag() -> AgenticRAG:
    global _RAG_INSTANCE
    if _RAG_INSTANCE is None:
        _RAG_INSTANCE = AgenticRAG()
    return _RAG_INSTANCE
