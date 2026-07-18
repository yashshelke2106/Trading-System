"""
Streamlit event/news dashboard - visual twin of scripts/events_dashboard.py.

Read-only: no API calls, no file writes. Reads the archives that
system_update.bat maintains.

RUN:  streamlit run events_app.py            (or events_app.bat)
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import date

import pandas as pd
import streamlit as st

_ROOT = os.path.dirname(os.path.abspath(__file__))
NEWS_ARCHIVE = os.path.join(_ROOT, "logs", "news_archive.jsonl")
REACTION_LOG = os.path.join(_ROOT, "logs", "news_reaction.jsonl")
STUDY_RESULTS = os.path.join(_ROOT, "logs", "event_study_results.json")
SIGNALS = os.path.join(_ROOT, "logs", "signals.json")

st.set_page_config(page_title="Event Dashboard", page_icon="📅", layout="wide")


@st.cache_data(ttl=300)
def _jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


@st.cache_data(ttl=300)
def _exact_events() -> dict:
    from core.news_filter import EventCalendar
    ec = EventCalendar()
    # plain dict of plain types so st.cache can hash/pickle it
    return {
        "events": {s: [(d.isoformat(), t) for d, t in evs]
                   for s, evs in ec.exact_events.items()},
        "gap_types": sorted(ec.GAP_RISK_EVENTS),
    }


data = _exact_events()
gap_types = set(data["gap_types"])
today = date.today()

flagged, upcoming = set(), set()
for sym, evs in data["events"].items():
    for d_iso, ev_type in evs:
        d = (date.fromisoformat(d_iso) - today).days
        if d > 30:
            continue
        upcoming.add((d, d_iso, sym, ev_type))
        if d <= 2 and ev_type in gap_types:
            flagged.add((d, sym, ev_type))

news = _jsonl(NEWS_ARCHIVE)
rx = _jsonl(REACTION_LOG)
ripe = sum(1 for r in rx if r.get("ripe"))

# ── header ────────────────────────────────────────────────────────────────
st.title("📅 Event & News Dashboard")
st.error(
    "**Stat-gate verdict (2026-07-18): REJECTED.** No corporate-event type shows "
    "tradeable abnormal return (clustered bootstrap + Bonferroni + cost). "
    "Events here are **risk context** — never entry signals."
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Risk flags (skip_entry)", len(flagged))
c2.metric("Events next 30d", len(upcoming))
c3.metric("News archived", len(news))
c4.metric("Reaction events (ripe)", f"{len(rx)} ({ripe})")

tab_trades, tab_flags, tab_cal, tab_study, tab_news, tab_rx = st.tabs(
    ["💼 Trades", "🚫 Risk Flags", "🗓 Calendar", "📊 Study", "📰 News",
     "🎯 Reactions"])

# ── trades (scanner signals x event risk flags) ───────────────────────────
with tab_trades:
    st.subheader("Scanner signals + event risk overlay")
    st.warning(
        "**PAPER ONLY.** Audit 2026-06-08: this strategy's backtest is PF 0.72 "
        "(net-negative) — no validated edge behind these picks. Shorts are "
        "paper-bench only, never funded. Selection quality is what the forward "
        "paper journal is measuring."
    )
    # sentinel banner - agentic halt flag (halt-only power, fail-safe reader)
    try:
        from core.sentinel import entries_halted
        _h, _why = entries_halted()
        if _h:
            st.error(f"🛑 RISK SENTINEL: new entries HALTED - {_why}  "
                     f"(clear: `python -m scripts.agent_risk_sentinel --clear`)")
    except Exception:
        pass

    flagged_syms = {sym for _, sym, _ in flagged}
    if os.path.exists(SIGNALS):
        sig_data = json.load(open(SIGNALS, encoding="utf-8"))
        sigs = sig_data.get("signals", [])
        scan_ts = sig_data.get("ts", "?")[:16]
        st.caption(f"last scan: {scan_ts} · grades: {sig_data.get('by_grade')}")
        if sigs:
            df = pd.DataFrame(sigs)
            cols = [c for c in ("symbol", "direction", "confluence_grade",
                                "entry_price", "sl_price", "target_price",
                                "rr_ratio", "confluence_score",
                                "agent_verdict", "agent_reason",
                                "sentinel_halt", "reason")
                    if c in df.columns]
            df = df[cols]
            df["event_risk"] = df["symbol"].map(
                lambda s: "SKIP - results <=2d" if s in flagged_syms else "clear")
            # event-blocked rows sort first so the danger is seen before the trades
            df = df.sort_values(["event_risk", "confluence_grade"])
            st.dataframe(df, use_container_width=True, hide_index=True)
            blocked = int((df["event_risk"] != "clear").sum())
            if blocked:
                st.error(f"{blocked} signal(s) blocked by event risk - known "
                         "results gap within 2 days. Do not take these.")
        else:
            st.info(
                "Scanner found **0 qualifying setups** in the last run - that is "
                "normal, it needs 3+ pattern votes with a 2-vote lead. "
                "An empty day is the filter working, not a bug.\n\n"
                "Refresh signals: run the scanner "
                "(`start_trading.bat`, or `python scan_only_v2.py`), then reload "
                "this page. Full terminal with option legs: http://localhost:3000"
            )
    else:
        st.info("No logs/signals.json yet - run the scanner first "
                "(`start_trading.bat` or `python scan_only_v2.py`).")

# ── risk flags ────────────────────────────────────────────────────────────
with tab_flags:
    st.subheader("Skip-entry flags — results / board meeting within 2 days")
    if flagged:
        df = pd.DataFrame(sorted(flagged), columns=["days", "symbol", "event"])
        df["when"] = df["days"].map(lambda d: "TODAY" if d == 0 else f"+{d}d")
        st.dataframe(df[["symbol", "event", "when"]],
                     use_container_width=True, hide_index=True)
        st.caption("These symbols get skip_entry=True in the scan pipeline "
                   "(EventCalendar.get_event_context).")
    else:
        st.success("No gap-risk events in the next 2 days.")

# ── calendar ──────────────────────────────────────────────────────────────
with tab_cal:
    horizon = st.slider("Horizon (days)", 3, 30, 7)
    rows = sorted(u for u in upcoming if u[0] <= horizon)
    if rows:
        df = pd.DataFrame(rows, columns=["days", "date", "symbol", "event"])
        df["gap risk"] = df["event"].map(lambda e: "⚠️" if e in gap_types else "")
        types = st.multiselect("Filter event types",
                               sorted(df["event"].unique().tolist()))
        if types:
            df = df[df["event"].isin(types)]
        st.dataframe(df[["date", "days", "symbol", "event", "gap risk"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("No dated events in this window. Run system_update.bat to refresh.")

# ── study ─────────────────────────────────────────────────────────────────
with tab_study:
    st.subheader("Retrospective study — abnormal return per event type")
    st.caption("Abnormal = excess vs NIFTY minus the stock's own baseline drift "
               "(strips survivorship). ~0 everywhere = no edge, as gated.")
    if os.path.exists(STUDY_RESULTS):
        res = json.load(open(STUDY_RESULTS, encoding="utf-8"))
        by_type = res.get("by_event_type", {})
        if by_type:
            rows = []
            for et, b in by_type.items():
                row = {"event_type": et, "n": b.get("n"),
                       "pre5%": b.get("pre5_avg")}
                for h in (1, 3, 5, 10, 20):
                    hb = b.get(f"h{h}")
                    row[f"+{h}d%"] = hb.get("avg") if hb else None
                rows.append(row)
            st.dataframe(pd.DataFrame(rows).sort_values("n", ascending=False),
                         use_container_width=True, hide_index=True)
            st.caption(f"metric: {res.get('metric')} | events used: "
                       f"{res.get('events_used')}")
        else:
            st.info("Results file has no per-type rows yet — run system_update.bat.")
    else:
        st.info("No study results yet — run system_update.bat.")

# ── news ──────────────────────────────────────────────────────────────────
with tab_news:
    st.subheader(f"News archive — {len(news)} articles (forward-built)")
    for r in sorted(news, key=lambda r: r.get("pub_date", ""), reverse=True)[:40]:
        st.markdown(
            f"**{r.get('pub_date', '')[:16]}** · {r.get('source', '')} — "
            f"[{r.get('title', '(no title)')}]({r.get('url', '')})")

# ── reactions ─────────────────────────────────────────────────────────────
with tab_rx:
    st.subheader("Typed news-reaction log (forward collector)")
    if rx:
        df = pd.DataFrame(rx)
        cols = [c for c in ("date", "symbol", "signal", "event_type", "score",
                            "news_candle", "ret_1d", "ret_3d", "ret_5d", "ripe")
                if c in df.columns]
        st.dataframe(df[cols].sort_values("date", ascending=False),
                     use_container_width=True, hide_index=True)
        sig = Counter(r.get("signal") for r in rx)
        st.caption(f"bullish {sig.get('bullish', 0)} · bearish "
                   f"{sig.get('bearish', 0)} · ripe {ripe}/{len(rx)}")
    else:
        st.info("No reaction events yet — run system_update.bat.")
