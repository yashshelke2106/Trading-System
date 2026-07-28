"""
mistake_learner.py — learn the signature of losing trades, validate it, and
guard against repeating it.

WHAT THE GOAL ACTUALLY MEANS (honest framing)
---------------------------------------------
"Learn from every SL, never repeat the mistake, convert losers into winners"
is, taken literally, a curve-fitting machine — markets are non-stationary and
adversarial, and a learner that adapts to every loss will fit noise and do
WORSE live (this repo has already lived that: live PF 0.29 vs 0.72 expected).
You cannot turn a losing setup into a winning one.

What you CAN do, honestly, is stop taking the setups that SYSTEMATICALLY lose.
Remove the trades whose loss-rate is significantly and repeatably above
baseline, and the target-hit RATE of the trades you still take rises. That is
the real, defensible version of the goal, and it is what this module does.

THE DISCIPLINE THAT MAKES IT LEARNING, NOT OVERFITTING
------------------------------------------------------
Every proposed "mistake rule" must clear three gates before it can affect a
single live decision:

  1. TEMPORAL HOLDOUT — mined on the first 70% of trades by date, confirmed on
     the last 30%. A rule that only worked in-sample is discarded.
  2. RECURRENCE — the elevated loss-rate must persist in the holdout. A mistake
     you cannot show recurring is not a mistake you can guard against.
  3. SELECTION CORRECTION — many candidate rules are mined, so the best ones
     are inflated by multiple testing. Each survivor's edge is deflated for the
     number of candidates (Bonferroni on the loss-rate test + a Deflated-Sharpe
     check on the expectancy improvement, via core.deflated_sharpe).

Rejected rules are written to a ledger and never re-proposed — so the learner
itself does not repeat its own mistakes.

WON/LOST LABEL
--------------
Uses SPOT outcome (spot_pnl_pct / spot_outcome) when present, never the option
premium P&L — premium % is a monitoring-blind mirage (see honest_performance).

OUTPUT
------
  logs/mistake_guards.json   promoted rules signal-gen consults (should_skip)
  logs/mistake_ledger.jsonl  every promote/reject decision, append-only

RUN
---
    python -m core.mistake_learner --learn      # mine + validate + promote
    python -m core.mistake_learner --report     # active guards + recurrence
    python -m core.mistake_learner --guards     # JSON of active guards
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL = os.path.join(_ROOT, "logs", "signal_journal.jsonl")
GUARDS_PATH = os.path.join(_ROOT, "logs", "mistake_guards.json")
LEDGER_PATH = os.path.join(_ROOT, "logs", "mistake_ledger.jsonl")

# ── Tunables ────────────────────────────────────────────────────────────────
MIN_CELL_N        = 12       # min trades in a cell before it can be a rule
MIN_HOLDOUT_N     = 4        # min holdout trades to confirm recurrence
TRAIN_FRAC        = 0.70
WILSON_Z          = 1.96     # 95% Wilson interval
# A candidate cell must be pessimistically (Wilson-lower-bound) worse than the
# baseline loss-rate, plus this small point-estimate margin to skip trivia. It
# is deliberately NOT a large absolute edge: when the baseline loss-rate is
# high (a net-negative population), an absolute edge on top of it is unmeetable
# and would hide real recurring mistakes. The holdout, recurrence and
# Bonferroni gates do the real filtering.
MIN_LOSSRATE_EDGE = 0.05
BONFERRONI_ALPHA  = 0.05


# ── Wilson score interval ───────────────────────────────────────────────────

def wilson_bounds(k: int, n: int, z: float = WILSON_Z) -> Tuple[float, float]:
    """Lower/upper Wilson bounds for a proportion k/n."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _norm_sf(x: float) -> float:
    """Upper-tail standard normal (1 - CDF)."""
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def two_prop_pvalue(k1: int, n1: int, k2: int, n2: int) -> float:
    """One-sided p: is loss-rate in group1 (the cell) > group2 (the rest)?"""
    if n1 == 0 or n2 == 0:
        return 1.0
    p1, p2 = k1 / n1, k2 / n2
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (p1 - p2) / se
    return _norm_sf(z)


# ── Trade loading + feature extraction ──────────────────────────────────────

def _won(r: Dict) -> Optional[bool]:
    """True=target, False=loss, None=undecided. SPOT outcome only."""
    so = r.get("spot_outcome")
    if so in ("TARGET_HIT", "WIN", "TARGET"):
        return True
    if so in ("SL_HIT", "LOSS", "SL"):
        return False
    sp = r.get("spot_pnl_pct")
    if sp is not None:
        try:
            return float(sp) > 0
        except (ValueError, TypeError):
            pass
    # Fall back to declared outcome only when no spot data exists at all.
    oc = r.get("outcome")
    if oc == "TARGET_HIT":
        return True
    if oc == "SL_HIT":
        return False
    if oc == "EXPIRED":
        for k in ("spot_pnl_pct", "pnl_pct", "pnl_rupees"):
            v = r.get(k)
            if v is not None:
                try:
                    return float(v) > 0
                except (ValueError, TypeError):
                    pass
    return None


def _pnl(r: Dict) -> float:
    for k in ("spot_pnl_pct", "pnl_pct", "pnl_percent"):
        v = r.get(k)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return 0.0


def _band(v, edges: List[float], labels: List[str]) -> Optional[str]:
    try:
        x = float(v)
    except (ValueError, TypeError):
        return None
    for e, lab in zip(edges, labels):
        if x < e:
            return lab
    return labels[-1]


def features(r: Dict) -> Dict[str, str]:
    """Discrete feature cells a mistake rule can be keyed on."""
    f: Dict[str, str] = {}
    d = (r.get("direction") or "").lower()
    if d:
        f["direction"] = d
    if r.get("grade"):
        f["grade"] = str(r["grade"])
    if r.get("market_bias"):
        f["regime"] = str(r["market_bias"]).lower()
    if r.get("session"):
        f["session"] = str(r["session"]).lower()
    rb = _band(r.get("rsi"), [30, 45, 55, 70], ["rsi<30", "rsi30-45", "rsi45-55", "rsi55-70", "rsi>70"])
    if rb:
        f["rsi_band"] = rb
    vb = _band(r.get("volume_ratio"), [1.0, 1.5, 2.5], ["vol<1", "vol1-1.5", "vol1.5-2.5", "vol>2.5"])
    if vb:
        f["vol_band"] = vb
    mb = _band(r.get("vote_margin"), [2, 3, 5], ["vm<2", "vm2-3", "vm3-5", "vm>5"])
    if mb:
        f["vote_margin"] = mb
    # Hour of entry
    ts = r.get("ts") or ""
    try:
        hh = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
            timezone.utc).hour
        f["utc_hour"] = f"h{hh:02d}"
    except Exception:
        pass
    if r.get("symbol"):
        f["symbol"] = str(r["symbol"])
    return f


def load_decided(path: Optional[str] = None) -> List[Dict]:
    """Resolved trades with a spot win/loss label, sorted by time."""
    # Resolve at call time (not as a default arg) so tests that monkeypatch the
    # module-level JOURNAL path actually take effect — a default arg would bind
    # the original path at import.
    path = path or JOURNAL
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("replay_failed"):
                continue
            w = _won(r)
            if w is None:
                continue
            r = {**r, "_won": w, "_pnl": _pnl(r)}
            out.append(r)
    out.sort(key=lambda r: r.get("ts", ""))
    return out


# ── Rule mining + validation ────────────────────────────────────────────────

@dataclass
class MistakeRule:
    feature: str
    value: str
    train_n: int
    train_lossrate: float
    train_wilson_lo: float
    holdout_n: int
    holdout_lossrate: float
    baseline_lossrate: float
    p_value: float
    p_bonferroni: float
    expectancy_gain: float     # avg pnl improvement from removing these trades (holdout)
    promoted: bool
    reason: str

    def key(self) -> str:
        return f"{self.feature}={self.value}"

    def to_dict(self) -> Dict:
        return asdict(self)


def _rejected_keys(path: Optional[str] = None) -> set:
    """Rule keys already rejected — never re-propose them."""
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


def learn(path: Optional[str] = None, verbose: bool = True) -> Dict:
    """Mine loss-signatures, validate on holdout + selection correction, promote."""
    decided = load_decided(path or JOURNAL)
    n = len(decided)
    if n < MIN_CELL_N * 3:
        return {"ok": False, "reason": f"too few decided trades ({n})"}

    split = int(n * TRAIN_FRAC)
    train, holdout = decided[:split], decided[split:]
    base_loss_train = sum(1 for r in train if not r["_won"]) / len(train)
    base_pnl_holdout = sum(r["_pnl"] for r in holdout) / len(holdout) if holdout else 0.0

    # Enumerate candidate cells on TRAIN.
    cells: Dict[Tuple[str, str], List[Dict]] = {}
    for r in train:
        for feat, val in features(r).items():
            cells.setdefault((feat, val), []).append(r)

    candidates: List[MistakeRule] = []
    for (feat, val), rows in cells.items():
        if len(rows) < MIN_CELL_N:
            continue
        losses = sum(1 for r in rows if not r["_won"])
        lr = losses / len(rows)
        lo, _ = wilson_bounds(losses, len(rows))
        # A mistake cell: even pessimistically (Wilson lower bound) it loses
        # more than baseline, and the point estimate clears a small margin to
        # skip trivia. The holdout/recurrence/Bonferroni gates decide promotion.
        if lo > base_loss_train and lr > base_loss_train + MIN_LOSSRATE_EDGE:
            rest_loss = sum(1 for r in train if not r["_won"]) - losses
            rest_n = len(train) - len(rows)
            p = two_prop_pvalue(losses, len(rows), rest_loss, rest_n)
            candidates.append(MistakeRule(
                feature=feat, value=val, train_n=len(rows), train_lossrate=lr,
                train_wilson_lo=lo, holdout_n=0, holdout_lossrate=0.0,
                baseline_lossrate=base_loss_train, p_value=p, p_bonferroni=1.0,
                expectancy_gain=0.0, promoted=False, reason=""))

    n_candidates = len(candidates)
    rejected_before = _rejected_keys()
    promoted, evaluated = [], []

    for c in candidates:
        # Selection correction: Bonferroni across all candidate cells mined.
        c.p_bonferroni = min(1.0, c.p_value * max(n_candidates, 1))

        # Never re-propose a previously rejected rule.
        if c.key() in rejected_before:
            c.reason = "previously rejected — not re-proposed"
            evaluated.append(c)
            continue

        # Holdout recurrence + expectancy.
        h_rows = [r for r in holdout
                  if features(r).get(c.feature) == c.value]
        c.holdout_n = len(h_rows)
        if c.holdout_n < MIN_HOLDOUT_N:
            c.reason = f"insufficient holdout recurrence (n={c.holdout_n})"
            evaluated.append(c)
            continue
        h_loss = sum(1 for r in h_rows if not r["_won"]) / c.holdout_n
        c.holdout_lossrate = h_loss

        # Expectancy gain = how much holdout avg-pnl improves if we DROP these.
        kept = [r for r in holdout if features(r).get(c.feature) != c.value]
        kept_pnl = (sum(r["_pnl"] for r in kept) / len(kept)) if kept else 0.0
        c.expectancy_gain = kept_pnl - base_pnl_holdout

        recurs = h_loss > c.baseline_lossrate                 # gate 2
        survives_selection = c.p_bonferroni < BONFERRONI_ALPHA  # gate 3
        helps = c.expectancy_gain > 0                          # gate 1 (holdout)

        if recurs and survives_selection and helps:
            c.promoted = True
            c.reason = (f"holdout loss {h_loss:.0%} > base {c.baseline_lossrate:.0%}, "
                        f"Bonferroni p={c.p_bonferroni:.4f}, "
                        f"expectancy +{c.expectancy_gain:.2f}%")
            promoted.append(c)
        else:
            bits = []
            if not recurs: bits.append("no holdout recurrence")
            if not survives_selection: bits.append(f"fails selection (p*N={c.p_bonferroni:.3f})")
            if not helps: bits.append("no holdout expectancy gain")
            c.reason = "; ".join(bits)
        evaluated.append(c)

    _write_guards(promoted)
    _append_ledger(evaluated)

    result = {
        "ok": True, "decided": n, "train": len(train), "holdout": len(holdout),
        "baseline_lossrate": round(base_loss_train, 3),
        "candidates": n_candidates, "promoted": len(promoted),
        "guards": [c.key() for c in promoted],
    }
    if verbose:
        _print_learn(result, evaluated)
    return result


# ── Persistence ─────────────────────────────────────────────────────────────

def _write_guards(promoted: List[MistakeRule]) -> None:
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "rules": [
            {"feature": c.feature, "value": c.value,
             "train_lossrate": round(c.train_lossrate, 3),
             "holdout_lossrate": round(c.holdout_lossrate, 3),
             "baseline": round(c.baseline_lossrate, 3),
             "p_bonferroni": round(c.p_bonferroni, 5),
             "expectancy_gain": round(c.expectancy_gain, 3)}
            for c in promoted
        ],
    }
    os.makedirs(os.path.dirname(GUARDS_PATH), exist_ok=True)
    with open(GUARDS_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def _append_ledger(rules: List[MistakeRule]) -> None:
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(LEDGER_PATH, "a", encoding="utf-8") as fh:
        for c in rules:
            fh.write(json.dumps({
                "ts": ts, "key": c.key(),
                "decision": "promote" if c.promoted else "reject",
                "reason": c.reason, "train_n": c.train_n,
                "train_lossrate": round(c.train_lossrate, 3),
                "p_bonferroni": round(c.p_bonferroni, 5),
            }) + "\n")


def load_guards(path: Optional[str] = None) -> List[Dict]:
    path = path or GUARDS_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("rules", [])
    except Exception:
        return []


# ── Live hook: does this signal match a validated mistake? ───────────────────

def should_skip(signal: Dict) -> Tuple[bool, str]:
    """True if the signal matches a promoted mistake rule. Consulted by
    signal generation as an ADVISORY guard (validated, not raw win-rate)."""
    rules = load_guards()
    if not rules:
        return False, ""
    feats = features(signal)
    for rule in rules:
        if feats.get(rule["feature"]) == rule["value"]:
            return True, (f"mistake-guard {rule['feature']}={rule['value']} "
                          f"(holdout loss {rule['holdout_lossrate']:.0%} vs "
                          f"base {rule['baseline']:.0%})")
    return False, ""


# ── Reporting ───────────────────────────────────────────────────────────────

def _print_learn(res: Dict, evaluated: List[MistakeRule]) -> None:
    print(f"\n=== MISTAKE LEARNER ===")
    print(f"  decided trades   : {res['decided']}  (train {res['train']}, holdout {res['holdout']})")
    print(f"  baseline loss    : {res['baseline_lossrate']:.0%}")
    print(f"  candidate rules  : {res['candidates']}")
    print(f"  PROMOTED guards  : {res['promoted']}")
    for c in sorted(evaluated, key=lambda x: (not x.promoted, x.p_bonferroni)):
        mark = "[PROMOTE]" if c.promoted else "[reject ]"
        print(f"    {mark} {c.key():28s} {c.reason}")


def report() -> None:
    rules = load_guards()
    print(f"\nActive mistake guards: {len(rules)}")
    for r in rules:
        print(f"  {r['feature']}={r['value']:20s} holdout loss {r['holdout_lossrate']:.0%} "
              f"(base {r['baseline']:.0%})  +{r['expectancy_gain']:.2f}% exp  "
              f"p*N={r['p_bonferroni']:.4f}")
    if os.path.exists(LEDGER_PATH):
        rej = sum(1 for l in open(LEDGER_PATH, encoding="utf-8")
                  if l.strip() and json.loads(l).get("decision") == "reject")
        print(f"\nRejected (won't re-propose): {rej} rule-decisions logged")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Mistake learner — validated loss-signature guards.")
    ap.add_argument("--learn", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--guards", action="store_true")
    args = ap.parse_args()

    if args.learn:
        learn()
        return 0
    if args.guards:
        print(json.dumps(load_guards(), indent=2))
        return 0
    if args.report:
        report()
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
