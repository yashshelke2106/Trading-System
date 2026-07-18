"""
Trade-autopsy TEAM - the loss-factor observer, built as an adversarial pair.

GOAL: find factors that separate losing trades from winners in the paper
journal, and turn SURVIVING factors into registry proposals - never directly
into filters.

WHY A TEAM (and what kind):
    Same-model clone committees are correlated theater. This uses two agents
    with OPPOSING objectives:
      Stage A  ANALYST  - proposes loss factors from the deterministic table.
      Stage B  CRITIC   - red-team pass: attacks each factor (multiple testing
                          across buckets, n too small, in-sample circularity,
                          metric mismatch, cost). Only factors the critic marks
                          UPHELD/WEAKENED survive.
    Survivors -> logs/autopsy_proposals.jsonl for HUMAN registry review, then
    holdout testing. A factor becomes a live filter only after that gate.

GUARDRAIL (why not filter directly): mining N trades for loss factors then
filtering those trades is in-sample curve fitting - the exact failure mode
this desk's audit history documents. The registry/holdout path is mandatory.

DETERMINISTIC STAGE (no LLM): buckets the journal by direction, grade,
market_bias, outcome, option side, AI-prob band, event proximity; computes
per-bucket PF / win rate / avg pnl / n. LLM never does arithmetic.

RUN:
    python -m scripts.agent_trade_autopsy --dry-run   # attribution table only
    python -m scripts.agent_trade_autopsy             # full team run (2 LLM calls)
Schedule: weekly, or after every ~50 new closed trades.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL = os.path.join(_ROOT, "logs", "signal_journal.jsonl")
PROPOSALS = os.path.join(_ROOT, "logs", "autopsy_proposals.jsonl")
REPORT = os.path.join(_ROOT, "logs", "trade_autopsy_report.md")
_TIMEOUT_S = 300
MIN_BUCKET_N = 15


def _model() -> str:
    return os.getenv("CLAUDE_AGENT_MODEL", "claude-haiku-4-5-20251001")


# ─────────────────── deterministic attribution ───────────────────

def _load_closed(engine_prefix: str = "v3-swing"):
    rows = []
    if not os.path.exists(JOURNAL):
        return rows
    with open(JOURNAL, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not r.get("outcome") or r.get("pnl_pct") is None:
                continue
            if engine_prefix and not str(r.get("engine_version") or "").startswith(engine_prefix):
                continue
            rows.append(r)
    return rows


def _bucket_key(r: dict, dim: str):
    if dim == "direction":
        return r.get("direction")
    if dim == "grade":
        return r.get("grade")
    if dim == "market_bias":
        return r.get("market_bias")
    if dim == "outcome":
        return r.get("outcome")
    if dim == "option_type":
        return r.get("option_type")
    if dim == "ai_prob_band":
        p = r.get("ai_prob")
        if p is None:
            return "none"
        return "high>=0.6" if p >= 0.6 else "mid0.4-0.6" if p >= 0.4 else "low<0.4"
    if dim == "iv_band":
        v = r.get("iv_pct")
        if v is None:
            return "none"
        return "iv_high>=60" if v >= 60 else "iv_mid30-60" if v >= 30 else "iv_low<30"
    return "?"


DIMS = ["direction", "grade", "market_bias", "option_type", "ai_prob_band", "iv_band"]


def attribution(rows) -> dict:
    """{dim: {bucket: {n, wr, pf, avg_pnl}}} - pure arithmetic, no LLM."""
    out = {}
    for dim in DIMS:
        buckets = defaultdict(list)
        for r in rows:
            buckets[str(_bucket_key(r, dim))].append(float(r["pnl_pct"]))
        table = {}
        for b, pnls in buckets.items():
            if len(pnls) < MIN_BUCKET_N:
                continue
            wins = sum(p for p in pnls if p > 0)
            losses = -sum(p for p in pnls if p < 0)
            table[b] = {
                "n": len(pnls),
                "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3),
                "pf": round(wins / losses, 3) if losses > 0 else None,
                "avg_pnl": round(sum(pnls) / len(pnls), 2),
            }
        if table:
            out[dim] = dict(sorted(table.items(),
                                   key=lambda kv: kv[1]["pf"] or 0))
    return out


def _fmt_table(attr: dict, total_n: int) -> str:
    lines = [f"closed trades analyzed: {total_n}  (buckets with n<{MIN_BUCKET_N} hidden)",
             "NOTE: pnl_pct is OPTION-PREMIUM percent (known metric caveat).", ""]
    for dim, table in attr.items():
        lines.append(f"[{dim}]")
        for b, s in table.items():
            lines.append(f"  {b:14s} n={s['n']:4d}  wr={s['win_rate']:.0%}  "
                         f"pf={s['pf']}  avg={s['avg_pnl']:+.1f}%")
        lines.append("")
    return "\n".join(lines)


# ─────────────────── the adversarial pair ───────────────────

_ANALYST = """You are the trade-autopsy analyst for an NSE F&O PAPER desk.
Below is a deterministic per-bucket performance table from the paper journal.
Identify up to 4 candidate LOSS FACTORS (bucket conditions that concentrate
losses). For each: the factor, the buckets supporting it, and a mechanism
guess. Do not do arithmetic; the table is authoritative. STRICT JSON only:
{"factors": [{"factor": "...", "evidence": "...", "mechanism": "..."}]}

TABLE:
%s
"""

_CRITIC = """You are the desk's red-team critic. An analyst proposed loss
factors from a bucket table (also below). Attack each factor:
- multiple testing: ~%d buckets were scanned; a lone weak bucket is noise
- n and overlap; in-sample circularity (factors mined from the same trades)
- premium-vs-underlying metric mismatch (pnl is option-premium %%)
- would a filter on this factor survive 0.10%% cost and a holdout?
Verdict per factor: UPHELD / WEAKENED / KILLED with one-line reason.
STRICT JSON only:
{"verdicts": [{"factor": "...", "verdict": "UPHELD|WEAKENED|KILLED", "reason": "..."}]}

ANALYST FACTORS:
%s

TABLE:
%s
"""


def _call(prompt: str) -> dict:
    result = subprocess.run(
        ["claude", "--model", _model(), "-p", prompt, "--output-format", "text"],
        capture_output=True, text=True, timeout=_TIMEOUT_S,
        encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr.strip()[-600:]}")
    text = result.stdout.strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Adversarial trade-autopsy team")
    p.add_argument("--dry-run", action="store_true", help="attribution only, no LLM")
    p.add_argument("--engine", default="v3-swing")
    args = p.parse_args(argv)

    rows = _load_closed(args.engine)
    if len(rows) < 50:
        print(f"only {len(rows)} closed trades - too few for attribution.")
        return 1
    attr = attribution(rows)
    table = _fmt_table(attr, len(rows))
    print(table)
    if args.dry_run:
        print("[dry-run] team not called.")
        return 0

    n_buckets = sum(len(t) for t in attr.values())
    print("stage A: analyst...")
    factors = _call(_ANALYST % table).get("factors", [])
    print(f"  analyst proposed {len(factors)} factor(s)")
    if not factors:
        print("no factors proposed - done.")
        return 0

    print("stage B: red-team critic...")
    verdicts = _call(_CRITIC % (n_buckets, json.dumps(factors, indent=1), table)
                     ).get("verdicts", [])

    ts = datetime.now().isoformat(timespec="seconds")
    survivors = []
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(f"# Trade autopsy - {ts}\n\n```\n{table}\n```\n\n")
        for v in verdicts:
            f.write(f"- **{v.get('verdict')}**: {v.get('factor')} - {v.get('reason')}\n")
            if v.get("verdict") in ("UPHELD", "WEAKENED"):
                survivors.append(v)
        f.write("\n## Next step for survivors\nRegister + holdout-test before "
                "any filter ships:\n`python -m core.hypothesis_registry register "
                "\"<factor>\" \"<holdout test plan>\"`\n")

    with open(PROPOSALS, "a", encoding="utf-8") as f:
        for s in survivors:
            f.write(json.dumps({"ts": ts, **s, "status": "NEEDS_REGISTRY"},
                               ensure_ascii=False) + "\n")

    print(f"\ncritic verdicts: {len(verdicts)}  survivors: {len(survivors)}")
    print(f"report: {REPORT}")
    print("Survivors are PROPOSALS - register + holdout before any filter ships.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
