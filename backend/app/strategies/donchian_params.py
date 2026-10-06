"""Fixed, pre-result parameters for the observation-only Donchian signal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import StrategyProvenance
from .provenance import parameter_hash

STRATEGY_ID = "donchian_breakout"
STRATEGY_VERSION = "signal-v1"
EXISTING_EXIT_POLICY_ID = "existing_stop_tp1_60_tp3"


@dataclass(frozen=True)
class DonchianParams:
    """Only the approved preset is supported; no tuning or execution authority."""

    channel_period: int = 20
    atr_period: int = 14
    atr_multiplier: float = 2.0
    breakout_mode: str = "close"
    strict: bool = True
    first_cross_only: bool = True
    zero_width_policy: str = "WAIT"
    tp_multiples: tuple[float, float, float] = (1.0, 2.0, 3.0)
    timeframe: str = "15m"
    exit_policy_id: str = EXISTING_EXIT_POLICY_ID

    def __post_init__(self) -> None:
        if not (
            type(self.channel_period) is int and self.channel_period == 20
            and type(self.atr_period) is int and self.atr_period == 14
            and type(self.atr_multiplier) in (int, float) and self.atr_multiplier == 2.0
            and self.breakout_mode == "close"
            and self.strict is True and self.first_cross_only is True
            and self.zero_width_policy == "WAIT"
            and isinstance(self.tp_multiples, tuple) and self.tp_multiples == (1.0, 2.0, 3.0)
            and all(type(value) in (int, float) for value in self.tp_multiples)
            and self.timeframe == "15m" and self.exit_policy_id == EXISTING_EXIT_POLICY_ID
        ):
            raise ValueError("Only the approved Donchian 20 / ATR14x2 / close-first-cross preset is supported")

    def parameters(self) -> dict[str, Any]:
        return {
            "channel_period": self.channel_period, "atr_period": self.atr_period,
            "atr_multiplier": float(self.atr_multiplier), "breakout_mode": self.breakout_mode,
            "strict": self.strict, "first_cross_only": self.first_cross_only,
            "zero_width_policy": self.zero_width_policy, "tp_multiples": [1.0, 2.0, 3.0],
            "timeframe": self.timeframe, "exit_policy_id": self.exit_policy_id,
            "entry_reference": "DECISION_CLOSE",
            "atr_history": "ALL_CONTIGUOUS_CLOSED_CANDLES_SEED_FIRST_14_TRUE_RANGES",
            "first_cross_reference": "PREVIOUS_CLOSE_VS_PREVIOUS_REFERENCE_CHANNEL",
            "zero_width_scope": "CURRENT_AND_PREVIOUS_REFERENCE_CHANNEL",
        }

    @property
    def provenance(self) -> StrategyProvenance:
        return StrategyProvenance(STRATEGY_ID, STRATEGY_VERSION, parameter_hash(self.parameters()))


STRATEGY_VERSION_V2 = "signal-v2"


@dataclass(frozen=True)
class DonchianParamsV2(DonchianParams):
    """Explicit opt-in ATR14x1.4 preset; signal-v1 and the default stay ATR14x2."""

    atr_multiplier: float = 1.4

    def __post_init__(self) -> None:
        if type(self.atr_multiplier) not in (int, float) or self.atr_multiplier != 1.4:
            raise ValueError("signal-v2 only supports the approved ATR14x1.4 preset")
        try:
            DonchianParams(
                channel_period=self.channel_period, atr_period=self.atr_period,
                atr_multiplier=2.0, breakout_mode=self.breakout_mode, strict=self.strict,
                first_cross_only=self.first_cross_only, zero_width_policy=self.zero_width_policy,
                tp_multiples=self.tp_multiples, timeframe=self.timeframe, exit_policy_id=self.exit_policy_id,
            )
        except ValueError as exc:
            raise ValueError("signal-v2 must retain the other approved Donchian parameters") from exc

    @property
    def provenance(self) -> StrategyProvenance:
        return StrategyProvenance(STRATEGY_ID, STRATEGY_VERSION_V2, parameter_hash(self.parameters()))


DONCHIAN_PRESETS = (DonchianParams(), DonchianParamsV2())


def donchian_preset(version: str) -> DonchianParams:
    """Select only registered immutable versions; no implicit default upgrade."""
    for preset in DONCHIAN_PRESETS:
        if preset.provenance.strategy_version == version:
            return preset
    raise ValueError(f"Unsupported Donchian preset version: {version!r}")
