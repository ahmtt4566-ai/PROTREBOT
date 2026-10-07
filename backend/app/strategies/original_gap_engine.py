"""Original native lifecycle plus the existing timestamp-derived gap exclusion."""

from collections import Counter
from dataclasses import asdict
from typing import Any

from app.backtest_baseline import Config
from app.backtest_data import Dataset
from app.strategies.offline_facade import _funding_gaps, _series_gaps
from app.strategies.original_gap_risk import PROFILE, OriginalGapRiskProfile
from app.strategies.original_offline_engine import OriginalOfflineRiskEngine
from app.strategies.original_offline_risk import OfflineRiskError
from app.strategies.provenance import parameter_hash

STREAMS = ("contract", "mark", "funding")


class OriginalGapRiskEngine(OriginalOfflineRiskEngine):
    def __init__(self, data: Dataset, config: Config, *, profile: OriginalGapRiskProfile = PROFILE):
        if type(profile) is not OriginalGapRiskProfile or profile != PROFILE:
            raise OfflineRiskError("UNREGISTERED_OFFLINE_PROFILE")
        # The parent's registered risk settings are identical; only this entry exclusion is new.
        super().__init__(data, config)
        self.profile = profile
        self.gaps = {}
        self.gap_inventory = {}
        self.blackout_by_symbol: Counter[str] = Counter()
        self.blackout_by_type: Counter[str] = Counter()
        self.blackout_by_symbol_and_type: dict[str, Counter[str]] = {}
        for symbol, frames in sorted(self.data.frames.items()):
            if "15m" not in frames or symbol not in self.data.marks:
                raise ValueError("Offline gap inventory requires contract and mark series per symbol")
            coverage, gaps = {}, []
            # Reuse the exact Donchian inventory functions, including funding tolerance.
            for stream, series in (("contract", frames["15m"]), ("mark", self.data.marks[symbol])):
                coverage[stream], missing = _series_gaps(series, stream, config)
                gaps.extend(missing)
            funding, missing = _funding_gaps(self.data.funding.get(symbol, []))
            gaps.extend(missing)
            self.gaps[symbol] = tuple(gaps)
            self.gap_inventory[symbol] = {
                "coverage": coverage, "funding": funding, "gaps": [asdict(gap) for gap in gaps],
            }
            self.blackout_by_symbol_and_type[symbol] = Counter()

    def entry_data_exclusion(self, symbol: str, at: int, phase: str) -> bool:
        if phase not in {"before_market_ranking", "before_entry"}:
            raise ValueError("Invalid offline gap exclusion phase")
        types = {
            gap.stream for gap in self.gaps[symbol]
            if gap.start - self.profile.gap_blackout_seconds <= at < gap.start
        }
        if not types:
            return False
        self.blackout_by_symbol[symbol] += 1
        self.blackout_by_type.update(types)
        self.blackout_by_symbol_and_type[symbol].update(types)
        self.reject(["gap_blackout"])
        return True

    async def _enter(self, symbol: str, signal: dict, at: int) -> None:
        if symbol in self.policy["allowed_symbols"] and self.entry_data_exclusion(symbol, at, "before_entry"):
            return
        await super()._enter(symbol, signal, at)

    def blackout_counts(self, symbols: list[str]) -> dict[str, Any]:
        return {
            "count": self.rejections["gap_blackout"],
            "by_symbol": {symbol: self.blackout_by_symbol[symbol] for symbol in symbols},
            "by_type": {stream: self.blackout_by_type[stream] for stream in STREAMS},
            "by_symbol_and_type": {
                symbol: {stream: self.blackout_by_symbol_and_type[symbol][stream] for stream in STREAMS}
                for symbol in symbols
            },
            "inventory_sha256": parameter_hash(self.gap_inventory),
            "type_counts_overlap": True,
        }
