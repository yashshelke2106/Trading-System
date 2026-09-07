"""The swing trade geometry has ONE definition, and every caller imports it.

Why this exists: on 2026-09-04 the stop was widened 2xATR -> 4xATR in
swing_screen.py, but backtest_swing_7gate.py carried its own hardcoded copy of
the same arithmetic AND was untracked, so the change could not reach it. The
backtest would have kept generating evidence for a strategy the live screen no
longer ran, and nothing would have said so.

That is the same shape as the liquidity-veto fail-open (2026-08-25): a
dependency that was not tracked, so a gate silently ran on stale inputs. This
test closes it structurally — a second hardcoded copy fails the build.
"""
from __future__ import annotations

import ast
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files that legitimately construct target/stop from an ATR multiple.
GEOMETRY_FILES = ["swing_screen.py", "backtest_swing_7gate.py"]

# swing_screen.py is the single source of truth.
SOURCE = "swing_screen.py"


def _read(rel: str) -> str:
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_the_constants_exist_and_are_asymmetric():
    import swing_screen as s
    assert s.TARGET_ATR_MULT == 2.0
    assert s.STOP_ATR_MULT == 4.0
    assert s.STOP_ATR_MULT > s.TARGET_ATR_MULT


def test_backtest_is_tracked_by_git():
    """It was untracked for months, which is why the geometry could diverge.

    An untracked evidence generator cannot receive a fix, cannot be reviewed,
    and does not travel to a worktree — all three bit this repo."""
    import subprocess
    r = subprocess.run(["git", "ls-files", "--error-unmatch",
                        "backtest_swing_7gate.py"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, (
        "backtest_swing_7gate.py is untracked again — it generates the "
        "evidence the swing strategy is judged on. git add it.")


def test_every_geometry_file_imports_the_constants():
    for rel in GEOMETRY_FILES:
        if rel == SOURCE:
            continue
        src = _read(rel)
        assert "STOP_ATR_MULT" in src and "TARGET_ATR_MULT" in src, (
            f"{rel} builds trade geometry but does not reference the shared "
            f"constants from {SOURCE}")


def test_no_file_hardcodes_the_atr_multiplier_in_geometry():
    """Catch `c + sign * 2 * a` and friends re-appearing anywhere."""
    # sign * <number> * <atr-ish name>
    pat = re.compile(r"sign\s*\*\s*\d+(?:\.\d+)?\s*\*\s*(?:a|atr)\b")
    offenders = []
    for dp, dn, fn in os.walk(ROOT):
        # `tests` is skipped for the same reason the repo's other AST audit
        # skips it: this very file quotes the offending pattern in its own
        # docstring, and a test asserting about geometry is not geometry.
        dn[:] = [d for d in dn if d not in {
            ".git", ".claude", "node_modules", "__pycache__", ".venv", "venv",
            "logs", "data", "brain", "trading-ui", "tests"}]
        for f in fn:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            try:
                src = io.open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for m in pat.finditer(src):
                line = src[:m.start()].count("\n") + 1
                rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
                offenders.append(f"{rel}:{line} {m.group(0)!r}")
    assert not offenders, (
        "trade geometry hardcoded instead of imported from swing_screen: "
        + "; ".join(sorted(offenders)))


def test_source_defines_geometry_exactly_once():
    """Two assignments to the same constant would make 'single source' a lie."""
    tree = ast.parse(_read(SOURCE))
    for name in ("TARGET_ATR_MULT", "STOP_ATR_MULT"):
        n = sum(1 for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                for t in node.targets
                if isinstance(t, ast.Name) and t.id == name)
        assert n == 1, f"{name} assigned {n} times in {SOURCE}"
