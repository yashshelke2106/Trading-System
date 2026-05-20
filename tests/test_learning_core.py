"""
Regression tests for the learning / cost / calibration core.

These guard the trustworthiness machinery built in phases F–O. Before
this file that logic had ZERO automated coverage — one careless edit
could silently break honest-fill costs, calibration monotonicity,
confidence shrinkage, the Wilson significance gate, engine-scoping or
selective-fire and no test would notice.

All tests are deterministic and offline: file constants are redirected
to tmp paths and any journal/calibrator I/O is monkeypatched, so the
real logs/ state is never touched.

Run:  python -m pytest tests/test_learning_core.py -q
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ════════════════════════════════════════════════════════════════════
# trade_mode (Phase I) — one switch, swing default
# ════════════════════════════════════════════════════════════════════

def test_trade_mode_swing_is_default():
    from core.trade_mode import get_mode
    m = get_mode()
    assert m.name == "swing"
    assert m.hold_horizon_hours == 240.0
    assert m.replay_bar == "1d"
    assert m.bypass_intraday_time_gates is True
    assert m.exec_tf == ("daily",) and m.trend_tf == ("weekly",)


def test_trade_mode_intraday_config_intact():
    from core.trade_mode import _MODES
    i = _MODES["intraday"]
    assert i.hold_horizon_hours == 6.5
    assert i.replay_bar == "5m"
    assert i.bypass_intraday_time_gates is False
    assert i.exec_tf == ("intraday", 5)


# ════════════════════════════════════════════════════════════════════
# Honest-fill cost model (Phase F1 / J)
# ════════════════════════════════════════════════════════════════════

def test_apply_fill_costs_only_subtracts():
    from core.signal_tracker import _apply_fill_costs
    # gross 120 on entry 100; costs can only reduce it
    net = _apply_fill_costs(100.0, 120.0, hours_held=2.0)
    assert net < 120.0
    # never below the 5% theta-worst floor
    assert _apply_fill_costs(100.0, 1.0, 999.0) >= 100.0 * 0.05


def test_apply_fill_costs_real_overrides_flat():
    from core.signal_tracker import _apply_fill_costs
    flat = _apply_fill_costs(100.0, 120.0, 48.0)                      # flat 6%/1.2%h
    real = _apply_fill_costs(100.0, 120.0, 48.0,
                             spread_rt=0.03, theta_per_h=0.0017)      # tight real
    # real (tighter) must keep more of the win than the flat guess
    assert real > flat


def test_real_costs_extraction_and_fallback():
    from core.signal_tracker import _real_costs
    srt, tph = _real_costs({"spread_pct": 0.03, "theta": -4.0,
                            "entry_prem": 100})
    assert srt == 0.03
    assert tph == pytest.approx(abs(-4.0) / 100 / 24.0)
    s2, t2 = _real_costs({})                       # no chain data → fallback
    assert s2 is None and t2 is None


# ════════════════════════════════════════════════════════════════════
# Calibrator (Phase F2) — monotone, cold-start safe, persisted
# ════════════════════════════════════════════════════════════════════

def test_prior_is_monotone_increasing():
    from core.calibrator import _prior
    xs = [0, 30, 55, 80, 120]
    ps = [_prior(x) for x in xs]
    assert ps == sorted(ps)
    assert all(0.0 <= p <= 1.0 for p in ps)


def test_calibrator_cold_start_uses_prior(tmp_path, monkeypatch):
    import core.calibrator as C
    monkeypatch.setattr(C, "CALIB_FILE", str(tmp_path / "calib.json"))
    cal = C.Calibrator()
    assert cal.status()["mode"] == "prior"
    # below MIN_CALIB → stays prior, predictions still monotone & bounded
    info = cal.fit([(50.0, True), (60.0, False)])
    assert info["fitted"] is False
    assert cal.predict(30) <= cal.predict(110)


def test_calibrator_fits_monotone_when_enough_data(tmp_path, monkeypatch):
    import core.calibrator as C
    monkeypatch.setattr(C, "CALIB_FILE", str(tmp_path / "calib.json"))
    cal = C.Calibrator()
    # higher score ⇒ more wins; need ≥ MIN_CALIB samples
    samples = []
    for i in range(C.MIN_CALIB + 20):
        score = float(i)
        win = i >= (C.MIN_CALIB + 20) / 2          # high half wins
        samples.append((score, win))
    info = cal.fit(samples)
    assert info["fitted"] is True
    lo, hi = cal.predict(0), cal.predict(C.MIN_CALIB + 19)
    assert hi >= lo                                # monotone non-decreasing
    assert 0.0 <= lo <= 1.0 and 0.0 <= hi <= 1.0
    # persisted + reloadable
    cal2 = C.Calibrator()
    assert cal2.status()["mode"] == "isotonic"


# ════════════════════════════════════════════════════════════════════
# OI signal (Phase G) — ΔOI 4-quadrant truth table
# ════════════════════════════════════════════════════════════════════

def _oi(prev, cur, direction):
    from core.oi_signal import compute_oi_features
    return compute_oi_features(prev, cur, direction)


def test_oi_cold_start_no_prev_is_neutral():
    cur = {"total_ce_oi": 1, "total_pe_oi": 1, "spot": 100, "pcr": 1.0,
           "max_ce_oi_strike": 105, "max_pe_oi_strike": 95}
    f = _oi(None, cur, "long")
    assert f["cold_start"] is True and f["score_delta"] == 0


def test_oi_long_buildup_confirms_long_opposes_short():
    prev = {"total_ce_oi": 100000, "total_pe_oi": 100000, "spot": 100.0,
            "pcr": 1.0, "max_ce_oi_strike": 105, "max_pe_oi_strike": 95}
    cur = {"total_ce_oi": 100000, "total_pe_oi": 110000, "spot": 102.0,
           "pcr": 1.1, "max_ce_oi_strike": 105, "max_pe_oi_strike": 95}
    assert _oi(prev, cur, "long")["score_delta"] > 0
    assert _oi(prev, cur, "short")["score_delta"] < 0


def test_oi_wall_blocks_long_into_ce_wall():
    cur = {"total_ce_oi": 100000, "total_pe_oi": 100000, "spot": 100.0,
           "pcr": 1.0, "max_ce_oi_strike": 100.2, "max_pe_oi_strike": 80}
    assert _oi(None, cur, "long")["wall_block"] is True


# ════════════════════════════════════════════════════════════════════
# adaptive_learner — labels, shrinkage, significance, scoping
# ════════════════════════════════════════════════════════════════════

def test_clean_won_prefers_spot_outcome_then_pnl():
    from core.adaptive_learner import _clean_won
    assert _clean_won({"spot_outcome": "TARGET_HIT"}) is True
    assert _clean_won({"spot_outcome": "SL_HIT"}) is False
    assert _clean_won({"spot_pnl_pct": 1.2}) is True
    assert _clean_won({"spot_pnl_pct": -0.4}) is False
    assert _clean_won({"outcome": "TARGET_HIT"}) is True   # fallback


def test_grade_weight_clamped_and_defaulted():
    from core.adaptive_learner import _grade_weight, GRADE_W_MIN, GRADE_W_MAX
    assert _grade_weight({}) == 1.0                          # no data → neutral
    big = _grade_weight({"entry_price": 100, "sl_price": 99,
                         "spot_pnl_pct": 50.0})
    assert big == GRADE_W_MAX                                # clamped high
    tiny = _grade_weight({"entry_price": 100, "sl_price": 90,
                          "spot_pnl_pct": 0.01})
    assert tiny == GRADE_W_MIN                               # clamped low


def test_wilson_bounds_known_values():
    from core.adaptive_learner import _wilson_bounds
    assert _wilson_bounds(0, 0) == (0.0, 1.0)                # no data → full
    lo, hi = _wilson_bounds(9, 10)
    assert lo > 0.5                                          # 90% excludes .5
    lo2, hi2 = _wilson_bounds(3, 5)
    assert lo2 < 0.5 < hi2                                   # 60%/n5 straddles


def test_resolved_scoped_filters_to_current_engine(monkeypatch):
    import core.adaptive_learner as A
    import core.signal_journal as J
    rows = [
        {"engine_version": "v3-swing-2026.05.18", "outcome": "TARGET_HIT"},
        {"engine_version": "v2-old", "outcome": "TARGET_HIT"},
        {"outcome": "SL_HIT"},                                # legacy <none>
    ]
    monkeypatch.setattr(J, "get_resolved_signals", lambda days=90: rows)
    monkeypatch.setattr(J, "ENGINE_VERSION", "v3-swing-2026.05.18")
    scoped = A._resolved_scoped(90)
    assert len(scoped) == 1
    assert scoped[0]["engine_version"] == "v3-swing-2026.05.18"


def _decided(n_pairs, win_pat="goodp", lose_pat="badp", direction="long"):
    """n_pairs winners carrying win_pat + n_pairs losers carrying lose_pat
    → direction baseline 50%, win_pat 100% (significant if n big)."""
    rows = []
    for _ in range(n_pairs):
        rows.append({"direction": direction, "outcome": "TARGET_HIT",
                     "_won_clean": True, "_grade_w": 1.0,
                     "patterns": [win_pat]})
        rows.append({"direction": direction, "outcome": "SL_HIT",
                     "_won_clean": False, "_grade_w": 1.0,
                     "patterns": [lose_pat]})
    return rows


def test_pattern_weights_winners_up_losers_down_bounded():
    from core.adaptive_learner import AdaptiveLearner
    L = AdaptiveLearner()
    L._params.setdefault("PATTERN_WEIGHTS", {})
    L._tune_pattern_weights(_decided(40), 0.5)
    w = L._params["PATTERN_WEIGHTS"]
    assert w["long:goodp"] > 1.0          # winner indicator boosted
    assert w["long:badp"] < 1.0           # loser indicator dampened
    assert 0.5 <= w["long:goodp"] <= 2.0  # bounded
    assert 0.5 <= w["long:badp"] <= 2.0


def test_pattern_weights_shrinkage_scales_with_evidence():
    from core.adaptive_learner import AdaptiveLearner
    def move(n):
        L = AdaptiveLearner()
        L._params.setdefault("PATTERN_WEIGHTS", {})["long:goodp"] = 1.0
        L._tune_pattern_weights(_decided(n), 0.5)
        return abs(L._params["PATTERN_WEIGHTS"]["long:goodp"] - 1.0)
    small, big = move(3), move(60)
    assert big > small                    # more evidence → larger step
    assert small >= 0.0


def test_pattern_weights_significance_gate_freezes_thin_noise():
    from core.adaptive_learner import AdaptiveLearner
    L = AdaptiveLearner()
    L._params.setdefault("PATTERN_WEIGHTS", {})
    # 'testpat' only 3W/2L vs 50% baseline → Wilson straddles → no move
    rows = []
    for _ in range(10):
        rows.append({"direction": "long", "outcome": "TARGET_HIT",
                     "_won_clean": True, "_grade_w": 1.0,
                     "patterns": ["fillw"]})
        rows.append({"direction": "long", "outcome": "SL_HIT",
                     "_won_clean": False, "_grade_w": 1.0,
                     "patterns": ["filll"]})
    for _ in range(3):
        rows.append({"direction": "long", "outcome": "TARGET_HIT",
                     "_won_clean": True, "_grade_w": 1.0,
                     "patterns": ["testpat"]})
    for _ in range(2):
        rows.append({"direction": "long", "outcome": "SL_HIT",
                     "_won_clean": False, "_grade_w": 1.0,
                     "patterns": ["testpat"]})
    L._tune_pattern_weights(rows, 0.5)
    assert L._params["PATTERN_WEIGHTS"].get("long:testpat", 1.0) == 1.0


def test_optimize_params_n0_noop_and_coarse_gated_on_thin():
    from core.adaptive_learner import AdaptiveLearner
    L = AdaptiveLearner()
    assert L._optimize_params([], [], {}, {}, force=True) == {}     # n=0 honest
    L._params.setdefault("PATTERN_WEIGHTS", {})
    ch = L._optimize_params(_decided(40), _decided(40), {}, {}, force=True)
    # thin sample (<MIN_TRADES_FOR_UPDATE): only the shrinkage-safe
    # per-pattern rule may fire; coarse structural knobs stay gated.
    assert all(k.startswith("pw:") for k in ch), ch


# ════════════════════════════════════════════════════════════════════
# signal_finalize (Phase F3 / G3) — calibrate + expectancy gate
# ════════════════════════════════════════════════════════════════════

def test_finalize_keeps_positive_expectancy_drops_negative(monkeypatch):
    import core.calibrator as C
    import core.signal_finalize as F

    class _FakeCal:
        def predict(self, score):           # high score → high P(win)
            return 0.55 if score >= 100 else 0.05

    monkeypatch.setattr(C, "get_calibrator", lambda: _FakeCal())
    good = {"symbol": "G", "confluence_score": 110, "entry_price": 100,
            "sl_price": 98, "target_price": 106, "reason": "r"}      # rr=3
    bad = {"symbol": "B", "confluence_score": 40, "entry_price": 100,
           "sl_price": 99, "target_price": 101, "reason": "r"}       # rr=1
    out = F.finalize_and_select([good, bad])
    syms = [s["symbol"] for s in out]
    assert "G" in syms and "B" not in syms
    g = next(s for s in out if s["symbol"] == "G")
    assert "calibrated_prob" in g and "expectancy_r" in g


def test_finalize_empty_is_safe():
    from core.signal_finalize import finalize_and_select
    assert finalize_and_select([]) == []


# ════════════════════════════════════════════════════════════════════
# exit_replay (Phase C / I) — walk-forward outcome on synthetic bars
# ════════════════════════════════════════════════════════════════════

def _bars(rows):
    return pd.DataFrame(rows, columns=["date", "high", "low", "close"])


def test_exit_replay_target_and_sl(monkeypatch):
    import core.exit_replay as R
    sig = {"symbol": "X", "direction": "long", "entry_price": 100,
           "sl_price": 98, "target_price": 104, "ts": "2026-01-01T10:00:00"}
    # bar 1 rallies through target → TARGET_HIT
    monkeypatch.setattr(R, "_fetch_bars", lambda *a, **k: _bars([
        ["t1", 105, 99, 104]]))
    assert R.replay_exit(sig)["outcome"] == "TARGET_HIT"
    # bar 1 breaks the stop → SL_HIT, negative pnl
    monkeypatch.setattr(R, "_fetch_bars", lambda *a, **k: _bars([
        ["t1", 101, 97, 98]]))
    res = R.replay_exit(sig)
    assert res["outcome"] == "SL_HIT" and res["pnl_pct"] < 0


def test_exit_replay_time_exit_and_no_data(monkeypatch):
    import core.exit_replay as R
    sig = {"symbol": "X", "direction": "long", "entry_price": 100,
           "sl_price": 98, "target_price": 104, "ts": "2026-01-01T10:00:00"}
    monkeypatch.setattr(R, "_fetch_bars", lambda *a, **k: _bars([
        ["t1", 101, 99, 100.5]]))                 # neither hit
    assert R.replay_exit(sig)["outcome"] == "TIME_EXIT"
    monkeypatch.setattr(R, "_fetch_bars", lambda *a, **k: None)
    assert R.replay_exit(sig)["outcome"] == "NO_DATA"


# ════════════════════════════════════════════════════════════════════
# iv_rank (Phase F4) — percentile gate, graceful on thin history
# ════════════════════════════════════════════════════════════════════

def test_iv_rank_blocks_rich_iv_graceful_when_thin(monkeypatch):
    import core.iv_rank as IV
    hist = [{"symbol": "RICH", "iv_pct": v} for v in range(10, 10 + 20)]
    hist += [{"symbol": "THIN", "iv_pct": 30}]          # < MIN_HIST samples
    monkeypatch.setattr("core.signal_journal._load_all", lambda: hist)
    r = IV.IVRank()
    assert r.should_block("RICH", 999) is True          # top of its range
    assert r.should_block("RICH", 1) is False           # bottom
    assert r.should_block("THIN", 999) is False         # thin → never block


# ════════════════════════════════════════════════════════════════════
# signal_journal (Phase H1) — engine stamp, dedup, extra persistence
# ════════════════════════════════════════════════════════════════════

def test_journal_stamp_dedup_and_resolve_extra(tmp_path, monkeypatch):
    import core.signal_journal as J
    jf = str(tmp_path / "j.jsonl")
    monkeypatch.setattr(J, "JOURNAL_FILE", jf)
    J._seq_counters.clear()

    sig = {"symbol": "ABC", "direction": "long", "entry_price": 100,
           "sl_price": 98, "target_price": 106}
    sid1 = J.record_signal(sig)
    sid2 = J.record_signal(dict(sig))               # same sym/dir/day
    assert sid1 == sid2                              # deduped, not duplicated

    rows = [json.loads(l) for l in open(jf) if l.strip()]
    assert len(rows) == 1
    assert rows[0]["engine_version"] == J.ENGINE_VERSION

    ok = J.resolve_signal(sid1, "TARGET_HIT", 106.0,
                          extra={"spot_outcome": "TARGET_HIT",
                                 "mfe_pct": 2.1})
    assert ok is True
    row = [json.loads(l) for l in open(jf) if l.strip()][0]
    assert row["outcome"] == "TARGET_HIT"
    assert row["spot_outcome"] == "TARGET_HIT"       # H1 extra persisted
    assert row["mfe_pct"] == 2.1
    # pnl_pct computed: spot path → (106 - 100) / 100 * 100 = 6.0
    assert row["pnl_pct"] == 6.0


def test_resolve_pnl_pct_option_path(tmp_path, monkeypatch):
    """resolve_signal computes pnl_pct from premium move when entry_prem present."""
    import core.signal_journal as J
    jf = str(tmp_path / "j.jsonl")
    monkeypatch.setattr(J, "JOURNAL_FILE", jf)
    J._seq_counters.clear()

    sig = {"symbol": "XYZ", "direction": "long", "entry_price": 500,
           "sl_price": 490, "target_price": 530, "entry_prem": 20.0}
    sid = J.record_signal(sig)
    J.resolve_signal(sid, "TARGET_HIT", 530.0, exit_prem=28.0)
    row = [json.loads(l) for l in open(jf) if l.strip()][0]
    # (28 - 20) / 20 * 100 = 40.0%
    assert row["pnl_pct"] == 40.0
    assert row["pnl_rupees"] == 8.0   # (28-20) * lot=1


def test_resolve_pnl_pct_short_spot_path(tmp_path, monkeypatch):
    """resolve_signal computes pnl_pct correctly for shorts (spot path)."""
    import core.signal_journal as J
    jf = str(tmp_path / "j.jsonl")
    monkeypatch.setattr(J, "JOURNAL_FILE", jf)
    J._seq_counters.clear()

    sig = {"symbol": "DEF", "direction": "short", "entry_price": 200,
           "sl_price": 210, "target_price": 180}
    sid = J.record_signal(sig)
    J.resolve_signal(sid, "TARGET_HIT", 180.0)
    row = [json.loads(l) for l in open(jf) if l.strip()][0]
    # short: (200 - 180) / 200 * 100 = 10.0%
    assert row["pnl_pct"] == 10.0


def test_pe_proxy_strike_premium_rises_when_spot_drops():
    """Regression: _estimate_premium_at_spot had inverted PE proxy strike.

    For PE (put) options, when spot drops (short signal's target), the put
    gets deeper ITM → premium RISES. The chain proxy strike should map to
    a deeper-ITM strike (higher strike PE), not a further-OTM one.

    Old bug: proxy = entry_strike + spot_delta → for spot_delta < 0 gave
    a LOWER strike (OTM PE = cheaper = wrong). Fixed to proxy = entry_strike
    - spot_delta for both CE and PE.
    """
    from core.option_translator import _estimate_premium_at_spot

    # Fake chain: strikes 3000–3600 at 100 intervals
    # PE LTPs: lower strike = further OTM = cheaper; higher = deeper ITM = expensive
    chain = [
        {"strike": 3000, "pe_ltp": 5,   "ce_ltp": 250},
        {"strike": 3100, "pe_ltp": 12,  "ce_ltp": 170},
        {"strike": 3200, "pe_ltp": 30,  "ce_ltp": 100},
        {"strike": 3300, "pe_ltp": 70,  "ce_ltp": 50},   # ← ATM
        {"strike": 3400, "pe_ltp": 140, "ce_ltp": 20},
        {"strike": 3500, "pe_ltp": 220, "ce_ltp": 8},
        {"strike": 3600, "pe_ltp": 310, "ce_ltp": 3},
    ]
    entry_strike = 3300
    entry_spot = 3300

    # Short signal: target spot = 3100 (spot drops 200)
    target_spot = 3100
    tgt_prem = _estimate_premium_at_spot(chain, "PE", entry_strike, entry_spot, target_spot)
    # Spot dropped 200 → PE deeper ITM → premium must RISE above 70 (entry ATM)
    assert tgt_prem > 70, f"PE target prem should rise when spot drops, got {tgt_prem}"

    # Short signal: SL spot = 3400 (spot rises 100)
    sl_spot = 3400
    sl_prem = _estimate_premium_at_spot(chain, "PE", entry_strike, entry_spot, sl_spot)
    # Spot rose 100 → PE further OTM → premium must DROP below 70
    assert sl_prem < 70, f"PE SL prem should drop when spot rises, got {sl_prem}"

    # CE sanity: long target at spot 3500
    tgt_prem_ce = _estimate_premium_at_spot(chain, "CE", entry_strike, entry_spot, 3500)
    assert tgt_prem_ce > 50, f"CE target prem should rise when spot rises, got {tgt_prem_ce}"
