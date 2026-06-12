"""
Aladdin Runner — Multi-agent trading system inspired by BlackRock's Aladdin.

Agent roster:
  [Existing pipeline]
    scanner    — volume surge detection (3m loop)
    signal     — technical signal generation (event)
    filter     — fake breakout filtering (event)
    risk       — position/portfolio gates (event)
    execution  — order entry + position mgmt (event + 60s loop)
    learning   — trade recording + auto-tune (event)
    market_ctx — Nifty/BankNifty regime (5m loop)
    notifier   — Telegram alerts (event)

  [Aladdin agents — new]
    architect_hallac      — pipeline health, self-heal (2m loop)
    quant_golub           — Kelly sizing, correlation (5m loop + event)
    alpha_fink            — macro regime, sector rotation (5m loop + event)
    compliance_novick     — audit trail, risk limits (1m loop + event)
    ops_kapito            — broker health, fill quality, trail SL (1m loop + event)
    innovation_ajitsaria  — ML patterns, feature importance (10m loop)
    coordinator_nair      — orchestration, consensus, post-market (30s loop)

Event flow:
  ScannerAgent → SURGE_DETECTED
    → SignalAgent → SIGNAL_GENERATED
      → FilterAgent → SIGNAL_FILTERED
        → RiskAgent → RISK_APPROVED
          → QuantAgent → QUANT_SIZED
            → ComplianceAgent → COMPLIANCE_APPROVED
              → OperationsAgent → EXECUTION_READY
                → CoordinatorAgent → FINAL_EXECUTE
                  → ExecutionAgent → TRADE_ENTERED / TRADE_CLOSED

Run: python aladdin_runner.py [--force] [--capital 100000]
"""

import os
import sys
import signal as _signal
import argparse
import time
import json
import logging
import threading
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Suppress yfinance noise
class _DelistedFilter(logging.Filter):
    def filter(self, record):
        return "possibly delisted" not in record.getMessage()
logging.getLogger("yfinance").addFilter(_DelistedFilter())

import config
from core.agent_bus import SharedState, EventBus
from core.api_dhan import DhanAPI, check_token_health, SECURITY_ID_MAP
from core.market_feed import DhanMarketFeed
from core.scanner import LiquidityScanner

# Existing agents
from core.agents.scanner_agent import ScannerAgent
from core.agents.signal_agent import SignalAgent
from core.agents.filter_agent import FilterAgent
from core.agents.risk_agent import RiskAgent
from core.agents.execution_agent import ExecutionAgent
from core.agents.learning_agent import LearningAgent
from core.agents.market_context_agent import MarketContextAgent
from core.agents.notifier_agent import NotifierAgent

# Aladdin agents
from core.agents.architect_agent import ArchitectAgent
from core.agents.quant_agent import QuantAgent
from core.agents.alpha_agent import AlphaAgent
from core.agents.compliance_agent import ComplianceAgent
from core.agents.operations_agent import OperationsAgent
from core.agents.innovation_agent import InnovationAgent
from core.agents.coordinator_agent import CoordinatorAgent
from core.agents.swarm_agent import SwarmAgent
from core.agents.opening_scout_agent import OpeningScoutAgent
from core.agents.meta_learning_agent import MetaLearningAgent

_IST = timezone(timedelta(hours=5, minutes=30))
_running = True

_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "state.json")


def _stop(sig, frame):
    global _running
    print("\nShutdown signal received. Stopping agents...")
    _running = False


def _market_open():
    now = datetime.now(_IST).replace(tzinfo=None)
    cur = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= cur <= (15 * 60 + 30)


def _enrich_positions(positions: dict, feed) -> dict:
    """Add live price, uPnL, % change, SL distance to each position dict."""
    enriched = {}
    for sym, pos in positions.items():
        # Support both legacy (entry/qty) and new (entry_price/quantity) key schemes.
        entry     = pos.get("entry_price", pos.get("entry", 0.0)) or 0.0
        qty       = pos.get("quantity",    pos.get("qty",   0))   or 0
        direction = pos.get("direction", "long")
        sl_price  = pos.get("sl_price", 0.0) or 0.0
        target    = pos.get("target_price", 0.0) or 0.0

        current = None
        try:
            if feed is not None:
                current = feed.get_ltp(sym)
        except Exception:
            current = None

        upnl = pct = sl_dist_pct = None
        if current and entry:
            if direction == "long":
                upnl = (current - entry) * qty
                pct  = (current - entry) / entry * 100
                sl_dist_pct = (current - sl_price) / entry * 100 if sl_price else None
            else:
                upnl = (entry - current) * qty
                pct  = (entry - current) / entry * 100
                sl_dist_pct = (sl_price - current) / entry * 100 if sl_price else None

        enriched[sym] = {
            "entry":         entry,
            "entry_price":   entry,    # mirror for callers expecting either key
            "qty":           qty,
            "quantity":      qty,
            "direction":     direction,
            "confidence":    round(float(pos.get("confidence", 0) or 0), 3),
            "grade":         pos.get("grade", ""),
            "sl_price":      sl_price,
            "target_price":  target,
            "current_price": round(current, 2) if current else None,
            "upnl":          round(upnl, 2) if upnl is not None else None,
            "pct":           round(pct, 2)  if pct  is not None else None,
            "sl_dist_pct":   round(sl_dist_pct, 2) if sl_dist_pct is not None else None,
        }
    return enriched


def _export_state(state, feed, agent_names) -> None:
    """Persist SharedState snapshot to logs/state.json.

    Streamlit/dashboard.py + Next.js UI both read this file for live open positions,
    daily P&L, regime, etc. Without periodic writes here, the file goes stale and
    UIs show pre-aladdin data.
    """
    try:
        snapshot = {
            "ts": datetime.now().isoformat(),
            "market_regime":         state.get("market_regime", "unknown"),
            "macro_bias":            state.get("macro_bias", "unknown"),
            "nifty_change_pct":      state.get("nifty_change_pct", 0.0),
            "banknifty_change_pct":  state.get("banknifty_change_pct", 0.0),
            "vix_proxy":             state.get("vix_proxy", 15.0),
            "portfolio_heat":        state.get("portfolio_heat", 0.0),
            "daily_pnl":             state.get("daily_pnl", 0.0),
            "total_trades_today":    state.get("total_trades_today", 0),
            "consecutive_losses":    state.get("consecutive_losses", 0),
            "win_streak":            state.get("win_streak", 0),
            "trading_halted":        state.get("trading_halted", False),
            "halt_reason":           state.get("halt_reason", ""),
            "volume_surges": {
                sym: round(float(ratio), 2)
                for sym, ratio in state.get("volume_surges", {}).items()
            },
            "open_positions": _enrich_positions(state.get("open_positions", {}), feed),
            "today_trades":   state.get("today_trades", []),
            "agents":         list(agent_names),
        }
        os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
        tmp_path = _STATE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, default=str)
        os.replace(tmp_path, _STATE_FILE)
    except Exception as e:
        logging.getLogger(__name__).debug(f"state export error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Background workers — let aladdin_runner.py do EVERYTHING (one-command run)
# ─────────────────────────────────────────────────────────────────────────────

def _start_api_server(port: int = 8000) -> threading.Thread:
    """Spawn uvicorn for FastAPI in a daemon thread.

    Replaces the separate `python -m uvicorn api_server:app` window.
    """
    def _run():
        try:
            import uvicorn
            uvicorn.run("api_server:app", host="127.0.0.1", port=port,
                        log_level="warning", reload=False)
        except Exception as e:
            logging.getLogger(__name__).error(f"[API] uvicorn crash: {e}")
    t = threading.Thread(target=_run, name="api-server", daemon=True)
    t.start()
    return t


def _start_scan_loop(force: bool, top_n: int = 10) -> threading.Thread:
    """Spawn TimeframeSync scan loop (the old scan_only_v2.py work) as daemon.

    Writes enriched signals to logs/signals.json every 30s. UI + Streamlit
    consume that file. Without this loop, only event-driven SignalAgent
    publishes signals (which depends on live WebSocket surges).
    """
    def _run():
        log = logging.getLogger("scan_loop")
        try:
            from core.api_dhan import DhanAPI as _DhanAPI
            from core.timeframe_sync import TimeframeSyncEngine
            from core.signal_writer import write_signals
            from core.universe import FO_UNIVERSE
            from core.signal_journal import record_signal
            from core.signal_tracker import check_outcomes
            from core.adaptive_learner import get_learner
            from core.option_translator import get_option_rec
            from core.dashboard_data import get_option_chain
            from core.nse_option_chain import validate_fno_universe
            from core.signal_finalize import finalize_and_select

            api = _DhanAPI()
            engine = TimeframeSyncEngine()
            validated = validate_fno_universe(FO_UNIVERSE) or FO_UNIVERSE
            log.info(f"[ScanLoop] universe={len(validated)} symbols")

            scan_count = 0
            while _running:
                if not force and not _market_open():
                    time.sleep(30)
                    continue

                try:
                    t0 = datetime.now()
                    sigs = engine.scan_universe(api, validated)
                    elapsed = (datetime.now() - t0).total_seconds()

                    # HARD F&O gate — drop signals without valid option chain.
                    # F&O-only trading: no chain = not tradeable. If a real
                    # F&O symbol fails here, root cause is auth/rate-limit
                    # (fix data layer), NOT the gate.
                    from core.nse_option_chain import is_fno_symbol
                    enriched = []
                    chain_failures = 0
                    rec_failures = 0
                    non_fno = 0
                    for s in sigs:
                        try:
                            # Stage 1: NSE F&O segment membership (static set)
                            if not is_fno_symbol(s["symbol"]):
                                non_fno += 1
                                continue
                            chain = get_option_chain(s["symbol"])
                            if not chain:
                                chain_failures += 1
                                continue
                            rec = get_option_rec(
                                symbol=s["symbol"], direction=s["direction"],
                                spot=s["entry_price"], entry=s["entry_price"],
                                sl=s["sl_price"], target=s["target_price"],
                                chain_data=chain,
                            )
                            if not (rec and rec["entry_prem"] > 0):
                                rec_failures += 1
                                continue
                            ep = rec["entry_prem"]
                            sl_r  = abs(ep - rec["sl_prem"]) / ep
                            tgt_r = abs(rec["target_prem"] - ep) / ep
                            if sl_r > 3.0 or tgt_r > 3.0:
                                rec_failures += 1
                                continue

                            # F4 IV-rank gate: skip rich-IV premium buys.
                            try:
                                from core.iv_rank import get_iv_rank
                                if get_iv_rank().should_block(
                                        s["symbol"], rec["iv_pct"]):
                                    rec_failures += 1
                                    continue
                            except Exception:
                                pass

                            # G OI edge: ΔOI 4-quadrant + walls. Wall-block
                            # drops; else adjusts the journaled score.
                            oi = {}
                            try:
                                from core.oi_signal import oi_features
                                oi = oi_features(s["symbol"], chain,
                                                 s["direction"])
                                if oi.get("wall_block"):
                                    rec_failures += 1
                                    continue
                            except Exception:
                                oi = {}

                            s.update({
                                "option_strike": rec["strike"],
                                "option_expiry": rec["expiry"],
                                "option_type":   rec["option_type"],
                                "entry_prem":    rec["entry_prem"],
                                "sl_prem":       rec["sl_prem"],
                                "target_prem":   rec["target_prem"],
                                "delta":         rec["delta"],
                                "iv_pct":        rec["iv_pct"],
                                "prem_source":   rec["source"],
                                "spread_pct":    rec.get("spread_pct"),
                                "bid":           rec.get("bid"),
                                "ask":           rec.get("ask"),
                                "theta":         rec.get("theta"),
                            })
                            if oi:
                                s["confluence_score"] = int(
                                    s.get("confluence_score", 0) or 0) \
                                    + int(oi.get("score_delta", 0) or 0)
                                s["oi_quadrant"] = oi.get("quadrant")
                                s["pcr"]         = oi.get("pcr")
                                s["pcr_regime"]  = oi.get("pcr_regime")
                                op = oi.get("patterns") or []
                                if op:
                                    pc = s.get("patterns_combined") \
                                        or s.get("patterns") or []
                                    if isinstance(pc, str):
                                        pc = [pc]
                                    s["patterns_combined"] = list(pc) + op
                                    s["patterns"] = s["patterns_combined"]
                                    s["reason"] = (f'{s.get("reason","")} '
                                                   f'| OI:{oi.get("quadrant")}')
                            enriched.append(s)
                        except Exception:
                            pass
                    if chain_failures or non_fno:
                        log.info(
                            f"[ScanLoop] dropped {non_fno} (not F&O), "
                            f"{chain_failures} (no chain), {rec_failures} (bad rec). "
                            f"Kept {len(enriched)}."
                        )
                    if chain_failures == len(sigs) and sigs:
                        log.warning(
                            "[ScanLoop] ALL signals dropped (no chain) — "
                            "Dhan token expired or rate-limited. Refresh dhan_token.txt"
                        )

                    for s in enriched:
                        try:
                            record_signal(s)
                        except Exception:
                            pass

                    # Single authoritative gate: calibrate OI-adjusted
                    # score → P(win), keep only positive-expectancy,
                    # rank best-edge-first, cap. Same path as scan_only_v2.
                    fired = finalize_and_select(enriched)
                    write_signals(fired, meta={
                        "elapsed_sec": round(elapsed, 2),
                        "universe_size": len(validated),
                        "source": "aladdin_scan_loop",
                    })
                    log.info(f"[ScanLoop] {len(enriched)} candidates → "
                             f"{len(fired)} fired in {elapsed:.1f}s")

                    scan_count += 1
                    # Tracker + learner every 10 scans (~5 min)
                    if scan_count % 10 == 0:
                        try:
                            th, sl_h, ex = check_outcomes()
                            if th or sl_h or ex:
                                log.info(f"[Tracker] TARGET={th} SL={sl_h} EXPIRED={ex}")
                            # audit #6 parity with scan_only_v2: in-session
                            # self-tuning stays FROZEN unless explicitly enabled
                            # in config — this loop was bypassing the freeze.
                            import config as _cfg
                            if getattr(_cfg, "LEARNING_ENABLED", False):
                                changes = get_learner().maybe_update()
                                if changes:
                                    log.info(f"[Learner] {len(changes)} param(s) updated")
                                engine.engine.reload_learned_params()
                        except Exception as e:
                            log.warning(f"[Tracker/Learner] {e}")
                except Exception as e:
                    log.error(f"[ScanLoop] scan failed: {e}")

                # 30s cadence
                for _ in range(30):
                    if not _running:
                        break
                    time.sleep(1)
        except Exception as e:
            logging.getLogger(__name__).error(f"[ScanLoop] fatal: {e}")

    t = threading.Thread(target=_run, name="scan-loop", daemon=True)
    t.start()
    return t


def _start_tracker_loop() -> threading.Thread:
    """Background tracker — resolves outcomes every 60s.

    Belt-and-suspenders: scan loop already calls check_outcomes every 10
    scans, but this one runs even when no new signals are firing so
    in-flight trades still get tracked to completion.
    """
    def _run():
        log = logging.getLogger("tracker_loop")
        from core.signal_tracker import check_outcomes
        while _running:
            try:
                th, sl_h, ex = check_outcomes()
                if th or sl_h or ex:
                    log.info(f"[Tracker] TARGET={th} SL={sl_h} EXPIRED={ex}")
            except Exception as e:
                log.debug(f"[Tracker] {e}")
            for _ in range(60):
                if not _running:
                    break
                time.sleep(1)
    t = threading.Thread(target=_run, name="tracker-loop", daemon=True)
    t.start()
    return t


def main():
    parser = argparse.ArgumentParser(description="Aladdin Multi-Agent Trading System")
    parser.add_argument("--force", action="store_true", help="Run outside market hours")
    parser.add_argument("--capital", type=float, default=100000, help="Trading capital (₹)")
    parser.add_argument("--no-api", action="store_true", help="Skip embedded FastAPI server")
    parser.add_argument("--no-scan", action="store_true", help="Skip TimeframeSync scan loop")
    parser.add_argument("--api-port", type=int, default=8000, help="FastAPI port (default 8000)")
    args = parser.parse_args()

    _signal.signal(_signal.SIGINT, _stop)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # Setup logging — force UTF-8 on Windows console to avoid cp1252 crashes
    import io
    console_handler = logging.StreamHandler(
        io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    )
    console_handler.setLevel(logging.INFO)
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    primary_log = os.path.join(logs_dir, "agents.log")
    try:
        file_handler = logging.FileHandler(primary_log, mode="a", encoding="utf-8")
    except PermissionError:
        fallback_log = os.path.join(logs_dir, f"agents-{os.getpid()}.log")
        print(f"  Log file locked: {primary_log}. Falling back to {fallback_log}")
        file_handler = logging.FileHandler(fallback_log, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s - %(message)s",
        datefmt="%H:%M:%S",
        handlers=[console_handler, file_handler],
    )

    print("=" * 60)
    print("  ALADDIN — Multi-Agent F&O Trading System")
    print("  Inspired by BlackRock's Aladdin Architecture")
    print("=" * 60)
    print(f"  Capital: ₹{args.capital:,.0f}")
    print(f"  Mode: {'LIVE (--force)' if args.force else 'Market Hours Only'}")
    print(f"  Paper Trade: {config.PAPER_TRADE}")
    print()

    # Token health
    if not config.USE_MOCK_DATA:
        check_token_health()
        # Reconcile stale hardcoded security IDs (prevents wrong-chain bugs
        # like NAVINFLUOR serving MUTHOOTFIN's option chain).
        try:
            from core.api_dhan import reconcile_security_ids
            reconcile_security_ids()
        except Exception as _e:
            logging.getLogger(__name__).warning(f"[SecID] reconcile failed: {_e}")

    # Core infrastructure
    state = SharedState()
    bus = EventBus()
    api = DhanAPI()
    scanner = LiquidityScanner()
    feed = DhanMarketFeed()

    # Tuner for learning agent
    try:
        from core.auto_repair import AutoTuner
        tuner = AutoTuner()
    except Exception:
        tuner = type("MockTuner", (), {
            "record_trade": lambda *a, **k: None,
            "tune": lambda *a: {},
            "trade_history": [],
        })()

    capital = args.capital

    # Universe + Notifier setup — validate against live NSE F&O list
    from core.universe import FO_UNIVERSE
    from core.nse_option_chain import validate_fno_universe
    print("Validating F&O universe against NSE...")
    validated_universe = validate_fno_universe(FO_UNIVERSE)
    if validated_universe and len(validated_universe) < len(FO_UNIVERSE):
        removed = len(FO_UNIVERSE) - len(validated_universe)
        print(f"  Removed {removed} non-F&O stocks from universe")
    fo_universe = validated_universe if validated_universe else FO_UNIVERSE
    try:
        from core.notifier import TelegramNotifier
        notifier = TelegramNotifier()
    except Exception:
        notifier = type("MockNotifier", (), {
            "send": lambda *a, **k: None,
            "send_signal": lambda *a, **k: None,
            "send_trade": lambda *a, **k: None,
        })()

    # ── Instantiate all agents ───────────────────────────────────────────
    agents = {}

    feed_map = {sym: sid for sym, sid in SECURITY_ID_MAP.items()
                if sym in set(fo_universe) | {"NIFTY", "BANKNIFTY"}}
    if feed_map:
        feed.start(feed_map)
        print(f"  Feed: subscribed to {len(feed_map)} mapped symbols")

    # Existing pipeline — scanner-derived agents need LiquidityScanner (has get_market_data),
    # not raw DhanAPI. Wrong wiring causes AttributeError per symbol -> 0 surges -> no signals.
    agents["scanner"] = ScannerAgent(state, bus, scanner, fo_universe, feed=feed)
    agents["signal"] = SignalAgent(state, bus, scanner)
    agents["filter"] = FilterAgent(state, bus, scanner)
    agents["risk"] = RiskAgent(state, bus, capital)
    agents["execution"] = ExecutionAgent(state, bus, scanner, capital, feed=feed)
    agents["learning"] = LearningAgent(state, bus, tuner)
    agents["market_ctx"] = MarketContextAgent(state, bus, scanner, feed=feed)
    agents["notifier"] = NotifierAgent(state, bus, notifier, feed=feed)

    # Aladdin agents
    agents["architect_hallac"] = ArchitectAgent(state, bus, api)
    agents["quant_golub"] = QuantAgent(state, bus, capital)
    agents["alpha_fink"] = AlphaAgent(state, bus, api)
    agents["compliance_novick"] = ComplianceAgent(state, bus, capital)
    agents["ops_kapito"] = OperationsAgent(state, bus, api, capital)
    agents["innovation_ajitsaria"] = InnovationAgent(state, bus)
    agents["swarm"] = SwarmAgent(state, bus)
    agents["opening_scout"] = OpeningScoutAgent(state, bus, scanner, fo_universe)
    agents["meta_learning"] = MetaLearningAgent(state, bus)
    agents["coordinator_nair"] = CoordinatorAgent(state, bus, agents)

    # Give coordinator reference to all agents (for restart capability)
    agents["coordinator_nair"].register_agents(agents)

    # ── Start all agents ─────────────────────────────────────────────────
    print("\nStarting agents:")
    print("-" * 45)
    for name, agent in agents.items():
        agent.start()
        interval = f"({agent.interval_sec}s loop)" if agent.interval_sec > 0 else "(event-driven)"
        print(f"  ✓ {name:<25} {interval}")
    print("-" * 45)
    print(f"\n  {len(agents)} agents active. System running.\n")

    # ── Start embedded background workers ────────────────────────────────
    if not args.no_api:
        _start_api_server(args.api_port)
        print(f"  ✓ FastAPI server          http://127.0.0.1:{args.api_port}")
    if not args.no_scan:
        _start_scan_loop(args.force)
        print(f"  ✓ TimeframeSync scan loop (30s) — writes signals.json")
    _start_tracker_loop()
    print(f"  ✓ Signal tracker loop     (60s) — resolves outcomes")
    print()

    # ── Main loop ────────────────────────────────────────────────────────
    status_interval = 60   # console status print every 60s
    state_interval  = 15   # state.json export every 15s — UI refresh cadence
    last_status = 0
    last_state_export = 0

    while _running:
        if not args.force and not _market_open():
            now = datetime.now(_IST).replace(tzinfo=None)
            cur = now.hour * 60 + now.minute
            open_min = 9 * 60 + 15
            if cur < open_min:
                wait = open_min - cur
                print(f"Pre-market. Opens in {wait}m. Waiting...")
                for _ in range(60):
                    if not _running:
                        break
                    time.sleep(1)
            else:
                print("Market closed. Triggering final post-market...")
                # Ensure post-market runs
                coord = agents["coordinator_nair"]
                if not coord._post_market_done_today:
                    coord._trigger_post_market()
                    coord._post_market_done_today = True
                # Daily honest-metrics rollup (was wired to the legacy
                # signal_tracker close path; this runner is the EOD trigger
                # now). Idempotent per day; failure must not block shutdown.
                try:
                    from core.metrics_writer import write_metrics
                    rec = write_metrics()
                    r30 = rec.get("rolling_30d", {})
                    print(f"[Metrics] {rec.get('date')} written: 30d WR {r30.get('wr')} "
                          f"PF {r30.get('pf')} DD {r30.get('max_dd_pct')}% "
                          f"| drift={rec.get('drift_alert')} "
                          f"reasons={rec.get('drift_reasons')}")
                except Exception as e:
                    print(f"[Metrics] rollup failed (non-fatal): {e}")
                break
            continue

        now = time.time()

        # State export every 15s — Streamlit + Next.js poll state.json for live data.
        if now - last_state_export > state_interval:
            _export_state(state, feed, list(agents.keys()))
            last_state_export = now

        # Console status every 60s
        if now - last_status > status_interval:
            health = state.get("system_health", {})
            pnl = state.get("daily_pnl", 0)
            trades = state.get("total_trades_today", 0)
            positions = len(state.get("open_positions", {}))
            macro = state.get("macro_bias", "?")
            h_status = health.get("status", "?") if isinstance(health, dict) else "?"

            print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                  f"PnL=₹{pnl:+,.0f} | trades={trades} | pos={positions} | "
                  f"macro={macro} | health={h_status}")
            last_status = now

        time.sleep(1)

    # ── Shutdown ─────────────────────────────────────────────────────────
    print("\nStopping agents...")
    # Final state flush so UIs see the last known PnL/positions, not stale data.
    _export_state(state, feed, list(agents.keys()))
    feed.stop()
    for name, agent in agents.items():
        agent.stop()
    print("All agents stopped. Aladdin offline.")


if __name__ == "__main__":
    main()
