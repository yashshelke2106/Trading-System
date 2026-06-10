# Anti-Mirage Protocol — how to not fool yourself, and not blow up

A standing checklist for any edge/strategy research in this repo. Built from this
system's own caught mirages (see case studies) and cross-reviewed by two model
families (Claude + Codex/GPT-5). Read before starting research; run
`core/research_integrity.py::validate_result()` before believing any positive.

## The two ways research kills you

1. **You fool yourself (a mirage).** Many degrees of freedom → you find the lucky
   configuration and believe it. Two sub-families:
   - *Selection bias* — tried many things, kept the winner.
   - *Look-ahead / execution fantasy* — the backtest did something you can't do.
2. **You're right and still blow up (survival failure).** A real-but-thin edge,
   levered, loads hardest into stressed states where spreads, slippage, margin,
   and correlations all gap together. **The signal was real; the survival model
   was fake.** This is the most common way a disciplined researcher loses money.

Part A defends against (1). Part B defends against (2). Part C is solo-operator
reality. Most people only do Part A — that is why they blow up.

---

## PART A — Research validity (don't fool yourself)

**A0. Mechanism (sanity check, NOT a hard gate).** State who pays you and why
they don't stop. Counterparty stories are cheap and post-hoc, so this *filters*
but does not *approve*. No mechanism = almost certainly noise; good mechanism =
necessary, not sufficient.

**A1. Honest data.**
- *Survivorship*: point-in-time universe incl. delisted/removed names. Today's
  F&O list leaks future eligibility. If you can't fix it, label every number an
  **optimistic upper bound**.
- *Corporate actions*: use adjusted / total-return / proper-contract series.
  ⚠ Clipping or dropping ex-date gaps is only valid if you do **not** hold
  through the event — if you hold through, clipping fabricates fake smoothness.
- *No look-ahead*: every input must have existed at the decision instant.

**A2. Executable timing (the strongest single test).**
- Never rank **and** fill on the same price. Decide on info available *before* the
  fill; execute at the next genuinely tradable price, then measure.
- Timing tests beat slippage models (slippage is arguable; timing is clean).
- For options/illiquid: open prints are sparse/stale — use an instrument-specific
  executable benchmark **and explicit no-fill logic** (a signal you can't fill is
  not a return).
- Assume worse fills on exactly the names your signal loves (extreme movers =
  widest spreads = adverse selection). **Sweep costs**; thin edges die between
  0.05% and 0.15%.

**A3. Matched null.** The null is matched on everything *except* the claimed edge
(correlation/beta/sector/liquidity/vol/turnover). Random-junk nulls give
flattering p-values. For "fewer trades is better" claims, use a **random-thinning
null of equal count**. Also beat the **best simple alternative** — but
**predeclare that baseline set**, or it becomes another search loop.

**A4. OOS is necessary, not sufficient.** IS-select / OOS-test blind; never tune
on OOS. One holdout controls *this* variant's fit, not the *family* of variants
you tried to get here. "Both halves positive" ≠ stable (it's one arbitrary cut).

**A5. Correct for the search — and count the WHOLE search.** Hypotheses aren't
just model params: winsorization thresholds, roll rules, signal lag, stop logic,
sizing, benchmark choice, and data-cleaning are **all** hypotheses. If uncounted,
every correction understates overfit. Use **one** solid method (Deflated Sharpe
or PBO) plus an honest research log — the full White/Hansen/FDR stack is usually
pseudo-rigor for a solo trader. Note: a permutation p has a floor of
`1/(draws+1)` — `p=0.003` from 300 draws is the floor, i.e. meaningless.

**A6. Robustness & concentration.** Is a few names/dates/months carrying it? Use
**block / contiguous-subperiod deletion** (single-date leave-one-out is too weak
under serial dependence). Parameter sensitivity: a real edge survives reasonable
perturbation on every knob — but don't fetishize an arbitrary ±20%. Split by
regime; **beta in a bull ≠ alpha**.

**A7. Economic significance.** Net of *all* frictions vs capital, risk, and
operational burden. Watch **effective sample size** — overlapping/correlated
trades mean far fewer independent bets than the trade count implies.

---

## PART B — Survival (don't blow up even when the edge is real)

**B1. Risk of ruin / sizing.** Size from worst-case path, not average. Kelly is an
upper bound, not a target; fractional Kelly at most. A thin edge (Sharpe < 1) must
**not** be levered — leverage is what converts a real edge into a blow-up.

**B2. Tail & stress path.** Model gap clusters, margin hikes, short-gamma tails,
expiry/settlement events, forced deleveraging. Backtest the **sequence**, not just
the average — drawdowns arrive in runs.

**B3. State-dependent frictions.** A flat cost % is false comfort. Costs and fill
quality degrade exactly when your signal concentrates (everyone wants the same
side in stress). Stress-test costs 2–3× in the worst decile of days.

**B4. Structural / exchange-rule breaks.** NSE F&O edges die from changes to
contract specs, **expiry structure** (stock derivatives moved Thu→Tue, 2025), lot
sizes, settlement (physical delivery), margin, and eligibility. "Generic regime
split" is too weak — watch the rulebook, not just the price regime.

**B5. Portfolio interaction.** Standalone alpha is bad if it doubles your existing
crash/liquidity exposure. Judge edges by their marginal contribution to the whole
book, not in isolation.

---

## PART C — Solo-operator reality

**C1. Execution & data semantics (top solo killer).** With daily bars + EOD
option/OI you will trade non-tradable prices, wrong contract identity, stale
strikes, or revised OI unless you check. Reconcile every backtest fill against
what the broker could actually have done.

**C2. Human re-optimization drift.** Adding ad-hoc filters, turning the system off
after a drawdown and on again later **is live data-snooping** — and it's the
discipline most solo traders fail. Pre-commit rules; log every change with a date
and reason; never re-tune mid-drawdown.

**C3. Tiny-live beats paper.** For retail derivatives, a paper forward-test is
weak (it can't feel fills, slippage, RMS rejects, margin). A real **tiny-capital**
live test is far more informative. Make that the final gate, not paper.

**C4. Keep a kill-log.** Record every dead idea and *why* (this repo's
`AUDIT_AND_EDGE_HUNT.md` is exactly right). It prevents re-deriving mirages and
honestly counts your search (feeds A5).

---

## Minimum viable protocol (if you do nothing else)

1. **Name the counterparty** (A0) — kills data-mining for free.
2. **Matched null + count every hypothesis** (A3, A5) — kills selection bias.
3. **Executable timing + cost sweep** (A2) — kills look-ahead.
4. **Never lever a thin edge; size for the worst path** (B1) — kills the blow-up.
5. **Tiny-live, not paper, as the final gate** (C3).

## Meta-principle

**Default to "no edge" and "you will blow up"; force the data to overturn both.**
The durable asset is not a strategy — it is the apparatus that reliably says "no":
`core/bar_cache.py` (fast iteration), the matched-null / thinning-null harnesses,
and `core/research_integrity.py`. Guard those harder than any signal.

---

## Case studies (this system's own caught mirages)

- **Pairs cointegration (2026-06-09).** v1 permutation gave `p=0.01` → looked real.
  The null was random-junk pairs (too weak). v2 with a **correlation-matched null**
  + roll costs → `p=0.12`, edge gone. *Lesson: A3 (matched null) + A5 (the search
  picked best 8 of ~5,886).*
- **Conditioned overnight (2026-06-09).** Naive long-overnight showed Sharpe 2.86,
  MOM Sharpe 5.30 → looked spectacular. It was **overnight market beta** in a bull
  regime + **non-executable close-print fills**. Market-neutralizing collapsed
  everything except a thin, decaying, cost-fragile MOM residual that still can't be
  filled. *Lesson: A2 (executable timing) + A6 (beta ≠ alpha) + the Sharpe was
  "too good," always a red flag.*
