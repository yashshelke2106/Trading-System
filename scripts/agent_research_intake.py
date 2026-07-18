"""
Research-intake agent - agentic AI that reads the week's market evidence and
DRAFTS hypothesis proposals for human review.

WHAT IT IS (and is not):
    An LLM (headless `claude -p`, quant-researcher persona) reads a
    deterministic brief built from: last-7d news archive, typed reaction log,
    event-study results, and the FULL hypothesis registry (so it cannot
    re-propose hunts already REJECTED). It outputs 0-3 falsifiable proposals.

    It NEVER registers hypotheses, never trades, never touches config. Output
    goes to logs/hypothesis_proposals.jsonl + a markdown report. A human reads
    the report and, if convinced, registers via:
        python -m core.hypothesis_registry register "<thesis>" "<test plan>"

    Governance: research plane only, propose-only, one-way valve preserved.

RUN:
    python -m scripts.agent_research_intake              # full agentic run
    python -m scripts.agent_research_intake --dry-run    # print brief, no LLM
Schedule: weekly (Sunday) - LLM call costs plan tokens; not in daily update.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_ARCHIVE = os.path.join(_ROOT, "logs", "news_archive.jsonl")
REACTION_LOG = os.path.join(_ROOT, "logs", "news_reaction.jsonl")
STUDY_RESULTS = os.path.join(_ROOT, "logs", "event_study_results.json")
PROPOSALS = os.path.join(_ROOT, "logs", "hypothesis_proposals.jsonl")
REPORT = os.path.join(_ROOT, "logs", "research_intake_report.md")

_TIMEOUT_S = 300


def _jsonl(path, days: int = None, ts_key: str = "captured_at"):
    if not os.path.exists(path):
        return []
    cutoff = (datetime.now() - timedelta(days=days)).isoformat() if days else ""
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if days and str(r.get(ts_key, "")) < cutoff:
                continue
            out.append(r)
    return out


def build_brief() -> str:
    """Deterministic evidence brief the LLM reasons over."""
    from core.hypothesis_registry import status
    news = _jsonl(NEWS_ARCHIVE, days=7)
    rx = _jsonl(REACTION_LOG)
    reg = status()

    lines = ["# WEEKLY RESEARCH BRIEF (auto-generated)", ""]

    lines.append("## Hypothesis registry - DO NOT re-propose anything below")
    for r in reg:
        lines.append(f"- {r['id']} [{r['verdict'] or 'OPEN'}]: {r['thesis']}")

    lines.append("\n## Standing constraints (non-negotiable)")
    lines.append("- F&O large-cap universe is efficient: 10 hunts, 1 thin survivor (RSI-2).")
    lines.append("- Round-trip cost: futures ~0.06-0.10%, cash delivery ~0.2-0.25%. Edges below cost are dead.")
    lines.append("- Corporate events: NO abnormal drift (holdout-confirmed rejection).")
    lines.append("- Survivorship: price data = current survivors; any raw-drift claim is suspect.")

    lines.append(f"\n## Last-7d news ({len(news)} articles)")
    for r in news[-40:]:
        lines.append(f"- {str(r.get('pub_date',''))[:10]} [{r.get('source','')}] {r.get('title','')}")

    lines.append(f"\n## Typed reaction log ({len(rx)} events)")
    for r in rx[-20:]:
        lines.append(f"- {r.get('date')} {r.get('symbol')} {r.get('signal')} "
                     f"event={r.get('event_type')} ret1d={r.get('ret_1d')} "
                     f"ret5d={r.get('ret_5d')} ripe={r.get('ripe')}")

    if os.path.exists(STUDY_RESULTS):
        res = json.load(open(STUDY_RESULTS, encoding="utf-8"))
        lines.append(f"\n## Event-study summary (metric: {res.get('metric')})")
        for et, b in list(res.get("by_event_type", {}).items())[:15]:
            lines.append(f"- {et}: n={b.get('n')} +5d={ (b.get('h5') or {}).get('avg') } "
                         f"+20d={ (b.get('h20') or {}).get('avg') }")
    return "\n".join(lines)


_PROMPT = """You are the quant-researcher for an NSE F&O desk. Read the brief below.

Propose 0 to 3 NEW trading-edge hypotheses worth testing. Rules:
1. NEVER re-propose anything in the registry list (any verdict). Adjacent variants of REJECTED hunts are also banned.
2. Every proposal must be falsifiable with the desk's data (10yr daily OHLC for 153 F&O symbols, forward news archive, dated corporate events) at zero data cost.
3. State expected effect size vs the cost hurdle. If you cannot argue the effect plausibly exceeds 0.10% round-trip, do not propose it.
4. Proposing ZERO hypotheses is a fully acceptable answer and is expected most weeks. Do not invent weak ideas to fill quota.

Output STRICT JSON only, no prose outside it:
{"proposals": [{"thesis": "...", "test_plan": "...", "expected_effect": "...", "why_not_already_rejected": "..."}]}

BRIEF:
"""


def run_llm(brief: str) -> dict:
    """Headless claude call. Returns parsed JSON or raises."""
    result = subprocess.run(
        ["claude", "-p", _PROMPT + brief, "--output-format", "text"],
        capture_output=True, text=True, timeout=_TIMEOUT_S,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr[:300]}")
    text = result.stdout.strip()
    # tolerate fenced output
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"no JSON in agent output: {text[:200]}")
    return json.loads(text[start:end + 1])


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Weekly research-intake agent")
    p.add_argument("--dry-run", action="store_true", help="print brief, skip LLM")
    args = p.parse_args(argv)

    brief = build_brief()
    if args.dry_run:
        # news headlines carry unicode (rupee sign etc.) the Windows cp1252
        # console can't encode - degrade lossy rather than crash
        sys.stdout.buffer.write(
            brief.encode(sys.stdout.encoding or "utf-8", errors="replace"))
        print(f"\n\n[dry-run] brief {len(brief)} chars; LLM not called.")
        return 0

    print(f"brief built ({len(brief)} chars); calling agent...")
    try:
        out = run_llm(brief)
    except Exception as e:
        print(f"agent call failed: {e}")
        return 1

    proposals = out.get("proposals", [])
    ts = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(PROPOSALS), exist_ok=True)
    with open(PROPOSALS, "a", encoding="utf-8") as f:
        for pr in proposals:
            pr["ts"] = ts
            pr["status"] = "PROPOSED"  # human must register explicitly
            f.write(json.dumps(pr, ensure_ascii=False) + "\n")

    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(f"# Research intake report - {ts}\n\n")
        if not proposals:
            f.write("Agent proposed **zero** hypotheses this week. That is the "
                    "expected outcome in an efficient universe - not a failure.\n")
        for i, pr in enumerate(proposals, 1):
            f.write(f"## Proposal {i}\n"
                    f"- **Thesis:** {pr.get('thesis')}\n"
                    f"- **Test plan:** {pr.get('test_plan')}\n"
                    f"- **Expected effect:** {pr.get('expected_effect')}\n"
                    f"- **Why not already rejected:** {pr.get('why_not_already_rejected')}\n\n"
                    f"To accept: `python -m core.hypothesis_registry register "
                    f"\"{pr.get('thesis')}\" \"{pr.get('test_plan')}\"`\n\n")

    print(f"{len(proposals)} proposal(s) -> {REPORT}")
    print("Human review required; nothing was registered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
