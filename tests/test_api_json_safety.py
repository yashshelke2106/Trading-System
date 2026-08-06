"""NaN must never reach the JSON encoder.

/api/accuracy returned a 500 ("Out of range float values are not JSON
compliant: nan") whenever the signal journal held an empty metric, so one
missing number blanked the whole ACCURACY panel. pandas is the usual source:
`df.where(df.notna(), other=None)` does NOT strip NaN from a float column —
None is coerced straight back to NaN.
"""

import math

from api_server import _json_safe


def test_json_safe_strips_nan_and_inf():
    out = _json_safe({"a": float("nan"), "b": float("inf"),
                      "c": float("-inf"), "d": 1.5})
    assert out["a"] is None
    assert out["b"] is None
    assert out["c"] is None
    assert out["d"] == 1.5


def test_json_safe_recurses_into_lists_and_dicts():
    out = _json_safe({"rows": [{"pnl": float("nan")}, {"pnl": 2.0}],
                      "nested": {"deep": [float("nan")]}})
    assert out["rows"][0]["pnl"] is None
    assert out["rows"][1]["pnl"] == 2.0
    assert out["nested"]["deep"][0] is None


def test_json_safe_leaves_non_floats_alone():
    out = _json_safe({"s": "x", "i": 3, "b": True, "n": None})
    assert out == {"s": "x", "i": 3, "b": True, "n": None}


def test_accuracy_endpoint_serialises_with_nan_in_journal(monkeypatch):
    """End-to-end: a NaN in the journal frame must not 500 the endpoint."""
    import pandas as pd
    from fastapi.testclient import TestClient

    import api_server as A

    class _DD:
        def load_signal_journal_frame(self):
            return pd.DataFrame({"symbol": ["X"], "pnl_pct": [float("nan")]})

        def get_signal_pnl_summary(self):
            return {"total": float("nan")}

        def get_live_signal_tracking(self):
            return [{"symbol": "X", "move": float("nan")}]

    monkeypatch.setattr(A, "_get_dashboard_data", lambda: _DD())
    A._CACHE.pop("accuracy", None)

    resp = TestClient(A.app).get("/api/accuracy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["records"][0]["pnl_pct"] is None
    assert body["pnl_summary"]["total"] is None
    assert not any(
        isinstance(v, float) and math.isnan(v)
        for rec in body["records"] for v in rec.values()
    )
