---
tags: [measurement]
---
# Paper Trading & the Premium Ratio

Forward validation = the only test with zero hindsight. The [[Covered Call]] tracker sells the model's call on paper each monthly cycle and records `premium_ratio = market quote / model price` at the same expiry.

**Pre-committed rule (set BEFORE data arrives — anti-[[Overfitting]]):** after 2-3 settled cycles, avg ratio ≥0.8 → model income honest → deployable-grade; <0.6 → skew eats the edge → keep plain [[Index Investing]]. First live reading: **0.797**.

Pattern to reuse: every candidate gets a falsifiable forward metric and a decision rule you cannot renegotiate with.
