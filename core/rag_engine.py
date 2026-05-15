from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Dict, List

import config

from .dashboard_data import (
    LEARNING_FILE,
    PERFORMANCE_FILE,
    STATE_FILE,
    STRATEGY_FILE,
    get_learning_data,
    get_performance_data,
    get_runtime_state,
    get_signal_payload,
    get_trade_summary,
    load_trades_frame,
    read_text_file,
)

try:
    from sklearn.feature_extraction.text import TfidfVectorizer

    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False


@dataclass
class KnowledgeChunk:
    source: str
    title: str
    text: str
    metadata: Dict


def _snippet(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


class LocalRAGEngine:
    def __init__(self):
        self.config = getattr(config, "RAG_CONFIG", {}) or {}
        self.top_k = int(self.config.get("top_k", 5))
        self.max_signal_docs = int(self.config.get("max_signal_docs", 25))
        self.max_trade_docs = int(self.config.get("max_trade_docs", 100))

    def build_corpus(self) -> List[KnowledgeChunk]:
        docs: List[KnowledgeChunk] = []
        docs.extend(self._strategy_chunks())
        docs.extend(self._signal_chunks())
        docs.extend(self._trade_chunks())
        docs.extend(self._summary_chunks())
        return [doc for doc in docs if doc.text.strip()]

    def _strategy_chunks(self) -> List[KnowledgeChunk]:
        text = read_text_file(STRATEGY_FILE)
        if not text:
            return []

        docs: List[KnowledgeChunk] = []
        current_title = "Strategy Overview"
        buffer: List[str] = []

        def flush():
            if not buffer:
                return
            body = "\n".join(buffer).strip()
            if body:
                docs.append(KnowledgeChunk(
                    source="strategy",
                    title=current_title,
                    text=body,
                    metadata={"path": STRATEGY_FILE},
                ))

        for line in text.splitlines():
            if line.startswith("## ") or line.startswith("### "):
                flush()
                current_title = line.lstrip("# ").strip()
                buffer = []
                continue
            buffer.append(line)
        flush()
        return docs

    def _signal_chunks(self) -> List[KnowledgeChunk]:
        payload = get_signal_payload()
        signals = payload.get("signals", []) or []
        if not signals:
            return []

        signals = sorted(
            signals,
            key=lambda row: row.get("confluence_score", 0),
            reverse=True,
        )[: self.max_signal_docs]

        docs: List[KnowledgeChunk] = []
        for signal in signals:
            symbol = signal.get("symbol", "?")
            direction = str(signal.get("direction", "")).upper()
            grade = signal.get("confluence_grade", "?")
            score = signal.get("confluence_score", 0)
            patterns = ", ".join(signal.get("patterns_combined") or [])
            text = (
                f"{symbol} {direction} grade {grade} confluence score {score}. "
                f"Entry {signal.get('entry_price', 0)}, stop {signal.get('sl_price', 0)}, "
                f"target {signal.get('target_price', 0)}. "
                f"Reason: {signal.get('reason', '')}. Patterns: {patterns}."
            )
            docs.append(KnowledgeChunk(
                source="signals",
                title=f"{symbol} {direction} signal",
                text=text,
                metadata={
                    "symbol": symbol,
                    "grade": grade,
                    "score": score,
                    "ts": signal.get("ts"),
                },
            ))
        return docs

    def _trade_chunks(self) -> List[KnowledgeChunk]:
        df = load_trades_frame()
        if df.empty:
            return []

        if "timestamp" in df.columns:
            df = df.sort_values("timestamp", ascending=False)
        df = df.head(self.max_trade_docs)

        docs: List[KnowledgeChunk] = []
        for _, row in df.iterrows():
            symbol = row.get("symbol", "?")
            direction = row.get("direction", "")
            outcome = row.get("outcome", "")
            pnl = row.get("pnl", 0)
            reason = row.get("exit_reason", "")
            text = (
                f"Trade {symbol} {direction} outcome {outcome}. "
                f"Entry {row.get('entry_price', 0)}, exit {row.get('exit_price', 0)}, "
                f"quantity {row.get('quantity', 0)}, pnl {pnl}, pnl percent {row.get('pnl_percent', 0)}. "
                f"Session {row.get('session', '')}, regime {row.get('volatility_regime', '')}, "
                f"reason {row.get('reason', '')}, exit reason {reason}."
            )
            docs.append(KnowledgeChunk(
                source="trades",
                title=f"{symbol} {outcome}",
                text=text,
                metadata={
                    "symbol": symbol,
                    "outcome": outcome,
                    "pnl": pnl,
                    "timestamp": str(row.get("timestamp", "")),
                },
            ))
        return docs

    def _summary_chunks(self) -> List[KnowledgeChunk]:
        docs: List[KnowledgeChunk] = []
        for source, path, payload in (
            ("learning", LEARNING_FILE, get_learning_data()),
            ("performance", PERFORMANCE_FILE, get_performance_data()),
            ("state", STATE_FILE, get_runtime_state()),
        ):
            if payload:
                docs.append(KnowledgeChunk(
                    source=source,
                    title=f"{source.title()} summary",
                    text=json.dumps(payload, default=str),
                    metadata={"path": path},
                ))
        return docs

    def _score_chunks(self, question: str, docs: List[KnowledgeChunk]) -> List[Dict]:
        if not docs:
            return []

        texts = [doc.text for doc in docs]
        if SKLEARN_AVAILABLE and len(texts) > 1:
            vectorizer = TfidfVectorizer(stop_words="english", ngram_range=(1, 2))
            matrix = vectorizer.fit_transform(texts)
            qvec = vectorizer.transform([question])
            scores = (matrix @ qvec.T).toarray().ravel().tolist()
        else:
            words = {token for token in re.findall(r"[a-zA-Z0-9_]+", question.lower()) if len(token) > 2}
            scores = []
            for text in texts:
                haystack = text.lower()
                score = sum(1 for word in words if word in haystack)
                scores.append(float(score))

        ranked = []
        for doc, score in zip(docs, scores):
            ranked.append({
                "score": float(score),
                "chunk": doc,
            })
        ranked.sort(key=lambda item: item["score"], reverse=True)
        return ranked

    def query(self, question: str, top_k: int | None = None) -> Dict:
        docs = self.build_corpus()
        ranked = self._score_chunks(question, docs)
        limit = top_k or self.top_k
        matches = []
        for item in ranked[:limit]:
            chunk: KnowledgeChunk = item["chunk"]
            if item["score"] <= 0:
                continue
            matches.append({
                "score": round(item["score"], 4),
                "source": chunk.source,
                "title": chunk.title,
                "snippet": _snippet(chunk.text),
                "metadata": chunk.metadata,
            })

        answer_parts = []
        if matches:
            answer_parts.append(f"Top evidence for '{question}':")
            for match in matches[:3]:
                answer_parts.append(
                    f"- {match['title']} [{match['source']}]: {match['snippet']}"
                )
        else:
            answer_parts.append(f"No strong matches found for '{question}'.")

        return {
            "question": question,
            "generated_at": datetime.now().isoformat(),
            "answer": "\n".join(answer_parts),
            "matches": matches,
        }

    def build_market_brief(self) -> Dict:
        payload = get_signal_payload()
        signals = sorted(
            payload.get("signals", []) or [],
            key=lambda row: row.get("confluence_score", 0),
            reverse=True,
        )
        trade_summary = get_trade_summary()["summary"]
        learning = get_learning_data()
        performance = get_performance_data()
        state = get_runtime_state()

        summary: List[str] = []
        recommendations: List[str] = []

        if signals:
            leaders = ", ".join(
                f"{sig.get('symbol', '?')} {str(sig.get('direction', '')).upper()} ({sig.get('confluence_grade', '?')}/{sig.get('confluence_score', 0)})"
                for sig in signals[:3]
            )
            summary.append(f"Top live setups: {leaders}.")

            grade_counts = payload.get("by_grade", {})
            summary.append(
                f"Signal mix: A={grade_counts.get('A', 0)}, B={grade_counts.get('B', 0)}, C={grade_counts.get('C', 0)}."
            )
        else:
            summary.append("No live signals are currently above threshold.")

        if state.get("trading_halted"):
            summary.append(f"Trading is halted: {state.get('halt_reason', 'no reason provided')}.")
        elif state:
            summary.append(
                f"Runtime state: daily PnL {state.get('daily_pnl', 0)}, "
                f"consecutive losses {state.get('consecutive_losses', 0)}, "
                f"trades today {state.get('total_trades_today', 0)}."
            )

        summary.append(
            f"Recent trade stats: total PnL {trade_summary.get('total_pnl', 0):+.2f}, "
            f"win rate {trade_summary.get('win_rate', 0):.1f}%, "
            f"open positions {trade_summary.get('open_positions', 0)}."
        )

        perf_summary = performance.get("summary", {})
        if perf_summary:
            summary.append(
                f"Performance log says {perf_summary.get('total_trades', 0)} tracked trades "
                f"with total PnL {perf_summary.get('total_pnl', 0):+.2f}."
            )

        for item in learning.get("recommendations", [])[:3]:
            recommendations.append(str(item))
        if not recommendations:
            recommendations.extend([
                "Keep favoring the highest-confluence symbols over broad chasing.",
                "Use the volume and option-chain panels together before acting on lower-grade signals.",
            ])

        evidence = self.query("risk management confluence breakout volume discipline", top_k=3)["matches"]

        return {
            "generated_at": datetime.now().isoformat(),
            "summary": summary,
            "recommendations": recommendations,
            "evidence": evidence,
        }


def build_default_rag_engine() -> LocalRAGEngine:
    return LocalRAGEngine()
