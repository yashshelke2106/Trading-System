# Intraday 15m-confirmation entry — PRELIMINARY + pre-registered forward test

**User hypothesis (2026-07-16):** enter on the 5-minute timeframe only after
the first 15-minute candle of entry day confirms the trade direction.

## Preliminary result (cached 5m data: 36 symbols, Apr–Jun 2026, n=322)

| Question | Result |
|---|---|
| Does first-15m confirmation predict the open-entry outcome? | Yes, strongly in-window: confirmed 67.4% win / +43.5bp vs unconfirmed 43.8% / −94.9bp |
| Is the implementable version profitable? (wait, enter at 15m close) | **≈ zero: +0.7bp/trade** — waiting costs ~57.9bp of favorable move, eating the edge |
| Same pattern both directions? | Yes (long +5 vs −95; short +66 vs −94) |

**Honest caveats:** one quarter, one (risk-off) regime, 36 survivors-only
symbols; signals cluster on days so the predictive split is inflated by the
day effect (up day ⇒ most stocks confirm AND most longs win — effective n is
~55 days, not 322). The +0.7bp implementable bottom line is the load-bearing
number.

**Verdict: NOT adopted for entry logic.** The candle carries information but
the market charges ~its full value in entry slippage to act on it.

## Pre-registered forward test (running since 2026-07-16)

`swing_tracker.annotate_confirmations` stamps every journaled paper trade
with its entry day's first-15m candle (first15_open/close, confirm_15m) from
yfinance 15m data. No entry logic changes; evidence accrues daily on the
real journal across regimes.

**Decision rule (fixed now, before the data):** evaluate at **n ≥ 150
resolved annotated trades**. Compute day-clustered difference in net return
between (a) production next-open entries and (b) hypothetical
confirmed-only entries priced at first15_close with the same exits. Adopt a
confirmation gate ONLY if (b) beats (a) with clustered t > 2 net of the
waiting slippage. Otherwise record the reject here and stop re-testing.

Reproduce preliminary: scratchpad confirm15_prelim.py (session 23dd6e33).
