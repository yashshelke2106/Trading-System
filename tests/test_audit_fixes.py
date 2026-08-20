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


# ── 3. strategy gates must default to fail-closed ────────────────────────────

def test_strategy_gates_default_to_fail_closed(monkeypatch):
    """This flag was ADDED after a sector gate was caught failing open, then
    defaulted to "0" and was set by no launcher - so the fix for a fail-open
    gate was a flag that itself defaulted to fail-open."""
    import importlib
    monkeypatch.delenv("GATES_FAIL_CLOSED", raising=False)
    import core.strategy_india_swing as isw
    importlib.reload(isw)
    assert isw.GATES_FAIL_CLOSED is True, (
        "an advanced gate whose module errors must skip the candidate, "
        "not wave it through")


def test_fail_open_remains_available_for_measurement_passes(monkeypatch):
    """Over-filtering masks the signal being measured, so the escape hatch
    stays - it just has to be asked for."""
    import importlib
    monkeypatch.setenv("GATES_FAIL_CLOSED", "0")
    import core.strategy_india_swing as isw
    importlib.reload(isw)
    assert isw.GATES_FAIL_CLOSED is False
    monkeypatch.delenv("GATES_FAIL_CLOSED", raising=False)
    importlib.reload(isw)


def test_the_quarantined_ml_model_is_not_loadable():
    """G10's model was quarantined by RENAMING the artifact; the code path will
    happily pick up a fresh ml_filter_model.pkl if one ever appears, and no
    launcher sets DISABLE_G10. Measured: the model picks the WORST trades
    (-3.04%/trade vs -1.20% baseline, accuracy 0.474 vs 0.868)."""
    live = os.path.join(ROOT, "logs", "ml_filter_model.pkl")
    assert not os.path.exists(live), (
        "a live ml_filter_model.pkl re-arms a gate measured to be actively "
        "harmful - keep it renamed, or set DISABLE_G10=1 in the launcher")


# ── 4. the ML gate cannot be re-armed by retraining ──────────────────────────

def test_ml_filter_refuses_to_load_by_default(monkeypatch, tmp_path):
    """The quarantine used to rest on a FILENAME: retraining writes a fresh
    logs/ml_filter_model.pkl and silently re-arms a gate measured to pick the
    worst trades. The load path now refuses unless explicitly enabled."""
    import importlib
    monkeypatch.delenv("ENABLE_ML_FILTER", raising=False)
    import core.ml_filter as mf
    importlib.reload(mf)
    assert mf.ML_FILTER_ENABLED is False
    # even with a model file sitting right there
    monkeypatch.setattr(mf, "MODEL_PATH", tmp_path / "ml_filter_model.pkl")
    (tmp_path / "ml_filter_model.pkl").write_bytes(b"not-a-real-pickle")
    assert mf._load_model() is None, "a present model must not re-arm the gate"


def test_ml_filter_stays_pass_through_when_disabled(monkeypatch):
    """Inert must mean inert: the gate passes candidates, it does not kill them."""
    import importlib
    monkeypatch.delenv("ENABLE_ML_FILTER", raising=False)
    import core.ml_filter as mf
    importlib.reload(mf)
    ok, info = mf.check_ml_filter({"rsi": 50, "score": 70, "grade": "A",
                                   "direction": "long", "patterns": []})
    assert ok is True
    assert info.get("ml_prob") is None


# ── 5. one lot must not silently breach the risk cap ─────────────────────────

def test_a_single_lot_over_budget_is_refused(monkeypatch):
    """max(1, lots) took the trade anyway whenever one lot exceeded the
    per-trade budget - measured at 6.7x (KOTAKBANK, lot 2000, Rs 20 stop:
    Rs 40,000 of risk against a Rs 6,000 budget)."""
    monkeypatch.delenv("ALLOW_MIN_LOT_BREACH", raising=False)
    from core.execution import ExecutionEngine
    e = ExecutionEngine()
    qty = e._calculate_futures_quantity(500_000, 390.0, 370.0, "KOTAKBANK")
    assert qty == 0, "a risk limit that yields whenever it binds is not a limit"
    r = e.last_size_refusal
    assert r and r["breach_multiple"] > 1
    assert "refused" in r["note"]


def test_an_affordable_position_still_sizes():
    from core.execution import ExecutionEngine
    e = ExecutionEngine()
    assert e._calculate_futures_quantity(5_000_000, 390.0, 388.0, "KOTAKBANK") > 0


def test_the_breach_override_still_exists(monkeypatch):
    monkeypatch.setenv("ALLOW_MIN_LOT_BREACH", "1")
    from core.execution import ExecutionEngine
    e = ExecutionEngine()
    assert e._calculate_futures_quantity(500_000, 390.0, 370.0, "KOTAKBANK") > 0


# ── 6. the trade frame must come from the journal ────────────────────────────

def test_trades_frame_carries_sl_and_target():
    """trades.csv is pinned to a legacy header, so stop_loss/target are blank in
    every row and the option columns never arrive. Anything reading it reads a
    strictly worse copy of the journal."""
    from core.dashboard_data import load_trades_frame
    df = load_trades_frame()
    if df.empty:
        pytest.skip("no resolved trades in this environment")
    assert "stop_loss" in df.columns and "target" in df.columns
    assert df["stop_loss"].notna().any(), "stop_loss blank => still reading the CSV"
    assert (df["quantity"] > 1).any(), "quantity should be lot-scaled, not 1"


# ── 7. mutating endpoints are guarded off-loopback ───────────────────────────

def _client():
    from fastapi.testclient import TestClient
    import api_server as A
    return TestClient(A.app)


def test_loopback_writes_are_unchanged(monkeypatch):
    """The fix must not break the normal desktop flow."""
    monkeypatch.delenv("API_HOST", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)
    assert _client().post("/api/learning/reset", json={}).status_code == 200


def test_a_widened_bind_without_a_token_is_refused(monkeypatch):
    """API_HOST is a one-variable mistake away from exposing credential writes
    to the network."""
    monkeypatch.setenv("API_HOST", "0.0.0.0")
    monkeypatch.delenv("API_TOKEN", raising=False)
    r = _client().post("/api/config/token", json={"token": "eyJfake"})
    assert r.status_code == 403


def test_a_configured_token_is_enforced(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "sekret")
    monkeypatch.delenv("API_HOST", raising=False)
    c = _client()
    assert c.post("/api/config/token", json={"token": "eyJfake"}).status_code == 401
    assert c.post("/api/config/token", json={"token": "eyJfake"},
                  headers={"X-API-Token": "sekret"}).status_code == 200


# ── 8. registry hygiene ──────────────────────────────────────────────────────

def test_scratch_registrations_are_hidden_but_still_counted():
    """Hiding a row from a table must not lower the multiple-testing bar."""
    from core.hypothesis_registry import status, trial_count
    shown = status()
    assert not any("test thesis" in str(h["thesis"]).lower() for h in shown)
    assert len(status(include_scratch=True)) >= len(shown)
    assert trial_count() >= len(status(include_scratch=True))


# ── 9. the backfill must not look past the exit ──────────────────────────────

def test_backfill_does_not_see_prices_after_the_exit():
    """The replay ran to option_expiry or MAX_HOLD, so a position that really
    closed in 20 hours got its spot label from up to ten days of subsequent
    action it was never exposed to."""
    import pandas as pd
    from backfill_spot_outcomes import replay

    idx = pd.to_datetime(["2026-08-04", "2026-08-05", "2026-08-06", "2026-08-12"])
    bars = pd.DataFrame(
        {"open": [100., 101., 102., 150.], "high": [101., 104., 103., 160.],
         "low": [99., 97., 101.5, 148.], "close": [100.5, 102., 102.5, 155.]},
        index=idx)
    row = {"entry_price": 100., "sl_price": 90., "target_price": 130.,
           "direction": "long", "ts": "2026-08-03T16:30:00",
           "exit_ts": "2026-08-06T15:30:00"}

    label, _, _, _, _, mfe, _ = replay(row, bars)
    assert label == "TIME_EXIT", "the 08-12 spike was after the exit"
    assert mfe < 10, f"mfe {mfe} includes bars past the exit"
