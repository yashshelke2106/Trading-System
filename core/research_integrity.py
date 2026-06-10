"""
research_integrity.py — executable anti-mirage checklist.

Run a positive backtest result through validate_result() BEFORE believing it.
It auto-flags the mechanical mirage signatures this system has actually been
fooled by (see ANTI_MIRAGE_PROTOCOL.md), and prints the human-judgment checklist
it cannot verify for you. HARD flags disqualify; WARN flags need a human answer.

    from core.research_integrity import validate_result
    ok, flags = validate_result("my_edge", sharpe=1.2, oos_half1=0.9,
        oos_half2=1.4, costs_tested=[0.0005,0.001,0.0015], null_type="matched",
        p_value=0.03, n_null_draws=500, n_hypotheses_tried=12, leverage=1.0)

Self-test (shows it would have caught the pairs + overnight mirages):
    .venv/Scripts/python.exe -m core.research_integrity
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence


HARD, WARN = "HARD", "WARN"

# auto-checkable human-judgment items (code can't verify; it only reminds)
HUMAN_CHECKLIST = [
    "A0  mechanism: can you name who pays you and why they don't stop?",
    "A1  point-in-time universe incl. delisted names (else: optimistic upper bound)",
    "A1  corporate actions: total-return/adjusted series; no clip if you HOLD THROUGH",
    "A2  executable timing: you do NOT rank and fill on the same price",
    "A2  fills realistic on the names the signal LOVES (widest-spread movers)",
    "B1  thin edge is NOT levered; sizing from worst-case path",
    "B4  no exchange-rule break in window (expiry/lot/margin/settlement)",
    "C3  final gate is TINY-LIVE, not paper",
    "C2  no re-optimization mid-drawdown; every change logged with date+reason",
]


@dataclass
class Flag:
    level: str
    code: str
    msg: str


@dataclass
class Report:
    name: str
    flags: List[Flag] = field(default_factory=list)

    @property
    def hard(self) -> List[Flag]:
        return [f for f in self.flags if f.level == HARD]

    @property
    def passed(self) -> bool:
        return len(self.hard) == 0

    def add(self, level, code, msg):
        self.flags.append(Flag(level, code, msg))


def validate_result(
    name: str,
    *,
    sharpe: float,
    oos_half1: float,
    oos_half2: float,
    costs_tested: Sequence[float],
    null_type: str,                 # "matched" | "random" | "none"
    p_value: Optional[float] = None,
    n_null_draws: Optional[int] = None,
    n_hypotheses_tried: Optional[int] = None,
    best_component_share: Optional[float] = None,   # 0..1, top name/pair share of PnL
    leverage: float = 1.0,
    max_drawdown_pct: Optional[float] = None,       # negative number, e.g. -18.0
    verbose: bool = True,
) -> "tuple[bool, List[Flag]]":
    """Auto-flag mechanical mirage/survival signatures. Returns (passed, flags)."""
    r = Report(name)

    # --- mirage signatures ---
    if abs(sharpe) > 2.5:
        r.add(HARD, "A2/A6", f"implausibly high Sharpe {sharpe:.2f} — suspect "
              "beta/regime, Sharpe inflation, or non-executable fills (too good)")
    if null_type == "random":
        r.add(HARD, "A3", "weak null: random null gives flattering p — use a "
              "covariate-MATCHED null (corr/beta/sector/liquidity/vol/turnover)")
    elif null_type == "none":
        r.add(HARD, "A3", "no null at all — cannot tell skill from luck")
    if p_value is not None and n_null_draws:
        floor = 1.0 / (n_null_draws + 1)
        if p_value <= 1.5 * floor:
            r.add(HARD, "A5", f"p={p_value:.4f} is at the Monte-Carlo floor "
                  f"(1/{n_null_draws+1}={floor:.4f}) — meaningless; add draws")
    if len(set(costs_tested)) < 2:
        r.add(HARD, "A2", "no cost sweep — a single cost can't show fragility")
    if (oos_half1 > 0) != (oos_half2 > 0):
        r.add(WARN, "A4", f"OOS halves unstable ({oos_half1:+.2f}/{oos_half2:+.2f})"
              " — sign flips across the split")
    if n_hypotheses_tried is None:
        r.add(WARN, "A5", "search not counted — track EVERY hypothesis incl. "
              "data-cleaning/roll/lag/sizing choices, not just model params")
    elif p_value is not None and p_value * n_hypotheses_tried > 0.05:
        r.add(HARD, "A5", f"fails multiple-testing: Bonferroni p*N = "
              f"{p_value*n_hypotheses_tried:.3f} > 0.05 over {n_hypotheses_tried} tries")
    if best_component_share is not None and best_component_share > 0.5:
        r.add(WARN, "A6", f"concentration: top component is {best_component_share:.0%}"
              " of PnL — use block/contiguous-subperiod deletion, not just LOO")

    # --- survival signatures ---
    if leverage > 1.0 and sharpe < 1.0:
        r.add(HARD, "B1", f"SURVIVAL RISK: leverage {leverage:.1f}x on a thin edge "
              f"(Sharpe {sharpe:.2f}) — leverage turns a real edge into a blow-up")
    if max_drawdown_pct is not None and max_drawdown_pct < -25:
        r.add(WARN, "B2", f"maxDD {max_drawdown_pct:.0f}% — model the tail PATH "
              "(gap clusters, margin hikes), not just the average")

    if verbose:
        _print(r)
    return r.passed, r.flags


def _print(r: Report) -> None:
    print(f"\n[research_integrity] {r.name}")
    if not r.flags:
        print("  no mechanical red flags.")
    for f in sorted(r.flags, key=lambda x: x.level):
        print(f"  [{f.level}] {f.code:6s} {f.msg}")
    print(f"  => {'PASS mechanical checks' if r.passed else 'BLOCKED (hard flags)'}; "
          "still owe the human checklist:")
    for item in HUMAN_CHECKLIST:
        print(f"     [ ] {item}")


def _selftest() -> None:
    print("=" * 70)
    print("SELF-TEST: would the checklist have caught this session's mirages?")
    print("=" * 70)
    # Pairs v1 — looked real (p=0.01) but weak random null
    validate_result("pairs_v1 (the mirage)", sharpe=1.26, oos_half1=0.90,
                    oos_half2=1.60, costs_tested=[0.001], null_type="random",
                    p_value=0.010, n_null_draws=200, n_hypotheses_tried=None)
    # Pairs v2 — honest matched null
    validate_result("pairs_v2 (honest)", sharpe=1.06, oos_half1=0.70,
                    oos_half2=1.41, costs_tested=[0.0015], null_type="matched",
                    p_value=0.120, n_null_draws=500, n_hypotheses_tried=1)
    # Overnight MOM — too-good Sharpe + MC-floor p + weak null
    validate_result("overnight_MOM (the mirage)", sharpe=5.30, oos_half1=9.41,
                    oos_half2=2.09, costs_tested=[0.0005, 0.0007, 0.001],
                    null_type="random", p_value=0.003, n_null_draws=300,
                    n_hypotheses_tried=4)


if __name__ == "__main__":
    _selftest()
