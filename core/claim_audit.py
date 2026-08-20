"""
claim_audit.py — find edge claims in the repo that nothing ever verified.

THE FAILURE THIS EXISTS TO CATCH
--------------------------------
On 2026-08-20 `pairs_program.py` was found describing itself, in its own module
docstring, as:

    "the honest build of the one lead that survived the hunt"

Its rigorous re-test, `pairs_validate_v2.py`, had been written — correctly, with
a matched null and roll costs — and **never run**. When finally executed it
returned p=0.190 and the lead died. The claim had sat in the codebase for weeks
reading like a validated result.

Note what did NOT fail here. `research_gates` existed. `research_integrity`
existed and its own docstring says it "would have caught the pairs mirage". The
hypothesis registry existed. Every piece of machinery worked. **Nothing forced
any of it to run.** A plausible claim shipped because no gate stood in its path.

That is the specific, recurring liability of fast idea generation — mine
especially. Writing a convincing rationale is cheap; running the test that
kills it is not. So this module inverts the burden of proof:

    a file that CLAIMS an edge must point at a CLOSED hypothesis, or it is
    flagged as unverified.

WHAT IT DOES
------------
Scans .py files for claim language ("edge", "alpha", "survived", "Sharpe
1.26", "profitable", "validated"...) and cross-references the hypothesis
registry. A claim is considered backed when the file names a hypothesis id
(H-0NN) that the registry records as closed.

WHAT IT DOES NOT DO
-------------------
It cannot judge whether a claim is TRUE. It only answers "did anyone check?".
An audit finding is a prompt to run the test or delete the sentence, never a
verdict on the strategy.

    python -m core.claim_audit
    python -m core.claim_audit --strict     # exit 1 if anything is unverified
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Phrases that assert an edge exists. Deliberately narrow: "we tested whether X
# has an edge" is a question, "X has an edge" is a claim. Matching too broadly
# would flag every research file and the audit would be ignored — the fate of
# every checklist that cries wolf.
CLAIM_PATTERNS = [
    r"\bthe one lead that survived\b",
    r"\bsurvived the hunt\b",
    r"\b(?:real|genuine|validated|confirmed)\s+(?:edge|alpha)\b",
    r"\bedge is real\b",
    r"\bprofitable\s+(?:strategy|setup|config|edge)\b",
    r"\bSharpe\s*>=?\s*[1-9]\d*(?:\.\d+)?\b",
    r"\bSharpe\s+[1-9]\d*\.\d+\b",
    r"\bbeats?\s+the\s+null\b",
    r"\bdeployable\b",
    r"\bready\s+for\s+(?:live|production|capital)\b",
]

# Language that marks the claim as already adjudicated — a file saying an idea
# was REJECTED is not making a claim, it is recording a closure.
NEGATION_NEAR = [
    r"\bnot\b", r"\bno\b", r"\bfail(?:s|ed)?\b", r"\breject(?:ed)?\b",
    r"\bmirage\b", r"\bartifact\b", r"\bdoes not\b", r"\bcannot\b",
    r"\bclosed\b", r"\bwould have caught\b", r"\bnever\b",
]

# A PRECONDITION is not an assertion. "Set True only until a genuine edge is
# validated" states what would have to become true; flagging it would train the
# reader to ignore the audit, which is how checklists die.
CONDITIONAL_NEAR = [
    r"\buntil\b", r"\bbefore\b", r"\bunless\b", r"\bonce\b",
    r"\brequires?\b", r"\bneed(?:s|ed)?\b", r"\bwould\b", r"\bshould\b",
    r"\bassum\w*\b", r"\bexample\b", r"\be\.g\.",
]

HID_RE = re.compile(r"\bH-\d{3}\b")
SKIP_DIRS = {".git", ".claude", "node_modules", "__pycache__", ".venv",
             "venv", "logs", "data", "brain", "docs", "tests"}


@dataclass
class Finding:
    path: str
    line_no: int
    line: str
    pattern: str
    hids: List[str]
    backed: bool
    reason: str


def _printable(text: str) -> str:
    """Echoed source can hold any Unicode; a Windows cp1252 console cannot
    print it. Degrade the echo rather than crash the audit."""
    enc = (sys.stdout.encoding or "utf-8")
    try:
        text.encode(enc)
        return text
    except (UnicodeEncodeError, LookupError):
        return text.encode(enc, errors="replace").decode(enc, errors="replace")


def _closed_hids() -> Set[str]:
    """Hypothesis ids the registry records as closed (any verdict)."""
    try:
        from core.hypothesis_registry import status
        return {h["id"] for h in status() if h.get("verdict")}
    except Exception:
        return set()


def _iter_py(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _discounted(line: str, span: "tuple[int,int]") -> bool:
    """Is the claim denied, or stated as a precondition rather than a fact?"""
    lo = max(0, span[0] - 90)
    ctx = line[lo:span[1] + 90].lower()
    return any(re.search(p, ctx) for p in NEGATION_NEAR + CONDITIONAL_NEAR)


def audit(root: Optional[str] = None) -> List[Finding]:
    root = root or _ROOT
    closed = _closed_hids()
    compiled = [(p, re.compile(p, re.I)) for p in CLAIM_PATTERNS]
    out: List[Finding] = []

    for path in _iter_py(root):
        # A file whose job is to define or test the claim language itself is
        # not making claims. Skip this module and its test.
        base = os.path.basename(path)
        if base in ("claim_audit.py", "test_claim_audit.py"):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue

        text = "".join(lines)
        file_hids = set(HID_RE.findall(text))
        backing = sorted(file_hids & closed)

        for i, line in enumerate(lines, 1):
            for pat, rx in compiled:
                m = rx.search(line)
                if not m:
                    continue
                if _discounted(line, m.span()):
                    continue
                rel = os.path.relpath(path, root).replace("\\", "/")
                snippet = _printable(line.strip()[:118])
                if backing:
                    reason = f"backed by closed {', '.join(backing)}"
                elif file_hids:
                    reason = (f"names {', '.join(sorted(file_hids))} but the "
                              "registry has no closure for it")
                else:
                    reason = "no hypothesis id anywhere in the file"
                out.append(Finding(rel, i, snippet, pat,
                                   sorted(file_hids), bool(backing), reason))
                break        # one finding per line is enough
    return out


def unverified(root: Optional[str] = None) -> List[Finding]:
    return [f for f in audit(root) if not f.backed]


def report(root: Optional[str] = None) -> str:
    findings = audit(root)
    bad = [f for f in findings if not f.backed]
    ok = [f for f in findings if f.backed]

    lines = ["CLAIM AUDIT — edge claims vs the hypothesis registry", "=" * 66]
    if not findings:
        lines.append("  no edge claims found")
        return "\n".join(lines)

    if bad:
        by_file: Dict[str, List[Finding]] = {}
        for f in bad:
            by_file.setdefault(f.path, []).append(f)
        lines.append(f"\nUNVERIFIED ({len(bad)} claim(s) in {len(by_file)} file(s))")
        lines.append("-" * 66)
        for path, fs in sorted(by_file.items()):
            lines.append(f"  {path}")
            lines.append(f"      {fs[0].reason}")
            for f in fs[:4]:
                lines.append(f"      L{f.line_no}: {f.line}")
            if len(fs) > 4:
                lines.append(f"      ... and {len(fs) - 4} more")
    if ok:
        lines.append(f"\nBACKED ({len(ok)})")
        lines.append("-" * 66)
        for f in sorted(ok, key=lambda x: x.path)[:12]:
            lines.append(f"  {f.path} L{f.line_no}  [{', '.join(f.hids)}]")

    lines.append("\n" + "=" * 66)
    if bad:
        lines.append("  Each unverified claim is a prompt, not a verdict:")
        lines.append("  run the test, or delete the sentence. A claim nothing")
        lines.append("  checked is how pairs (H-019) read as validated for weeks.")
    else:
        lines.append("  Every edge claim points at a closed hypothesis.")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 when any claim is unverified")
    ap.add_argument("--root", default=None)
    args = ap.parse_args(argv)
    print(report(args.root))
    return 1 if (args.strict and unverified(args.root)) else 0


if __name__ == "__main__":
    sys.exit(main())
