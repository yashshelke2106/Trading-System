"""
fo_bhavcopy_archive.py — daily NSE F&O bhavcopy (futures + options, with OI).

WHY
---
The cash bhavcopy archive (logs/bhavcopy_archive) has price and volume but no
OPEN INTEREST, so the one institutional signal this system has never been able
to test -- futures positioning, i.e. long buildup / short buildup / short
covering / long unwinding -- had no data behind it. The F&O bhavcopy carries
OpnIntrst and ChngInOpnIntrst per contract per day, plus the full option chain
(strike, CE/PE, OI, volume), which is also what a real IV-rank or PCR history
would need.

SOURCE
    https://nsearchives.nseindia.com/content/fo/
        BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip     (~1 MB/day)

WHAT IS KEPT
    futures rows (STF/IDF)  -> logs/fo_bhavcopy/fut/<YYYYMMDD>.parquet
    option rows  (OPTSTK/   -> logs/fo_bhavcopy/opt/<YYYYMMDD>.parquet
                  OPTIDX)
Split because the futures file is tiny (~600 rows) and gets read on every
study, while the options file is ~32k rows and is only needed for chain work.

RESUMABLE: existing days are skipped, so this can be re-run to extend or to
fill gaps after an interruption. Weekends/holidays return 404 and are recorded
as misses so they are not retried forever.

    python fo_bhavcopy_archive.py --days 250
    python fo_bhavcopy_archive.py --from 2024-01-01 --to 2024-12-31
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
import zipfile
from datetime import date, timedelta
from typing import Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "logs", "fo_bhavcopy")
FUT_DIR = os.path.join(OUT, "fut")
OPT_DIR = os.path.join(OUT, "opt")
MISS = os.path.join(OUT, "_misses.txt")

URL = ("https://nsearchives.nseindia.com/content/fo/"
       "BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip")

FUT_TYPES = ("STF", "IDF")
OPT_TYPES = ("STO", "IDO", "OPTSTK", "OPTIDX")

KEEP_FUT = ["TradDt", "TckrSymb", "FinInstrmTp", "XpryDt", "OpnPric", "HghPric",
            "LwPric", "ClsPric", "PrvsClsgPric", "UndrlygPric", "OpnIntrst",
            "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd"]
KEEP_OPT = KEEP_FUT + ["StrkPric", "OptnTp"]


def _load_misses() -> set:
    if not os.path.exists(MISS):
        return set()
    return {ln.strip() for ln in open(MISS, encoding="utf-8") if ln.strip()}


def _record_miss(ymd: str) -> None:
    with open(MISS, "a", encoding="utf-8") as f:
        f.write(ymd + "\n")


def fetch_day(session, d: date, timeout: int = 30) -> Optional[pd.DataFrame]:
    ymd = d.strftime("%Y%m%d")
    try:
        r = session.get(URL.format(ymd=ymd), timeout=timeout, verify=False)
    except Exception:
        return None
    if r.status_code != 200 or len(r.content) < 5000:
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        return pd.read_csv(z.open(z.namelist()[0]))
    except Exception:
        return None


def _subset(df: pd.DataFrame, types, keep) -> pd.DataFrame:
    if "FinInstrmTp" not in df.columns:
        return pd.DataFrame()
    out = df[df["FinInstrmTp"].isin(types)]
    cols = [c for c in keep if c in out.columns]
    return out[cols].copy()


def build(start: date, end: date, sleep: float = 0.4) -> None:
    from bhavcopy_archive import _get_session       # reuses NSE session/headers

    os.makedirs(FUT_DIR, exist_ok=True)
    os.makedirs(OPT_DIR, exist_ok=True)
    session = _get_session()
    misses = _load_misses()

    ok = skipped = missing = 0
    d = start
    while d <= end:
        ymd = d.strftime("%Y%m%d")
        fut_p = os.path.join(FUT_DIR, f"{ymd}.parquet")
        if d.weekday() >= 5 or ymd in misses:
            d += timedelta(days=1)
            continue
        if os.path.exists(fut_p):
            skipped += 1
            d += timedelta(days=1)
            continue

        df = fetch_day(session, d)
        if df is None or df.empty:
            missing += 1
            _record_miss(ymd)          # holiday or not published
            d += timedelta(days=1)
            time.sleep(sleep)
            continue

        fut = _subset(df, FUT_TYPES, KEEP_FUT)
        opt = _subset(df, OPT_TYPES, KEEP_OPT)
        if not fut.empty:
            fut.to_parquet(fut_p, index=False)
        if not opt.empty:
            opt.to_parquet(os.path.join(OPT_DIR, f"{ymd}.parquet"), index=False)
        ok += 1
        if ok % 25 == 0:
            print(f"  [{ymd}] {ok} days written "
                  f"(fut {len(fut)}, opt {len(opt)})", flush=True)
        d += timedelta(days=1)
        time.sleep(sleep)

    print(f"\nDONE  written={ok}  skipped(existing)={skipped}  missing={missing}")
    print(f"futures: {FUT_DIR}")
    print(f"options: {OPT_DIR}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=250,
                   help="calendar days back from today")
    p.add_argument("--from", dest="frm", default="")
    p.add_argument("--to", dest="to", default="")
    p.add_argument("--sleep", type=float, default=0.4)
    a = p.parse_args()

    if a.frm and a.to:
        y, m, dd = (int(x) for x in a.frm.split("-"))
        start = date(y, m, dd)
        y, m, dd = (int(x) for x in a.to.split("-"))
        end = date(y, m, dd)
    else:
        end = date.today()
        start = end - timedelta(days=a.days)

    print(f"F&O bhavcopy archive: {start} .. {end}")
    build(start, end, a.sleep)


if __name__ == "__main__":
    main()
