"""The funded paper account — does the money actually move, and correctly?

Every prior P&L view in this repo was a per-trade statistic wearing a rupee
sign: nothing was ever debited, so an unaffordable trade counted the same as an
affordable one. These tests pin the properties that make this book a book —
cash conservation, risk-based sizing, pessimistic fills, and the expiry
roll-guard that once manufactured phantom +200% wins.
"""

from datetime import date, datetime, timedelta

import pytest

from core import trading_account as ta


# ── harness ─────────────────────────────────────────────────────────────────

@pytest.fixture
def book(tmp_path, monkeypatch):
    """A fresh account in a temp directory, never touching logs/."""
    monkeypatch.setattr(ta, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(ta, "LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setattr(ta, "SIGNALS_PATH", str(tmp_path / "signals.json"))
    monkeypatch.setattr(ta, "_lot_size_for", lambda s: 100)
    ta.reset(1_000_000.0, ta.RiskPolicy(), force=True)
    return ta.load_state()


def sig(symbol="ACME", direction="long", entry=100.0, sl=95.0, target=115.0,
        grade="A", score=90.0, **kw):
    s = {"symbol": symbol, "direction": direction, "entry_price": entry,
         "sl_price": sl, "target_price": target, "confluence_grade": grade,
         "confluence_score": score, "ts": datetime.now().isoformat()}
    s.update(kw)
    return s


def opt(symbol="ACME", expiry_days=7, prem=20.0, sl_prem=10.0, target_prem=35.0,
        **kw):
    exp = (date.today() + timedelta(days=expiry_days)).isoformat()
    return sig(symbol=symbol, option_strike=100.0, option_type="CE",
               option_expiry=exp, entry_prem=prem, sl_prem=sl_prem,
               target_prem=target_prem, delta=0.5, **kw)


def bars(start_day, rows):
    """rows: list of (high, low, close) starting the day AFTER start_day."""
    out = []
    for i, (h, l, c) in enumerate(rows, start=1):
        out.append({"date": start_day + timedelta(days=i), "open": c,
                    "high": h, "low": l, "close": c})
    return out


# ── the account starts as a real pot ────────────────────────────────────────

def test_reset_opens_a_funded_book(book):
    assert book["capital"] == 1_000_000.0
    assert book["cash"] == 1_000_000.0
    assert book["realised_pnl"] == 0.0
    assert book["open"] == [] and book["closed"] == []
    assert ta.equity_of(book) == 1_000_000.0


def test_reset_archives_rather_than_deletes(tmp_path, monkeypatch):
    """The legacy evidence base is never destroyed by a reset."""
    monkeypatch.setattr(ta, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(ta, "LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    ta.reset(500_000.0, force=True)
    ta.select_and_open(ta.load_state(), signals=[sig()])
    r = ta.reset(1_000_000.0, force=True)
    assert r["ok"] and ta.load_state()["capital"] == 1_000_000.0
    # the prior state file was copied out before being replaced
    assert r["archived"], "a reset must archive the book it replaces"


# ── sizing is driven by risk, not by a rupee constant ───────────────────────

def test_size_scales_inversely_with_stop_distance(book):
    """The core property: a wider stop buys fewer shares, so both positions
    risk the same money."""
    tight = ta.size_equity(100.0, 99.0, 7_500.0, 1_000_000.0, 1_000_000.0,
                           ta.RiskPolicy())
    wide = ta.size_equity(100.0, 90.0, 7_500.0, 1_000_000.0, 1_000_000.0,
                          ta.RiskPolicy())
    assert tight.qty > wide.qty
    assert wide.risk_rupees == pytest.approx(7_500.0, abs=100.0)


def test_position_cap_clips_a_tight_stop(book):
    """A 1-rupee stop wants 7,500 shares = Rs 7.5 lakh. The 15% cap must bite."""
    p = ta.RiskPolicy(max_position_pct=0.15)
    s = ta.size_equity(100.0, 99.0, 7_500.0, 1_000_000.0, 1_000_000.0, p)
    assert s.cost_basis <= 150_000.0 + 1e-6
    assert s.risk_rupees < 7_500.0        # clipped size risks less, never more


def test_sizing_refuses_when_one_share_exceeds_the_budget(book):
    s = ta.size_equity(100.0, 50.0, 10.0, 1_000_000.0, 1_000_000.0, ta.RiskPolicy())
    assert not s.ok and "risk budget" in s.reason


def test_option_risk_can_never_exceed_the_premium_paid(book):
    """A long option's max loss is what it cost, whatever the stop says."""
    s = ta.size_option("ACME", prem=20.0, sl_prem=0.0, risk_budget=7_500.0,
                       equity=1_000_000.0, cash=1_000_000.0,
                       options_room=300_000.0, policy=ta.RiskPolicy())
    assert s.ok
    assert s.risk_rupees <= s.cost_basis


# ── cash is a real constraint ───────────────────────────────────────────────

def test_unaffordable_signal_is_skipped_and_counted(book):
    """A pot of Rs 500 cannot buy a Rs 5,000 share. The old view paid it out
    anyway; this one records the skip."""
    ta.reset(500.0, force=True)
    r = ta.select_and_open(ta.load_state(),
                           signals=[sig(entry=5000.0, sl=4900.0, target=5400.0)])
    assert r["n_opened"] == 0
    assert ta.load_state()["counters"]["skipped_no_cash"] == 1


def test_book_is_never_fully_invested(book):
    """The gross deployment cap keeps dry powder for tomorrow."""
    signals = [sig(symbol=f"SYM{i}", entry=100.0, sl=99.0, target=115.0)
               for i in range(12)]
    ta.select_and_open(book, signals=signals)
    st = ta.load_state()
    pol = ta.RiskPolicy.from_dict(st["policy"])
    assert ta.committed(st) <= ta.equity_of(st) * pol.max_gross_deployed_pct + 1.0
    assert st["cash"] > 0


def test_portfolio_risk_cap_stops_adding_risk(book):
    signals = [sig(symbol=f"SYM{i}", entry=100.0, sl=90.0, target=130.0)
               for i in range(20)]
    ta.select_and_open(book, signals=signals)
    st = ta.load_state()
    pol = ta.RiskPolicy.from_dict(st["policy"])
    assert ta.open_risk(st) <= ta.equity_of(st) * pol.max_portfolio_risk_pct + 1.0


def test_one_position_per_symbol(book):
    ta.select_and_open(book, signals=[sig(symbol="ACME")])
    ta.select_and_open(ta.load_state(), signals=[sig(symbol="ACME")])
    assert len(ta.load_state()["open"]) == 1


# ── money actually moves ────────────────────────────────────────────────────

def _open_one(book, s, monkeypatch=None):
    ta.select_and_open(book, signals=[s])
    st = ta.load_state()
    assert st["open"], "expected the signal to be funded"
    return st, st["open"][0]


def test_target_hit_pays_the_account(book, monkeypatch):
    st, p = _open_one(book, sig(entry=100.0, sl=95.0, target=115.0))
    entry_day = ta._parse_date(p["opened_date"])
    monkeypatch.setattr(ta, "daily_bars",
                        lambda s, days_back=120: bars(entry_day,
                                                      [(116.0, 101.0, 115.5)]))
    ta.mark_and_exit(st)
    out = ta.load_state()
    closed = out["closed"][0]
    assert closed["exit_reason"] == "TARGET_HIT"
    assert closed["pnl"] > 0
    assert out["cash"] > 1_000_000.0          # the pot grew
    assert out["realised_pnl"] == pytest.approx(closed["pnl"], abs=0.01)


def test_stop_hit_costs_the_account(book, monkeypatch):
    st, p = _open_one(book, sig(entry=100.0, sl=95.0, target=115.0))
    entry_day = ta._parse_date(p["opened_date"])
    monkeypatch.setattr(ta, "daily_bars",
                        lambda s, days_back=120: bars(entry_day,
                                                      [(101.0, 94.0, 94.5)]))
    ta.mark_and_exit(st)
    out = ta.load_state()
    closed = out["closed"][0]
    assert closed["exit_reason"] == "SL_HIT"
    assert closed["pnl"] < 0
    assert out["cash"] < 1_000_000.0          # the pot shrank
    # loss is close to the planned risk, plus costs — never wildly beyond it
    assert abs(closed["pnl"]) <= p["risk_rupees"] + p["costs"] * 2 + 1.0


def test_a_bar_spanning_both_levels_is_recorded_as_the_stop(book, monkeypatch):
    """Intrabar order is unknowable from daily data. Assuming the good fill is
    how a backtest manufactures an edge it does not have."""
    st, p = _open_one(book, sig(entry=100.0, sl=95.0, target=115.0))
    entry_day = ta._parse_date(p["opened_date"])
    monkeypatch.setattr(ta, "daily_bars",
                        lambda s, days_back=120: bars(entry_day,
                                                      [(116.0, 94.0, 100.0)]))
    ta.mark_and_exit(st)
    assert ta.load_state()["closed"][0]["exit_reason"] == "SL_HIT"


def test_entry_day_bar_cannot_resolve_the_trade(book, monkeypatch):
    """No lookahead: the bar the signal was born on must not close it."""
    st, p = _open_one(book, sig(entry=100.0, sl=95.0, target=115.0))
    entry_day = ta._parse_date(p["opened_date"])
    same_day = [{"date": entry_day, "open": 100.0, "high": 130.0,
                 "low": 90.0, "close": 120.0}]
    monkeypatch.setattr(ta, "daily_bars", lambda s, days_back=120: same_day)
    ta.mark_and_exit(st)
    assert ta.load_state()["closed"] == []


def test_short_profits_when_price_falls(book, monkeypatch):
    st, p = _open_one(book, sig(direction="short", entry=100.0, sl=105.0,
                                target=85.0))
    assert p["instrument"] == "futures"        # you cannot short cash equity
    entry_day = ta._parse_date(p["opened_date"])
    monkeypatch.setattr(ta, "daily_bars",
                        lambda s, days_back=120: bars(entry_day,
                                                      [(99.0, 84.0, 85.0)]))
    ta.mark_and_exit(st)
    closed = ta.load_state()["closed"][0]
    assert closed["exit_reason"] == "TARGET_HIT"
    assert closed["pnl"] > 0


def test_futures_margin_returns_to_cash_on_exit(book, monkeypatch):
    """Margin is blocked, not spent. It must come back plus the P&L."""
    st, p = _open_one(book, sig(direction="short", entry=100.0, sl=105.0,
                                target=85.0))
    blocked = p["cost_basis"]
    assert ta.load_state()["cash"] == pytest.approx(1_000_000.0 - blocked, abs=0.01)
    entry_day = ta._parse_date(p["opened_date"])
    monkeypatch.setattr(ta, "daily_bars",
                        lambda s, days_back=120: bars(entry_day,
                                                      [(99.0, 84.0, 85.0)]))
    ta.mark_and_exit(st)
    out = ta.load_state()
    assert out["cash"] == pytest.approx(1_000_000.0 + out["realised_pnl"], abs=0.01)


# ── the ledger cannot leak ──────────────────────────────────────────────────

def test_cash_plus_committed_always_equals_capital_plus_realised(book, monkeypatch):
    signals = [sig(symbol=f"SYM{i}", entry=100.0 + i, sl=95.0 + i,
                   target=115.0 + i) for i in range(6)]
    ta.select_and_open(book, signals=signals)
    st = ta.load_state()
    ta.check_invariant(st)
    entry_day = ta._parse_date(st["open"][0]["opened_date"])
    monkeypatch.setattr(ta, "daily_bars",
                        lambda s, days_back=120: bars(entry_day,
                                                      [(140.0, 130.0, 135.0)]))
    ta.mark_and_exit(st)
    out = ta.load_state()
    ta.check_invariant(out)
    assert out["cash"] + ta.committed(out) == pytest.approx(
        out["capital"] + out["realised_pnl"], abs=1.0)


def test_a_broken_ledger_raises_rather_than_reporting_a_number(book):
    book["cash"] = 5_000_000.0                # money from nowhere
    with pytest.raises(AssertionError):
        ta.check_invariant(book)


# ── options: the traps this repo already paid for ───────────────────────────

def test_expiry_day_option_is_refused(book):
    """Entering on expiry day is the gamma trap that leaked INDUSTOWER PE."""
    ok, why = ta._option_leg_usable(opt(expiry_days=0), today=date.today())
    assert not ok and "expiry" in why


def test_expired_option_settles_at_intrinsic_not_the_next_chain(book, monkeypatch):
    """Marking an expired contract against the next expiry manufactured phantom
    +200% 'EXPIRED wins' in the journal (audited 2026-07-04)."""
    p = ta.RiskPolicy(prefer="cheapest", max_position_pct=0.05)
    book["policy"] = p.to_dict()
    ta.save_state(book)
    st, pos = _open_one(book, opt(expiry_days=3, prem=20.0, sl_prem=10.0,
                                  target_prem=35.0))
    assert pos["instrument"] == "option"
    entry_day = ta._parse_date(pos["opened_date"])
    # price drifts within the levels, then the contract expires 2 points ITM
    rows = [(101.0, 99.0, 100.0), (102.0, 99.0, 101.0), (103.0, 100.0, 102.0),
            (104.0, 101.0, 103.0), (105.0, 102.0, 104.0)]
    monkeypatch.setattr(ta, "daily_bars",
                        lambda s, days_back=120: bars(entry_day, rows))
    ta.mark_and_exit(st)
    closed = ta.load_state()["closed"][0]
    assert closed["exit_reason"] == "EXPIRED"
    # intrinsic of a 100 CE with spot 102 is 2.00 — not a next-expiry premium
    assert closed["exit"] == pytest.approx(2.0, abs=0.01)
    assert closed["pnl"] < 0                  # paid 20, settled at 2


def test_options_sleeve_is_capped(book, monkeypatch):
    """A sleeve measured at PF 0.44 must never be able to sink the whole book."""
    book["policy"] = ta.RiskPolicy(prefer="cheapest").to_dict()
    ta.save_state(book)
    signals = [opt(symbol=f"SYM{i}", expiry_days=20) for i in range(12)]
    ta.select_and_open(book, signals=signals)
    st = ta.load_state()
    pol = ta.RiskPolicy.from_dict(st["policy"])
    assert ta.options_deployed(st) <= ta.equity_of(st) * pol.options_sleeve_max_pct + 1.0


def test_delta_one_is_preferred_over_paying_theta(book):
    """Default routing takes the cash-equity leg when it fits; options are the
    leverage fallback, because option buying measured PF 0.44 here."""
    s = ta.choose_sizing(opt(expiry_days=20), equity=1_000_000.0,
                         cash=1_000_000.0, options_room=300_000.0,
                         policy=ta.RiskPolicy(prefer="delta_one"))
    assert s.ok and s.instrument == "equity"


def test_options_are_used_when_the_cash_leg_becomes_a_token(book):
    """Cash equity is infinitely divisible, so it can always be scaled down to
    whatever cash is left. That makes "does it fit" the wrong test: 50 shares
    carrying 3% of the intended risk is a token, not the trade. The option
    delivers 67% of the risk budget for Rs 2,000, so it wins — which is exactly
    the case where paying theta is justified."""
    s = ta.choose_sizing(opt(expiry_days=20, entry=100.0, sl=99.0),
                         equity=200_000.0, cash=5_000.0, options_room=20_000.0,
                         policy=ta.RiskPolicy(prefer="delta_one"))
    assert s.ok and s.instrument == "option"
    assert s.risk_fill > 0.5


def test_a_token_sized_cash_leg_never_wins_on_fundability_alone(book):
    """The regression this guards: routing on "is it fundable" sent every long
    to equity and the options sleeve never traded."""
    equity_leg = ta.size_equity(100.0, 99.0, 1_500.0, 200_000.0, 5_000.0,
                                ta.RiskPolicy())
    assert equity_leg.ok                       # it does fit...
    assert equity_leg.risk_rupees / 1_500.0 < 0.1   # ...but carries almost no risk


# ── filters ─────────────────────────────────────────────────────────────────

def test_poor_reward_to_risk_is_rejected(book):
    ok, why = ta._passes_filters(sig(entry=100.0, sl=95.0, target=101.0),
                                 ta.RiskPolicy())
    assert not ok and "rr" in why


def test_inconsistent_levels_are_rejected(book):
    ok, why = ta._passes_filters(sig(direction="long", entry=100.0, sl=105.0,
                                     target=115.0), ta.RiskPolicy())
    assert not ok and "inconsistent" in why


def test_stale_signal_is_rejected(book):
    old = (datetime.now() - timedelta(hours=100)).isoformat()
    ok, why = ta._passes_filters(sig(ts=old), ta.RiskPolicy())
    assert not ok and "stale" in why


def test_daily_loss_stop_halts_new_entries(book):
    book["day"] = {"date": str(date.today()), "start_equity": 1_000_000.0,
                   "halted": False, "halt_reason": ""}
    book["cash"] = 950_000.0
    book["capital"] = 1_000_000.0
    book["realised_pnl"] = -50_000.0          # -5% on the day
    ta.save_state(book)
    ta._roll_day(book, date.today())
    assert book["day"]["halted"]
    r = ta.select_and_open(book, signals=[sig()])
    assert r.get("halted") and r["opened"] == []


# ── reporting stays honest ──────────────────────────────────────────────────

def test_summary_reports_confidence_alongside_pnl(book):
    s = ta.summary(book)
    assert s["confidence"]["verdict"].startswith("MECHANICS ONLY")
    assert "not a validated edge" in s["confidence"]["note"]


def test_summary_return_pct_tracks_equity(book, monkeypatch):
    st, p = _open_one(book, sig(entry=100.0, sl=95.0, target=115.0))
    entry_day = ta._parse_date(p["opened_date"])
    monkeypatch.setattr(ta, "daily_bars",
                        lambda s, days_back=120: bars(entry_day,
                                                      [(116.0, 101.0, 115.5)]))
    ta.mark_and_exit(st)
    s = ta.summary(ta.load_state())
    assert s["return_pct"] > 0
    assert s["equity"] == pytest.approx(s["cash"] + s["committed"], abs=1.0)
