"""The P&L / Verdict / Accuracy tabs must answer the same question the same way.

Three defects made one book look like three books:

1. The P&L tab read logs/trades.csv and gated every row on `sl_price` /
   `target_price`. The CSV spells those `stop_loss` / `target` and leaves them
   blank, so all 118 settled trades were disqualified and the tab reported
   Rs 0 next to a full table of trades.

2. Rupee P&L was scaled by a lot lookup that fell back to ONE SHARE for any
   symbol missing from the stale static map. A 2,250-share NTPC loss and a
   1-share SHREECEM loss landed in the same column, understating the book
   roughly 5x.

3. The Accuracy tab renders a fixed tile list and hides tiles whose key is
   absent. get_signal_pnl_summary() emitted `win_rate` / `wins` / `losses`
   while the tab asked for `win_rate_pct` / `target_hit` / `sl_hit` /
   `closed_signals` / `avg_pnl_rupees`, so five of seven tiles vanished.
"""

import json

import pytest

import api_server as A


def _row(**kw):
    base = {
        "signal_id": "X_1", "symbol": "NTPC", "direction": "long",
        "entry_price": 100.0, "exit_price": 101.0,
        "sl_price": 98.0, "target_price": 104.0,
        "outcome": "TARGET_HIT", "ts": "2026-08-19T10:00:00",
        "exit_ts": "2026-08-19T14:00:00",
        "entry_prem": 5.0, "exit_prem": 7.0,
        "option_type": "CE", "option_strike": 100.0,
        "spot_pnl_pct": 1.0, "grade": "A",
    }
    base.update(kw)
    return base


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """Point the API at a throwaway journal."""
    def _write(rows):
        f = tmp_path / "signal_journal.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        monkeypatch.setattr(A, "JOURNAL_FILE", f)
        return f
    return _write


# ── 1. a settled trade counts without SL/target ──────────────────────────────

def test_settled_trade_counts_without_sl_or_target(journal, monkeypatch):
    """SL and target are ENTRY-PLAN fields. Demanding them to report a
    REALISED P&L is what zeroed the tab."""
    monkeypatch.setattr(A, "_lot_for", lambda s: 100)
    journal([_row(sl_price=None, target_price=None)])

    trades = A._journal_trades(0)
    stats = A._compute_stats(trades)

    assert stats["total"] == 1
    assert stats["qualified"] == 1, "settled trade must not be skipped"
    assert stats["total_pnl"] == pytest.approx(200.0)   # (7-5) * 100
    assert stats["skipped_incomplete"] == 0


def test_open_signals_are_not_reported_as_trades(journal, monkeypatch):
    monkeypatch.setattr(A, "_lot_for", lambda s: 100)
    journal([_row(outcome=None, exit_price=None, exit_prem=None), _row()])
    assert len(A._journal_trades(0)) == 1


# ── 2. lot scaling ───────────────────────────────────────────────────────────

def test_rupee_pnl_uses_the_resolved_lot(journal, monkeypatch):
    monkeypatch.setattr(A, "_lot_for", lambda s: 2250)
    journal([_row(entry_prem=5.15, exit_prem=0.86)])

    t = A._journal_trades(0)[0]
    assert t["quantity"] == 2250
    assert t["lot_resolved"] is True
    assert t["pnl"] == pytest.approx((0.86 - 5.15) * 2250, rel=1e-6)


def test_unresolved_lot_is_excluded_not_counted_as_one_share(journal, monkeypatch):
    """The whole point: an unknown contract size must not silently become 1."""
    monkeypatch.setattr(A, "_lot_for", lambda s: 1)
    journal([_row(symbol="MCDOWELL-N", entry_prem=2210.25, exit_prem=110.51)])

    trades = A._journal_trades(0)
    stats = A._compute_stats(trades)

    assert trades[0]["lot_resolved"] is False
    assert trades[0]["pnl"] is None
    assert stats["unresolved_lot"] == 1
    assert stats["total_pnl"] == 0.0, "unscaled row must not pollute the total"
    assert stats["total"] == 1, "but it is still reported, not hidden"


def test_stored_pnl_rupees_is_not_trusted_over_the_live_lot(journal, monkeypatch):
    """History was written with the broken lookup; recompute from premiums."""
    monkeypatch.setattr(A, "_lot_for", lambda s: 375)
    journal([_row(entry_prem=31.1, exit_prem=17.65, pnl_rupees=-13.45)])

    t = A._journal_trades(0)[0]
    assert t["pnl"] == pytest.approx((17.65 - 31.1) * 375, rel=1e-6)


# ── 3. the two bases never blend ─────────────────────────────────────────────

def test_skill_basis_is_spot_and_matches_the_verdict_gate(journal, monkeypatch):
    """/api/trades must report the SAME honest numbers the Verdict tab shows,
    computed by the same module — otherwise the tabs contradict each other."""
    from core.honest_performance import honest_performance

    monkeypatch.setattr(A, "_lot_for", lambda s: 100)
    rows = [_row(signal_id=f"S{i}", spot_pnl_pct=v, entry_prem=5.0, exit_prem=1.0)
            for i, v in enumerate([1.0, -0.5, 2.0, -1.5] * 10)]
    journal(rows)

    stats = A._compute_stats(A._journal_trades(0))
    expected = honest_performance(
        [{"spot_pnl_pct": r["spot_pnl_pct"], "entry_price": r["entry_price"],
          "exit_price": r["exit_price"], "direction": "long"} for r in rows]
    ).as_dict()

    assert stats["honest"]["profit_factor"] == expected["profit_factor"]
    assert stats["honest"]["win_rate"] == expected["win_rate"]
    # premium cash is deeply negative here while spot skill is positive:
    # proof the two bases are kept apart rather than averaged into mush.
    assert stats["total_pnl"] < 0
    assert stats["honest"]["profit_factor"] > 1


def test_stats_declare_their_basis(journal, monkeypatch):
    monkeypatch.setattr(A, "_lot_for", lambda s: 100)
    journal([_row()])
    assert "basis" in A._compute_stats(A._journal_trades(0))


# ── 4. the Accuracy tab's tile keys ──────────────────────────────────────────

ACCURACY_TILE_KEYS = [
    "total_signals", "closed_signals", "target_hit", "sl_hit",
    "win_rate_pct", "total_pnl_rupees", "avg_pnl_rupees",
]


def test_pnl_summary_emits_every_key_the_accuracy_tab_renders(monkeypatch, tmp_path):
    """A tile whose key is missing is hidden with no error — the page looks
    fine and is simply, silently, mostly empty."""
    import core.dashboard_data as dd

    f = tmp_path / "j.jsonl"
    f.write_text("\n".join(json.dumps(_row(signal_id=f"S{i}")) for i in range(5)),
                 encoding="utf-8")
    monkeypatch.setattr(dd, "JOURNAL_FILE", str(f))
    dd.load_signal_journal_frame.cache_clear() if hasattr(
        dd.load_signal_journal_frame, "cache_clear") else None

    out = dd.get_signal_pnl_summary()
    missing = [k for k in ACCURACY_TILE_KEYS if k not in out]
    assert not missing, f"Accuracy tab would hide these tiles: {missing}"


def test_pnl_summary_empty_case_still_has_the_tile_keys(monkeypatch, tmp_path):
    import core.dashboard_data as dd
    f = tmp_path / "empty.jsonl"
    f.write_text("", encoding="utf-8")
    monkeypatch.setattr(dd, "JOURNAL_FILE", str(f))
    out = dd.get_signal_pnl_summary()
    assert not [k for k in ACCURACY_TILE_KEYS if k not in out]


# ── 5. the Dhan banner must track the live probe ─────────────────────────────

def _verdict_payload(monkeypatch, probe):
    monkeypatch.setattr(A, "_dhan_probe_cached", lambda *a, **k: probe)
    A._CACHE.pop("verdict", None)
    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        return c.get("/api/verdict").json()


def test_dhan_banner_clears_when_the_probe_says_working(monkeypatch):
    """This was hardcoded `expired: True, since 2026-07-24`. The subscription
    came back on 2026-08-04, so the Verdict tab showed a red DHAN DATA API
    EXPIRED banner while the header two rows above reported DATA API Active —
    the dashboard contradicting itself on the same screen."""
    out = _verdict_payload(monkeypatch, {"status": "working", "working": True,
                                         "message": "Dhan data API responding"})
    assert out["dhan"]["expired"] is False


def test_dhan_banner_shows_when_the_probe_says_expired(monkeypatch):
    out = _verdict_payload(monkeypatch, {"status": "expired", "working": False,
                                         "message": "401 - subscription expired"})
    assert out["dhan"]["expired"] is True
    assert out["dhan"]["consequence"]


def test_dhan_banner_does_not_cry_expired_on_a_transient_blip(monkeypatch):
    """429 / 5xx / unreachable are NOT an expired subscription."""
    for status in ("rate_limited", "dhan_down", "unreachable"):
        out = _verdict_payload(monkeypatch, {"status": status, "working": False,
                                             "transient": True, "message": status})
        assert out["dhan"]["expired"] is False, f"{status} misread as expired"


# ── 6. pattern win rates use the same label the learner trains on ────────────

def test_pattern_stats_use_the_clean_spot_label(monkeypatch):
    """`_won_clean` was attached only inside maybe_update(), so the Pattern Win
    Rates tab fell through to the OPTION outcome. It showed the premium hit
    rate (~16%) for a learner training on the spot label (~42%), and every
    pattern read as a loser."""
    import core.adaptive_learner as al

    # Option expired worthless (SL_HIT on premium) but the SPOT call was right.
    rows = [{"symbol": "X", "direction": "long", "patterns": ["ema_uptrend"],
             "outcome": "SL_HIT", "spot_outcome": "TARGET_HIT",
             "spot_pnl_pct": 1.2, "entry_price": 100.0, "exit_price": 101.2,
             "pnl_rupees": -500.0, "engine_version": "v"} for _ in range(10)]
    monkeypatch.setattr(al, "_resolved_scoped", lambda days=90: rows)

    ps = al.get_learner().get_pattern_stats()["ema_uptrend"]
    assert ps["win_rate"] == 1.0, "spot-correct calls must count as wins"
    assert ps["avg_spot_pct"] == pytest.approx(1.2)
    assert ps["avg_pnl"] < 0, "premium cash stays visible and stays negative"


def test_pattern_stats_expose_spot_basis_alongside_premium(monkeypatch):
    """avg_pnl is negative for nearly every pattern BY CONSTRUCTION - a long
    option book pays theta whatever the signal does - so the tab needs a
    basis that measures the pattern rather than the instrument."""
    import core.adaptive_learner as al

    rows = [{"symbol": "X", "direction": "long", "patterns": ["p_good"],
             "outcome": "SL_HIT", "spot_pnl_pct": +2.0, "pnl_rupees": -900.0,
             "entry_price": 100.0, "exit_price": 102.0, "engine_version": "v"},
            {"symbol": "Y", "direction": "long", "patterns": ["p_bad"],
             "outcome": "SL_HIT", "spot_pnl_pct": -2.0, "pnl_rupees": -900.0,
             "entry_price": 100.0, "exit_price": 98.0, "engine_version": "v"}]
    monkeypatch.setattr(al, "_resolved_scoped", lambda days=90: rows)

    ps = al.get_learner().get_pattern_stats()
    assert ps["p_good"]["avg_spot_pct"] > 0 > ps["p_bad"]["avg_spot_pct"]
    # identical premium cash -> the premium column cannot tell them apart
    assert ps["p_good"]["avg_pnl"] == ps["p_bad"]["avg_pnl"]


# ── 7. lane prose must follow the lane verdict ───────────────────────────────

@pytest.mark.parametrize("verdict,forbidden", [
    ("REJECTED", "pending"),
    ("REJECTED-WEAK", "pending"),
])
def test_rsi2_lane_prose_follows_its_verdict(monkeypatch, verdict, forbidden):
    """The chip came from the registry; the evidence/action strings were a
    frozen snapshot from when the hunt was open. Once H-009 closed, the row
    rendered a REJECTED chip beside 'final statistician gate pending'."""
    monkeypatch.setattr(A, "_dhan_probe_cached", lambda *a, **k: {"status": "working"})
    monkeypatch.setattr(
        A, "_cached",
        lambda key, ttl, loader: loader(),   # bypass the 5-minute verdict cache
    )

    import core.hypothesis_registry as hr
    monkeypatch.setattr(hr, "status",
                        lambda: [{"id": "H-009",
                                  "thesis": "RSI-2 mean-reversion via futures",
                                  "verdict": verdict}])

    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        lanes = c.get("/api/verdict").json()["lanes"]

    row = next(l for l in lanes if l["lane"].startswith("RSI-2"))
    assert row["verdict"] == verdict
    assert forbidden not in row["action"].lower(), \
        f"{verdict} lane still claims the hunt is open: {row['action']!r}"
    assert "closed" in row["action"].lower()


def test_rsi2_lane_keeps_the_open_wording_while_it_is_open(monkeypatch):
    monkeypatch.setattr(A, "_dhan_probe_cached", lambda *a, **k: {"status": "working"})
    monkeypatch.setattr(A, "_cached", lambda key, ttl, loader: loader())

    import core.hypothesis_registry as hr
    monkeypatch.setattr(hr, "status",
                        lambda: [{"id": "H-009",
                                  "thesis": "RSI-2 mean-reversion via futures",
                                  "verdict": "CONDITIONAL-PASS"}])

    from fastapi.testclient import TestClient
    with TestClient(A.app) as c:
        lanes = c.get("/api/verdict").json()["lanes"]

    row = next(l for l in lanes if l["lane"].startswith("RSI-2"))
    assert "pending" in row["action"].lower()


# ── 8. the learner must be able to reach a feasible target ───────────────────

def test_rr_ratio_floor_is_inside_the_reachable_move_distribution():
    """The floor was 3.5R, chosen for option premium economics. With a 1% stop
    that asks for a 3.5-5.0% move; measured max favourable excursion over the
    real hold was 2.83%, median 0.693%. Every value the learner was allowed to
    pick was unreachable, so it could never converge."""
    from core.adaptive_learner import TUNABLE_PARAMS
    default, lo, hi, step, section = TUNABLE_PARAMS["rr_ratio"]

    MAX_REACHABLE_R = 2.83          # largest MFE seen, as multiples of a 1% stop
    assert lo < MAX_REACHABLE_R, (
        f"rr_ratio floor {lo}R is outside the measured move distribution "
        f"(max excursion {MAX_REACHABLE_R}R) - the learner cannot converge")
    assert lo <= 1.0, "a reachable target needs roughly 1R or less"
    assert hi >= default, "ceiling must still admit the old default"


def test_rr_tuner_can_actually_walk_down_to_the_floor():
    """The tuner reads its bounds from TUNABLE_PARAMS; pin that it is not
    clamped by a second hardcoded floor somewhere."""
    from core.adaptive_learner import TUNABLE_PARAMS, get_learner
    _, lo, _, _, _ = TUNABLE_PARAMS["rr_ratio"]
    L = get_learner()
    assert L._clip("rr_ratio", 0.5) >= lo
    assert L._clip("rr_ratio", 0.0) == lo, "clip should land on the floor, not below"


# ── 9. path stats must be persisted, not recomputed ──────────────────────────

def test_backfill_replay_returns_excursions():
    """exit_replay computed MFE/MAE then discarded them, so answering a bracket
    question needed a full re-replay (live API + a timezone correction). The
    backfill walks the same bars; it now keeps them."""
    import pandas as pd
    from backfill_spot_outcomes import replay

    idx = pd.to_datetime(["2026-08-04", "2026-08-05", "2026-08-06"])
    bars = pd.DataFrame(
        {"open": [100.0, 101.0, 102.0], "high": [101.0, 104.0, 103.0],
         "low": [99.0, 97.0, 101.5], "close": [100.5, 102.0, 102.5]}, index=idx)
    row = {"entry_price": 100.0, "sl_price": 90.0, "target_price": 130.0,
           "direction": "long", "ts": "2026-08-03T16:30:00"}

    res = replay(row, bars)
    assert res is not None
    assert len(res) == 7, "replay must return mfe and mae alongside the outcome"
    label, pnl, exit_px, entry_dt, exit_dt, mfe, mae = res
    # entry is the next session's OPEN = 100.0; high 104 -> +4%, low 97 -> -3%
    assert mfe == pytest.approx(4.0, abs=0.01)
    assert mae == pytest.approx(-3.0, abs=0.01)


def test_backfill_excursions_are_signed_in_trade_direction_for_shorts():
    import pandas as pd
    from backfill_spot_outcomes import replay

    idx = pd.to_datetime(["2026-08-04", "2026-08-05"])
    bars = pd.DataFrame(
        {"open": [100.0, 99.0], "high": [102.0, 100.0],
         "low": [96.0, 98.0], "close": [99.0, 99.5]}, index=idx)
    row = {"entry_price": 100.0, "sl_price": 110.0, "target_price": 70.0,
           "direction": "short", "ts": "2026-08-03T16:30:00"}

    _, _, _, _, _, mfe, mae = replay(row, bars)
    # short from 100: low 96 is FAVOURABLE (+4%), high 102 is ADVERSE (-2%)
    assert mfe == pytest.approx(4.0, abs=0.01)
    assert mae == pytest.approx(-2.0, abs=0.01)
