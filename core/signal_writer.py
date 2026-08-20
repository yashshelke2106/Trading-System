"""Atomic JSON writer for logs/signals.json — shared between scanner and Streamlit.

RETENTION (changed 2026-08-14 — reported: "trades disappear from the dashboard
before they hit target or stoploss"):

An UNRESOLVED trade is never aged out. SIGNAL_TTL_SEC used to drop every signal
15 minutes after first discovery no matter what it was doing, so a position you
were actively watching vanished from the terminal while it was still live —
there was no way to see how it ended. A signal now survives while the journal
still lists it as open, and leaves only when the tracker resolves it
(TARGET_HIT / SL_HIT / EXPIRED) or it exceeds the runaway cap below.

The old TTL still applies to signals the journal is NOT tracking, which keeps
the original "sticky signal" fix intact for untracked chatter.
"""
import json
import os
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNALS_FILE = os.path.join(_BASE, "logs", "signals.json")
SIGNALS_PATH = SIGNALS_FILE
HISTORY_FILE = os.path.join(_BASE, "logs", "signals_history.jsonl")

SIGNAL_TTL_SEC = 900        # untracked signals expire 15 min after discovery
RE_ENTRY_COOLDOWN_SEC = 600   # 10 min before same symbol can re-signal
# Safety valve: if the tracker stops resolving (crashed, no price feed), an
# open signal must not wedge the feed forever. Generous enough that a genuine
# multi-day hold is never cut short.
MAX_OPEN_AGE_SEC = 3 * 24 * 3600


def _open_keys() -> set:
    """(symbol, direction) the journal still considers unresolved.

    Empty set on any failure, which degrades to the old age-only behaviour
    rather than pinning signals on screen forever.
    """
    try:
        from core.signal_journal import get_open_signals
        return {(r.get("symbol"), r.get("direction")) for r in get_open_signals()}
    except Exception:
        return set()


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
    open_keys = _open_keys()
    for sig in existing:
        sym = sig.get("symbol", "")
        first_ts = sig.get("first_seen_ts") or sig.get("ts", "")
        try:
            first_time = datetime.fromisoformat(first_ts)
            age_sec = (now - first_time).total_seconds()
            # An unresolved trade outranks the clock: keep it until the tracker
            # says TARGET_HIT / SL_HIT / EXPIRED. Dropping a live position at
            # 15 minutes is what made trades vanish mid-flight.
            still_open = (sym, sig.get("direction")) in open_keys
            if still_open and age_sec <= MAX_OPEN_AGE_SEC:
                sig["open"] = True
                merged[sym] = sig
            elif not still_open and age_sec <= SIGNAL_TTL_SEC:
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

    # Journal at the WRITE boundary, not in the callers. scan_only_v2 records
    # its signals but core/agents/signal_agent.py (the other writer, started
    # unconditionally by aladdin_runner) had zero record_signal calls — so
    # everything it produced was untracked and could never be resolved to
    # TARGET_HIT/SL_HIT at all. Journaling here covers every writer at once.
    # record_signal dedupes on (symbol, direction, date), so the 30s scan loop
    # re-recording the same signal is a no-op that returns the existing id.
    try:
        from core.signal_journal import record_signal
        for s in signals:
            try:
                s["signal_id"] = record_signal(s)
            except Exception:
                pass          # journaling must never block the write
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
    # PER-PROCESS staging file. The tmp path used to be shared, and
    # start_trading.bat runs TWO writers concurrently: scan_only_v2 and
    # aladdin_runner's SignalAgent (started unconditionally at
    # aladdin_runner.py:541 — `--no-scan` only disables its other writer).
    # Both opened "signals.json.tmp" with mode "w", so the shorter payload
    # truncated the longer one mid-flight and os.replace published a valid
    # JSON document with a foreign tail glued on. Observed 2026-08-11: a
    # 5,772-byte file whose document ended at char 3701. Every reader
    # swallows the parse error and returns empty, so the whole UI just went
    # blank with no error anywhere. os.replace stays atomic; last writer
    # simply wins with a COMPLETE document.
    tmp = f"{SIGNALS_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SIGNALS_FILE)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)          # crashed mid-write: don't leak staging files
            except OSError:
                pass

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
