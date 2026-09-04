"""core/symbol_groups.py — correlation groups for portfolio caps.

Why this exists: on 2026-08-31 the swing paper book closed 23 positions in one
session for -60.7 points, and they were not 23 bets. Seven were Adani-group
names (ADANIENT x3, ADANIGREEN x2, ADANIPORTS, ADANIPOWER) and six were the
same symbol entered twice from two different signals. Nothing in the path
counted them as related, because nothing in the path counted positions at all.

A group here means "these move together far more than the universe average" --
either a shared promoter (one governance/leverage story) or a tight sector
cluster that reprices as a block (PSU banks on a rate print, IT on a dollar
move). It is deliberately NOT a full sector map: an over-broad group would
block ordinary diversification, and a wrong one is worse than none.

Symbols with no entry are their own singleton group, so an unmapped name is
never blocked by this cap. The map is therefore a floor on correlation
awareness, not a ceiling -- add clusters as they prove themselves, and prefer
leaving a name unmapped to guessing.
"""

from __future__ import annotations

# Shared promoter / holding structure -- one balance-sheet story.
_PROMOTER = {
    "ADANI": ["ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER"],
    "TATA": ["TCS", "TATACONSUM", "TATAPOWER", "TATASTEEL", "TATAMOTORS",
             "TRENT", "INDHOTEL", "TITAN", "VOLTAS"],
    "BAJAJ": ["BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE"],
    "BIRLA": ["GRASIM", "ULTRACEMCO", "HINDALCO"],
    "GODREJ": ["GODREJCP", "GODREJPROP"],
    "RELIANCE": ["RELIANCE", "JIOFIN"],
}

# Tight sector blocks that reprice together on a single macro print.
_SECTOR = {
    "PSU_BANK": ["SBIN", "BANKBARODA", "CANBK", "PNB"],
    "PVT_BANK": ["HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK",
                 "INDUSINDBK", "FEDERALBNK", "IDFCFIRSTB", "BANDHANBNK"],
    "IT": ["INFY", "WIPRO", "HCLTECH", "TECHM", "PERSISTENT", "OFSS",
           "MPHASIS"],
    "PSU_OILGAS": ["ONGC", "BPCL", "HINDPETRO", "GAIL", "IOC"],
    "PSU_LENDER": ["PFC", "RECLTD", "IRFC", "LICHSGFIN"],
    "METAL": ["JSWSTEEL", "SAIL", "NMDC", "NATIONALUM", "COALINDIA",
              "JINDALSTEL", "VEDL"],
    "NBFC_GOLD": ["MUTHOOTFIN", "MANAPPURAM"],
}

# TATA holds TCS, so TCS is a promoter member AND an IT name. Promoter wins:
# a governance event hits every Tata name at once, which is the tail the cap
# exists to stop. Sector membership therefore loads first and promoter
# membership overwrites it.
_GROUP: dict = {}
for _g, _syms in _SECTOR.items():
    for _s in _syms:
        _GROUP[_s] = _g
for _g, _syms in _PROMOTER.items():
    for _s in _syms:
        _GROUP[_s] = _g


def group_of(symbol: str) -> str:
    """Correlation group for `symbol`. Unmapped names get a singleton group
    keyed on themselves, so they are never capped against anything else."""
    return _GROUP.get(symbol.upper(), "_SOLO:" + symbol.upper())


def groups_present(symbols) -> dict:
    """{group: [symbols]} for a collection -- used by the cap and by tests."""
    out: dict = {}
    for s in symbols:
        out.setdefault(group_of(s), []).append(s)
    return out
