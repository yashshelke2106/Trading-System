"""The audit that would have caught pairs, and the gate that would have too.

Every piece of validation machinery in this repo already existed when
pairs_program.py shipped a claim its own re-test disproved. research_gates,
research_integrity and the hypothesis registry all worked. Nothing forced any
of them to run. These tests pin the two things added to close that gap:

  * claim_audit    — a file asserting an edge must point at a closed hypothesis
  * gate 7         — the validated book must be one the account can hold
"""

import textwrap

import pytest

from core.claim_audit import audit, unverified, report
from core.research_gates import GateReport


# ── claim_audit ──────────────────────────────────────────────────────────────

def _repo(tmp_path, **files):
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(tmp_path)


def _closed(monkeypatch, *hids):
    import core.claim_audit as ca
    monkeypatch.setattr(ca, "_closed_hids", lambda: set(hids))


def test_flags_the_exact_sentence_that_shipped(tmp_path, monkeypatch):
    """pairs_program.py's real first line, verbatim."""
    _closed(monkeypatch)
    root = _repo(tmp_path, **{"pairs_program.py": '''
        """
        pairs_program.py — a market-neutral statistical-arbitrage program.
        The honest build of the one lead that survived the hunt.
        """
    '''})
    found = unverified(root)
    assert len(found) == 1
    assert "survived the hunt" in found[0].line
    assert found[0].reason == "no hypothesis id anywhere in the file"


def test_a_closed_hypothesis_id_clears_the_claim(tmp_path, monkeypatch):
    _closed(monkeypatch, "H-019")
    root = _repo(tmp_path, **{"pairs_program.py": '''
        """
        The one lead that survived the hunt.
        CLOSED - H-019 REJECTED, p=0.190 against a matched null.
        """
    '''})
    assert unverified(root) == []
    assert audit(root)[0].backed is True


def test_an_open_hypothesis_id_does_not_clear_it(tmp_path, monkeypatch):
    """Registering an idea is not testing it. Only a CLOSURE backs a claim."""
    _closed(monkeypatch, "H-019")          # H-020 registered but still open
    root = _repo(tmp_path, **{"thing.py": '''
        """A genuine edge, tracked as H-020."""
    '''})
    found = unverified(root)
    assert len(found) == 1
    assert "no closure" in found[0].reason


def test_recording_a_rejection_is_not_a_claim(tmp_path, monkeypatch):
    _closed(monkeypatch)
    root = _repo(tmp_path, **{"notes.py": '''
        """This was not a real edge - the Sharpe 1.26 was a mirage."""
    '''})
    assert unverified(root) == []


def test_a_precondition_is_not_a_claim(tmp_path, monkeypatch):
    """config.py says 'until a genuine edge is validated'. Flagging that would
    train the reader to ignore the audit."""
    _closed(monkeypatch)
    root = _repo(tmp_path, **{"config.py": '''
        # Set True only until a genuine edge is validated by the pipeline.
        PAPER_TRADE = True
    '''})
    assert unverified(root) == []


def test_a_keyword_argument_is_not_a_sharpe_claim(tmp_path, monkeypatch):
    """`sharpe=1.2` in a docstring example is not an assertion; `Sharpe > 1` is."""
    _closed(monkeypatch)
    root = _repo(tmp_path, **{
        "ex.py": '"""    validate_result("my_edge", sharpe=1.2, oos_half1=0.9)"""',
        "claimy.py": '"""Result: Sharpe > 1 on both halves."""',
    })
    paths = {f.path for f in unverified(root)}
    assert "ex.py" not in paths
    assert "claimy.py" in paths


def test_tests_directory_is_not_audited(tmp_path, monkeypatch):
    _closed(monkeypatch)
    root = _repo(tmp_path, **{"tests/test_x.py": '"""a real edge, Sharpe > 3"""'})
    assert unverified(root) == []


def test_report_renders_and_names_the_remedy(tmp_path, monkeypatch):
    _closed(monkeypatch)
    root = _repo(tmp_path, **{"x.py": '"""the one lead that survived the hunt"""'})
    text = report(root)
    assert "UNVERIFIED" in text
    assert "run the test, or delete the sentence" in text


def test_clean_repo_reports_clean(tmp_path, monkeypatch):
    _closed(monkeypatch)
    root = _repo(tmp_path, **{"quiet.py": '"""Loads bars from disk."""'})
    assert unverified(root) == []
    assert "no edge claims found" in report(root)


def test_the_live_repo_has_pairs_backed_by_H019():
    """End-to-end against the real tree: the claim that started this must now
    resolve to its closure, not to an unverified assertion."""
    live = [f for f in audit() if f.path.startswith("pairs_")]
    assert live, "pairs files should still contain claim language"
    assert all(f.backed for f in live), \
        [f"{f.path}:{f.line_no}" for f in live if not f.backed]
    assert all("H-019" in f.hids for f in live)


# ── gate 7: fundability ──────────────────────────────────────────────────────

def test_fundability_fails_the_book_you_cannot_hold():
    """H-019's third kill: 8 pairs = 16 futures legs at Rs 1.34 lakh each."""
    r = GateReport("pairs").fundability(capital=1_000_000,
                                        margin_per_unit=134_118,
                                        units_required=16)
    g = r.gates[-1]
    assert g.passed is False
    assert "not fundable" in g.detail


def test_fundability_passes_when_the_capital_covers_it():
    r = GateReport("small book").fundability(capital=1_000_000,
                                             margin_per_unit=100_000,
                                             units_required=6)
    assert r.gates[-1].passed is True


def test_fundability_is_required_so_silence_is_not_a_pass():
    """The other six gates cannot save a strategy the account cannot carry."""
    r = GateReport("t")
    r.baseline(0.01, 0.001)
    r.spread_t([0.01] * 40 + [0.011] * 40)
    r.point_in_time(True)
    r.corp_actions(True)
    r.cost_sweep({10: 0.10, 20: 0.05})
    r.shuffle(2.0, [0.1, 0.2, 0.3])
    assert "7 FUNDABILITY" in r.missing()
    assert not r.passed()
    assert "INCOMPLETE" in r.verdict()

    r.fundability(capital=500_000, margin_per_unit=50_000, units_required=4)
    assert r.missing() == []
    assert r.passed()


def test_fundability_rejects_nonsense_inputs():
    r = GateReport("t").fundability(capital=100, margin_per_unit=0, units_required=3)
    assert r.gates[-1].passed is False
