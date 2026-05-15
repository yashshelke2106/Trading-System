import os
import sys
import signal as _signal
import time
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging as _logging

class _DelistedFilter(_logging.Filter):
    def filter(self, record):
        return "possibly delisted" not in record.getMessage()

_logging.getLogger("yfinance").addFilter(_DelistedFilter())

import config
from core.api_dhan import DhanAPI, check_token_health
from core.timeframe_sync import TimeframeSyncEngine
from core.signal_writer import write_signals, SIGNALS_FILE
from core.universe import FO_UNIVERSE
from core.signal_journal import record_signal
from core.signal_tracker import check_outcomes
from core.adaptive_learner import get_learner
from core.option_translator import get_option_rec
from core.dashboard_data import get_option_chain
from core.nse_option_chain import validate_fno_universe

_scan_count = 0

SCAN_INTERVAL_SEC = 30
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


def _scan(engine, api, top_n, universe=None):
    global _scan_count
    universe = universe or FO_UNIVERSE
    ts = datetime.now().strftime('%H:%M:%S')
    t0 = datetime.now()
    print(f'[{ts}] Scanning {len(universe)} symbols (1D+15m+5m confluence)...')
    sigs = engine.scan_universe(api, universe)
    elapsed = round((datetime.now() - t0).total_seconds(), 2)

    # Enrich each signal with live option chain data.
    # HARD GATE: drop signals where option chain is empty (not F&O eligible).
    # Only F&O-tradeable signals survive.
    enriched = []
    dropped_no_chain = []
    for s in sigs:
        try:
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
                # Sanity check: option SL/target must be within 3x of entry premium.
                entry_p = rec["entry_prem"]
                if entry_p > 0:
                    sl_ratio = abs(entry_p - rec["sl_prem"]) / entry_p
                    tgt_ratio = abs(rec["target_prem"] - entry_p) / entry_p
                    if sl_ratio > 3.0 or tgt_ratio > 3.0:
                        dropped_no_chain.append(s["symbol"] + "(bad_prem)")
                        continue

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
                })
                enriched.append(s)
            else:
                dropped_no_chain.append(s["symbol"] + "(no_rec)")
        except Exception:
            pass

    if dropped_no_chain:
        print(f'  Dropped {len(dropped_no_chain)} non-F&O/bad-chain: {", ".join(dropped_no_chain[:10])}')
    sigs = enriched
        try:
            record_signal(s)
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
        print(f'  {"Symbol":<14} {"Dir":<6} {"Grd":<4} {"Score":>5} {"Entry":>9} {"SL":>9}  Reason')
        print('  ' + '-' * 75)
        for s in top:
            grade = s["confluence_grade"]
            prefix = '* ' if grade == 'S' else '  '
            line = (
                f'{prefix}{s["symbol"]:<14} {s["direction"].upper():<6} '
                f'{grade:<4} {s["confluence_score"]:>5} '
                f'{s["entry_price"]:>9.2f} {s["sl_price"]:>9.2f}  '
                f'{s["reason"][:48]}'
            )
            if grade == 'S':
                line += ' [~75-80% WR T1]'
            print(line)
    else:
        print('  No signals above threshold.')

    # Every 10 scans: resolve open signal outcomes + maybe trigger learning cycle
    _scan_count += 1
    if _scan_count % 10 == 0:
        try:
            th, sl, ex = check_outcomes()
            print(f'  [Tracker] outcomes: TARGET={th} SL={sl} EXPIRED={ex}')
            changes = get_learner().maybe_update()
            if changes:
                print(f'  [Learner] {len(changes)} param(s) updated: {", ".join(changes.keys())}')
            # Reload learned params into running SignalEngine immediately — closes feedback loop
            engine.engine.reload_learned_params()
            lp = get_learner().get_learned_params()
            sc = lp.get("SIGNAL_CONFIG", {})
            if sc:
                print(f'  [Learner] active: votes={sc.get("min_votes","?")} '
                      f'strength={sc.get("min_strength","?")} '
                      f'vol={sc.get("vol_surge_threshold","?")} '
                      f'rsi_os={sc.get("rsi_oversold","?")}')
        except Exception as e:
            print(f'  [Tracker] error: {e}')

    return len(sigs)


def main():
    parser = argparse.ArgumentParser(description='F&O signal scanner - no execution')
    parser.add_argument('--force', action='store_true', help='Run outside market hours')
    parser.add_argument('--top',   type=int, default=10)
    args = parser.parse_args()

    _signal.signal(_signal.SIGINT, _stop)

    if not config.USE_MOCK_DATA:
        check_token_health()

    api    = DhanAPI()
    engine = TimeframeSyncEngine()

    # Validate universe against live NSE F&O list — remove non-F&O stocks
    print('Validating F&O universe against NSE...')
    validated_universe = validate_fno_universe(FO_UNIVERSE)
    if len(validated_universe) < len(FO_UNIVERSE):
        removed = len(FO_UNIVERSE) - len(validated_universe)
        print(f'  Removed {removed} non-F&O stocks from universe')
    universe = validated_universe if validated_universe else FO_UNIVERSE

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
                print('Market closed. Exiting.')
                break
            continue

        _scan(engine, api, args.top, universe=universe)

        for _ in range(SCAN_INTERVAL_SEC):
            if not _running:
                break
            time.sleep(1)

    print('Scanner stopped.')


if __name__ == '__main__':
    main()
