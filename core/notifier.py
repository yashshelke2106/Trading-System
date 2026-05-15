"""
TelegramNotifier — sends trade alerts to Telegram + writes logs/notifications.json
for the Streamlit dashboard to display.

Setup:
  1. Message @BotFather on Telegram → /newbot → copy token
  2. Message @userinfobot on Telegram → copy your chat ID
  3. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in config.py
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Optional

import requests

import config

log = logging.getLogger(__name__)

LOG_DIR      = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
NOTIF_FILE   = os.path.join(LOG_DIR, "notifications.json")
MAX_NOTIFS   = 100
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """
    Thread-safe notifier.
    - notify() sends Telegram message AND appends to notifications.json
    - If token not configured, skips Telegram silently but still writes file
    """

    def __init__(self):
        token    = getattr(config, "TELEGRAM_BOT_TOKEN", "")
        chat_id  = getattr(config, "TELEGRAM_CHAT_ID", "")
        self.token   = token
        self.chat_id = chat_id
        self.enabled = (
            bool(token)
            and token != "your_telegram_bot_token"
            and bool(chat_id)
            and chat_id != "your_telegram_chat_id"
        )
        self._lock = threading.Lock()
        os.makedirs(LOG_DIR, exist_ok=True)
        if self.enabled:
            log.info(f"[Notifier] Telegram enabled -> chat {chat_id}")
        else:
            log.info("[Notifier] Telegram not configured - file-only mode")

    # ── Public ────────────────────────────────────────────────────────────────

    def notify(
        self,
        event_type: str,
        message: str,
        symbol: str = "",
        pnl: float = 0.0,
    ) -> None:
        """Save to file + send Telegram (if configured). Non-blocking."""
        self._save(event_type, message, symbol, pnl)
        if self.enabled:
            threading.Thread(
                target=self._send,
                args=(message,),
                daemon=True,
            ).start()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _send(self, message: str) -> None:
        try:
            url  = TELEGRAM_API.format(token=self.token)
            resp = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"},
                timeout=8,
            )
            if resp.status_code != 200:
                log.warning(f"[Notifier] Telegram HTTP {resp.status_code}: {resp.text[:120]}")
        except Exception as e:
            log.debug(f"[Notifier] Telegram send error: {e}")

    def _save(self, event_type: str, message: str, symbol: str, pnl: float) -> None:
        try:
            with self._lock:
                notifs = []
                if os.path.exists(NOTIF_FILE):
                    try:
                        with open(NOTIF_FILE) as f:
                            notifs = json.load(f)
                    except Exception:
                        notifs = []

                notifs.append({
                    "ts":      datetime.now().isoformat(),
                    "type":    event_type,
                    "symbol":  symbol,
                    "pnl":     pnl,
                    "message": message,
                })
                notifs = notifs[-MAX_NOTIFS:]

                with open(NOTIF_FILE, "w") as f:
                    json.dump(notifs, f, indent=2, default=str)
        except Exception as e:
            log.debug(f"[Notifier] file save error: {e}")
