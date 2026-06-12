# Generates the Obsidian vault `brain/` — one note per concept/strategy/lesson,
# densely [[wikilinked]] so Obsidian's graph view shows factor relationships.
# Folders drive graph coloring: concepts/ strategies/ measurement/ lessons/.
# Re-runnable (overwrites). Numbers are the project's real measured results.
import os, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain")

NOTES = {

"Home.md": """---
tags: [moc]
---
# Trading System Brain

The map. Open **Graph View** (Ctrl+G) to see how everything connects.

## The two master questions
- [[Edge]] — does money flow to me on average, and why?
- [[Strategy vs Edge]] — rules are not profitability.

## What kills strategies (the four horsemen)
[[Transaction Costs]] · [[Overfitting]] · [[Regime Dependence]] · [[Survivorship Bias]]

## What we built to find truth
[[Honest Measurement]] · [[The Validation Gauntlet]] · [[Out-of-Sample Testing]] · [[Gap-Honest Fills]] · [[The PF-16 Mirage]]

## Strategies tested
Dead: [[17-Detector Vote Stack]] · [[India Swing Strategy]] · [[Breakout Strategies]] · [[Trend Following]] · [[Momentum]] · [[Iron Condor]] · [[VIX-Spike Reversion]] · [[Overnight Drift]]
Close: [[Mean Reversion RSI-2]] · [[Pairs Trading]]
Alive: [[Covered Call]] · [[Index Investing]]

## The lessons
[[Survivor Architecture]] · [[The Evidence Ladder]] · [[Accuracy]] · [[Position Sizing and Risk Limits]]
""",

# ── CONCEPTS ────────────────────────────────────────────────────────────────
"concepts/Edge.md": """---
tags: [concept]
---
# Edge

A **reason you make money on average, after costs** — a property, not a procedure ([[Strategy vs Edge]]).

The one test: *who is on the other side of my trade, and why do they keep losing to me?* No answer → the loser is you.

Real edges have a payer: [[Volatility Risk Premium]] (insurance buyers), [[Index Investing]] (paid to bear equity risk), market-making (impatient traders pay the spread).

Edges die through [[Transaction Costs]] (we measured signals that flip sign with cost), [[Regime Dependence]], and crowding. [[Accuracy]] decides how much of an edge you *keep* — it cannot create one.

Verdict for this project: ~20 strategies through [[The Validation Gauntlet]] → only [[Covered Call]] survived.
""",

"concepts/Strategy vs Edge.md": """---
tags: [concept]
---
# Strategy vs Edge

**A strategy is a machine. An edge is whether the machine's output is worth more than its fuel.**

Every strategy produces trades; only some have an [[Edge]]. You cannot tell which by looking at the rules — only [[Honest Measurement]] over many trades reveals it.

The dangerous quadrant: elaborate strategy + no edge = professional-feeling losses ([[India Swing Strategy]], [[17-Detector Vote Stack]]). The surprising quadrant: edge + almost no strategy = [[Index Investing]] — and it wins.

Right order of operations: find the edge first (named counterparty), build the harvesting strategy second ([[Covered Call]] did it in this order — the only survivor).
""",

"concepts/Expectancy.md": """---
tags: [concept]
---
# Expectancy

`E = Win% × AvgWin − Loss% × AvgLoss − Costs` — the sign of this number IS the verdict on a strategy.

Realized result ≈ [[Edge]] − [[Transaction Costs]] − errors. [[Accuracy]] removes the error term only.

Win rate alone misleads: [[Iron Condor]] won 75% of trades with **negative** expectancy. [[Covered Call]] wins by Sharpe, not by win rate.

High [[Position Sizing and Risk Limits|sizing]] on negative expectancy = guaranteed ruin; on zero expectancy = slow bleed via costs.
""",

"concepts/Transaction Costs.md": """---
tags: [concept]
---
# Transaction Costs

The #1 killer in this project. Brokerage + STT + exchange fees + GST + **slippage** + spread ≈ 0.06% (liquid futures) to 0.30% (midcaps) round-trip.

The decisive measurement ([[Mean Reversion RSI-2]]): same signal, PF **1.13 @ 0.06%** cost vs **0.99 @ 0.20%** — *cost decides who has the [[Edge]] on identical trades.*

Killed: [[Overnight Drift]] (real +0.061%/day signal → −10%/yr after daily trading costs), [[Mean Reversion RSI-2]], thinned [[Pairs Trading]], and the wing-cost variant killed [[Iron Condor]].

Survivor logic: [[Covered Call]] needs no hedge purchase (shares cover it) and trades 1×/month; [[Index Investing]] trades ~never. **Low turnover is a structural edge component.**
""",

"concepts/Overfitting.md": """---
tags: [concept]
---
# Overfitting

Tuning rules until the backtest looks good = memorizing the past, not predicting the future.

Project evidence: parameters tuned on 30 stocks **reversed sign** on 152 ([[India Swing Strategy]] RSI zones); an in-session "adaptive learner" mutated params every 10 scans against its own journal — frozen permanently.

Antidotes: fixed a-priori parameters, [[Out-of-Sample Testing]], [[Multiple Testing]] awareness, pre-committed decision rules ([[Paper Trading and Premium Ratio]]).

The trap shape: every "improvement" feels like progress while expectancy stays negative — see [[Strategy vs Edge]].
""",

"concepts/Survivorship Bias.md": """---
tags: [concept]
---
# Survivorship Bias

Testing on today's stock list = testing only winners that survived → inflated results.

Seen twice: backtests on the current F&O universe (optimistic by construction), and "learning from internet trading gurus" — the visible winners of a game whose thousands of losers went silent ([[Survivor Architecture]]).

Rule used here: a **negative** result on a survivor-biased universe is decisive; a **positive** one gets discounted and re-tested. The equal-weight basket "beating" NIFTY in the bake-off was this bias, not skill.
""",

"concepts/Regime Dependence.md": """---
tags: [concept]
---
# Regime Dependence

A strategy that only works in one market regime has no [[Edge]] — it has beta with extra steps.

Caught three times by split-sample testing ([[Out-of-Sample Testing]]):
- [[Breakout Strategies]]: IS PF 1.29 → OOS 0.66 when NIFTY turned down. Breakouts = leveraged market direction.
- [[VIX-Spike Reversion]]: +0.27%/trade in the 2022-26 bull → **−0.75%/trade through COVID** (its worst trades were all March 2020).
- [[Pairs Trading]]: OOS halves Sharpe −0.50 / +1.37 — flickering, not stable.

Pass bar everywhere: positive in BOTH halves, including a drawdown regime.
""",

"concepts/Multiple Testing.md": """---
tags: [concept]
---
# Multiple Testing

Test 20 ideas → ~1 looks great by pure chance (5% × 20). The more you search, the more lucky mirages you find.

Applied: the 7-hypothesis [[The Validation Gauntlet|edge battery]] flagged 3 "passes" — interrogation killed all 3 ([[Overnight Drift]] = cost mirage, reversal = known cost-marginal, [[VIX-Spike Reversion]] = bull artifact).

Rule: a single pass is a **lead to re-test**, never an edge. Uniform behavior across variants is stronger evidence than one winner (all 5 breakouts failing together = real verdict; all 3 covered-call strikes passing together = real effect).
""",

"concepts/Volatility Risk Premium.md": """---
tags: [concept]
---
# Volatility Risk Premium (VRP)

Option buyers systematically overpay for protection → **implied vol > subsequent realized vol**. The seller earns the gap. A real premium with a named counterparty (insurance buyers).

Measured (India VIX vs NIFTY realized, 2019-2026 incl COVID): positive 77-80% of periods, weekly HAC t-stat **2.67** (real), but tail brutal — worst period 18-32× the mean premium, kurtosis +12 to +23.

Harvest attempts: [[Iron Condor]] **failed** (wing cost eats the premium); [[Covered Call]] **succeeded** (shares are the hedge — no wing to buy). Same premium, different structure → opposite outcomes. Structure decides harvestability, not the signal.

Related decay force: [[Theta Decay]].
""",

"concepts/Theta Decay.md": """---
tags: [concept]
---
# Theta Decay

Options lose time-value daily. For option BUYERS it is a tax; for sellers it is income ([[Volatility Risk Premium]]).

Project's brutal measurement: on trades where the directional view was RIGHT, median spot +1.13% but the bought option still lost −20.4%. 911-trade journal: stops fired on premium decay, not price — why the system moved from options to futures, and why [[The PF-16 Mirage]] (premium-% math) was so misleading.

Flip side: [[Covered Call]] collects theta instead of paying it.
""",

"concepts/Gap Risk.md": """---
tags: [concept]
---
# Gap Risk

Indian stocks gap overnight (earnings, news). A stop order does NOT fill at the stop price through a gap — it fills at the open, worse.

The old backtest filled stops AT the stop → hid +1.2R of losses. Fixed by [[Gap-Honest Fills]]. Related: the earnings-blackout gate (G7) was dead code while positions held through earnings — pure unpriced gap exposure.

Defenses: defined-risk structures ([[Covered Call]] worst case = shares called away at profit), [[Position Sizing and Risk Limits]], earnings calendars that fail closed.
""",

"concepts/Position Sizing and Risk Limits.md": """---
tags: [concept]
---
# Position Sizing & Risk Limits

Survival is engineered, not hoped for ([[Survivor Architecture]]).

This system's limits: risk/trade = 1.2% of capital via `qty = risk₹ / |entry−stop|`; daily kill-switch 4% **including open MTM** (was realized-only — a book down 55% on open positions could keep trading); max 2 positions/sector (correlation cap); 2 consecutive losses → halt; per-symbol same-day re-entry block.

Sizing cannot fix negative [[Expectancy]] — it only sets the speed of the outcome. With an [[Edge]], sizing is what lets you survive to collect it ([[Gap Risk]], tail events).
""",

# ── STRATEGIES ──────────────────────────────────────────────────────────────
"strategies/17-Detector Vote Stack.md": """---
tags: [strategy, dead]
---
# 17-Detector Vote Stack — DEAD

The original engine: 17 pattern detectors (EMA cross, VWAP, RSI zones, NR7, supertrend, flags, supply/demand zones, PVSRA, WAE...) each vote long/short; ≥5 votes + 3-vote lead = signal.

**Why it failed:** the votes correlate — they all measure trend direction. Stacking correlated indicators adds confidence, not information ([[Overfitting]]). Live: 30% WR; Grade-S (highest conviction) was the WORST bucket — the ranking sorted noise.

Lesson feeding [[India Swing Strategy]]: replace voting with sequential binary gates. (Also failed — the problem was never the architecture; it was no [[Edge]].)
""",

"strategies/India Swing Strategy.md": """---
tags: [strategy, dead]
---
# India Swing Strategy — DEAD

The flagship: 11 sequential gates (NIFTY regime → daily trend stack → pullback w/ rejection → reversal candle + 2× volume → structural pivot stop → RSI/RS quality → sector → earnings → delivery → sector-leader rank → ML filter). 1.5R target.

**Numbers:** committed backtest PF **0.72** (−0.54%/trade); [[Gap-Honest Fills]] re-test PF **0.50**; full live-parity config ≈ **0 trades** — eleven ~35%-pass gates multiply to nothing.

**Why it failed:** each gate was curve-fit to delete past losers ([[Overfitting]]). With no [[Edge]], tightening filters chokes trades without creating profit — an edgeless system tightened honestly converges to silence. Signal scarcity was the honest verdict, not a bug.
""",

"strategies/Breakout Strategies.md": """---
tags: [strategy, dead]
---
# Breakout Strategies — DEAD (all five)

User hypothesis: "true breakouts" on F&O stocks, filtered by volume/compression/retest. Tested: Donchian-20, +1.5× volume, squeeze, 52-week-high, breakout-retest — same gap-honest exit for all, ~3,400 OOS trades.

**All five negative OOS** (PF 0.64–0.82, exp −0.42 to −0.95%/trade). The filters changed nothing (b20_vol ≈ b20) — there is no "true vs false breakout" to separate.

The tell: retest variant IS PF 1.29 → OOS 0.66 = [[Regime Dependence]]. Breakouts are leveraged market beta; win rates fell uniformly 35%→26% when NIFTY turned. Regime-gating already falsified too (G0 existed in [[India Swing Strategy]]; still PF 0.72).
""",

"strategies/Trend Following.md": """---
tags: [strategy, dead]
---
# Trend Following (single-market) — DEAD

close > SMA200 filter, long-only, per name. **Trend-filtered +1.0%/yr (Sharpe 0.14) vs always-in buy&hold +12%/yr (Sharpe 0.98).** The filter destroyed value.

Nuance: diversified trend-following across 50+ uncorrelated markets is the best-documented strategy in the literature — its power is **breadth**. On a single equity market it collapses to a lagging timing filter. Best-in-literature ≠ accessible-in-your-setup ([[Survivor Architecture]]).

This empirically refuted the system's core premise — every engine here was trend-following at heart.
""",

"strategies/Momentum.md": """---
tags: [strategy, dead]
---
# Cross-Sectional Momentum — DEAD (here)

Jegadeesh-Titman 12-1: rank by 12-month return skipping last month, long top tercile monthly. The most robust documented equity anomaly globally.

Here: **−5%/yr long-only (Sharpe −0.34), negative in both halves**, vs NIFTY +1%. On a survivor-biased bull window where it should shine.

Reading: the anomaly's strength is breadth + institutional cost; a 30-150 name single-country universe at retail cost doesn't carry it. Buy it instead via a momentum index fund ([[Index Investing]]).
""",

"strategies/Mean Reversion RSI-2.md": """---
tags: [strategy, close]
---
# Mean Reversion (Connors RSI-2) — REAL SIGNAL, COST-KILLED

Buy RSI(2)<10 while close>SMA200, exit close>SMA5, 10-day stop. 3,447 real-OHLC trades.

**The signal is real and temporally stable** (gross PF 1.19; held-out half PF 1.34 ≈ first half). **But:** PF 1.13 @0.06% cost → 0.99 @0.20% → breaks even ~0.18-0.20% round-trip — [[Transaction Costs]] in one picture.

Liquid-tier attempt: top-10 names had a BIGGER gross effect (PF 1.36) and cheap costs — but the held-out half collapsed to 0.99 ([[Regime Dependence]] + [[Multiple Testing]]). Exactly what theory predicts: short-term reversal is real and largely cost-arbitraged.
""",

"strategies/Pairs Trading.md": """---
tags: [strategy, close]
---
# Pairs / Stat-Arb — CLOSEST MISS

Market-neutral: cointegrated same-sector pairs (ADF on log-spread residual, half-life 3-40d), z-score entry |z|>2, exit |z|<0.5. Pairs selected on first 60% of data, traded BLIND on the rest ([[Out-of-Sample Testing]]).

17 economically-real pairs (PSU banks, metals, IT, refiners). OOS @0.20% cost: 178 trades, WR 69%, PF 1.53, **maxDD only −2.7%** (market-neutrality works), Sharpe 0.60.

**Fails the bar:** OOS halves Sharpe **−0.50 / +1.37** — unstable ([[Regime Dependence]]), thin (+2.3%/yr), and India requires both legs as stock futures (more [[Transaction Costs]]) + cointegration-break tail risk. Status: paper research only.
""",

"strategies/Iron Condor.md": """---
tags: [strategy, dead]
---
# Iron Condor — DEAD

The defined-risk [[Volatility Risk Premium]] vehicle: sell ~1σ strangle + buy protective wings, hold to expiry. BS-priced legs, 7yr incl COVID.

**Mean −1.8% of risk per trade, negative in BOTH halves (IS −2.1% / OOS −1.2%), despite a 75% win rate** — the seductive income-strategy trap ([[Expectancy]]).

Why: the wings (tail insurance you cannot skip) cost more than the thin premium they protect. Compare [[Covered Call]]: same premium, but the shares ARE the hedge → no wing cost → it passes. Structure decides harvestability.
""",

"strategies/VIX-Spike Reversion.md": """---
tags: [strategy, dead]
---
# VIX-Spike Reversion — DEAD (bull artifact)

Buy NIFTY after a fear spike (VIX 20-day high + 5% jump), hold 5 days. Looked good 2023-26: 70% WR, +0.67%/trade, stable halves.

Extended through COVID: **−0.10%/trade overall, −0.75%/trade 2019-21; its five worst trades were ALL Feb-Mar 2020** — buying fear spikes while the market kept crashing.

The textbook case of [[Regime Dependence]] + [[Multiple Testing]]: every dip bounced in the test window, so "buy fear" looked like an edge. The crash regime is the test that matters.
""",

"strategies/Overnight Drift.md": """---
tags: [strategy, dead]
---
# Overnight Drift — REAL SIGNAL, UNHARVESTABLE

Equities earn returns overnight, not intraday: measured overnight +0.061%/day vs intraday −0.023%/day (gross Sharpe ~1.8 — verified NOT a data artifact).

But harvesting requires trading every name at open AND close daily: at 0.10%/day round-trip → **−9.7%/yr**. Only fantasy costs (0.05%) scrape positive.

The purest example of [[Transaction Costs]] deciding everything: the signal exists for everyone; only sub-retail-cost players can own it ([[Edge]]).
""",

"strategies/Covered Call.md": """---
tags: [strategy, alive]
---
# Covered Call — THE SURVIVOR ✅

Hold NIFTY (ETF) + sell monthly 2.5%-OTM call (50-pt grid), settle at expiry. Harvests [[Volatility Risk Premium]] + [[Theta Decay]] with the shares as the hedge — no wing cost (the flaw that killed [[Iron Condor]]).

**Validated:** 83 monthly cycles 2019-2026 incl COVID — Sharpe **0.99 vs buy&hold 0.72**, beat B&H in BOTH halves; ALL three strike variants improved Sharpe (uniformity = real effect, [[Multiple Testing]]); matches decades of BXM buy-write literature.

**Caveats:** model income priced @ India VIX is optimistic (call skew) → now in forward [[Paper Trading and Premium Ratio]] validation (first live ratio **0.797**, threshold ≥0.8). Practicality: one NIFTY lot needs ~₹19-20L of ETF. Worst case = upside capped — structurally cannot blow up ([[Gap Risk]]).
""",

"strategies/Index Investing.md": """---
tags: [strategy, alive]
---
# Index Investing — THE FREE EDGE ✅

The equity risk premium: paid to bear the risk of owning businesses. The one [[Edge]] capturable with zero skill, zero signals, near-zero [[Transaction Costs]] (~0.1-0.2%/yr fund cost). NIFTY ~+11.5%/yr, Sharpe 0.72 over the test window.

Benchmark role: every strategy in [[The Validation Gauntlet]] had to beat THIS to justify existing — almost nothing did. Edge-without-strategy beats strategy-without-edge ([[Strategy vs Edge]]).

Implementation: low-cost index fund SIP (core) + optional factor satellites (momentum/value/low-vol funds — buying [[Momentum]] cheaply instead of trading it). Pledgeable as F&O collateral; the base for [[Covered Call]].
""",

# ── MEASUREMENT ─────────────────────────────────────────────────────────────
"measurement/The PF-16 Mirage.md": """---
tags: [measurement, story]
---
# The PF-16 Mirage

The system reported **PF ~16, 50% WR** while losing money. Root cause: the metric summed **option-premium %** moves (mean +28%/trade — [[Theta Decay]]/IV swings that don't compound), and the drift alarm fired at PF<0.9 — *mathematically unable to trigger* on a metric pinned at 16.

The monitoring was structurally blind. Real strategy truth: PF **0.72**.

Fixes: P&L attribution on directional spot/futures only; premium rows excluded; alarm now ALSO fires on **PF>3** ("impossibly good = your metric is broken"). The founding story of [[Honest Measurement]].
""",

"measurement/Honest Measurement.md": """---
tags: [measurement]
---
# Honest Measurement

The project's core discovery: **the system's job is to tell the truth; trading is a side effect of validated truth.**

Components: trustworthy P&L attribution (born from [[The PF-16 Mirage]]), [[Gap-Honest Fills]], point-in-time data (no look-ahead), one code path (backtest calls the SAME function live runs), [[Out-of-Sample Testing]], cost sweeps ([[Transaction Costs]]), [[Survivorship Bias]] flags, [[Multiple Testing]] control, alarms that can actually fire, 80-test suite guarding it all.

What it solves: every problem of *not knowing*. What it cannot solve: *not having* an [[Edge]] — see [[Accuracy]].
""",

"measurement/Gap-Honest Fills.md": """---
tags: [measurement]
---
# Gap-Honest Fills

If a bar OPENS beyond your stop, you fill at the OPEN — worse than the stop price ([[Gap Risk]]). The old backtest filled at the stop and hid **+1.2R** of losses; intrabar ambiguity resolved stop-first (conservative).

Impact: [[India Swing Strategy]] PF 0.72 → **0.50** under honest fills. Every later harness ([[Breakout Strategies]], bakeoffs) used this engine, verified by an offline selftest.

Principle: simulate the fill the market would actually give, not the one your rules wish for.
""",

"measurement/Out-of-Sample Testing.md": """---
tags: [measurement]
---
# Out-of-Sample Testing

Parameters/selection fixed on the FIRST part of history; verdict ONLY on the held-out remainder. The single strongest weapon against [[Overfitting]].

Kills it delivered: [[Breakout Strategies]] (IS 1.29 → OOS 0.66), liquid-tier [[Mean Reversion RSI-2]] (full PF 1.30 → H2 0.99), [[Pairs Trading]] instability (−0.50/+1.37), [[VIX-Spike Reversion]] (COVID extension).

Pass bar used: positive in BOTH halves + beats [[Index Investing]] + survives [[Transaction Costs]] + plausible (not "too good" — see [[The PF-16 Mirage]]).
""",

"measurement/The Validation Gauntlet.md": """---
tags: [measurement]
---
# The Validation Gauntlet

The standard pipeline every idea must survive — ~20 entered, **19 died, 1 passed** ([[Covered Call]]).

1. Hypothesis with a named counterparty ([[Edge]])
2. Fixed a-priori params — zero tuning ([[Overfitting]])
3. [[Gap-Honest Fills]] + realistic cost sweep ([[Transaction Costs]])
4. [[Out-of-Sample Testing]] incl. a crash regime ([[Regime Dependence]])
5. Benchmark = holding NIFTY ([[Index Investing]])
6. [[Multiple Testing]] discount on any pass
7. Forward paper test with a pre-committed rule ([[Paper Trading and Premium Ratio]])

A falsification is a SUCCESS of this pipeline, not a failure of the project — each "no" cost compute instead of capital.
""",

"measurement/Paper Trading and Premium Ratio.md": """---
tags: [measurement]
---
# Paper Trading & the Premium Ratio

Forward validation = the only test with zero hindsight. The [[Covered Call]] tracker sells the model's call on paper each monthly cycle and records `premium_ratio = market quote / model price` at the same expiry.

**Pre-committed rule (set BEFORE data arrives — anti-[[Overfitting]]):** after 2-3 settled cycles, avg ratio ≥0.8 → model income honest → deployable-grade; <0.6 → skew eats the edge → keep plain [[Index Investing]]. First live reading: **0.797**.

Pattern to reuse: every candidate gets a falsifiable forward metric and a decision rule you cannot renegotiate with.
""",

"measurement/Accuracy.md": """---
tags: [measurement, concept]
---
# Accuracy

Accuracy = the system does exactly what it claims: honest metrics, correct data, real fills, enforced limits, tested math.

**Realized ≈ [[Edge]] − [[Transaction Costs]] − Errors.** Accuracy deletes the Errors term only. Perfectly accurate + no edge = losing *precisely* at the cost rate (the disciplined roulette player).

What it solved here: self-deception ([[The PF-16 Mirage]]), false confidence ([[Gap-Honest Fills]]), wrong sizing (stale lot sizes), blowup paths (MTM kill-switch), silent decay (firing alarms), unknowable expectancy (one code path).

What it can't do: be stacked into an edge — errors subtract with a floor at zero; edge must be paid by a counterparty. Accuracy decides how much edge you KEEP.
""",

# ── LESSONS ─────────────────────────────────────────────────────────────────
"lessons/Survivor Architecture.md": """---
tags: [lesson]
---
# Survivor Architecture

What the real winners share (market makers, Renaissance, AQR, Universa, the casino, Buffett):

1. **Paid FOR something** — liquidity, insurance, risk-bearing, patience. Never "predicted direction" ([[Edge]]).
2. **Survival engineered first** — [[Position Sizing and Risk Limits]] designed before trade #1.
3. **Cost obsession** ([[Transaction Costs]]).
4. **One niche, deeply** — structural advantage only.
5. **Edge measured continuously, retired when dead** ([[Honest Measurement]]).

Retail-accessible survivor games: Buffett's (→ [[Index Investing]]), covered insurance-selling (→ [[Covered Call]]), casino discipline (→ the risk engine). Inaccessible: out-predicting markets on public daily bars — that's racing Renaissance barehanded ([[Trend Following]], [[Momentum]]).

Warning: learning from internet "winners" = [[Survivorship Bias]] — how the [[17-Detector Vote Stack]] got built.
""",

"lessons/The Evidence Ladder.md": """---
tags: [lesson]
---
# The Evidence Ladder

Capital follows evidence, never hope:

1. Hypothesis with named counterparty ([[Edge]])
2. [[The Validation Gauntlet]] (most ideas die here — correctly)
3. Forward paper with a pre-committed metric ([[Paper Trading and Premium Ratio]])
4. Tiny real capital (being wrong must be trivial)
5. Scale slowly

Current rungs: [[Covered Call]] on rung 3 · [[Pairs Trading]] parked at 2 · [[Index Investing]] needs no ladder · everything else fell off at 2.

Hard rules: never loosen gates to manufacture trades; never tune a backtest green; money needed within a year never enters the ladder at all.
""",
}

def main():
    os.makedirs(ROOT, exist_ok=True)
    for rel, content in NOTES.items():
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    print(f"brain/ written: {len(NOTES)} notes")
    # quick link integrity check: every [[link]] must resolve to a note name
    import re
    names = {os.path.splitext(os.path.basename(k))[0] for k in NOTES}
    bad = []
    for rel, content in NOTES.items():
        for m in re.findall(r"\[\[([^\]|#]+?)(?:\|[^\]]*)?\]\]", content):
            if m.strip() not in names:
                bad.append(f"{rel}: [[{m}]]")
    print("broken links:", len(bad))
    for b in bad:
        print("  ", b)
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
