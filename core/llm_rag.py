"""
LLM-backed RAG over the trading system's OWN data.

The existing core/rag_engine.LocalRAGEngine does retrieval (TF-IDF over the
journal, FINDINGS, strategy docs, trades) and returns raw snippets. This layer
adds Claude synthesis on top: it retrieves the relevant chunks, then asks
claude-opus-4-8 to compose a grounded, cited answer from ONLY those chunks.

Honest scope: this answers questions ABOUT your system ("why did india_swing
fail?", "which factor had the best OOS t-stat?", "summarize last week's
trades") from your real files. It does NOT predict prices or generate alpha —
the edge search already proved no price-factor edge exists. This is a research
/ understanding tool, not a signal generator.

Design (per the claude-api skill):
  - Official `anthropic` SDK, model claude-opus-4-8.
  - Prompt caching on the frozen system prompt (stable prefix) so repeated
    questions only pay full price for the volatile question + retrieved context.
  - Cold-start safe: if `anthropic` isn't installed or ANTHROPIC_API_KEY is
    unset, it falls back to the existing TF-IDF snippet answer — no crash.

Setup:  pip install anthropic   +   set ANTHROPIC_API_KEY=sk-ant-...
Usage:  from core.llm_rag import ask_llm_rag
        print(ask_llm_rag("why does india_swing have no edge?")["answer"])
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
MAX_CONTEXT_CHUNKS = 8
MAX_TOKENS = 4000

_SYSTEM_PROMPT = (
    "You are the analyst for a personal F&O (futures & options) trading-research "
    "system. You answer questions strictly from the CONTEXT chunks provided in the "
    "user message — the system's own journal, backtests, factor-analysis findings, "
    "and strategy docs.\n\n"
    "Rules:\n"
    "1. Ground every claim in the context. Cite the source tag in brackets, e.g. "
    "[findings], [journal], [strategy]. If the context doesn't answer the question, "
    "say so plainly — do NOT invent numbers or fill gaps from general knowledge.\n"
    "2. This system has NO proven price-prediction edge (established by a 7-year, "
    "152-stock, out-of-sample factor search). Never imply a tradeable edge exists "
    "unless the context explicitly shows one holding out-of-sample. Be honest about "
    "uncertainty and sample size.\n"
    "3. Be concise and specific. Prefer real figures (PF, win rate, t-stat, n) from "
    "the context over vague description. Distinguish in-sample from out-of-sample.\n"
    "4. When asked 'what should I do', give measured, evidence-based options — never "
    "hype, never a guaranteed-profit claim."
)


def _retrieve(question: str, k: int) -> List[Dict]:
    """Use the existing TF-IDF engine to get the top-k chunks (full text)."""
    try:
        from core.rag_engine import build_default_rag_engine
        engine = build_default_rag_engine()
        docs = engine.build_corpus()
        ranked = engine._score_chunks(question, docs)  # [{score, chunk}, ...]
        out = []
        for item in ranked[:k]:
            if item.get("score", 0) <= 0:
                continue
            c = item["chunk"]
            out.append({"source": c.source, "title": c.title, "text": c.text,
                        "score": round(float(item["score"]), 4)})
        return out
    except Exception as e:
        log.warning("[LLM-RAG] retrieval failed: %s", e)
        return []


def _tfidf_fallback(question: str) -> Dict:
    """No LLM available — return the existing TF-IDF snippet answer."""
    try:
        from core.rag_engine import build_default_rag_engine
        res = build_default_rag_engine().query(question, top_k=MAX_CONTEXT_CHUNKS)
        return {
            "answer": res.get("answer", "No answer."),
            "sources": [m.get("source") for m in res.get("matches", [])],
            "mode": "tfidf_fallback",
            "note": "LLM unavailable (no anthropic SDK or ANTHROPIC_API_KEY) — "
                    "showing retrieval-only snippets.",
        }
    except Exception as e:
        return {"answer": f"RAG unavailable: {e}", "sources": [], "mode": "error"}


def ask_llm_rag(question: str, k: int = MAX_CONTEXT_CHUNKS) -> Dict:
    """Answer a question about the system, grounded in its own data.

    Returns {answer, sources, mode, usage?}. mode is 'llm' on success or
    'tfidf_fallback' when no LLM is available."""
    # Cold-start guards
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return _tfidf_fallback(question)
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return _tfidf_fallback(question)

    chunks = _retrieve(question, k)
    if not chunks:
        return _tfidf_fallback(question)

    context = "\n\n".join(
        f"[{c['source']}] {c['title']} (relevance {c['score']}):\n{c['text']}"
        for c in chunks
    )
    user_content = (
        f"CONTEXT (the only facts you may use):\n{context}\n\n"
        f"QUESTION: {question}\n\n"
        "Answer from the context above, citing source tags in brackets."
    )

    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # Cache the frozen system prompt (stable prefix); the volatile
            # context + question stay after the breakpoint, uncached.
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
        )
        answer = next((b.text for b in resp.content if b.type == "text"), "")
        return {
            "answer": answer,
            "sources": [c["source"] for c in chunks],
            "mode": "llm",
            "usage": {
                "input": resp.usage.input_tokens,
                "output": resp.usage.output_tokens,
                "cache_read": getattr(resp.usage, "cache_read_input_tokens", 0),
                "cache_write": getattr(resp.usage, "cache_creation_input_tokens", 0),
            },
        }
    except anthropic.AuthenticationError:
        return {"answer": "ANTHROPIC_API_KEY is invalid.", "sources": [], "mode": "error"}
    except anthropic.RateLimitError:
        return {"answer": "Rate limited — try again shortly.", "sources": [], "mode": "error"}
    except Exception as e:
        log.warning("[LLM-RAG] LLM call failed: %s", e)
        return _tfidf_fallback(question)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    q = " ".join(sys.argv[1:]) or "Why does india_swing have no edge, and what survived the factor search?"
    print(f"Q: {q}\n")
    res = ask_llm_rag(q)
    print(f"[mode: {res['mode']}]")
    if res.get("note"):
        print(res["note"])
    print("\n" + res["answer"])
    if res.get("usage"):
        u = res["usage"]
        print(f"\n(tokens in={u['input']} out={u['output']} "
              f"cache_read={u['cache_read']} cache_write={u['cache_write']})")
