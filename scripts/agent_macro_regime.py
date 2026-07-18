"""
Macro-regime agent - agentic AI advisory on market regime.

SPLIT OF LABOR (deliberate):
    Numbers are computed DETERMINISTICALLY (NIFTY trend/vol/drawdown from
    yfinance; India VIX when available). The LLM (headless `claude -p`,
    macro-analyst persona) only INTERPRETS them into a regime label + risk
    posture note. No LLM arithmetic, no LLM data fetching.

    ADVISORY ONLY: output goes to logs/regime_report.json for the human and
    the dashboard. It changes no config, sizes no position, gates no trade.
    (The live plane stays deterministic - "the system can reduce risk
    autonomously, it can never increase risk autonomously.")

RUN:
    python -m scripts.agent_macro_regime             # full agentic run
    python -m scripts.agent_macro_regime --dry-run   # features only, no LLM
Schedule: weekly, or before changing allocation posture.
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
REGIME_REPORT = os.path.join(_ROOT, "logs", "regime_report.json")
_TIMEOUT_S = 240


def compute_features() -> dict:
    """Deterministic regime features from NIFTY (and India VIX if fetchable)."""
    import pandas as pd
    import yfinance as yf

    nifty = yf.download("^NSEI", period="1y", interval="1d",
                        progress=False, auto_adjust=False)
    if nifty is None or nifty.empty:
        raise RuntimeError("NIFTY fetch failed")
    if isinstance(nifty.columns, pd.MultiIndex):
        nifty.columns = nifty.columns.get_level_values(0)
    close = nifty["Close"].dropna()

    last = float(close.iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1])
    ret_1m = float(close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) > 21 else None
    ret_3m = float(close.iloc[-1] / close.iloc[-63] - 1) * 100 if len(close) > 63 else None
    # realized vol, annualized, last 21 sessions
    rets = close.pct_change().dropna()
    rv21 = float(rets.tail(21).std() * (252 ** 0.5)) * 100
    peak = float(close.cummax().iloc[-1])
    drawdown = (last / peak - 1) * 100

    vix = None
    try:
        v = yf.download("^INDIAVIX", period="1mo", interval="1d",
                        progress=False, auto_adjust=False)
        if v is not None and not v.empty:
            if isinstance(v.columns, pd.MultiIndex):
                v.columns = v.columns.get_level_values(0)
            vix = float(v["Close"].dropna().iloc[-1])
    except Exception:
        pass

    return {
        "asof": str(close.index[-1])[:10],
        "nifty": round(last, 1),
        "vs_sma50_pct": round((last / sma50 - 1) * 100, 2),
        "vs_sma200_pct": round((last / sma200 - 1) * 100, 2),
        "ret_1m_pct": round(ret_1m, 2) if ret_1m is not None else None,
        "ret_3m_pct": round(ret_3m, 2) if ret_3m is not None else None,
        "realized_vol_21d_ann_pct": round(rv21, 1),
        "drawdown_from_peak_pct": round(drawdown, 2),
        "india_vix": round(vix, 2) if vix else None,
    }


_PROMPT = """You are the macro analyst for an NSE F&O desk running a paper-traded
swing system plus an index-core allocation engine. Interpret these
deterministically-computed features into a regime read.

Rules:
- Advisory only. You cannot change positions or config.
- You may recommend REDUCING risk; you may never recommend increasing it.
- No prediction theater: no price targets, no "market will".
- Be terse.

Output STRICT JSON only:
{"regime": "trend_up|trend_down|chop|stress",
 "confidence": "low|medium|high",
 "risk_posture": "normal|reduce|halt_new_entries",
 "read": "<=60 words, what the features say>",
 "watch": "<=30 words, what would change the read>"}

FEATURES:
"""


def run_llm(features: dict) -> dict:
    result = subprocess.run(
        ["claude", "-p", _PROMPT + json.dumps(features, indent=2),
         "--output-format", "text"],
        capture_output=True, text=True, timeout=_TIMEOUT_S,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr[:300]}")
    text = result.stdout.strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"no JSON in agent output: {text[:200]}")
    return json.loads(text[start:end + 1])


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Macro-regime advisory agent")
    p.add_argument("--dry-run", action="store_true", help="features only, no LLM")
    args = p.parse_args(argv)

    feats = compute_features()
    print("features:", json.dumps(feats, indent=2))
    if args.dry_run:
        print("[dry-run] LLM not called.")
        return 0

    print("calling agent...")
    try:
        read = run_llm(feats)
    except Exception as e:
        print(f"agent call failed: {e}")
        return 1

    # hard guard: an agent may never escalate risk
    if read.get("risk_posture") not in ("normal", "reduce", "halt_new_entries"):
        read["risk_posture"] = "normal"

    out = {"ts": datetime.now().isoformat(timespec="seconds"),
           "features": feats, "agent_read": read}
    os.makedirs(os.path.dirname(REGIME_REPORT), exist_ok=True)
    json.dump(out, open(REGIME_REPORT, "w", encoding="utf-8"), indent=2)
    print(json.dumps(read, indent=2))
    print(f"\nsaved: {REGIME_REPORT}  (advisory only - changes nothing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
