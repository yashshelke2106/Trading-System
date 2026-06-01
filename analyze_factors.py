"""
Factor analysis — what actually separates winning trades from SL hits.

Reads closed trades (live journal or a backtest CSV), measures the SPOT move
(the true directional outcome — NOT option premium, which theta/IV pollute),
and reports which signal conditions discriminate winners from losers.

CRITICAL METHOD: every factor is reported WITHIN each direction. A single
trending month makes one side look brilliant and the other terrible
(e.g. a down-month makes shorts look 81% and longs 48%) — that's a regime
artifact, not an edge. A factor only counts as REAL if it separates winners
from losers *inside* a direction, where the market trend is held constant.

Run:
    python analyze_factors.py                       # live journal (default)
    python analyze_factors.py --csv backtest_india_swing_trades.csv
    python analyze_factors.py --min-n 12            # raise bucket floor

Use this BEFORE removing or adding any factor. Act only on findings that
(a) discriminate within-direction and (b) have a plausible mechanism — never
on a single-window direction/regime quirk.
"""

from __future__ import annotations

import argparse
import csv as _csv
import json
from collections import defaultdict
from pathlib import Path
import statistics as st

ROOT = Path(__file__).resolve().parent


def _is_junk(r):
    e = r.get("extra") or {}
    return isinstance(e, dict) and bool(e.get("replay_failed"))


def _spot_move(r):
    """Signed spot % move in the trade's direction — the futures outcome."""
    ep = r.get("entry_price"); xp = r.get("exit_price")
    d = str(r.get("direction", "long")).lower()
    try:
        ep = float(ep); xp = float(xp)
    except (TypeError, ValueError):
        return None
    if ep <= 0 or xp <= 0:
        return None
    return (xp - ep) / ep * 100 if d == "long" else (ep - xp) / ep * 100


def _load(path: Path, is_csv: bool):
    rows = []
    if is_csv:
        with open(path) as f:
            for r in _csv.DictReader(f):
                for k in ("entry_price", "exit_price", "rsi", "score", "volume_ratio", "vote_margin"):
                    if k in r and r[k] not in ("", None):
                        try: r[k] = float(r[k])
                        except ValueError: pass
                rows.append(r)
    else:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try: rows.append(json.loads(line))
            except Exception: continue
    return rows


def _num(v):
    return isinstance(v, (int, float))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None, help="Backtest CSV instead of the live journal")
    ap.add_argument("--min-n", type=int, default=8, help="Min trades per bucket to report")
    args = ap.parse_args()

    if args.csv:
        path = Path(args.csv); is_csv = True
    else:
        path = ROOT / "logs" / "signal_journal.jsonl"; is_csv = False
    if not path.exists():
        print(f"Not found: {path}"); return

    rows = _load(path, is_csv)
    T = []
    for r in rows:
        if not is_csv and _is_junk(r):
            continue
        m = _spot_move(r)
        if m is None:
            continue
        T.append({**r, "_spot": m, "_win": m > 0.0})

    if not T:
        print("No usable closed trades with spot outcome."); return

    base_wr = sum(t["_win"] for t in T) / len(T) * 100
    print(f"Source: {path.name}   trades: {len(T)}   overall spot WR {base_wr:.0f}%\n")

    NUM_FACTORS = {
        "rsi":          [("<40", 0, 40), ("40-50", 40, 50), ("50-60", 50, 60), ("60-70", 60, 70), ("70+", 70, 999)],
        "score":        [("<80", 0, 80), ("80-100", 80, 100), ("100-130", 100, 130), ("130+", 130, 1e9)],
        "volume_ratio": [("<1.5x", 0, 1.5), ("1.5-2x", 1.5, 2), ("2-2.5x", 2, 2.5), ("2.5x+", 2.5, 1e9)],
        "vote_margin":  [("<=7", 0, 7.01), ("8-10", 7.01, 10.01), ("11+", 10.01, 1e9)],
    }
    CAT_FACTORS = ["grade", "confluence_grade"]

    for dirn in ("long", "short"):
        D = [t for t in T if str(t.get("direction", "")).lower() == dirn]
        if len(D) < args.min_n:
            continue
        dwr = sum(t["_win"] for t in D) / len(D) * 100
        print(f"{'='*64}\n {dirn.upper()} only - n={len(D)}, within-dir base WR {dwr:.0f}%\n{'='*64}")

        for fac, buckets in NUM_FACTORS.items():
            present = [t for t in D if _num(t.get(fac))]
            if len(present) < args.min_n:
                continue
            print(f"  -- {fac} --")
            for lab, lo, hi in buckets:
                sub = [t for t in present if lo <= t[fac] < hi]
                if len(sub) < args.min_n:
                    continue
                wr = sum(s["_win"] for s in sub) / len(sub) * 100
                exp = st.mean(s["_spot"] for s in sub)
                edge = wr - dwr
                print(f"     {lab:12} n={len(sub):3} WR {wr:4.0f}% exp {exp:+.2f}% edge {edge:+4.0f}pt"
                      f"{'  <<<' if abs(edge) >= 10 else ''}")

        for fac in CAT_FACTORS:
            vals = defaultdict(list)
            for t in D:
                v = t.get(fac)
                if v:
                    vals[str(v)].append(t)
            if not vals:
                continue
            shown = False
            for v, sub in sorted(vals.items()):
                if len(sub) < args.min_n:
                    continue
                if not shown:
                    print(f"  -- {fac} --"); shown = True
                wr = sum(s["_win"] for s in sub) / len(sub) * 100
                edge = wr - dwr
                print(f"     {v:12} n={len(sub):3} WR {wr:4.0f}% edge {edge:+4.0f}pt"
                      f"{'  <<<' if abs(edge) >= 10 else ''}")
        print()

    # Per-pattern loss-magnet scan, within longs (the side we trade)
    print(f"{'='*64}\n PATTERN loss-magnets (LONGS) — patterns in losing long trades\n{'='*64}")
    longs = [t for t in T if str(t.get("direction", "")).lower() == "long"]
    if longs:
        lwr = sum(t["_win"] for t in longs) / len(longs) * 100
        pat = defaultdict(list)
        for t in longs:
            ps = t.get("patterns") or t.get("patterns_combined") or []
            if isinstance(ps, str):
                ps = [p.strip() for p in ps.split(",")]
            for p in ps:
                if p:
                    pat[p].append(t["_win"])
        ranked = sorted(((p, sum(w) / len(w) * 100, len(w)) for p, w in pat.items() if len(w) >= 10),
                        key=lambda x: x[1])
        print(f"  (long base WR {lwr:.0f}%)  — bottom 10 by WR:")
        for p, wr, n in ranked[:10]:
            print(f"     {p:30} n={n:3} WR {wr:4.0f}% {wr-lwr:+4.0f}pt"
                  f"{'  LOSS-MAGNET' if wr < lwr - 12 else ''}")

    print("\nGuidance: act only on factors that discriminate WITHIN a direction")
    print("AND have a mechanism. Direction/regime splits across the whole sample")
    print("are artifacts of the window's trend — do NOT hardcode them.")


if __name__ == "__main__":
    main()
