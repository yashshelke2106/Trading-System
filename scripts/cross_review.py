"""
cross_review.py — Claude + Kimi cross-verification setup.

Roles (one writer, two reviewers):
  Claude  = implements, tests, commits, remembers (the writer)
  Kimi    = external red-team reviewer (fresh eyes, no attachment)
  You     = filter between them

Flow:
  1. python scripts/cross_review.py --bundle [N]
       Packages the last N commits (default 1) — messages, stats, diffs —
       plus current strategy state into ONE paste-ready markdown bundle
       with a structured attack-prompt. Secret-scanned before writing.
       Output: logs/cross_review/bundle_<date>.md  (also copied to clipboard)
  2. Paste the bundle into Kimi (kimi.com) — or any second model.
  3. Save Kimi's reply to a file, then:
       python scripts/cross_review.py --intake <file>
       Archives it and prints the triage instruction for the next Claude
       session ("triage the latest cross-review").
  4. Claude triages each finding with visible evidence (valid/stale/wrong),
     applies valid ones, records verdicts in docs/research/.

Optional automation: set MOONSHOT_API_KEY and use --api to send the bundle
to Kimi's API directly and save the reply without copy-paste.

HARD RULES baked in:
  - bundle is secret-scanned (JWT/key/token patterns) and refuses to build
    if anything matches
  - gitignored files (config.py, tokens) can never enter a bundle (diffs
    only contain tracked files)
  - no reviewer output is auto-applied; everything goes through triage
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

OUT_DIR = os.path.join("logs", "cross_review")
SECRET_PATTERNS = [
    r"eyJ[A-Za-z0-9_-]{25,}",                # JWT body
    r"BEGIN [A-Z ]*PRIVATE KEY",
    r"[0-9]{9,}:[A-Za-z0-9_-]{30,}",         # telegram-style token
    r"(api_key|access_token|password)\s*=\s*['\"][A-Za-z0-9]{12,}",
]

ATTACK_PROMPT = """# Cross-review request (external reviewer)

You are reviewing changes to a production-grade Indian F&O trading system
(public repo: https://github.com/yashshelke2106/fno-signal-terminal).
Your job is to ATTACK, not to praise. For each area below, report concrete
findings with file/line references where possible.

1. CORRECTNESS — bugs, edge cases, wrong math, broken logic in the diffs.
2. STATISTICAL VALIDITY — any claim of edge/accuracy: could it be curve
   fitting, survivorship, look-ahead, multiple testing, or a selection
   artifact? Our standards: date-clustered t>2, both-halves stability,
   costs included, pre-registered rules.
3. RISK — could any change place unintended orders, oversize a position,
   bypass PAPER_TRADE=True, or weaken a pre-registered rule?
4. SECURITY — credential handling, injection, unsafe file/network use.
5. DECAY/DRIFT — does anything assume market conditions that may not hold?

Format each finding as: [SEVERITY: high/med/low] file — claim — why.
If something is fine, say nothing about it. End with your top-3 list.
"""


def _run(cmd: list) -> str:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def build_bundle(n_commits: int) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    log = _run(["git", "log", f"-{n_commits}", "--stat", "--date=short",
                "--pretty=format:## %h %ad %s%n"])
    diff = _run(["git", "diff", f"HEAD~{n_commits}", "HEAD",
                 "--", ".", ":!*.jsonl", ":!*.csv", ":!*.parquet",
                 ":!package*", ":!*.lock"])
    if len(diff) > 120_000:
        diff = diff[:120_000] + "\n... [diff truncated at 120k chars]"

    state = []
    for f, label in [(os.path.join("logs", "strategy_health.json"), "Strategy health"),
                     (os.path.join("logs", "swing_screen.json"), "Latest screen")]:
        if os.path.exists(f):
            with open(f, encoding="utf-8") as fh:
                content = fh.read()
            if len(content) < 6000:
                state.append(f"### {label}\n```json\n{content}\n```")

    bundle = (ATTACK_PROMPT
              + "\n\n# Commits under review\n" + log
              + "\n\n# Diffs\n```diff\n" + diff + "\n```\n\n"
              + "# Current strategy state\n" + "\n".join(state) + "\n")

    for pat in SECRET_PATTERNS:
        m = re.search(pat, bundle)
        if m:
            print(f"ABORT: bundle matches secret pattern {pat!r} "
                  f"(…{bundle[max(0, m.start()-30):m.start()]}<MATCH>). Nothing written.")
            sys.exit(1)

    path = os.path.join(OUT_DIR, f"bundle_{datetime.now():%Y%m%d_%H%M}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(bundle)
    clip_note = ""
    try:
        if shutil.which("clip"):
            subprocess.run("clip", input=bundle.encode("utf-16-le"))
            clip_note = " (copied to clipboard — paste straight into Kimi)"
    except Exception:
        clip_note = " (clipboard copy failed — open the file and copy manually)"
    print(f"bundle: {path}  [{len(bundle):,} chars, secret-scan clean]{clip_note}")
    return path


def intake(path: str) -> None:
    os.makedirs(os.path.join(OUT_DIR, "reviews"), exist_ok=True)
    dst = os.path.join(OUT_DIR, "reviews",
                       f"review_{datetime.now():%Y%m%d_%H%M}.md")
    shutil.copy(path, dst)
    print(f"archived reviewer response -> {dst}")
    print("\nNext step — tell Claude:")
    print('  "triage the latest cross-review in logs/cross_review/reviews/"')
    print("Claude will verify each finding against the code with evidence")
    print("(valid/stale/wrong), apply valid ones, and record the verdicts.")


def send_api(bundle_path: str) -> None:
    key = os.getenv("MOONSHOT_API_KEY", "")
    if not key:
        print("MOONSHOT_API_KEY not set — use the manual paste flow instead.")
        sys.exit(1)
    import json
    import urllib.request
    body = json.dumps({
        "model": os.getenv("KIMI_MODEL", "kimi-k2-0905-preview"),
        "messages": [{"role": "user",
                      "content": open(bundle_path, encoding="utf-8").read()}],
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(
        os.getenv("KIMI_API_URL", "https://api.moonshot.ai/v1/chat/completions"),
        data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        out = json.load(resp)
    text = out["choices"][0]["message"]["content"]
    tmp = os.path.join(OUT_DIR, "_api_reply.md")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    intake(tmp)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", type=int, nargs="?", const=1, default=None,
                    metavar="N", help="build review bundle from last N commits")
    ap.add_argument("--intake", metavar="FILE", help="archive a reviewer response")
    ap.add_argument("--api", action="store_true",
                    help="with --bundle: send to Kimi API (needs MOONSHOT_API_KEY)")
    a = ap.parse_args()
    if a.bundle is not None:
        p = build_bundle(a.bundle)
        if a.api:
            send_api(p)
    elif a.intake:
        intake(a.intake)
    else:
        ap.print_help()
