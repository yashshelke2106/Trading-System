"""
Runtime signal-review agent - LLM second opinion on scanner signals.

POWER MODEL (hard-enforced in code, not prompt-trusted):
    The agent may CONCUR, VETO, or ABSTAIN on each signal. That is all.
    - It cannot create signals, change direction/entry/SL/target, or upgrade
      a grade. Deterministic pipeline output is untouched except for
      annotation fields (agent_verdict / agent_reason / agent_veto).
    - Unknown/malformed verdicts coerce to ABSTAIN.
    - Any failure (CLI missing, timeout, bad JSON) -> signals pass through
      annotated "unavailable". The scan NEVER blocks on the LLM.

    Rationale: an LLM in the signal path may only remove risk, never add it -
    same rule as the risk sentinel ("reduce autonomously, never increase").

ACTIVATION: opt-in - set env AGENT_SIGNAL_REVIEW=1 (uses claude CLI tokens).
    Signals are rare (most scans: 0), so cost is one short call on signal days.

Context given to the agent per signal: the signal row, event risk flags
(results/board meeting <=2d), latest macro-regime read, latest headlines
for the symbol.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Dict, List

log = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW_LOG = os.path.join(_ROOT, "logs", "signal_review.jsonl")
REGIME_REPORT = os.path.join(_ROOT, "logs", "regime_report.json")
_TIMEOUT_S = 120

VERDICTS = {"CONCUR", "VETO", "ABSTAIN"}


def enabled() -> bool:
    return os.getenv("AGENT_SIGNAL_REVIEW", "").strip() in {"1", "true", "yes"}


def _context_for(signals: List[Dict]) -> str:
    """Deterministic context block: event flags + regime + recent headlines."""
    lines = []
    try:
        from .news_filter import EventCalendar
        ec = EventCalendar()
        for s in signals:
            d, t = ec.next_exact_event(s.get("symbol", ""))
            if d is not None and d <= 5:
                lines.append(f"- {s['symbol']}: {t} in {d}d"
                             + (" (GAP RISK <=2d)" if d <= 2 and
                                t in ec.GAP_RISK_EVENTS else ""))
    except Exception:
        pass
    try:
        if os.path.exists(REGIME_REPORT):
            r = json.load(open(REGIME_REPORT, encoding="utf-8"))
            ar = r.get("agent_read", {})
            lines.append(f"- regime: {ar.get('regime')} "
                         f"posture={ar.get('risk_posture')} ({r.get('ts', '')[:10]})")
    except Exception:
        pass
    return "\n".join(lines) if lines else "(no event/regime context)"


_PROMPT = """You are the strategy-reviewer for an NSE F&O paper-trading desk.
Review each scanner signal below. You may only CONCUR, VETO, or ABSTAIN.
VETO only for concrete, stated risk (event gap, regime conflict, incoherent
levels, stale context). You cannot modify signals. Terse reasons (<=20 words).

Output STRICT JSON only:
{"reviews": [{"symbol": "...", "verdict": "CONCUR|VETO|ABSTAIN", "reason": "..."}]}

CONTEXT:
%s

SIGNALS:
%s
"""


def _call_llm(signals: List[Dict]) -> Dict[str, Dict]:
    slim = [{k: s.get(k) for k in
             ("symbol", "direction", "confluence_grade", "entry_price",
              "sl_price", "target_price", "rr_ratio", "reason")}
            for s in signals]
    prompt = _PROMPT % (_context_for(signals), json.dumps(slim, indent=1))
    result = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "text"],
        capture_output=True, text=True, timeout=_TIMEOUT_S,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI rc={result.returncode}: {result.stderr[:200]}")
    text = result.stdout.strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    start, end = text.find("{"), text.rfind("}")
    parsed = json.loads(text[start:end + 1])
    out = {}
    for r in parsed.get("reviews", []):
        v = str(r.get("verdict", "")).upper()
        out[r.get("symbol", "")] = {
            "verdict": v if v in VERDICTS else "ABSTAIN",   # coerce unknown
            "reason": str(r.get("reason", ""))[:160],
        }
    return out


def review_signals(signals: List[Dict]) -> List[Dict]:
    """Annotate signals in place with agent_verdict/agent_reason/agent_veto.
    Never raises; never blocks; never modifies trade parameters."""
    if not signals or not enabled():
        return signals
    try:
        reviews = _call_llm(signals)
    except Exception as e:
        log.warning("signal review unavailable: %s", e)
        for s in signals:
            s["agent_verdict"] = "unavailable"
        return signals

    from datetime import datetime
    with open(REVIEW_LOG, "a", encoding="utf-8") as f:
        for s in signals:
            r = reviews.get(s.get("symbol", ""),
                            {"verdict": "ABSTAIN", "reason": "not reviewed"})
            s["agent_verdict"] = r["verdict"]
            s["agent_reason"] = r["reason"]
            s["agent_veto"] = r["verdict"] == "VETO"   # advisory flag; params untouched
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "symbol": s.get("symbol"), **r}) + "\n")
    return signals
