---
tags: [concept]
---
# Position Sizing & Risk Limits

Survival is engineered, not hoped for ([[Survivor Architecture]]).

This system's limits: risk/trade = 1.2% of capital via `qty = risk₹ / |entry−stop|`; daily kill-switch 4% **including open MTM** (was realized-only — a book down 55% on open positions could keep trading); max 2 positions/sector (correlation cap); 2 consecutive losses → halt; per-symbol same-day re-entry block.

Sizing cannot fix negative [[Expectancy]] — it only sets the speed of the outcome. With an [[Edge]], sizing is what lets you survive to collect it ([[Gap Risk]], tail events).
