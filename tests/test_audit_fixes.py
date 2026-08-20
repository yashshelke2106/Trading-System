"""Two loopholes found by auditing for the bug CLASSES that already bit.

1. A GATE THAT FAILS OPEN. check_margin_affordable returned True from both of
   its exception handlers, so an error did not degrade the check - it deleted
   it, and every position it should have blocked went through unmargined.
   Margin is the binding constraint on this account, which makes "allow on
   error" the most expensive available default.

2. A STALE TABLE READ DIRECTLY. config.NSE_LOT_SIZES is correct for 3 of the
   70 symbols in the live journal: 51 are absent (and silently size at ONE
   SHARE) and 16 are wrong by up to 6x. Four call sites still read it directly
   instead of coming through lot_size_for().
"""

import ast
import io
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 1. the margin gate must fail closed ──────────────────────────────────────

def _engine():
    from core.risk_engine import RiskEngine
    try:
        return RiskEngine(capital=1_000_000)
    except TypeError:
        e = RiskEngine()
        e.capital = 1_000_000
        return e


def test_margin_check_refuses_when_the_estimator_raises(monkeypatch):
    import core.margin as margin

    def boom(*a, **k):
        raise RuntimeError("scrip master unreachable")

    monkeypatch.setattr(margin, "estimate", boom)
    eng = _engine()
    assert eng.check_margin_affordable("RELIANCE", 1400.0) is False, \
        "an unusable margin estimate must refuse, not wave the trade through"


def test_a_refusal_records_why(monkeypatch):
    """A silent refusal is only marginally better than a silent pass."""
    import core.margin as margin
    monkeypatch.setattr(margin, "estimate",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad price")))
    eng = _engine()
    eng.check_margin_affordable("RELIANCE", 1400.0)
    blk = eng.last_margin_block
    assert blk and blk.get("error"), "the reason must be recorded"
    assert "could not be checked" in blk.get("note", "")


def test_margin_check_still_allows_an_affordable_position():
    """Failing closed must not mean refusing everything."""
    eng = _engine()
    # long option = premium only, no SPAN; trivially inside a 10 lakh account
    assert eng.check_margin_affordable(
        "RELIANCE", 1400.0, lots=1, instrument="option", premium=5.0) is True


def test_no_gate_in_the_tree_returns_allow_from_an_except_handler():
    """The scan that found this one, kept as a regression test."""
    SKIP = {".git", ".claude", "node_modules", "__pycache__", ".venv", "venv",
            "logs", "data", "brain", "package", "tmp_stat_test", "tests"}
    GATEISH = ("gate", "check", "filter", "allow", "is_", "can_", "should",
               "valid", "ok_", "permit", "veto", "block", "guard", "eligible",
               "safe", "approve", "qualif", "pass_")

    def permissive(handler):
        for n in ast.walk(handler):
            if isinstance(n, ast.Return):
                v = n.value
                if isinstance(v, ast.Constant) and v.value is True:
                    return True
                if isinstance(v, ast.Tuple) and v.elts:
                    e = v.elts[0]
                    if isinstance(e, ast.Constant) and e.value is True:
                        return True
        return False

    offenders = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in SKIP]
        for f in fn:
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            try:
                tree = ast.parse(io.open(p, encoding="utf-8",
                                         errors="replace").read())
            except Exception:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not any(k in node.name.lower() for k in GATEISH):
                    continue
                for h in ast.walk(node):
                    if isinstance(h, ast.ExceptHandler) and permissive(h):
                        rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
                        offenders.append(f"{rel}:{h.lineno} {node.name}()")
    assert not offenders, "gate(s) fail OPEN on error: " + "; ".join(sorted(set(offenders)))


# ── 2. contract size must come from the live source ──────────────────────────

def test_lot_size_for_answers_without_config(monkeypatch):
    """futures_leg imported config at module scope, so in a git worktree (where
    config.py is untracked) the import raised and EVERY lot collapsed to 1 -
    the exact failure the helper exists to prevent."""
    import core.futures_leg as fl
    monkeypatch.setattr(fl, "config", None)
    assert fl.lot_size_for("NTPC") > 1


def test_lot_size_for_never_returns_zero_or_negative():
    from core.futures_leg import lot_size_for
    for sym in ("NTPC", "", "NOT_A_REAL_SYMBOL_XYZ", None):
        assert lot_size_for(sym) >= 1


def test_no_module_reads_the_stale_lot_map_outside_a_fallback():
    """Direct reads are the bug. A read inside an `except` is the documented
    last resort and is allowed."""
    offenders = []
    for rel in ("core/dashboard_data.py", "core/risk_engine.py",
                "core/signal_tracker.py", "core/agents/quant_agent.py"):
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        lines = io.open(path, encoding="utf-8", errors="replace").readlines()
        for i, line in enumerate(lines):
            if "NSE_LOT_SIZES" not in line or line.lstrip().startswith("#"):
                continue
            # walk back to the nearest non-blank line with less indentation
            indent = len(line) - len(line.lstrip())
            in_fallback = False
            for j in range(i - 1, max(-1, i - 6), -1):
                prev = lines[j]
                if not prev.strip():
                    continue
                if (len(prev) - len(prev.lstrip())) < indent and "except" in prev:
                    in_fallback = True
                    break
            if not in_fallback:
                offenders.append(f"{rel}:{i + 1}")
    assert not offenders, (
        "config.NSE_LOT_SIZES read outside a fallback (correct for only 3 of 70 "
        "live symbols): " + ", ".join(offenders))
