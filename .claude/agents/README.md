# The Agent Desk — Institutional Trading Team as Subagents

This directory turns an institutional options-trading org chart into a roster of
Claude Code **subagents**. Each agent embodies one role, is bound to an installed
skill, and is grounded in *this* codebase (NSE F&O signal terminal, Dhan + yfinance,
Python + FastAPI + Next.js).

The **main session is the desk head**: you (or the lead assistant) dispatch work to
these agents with the `Agent` tool. Subagents cannot spawn other subagents, so the
orchestration always flows from the main thread.

## Honest scope (read this first)

These agents automate the **knowledge work** of a quant desk: research, options
pricing, statistical validation, risk analysis, coding, backtesting, monitoring,
docs. They do **not**:

- Replace real low-latency C++/kernel-bypass production infrastructure.
- Get handed live capital unsupervised. `config.py PAPER_TRADE` stays `True` until a
  human explicitly goes live. Every agent treats this as a hard rule.
- Substitute for legal/compliance accountability. A human is the accountable trader.

The current honest state of the system (per audit): **no validated edge** — backtest
PF ≈ 0.72, the PF≈16 figure is a premium-% mirage. The whole desk exists to fix that
*before* anything goes live.

## Org chart

```
                          portfolio-manager  (desk head / capital allocation)
                                   │
        ┌──────────────┬───────────┼───────────────┬─────────────────┐
   ALPHA & VALIDATION   RISK     PRICING        ENGINEERING        PLATFORM
   quant-researcher  risk-analyst options-quant quant-developer  software-architect
   statistician                  financial-eng data-engineer     devops-engineer
   ml-engineer                                 execution-engineer agentic-ai-engineer
                                               backend-engineer   cybersecurity-engineer
                                               frontend-engineer
```

## Roster

| Agent | Role | Skill | Model |
|-------|------|-------|-------|
| `quant-researcher` | Alpha / factor / signal research | quant-analyst, statsmodels | opus |
| `statistician` | Is the edge real? Hypothesis tests, bootstrap, regime | statsmodels | opus |
| `options-quant` | Greeks, IV surface/skew, BS/Heston/MC pricing | quant-analyst | opus |
| `financial-engineer` | Structured sleeves: covered calls, iron condors, VRP | modeling-fx-derivative-pricing | opus |
| `ml-engineer` | Forecasting, feature engineering, model serving | ml-engineer | opus |
| `risk-analyst` | VaR, ES, stress, limits, drawdown, kill switches | risk-manager | opus |
| `portfolio-manager` | Capital allocation, position sizing, expectancy | risk-manager | opus |
| `quant-developer` | Research → production code | backend-patterns, agentic-engineering | sonnet |
| `data-engineer` | Market data pipelines (Dhan/yfinance/NSE/OI) | backend-patterns | sonnet |
| `execution-engineer` | OMS/EMS, TWAP/VWAP/Iceberg, fills | order-execution-patterns | sonnet |
| `backend-engineer` | APIs, OMS, DB, FastAPI | backend-patterns | sonnet |
| `frontend-engineer` | Trading dashboards (Next.js, Streamlit) | ui-styling | sonnet |
| `software-architect` | Overall system design | software-architect | opus |
| `devops-engineer` | Infra, CI/CD, monitoring, deploy | (prompt-only) | sonnet |
| `agentic-ai-engineer` | Builds/maintains the agent system, RAG | agentic-engineering, agentic-jujutsu | opus |
| `cybersecurity-engineer` | Security & compliance review | security-review | sonnet |

## Standard workflow (edge → live)

1. `quant-researcher` proposes a hypothesis + signal.
2. `statistician` validates it is not chance (bootstrap, multiple-testing correction).
3. `options-quant` / `financial-engineer` size the options expression + Greeks.
4. `risk-analyst` stress-tests; `portfolio-manager` allocates and sizes.
5. `quant-developer` + `backend-engineer` ship it behind `PAPER_TRADE=True`.
6. `execution-engineer` models fills honestly (gaps, slippage).
7. `cybersecurity-engineer` + `software-architect` review before any go-live.

No step is skipped. A signal that fails step 2 never reaches step 5.
