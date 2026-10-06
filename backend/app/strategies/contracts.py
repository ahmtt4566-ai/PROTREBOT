"""Additive strategy contracts; no execution or account-risk authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

Decision = Literal["BUY", "SELL", "WAIT"]
Number = int | float


@dataclass(frozen=True)
class StrategyInput:
    """Legacy-shaped candle input; reported metadata never overrides native gates.

    Candle timestamps are opening times in UTC Unix seconds. The native
    evaluator remains responsible for validation and closed-candle selection.
    No client, credentials, sizing, leverage, margin or account policy is accepted.
    """

    symbol: str
    decision_time: int
    candles_by_timeframe: dict[str, list[dict[str, Any]]]
    required_intervals: tuple[str, ...] = ("15m", "1h", "4h", "1d")
    historical_policy_override: Mapping[str, Any] | None = None
    market_type: str | None = None
    exit_policy_id: str | None = None
    latest_closed_timestamps: Mapping[str, int] | None = None
    data_health: Mapping[str, Any] | None = None
    warmup: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class StrategyProvenance:
    strategy_id: str
    strategy_version: str
    parameter_hash: str


@dataclass(frozen=True)
class QualityCheck:
    key: str
    passed: bool
    value: Any
    threshold: Any


@dataclass(frozen=True)
class StrategySignal:
    """Strategy eligibility is NOT order permission.

    Account/risk, authorization, execution and protection gates remain mandatory
    outside this contract. WAIT may retain legacy illustrative price levels.
    """

    decision: Decision
    strategy_eligible: bool
    symbol: str
    decision_time: int
    signal_open_time: int | None
    latest_closed_timestamps: dict[str, int]
    provenance: StrategyProvenance
    market_type: str | None
    exit_policy_id: str | None
    direction: str | None
    entry: Number | None
    stop: Number | None
    tp1: Number | None
    tp2: Number | None
    tp3: Number | None
    atr: Number | None
    invalidation: None
    reason: str
    reasons: tuple[str, ...]
    quality_checks: tuple[QualityCheck, ...]
    diagnostics: dict[str, Any]
    overlay: dict[str, Any] | None


@dataclass(frozen=True)
class StrategyResult:
    """Independent normalized projection and untouched native result snapshot."""

    signal: StrategySignal
    legacy: dict[str, Any]
