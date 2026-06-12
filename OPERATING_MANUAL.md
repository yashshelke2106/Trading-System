# Operating Manual — How to Use This System Properly

The system's job (post-audit, 2026-06): it is a **truth-finding instrument and
forward-validation engine**, not a real-money signal source. Used properly it
(a) validates/falsifies strategies cheaply, (b) runs the covered-call paper
test to a pre-committed verdict, (c) keeps you ready to capture any edge that
ever proves real. Used improperly (trading its directional signals with real
money) it loses money with precision — that is measured, not opinion.

---

## Daily (market days, ~5 minutes)

1. **Start the stack once, cleanly** (no duplicate runners):
   `stop_trading.bat` → confirm `tasklist | findstr python` is empty → start.
2. **Covered-call cycle:** `python covered_call_tracker.py`
   (settles any expired cycle, opens the next, captures `premium_ratio`).
3. **Expect ~0 india_swing signals.** That is the strategy's measured honest
   behavior, not a malfunction. Do NOT loosen gates to make signals appear.
4. **Do not act on signals with real money.** Paper collection only.

## Weekly (~15 minutes)

- `python covered_call_tracker.py --status` — premium_ratio trend.
- `python honest_metrics.py` — the honest journal read.
- Check the tail of `logs/metrics_daily.jsonl` — `drift_alert` / `drift_reasons`
  now actually fire; if drift fires, read why before anything else.
- After ANY code change: `python -m pytest tests/ -q` (80 tests guard the
  money-math and the safety flags).

## Per expiry cycle (monthly)

- CC cycle settles automatically on the next tracker run after expiry.
- **After 2–3 settled cycles, apply the PRE-COMMITTED rule** (no renegotiation):
  - avg `premium_ratio >= 0.8` → model income honest → sleeve is
    deployable-grade **if** capital covers one lot (~₹19–20L of NIFTYBEES).
  - `< 0.6` → skew eats the edge → drop the overlay, keep plain index.
  - between → keep collecting cycles.

## Capital rules (outside the code — these matter most)

| Money | Where it goes |
|---|---|
| Needed within 1 year | Safe & liquid (FD / liquid fund). NEVER in the edge hunt. |
| Long-term core | Low-cost index fund SIP (the one real edge you can always capture). Optional factor satellites (momentum / value / low-vol funds). |
| Options sleeve | ONLY if the CC verdict passes AND capital ≥ one lot coverage. Covered calls only — defined risk by construction. |
| Directional F&O from system signals | **Never. This is where the losses came from.** |

## The evidence ladder (for ANY new strategy idea, no exceptions)

1. Hypothesis with a **named counterparty** ("who loses to me and why?").
2. Through the gauntlet: fixed a-priori params, OOS split, net realistic costs,
   vs holding NIFTY (`edge_hunt.py` / `backtest_live_pipeline.py` pattern).
3. Survives → paper-trade forward (tracker pattern) with a pre-committed
   pass/fail metric.
4. Survives → tiny real capital, sized so being wrong is trivial.
5. Only then scale, slowly. Most ideas die at step 2 — that is the system
   working, not failing.

## Hard don'ts (each one is a measured lesson, not a preference)

- Don't set `PAPER_TRADE=False` without a strategy that climbed the full ladder.
- Don't re-enable `LEARNING_ENABLED` (in-session tuning = self-overfitting).
- Don't loosen gates to manufacture trades (scarcity = honest no-edge verdict).
- Don't tune any backtest until it turns green (that's how the losses started).
- Don't trust any PF > 3 (PF~16 mirage); the alarm now fires on it — believe it.
- Don't run two runner processes (shared Dhan key → 429 storm).

## Reference docs

- `AUDIT_AND_EDGE_HUNT.md` — full audit + every falsified strategy + tools list.
- Memory of record: covered-call test passed (Sharpe 0.99 vs 0.72 B&H, both
  halves); first live ratio 0.797; everything directional falsified.
