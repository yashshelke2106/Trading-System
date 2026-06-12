---
tags: [strategy, dead]
---
# 17-Detector Vote Stack — DEAD

The original engine: 17 pattern detectors (EMA cross, VWAP, RSI zones, NR7, supertrend, flags, supply/demand zones, PVSRA, WAE...) each vote long/short; ≥5 votes + 3-vote lead = signal.

**Why it failed:** the votes correlate — they all measure trend direction. Stacking correlated indicators adds confidence, not information ([[Overfitting]]). Live: 30% WR; Grade-S (highest conviction) was the WORST bucket — the ranking sorted noise.

Lesson feeding [[India Swing Strategy]]: replace voting with sequential binary gates. (Also failed — the problem was never the architecture; it was no [[Edge]].)
