"""
core/portfolio_risk.py — Book-level VaR / Expected Shortfall / Stress Engine

v1, monitoring-only. Call compute_book_risk() from any observer (live_runner,
api_server, dashboard) — it is a pure function with no side effects. It does NOT
alter the gate chain, does NOT place orders, and does NOT mutate positions.

Spec: docs/risk/es_var_stress_spec.md  (Track B, gap #1)
Phase wiring:
  Phase 1 (this build): monitoring-only — caller logs/surfaces BookRisk, no gating.
  Phase 2 (follow-up):  add check_book_es() call inside RiskEngine.can_trade().
  Phase 3 (explicit):   promote worst stress to session-halt, mirroring check_drawdown_halt.

Guard: PORTFOLIO_RISK_CONFIG["enabled"] = False by default (config.py).
  When enabled is False, compute_book_risk() still returns a filled BookRisk —
  "enabled" gates Phase-2/3 hard stops only. The pure numbers are always available
  to observers regardless of the flag. This keeps monitoring live without risk of
  accidental gating.

TODO (v2 prerequisite): attach delta, gamma, vega, theta, iv, entry_prem, spot_ref
  to Position (core/risk_engine.py:14-33) and populate in execution.py:333-347
  from the signal dict (values already present there). Until then v1 recomputes
  Greeks per scenario via BSM on the fly — the correct safe fallback.
  See spec Section 2-B and Section 7.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Data classes (Section 4 of spec)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PositionRisk:
    """Per-position risk decomposition for one open option leg."""
    symbol: str
    option_expiry: str
    premium_at_risk: float          # entry_prem × quantity (₹)
    iv_used: float                  # annualized IV used for repricing
    iv_source: str                  # "journal" | "default"
    scenario_pnl: Dict[str, float]  # {scenario_name: ₹ P&L}


@dataclass
class BookRisk:
    """Aggregate book-level risk snapshot."""
    capital: float
    n_positions: int
    insufficient_data: bool                     # True when journal < min_journal_samples
    correlation_assumption: str                 # "comonotonic_v1"
    # VaR / ES (₹) — None when insufficient_data=True
    var_95: Optional[float]
    var_99: Optional[float]
    es_95: Optional[float]
    es_99: Optional[float]
    # VaR / ES as % of capital
    var_95_pct: Optional[float]
    es_95_pct: Optional[float]
    es_99_pct: Optional[float]
    method: str                                 # "historical_journal" | "none"
    horizon_days: int                           # always 1 for v1
    # Stress scenarios — always populated even when insufficient_data=True
    scenario_book_pnl: Dict[str, float]         # {scenario: ₹ sum}
    scenario_book_pnl_pct: Dict[str, float]     # {scenario: % of capital}
    worst_expiry: Dict[str, float]              # {scenario: worst per-expiry sum ₹}
    per_position: List[PositionRisk] = field(default_factory=list)
    breaches: List[str] = field(default_factory=list)   # Phase 2/3 messages (empty in v1)


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_spot(position, mark_prices: Optional[Dict[str, float]]) -> float:
    """Best-available spot for a position.

    Priority: caller-supplied mark_prices > Position.entry_price (the spot
    level at signal time, which is what the spec says to use as fallback).
    Position.entry_price is the SPOT entry for option trades — not the premium.
    """
    if mark_prices:
        spot = mark_prices.get(position.symbol)
        if spot and spot > 0:
            return float(spot)
    return float(position.entry_price)


def _resolve_tte(position, today: date) -> float:
    """Time-to-expiry in years.

    Uses position.option_expiry (YYYY-MM-DD string). Expiry-day → tte=0 so
    BSM returns intrinsic (the INDUSTOWER pin scenario).
    """
    if not position.option_expiry:
        # Non-option or unknown expiry — use 7 days as fallback (won't be used
        # for meaningful greeks but prevents divide-by-zero in BSM).
        return 7.0 / 365.0
    try:
        exp = date.fromisoformat(position.option_expiry)
        days = (exp - today).days
        return max(days, 0) / 365.0
    except ValueError:
        return 7.0 / 365.0


def _resolve_iv(position, journal_records: List[Dict]) -> Tuple[float, str]:
    """Return (iv_decimal, source_tag).

    Priority:
      1. Latest resolved journal record for same (symbol, option_expiry) that
         has a numeric iv_pct → "journal"
      2. option_translator._estimate_iv(symbol) → likely the _DEFAULT_IV=0.30
         unless a recent chain fetch cached something → "default"

    iv_pct in the journal is stored as a percentage (e.g. 30.0 meaning 30%),
    so divide by 100 to get the decimal iv needed by BSM.
    """
    sym = position.symbol
    exp = position.option_expiry

    # Search journal newest-first for a matching resolved record with iv_pct
    for rec in reversed(journal_records):
        if rec.get("symbol") != sym:
            continue
        if exp and rec.get("option_expiry") != exp:
            continue
        iv_raw = rec.get("iv_pct")
        if iv_raw is not None:
            try:
                iv = float(iv_raw) / 100.0
                if 0.05 <= iv <= 5.0:          # sanity range
                    return iv, "journal"
            except (TypeError, ValueError):
                pass

    # Fallback: option_translator._estimate_iv
    try:
        from core.option_translator import _estimate_iv, _DEFAULT_IV
        iv = _estimate_iv(sym)
        # _estimate_iv returns the _DEFAULT_IV constant (0.30) when no chain
        # data is cached — flag this explicitly.
        source = "journal" if abs(iv - _DEFAULT_IV) > 1e-6 else "default"
        return float(iv), source
    except Exception:
        return 0.30, "default"


def _reprice_leg(
    option_type: str,       # "CE" or "PE"
    spot_new: float,
    strike: float,
    tte: float,
    iv_new: float,
    bsm,
) -> float:
    """Re-price a single option leg via BSM. Returns new premium (₹/share)."""
    if option_type == "PE":
        return bsm.put_price(spot_new, strike, tte, iv_new)
    else:
        return bsm.call_price(spot_new, strike, tte, iv_new)


def _pin_nearest_strike(spot: float, symbol: str) -> float:
    """Nearest valid strike to the current spot (for expiry_pin scenario)."""
    try:
        from core.option_translator import _strike_step
        step = _strike_step(spot, symbol)
    except Exception:
        step = 50  # safe default for NSE large-caps
    return round(spot / step) * step


def _build_scenario_params(cfg: Dict) -> Dict[str, Dict]:
    """Return the per-scenario parameter dict from config (Section 3 of spec).

    Each scenario is a dict with keys:
      spot_shock  — multiplicative factor on spot (1.0 = no change)
      iv_shock    — multiplier on iv (1.0 = no change)
      tte_mode    — "unchanged" | "zero" (expiry_pin) | "unchanged"
      exit_haircut— None or a fraction subtracted from entry_prem (liquidity_drain)
      pin         — True for expiry_pin (spot snapped to nearest strike)
    """
    spread_haircut = cfg.get("spread_haircut", 0.15)
    return {
        "gap_down_5": {
            "spot_shock": 0.95, "iv_shock": 1.0, "tte_mode": "unchanged",
            "exit_haircut": None, "pin": False,
        },
        "vol_spike_vix50": {
            "spot_shock": 1.0, "iv_shock": 1.5, "tte_mode": "unchanged",
            "exit_haircut": None, "pin": False,
        },
        "gap_down_5_vol_spike": {
            "spot_shock": 0.95, "iv_shock": 1.5, "tte_mode": "unchanged",
            "exit_haircut": None, "pin": False,
        },
        "expiry_pin": {
            "spot_shock": 1.0, "iv_shock": 1.0, "tte_mode": "zero",
            "exit_haircut": None, "pin": True,
        },
        "liquidity_drain": {
            "spot_shock": 1.0, "iv_shock": 1.0, "tte_mode": "unchanged",
            "exit_haircut": spread_haircut, "pin": False,
        },
    }


def _compute_position_scenario_pnl(
    position,
    spot: float,
    tte: float,
    iv: float,
    entry_prem: float,
    qty: int,
    scenarios: Dict[str, Dict],
    bsm,
    today: date,
) -> Dict[str, float]:
    """Compute per-scenario P&L (₹) for one position.

    All scenarios in the spec treat the book as option-buyer (long CE for long
    direction, long PE for short direction). P&L = (P_new - entry_prem) × qty.

    For gap_* scenarios the spec says "compute both directions, report the
    worse." For a single-direction book that is always gap-down for CE and
    gap-up for PE. We implement this by ensuring the correct option_type is
    used and the gap_down shock applies — the CE takes the hit.

    For gap_up on a PE book the spec's gap_down_5 already uses 0.95 which
    hurts CE; a pure PE book would be hurt by gap_UP (1.05). We implement the
    worst-of-both for each leg: we compute the shocked scenario and also the
    mirror (spot×1.05 for gap), then report the more negative of the two.
    This matches the spec's "report the worse" language.
    """
    option_type = (position.option_type or "CE").upper()
    pnl: Dict[str, float] = {}

    for name, params in scenarios.items():
        try:
            # ── liquidity_drain: exit at bid (no BSM needed) ─────────────────
            if params["exit_haircut"] is not None:
                haircut = float(params["exit_haircut"])
                exit_price = entry_prem * (1.0 - haircut)
                pnl[name] = (exit_price - entry_prem) * qty
                continue

            # ── expiry_pin: snap spot to nearest valid strike, tte → 0 ──────
            if params["pin"]:
                pinned_spot = _pin_nearest_strike(spot, position.symbol)
                p_new = _reprice_leg(option_type, pinned_spot,
                                     float(position.option_strike or spot),
                                     0.0, iv, bsm)
                pnl[name] = (p_new - entry_prem) * qty
                continue

            # ── BSM re-pricing for all other scenarios ───────────────────────
            tte_new = 0.0 if params["tte_mode"] == "zero" else tte
            iv_new = iv * float(params["iv_shock"])

            # spot shock — apply and also mirror; report worse for gap scenarios
            spot_shocked = spot * float(params["spot_shock"])
            p_shocked = _reprice_leg(option_type, spot_shocked,
                                     float(position.option_strike or spot),
                                     tte_new, iv_new, bsm)
            pnl_shocked = (p_shocked - entry_prem) * qty

            # Mirror shock for "gap_*" (spec: report worse of both directions)
            if name.startswith("gap_"):
                spot_shock_mirror = spot * (2.0 - float(params["spot_shock"]))
                p_mirror = _reprice_leg(option_type, spot_shock_mirror,
                                        float(position.option_strike or spot),
                                        tte_new, iv_new, bsm)
                pnl_mirror = (p_mirror - entry_prem) * qty
                # "worse" = more negative
                pnl[name] = min(pnl_shocked, pnl_mirror)
            else:
                pnl[name] = pnl_shocked

        except Exception as exc:
            log.warning("scenario %s failed for %s: %s", name, position.symbol, exc)
            pnl[name] = 0.0

    return pnl


# ─────────────────────────────────────────────────────────────────────────────
# Historical VaR / ES from signal journal (Section 2-A of spec)
# ─────────────────────────────────────────────────────────────────────────────

def _historical_book_var_es(
    position_pars: List[Tuple],   # [(premium_at_risk, pnl_pct_array), ...]
    capital: float,
    min_samples: int,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float], bool, str]:
    """Return (var_95, var_99, es_95, es_99, insufficient_data, method).

    Aggregation method: comonotonic sum — conservative tail for a book that
    "dies in gaps" (all legs move against at once). This is v1 under the
    assumption of perfect positive correlation in the tail. Surfaced as
    correlation_assumption="comonotonic_v1".

    pnl_pct_array: 1-D numpy array of historical pnl_pct values (% moves on
    premium) from the journal, one element per resolved trade.

    Per-position 1-day loss samples:
        position_loss_i = premium_at_risk × (pnl_pct_i / 100)
    The sign of pnl_pct: positive = gain, negative = loss.
    Loss (positive number) = -gain.

    VaR_95 = 5th percentile of book-loss samples = -book_gain at 5th pct.
    ES_95  = mean of worst 5% of book-loss samples.
    """
    if not position_pars:
        return None, None, None, None, True, "none"

    # Check minimum samples across all positions (use the minimum array length)
    min_len = min(len(pp[1]) for pp in position_pars)
    if min_len < min_samples:
        return None, None, None, None, True, "none"

    # Truncate all arrays to min_len for alignment (comonotonic: same index = same date)
    n = min_len
    book_loss_samples = np.zeros(n)
    for premium_at_risk, pnl_pct_arr in position_pars:
        # pnl_pct is % of premium; premium_at_risk is ₹ at risk
        position_gains = premium_at_risk * (pnl_pct_arr[:n] / 100.0)
        book_loss_samples -= position_gains  # loss = negative of gain

    # VaR = loss at quantile (positive = loss)
    var_95 = float(np.percentile(book_loss_samples, 95))
    var_99 = float(np.percentile(book_loss_samples, 99))

    # ES = mean of losses exceeding the VaR threshold
    tail_95 = book_loss_samples[book_loss_samples >= var_95]
    tail_99 = book_loss_samples[book_loss_samples >= var_99]

    es_95 = float(np.mean(tail_95)) if len(tail_95) > 0 else var_95
    es_99 = float(np.mean(tail_99)) if len(tail_99) > 0 else var_99

    return var_95, var_99, es_95, es_99, False, "historical_journal"


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def compute_book_risk(
    positions,
    capital: float,
    mark_prices: Optional[Dict[str, float]] = None,
    config_override: Optional[Dict] = None,
) -> "BookRisk":
    """Compute aggregate book-level VaR, ES, and deterministic stress metrics.

    Parameters
    ----------
    positions    : list of core.risk_engine.Position (from RiskEngine.get_open_positions())
    capital      : account capital in ₹
    mark_prices  : optional {symbol: current_spot} — used to improve spot accuracy
                   beyond entry_price. Falls back to Position.entry_price when absent.
    config_override : override any key in PORTFOLIO_RISK_CONFIG for testing.

    Returns
    -------
    BookRisk dataclass — pure value object, no side effects.

    Guard
    -----
    PORTFOLIO_RISK_CONFIG["enabled"] gates Phase-2/3 hard stops only.
    This function always returns numbers — "enabled=False" just means no
    breaches are raised and no gating happens downstream. Safe to call
    unconditionally.
    """
    import config as _cfg
    cfg: Dict = dict(getattr(_cfg, "PORTFOLIO_RISK_CONFIG", {}))
    if config_override:
        cfg.update(config_override)

    min_samples: int = int(cfg.get("min_journal_samples", 30))
    spread_haircut: float = float(cfg.get("spread_haircut", 0.15))
    enabled: bool = bool(cfg.get("enabled", False))

    # Rebuild scenario params (includes spread_haircut)
    scenario_params = _build_scenario_params(cfg)

    today = date.today()

    # ── Empty book fast-path ─────────────────────────────────────────────────
    if not positions:
        return BookRisk(
            capital=capital,
            n_positions=0,
            insufficient_data=True,
            correlation_assumption="comonotonic_v1",
            var_95=None, var_99=None, es_95=None, es_99=None,
            var_95_pct=None, es_95_pct=None, es_99_pct=None,
            method="none",
            horizon_days=1,
            scenario_book_pnl={k: 0.0 for k in scenario_params},
            scenario_book_pnl_pct={k: 0.0 for k in scenario_params},
            worst_expiry={k: 0.0 for k in scenario_params},
            per_position=[],
            breaches=[],
        )

    # ── Load journal once ────────────────────────────────────────────────────
    try:
        from core.signal_journal import _load_all
        journal_records: List[Dict] = _load_all()
    except Exception as exc:
        log.warning("portfolio_risk: journal load failed: %s", exc)
        journal_records = []

    # Filter to resolved records with pnl_pct (the only ones useful for hist-sim)
    resolved_records = [
        r for r in journal_records
        if r.get("outcome") is not None and r.get("pnl_pct") is not None
    ]

    # ── BSM instance (one per call — stateless) ──────────────────────────────
    try:
        from core.options_greeks import BlackScholesModel
        bsm = BlackScholesModel()
    except Exception as exc:
        log.error("portfolio_risk: cannot import BlackScholesModel: %s", exc)
        raise

    # ── Per-position calculations ────────────────────────────────────────────
    per_position: List[PositionRisk] = []
    hist_inputs: List[Tuple] = []   # (premium_at_risk, pnl_pct_array)

    # Accumulate per-scenario book totals and worst-expiry tracking
    scenario_book_pnl: Dict[str, float] = {k: 0.0 for k in scenario_params}
    expiry_scenario_pnl: Dict[str, Dict[str, float]] = {}  # {expiry: {scenario: sum}}

    for pos in positions:
        spot = _resolve_spot(pos, mark_prices)
        tte = _resolve_tte(pos, today)
        iv, iv_src = _resolve_iv(pos, resolved_records)

        # premium at risk: Position.premium is the option premium field;
        # entry_prem from journal is equivalent. Use Position.premium when set.
        prem = float(pos.premium) if pos.premium > 0 else 0.0
        if prem <= 0:
            # Last resort: BSM ATM premium so the stress can still run
            strike = float(pos.option_strike) if pos.option_strike else spot
            opt_type = (pos.option_type or "CE").upper()
            try:
                prem = _reprice_leg(opt_type, spot, strike, tte, iv, bsm)
            except Exception:
                prem = 0.0

        qty = int(pos.quantity)
        premium_at_risk = prem * qty

        # ── Scenario P&L for this position ───────────────────────────────────
        scen_pnl = _compute_position_scenario_pnl(
            pos, spot, tte, iv, prem, qty, scenario_params, bsm, today,
        )

        per_position.append(PositionRisk(
            symbol=pos.symbol,
            option_expiry=pos.option_expiry,
            premium_at_risk=round(premium_at_risk, 2),
            iv_used=round(iv, 4),
            iv_source=iv_src,
            scenario_pnl={k: round(v, 2) for k, v in scen_pnl.items()},
        ))

        # Accumulate book totals
        for scen, val in scen_pnl.items():
            scenario_book_pnl[scen] += val

        # Accumulate per-expiry totals
        exp_key = pos.option_expiry or "unknown"
        if exp_key not in expiry_scenario_pnl:
            expiry_scenario_pnl[exp_key] = {k: 0.0 for k in scenario_params}
        for scen, val in scen_pnl.items():
            expiry_scenario_pnl[exp_key][scen] += val

        # ── Historical-sim inputs ─────────────────────────────────────────────
        if premium_at_risk > 0:
            sym_records = [
                r for r in resolved_records
                if r.get("symbol") == pos.symbol and r.get("pnl_pct") is not None
            ]
            if sym_records:
                pnl_pct_arr = np.array(
                    [float(r["pnl_pct"]) for r in sym_records], dtype=np.float64
                )
                hist_inputs.append((premium_at_risk, pnl_pct_arr))

    # ── Worst-expiry subtotals (spec: per-expiry worst-case subtotal) ─────────
    worst_expiry: Dict[str, float] = {}
    for scen in scenario_params:
        # Find the expiry that has the largest *loss* for this scenario
        worst_val = 0.0
        for exp_key, scen_map in expiry_scenario_pnl.items():
            val = scen_map.get(scen, 0.0)
            if val < worst_val:
                worst_val = val
        worst_expiry[scen] = round(worst_val, 2)

    # ── Historical VaR / ES ──────────────────────────────────────────────────
    var_95, var_99, es_95, es_99, insufficient, method = _historical_book_var_es(
        hist_inputs, capital, min_samples
    )

    # % of capital
    def _pct(v):
        if v is None or capital <= 0:
            return None
        return round(v / capital, 6)

    # Round ₹ values
    def _rnd(v):
        return round(v, 2) if v is not None else None

    # ── Scenario book pnl as % of capital ────────────────────────────────────
    scen_pct: Dict[str, float] = {}
    for k, v in scenario_book_pnl.items():
        scen_pct[k] = round(v / capital, 6) if capital > 0 else 0.0
        scenario_book_pnl[k] = round(v, 2)

    return BookRisk(
        capital=capital,
        n_positions=len(positions),
        insufficient_data=insufficient,
        correlation_assumption="comonotonic_v1",
        var_95=_rnd(var_95),
        var_99=_rnd(var_99),
        es_95=_rnd(es_95),
        es_99=_rnd(es_99),
        var_95_pct=_pct(var_95),
        es_95_pct=_pct(es_95),
        es_99_pct=_pct(es_99),
        method=method,
        horizon_days=1,
        scenario_book_pnl=scenario_book_pnl,
        scenario_book_pnl_pct=scen_pct,
        worst_expiry=worst_expiry,
        per_position=per_position,
        breaches=[],   # Phase 2/3 only — never populated in v1
    )
