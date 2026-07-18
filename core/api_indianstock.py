"""
indianapi.in adapter - REST stock-data API at https://stock.indianapi.in

Auth: header `X-Api-Key: <key>` (permanent key, no rotation).
Key stored via core.secrets.save_indianapi_key / get_indianapi_key.

WHY defensive parsing:
    The exact JSON shape of each endpoint is not verified in this sandbox (no
    key here). Every fetch method returns a best-effort normalized result AND
    the raw payload is reachable via `probe` so the shape can be locked down
    against a real response. Historical OHLC in particular may come back as
    close-only line data on some plans - `get_daily` flags that.

NORMALIZED OUTPUT (get_daily): DataFrame with columns
    date, open, high, low, close, volume   (same schema as dhan_daily)
    - so it can drop into timeframe_sync / signal_engine unchanged.

RUN:
    python -m core.api_indianstock probe historical RELIANCE   # dump raw JSON
    python -m core.api_indianstock probe news
    python -m core.api_indianstock daily RELIANCE              # normalized df
    python -m core.api_indianstock news
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, List, Optional

import pandas as pd

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from .secrets import get_indianapi_key

log = logging.getLogger(__name__)

BASE_URL = "https://stock.indianapi.in"
_TIMEOUT = 15

# indianapi.in period tokens accepted by /historical_data
VALID_PERIODS = {"1m", "6m", "1yr", "3yr", "5yr", "10yr", "max"}


class IndianStockAPI:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = (api_key or get_indianapi_key()).strip()
        if not self.api_key:
            raise RuntimeError(
                "No indianapi.in key. Set it: "
                "python -c \"from core.secrets import save_indianapi_key; "
                "save_indianapi_key('YOUR_KEY')\""
            )
        if requests is None:
            raise RuntimeError("`requests` not installed - pip install requests")
        self._session = requests.Session()
        self._session.headers.update({"X-Api-Key": self.api_key})

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Raw GET. Returns parsed JSON (dict/list) or raises."""
        url = f"{BASE_URL}/{path.lstrip('/')}"
        resp = self._session.get(url, params=params or {}, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    # ── raw endpoints (return whatever the API sends) ──────────────────────

    def raw_historical(self, symbol: str, period: str = "1yr",
                       filter_: str = "price") -> Any:
        if period not in VALID_PERIODS:
            log.warning("period %s not in %s - sending anyway", period, VALID_PERIODS)
        return self._get("historical_data", {
            "stock_name": symbol, "period": period, "filter": filter_,
        })

    def raw_stock(self, name: str) -> Any:
        return self._get("stock", {"name": name})

    def raw_news(self) -> Any:
        return self._get("news")

    def raw_trending(self) -> Any:
        return self._get("trending")

    def raw_corporate_actions(self, symbol: str) -> Any:
        """Structured corporate actions: board_meetings, dividends, splits,
        bonus, rights (each typically a list of dated items)."""
        return self._get("corporate_actions", {"stock_name": symbol})

    def raw_announcements(self, symbol: str) -> Any:
        """Recent exchange announcements: list of {title, link, date}."""
        return self._get("recent_announcements", {"stock_name": symbol})

    def raw(self, path: str, **params) -> Any:
        """Generic GET for probing undocumented endpoints.
        e.g. api.raw('corporate_actions', stock_name='RELIANCE')"""
        return self._get(path, params)

    # -- normalized ----------------------------------------------------------

    def get_daily(self, symbol: str, period: str = "1yr") -> pd.DataFrame:
        """Daily bars -> date/open/high/low/close/volume.

        indianapi /historical_data commonly returns close-only line data
        ({datasets:[{metric:'Price', values:[[date, val], ...]}]}). When only
        close is present, open/high/low are filled with close and a warning is
        logged - usable for trend, NOT for candlestick-body patterns. If the
        real payload carries full OHLC, extend the parser here (probe first)."""
        try:
            raw = self.raw_historical(symbol, period=period)
        except Exception as e:
            log.warning("indianapi historical %s failed: %s", symbol, e)
            return pd.DataFrame()

        rows = _parse_historical(raw)
        if not rows:
            log.warning("indianapi historical %s: could not parse payload "
                        "(run `probe historical %s` to inspect)", symbol, symbol)
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                df[col] = df["close"] if col != "volume" else 0
        df = df[["date", "open", "high", "low", "close", "volume"]]
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df.sort_values("date").reset_index(drop=True)

    def get_news(self, symbol: Optional[str] = None) -> List[Dict]:
        """Normalized news -> list of {title, date, url, summary}. If `symbol`
        given, filter titles containing it (API /news is market-wide)."""
        try:
            raw = self.raw_news()
        except Exception as e:
            log.warning("indianapi news failed: %s", e)
            return []
        items = _parse_news(raw)
        if symbol:
            s = symbol.upper()
            items = [n for n in items if s in (n.get("title", "") or "").upper()]
        return items


# Candidate endpoints for STRUCTURED corporate events (better than scraping the
# news feed). Not all exist on every plan - probe to find which return data.
# Discover with: python -m core.api_indianstock discover
CANDIDATE_EVENT_ENDPOINTS = [
    "corporate_actions", "recent_announcements", "announcements",
    "historical_stats", "statement", "analyst_recommendations",
    "stock_forecasts", "ipo", "price_shockers",
]


# --------------------------- parsers (defensive) ---------------------------

def _parse_historical(raw: Any) -> List[Dict]:
    """Handle the shapes indianapi is known/likely to emit.
    Returns list of {date, close[, open, high, low, volume]}."""
    # Shape A: {"datasets": [{"metric": "Price", "values": [[date, val], ...]}]}
    if isinstance(raw, dict) and "datasets" in raw:
        by_metric: Dict[str, Dict[str, float]] = {}
        for ds in raw.get("datasets", []):
            metric = str(ds.get("metric", ds.get("label", ""))).lower()
            for pair in ds.get("values", []):
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                d = str(pair[0])
                try:
                    v = float(pair[1])
                except (TypeError, ValueError):
                    continue
                by_metric.setdefault(metric, {})[d] = v
        price = by_metric.get("price") or by_metric.get("close") or {}
        vol = by_metric.get("volume") or {}
        rows = [{"date": d, "close": c, "volume": vol.get(d, 0)}
                for d, c in price.items()]
        return rows

    # Shape B: {"data": [{"date":..,"open":..,"high":..,"low":..,"close":..,"volume":..}]}
    seq = None
    if isinstance(raw, list):
        seq = raw
    elif isinstance(raw, dict):
        for key in ("data", "history", "historical", "values", "prices"):
            if isinstance(raw.get(key), list):
                seq = raw[key]
                break
    if seq:
        rows = []
        for item in seq:
            if not isinstance(item, dict):
                continue
            d = item.get("date") or item.get("Date") or item.get("timestamp")
            c = item.get("close", item.get("Close", item.get("price")))
            if d is None or c is None:
                continue
            row = {"date": d, "close": _f(c)}
            for k_src, k_dst in (("open", "open"), ("Open", "open"),
                                 ("high", "high"), ("High", "high"),
                                 ("low", "low"), ("Low", "low"),
                                 ("volume", "volume"), ("Volume", "volume")):
                if k_src in item:
                    row[k_dst] = _f(item[k_src])
            rows.append(row)
        return rows

    return []


def _parse_news(raw: Any) -> List[Dict]:
    seq = raw if isinstance(raw, list) else (
        raw.get("news") or raw.get("data") or raw.get("articles") or []
        if isinstance(raw, dict) else []
    )
    out = []
    for item in seq:
        if not isinstance(item, dict):
            continue
        out.append({
            "title": item.get("title") or item.get("headline") or "",
            "date": (item.get("pub_date") or item.get("date") or item.get("pubDate")
                     or item.get("published") or item.get("published_at", "")),
            "url": item.get("url") or item.get("link", ""),
            "summary": item.get("summary") or item.get("description", ""),
            "topics": item.get("topics", []),
            "source_name": item.get("source", ""),
        })
    return out


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────── CLI ───────────────────────────

def main(argv: List[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]

    if cmd == "discover":
        api = IndianStockAPI()
        sym = argv[1] if len(argv) > 1 else "RELIANCE"
        print(f"probing candidate event endpoints (stock={sym})...\n")
        for ep in CANDIDATE_EVENT_ENDPOINTS:
            for params in ({"stock_name": sym}, {"name": sym}, {}):
                try:
                    r = api.raw(ep, **params)
                    n = len(r) if isinstance(r, (list, dict)) else "?"
                    keys = (list(r[0].keys())[:6] if isinstance(r, list) and r
                            and isinstance(r[0], dict)
                            else list(r.keys())[:8] if isinstance(r, dict) else "")
                    print(f"  OK   {ep:24s} params={params} -> {n} items, keys={keys}")
                    break
                except Exception as e:
                    code = getattr(getattr(e, "response", None), "status_code", "")
                    if code and int(code) != 404:
                        print(f"  {code}  {ep:24s} params={params}")
                        break
            else:
                print(f"  404  {ep:24s} (not on this plan)")
        return 0

    if cmd == "probe":
        api = IndianStockAPI()
        what = argv[1] if len(argv) > 1 else "trending"
        sym = argv[2] if len(argv) > 2 else "RELIANCE"
        known = {
            "historical": lambda: api.raw_historical(sym),
            "stock": lambda: api.raw_stock(sym),
            "news": api.raw_news,
            "trending": api.raw_trending,
        }
        if what in known:
            raw = known[what]()
        else:
            # any other endpoint name -> generic probe (corporate_actions,
            # recent_announcements, price_shockers, ...)
            raw = api.raw(what, stock_name=sym)
        print(json.dumps(raw, indent=2, ensure_ascii=False)[:4000])
        return 0

    if cmd == "daily":
        api = IndianStockAPI()
        df = api.get_daily(argv[1] if len(argv) > 1 else "RELIANCE")
        print(df.tail(10).to_string() if not df.empty else "(empty)")
        return 0

    if cmd == "news":
        api = IndianStockAPI()
        for n in api.get_news(argv[1] if len(argv) > 1 else None)[:10]:
            print(f"- {n['date']} | {n['title'][:80]}")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
