"""
core/research_gates.py — the six checks every strategy claim must pass.

WHY A MODULE AND NOT A CHECKLIST
--------------------------------
Six readings were overturned in a single day of research: a beta illusion,
survivorship, corporate actions (twice), a data window that was too short, and
an optimistic cost assumption. Each one produced a result that looked
significant and was not. A checklist in a document does not run; this does.

THE GATES, cheapest first so most ideas die early
-------------------------------------------------
  1 BASELINE   Does it beat holding the same universe for the same horizon?
                Holding anything liquid 3 days earned +0.186%. An "edge" below
                that is a worse way to own the market.
  2 SPREAD     Is the t-stat computed on the SPREAD over that baseline, not on
                the raw return? Momentum read t=3.67 against zero and t=1.01
                against the universe. A long book tested against zero measures
                market beta.
  3 POINT_IN_TIME  Was the universe the one actually listed that day?
                Survivors-only data halved momentum's apparent edge.
  4 CORP_ACTIONS   Were splits and bonuses neutralised? A 1:10 split reads as
                -90%. In one study 0.41% of trades moved the mean 17x.
  5 COST_SWEEP Does the verdict survive a plausible range of costs? One result
                was +9.6%/yr at 10% costs and negative at 30%. If the sign
                flips inside the plausible range, there is no verdict.
  6 SHUFFLE    For "best cell in a grid" claims, does it beat a permutation
                null? Bonferroni assumes independent tests and ignores that you
                selected a maximum. A real best cell once sat at the 0th
                percentile of its own shuffled null.
  7 FUNDABILITY Can the account HOLD the book that was validated? Pairs passed
                on shape and died here: the 8-pair portfolio whose
                diversification produced the low drawdown needs ~Rs 21 lakh of
                futures margin. Statistics computed on a book you cannot carry
                describe a strategy you do not have.

USAGE
-----
    from core.research_gates import GateReport
    rep = GateReport("my thesis")
    rep.baseline(strategy_ret=0.004, baseline_ret=0.00186)
    rep.spread_t(spread_series)
    rep.point_in_time(True); rep.corp_actions(True)
    rep.cost_sweep({10: 0.15, 20: 0.02, 30: -0.12})
    rep.fundability(capital=1_000_000, margin_per_unit=134_118, units_required=16)
    print(rep.verdict())

Gates you genuinely do not need (a single-cell test needs no shuffle) are
declared N/A explicitly. Silence is not a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

T_REQUIRED = 2.0
BASELINE_3D_PCT = 0.186     # measured: holding the liquid universe 3 days


@dataclass
class Gate:
    name: str
    passed: Optional[bool]      # None = not applicable
    detail: str

    @property
    def mark(self) -> str:
        return {True: "PASS", False: "FAIL", None: "n/a "}[self.passed]


@dataclass
class GateReport:
    thesis: str
    gates: List[Gate] = field(default_factory=list)

    def _add(self, name: str, passed: Optional[bool], detail: str) -> "GateReport":
        # Cast to a real bool. numpy comparisons return np.bool_, and
        # `np.bool_(False) is False` is False, so an identity check on
        # `passed` silently dropped failed gates out of failures() -- a gate
        # reporting FAIL while the verdict said the run was clean.
        if passed is not None:
            passed = bool(passed)
        self.gates = [g for g in self.gates if g.name != name]
        self.gates.append(Gate(name, passed, detail))
        return self

    # 1 ---------------------------------------------------------------
    def baseline(self, strategy_ret: float,
                 baseline_ret: float = BASELINE_3D_PCT / 100) -> "GateReport":
        """Both in the same units (fractions), same horizon."""
        ok = strategy_ret > baseline_ret
        return self._add("1 BASELINE", ok,
                         f"strategy {strategy_ret*100:+.3f}% vs baseline "
                         f"{baseline_ret*100:+.3f}%")

    # 2 ---------------------------------------------------------------
    def spread_t(self, spread: Sequence[float],
                 t_required: float = T_REQUIRED) -> "GateReport":
        """t-stat of the SPREAD over the benchmark, not of the raw return."""
        x = np.asarray([v for v in spread if np.isfinite(v)], dtype=float)
        if len(x) < 2 or x.std() == 0:
            return self._add("2 SPREAD_T", False, "insufficient spread data")
        t = x.mean() / (x.std() / np.sqrt(len(x)))
        return self._add("2 SPREAD_T", abs(t) >= t_required and t > 0,
                         f"t={t:.2f} on n={len(x)} (need >= {t_required})")

    # 3 ---------------------------------------------------------------
    def point_in_time(self, is_pit: bool, note: str = "") -> "GateReport":
        return self._add("3 POINT_IN_TIME", bool(is_pit),
                         note or ("ranked within the listed set"
                                  if is_pit else "SURVIVORS-ONLY data"))

    # 4 ---------------------------------------------------------------
    def corp_actions(self, cleaned: bool, note: str = "") -> "GateReport":
        return self._add("4 CORP_ACTIONS", bool(cleaned),
                         note or ("splits/bonuses neutralised" if cleaned
                                  else "RAW prices: a 1:10 split reads as -90%"))

    # 5 ---------------------------------------------------------------
    def cost_sweep(self, results: Dict[float, float]) -> "GateReport":
        """{cost_bps_or_pct: net_result}. Fails if the sign flips in range."""
        if len(results) < 2:
            return self._add("5 COST_SWEEP", False, "need >= 2 cost points")
        vals = [results[k] for k in sorted(results)]
        signs = {v > 0 for v in vals}
        ok = len(signs) == 1 and vals[-1] > 0
        span = ", ".join(f"{k:g}:{results[k]:+.3f}" for k in sorted(results))
        return self._add("5 COST_SWEEP", ok,
                         f"{span}" + ("" if ok else "  <- sign flips in range"))

    # 6 ---------------------------------------------------------------
    def shuffle(self, real_best: float, null_best: Sequence[float],
                percentile_required: float = 95.0) -> "GateReport":
        """Compare the real best cell against bests from permuted labels."""
        n = np.asarray(list(null_best), dtype=float)
        if n.size == 0:
            return self._add("6 SHUFFLE", False, "empty null")
        pct = float((n < real_best).mean() * 100)
        return self._add("6 SHUFFLE", pct >= percentile_required,
                         f"real {real_best:+.3f} at {pct:.0f}th pct of null "
                         f"(null best mean {n.mean():+.3f})")

    # 7 ---------------------------------------------------------------
    def fundability(self, capital: float, margin_per_unit: float,
                    units_required: int) -> "GateReport":
        """Can the account actually HOLD the thing that was validated?

        Added after H-019. Cointegrated pairs validated as an 8-pair book whose
        low drawdown came FROM the diversification; a stock-futures leg costs
        Rs 1.34 lakh of margin, so Rs 10 lakh holds three legs. The fundable
        version was never the version that was tested — a distinction no
        statistical gate can catch, because the statistics were computed on a
        book the account could not carry.

        Screen this BEFORE spending compute on a backtest.
        """
        if margin_per_unit <= 0 or units_required <= 0:
            return self._add("7 FUNDABILITY", False,
                             "margin per unit and units required must be > 0")
        need = margin_per_unit * units_required
        affordable = int(capital // margin_per_unit)
        ok = affordable >= units_required
        return self._add(
            "7 FUNDABILITY", ok,
            f"needs {units_required} units x Rs {margin_per_unit:,.0f} "
            f"= Rs {need:,.0f}; capital Rs {capital:,.0f} holds {affordable}"
            + ("" if ok else "  <- validated book is not fundable"))

    def not_applicable(self, gate: str, why: str) -> "GateReport":
        """Declare a gate N/A explicitly. Silence is not a pass."""
        return self._add(gate, None, why)

    # ------------------------------------------------------------------
    def failures(self) -> List[Gate]:
        return [g for g in self.gates if g.passed is False]

    def missing(self) -> List[str]:
        required = {"1 BASELINE", "2 SPREAD_T", "3 POINT_IN_TIME",
                    "4 CORP_ACTIONS", "5 COST_SWEEP", "6 SHUFFLE",
                    "7 FUNDABILITY"}
        seen = {g.name for g in self.gates}
        return sorted(required - seen)

    def passed(self) -> bool:
        return not self.failures() and not self.missing()

    def verdict(self) -> str:
        order = {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7}
        gs = sorted(self.gates, key=lambda g: order.get(g.name[0], 9))
        lines = [f"THESIS: {self.thesis}", "-" * 58]
        lines += [f"  [{g.mark}] {g.name:16s} {g.detail}" for g in gs]
        miss = self.missing()
        if miss:
            lines.append(f"  [ !! ] NOT RUN: {', '.join(miss)}")
        lines.append("-" * 58)
        if self.passed():
            lines.append("  VERDICT: all gates cleared - promote to holdout")
        elif miss:
            lines.append("  VERDICT: INCOMPLETE - an unrun gate is not a pass")
        else:
            names = ", ".join(g.name for g in self.failures())
            lines.append(f"  VERDICT: REJECT on {names}")
        return "\n".join(lines)
