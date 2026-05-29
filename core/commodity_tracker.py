import time
from typing import Dict, Optional

# ticker → (yfinance_symbol, direction_multiplier)
# direction: +1 means stock benefits when commodity rises, -1 means it hurts
COMMODITY_MAP: Dict[str, tuple] = {
    # Aluminum-linked (LME aluminum futures proxy)
    'NALCO':      ('ALI=F', +1),
    'VEDANTA':    ('ALI=F', +1),
    'HINDALCO':   ('ALI=F', +1),
    # Steel
    'JSWSTEEL':   ('HRC=F', +1),   # hot-rolled coil futures
    'TATASTEEL':  ('HRC=F', +1),
    # Crude oil — upstream benefits, refiner (BPCL) benefits from lower crude margins inv.
    'ONGC':       ('CL=F',  +1),
    'BPCL':       ('CL=F',  -1),   # refiner: lower crude → better margins
    # Coal
    'COALINDIA':  ('MTF=F', +1),   # thermal coal futures proxy
    # Power / renewables — crude has indirect effect
    'NTPC':       ('CL=F',  -1),   # lower fuel cost = better for thermal power
    'TATAPOWER':  ('CL=F',  -1),
}

_CACHE: Dict[str, tuple] = {}   # ticker → (timestamp, pct_change)
_CACHE_TTL = 3600               # 1-hour cache; commodity prices don't move intraday often


def get_commodity_signal(symbol: str) -> Dict:
    """Return commodity tailwind/headwind signal for given stock symbol."""
    if symbol not in COMMODITY_MAP:
        return {'applicable': False}

    commodity_ticker, direction = COMMODITY_MAP[symbol]

    pct = _fetch_pct_change(commodity_ticker)
    if pct is None:
        return {'applicable': True, 'commodity': commodity_ticker, 'error': 'fetch failed'}

    tailwind = (pct > 0) == (direction > 0)

    if abs(pct) < 0.3:
        confidence_modifier = 1.0   # negligible move
    elif tailwind:
        confidence_modifier = 1.15 if abs(pct) > 1.0 else 1.08
    else:
        confidence_modifier = 0.75 if abs(pct) > 1.0 else 0.88

    return {
        'applicable':           True,
        'commodity':            commodity_ticker,
        'commodity_pct':        round(pct, 3),
        'tailwind':             tailwind,
        'confidence_modifier':  confidence_modifier,
        'reason': (
            f"{commodity_ticker} {'+' if pct >= 0 else ''}{pct:.2f}% → "
            f"{'tailwind' if tailwind else 'headwind'} for {symbol}"
        ),
    }


def _fetch_pct_change(ticker: str) -> Optional[float]:
    now = time.time()
    cached = _CACHE.get(ticker)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    # Global commodity prices (crude, metals on COMEX/NYMEX) are NOT on Dhan.
    # yfinance permanently removed -> commodity cue disabled, returns None
    # (callers treat None as 'not applicable'). Re-enable only with a real
    # commodity-data feed, never yfinance.
    return None


if __name__ == '__main__':
    test_symbols = ['NALCO', 'VEDANTA', 'ONGC', 'BPCL', 'JSWSTEEL', 'RELIANCE']
    for sym in test_symbols:
        result = get_commodity_signal(sym)
        if result.get('applicable'):
            print(f"{sym}: {result.get('reason', 'N/A')}  modifier={result.get('confidence_modifier')}")
        else:
            print(f"{sym}: not commodity-linked")
