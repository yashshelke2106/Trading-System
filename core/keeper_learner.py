"""
keeper_learner.py — the positive dual of the mistake learner.

Where core.mistake_learner finds the signature of trades that SYSTEMATICALLY
LOSE and guards against them, this finds the signature of trades that
SYSTEMATICALLY WIN — the contexts worth leaning into — and emits a size/rank
BOOST for them. Together they are the honest reading of "convert toward more
target-hitting trades": avoid the proven-bad, prefer the proven-good.

It shares every primitive with mistake_learner (feature cells, Wilson bounds,
the two-proportion test, spot-outcome labelling, the temporal split) so the
two modules cannot drift apart, and applies the SAME three gates:

  1. TEMPORAL HOLDOUT   — mined on the first 70% by date, confirmed on last 30%.
  2. PERSISTENCE        — the win advantage must survive in the holdout.
  3. SELECTION          — deflated for the number of candidate cells mined
                          (Bonferroni), so the best cells are not just the
                          luckiest of many.

THE ONE HARD RULE
-----------------
A boost may raise a signal's RANK and suggest a larger SIZE. It must NEVER be
allowed to push a signal past the expectancy gate — that would turn "size up
what works" into "lower the bar," the exact overfitting this project keeps
getting burned by. signal_finalize therefore applies keeper boosts only AFTER
the expectancy gate, to ordering and sizing, never to pass/fail.

OUTPUT
------
  logs/keeper_boosts.json    promoted boost rules (feature=value -> multiplier)
  logs/keeper_ledger.jsonl   every promote/reject decision, append-only

RUN
---
    python -m core.keeper_learner --learn
    python -m core.keeper_learner --report
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# Reuse the mistake learner's primitives — single source of truth for how a
# trade is labelled and bucketed, so avoid-rules and boost-rules always agree.
from core.mistake_learner import (
    load_decided, features, wilson_bounds, two_prop_pvalue,
    MIN_CELL_N, MIN_HOLDOUT_N, TRAIN_FRAC, BONFERRONI_ALPHA,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOSTS_PATH = os.path.join(_ROOT, "logs", "keeper_boosts.json")
LEDGER_PATH = os.path.join(_ROOT, "logs", "keeper_ledger.jsonl")

# A keeper cell must be pessimistically (Wilson upper bound) BELOW baseline
# loss-rate, and beat it on the point estimate by this margin.
MIN_WINRATE_EDGE = 0.05
# Boost multiplier band. Deliberately gentle — a proven-good context earns a
# modest lean, not a bet-the-book multiplier. Sizing risk stays with the risk
# engine; this only expresses relative preference.
MIN_BOOST = 1.0
MAX_BOOST = 1.5


@dataclass
class KeeperRule:
    feature: str
    value: str
    train_n: int
    train_winrate: float
    train_wilson_hi_loss: float     # Wilson UPPER bound on loss-rate
    holdout_n: int
    holdout_winrate: float
    baseline_winrate: float
    p_value: float
    p_bonferroni: float
    expectancy_gain: float          # avg-pnl of the cell minus overall (holdout)
    boost: float
    promoted: bool
    reason: str

    def key(self) -> str:
        return f"{self.feature}={self.value}"

    def to_dict(self) -> Dict:
        return asdict(self)


def _rejected_keys(path: Optional[str] = None) -> set:
    path = path or LEDGER_PATH
    keys = set()
    if not os.path.exists(path):
        return keys
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
                if e.get("decision") == "reject":
                    keys.add(e.get("key"))
            except json.JSONDecodeError:
                continue
    return keys


def _boost_from_edge(win_edge: float) -> float:
    """Map a holdout win-rate edge over baseline to a gentle multiplier.
    +0 edge -> 1.0, +0.20 edge -> MAX_BOOST, linear, clamped."""
    b = MIN_BOOST + (win_edge / 0.20) * (MAX_BOOST - MIN_BOOST)
    return round(max(MIN_BOOST, min(MAX_BOOST, b)), 3)


def learn(path: Optional[str] = None, verbose: bool = True) -> Dict:
    decided = load_decided(path)
    n = len(decided)
    if n < MIN_CELL_N * 3:
        return {"ok": False, "reason": f"too few decided trades ({n})"}

    split = int(n * TRAIN_FRAC)
    train, holdout = decided[:split], decided[split:]
    base_win_train = sum(1 for r in train if r["_won"]) / len(train)
    base_loss_train = 1.0 - base_win_train
    base_pnl_holdout = sum(r["_pnl"] for r in holdout) / len(holdout) if holdout else 0.0

    # Enumerate candidate cells on TRAIN.
    cells: Dict[Tuple[str, str], List[Dict]] = {}
    for r in train:
        for feat, val in features(r).items():
            cells.setdefault((feat, val), []).append(r)

    candidates: List[KeeperRule] = []
    for (feat, val), rows in cells.items():
        if len(rows) < MIN_CELL_N:
            continue
        wins = sum(1 for r in rows if r["_won"])
        wr = wins / len(rows)
        losses = len(rows) - wins
        _, hi_loss = wilson_bounds(losses, len(rows))   # upper bound on loss
        # A keeper: even pessimistically the loss-rate is below baseline, and
        # the win-rate clears baseline by the margin.
        if hi_loss < base_loss_train and wr > base_win_train + MIN_WINRATE_EDGE:
            rest_win = sum(1 for r in train if r["_won"]) - wins
            rest_n = len(train) - len(rows)
            # One-sided: is the cell's LOSS-rate below the rest's?
            p = two_prop_pvalue(rest_n - rest_win, rest_n, losses, len(rows))
            candidates.append(KeeperRule(
                feature=feat, value=val, train_n=len(rows), train_winrate=wr,
                train_wilson_hi_loss=hi_loss, holdout_n=0, holdout_winrate=0.0,
                baseline_winrate=base_win_train, p_value=p, p_bonferroni=1.0,
                expectancy_gain=0.0, boost=1.0, promoted=False, reason=""))

    n_candidates = len(candidates)
    rejected_before = _rejected_keys()
    promoted, evaluated = [], []

    for c in candidates:
        c.p_bonferroni = min(1.0, c.p_value * max(n_candidates, 1))
        if c.key() in rejected_before:
            c.reason = "previously rejected — not re-proposed"
            evaluated.append(c)
            continue

        h_rows = [r for r in holdout if features(r).get(c.feature) == c.value]
        c.holdout_n = len(h_rows)
        if c.holdout_n < MIN_HOLDOUT_N:
            c.reason = f"insufficient holdout recurrence (n={c.holdout_n})"
            evaluated.append(c)
            continue
        c.holdout_winrate = sum(1 for r in h_rows if r["_won"]) / c.holdout_n
        cell_pnl = sum(r["_pnl"] for r in h_rows) / c.holdout_n
        c.expectancy_gain = cell_pnl - base_pnl_holdout

        persists = c.holdout_winrate > c.baseline_winrate     # gate 2
        survives_selection = c.p_bonferroni < BONFERRONI_ALPHA  # gate 3
        helps = c.expectancy_gain > 0                          # gate 1 (holdout)

        if persists and survives_selection and helps:
            c.boost = _boost_from_edge(c.holdout_winrate - c.baseline_winrate)
            c.promoted = True
            c.reason = (f"holdout WR {c.holdout_winrate:.0%} > base "
                        f"{c.baseline_winrate:.0%}, Bonferroni p={c.p_bonferroni:.4f}, "
                        f"expectancy +{c.expectancy_gain:.2f}%, boost x{c.boost}")
            promoted.append(c)
        else:
            bits = []
            if not persists: bits.append("win edge did not persist in holdout")
            if not survives_selection: bits.append(f"fails selection (p*N={c.p_bonferroni:.3f})")
            if not helps: bits.append("no holdout expectancy gain")
            c.reason = "; ".join(bits)
        evaluated.append(c)

    _write_boosts(promoted)
    _append_ledger(evaluated)

    result = {
        "ok": True, "decided": n, "train": len(train), "holdout": len(holdout),
        "baseline_winrate": round(base_win_train, 3),
        "candidates": n_candidates, "promoted": len(promoted),
        "boosts": {c.key(): c.boost for c in promoted},
    }
    if verbose:
        _print_learn(result, evaluated)
    return result


# ── Persistence ─────────────────────────────────────────────────────────────

def _write_boosts(promoted: List[KeeperRule]) -> None:
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "rules": [
            {"feature": c.feature, "value": c.value, "boost": c.boost,
             "train_winrate": round(c.train_winrate, 3),
             "holdout_winrate": round(c.holdout_winrate, 3),
             "baseline": round(c.baseline_winrate, 3),
             "p_bonferroni": round(c.p_bonferroni, 5),
             "expectancy_gain": round(c.expectancy_gain, 3)}
            for c in promoted
        ],
    }
    os.makedirs(os.path.dirname(BOOSTS_PATH), exist_ok=True)
    with open(BOOSTS_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def _append_ledger(rules: List[KeeperRule]) -> None:
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
        for c in rules:
            fh.write(json.dumps({
                "ts": ts, "key": c.key(),
                "decision": "promote" if c.promoted else "reject",
                "reason": c.reason, "train_n": c.train_n,
                "train_winrate": round(c.train_winrate, 3),
                "boost": c.boost, "p_bonferroni": round(c.p_bonferroni, 5),
            }) + "\n")


def load_boosts(path: Optional[str] = None) -> List[Dict]:
    path = path or BOOSTS_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("rules", [])
    except Exception:
        return []


# ── Live hook: multiplier for a signal that matches a keeper context ─────────

def confidence_boost(signal: Dict) -> Tuple[float, str]:
    """Return (multiplier, reason). 1.0 when no keeper matches.

    If several keeper cells match, the LARGEST single boost is used (not the
    product) — stacking correlated boosts would double-count the same edge.
    """
    rules = load_boosts()
    if not rules:
        return 1.0, ""
    feats = features(signal)
    best, why = 1.0, ""
    for rule in rules:
        if feats.get(rule["feature"]) == rule["value"] and rule["boost"] > best:
            best = rule["boost"]
            why = (f"keeper {rule['feature']}={rule['value']} "
                   f"(holdout WR {rule['holdout_winrate']:.0%} vs "
                   f"base {rule['baseline']:.0%})")
    return best, why


# ── Reporting ───────────────────────────────────────────────────────────────

def _print_learn(res: Dict, evaluated: List[KeeperRule]) -> None:
    print(f"\n=== KEEPER LEARNER ===")
    print(f"  decided trades   : {res['decided']}  (train {res['train']}, holdout {res['holdout']})")
    print(f"  baseline win     : {res['baseline_winrate']:.0%}")
    print(f"  candidate cells  : {res['candidates']}")
    print(f"  PROMOTED boosts  : {res['promoted']}")
    for c in sorted(evaluated, key=lambda x: (not x.promoted, x.p_bonferroni)):
        mark = "[BOOST  ]" if c.promoted else "[reject ]"
        print(f"    {mark} {c.key():28s} {c.reason}")


def report() -> None:
    rules = load_boosts()
    print(f"\nActive keeper boosts: {len(rules)}")
    for r in rules:
        print(f"  {r['feature']}={r['value']:20s} x{r['boost']}  holdout WR "
              f"{r['holdout_winrate']:.0%} (base {r['baseline']:.0%})  "
              f"+{r['expectancy_gain']:.2f}% exp  p*N={r['p_bonferroni']:.4f}")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Keeper learner — validated win-signature boosts.")
    ap.add_argument("--learn", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--boosts", action="store_true")
    args = ap.parse_args()
    if args.learn:
        learn()
        return 0
    if args.boosts:
        print(json.dumps(load_boosts(), indent=2))
        return 0
    if args.report:
        report()
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
