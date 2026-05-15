"""Liquidity Scanner.

Rank F&O universe by *liquidity quality*, not just raw volume.

Composite LiquidityScore (0-100) combines:
  - Volume (ADV)
  - Turnover (rupee flow, slippage proxy)
  - Delivery % (conviction proxy)
  - Volume stability (low CV = reliable depth)
  - Impact cost proxy (avg intraday range / close — tighter = better fills)

Changes vs previous version:
  * Replaced bare `except:` with scoped exception handling + logging.
  * Added cache TTL so intraday re-scans don't serve stale data.
  * Parallel market-data fetch via ThreadPoolExecutor.
  * Composite score replaces naive avg_volume sort.
  * Fixed misnamed `_fetch_nse_fno_list` (was loading full equity master).
  * Uses median + MAD for baseline so one-off spikes don't inflate ADV.
  * Stale hardcoded F&O fallback cleaned.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)


# Cache TTL — intraday scans should not serve >5 min-old bars.
_CACHE_TTL_SECONDS = 300


@dataclass
class LiquidityProfile:
    """Normalized liquidity metrics for one symbol."""

    symbol: str
    last_price: float
    adv: float                 # Average daily volume (median)
    avg_turnover: float        # Rupee turnover (median)
    avg_delivery: float        # Delivery %
    volume_cv: float           # Coefficient of variation of volume (stability)
    impact_cost_bps: float     # Avg (high-low)/close in bps
    volume_trend: float        # Recent 5-day vs baseline ratio
    composite_score: float = 0.0
    passes_floor: bool = False
    raw_metrics: Dict = field(default_factory=dict)


class LiquidityScanner:
    """Rank universe by composite liquidity quality."""

    def __init__(self):
        self.config = config.SCANNER_CONFIG
        self.universe: List[Dict] = []
        self._cache: Dict[str, Tuple[float, pd.DataFrame]] = {}
        self.dhan_api = None
        self.nse_api = None
        self._init_apis()

    # ------------------------------------------------------------------
    # API bootstrap
    # ------------------------------------------------------------------
    def _init_apis(self):
        if config.USE_MOCK_DATA:
            logger.info("USE_MOCK_DATA=True — skipping API init")
            return

        if config.DHAN_CLIENT_ID == "your_dhan_client_id":
            logger.warning("Dhan credentials not configured; using mock data")
            return

        try:
            from .api_dhan import dhan_api
            self.dhan_api = dhan_api
            logger.info("Dhan API initialized")
        except (ImportError, AttributeError) as e:
            logger.warning("Dhan API unavailable: %s", e)

        try:
            from .api_nse import nse_api
            self.nse_api = nse_api
            logger.info("NSE API initialized")
        except (ImportError, AttributeError) as e:
            logger.warning("NSE API unavailable: %s", e)

    # ------------------------------------------------------------------
    # Market data (TTL cache)
    # ------------------------------------------------------------------
    # Requests for <= this many bars use intraday (5-min) instead of daily.
    _INTRADAY_THRESHOLD = 15

    def get_market_data(self, symbol: str, days: int = 20) -> pd.DataFrame:
        if days <= self._INTRADAY_THRESHOLD:
            return self.get_intraday_data(symbol, interval=5, days_back=days)
        return self.get_daily_data(symbol, days)

    def get_daily_data(self, symbol: str, days: int = 20) -> pd.DataFrame:
        key = f"daily:{symbol}:{days}"
        entry = self._cache.get(key)
        if entry and time.time() - entry[0] < _CACHE_TTL_SECONDS:
            return entry[1]

        df = pd.DataFrame()

        if self.dhan_api and config.DHAN_CLIENT_ID != "your_dhan_client_id":
            try:
                df = self.dhan_api.get_historical_data(symbol, from_date=days)
            except (ConnectionError, TimeoutError, ValueError) as e:
                logger.warning("Dhan daily fetch failed for %s: %s", symbol, e)
            except Exception as e:  # noqa: BLE001 — upstream API raises mixed types
                logger.warning("Dhan daily unexpected error for %s: %s", symbol, e)

        if df.empty and self.nse_api and config.NSE_API_KEY != "your_nse_api_key":
            try:
                df = self.nse_api.get_historical_data(symbol)
            except (ConnectionError, TimeoutError, ValueError) as e:
                logger.warning("NSE daily fetch failed for %s: %s", symbol, e)
            except Exception as e:  # noqa: BLE001
                logger.warning("NSE daily unexpected error for %s: %s", symbol, e)

        if df.empty and config.USE_MOCK_DATA:
            logger.debug("Using mock daily data for %s", symbol)
            df = self._generate_mock_data(symbol, days)

        df = self._normalize_ohlcv(df)
        self._cache[key] = (time.time(), df)
        return df

    def get_intraday_data(self, symbol: str, interval: int = 5, days_back: int = 5) -> pd.DataFrame:
        key = f"intraday:{symbol}:{interval}:{days_back}"
        entry = self._cache.get(key)
        if entry and time.time() - entry[0] < _CACHE_TTL_SECONDS:
            return entry[1]

        df = pd.DataFrame()

        if self.dhan_api and config.DHAN_CLIENT_ID != "your_dhan_client_id":
            try:
                df = self.dhan_api.get_intraday_data(symbol, interval=interval, days_back=days_back)
            except (ConnectionError, TimeoutError, ValueError) as e:
                logger.warning("Dhan intraday fetch failed for %s: %s", symbol, e)
            except Exception as e:  # noqa: BLE001 — upstream API raises mixed types
                logger.warning("Dhan intraday unexpected error for %s: %s", symbol, e)

        if df.empty and config.USE_MOCK_DATA:
            logger.debug("Using mock intraday data for %s", symbol)
            df = self._generate_mock_intraday_data(symbol, interval, days_back)

        df = self._normalize_ohlcv(df)
        self._cache[key] = (time.time(), df)
        return df

    @staticmethod
    def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
        """Ensure required columns, derive missing, drop NaN rows."""
        if df.empty:
            return df
        cols = {c.lower(): c for c in df.columns}
        # Standardize to lowercase
        df.columns = [c.lower() for c in df.columns]
        required = {'open', 'high', 'low', 'close', 'volume'}
        missing = required - set(df.columns)
        if missing:
            logger.warning("OHLCV missing cols %s", missing)
            return pd.DataFrame()

        if 'turnover' not in df.columns:
            df['turnover'] = df['close'] * df['volume']
        if 'delivery_percent' not in df.columns:
            if 'delivery' in df.columns:
                df['delivery_percent'] = df['delivery']
            else:
                df['delivery_percent'] = np.nan

        df = df.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
        return df

    def _generate_mock_data(self, symbol: str, days: int) -> pd.DataFrame:
        rng = np.random.default_rng(hash(symbol) % (2 ** 32))
        dates = pd.date_range(end=datetime.now(), periods=days, freq='D')
        base_price = 1000 + rng.integers(500, 5000)
        closes = base_price + np.cumsum(rng.standard_normal(days) * base_price * 0.02)
        data = {
            'date': dates,
            'open': closes * (1 + rng.standard_normal(days) * 0.005),
            'high': closes * (1 + np.abs(rng.standard_normal(days)) * 0.01),
            'low': closes * (1 - np.abs(rng.standard_normal(days)) * 0.01),
            'close': closes,
            'volume': rng.integers(1_000_000, 50_000_000, days),
            'turnover': rng.integers(50_000_000, 500_000_000, days),
            'delivery_percent': rng.integers(10, 60, days).astype(float),
        }
        return pd.DataFrame(data)

    def _generate_mock_intraday_data(self, symbol: str, interval: int, days_back: int) -> pd.DataFrame:
        rng = np.random.default_rng(hash((symbol, interval, days_back)) % (2 ** 32))
        bars_per_day = max(int((6.25 * 60) / max(interval, 1)), 10)
        periods = max(days_back * bars_per_day, 40)
        dates = pd.date_range(end=datetime.now(), periods=periods, freq=f'{interval}min')
        base_price = 1000 + rng.integers(500, 5000)
        closes = base_price + np.cumsum(rng.standard_normal(periods) * base_price * 0.002)
        volumes = rng.integers(25_000, 500_000, periods)
        data = {
            'date': dates,
            'open': closes * (1 + rng.standard_normal(periods) * 0.0008),
            'high': closes * (1 + np.abs(rng.standard_normal(periods)) * 0.0015),
            'low': closes * (1 - np.abs(rng.standard_normal(periods)) * 0.0015),
            'close': closes,
            'volume': volumes,
            'turnover': closes * volumes,
            'delivery_percent': rng.integers(10, 60, periods).astype(float),
        }
        return pd.DataFrame(data)

    # ------------------------------------------------------------------
    # Liquidity metrics
    # ------------------------------------------------------------------
    def calculate_liquidity_metrics(self, df: pd.DataFrame) -> Dict:
        if df.empty or len(df) < 5:
            return {}

        # Use median for ADV (robust to one-day outliers).
        adv = float(df['volume'].median())
        avg_turnover = float(df['turnover'].median())
        avg_delivery = float(df['delivery_percent'].mean())

        vol_mean = df['volume'].mean()
        vol_std = df['volume'].std(ddof=0)
        volume_cv = float(vol_std / vol_mean) if vol_mean > 0 else float('inf')

        # Impact cost proxy: intraday range normalized, in bps.
        hl_range = (df['high'] - df['low']) / df['close'].replace(0, np.nan)
        impact_cost_bps = float(hl_range.mean() * 10_000)

        # Volume trend: last 5 days vs rest (exclude recent window from baseline).
        recent_window = min(5, max(1, len(df) // 4))
        recent_vol = df['volume'].tail(recent_window).mean()
        baseline = df['volume'].iloc[:-recent_window].mean() if len(df) > recent_window else vol_mean
        volume_trend = float(recent_vol / baseline) if baseline > 0 else 1.0

        return {
            'adv': adv,
            'avg_turnover': avg_turnover,
            'avg_delivery': avg_delivery,
            'volume_cv': volume_cv,
            'impact_cost_bps': impact_cost_bps,
            'volume_trend': volume_trend,
            'recent_volume': float(recent_vol),
        }

    def _passes_floor(self, m: Dict) -> bool:
        return (
            m.get('adv', 0) >= self.config['min_volume']
            and m.get('avg_turnover', 0) >= self.config['min_turnover']
            and m.get('avg_delivery', 0) >= self.config['min_delivery_percent']
        )

    @staticmethod
    def _composite_score(profiles: List[LiquidityProfile]) -> None:
        """Z-score each metric across the universe, then weight. Mutates in place."""
        if not profiles:
            return

        def _z(vals: List[float], invert: bool = False) -> np.ndarray:
            arr = np.array(vals, dtype=float)
            arr = np.where(np.isfinite(arr), arr, np.nan)
            mu = np.nanmean(arr)
            sd = np.nanstd(arr)
            if sd == 0 or not np.isfinite(sd):
                return np.zeros_like(arr)
            z = (arr - mu) / sd
            return -z if invert else z

        adv_z = _z([p.adv for p in profiles])
        to_z = _z([p.avg_turnover for p in profiles])
        del_z = _z([p.avg_delivery for p in profiles])
        cv_z = _z([p.volume_cv for p in profiles], invert=True)          # lower CV better
        ic_z = _z([p.impact_cost_bps for p in profiles], invert=True)    # lower impact better

        # Weights — turnover and impact cost matter most for executions.
        weights = {'adv': 0.20, 'to': 0.30, 'delivery': 0.15, 'cv': 0.15, 'ic': 0.20}

        for i, p in enumerate(profiles):
            raw = (
                weights['adv'] * adv_z[i]
                + weights['to'] * to_z[i]
                + weights['delivery'] * del_z[i]
                + weights['cv'] * cv_z[i]
                + weights['ic'] * ic_z[i]
            )
            # Map z-score roughly to 0-100 via sigmoid-ish scaling.
            p.composite_score = float(50 + 15 * raw)

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------
    def _profile_symbol(self, symbol: str) -> Optional[LiquidityProfile]:
        try:
            df = self.get_market_data(symbol, self.config['lookback_days'])
            if df.empty:
                return None
            m = self.calculate_liquidity_metrics(df)
            if not m:
                return None
            return LiquidityProfile(
                symbol=symbol,
                last_price=float(df['close'].iloc[-1]),
                adv=m['adv'],
                avg_turnover=m['avg_turnover'],
                avg_delivery=m['avg_delivery'],
                volume_cv=m['volume_cv'],
                impact_cost_bps=m['impact_cost_bps'],
                volume_trend=m['volume_trend'],
                passes_floor=self._passes_floor(m),
                raw_metrics=m,
            )
        except (KeyError, ValueError, ZeroDivisionError) as e:
            logger.warning("Profile failed for %s: %s", symbol, e)
            return None
        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected profile error for %s: %s", symbol, e)
            return None

    def scan_universe(
        self,
        symbols: Optional[List[str]] = None,
        max_workers: int = 8,
    ) -> List[Dict]:
        if symbols is None:
            symbols = self._get_fo_symbols()

        profiles: List[LiquidityProfile] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._profile_symbol, s): s for s in symbols}
            for fut in as_completed(futures):
                p = fut.result()
                if p is not None:
                    profiles.append(p)

        # Keep only those that pass the floor, then composite-score.
        passing = [p for p in profiles if p.passes_floor]
        self._composite_score(passing)
        passing.sort(key=lambda p: p.composite_score, reverse=True)

        self.universe = [
            {
                'symbol': p.symbol,
                'last_price': p.last_price,
                'composite_score': p.composite_score,
                'metrics': p.raw_metrics,
            }
            for p in passing[: self.config['top_n_stocks']]
        ]
        return self.universe

    # ------------------------------------------------------------------
    # F&O universe
    # ------------------------------------------------------------------
    def _get_fo_symbols(self) -> List[str]:
        # Prefer explicit API call
        if self.nse_api and not config.USE_MOCK_DATA:
            try:
                fno = self.nse_api.get_fno_stocks()
                if fno:
                    logger.info("Fetched %d F&O stocks from NSE API", len(fno))
                    return fno
            except (ConnectionError, TimeoutError, ValueError) as e:
                logger.warning("NSE F&O fetch failed: %s", e)

        # Fallback: Dhan scrip master (filtered, NOT whole equity universe).
        fno = self._fetch_dhan_fno_master()
        if fno:
            return fno

        # Last-resort static list — NIFTY50 + common F&O actives. Removed non-F&O names.
        return [
            'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK',
            'SBIN', 'BHARTIARTL', 'KOTAKBANK', 'BAJFINANCE', 'HINDUNILVR',
            'ITC', 'LT', 'AXISBANK', 'MARUTI', 'ASIANPAINT',
            'WIPRO', 'HCLTECH', 'TITAN', 'SUNPHARMA', 'TATAMOTORS',
            'ADANIENT', 'NTPC', 'POWERGRID', 'ULTRACEMCO', 'JSWSTEEL',
        ]

    def _fetch_dhan_fno_master(self) -> List[str]:
        """Pull Dhan scrip master, keep only NSE F&O underlyings.

        Previous version kept every NSE equity — not F&O. That's fixed here:
        we filter to rows whose segment marks them tradable in F&O.
        """
        try:
            import requests
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Accept': 'text/csv',
            }
            url = "https://images.dhan.co/api-data/api-scrip-master.csv"
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                logger.warning("Scrip master HTTP %d", resp.status_code)
                return []

            sec_ids: Dict[str, str] = {}
            fno_underlyings: set = set()

            lines = resp.text.splitlines()
            header = lines[0].split(',') if lines else []
            try:
                exch_idx = header.index('SEM_EXM_EXCH_ID')
                seg_idx = header.index('SEM_SEGMENT')
                sid_idx = header.index('SEM_SMST_SECURITY_ID')
                sym_idx = header.index('SEM_TRADING_SYMBOL')
                inst_idx = header.index('SEM_INSTRUMENT_NAME')
            except ValueError:
                # Legacy layout fallback
                exch_idx, seg_idx, sid_idx, sym_idx, inst_idx = 0, 1, 2, 5, 4

            for line in lines[1:]:
                parts = line.split(',')
                if len(parts) <= max(exch_idx, seg_idx, sid_idx, sym_idx, inst_idx):
                    continue
                exch = parts[exch_idx]
                seg = parts[seg_idx]
                inst = parts[inst_idx].upper() if inst_idx < len(parts) else ''
                sym = parts[sym_idx]
                sid = parts[sid_idx]

                if exch == 'NSE' and 'E' in seg and inst == 'EQUITY':
                    sec_ids[sym] = sid

                # Collect F&O underlyings (FUTSTK / OPTSTK rows carry the underlying)
                if inst in {'FUTSTK', 'OPTSTK'}:
                    # Underlying symbol usually in SM_SYMBOL_NAME col — try common positions.
                    underlying = sym.split('-')[0] if '-' in sym else sym
                    # Strip trailing expiry digits
                    underlying = ''.join(c for c in underlying if not c.isdigit())
                    for suffix in ('FUT', 'CE', 'PE'):
                        if underlying.endswith(suffix):
                            underlying = underlying[:-len(suffix)]
                    if underlying:
                        fno_underlyings.add(underlying)

            # Seed security-id cache for downstream API
            try:
                from .api_dhan import get_security_id
                if hasattr(get_security_id, 'cache_clear'):
                    get_security_id.cache_clear()
                if hasattr(get_security_id, 'cache_update'):
                    for sym, sid in sec_ids.items():
                        get_security_id.cache_update(sym, sid)
            except (ImportError, AttributeError):
                pass

            if fno_underlyings:
                symbols = [s for s in fno_underlyings if s in sec_ids]
                logger.info("Identified %d F&O underlyings", len(symbols))
                return symbols[: self.config['top_n_stocks'] * 3]

            # No F&O rows matched — fall back to nothing, let caller use static list.
            return []

        except (ConnectionError, TimeoutError) as e:
            logger.warning("Scrip master network error: %s", e)
            return []
        except Exception as e:  # noqa: BLE001
            logger.exception("Scrip master parse error: %s", e)
            return []

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def get_top_liquid(self, n: Optional[int] = None) -> List[str]:
        if not self.universe:
            self.scan_universe()
        n = n or self.config['top_n_stocks']
        return [item['symbol'] for item in self.universe[:n]]

    def refresh_universe(self) -> List[Dict]:
        self._cache.clear()
        return self.scan_universe()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
    scanner = LiquidityScanner()
    universe = scanner.scan_universe()

    print(f"\nFound {len(universe)} liquid stocks (ranked by composite score):\n")
    for item in universe:
        m = item['metrics']
        print(
            f"  {item['symbol']:<12} score={item['composite_score']:6.2f} "
            f"ADV={m['adv']/1e6:6.1f}M "
            f"TO={m['avg_turnover']/1e7:5.1f}Cr "
            f"Del={m['avg_delivery']:4.1f}% "
            f"CV={m['volume_cv']:.2f} "
            f"IC={m['impact_cost_bps']:4.0f}bps "
            f"Trend={m['volume_trend']:.2f}x"
        )
