---
tags: [concept]
---
# Gap Risk

Indian stocks gap overnight (earnings, news). A stop order does NOT fill at the stop price through a gap — it fills at the open, worse.

The old backtest filled stops AT the stop → hid +1.2R of losses. Fixed by [[Gap-Honest Fills]]. Related: the earnings-blackout gate (G7) was dead code while positions held through earnings — pure unpriced gap exposure.

Defenses: defined-risk structures ([[Covered Call]] worst case = shares called away at profit), [[Position Sizing and Risk Limits]], earnings calendars that fail closed.
