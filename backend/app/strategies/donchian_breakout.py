"""Observation-only Donchian: no admission, account risk, execution or I/O."""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

from ..analysis import atr
from .contracts import (
    Decision,
    QualityCheck,
    StrategyInput,
    StrategyResult,
    StrategySignal,
)
from .donchian_indicator import (
    CandleDataError,
    closed_contract_candles,
    reference_channel,
)
from .donchian_params import DonchianParams


def evaluate(request: StrategyInput, params: DonchianParams | None = None) -> StrategyResult:
    """Return strategy_eligible, NOT order permission; never fabricate quality scores.

    First crossing compares each close to its own prior-N reference channel.
    ATR uses all supplied contiguous CLOSED 15m contract candles. The exit ID is
    metadata describing existing behavior, not an installed or enabled policy.
    No Original-compatible legacy decision exists, so result.legacy is empty.
    """
    from ..main import v20_target_plan

    settings = params if params is not None else DonchianParams()
    if request.historical_policy_override is not None:
        raise ValueError("Original historical_policy_override is not a Donchian parameter")
    if request.exit_policy_id not in (None, settings.exit_policy_id):
        raise ValueError("Donchian observation must retain the existing exit behavior")

    diagnostics: dict[str, Any] = {"observation_only": True, "admission": "NOT_EVALUATED"}
    checks: list[QualityCheck] = []
    signal_time: int | None = None
    timestamps: dict[str, int] = {}
    overlay: dict[str, Any] | None = None
    direction: str | None = None
    atr_value: float | None = None

    def finish(
        reason: str, *, decision: Decision = "WAIT", plan: dict[str, float] | None = None,
    ) -> StrategyResult:
        levels = plan if plan is not None else {}
        signal = StrategySignal(
            decision=decision, strategy_eligible=decision != "WAIT",
            symbol=request.symbol, decision_time=request.decision_time,
            signal_open_time=signal_time, latest_closed_timestamps=dict(timestamps),
            provenance=settings.provenance, market_type=request.market_type,
            exit_policy_id=settings.exit_policy_id, direction=direction,
            entry=levels.get("entry"), stop=levels.get("stop"),
            tp1=levels.get("tp1"), tp2=levels.get("tp2"), tp3=levels.get("tp3"),
            atr=atr_value, invalidation=None, reason=reason, reasons=(reason,),
            quality_checks=tuple(checks), diagnostics=diagnostics, overlay=overlay,
        )
        return StrategyResult(signal=signal, legacy={})

    if "15m" not in request.required_intervals:
        return finish("UNSUPPORTED_TIMEFRAME")
    try:
        candles = closed_contract_candles(request.candles_by_timeframe.get("15m", []), request.decision_time)
    except CandleDataError as exc:
        diagnostics["data_error"] = exc.detail
        return finish(exc.reason)

    available = len(candles)
    channel_required, atr_required = settings.channel_period + 2, settings.atr_period + 1
    diagnostics["warmup"] = {
        "available": available, "channel_required": channel_required, "atr_required": atr_required,
        "channel_ready": available >= channel_required, "atr_ready": available >= atr_required,
    }
    ready = available >= max(channel_required, atr_required)
    checks.append(QualityCheck("warmup", ready, available, max(channel_required, atr_required)))
    if candles:
        signal_time = candles[-1].time
        timestamps["15m"] = signal_time
    if not ready:
        return finish("INSUFFICIENT_CLOSED_CANDLES")

    channel = reference_channel(candles, available - 1, settings.channel_period)
    previous = reference_channel(candles, available - 2, settings.channel_period)
    if channel is None or previous is None:
        raise RuntimeError("Donchian warmup invariant violated")
    diagnostics["channel"], diagnostics["previous_channel"] = asdict(channel), asdict(previous)
    overlay = {
        "donchian_upper": [{"time": candles[-2].time, "value": previous.upper},
                           {"time": signal_time, "value": channel.upper}],
        "donchian_lower": [{"time": candles[-2].time, "value": previous.lower},
                           {"time": signal_time, "value": channel.lower}],
    }
    nonzero = channel.width > 0 and previous.width > 0
    checks.append(QualityCheck("nonzero_channel", nonzero, (channel.width, previous.width), "> 0"))
    if not nonzero:
        return finish("ZERO_WIDTH_CHANNEL")

    close, previous_close = candles[-1].close, candles[-2].close
    direction = "LONG" if close > channel.upper else "SHORT" if close < channel.lower else None
    checks.append(QualityCheck("close_breakout", direction is not None, close, (channel.lower, channel.upper)))
    if direction is None:
        return finish("NO_BREAKOUT")
    first = previous_close <= previous.upper if direction == "LONG" else previous_close >= previous.lower
    checks.append(QualityCheck("first_cross", first, previous_close, (previous.lower, previous.upper)))
    if not first:
        return finish("ALREADY_OUTSIDE_CHANNEL")

    try:
        measured_atr = atr(
            [row.high for row in candles], [row.low for row in candles],
            [row.close for row in candles], period=settings.atr_period,
        )
    except OverflowError as exc:
        diagnostics["atr_error"] = str(exc)
        return finish("INVALID_ATR")
    valid_atr = math.isfinite(measured_atr) and measured_atr > 0
    checks.append(QualityCheck("atr", valid_atr, measured_atr if math.isfinite(measured_atr) else None, "> 0"))
    if not valid_atr:
        return finish("INVALID_ATR")
    atr_value = measured_atr
    stop = close - settings.atr_multiplier * atr_value if direction == "LONG" else close + settings.atr_multiplier * atr_value
    valid_stop = math.isfinite(stop) and stop > 0 and (stop < close if direction == "LONG" else stop > close)
    checks.append(QualityCheck("stop", valid_stop, stop if math.isfinite(stop) else None, direction))
    if not valid_stop:
        return finish("INVALID_STOP_PLAN")
    try:
        targets = v20_target_plan(close, stop, direction)
        levels = {key: float(targets[key]) for key in ("tp1", "tp2", "tp3")}
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        diagnostics["target_error"] = str(exc)
        return finish("INVALID_TARGET_PLAN")
    values = (levels["tp1"], levels["tp2"], levels["tp3"])
    valid_targets = all(math.isfinite(value) and value > 0 for value in values) and (
        close < values[0] < values[1] < values[2] if direction == "LONG"
        else close > values[0] > values[1] > values[2]
    )
    checks.append(QualityCheck("targets", valid_targets, values, "positive, strictly ordered, profitable side"))
    if not valid_targets:
        return finish("INVALID_TARGET_PLAN")
    return finish("READY", decision="BUY" if direction == "LONG" else "SELL",
                  plan={"entry": close, "stop": stop, **levels})
