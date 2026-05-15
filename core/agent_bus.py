"""
Shared state + event bus for multi-agent trading system.

SharedState  — single thread-safe truth shared across all agents
EventBus     — synchronous pub/sub with dead letter queue
"""

import logging
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

log = logging.getLogger(__name__)


@dataclass
class AgentEvent:
    type: str
    payload: Dict[str, Any]
    source: str = ""
    ts: float = field(default_factory=time.time)


class EventBus:
    """Async pub/sub event bus with thread-pool dispatch.

    Handlers run in a ThreadPoolExecutor — slow handler can't block
    other handlers or the publisher. Critical events (FINAL_EXECUTE,
    TRADE_ENTERED) run synchronously to preserve ordering guarantees.
    """
    MAX_DLQ = 100  # keep last 100 failed events for diagnostics

    # Events that MUST execute synchronously (ordering matters)
    _SYNC_EVENTS = frozenset({
        "FINAL_EXECUTE", "TRADE_ENTERED", "TRADE_CLOSED",
        "RISK_APPROVED", "EXECUTION_READY",
    })

    def __init__(self, max_workers: int = 8):
        self._subs: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()
        self.dead_letters: deque = deque(maxlen=self.MAX_DLQ)
        self._event_counts: Dict[str, int] = {}
        self._error_counts: Dict[str, int] = {}
        self._handler_latency: Dict[str, float] = {}  # handler → avg ms

        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="bus")

    def subscribe(self, event_type: str, handler: Callable) -> None:
        with self._lock:
            self._subs.setdefault(event_type, []).append(handler)

    def publish(self, event: AgentEvent) -> None:
        with self._lock:
            handlers = list(self._subs.get(event.type, []))
        self._event_counts[event.type] = self._event_counts.get(event.type, 0) + 1

        is_sync = event.type in self._SYNC_EVENTS
        for fn in handlers:
            if is_sync:
                self._call_handler(fn, event)
            else:
                self._pool.submit(self._call_handler, fn, event)

    def _call_handler(self, fn: Callable, event: AgentEvent) -> None:
        t0 = time.time()
        try:
            fn(event)
        except Exception as e:
            self._error_counts[event.type] = self._error_counts.get(event.type, 0) + 1
            self.dead_letters.append({
                "event_type": event.type,
                "payload": event.payload,
                "handler": fn.__qualname__,
                "error": str(e),
                "ts": time.time(),
            })
            log.error(f"[Bus] handler {fn.__qualname__} failed on {event.type}: {e}")
        finally:
            elapsed_ms = (time.time() - t0) * 1000
            # Running average for handler latency tracking
            key = fn.__qualname__
            prev = self._handler_latency.get(key, elapsed_ms)
            self._handler_latency[key] = prev * 0.8 + elapsed_ms * 0.2

    def get_stats(self) -> Dict[str, Any]:
        # Top 5 slowest handlers
        sorted_latency = sorted(self._handler_latency.items(),
                                key=lambda x: x[1], reverse=True)[:5]
        return {
            "events_published": dict(self._event_counts),
            "errors": dict(self._error_counts),
            "dead_letters": len(self.dead_letters),
            "slow_handlers": {k: f"{v:.0f}ms" for k, v in sorted_latency},
        }


class SharedState:
    """
    Thread-safe state visible to all agents.

    Voting API lets each agent approve/reject a symbol.
    get_consensus() requires N agents to agree on the same direction.
    """

    def __init__(self):
        self._lock = threading.RLock()

        # Market context (MarketContextAgent → every 5 min)
        self.market_regime: str = "unknown"   # bull/bear/neutral/volatile
        self.nifty_change_pct: float = 0.0
        self.banknifty_change_pct: float = 0.0
        self.vix_proxy: float = 15.0

        # Scanner (ScannerAgent → every 3 min)
        self.volume_surges: Dict[str, float] = {}   # sym -> projected_vol_ratio
        self.last_scan_ts: float = 0.0

        # Signals (SignalAgent → event-driven)
        self.signals: Dict[str, Any] = {}           # sym -> Signal

        # Filter results (FilterAgent → event-driven)
        self.filter_results: Dict[str, Any] = {}    # sym -> FilterResult
        self.order_flow: Dict[str, Any] = {}        # sym -> OrderFlowAnalysis

        # Risk (RiskAgent → event-driven)
        self.portfolio_heat: float = 0.0            # fraction of capital at risk
        self.daily_pnl: float = 0.0
        self.trading_halted: bool = False
        self.halt_reason: str = ""

        # Positions (ExecutionAgent → managed)
        self.open_positions: Dict[str, Any] = {}    # sym -> {entry, qty, direction, ...}
        self.today_trades: List[Dict] = []
        self.total_trades_today: int = 0

        # Streaks
        self.consecutive_losses: int = 0
        self.win_streak: int = 0

        # Votes: sym -> {agent_name: vote_record}
        self._votes: Dict[str, Dict[str, Dict]] = {}

    # ── Generic accessors ─────────────────────────────────────────────────

    def get(self, key: str, default=None):
        with self._lock:
            return getattr(self, key, default)

    def set(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    # ── Voting API ────────────────────────────────────────────────────────

    def cast_vote(self, symbol: str, agent: str, approve: bool,
                  direction: str = "", confidence: float = 0.5) -> None:
        with self._lock:
            self._votes.setdefault(symbol, {})[agent] = {
                "approve": approve,
                "direction": direction,
                "confidence": confidence,
                "ts": time.time(),
            }

    def clear_votes(self, symbol: str) -> None:
        with self._lock:
            self._votes.pop(symbol, None)

    def get_votes(self, symbol: str) -> Dict[str, Dict]:
        with self._lock:
            return dict(self._votes.get(symbol, {}))

    def get_consensus(self, symbol: str, required: int = 3) -> tuple:
        """
        Returns (should_trade: bool, direction: str, avg_confidence: float).
        All approving voters with a direction must agree on the same direction.
        """
        with self._lock:
            votes = dict(self._votes.get(symbol, {}))

        yes = {a: v for a, v in votes.items() if v["approve"]}
        if len(yes) < required:
            return False, "", 0.0

        dirs = [v["direction"] for v in yes.values() if v["direction"]]
        if dirs:
            top_dir, cnt = Counter(dirs).most_common(1)[0]
            if cnt < len([d for d in dirs]):  # any disagreement
                return False, "", 0.0
            direction = top_dir
        else:
            direction = ""

        conf = sum(v["confidence"] for v in yes.values()) / len(yes)
        return True, direction, conf
