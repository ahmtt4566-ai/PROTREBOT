"""Opt-in Original adapter; existing callers and research paths are unchanged."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .contracts import QualityCheck, StrategyInput, StrategyResult, StrategySignal
from .provenance import original_provenance


def _quality_checks(
    legacy: dict[str, Any], effective_policy: Mapping[str, Any],
) -> tuple[QualityCheck, ...]:
    analysis = legacy.get("analysis")
    if analysis is None:
        return ()
    radar = analysis["radar"]
    mtf = legacy["mtf"]
    return (
        QualityCheck("direction", analysis["direction"] in {"LONG", "SHORT"},
                     analysis["direction"], ("LONG", "SHORT")),
        QualityCheck("confidence", analysis["confidence"] >= effective_policy["confidence_threshold"],
                     analysis["confidence"], effective_policy["confidence_threshold"]),
        QualityCheck("trap", radar["trap_score"] <= 35, radar["trap_score"], 35),
        QualityCheck("breakout", radar["breakout_quality"] >= effective_policy["breakout_quality_threshold"],
                     radar["breakout_quality"], effective_policy["breakout_quality_threshold"]),
        QualityCheck("mtf", bool(mtf["entry_permission"]), deepcopy(mtf), True),
    )


def evaluate(request: StrategyInput) -> StrategyResult:
    """Evaluate strategy eligibility, NOT order permission, with no account risk.

    Delegates all decisions and rejection precedence to the native canonical
    function with policy=None and account_context=None. Reported health/warmup
    metadata cannot change its validation, filtering or minimum-candle rules.
    """
    from ..main import (
        _resolve_historical_policy_override,
        canonical_historical_decision,
    )

    candles = deepcopy(request.candles_by_timeframe)
    override = deepcopy(request.historical_policy_override)
    effective_policy = _resolve_historical_policy_override(override)
    native = canonical_historical_decision(
        request.symbol, candles, request.decision_time,
        required_intervals=request.required_intervals,
        policy=None, account_context=None,
        historical_policy_override=override,
    )
    legacy = deepcopy(native)
    analysis = legacy.get("analysis") or {}
    decision = legacy["decision"]
    if decision not in {"BUY", "SELL", "WAIT"}:
        raise ValueError(f"Unsupported native strategy decision: {decision!r}")
    signal = StrategySignal(
        decision=decision,
        strategy_eligible=legacy["entry_eligible"],
        symbol=request.symbol,
        decision_time=request.decision_time,
        signal_open_time=legacy.get("signal_timestamp"),
        latest_closed_timestamps=deepcopy(legacy.get("latest_closed_timestamps", {})),
        provenance=original_provenance(effective_policy, request.required_intervals),
        market_type=request.market_type,
        exit_policy_id=request.exit_policy_id,
        direction=analysis.get("direction"),
        entry=analysis.get("entry"),
        stop=analysis.get("stop_loss"),
        tp1=analysis.get("tp1"),
        tp2=analysis.get("tp2"),
        tp3=analysis.get("tp3"),
        atr=analysis.get("atr"),
        invalidation=None,
        reason=legacy["reason"],
        reasons=tuple(legacy["reasons"]),
        quality_checks=_quality_checks(legacy, effective_policy),
        diagnostics={
            "analysis": deepcopy(analysis),
            "mtf": deepcopy(legacy.get("mtf")),
            "regime": legacy.get("regime"),
        },
        overlay=deepcopy(analysis.get("series")),
    )
    return StrategyResult(signal=signal, legacy=legacy)
