"""One immutable offline risk profile; never a LIVE/Demo policy option."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Any

from app import execution_core as core
from app.strategies.provenance import parameter_hash


class OfflineRiskError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class OriginalOfflineRiskProfile:
    profile_id: str = "original-fixed-cap6-lev3-v1"
    experiment_id: str = "kais_original_v2_offline_risk"
    scope: str = "OFFLINE_ONLY"
    stop_mode: str = "FIXED_CAP"
    stop_cap_pct: float = 6.0
    risk_budget_usdt: float = 3.0
    leverage: int = 3
    max_margin_usdt: float = 25.0
    min_margin_usdt: float = 5.0
    liquidation_buffer_pct: float = 0.5
    exposure_usdt: float = 350.0

    def __post_init__(self) -> None:
        if any(type(getattr(self, field.name)) is not type(field.default)
               or getattr(self, field.name) != field.default for field in fields(self)):
            raise OfflineRiskError("UNREGISTERED_OFFLINE_PROFILE")

    @property
    def profile_hash(self) -> str:
        return parameter_hash(asdict(self))

    def policy(self, source: dict[str, Any]) -> dict[str, Any]:
        return core.sanitize_execution_policy({
            **source, "max_loss_per_trade": self.risk_budget_usdt,
            "max_leverage": self.leverage, "max_margin_per_trade": self.max_margin_usdt,
            "liquidation_buffer_pct": self.liquidation_buffer_pct,
            "max_total_exposure_usdt": self.exposure_usdt,
        }, preserve_empty_allowed_symbols=True)

    def accepts(self, entry: float, stop: float) -> bool:
        if not all(math.isfinite(value) and value > 0 for value in (entry, stop)) or entry == stop:
            raise OfflineRiskError("INVALID_OFFLINE_STOP")
        return abs(entry - stop) / entry * 100 <= self.stop_cap_pct + 1e-9

    def size(self, entry: float, stop: float, *, margin_limit: float | None = None) -> dict[str, Any]:
        if not self.accepts(entry, stop):
            raise OfflineRiskError("profile_cap")
        limit = self.max_margin_usdt if margin_limit is None else margin_limit
        if not math.isfinite(limit) or not 0 < limit <= self.max_margin_usdt:
            raise OfflineRiskError("INVALID_OFFLINE_MARGIN_LIMIT")
        distance = abs(entry - stop) / entry * 100
        risk_notional = self.risk_budget_usdt / (distance / 100)
        notional = min(risk_notional, limit * self.leverage)
        return {
            "entry": round(entry, 10), "stop": round(stop, 10),
            "stop_distance_pct": round(distance, 5), "max_stop_distance_pct": self.stop_cap_pct,
            "leverage": self.leverage, "notional_usdt": round(notional, 6),
            "margin_usdt": round(notional / self.leverage, 6),
            "estimated_stop_loss_usdt": round(notional * distance / 100, 6),
            "capped": notional + 1e-9 < risk_notional,
        }


PROFILE = OriginalOfflineRiskProfile()
