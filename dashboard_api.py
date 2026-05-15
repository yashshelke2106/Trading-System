from __future__ import annotations

from typing import List, Optional

import config
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from core.dashboard_data import (
    get_dashboard_snapshot,
    get_live_positions,
    get_option_chain_view,
    get_signal_payload,
    get_trade_summary,
    get_volume_analytics,
    load_signal_journal_frame,
    universe_subset,
)
from core.rag_engine import build_default_rag_engine
from core.universe import FO_UNIVERSE

app = FastAPI(
    title="Trading System Dashboard API",
    version="1.0.0",
    description="Shared data and intelligence API for the Streamlit dashboard and external tools.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

rag = build_default_rag_engine()


def _parse_symbols(symbols: Optional[str], limit: int = 0) -> List[str]:
    if symbols:
        return [sym.strip().upper() for sym in symbols.split(",") if sym.strip()]
    if limit > 0:
        return universe_subset(limit)
    return list(FO_UNIVERSE)


@app.get("/health")
def health() -> dict:
    snapshot = get_dashboard_snapshot()
    return {
        "status": "ok",
        "generated_at": snapshot["generated_at"],
        "signals_present": bool(snapshot["signals"]),
        "positions_count": len(snapshot["positions"]),
    }


@app.get("/snapshot")
def snapshot() -> dict:
    return get_dashboard_snapshot()


@app.get("/signals")
def signals() -> dict:
    return get_signal_payload()


@app.get("/positions")
def positions() -> dict:
    return {"positions": get_live_positions()}


@app.get("/volume")
def volume(
    symbols: Optional[str] = Query(default=None, description="Comma-separated symbols"),
    limit: int = Query(default=25, ge=1, le=153),
) -> dict:
    return get_volume_analytics(_parse_symbols(symbols, limit=limit))


@app.get("/option-chain/{symbol}")
def option_chain(symbol: str) -> dict:
    return get_option_chain_view(symbol.upper())


@app.get("/trades/summary")
def trades_summary() -> dict:
    return get_trade_summary()


@app.get("/analytics/journal")
def analytics_journal(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    df = load_signal_journal_frame()
    if df.empty:
        return {"rows": []}
    recent = df.sort_values("ts_outcome", ascending=False).head(limit).copy()
    if "ts_outcome" in recent.columns:
        recent["ts_outcome"] = recent["ts_outcome"].astype(str)
    return {"rows": recent.to_dict("records")}


@app.get("/intelligence/brief")
def intelligence_brief() -> dict:
    return rag.build_market_brief()


@app.get("/intelligence/query")
def intelligence_query(
    q: str = Query(..., min_length=3, description="Natural-language question"),
    top_k: int = Query(default=5, ge=1, le=10),
) -> dict:
    return rag.query(q, top_k=top_k)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "dashboard_api:app",
        host=config.DASHBOARD_API_HOST,
        port=config.DASHBOARD_API_PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
