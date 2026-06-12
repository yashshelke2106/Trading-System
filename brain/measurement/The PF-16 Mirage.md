---
tags: [measurement, story]
---
# The PF-16 Mirage

The system reported **PF ~16, 50% WR** while losing money. Root cause: the metric summed **option-premium %** moves (mean +28%/trade — [[Theta Decay]]/IV swings that don't compound), and the drift alarm fired at PF<0.9 — *mathematically unable to trigger* on a metric pinned at 16.

The monitoring was structurally blind. Real strategy truth: PF **0.72**.

Fixes: P&L attribution on directional spot/futures only; premium rows excluded; alarm now ALSO fires on **PF>3** ("impossibly good = your metric is broken"). The founding story of [[Honest Measurement]].
