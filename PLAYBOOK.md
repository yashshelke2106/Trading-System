# F&O Operating Playbook

All figures measured 2026-08-06 on point-in-time NSE data, net of real charges,
in this repository. Where a step exists to STOP you doing something, the
measurement that earned it is cited.

## Read first

**No strategy tested here beats owning the market.** Twelve approaches were
measured on clean data — momentum, breakouts, events, OI positioning, option
buying, option selling, IV rank, brackets, seasonality. All landed at or below
zero. A top-100 equal-weight book returned **19.56%/yr net, Sharpe 0.96**. That
is the return source. This playbook is a process for capturing it with
discipline and for killing bad ideas cheaply — not for manufacturing alpha.

## 1. Stock selection

Rank by **median daily rupee turnover**. Never share volume (not comparable
across price levels), never the mean (set by news days you won't trade).
Rebuild: `python build_top50_liquid.py`

| Tier | Names | Use for |
|---|---|---|
| >= Rs 500 Cr/day | 16 | Options. Spreads stay payable. |
| >= Rs 300 Cr/day | 40 | Futures, 1-3 day holds. |
| >= Rs 260 Cr/day | 50 | Research universe only. |

Check `stability` (p25/median) before sizing. Banks and IT majors run 0.73-0.77.
MCX 0.60, PAYTM 0.61, BHEL 0.61 are liquid on event days and thin between them.

**Filters that do NOT work — do not add them back:**
- *"Best month" seasonality.* Strongest real cell (TRENT Aug +12.07%) sits at the
  **0th percentile** of a shuffled null whose typical best is +18.15%. Weaker
  than chance.
- *Liquidity seasonality is real but is not a return signal.* Turnover +17..+22%
  Mar-Jun, -9..-11% Aug-Dec. Governs execution cost, not direction.

## 2. Strategy building — six gates, in order

1. **State the baseline first.** Holding anything liquid 3 days earns +0.186%.
   Beat that or it's a worse way to own the market.
2. **t-test the SPREAD, never the return.** Momentum: t=3.67 vs zero, t=1.97 vs
   universe, t=1.01 point-in-time. Testing a long book against zero measures beta.
3. **Point-in-time data only.** Survivors-only halved momentum's edge
   (+9.01% -> +5.26%/yr).
4. **Neutralise corporate actions.** Bhavcopy is raw; a 1:10 split reads as -90%.
   296 events/400 days. In the short-premium study 21 trades (0.41%) moved the
   mean 17x and flipped t from -0.25 to -3.01.
5. **Sweep the cost assumption.** Cheap-premium selling: +9.6%/yr at 10% costs,
   +1.0% at 20%, negative at 30%. If the verdict flips inside a plausible range,
   there is no verdict.
6. **Shuffle null, then split-sample.** For "best cell in a grid" claims,
   Bonferroni is wrong — it assumes independent tests and ignores that you
   selected a maximum. Permute labels and compare; then check first half
   predicts second.

Six readings were overturned in one day: beta illusion, survivorship, corporate
actions (twice), a short data window, an optimistic cost assumption. **Every
apparent edge died under scrutiny; the beta baseline never moved.**

## 3. Position sizing — fixed order

1. **Can you fund it?** `risk_engine.check_margin_affordable()`. One stock futures
   lot needs Rs 0.9-1.7 **lakh** margin. At Rs 1 lakh capital, 6 of 7 names are
   unfundable and none fit a half-of-capital cap.
2. **Live lot size.** `futures_leg.lot_size_for()`. `config.NSE_LOT_SIZES` had 34
   of 62 entries wrong and 81 names missing (those silently became lot size 1).
3. **Risk budget.** `capital * max_risk_per_trade / (entry - stop)`, rounded down
   to whole lots.
4. **Check the floor breach.** When one lot risks more than the cap the trade is
   still taken. Rs 500k at 1.2% = Rs 6,000 budget, but KOTAKBANK risks Rs 40,000
   — **6.7x over**. Logs to `risk.last_size_breach`. The default does not refuse;
   decide that policy deliberately.

| Capital | Stock futures | Short options | Long options |
|---|---|---|---|
| Rs 1 lakh | unfundable | unfundable | Rs 16.8k |
| Rs 5 lakh | 1 lot | 1 lot | yes |

Below ~Rs 5 lakh, F&O in stocks is not mechanically possible with any risk
discipline. Capital constraint, not a strategy one.

## 4. Trade mechanics

**Expiry — read the contract list, never a weekday rule.** The rule said
Thursday; the exchange lists NIFTY weeklies on Tuesdays and RELIANCE on 25 Aug,
not 27. `nearest_expiry()` reads `scrip_master.nearest_listed_expiry()`.

**Costs — net them or every P&L is fiction.** `core/charges.py`. STT on the sell
leg for futures but BOTH legs for delivery; stamp duty buy-leg only; options
levied on premium not notional; GST on brokerage+exchange+SEBI, not on STT.
Round trip: futures 3.6 bps, delivery 22.2 bps.

**Options — check decay before direction.** `required_daily_move = |theta|/delta`

| Leg | DTE | Decay/day | Drift required |
|---|---|---|---|
| RELIANCE ATM 1320 CE | 19 | 2.47% | 0.100%/day |
| TATASTEEL ATM 190 CE | 19 | 2.89% | 0.139%/day |
| NIFTY ATM 24650 CE | 5 | 10.64% | 0.120%/day |

The market supplies **0.0982%/day** of drift. The hurdle is the entire available
drift, before spreads. Trap: the universe moves 1.468%/day in ABSOLUTE terms —
that is volatility, not drift. Delta needs direction; noise averages to zero.

## 5. Improving over time

- **Journal every signal, including skipped ones.** Outcomes on taken trades only
  is a survivorship study of your own behaviour.
- **Compare to the baseline monthly**, not to zero. In a market compounding
  ~20%/yr, "did I make money" is nearly free. Ask "did I beat holding".
- **Register the hypothesis before the test**, keep the rejections. ~20 closed
  hypotheses here; their value is never being re-run by accident.
- **Never retune on a losing result.** A model trained on this journal picked the
  WORST trades (-3.04%/trade vs -1.20% ungated) and was quarantined, not shipped.

**The asymmetry to internalise:** `EV = P*T - (1-P)*S = 0` at every hit rate,
because rate and payoff move in lockstep. A 10% stop with 4.286% target hits
**74.3%** and returns ~9.4%/yr against ~20% for holding. The 7-in-10 is real; it
costs half the return.

## 6. What to actually do

1. **Deploy the allocation engine.** Index core, real capital, monthly review.
   Only positive-expectancy path measured; works at any capital.
2. **Accept the drawdown or don't start.** 19.56%/yr carries **-43.4%** max
   drawdown. The 200-DMA overlay halves it to -18.4% but costs **6.5%/yr** and
   does not improve Sharpe — risk tolerance, not performance.
3. **Keep F&O paper-only until capital clears Rs 5 lakh.**
4. **Spend research time on new theses with a mechanism**, not re-parameterising
   the twelve that closed. A promising idea will fail one of the six gates — find
   out which in an afternoon, not a quarter.

---
Evidence base: 497 days F&O bhavcopy with OI · 1,878 days point-in-time cash
bhavcopy · 357k daily bars · 18.2M option rows · 402 tests green.
