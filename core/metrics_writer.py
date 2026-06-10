"""
Daily metrics writer — runs once at session close (or on-demand).

Reads:
  - logs/signal_journal.jsonl  (outcomes appended by signal_tracker.py)
  - logs/signals.json          (signals emitted today by scan_only_v2.py)

Writes:
  - logs/metrics_daily.jsonl   (one JSON line per trading day)

Schema per line:
  {
    "date": "2026-05-29",
    "regime": {"tag": "trend_up", "adx": 27.7, "atr_pct": 74.2},
    "signals_emitted": 5,
    "signals_taken": 3,
    "wins": 1, "losses": 1, "timeouts": 1,
    "win_rate": 0.50,
    "rolling_30d": {"signals": 60, "wr": 0.42, "pf": 1.05, "max_dd_pct": -3.2},
    "rolling_90d": {"signals": 178, "wr": 0.39, "pf": 0.94, "max_dd_pct": -5.1},
    "drift_alert": false,         # true if 30d PF < 0.9 or DD > 8%
    "redesign_alert": false       # true if 90d WR < 35% for 60d straight
  }

Call from a daily cron or at the end of signal_tracker.py session.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

ROOT          = Path(__file__).resolve().parent.parent
LOG_DIR       = ROOT / "logs"
JOURNAL_FILE  = LOG_DIR / "signal_journal.jsonl"
SIGNALS_FILE  = LOG_DIR / "signals.json"
METRICS_FILE  = LOG_DIR / "metrics_daily.jsonl"

# ── Drift / redesign thresholds ──────────────────────────────────────
DRIFT_PF_30D_MIN     = 0.90
DRIFT_DD_30D_MAX_PCT = 8.0
REDESIGN_WR_90D_MIN  = 0.35
REDESIGN_STREAK_DAYS = 60
# A real spot/futures PF cannot sustain >~3. A higher value means the window
# is being polluted by OPTION-PREMIUM %s (theta/IV swings) — i.e. the metric
# itself is broken, NOT that the system is brilliant. Fire the alarm either way.
IMPLAUSIBLE_PF_MAX   = 3.0
# Below this many TRUSTWORTHY (spot/futures) closed trades the window is not
# informative — report "insufficient data", never a false "healthy".
MIN_CLEAN_TRADES_30D = 20


def _load_journal(since: Optional[date] = None) -> List[Dict]:
    if not JOURNAL_FILE.exists():
        return []
    rows = []
    with open(JOURNAL_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if since:
                    ts = row.get("exit_ts") or row.get("entry_ts") or row.get("ts")
                    if ts:
                        d = datetime.fromisoformat(ts.split("T")[0]).date()
                        if d < since:
                            continue
                rows.append(row)
            except Exception:
                continue
    return rows


def _signals_emitted_today() -> int:
    if not SIGNALS_FILE.exists():
        return 0
    try:
        with open(SIGNALS_FILE) as f:
            data = json.load(f)
        return int(data.get("count", len(data.get("signals", []))))
    except Exception:
        return 0


def _classify(row: Dict) -> str:
    """Map journal row → WIN / LOSS / TIMEOUT on the TRUSTWORTHY directional
    result (spot/futures), not the option-premium outcome.

    Premium tags like TARGET_HIT/SL_HIT describe the OPTION leg (theta/IV
    polluted). For a futures-mode system the spot move is what matters, so we
    classify on the sign of the clean pnl (spot_pnl_pct, or pnl_pct for pure
    futures rows). Falls back to the outcome tag only when no clean pnl exists.
    BE_STOP / ~flat = TIMEOUT (scratch, neither win nor loss).
    """
    pnl = _clean_pnl_pct(row)
    if pnl is not None:
        if pnl > 0.05:
            return "WIN"
        if pnl < -0.05:
            return "LOSS"
        return "TIMEOUT"
    out = (row.get("outcome") or row.get("status") or "").upper()
    if out in ("WIN", "TARGET", "TARGET_HIT", "T1", "T2"):
        return "WIN"
    if out == "BE_STOP":
        return "TIMEOUT"
    if out in ("LOSS", "SL", "SL_HIT", "STOP"):
        return "LOSS"
    return "TIMEOUT"


def _pnl_pct(row: Dict) -> float:
    v = row.get("pnl_pct") or row.get("pnl_percent")
    try:
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def _clean_pnl_pct(row: Dict) -> Optional[float]:
    """TRUSTWORTHY directional %% for a row, or None if not trustworthy.

    Prefers spot_pnl_pct (theta/IV-denoised spot move). Accepts pnl_pct ONLY
    for pure futures/spot rows (no option leg). Option-premium rows return
    None so their huge premium swings can never inflate PF again — this is the
    fix for the PF~16 mirage that blinded the drift alarm.
    """
    sp = row.get("spot_pnl_pct")
    if sp is None:
        extra = row.get("extra")
        if isinstance(extra, dict):
            sp = extra.get("spot_pnl_pct")
    if sp is not None:
        try:
            return float(sp)
        except Exception:
            return None
    instrument = str(row.get("instrument") or "").upper()
    if row.get("entry_prem") is None and instrument in ("FUT", "FUTURE", "FUTURES", "SPOT"):
        return _pnl_pct(row)
    return None


def _is_junk(row: Dict) -> bool:
    """Replay-failed / holiday-shifted rows have no real fill — exclude from
    stats. extra.replay_failed is set by signal_tracker when price data was
    missing (e.g. holiday). Including them produced the -491% DD nonsense."""
    extra = row.get("extra") or {}
    if isinstance(extra, dict) and extra.get("replay_failed"):
        return True
    return False


def _sort_key(row: Dict):
    ts = row.get("exit_ts") or row.get("entry_ts") or row.get("ts") or ""
    return ts


def _window_stats(rows: List[Dict]) -> Dict:
    # Drop junk rows, then order by exit timestamp so cumulative equity (and
    # therefore drawdown) is chronological — not file-append order.
    rows = sorted((r for r in rows if not _is_junk(r)), key=_sort_key)
    seen = len(rows)
    # Only TRUSTWORTHY (spot/futures) rows count toward money stats. Option-
    # premium rows are excluded so their theta/IV swings can't inflate PF.
    clean = [(r, _clean_pnl_pct(r)) for r in rows]
    clean = [(r, p) for r, p in clean if p is not None]
    n = len(clean)
    excluded_premium = seen - n
    if n == 0:
        return {"signals": 0, "wr": 0.0, "pf": 0.0, "max_dd_pct": 0.0,
                "clean_n": 0, "excluded_premium": excluded_premium}
    wins = [(r, p) for r, p in clean if _classify(r) == "WIN"]
    losses = [(r, p) for r, p in clean if _classify(r) == "LOSS"]
    wr = len(wins) / max(n, 1)
    gross_win = sum(p for _, p in wins)
    gross_loss = abs(sum(p for _, p in losses))
    pf = gross_win / max(gross_loss, 0.001)
    # Max drawdown on a FIXED-FRACTION equity curve from the clean directional
    # %s (these are real spot/futures moves, so they compound honestly). Model
    # each trade as risking ~1% of equity scaled by the move.
    equity = 100.0; peak = 100.0; mdd = 0.0
    for _, p in clean:
        ret = max(-50.0, min(50.0, p))               # clamp pathological rows
        equity *= (1 + 0.01 * (ret / 100.0))
        peak = max(peak, equity)
        if peak > 0:
            mdd = min(mdd, (equity - peak) / peak * 100)
    return {
        "signals": n,
        "wr": round(wr, 3),
        "pf": round(pf, 2),
        "max_dd_pct": round(mdd, 2),
        "clean_n": n,
        "excluded_premium": excluded_premium,
    }


def _wr_streak_days(metrics_history: List[Dict], min_wr: float) -> int:
    """Count consecutive trailing days whose rolling-90d WR is below min_wr."""
    streak = 0
    for entry in reversed(metrics_history):
        wr = entry.get("rolling_90d", {}).get("wr", 1.0)
        if wr < min_wr:
            streak += 1
        else:
            break
    return streak


def write_metrics(as_of: Optional[date] = None, force: bool = False) -> Dict:
    """Compute today's metrics line + append to metrics_daily.jsonl.
    Returns the written dict.

    Idempotent per day: if a record for `today` already exists, returns it
    unchanged instead of appending a duplicate (tracker may restart after
    close). Pass force=True to overwrite-append anyway (e.g. backfill)."""
    today = as_of or date.today()

    # Idempotency guard — skip if today already recorded
    if not force and METRICS_FILE.exists():
        with open(METRICS_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("date") == today.isoformat():
                        return rec
                except Exception:
                    continue

    # Pull rolling windows
    rows_30d = _load_journal(since=today - timedelta(days=30))
    rows_90d = _load_journal(since=today - timedelta(days=90))
    rows_today = [r for r in rows_30d
                  if (r.get("exit_ts", "") or r.get("entry_ts", ""))[:10] == today.isoformat()]

    wins_today = [r for r in rows_today if _classify(r) == "WIN"]
    losses_today = [r for r in rows_today if _classify(r) == "LOSS"]
    timeouts_today = [r for r in rows_today if _classify(r) == "TIMEOUT"]
    n_today = len(rows_today)
    wr_today = len(wins_today) / max(n_today, 1)

    # Regime snapshot (no-op if unavailable)
    regime_info = {}
    try:
        from core.regime_filter import get_regime
        snap = get_regime()
        regime_info = {"tag": snap.tag, "adx": snap.adx, "atr_pct": snap.atr_pct,
                       "ret_20d_pct": snap.ret_20d_pct}
    except Exception:
        regime_info = {"tag": "unknown"}

    r30 = _window_stats(rows_30d)
    r90 = _window_stats(rows_90d)

    # Drift signals — now able to FIRE (the old version was blind because PF
    # was computed on premium %s pinned near 16, never < 0.9).
    drift = False
    drift_reasons: List[str] = []
    clean_n = r30.get("clean_n", r30.get("signals", 0))
    if clean_n < MIN_CLEAN_TRADES_30D:
        drift = True
        drift_reasons.append(f"insufficient_clean_data_{clean_n}<{MIN_CLEAN_TRADES_30D}")
    else:
        if r30["pf"] < DRIFT_PF_30D_MIN:
            drift = True
            drift_reasons.append(f"pf_{r30['pf']}_below_{DRIFT_PF_30D_MIN}")
        if r30["pf"] > IMPLAUSIBLE_PF_MAX:
            drift = True
            drift_reasons.append(f"pf_{r30['pf']}_IMPLAUSIBLE_metric_bug_or_bias")
        if r30["max_dd_pct"] <= -DRIFT_DD_30D_MAX_PCT:
            drift = True
            drift_reasons.append(f"dd_{r30['max_dd_pct']}_exceeds_{DRIFT_DD_30D_MAX_PCT}")

    # Read prior metrics history to compute redesign streak
    prior_history: List[Dict] = []
    if METRICS_FILE.exists():
        with open(METRICS_FILE) as f:
            for line in f:
                try:
                    prior_history.append(json.loads(line))
                except Exception:
                    continue
    redesign = _wr_streak_days(prior_history, REDESIGN_WR_90D_MIN) >= REDESIGN_STREAK_DAYS

    record = {
        "date": today.isoformat(),
        "regime": regime_info,
        "signals_emitted": _signals_emitted_today(),
        "signals_taken": n_today,
        "wins": len(wins_today),
        "losses": len(losses_today),
        "timeouts": len(timeouts_today),
        "win_rate": round(wr_today, 3),
        "rolling_30d": r30,
        "rolling_90d": r90,
        "clean_trades_30d": clean_n,
        "excluded_premium_30d": r30.get("excluded_premium", 0),
        "drift_alert": bool(drift),
        "drift_reasons": drift_reasons,
        "redesign_alert": bool(redesign),
    }

    LOG_DIR.mkdir(exist_ok=True)
    with open(METRICS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")

    # Alert on drift / redesign so a degrading system doesn't go unnoticed.
    try:
        from core.health import alert
        if drift:
            alert("DRIFT",
                  f"⚠️ DRIFT {today.isoformat()} — {'; '.join(drift_reasons)} "
                  f"(30d PF {r30.get('pf')} DD {r30.get('max_dd_pct')}% "
                  f"clean_n {clean_n}). Review before next session.")
        if redesign:
            alert("REDESIGN",
                  f"🛑 REDESIGN {today.isoformat()} — 60+ days of 90d WR "
                  f"{r90.get('wr')} below {REDESIGN_WR_90D_MIN:.0%}. Edge gone.")
    except Exception:
        pass

    return record


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rec = write_metrics()
    print(json.dumps(rec, indent=2))
