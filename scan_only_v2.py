import os
import sys
import signal as _signal
import time
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:                                  # Windows cp1252 chokes on the ✅/⚠ icons we print
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import logging as _logging

class _DelistedFilter(_logging.Filter):
    def filter(self, record):
        return "possibly delisted" not in record.getMessage()

_logging.getLogger("yfinance").addFilter(_DelistedFilter())

import config
from core.api_dhan import DhanAPI, check_token_health
from core.timeframe_sync import TimeframeSyncEngine
from core.signal_writer import write_signals, SIGNALS_FILE
from core.universe import FO_UNIVERSE, TOP100_FO
from core.signal_journal import record_signal
from core.signal_finalize import finalize_and_select
from core.signal_tracker import check_outcomes
from core.adaptive_learner import get_learner
from core.option_translator import get_option_rec
from core.dashboard_data import get_option_chain
from core.nse_option_chain import validate_fno_universe

_scan_count = 0
# audit #4: india_swing is daily-close; generate once/day and hold (see _scan).
_iswing_last_date = None
_iswing_cache: list = []

SCAN_INTERVAL_SEC = 15  # was 30 — halve latency for faster entry detection
_OPEN  = (9, 15)
_CLOSE = (15, 30)
_running = True


def _stop(sig, frame):
    global _running
    print('\nShutdown signal. Finishing current scan...')
    _running = False


def _market_open():
    n = datetime.now()
    cur = n.hour * 60 + n.minute
    return (_OPEN[0]*60 + _OPEN[1]) <= cur <= (_CLOSE[0]*60 + _CLOSE[1])


def _scan(engine, api, top_n, universe=None, orb_only=False, vol_only=False):
    global _scan_count
    universe = universe or FO_UNIVERSE
    ts = datetime.now().strftime('%H:%M:%S')
    t0 = datetime.now()

    # ── ORB STRATEGY (runs alongside or instead of pattern voting) ──
    orb_sigs = []
    try:
        from core.orb_strategy import detect_orb_signal, is_orb_window_active
        active, phase = is_orb_window_active()
        if active:
            print(f'[{ts}] ORB window ACTIVE (phase={phase}) - scanning {len(universe)} symbols')
            for sym in universe:
                try:
                    df = api.get_intraday_data(sym, interval=5, days_back=1)
                    if df is None or df.empty:
                        continue
                    orb_sig = detect_orb_signal(sym, df)
                    if orb_sig:
                        orb_sigs.append(orb_sig)
                except Exception:
                    continue
            if orb_sigs:
                print(f'  [ORB] {len(orb_sigs)} breakouts detected: '
                      f'{", ".join(s["symbol"] + ":" + s["direction"][:1].upper() for s in orb_sigs[:5])}')
        else:
            print(f'[{ts}] ORB window NOT active (phase={phase})')
    except Exception as e:
        log.debug(f"ORB detection failed: {e}")

    # ── VOLATILITY STRATEGY (compression → expansion straddles/strangles) ──
    vol_sigs = []
    try:
        from core.volatility_strategy import detect_vol_signal, is_vol_window_active
        from core.dashboard_data import get_option_chain
        v_active, v_phase = is_vol_window_active()
        if v_active:
            print(f'[{ts}] VOL window active (phase={v_phase})')
            # Only scan top-N by liquidity to keep latency low (compression rare)
            vol_universe = universe[:30] if len(universe) > 30 else universe
            for sym in vol_universe:
                try:
                    df_5m = api.get_intraday_data(sym, interval=5, days_back=1)
                    df_1d = api.get_daily_data(sym, days=60) if hasattr(api, 'get_daily_data') else None
                    chain = get_option_chain(sym)
                    if not chain:
                        continue
                    vsig = detect_vol_signal(sym, df_5m, df_1d, chain)
                    if vsig:
                        vol_sigs.append(vsig)
                except Exception:
                    continue
            if vol_sigs:
                print(f'  [VOL] {len(vol_sigs)} compression setups: '
                      f'{", ".join(s["symbol"] + ":" + s["strategy"] for s in vol_sigs[:5])}')
    except Exception as e:
        log.debug(f"Vol strategy failed: {e}")

    if orb_only:
        sigs = orb_sigs
        print(f'  [ORB-ONLY] {len(sigs)} signals (pattern voting disabled)')
    elif vol_only:
        sigs = vol_sigs
        print(f'  [VOL-ONLY] {len(sigs)} compression signals (other strategies disabled)')
    else:
        # ── STRATEGY SWITCH ────────────────────────────────────────────────
        # Default: india_swing 8-gate sequential (replaces 17-detector vote stack).
        # Legacy: set STRATEGY_LEGACY=1 env to fall back to old TimeframeSync engine.
        # Shadow A/B: set STRATEGY_SHADOW=1 to also run legacy engine in
        # background and journal its signals with strategy_origin='legacy_shadow'
        # for 30-day WR comparison (NOT enriched/emitted to UI).
        # Vote-stack delivered 30% WR with no Grade-A edge (journal audit
        # 2026-05-26, n=932). New gates target 40-50% WR with 1:3 RR.
        use_legacy = os.environ.get("STRATEGY_LEGACY") == "1"
        run_shadow = os.environ.get("STRATEGY_SHADOW") == "1"
        if use_legacy:
            print(f'[{ts}] Scanning {len(universe)} symbols (LEGACY 17-detector vote stack)...')
            sigs = engine.scan_universe(api, universe)
            for s in sigs:
                s["strategy_origin"] = "legacy"
        else:
            # FIX (audit #4): india_swing is a DAILY-CLOSE swing strategy — its
            # own design says "decisions are made on the close." Re-running it
            # every 15s on an INCOMPLETE daily bar produced unstable signals that
            # flip intraday + journal churn + live≠backtest. Generate ONCE per
            # trading day, then HOLD those signals all day (intraday price action
            # is for the ORB/VOL paths, not this one).
            #   - default: run on the first scan of a new day, reuse rest of day.
            #   - config.ISWING_DECISION_TIME=(15,15): wait until the close so the
            #     daily bar is complete before deciding (most correct).
            #   - ISWING_INTRADAY_RERUN=1: revert to per-scan (old behavior).
            global _iswing_last_date, _iswing_cache
            now_dt = datetime.now()
            today_str = now_dt.strftime('%Y-%m-%d')
            cutoff = getattr(config, 'ISWING_DECISION_TIME', None)
            after_cutoff = (cutoff is None) or ((now_dt.hour, now_dt.minute) >= tuple(cutoff))
            rerun = os.environ.get('ISWING_INTRADAY_RERUN') == '1'
            run_iswing = rerun or (_iswing_last_date != today_str
                                   and (after_cutoff or not _market_open()))
            if run_iswing:
                print(f'[{ts}] Scanning {len(universe)} symbols (india_swing daily-close)...')
                from core.strategy_india_swing import scan_universe_india_swing
                sigs = scan_universe_india_swing(api, universe)
                for s in sigs:
                    s["strategy_origin"] = "india_swing"
                print(f'  [ISW] {len(sigs)} signals passed all gates')
                _iswing_last_date = today_str
                _iswing_cache = list(sigs)

                # Shadow-mode: run legacy engine and journal its candidates without
                # enriching/emitting them. Compares both engines' raw output for WR.
                if run_shadow:
                    try:
                        shadow_sigs = engine.scan_universe(api, universe)
                        print(f'  [SHADOW] legacy engine produced {len(shadow_sigs)} candidates')
                        try:
                            from core.signal_journal import record_signal as _record
                            for ss in shadow_sigs:
                                ss["strategy_origin"] = "legacy_shadow"
                                ss["shadow"] = True
                                try:
                                    _record(ss)
                                except Exception:
                                    pass
                        except Exception as _je:
                            log.debug(f"shadow journal err: {_je}")
                    except Exception as _se:
                        log.debug(f"shadow scan err: {_se}")
            else:
                # never serve yesterday's signals before today's decision time
                sigs = list(_iswing_cache) if _iswing_last_date == today_str else []
                _cut = f"{cutoff[0]:02d}:{cutoff[1]:02d}" if cutoff else "first scan"
                print(f"[{ts}] [ISW] daily-close strategy: holding {len(sigs)} signal(s) "
                      f"(eval once/day @ {_cut}; ISWING_INTRADAY_RERUN=1 to override)")

        # Merge ORB signals — for symbols where ORB fired, prefer ORB over pattern signal
        if orb_sigs:
            orb_syms = {s["symbol"] for s in orb_sigs}
            sigs = [s for s in sigs if s["symbol"] not in orb_syms]
            sigs.extend(orb_sigs)
            print(f'  [ORB] {len(orb_sigs)} ORB signals merged into pipeline')
        # Add vol signals (don't replace — vol is direction-agnostic, complementary)
        if vol_sigs:
            sigs.extend(vol_sigs)
            print(f'  [VOL] {len(vol_sigs)} vol-expansion signals added')

    elapsed = round((datetime.now() - t0).total_seconds(), 2)

    # Enrich each signal with live option chain data.
    # HARD GATE: drop signals where option chain is empty (not F&O eligible).
    # Only F&O-tradeable signals survive.
    enriched = []
    dropped_no_chain = []
    _instrument_mode = getattr(config, "INSTRUMENT_MODE", "futures")
    for s in sigs:
        try:
            # ── UNIVERSE FILTER ──────────────────────────────────────────
            # Block stocks where this setup is a proven loser on SPOT (the
            # real price move, not option premium). Untested symbols pass.
            try:
                from core.universe_filter import is_tradeable
                _ok, _uinfo = is_tradeable(s["symbol"])
                if not _ok:
                    dropped_no_chain.append(s["symbol"] + f"(universe:{_uinfo.get('reason')})")
                    continue
            except Exception:
                pass

            # ── FUTURES MODE ─────────────────────────────────────────────
            # Express the directional view through the stock FUTURE — no
            # option chain, strike, theta or IV. The backtested edge (PF 1.17)
            # is a SPOT edge; futures carry it without premium-decay tax.
            if _instrument_mode == "futures":
                from core.futures_leg import attach_futures_leg
                fs = attach_futures_leg(s)
                if fs is None:
                    dropped_no_chain.append(s["symbol"] + "(fut_leg_fail)")
                    continue
                # Entry guard still applies (RSI extremes, dead vol, VWAP, etc.)
                if fs.get("strategy") not in ("STRADDLE", "STRANGLE", "ORB"):
                    try:
                        from core.entry_guard import check_entry
                        _g_ok, _g_reason = check_entry(fs)
                        if not _g_ok:
                            dropped_no_chain.append(s["symbol"] + f"(guard:{_g_reason})")
                            continue
                    except Exception:
                        pass
                enriched.append(fs)
                try:
                    record_signal(fs)
                except Exception:
                    pass
                continue  # skip all option-chain enrichment below

            # PRE-CHAIN EXPIRY GATE: skip symbols whose nearest expiry is today.
            # Saves an NSE API call and short-circuits before chain-derived expiry
            # can leak through. Uses calendar truth (holiday-shift-aware).
            try:
                from core.nse_calendar import days_to_next_expiry
                if days_to_next_expiry(s["symbol"]) < 1:
                    dropped_no_chain.append(s["symbol"] + "(expiry_today_calendar)")
                    continue
            except Exception as _e:
                # Calendar lookup must not silently let signal through — log and
                # rely on the post-chain HARD GATE below as the backstop.
                pass

            chain = get_option_chain(s["symbol"])
            if not chain:
                dropped_no_chain.append(s["symbol"])
                continue  # NOT F&O — drop entirely

            rec = get_option_rec(
                symbol=s["symbol"], direction=s["direction"],
                spot=s["entry_price"], entry=s["entry_price"],
                sl=s["sl_price"], target=s["target_price"],
                chain_data=chain,
            )
            if rec:
                # HARD GATE: never trade an option expiring today (or already expired).
                # Same-day expiry = pure gamma/theta — premium can go to ~0 within hours
                # regardless of direction. Holiday-shifted monthly expiry (e.g. Tue 2026-05-26
                # for Buddha Purnima) slipped past Thursday-only volatility filter.
                try:
                    _exp_d = datetime.fromisoformat(rec["expiry"]).date()
                    if (_exp_d - datetime.now().date()).days < 1:
                        dropped_no_chain.append(s["symbol"] + "(expiry_today)")
                        continue
                except Exception:
                    dropped_no_chain.append(s["symbol"] + "(expiry_parse_fail)")
                    continue

                # Sanity check: option SL/target must be within 3x of entry premium.
                entry_p = rec["entry_prem"]
                if entry_p > 0:
                    sl_ratio = abs(entry_p - rec["sl_prem"]) / entry_p
                    tgt_ratio = abs(rec["target_prem"] - entry_p) / entry_p
                    if sl_ratio > 3.0 or tgt_ratio > 3.0:
                        dropped_no_chain.append(s["symbol"] + "(bad_prem)")
                        continue

                # IV-rank gate: don't buy premium when this symbol's IV is
                # in its own top quartile (vega-long buyer eats IV crush).
                try:
                    from core.iv_rank import get_iv_rank
                    if get_iv_rank().should_block(s["symbol"], rec["iv_pct"]):
                        dropped_no_chain.append(s["symbol"] + "(rich_iv)")
                        continue
                except Exception:
                    pass

                # OI edge: ΔOI 4-quadrant + PCR regime + OI walls. The one
                # orthogonal (non-price) input. Wall-block drops the trade
                # outright; otherwise it adjusts the score the calibrator
                # learns from. Cold-start neutral, never raises.
                oi = {}
                try:
                    from core.oi_signal import oi_features
                    oi = oi_features(s["symbol"], chain, s["direction"])
                    if oi.get("wall_block"):
                        dropped_no_chain.append(s["symbol"] + "(oi_wall)")
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
                    # REAL fill data (replaces flat cost-model guesses)
                    "spread_pct":    rec.get("spread_pct"),
                    "bid":           rec.get("bid"),
                    "ask":           rec.get("ask"),
                    "theta":         rec.get("theta"),
                })
                if oi:
                    s["confluence_score"] = int(float(s.get("confluence_score", 0) or 0)) \
                        + int(float(oi.get("score_delta", 0) or 0))
                    s["oi_quadrant"]  = oi.get("quadrant")
                    s["pcr"]          = oi.get("pcr")
                    s["pcr_regime"]   = oi.get("pcr_regime")
                    op = oi.get("patterns") or []
                    if op:
                        pc = s.get("patterns_combined") or s.get("patterns") or []
                        if isinstance(pc, str):
                            pc = [pc]
                        s["patterns_combined"] = list(pc) + op
                        s["patterns"] = s["patterns_combined"]
                        s["reason"] = f'{s.get("reason","")} | OI:{oi.get("quadrant")}'

                # Theta decay penalty: penalize signals with high theta bleed
                try:
                    from core.theta_decay import theta_score_penalty
                    theta_pen = theta_score_penalty(s)
                    if theta_pen != 0:
                        s["confluence_score"] = int(float(s.get("confluence_score", 0) or 0)) + theta_pen
                        s["theta_penalty"] = theta_pen
                        s["reason"] = f'{s.get("reason","")} | θ={theta_pen:+d}'
                except Exception:
                    pass

                # Pattern quality scoring: each pattern has different predictive power.
                # Replaces binary "3+ votes = signal" with weighted quality combo.
                try:
                    from core.pattern_quality import compute_signal_quality
                    pq = compute_signal_quality(s)
                    if pq["score_adj"] != 0:
                        s["confluence_score"] = int(float(s.get("confluence_score", 0) or 0)) + pq["score_adj"]
                        s["pattern_quality"] = pq["quality"]
                        s["quality_verdict"] = pq["verdict"]
                        s["reason"] = f'{s.get("reason","")} | PQ:{pq["verdict"]}({pq["quality"]:.2f})'
                except Exception:
                    pass

                # Smart money proxy: institutional alignment score (L2 substitute)
                try:
                    from core.smart_money_proxy import institutional_alignment_score
                    # Need 5m df to compute — skip if not available, signal already enriched
                    sm = institutional_alignment_score(s, df_5m=None)
                    if sm["score"] < 40:
                        # Skip — institutional flow against trade
                        dropped_no_chain.append(s["symbol"] + f"(smartmoney:{sm['score']})")
                        continue
                    s["smart_money_score"] = sm["score"]
                    s["reason"] = f'{s.get("reason","")} | SM:{sm["score"]}'
                except Exception:
                    pass

                # Entry guard: block known bias modes (dead vol, RSI extremes, VWAP, etc.)
                # Setup-matched signals bypass. Vol/ORB strategies bypass too —
                # they have their own validation logic.
                if s.get("strategy") not in ("STRADDLE", "STRANGLE", "ORB"):
                    try:
                        from core.entry_guard import check_entry
                        ok, reason = check_entry(s)
                        if not ok:
                            dropped_no_chain.append(s["symbol"] + f"(guard:{reason})")
                            continue  # KILL signal
                    except Exception:
                        pass

                # Breakout profile match: score current vs stock's historical fingerprint
                try:
                    from core.breakout_study import score_current_vs_profile
                    pats = s.get("patterns_combined") or s.get("patterns") or []
                    if isinstance(pats, str):
                        pats = [p.strip() for p in pats.split(",")]
                    bp = score_current_vs_profile(
                        s["symbol"], s["direction"],
                        rsi=float(s.get("rsi", 50) or 50),
                        volume_ratio=float(s.get("volume_ratio", 1) or 1),
                        ema9_above_21=("ema_uptrend" in pats or "ema_bullish_cross" in pats),
                        supertrend_up=("supertrend_up" in pats),
                        above_vwap=("above_vwap" in pats),
                    )
                    if bp:
                        s["bp_match"] = bp["match_score"]
                        s["bp_checks"] = bp["checks"]
                except Exception:
                    pass

                # Setup detection: "Is this THE setup?"
                # Checks for known high-WR pattern combos and loss magnets.
                try:
                    from core.setup_detector import detect_setup
                    setup = detect_setup(s)
                    if setup:
                        if setup["is_loss_magnet"]:
                            dropped_no_chain.append(s["symbol"] + f"(lossmag:{setup['setup_name']})")
                            continue  # KILL — 0% WR combo
                        s["setup_name"] = setup["setup_name"]
                        s["setup_type"] = setup["setup_type"]
                        s["setup_wr"] = setup["win_rate"]
                        s["confluence_score"] = int(float(s.get("confluence_score", 0) or 0)) \
                            + setup["score_boost"]
                        s["reason"] = f'{s.get("reason","")} | SETUP:{setup["setup_name"]}({setup["win_rate"]:.0%})'
                except Exception:
                    pass

                enriched.append(s)
                try:
                    record_signal(s)
                except Exception:
                    pass
            else:
                dropped_no_chain.append(s["symbol"] + "(no_rec)")
        except Exception:
            pass

    if dropped_no_chain:
        print(f'  Dropped {len(dropped_no_chain)} non-F&O/bad-chain: {", ".join(dropped_no_chain[:10])}')
    # Single authoritative gate: calibrate OI-adjusted score → P(win),
    # keep only positive-expectancy signals, rank best-edge-first, cap.
    setups = finalize_and_select(enriched)

    # ── PULLBACK ENTRY: setups go to pending queue, NOT fired immediately ──
    # Detected breakouts wait for retest before becoming entries.
    # Eliminates chase entries that cost 5/6 trades today.
    pullback_enabled = True
    try:
        from core.pullback_entry import get_queue
        queue = get_queue()
        # Step 1: add new setups to pending queue
        # EXCEPTION: ORB and VOL signals bypass pullback
        #   - ORB: range edge IS the entry level (no retest needed)
        #   - VOL: direction-agnostic, premium-based entry/exit
        bypass_pullback = [s for s in setups if s.get("strategy") in ("ORB", "STRADDLE", "STRANGLE")]
        normal_setups = [s for s in setups if s.get("strategy") not in ("ORB", "STRADDLE", "STRANGLE")]
        for s in normal_setups:
            queue.add(s)

        # Step 2: check pending signals for retest using current API prices
        def _price_lookup(sym: str):
            try:
                df = api.get_intraday_data(sym, interval=5, days_back=1)
                if df is None or df.empty:
                    return 0, None
                cur = float(df['close'].iloc[-1])
                recent = df.tail(3).to_dict('records')
                return cur, recent
            except Exception:
                return 0, None

        fired = queue.check_all(_price_lookup)
        if fired:
            print(f'  [Pullback] {len(fired)} signals FIRED on retest')
        stats = queue.stats()
        if stats['pending_count']:
            print(f'  [Pullback] {stats["pending_count"]} pending: {", ".join(stats["pending_symbols"][:5])}')

        # ORB + VOL signals fire IMMEDIATELY (no retest needed) + pullback-fired signals
        sigs = bypass_pullback + fired
    except Exception as e:
        log.warning(f"[Pullback] failed: {e} — falling back to direct entries")
        sigs = setups  # includes ORB signals already if any
        pullback_enabled = False

    # Monte Carlo simulation: enrich signals with probability estimates
    if sigs:
        try:
            from core.monte_carlo import simulate_batch
            sigs = simulate_batch(sigs, n_sims=1000)  # 1k sims for speed in scanner
            mc_valid = [s for s in sigs if s.get("mc_p_target") is not None]
            if mc_valid:
                mc_pos = sum(1 for s in mc_valid if s.get("mc_edge", 0) > 0)
                print(f'  [MC] {len(mc_valid)}/{len(sigs)} simulated, '
                      f'{mc_pos} positive-edge')
        except Exception as e:
            log.debug(f"MC batch failed: {e}")

    # Position sizing by R:R (a real, mechanical factor) — NOT by grade.
    # 919-trade journal proof: grade does NOT predict outcome — Grade "S"
    # (supposedly best, sized 1.0) had 11-25% spot win-rate, the WORST bucket
    # in both directions, while confluence_score 130+ was also the worst for
    # longs. Sizing up by grade = sizing up the losers. Removed permanently.
    # R:R is mechanical and trustworthy: a 1:3 trade risks the same rupees as
    # a 1:1.5 trade but pays double, so it earns more size. Base 0.5, +0.25
    # per R above 1.5, capped 1.0. Grade/score now affect only display, never
    # size. (See FINDINGS_FACTORS.md.)
    for s in sigs:
        rr = float(s.get("rr_ratio", 0) or 0)
        size_mult = max(0.5, min(1.0, 0.5 + 0.25 * max(0.0, rr - 1.5)))
        # Setup-matched signals (data-backed combos) still earn a mild boost
        if s.get("setup_type") in ("mega_winner", "high_wr"):
            size_mult = min(size_mult * 1.25, 1.0)
        s["size_mult"] = round(size_mult, 2)

    # ── DATA-STALE HALT ──────────────────────────────────────────────────
    # Dhan is the only source now. If this whole cycle got no fresh bars, do
    # NOT publish signals (they'd be built on stale/empty data) — alert + skip
    # the write. The first cycle after startup is exempt (cold cache).
    try:
        from core.health import check_data_or_alert
        if _scan_count >= 1 and not check_data_or_alert():
            print(f'[{ts}] DATA STALE — skipping signal publish this cycle (alert sent).')
            return
    except Exception:
        pass

    # Write enriched signals (option fields now present for UI)
    write_signals(sigs, meta={"elapsed_sec": elapsed, "universe_size": len(universe)})

    s_ct = sum(1 for s in sigs if s['confluence_grade'] == 'S')
    a = sum(1 for s in sigs if s['confluence_grade'] == 'A')
    b = sum(1 for s in sigs if s['confluence_grade'] == 'B')
    c = sum(1 for s in sigs if s['confluence_grade'] == 'C')
    print(f'  Grade S={s_ct}  A={a}  B={b}  C={c}  total={len(sigs)}')
    top = sigs[:top_n]
    if top:
        print(f'  {"Symbol":<14} {"Dir":<6} {"Grd":<4} {"Score":>5} {"Entry":>9} {"SL":>9} {"MC":>6} {"BP":>4}  Reason')
        print('  ' + '-' * 88)
        for s in top:
            grade = s["confluence_grade"]
            prefix = '* ' if grade == 'S' else '  '
            mc_edge = s.get("mc_edge")
            mc_str = f'{mc_edge:+.2f}' if mc_edge is not None else '  n/a'
            bp = s.get("bp_match")
            bp_str = f'{bp:.0%}' if bp is not None else 'n/a'
            line = (
                f'{prefix}{s["symbol"]:<14} {s["direction"].upper():<6} '
                f'{grade:<4} {s["confluence_score"]:>5} '
                f'{s["entry_price"]:>9.2f} {s["sl_price"]:>9.2f} '
                f'{mc_str:>6} {bp_str:>4}  '
                f'{s["reason"][:40]}'
            )
            if grade == 'S':
                # FIX (audit #15): removed the false "~75-80% WR" label. The
                # journal showed Grade-S was the WORST bucket (11-25% spot WR),
                # not the best. Grade is display-only; it does NOT predict WR.
                line += ' [top score — grade does NOT predict WR]'
            setup_name = s.get("setup_name")
            if setup_name:
                line += f' *** SETUP:{setup_name} WR={s.get("setup_wr",0):.0%}'
            print(line)
    else:
        print('  No signals above threshold.')

    # Every 10 scans: resolve open signal outcomes + maybe trigger learning cycle
    _scan_count += 1
    if _scan_count % 10 == 0:
        try:
            th, sl, ex = check_outcomes()
            print(f'  [Tracker] outcomes: TARGET={th} SL={sl} EXPIRED={ex}')
            # FIX (audit #6): in-session self-tuning is FROZEN by default. It was
            # refitting params to the biased journal every 10 scans = continuous
            # overfitting to noise. Re-enable only via config.LEARNING_ENABLED
            # after a real edge clears backtest_live_pipeline.py, and even then
            # prefer OFFLINE refits with an out-of-sample holdout.
            if getattr(config, "LEARNING_ENABLED", False):
                changes = get_learner().maybe_update()
                if changes:
                    print(f'  [Learner] {len(changes)} param(s) updated: {", ".join(changes.keys())}')
                try:
                    from core.setup_detector import refresh_from_journal
                    n_setups = refresh_from_journal()
                    if n_setups:
                        print(f'  [Setups] refreshed: {n_setups} auto-mined combos')
                except Exception:
                    pass
                try:
                    from core.pattern_quality import refresh_from_journal as refresh_pq
                    n_pq = refresh_pq()
                    if n_pq:
                        print(f'  [PatternQ] refreshed: {n_pq} pattern qualities')
                except Exception:
                    pass
                engine.engine.reload_learned_params()
                lp = get_learner().get_learned_params()
                sc = lp.get("SIGNAL_CONFIG", {})
                if sc:
                    print(f'  [Learner] active: votes={sc.get("min_votes","?")} '
                          f'strength={sc.get("min_strength","?")} '
                          f'vol={sc.get("vol_surge_threshold","?")} '
                          f'rsi_os={sc.get("rsi_oversold","?")}')
            else:
                print('  [Learner] FROZEN (config.LEARNING_ENABLED=False) — no in-session param drift')
        except Exception as e:
            print(f'  [Tracker] error: {e}')

    return len(sigs)


def _explain(sym: str) -> int:
    """Trace ONE symbol through the pipeline. Captures every DEBUG/INFO
    kill/pass line the engine emits for that symbol, then prints the
    verdict. Read-only — no signals.json write, no journal."""
    buf: list = []

    class _Cap(_logging.Handler):
        def emit(self, rec):
            try:
                msg = rec.getMessage()
            except Exception:
                return
            if sym in msg:
                buf.append(f"{rec.name.split('.')[-1]}: {msg}")

    cap = _Cap()
    cap.setLevel(_logging.DEBUG)
    targets = [
        "core.signal_engine", "core.agents.signal_agent",
        "core.timeframe_sync", "core.trade_ranker", "core.order_flow",
    ]
    saved = {}
    for name in targets:
        lg = _logging.getLogger(name)
        saved[name] = lg.level
        lg.setLevel(_logging.DEBUG)
        lg.addHandler(cap)

    api = DhanAPI()
    engine = TimeframeSyncEngine()
    print(f"\n=== EXPLAIN {sym} ===")
    try:
        sigs = engine.scan_universe(api, [sym])
    except Exception as e:
        print(f"  scan error: {e}")
        sigs = []
    finally:
        for name in targets:
            lg = _logging.getLogger(name)
            lg.removeHandler(cap)
            lg.setLevel(saved[name])

    print(f"\n  pipeline trace ({len(buf)} lines):")
    if not buf:
        print("    (no stage logged this symbol - likely no OHLCV data, "
              "or killed before signal_engine. Run with --force if market shut.)")
    for line in buf:
        print(f"    {line}")

    mine = [s for s in sigs if s.get("symbol") == sym]
    print("\n  verdict:")
    if mine:
        s = mine[0]
        print(f"    PASS -> {s['direction'].upper()} grade={s['confluence_grade']} "
              f"score={s.get('confluence_score')} entry={s.get('entry_price')} "
              f"sl={s.get('sl_price')} tgt={s.get('target_price')}")
        print(f"    patterns: {s.get('patterns_combined') or s.get('reason')}")
    else:
        print("    KILLED - no signal. Last 'KILL:' line above = the gate that "
              "stopped it.")
    return 0


def main():
    parser = argparse.ArgumentParser(description='F&O signal scanner - no execution')
    parser.add_argument('--force', action='store_true', help='Run outside market hours')
    parser.add_argument('--top',   type=int, default=10)
    parser.add_argument('--explain', metavar='SYMBOL',
                        help='Trace one symbol through the pipeline: show which '
                             'stage killed or passed it, then exit.')
    parser.add_argument('--all', action='store_true',
                        help='Scan full 153 F&O universe (default: top 100 most '
                             'liquid only).')
    parser.add_argument('--orb-only', action='store_true',
                        help='ORB-only mode: disable pattern voting, trade only '
                             'Opening Range Breakouts (9:45-12:00 IST).')
    parser.add_argument('--vol-only', action='store_true',
                        help='Volatility-only mode: trade compression -> expansion '
                             'straddles/strangles (9:45-11:30 IST).')
    parser.add_argument('--no-eod', action='store_true',
                        help='Skip the post-market EOD learning batch on close.')
    args = parser.parse_args()

    if args.explain:
        return _explain(args.explain.upper())

    _signal.signal(_signal.SIGINT, _stop)

    if not config.USE_MOCK_DATA:
        check_token_health()

    api    = DhanAPI()
    engine = TimeframeSyncEngine()

    # Validate universe against live NSE F&O list — remove non-F&O stocks
    print('Validating F&O universe against NSE...')
    base_universe = FO_UNIVERSE if args.all else TOP100_FO
    print(f'  Universe pool: {len(base_universe)} stocks '
          f'({"full F&O" if args.all else "top 100 liquid"})')
    validated_universe = validate_fno_universe(base_universe)
    if len(validated_universe) < len(base_universe):
        removed = len(base_universe) - len(validated_universe)
        print(f'  Removed {removed} non-F&O stocks from universe')
    universe = validated_universe if validated_universe else base_universe

    print(f'F&O Signal Scanner v2 -- {SCAN_INTERVAL_SEC}s interval -- SIGNAL ONLY (no orders)')
    print(f'Signals file: {SIGNALS_FILE}')
    print(f'Universe: {len(universe)} F&O stocks (validated)')
    print('Run  streamlit run streamlit_app.py  in another terminal for live UI.\n')

    while _running:
        if not args.force and not _market_open():
            n = datetime.now()
            open_min = _OPEN[0]*60 + _OPEN[1]
            cur_min  = n.hour*60 + n.minute
            if cur_min < open_min:
                wait = (open_min - cur_min) * 60
                print(f'Pre-market. Opens in {wait//60}m {wait%60}s. Sleeping 60s...')
                for _ in range(60):
                    if not _running:
                        break
                    time.sleep(1)
            else:
                print('Market closed.')
                if not args.no_eod:
                    try:
                        print('Running post-market EOD learning batch...')
                        from core.eod_learn import run_eod
                        run_eod()
                    except Exception as e:
                        print(f'  EOD batch error (non-fatal): {e}')
                print('Exiting.')
                break
            continue

        _scan(engine, api, args.top, universe=universe,
              orb_only=args.orb_only, vol_only=args.vol_only)

        for _ in range(SCAN_INTERVAL_SEC):
            if not _running:
                break
            time.sleep(1)

    print('Scanner stopped.')


if __name__ == '__main__':
    main()
