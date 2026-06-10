"""
F&O breakout scanner with live options context — a WATCHLIST + DATA-COLLECTOR.

Finds F&O-segment stocks that are breaking out of a daily swing high RIGHT NOW,
and tags each with the live options signature (PCR, OI support/resistance walls)
the running system already collected in logs/oi_snapshots.jsonl. Then LOGS the
combined snapshot so that, over months, you accumulate the one dataset that could
eventually TEST whether the options signature predicts which breakouts work.

  python fno_breakout_options_scan.py            # scan, print ranked watchlist
  python fno_breakout_options_scan.py --window 3 # only breaks in last 3 days

═══════════════════════════════════════════════════════════════════════════════
HONESTY — READ THIS (it is not optional):
  * This is NOT a validated edge and NOT a buy signal. The 13,238-event study
    (breakout_fingerprint.py) PROVED daily swing breakouts on NSE F&O slightly
    FADE and lose after costs, and that NO indicator at the breakout separates
    winners from losers.
  * The options-signature conditioning is UNTESTED and currently UNTESTABLE —
    there are only ~14 days of OI history (Dhan gives no multi-year chain data),
    nowhere near enough to validate anything.
  * So treat this as: (a) a discretionary watchlist of where to LOOK, and
    (b) a forward data-collector. Its log (logs/breakout_options_watch.jsonl)
    is the asset — in a few months it can be joined to outcomes and tested for
    real with the same anti-mirage rigor as everything else.
  Do NOT size up on this. Money belongs in index/factor funds (the research
  verdict). This is a research instrument.
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations
import os, sys, json, argparse
from datetime import datetime, date
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logging
for n in ("core.api_dhan", "core.bar_cache"):
    logging.getLogger(n).setLevel(logging.CRITICAL)

OI_FILE  = os.path.join("logs", "oi_snapshots.jsonl")
OUT_FILE = os.path.join("logs", "breakout_options_watch.jsonl")
BREAK_WINDOW = 5     # consider a break "fresh" if it crossed within this many bars


def latest_oi() -> dict:
    """Latest OI snapshot per symbol from what the live system already collected."""
    snaps = {}
    if not os.path.exists(OI_FILE):
        return snaps
    for line in open(OI_FILE, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        s = r.get("symbol")
        if s and (s not in snaps or r.get("ts", "") > snaps[s].get("ts", "")):
            snaps[s] = r
    return snaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=BREAK_WINDOW,
                    help="break must have crossed within this many trading days")
    ap.add_argument("--days", type=int, default=300)
    args = ap.parse_args()

    from core.bar_cache import cached_daily
    from core.universe import FO_UNIVERSE
    from core.strategy_india_swing import _swing_high

    oi = latest_oi()
    print("=" * 96)
    print(f"  F&O BREAKOUT WATCHLIST + OPTIONS CONTEXT   {datetime.now():%Y-%m-%d %H:%M}   "
          f"(OI snapshots for {len(oi)} symbols)")
    print("  NOT a buy signal — daily breakouts PROVEN to fade; options conditioning UNTESTED. "
          "Watchlist + data-collector only.")
    print("=" * 96)

    rows = []
    for sym in FO_UNIVERSE:
        try:
            df = cached_daily(sym, args.days)
            if df is None or len(df) < 60:
                continue
            df = df[~df.index.duplicated(keep="last")].sort_index()
            close = df["close"].values
            vol = df["volume"].values
            level = _swing_high(df)                       # most-recent confirmed pivot high
            if not level or close[-1] <= level:
                continue
            # how many bars since the cross (fresh breakouts only)
            since = 0
            for k in range(1, args.window + 2):
                if len(close) > k and close[-1 - k] <= level:
                    since = k; break
            if since == 0 or since > args.window:
                continue
            ext = (close[-1] - level) / level * 100        # % above the broken level
            vr = vol[-1] / max(vol[-20:].mean(), 1)        # volume vs 20d avg
            o = oi.get(sym, {})
            rows.append({
                "symbol": sym, "close": round(float(close[-1]), 1),
                "level": round(float(level), 1), "ext_pct": round(ext, 1),
                "days_since": since, "vol_x": round(float(vr), 2),
                "pcr": o.get("pcr"), "support": o.get("max_pe_oi_strike"),
                "resistance": o.get("max_ce_oi_strike"), "oi_ts": o.get("ts"),
            })
        except Exception:
            continue

    if not rows:
        print("\n  No fresh F&O breakouts in the window."); return
    # rank: freshest + has options context + moderate (not over-) extension first
    rows.sort(key=lambda r: (r["days_since"], -(r["pcr"] is not None), r["ext_pct"]))

    print(f"\n  {len(rows)} F&O stock(s) breaking a daily swing high (last {args.window}d)\n")
    print(f"  {'SYMBOL':12} {'close':>8} {'broke>':>8} {'ext%':>5} {'dSince':>6} "
          f"{'vol×':>5} | {'PCR':>5} {'support':>8} {'resist':>8} {'headroom%':>9}  OI age")
    print("  " + "-" * 92)
    for r in rows:
        head = ""
        if r["resistance"] and r["close"]:
            head = f"{(r['resistance']-r['close'])/r['close']*100:+.1f}"
        age = ""
        if r["oi_ts"]:
            try:
                age = f"{(date.today()-date.fromisoformat(str(r['oi_ts'])[:10])).days}d"
            except Exception:
                age = "?"
        pcr = f"{r['pcr']:.2f}" if r["pcr"] is not None else "  —"
        sup = f"{r['support']:.0f}" if r["support"] else "    —"
        res = f"{r['resistance']:.0f}" if r["resistance"] else "    —"
        flag = " ⚠ext" if r["ext_pct"] > 5 else ""      # over-extended = worse (study)
        print(f"  {r['symbol']:12} {r['close']:>8} {r['level']:>8} {r['ext_pct']:>5} "
              f"{r['days_since']:>6} {r['vol_x']:>5} | {pcr:>5} {sup:>8} {res:>8} "
              f"{head:>9}  {age:>4}{flag}")

    # forward data-collection: append today's watchlist (outcomes joined later)
    with open(OUT_FILE, "a", encoding="utf-8") as f:
        stamp = datetime.now().isoformat()
        for r in rows:
            f.write(json.dumps({**r, "scan_ts": stamp}) + "\n")
    print(f"\n  logged {len(rows)} rows → {OUT_FILE} (forward dataset; join to outcomes later)")
    print("  PCR<1 = call-heavy, PCR>1 = put-heavy. support/resist = biggest PE/CE OI walls.")
    print("  headroom% = room to the call-OI resistance wall. ⚠ext = >5% past the level "
          "(the study showed over-extended breaks fare worse).")


if __name__ == "__main__":
    main()
