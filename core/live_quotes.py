"""
live_quotes.py — legal near-real-time quotes, no paid data subscription.

WHY THIS EXISTS
---------------
The Dhan Data API subscription is expired, so the system's freshest price source
was yfinance intraday — roughly 3 MINUTES behind live. For a swing/overlay
system that is usually fine, but the 200-DMA overlay and the condor's VIX input
both read cleaner off live data, and there is a legal source for it that costs
nothing.

WHAT IT USES — and what it deliberately does NOT
------------------------------------------------
Source: NSE's OWN PUBLIC endpoint, https://www.nseindia.com/api/allIndices —
the exact JSON the public nseindia.com site renders for everyone. It carries
NIFTY, BANK NIFTY, MIDCAP and INDIA VIX, timestamped to the last exchange
update (~seconds during market hours). Reading a public website's public data
is not privileged access.

This is NOT and will never be:
  - tapping a broker's private line, another terminal's session, or colocation;
  - any form of intrusion, spoofing, or feed theft;
  - a latency edge. It is seconds-fresh, not microseconds. It cannot and is not
    meant to compete with colocated HFT — that race is unwinnable and this does
    not enter it. It simply replaces a 3-minute lag with a few-second one for a
    strategy that holds for weeks.

Per-STOCK live quotes are not available here: NSE's quote-equity endpoint is
bot-hardened (403) and there is no free per-symbol real-time source. For live
single-stock prices you need a broker WebSocket with your own API key — the
DhanMarketFeed class (core/market_feed.py) is the slot for that, and
`broker_feed_available()` reports whether it can run.

FALLBACK CHAIN (freshest first, never fabricates):
  1. NSE allIndices          (~seconds, indices + VIX)   — legal public data
  2. yfinance                (~3 min, indices + stocks)  — free
  3. None                    (never a synthetic number)

RUN
---
    python -m core.live_quotes --indices
    python -m core.live_quotes --quote NIFTY
"""
from __future__ import annotations

import argparse
import ssl
import sys
import time
import warnings
from dataclasses import dataclass
from typing import Dict, Optional

warnings.filterwarnings("ignore")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ssl._create_default_https_context = ssl._create_unverified_context

# NSE index name -> the label used across this codebase.
_NSE_INDEX_MAP = {
    "NIFTY 50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTY MIDCAP 100": "MIDCAP100",
    # NSE renamed this one; the old "NIFTY FIN SERVICE" key matched nothing,
    # so FINNIFTY silently never appeared in the live index set.
    "NIFTY FINANCIAL SERVICES": "FINNIFTY",
    "INDIA VIX": "INDIAVIX",
}
_YF_INDEX = {
    "NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK",
    "INDIAVIX": "^INDIAVIX", "MIDCAP100": "^CNXMIDCAP",
}

_CACHE_TTL = 20            # seconds; well inside a swing system's needs
_cache: Dict[str, tuple] = {}     # key -> (epoch, payload)
_session = None


@dataclass
class Quote:
    symbol: str
    last: float
    source: str
    ts: Optional[str] = None
    change_pct: Optional[float] = None
    prev_close: Optional[float] = None


# ── NSE public session ──────────────────────────────────────────────────────

def _nse_session():
    global _session
    if _session is not None:
        return _session
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    try:
        s.get("https://www.nseindia.com", timeout=10, verify=False)   # cookie
    except Exception:
        pass
    _session = s
    return s


def _nse_all_indices() -> Dict[str, Quote]:
    """All indices in one call. Cached briefly so a scan of many symbols is
    one HTTP request, not N."""
    now = time.time()
    hit = _cache.get("nse_all")
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    out: Dict[str, Quote] = {}
    try:
        s = _nse_session()
        r = s.get("https://www.nseindia.com/api/allIndices", timeout=12, verify=False)
        if r.status_code == 200:
            j = r.json()
            ts = j.get("timestamp")
            for row in j.get("data", []):
                key = _NSE_INDEX_MAP.get(row.get("index"))
                if not key:
                    continue
                try:
                    out[key] = Quote(
                        symbol=key, last=float(row["last"]), source="nse_live",
                        ts=ts,
                        change_pct=_f(row.get("percentChange")),
                        prev_close=_f(row.get("previousClose")))
                except (KeyError, ValueError, TypeError):
                    continue
    except Exception:
        pass
    _cache["nse_all"] = (now, out)
    return out


def _f(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ── yfinance fallback ───────────────────────────────────────────────────────

def _yf_quote(symbol: str) -> Optional[Quote]:
    tk = _YF_INDEX.get(symbol.upper())
    if tk is None:
        tk = f"{symbol.upper()}.NS"       # stock fallback
    try:
        import yfinance as yf
        d = yf.Ticker(tk).history(period="5d", interval="1d")
        if d is None or len(d) < 1:
            return None
        last = float(d["Close"].iloc[-1])
        prev = float(d["Close"].iloc[-2]) if len(d) > 1 else None
        chg = ((last / prev - 1) * 100) if prev else None
        return Quote(symbol=symbol.upper(), last=last, source="yfinance",
                     ts=str(d.index[-1].date()),
                     change_pct=round(chg, 2) if chg is not None else None,
                     prev_close=prev)
    except Exception:
        return None


# ── Public API ──────────────────────────────────────────────────────────────

def get_index_quotes() -> Dict[str, Quote]:
    """All available indices, freshest source. Empty dict only if all sources
    fail — never a fabricated value."""
    q = _nse_all_indices()
    if q:
        return q
    # NSE down: rebuild what we can from yfinance.
    out = {}
    for k in ("NIFTY", "BANKNIFTY", "INDIAVIX"):
        yq = _yf_quote(k)
        if yq:
            out[k] = yq
    return out


def get_quote(symbol: str) -> Optional[Quote]:
    """Single symbol. Indices come live from NSE; stocks fall to yfinance
    (no free per-stock live source without a broker key)."""
    sym = symbol.upper()
    if sym in _NSE_INDEX_MAP.values():
        live = _nse_all_indices().get(sym)
        if live:
            return live
    return _yf_quote(sym)


def broker_feed_available() -> bool:
    """True only if a broker WebSocket (real per-stock live) can actually run —
    i.e. valid credentials exist. Reports the honest state of the fast path."""
    try:
        from core import secrets as _sec
        tok = _sec.get_access_token()
        return bool(tok and tok.startswith("eyJ"))
    except Exception:
        return False


def source_status() -> Dict:
    """What the system can actually see right now, and how fresh."""
    idx = get_index_quotes()
    nse_live = any(q.source == "nse_live" for q in idx.values())
    return {
        "nse_live_indices": nse_live,
        "indices_available": sorted(idx.keys()),
        "freshest_index_source": ("nse_live (~seconds)" if nse_live
                                  else "yfinance (~3 min)" if idx else "NONE"),
        "per_stock_live": broker_feed_available(),
        "per_stock_source": ("broker websocket" if broker_feed_available()
                             else "yfinance (~3 min) — add a broker key for live"),
        "nifty": idx.get("NIFTY").last if idx.get("NIFTY") else None,
        "india_vix": idx.get("INDIAVIX").last if idx.get("INDIAVIX") else None,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Legal near-real-time quotes.")
    ap.add_argument("--indices", action="store_true")
    ap.add_argument("--quote", type=str)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.indices:
        for k, q in sorted(get_index_quotes().items()):
            chg = f"{q.change_pct:+.2f}%" if q.change_pct is not None else "  -"
            print(f"  {k:12s} {q.last:>12,.2f}  {chg:>8}  [{q.source}]  {q.ts}")
        return 0
    if args.quote:
        q = get_quote(args.quote)
        if q:
            print(f"{q.symbol}: {q.last:,.2f}  "
                  f"({q.change_pct:+.2f}% )" if q.change_pct is not None
                  else f"{q.symbol}: {q.last:,.2f}")
            print(f"  source {q.source}  ts {q.ts}")
        else:
            print(f"{args.quote}: no quote (all sources failed)")
        return 0
    if args.status:
        import json
        print(json.dumps(source_status(), indent=2, default=str))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
