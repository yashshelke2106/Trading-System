# SPEC: Book-Level VaR / Expected Shortfall / Stress Engine (`core/portfolio_risk.py`)

> Authored by the `risk-analyst` agent (Track B, gap #1). Build target: **v1, monitoring-only,
> `enabled=False` default.** Do not wire any gate/kill-switch until Phase-1 numbers are validated
> against the 4-week paper window.

## 0. Guardrails (verified, do not touch)
- `config.py:17` `PAPER_TRADE = True`.
- `core/risk_engine.py` daily-loss ratio `daily_loss_pct = loss / self.capital` vs `max_daily_loss` (0.04) — intact.
- Expiry 4-layer gate + per-expiry cap (`max_per_expiry=3`) — intact. New engine is **additive monitoring**, must not alter any of these or the GAP #2/#3/#4 checks.

## 1. The honest gap (shapes everything)
True Greeks-based book ES is **NOT** computable on the live book today:
- `Position` (`risk_engine.py:14-33`) carries **no** delta/gamma/vega/theta/iv. Greeks are computed at signal time (`options_greeks.py` → `option_translator.get_option_rec` emits delta/gamma/theta/iv_pct) but `execution.py:333-347` **drops every Greek** when building the `Position`.
- `gamma`/`vega` are **never persisted**; `signal_journal` stores `delta`, `theta`, `iv_pct` only.
- `Position.entry_price`/`sl_price`/`target_price` are **SPOT** levels for option trades; premium economics live in side-car fields (`premium`, journal `entry_prem`). `unrealized_pnl()` is spot-based and **wrong for an option leg** (out of scope here, but noted).

**Two tiers:**
- **v1 (build now, only data that exists):** premium-at-risk + empirical/parametric P&L distribution from the **signal journal**, plus **deterministic spot-shock stress** that re-prices each open option leg through BSM (`options_greeks.BlackScholesModel`) using Greeks recomputed on the fly from `(spot, strike, tte, iv)`.
- **v2 (later):** full parametric delta-gamma-vega-theta book VaR/ES.

**v2 prerequisite (flag, ~15 lines):** add `delta, gamma, vega, theta, iv, entry_prem, spot_ref` to `Position` and populate in `execution.py` from the signal dict (values already present there). Until then v1 recomputes Greeks per scenario via BSM — the correct fallback.

## 2. Method — book 1-day VaR & ES (95% & 99%)

**(A) Historical simulation from the signal journal — PRIMARY for VaR/ES.**
- Source: `signal_journal._load_all()` resolved records with `pnl_pct` (option premium % move).
- Per open position: scale historical `pnl_pct` distribution by premium-at-risk = `entry_prem × quantity` (or `premium × quantity`). Per-position 1-day loss samples = `premium_at_risk × pnl_pct_draw`.
- **Book aggregation:** sample **jointly by date** where journal dates overlap; else fall back to the **comonotonic (perfectly-correlated) sum** — conservative tail for a book that "dies in gaps". Surface as `correlation_assumption: "comonotonic_v1"`.
- VaR_95 = 5th pct of book-loss samples; ES_95 = mean of worst 5%. Same for 99%. Report **rupees and % of capital**.
- Guard: require ≥ `min_journal_samples` (default 30) resolved records, else `insufficient_data=True` and emit only the stress block (never fabricate a VaR).

**(C) Deterministic spot-shock re-pricing — PRIMARY for the gap/gamma tail (the number that matters).**
- Per open option position, recompute premium under each scenario via BSM:
  `P_new = BlackScholesModel().call_price/put_price(spot', strike, tte', iv')`, `spot' = spot×(1+shock)`.
- Per-position scenario P&L = `(P_new − entry_prem) × quantity` (book is option-**buyer**: long CE for long, long PE for short — both long premium).
- `spot`: `mark_prices[symbol]`, fallback `Position.entry_price` (the spot entry). `tte`: `(option_expiry − today)/365`, expiry-day → `tte=0` → BSM returns intrinsic. `iv`: latest journal `iv_pct` for symbol/expiry, else `option_translator._estimate_iv`/`_DEFAULT_IV=0.30`; flag fallback with `iv_source:"default"`.

**(B) Parametric delta-gamma-vega — v2 only.** `dP ≈ δ·dS + ½γ·dS² + ν·dσ + θ·dt`. Defer; keep signature room.

**Why this mix:** option books have fat tails + gamma convexity; Gaussian delta-VaR understates the gap tail. Historical sim keeps the empirical tail; deterministic re-pricing captures gamma convexity exactly. **ES is the headline** for any future kill-switch.

## 3. Stress scenarios (config-driven `STRESS_SCENARIOS`)
Each maps to per-position P&L via BSM re-pricing; book P&L = sum (comonotonic, shared shock).

| Scenario | Spot `dS` | IV `dσ` | tte | Leg math |
|---|---|---|---|---|
| `gap_down_5` | −5% | +0% | unchanged | `P_new=BSM(spot×0.95,K,tte,iv)`; PnL=`(P_new−entry_prem)×qty` |
| `vol_spike_vix50` | 0% | `iv×1.5` | unchanged | re-price at inflated iv; **long options gain** (report, don't over-flag) |
| `gap_down_5_vol_spike` | −5% | `iv×1.5` | unchanged | realistic crash; CE delta loss partly offset by vega |
| `expiry_pin` | spot→nearest strike | n/a | `tte=0` | `P_new=intrinsic`; ATM/OTM legs → premium-to-zero (the INDUSTOWER trap, quantified) |
| `liquidity_drain` | 0% | +0% | unchanged | exit at bid: `P_exit=entry_prem×(1−spread_haircut)`, default 0.15 |

Rules: `gap_*` sign — compute both directions per leg, report the **worse** (CE book killed by gap-down, PE by gap-up; don't assume direction). Pin `step`: reuse `option_translator._strike_step(spot, symbol)`. Every scenario outputs per-position P&L, book total (₹ + % capital), and **worst-expiry subtotal** (group by `option_expiry`).

## 4. Module design — `core/portfolio_risk.py`
**Build fresh; do NOT extend `monte_carlo.py`** (per-signal spot bootstrap, wrong shape). Import `options_greeks.BlackScholesModel` and `signal_journal._load_all`.

```python
@dataclass
class PositionRisk:
    symbol: str; option_expiry: str; premium_at_risk: float
    iv_used: float; iv_source: str            # "journal" | "default"
    scenario_pnl: Dict[str, float]

@dataclass
class BookRisk:
    capital: float; n_positions: int; insufficient_data: bool
    correlation_assumption: str               # "comonotonic_v1"
    var_95: float; var_99: float; es_95: float; es_99: float
    var_95_pct: float; es_95_pct: float; es_99_pct: float
    method: str                               # "historical_journal" | "none"
    horizon_days: int                         # 1
    scenario_book_pnl: Dict[str, float]; scenario_book_pnl_pct: Dict[str, float]
    worst_expiry: Dict[str, float]
    per_position: List[PositionRisk] = field(default_factory=list)
    breaches: List[str] = field(default_factory=list)

def compute_book_risk(positions, capital, mark_prices=None, config_override=None) -> BookRisk: ...
# pure helpers: _reprice_leg, _historical_book_var_es, _resolve_iv, _resolve_spot, _resolve_tte
```
Inputs: open `Position` list (`RiskEngine.get_open_positions()`), `capital`, optional spot `mark_prices`. Output: one JSON-serializable `BookRisk`. **Pure function, no side effects** — does not mutate positions, place orders, or touch the gate chain. That is what makes it safe to land under `PAPER_TRADE=True`.

## 5. Integration — phased
- **Phase 1 (land first, monitoring-only):** call `compute_book_risk` from the live loop's position pass (`live_runner.py` ~631, where `get_open_positions()` is iterated) and/or `api_server.py` health; log + surface on dashboard (`core/dashboard_data.py`). **No gating.**
- **Phase 2 (after numbers trusted):** one `check_book_es()` in `RiskEngine.can_trade()` (mirror GAP #3/#4 pattern), blocks **new entries only** when post-trade `es_99_pct` / worst stress `% capital` exceeds limit. Never force-closes positions.
- **Phase 3 (explicit go-live decision):** promote worst-case stress to a hard session halt, mirroring `check_drawdown_halt` (`risk_engine.py:113-131`).

**Config (`config.py`, new block, conservative defaults):**
```python
PORTFOLIO_RISK_CONFIG = {
    "enabled": False,
    "es_99_limit_pct": 0.06,        # ES99 ceiling = 6% capital (~1.5× max_daily_loss)
    "stress_loss_limit_pct": 0.10,  # worst single scenario ceiling = 10% capital
    "gate_new_entries": False,      # Phase 2
    "kill_switch": False,           # Phase 3
    "min_journal_samples": 30,
    "spread_haircut": 0.15,
    "scenarios": { ... },           # Section 3 table, parameterized
}
```

## 6. Test plan (`tests/test_portfolio_risk.py`)
1. **Disabled-by-default:** `enabled=False` → numbers returned, no `breaches`; `can_trade` byte-identical to today (existing 87-test suite stays green).
2. **Known single long call:** spot 25000, K 25000 CE, iv 0.20, tte 7/365, qty 50 → assert `gap_down_5` PnL within ₹1 of hand-computed `(BSM(23750,25000,tte,0.20)−entry_prem)×50`.
3. **Expiry-pin → premium-to-zero:** `option_expiry=today`, pin → intrinsic 0 → PnL = `−entry_prem×50`.
4. **Vol-spike sign:** long option under `vol_spike_vix50` must be **positive** (guards a sign bug).
5. **Distribution sanity:** `ES_95 ≤ VaR_95`, `ES_99 ≤ ES_95`, `ES_95 ≤ mean(worst 5%)`.
6. **Insufficient data:** < `min_journal_samples` → `insufficient_data=True`, VaR/ES None, stress still populated.
7. **Comonotonic ≥ independent.**
8. **Empty book:** zero positions → all-zero, no crash.
9. **IV fallback flagged:** no journal IV → `iv_source="default"`.

## 7. Prerequisites / what unlocks v2
- v2 prereq: attach Greeks to `Position` (`risk_engine.py:14-33`) + copy in `execution.py:333-347`.
- Missing today: per-symbol return covariance (v1 = comonotonic); live option mark for book MTM (current `unrealized_pnl` is spot-based, mis-states option legs); persisted gamma/vega.
- v1 computes only-what-exists: premium-at-risk; historical-sim VaR/ES from journal `pnl_pct`; deterministic BSM-repriced stress incl. per-expiry concentration. Output states `method` + `correlation_assumption` so it's never mistaken for covariance-based parametric VaR.

## Key files
- `core/risk_engine.py` (`Position` 14-33; `can_trade` 199-229; daily-loss 98-105; drawdown-halt pattern 113-131)
- `core/options_greeks.py` (`BlackScholesModel` — reuse for re-pricing)
- `core/option_translator.py` (`get_option_rec` greeks 414-419; `_strike_step` 117-127; `_estimate_iv`/`_DEFAULT_IV` 67-96)
- `core/signal_journal.py` (`pnl_pct` 197-201; persists delta/theta/iv_pct, not gamma/vega)
- `core/execution.py` (Position construction drops greeks 333-347 — v2 prereq fix here)
- `core/monte_carlo.py` (reviewed, rejected for reuse)
- `config.py` (`RISK_CONFIG` 124-152; `OPTIONS_CONFIG` risk_free_rate=0.065; `PAPER_TRADE` 17)
- New: `core/portfolio_risk.py`, `tests/test_portfolio_risk.py`
