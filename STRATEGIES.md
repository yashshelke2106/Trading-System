# TRADING SYSTEM - COMPLETE STRATEGY DOCUMENTATION v3.0

## System Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    TRADING PIPELINE                               │
├─────────────────────────────────────────────────────────────────┤
│  1. Scanner        → 2. Market Bias   → 3. Time Filter          │
│         ↓                  ↓                    ↓               │
│  4. Signal Engine  → 5. Volatility VIL → 6. Fake Breakout Filter │
│         ↓                  ↓                    ↓               │
│  7. Order Flow     → 8. Strike Selection → 9. AI Filter         │
│         ↓                  ↓                    ↓               │
│ 10. Trade Ranker   → 11. Risk Engine  → 12. Execution            │
└─────────────────────────────────────────────────────────────────┘
```

---

## CORE MODULES (9 Original)

### MODULE 1: LIQUIDITY SCANNER
- Volume >= 1M, Turnover >= 5M, Delivery % >= 10%
- Sort by volume, take top 15

### MODULE 2: SIGNAL ENGINE
- Breakout, Trend, Range detection via ATR + SMA
- Signal strength >= 20

### MODULE 3: FAKE BREAKOUT FILTER
- Body ratio > 50%, Follow-through >= 2 candles, Rejections < 4

### MODULE 4: ORDER FLOW ANALYSIS
- Aggression (trade), Neutral (maybe), Absorption/Exhaustion (avoid)

### MODULE 5: VOLATILITY VIL
- LOW = avoid, NORMAL = full, HIGH = half size

### MODULE 6: STRIKE SELECTION
- HIGH vol = ITM, NORMAL = ATM, LOW = avoid options

### MODULE 7: AI FILTER
- Probability >= 70% = full, 50-70% = half, < 50% = skip

### MODULE 8: RISK ENGINE
- 2% per trade, 5% daily loss, 4 consecutive loss pause

### MODULE 9: EXECUTION ENGINE
- Limit orders, slippage control, trailing SL

---

## NEW MODULES (Phase A - Context & Selection)

---

## MODULE 10: MARKET BIAS ENGINE

### Purpose
Understand market-wide direction before individual trades

### Bias States
| Bias | Condition | Trade Action |
|------|-----------|-------------|
| STRONG_LONG | Both indices strong up | Favor LONG |
| LONG_BIAS | NIFTY up | Partial LONG |
| NEUTRAL | Mixed signals | Any direction |
| SHORT_BIAS | NIFTY down | Partial SHORT |
| STRONG_SHORT | Both indices down | Favor SHORT |

### Logic
```python
nifty_trend, bank_trend = analyze_trends()

if nifty_trend == STRONG_UP and bank_trend == STRONG_UP:
    bias = STRONG_LONG
elif nifty_trend == STRONG_DOWN and bank_trend == STRONG_DOWN:
    bias = STRONG_SHORT
```

### Signal Filtering
- STRONG_LONG + SHORT signal = REJECT
- STRONG_SHORT + LONG signal = REJECT
- Otherwise = proceed with adjusted confidence

---

## MODULE 11: TIME FILTER

### Session Rules
| Session | Time | Trade Mult | Best For |
|---------|------|------------|----------|
| OPEN | 09:15-09:30 | 0.5x | Avoid |
| FIRST_HOUR | 09:30-10:30 | 1.0x | Best |
| DEAD_ZONE | 11:30-13:30 | 0.3x | No new |
| POWER_HOUR | 14:30-15:15 | 1.2x | Breakouts |
| CLOSE | 15:15-15:30 | 0.5x | Close only |

### Logic
```python
session = get_session()

if session == DEAD_ZONE and not strong_breakout:
    return False
    
if session == POWER_HOUR:
    return True
```

---

## MODULE 12: TRADE RANKER

### Scoring System (7 Components)
| Component | Weight | Score Basis |
|-----------|--------|-------------|
| Liquidity | 15% | Volume + delivery |
| Signal Strength | 20% | Strength score |
| Volatility | 10% | Regime fit |
| Order Flow | 20% | Flow type |
| Breakout Quality | 15% | Quality score |
| Market Bias | 10% | Index alignment |
| Time Filter | 10% | Session fit |

### Verdict Thresholds
| Score | Verdict |
|-------|---------|
| >= 0.75 | EXCELLENT |
| 0.60-0.74 | GOOD |
| 0.45-0.59 | AVERAGE |
| < 0.45 | AVOID |

### Ranking Logic
```python
total = sum(component * weight)
ranked = sorted(signals, key=total, reverse=True)
return ranked[:3]  # Top 3 only
```

---

## MODULE 13: LIQUIDITY SPIKE DETECTION

### Spike Detection
```python
spike_ratio = recent_vol / avg_vol
if spike_ratio >= 1.8:
    spike_detected = True
```

### Spike Types
| Type | Price Move | Interpretation |
|------|------------|----------------|
| TREND_DRIVEN | >2% | Real breakout |
| ACCUMULATION | <0.5% | Institutions buying |
| NEWS_DRIVEN | >3% | Event-driven |
| DISTRIBUTION | Price down | Selling pressure |

### Priority Adjustment
- TREND_DRIVEN: 1.3x score
- NEWS_DRIVEN: 0.8x score
- ACCUMULATION: 1.1x score

---

## MODULE 14: LOSS CLUSTER CONTROL

### Position Sizing
| Loss Streak | Position Mult |
|-------------|---------------|
| 0-1 | 1.0x (normal) |
| 2 | 0.75x |
| 3 | 0.50x |
| 4+ | PAUSE 30 min |

---

## MODULE 15: EXECUTION REFINEMENT

### Entry Types
| Type | Condition | Action |
|------|-----------|--------|
| DIRECT | VWAP aligned | Enter now |
| PULLBACK | Retraced 0.3 ATR | Enter pullback |
| WAIT_VWAP | Away from VWAP | Wait |
| AVOID | No valid setup | Skip |

### Multi-Timeframe
```python
if intraday_trend == daily_trend:
    return 1.0  # Perfect alignment
else:
    return 0.5  # Reduce confidence
```

---

## COMPLETE PIPELINE FLOW

```
Scanner → Market Bias → Time Filter → Signals
    ↓           ↓            ↓          ↓
Volatility → Fake Breakout → Order Flow → Liquidity Spike
    ↓           ↓              ↓           ↓
Strikes → AI Filter → Bias Check → Rank (Top 3)
    ↓           ↓              ↓          ↓
Loss Cluster → Risk Check → Execution Refine → Execute
```

---

## PERFORMANCE EXPECTATIONS

| Scenario | Win Rate | Risk-Reward |
|----------|----------|-------------|
| All filters pass | 65-70% | 1:3 |
| Most filters pass | 55-60% | 1:2 |
| Basic filters only | 45-50% | 1:1.5 |

---

## SYSTEM VERSION HISTORY

### v1.0 - Foundation
Scanner + Signal + Filters

### v2.0 - Intelligence
+VIL + AI + Strike Selection

### v3.0 - Context
+Market Bias + Time Filter + Trade Ranker

### v4.0 - Refinement
+Liquidity Spike + Loss Cluster + Execution

---

## NEXT PHASE B (Optional)

- Options Greeks (Delta, Theta, IV)
- Real ML model (GradientBoosting)
- Multi-timeframe alignment
- News event filtering