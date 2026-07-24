"""
market_state.py — per-symbol trend-state classifier over capturable factors.

WHAT THIS IS
------------
Given a symbol, decide whether it is trending UP, DOWN, or SIDEWAYS, and show
the evidence for that call factor by factor. It is a MONITOR, not a signal
generator: it reports state, assigns no edge, and produces no entries.

That distinction is load-bearing. This repo's research history is an exhaustive
negative on the "watch every factor, find alpha" thesis for this universe —
gap-fade, PEAD, vol-managed, index-rebalance and the regime gate were all
tested and rejected. Nothing here should be read as reviving that. The value of
a trend monitor is knowing what the book is sitting in, not predicting it.

HONEST INPUT INVENTORY
----------------------
The classic "factors that move a stock" list is much larger than what this
stack can actually measure. Rather than quietly dropping the rest, every
classification carries an explicit `unavailable` list, so a reader can never
mistake a thin evidence base for a complete one.

  MEASURED (from captured data)
    price_structure   swing highs/lows over the lookback
    ma_alignment      close vs EMA20 vs EMA50, and EMA20 slope
    adx_di            Wilder ADX strength + directional index
    volume_confirm    whether volume backs the up-days or the down-days
    sector            sector index trend (core.sector_rotation)
    market            broad NIFTY bias (core.market_bias)

  NOT AVAILABLE ON THIS STACK — and why
    order_flow        Dhan Data API subscription expired; core/market_feed.py
                      (the WebSocket carrying buy/sell queue sizes) is dead
    bid_ask_spread    no L1 quote source without a paid feed
    book_depth        no L2 source at any tier we hold
    news_sentiment    Google News RSS reachable but returns stale items
                      (freshest ~68h, most 800-1300h old) — unusable live
    analyst_ratings   no free source
    short_interest    no free NSE equivalent
    options_flow      would need a live chain feed; Dhan expired

CLASSIFICATION
--------------
ADX gates direction. Below ADX_TREND_FLOOR the tape is not trending regardless
of what the other factors say, so the call is SIDEWAYS — this mirrors the
standard reading that ADX < 20 means no trend, and it stops a stack of weakly
aligned factors from manufacturing conviction out of chop.

Above the floor, direction is the sign of the factor-vote sum, and `strength`
is the share of available factors that agree — but only if at least
MIN_AGREEING_FACTORS of them actually point that way. One lone factor is not
allowed to name a direction.

KNOWN LIMITATION — read before trusting a label
------------------------------------------------
These factors cannot distinguish a trend from a lucky random walk, and this
is a property of the indicators, not a bug to be tuned out. Measured on
synthetic mean-reverting tape, ADX(14) lands anywhere in ~18-30 on pure
noise, straddling the trend floor; a driftless random walk classified as "up"
on several seeds. MIN_AGREEING_FACTORS removes the single-factor calls, but
a random walk that happens to print higher highs AND rising EMAs will still
read as an uptrend, because at that point it is indistinguishable from one on
the evidence available.

So: treat a label as a description of what the tape has done, never as
evidence that it will continue. That is also why this module deliberately
emits no signals — see the note above about this repo's rejected hunts.

DATA SOURCE
-----------
Daily bars from the survivorship-complete bhavcopy archive when present
(logs/bhavcopy_archive/symbols/), else yfinance daily. The bhavcopy path is
preferred because it is point-in-time and includes since-delisted names.

RUN
---
    python -m core.market_state RELIANCE
    python -m core.market_state --universe top100 --summary
"""
from __future__ import annotations

import argparse
import logging
import os
import ssl
import sys
import warnings
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

if os.environ.get("DHAN_SSL_STRICT", "").lower() not in ("1", "true", "yes"):
    ssl._create_default_https_context = ssl._create_unverified_context
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

log = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BHAV_SYMBOL_DIR = os.path.join(_ROOT, "logs", "bhavcopy_archive", "symbols")

# ── Tunables ────────────────────────────────────────────────────────────────
ADX_LOOKBACK     = 14
ADX_TREND_FLOOR  = 20.0    # below this the tape is chop, not trend
EMA_FAST         = 20
EMA_SLOW         = 50
SWING_WINDOW     = 5       # bars each side for a confirmed swing point
MIN_BARS         = 60      # refuse to classify on less history than this

# A direction needs at least this many non-abstaining factors pointing the
# same way. Measured on mean-reverting synthetic tape, ADX(14) straddles the
# 20 floor (~18-30) purely on noise, so adx_di alone would hand back a
# confident "up" on roughly half of random seeds. Requiring corroboration
# removes the single-factor calls. It does NOT make the classifier able to
# tell a lucky random walk from a trend — see KNOWN LIMITATION below.
MIN_AGREEING_FACTORS = 2

# Factors that exist in the canonical list but cannot be measured on this
# stack. Surfaced with every classification — see module docstring.
UNAVAILABLE_FACTORS: Dict[str, str] = {
    "order_flow":     "Dhan Data API subscription expired (market_feed.py dead)",
    "bid_ask_spread": "no L1 quote source without a paid feed",
    "book_depth":     "no L2 depth source",
    "news_sentiment": "Google News RSS returns stale items (68h-1300h old)",
    "analyst_ratings": "no free source",
    "short_interest": "no free NSE equivalent",
    "options_flow":   "needs a live option-chain feed; Dhan expired",
}


@dataclass
class FactorReading:
    name: str
    vote: int            # +1 up, -1 down, 0 neutral
    detail: str          # the measurement, in words
    available: bool = True


@dataclass
class TrendState:
    symbol: str
    direction: str       # "up" | "down" | "sideways" | "unknown"
    strength: float      # 0..1 — share of available factors agreeing
    adx: float
    asof: Optional[str]
    bars: int
    factors: List[FactorReading] = field(default_factory=list)
    unavailable: Dict[str, str] = field(default_factory=lambda: dict(UNAVAILABLE_FACTORS))
    note: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["factors"] = [asdict(f) for f in self.factors]
        return d


# ── Indicator math ──────────────────────────────────────────────────────────

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _adx_di(df: pd.DataFrame, n: int = ADX_LOOKBACK):
    """Wilder ADX with +DI/-DI.

    Same formulation as core.regime_filter._adx, extended to return the
    directional indices (that helper returns only the ADX scalar).
    Returns (adx, plus_di, minus_di), any of which may be NaN.
    """
    h = df["high"].reset_index(drop=True)
    l = df["low"].reset_index(drop=True)
    c = df["close"].reset_index(drop=True)
    up = h.diff()
    dn = -l.diff()
    plus_dm  = pd.Series(np.where((up > dn) & (up > 0), up, 0.0))
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0))
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_n = tr.rolling(n, min_periods=n).mean()
    plus_di  = 100 * plus_dm.rolling(n, min_periods=n).mean()  / atr_n.replace(0, np.nan)
    minus_di = 100 * minus_dm.rolling(n, min_periods=n).mean() / atr_n.replace(0, np.nan)
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx = dx.rolling(n, min_periods=n).mean()

    def _last(s):
        s = s.dropna()
        return float(s.iloc[-1]) if len(s) else float("nan")

    return _last(adx), _last(plus_di), _last(minus_di)


def _swing_points(df: pd.DataFrame, w: int = SWING_WINDOW):
    """Confirmed swing highs/lows: an extreme with `w` lower/higher bars each side."""
    h, l = df["high"].values, df["low"].values
    highs, lows = [], []
    for i in range(w, len(df) - w):
        if h[i] == max(h[i - w:i + w + 1]):
            highs.append((i, h[i]))
        if l[i] == min(l[i - w:i + w + 1]):
            lows.append((i, l[i]))
    return highs, lows


# ── Individual factors ──────────────────────────────────────────────────────

def _f_price_structure(df: pd.DataFrame) -> FactorReading:
    highs, lows = _swing_points(df)
    if len(highs) < 2 or len(lows) < 2:
        return FactorReading("price_structure", 0, "too few confirmed swings")
    hh = highs[-1][1] > highs[-2][1]
    hl = lows[-1][1] > lows[-2][1]
    lh = highs[-1][1] < highs[-2][1]
    ll = lows[-1][1] < lows[-2][1]
    if hh and hl:
        return FactorReading("price_structure", +1, "higher high + higher low")
    if lh and ll:
        return FactorReading("price_structure", -1, "lower high + lower low")
    return FactorReading("price_structure", 0, "mixed swings (no clean structure)")


def _f_ma_alignment(df: pd.DataFrame) -> FactorReading:
    c = df["close"]
    if len(c) < EMA_SLOW + 5:
        return FactorReading("ma_alignment", 0, "insufficient bars for EMA50")
    ef, es = _ema(c, EMA_FAST), _ema(c, EMA_SLOW)
    px, f, s = float(c.iloc[-1]), float(ef.iloc[-1]), float(es.iloc[-1])
    slope = float(ef.iloc[-1] - ef.iloc[-6])
    if px > f > s and slope > 0:
        return FactorReading("ma_alignment", +1, f"close>{EMA_FAST}>{EMA_SLOW}, EMA20 rising")
    if px < f < s and slope < 0:
        return FactorReading("ma_alignment", -1, f"close<{EMA_FAST}<{EMA_SLOW}, EMA20 falling")
    return FactorReading("ma_alignment", 0, "EMAs converging / not aligned")


def _f_adx_di(adx: float, pdi: float, mdi: float) -> FactorReading:
    if not np.isfinite(adx) or not np.isfinite(pdi) or not np.isfinite(mdi):
        return FactorReading("adx_di", 0, "ADX unavailable")
    if adx < ADX_TREND_FLOOR:
        return FactorReading("adx_di", 0, f"ADX {adx:.1f} < {ADX_TREND_FLOOR:.0f} (no trend)")
    if pdi > mdi:
        return FactorReading("adx_di", +1, f"ADX {adx:.1f}, +DI {pdi:.1f} > -DI {mdi:.1f}")
    return FactorReading("adx_di", -1, f"ADX {adx:.1f}, -DI {mdi:.1f} > +DI {pdi:.1f}")


def _f_volume_confirm(df: pd.DataFrame, lookback: int = 20) -> FactorReading:
    d = df.tail(lookback)
    if len(d) < 10 or "volume" not in d or d["volume"].sum() <= 0:
        return FactorReading("volume_confirm", 0, "no usable volume")
    ret = d["close"].pct_change()
    up_v = d["volume"][ret > 0].mean()
    dn_v = d["volume"][ret < 0].mean()
    if not np.isfinite(up_v) or not np.isfinite(dn_v) or dn_v == 0:
        return FactorReading("volume_confirm", 0, "one-sided tape")
    r = up_v / dn_v
    if r >= 1.15:
        return FactorReading("volume_confirm", +1, f"up-day volume {r:.2f}x down-day")
    if r <= 0.87:
        return FactorReading("volume_confirm", -1, f"down-day volume {1/r:.2f}x up-day")
    return FactorReading("volume_confirm", 0, f"volume balanced ({r:.2f}x)")


def _f_sector(symbol: str) -> FactorReading:
    """Sector index trend via core.sector_rotation.sector_bias."""
    try:
        from core.sector_rotation import sector_bias
        bias, info = sector_bias(symbol)
    except Exception as exc:
        return FactorReading("sector", 0, f"sector unavailable: {exc}", available=False)

    sec = info.get("sector", "?")
    if bias == "unknown":
        return FactorReading("sector", 0, f"{sec}: no data", available=False)
    if bias == "bullish":
        return FactorReading("sector", +1, f"{sec} bullish")
    if bias == "bearish":
        return FactorReading("sector", -1, f"{sec} bearish")
    return FactorReading("sector", 0, f"{sec} neutral")


def _f_market() -> FactorReading:
    """Broad NIFTY/BANKNIFTY bias via core.market_bias.MarketBiasEngine.

    Note this path pulls index bars, which on the current stack means a
    yfinance round-trip; it fails soft to available=False rather than
    blocking a classification.
    """
    try:
        from core.market_bias import MarketBiasEngine
        ctx = MarketBiasEngine().get_market_bias()
    except Exception as exc:
        return FactorReading("market", 0, f"market bias unavailable: {exc}", available=False)

    # Index fetch failed and the engine fell back to synthetic bars — do not
    # let a fabricated bias count as evidence.
    if getattr(ctx, "synthetic", False):
        return FactorReading("market", 0, "index data unavailable (synthetic)",
                             available=False)

    b = str(getattr(getattr(ctx, "bias", None), "value", "")).lower()
    if not b:
        return FactorReading("market", 0, "market bias empty", available=False)
    if "long" in b:
        return FactorReading("market", +1, f"NIFTY {b}")
    if "short" in b:
        return FactorReading("market", -1, f"NIFTY {b}")
    return FactorReading("market", 0, f"NIFTY {b}")


# ── Daily bar source ────────────────────────────────────────────────────────

def _from_bhavcopy(symbol: str) -> Optional[pd.DataFrame]:
    p = os.path.join(BHAV_SYMBOL_DIR, f"{symbol.upper()}.parquet")
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
        df.index = pd.to_datetime(df.index)
        need = ("open", "high", "low", "close", "volume")
        if not all(c in df.columns for c in need):
            return None
        return df[list(need)].sort_index()
    except Exception:
        return None


def _from_yfinance(symbol: str, days: int = 400) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        from core.intraday_capture import yf_ticker
        raw = yf.Ticker(yf_ticker(symbol)).history(period=f"{days}d", interval="1d")
        if raw is None or raw.empty:
            return None
        df = raw.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                 "Close": "close", "Volume": "volume"})
        return df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    except Exception as exc:
        log.warning("[state] %s yfinance daily failed: %s", symbol, exc)
        return None


def daily_bars(symbol: str, prefer_archive: bool = True) -> Optional[pd.DataFrame]:
    """Daily OHLCV, bhavcopy archive first (point-in-time), else yfinance."""
    if prefer_archive:
        df = _from_bhavcopy(symbol)
        # The archive is only useful here if it reaches roughly the present.
        if df is not None and len(df) >= MIN_BARS:
            age = (pd.Timestamp.now().normalize() - df.index.max().normalize()).days
            if age <= 7:
                return df
    return _from_yfinance(symbol)


# ── Classification ──────────────────────────────────────────────────────────

def classify(symbol: str, df: Optional[pd.DataFrame] = None,
             with_context: bool = True) -> TrendState:
    """Classify a symbol's trend state with per-factor evidence.

    with_context=False skips the sector and market factors, which hit the
    network. Use it for batch runs where those would dominate runtime.
    """
    if df is None:
        df = daily_bars(symbol)

    if df is None or len(df) < MIN_BARS:
        n = 0 if df is None else len(df)
        return TrendState(symbol=symbol, direction="unknown", strength=0.0,
                          adx=float("nan"), asof=None, bars=n,
                          note=f"need >= {MIN_BARS} daily bars, have {n}")

    adx, pdi, mdi = _adx_di(df)

    factors = [
        _f_price_structure(df),
        _f_ma_alignment(df),
        _f_adx_di(adx, pdi, mdi),
        _f_volume_confirm(df),
    ]
    if with_context:
        factors.append(_f_sector(symbol))
        factors.append(_f_market())

    usable = [f for f in factors if f.available]
    votes = [f.vote for f in usable]
    net = sum(votes)

    # ADX gates everything: no trend strength => sideways, whatever else agrees.
    if not np.isfinite(adx) or adx < ADX_TREND_FLOOR:
        direction = "sideways"
        agree = sum(1 for v in votes if v == 0)
        note = (f"ADX {adx:.1f} below {ADX_TREND_FLOOR:.0f} — treated as range "
                f"regardless of factor alignment" if np.isfinite(adx)
                else "ADX unavailable — defaulting to sideways")
    elif net > 0 and sum(1 for v in votes if v > 0) >= MIN_AGREEING_FACTORS:
        direction = "up"
        agree = sum(1 for v in votes if v > 0)
        note = ""
    elif net < 0 and sum(1 for v in votes if v < 0) >= MIN_AGREEING_FACTORS:
        direction = "down"
        agree = sum(1 for v in votes if v < 0)
        note = ""
    elif net != 0:
        # Direction indicated, but by too thin an evidence base to call.
        direction = "sideways"
        agree = sum(1 for v in votes if v == 0)
        note = (f"only {sum(1 for v in votes if v * net > 0)} factor(s) agree, "
                f"need {MIN_AGREEING_FACTORS}")
    else:
        direction = "sideways"
        agree = sum(1 for v in votes if v == 0)
        note = "factors offset exactly"

    strength = (agree / len(usable)) if usable else 0.0

    return TrendState(
        symbol=symbol, direction=direction, strength=round(strength, 3),
        adx=round(adx, 2) if np.isfinite(adx) else float("nan"),
        asof=str(df.index.max().date()), bars=len(df),
        factors=factors, note=note,
    )


def classify_many(symbols: List[str], with_context: bool = False) -> List[TrendState]:
    out = []
    for s in symbols:
        try:
            out.append(classify(s, with_context=with_context))
        except Exception as exc:
            log.warning("[state] %s failed: %s", s, exc)
            out.append(TrendState(symbol=s, direction="unknown", strength=0.0,
                                  adx=float("nan"), asof=None, bars=0,
                                  note=f"error: {exc}"))
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────

_ARROW = {"up": "UP  ^", "down": "DOWN v", "sideways": "FLAT -", "unknown": "?????"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-symbol trend-state monitor.")
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--universe", choices=["fo", "top100"], default=None)
    ap.add_argument("--summary", action="store_true", help="One line per symbol")
    ap.add_argument("--no-context", action="store_true",
                    help="Skip sector/market factors (no network)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    symbols = list(args.symbols)
    if args.universe:
        from core.universe import FO_UNIVERSE, TOP100_LIQUID
        symbols = list(FO_UNIVERSE if args.universe == "fo" else TOP100_LIQUID)
    if not symbols:
        ap.error("give symbols or --universe")

    ctx = not args.no_context
    states = classify_many(symbols, with_context=ctx) if len(symbols) > 1 \
        else [classify(symbols[0], with_context=ctx)]

    if args.summary or len(states) > 1:
        print(f"{'SYMBOL':<14}{'STATE':<8}{'STR':>6}{'ADX':>7}  {'ASOF':<12}NOTE")
        print("-" * 78)
        for s in states:
            adx = f"{s.adx:.1f}" if s.adx == s.adx else "  -"
            print(f"{s.symbol:<14}{_ARROW[s.direction]:<8}{s.strength:>6.2f}{adx:>7}  "
                  f"{str(s.asof or '-'):<12}{s.note[:30]}")
        tally = {}
        for s in states:
            tally[s.direction] = tally.get(s.direction, 0) + 1
        print("-" * 78)
        print("  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
        return 0

    s = states[0]
    print(f"\n{s.symbol}  ->  {s.direction.upper()}   "
          f"strength {s.strength:.2f}   ADX {s.adx:.1f}   "
          f"as of {s.asof} ({s.bars} bars)")
    if s.note:
        print(f"  note: {s.note}")
    print("\n  MEASURED FACTORS")
    for f in s.factors:
        mark = {1: "+", -1: "-", 0: "."}[f.vote]
        avail = "" if f.available else "   [unavailable]"
        print(f"    [{mark}] {f.name:<18} {f.detail}{avail}")
    print("\n  NOT MEASURABLE ON THIS STACK")
    for k, v in s.unavailable.items():
        print(f"    [x] {k:<18} {v}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
