"""Participant-flow capture must not manufacture a lookahead edge.

NSCCL publishes participant-wise OI for trade date T *after* the close of T.
Joining that row to T's own return is a one-line way to invent an edge that
cannot be traded -- the same shape as the open-fill mirage (H-003) and the
forming-bar corruption already found in the capture layer. The loader therefore
computes `available_from` and studies join on it; these tests hold that
contract, and hold the "a gap stays a gap" rule that stops a failed fetch from
being silently zero-filled.
"""

import json
from datetime import date

import pandas as pd
import pytest

from core import flow_capture as fc

# Real NSCCL rows (2026-08-17). Header keeps NSE's trailing whitespace, which
# is the thing that silently mis-maps columns if it is not stripped on read.
SAMPLE = (
    '""Participant wise Open Interest as on Aug 17, 2026"",,,,,,,,,,,,,,\n'
    "Client Type,Future Index Long,Future Index Short,Future Stock Long,"
    "Future Stock Short       ,Option Index Call Long,Option Index Put Long,"
    "Option Index Call Short,Option Index Put Short,Option Stock Call Long,"
    "Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,"
    "Total Long Contracts      ,Total Short Contracts\n"
    "Client,215653,55062,3294002,262390,3502967,2930320,3407366,3591086,"
    "2867757,972942,1556553,1310311,13783641,10182767\n"
    "DII,50815,19776,334344,4429649,7390,46308,80,0,5895,41025,355265,20351,"
    "485777,4825121\n"
    "FII,25299,206886,3547694,2919718,606002,1019884,834527,517429,221371,"
    "389353,429052,221921,5809602,5129533\n"
    "Pro,34053,44096,935593,499876,1135458,1142752,1009844,1030750,1031141,"
    "1098289,1785294,949026,5377287,5318886\n"
    "TOTAL,325820,325820,8111633,8111633,5251817,5139264,5251817,5139264,"
    "4126164,2501609,4126164,2501609,25456307,25456307\n"
)


def _frame(d=date(2026, 8, 17)):
    return fc.parse_participant_csv(SAMPLE, d)


# ------------------------------------------------------------------ parsing --

def test_parses_all_five_participant_rows():
    df = _frame()
    assert list(df["participant"]) == ["Client", "DII", "FII", "Pro", "TOTAL"]


def test_trailing_space_headers_do_not_shift_columns():
    """'Future Stock Short       ' and 'Total Long Contracts      ' ship padded."""
    fii = _frame().set_index("participant").loc["FII"]
    assert fii.fut_stk_short == 2919718
    assert fii.total_long == 5809602
    assert fii.total_short == 5129533


def test_total_long_equals_total_short():
    """Every derivative contract has a long and a short; violation = misalignment."""
    tot = _frame().set_index("participant").loc["TOTAL"]
    assert tot.total_long == tot.total_short
    assert tot.fut_idx_long == tot.fut_idx_short


def test_participants_sum_to_total():
    df = _frame()
    parts = df[df.participant != "TOTAL"]
    tot = df[df.participant == "TOTAL"].iloc[0]
    for col in ("fut_idx_long", "fut_stk_long", "total_long", "total_short"):
        assert int(parts[col].sum()) == int(tot[col]), col


def test_garbage_returns_none_not_partial_frame():
    assert fc.parse_participant_csv("garbage\nnot,a,csv\n", date(2026, 8, 17)) is None
    assert fc.parse_participant_csv("", date(2026, 8, 17)) is None


def test_missing_columns_become_na_not_zero():
    """Older files predate stock options. NA is honest; 0 is a fabricated position."""
    trimmed = "\n".join(
        ",".join(ln.split(",")[:5]) for ln in SAMPLE.strip().split("\n")
    )
    df = fc.parse_participant_csv(trimmed, date(2014, 3, 14))
    assert df is not None
    assert df["opt_stk_ce_long"].isna().all()
    assert not (df["opt_stk_ce_long"] == 0).any()


# ------------------------------------------------- lookahead guard (core) --

def _seed_archive(tmp_path, monkeypatch, days):
    """Write one parquet per given trade date into a temp archive."""
    oi = tmp_path / "participant_oi"
    oi.mkdir()
    for d in days:
        _frame(d).to_parquet(oi / f"{d.strftime('%Y%m%d')}.parquet", index=False)
    monkeypatch.setattr(fc, "OI_DIR", str(oi))
    return oi


def test_available_from_is_strictly_after_trade_date(tmp_path, monkeypatch):
    _seed_archive(tmp_path, monkeypatch,
                  [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17)])
    panel = fc.load_panel()
    known = panel.dropna(subset=["available_from"])
    assert len(known) > 0
    assert (known["available_from"] > known["trade_date"]).all(), \
        "a row usable on its own trade date is lookahead"


def test_available_from_skips_the_weekend_gap(tmp_path, monkeypatch):
    """Fri 14 Aug -> next SESSION is Mon 17 Aug, not Sat 15 (calendar+1 is wrong)."""
    _seed_archive(tmp_path, monkeypatch,
                  [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17)])
    panel = fc.load_panel()
    fri = panel[panel.trade_date == pd.Timestamp("2026-08-14")].iloc[0]
    assert fri.available_from == pd.Timestamp("2026-08-17")


def test_last_session_has_no_available_from_and_is_dropped(tmp_path, monkeypatch):
    """The successor session has not happened; guessing it would be fabrication."""
    _seed_archive(tmp_path, monkeypatch, [date(2026, 8, 13), date(2026, 8, 14)])
    panel = fc.load_panel()
    last = panel[panel.trade_date == pd.Timestamp("2026-08-14")]
    assert last["available_from"].isna().all()

    sig = fc.as_signal("FII")
    assert pd.Timestamp("2026-08-14") not in set(sig["trade_date"])
    assert sig.index.name == "available_from"


def test_as_signal_is_indexed_by_actionable_date(tmp_path, monkeypatch):
    _seed_archive(tmp_path, monkeypatch,
                  [date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17)])
    sig = fc.as_signal("FII")
    assert (sig["participant"] == "FII").all()
    assert (sig.index > sig["trade_date"]).all()


# ------------------------------------------------------- archive-hole guard --

def test_hole_in_archive_does_not_become_a_session_boundary(tmp_path, monkeypatch):
    """Caught live: a part-filled archive dated a 2015 row actionable in 2026.

    `available_from` is "the next file present", which equals "the next session"
    only when the archive is complete. Across a hole it must be unknown.
    """
    _seed_archive(tmp_path, monkeypatch, [date(2015, 6, 10), date(2026, 8, 10)])
    panel = fc.load_panel()
    old = panel[panel.trade_date == pd.Timestamp("2015-06-10")]
    assert old["available_from"].isna().all(), \
        "an 11-year gap was treated as an overnight hold"


def test_change_across_a_hole_is_null_not_an_eleven_year_drift(tmp_path, monkeypatch):
    _seed_archive(tmp_path, monkeypatch, [date(2015, 6, 10), date(2026, 8, 10)])
    panel = fc.load_panel()
    later = panel[panel.trade_date == pd.Timestamp("2026-08-10")]
    assert later["fut_idx_net_chg"].isna().all()
    assert later["long_share_chg"].isna().all()


def test_ordinary_holiday_weekend_is_still_a_valid_boundary(tmp_path, monkeypatch):
    """The guard must not be so tight that Diwali or a long weekend nulls data."""
    _seed_archive(tmp_path, monkeypatch, [date(2026, 8, 14), date(2026, 8, 20)])
    panel = fc.load_panel()
    before = panel[panel.trade_date == pd.Timestamp("2026-08-14")]
    assert (before["available_from"] == pd.Timestamp("2026-08-20")).all()
    after = panel[panel.trade_date == pd.Timestamp("2026-08-20")]
    assert after["fut_idx_net_chg"].notna().all()


def test_hole_rows_are_excluded_from_the_tradeable_signal(tmp_path, monkeypatch):
    _seed_archive(tmp_path, monkeypatch, [date(2015, 6, 10), date(2026, 8, 10)])
    sig = fc.as_signal("FII")
    assert pd.Timestamp("2015-06-10") not in set(sig["trade_date"])


# ------------------------------------------------------------------ derived --

def test_net_positions_are_long_minus_short():
    d = fc.add_derived(_frame()).set_index("participant")
    assert d.loc["FII"].fut_idx_net == 25299 - 206886
    assert d.loc["FII"].fut_stk_net == 3547694 - 2919718
    assert d.loc["DII"].fut_stk_net == 334344 - 4429649


def test_long_share_is_scale_free():
    d = fc.add_derived(_frame())
    assert ((d["long_share"] > 0) & (d["long_share"] < 1)).all()


def test_total_row_is_balanced_in_derived_space():
    d = fc.add_derived(_frame()).set_index("participant")
    assert d.loc["TOTAL"].net_contracts == 0
    assert d.loc["TOTAL"].long_share == pytest.approx(0.5)


def test_change_is_per_participant_not_across_rows(tmp_path, monkeypatch):
    """A diff over an unsorted frame would difference FII against Client."""
    _seed_archive(tmp_path, monkeypatch, [date(2026, 8, 13), date(2026, 8, 14)])
    panel = fc.load_panel()
    first = panel[panel.trade_date == pd.Timestamp("2026-08-13")]
    assert first["fut_idx_net_chg"].isna().all(), "first obs per participant has no prior"
    # identical seeded days => zero change, never a cross-participant difference
    second = panel[panel.trade_date == pd.Timestamp("2026-08-14")]
    assert (second["fut_idx_net_chg"] == 0).all()


# --------------------------------------------------------------------- cash --

def test_append_cash_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "ARCHIVE", str(tmp_path))
    monkeypatch.setattr(fc, "CASH_PATH", str(tmp_path / "cash.jsonl"))
    rows = [{"trade_date": "2026-08-17", "participant": "FII/FPI",
             "buy_cr": 1.0, "sell_cr": 2.0, "net_cr": -1.0, "captured_at": "x"}]
    assert fc.append_cash(rows) == 1
    assert fc.append_cash(rows) == 0, "re-running the daily job must not duplicate"
    lines = open(fc.CASH_PATH, encoding="utf-8").read().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["net_cr"] == -1.0
