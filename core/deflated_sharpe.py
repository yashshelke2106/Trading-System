"""
deflated_sharpe.py — selection-bias correction for the research program.

WHY THIS EXISTS
---------------
The repo already does per-study rigour: Bonferroni inside each study, a
one-shot holdout ledger with no retries, a leakage audit. What it did NOT do
is correct a WINNING result for the fact that the program ran many hunts to
find it. A t-stat that clears one test can still be a false positive when it is
the maximum of N correlated trials — this is backtest overfitting, and it is
the single most common way a "validated" edge turns out to be noise in live
trading.

Elite desks answer this with the Deflated Sharpe Ratio (Bailey & López de
Prado, 2014): before capital, a strategy's Sharpe must beat not zero, but the
Sharpe you would EXPECT as the best of N trials under the null — adjusted for
sample length and for the non-normality (skew, fat tails) of the returns.

This module is a GATE, not a signal generator. It can only downgrade a claim,
never create one. It exists so the one CONDITIONAL-PASS on the books (RSI-2)
faces the same standard a fund would apply, and so any future pass must too.

REFERENCES
----------
  Bailey, D. & López de Prado, M. (2014). "The Deflated Sharpe Ratio:
    Correcting for Selection Bias, Backtest Overfitting and Non-Normality."
    Journal of Portfolio Management.

KEY FORMULAS (all Sharpes in the SAME per-observation units unless annualized)
-----------------------------------------------------------------------------
  Expected maximum Sharpe under the null, across N independent trials whose
  trial-Sharpes have standard deviation σ_SR:

    SR0 = σ_SR * [ (1-γ)·Z⁻¹(1 - 1/N) + γ·Z⁻¹(1 - 1/(N·e)) ]

  where γ ≈ 0.5772 (Euler–Mascheroni) and Z⁻¹ is the inverse standard-normal.

  Deflated Sharpe Ratio (a probability):

    DSR = Φ( ( (SR̂ - SR0)·√(T-1) ) / √(1 - γ3·SR̂ + ((γ4-1)/4)·SR̂²) )

  with T observations, γ3 = skew, γ4 = kurtosis (Fisher: normal → 3) of the
  returns. DSR > 0.95 means the observed Sharpe is significantly above the
  selection-adjusted benchmark.

  Minimum Track Record Length — observations needed for SR̂ to be significantly
  above a benchmark SR* at confidence p:

    MinTRL = 1 + (1 - γ3·SR̂ + ((γ4-1)/4)·SR̂²) · ( Z⁻¹(p) / (SR̂ - SR*) )²
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Sequence

# Standard-normal helpers via the error function — no scipy dependency, so this
# gate runs anywhere the rest of core does.
_SQRT2 = math.sqrt(2.0)
GAMMA_EM = 0.5772156649015329  # Euler–Mascheroni


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF (Acklam's rational approximation, |err|<1e-9)."""
    if not (0.0 < p < 1.0):
        raise ValueError(f"norm_ppf domain is (0,1), got {p}")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ── Sample moments (population-free of scipy) ───────────────────────────────

def _moments(returns: Sequence[float]):
    n = len(returns)
    if n < 3:
        raise ValueError("need >= 3 observations")
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 0.0
    if sd == 0:
        return mean, sd, 0.0, 3.0
    m3 = sum((r - mean) ** 3 for r in returns) / n
    m4 = sum((r - mean) ** 4 for r in returns) / n
    skew = m3 / (sd ** 3)
    kurt = m4 / (sd ** 4)   # Fisher NON-excess: normal == 3
    return mean, sd, skew, kurt


def sharpe(returns: Sequence[float]) -> float:
    """Per-observation Sharpe (mean/sd). Multiply by √periods to annualize."""
    mean, sd, _, _ = _moments(returns)
    return mean / sd if sd > 0 else 0.0


# ── Core statistics ─────────────────────────────────────────────────────────

def expected_max_sharpe(n_trials: int, sr_trial_sd: float) -> float:
    """SR0: the Sharpe expected as the best of `n_trials` under the null.

    sr_trial_sd is a STANDARD DEVIATION and must be non-negative. A negative
    value would flip the sign of SR0 and produce a benchmark BELOW zero, which
    a losing strategy would then "beat" — see the guard in evaluate().
    """
    if n_trials < 1:
        raise ValueError("n_trials >= 1")
    if sr_trial_sd < 0:
        raise ValueError(f"sr_trial_sd is a standard deviation, must be >= 0 "
                         f"(got {sr_trial_sd})")
    if n_trials == 1:
        return 0.0
    a = _norm_ppf(1.0 - 1.0 / n_trials)
    b = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return sr_trial_sd * ((1.0 - GAMMA_EM) * a + GAMMA_EM * b)


def deflated_sharpe(sr_hat: float, T: int, skew: float, kurt: float,
                    sr0: float) -> float:
    """DSR probability that SR̂ exceeds the selection benchmark SR0.

    sr_hat and sr0 must be in the SAME per-observation units.
    kurt is non-excess (normal = 3).
    """
    if T < 2:
        return float("nan")
    denom = 1.0 - skew * sr_hat + ((kurt - 1.0) / 4.0) * sr_hat ** 2
    if denom <= 0:
        return float("nan")
    z = (sr_hat - sr0) * math.sqrt(T - 1) / math.sqrt(denom)
    return _norm_cdf(z)


def min_track_record_length(sr_hat: float, skew: float, kurt: float,
                            sr_benchmark: float = 0.0,
                            confidence: float = 0.95) -> float:
    """Observations needed for SR̂ to be significantly above sr_benchmark."""
    if sr_hat <= sr_benchmark:
        return float("inf")
    denom = 1.0 - skew * sr_hat + ((kurt - 1.0) / 4.0) * sr_hat ** 2
    z = _norm_ppf(confidence)
    return 1.0 + denom * (z / (sr_hat - sr_benchmark)) ** 2


# ── Program-wide false-discovery rate (Benjamini–Hochberg) ──────────────────

def benjamini_hochberg(pvalues: Sequence[float], fdr: float = 0.10) -> Dict:
    """BH step-up: which of a family of p-values survive at the given FDR.

    Complements per-test Bonferroni: BH controls the expected PROPORTION of
    false discoveries, the right lens when a program runs many hunts and a few
    pass. Returns the threshold and the surviving ranks.
    """
    m = len(pvalues)
    if m == 0:
        return {"threshold": 0.0, "n_significant": 0, "survivors": []}
    order = sorted(range(m), key=lambda i: pvalues[i])
    thresh = 0.0
    k_max = -1
    for rank, idx in enumerate(order, start=1):
        crit = rank / m * fdr
        if pvalues[idx] <= crit:
            k_max = rank
            thresh = crit
    survivors = [order[i] for i in range(k_max)] if k_max > 0 else []
    return {"threshold": thresh, "n_significant": len(survivors),
            "survivors": survivors, "m": m, "fdr": fdr}


# ── Verdict object ──────────────────────────────────────────────────────────

@dataclass
class DSRVerdict:
    sr_hat_per_obs: float
    sr0_per_obs: float
    T: int
    n_trials: int
    skew: float
    kurt: float
    dsr: float
    passes: bool            # DSR > 0.95 AND survives selection benchmark
    min_trl: float
    note: str

    def to_dict(self) -> Dict:
        return asdict(self)


def evaluate(returns: Optional[Sequence[float]] = None, *,
             sr_hat: Optional[float] = None, T: Optional[int] = None,
             skew: Optional[float] = None, kurt: Optional[float] = None,
             n_trials: int = 1, sr_trial_sd: Optional[float] = None,
             dsr_threshold: float = 0.95) -> DSRVerdict:
    """Full deflated-Sharpe verdict.

    Either pass a `returns` series (moments computed for you) or supply
    sr_hat / T / skew / kurt directly when only summary stats survive.

    sr_trial_sd is the standard deviation of Sharpes across the trials the
    program ran. When unknown, it defaults to sr_hat (a conservative,
    commonly-used stand-in: it assumes the trials were as variable as the
    winner is large).
    """
    if returns is not None:
        _, _, sk, ku = _moments(returns)
        srh = sharpe(returns)
        n = len(returns)
        skew = sk if skew is None else skew
        kurt = ku if kurt is None else kurt
        sr_hat = srh if sr_hat is None else sr_hat
        T = n if T is None else T
    if sr_hat is None or T is None:
        raise ValueError("supply returns, or sr_hat and T")
    skew = 0.0 if skew is None else skew
    kurt = 3.0 if kurt is None else kurt
    # The default stand-in for trial dispersion is |SR̂|. The absolute value is
    # essential: a negative observed Sharpe would otherwise yield a NEGATIVE
    # SR0 benchmark, which the losing strategy then "beats" — certifying a
    # money-loser as validated (measured: SR -0.05 scored DSR 0.999 before this
    # guard). A standard deviation is never negative.
    sd_trials = abs(sr_hat) if sr_trial_sd is None else abs(sr_trial_sd)

    sr0 = expected_max_sharpe(n_trials, sd_trials)
    dsr = deflated_sharpe(sr_hat, T, skew, kurt, sr0)
    trl = min_track_record_length(sr_hat, skew, kurt, sr_benchmark=sr0)
    # A strategy that loses money is never "validated", whatever the DSR says.
    passes = ((dsr == dsr) and dsr > dsr_threshold
              and sr_hat > sr0 and sr_hat > 0.0)

    if sr_hat <= 0.0:
        note = f"SR {sr_hat:.4f} is not positive — nothing to validate"
    elif passes:
        note = f"survives selection over {n_trials} trials (DSR {dsr:.3f})"
    elif sr_hat <= sr0:
        note = (f"SR {sr_hat:.4f} below the best-of-{n_trials} null "
                f"SR0 {sr0:.4f} — indistinguishable from luck")
    else:
        note = (f"SR beats null but DSR {dsr:.3f} < {dsr_threshold} — "
                f"not significant after selection")

    return DSRVerdict(sr_hat, sr0, T, n_trials, skew, kurt, dsr, passes, trl, note)
