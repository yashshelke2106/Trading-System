import logging
import os
import ssl

# Global SSL bypass — corporate proxy/antivirus intercepts certs.
# Force every requests.Session call + stdlib ssl to skip verify.
# Override with DHAN_SSL_STRICT=1.
if os.environ.get("DHAN_SSL_STRICT", "").lower() not in ("1", "true", "yes"):
    ssl._create_default_https_context = ssl._create_unverified_context
    os.environ["PYTHONHTTPSVERIFY"] = "0"
    os.environ["CURL_CA_BUNDLE"] = ""
    os.environ["REQUESTS_CA_BUNDLE"] = ""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

import requests

# Monkey-patch requests.Session.request to default verify=False globally.
# Affects yfinance + every library using requests.
if os.environ.get("DHAN_SSL_STRICT", "").lower() not in ("1", "true", "yes"):
    _orig_request = requests.Session.request
    def _patched_request(self, method, url, **kwargs):
        kwargs.setdefault("verify", False)
        return _orig_request(self, method, url, **kwargs)
    requests.Session.request = _patched_request  # type: ignore[assignment]
    # Also patch top-level requests.get/post etc which use a fresh session
    _orig_top_request = requests.api.request
    def _patched_top_request(method, url, **kwargs):
        kwargs.setdefault("verify", False)
        return _orig_top_request(method, url, **kwargs)
    requests.api.request = _patched_top_request  # type: ignore[assignment]
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import base64
import pandas as pd
from typing import Optional, List, Dict
from datetime import datetime, timedelta, timezone
import config

log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> datetime:
    return datetime.now(_IST).replace(tzinfo=None)

# Centralized credential resolution — keyring → dhan_token.txt → config fallback.
# Imported defensively so legacy direct imports of TOKEN_FILE/_read_token_file keep working.
try:
    from . import secrets as _sec
except ImportError:  # script-style import
    import core.secrets as _sec  # type: ignore

# Path kept for backward compat with volume_live.py / multi_agent_runner.py.
TOKEN_FILE = _sec.TOKEN_FILE
REQUEST_TIMEOUT = (5, 20)


def _read_token_file() -> str:
    """Resolve current Dhan access token via the secrets module."""
    return _sec.get_access_token() or ""


def _token_expiry(token: str) -> "datetime | None":
    """Decode JWT exp field without a library. Returns None on failure."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=="   # add padding
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        exp = decoded.get("exp")
        return datetime.utcfromtimestamp(exp) if exp else None
    except Exception:
        return None


def check_token_health(token: "str | None" = None) -> None:
    """Print token expiry status. Call at startup."""
    h = _sec.token_health(token)
    print(f"[Token] {h.message}")


_YF_TICKER_MAP = {
    "TATAMOTORS": "TMCV.NS",       # post-2024 demerger commercial vehicle entity
    "MCDOWELL-N": "UNITDSPR.NS",   # United Spirits (NSE: MCDOWELL-N)
    "DEEPAKNTR":  "DEEPAKNTR.NS",  # Deepak Nitrite (NSE: DEEPAKNTR)
    "MOTHERSON":  "MOTHERSON.NS",  # Samvardhana Motherson International
    "PVRINOX":    "PVRINOX.NS",    # PVR-INOX merged entity
    "JIOFIN":     "JIOFIN.NS",     # Jio Financial Services
    "LICI":       "LICI.NS",       # LIC India
    "IRFC":       "IRFC.NS",       # Indian Railway Finance Corp
    "ADANIGREEN": "ADANIGREEN.NS", # Adani Green Energy
    "ADANIPOWER": "ADANIPOWER.NS", # Adani Power
    "INDUSTOWER": "INDUSTOWER.NS", # Indus Towers (formerly Bharti Infratel)
    "CROMPTON":   "CROMPTON.NS",   # Crompton Greaves Consumer
    "OLECTRA":    "OLECTRA.NS",    # Olectra Greentech
    "NYKAA":      "NYKAA.NS",      # FSN E-Commerce (Nykaa)
    "PAYTM":      "PAYTM.NS",      # One97 Communications
    "ETERNAL":    "ETERNAL.NS",    # Eternal Ltd (formerly Zomato — NSE ticker changed 2025)
    "ZOMATO":     "ETERNAL.NS",    # backward-compat alias in case old symbol appears
    "LTIM":       "LTIM.NS",      # LTIMindtree — explicit map suppresses $LTIM.NS format error
    # New sector coverage additions
    "NALCO":      "NATIONALUM.NS",  # National Aluminium Company
    "VEDANTA":    "VEDL.NS",        # Vedanta Ltd (not VED London)
    "KPIT":       "KPITTECH.NS",    # KPIT Technologies
    "BOSCH":      "BOSCHLTD.NS",    # Bosch India
    "WAAREE":     "WAAREEENER.NS",  # Waaree Energies
    "SAMMAAN":    "SAMMAANCAP.NS",  # Sammaan Capital (formerly Indiabulls HF)
    "ICICISEC":   "ISEC.NS",        # ICICI Securities (NSE: ISEC)
}


# ─────────────────────────────────────────────────────────────────────────
# DHAN-ONLY DATA ACCESS
# yfinance was permanently removed (user has a paid Dhan Data API sub). All
# OHLCV — intraday and daily — now comes ONLY from Dhan /charts endpoints.
# These module-level helpers wrap a lazy DhanAPI singleton so non-API modules
# (sector_leader, regime_filter, backtests, etc.) fetch through one path
# instead of importing yfinance directly. Caching kept (Dhan rate-limits too).
# ─────────────────────────────────────────────────────────────────────────
_INTRADAY_CACHE: Dict = {}
_INTRADAY_CACHE_TTL = 60          # seconds (5m bars refresh every 5min)
_DAILY_CACHE: Dict = {}
_DAILY_CACHE_TTL = 3600           # legacy fallback; session-aware expiry below wins


def _daily_cache_expiry_epoch() -> float:
    """Epoch when currently-cached DAILY bars become stale.

    A daily-bars response can only meaningfully change at two moments:
    the session open (~09:20 IST, today's forming bar appears) and just
    after the close (~15:35 IST, the bar completes). Re-fetching 152
    symbols' full history every hour was the #1 source of Dhan 429
    storms — this caps daily refetches at ~2/symbol/day.
    """
    import time as _time
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    boundaries = []
    for h, m in ((9, 20), (15, 35)):
        b = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if b <= now:
            b += timedelta(days=1)
        boundaries.append(b)
    return min(boundaries).timestamp()

_API_SINGLETON = None

# ── Cross-process 429 backoff ────────────────────────────────────────────────
# The class-level backoff attrs are PROCESS-LOCAL. The aladdin runner spawns
# several python processes sharing ONE Dhan key — each tripped its own backoff
# while the others kept hammering, so the "global 30s backoff" never actually
# stopped the storm. This file shares the backoff clock across all processes.
_BACKOFF_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "logs", "dhan_backoff.json")
_backoff_read_cache = {"ts": 0.0, "data": {}}


def _shared_backoff_get(kind: str) -> float:
    """Epoch until which `kind` ('chart'|'oc') is backed off, across processes.
    File read at most once/second per process."""
    import time as _time
    now = _time.time()
    if now - _backoff_read_cache["ts"] > 1.0:
        try:
            import json as _json
            with open(_BACKOFF_FILE, encoding="utf-8") as f:
                _backoff_read_cache["data"] = _json.load(f)
        except Exception:
            _backoff_read_cache["data"] = {}
        _backoff_read_cache["ts"] = now
    try:
        return float(_backoff_read_cache["data"].get(kind, 0.0))
    except Exception:
        return 0.0


def _shared_backoff_set(kind: str, until: float) -> None:
    """Publish a backoff so every process honors it. Best-effort."""
    try:
        import json as _json
        data = {}
        try:
            with open(_BACKOFF_FILE, encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            pass
        if until > float(data.get(kind, 0.0) or 0.0):
            data[kind] = until
            os.makedirs(os.path.dirname(_BACKOFF_FILE), exist_ok=True)
            tmp = _BACKOFF_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(data, f)
            os.replace(tmp, _BACKOFF_FILE)
        _backoff_read_cache["data"][kind] = max(
            until, float(_backoff_read_cache["data"].get(kind, 0.0) or 0.0))
    except Exception:
        pass

# Data-staleness tracking: timestamp of the last NON-EMPTY Dhan fetch. With
# yfinance gone, Dhan is the single source — if it goes silent mid-session we
# must HALT (not trade blind). seconds_since_last_data() lets the scanner gate.
_LAST_OK_FETCH = [0.0]


def _mark_data_ok():
    import time as _t
    _LAST_OK_FETCH[0] = _t.time()


def seconds_since_last_data() -> float:
    """Seconds since the last non-empty Dhan fetch. Large = data stale/dead.
    Returns a big number if nothing has ever been fetched."""
    import time as _t
    if _LAST_OK_FETCH[0] <= 0:
        return 1e9
    return _t.time() - _LAST_OK_FETCH[0]


def _get_singleton():
    """Lazy DhanAPI singleton for module-level data helpers. Built once."""
    global _API_SINGLETON
    if _API_SINGLETON is None:
        _API_SINGLETON = DhanAPI()
    return _API_SINGLETON


def dhan_intraday(symbol: str, interval_min: int = 5, days_back: int = 5) -> pd.DataFrame:
    """Intraday OHLCV from Dhan only. Columns: date/open/high/low/close/volume.
    Empty DataFrame on failure (NO yfinance fallback — by design)."""
    import time as _time
    key = (symbol.upper(), interval_min, days_back)
    cached = _INTRADAY_CACHE.get(key)
    if cached and (_time.time() - cached[0]) < _INTRADAY_CACHE_TTL:
        return cached[1]
    try:
        df = _get_singleton().get_intraday_data(symbol, interval=interval_min, days_back=days_back)
    except Exception as e:
        logging.getLogger(__name__).warning("dhan_intraday %s failed: %s", symbol, e)
        df = pd.DataFrame()
    df = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if not df.empty and "date" in df.columns:
        # Dhan occasionally returns duplicate-date rows; drop them at the source
        # so downstream panel/reindex code can't crash ("duplicate labels").
        df = df.drop_duplicates(subset="date", keep="last")
    if not df.empty:
        _mark_data_ok()
    _INTRADAY_CACHE[key] = (_time.time(), df)
    return df


def dhan_daily(symbol: str, days_back: int = 365) -> pd.DataFrame:
    """Daily OHLCV from Dhan only. Columns: date/open/high/low/close/volume.
    Empty DataFrame on failure (NO yfinance fallback — by design)."""
    import time as _time
    # Yahoo-style index tickers (^NSEI, ^CNXIT, ...) are never valid Dhan
    # symbols — some legacy callers (sector_rotation) still pass them. Skip
    # quietly instead of spamming "no data" warnings; caller degrades neutral.
    if symbol.startswith("^"):
        logging.getLogger(__name__).debug("dhan_daily: skipping yahoo ticker %s", symbol)
        return pd.DataFrame()
    key = (symbol.upper(), days_back)
    cached = _DAILY_CACHE.get(key)
    # Session-aware expiry: entry stores the epoch it becomes stale at
    # (open/close boundary), NOT a fixed TTL. See _daily_cache_expiry_epoch.
    if cached and _time.time() < cached[0]:
        return cached[1]
    try:
        df = _get_singleton().get_historical_data(symbol, from_date=days_back)
    except Exception as e:
        logging.getLogger(__name__).warning("dhan_daily %s failed: %s", symbol, e)
        df = pd.DataFrame()
    df = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if not df.empty and "date" in df.columns:
        # Dhan occasionally returns duplicate-date rows; drop them at the source
        # so downstream panel/reindex code can't crash ("duplicate labels").
        df = df.drop_duplicates(subset="date", keep="last")
    if not df.empty:
        _mark_data_ok()
        # Cache until the next session boundary (open/close). Empty results get
        # only a short retry window so a transient 429 doesn't blank a symbol
        # for the whole session.
        _DAILY_CACHE[key] = (_daily_cache_expiry_epoch(), df)
    else:
        _DAILY_CACHE[key] = (_time.time() + 120, df)
    return df


SECURITY_ID_MAP = {
    # ── Core NIFTY50 / large-cap F&O universe ───────────────────────────────
    "RELIANCE":   "2885",
    "TCS":        "11536",
    "INFY":       "1594",
    "HDFCBANK":   "1333",
    "ICICIBANK":  "4963",
    "SBIN":       "3045",
    "BHARTIARTL": "10604",
    "KOTAKBANK":  "1922",
    "BAJFINANCE": "317",
    "HINDUNILVR": "1394",
    "ITC":        "1660",
    "LT":         "11483",
    "AXISBANK":   "5900",
    "MARUTI":     "10999",
    "ASIANPAINT": "236",
    "WIPRO":      "3787",
    "HCLTECH":    "7229",
    "TITAN":      "3506",
    "SUNPHARMA":  "3351",
    "ADANIENT":   "25",
    "NTPC":       "11630",
    "POWERGRID":  "14977",
    "ULTRACEMCO": "11532",   # was 2952 (stale — wrong chain)
    "JSWSTEEL":   "11723",
    "ONGC":       "2475",
    "COALINDIA":  "20374",
    "BPCL":       "526",
    "HEROMOTOCO": "1348",
    "EICHERMOT":  "910",
    "TATASTEEL":  "3499",
    "HINDALCO":   "1363",
    "GRASIM":     "1232",
    "CIPLA":      "694",
    "DIVISLAB":   "10940",
    "DRREDDY":    "881",
    "BRITANNIA":  "547",
    "NESTLEIND":  "17963",
    "PIDILITIND": "2664",
    "TATACONSUM": "3432",
    "M&M":        "2031",
    "BAJAJ-AUTO": "16669",
    "INDUSINDBK": "5258",
    "TECHM":      "13538",
    "ATUL":       "263",     # was 383 (stale — wrong chain)
    "NAVINFLUOR": "14672",   # was 23650 (MUTHOOTFIN's id — caused chain leak)
    "BALKRISIND": "335",
    "PERSISTENT": "18365",
    # ── Additional liquid stocks in map ─────────────────────────────────────
    "UNIONBANK":  "10753",
    "FEDERALBANK":"1023",
    "IDBIBANK":   "10040",
    "BANKBARODA": "4668",
    # ── Indices (spot) — authoritative IDs from Dhan scrip_master.csv,
    #    segment NSE/I (IDX_I), instrument INDEX. The old 26009/26000/22344
    #    were derivative/feed IDs and returned NO historical data. ──────────
    "NIFTY":      "13",     # Nifty 50
    "BANKNIFTY":  "25",     # Nifty Bank
    "FINNIFTY":   "27",     # Nifty Fin Service
    "MIDCPNIFTY": "442",    # Nifty Midcap Select
    "NIFTYIT":    "29",     # Nifty IT
    "INDIAVIX":   "21",     # India VIX
    # ── Post-corporate-action aliases ────────────────────────────────────────
    "TATAMOTORS": "759782",  # post-2024 demerger TMCV entity
    "MCDOWELL-N": "10447",
    "DEEPAKNTR":  "19943",  # Deepak Nitrite Ltd
}


def get_security_id(symbol: str) -> str:
    """Resolve security_id for a symbol.

    Order:
      1. Static SECURITY_ID_MAP (fast path, hand-curated)
      2. Lazy lookup against Dhan scrip master (downloads CSV once/day)
      3. Fallback: return the symbol unchanged (call will fail at API but
         caller can detect empty response)

    Successful master lookups are memoized back into SECURITY_ID_MAP so
    subsequent calls in the same process skip the master.
    """
    sym = symbol.upper()
    sid = SECURITY_ID_MAP.get(sym)
    if sid:
        return sid
    try:
        from . import scrip_master  # local import — keeps top-level fast
        sid = scrip_master.lookup(sym)
        if sid:
            SECURITY_ID_MAP[sym] = sid
            return sid
    except Exception:
        pass
    return sym


_RECONCILED = [False]


def reconcile_security_ids() -> dict:
    """Validate hardcoded SECURITY_ID_MAP against Dhan scrip master.

    Stale hardcoded IDs cause WRONG option chains (e.g. NAVINFLUOR served
    MUTHOOTFIN's chain because both mapped to 23650). Run once at startup:
    any equity symbol whose hardcoded id != scrip-master id is auto-corrected
    in memory and logged loudly.

    Returns {symbol: (old_id, new_id)} of corrections.
    """
    if _RECONCILED[0]:
        return {}
    _RECONCILED[0] = True
    corrections: dict = {}
    skip = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYIT"}
    try:
        from . import scrip_master
        for s, hid in list(SECURITY_ID_MAP.items()):
            if s in skip:
                continue
            try:
                real = scrip_master.lookup(s)
            except Exception:
                continue
            if real and str(real) != str(hid):
                SECURITY_ID_MAP[s] = str(real)
                corrections[s] = (hid, str(real))
        if corrections:
            log.warning(
                f"[SecID] reconciled {len(corrections)} stale IDs vs scrip master: "
                + ", ".join(f"{s}:{o}->{n}" for s, (o, n) in corrections.items())
            )
        else:
            log.info("[SecID] all hardcoded IDs match scrip master")
    except Exception as e:
        log.warning(f"[SecID] reconcile skipped: {e}")
    return corrections


class DhanAPI:
    _oc_last_call = 0.0       # timestamp of last option-chain API call
    _OC_RATE_LIMIT = 2.5      # min 2.5s between option chain calls (was 1.0 — caused 429 on 153-symbol scans)
    _oc_backoff_until = 0.0   # when this is in future, skip option chain calls entirely

    # /charts/intraday and /charts/historical share Dhan's chart API quota
    # (~5 req/sec). Without throttling, an 80+ symbol scan blows the limit
    # in <2s and urllib3 buries the 429s as connection errors — every fetch
    # comes back empty. Mirrors the option-chain pattern above.
    _chart_last_call = 0.0
    _CHART_RATE_LIMIT = 1.0        # 1 req/sec — Dhan's effective chart limit is
                                   # tighter than docs suggest (4.5/sec triggered
                                   # 429s on half of calls); 1/sec stays clean
    _chart_backoff_until = 0.0     # extended by the 429 handler in _request

    def __init__(self):
        # Prefer keyring/saved value over config to allow runtime client_id swap.
        self.client_id = _sec.get_client_id() or config.DHAN_CLIENT_ID
        self.base_url = "https://api.dhan.co/v2"
        self.session = requests.Session()
        self._configure_session(self.session, allow_retries=False)
        self._apply_token(_read_token_file())
        # Separate session for Data API (may use different token)
        self.data_session = requests.Session()
        self._configure_session(self.data_session, allow_retries=True)
        self._apply_data_token(_sec.get_data_token())

    @staticmethod
    def _configure_session(session: requests.Session, allow_retries: bool) -> None:
        retries = Retry(
            total=2 if allow_retries else 0,
            connect=2 if allow_retries else 0,
            read=2 if allow_retries else 0,
            status=2 if allow_retries else 0,
            backoff_factor=0.5 if allow_retries else 0,
            # 429 deliberately NOT in the list — urllib3 would silently retry
            # and bury the response as a ResponseError, hiding the rate-limit
            # from our _request handler. We pre-throttle via _throttle_chart
            # and handle any leftover 429 explicitly below.
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=None if allow_retries else frozenset({"GET"}),
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=20)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        # SSL: bypass by default (corporate proxy/antivirus intercepts certs).
        # Set DHAN_SSL_STRICT=1 to re-enable verification if needed.
        if os.environ.get("DHAN_SSL_STRICT", "").lower() in ("1", "true", "yes"):
            try:
                import certifi
                session.verify = certifi.where()
            except ImportError:
                pass
        else:
            session.verify = False
            try:
                import urllib3
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:
                pass

    def _apply_token(self, token: str) -> None:
        self.access_token = token
        self.session.headers.update({
            "Content-Type": "application/json",
            "Access-Token": token,
            "client-id": self.client_id,     # Dhan v2 requires dash, not underscore
        })

    def _apply_data_token(self, token: str) -> None:
        self.data_token = token
        headers = {
            "Content-Type": "application/json",
            "Access-Token": token,
            "client-id": self.client_id,     # Dhan v2 requires dash, not underscore
        }
        api_secret = _sec.get_data_api_secret()
        if api_secret:
            headers["api-secret"] = api_secret
        self.data_session.headers.update(headers)

    def _reload_token(self) -> bool:
        """Re-read token. Keyring-aware path first, then direct file fallback.

        If keyring is stale (returns same expired token as we already have),
        read dhan_token.txt directly to bypass the keyring cache.
        """
        fresh = _read_token_file()  # keyring → file → config

        # Keyring stale: returned same expired token. Try file directly.
        if not fresh or fresh == self.access_token:
            try:
                if os.path.exists(TOKEN_FILE):
                    file_tok = open(TOKEN_FILE).read().strip()
                    if file_tok and file_tok != self.access_token:
                        fresh = file_tok
            except Exception:
                pass

        if fresh and fresh != self.access_token:
            self._apply_token(fresh)
            return True
        return False

    def _throttle_chart(self, endpoint: str) -> None:
        """Pace /charts/* calls under Dhan's ~5 req/sec ceiling."""
        if "/charts/" not in endpoint:
            return
        import time as _t
        now = _t.time()
        # Honor BOTH the process-local and the cross-process shared backoff.
        until = max(DhanAPI._chart_backoff_until, _shared_backoff_get("chart"))
        if now < until:
            _t.sleep(until - now)
            now = _t.time()
        elapsed = now - DhanAPI._chart_last_call
        if elapsed < DhanAPI._CHART_RATE_LIMIT:
            _t.sleep(DhanAPI._CHART_RATE_LIMIT - elapsed)
        DhanAPI._chart_last_call = _t.time()

    def _request(self, method: str, endpoint: str, data: dict = None,
                 _retry: bool = True, _use_data_session: bool = False) -> dict:
        self._throttle_chart(endpoint)
        url = f"{self.base_url}{endpoint}"
        sess = self.data_session if _use_data_session else self.session

        try:
            if method == "GET":
                response = sess.get(url, params=data, timeout=REQUEST_TIMEOUT)
            elif method == "POST":
                response = sess.post(url, json=data, timeout=REQUEST_TIMEOUT)
            elif method == "PUT":
                response = sess.put(url, json=data, timeout=REQUEST_TIMEOUT)
            elif method == "DELETE":
                response = sess.delete(url, timeout=REQUEST_TIMEOUT)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            # Handle rate limit: back off and retry once
            if response.status_code == 429 and _retry:
                import time as _t
                wait = float(response.headers.get("Retry-After", 2))
                log.debug(f"Dhan 429 rate limit on {endpoint}, waiting {wait}s")
                # For option chain endpoint, set global 30s backoff so the
                # entire scan loop stops hammering instead of looping retries.
                if "/optionchain" in endpoint:
                    DhanAPI._oc_backoff_until = _t.time() + 30
                    _shared_backoff_set("oc", DhanAPI._oc_backoff_until)
                    log.warning(f"Dhan 429 on option chain — global 30s backoff set")
                if "/charts/" in endpoint:
                    # Honor Retry-After if Dhan sent one, else 30s — short
                    # windows (10s) bounced straight back into 429s in testing
                    chart_wait = max(wait, 30.0)
                    DhanAPI._chart_backoff_until = _t.time() + chart_wait
                    _shared_backoff_set("chart", DhanAPI._chart_backoff_until)
                    log.warning(f"Dhan 429 on charts — global {chart_wait:.0f}s backoff set "
                                f"(shared across processes)")
                _t.sleep(min(wait, 5))
                return self._request(method, endpoint, data, _retry=False,
                                     _use_data_session=_use_data_session)

            if response.status_code == 401 and _retry:
                try:
                    body = response.json()
                except Exception:
                    body = {}
                ec  = body.get("errorCode", "")
                msg = body.get("message", body.get("error", "token expired or wrong credentials"))

                if ec == "DH-902":
                    return {"error": "DH-902: Data API subscription not active", "status": "error"}

                if _use_data_session:
                    # Reload data credentials (new key/secret saved since instance was created)
                    self._apply_data_token(_sec.get_data_token())
                    return self._request(method, endpoint, data, _retry=False,
                                         _use_data_session=True)
                else:
                    changed = self._reload_token()
                    if changed:
                        return self._request(method, endpoint, data, _retry=False,
                                             _use_data_session=False)

                return {"error": f"401 Unauthorized — {msg}", "status": "error"}

            response.raise_for_status()
            return response.json()

        except requests.exceptions.HTTPError as e:
            return {"error": str(e), "status": "error"}
        except requests.exceptions.RequestException as e:
            return {"error": str(e), "status": "error"}

    def test_data_api(self) -> dict:
        """Quick connectivity test for Data API. Returns {ok, message}."""
        self._apply_data_token(_sec.get_data_token())
        result = self._request("POST", "/charts/intraday", {
            "securityId": "2885",
            "exchangeSegment": "NSE_EQ",
            "instrument": "EQUITY",
            "interval": "5",
            "fromDate": __import__("datetime").date.today().strftime("%Y-%m-%d"),
            "toDate":   __import__("datetime").date.today().strftime("%Y-%m-%d"),
        }, _use_data_session=True)
        if "error" in result:
            return {"ok": False, "message": result["error"]}
        return {"ok": True, "message": f"Active — got {len(result.get('open', []))} bars"}

    # Index spot symbols — must use IDX_I segment + INDEX instrument together,
    # else Dhan returns no data (the NIFTY 0-bars bug). Keep both helpers in
    # sync via this one set.
    _INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "NIFTYIT", "FINNIFTY",
                      "MIDCPNIFTY", "INDIAVIX"}

    def _segment_for(self, symbol: str) -> str:
        """Return correct exchangeSegment for index vs equity.
        Dhan v2 index segment is 'IDX_I' (confirmed against the official
        dhanhq SDK constant INDEX='IDX_I'); 'NSE_IDX' was wrong and would
        DH-905 every index chart request."""
        if symbol.upper() in self._INDEX_SYMBOLS:
            return "IDX_I"
        return "NSE_EQ"

    def _instrument_for(self, symbol: str) -> str:
        if symbol.upper() in self._INDEX_SYMBOLS:
            return "INDEX"
        return "EQUITY"

    def _parse_ohlcv(self, result: dict) -> pd.DataFrame:
        if "error" in result:
            log.warning("Dhan OHLCV error body: %s", result.get("error"))
            return pd.DataFrame()
        if not all(k in result for k in ["open", "high", "low", "close", "volume"]):
            keys = list(result.keys()) if isinstance(result, dict) else type(result).__name__
            log.warning("Dhan OHLCV unexpected shape: keys=%s", keys)
            return pd.DataFrame()
        n = len(result["open"])
        return pd.DataFrame({
            "date":   pd.to_datetime([result["timestamp"][i] for i in range(n)], unit="s"),
            "open":   result["open"],
            "high":   result["high"],
            "low":    result["low"],
            "close":  result["close"],
            "volume": result["volume"],
        })

    def get_historical_data(
        self, symbol: str, from_date: int = 30, to_date: str = None
    ) -> pd.DataFrame:
        if to_date is None:
            to_date = datetime.now().strftime("%Y-%m-%d")
        from_dt = (datetime.now() - timedelta(from_date)).strftime("%Y-%m-%d")
        security_id = get_security_id(symbol)
        data = {
            "securityId":      security_id,
            "exchangeSegment": self._segment_for(symbol),
            "instrument":      self._instrument_for(symbol),
            "expiryCode": 0,
            "oi": False,
            "fromDate": from_dt,
            "toDate":   to_date,
        }
        result = self._request("POST", "/charts/historical", data, _use_data_session=True)
        df = self._parse_ohlcv(result)
        if df.empty:
            log.warning("Dhan historical returned no data for %s (no fallback — Dhan-only)", symbol)
        return df

    def get_intraday_data(
        self, symbol: str, interval: int = 5, days_back: int = 1
    ) -> pd.DataFrame:
        """
        Fetch intraday OHLCV bars via Dhan /charts/intraday.
        interval: bar size in minutes (1, 5, 15, 25, 60)
        days_back: how many past trading days to include (max 5 for 1-min, 90 for 60-min)
        Dhan-only — no yfinance fallback. Returns empty DataFrame on failure.
        """
        security_id = get_security_id(symbol)
        from_dt = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        to_dt   = datetime.now().strftime("%Y-%m-%d")
        data = {
            "securityId":      security_id,
            "exchangeSegment": self._segment_for(symbol),
            "instrument":      self._instrument_for(symbol),
            "interval":        str(interval),
            "fromDate":        from_dt,
            "toDate":          to_dt,
        }
        result = self._request("POST", "/charts/intraday", data, _use_data_session=True)
        df = self._parse_ohlcv(result)
        if df.empty:
            log.warning("Dhan intraday returned no data for %s (no fallback — Dhan-only)", symbol)
        return df

    def get_quote(self, symbols: List[str], exchange: str = "NSE_EQ") -> Dict:
        """Fetch LTP + volume. Dhan v2 endpoint: POST /marketFeed/quote.
        Returns normalized {"data": [{"symbol": str, "volume": int, "last_price": float}, ...]}
        so callers don't need to handle the security-id-keyed response format.
        """
        endpoint = "/marketFeed/quote"

        # Build reverse map: security_id → symbol for response parsing
        id_to_sym: Dict[str, str] = {}
        for sym in symbols:
            sid = get_security_id(sym.upper())
            id_to_sym[str(sid)] = sym.upper()

        # Dhan v2 quote request: {exchange_segment: [security_id, ...]}
        request_data = {exchange: list(id_to_sym.keys())}
        result = self._request("POST", endpoint, request_data)

        if "error" in result:
            return result

        # Normalize to list-of-dicts so live_runner._scan_via_quotes() can iterate normally
        raw_data = result.get("data", {}) if isinstance(result, dict) else {}
        items: List[Dict] = []
        if isinstance(raw_data, dict):
            for sid, info in raw_data.items():
                if not isinstance(info, dict):
                    continue
                sym = id_to_sym.get(str(sid), info.get("tradingSymbol", sid))
                items.append({
                    "symbol":         sym,
                    "tradingSymbol":  sym,
                    "volume":         info.get("volume", 0) or info.get("totalTradedVolume", 0),
                    "last_price":     info.get("lastTradedPrice", 0) or info.get("last_price", 0),
                })
        elif isinstance(raw_data, list):
            items = raw_data

        return {"data": items}

    def get_option_quote(self, underlying: str, expiry_iso: str, strike: float,
                          ce_pe: str) -> Optional[float]:
        """Get live LTP for a specific option contract via /marketFeed/quote.

        Uses trading JWT (NOT Data API key) — works without Data API subscription.
        Returns LTP float or None on failure.
        """
        try:
            from . import scrip_master as _sm
            sid = _sm.lookup_option(underlying, expiry_iso, strike, ce_pe)
            if not sid:
                log.debug(f"option sid not found: {underlying} {expiry_iso} {strike} {ce_pe}")
                return None
            # NSE F&O segment for stock options
            seg = "NSE_FNO"
            result = self._request("POST", "/marketFeed/quote",
                                   {seg: [int(sid) if str(sid).isdigit() else sid]})
            if "error" in result:
                log.debug(f"option quote error: {result['error']}")
                return None
            data = result.get("data", {})
            if isinstance(data, dict):
                for _, info in data.items():
                    if isinstance(info, dict):
                        ltp = info.get("lastTradedPrice", 0) or info.get("last_price", 0)
                        if ltp:
                            return float(ltp)
            return None
        except Exception as exc:
            log.debug(f"get_option_quote failed: {exc}")
            return None

    def get_market_depth(self, symbol: str, exchange: str = "NSE") -> Dict:
        endpoint = "/marketdepth"
        data = {"symbol": symbol, "exchange": exchange}

        result = self._request("GET", endpoint, data)
        return result

    def get_fno_scrip(self, symbol: str) -> List[Dict]:
        endpoint = "/fno/subtokens"
        data = {"symbol": symbol}

        result = self._request("GET", endpoint, data)

        if "data" in result:
            return result["data"]
        return []

    def _underlying_segment(self, symbol: str) -> str:
        """Dhan UnderlyingSeg for option chain requests.

        IDX_I = index options (NIFTY, BANKNIFTY, etc.)
        NSE_FNO = stock F&O options (RELIANCE, TCS, etc.)
        """
        if symbol.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYIT"):
            return "IDX_I"
        return "NSE_FNO"

    def get_option_expiry_list(self, symbol: str) -> List[str]:
        """Fetch real available expiry dates from Dhan for a symbol.
        Returns sorted list of 'YYYY-MM-DD' strings, or [] on failure.
        """
        sec_id = get_security_id(symbol.upper())
        seg = self._underlying_segment(symbol)
        result = self._request("POST", "/optionchain/expirylist", {
            "UnderlyingScrip": int(sec_id) if str(sec_id).isdigit() else sec_id,
            "UnderlyingSeg": seg,
        })
        if isinstance(result, dict):
            dates = result.get("data", [])
            if isinstance(dates, list) and dates:
                return sorted(str(d) for d in dates)
        return []

    def get_option_chain(self, symbol: str, expiry_date: str = None) -> List[Dict]:
        """Fetch live option chain from Dhan using correct POST format.
        Returns list of {strike, ce_ltp, ce_oi, ce_iv, pe_ltp, pe_oi, pe_iv}.
        """
        import time as _t
        symbol = symbol.upper()

        # ── Global 429 backoff: when Dhan recently rate-limited, skip all
        #    option chain calls for 30s instead of compounding the abuse.
        #    Honors the CROSS-PROCESS shared backoff too (logs/dhan_backoff.json).
        now = _t.time()
        if now < max(DhanAPI._oc_backoff_until, _shared_backoff_get("oc")):
            log.debug(f"option chain {symbol}: in 429 backoff window, skipping")
            return []

        # ── Per-call rate limit (avoid 429 when scanning many symbols) ──────
        elapsed = now - DhanAPI._oc_last_call
        if elapsed < DhanAPI._OC_RATE_LIMIT:
            _t.sleep(DhanAPI._OC_RATE_LIMIT - elapsed)
        DhanAPI._oc_last_call = _t.time()

        # ── Resolve expiry: ask Dhan for real dates, don't guess ─────────────
        if expiry_date is None:
            expiries = self.get_option_expiry_list(symbol)
            if expiries:
                today_str = _now_ist().strftime("%Y-%m-%d")
                future = [e for e in expiries if e >= today_str]
                expiry_date = future[0] if future else expiries[-1]
            else:
                expiry_date = self._get_next_expiry(symbol)

        sec_id = get_security_id(symbol)
        seg = self._underlying_segment(symbol)

        result = self._request("POST", "/optionchain", {
            "UnderlyingScrip": int(sec_id) if str(sec_id).isdigit() else sec_id,
            "UnderlyingSeg": seg,
            "Expiry": expiry_date,
        })

        if not isinstance(result, dict):
            log.warning("option chain %s: non-dict response %r", symbol, result)
            return []

        if result.get("status") == "error" or "error" in result:
            log.warning("option chain %s expiry=%s: API error - %s", symbol, expiry_date, result.get("error", result))
            return []

        data = result.get("data", {})
        if not isinstance(data, dict) or "oc" not in data:
            log.warning("option chain %s expiry=%s: unexpected response shape - %r", symbol, expiry_date, list(result.keys()))
            return []

        rows: List[Dict] = []
        for strike_str, leg in data["oc"].items():
            try:
                strike = float(strike_str)
            except Exception:
                continue
            ce = leg.get("ce", {}) or {}
            pe = leg.get("pe", {}) or {}
            rows.append({
                "strike":  strike,
                "ce_ltp":  float(ce.get("last_price", 0) or 0),
                "ce_oi":   int(ce.get("oi", 0) or 0),
                "ce_iv":   float(ce.get("implied_volatility", 0) or ce.get("iv", 0) or 0),
                "pe_ltp":  float(pe.get("last_price", 0) or 0),
                "pe_oi":   int(pe.get("oi", 0) or 0),
                "pe_iv":   float(pe.get("implied_volatility", 0) or pe.get("iv", 0) or 0),
                "_expiry": expiry_date,
                "_spot":   float(data.get("last_price", 0) or 0),
            })
        rows.sort(key=lambda r: r["strike"])
        return rows

    def _get_next_expiry(self, symbol: str) -> str:
        """Fallback: NSE holiday-adjusted expiry when expiry-list API unavailable.

        NSE weekly expiry schedule (2024+):
          - Stock options: Tuesday
          - NIFTY: Tuesday
          - BANKNIFTY: Wednesday
          - FINNIFTY: Tuesday
        """
        try:
            from .nse_calendar import expiry_str_iso
            return expiry_str_iso(symbol)
        except Exception:
            today = _now_ist()
            # Stock + NIFTY = Tuesday (weekday 1), BANKNIFTY = Wednesday (weekday 2)
            sym_upper = symbol.upper()
            if sym_upper == "BANKNIFTY":
                target_day = 2  # Wednesday
            else:
                target_day = 1  # Tuesday (stocks + NIFTY + FINNIFTY)
            days_ahead = (target_day - today.weekday()) % 7
            if days_ahead == 0:
                import datetime as _dt2
                if today.time() >= _dt2.time(15, 30):
                    days_ahead = 7
            return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    def place_order(
        self,
        symbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "LIMIT",
        price: float = None,
        stop_loss: float = None,
        product_type: str = "INTRADAY",
        validity: str = "DAY",
    ) -> Dict:
        endpoint = "/orders"
        security_id = get_security_id(symbol)

        # Dhan API v2 uses camelCase field names
        order_data = {
            "dhanClientId":      self.client_id,
            "transactionType":   transaction_type,   # "BUY" or "SELL"
            "exchangeSegment":   exchange,            # "NSE_EQ" or "NSE_FNO"
            "productType":       product_type,        # "INTRADAY" or "CNC"
            "orderType":         order_type,          # "LIMIT" or "MARKET"
            "validity":          validity,             # "DAY" or "IOC"
            "tradingSymbol":     symbol,
            "securityId":        security_id,
            "quantity":          quantity,
            "disclosedQuantity": 0,
            "price":             price or 0,
            "triggerPrice":      stop_loss or 0,
            "afterMarketOrder":  False,
        }

        result = self._request("POST", endpoint, order_data)
        return result

    def modify_order(
        self,
        order_id: str,
        price: float = None,
        quantity: int = None,
        validity: str = "DAY",
    ) -> Dict:
        endpoint = f"/orders/{order_id}"

        order_data = {}
        if price:
            order_data["price"] = price
        if quantity:
            order_data["quantity"] = quantity
        if validity:
            order_data["validity"] = validity

        return self._request("PUT", endpoint, order_data)

    def cancel_order(self, order_id: str) -> Dict:
        endpoint = f"/orders/{order_id}"
        return self._request("DELETE", endpoint)

    def get_order_book(self) -> List[Dict]:
        endpoint = "/orders"
        result = self._request("GET", endpoint)

        if "data" in result:
            return result["data"]
        return []

    def get_positions(self) -> List[Dict]:
        endpoint = "/positions"
        result = self._request("GET", endpoint)

        if "data" in result:
            return result["data"]
        return []

    def get_trades(self) -> List[Dict]:
        endpoint = "/trades"
        result = self._request("GET", endpoint)

        if "data" in result:
            return result["data"]
        return []

    def get_holdings(self) -> List[Dict]:
        endpoint = "/holdings"
        result = self._request("GET", endpoint)

        if "data" in result:
            return result["data"]
        return []

    def get_fno_oi(self, symbol: str) -> Dict:
        endpoint = "/fno/oi"
        data = {"symbol": symbol}

        result = self._request("GET", endpoint, data)
        return result

    def get_fno_ltp(
        self,
        symbol: str,
        expiry_date: str = None,
        strike_price: float = None,
        option_type: str = None,
    ) -> float:
        if expiry_date is None:
            expiry_date = self._get_next_expiry(symbol)

        if strike_price and option_type:
            scrip = f"{symbol}{expiry_date}{int(strike_price)}{option_type}"
        else:
            scrip = symbol

        result = self.get_quote([scrip])

        if "data" in result and scrip in result["data"]:
            return float(result["data"][scrip].get("last_price", 0))

        return 0


dhan_api = DhanAPI()
