"""
config.example.py — the complete default configuration.

    cp config.example.py config.py     # then edit

`config.py` is gitignored and never committed. This example is a full,
runnable copy of the defaults: every threshold the engine uses lives here,
nothing is hardcoded in core/.

Credentials are read from environment variables (or the OS keyring via
core/secrets.py) — do NOT paste live tokens into this file.

Safety: PAPER_TRADE defaults to True. Leave it that way unless you are
consciously going live with real money.
"""

import os

# Indian F&O Trading System Configuration

# Market Data APIs
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")

# NSE API (FYERS or similar)
NSE_API_KEY = "your_nse_api_key"
NSE_API_SECRET = "your_nse_api_secret"

# Set to False to use real API data
USE_MOCK_DATA = False

# Paper-trade mode: real market data, simulated order fills (no real orders)
PAPER_TRADE = True

# Refuse F&O trades in names below the measured liquidity floor for the
# instrument (core/selection.py: options >= Rs 500 Cr/day, futures >= 300 Cr).
# Options are quoted per STRIKE, so their spreads widen far faster than the
# underlying's as turnover falls -- an option on a Rs 261 Cr/day name is a
# different instrument from one on HDFCBANK.
# This NARROWS the tradeable set (151 F&O names -> 40 futures / 16 options).
# Set False to size on the full universe and keep the older behaviour.
# Absent from config.py, the code defaults to True.
ENFORCE_LIQUIDITY_TIER = True

# Honest paper-fill cost (basis points). A paper fill is priced off the LIVE
# quote and CROSSES the spread + slippage by this many bps — never the requested
# price (that perfect-fill assumption is the mirage that inflated old results).
# BUY fills at ref*(1+bps/1e4), SELL at ref*(1-bps/1e4). Options are wider.
PAPER_FILL_COST_BPS = {"equity": 5, "futures": 5, "option": 50}

# Instrument mode: how the directional view is expressed.
#   "futures" — trade the stock FUTURE (delta~1, no theta/IV/strike). DEFAULT.
#   "options" — buy ATM/ITM option leg (legacy; theta+IV+spread pollute edge).
# 16-day live journal proof (911 trades): on SL hits the SPOT moved only
# -0.60% median but the OPTION premium lost -9.56% — i.e. stops were firing on
# premium decay, not price. 15% of trades had the direction RIGHT yet the
# option still lost. The backtested edge (PF 1.17) was measured on SPOT, so
# futures express it cleanly. Override with env INSTRUMENT_MODE.
INSTRUMENT_MODE = os.getenv("INSTRUMENT_MODE", "futures").lower()

# Round-trip cost (% of notional) subtracted from every closed-trade P&L so
# the journal + metrics are NET, not gross. Futures all-in: STT (sell-side
# 0.0125-0.02%), exchange txn, GST, stamp, SEBI + brokerage, plus half-spread
# slippage each way ~= 0.06% on liquid names. Raise for illiquid stocks.
FUT_COST_ROUNDTRIP_PCT = float(os.getenv("FUT_COST_ROUNDTRIP_PCT", "0.06"))

# FIX (audit #9): the earnings calendar feed is currently dead (returns no
# data), so the earnings-blackout gate cannot protect against earnings gaps.
# When True, the gate FAILS CLOSED (blocks entries it cannot verify) instead of
# trading blind. Default False to preserve current behavior; set True for live
# capital once an earnings feed is wired, or to be safe-by-default meanwhile.
REQUIRE_EARNINGS_DATA = os.getenv("REQUIRE_EARNINGS_DATA", "0") == "1"

# FIX (audit #4): india_swing is a daily-close swing strategy and must not be
# re-evaluated every 15s on an incomplete daily bar. The scanner generates it
# once per trading day and holds. None = decide on the first scan of the day;
# (15, 15) = wait until ~the close so the daily bar is complete (most correct).
ISWING_DECISION_TIME = None  # e.g. (15, 15) to decide on the close

# Advanced gates G6/G8/G9/G10 wrap their module calls in try/except. When True,
# a gate that ERRORS fails CLOSED (skips the trade) instead of silently passing
# — safer for live capital. Default False keeps current behavior; either way
# the failure is now logged LOUD (warning), not a silent debug line.
GATES_FAIL_CLOSED = os.getenv("GATES_FAIL_CLOSED", "0") == "1"

# Scanner Settings
SCANNER_CONFIG = {
    "min_volume": 1000000,        # Min avg volume
    "min_turnover": 5000000,      # Min turnover
    "min_delivery_percent": 10,   # Min delivery %
    "top_n_stocks": 15,           # Number of stocks to trade
    "lookback_days": 30,          # Days to calculate avg volume
}

# Signal Engine Settings
SIGNAL_CONFIG = {
    "atr_period": 14,
    "atr_multiplier": 1.5,
    "volume_multiplier": 1.5,
    "range_period": 20,
    # Enhanced signal parameters
    "ema_fast": 9,
    "ema_slow": 21,
    "rsi_period": 14,
    "rsi_oversold": 38,
    "rsi_overbought": 70,
    "supertrend_period": 10,
    "supertrend_multiplier": 3.0,
    "nr7_lookback": 7,
    "min_votes": 5,              # Raised from 4. More confluence = higher WR. Data shows vote_margin 7+ wins
    "min_vote_lead": 3,          # Directional lead must be clear
    "vol_surge_threshold": 1.5,
    "rsi_extreme_oversold": 30,
    "rsi_extreme_overbought": 75,
    "min_strength": 55,          # Raised from 50. Only high-confidence signals pass
    "rsi_long_momentum_min": 62, # Raised from 58. RSI 60+ = 40-42% WR vs <60 = 18-23% WR
    "rsi_short_floor": 30,       # Only block shorts in extreme oversold (bounce risk). 30-50 = bearish momentum zone, shorts SHOULD work there
    "disable_shorts": False,     # Both directions enabled. Short WR fixed via proper MTF + RSI filters
    "rr_ratio": 4.0,             # T2 runner target for 50%+ premium gain. T1 partial at 1.5R.
    # Time-based filtering
    "block_after_hour": 12,      # 12:00+ trades = low WR. Morning-only trading
    "skip_first_minutes": 15,    # Skip 09:15-09:30 opening noise (false breakouts)
    "best_hours": [10, 11],      # 10:00 = 67% WR. Boost signals in these hours
    # Pattern blacklist: direction-neutral 0% WR patterns only.
    # Do NOT blacklist short-specific or long-specific patterns — keeps system unbiased.
    "blacklisted_patterns": [
        "range_squeeze_down",        # 0% WR regardless of direction
    ],
    # Volume filter (counterintuitive: high vol ratio = LOSSES)
    "max_volume_ratio": 3.0,     # Wins avg vol_ratio=0.96, Losses=1.70. Cap at 3x to avoid chasing
}

# Fake Breakout Filter Settings
FILTER_CONFIG = {
    "min_candle_size": 0.5,      # ATR multiplier
    "follow_thresh": 1.2,         # Follow-through threshold
    "rejection_wick_ratio": 0.6, # Max wick/body ratio
}

# Order Flow Settings
ORDER_FLOW_CONFIG = {
    "absorption_wick_ratio": 3,    # Upper wick must be Nx body to count as rejection candle
    "min_absorption_count": 3,     # Min rejection candles in 10-bar window = absorption signal
    "exhaustion_volume_ratio": 2.5, # 5-bar vol avg / 10-bar rolling avg — higher than entry to reduce false positives
    "cluster_threshold": 3,
}

# Strike Selection
STRIKE_CONFIG = {
    "itm_delta": 0.3,           # ITM delta threshold
    "otm_delta": 0.15,          # OTM delta threshold
    "spot_otm_multiplier": 0.02, # % from spot for OTM
}

# Risk Engine
RISK_CONFIG = {
    "max_risk_per_trade": 0.012, # 1.2% capital per trade (tighter - preserve capital)
    "sl_pct": 0.05,              # Fallback only when ATR missing; multi-TF SL is primary
    "atr_sl_multiplier": 1.2,    # Reduced from 1.5. SL 0.5-1% = 38% WR (sweet spot). Tighter ATR mult
    "min_sl_pct": 0.010,         # 1.0% floor — structural room for noise on F&O stocks
    "max_sl_pct": 0.025,         # 2.5% ceiling — allows capturing 3.5-5% moves with 3.5R target
    "max_daily_loss": 0.04,      # 4% max daily loss (tighter capital protection)
    "max_consecutive_losses": 2, # Stop after 2 consecutive losses (preserve capital for good setups)
    "max_trades_per_day": 3,     # Reduced from 4. Fewer, higher-quality trades
    "min_risk_reward": 1.5,      # Minimum 1.5R to enter — ensures decent premium gain
    "max_per_sector": 2,         # audit #17: cap simultaneous correlated positions per sector
    # GAP #4: peak-to-trough intraday drawdown halt.
    # When (peak_equity - current_equity) / capital exceeds this ratio, no new
    # trades are opened for the rest of the session. Realized + unrealized.
    # 8% is ~2× max_daily_loss — a second layer that catches a bad run BEFORE the
    # daily loss cap is hit on a single position. Set to 0 to disable.
    "max_drawdown_halt": 0.08,
    # GAP #3: global concurrent-open-positions ceiling.
    # max_per_sector=2 across ~8 sectors still permits ~16 concurrent; the
    # realized book ran ~9 with no ceiling. Cap the total book size regardless
    # of sector. Set to 0 to disable (reverts to sector-only enforcement).
    "max_concurrent_positions": 5,
    # GAP #3: per-expiry concentration cap.
    # N positions on the same weekly/monthly expiry share ONE gap/pin event —
    # this is the dangerous unmodeled blow-up for an options book. Cap positions
    # per expiry date. Futures/equity positions carry no expiry (blank string)
    # and are never blocked by this check. Set to 0 to disable.
    "max_per_expiry": 3,
}

# Volume-based exit (book profit when liquidity dries up)
VOLUME_EXIT_CONFIG = {
    "entry_min_vol_ratio": 2.0,   # entry requires 2x 20d avg volume (no fake breakouts)
    "exit_vol_threshold": 0.50,   # exit when volume drops to 50% of 20d avg — genuine drying up
    "profit_required_pct": 0.015, # min 1.5% profit before volume exit triggers (0.5% was too small)
    "vol_surge_lookback": 20,     # rolling window for avg volume
}

# Execution Settings
EXECUTION_CONFIG = {
    "order_type": "LIMIT",
    "slippage_tolerance": 0.001, # 0.1%
    "retry_attempts": 3,
    "retry_delay": 2,            # Seconds
}

# AI Filter
AI_CONFIG = {
    "model_path": "models/probability_model.pkl",
    "high_prob_threshold": 0.7,
    "medium_prob_threshold": 0.5,
    "low_possize_multiplier": 0.5,
}

# Trading Hours (IST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30

# Logging
LOG_CONFIG = {
    "level": "INFO",
    "file": "logs/trading.log",
    "console": True,
}

# Volatility regime thresholds and NSE strike step
VOLATILITY_CONFIG = {
    'low_thresh': 0.5,   # avoid trading when ATR < 50% of median — truly dead market
    'high_thresh': 2.5,  # cautious when ATR > 2.5x median — genuine high-vol, not normal variation
    'nse_strike_step': 50,
}

# Options / Black-Scholes parameters
OPTIONS_CONFIG = {
    'risk_free_rate': 0.065,
    'iv_high_threshold': 1.3,
    'iv_low_threshold': 0.7,
}

# NSE F&O lot sizes (shares per contract) — used for position sizing.
# FALLBACK ONLY: core.scrip_master.lot_size() is the LIVE source of truth (NSE
# revises lots periodically, so these go stale). Index lots updated to 2024+
# values; verify against the scrip master, which always wins.
NSE_LOT_SIZES = {
    "NIFTY": 75, "BANKNIFTY": 35, "FINNIFTY": 65, "MIDCPNIFTY": 120,
    "RELIANCE": 250, "TCS": 150, "INFY": 300, "HDFCBANK": 550,
    "ICICIBANK": 700, "SBIN": 1500, "BHARTIARTL": 500, "KOTAKBANK": 400,
    "BAJFINANCE": 125, "HINDUNILVR": 300, "ITC": 1600, "LT": 150,
    "AXISBANK": 625, "MARUTI": 100, "ASIANPAINT": 200, "WIPRO": 1000,
    "HCLTECH": 350, "TITAN": 175, "SUNPHARMA": 350, "TATAMOTORS": 1425,
    "ADANIENT": 250, "NTPC": 2250, "POWERGRID": 2700, "ULTRACEMCO": 100,
    "JSWSTEEL": 675, "ONGC": 1925, "COALINDIA": 2100, "BPCL": 1800,
    "HEROMOTOCO": 150, "EICHERMOT": 50, "TATASTEEL": 1425, "HINDALCO": 700,
    "GRASIM": 175, "CIPLA": 650, "DRREDDY": 125, "DEEPAKNT": 200,
    # Extended lot sizes for sector coverage stocks
    "BAJAJ-AUTO":  75,    "FORCEMOT":    150,
    "KPIT":       400,    "KPITTECH":   400,    "BOSCH":       50,
    "MOTHERSON": 5000,
    "BHEL":      3200,    "BEL":        3500,    "HAL":         150,
    "NALCO":     7200,    "VEDANTA":    2000,    "VEDL":        2000,
    "ADANIGREEN":  350,   "TATAPOWER":  3375,    "WAAREE":       50,
    "CHOLAFIN":  1000,    "MUTHOOTFIN":  400,
    "MOTILALOFS":  280,   "ANGELONE":    300,    "ICICISEC":    350,
    "SAMMAAN":   5000,    "SAMMAANCAP": 5000,
}

# Telegram notifications
# Get bot token: message @BotFather on Telegram → /newbot
# Get chat ID: message @userinfobot on Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# FIX (audit #6): in-session self-tuning was overfitting to the (biased) journal
# every 10 scans — chasing noise with real param mutations mid-trading. FROZEN
# until a genuine edge is validated by backtest_live_pipeline.py. Set True only
# after that, and only to refit OFFLINE with a strict out-of-sample holdout.
LEARNING_ENABLED = False

# Auto-Repair Engine
AUTO_REPAIR_CONFIG = {
    'enabled': False,                 # audit #6: frozen — see LEARNING_ENABLED above
    'adaptation_interval': 10,        # adapt params every N pipeline cycles
    'min_samples_for_adaptation': 5,  # need at least N cycles before first adapt
    'history_file': 'logs/repair_history.json',
    'print_health_every': 5,          # print health report every N cycles
}

# Optional FastAPI sidecar for dashboard / automation integrations
DASHBOARD_API_HOST = os.getenv("DASHBOARD_API_HOST", "127.0.0.1")
DASHBOARD_API_PORT = int(os.getenv("DASHBOARD_API_PORT", "8000"))
DASHBOARD_API_URL = os.getenv("DASHBOARD_API_URL", "")

# Stock → Sector mapping for sector momentum scoring in trade_ranker
STOCK_SECTOR_MAP = {
    'BAJAJ-AUTO': 'AUTO',   'MARUTI': 'AUTO',       'TATAMOTORS': 'AUTO',
    'HEROMOTOCO': 'AUTO',   'EICHERMOT': 'AUTO',    'FORCEMOT': 'AUTO',
    'KPIT':       'AUTO_TECH', 'BOSCH': 'AUTO_TECH', 'MOTHERSON': 'AUTO_TECH',
    'BHEL':       'PSU_CAPEX', 'BEL': 'PSU_CAPEX',  'HAL': 'PSU_CAPEX',
    'NTPC':       'PSU_CAPEX', 'POWERGRID': 'PSU_CAPEX',
    'NALCO':      'METAL',  'VEDANTA': 'METAL',     'HINDALCO': 'METAL',
    'JSWSTEEL':   'METAL',  'TATASTEEL': 'METAL',   'COALINDIA': 'METAL',
    'ADANIGREEN': 'RENEWABLE', 'TATAPOWER': 'RENEWABLE', 'WAAREE': 'RENEWABLE',
    'SAMMAAN':    'NBFC',   'BAJFINANCE': 'NBFC',   'CHOLAFIN': 'NBFC',
    'MUTHOOTFIN': 'NBFC',
    'MOTILALOFS': 'BROKING', 'ANGELONE': 'BROKING', 'ICICISEC': 'BROKING',
    'ISEC':       'BROKING',   # ICICI Securities (NSE: ISEC)
    'INFY':       'IT',     'TCS': 'IT',            'WIPRO': 'IT',
    'HCLTECH':    'IT',
    'HDFCBANK':   'BANK',   'ICICIBANK': 'BANK',    'KOTAKBANK': 'BANK',
    'SBIN':       'BANK',   'AXISBANK': 'BANK',
    'HINDUNILVR': 'FMCG',  'ITC': 'FMCG',          'ASIANPAINT': 'FMCG',
    'RELIANCE':   'ENERGY', 'ONGC': 'ENERGY',       'BPCL': 'ENERGY',
}

# Sectors with known external risk factors — used for risk flag tagging
SECTOR_RISK_FLAGS = {
    'RENEWABLE': ['US_TARIFF', 'FII_OUTFLOW'],
    'METAL':     ['LME_PRICE', 'CHINA_SUPPLY'],
    'NBFC':      ['RBI_POLICY', 'CREDIT_EVENT'],
    'AUTO_TECH': ['AUTO_SECTOR'],
}

# Local RAG configuration for dashboard intelligence
RAG_CONFIG = {
    "enabled": True,
    "top_k": 5,
    "max_signal_docs": 25,
    "max_trade_docs": 100,
}

# ── Book-level VaR / ES / Stress config (Track B gap #1) ─────────────────────
# Spec: docs/risk/es_var_stress_spec.md
# Phase 1 (monitoring-only): enabled=False keeps numbers flowing to observers
#   but no breaches are raised and the gate chain is NOT touched.
# Phase 2 (future): set gate_new_entries=True after 4-week paper validation
#   to block new entries when es_99_pct exceeds es_99_limit_pct.
# Phase 3 (future): set kill_switch=True to promote worst stress to session halt.
# NOTE: config.py is gitignored — changes here are local-only. Coordinate with
#   the live_runner / api_server integration (Phase 1) before editing limits.
PORTFOLIO_RISK_CONFIG = {
    "enabled": False,                 # Phase 2/3 gate disabled; numbers always computed
    "es_99_limit_pct": 0.06,          # ES99 ceiling = 6% capital (~1.5× max_daily_loss)
    "stress_loss_limit_pct": 0.10,    # worst single scenario ceiling = 10% capital
    "gate_new_entries": False,        # Phase 2: blocks new entries when es_99 breached
    "kill_switch": False,             # Phase 3: session halt on worst stress breach
    "min_journal_samples": 30,        # require ≥30 resolved records for hist-sim VaR
    "spread_haircut": 0.15,           # liquidity_drain: exit at entry_prem × (1−0.15)
    "scenarios": {                    # parameterized in core/portfolio_risk._build_scenario_params
        "gap_down_5":            {"spot_shock": 0.95, "iv_shock": 1.0},
        "vol_spike_vix50":       {"spot_shock": 1.0,  "iv_shock": 1.5},
        "gap_down_5_vol_spike":  {"spot_shock": 0.95, "iv_shock": 1.5},
        "expiry_pin":            {"spot_shock": 1.0,  "iv_shock": 1.0},
        "liquidity_drain":       {"spot_shock": 1.0,  "iv_shock": 1.0},
    },
}
