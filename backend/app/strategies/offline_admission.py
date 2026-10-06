"""Donchian-only offline admission; no order authority or LIVE/Demo integration.

The native aggregate result is retained, including its failed legacy quality
checks. Offline eligibility is a separate, stage-local result, not a rewritten
core verdict. Any change to the native gate schema requires an explicit review.
"""

from __future__ import annotations

import logging
import math
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from .contracts import QualityCheck, StrategyInput, StrategyResult
from .donchian_breakout import evaluate
from .donchian_indicator import CandleDataError, closed_contract_candles
from .donchian_params import DonchianParams, donchian_preset

logger = logging.getLogger(__name__)

CORE_GATE_KEYS = (
    "arm", "symbol", "direction", "long", "short", "confidence", "trap",
    "spread", "one_way", "positions", "duplicate", "active_plan", "exposure",
    "daily_trades", "daily_loss", "open_loss", "pnl_verified",
    "consecutive_losses", "same_direction_positions", "direction_exposure",
)
LEGACY_QUALITY_KEYS = ("confidence", "trap")
STRATEGY_QUALITY_KEYS = ("data", "warmup", "channel", "breakout", "atr", "stop", "tp", "provenance")


@dataclass(frozen=True)
class AdmissionReport:
    """Only offline eligibility for the evaluated stage; never order permission."""

    offline_eligible: bool
    strategy_quality: tuple[QualityCheck, ...]
    raw_entry_gates: Any
    legacy_quality: tuple[dict[str, Any], ...]
    failures: tuple[str, ...]
    candidate_notional_usdt: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def approved_params(params: DonchianParams) -> DonchianParams:
    """Resolve only registered, immutable presets without changing their defaults."""
    registered = donchian_preset(params.provenance.strategy_version)
    if params.provenance != registered.provenance or params.parameters() != registered.parameters():
        raise ValueError("Offline admission requires an unmodified registered Donchian preset")
    return registered


def _positive(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def strategy_quality(
    request: StrategyInput, result: StrategyResult, params: DonchianParams,
) -> tuple[QualityCheck, ...]:
    """Revalidate actual closed data and compare with the unchanged pure signal.

    Recomputing here prevents a caller-modified cached projection or fabricated
    check flags from becoming admission evidence. Reported health is not trusted.
    """
    registered = approved_params(params)
    expected = evaluate(request, registered)
    signal, native = result.signal, expected.signal
    checks = {check.key: check for check in native.quality_checks}
    try:
        closed = closed_contract_candles(request.candles_by_timeframe.get("15m", []), request.decision_time)
        data_ok = (
            request.decision_time % 900 == 0
            and bool(closed) and closed[-1].time == request.decision_time - 900
            and request.required_intervals == ("15m",)
            and request.market_type == "USD_M_CONTRACT"
            and signal.symbol == request.symbol and signal.decision_time == request.decision_time
            and signal.signal_open_time == native.signal_open_time
            and signal.latest_closed_timestamps == native.latest_closed_timestamps
        )
        data_value = {"closed_count": len(closed), "latest_open": closed[-1].time if closed else None}
    except CandleDataError as exc:
        data_ok, data_value = False, {"reason": exc.reason, "detail": exc.detail}

    def passed(key: str) -> bool:
        return key in checks and checks[key].passed is True

    warmup = passed("warmup") and signal.diagnostics.get("warmup") == native.diagnostics.get("warmup")
    channel = (
        passed("nonzero_channel")
        and signal.diagnostics.get("channel") == native.diagnostics.get("channel")
        and signal.diagnostics.get("previous_channel") == native.diagnostics.get("previous_channel")
        and signal.overlay == native.overlay
    )
    breakout = (
        passed("close_breakout") and passed("first_cross")
        and signal.decision == native.decision and signal.direction == native.direction
        and signal.strategy_eligible is True and native.strategy_eligible is True
    )
    atr = passed("atr") and _positive(signal.atr) and signal.atr == native.atr
    stop = (
        passed("stop") and _positive(signal.entry) and _positive(signal.stop)
        and signal.entry == native.entry and signal.stop == native.stop
    )
    targets = (signal.tp1, signal.tp2, signal.tp3)
    tp = passed("targets") and all(_positive(value) for value in targets) and targets == (native.tp1, native.tp2, native.tp3)
    provenance = (
        signal.provenance == registered.provenance and signal.exit_policy_id == registered.exit_policy_id
        and signal.market_type == request.market_type and result.legacy == {}
        and result == expected
    )
    values = (
        (data_ok, data_value), (warmup, signal.diagnostics.get("warmup")),
        (channel, signal.diagnostics.get("channel")), (breakout, signal.decision),
        (atr, signal.atr), (stop, {"entry": signal.entry, "stop": signal.stop}),
        (tp, targets), (provenance, asdict(signal.provenance)),
    )
    return tuple(
        QualityCheck(key, bool(ok), deepcopy(value), "Verified closed-data Donchian contract")
        for key, (ok, value) in zip(STRATEGY_QUALITY_KEYS, values, strict=True)
    )


def gate_schema_error(raw: object, direction: str | None) -> str | None:
    """Exact keys, order, field types and aggregate consistency; no truth coercion."""
    if not isinstance(raw, dict) or set(raw) != {"passed", "decision", "reason", "gates"}:
        return "Invalid native gate result fields"
    if (
        type(raw["passed"]) is not bool or not isinstance(raw["decision"], str)
        or not isinstance(raw["reason"], str) or not raw["reason"].strip()
    ):
        return "Invalid native verdict types"
    gates = raw["gates"]
    if not isinstance(gates, list) or len(gates) != len(CORE_GATE_KEYS):
        return "Missing or added native gate"
    for gate in gates:
        if not isinstance(gate, dict) or set(gate) != {"passed", "key", "label", "detail"}:
            return "Invalid native gate fields"
        if type(gate["passed"]) is not bool or any(
            not isinstance(gate[key], str) or not gate[key].strip() for key in ("key", "label", "detail")
        ):
            return "Invalid native gate field types"
    if tuple(gate["key"] for gate in gates) != CORE_GATE_KEYS:
        return "Missing, unknown, duplicated or reordered native gate key"
    failed = [gate for gate in gates if not gate["passed"]]
    if raw["passed"] is not (not failed) or raw["decision"] != (direction if not failed else "BEKLE"):
        return "Inconsistent native aggregate verdict"
    if failed and raw["reason"] != failed[0]["detail"]:
        return "Inconsistent native rejection precedence"
    if any(gate["passed"] for gate in gates if gate["key"] in LEGACY_QUALITY_KEYS):
        return "Unexpected legacy quality pass for an unscored Donchian signal"
    return None


def admit(
    request: StrategyInput, result: StrategyResult, params: DonchianParams, *,
    raw_entry_gates: Any, candidate_notional_usdt: float,
) -> AdmissionReport:
    quality = strategy_quality(request, result, params)
    failures = [f"strategy_{check.key}" for check in quality if not check.passed]
    notional_valid = (
        type(candidate_notional_usdt) in (int, float)
        and math.isfinite(candidate_notional_usdt) and candidate_notional_usdt >= 0
    )
    if not notional_valid:
        failures.append("candidate_notional_invalid")
    error = gate_schema_error(raw_entry_gates, result.signal.direction)
    legacy: list[dict[str, Any]] = []
    if error is not None:
        failures.append("gate_schema")
        logger.warning("OFFLINE_ADMISSION_SCHEMA_REJECTED symbol=%s reason=%s", request.symbol, error)
    else:
        for gate in raw_entry_gates["gates"]:
            if gate["key"] in LEGACY_QUALITY_KEYS:
                legacy.append({
                    "key": gate["key"], "applicability": "NOT_APPLICABLE_LEGACY_QUALITY",
                    "reason": "Donchian does not produce Original confidence/trap scores",
                    "raw_gate": deepcopy(gate),
                })
            elif not gate["passed"]:
                failures.append(gate["key"])
    if failures:
        logger.info("OFFLINE_DONCHIAN_ADMISSION_REJECTED symbol=%s failures=%s", request.symbol, failures)
    return AdmissionReport(
        offline_eligible=not failures, strategy_quality=quality,
        raw_entry_gates=deepcopy(raw_entry_gates), legacy_quality=tuple(legacy),
        failures=tuple(failures), candidate_notional_usdt=candidate_notional_usdt,
    )
