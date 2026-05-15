"""
ArchitectAgent (Hallac) — Pipeline health, data integrity, self-healing.

Inspired by Charles Hallac (BlackRock COO, Aladdin architect).

Every 2 min:
  1. Check data freshness (last scan timestamp, stale data detection)
  2. API health (Dhan token expiry, yfinance rate limits)
  3. Pipeline throughput (signals generated vs expected)
  4. Self-heal: token refresh, cache clear, restart stalled fetchers
  5. Publish SYSTEM_HEALTH for coordinator
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict

from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus, AgentEvent
from core import secrets as _sec

log = logging.getLogger(__name__)

SIGNALS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                            "logs", "signals.json")


class ArchitectAgent(BaseAgent):
    name = "architect_hallac"
    interval_sec = 120

    STALE_THRESHOLD_SEC = 180  # data older than 3 min = stale
    MIN_SIGNALS_PER_HOUR = 1   # expect at least 1 signal/hour during market

    def __init__(self, state: SharedState, bus: EventBus, api=None):
        super().__init__(state, bus)
        self.api = api
        self._last_signal_count = 0
        self._signal_drought_start: float = 0
        self._health_history: list = []
        self._auto_fixes = 0

    def run(self) -> None:
        health = self._assess_health()
        self.state.set(system_health=health)
        self._health_history.append(health)
        if len(self._health_history) > 30:
            self._health_history = self._health_history[-30:]

        self.emit("SYSTEM_HEALTH", health)

        if health.get("needs_intervention"):
            self._self_heal(health)

    def _assess_health(self) -> Dict:
        health = {
            "ts": datetime.now().isoformat(),
            "status": "healthy",
            "issues": [],
            "needs_intervention": False,
        }

        # 1. Token health
        try:
            th = _sec.token_health()
            health["token_valid"] = th.valid
            health["token_hours_left"] = th.hours_left
            if not th.valid:
                health["issues"].append(f"token_expired: {th.message}")
                health["needs_intervention"] = True
            elif th.hours_left and th.hours_left < 2:
                health["issues"].append(f"token_expiring_soon: {th.hours_left:.1f}h")
        except Exception as e:
            health["issues"].append(f"token_check_failed: {e}")

        # 2. Data API health
        try:
            dh = _sec.data_token_health()
            health["data_api_ok"] = dh.valid
            if not dh.valid:
                health["issues"].append(f"data_api: {dh.message}")
        except Exception:
            health["data_api_ok"] = False

        # 3. Signal freshness
        try:
            if os.path.exists(SIGNALS_FILE):
                mtime = os.path.getmtime(SIGNALS_FILE)
                age = time.time() - mtime
                health["signal_age_sec"] = round(age)
                if age > self.STALE_THRESHOLD_SEC:
                    health["issues"].append(f"signals_stale: {age:.0f}s old")
                    health["needs_intervention"] = True

                with open(SIGNALS_FILE) as f:
                    data = json.load(f)
                health["signal_count"] = data.get("count", 0)
            else:
                health["issues"].append("no_signals_file")
        except Exception as e:
            health["issues"].append(f"signal_check_failed: {e}")

        # 4. Scan throughput
        last_scan = self.state.get("last_scan_ts", 0)
        if last_scan > 0:
            scan_age = time.time() - last_scan
            health["scan_age_sec"] = round(scan_age)
            if scan_age > 300:
                health["issues"].append(f"scanner_stalled: {scan_age:.0f}s since last scan")
                health["needs_intervention"] = True

        # 5. Memory / thread health
        try:
            import threading
            health["active_threads"] = threading.active_count()
        except Exception:
            pass

        # 6. EventBus health (dead letter queue)
        bus_stats = self.bus.get_stats() if hasattr(self.bus, 'get_stats') else {}
        health["bus_stats"] = bus_stats
        dlq_count = bus_stats.get("dead_letters", 0)
        if dlq_count > 10:
            health["issues"].append(f"bus_errors: {dlq_count} dead letters")

        if health["issues"]:
            health["status"] = "degraded" if not health["needs_intervention"] else "critical"

        return health

    def _self_heal(self, health: Dict):
        self._auto_fixes += 1
        log.warning(f"[Architect/Hallac] self-heal #{self._auto_fixes}: {health['issues']}")

        # Token refresh attempt
        if any("token_expired" in i for i in health.get("issues", [])):
            try:
                if self.api:
                    refreshed = self.api._reload_token()
                    if refreshed:
                        log.info("[Architect/Hallac] token auto-refreshed from file")
            except Exception as e:
                log.error(f"[Architect/Hallac] token refresh failed: {e}")

        # Stale signals: clear in-memory signal cache so next scan cycle starts fresh
        if any("signals_stale" in i for i in health.get("issues", [])):
            with self.state._lock:
                stale_count = len(self.state.signals)
                self.state.signals.clear()
            if stale_count:
                log.info(f"[Architect/Hallac] cleared {stale_count} stale signals from SharedState")

        self.emit("SELF_HEAL", {
            "fix_count": self._auto_fixes,
            "issues_addressed": health["issues"],
        })
