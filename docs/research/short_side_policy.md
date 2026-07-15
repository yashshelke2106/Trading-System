# Short-side policy: PAPER BENCH ONLY (2026-07-15)

**User-reported issue (correct):** during risk-off regimes the screen and both
UIs presented SHORT setups as "TRADEABLE now". Funding those would repeat the
old book's failure mode (74% short, 17.8% win rate, the bulk of -Rs166k).

**Evidence (15y symmetric framework test, logs/backtest_15y_trades.csv):**

| Side | n | SL rate | TGT rate | avg/trade | total |
|---|---|---|---|---|---|
| long | 14,137 | 28.1% | 34.8% | +18bp | +2,565% |
| **short** | **2,816** | **34.8%** | **23.9%** | **-125bp** | **-3,522%** |

The Indian market's structural upward drift (equity premium) makes mirrored
short rules net-negative even inside stock downtrends. Symmetric RULES do not
produce symmetric RESULTS - and honest policy follows results.

**Policy (single source of truth = `fundable` flag in swing_screen.py):**
1. Shorts are NEVER a funded recommendation. `fundable = long AND risk_on
   AND not RETIRED`. All surfaces (CLI, Streamlit, Next.js) read this flag.
2. Risk-off regime => FUNDED ACTION: NONE - stand aside in cash (consistent
   with the allocation engine already de-risking).
3. Shorts stay on the PAPER BENCH: journaled + resolved + fed to the learner,
   so the negative-expectancy claim keeps being tested rather than assumed.

**Pre-registered unlock condition (do not weaken casually):** shorts may be
promoted to fundable only if BOTH: (a) the live short bench reaches a
Wilson-95%-lower-bound win rate above 50% with n >= 40 resolved, AND (b) a
fresh 15y-style gate (clustered t > 2, both halves, cross-sectional split)
passes for the short side net of futures costs. Either alone is insufficient.
