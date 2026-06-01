"""
Agentic research assistant for the trading system.

Automates the manual research loop we run by hand: it can search the system's
own knowledge (RAG), read the factor-analysis findings, run the factor
analyzer, and read daily metrics — then reason over the results to answer a
research question. Built on the Claude tool runner (claude-opus-4-8, adaptive
thinking) so the model decides which tools to call and when.

Honest scope (same as core/llm_rag): this is a RESEARCH / understanding agent.
It interrogates your data and runs your analysis scripts; it does NOT predict
markets or invent an edge. The 7-year out-of-sample search already showed no
price-factor edge — an LLM doesn't change that. Use it to understand the
system fast, not to manufacture alpha.

Tools given to the agent (all read-only / analysis — nothing places orders):
  query_knowledge   — RAG over journal/findings/strategy/trades
  read_findings     — the FINDINGS_FACTORS.md conclusions
  run_factor_scan   — run analyze_factors on the live journal, return the table
  read_metrics      — last N daily metrics rows (WR/PF/DD/drift)

Setup:  pip install "anthropic[mcp]"   (or just `anthropic`)  +  ANTHROPIC_API_KEY
Run:    python research_agent.py "Did any factor survive out-of-sample, and what should I do?"
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = "claude-opus-4-8"

SYSTEM = (
    "You are a quantitative research assistant for a personal F&O trading system. "
    "Use the provided tools to gather evidence from the system's own data and "
    "analysis scripts, then give a grounded, honest answer.\n\n"
    "Ground truth you must respect: a rigorous 7-year, 152-stock, out-of-sample "
    "factor search found NO price-and-volume edge that survives after costs "
    "(india_swing PF 0.72; momentum/reversal/low-vol all failed OOS). Do not "
    "claim or imply a tradeable edge unless a tool result explicitly shows one "
    "holding out-of-sample. Cite which tool gave you each figure. Prefer real "
    "numbers (PF, WR, t-stat, n) over adjectives, and always separate in-sample "
    "from out-of-sample. If the evidence says 'no edge', say so."
)


def _make_tools():
    """Define tools with the @beta_tool decorator. Imported lazily so the file
    still imports without the SDK installed."""
    from anthropic import beta_tool

    @beta_tool
    def query_knowledge(question: str) -> str:
        """Search the trading system's own knowledge base (journal, findings,
        strategy docs, trades) and return a grounded answer with sources.

        Args:
            question: A natural-language question about the system's data.
        """
        try:
            from core.llm_rag import ask_llm_rag
            r = ask_llm_rag(question)
            return f"[{r['mode']}] {r['answer']}"
        except Exception as e:
            return f"query_knowledge failed: {e}"

    @beta_tool
    def read_findings() -> str:
        """Read FINDINGS_FACTORS.md — the conclusions of the factor analysis
        (what was removed, what survived, the no-edge verdict)."""
        p = ROOT / "FINDINGS_FACTORS.md"
        return p.read_text(encoding="utf-8", errors="ignore")[:12000] if p.exists() else "FINDINGS_FACTORS.md not found."

    @beta_tool
    def run_factor_scan() -> str:
        """Run analyze_factors.py on the live signal journal and return its
        within-direction factor table (which conditions separate winners from
        SL hits). Takes ~5s."""
        try:
            out = subprocess.run(
                [sys.executable, str(ROOT / "analyze_factors.py")],
                capture_output=True, text=True, timeout=120, cwd=str(ROOT),
            )
            return (out.stdout or "")[-6000:] or (out.stderr or "no output")[-2000:]
        except Exception as e:
            return f"run_factor_scan failed: {e}"

    @beta_tool
    def read_metrics(rows: int = 10) -> str:
        """Read the last N rows of logs/metrics_daily.jsonl (daily rolling
        WR/PF/drawdown + drift/redesign alerts).

        Args:
            rows: How many recent days to return (default 10).
        """
        p = ROOT / "logs" / "metrics_daily.jsonl"
        if not p.exists():
            return "metrics_daily.jsonl not found (no forward data yet)."
        lines = [l for l in p.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
        out = []
        for l in lines[-rows:]:
            try:
                d = json.loads(l)
                r30 = d.get("rolling_30d", {})
                out.append(f"{d.get('date')}: WR {d.get('win_rate')} | 30d PF {r30.get('pf')} "
                           f"DD {r30.get('max_dd_pct')}% drift={d.get('drift_alert')}")
            except Exception:
                continue
        return "\n".join(out) or "no parseable metrics rows."

    return [query_knowledge, read_findings, run_factor_scan, read_metrics]


def research(question: str) -> str:
    # Cold-start guards
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return ("anthropic SDK not installed. Run: pip install \"anthropic[mcp]\"\n"
                "Then set ANTHROPIC_API_KEY. (Falling back: try `python -m core.llm_rag "
                f"\"{question}\"` for retrieval-only.)")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return "ANTHROPIC_API_KEY not set. export/set it, then re-run."

    import anthropic
    client = anthropic.Anthropic()
    tools = _make_tools()

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},          # let the model decide depth + tool calls
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        tools=tools,
        messages=[{"role": "user", "content": question}],
    )

    final_text = ""
    for message in runner:                       # tool runner drives the loop
        for block in message.content:
            if block.type == "text" and block.text:
                final_text = block.text          # keep the latest assistant text
            elif block.type == "tool_use":
                print(f"  · agent called {block.name}({json.dumps(block.input)[:80]})", file=sys.stderr)
    return final_text or "(no text answer produced)"


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "Did any factor survive out-of-sample? What should I do next, honestly?"
    print(f"Q: {q}\n", file=sys.stderr)
    print(research(q))
