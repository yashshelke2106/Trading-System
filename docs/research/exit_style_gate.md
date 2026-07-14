# Exit-style gate: "let winners run" ADOPTED for the paper sleeve (2026-07-15)

**Hypothesis (pre-registered from verified-trader lore + our own data):** every
verified great (Turtles, Livermore, Minervini, Druckenmiller) cuts losses fast
and lets winners run; our fixed 2xATR target caps winners, and the 15y trade
log showed the TIME_EXIT bucket (38% of trades) bleeding -24.5bp. Replace the
fixed target with a momentum-hold exit.

**Test:** same entries (any-of-3 pullback-in-uptrend, risk-on, long-only,
100 liquid F&O stocks, 2011-2026, gap-honest, next-open fills):

| Exit style | n | win% | PF | avg (cash .25%) | clust t | H1 / H2 |
|---|---|---|---|---|---|---|
| A fixed 2ATR target, hold<=10 (old) | 14,137 | 52.2 | 1.09 | +18.0bp | +2.17 | +2.23 / +0.88 |
| B chandelier 2.5ATR trail, hold<=20 | 10,027 | 42.5 | 1.22 | +55.2bp | +2.89 | +1.80 / +2.29 |
| **C stop 2ATR; winners ride until close<5DMA; hold<=20** | 14,198 | 62.4 | **1.21** | **+34.2bp** | **+3.57** | **+1.92 / +3.08** |

At futures costs (0.10%): C = +49.2bp, t=+5.72 (H1 +3.44 / H2 +4.60).

**Confirmation gates:**
- Cross-sectional (odd/even stocks): +39.9bp t=+2.69 / +28.5bp t=+2.96 — STABLE
  (this is the gate that killed the deep-entry idea).
- Paired C-minus-A on 2,512 common days: +9.9bp/day, t=+1.55 overall —
  H1 -0.38 (no harm) / **H2 +2.56 (significantly better in the modern era)**.

**Decision: ADOPT exit style C for the swing sleeve (paper).**
Rationale: standalone significance exceeds the incumbent's everywhere including
both stock halves and (uniquely, for the first time in this project) the recent
half alone at cash costs; the paired test shows no era where it hurts. The
sleeve is paper-only, Gate 7 and the decay monitor still govern any funding,
and live paper results will confirm or refute under the same persistence rules
(new baseline +34.2bp).

**Honest caveats:** survivors-only upper bound still applies to LEVELS (the
C-vs-A comparison is less affected, being paired on the same universe); the
paired overall t (+1.55) alone would not clear — adoption leans on the
standalone gates plus era-consistency plus zero-cost-of-change. If live paper
persistence under C reads < 0.5 by ~40 resolved trades, revert to A and record.

Reproduce: scratchpad exit_style_gate.py / exit_confirm_gate.py (session
23dd6e33). Also note: the INSTRUMENT lever (futures 0.10% vs cash 0.25%) is
worth +15bp/trade on any exit style — larger than most signal ideas ever
tested; blocked only by lot-size capital requirements.
