"""
Corporate-event taxonomy classifier for NSE stock news.

WHY typed events (not just bull/bear sentiment):
    The user's thesis: results, buyback, dividend, bonus, merger, demerger,
    order wins, expansion, licence/approval, stake sales etc. each drive a
    *characteristic* reaction. A binary sentiment score throws that structure
    away. This classifier tags a headline (title + summary) with EVENT TYPES,
    each carrying a prior expected direction. The reaction study then measures
    the ACTUAL forward drift PER EVENT TYPE - i.e. does a buyback really pump,
    does a demerger unlock value, how long does an order-win rally hold.

    The priors below are HYPOTHESES, not truths. The study exists to confirm
    or refute them on this universe.

OUTPUT:
    classify(text) -> list[EventMatch]  (may be empty / multiple)
    classify_primary(text) -> (event_type, expected_dir, confidence)

INTEGRATION:
    news_reaction.collect tags each event row with event_type so analyze()
    can group forward returns by type.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class EventMatch:
    event_type: str
    expected_dir: int      # +1 bullish prior, -1 bearish, 0 ambiguous
    confidence: float      # 0..1 prior strength (tuned by study later)
    hit: str               # the phrase that matched


# Each rule: (event_type, expected_dir, confidence, [regex-ish phrases]).
# Phrases are matched case-insensitively with word boundaries. Ordered by
# specificity - more specific / higher-signal events first so they win the
# "primary" pick. Priors are deliberately conservative.
_RULES: List[Tuple[str, int, float, List[str]]] = [
    # ── earnings ────────────────────────────────────────────────────────
    ("results_beat", +1, 0.7, [
        "beats estimate", "beat estimate", "tops estimate", "tops charts",
        "profit jumps", "profit surges", "profit rises", "profit grows",
        "profit up", "record profit", "record revenue", "strong results",
        "strong operating", "robust growth", "double-digit growth",
        "pat rises", "pat jumps", "margin expansion",
    ]),
    ("results_miss", -1, 0.7, [
        "misses estimate", "miss estimate", "profit falls", "profit drops",
        "profit declines", "profit slumps", "net loss", "posts loss",
        "widens loss", "revenue miss", "weak results", "margin contraction",
        "disappointing", "profit down",
    ]),
    # ── capital-return ─────────────────────────────────────────────────
    ("buyback", +1, 0.6, ["buyback", "buy-back", "buy back shares", "share repurchase"]),
    ("bonus", +1, 0.55, ["bonus issue", "bonus share", "bonus shares"]),
    ("stock_split", +1, 0.45, ["stock split", "share split", "sub-division of shares"]),
    ("dividend", +1, 0.4, ["special dividend", "interim dividend", "final dividend",
                           "dividend declared", "record dividend"]),
    # ── structural ─────────────────────────────────────────────────────
    ("demerger", +1, 0.6, ["demerger", "demerge", "spin off", "spin-off",
                           "hive off", "hive-off", "value unlock"]),
    ("merger", +1, 0.45, ["merger", "to merge", "amalgamation", "merge with"]),
    ("acquisition", +1, 0.5, ["acquires", "acquisition", "to acquire", "buys stake",
                              "takeover", "acquire controlling"]),
    ("stake_sale", -1, 0.55, ["stake sale", "block deal", "offer for sale", " ofs ",
                              "promoter sells", "promoter selling", "pledge increase",
                              "sells stake"]),
    ("fundraise", 0, 0.35, ["qip", "raises funds", "fund raising", "fundraise",
                            "preferential issue", "rights issue"]),  # dilution vs growth - ambiguous
    # ── business wins ──────────────────────────────────────────────────
    ("order_win", +1, 0.6, ["wins order", "bags order", "order win", "order received",
                            "secures order", "secures contract", "wins contract",
                            "bags contract", "successful bidder", "l1 bidder",
                            "awarded contract", "emerges as", "new order"]),
    ("expansion", +1, 0.5, ["expansion", "capex", "new plant", "capacity addition",
                            "capacity expansion", "sets up", "greenfield", "brownfield",
                            "commissions"]),
    ("approval", +1, 0.55, ["usfda", "us fda", "fda approval", "drug approval",
                            "regulatory approval", "gets approval", "receives approval",
                            "clearance", "granted licence", "granted license",
                            "new licence", "cdsco", "anda approval", "gets nod"]),
    # ── ratings ────────────────────────────────────────────────────────
    ("upgrade", +1, 0.5, ["rating upgrade", "upgraded to", "raised to buy",
                          "target raised", "target hiked", "brokerage upgrade"]),
    ("downgrade", -1, 0.5, ["rating downgrade", "downgraded to", "cut to sell",
                            "target cut", "target slashed", "brokerage downgrade"]),
    # ── negatives ──────────────────────────────────────────────────────
    ("regulatory_action", -1, 0.65, ["sebi notice", "sebi order", "sebi probe",
                                     "income tax raid", "ed raid", "cbi probe",
                                     "penalty", "imposes fine", "fraud", "money laundering",
                                     "show cause", "gst notice", "tax demand"]),
    ("default_risk", -1, 0.7, ["debt default", "loan default", "insolvency",
                               "nclt", "bankruptcy", "rating downgrade to default",
                               "debt concern", "pledge invoked"]),
    ("management_exit", -1, 0.4, ["ceo resigns", "cfo resigns", "md resigns",
                                  "steps down", "resignation", "auditor resigns"]),
    ("recall_ban", -1, 0.55, ["product recall", "import ban", "plant shutdown",
                              "operations halted", "banned"]),
]

# precompiled
_COMPILED = [
    (etype, edir, conf, re.compile(
        r"\b(?:%s)\b" % "|".join(re.escape(p.strip()) for p in phrases),
        re.IGNORECASE))
    for etype, edir, conf, phrases in _RULES
]


def classify(text: str) -> List[EventMatch]:
    """All event types present in the text. Empty if none."""
    if not text:
        return []
    out: List[EventMatch] = []
    for etype, edir, conf, rx in _COMPILED:
        m = rx.search(text)
        if m:
            out.append(EventMatch(etype, edir, conf, m.group(0)))
    return out


def classify_primary(text: str) -> Tuple[str, int, float]:
    """Single strongest event -> (event_type, expected_dir, confidence).
    Highest-confidence match wins; ('none', 0, 0.0) if nothing matched."""
    matches = classify(text)
    if not matches:
        return ("none", 0, 0.0)
    best = max(matches, key=lambda e: e.confidence)
    return (best.event_type, best.expected_dir, best.confidence)


def net_direction(text: str) -> Tuple[int, float, List[str]]:
    """Aggregate direction across ALL matched events.
    Returns (net_sign, confidence, [event_types]). Handles mixed news
    (e.g. 'profit beats but sebi notice')."""
    matches = classify(text)
    if not matches:
        return (0, 0.0, [])
    score = sum(e.expected_dir * e.confidence for e in matches)
    types = [e.event_type for e in matches]
    sign = 1 if score > 0.15 else -1 if score < -0.15 else 0
    return (sign, round(abs(score), 2), types)


if __name__ == "__main__":
    tests = [
        "Reliance Q1 profit jumps 12%, beats estimate on strong operating performance",
        "XYZ announces Rs 5000 cr buyback at premium",
        "ABC Pharma gets USFDA approval for generic drug",
        "PQR to demerge financial services arm, value unlock expected",
        "LMN wins order worth Rs 2000 cr from Railways",
        "DEF hit by SEBI notice, promoter sells stake in block deal",
        "GHI Q2 net loss widens, misses estimate",
        "Central Bank of India Q1 profit inches up 3.2 pc",  # 'ban' trap
    ]
    for t in tests:
        et, ed, c = classify_primary(t)
        ns, nc, types = net_direction(t)
        print(f"[{et:18s} dir={ed:+d} conf={c:.2f}] net={ns:+d}({nc}) {types}")
        print(f"    {t[:70]}")
