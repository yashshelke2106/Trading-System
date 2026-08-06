"""
Dhan scrip master loader — lazy security_id resolution.

Source CSV: https://images.dhan.co/api-data/api-scrip-master.csv  (~30 MB)

Strategy:
  1. Cache CSV on disk at logs/scrip_master.csv (TTL 24h).
  2. Build in-memory dicts on first access:
       eq_by_symbol  : {symbol_clean -> security_id}     # NSE EQUITY
       idx_by_symbol : {symbol -> security_id}            # INDEX rows
  3. lookup(sym) checks idx_by_symbol then eq_by_symbol.

Symbol cleaning: strip trailing "-EQ", "-BE", "-BL", etc.; uppercase.
For F&O underlyings we use the equity security ID — Dhan's
/charts/intraday and /quotes endpoints accept that for spot data.
"""

from __future__ import annotations

import csv
import logging
import os
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_PROJECT_ROOT, "logs")
_CACHE_PATH = os.path.join(_CACHE_DIR, "scrip_master.csv")
_TTL_SECONDS = 24 * 3600  # 1 day
_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

_SUFFIX_STRIP = ("-EQ", "-BE", "-BL", "-BZ", "-BT", "-IL", "-RR", "-SM", "-ST")

# In-memory caches built on first call
_eq_by_symbol: Optional[Dict[str, str]] = None
_idx_by_symbol: Optional[Dict[str, str]] = None
# Option index: key = (UNDERLYING, expiry_YYYYMMDD, strike, "CE"|"PE")
_opt_by_key: Optional[Dict[tuple, str]] = None
# F&O lot size per underlying (LIVE source of truth; config.NSE_LOT_SIZES is a
# static fallback that goes stale — NSE revises lot sizes periodically).
_lot_by_symbol: Optional[Dict[str, int]] = None
_loaded_at: float = 0.0


def _clean_symbol(raw: str) -> str:
    s = raw.strip().upper()
    for suf in _SUFFIX_STRIP:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s


def _need_refresh() -> bool:
    if not os.path.exists(_CACHE_PATH):
        return True
    age = time.time() - os.path.getmtime(_CACHE_PATH)
    return age > _TTL_SECONDS


def _download() -> bool:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    try:
        logger.info("Downloading Dhan scrip master…")
        resp = requests.get(_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            logger.warning(f"scrip master HTTP {resp.status_code}")
            return False
        with open(_CACHE_PATH, "wb") as f:
            f.write(resp.content)
        logger.info(f"scrip master saved → {_CACHE_PATH} ({len(resp.content)/1e6:.1f} MB)")
        return True
    except Exception as e:
        logger.warning(f"scrip master download failed: {e}")
        return False


def _build_indexes() -> None:
    """Parse the CSV into eq_by_symbol + idx_by_symbol + opt_by_key dicts."""
    global _eq_by_symbol, _idx_by_symbol, _opt_by_key, _lot_by_symbol, _loaded_at

    if _need_refresh():
        _download()
    if not os.path.exists(_CACHE_PATH):
        _eq_by_symbol = {}
        _idx_by_symbol = {}
        _opt_by_key = {}
        _lot_by_symbol = {}
        _loaded_at = time.time()
        return

    eq: Dict[str, str] = {}
    idx: Dict[str, str] = {}
    opt: Dict[tuple, str] = {}
    lot: Dict[str, int] = {}

    try:
        with open(_CACHE_PATH, "r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                exch = (row.get("SEM_EXM_EXCH_ID") or "").upper()
                inst = (row.get("SEM_INSTRUMENT_NAME") or "").upper()
                sym  = row.get("SEM_TRADING_SYMBOL") or ""
                sid  = row.get("SEM_SMST_SECURITY_ID") or ""

                if not sym or not sid:
                    continue
                if exch not in ("NSE", "BSE"):
                    continue

                # LIVE F&O lot size per underlying (from any derivative row).
                if inst in ("OPTSTK", "OPTIDX", "FUTSTK", "FUTIDX"):
                    und = sym.split("-")[0].upper() if "-" in sym else _clean_symbol(sym)
                    lot_raw = (row.get("SEM_LOT_UNITS") or row.get("SEM_LOT_SIZE")
                               or row.get("LOT_SIZE") or "")
                    try:
                        lv = int(float(lot_raw))
                        if und and lv > 0:
                            lot.setdefault(und, lv)
                    except Exception:
                        pass

                if inst == "EQUITY" and exch == "NSE":
                    clean = _clean_symbol(sym)
                    eq.setdefault(clean, sid)
                elif inst == "INDEX" and exch == "NSE":
                    idx.setdefault(_clean_symbol(sym), sid)
                elif inst in ("OPTSTK", "OPTIDX"):
                    # Option row — index by (underlying, expiry, strike, ce/pe)
                    # SEM_TRADING_SYMBOL format: "RELIANCE-Jun2026-1410-PE"
                    underlying_name = (row.get("SM_SYMBOL_NAME") or "").upper().replace("OPT", "")
                    strike = row.get("SEM_STRIKE_PRICE") or "0"
                    ce_pe = (row.get("SEM_OPTION_TYPE") or "").upper()
                    expiry_raw = row.get("SEM_EXPIRY_DATE") or ""
                    # "2026-06-25 15:30:00" -> "20260625"
                    expiry_iso = expiry_raw.split()[0] if expiry_raw else ""
                    expiry_compact = expiry_iso.replace("-", "")
                    if not all([underlying_name, expiry_compact, ce_pe in ("CE", "PE")]):
                        continue
                    try:
                        strike_f = float(strike)
                    except Exception:
                        continue
                    # Extract clean underlying from trading symbol if SM_SYMBOL_NAME unreliable
                    # e.g. "RELIANCE-Jun2026-1410-PE" -> "RELIANCE"
                    if "-" in sym:
                        ts_under = sym.split("-")[0].upper()
                        if ts_under:
                            underlying_name = ts_under
                    key = (underlying_name, expiry_compact, strike_f, ce_pe)
                    opt.setdefault(key, sid)
    except Exception as e:
        logger.warning(f"scrip master parse failed: {e}")

    _eq_by_symbol = eq
    _idx_by_symbol = idx
    _opt_by_key = opt
    _lot_by_symbol = lot
    _loaded_at = time.time()
    logger.info(f"scrip master indexed: {len(eq)} EQUITY, {len(idx)} INDEX, "
                f"{len(opt)} OPTIONS, {len(lot)} F&O lot sizes")


def _ensure_loaded() -> None:
    if (_eq_by_symbol is None or _idx_by_symbol is None
            or _opt_by_key is None or _lot_by_symbol is None):
        _build_indexes()


def lookup_option(underlying: str, expiry_iso: str, strike: float,
                  ce_pe: str) -> Optional[str]:
    """Return security_id for a specific option contract.

    underlying: 'RELIANCE'
    expiry_iso: '2026-06-25' (or '20260625', or '2026-06-25 15:30:00')
    strike: 1410.0
    ce_pe: 'CE' or 'PE'
    """
    _ensure_loaded()
    if not _opt_by_key:
        return None
    expiry_compact = expiry_iso.split()[0].replace("-", "") if expiry_iso else ""
    key = (underlying.upper(), expiry_compact, float(strike), ce_pe.upper())
    sid = _opt_by_key.get(key)
    if sid:
        return sid
    # Try fuzzy strike match (handles 1410.0 vs 1410.00000 type rounding)
    target_strike = float(strike)
    for k, v in _opt_by_key.items():
        if (k[0] == underlying.upper() and k[1] == expiry_compact
                and k[3] == ce_pe.upper()
                and abs(k[2] - target_strike) < 0.01):
            return v
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

# Corporate actions rename the NSE trading symbol while the universe list and
# our history archive still carry the old name. Without these, lookup() misses
# and get_security_id() falls back to passing the SYMBOL as securityId, which
# Dhan rejects with HTTP 400 on every /charts request for that name.
# Verified against the cached scrip master (SM_SYMBOL_NAME in the comment).
_RENAMES = {
    "TATAMOTORS": "TMCV",       # TATA MOTORS LIMITED (demerger: TMCV cv / TMPV pv)
    "MCDOWELL-N": "UNITDSPR",   # UNITED SPIRITS LIMITED
    "IDFC":       "IDFCFIRSTB", # IDFC Ltd merged into IDFC FIRST Bank (successor)
    "HPCL":       "HINDPETRO",  # HINDUSTAN PETROLEUM CORP (NSE symbol differs)
}
# Requested by callers but absent from Dhan's scrip master AND yfinance, i.e.
# genuinely delisted/merged with no successor row to point at. Kept as data,
# not code, so the universe can be pruned deliberately rather than silently.
DELISTED_NO_SUCCESSOR = ("LTIM", "GUJGASLTD")


def lookup(symbol: str) -> Optional[str]:
    """Return Dhan security_id (string) for symbol, or None on miss.

    Order: index map → equity map → renamed-symbol retry.
    """
    if not symbol:
        return None
    _ensure_loaded()
    s = _clean_symbol(symbol)
    if _idx_by_symbol and s in _idx_by_symbol:
        return _idx_by_symbol[s]
    if _eq_by_symbol and s in _eq_by_symbol:
        return _eq_by_symbol[s]
    renamed = _RENAMES.get(s)
    if renamed and _eq_by_symbol and renamed in _eq_by_symbol:
        return _eq_by_symbol[renamed]
    return None


def lot_size(symbol: str) -> Optional[int]:
    """LIVE F&O lot size for an underlying from the Dhan scrip master, or None.
    Preferred over the static config.NSE_LOT_SIZES, which goes stale whenever
    NSE revises lot sizes. Callers should fall back to the static map then 1."""
    if not symbol:
        return None
    _ensure_loaded()
    if not _lot_by_symbol:
        return None
    return _lot_by_symbol.get(_clean_symbol(symbol))


def force_refresh() -> bool:
    """Re-download CSV, rebuild indexes. Returns True on success."""
    global _eq_by_symbol, _idx_by_symbol, _opt_by_key, _lot_by_symbol
    ok = _download()
    _eq_by_symbol = None
    _idx_by_symbol = None
    _opt_by_key = None
    _lot_by_symbol = None
    if ok:
        _build_indexes()
    return ok


def stats() -> dict:
    _ensure_loaded()
    return {
        "equity_count": len(_eq_by_symbol or {}),
        "index_count":  len(_idx_by_symbol or {}),
        "cache_path":   _CACHE_PATH,
        "cache_age_h":  round((time.time() - os.path.getmtime(_CACHE_PATH)) / 3600, 1)
                        if os.path.exists(_CACHE_PATH) else None,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("Stats:", stats())
    for sym in ["RELIANCE", "WIPRO", "M&M", "BAJAJ-AUTO", "MCDOWELL-N",
                "NIFTY", "BANKNIFTY", "BALKRISIND", "DEEPAKNT"]:
        print(f"  {sym:12} -> {lookup(sym)}")
