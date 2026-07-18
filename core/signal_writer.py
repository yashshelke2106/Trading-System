"""Atomic JSON writer for logs/signals.json — shared between scanner and Streamlit.

Signals persist for SIGNAL_TTL_SEC after FIRST discovery. Once they age out,
they're removed even if the scanner keeps re-detecting them. This prevents
"sticky signal" bug where same trades show all day.
"""
import json
import os
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNALS_FILE = os.path.join(_BASE, "logs", "signals.json")
SIGNALS_PATH = SIGNALS_FILE
HISTORY_FILE = os.path.join(_BASE, "logs", "signals_history.jsonl")

SIGNAL_TTL_SEC = 900        # signals expire 15 min after FIRST discovery
RE_ENTRY_COOLDOWN_SEC = 600   # 10 min before same symbol can re-signal


def _merge_with_existing(new_signals: list) -> list:
    """Merge new signals with unexpired existing signals.

    Key rules:
      1. Each signal has first_seen_ts (set once, never refreshed)
      2. Signal removed when (now - first_seen_ts) > SIGNAL_TTL_SEC
      3. Once removed, same symbol can't re-signal for RE_ENTRY_COOLDOWN_SEC
      4. ts = display timestamp (refreshes on re-detect, for UI freshness)
    """
    now = datetime.now()
    now_iso = now.isoformat()

    existing = []
    cooldown: dict = {}   # symbol -> expiry timestamp
    try:
        if os.path.exists(SIGNALS_FILE):
            with open(SIGNALS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            existing = data.get("signals", [])
            cooldown = data.get("_cooldown", {}) or {}
    except Exception:
        pass

    merged = {}

    # Step 1: keep existing signals that haven't expired (use first_seen_ts)
    # Skip cooldown for signals far older than TTL — they're stale from a
    # system restart, NOT "just expired". Otherwise every restart blocks
    # yesterday's symbols for 10 min for no reason.
    STALE_GAP_SEC = 3600   # >1h old = stale, drop silently
    for sig in existing:
        sym = sig.get("symbol", "")
        first_ts = sig.get("first_seen_ts") or sig.get("ts", "")
        try:
            first_time = datetime.fromisoformat(first_ts)
            age_sec = (now - first_time).total_seconds()
            if age_sec <= SIGNAL_TTL_SEC:
                merged[sym] = sig
            elif age_sec < STALE_GAP_SEC:
                # Recently expired — apply cooldown so we don't re-signal immediately
                cooldown[sym] = now.timestamp() + RE_ENTRY_COOLDOWN_SEC
            # else: stale signal from old session — drop, no cooldown registration
        except (ValueError, TypeError):
            pass

    # Step 2: clean expired cooldowns
    cooldown = {sym: ts for sym, ts in cooldown.items() if ts > now.timestamp()}

    # Step 3: accept new signals (unless symbol in cooldown)
    for sig in new_signals:
        sym = sig.get("symbol", "")
        if sym in cooldown:
            continue   # symbol just expired, wait for cooldown
        if sym in merged:
            # Re-detected: keep original first_seen_ts, refresh ts only
            sig["first_seen_ts"] = merged[sym].get("first_seen_ts") or merged[sym].get("ts", now_iso)
        else:
            sig["first_seen_ts"] = now_iso
        sig["ts"] = now_iso
        merged[sym] = sig

    # Stash cooldown for next write
    _merge_with_existing._last_cooldown = cooldown
    return list(merged.values())


def write_signals(signals: list, meta: dict = None) -> None:
    os.makedirs(os.path.dirname(SIGNALS_FILE), exist_ok=True)

    # Validate at boundary — reject malformed signals BEFORE they pollute
    # downstream consumers (Streamlit / adaptive learner / signal tracker).
    try:
        from core.schemas import batch_validate_signals
        signals = batch_validate_signals(signals)
    except Exception:
        pass  # schemas optional — fall through

    # F&O-only gate at write boundary — non-F&O never reaches signals.json
    try:
        from core.nse_option_chain import is_fno_symbol
        before = len(signals)
        signals = [s for s in signals if is_fno_symbol(s.get("symbol", ""))]
        if len(signals) < before:
            import logging as _lg
            _lg.getLogger(__name__).info(
                f"[signal_writer] dropped {before - len(signals)} non-F&O signals"
            )
    except Exception:
        pass

    # ── agentic layer (both strictly risk-reducing; never block the write) ──
    # LLM review: CONCUR/VETO/ABSTAIN annotation only (opt-in via
    # AGENT_SIGNAL_REVIEW=1). Cannot alter trade parameters.
    try:
        from core.agent_signal_review import review_signals
        signals = review_signals(signals)
    except Exception:
        pass
    # Sentinel: halt-only flag from the risk-sentinel agent. Marks new signals
    # blocked; deterministic pipeline output otherwise untouched.
    try:
        from core.sentinel import entries_halted
        halted, why = entries_halted()
        if halted:
            for s in signals:
                s["sentinel_halt"] = True
                s["sentinel_reason"] = why
    except Exception:
        pass

    all_signals = _merge_with_existing(signals)
    cooldown = getattr(_merge_with_existing, "_last_cooldown", {})

    by_grade = {
        "S": sum(1 for s in all_signals if s.get("confluence_grade") == "S"),
        "A": sum(1 for s in all_signals if s.get("confluence_grade") == "A"),
        "B": sum(1 for s in all_signals if s.get("confluence_grade") == "B"),
        "C": sum(1 for s in all_signals if s.get("confluence_grade") == "C"),
    }
    payload = {
        "ts": datetime.now().isoformat(),
        "count": len(all_signals),
        "new_this_scan": len(signals),
        "by_grade": by_grade,
        "meta": meta or {},
        "signals": all_signals,
        "_cooldown": cooldown,   # symbol -> unix ts when cooldown ends
    }
    tmp = SIGNALS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SIGNALS_FILE)

    if signals:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": payload["ts"], "signals": signals}, default=str) + chr(10))


def read_signals() -> dict:
    if not os.path.exists(SIGNALS_FILE):
        return {}
    try:
        with open(SIGNALS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
