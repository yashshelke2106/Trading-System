"""
Sentinel flag reader - the ONLY channel through which agentic AI touches the
live path, and it is one-directional: an agent can HALT new entries, nothing
else.

Fail-safe rules (enforced here, not in the agent):
  - file missing            -> not halted (normal ops)
  - file stale (>24h)       -> not halted (a dead agent must not wedge the desk)
  - posture not recognized  -> not halted
  - halt applies to NEW entries only; exits/stops always run.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENTINEL_FILE = os.path.join(_ROOT, "logs", "risk_sentinel.json")
_STALE_H = 24


def entries_halted() -> Tuple[bool, str]:
    """(halted, reason). See fail-safe rules in module docstring."""
    if not os.path.exists(SENTINEL_FILE):
        return False, ""
    try:
        d = json.load(open(SENTINEL_FILE, encoding="utf-8"))
        ts = datetime.fromisoformat(d.get("ts", "1970-01-01T00:00:00"))
        if datetime.now() - ts > timedelta(hours=_STALE_H):
            return False, ""
        if d.get("posture") == "halt_new_entries":
            return True, d.get("reason", "sentinel halt")[:160]
    except Exception:
        pass
    return False, ""
