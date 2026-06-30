# Factor sleeve — survivorship deflation test (path #1, item #2)

**Question:** is the self-built factor outperformance (momentum 0.77 Sharpe on
today's 153 F&O survivors) real, or survivorship bias? **Answer: materially
inflated — do not trade a self-built factor tilt.**

## Method
Same momentum (6-1) and low-vol (60d) top-30 strategy, monthly rebalance, net of
~0.25% delivery cost, run twice on the bhavcopy archive (2019-01 → 2020-05, 339
days, 1810 symbols):
- **survivorship-complete:** point-in-time top-120 by turnover from the FULL
  bhavcopy — includes DHFL, YESBANK, RCOM, JETAIRWAYS, IBULHSGFIN, ZEEL etc. that
  were liquid in 2019 and later collapsed/delisted.
- **survivors-only:** the same universe restricted to names still in bar_cache
  today (the inflated version).

Reproducible: `docs/research/_factor_survivorship.py`.

## Result
| Factor | survivorship-complete | survivors-only | inflation |
|---|---|---|---|
| Momentum top-30 | CAGR −7.8%, Sharpe −0.54, maxDD −29% | CAGR −1.9%, Sharpe −0.33 | **~+6 pp/yr** |
| Low-vol top-30 | CAGR −8.2%, Sharpe −0.70 | CAGR −9.0%, Sharpe −0.72 | ~negligible |

## Findings
1. **Momentum is materially survivorship-inflated (~6 pp CAGR here).** Removing the
   fallen-angels that the survivors-only universe silently drops cuts the result
   hard. The earlier "0.77 Sharpe" momentum backtest is not trustworthy.
2. **Low-vol is barely affected** — it avoids the volatile names that delist, so
   survivorship matters little for it.
3. **Window caveat:** 2019-20 is short and COVID-shadowed — a known bad regime for
   momentum. The robust takeaway is the DEFLATION magnitude, not the absolute
   negative returns.

## Verdict
Do NOT allocate real money to a self-built factor tilt. Survivorship inflation is
confirmed material. For factor exposure, use real factor-INDEX ETFs (Nifty200
Momentum 30, Nifty Low Vol 30) with live track records. A clean multi-regime
verdict needs the FULL bhavcopy archive (2018-present, on a real machine) + this
same test; the partial-archive demonstration already shows the inflation is real.
