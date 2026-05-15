"""
MarketContextAgent — tracks NIFTY/BANKNIFTY regime every 5 minutes.

Regime classification:
  bull:     NIFTY intraday +0.3%+ and VIX proxy < 16
  bear:     NIFTY intraday -0.3%- OR VIX proxy > 22
  volatile: VIX proxy > 25
  neutral:  everything else

Sets SharedState.market_regime.
Publishes REGIME_CHANGED when regime flips.
"""

import logging
import pandas as pd
from core.agents.base_agent import BaseAgent
from core.agent_bus import SharedState, EventBus

log = logging.getLogger(__name__)


class MarketContextAgent(BaseAgent):
    name = "market_context"
    interval_sec = 300   # 5 minutes

    BULL_MIN   =  0.3    # % intraday gain
    BEAR_MAX   = -0.3    # % intraday drop
    VIX_BEAR   = 22.0
    VIX_VOLATILE = 25.0
    VIX_LOW    = 16.0

    def __init__(self, state: SharedState, bus: EventBus, scanner, feed=None):
        super().__init__(state, bus)
        self.scanner = scanner
        self.feed = feed   # DhanMarketFeed — live NIFTY/BANKNIFTY when available
        self._prev_regime = "unknown"
        self._nifty_open: float = 0.0     # set once at market open
        self._bnf_open:   float = 0.0

    def run(self) -> None:
        regime, nifty_chg, bnf_chg, vix = self._classify()
        self.state.set(
            market_regime=regime,
            nifty_change_pct=nifty_chg,
            banknifty_change_pct=bnf_chg,
            vix_proxy=vix,
        )
        if regime != self._prev_regime:
            log.info(f"[Context] regime: {self._prev_regime} -> {regime} "
                     f"| NIFTY {nifty_chg:+.2f}% | VIX~{vix:.1f}")
            self.emit("REGIME_CHANGED", {"regime": regime, "nifty_pct": nifty_chg, "vix": vix})
            self._prev_regime = regime

    @staticmethod
    def _session_frame(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty or 'date' not in df.columns:
            return pd.DataFrame()
        try:
            dt = pd.to_datetime(df['date'], errors='coerce')
            if dt.isna().all():
                return pd.DataFrame()
            latest_day = dt.dt.date.max()
            return df.loc[dt.dt.date == latest_day].reset_index(drop=True)
        except Exception:
            return pd.DataFrame()

    def _classify(self):
        try:
            nifty_df = self.scanner.get_intraday_data("NIFTY", interval=5, days_back=2)
            nifty_chg = 0.0
            nifty_session = self._session_frame(nifty_df)
            if not nifty_session.empty:
                # Seed open price once
                if self._nifty_open == 0.0:
                    self._nifty_open = float(nifty_session['open'].iloc[0])

                # Prefer live WebSocket LTP over last REST bar
                live_nifty = self.feed.get_ltp("NIFTY") if self.feed else None
                current_price = live_nifty if live_nifty else float(nifty_session['close'].iloc[-1])
                ref_price = self._nifty_open if self._nifty_open > 0 else float(nifty_session['open'].iloc[0])
                nifty_chg = (current_price - ref_price) / ref_price * 100

            vix = self._vix_proxy(nifty_session)

            bnf_df = self.scanner.get_intraday_data("BANKNIFTY", interval=5, days_back=2)
            bnf_chg = 0.0
            bnf_session = self._session_frame(bnf_df)
            if not bnf_session.empty:
                if self._bnf_open == 0.0:
                    self._bnf_open = float(bnf_session['open'].iloc[0])
                live_bnf = self.feed.get_ltp("BANKNIFTY") if self.feed else None
                current_bnf = live_bnf if live_bnf else float(bnf_session['close'].iloc[-1])
                ref_bnf = self._bnf_open if self._bnf_open > 0 else float(bnf_session['open'].iloc[0])
                bnf_chg = (current_bnf - ref_bnf) / ref_bnf * 100

            if vix > self.VIX_VOLATILE:
                regime = "volatile"
            elif nifty_chg >= self.BULL_MIN and vix < self.VIX_LOW:
                regime = "bull"
            elif nifty_chg <= self.BEAR_MAX or vix > self.VIX_BEAR:
                regime = "bear"
            else:
                regime = "neutral"

            return regime, nifty_chg, bnf_chg, vix

        except Exception as e:
            log.debug(f"[Context] fetch error: {e}")
            return "neutral", 0.0, 0.0, 15.0

    def _vix_proxy(self, df) -> float:
        """Estimate realized volatility as VIX proxy when India VIX not available."""
        try:
            if df is None or len(df) < 5:
                return 15.0
            # 5-bar ATR as % of close, annualized
            atr = (df['high'] - df['low']).rolling(5).mean().iloc[-1]
            pct = float(atr / df['close'].iloc[-1] * 100)
            return min(max(pct * 12, 8.0), 50.0)  # rough annualization
        except Exception:
            return 15.0

    def direction_ok(self, direction: str) -> bool:
        """Check regime alignment. Both directions always allowed (unbiased).
        Volatile = pause. Counter-trend = OK but caller should reduce size."""
        regime = self.state.get("market_regime", "neutral")
        if regime == "volatile":
            return False
        # No directional blocking — system treats longs and shorts equally.
        # Counter-trend sizing handled by RiskAgent (0.6x multiplier).
        return True

    def is_counter_trend(self, direction: str) -> bool:
        """Check if trade goes against regime — for sizing, not blocking."""
        regime = self.state.get("market_regime", "neutral")
        return (
            (regime in ("bull", "strong_bull") and direction == "short") or
            (regime in ("bear", "strong_bear") and direction == "long")
        )

    def size_multiplier(self, direction: str = None) -> float:
        regime = self.state.get("market_regime", "neutral")
        base = {"bull": 1.0, "bear": 1.0, "neutral": 0.85, "volatile": 0.0}.get(regime, 0.85)
        if direction and self.is_counter_trend(direction):
            return base * 0.6  # reduced but not zero
        return base
