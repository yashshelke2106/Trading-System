"""
Bulk corporate-events archive for the whole universe, via indianapi.in.

Pulls STRUCTURED events per symbol from two endpoints:
    corporate_actions   -> board_meetings, dividends, splits, bonus, rights
    recent_announcements -> {title, link, date}

WHY this beats the news feed:
    These carry EXPLICIT event dates. corporate_actions in particular usually
    includes past-dated actions, so combined with the 10yr OHLC archive you can
    run a RETROSPECTIVE event study (did bonus/split/dividend actually move the
    stock, historically) - not only forward-collection.

OUTPUTS:
    data/events/<SYMBOL>.json          raw payloads (both endpoints), for re-parsing
    logs/corporate_events.jsonl        normalized, dated, one line per event
    data/events/_manifest.json         per-symbol status

DEFENSIVE:
    The exact nested JSON of corporate_actions is not verified here. The
    normalizer keeps the RAW item as `detail` and best-effort extracts a date +
    category, so nothing is lost even if the shape differs from expectation.
    Re-run the normalizer over the saved raw JSON once the shape is confirmed
    (python -m scripts.build_events_archive --renormalize).

RUN (real machine, key installed):
    python -m scripts.build_events_archive --probe            # 1 symbol, show what came back
    python -m scripts.build_events_archive                    # full FO_UNIVERSE
    python -m scripts.build_events_archive --top100 --sleep 2
    python -m scripts.build_events_archive --renormalize      # rebuild jsonl from saved raw
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.universe import FO_UNIVERSE, TOP100_LIQUID  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(_ROOT, "data", "events")
EVENTS_JSONL = os.path.join(_ROOT, "logs", "corporate_events.jsonl")
MANIFEST = os.path.join(RAW_DIR, "_manifest.json")

_DATE_RX = re.compile(r"\b(\d{2})[-/](\d{2})[-/](\d{4})\b")  # DD-MM-YYYY (exchange format)
_ISO_RX = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# category (plural, as the API sends) -> clean singular event_type
_CATEGORY_MAP = {
    "dividends": "dividend", "splits": "split", "bonus": "bonus",
    "rights": "rights", "board_meetings": "board_meeting",
}


def _to_iso(s: Any) -> Optional[str]:
    """Parse the exchange's DD-MM-YYYY (or an ISO date) -> 'YYYY-MM-DD'. None if
    the string isn't a real date (announcements put junk in the date field)."""
    if not isinstance(s, str):
        return None
    m = _ISO_RX.search(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _DATE_RX.search(s)
    if m:
        dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
        if 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31:
            return f"{yyyy}-{mm}-{dd}"
    return None


def _date_col(header: List[str]) -> int:
    """Pick the event-date column: Ex-Date (price adjusts here) > Record Date >
    Date > first column."""
    low = [str(h).lower() for h in (header or [])]
    for want in ("ex-date", "ex date", "exdate"):
        for i, h in enumerate(low):
            if want in h:
                return i
    for i, h in enumerate(low):
        if "record date" in h:
            return i
    for i, h in enumerate(low):
        if "date" in h:
            return i
    return 0


def _expand_table(symbol: str, category: str, tbl: Dict) -> List[Dict]:
    """A corporate_actions category is {header, data:[[...],...]}. Emit one dated
    event per data row. board_meeting agendas are sub-classified (Quarterly
    Results / dividend / buyback ...) from their text."""
    from core.event_classifier import classify_primary
    header = tbl.get("header") or []
    data = tbl.get("data") or []
    if not isinstance(data, list) or not data:
        return []
    dcol = _date_col(header)
    base_type = _CATEGORY_MAP.get(category, category)
    out = []
    for row in data:
        if not isinstance(row, (list, tuple)) or dcol >= len(row):
            continue
        iso = _to_iso(row[dcol])
        if not iso:
            continue
        # sub-classify board meetings by their agenda text
        etype = base_type
        agenda = " ".join(str(c) for c in row)
        if category == "board_meetings":
            sub, _, _ = classify_primary(agenda)
            if sub != "none":
                etype = sub
            elif "quarterly result" in agenda.lower() or "financial result" in agenda.lower():
                etype = "results"
        out.append({
            "symbol": symbol,
            "source": "corporate_action",
            "category": base_type,
            "event_type": etype,
            "date": iso,
            "detail": dict(zip([str(h) for h in header], row)) if header else list(row),
        })
    return out


def normalize(symbol: str, corp: Any, ann: Any) -> List[Dict]:
    """Flatten both payloads into dated event rows. Keeps parsed row as `detail`."""
    rows: List[Dict] = []

    # corporate_actions: dict{category -> {header, data}}
    if isinstance(corp, dict):
        for category, tbl in corp.items():
            if isinstance(tbl, dict):
                rows.extend(_expand_table(symbol, category, tbl))

    # recent_announcements: list[{title, link, date}] -> classify title.
    # NOTE: the API's `date` field is unreliable (often holds description text);
    # kept for context but usually dateless, so excluded from the timed study.
    from core.event_classifier import classify_primary
    ann_seq = ann if isinstance(ann, list) else (
        ann.get("data") or ann.get("announcements") or [] if isinstance(ann, dict) else [])
    for it in ann_seq:
        if not isinstance(it, dict):
            continue
        title = it.get("title") or it.get("headline") or ""
        etype, edir, _ = classify_primary(title)
        rows.append({
            "symbol": symbol,
            "source": "announcement",
            "category": etype,
            "event_type": etype,
            "expected_dir": edir,
            "date": _to_iso(it.get("date", "")) or _to_iso(title),
            "title": title,
            "link": it.get("link") or it.get("url", ""),
        })
    return rows


def _event_key(r: Dict) -> str:
    return f"{r.get('symbol')}|{r.get('source')}|{r.get('category')}|{r.get('date')}|{r.get('title','')}"


def _load_keys() -> set:
    keys = set()
    if os.path.exists(EVENTS_JSONL):
        with open(EVENTS_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    keys.add(_event_key(json.loads(line)))
                except Exception:
                    continue
    return keys


def _load_manifest() -> Dict:
    if os.path.exists(MANIFEST):
        try:
            return json.load(open(MANIFEST, encoding="utf-8"))
        except Exception:
            pass
    return {"built_at": None, "symbols": {}}


def _save_manifest(m: Dict) -> None:
    m["built_at"] = datetime.now().isoformat(timespec="seconds")
    json.dump(m, open(MANIFEST, "w", encoding="utf-8"), indent=2, default=str)


def renormalize() -> int:
    """Rebuild the jsonl from saved raw JSON (after confirming the shape)."""
    if not os.path.isdir(RAW_DIR):
        print("no raw events yet.")
        return 1
    rows: List[Dict] = []
    for fn in os.listdir(RAW_DIR):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        sym = fn[:-5]
        try:
            payload = json.load(open(os.path.join(RAW_DIR, fn), encoding="utf-8"))
        except Exception:
            continue
        rows.extend(normalize(sym, payload.get("corporate_actions"),
                              payload.get("recent_announcements")))
    with open(EVENTS_JSONL, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    print(f"renormalized {len(rows)} events into {EVENTS_JSONL}")
    return 0


def build(symbols: List[str], sleep: float, refresh: bool, probe: bool) -> int:
    from core.api_indianstock import IndianStockAPI
    api = IndianStockAPI()
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(EVENTS_JSONL), exist_ok=True)
    manifest = _load_manifest()
    seen = _load_keys()

    ok = failed = skipped = total_events = 0
    for i, sym in enumerate(symbols, 1):
        safe = sym.replace("/", "_")
        raw_path = os.path.join(RAW_DIR, f"{safe}.json")
        if not refresh and os.path.exists(raw_path):
            skipped += 1
            continue

        try:
            corp = api.raw_corporate_actions(sym)
        except Exception as e:
            corp = {"_error": str(e)}
        try:
            ann = api.raw_announcements(sym)
        except Exception as e:
            ann = {"_error": str(e)}

        if (isinstance(corp, dict) and "_error" in corp) and \
           (isinstance(ann, dict) and "_error" in ann):
            failed += 1
            manifest["symbols"][sym] = {"status": "failed",
                                        "error": corp.get("_error")}
            print(f"[{i}/{len(symbols)}] {sym:14s} FAILED  {corp.get('_error')}")
            time.sleep(sleep)
            continue

        json.dump({"symbol": sym, "corporate_actions": corp,
                   "recent_announcements": ann},
                  open(raw_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2, default=str)

        rows = normalize(sym, corp, ann)
        new = 0
        with open(EVENTS_JSONL, "a", encoding="utf-8") as f:
            for r in rows:
                k = _event_key(r)
                if k in seen:
                    continue
                seen.add(k)
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
                new += 1
        ok += 1
        total_events += new
        manifest["symbols"][sym] = {"status": "ok", "events": len(rows), "new": new}

        if probe:
            print(f"\nPROBE {sym}: {len(rows)} events parsed. Sample:")
            for r in rows[:8]:
                print(f"  {str(r.get('date')):12s} {r.get('source'):16s} "
                      f"{str(r.get('event_type')):16s} {str(r.get('title',''))[:40]}")
            _save_manifest(manifest)
            print(f"\nraw saved: {raw_path}")
            return 0

        print(f"[{i}/{len(symbols)}] {sym:14s} ok  {len(rows):3d} events (+{new} new)")
        _save_manifest(manifest)
        time.sleep(sleep)

    _save_manifest(manifest)
    print("\n" + "=" * 60)
    print(f"DONE  ok={ok}  failed={failed}  skipped={skipped}  "
          f"new_events={total_events}")
    print(f"raw:    {RAW_DIR}")
    print(f"events: {EVENTS_JSONL}")
    print("=" * 60)
    return 0


def main(argv: List[str]) -> int:
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Bulk corporate-events archive via indianapi.in")
    p.add_argument("--top100", action="store_true")
    p.add_argument("--symbols", type=str, default="")
    p.add_argument("--sleep", type=float, default=1.5)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--probe", action="store_true", help="1 symbol, show parsed events, stop")
    p.add_argument("--renormalize", action="store_true", help="rebuild jsonl from saved raw")
    args = p.parse_args(argv)

    if args.renormalize:
        return renormalize()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.top100:
        symbols = list(TOP100_LIQUID)
    else:
        symbols = list(FO_UNIVERSE)

    print(f"universe: {len(symbols)} symbols   out: {RAW_DIR}")
    return build(symbols, args.sleep, args.refresh, args.probe)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
