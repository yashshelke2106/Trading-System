# RSI-2 final gate — VERDICT: REJECT (not deployable)

Main-thread gate (subagent dropped 4×). Reproducible: `docs/research/_rsi2_gate.py`.
Signal: RSI(2)<10 & close>SMA200; exit first close>SMA5 within 10 bars else 10th-bar
close. Universe: 153 bar_cache names, 2018-2026. Independence unit = entry DATE
(date-clustered sign-flip permutation, 10k reps).

## VERDICT: REJECT

The session's only real candidate fails the deployable-edge gate on two
independent, decisive grounds. It is not a mirage in the gross sense (a real
short-term reversal exists) but it is **not tradeable**.

### 1. The edge is entirely an overnight-gap / close-fill artifact
Entry timing is the whole effect:
- entry at the **signal-day close** (MOC): full-universe +0.176%/trade, t=2.12, perm_p=0.018
- entry at the **next-day open** (the realistic fill a few hours later): +0.019%/trade, **t=0.23, perm_p=0.42 — DEAD**

The bounce happens between the oversold close and the next open; by the time you
could realistically enter, it's gone. Same failure class as the rejected gap-fade
and intraday gap-reversal (open-print / unfillable-price artifacts).

### 2. Net-NEGATIVE on the liquid names where it could actually be traded
The 0.10% cost assumption is only realistic on the most-liquid futures. Restricted
to the top-50 most-liquid names, RSI-2 is net-negative in EVERY variant:
- close-entry, no-overlap: −0.030%/trade, t=−0.26, perm_p=0.60
- close-entry, overlap: −0.125%/trade, t=−1.18
- next-open, no-overlap: −0.151%/trade, t=−1.33

The full-universe significance is carried entirely by less-liquid names whose real
futures spreads are far wider than the assumed 0.10% — the textbook
limits-to-arbitrage signature (same as the midcap-PEAD reject): the edge exists
only where it can't be traded cheaply.

### 3. Both-halves weak even under the favourable (close-entry) reconstruction
H1 t=1.19 (not significant), H2 t=2.03. The effect is concentrated in the later
window; the first half does not independently confirm.

## Reconciliation with the researcher's "alive @0.06-0.10%"
The quant-researcher's PF 1.21 / p=0.0019 used close-entry on the FULL universe,
where it is marginally significant. This gate adds the two decisive cuts the
researcher left open: realistic next-open entry (kills it, t=0.23) and the
top-50-liquid restriction (net-negative). Both independently disqualify it.

## What this can't settle / honest limits
- Survivorship: today's constituents bias all numbers UP, so the reject is robust
  (it fails even with the optimistic universe).
- A genuine close-MOC execution on the liquid subset still nets ~0 to negative —
  not worth deploying capital or even paper-forward-testing as a standalone edge.

## Bottom line for the desk
RSI-2 is real-but-untradeable. Combined with the F&O large-cap rejects (regime,
gap-fade, PEAD) and the midcap-PEAD reject (trapped in illiquidity), the edge hunt
is an exhaustive negative across the accessible universe. The reconstruction
sensitivity (close vs next-open flips it) is the final tell: there is no robust,
tradeable edge here. Strategically this strengthens path #1 — cheap exposure to
proven premia, not a manufactured edge.
