"""Post-canonical F2 admission only; parent risk and open positions are unchanged."""

from collections import Counter
from collections.abc import Mapping
from math import isfinite
from typing import Any

from app.backtest_baseline import Config
from app.backtest_data import Dataset
from app.strategies.original_f2_risk import PROFILE, OriginalF2RiskProfile
from app.strategies.original_gap_engine import OriginalGapRiskEngine
from app.strategies.original_offline_risk import OfflineRiskError


def f2_value(signal: Mapping[str, Any]) -> float | None:
    averages = signal.get("ema")
    if not isinstance(averages, Mapping):
        return None
    shortest, longest, atr = averages.get("ema20"), averages.get("ema200"), signal.get("atr")
    if any(type(value) not in (int, float) or not isfinite(value) for value in (shortest, longest, atr)):
        return None
    if atr <= 0:
        return None
    result = abs(shortest - longest) / atr
    return result if isfinite(result) else None


class OriginalF2RiskEngine(OriginalGapRiskEngine):
    def __init__(self, data: Dataset, config: Config, *, profile: OriginalF2RiskProfile = PROFILE):
        if type(profile) is not OriginalF2RiskProfile or profile != PROFILE:
            raise OfflineRiskError("UNREGISTERED_OFFLINE_PROFILE")
        super().__init__(data, config)
        self.profile = profile
        self.candidates_before_f2 = 0
        self.candidates_after_f2 = 0
        self.f2_rejections: Counter[str] = Counter()
        self.f2_uncomputable: Counter[str] = Counter()

    def f2_accepts(self, signal: Mapping[str, Any]) -> bool:
        value = f2_value(signal)
        return value is not None and value > self.profile.f2_threshold

    def apply_f2(self, signal: dict[str, Any]) -> bool:
        direction = signal.get("direction")
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("Invalid canonical F2 candidate direction")
        self.candidates_before_f2 += 1
        if self.f2_accepts(signal):
            self.candidates_after_f2 += 1
            return True
        self.f2_rejections[direction] += 1
        if f2_value(signal) is None:
            self.f2_uncomputable[direction] += 1
        self.reject(["f2_filter"])
        return False

    async def _enter(self, symbol: str, signal: dict, at: int) -> None:
        if symbol in self.policy["allowed_symbols"] and not self.f2_accepts(signal):
            self.apply_f2(signal)
            return
        await super()._enter(symbol, signal, at)

    def filter_counts(self) -> dict:
        return {
            "candidates_before": self.candidates_before_f2,
            "candidates_after": self.candidates_after_f2,
            "rejections_by_direction": {side: self.f2_rejections[side] for side in ("LONG", "SHORT")},
            "uncomputable_by_direction": {side: self.f2_uncomputable[side] for side in ("LONG", "SHORT")},
            "at_or_below_threshold": sum(self.f2_rejections.values()) - sum(self.f2_uncomputable.values()),
            "uncomputable": sum(self.f2_uncomputable.values()),
        }
