"""
News-reaction study - does good/bad news actually move the stock, and how much?

WHY forward-collector (not historical backfill):
    Google News RSS is a LIVE feed with only ~weeks of lookback. There is no
    free per-symbol news archive keyed to arbitrary past dates, so we cannot
    reconstruct "what news hit on 2024-03-11". Instead we SNAPSHOT news +
    sentiment + the news-day candle each run, then later fill in the forward
    return once the days have elapsed. Same pattern as the 15m forward
    collector (commit eac6b63).

FLOW:
    collect(symbols)  -> append one event row per symbol per day to the log
    analyze()         -> for events whose forward window has elapsed, fetch
                         daily bars, compute +1d/+3d/+5d return from the
                         news-day close, tag reacted_as_expected, aggregate.

DATA:
    Daily bars via yfinance (Dhan intraday is DH-905-blocked in this sandbox;
    yfinance DAILY is verified alive). Sentiment via
    NewsEventFilter.fetch_news_sentiment (Google News RSS + keyword score).

RUN:
    python -m core.news_reaction snapshot           # append full news feed to archive (daily)
    python -m core.news_reaction collect            # per-symbol reaction rows
    python -m core.news_reaction collect RELIANCE INFY TCS
    python -m core.news_reaction analyze            # forward-return report

NOTE ON NEWS HISTORY:
    There is NO historical news to download - indianapi /news (and Google RSS)
    are live feeds only. `snapshot` builds a news archive FORWARD from today.
    Schedule it daily; history accumulates one day at a time.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional

import pandas as pd

from .news_filter import NewsEventFilter

log = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Raw forward-built news archive (one line per unique article, dedup by URL).
# indianapi /news is a LIVE ~20-item feed with NO history - the only way to
# accumulate a news archive is to snapshot it daily from today onward.
NEWS_ARCHIVE = os.path.join(_ROOT, "logs", "news_archive.jsonl")
NEWS_LOG = os.path.join(_ROOT, "logs", "news_reaction.jsonl")

# Forward windows (trading days) measured from the news-day close.
FORWARD_DAYS = (1, 3, 5)
# Max window that must elapse before an event is "ripe" for analysis.
_MAX_FWD = max(FORWARD_DAYS)

# yfinance ticker overrides - most NSE symbols map to SYMBOL.NS, these don't.
# (edge cases documented in CLAUDE.md)
_YF_OVERRIDES = {
    "TATAMOTORS": "TMCV.NS",
    "MCDOWELL-N": "UNITDSPR.NS",
    "DEEPAKNT": "DEEPAKNTR.NS",
}


def _yf_ticker(symbol: str) -> str:
    s = symbol.upper()
    return _YF_OVERRIDES.get(s, f"{s}.NS")


def _daily_bars(symbol: str, days_back: int = 30) -> pd.DataFrame:
    """Daily OHLCV via yfinance. Columns: date/open/high/low/close/volume.
    Empty DataFrame on failure. yfinance DAILY is the only source verified
    alive in this sandbox (Dhan intraday = DH-905)."""
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed - cannot fetch daily bars")
        return pd.DataFrame()

    period = f"{max(days_back, 7)}d"
    try:
        raw = yf.download(
            _yf_ticker(symbol), period=period, interval="1d",
            progress=False, auto_adjust=False,
        )
    except Exception as e:
        log.warning("yfinance daily %s failed: %s", symbol, e)
        return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    # yfinance returns a MultiIndex column frame for single tickers too.
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    df = raw.reset_index()[["Date", "Open", "High", "Low", "Close", "Volume"]]
    df.columns = ["date", "open", "high", "low", "close", "volume"]
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


# Keyword scoring for real financial headlines (title + summary). Tuned against
# actual indianapi/moneycontrol/CNBC phrasing - the old news_filter lexicon
# (order-win/contract only) scored real earnings headlines at zero.
# NOTE: keyword sentiment is intentionally ROUGH. It is the *input* to the
# reaction study, not ground truth - the study measures whether this rough
# label actually predicts forward return. Substring match, so phrases are
# chosen to be unambiguous.
_POSITIVE = [
    'profit up', 'profit rise', 'profit rises', 'profit jump', 'profit surge',
    'profit grows', 'profit inches up', 'net profit rise', 'pat rises',
    'beats estimate', 'beat estimate', 'tops estimate', 'tops charts',
    'record profit', 'record revenue', 'record high', 'all-time high',
    'strong operating', 'strong growth', 'robust growth', 'double-digit growth',
    'revenue up', 'revenue rise', 'revenue grows', 'order win', 'wins order',
    'bags order', 'bags contract', 'wins contract', 'secures order',
    'successful bidder', 'emerges as', 'upgrade', 'buyback', 'bonus issue',
    'special dividend', 'stake acquisition', 'acquisition', 'expansion',
    'capex', 'surges', 'jumps', 'rallies', 'gains', 'outperform', 'multibagger',
]
_NEGATIVE = [
    'profit down', 'profit fall', 'profit falls', 'profit drop', 'profit slump',
    'profit declines', 'net loss', 'posts loss', 'widens loss', 'misses estimate',
    'miss estimate', 'revenue miss', 'revenue fall', 'revenue declines',
    'downgrade', 'cut to', 'target cut', 'slumps', 'plunges', 'tanks', 'crashes',
    'falls', 'drops', 'declines', 'slides', 'tumbles', 'weak', 'probe', 'penalty',
    'fine', 'fraud', 'raid', 'sebi notice', 'sebi order', 'default', 'downgraded',
    'pledge increase', 'debt concern', 'stake sale', 'block deal sell', 'resigns',
    'layoff', 'recall', 'ban', 'warning', 'write-off', 'impairment',
]


def _compile(words: List[str]) -> "re.Pattern":
    # word-boundary match so 'ban' doesn't fire on "bank", 'fine' on "refine",
    # 'raid' on "afraid". Escape, allow internal spaces/hyphens as literals.
    alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"\b(?:{alt})\b", re.IGNORECASE)


_POS_RE = None
_NEG_RE = None


def _score_headlines(items: List) -> Dict:
    """Score news items -> {score, signal, headlines}. Each item may be a plain
    title string or a dict with title+summary (summary adds signal). Directional
    only when |score| > 1 (same gate as the RSS path)."""
    global _POS_RE, _NEG_RE
    if _POS_RE is None:
        _POS_RE, _NEG_RE = _compile(_POSITIVE), _compile(_NEGATIVE)

    score = 0
    titles: List[str] = []
    for it in items:
        if isinstance(it, dict):
            title = it.get("title", "") or ""
            text = f"{title} {it.get('summary', '') or ''}"
        else:
            title = it or ""
            text = title
        titles.append(title)
        score += len(_POS_RE.findall(text))
        score -= len(_NEG_RE.findall(text))
    signal = "bullish" if score > 1 else "bearish" if score < -1 else "neutral"
    return {"score": score, "signal": signal, "headlines": titles[:3]}


def _fetch_sentiment(symbol: str, nf: "NewsEventFilter") -> Dict:
    """News sentiment for a symbol. Prefer indianapi.in (real dated headlines,
    unaffected by the RSS 36h-vs-fake-clock problem); fall back to Google News
    RSS via news_filter when indianapi is unavailable/keyless."""
    try:
        from .api_indianstock import IndianStockAPI
        api = IndianStockAPI()
        items = api.get_news(symbol)
        if items:
            out = _score_headlines(items)  # dicts -> title+summary scored
            out["source"] = "indianapi"
            _attach_event(out, items)
            return out
    except Exception as e:
        log.debug("indianapi news unavailable for %s (%s) - RSS fallback", symbol, e)
    out = nf.fetch_news_sentiment(symbol)
    out["source"] = "google_rss"
    _attach_event(out, [{"title": h} for h in out.get("headlines", [])])
    return out


def _attach_event(out: Dict, items: List) -> None:
    """Tag the sentiment dict with a typed corporate event (results_beat,
    buyback, demerger, order_win, ...) via event_classifier. When a typed event
    is present it OVERRIDES the keyword signal - events are higher-signal than
    generic sentiment. Falls back to the keyword signal when no event matches."""
    from .event_classifier import classify_primary, net_direction
    text = " ".join(
        f"{(it.get('title','') if isinstance(it, dict) else it) or ''} "
        f"{(it.get('summary','') if isinstance(it, dict) else '')}"
        for it in items
    )
    etype, edir, conf = classify_primary(text)
    sign, _, types = net_direction(text)
    out["event_type"] = etype
    out["event_types"] = types
    if etype != "none":
        out["signal"] = "bullish" if sign > 0 else "bearish" if sign < 0 else out.get("signal", "neutral")


def classify_candle(o: float, h: float, lo: float, c: float) -> str:
    """Candle shape on the news day. bullish / bearish / doji."""
    rng = h - lo
    if rng <= 0:
        return "doji"
    body = abs(c - o)
    if body / rng < 0.25:
        return "doji"
    return "bullish" if c >= o else "bearish"


# ─────────────────────── news archive (forward) ───────────────────────

def _archived_urls() -> set:
    urls = set()
    if os.path.exists(NEWS_ARCHIVE):
        with open(NEWS_ARCHIVE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    urls.add(json.loads(line).get("url"))
                except Exception:
                    continue
    return urls


def snapshot_news() -> int:
    """Fetch the full indianapi /news feed once, dedup by URL against the
    archive, append new articles. Run daily (scheduler/cron) to build a news
    history going forward. Returns new articles written.

    This is the RAW store. `collect()` builds the per-symbol reaction rows;
    this builds the full-market news history for later mining."""
    try:
        from .api_indianstock import IndianStockAPI
        api = IndianStockAPI()
    except Exception as e:
        log.warning("indianapi unavailable for news snapshot: %s", e)
        return 0

    items = api.get_news()  # market-wide, all items
    if not items:
        log.info("news snapshot: feed empty")
        return 0

    seen = _archived_urls()
    os.makedirs(os.path.dirname(NEWS_ARCHIVE), exist_ok=True)
    written = 0
    with open(NEWS_ARCHIVE, "a", encoding="utf-8") as f:
        for n in items:
            url = n.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            row = {
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "pub_date": n.get("date", ""),
                "title": n.get("title", ""),
                "summary": n.get("summary", ""),
                "url": url,
                "topics": n.get("topics", []),
                "source": n.get("source_name", ""),
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    log.info("news snapshot: +%d new (archive now %d)", written, len(seen))
    return written


# ─────────────────────────── collect ───────────────────────────

def _already_logged_today(symbol: str, day: str) -> bool:
    if not os.path.exists(NEWS_LOG):
        return False
    with open(NEWS_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("symbol") == symbol and r.get("date") == day:
                return True
    return False


def collect(symbols: List[str]) -> int:
    """Snapshot today's news + sentiment + news-day candle per symbol.
    One row per symbol per day (idempotent). Returns rows written.

    Only records events that carry a directional signal (bullish/bearish) -
    neutral days are noise for a reaction study."""
    nf = NewsEventFilter()
    today = date.today().isoformat()
    os.makedirs(os.path.dirname(NEWS_LOG), exist_ok=True)

    written = 0
    for symbol in symbols:
        symbol = symbol.upper().strip()
        if not symbol:
            continue
        if _already_logged_today(symbol, today):
            continue

        news = _fetch_sentiment(symbol, nf)
        signal = news.get("signal", "neutral")
        if signal == "neutral":
            continue  # no directional catalyst - skip

        bars = _daily_bars(symbol, days_back=10)
        if bars.empty:
            log.warning("no daily bars for %s - skipping snapshot", symbol)
            continue
        last = bars.iloc[-1]
        candle = classify_candle(last["open"], last["high"], last["low"], last["close"])

        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "date": str(last["date"]),      # the actual bar date (not wall clock)
            "symbol": symbol,
            "signal": signal,               # bullish | bearish
            "score": news.get("score", 0),
            "event_type": news.get("event_type", "none"),   # typed corporate event
            "event_types": news.get("event_types", []),
            "news_candle": candle,          # how it reacted SAME day
            "close": round(float(last["close"]), 2),
            "source": news.get("source", "unknown"),
            "headlines": news.get("headlines", [])[:3],
            # forward returns filled in later by analyze()
            "ret_1d": None, "ret_3d": None, "ret_5d": None,
            "ripe": False,
        }
        with open(NEWS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        written += 1
        log.info("logged news event %s %s (%s, score %s)",
                 symbol, signal, candle, row["score"])

    return written


# ─────────────────────────── analyze ───────────────────────────

def _fwd_return(bars: pd.DataFrame, event_day: date, n: int, base: float) -> Optional[float]:
    """Return over n trading days AFTER event_day, vs base close. None if not
    enough bars have elapsed yet."""
    after = bars[bars["date"] > event_day].reset_index(drop=True)
    if len(after) < n:
        return None
    fwd_close = float(after.iloc[n - 1]["close"])
    return round((fwd_close - base) / base * 100, 2)


def _expected_sign(signal: str) -> int:
    return 1 if signal == "bullish" else -1 if signal == "bearish" else 0


def analyze() -> Dict:
    """Fill forward returns for ripe events, rewrite the log, print a report.
    Returns aggregate stats dict."""
    if not os.path.exists(NEWS_LOG):
        print("No news_reaction log yet. Run `collect` first.")
        return {}

    rows: List[Dict] = []
    with open(NEWS_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue

    today = date.today()
    bars_cache: Dict[str, pd.DataFrame] = {}

    for r in rows:
        if r.get("ripe"):
            continue
        event_day = datetime.fromisoformat(r["date"]).date()
        # Only bother fetching once the max window could plausibly have elapsed
        # (calendar days as a cheap gate; trading-day check happens in _fwd_return).
        if (today - event_day).days < _MAX_FWD:
            continue

        sym = r["symbol"]
        if sym not in bars_cache:
            bars_cache[sym] = _daily_bars(sym, days_back=45)
        bars = bars_cache[sym]
        if bars.empty:
            continue

        base = float(r["close"])
        for n in FORWARD_DAYS:
            r[f"ret_{n}d"] = _fwd_return(bars, event_day, n, base)
        # ripe once the longest window is filled
        if r.get(f"ret_{_MAX_FWD}d") is not None:
            r["ripe"] = True

    # rewrite log with filled returns
    with open(NEWS_LOG, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return _report(rows)


def _report(rows: List[Dict]) -> Dict:
    ripe = [r for r in rows if r.get("ripe")]
    print("\n" + "=" * 64)
    print("NEWS-REACTION STUDY  (daily bars)")
    print("=" * 64)
    print(f"events logged: {len(rows)}   ripe (forward window elapsed): {len(ripe)}")

    if not ripe:
        pending = len(rows)
        print(f"\nNothing ripe yet - {pending} event(s) still inside the "
              f"{_MAX_FWD}-day forward window. Re-run `analyze` in a few days.")
        return {"logged": len(rows), "ripe": 0}

    stats: Dict[str, Dict] = {}
    for sig in ("bullish", "bearish"):
        grp = [r for r in ripe if r["signal"] == sig]
        if not grp:
            continue
        exp = _expected_sign(sig)
        block = {"n": len(grp)}
        for nd in FORWARD_DAYS:
            vals = [r[f"ret_{nd}d"] for r in grp if r.get(f"ret_{nd}d") is not None]
            if not vals:
                continue
            avg = sum(vals) / len(vals)
            # "reacted as expected" = forward move in the news direction
            wins = sum(1 for v in vals if v * exp > 0)
            block[f"{nd}d"] = {
                "avg_ret": round(avg, 2),
                "win_rate": round(wins / len(vals) * 100, 1),
                "n": len(vals),
            }
        stats[sig] = block

    for sig, block in stats.items():
        print(f"\n{sig.upper()}  (n={block['n']})")
        for nd in FORWARD_DAYS:
            b = block.get(f"{nd}d")
            if b:
                print(f"  +{nd}d: avg {b['avg_ret']:+.2f}%   "
                      f"moved-as-expected {b['win_rate']:.0f}%   (n={b['n']})")

    # ── PER-EVENT-TYPE drift - the core of the thesis ─────────────────────
    # Does a buyback actually pump? Does a demerger unlock value? How long
    # does an order-win rally hold? One row per corporate-event type.
    from collections import defaultdict
    by_type: Dict[str, List[Dict]] = defaultdict(list)
    for r in ripe:
        et = r.get("event_type", "none")
        if et and et != "none":
            by_type[et].append(r)

    if by_type:
        print("\nPER-EVENT-TYPE forward drift (avg %, n):")
        print(f"  {'event':20s} {'n':>3s}  " +
              "  ".join(f"+{nd}d" for nd in FORWARD_DAYS))
        event_stats: Dict[str, Dict] = {}
        for et, grp in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
            cells, es = [], {"n": len(grp)}
            for nd in FORWARD_DAYS:
                vals = [r[f"ret_{nd}d"] for r in grp if r.get(f"ret_{nd}d") is not None]
                if vals:
                    avg = round(sum(vals) / len(vals), 2)
                    es[f"{nd}d"] = avg
                    cells.append(f"{avg:+6.2f}")
                else:
                    cells.append("   -- ")
            event_stats[et] = es
            print(f"  {et:20s} {len(grp):3d}  " + "  ".join(cells))
        stats["by_event_type"] = event_stats

    # same-day candle vs signal - did the news day itself confirm?
    print("\nSame-day candle vs news signal:")
    for sig in ("bullish", "bearish"):
        grp = [r for r in ripe if r["signal"] == sig]
        if not grp:
            continue
        match = sum(1 for r in grp if r["news_candle"] == sig)
        print(f"  {sig}: {match}/{len(grp)} days closed {sig}")

    print("\nReminder: forward-collected, thin sample. Per-event-type rows need "
          "many observations before trusting - the priors are HYPOTHESES.")
    return {"logged": len(rows), "ripe": len(ripe), "stats": stats}


# ─────────────────────────── CLI ───────────────────────────

def _default_universe() -> List[str]:
    """Fall back to a small liquid set if none passed."""
    try:
        from .universe import get_universe  # optional
        u = get_universe()
        if u:
            return u[:40]
    except Exception:
        pass
    return ["RELIANCE", "INFY", "TCS", "HDFCBANK", "ICICIBANK",
            "SBIN", "TATAMOTORS", "AXISBANK", "ITC", "LT"]


def main(argv: List[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cmd = argv[0] if argv else "analyze"

    if cmd == "snapshot":
        n = snapshot_news()
        print(f"news snapshot: +{n} new article(s) into {NEWS_ARCHIVE}")
        return 0

    if cmd == "collect":
        syms = argv[1:] or _default_universe()
        n = collect(syms)
        print(f"collected {n} new news event(s) into {NEWS_LOG}")
        return 0
    if cmd == "analyze":
        analyze()
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
