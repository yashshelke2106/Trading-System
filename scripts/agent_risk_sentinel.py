"""
Risk-sentinel agent - agentic AI with exactly ONE power: halt new entries.

INPUT (all computed deterministically, LLM only interprets):
  - tracking error (live paper PF vs backtest expectation)
  - macro regime features (NIFTY trend/vol/drawdown, India VIX)
  - today's event risk flags (results gaps within 2d across universe)
  - recent paper-trade outcomes

OUTPUT: logs/risk_sentinel.json  {posture: normal | halt_new_entries, reason}
  Code coerces anything else to "normal". core/sentinel.entries_halted() is
  fail-safe: missing/stale file = normal ops. Exits and stops always run -
  the sentinel cannot freeze an open book, only stop NEW risk.

This is the diagram's rule made executable: "the system can reduce risk
autonomously; it can never increase risk autonomously."

RUN:
    python -m scripts.agent_risk_sentinel            # full agentic run
    python -m scripts.agent_risk_sentinel --dry-run  # inputs only, no LLM
    python -m scripts.agent_risk_sentinel --clear    # remove halt (human hand)
Schedule: daily pre-open (~09:00 IST), or on demand.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTINEL_FILE = os.path.join(_ROOT, "logs", "risk_sentinel.json")
_TIMEOUT_S = 240


def gather_inputs() -> dict:
    out: dict = {"asof": datetime.now().isoformat(timespec="seconds")}

    # 1. tracking error (silent run)
    try:
        import io
        from contextlib import redirect_stdout
        from core.tracking_error import run as te_run
        buf = io.StringIO()
        with redirect_stdout(buf):
            te = te_run()
        out["tracking_error"] = te
    except Exception as e:
        out["tracking_error"] = {"error": str(e)[:120]}

    # 2. macro features (reuse the macro agent's deterministic computation)
    try:
        from scripts.agent_macro_regime import compute_features
        out["macro"] = compute_features()
    except Exception as e:
        out["macro"] = {"error": str(e)[:120]}

    # 3. event risk flags across universe (results gaps <=2d)
    try:
        from core.news_filter import EventCalendar
        ec = EventCalendar()
        from datetime import date
        today = date.today()
        flags = sorted({
            (sym, t, (d - today).days)
            for sym, evs in ec.exact_events.items()
            for d, t in evs
            if (d - today).days <= 2 and t in ec.GAP_RISK_EVENTS
        }, key=lambda x: x[2])
        out["gap_events_2d"] = [f"{s} {t} +{dd}d" for s, t, dd in flags[:15]]
    except Exception as e:
        out["gap_events_2d"] = [f"error: {e}"[:120]]

    # 4. recent paper outcomes (last 20 closed)
    try:
        rows = []
        with open(os.path.join(_ROOT, "logs", "signal_journal.jsonl"),
                  encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get("outcome"):
                        rows.append(r)
                except Exception:
                    continue
        out["recent_trades"] = [
            {"sym": r.get("symbol"), "dir": r.get("direction"),
             "outcome": r.get("outcome"), "pnl_pct": r.get("pnl_pct")}
            for r in rows[-20:]]
    except Exception:
        out["recent_trades"] = []
    return out


_PROMPT = """You are the risk sentinel for an NSE F&O PAPER-trading desk.
Your ONLY power: set posture "halt_new_entries" or "normal". You cannot size,
enter, exit, or change anything else. Exits/stops run regardless of you.

Halt when the evidence says new entries add risk with no edge: severe live-vs-
backtest drift, stress regime, or a wall of imminent results gaps. Otherwise
normal. Note this desk is PAPER - halting costs only learning data, so do not
halt for mild concerns.

Output STRICT JSON only:
{"posture": "normal|halt_new_entries", "reason": "<=40 words", "confidence": "low|medium|high"}

INPUTS:
"""


def run_llm(inputs: dict) -> dict:
    result = subprocess.run(
        ["claude", "-p", _PROMPT + json.dumps(inputs, indent=1, default=str),
         "--output-format", "text"],
        capture_output=True, text=True, timeout=_TIMEOUT_S,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr[:200]}")
    text = result.stdout.strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Halt-only risk sentinel agent")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--clear", action="store_true",
                   help="human override: remove any halt")
    args = p.parse_args(argv)

    if args.clear:
        if os.path.exists(SENTINEL_FILE):
            os.remove(SENTINEL_FILE)
        print("sentinel cleared - normal ops.")
        return 0

    inputs = gather_inputs()
    print(json.dumps(inputs, indent=1, default=str)[:2000])
    if args.dry_run:
        print("[dry-run] LLM not called.")
        return 0

    print("calling sentinel agent...")
    try:
        read = run_llm(inputs)
    except Exception as e:
        print(f"agent failed: {e} - fail-safe: no file written, normal ops.")
        return 1

    posture = read.get("posture")
    if posture not in ("normal", "halt_new_entries"):
        posture = "normal"   # hard guard - unknown output cannot halt or worse
    payload = {"ts": datetime.now().isoformat(timespec="seconds"),
               "posture": posture,
               "reason": str(read.get("reason", ""))[:160],
               "confidence": read.get("confidence", "low"),
               "inputs_digest": {k: inputs.get(k) for k in
                                 ("tracking_error", "gap_events_2d")}}
    json.dump(payload, open(SENTINEL_FILE, "w", encoding="utf-8"), indent=2)
    print(json.dumps(payload, indent=2)[:800])
    print(f"\nsaved: {SENTINEL_FILE}")
    if posture == "halt_new_entries":
        print("NEW ENTRIES HALTED. Clear with: "
              "python -m scripts.agent_risk_sentinel --clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
