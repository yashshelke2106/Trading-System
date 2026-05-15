"""
Champion/Challenger A/B test framework.

Champion = currently active params (default).
Challenger = experimental params being tested.

Every signal: rolled dice -> 80% champion, 20% challenger.
Outcomes tagged. After N trades each, compare WR.
If challenger WR > champion WR + threshold and stat-significant -> promote.
If challenger WR < champion WR - threshold -> kill challenger, keep champion.

Stored at logs/cc_state.json.
"""

import json
import logging
import os
import random
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

CC_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "logs", "cc_state.json")

MIN_TRADES_PER_VARIANT = 20      # need 20+ trades each before deciding
PROMOTE_WR_DELTA = 0.08          # challenger must beat champion by 8% WR
KILL_WR_DELTA = 0.10             # kill challenger if 10% worse
CHALLENGER_TRAFFIC_PCT = 0.20    # 20% of signals try challenger


@dataclass
class Variant:
    name: str                  # "champion" or "challenger"
    params: Dict               # SIGNAL_CONFIG override dict
    trades: List[Dict] = field(default_factory=list)  # [{outcome, pnl, ts}]
    created_ts: float = field(default_factory=time.time)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.get("outcome") == "TARGET_HIT")

    @property
    def wr(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.get("pnl", 0) for t in self.trades)


class ChampionChallenger:
    """Manages champion vs challenger experiments with auto-promotion/kill."""

    def __init__(self):
        self.champion: Variant = Variant(name="champion", params={})
        self.challenger: Optional[Variant] = None
        self._load()

    def _load(self) -> None:
        if not os.path.exists(CC_FILE):
            return
        try:
            with open(CC_FILE) as f:
                data = json.load(f)
            self.champion = Variant(**data.get("champion", {"name": "champion", "params": {}}))
            ch = data.get("challenger")
            self.challenger = Variant(**ch) if ch else None
        except Exception as e:
            log.warning(f"[CC] load failed: {e}")

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(CC_FILE), exist_ok=True)
            data = {
                "champion": asdict(self.champion),
                "challenger": asdict(self.challenger) if self.challenger else None,
                "updated_at": time.time(),
            }
            tmp = CC_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, CC_FILE)
        except Exception as e:
            log.error(f"[CC] save failed: {e}")

    def deploy_challenger(self, params: Dict) -> None:
        """Deploy new challenger. Resets any existing challenger."""
        self.challenger = Variant(name="challenger", params=dict(params))
        self._save()
        log.info(f"[CC] new challenger deployed: {params}")

    def pick_variant(self) -> Variant:
        """Stochastic: pick champion or challenger for next signal."""
        if not self.challenger:
            return self.champion
        if random.random() < CHALLENGER_TRAFFIC_PCT:
            return self.challenger
        return self.champion

    def record_outcome(self, variant_name: str, outcome: str, pnl: float, sym: str = "") -> None:
        """Tag a trade outcome to its variant."""
        target = self.challenger if variant_name == "challenger" and self.challenger else self.champion
        target.trades.append({
            "outcome": outcome,
            "pnl": pnl,
            "symbol": sym,
            "ts": time.time(),
        })
        self._save()
        self._maybe_decide()

    def _maybe_decide(self) -> Optional[str]:
        """Check if challenger should be promoted or killed. Returns action taken."""
        if not self.challenger:
            return None
        if self.challenger.n < MIN_TRADES_PER_VARIANT:
            return None
        if self.champion.n < MIN_TRADES_PER_VARIANT:
            return None  # need baseline too

        delta = self.challenger.wr - self.champion.wr
        if delta >= PROMOTE_WR_DELTA:
            return self._promote()
        elif delta <= -KILL_WR_DELTA:
            return self._kill()
        return None

    def _promote(self) -> str:
        old_wr = self.champion.wr
        new_params = dict(self.challenger.params)
        log.warning(
            f"[CC] PROMOTING challenger -> champion. "
            f"Old WR={old_wr:.1%} -> New WR={self.challenger.wr:.1%} "
            f"params={new_params}"
        )
        self.champion = Variant(name="champion", params=new_params)
        self.challenger = None
        self._save()
        return "promoted"

    def _kill(self) -> str:
        log.warning(
            f"[CC] KILLING challenger. WR={self.challenger.wr:.1%} "
            f"vs champion {self.champion.wr:.1%}"
        )
        self.challenger = None
        self._save()
        return "killed"

    def get_active_params(self, variant_name: str = "champion") -> Dict:
        if variant_name == "challenger" and self.challenger:
            return dict(self.challenger.params)
        return dict(self.champion.params)

    def stats(self) -> Dict:
        return {
            "champion": {
                "n": self.champion.n, "wr": self.champion.wr,
                "pnl": self.champion.total_pnl, "params": self.champion.params,
            },
            "challenger": {
                "n": self.challenger.n, "wr": self.challenger.wr,
                "pnl": self.challenger.total_pnl, "params": self.challenger.params,
            } if self.challenger else None,
        }


# Module-level singleton
_cc: Optional[ChampionChallenger] = None


def get_cc() -> ChampionChallenger:
    global _cc
    if _cc is None:
        _cc = ChampionChallenger()
    return _cc
