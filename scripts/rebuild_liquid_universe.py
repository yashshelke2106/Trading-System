"""
rebuild_liquid_universe.py — regenerate the TOP100_LIQUID list in
core/universe.py, ranked by MEASURED average daily turnover (close*volume,
trailing ~252 sessions) from logs/bar_cache. Run after refreshing the cache.

    python scripts/rebuild_liquid_universe.py            # print the ranked literal
    python scripts/rebuild_liquid_universe.py --write    # rewrite universe.py

Swing rationale: turnover (not price) is the liquidity that matters when
holding across days — it sets spread cost and fill reliability.
"""
import argparse
import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.universe import FO_UNIVERSE  # noqa: E402

UNIVERSE_PY = os.path.join(os.path.dirname(__file__), "..", "core", "universe.py")
LOOKBACK = 252
MIN_BARS = 250
TOP_N = 100


def ranked_liquid() -> list:
    turn = {}
    for sym in FO_UNIVERSE:
        p = os.path.join("logs", "bar_cache", f"{sym}.parquet")
        if not os.path.exists(p):
            continue
        d = pd.read_parquet(p).sort_index()
        if len(d) < MIN_BARS or "volume" not in d:
            continue
        turn[sym] = float((d["close"] * d["volume"]).tail(LOOKBACK).mean())
    return sorted(turn, key=turn.get, reverse=True)[:TOP_N]


def as_literal(names: list) -> str:
    rows = ["    " + " ".join(f'"{s}",' for s in names[i:i + 5])
            for i in range(0, len(names), 5)]
    return "TOP100_LIQUID = [\n" + "\n".join(rows) + "\n]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="rewrite core/universe.py")
    args = ap.parse_args()

    names = ranked_liquid()
    lit = as_literal(names)
    if not args.write:
        print(lit)
        print(f"\n# {len(names)} names ranked by trailing-{LOOKBACK} turnover")
        return 0

    src = open(UNIVERSE_PY, encoding="utf-8").read()
    new = re.sub(r"TOP100_LIQUID = \[.*?\]", lit, src, count=1, flags=re.DOTALL)
    if new == src:
        print("could not locate TOP100_LIQUID block — aborting")
        return 1
    open(UNIVERSE_PY, "w", encoding="utf-8").write(new)
    print(f"rewrote {len(names)} names into core/universe.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
