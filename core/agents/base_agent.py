import threading
import time
import logging
from abc import ABC, abstractmethod
from core.agent_bus import SharedState, EventBus, AgentEvent

log = logging.getLogger(__name__)


class BaseAgent(ABC):
    name: str = "base"
    interval_sec: float = 0   # 0 = event-driven (no timed loop)

    def __init__(self, state: SharedState, bus: EventBus):
        self.state = state
        self.bus = bus
        self._thread: threading.Thread = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()
        log.info(f"[{self.name}] started")

    def stop(self) -> None:
        self._running = False

    def emit(self, event_type: str, payload: dict) -> None:
        self.bus.publish(AgentEvent(type=event_type, payload=payload, source=self.name))

    def _loop(self) -> None:
        if self.interval_sec > 0:
            while self._running:
                try:
                    self.run()
                except Exception as e:
                    log.error(f"[{self.name}] error: {e}", exc_info=True)
                time.sleep(self.interval_sec)
        else:
            # Event-driven: run() for setup, then idle
            try:
                self.run()
            except Exception as e:
                log.error(f"[{self.name}] setup error: {e}")
            while self._running:
                time.sleep(1)

    @abstractmethod
    def run(self) -> None:
        pass
