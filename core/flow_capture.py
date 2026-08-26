"""
core/flow_capture.py — institutional flow capture (FII / DII / Pro / Client).

WHY
---
The hypothesis registry has 13 closed trials and none of them touch
institutional POSITIONING, because the data was never captured. Every hunt so
far has run on price/volume/OI derived from the tape itself -- which is exactly
the information every other participant already has. Participant-wise OI is a
different axis: it says WHO is on each side, published daily by NSCCL, free,
and archived back years.

This module captures three things:

  1. participant-wise OPEN INTEREST   (positioning: who is net long/short)
  2. participant-wise TRADING VOLUME  (activity: who is churning)
  3. FII/DII CASH-segment buy/sell    (the daily flow number the press quotes)

SOURCES
    https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_<DDMMYYYY>.csv
    https://nsearchives.nseindia.com/content/nsccl/fao_participant_vol_<DDMMYYYY>.csv
    https://www.nseindia.com/api/fiidiiTradeReact          (current day only)

POINT-IN-TIME DISCIPLINE
    Participant OI for trade date T is published by NSCCL after the close of T.
    It is therefore NOT actionable during session T. `load_panel()` attaches an
    `available_from` column = the next trading date present in the archive, and
    every study MUST join on `available_from`, never on `trade_date`. Joining on
    trade_date is a one-line way to manufacture a lookahead edge -- this project
    has already burned itself on open-fill and forming-bar leaks, so the guard
    is built into the loader rather than left to the caller.

    The cash FII/DII endpoint returns only the current day and has no archive,
    so it accumulates forward from first run. There is no backfill; a study that
    needs cash-flow history has to wait for it to build.

NEVER FABRICATES
    A failed fetch writes nothing and records a miss. No zero-filling, no
    forward-filling, no "last known value" -- a gap stays a gap.

RUN
    python -m core.flow_capture --days 400        # backfill participant archive
    python -m core.flow_capture --from 2020-01-01 --to 2026-08-17
    python -m core.flow_capture --cash            # append today's FII/DII cash
    python -m core.flow_capture --status
    python -m core.flow_capture --selftest
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ARCHIVE = os.path.join(_ROOT, "logs", "flow_archive")
OI_DIR = os.path.join(ARCHIVE, "participant_oi")
VOL_DIR = os.path.join(ARCHIVE, "participant_vol")
CASH_PATH = os.path.join(ARCHIVE, "fii_dii_cash.jsonl")
MISS_PATH = os.path.join(ARCHIVE, "_misses.txt")

OI_URL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{dmy}.csv"
VOL_URL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_vol_{dmy}.csv"
CASH_URL = "https://www.nseindia.com/api/fiidiiTradeReact"

# NSE ships these with trailing whitespace in the header; normalised on read.
COLMAP = {
    "Client Type": "participant",
    "Future Index Long": "fut_idx_long",
    "Future Index Short": "fut_idx_short",
    "Future Stock Long": "fut_stk_long",
    "Future Stock Short": "fut_stk_short",
    "Option Index Call Long": "opt_idx_ce_long",
    "Option Index Put Long": "opt_idx_pe_long",
    "Option Index Call Short": "opt_idx_ce_short",
    "Option Index Put Short": "opt_idx_pe_short",
    "Option Stock Call Long": "opt_stk_ce_long",
    "Option Stock Put Long": "opt_stk_pe_long",
    "Option Stock Call Short": "opt_stk_ce_short",
    "Option Stock Put Short": "opt_stk_pe_short",
    "Total Long Contracts": "total_long",
    "Total Short Contracts": "total_short",
}

NUMERIC = [v for v in COLMAP.values() if v != "participant"]
PARTICIPANTS = ("Client", "DII", "FII", "Pro", "TOTAL")

# Largest believable gap between consecutive NSE sessions. Diwali/long weekends
# run to ~5 days; 10 is generous. Beyond this the archive has a HOLE, and the
# next file present is not the next session -- it is simply the next thing that
# was captured. Bridging a hole would date a 2015 observation as actionable in
# 2026 and report an 11-year position change as a one-day move. Gaps are marked
# unknown instead, because a missing day must stay missing.
MAX_SESSION_GAP_DAYS = 10


# ----------------------------------------------------------------- plumbing --

def _get_session():
    """Reuse the warmed NSE session/headers from the bhavcopy archiver."""
    sys.path.insert(0, _ROOT)
    from bhavcopy_archive import _get_session as _s  # noqa: WPS433
    return _s()


def _load_misses() -> set:
    if not os.path.exists(MISS_PATH):
        return set()
    return {ln.strip() for ln in open(MISS_PATH, encoding="utf-8") if ln.strip()}


def _record_miss(key: str) -> None:
    os.makedirs(ARCHIVE, exist_ok=True)
    with open(MISS_PATH, "a", encoding="utf-8") as f:
        f.write(key + "\n")


# ------------------------------------------------------------------ parsing --

def parse_participant_csv(text: str, trade_date: date) -> Optional[pd.DataFrame]:
    """Parse an NSCCL participant CSV into tidy rows. None if unrecognisable."""
    try:
        df = pd.read_csv(io.StringIO(text), skiprows=1)
    except Exception:
        return None

    df.columns = [str(c).strip() for c in df.columns]
    known = {c: COLMAP[c] for c in df.columns if c in COLMAP}
    if "Client Type" not in known:
        return None
    df = df[list(known)].rename(columns=known)

    df["participant"] = df["participant"].astype(str).str.strip()
    df = df[df["participant"].isin(PARTICIPANTS)]
    if df.empty:
        return None

    for c in NUMERIC:
        if c in df.columns:
            df[c] = pd.to_numeric(
                df[c].astype(str).str.replace(",", "", regex=False),
                errors="coerce",
            )
        else:
            # Older files predate stock options. NaN (not 0) keeps "we do not
            # know" distinct from "the position was flat"; float (not pd.NA)
            # keeps the dtype stable so concatenating a 2012 file with a 2026
            # file does not silently change column types.
            df[c] = float("nan")
        df[c] = df[c].astype("float64")

    df.insert(0, "trade_date", pd.Timestamp(trade_date).normalize())
    return df[["trade_date", "participant"] + NUMERIC].reset_index(drop=True)


def fetch_participant(session, d: date, kind: str, timeout: int = 25
                      ) -> Optional[pd.DataFrame]:
    """kind is 'oi' or 'vol'. None on any failure (holiday, 404, garbage)."""
    url = (OI_URL if kind == "oi" else VOL_URL).format(dmy=d.strftime("%d%m%Y"))
    try:
        r = session.get(url, timeout=timeout, verify=False)
    except Exception:
        return None
    if r.status_code != 200 or len(r.content) < 300:
        return None
    return parse_participant_csv(r.text, d)


def fetch_cash_flow(session, timeout: int = 20) -> Optional[List[Dict]]:
    """
    Current-day FII/DII cash-segment buy/sell (Rs crore). None on failure.

    Accept-Encoding is overridden per-request: the shared archive session asks
    for `br`, www.nseindia.com honours it, and without the brotli package the
    body decodes to mojibake that fails json() with a misleading 200 status.
    The archives host does not serve brotli, so only this endpoint needs it.
    """
    try:
        r = session.get(CASH_URL, timeout=timeout, verify=False,
                        headers={"Accept-Encoding": "gzip, deflate"})
        rows = r.json()
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None

    out = []
    for row in rows:
        try:
            d = datetime.strptime(str(row["date"]).strip(), "%d-%b-%Y").date()
        except Exception:
            continue
        out.append({
            "trade_date": d.isoformat(),
            "participant": str(row.get("category", "")).strip(),
            "buy_cr": float(row.get("buyValue") or 0.0),
            "sell_cr": float(row.get("sellValue") or 0.0),
            "net_cr": float(row.get("netValue") or 0.0),
            "captured_at": datetime.now().isoformat(timespec="seconds"),
        })
    return out or None


# ------------------------------------------------------------------ writing --

def append_cash(rows: List[Dict]) -> int:
    """Append-only, idempotent on (trade_date, participant)."""
    os.makedirs(ARCHIVE, exist_ok=True)
    seen = set()
    if os.path.exists(CASH_PATH):
        for ln in open(CASH_PATH, encoding="utf-8"):
            try:
                r = json.loads(ln)
                seen.add((r["trade_date"], r["participant"]))
            except Exception:
                continue

    added = 0
    with open(CASH_PATH, "a", encoding="utf-8") as f:
        for r in rows:
            if (r["trade_date"], r["participant"]) in seen:
                continue
            f.write(json.dumps(r) + "\n")
            added += 1
    return added


def build(start: date, end: date, sleep: float = 0.4, verbose: bool = True) -> Dict:
    """Backfill the participant archive over [start, end]. Resumable."""
    os.makedirs(OI_DIR, exist_ok=True)
    os.makedirs(VOL_DIR, exist_ok=True)
    session = _get_session()
    misses = _load_misses()

    ok = skipped = missing = 0
    d = start
    while d <= end:
        ymd = d.strftime("%Y%m%d")
        oi_path = os.path.join(OI_DIR, f"{ymd}.parquet")

        if d.weekday() >= 5 or ymd in misses:
            d += timedelta(days=1)
            continue
        if os.path.exists(oi_path):
            skipped += 1
            d += timedelta(days=1)
            continue

        oi = fetch_participant(session, d, "oi")
        if oi is None or oi.empty:
            missing += 1
            _record_miss(ymd)              # holiday or not published
            d += timedelta(days=1)
            time.sleep(sleep)
            continue

        oi.to_parquet(oi_path, index=False)
        vol = fetch_participant(session, d, "vol")
        if vol is not None and not vol.empty:
            vol.to_parquet(os.path.join(VOL_DIR, f"{ymd}.parquet"), index=False)

        ok += 1
        if verbose and ok % 25 == 0:
            print(f"  [{ymd}] {ok} days written", flush=True)
        d += timedelta(days=1)
        time.sleep(sleep)

    if verbose:
        print(f"\nDONE  written={ok}  skipped(existing)={skipped}  missing={missing}")
        print(f"  {OI_DIR}")
    return {"written": ok, "skipped": skipped, "missing": missing}


# ------------------------------------------------------------------ loading --

def _read_dir(path: str) -> pd.DataFrame:
    if not os.path.isdir(path):
        return pd.DataFrame()
    files = sorted(f for f in os.listdir(path) if f.endswith(".parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat(
        [pd.read_parquet(os.path.join(path, f)) for f in files],
        ignore_index=True,
    )


def load_panel(kind: str = "oi", derived: bool = True) -> pd.DataFrame:
    """
    Full participant panel, one row per (trade_date, participant).

    Adds `available_from` = the NEXT trading date in the archive. NSCCL publishes
    after the close, so this row cannot inform a decision on trade_date itself.
    Studies must join on available_from. The last date has available_from = NaT
    (its successor has not happened yet) and is dropped by `as_signal()`.
    """
    df = _read_dir(OI_DIR if kind == "oi" else VOL_DIR)
    if df.empty:
        return df

    df = df.sort_values(["trade_date", "participant"]).reset_index(drop=True)

    sessions = pd.Index(sorted(df["trade_date"].unique()))
    nxt = {}
    for i, t in enumerate(sessions):
        if i + 1 >= len(sessions):
            nxt[t] = pd.NaT                      # successor has not happened yet
            continue
        nxt_t = sessions[i + 1]
        # A hole in the archive is not a session boundary.
        nxt[t] = nxt_t if (nxt_t - t).days <= MAX_SESSION_GAP_DAYS else pd.NaT
    df["available_from"] = df["trade_date"].map(nxt)

    if derived:
        df = add_derived(df)
    return df


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """
    Positioning features. Contract counts, NOT delta- or notional-weighted --
    an option contract and a futures contract are not comparable exposure, so
    the option column is a crude directional proxy and is named accordingly.
    """
    d = df.copy()
    d["fut_idx_net"] = d["fut_idx_long"] - d["fut_idx_short"]
    d["fut_stk_net"] = d["fut_stk_long"] - d["fut_stk_short"]
    d["fut_net"] = d["fut_idx_net"] + d["fut_stk_net"]

    # long call + short put = bullish side; long put + short call = bearish side
    d["opt_idx_dir_proxy"] = (
        (d["opt_idx_ce_long"] + d["opt_idx_pe_short"])
        - (d["opt_idx_pe_long"] + d["opt_idx_ce_short"])
    )
    d["opt_stk_dir_proxy"] = (
        (d["opt_stk_ce_long"] + d["opt_stk_pe_short"])
        - (d["opt_stk_pe_long"] + d["opt_stk_ce_short"])
    )

    d["net_contracts"] = d["total_long"] - d["total_short"]
    denom = (d["total_long"] + d["total_short"]).replace(0, pd.NA)
    d["long_share"] = d["total_long"] / denom          # 0..1, scale-free

    # Day-on-day CHANGE is the tradeable quantity; level is a slow-moving stock.
    # A diff taken across an archive hole is not a day-on-day change -- it is the
    # drift of however many years are missing -- so those are nulled, not kept.
    d = d.sort_values(["participant", "trade_date"])
    prior_gap = d.groupby("participant")["trade_date"].diff().dt.days
    bridged = prior_gap > MAX_SESSION_GAP_DAYS
    for c in ("fut_idx_net", "fut_stk_net", "fut_net", "long_share"):
        chg = d.groupby("participant")[c].diff()
        d[f"{c}_chg"] = chg.mask(bridged)
    return d.sort_values(["trade_date", "participant"]).reset_index(drop=True)


def as_signal(participant: str = "FII", kind: str = "oi") -> pd.DataFrame:
    """
    Point-in-time-safe view for one participant: indexed by `available_from`,
    the first date the information could have been acted on. Rows whose
    successor session is unknown are dropped rather than guessed.
    """
    df = load_panel(kind=kind)
    if df.empty:
        return df
    out = df[df["participant"] == participant].dropna(subset=["available_from"])
    return out.set_index("available_from").sort_index()


def load_cash() -> pd.DataFrame:
    if not os.path.exists(CASH_PATH):
        return pd.DataFrame()
    rows = []
    for ln in open(CASH_PATH, encoding="utf-8"):
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.sort_values(["trade_date", "participant"]).reset_index(drop=True)


def status() -> Dict:
    oi = _read_dir(OI_DIR)
    vol = _read_dir(VOL_DIR)
    cash = load_cash()
    out = {
        "oi_days": 0 if oi.empty else oi["trade_date"].nunique(),
        "vol_days": 0 if vol.empty else vol["trade_date"].nunique(),
        "cash_days": 0 if cash.empty else cash["trade_date"].nunique(),
        "misses": len(_load_misses()),
    }
    if not oi.empty:
        out["oi_from"] = str(oi["trade_date"].min().date())
        out["oi_to"] = str(oi["trade_date"].max().date())
    return out


# ----------------------------------------------------------------- selftest --

_SAMPLE = (
    '""Participant wise Open Interest as on Aug 17, 2026"",,,,,,,,,,,,,,\n'
    "Client Type,Future Index Long,Future Index Short,Future Stock Long,"
    "Future Stock Short       ,Option Index Call Long,Option Index Put Long,"
    "Option Index Call Short,Option Index Put Short,Option Stock Call Long,"
    "Option Stock Put Long,Option Stock Call Short,Option Stock Put Short,"
    "Total Long Contracts      ,Total Short Contracts\n"
    "Client,215653,55062,3294002,262390,3502967,2930320,3407366,3591086,"
    "2867757,972942,1556553,1310311,13783641,10182767\n"
    "DII,50815,19776,334344,4429649,7390,46308,80,0,5895,41025,355265,20351,"
    "485777,4825121\n"
    "FII,25299,206886,3547694,2919718,606002,1019884,834527,517429,221371,"
    "389353,429052,221921,5809602,5129533\n"
    "Pro,34053,44096,935593,499876,1135458,1142752,1009844,1030750,1031141,"
    "1098289,1785294,949026,5377287,5318886\n"
    "TOTAL,325820,325820,8111633,8111633,5251817,5139264,5251817,5139264,"
    "4126164,2501609,4126164,2501609,25456307,25456307\n"
)


def _selftest() -> int:
    fails = []

    df = parse_participant_csv(_SAMPLE, date(2026, 8, 17))
    if df is None or len(df) != 5:
        fails.append(f"parse: expected 5 rows, got {None if df is None else len(df)}")
        print("FAIL", fails[-1])
        return 1

    fii = df[df.participant == "FII"].iloc[0]
    if fii.fut_idx_long != 25299 or fii.total_short != 5129533:
        fails.append("parse: FII values mis-mapped (trailing-space header?)")

    # TOTAL long must equal TOTAL short -- every contract has both sides.
    tot = df[df.participant == "TOTAL"].iloc[0]
    if tot.total_long != tot.total_short:
        fails.append("parse: TOTAL long != short, column alignment is wrong")

    # Participant columns must sum to TOTAL.
    parts = df[df.participant != "TOTAL"]
    if int(parts.fut_idx_long.sum()) != int(tot.fut_idx_long):
        fails.append("parse: participants do not sum to TOTAL")

    d = add_derived(df)
    fii_d = d[d.participant == "FII"].iloc[0]
    if fii_d.fut_idx_net != 25299 - 206886:
        fails.append("derived: fut_idx_net wrong")
    if not (0 < fii_d.long_share < 1):
        fails.append("derived: long_share out of range")

    # Malformed input must return None, never a half-built frame.
    if parse_participant_csv("garbage\nnot,a,csv\n", date(2026, 8, 17)) is not None:
        fails.append("parse: garbage input did not return None")

    for f in fails:
        print("FAIL", f)
    if not fails:
        print("selftest OK — parse, TOTAL identity, sum check, derived, "
              "garbage rejection")
    return 1 if fails else 0


# ---------------------------------------------------------------------- CLI --

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=0, help="calendar days back from today")
    p.add_argument("--from", dest="frm", default="")
    p.add_argument("--to", dest="to", default="")
    p.add_argument("--sleep", type=float, default=0.4)
    p.add_argument("--cash", action="store_true", help="append today's FII/DII cash")
    p.add_argument("--status", action="store_true")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()

    if a.selftest:
        return _selftest()

    if a.status:
        for k, v in status().items():
            print(f"  {k:12s} {v}")
        return 0

    if a.cash:
        rows = fetch_cash_flow(_get_session())
        if not rows:
            print("cash: fetch failed — nothing written")
            return 1
        print(f"cash: +{append_cash(rows)} new rows")
        for r in rows:
            print(f"  {r['trade_date']}  {r['participant']:8s} net {r['net_cr']:>12,.2f} Cr")
        return 0

    if a.frm and a.to:
        start = date(*(int(x) for x in a.frm.split("-")))
        end = date(*(int(x) for x in a.to.split("-")))
    elif a.days:
        end = date.today()
        start = end - timedelta(days=a.days)
    else:
        p.print_help()
        return 0

    print(f"participant flow archive: {start} .. {end}")
    build(start, end, a.sleep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
