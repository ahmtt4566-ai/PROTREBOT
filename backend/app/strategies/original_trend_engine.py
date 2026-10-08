"""SMA600 admission after Original canonical decisions; native open positions are untouched."""

from collections import Counter
from dataclasses import dataclass
from itertools import pairwise
from math import isfinite
from statistics import fmean

from app.backtest_baseline import Config
from app.backtest_data import Dataset, Series
from app.strategies.original_gap_engine import OriginalGapRiskEngine
from app.strategies.original_offline_risk import OfflineRiskError
from app.strategies.original_trend_risk import PROFILE, OriginalTrendRiskProfile

INTERVAL = 4 * 3600
PERMISSIONS = ("LONG", "SHORT", "NONE")
CAUSES = ("ALLOWED", "DIRECTION_MISMATCH", "INSUFFICIENT_WINDOW", "NON_CONTIGUOUS_WINDOW",
          "MISSING_LAST_CLOSED_4H", "EQUAL_TO_SMA", "INVALID_4H_CLOSE")
WARMUP_CAUSES = {"INSUFFICIENT_WINDOW", "NON_CONTIGUOUS_WINDOW", "MISSING_LAST_CLOSED_4H"}


@dataclass(frozen=True)
class TrendPermission:
    direction: str
    cause: str
    last_close: float | None = None
    sma: float | None = None


def closed_sma600(series: Series, at: int) -> TrendPermission:
    if series.interval != "4h":
        raise ValueError("SMA600 requires contract 4h candles")
    rows = series.closed(at, limit=PROFILE.trend_period)
    if len(rows) != PROFILE.trend_period:
        return TrendPermission("NONE", "INSUFFICIENT_WINDOW")
    if rows[-1]["time"] != at // INTERVAL * INTERVAL - INTERVAL:
        return TrendPermission("NONE", "MISSING_LAST_CLOSED_4H")
    if any(type(row["time"]) is not int or row["time"] % INTERVAL for row in rows) or any(
        right["time"] - left["time"] != INTERVAL for left, right in pairwise(rows)
    ):
        return TrendPermission("NONE", "NON_CONTIGUOUS_WINDOW")
    values = [row["close"] for row in rows]
    if any(type(value) not in (int, float) or not isfinite(value) or value <= 0 for value in values):
        return TrendPermission("NONE", "INVALID_4H_CLOSE")
    average, last = fmean(values), values[-1]
    if last == average:
        return TrendPermission("NONE", "EQUAL_TO_SMA", last, average)
    return TrendPermission("LONG" if last > average else "SHORT", "ALLOWED", last, average)


class OriginalTrendRiskEngine(OriginalGapRiskEngine):
    def __init__(self, data: Dataset, config: Config, *, profile: OriginalTrendRiskProfile = PROFILE):
        if type(profile) is not OriginalTrendRiskProfile or profile != PROFILE:
            raise OfflineRiskError("UNREGISTERED_OFFLINE_PROFILE")
        super().__init__(data, config)
        self.profile = profile
        self._trend_cache: dict[tuple[str, int], TrendPermission] = {}
        self.candidates_before_trend = 0
        self.candidates_after_trend = 0
        self.trend_rejections: Counter[str] = Counter()
        self.trend_rejection_causes: Counter[str] = Counter()
        self.permission_points = {symbol: Counter() for symbol in self.data.frames}
        self.warmup_points = {symbol: Counter() for symbol in self.data.frames}
        self.points_per_day: Counter[int] = Counter()

    def permission(self, symbol: str, at: int) -> TrendPermission:
        # Key by expected closed slot, not available index: missing new bars must invalidate readiness.
        key = (symbol, at // INTERVAL)
        if key not in self._trend_cache:
            self._trend_cache[key] = closed_sma600(self.data.frames[symbol]["4h"], at)
        return self._trend_cache[key]

    def sample_permissions(self, symbols: list[str], at: int) -> None:
        day = at // 86400
        self.points_per_day[day] += 1
        for symbol in symbols:
            observation = self.permission(symbol, at)
            self.permission_points[symbol][observation.direction] += 1
            if observation.cause in WARMUP_CAUSES:
                self.warmup_points[symbol][day] += 1

    def trend_accepts(self, symbol: str, direction: str, at: int) -> bool:
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("Invalid canonical trend candidate direction")
        return self.permission(symbol, at).direction == direction

    def apply_trend(self, symbol: str, direction: str, at: int) -> bool:
        self.candidates_before_trend += 1
        if self.trend_accepts(symbol, direction, at):
            self.candidates_after_trend += 1
            return True
        observation = self.permission(symbol, at)
        cause = observation.cause if observation.direction == "NONE" else "DIRECTION_MISMATCH"
        self.trend_rejections[direction] += 1
        self.trend_rejection_causes[cause] += 1
        self.reject(["trend_filter"])
        return False

    async def _enter(self, symbol: str, signal: dict, at: int) -> None:
        if symbol in self.policy["allowed_symbols"] and not self.trend_accepts(symbol, signal["direction"], at):
            self.apply_trend(symbol, signal["direction"], at)
            return
        await super()._enter(symbol, signal, at)

    def trend_counts(self, symbols: list[str]) -> dict:
        opened_days = {position.opened_at // 86400 for position in self.trades}
        all_warm_days = [
            day for day, points in self.points_per_day.items()
            if all(self.warmup_points[symbol][day] == points for symbol in symbols) and day not in opened_days
        ]
        return {
            "candidates_before": self.candidates_before_trend,
            "candidates_after": self.candidates_after_trend,
            "rejections_by_direction": {side: self.trend_rejections[side] for side in ("LONG", "SHORT")},
            "rejections_by_cause": {cause: self.trend_rejection_causes[cause] for cause in CAUSES if cause != "ALLOWED"},
            "all_symbols_full_warmup_no_entry_days": len(all_warm_days),
            "by_symbol": {
                symbol: {
                    "decision_points": sum(self.permission_points[symbol].values()),
                    "permission_points": {side: self.permission_points[symbol][side] for side in PERMISSIONS},
                    "permission_pct": {
                        side: self.permission_points[symbol][side] / sum(self.permission_points[symbol].values()) * 100
                        if self.permission_points[symbol] else None for side in PERMISSIONS
                    },
                    "full_warmup_days": sum(self.warmup_points[symbol][day] == points
                                            for day, points in self.points_per_day.items()),
                    "partial_warmup_days": sum(0 < self.warmup_points[symbol][day] < points
                                               for day, points in self.points_per_day.items()),
                } for symbol in symbols
            },
        }
