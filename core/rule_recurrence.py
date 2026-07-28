"""
rule_recurrence.py — did the learned rules actually hold up after deployment?

WHY THIS EXISTS
---------------
core.mistake_learner and core.keeper_learner both promote rules that cleared a
temporal holdout, a recurrence check and a Bonferroni correction. That earns a
rule the right to be TRIED. It does not prove it keeps working.

Without this module the system can only claim "it learned". With it, the system
can show — on trades that happened AFTER a rule went live — whether the effect
persisted, and it retires the rules that did not. That is the difference
between a learner and a story about a learner.

THE OBSERVABILITY ASYMMETRY (the honest core of this module)
------------------------------------------------------------
A working GUARD destroys its own evidence. If the WIPRO guard correctly stops
WIPRO trades being taken, no new WIPRO outcomes appear, so there is nothing to
confirm it with. Absence of post-deploy losses is NOT proof the guard was
right — it is proof the guard was obeyed.

KEEPERS have no such problem: boosted signals are still traded, so their
post-deploy win-rate is directly measurable.

This module therefore reports different verdicts for the two, and never
pretends a guard was validated by silence:

  CONFIRMED   effect persisted on post-deploy trades (keepers, and guards
              measured on signals that legitimately bypassed the guard)
  REVERTED    effect disappeared or flipped -> auto-retire
  UNPROVEN    too few post-deploy observations to judge (keep, keep watching)
  UNOBSERVABLE guard is doing its job; no matching trades were taken, so no
              evidence either way. Reported as such, never as success.

RETIREMENT
----------
A REVERTED rule is removed from the active file and written to
logs/rule_retirements.jsonl. Its key is also appended to the originating
learner's ledger as a `reject`, so neither learner will re-propose it — the
system does not re-learn a lesson it has already disproved.

Retirement is deliberately one-directional and conservative: only REVERTED
rules are retired. UNPROVEN and UNOBSERVABLE rules are left alone, because
"not yet measurable" is not evidence of failure.

RUN
---
    python -m core.rule_recurrence --check      # report only, changes nothing
    python -m core.rule_recurrence --enforce    # report + retire REVERTED rules
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from core.mistake_learner import (
    load_decided, features, wilson_bounds,
    GUARDS_PATH, LEDGER_PATH as MISTAKE_LEDGER,
)
from core.keeper_learner import (
    BOOSTS_PATH, LEDGER_PATH as KEEPER_LEDGER,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RETIREMENT_PATH = os.path.join(_ROOT, "logs", "rule_retirements.jsonl")

# Minimum post-deploy trades before a rule can be judged at all. Below this the
# verdict is UNPROVEN — retiring on 2 trades would itself be overfitting.
MIN_POSTDEPLOY_N = 8

# A guard is REVERTED if the cell's post-deploy loss-rate falls back to (or
# below) baseline by this margin — i.e. the "mistake" stopped being a mistake.
GUARD_REVERT_MARGIN = 0.0
# A keeper is REVERTED if its post-deploy win-rate falls to/below baseline.
KEEPER_REVERT_MARGIN = 0.0

CONFIRMED, REVERTED, UNPROVEN, UNOBSERVABLE = (
    "CONFIRMED", "REVERTED", "UNPROVEN", "UNOBSERVABLE")


@dataclass
class RecurrenceVerdict:
    kind: str               # "guard" | "keeper"
    key: str
    feature: str
    value: str
    deployed_at: Optional[str]
    post_n: int
    post_rate: Optional[float]      # loss-rate for guards, win-rate for keepers
    baseline_rate: float
    expected_rate: float            # what the rule predicted at promotion
    verdict: str
    detail: str

    def to_dict(self) -> Dict:
        return asdict(self)


# ── Ledger helpers ──────────────────────────────────────────────────────────

def _first_promote_ts(ledger_path: str, key: str) -> Optional[str]:
    """Earliest promote timestamp for a rule — its deployment moment."""
    if not os.path.exists(ledger_path):
        return None
    best = None
    with open(ledger_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("key") == key and e.get("decision") == "promote":
                ts = e.get("ts")
                if ts and (best is None or ts < best):
                    best = ts
    return best


def _append_reject(ledger_path: str, key: str, reason: str) -> None:
    """Write a reject record so the learner never re-proposes a retired rule."""
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "key": key, "decision": "reject",
            "reason": f"RETIRED by recurrence check: {reason}",
        }) + "\n")


def _log_retirement(v: RecurrenceVerdict) -> None:
    os.makedirs(os.path.dirname(RETIREMENT_PATH), exist_ok=True)
    with open(RETIREMENT_PATH, "a", encoding="utf-8") as fh:
        rec = v.to_dict()
        rec["retired_at"] = datetime.now(timezone.utc).isoformat()
        fh.write(json.dumps(rec) + "\n")


# ── Core evaluation ─────────────────────────────────────────────────────────

def _post_deploy_rows(decided: List[Dict], feature: str, value: str,
                      deployed_at: Optional[str]) -> List[Dict]:
    """Trades matching the rule cell that were resolved AFTER deployment."""
    if not deployed_at:
        return []
    out = []
    for r in decided:
        ts = r.get("ts") or ""
        if ts <= deployed_at:
            continue
        if features(r).get(feature) == value:
            out.append(r)
    return out


def evaluate(decided: Optional[List[Dict]] = None) -> List[RecurrenceVerdict]:
    """Judge every active guard and boost against post-deployment outcomes."""
    from core.mistake_learner import load_guards
    from core.keeper_learner import load_boosts

    if decided is None:
        decided = load_decided()
    if not decided:
        return []

    base_loss = sum(1 for r in decided if not r["_won"]) / len(decided)
    base_win = 1.0 - base_loss
    verdicts: List[RecurrenceVerdict] = []

    # ── Guards ──────────────────────────────────────────────────────────
    for rule in load_guards():
        key = f"{rule['feature']}={rule['value']}"
        dep = _first_promote_ts(MISTAKE_LEDGER, key)
        rows = _post_deploy_rows(decided, rule["feature"], rule["value"], dep)
        n = len(rows)
        if n == 0:
            verdicts.append(RecurrenceVerdict(
                "guard", key, rule["feature"], rule["value"], dep, 0, None,
                round(base_loss, 3), rule.get("holdout_lossrate", 0.0),
                UNOBSERVABLE,
                "no matching trades taken since deploy — the guard is being "
                "obeyed, which is not the same as being proven right"))
            continue

        post_loss = sum(1 for r in rows if not r["_won"]) / n
        if n < MIN_POSTDEPLOY_N:
            verdicts.append(RecurrenceVerdict(
                "guard", key, rule["feature"], rule["value"], dep, n,
                round(post_loss, 3), round(base_loss, 3),
                rule.get("holdout_lossrate", 0.0), UNPROVEN,
                f"only {n} post-deploy trades (need {MIN_POSTDEPLOY_N})"))
            continue

        # These are trades that leaked past the guard (e.g. setup-exempt).
        # If they no longer lose more than baseline, the "mistake" is gone.
        if post_loss <= base_loss + GUARD_REVERT_MARGIN:
            verdicts.append(RecurrenceVerdict(
                "guard", key, rule["feature"], rule["value"], dep, n,
                round(post_loss, 3), round(base_loss, 3),
                rule.get("holdout_lossrate", 0.0), REVERTED,
                f"post-deploy loss {post_loss:.0%} no longer exceeds baseline "
                f"{base_loss:.0%} over {n} trades — effect gone"))
        else:
            verdicts.append(RecurrenceVerdict(
                "guard", key, rule["feature"], rule["value"], dep, n,
                round(post_loss, 3), round(base_loss, 3),
                rule.get("holdout_lossrate", 0.0), CONFIRMED,
                f"post-deploy loss {post_loss:.0%} still above baseline "
                f"{base_loss:.0%} over {n} trades"))

    # ── Keepers ─────────────────────────────────────────────────────────
    for rule in load_boosts():
        key = f"{rule['feature']}={rule['value']}"
        dep = _first_promote_ts(KEEPER_LEDGER, key)
        rows = _post_deploy_rows(decided, rule["feature"], rule["value"], dep)
        n = len(rows)
        if n < MIN_POSTDEPLOY_N:
            verdicts.append(RecurrenceVerdict(
                "keeper", key, rule["feature"], rule["value"], dep, n,
                (round(sum(1 for r in rows if r["_won"]) / n, 3) if n else None),
                round(base_win, 3), rule.get("holdout_winrate", 0.0), UNPROVEN,
                f"only {n} post-deploy trades (need {MIN_POSTDEPLOY_N})"))
            continue

        post_win = sum(1 for r in rows if r["_won"]) / n
        if post_win <= base_win + KEEPER_REVERT_MARGIN:
            verdicts.append(RecurrenceVerdict(
                "keeper", key, rule["feature"], rule["value"], dep, n,
                round(post_win, 3), round(base_win, 3),
                rule.get("holdout_winrate", 0.0), REVERTED,
                f"post-deploy win {post_win:.0%} no longer beats baseline "
                f"{base_win:.0%} over {n} trades — edge gone"))
        else:
            verdicts.append(RecurrenceVerdict(
                "keeper", key, rule["feature"], rule["value"], dep, n,
                round(post_win, 3), round(base_win, 3),
                rule.get("holdout_winrate", 0.0), CONFIRMED,
                f"post-deploy win {post_win:.0%} still beats baseline "
                f"{base_win:.0%} over {n} trades"))

    return verdicts


# ── Retirement ──────────────────────────────────────────────────────────────

def _rewrite_rules(path: str, drop_keys: set) -> int:
    """Remove retired rules from an active-rule file. Returns count removed."""
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return 0
    rules = payload.get("rules", [])
    kept = [r for r in rules if f"{r['feature']}={r['value']}" not in drop_keys]
    removed = len(rules) - len(kept)
    if removed:
        payload["rules"] = kept
        payload["updated"] = datetime.now(timezone.utc).isoformat()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    return removed


def enforce(verdicts: Optional[List[RecurrenceVerdict]] = None) -> Dict:
    """Retire every REVERTED rule. Conservative: touches nothing else."""
    if verdicts is None:
        verdicts = evaluate()
    reverted = [v for v in verdicts if v.verdict == REVERTED]

    guard_keys = {v.key for v in reverted if v.kind == "guard"}
    keeper_keys = {v.key for v in reverted if v.kind == "keeper"}

    n_guards = _rewrite_rules(GUARDS_PATH, guard_keys)
    n_keepers = _rewrite_rules(BOOSTS_PATH, keeper_keys)

    for v in reverted:
        _log_retirement(v)
        _append_reject(MISTAKE_LEDGER if v.kind == "guard" else KEEPER_LEDGER,
                       v.key, v.detail)

    return {
        "evaluated": len(verdicts),
        "retired_guards": n_guards,
        "retired_keepers": n_keepers,
        "retired_keys": sorted(guard_keys | keeper_keys),
        "confirmed": sum(1 for v in verdicts if v.verdict == CONFIRMED),
        "unproven": sum(1 for v in verdicts if v.verdict == UNPROVEN),
        "unobservable": sum(1 for v in verdicts if v.verdict == UNOBSERVABLE),
    }


# ── Reporting ───────────────────────────────────────────────────────────────

def report(verdicts: Optional[List[RecurrenceVerdict]] = None) -> None:
    if verdicts is None:
        verdicts = evaluate()
    print("\n=== RULE RECURRENCE (post-deployment evidence) ===")
    if not verdicts:
        print("  no active rules to check.")
        return
    for v in sorted(verdicts, key=lambda x: (x.kind, x.verdict)):
        rate = f"{v.post_rate:.0%}" if v.post_rate is not None else "  -"
        print(f"  [{v.verdict:12s}] {v.kind:6s} {v.key:24s} "
              f"n={v.post_n:<4d} post={rate:>5s} base={v.baseline_rate:.0%}")
        print(f"                 {v.detail}")
    print("\n  CONFIRMED = effect persisted · REVERTED = retire · "
          "UNPROVEN = need more data")
    print("  UNOBSERVABLE = guard obeyed, so no evidence either way "
          "(NOT proof it works)")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Check whether learned rules held up after deployment.")
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--enforce", action="store_true",
                    help="report and retire REVERTED rules")
    args = ap.parse_args()

    verdicts = evaluate()
    if args.enforce:
        report(verdicts)
        res = enforce(verdicts)
        print(f"\n  retired: {res['retired_guards']} guard(s), "
              f"{res['retired_keepers']} keeper(s)")
        if res["retired_keys"]:
            print(f"  keys: {', '.join(res['retired_keys'])}")
        return 0
    if args.check:
        report(verdicts)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
