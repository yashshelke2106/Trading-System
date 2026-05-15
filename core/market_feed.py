"""
DhanMarketFeed — live WebSocket price/volume feed via dhanhq SDK.

Runs DhanFeed in a daemon thread with its own asyncio event loop.
Thread-safe reads from any agent via get_ltp() / get_volume() / get_order_flow().

Usage:
    feed = DhanMarketFeed()
    feed.start(symbol_id_map)   # non-blocking, daemon thread
    ltp = feed.get_ltp("RELIANCE")
    feed.stop()
"""

import asyncio
import logging
import threading
import time
from typing import Dict, Optional, Tuple

import config
from core import secrets as _sec

log = logging.getLogger(__name__)

# Dhan security IDs for indices
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "NIFTYIT"}


class DhanMarketFeed:
    """
    Live WebSocket feed wrapping dhanhq.marketfeed.DhanFeed.
    Maintains LTP, cumulative intraday volume, and buy/sell queue sizes.
    Falls back silently if WebSocket unavailable (mock/no credentials).
    """

    def __init__(self):
        self._prices:    Dict[str, float] = {}   # security_id → LTP
        self._volumes:   Dict[str, int]   = {}   # security_id → cumulative vol today
        self._buy_qty:   Dict[str, int]   = {}   # security_id → total buy queue
        self._sell_qty:  Dict[str, int]   = {}   # security_id → total sell queue
        self._lock = threading.Lock()
        self._running = False
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._symbol_to_id: Dict[str, str] = {}   # "RELIANCE" → "2885"
        self._id_to_symbol: Dict[str, str] = {}   # "2885" → "RELIANCE"

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, symbol_id_map: Dict[str, str], use_quote: bool = True) -> None:
        """
        Start WebSocket feed in background thread.
        symbol_id_map: {"SYMBOL": "security_id", ...}
        use_quote=True → receive LTP + OHLC + volume (recommended)
        use_quote=False → Ticker only (LTP, faster, less data)
        """
        if config.USE_MOCK_DATA:
            log.info("[Feed] mock mode - WebSocket disabled")
            return
        tok = _sec.get_access_token()
        if not tok or not tok.startswith("eyJ"):
            log.warning("[Feed] Dhan token not set - WebSocket disabled")
            return

        self._symbol_to_id = {k.upper(): v for k, v in symbol_id_map.items()}
        self._id_to_symbol = {v: k.upper() for k, v in symbol_id_map.items()}

        instruments = self._build_instruments(use_quote)
        if not instruments:
            log.warning("[Feed] no instruments to subscribe")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._feed_thread,
            args=(instruments,),
            daemon=True,
            name="dhan-market-feed",
        )
        self._thread.start()
        log.info(f"[Feed] WebSocket started - {len(instruments)} instruments")

    def stop(self) -> None:
        self._running = False
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def get_ltp(self, symbol: str) -> Optional[float]:
        sid = self._symbol_to_id.get(symbol.upper())
        if sid is None:
            return None
        with self._lock:
            return self._prices.get(sid)

    def get_volume(self, symbol: str) -> Optional[int]:
        """Cumulative intraday volume for the symbol."""
        sid = self._symbol_to_id.get(symbol.upper())
        if sid is None:
            return None
        with self._lock:
            return self._volumes.get(sid)

    def get_order_flow(self, symbol: str) -> Tuple[int, int]:
        """Returns (total_buy_qty, total_sell_qty) from order queue."""
        sid = self._symbol_to_id.get(symbol.upper())
        if sid is None:
            return 0, 0
        with self._lock:
            return self._buy_qty.get(sid, 0), self._sell_qty.get(sid, 0)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_instruments(self, use_quote: bool) -> list:
        try:
            from dhanhq import marketfeed as mf
            sub_type = mf.Quote if use_quote else mf.Ticker
            instruments = []
            for sym, sid in self._symbol_to_id.items():
                if sym in INDEX_SYMBOLS:
                    instruments.append((mf.IDX, sid, sub_type))
                else:
                    instruments.append((mf.NSE, sid, sub_type))
            return instruments
        except ImportError:
            log.error("[Feed] dhanhq not installed - pip install dhanhq")
            return []

    def _feed_thread(self, instruments: list) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._feed_loop(instruments, loop))
        except Exception as e:
            log.error(f"[Feed] thread error: {e}")
        finally:
            loop.close()

    async def _feed_loop(self, instruments: list, loop) -> None:
        from dhanhq import marketfeed as mf

        backoff = 2
        while self._running:
            feed = None
            try:
                feed = mf.DhanFeed(
                    _sec.get_client_id() or config.DHAN_CLIENT_ID,
                    _sec.get_access_token() or config.DHAN_ACCESS_TOKEN,
                    instruments,
                )
                feed.loop = loop
                await feed.connect()
                self._connected = True
                backoff = 2
                log.info("[Feed] WebSocket connected")

                while self._running:
                    data = await feed.get_instrument_data()
                    if data:
                        self._update(data)

            except Exception as e:
                self._connected = False
                if self._running:
                    log.warning(f"[Feed] disconnected ({e}), reconnect in {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    def _update(self, data: dict) -> None:
        sec_id = str(data.get("security_id", ""))
        if not sec_id:
            return
        with self._lock:
            if "LTP" in data:
                try:
                    self._prices[sec_id] = float(data["LTP"])
                except (ValueError, TypeError):
                    pass
            if "volume" in data:
                try:
                    self._volumes[sec_id] = int(data["volume"])
                except (ValueError, TypeError):
                    pass
            if "total_buy_quantity" in data:
                try:
                    self._buy_qty[sec_id]  = int(data["total_buy_quantity"])
                    self._sell_qty[sec_id] = int(data["total_sell_quantity"])
                except (ValueError, TypeError):
                    pass
