"""Shared F&O universe used by runner, scanner, and dashboards.

LTIM and GUJGASLTD were REMOVED 2026-08-06: absent from Dhan's scrip master
AND from yfinance, with no successor row to alias to, so every fetch for them
failed silently. Do not re-add without checking scrip_master.lookup() first.

Prefer `core.selection.universe(tier)` over these constants for anything that
places or sizes a trade. This list says nothing about HOW liquid a name is --
it ranked a Rs 261 Cr/day name alongside a Rs 2,322 Cr/day one -- while
selection reads measured turnover and returns the tier appropriate to the
instrument. These constants remain for callers that genuinely want the whole
F&O set (archive builders, research sweeps).
"""

FO_UNIVERSE = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "SBIN", "BHARTIARTL", "KOTAKBANK", "BAJFINANCE", "HINDUNILVR",
    "ITC", "LT", "AXISBANK", "MARUTI", "ASIANPAINT",
    "WIPRO", "HCLTECH", "TITAN", "SUNPHARMA", "TATAMOTORS",
    "ADANIENT", "NTPC", "POWERGRID", "ULTRACEMCO", "JSWSTEEL",
    "ONGC", "COALINDIA", "BPCL", "HEROMOTOCO", "EICHERMOT",
    "TATASTEEL", "HINDALCO", "GRASIM", "CIPLA", "DIVISLAB",
    "DRREDDY", "BRITANNIA", "NESTLEIND", "TATACONSUM", "M&M",
    "BAJAJ-AUTO", "INDUSINDBK", "TECHM", "ADANIPORTS", "BAJAJFINSV",
    "HDFCLIFE", "SHRIRAMFIN", "TRENT", "APOLLOHOSP",
    "ABB", "ALKEM", "AMBUJACEM", "APOLLOTYRE", "AUROPHARMA",
    "BANDHANBNK", "BANKBARODA", "BEL", "BERGEPAINT", "BHARATFORG",
    "BHEL", "BIOCON", "BOSCHLTD", "CANBK", "CEATLTD",
    "CHOLAFIN", "COLPAL", "CONCOR", "COROMANDEL", "DABUR",
    "DLF", "DMART", "ESCORTS", "EXIDEIND", "FEDERALBNK",
    "GAIL", "GLENMARK", "GODREJCP", "GODREJPROP", "GRANULES",
    "HAVELLS", "HINDPETRO", "ICICIPRULI", "ICICIGI",
    "IDFCFIRSTB", "IGL", "INDHOTEL", "INDIGO", "INDUSTOWER",
    "IPCALAB", "IRCTC", "IRFC", "JINDALSTEL", "JIOFIN",
    "LALPATHLAB", "LAURUSLABS", "LICHSGFIN", "LICI",
    "LUPIN", "M&MFIN", "MANAPPURAM", "MARICO", "MAXHEALTH",
    "MCX", "METROPOLIS", "MCDOWELL-N", "MOTHERSON", "MPHASIS",
    "MUTHOOTFIN", "NATIONALUM", "NAUKRI", "NCC", "NHPC",
    "NMDC", "NYKAA", "OBEROIRLTY", "OFSS", "OLECTRA",
    "PAGEIND", "PAYTM", "PFC", "PIIND",
    "PNB", "POLYCAB", "PVRINOX", "RAMCOCEM", "RECLTD",
    "SAIL", "SBICARD", "SBILIFE", "SHREECEM", "SIEMENS",
    "SUNTV", "TATACOMM", "TATACHEM", "TATAELXSI", "TATAPOWER",
    "TVSMOTOR", "UBL", "UPL", "VEDL", "VOLTAS",
    "ETERNAL", "ZYDUSLIFE", "ADANIGREEN", "ADANIPOWER",
    "ATUL", "BALKRISIND", "DEEPAKNTR", "NAVINFLUOR", "PERSISTENT",
    "PIDILITIND",
]

# Most-liquid 100 F&O names, RANK-ORDERED by MEASURED avg daily turnover
# (close*volume, trailing ~252 sessions from logs/bar_cache; rebuilt 2026-07-07).
# This is the swing-trade universe: high volume => tight spreads + reliable
# delivery fills, which is what actually matters holding across days. The
# #100 name still trades ~Rs150 Cr/day. Regenerate with scripts/rebuild_liquid_universe.py
# whenever the cache is refreshed. TATAMOTORS is excluded only by a bar_cache
# gap and can be re-added once cached. LTIM cannot: see the header note.
TOP100_LIQUID = [
    "HDFCBANK", "RELIANCE", "ICICIBANK", "BHARTIARTL", "INFY",
    "SBIN", "ETERNAL", "TCS", "M&M", "LT",
    "AXISBANK", "INDIGO", "BAJFINANCE", "BEL", "ITC",
    "MARUTI", "SHRIRAMFIN", "TATASTEEL", "MCX", "HINDALCO",
    "ADANIPOWER", "HCLTECH", "SUNPHARMA", "PAYTM", "HINDUNILVR",
    "JIOFIN", "TITAN", "NTPC", "COALINDIA", "VEDL",
    "NATIONALUM", "ADANIENT", "ONGC", "POWERGRID", "KOTAKBANK",
    "ADANIGREEN", "HEROMOTOCO", "ADANIPORTS", "EICHERMOT", "CANBK",
    "INDUSINDBK", "BAJAJ-AUTO", "BHEL", "ULTRACEMCO", "TRENT",
    "WIPRO", "TECHM", "ASIANPAINT", "MAXHEALTH", "PERSISTENT",
    "APOLLOHOSP", "SAIL", "TVSMOTOR", "BPCL", "CHOLAFIN",
    "PFC", "INDUSTOWER", "POLYCAB", "BANKBARODA", "RECLTD",
    "FEDERALBNK", "BAJAJFINSV", "MUTHOOTFIN", "HINDPETRO", "DIVISLAB",
    "DRREDDY", "CIPLA", "HDFCLIFE", "DLF", "IDFCFIRSTB",
    "DMART", "PNB", "TATAPOWER", "LUPIN", "MOTHERSON",
    "BRITANNIA", "GRASIM", "INDHOTEL", "SBILIFE", "LAURUSLABS",
    "JSWSTEEL", "ABB", "NAUKRI", "GAIL", "NMDC",
    "BHARATFORG", "UPL", "GODREJPROP", "GLENMARK", "NESTLEIND",
    "GODREJCP", "AUROPHARMA", "IRFC", "TATACONSUM", "OFSS",
    "JINDALSTEL", "BANDHANBNK", "NYKAA", "VOLTAS", "MCDOWELL-N",
]

# Back-compat alias; now points at the data-verified liquid ranking.
TOP100_FO = TOP100_LIQUID
