# Cross-verification protocol — Claude × Kimi (or any second model)

Established 2026-07-08. One writer, two reviewers, visible triage.

## Roles
| Who | Role | Never does |
|---|---|---|
| **Claude** | Writer: implements, tests, commits, remembers context, runs statistical gates | Skip triage of external findings |
| **Kimi** | External red-team: attacks changes with fresh eyes, no attachment | Gets write access, credentials, or unsanitized files |
| **You** | Filter: carries bundles across, picks what to pursue | Applies either model's advice without the other's check |

## The loop (after every meaningful change; weekly at minimum)

```
Claude commits work
   → python scripts/cross_review.py --bundle        (secret-scanned package)
   → paste into Kimi (or --api with MOONSHOT_API_KEY)
   → save reply, python scripts/cross_review.py --intake <file>
   → tell Claude: "triage the latest cross-review"
   → Claude verifies each finding vs code/data, VISIBLE verdict table
     (valid → fixed & credited · stale → evidence cited · wrong → test shown)
   → verdicts recorded (docs/research/ for strategy claims)
```

## What gets cross-verified
- **Code changes**: every commit bundle includes diffs + stats.
- **Strategy claims**: any new edge/accuracy claim from EITHER model goes
  through the statistician gate (date-clustered t > 2, both halves, real
  costs, survivorship-complete where possible) before any capital logic.
- **Updates to pre-registered rules** (decay monitor thresholds, PAPER_TRADE,
  risk formulas): require BOTH a Claude justification and a Kimi review in
  the same bundle — these are the crown jewels; single-model changes are
  not allowed.

## Hard rules
1. **No credentials leave this machine.** The bundler secret-scans and
   aborts on any token pattern. Never upload config.py, dhan_token.txt,
   .dhan_*, or raw zips of the repo (this already leaked a token once —
   2026-07-07 audit PDF).
2. **Public repo is the reference** for external reviewers:
   https://github.com/yashshelke2106/fno-signal-terminal
3. **Nothing auto-applies.** Reviewer output is input to triage, never a
   patch. The triage table is the deliverable.
4. **Disagreement resolution:** if Claude and Kimi disagree, the tiebreak
   is a runnable test, not authority. No test possible → the conservative
   option wins (don't trade it / don't change it).

## Precedent
The 2026-07-07 external audit: 10 findings → 2 valid (fixed same day,
credited), 6 stale (evidence cited), 2 artifacts. That triage table format
is the standard.
