"""
NSE Option Chain — free real-time fallback when Dhan Data API unavailable.

Scrapes NSE's public option chain API directly (no auth required, but needs
cookie handshake). Returns same schema as Dhan: {strike, ce_ltp, ce_oi, ce_iv,
pe_ltp, pe_oi, pe_iv}.

Cache TTL: 5s (option premiums move tick-by-tick during market).
Rate limit: stagger calls 0.5s apart to avoid NSE throttle.
"""

import json
import logging
import os
import time
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)

_BASE = "https://www.nseindia.com"
_OC_URL = f"{_BASE}/api/option-chain-equities"
_OC_INDEX_URL = f"{_BASE}/api/option-chain-indices"

# Session with NSE-required cookie handshake
_session: Optional[requests.Session] = None
_last_call = [0.0]
_RATE_LIMIT = 0.5  # 500ms between requests

_INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}


def _get_session() -> requests.Session:
    """Initialize NSE session with browser-like fingerprint to bypass bot detection."""
    global _session
    if _session is not None:
        return _session

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://www.nseindia.com/option-chain",
    })
    # SSL bypass: always on Windows (NSE cert chain fails with default Python certs),
    # or when explicitly set via env var.
    import platform
    ssl_bypass = (
        platform.system() == "Windows" or
        os.environ.get("DHAN_SSL_BYPASS", "").lower() in ("1", "true", "yes")
    )
    if ssl_bypass:
        s.verify = False
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        # NSE requires this exact sequence to seed cookies:
        # 1. Visit home (sets initial cookies)
        # 2. Visit option-chain page (sets app-specific cookies)
        s.get("https://www.nseindia.com/", timeout=20)
        time.sleep(1)
        s.get("https://www.nseindia.com/option-chain", timeout=20)
        time.sleep(0.5)
        _session = s
    except Exception as e:
        log.warning(f"NSE session init failed: {e}")
        _session = s
    return _session


def _throttle():
    elapsed = time.time() - _last_call[0]
    if elapsed < _RATE_LIMIT:
        time.sleep(_RATE_LIMIT - elapsed)
    _last_call[0] = time.time()


def fetch_option_chain(symbol: str) -> List[Dict]:
    """
    Fetch live option chain from NSE public API.

    Returns list of dicts with same schema as Dhan get_option_chain():
      [{strike, ce_ltp, ce_oi, ce_iv, pe_ltp, pe_oi, pe_iv, _spot, _expiry}, ...]

    Empty list on failure.
    """
    symbol = symbol.upper().replace(" ", "")

    _throttle()
    session = _get_session()

    try:
        if symbol in _INDEX_SYMBOLS:
            url = _OC_INDEX_URL
        else:
            url = _OC_URL

        resp = session.get(url, params={"symbol": symbol}, timeout=20)
        if resp.status_code != 200:
            log.debug(f"NSE OC {symbol}: HTTP {resp.status_code}")
            # Cookie may have expired — reset session and retry once
            global _session
            _session = None
            session = _get_session()
            resp = session.get(url, params={"symbol": symbol}, timeout=20)
            if resp.status_code != 200:
                return []

        data = resp.json()
        records = data.get("records", {})
        all_strikes = records.get("data", [])
        underlying = float(records.get("underlyingValue", 0))
        expiry_dates = records.get("expiryDates", [])
        nearest_expiry = expiry_dates[0] if expiry_dates else ""

        # Swing roll-guard: a 10-day hold on an option expiring in 3 days
        # is a theta bonfire. If the nearest expiry is closer than the
        # mode's min_days_to_expiry, roll to the first expiry that gives
        # enough time value. Intraday mode keeps nearest (min=2).
        try:
            from core.trade_mode import get_mode
            from datetime import datetime as _dt
            min_days = int(get_mode().min_days_to_expiry)
            today = _dt.now().date()
            for exp in expiry_dates:
                try:
                    iso = _nse_expiry_to_iso(exp)
                    d = _dt.fromisoformat(iso).date()
                except Exception:
                    continue
                if (d - today).days >= min_days:
                    nearest_expiry = exp
                    break
        except Exception:
            pass

        # Filter to selected expiry only
        nearest = [s for s in all_strikes if s.get("expiryDate") == nearest_expiry]

        rows: List[Dict] = []
        for entry in nearest:
            strike = float(entry.get("strikePrice", 0))
            ce = entry.get("CE", {}) or {}
            pe = entry.get("PE", {}) or {}
            rows.append({
                "strike":  strike,
                "ce_ltp":  float(ce.get("lastPrice", 0) or 0),
                "ce_oi":   int(ce.get("openInterest", 0) or 0),
                "ce_iv":   float(ce.get("impliedVolatility", 0) or 0),
                "ce_bid":  float(ce.get("bidprice", 0) or 0),
                "ce_ask":  float(ce.get("askPrice", 0) or 0),
                "pe_ltp":  float(pe.get("lastPrice", 0) or 0),
                "pe_oi":   int(pe.get("openInterest", 0) or 0),
                "pe_iv":   float(pe.get("impliedVolatility", 0) or 0),
                "pe_bid":  float(pe.get("bidprice", 0) or 0),
                "pe_ask":  float(pe.get("askPrice", 0) or 0),
                "_expiry": _nse_expiry_to_iso(nearest_expiry),
                "_spot":   underlying,
            })

        rows.sort(key=lambda r: r["strike"])
        return rows
    except Exception as e:
        log.warning(f"NSE OC {symbol} failed: {e}")
        return []


def _nse_expiry_to_iso(expiry_str: str) -> str:
    """Convert '07-May-2026' → '2026-05-07'."""
    try:
        from datetime import datetime
        dt = datetime.strptime(expiry_str, "%d-%b-%Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return expiry_str


# ── Live NSE F&O stock list ────────────────────────────────────────────────────
_fno_cache: Optional[List[str]] = None
_fno_cache_ts: float = 0.0
_FNO_CACHE_TTL = 86400  # 24h — F&O list changes only on exchange circulars


def fetch_nse_fno_list() -> List[str]:
    """Fetch current F&O-eligible stocks from NSE public API.

    Returns list of NSE symbols (e.g. ['RELIANCE', 'TCS', ...]).
    Cached for 24h. Returns empty list on failure (caller keeps hardcoded fallback).
    """
    global _fno_cache, _fno_cache_ts

    if _fno_cache is not None and (time.time() - _fno_cache_ts) < _FNO_CACHE_TTL:
        return _fno_cache

    session = _get_session()
    _throttle()

    try:
        # NSE public API: all securities in F&O segment
        url = f"{_BASE}/api/equity-stockIndices"
        resp = session.get(url, params={"index": "SECURITIES IN F&O"},
                          timeout=20)
        if resp.status_code != 200:
            log.warning(f"NSE F&O list: HTTP {resp.status_code}")
            return _fno_cache or []

        data = resp.json()
        stocks = data.get("data", [])
        symbols = []
        for s in stocks:
            sym = s.get("symbol", "").strip()
            if sym and sym != "NIFTY 50":
                symbols.append(sym)

        if symbols:
            _fno_cache = sorted(set(symbols))
            _fno_cache_ts = time.time()
            log.info(f"NSE F&O list: {len(_fno_cache)} stocks fetched")
        return _fno_cache or []

    except Exception as e:
        log.warning(f"NSE F&O list fetch failed: {e}")
        return _fno_cache or []


# ─────────────────────────────────────────────────────────────────────────────
# Authoritative NSE F&O equity list (NSE FNO segment, ~190 stocks).
# NSE adds/removes ~quarterly — review every few months. This is the ground
# truth when the live NSE API is bot-blocked (which is most of the time).
# Source: NSE F&O securities list. Index symbols handled separately.
# ─────────────────────────────────────────────────────────────────────────────
NSE_FNO_STATIC = frozenset({
    "AARTIIND", "ABB", "ABBOTINDIA", "ABCAPITAL", "ABFRL", "ACC", "ADANIENT",
    "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "ALKEM", "AMBUJACEM", "ANGELONE",
    "APLAPOLLO", "APOLLOHOSP", "APOLLOTYRE", "ASHOKLEY", "ASIANPAINT", "ASTRAL",
    "ATGL", "ATUL", "AUBANK", "AUROPHARMA", "AXISBANK", "BAJAJ-AUTO",
    "BAJAJFINSV", "BAJFINANCE", "BALKRISIND", "BANDHANBNK", "BANKBARODA",
    "BANKINDIA", "BATAINDIA", "BEL", "BERGEPAINT", "BHARATFORG", "BHARTIARTL",
    "BHEL", "BIOCON", "BOSCHLTD", "BPCL", "BRITANNIA", "BSE", "BSOFT",
    "CAMS", "CANBK", "CDSL", "CEAT", "CESC", "CGPOWER", "CHAMBLFERT",
    "CHOLAFIN", "CIPLA", "COALINDIA", "COFORGE", "COLPAL", "CONCOR",
    "COROMANDEL",
    "CROMPTON", "CUMMINSIND", "CYIENT", "DABUR", "DALBHARAT", "DEEPAKNTR",
    "DELHIVERY", "DIVISLAB", "DIXON", "DLF", "DMART", "DRREDDY", "EICHERMOT",
    "ESCORTS", "ETERNAL", "EXIDEIND", "FEDERALBNK", "GAIL", "GLENMARK",
    "GMRAIRPORT", "GODREJCP", "GODREJPROP", "GRANULES", "GRASIM", "GUJGASLTD",
    "HAL", "HAVELLS", "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HFCL", "HINDALCO", "HINDCOPPER", "HINDPETRO", "HINDUNILVR",
    "HUDCO", "ICICIBANK", "ICICIGI", "ICICIPRULI", "IDEA", "IDFCFIRSTB",
    "IEX", "IGL", "IIFL", "INDHOTEL", "INDIANB", "INDIGO", "INDUSINDBK",
    "INDUSTOWER", "INFY", "INOXWIND", "IOC", "IPCALAB", "IRB", "IRCTC",
    "IREDA", "IRFC", "ITC", "JINDALSTEL", "JIOFIN", "JSL", "JSWENERGY",
    "JSWSTEEL", "JUBLFOOD", "KALYANKJIL", "KEI", "KOTAKBANK", "KPITTECH",
    "LALPATHLAB", "LAURUSLABS", "LICHSGFIN", "LICI", "LODHA", "LT", "LTF",
    "LTIM", "LUPIN", "M&M", "M&MFIN", "MANAPPURAM", "MARICO", "MARUTI",
    "MAXHEALTH", "MCX", "METROPOLIS", "MFSL", "MGL", "MOTHERSON", "MPHASIS",
    "MRF", "MUTHOOTFIN", "NATIONALUM", "NAUKRI", "NAVINFLUOR", "NBCC", "NCC",
    "NESTLEIND", "NHPC", "NMDC", "NTPC", "NYKAA", "OBEROIRLTY", "OFSS",
    "OIL", "OLECTRA", "ONGC", "PAGEIND", "PATANJALI", "PAYTM", "PEL",
    "PERSISTENT", "PETRONET", "PFC", "PHOENIXLTD", "PIDILITIND", "PIIND",
    "PNB", "PNBHOUSING", "POLYCAB", "POONAWALLA", "POWERGRID", "PRESTIGE",
    "PVRINOX", "RAMCOCEM", "RBLBANK",
    "RECLTD", "RELIANCE", "RVNL", "SAIL", "SBICARD", "SBILIFE", "SBIN",
    "SHREECEM", "SHRIRAMFIN", "SIEMENS", "SJVN", "SOLARINDS", "SONACOMS",
    "SRF", "SUNPHARMA", "SUPREMEIND", "SUZLON", "SYNGENE", "TATACHEM",
    "TATACOMM", "TATACONSUM", "TATAELXSI", "TATAMOTORS", "TATAPOWER",
    "TATASTEEL", "TATATECH", "TCS", "TECHM", "TIINDIA", "TITAGARH", "TITAN",
    "TORNTPHARM", "TORNTPOWER", "TRENT", "TVSMOTOR", "UBL", "ULTRACEMCO",
    "UNIONBANK", "UNITDSPR", "UPL", "VBL", "VEDL", "VOLTAS", "WIPRO",
    "YESBANK", "ZYDUSLIFE",
})

# Ticker aliases — system symbol → NSE F&O canonical symbol
FNO_SYMBOL_ALIASES = {
    "DEEPAKNT":   "DEEPAKNTR",
    "MCDOWELL-N": "UNITDSPR",   # United Spirits trades as UNITDSPR in F&O
    "ZOMATO":     "ETERNAL",    # Zomato renamed to Eternal
    "CEATLTD":    "CEAT",       # NSE F&O symbol is CEAT
    "NAUKRI":     "NAUKRI",     # Info Edge — kept
}


def is_fno_symbol(symbol: str) -> bool:
    """True if symbol (or its alias) is in the NSE F&O segment.

    Uses live NSE list if available, else the static authoritative set.
    """
    s = (symbol or "").strip().upper()
    s = FNO_SYMBOL_ALIASES.get(s, s)
    live = fetch_nse_fno_list()
    if live:
        return s in set(live)
    return s in NSE_FNO_STATIC


def validate_fno_universe(hardcoded: List[str]) -> List[str]:
    """Filter hardcoded universe against F&O list.

    Priority: live NSE list → static authoritative set (NSE bot-blocks us
    ~always, so the static set is the real gate, NOT a blind passthrough).
    """
    live = fetch_nse_fno_list()
    if live:
        live_set = set(live)
        source = "live NSE"
    else:
        live_set = set(NSE_FNO_STATIC)
        source = "static F&O set"
        log.warning(f"F&O validation: NSE fetch failed, using {source} ({len(live_set)} stocks)")

    def _canon(s: str) -> str:
        return FNO_SYMBOL_ALIASES.get(s.upper(), s.upper())

    valid   = [s for s in hardcoded if _canon(s) in live_set]
    removed = [s for s in hardcoded if _canon(s) not in live_set]

    if removed:
        log.warning(f"F&O validation ({source}): {len(removed)} non-F&O removed: {removed}")

    return valid
