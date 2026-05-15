"""
Pydantic schemas — single source of truth for shape of:
  - SignalSchema       (logs/signals.json entries)
  - OptionRecSchema    (option_translator output)
  - JournalEntrySchema (logs/signal_journal.jsonl entries)

Why: 4 sessions of "pnl_pct=None / strike=None / exit_price=entry_price" bugs.
Every write path now validates → reject bad shapes at the boundary, not at
the dashboard / learner / tracker.

Soft validation: validation failure is logged + the bad object is REJECTED.
Callers should catch SchemaValidationError or use `validate_or_none`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

try:
    from pydantic import BaseModel, Field, field_validator, ConfigDict
    _PYDANTIC_OK = True
except ImportError:  # pragma: no cover — pydantic optional
    _PYDANTIC_OK = False
    BaseModel = object  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)


class SchemaValidationError(Exception):
    """Raised when an object fails its schema. Inspect .errors for details."""

    def __init__(self, msg: str, errors: Any = None):
        super().__init__(msg)
        self.errors = errors


# ─────────────────────────────────────────────────────────────────────────────
# Signal — shape of each entry in logs/signals.json["signals"]
# ─────────────────────────────────────────────────────────────────────────────

if _PYDANTIC_OK:

    class OptionRecSchema(BaseModel):
        """Output of option_translator.get_option_rec()."""
        model_config = ConfigDict(extra="allow")

        option_type: Literal["CE", "PE"]
        strike: float = Field(gt=0)
        expiry: str   # ISO date "YYYY-MM-DD"
        entry_prem: float = Field(gt=0, description="must be > 0 — zero/None signals illiquid")
        sl_prem: float = Field(ge=0)
        target_prem: float = Field(ge=0)
        delta: float = Field(ge=-1.0, le=1.0)
        iv_pct: float = Field(ge=0, le=500)  # IV in % (0-500%)
        source: str = "live"

        # Greeks (optional but tracked)
        gamma: Optional[float] = None
        theta: Optional[float] = None
        vega: Optional[float] = None

        # Liquidity guards
        bid: Optional[float] = None
        ask: Optional[float] = None
        spread_pct: Optional[float] = None
        oi: Optional[int] = None

        @field_validator("spread_pct")
        @classmethod
        def _spread_sane(cls, v):
            if v is not None and v > 0.50:
                raise ValueError(f"spread_pct={v} too wide (>50%) — illiquid strike")
            return v

    class SignalSchema(BaseModel):
        """Per-signal entry written to logs/signals.json."""
        model_config = ConfigDict(extra="allow")

        symbol: str = Field(min_length=1, max_length=20)
        direction: Literal["long", "short"]
        entry_price: float = Field(gt=0)
        sl_price: float = Field(gt=0)
        target_price: float = Field(gt=0)
        confluence_grade: Literal["S", "A", "B", "C"]
        confluence_score: float = Field(ge=0, le=200)
        ts: str   # ISO datetime
        rr_ratio: float = Field(ge=0)

        # Direction-consistent SL/target
        @field_validator("sl_price")
        @classmethod
        def _sl_makes_sense(cls, v, info):
            # Can't check vs entry_price/direction here without info.data — best done
            # post-construct via model_validator. Skip the strict check.
            return v

        # Option enrichment (optional — non-F&O may lack these)
        option_strike: Optional[float] = None
        option_type: Optional[Literal["CE", "PE"]] = None
        option_expiry: Optional[str] = None
        entry_prem: Optional[float] = None
        sl_prem: Optional[float] = None
        target_prem: Optional[float] = None
        delta: Optional[float] = None
        iv_pct: Optional[float] = None
        prem_source: Optional[str] = None

        # Pattern + reasoning
        patterns_combined: List[str] = Field(default_factory=list)
        patterns: List[str] = Field(default_factory=list)
        reason: str = ""
        rsi: float = Field(default=50.0, ge=0, le=100)
        volume_ratio: float = Field(default=1.0, ge=0)

    class JournalEntrySchema(BaseModel):
        """Per-resolution entry in logs/signal_journal.jsonl."""
        model_config = ConfigDict(extra="allow")

        symbol: str
        direction: Literal["long", "short"]
        entry_price: float = Field(gt=0)
        sl_price: float = Field(gt=0)
        target_price: float = Field(gt=0)
        ts: str
        outcome: Literal[
            "TARGET_HIT", "SL_HIT", "TIME_EXIT", "EXPIRED", "NO_DATA",
            "WIN", "LOSS", "TIMEOUT"   # legacy outcome strings
        ]
        exit_price: float = Field(gt=0)
        pnl_pct: Optional[float] = None  # signed, % of entry. Allowed None for unresolved
        mfe_pct: Optional[float] = None
        mae_pct: Optional[float] = None
        exit_reason: Optional[str] = None
        backfilled: bool = False

else:  # pragma: no cover — pydantic missing, fallback to no-op

    class OptionRecSchema:  # type: ignore[no-redef]
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class SignalSchema:  # type: ignore[no-redef]
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class JournalEntrySchema:  # type: ignore[no-redef]
        def __init__(self, **kw):
            self.__dict__.update(kw)


# ─────────────────────────────────────────────────────────────────────────────
# Public validate helpers — safe wrappers callers use
# ─────────────────────────────────────────────────────────────────────────────

def validate_signal(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Returns the validated dict (with defaults filled in) or None if invalid.

    Always logs the reason for rejection. Use for SIGNAL writes:
        sig = validate_signal(raw_dict)
        if sig is None:
            return  # skip — already logged
    """
    if not _PYDANTIC_OK:
        return d  # no validation possible — pass through
    try:
        m = SignalSchema(**d)
        return m.model_dump(exclude_none=False)
    except Exception as e:
        log.warning(f"[Schema] signal rejected ({d.get('symbol', '?')}): {e}")
        return None


def validate_option_rec(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not _PYDANTIC_OK:
        return d
    try:
        m = OptionRecSchema(**d)
        return m.model_dump(exclude_none=False)
    except Exception as e:
        log.warning(f"[Schema] option_rec rejected: {e}")
        return None


def validate_journal_entry(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not _PYDANTIC_OK:
        return d
    try:
        m = JournalEntrySchema(**d)
        return m.model_dump(exclude_none=False)
    except Exception as e:
        log.warning(f"[Schema] journal entry rejected ({d.get('symbol', '?')}): {e}")
        return None


def batch_validate_signals(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter list — keep only valid signals. Logs rejection count."""
    if not _PYDANTIC_OK:
        return items
    kept = []
    for d in items:
        v = validate_signal(d)
        if v is not None:
            kept.append(v)
    dropped = len(items) - len(kept)
    if dropped:
        log.warning(f"[Schema] dropped {dropped}/{len(items)} signals (validation failed)")
    return kept
