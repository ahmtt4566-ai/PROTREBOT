"""Synthetic branch matrix for the observation-only, fixed Donchian preset.

BUY/SELL: first strict close crossing; WAIT: inside/equality/repeated crossing,
zero current/previous channel, warmup, missing/gapped/invalid/unsorted OHLCV.
Plan branches: invalid ATR/Stop/TP and target-helper errors; no admission.
Future-price changes, exact closing boundary, native helper reuse, immutable
inputs, no I/O/trades/account gates, and deterministic parameters are checked.
"""

from __future__ import annotations

import copy
import math
import socket
import sqlite3
import sys
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import analysis, main
from app.strategies import donchian_breakout as strategy
from app.strategies.contracts import StrategyInput
from app.strategies.donchian_indicator import (
    CandleDataError,
    closed_contract_candles,
    reference_channel,
)
from app.strategies.donchian_params import EXISTING_EXIT_POLICY_ID, DonchianParams

AT = 1585699200


def rows(count: int = 22, side: str = "LONG", scale: float = 1.0) -> list[dict[str, Any]]:
    result = [
        {"time": AT - (count - index) * 900, "open": 100.0 * scale,
         "high": 101.0 * scale, "low": 99.0 * scale, "close": 100.0 * scale, "volume": 100.0}
        for index in range(count)
    ]
    if result:
        close = (110.0 if side == "LONG" else 90.0) * scale
        result[-1].update(close=close, high=max(close, 101.0 * scale), low=min(close, 99.0 * scale))
    return result


def request(count: int = 22, side: str = "LONG", scale: float = 1.0) -> StrategyInput:
    return StrategyInput("BTCUSDT", AT, {"15m": rows(count, side, scale)}, required_intervals=("15m",))


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Donchian is observation-only: no I/O, admission or orders")

    for target in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, target, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(main, "risk_sized_order", forbidden)
    monkeypatch.setattr(main, "evaluate_entry_gates", forbidden)
    monkeypatch.setattr(main, "canonical_historical_decision", forbidden)
    monkeypatch.setattr(main, "fetch_candles", forbidden)
    monkeypatch.setattr("app.v25_execution.execute_live_order", forbidden)
    monkeypatch.setattr("app.binance_demo.execute_demo_order", forbidden)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
def test_first_cross_calls_existing_atr_and_targets_only_for_levels(side, symbol):
    value = replace(request(side=side), symbol=symbol)
    with (
        patch.object(strategy, "atr", wraps=analysis.atr) as atr_spy,
        patch.object(main, "v20_target_plan", wraps=main.v20_target_plan) as targets_spy,
    ):
        result = strategy.evaluate(value)
    signal = result.signal
    assert signal.decision == ("BUY" if side == "LONG" else "SELL")
    assert signal.strategy_eligible is True
    assert signal.exit_policy_id == EXISTING_EXIT_POLICY_ID and signal.invalidation is None
    assert result.legacy == {}
    assert "confidence" not in signal.diagnostics and "trap_score" not in signal.diagnostics
    assert all(check.key not in {"confidence", "trap"} for check in signal.quality_checks)
    assert atr_spy.call_args.kwargs == {"period": 14}
    assert len(atr_spy.call_args.args[0]) == 22
    assert targets_spy.call_args.args == (signal.entry, signal.stop, side)
    assert targets_spy.call_args.kwargs == {}
    distance = abs(signal.entry - signal.stop)
    sign = 1 if side == "LONG" else -1
    assert signal.stop == signal.entry - sign * 2 * signal.atr
    assert (signal.tp1, signal.tp2, signal.tp3) == tuple(
        round(signal.entry + sign * distance * multiple, 10) for multiple in (1, 2, 3)
    )
    assert "partial_plan" not in signal.diagnostics


def test_reference_window_excludes_both_decision_and_later_candles():
    raw = rows(25)
    closed = closed_contract_candles(raw, AT)
    channel = reference_channel(closed, 21)
    assert channel is not None
    assert channel.count == 20 and channel.upper == 101 and channel.lower == 99
    assert channel.start_open_time == closed[1].time and channel.end_open_time == closed[20].time
    changed = list(closed)
    changed[21] = replace(changed[21], high=10000, low=1)
    changed[24] = replace(changed[24], high=99999, low=0.1)
    assert reference_channel(changed, 21) == channel
    assert reference_channel(closed, 19) is None


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_future_changes_and_unclosed_invalid_ohlcv_cannot_change_result(side):
    value = request(side=side)
    expected = strategy.evaluate(value)
    future = {"time": AT, "open": 9000, "high": 1, "low": 20000, "close": math.nan, "volume": -1}
    value.candles_by_timeframe["15m"].append(future)
    value.candles_by_timeframe["15m"].append({"time": AT + 900})
    assert strategy.evaluate(value) == expected
    assert closed_contract_candles(value.candles_by_timeframe["15m"], AT)[-1].time + 900 == AT
    assert strategy.evaluate(replace(value, decision_time=AT - 1)).signal.reason == "INSUFFICIENT_CLOSED_CANDLES"


@pytest.mark.parametrize("count", [0, 14, 15, 20, 21, 22])
def test_channel_and_atr_warmup_boundaries(count):
    signal = strategy.evaluate(request(count)).signal
    warmup = signal.diagnostics["warmup"]
    assert warmup["channel_required"] == 22 and warmup["atr_required"] == 15
    assert warmup["channel_ready"] is (count >= 22)
    assert warmup["atr_ready"] is (count >= 15)
    assert signal.reason == ("READY" if count == 22 else "INSUFFICIENT_CLOSED_CANDLES")


@pytest.mark.parametrize("close", [99.0, 100.0, 101.0])
def test_inside_and_equal_prices_wait(close):
    value = request()
    value.candles_by_timeframe["15m"][-1].update(close=close, high=101, low=99)
    assert strategy.evaluate(value).signal.reason == "NO_BREAKOUT"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_repeated_outside_close_is_not_a_new_first_cross(side):
    value = request(side=side)
    previous = 105 if side == "LONG" else 95
    value.candles_by_timeframe["15m"][-2].update(
        close=previous, high=max(previous, 101), low=min(previous, 99),
    )
    signal = strategy.evaluate(value).signal
    assert signal.reason == "ALREADY_OUTSIDE_CHANNEL" and signal.strategy_eligible is False


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_previous_close_equal_to_previous_boundary_allows_first_cross(side):
    value = request(side=side)
    value.candles_by_timeframe["15m"][-2]["close"] = 101 if side == "LONG" else 99
    assert strategy.evaluate(value).signal.strategy_eligible is True


@pytest.mark.parametrize("previous_only", [False, True])
def test_zero_width_current_or_previous_reference_waits(previous_only):
    value = request()
    for row in value.candles_by_timeframe["15m"][:-1]:
        row.update(open=100, high=100, low=100, close=100)
    if previous_only:
        value.candles_by_timeframe["15m"][-2]["high"] = 101
    assert strategy.evaluate(value).signal.reason == "ZERO_WIDTH_CHANNEL"


@pytest.mark.parametrize(
    ("case", "reason"),
    [("container", "INVALID_CANDLE_DATA"), ("row", "INVALID_CANDLE_DATA"),
     ("missing_time", "INVALID_CANDLE_DATA"), ("missing_close", "INVALID_CANDLE_DATA"),
     ("boolean", "INVALID_CANDLE_DATA"), ("nonnumeric", "INVALID_CANDLE_DATA"),
     ("nan", "INVALID_CANDLE_DATA"), ("infinity", "INVALID_CANDLE_DATA"),
     ("negative_time", "INVALID_CANDLE_DATA"), ("fractional_time", "INVALID_CANDLE_DATA"),
     ("unaligned", "INVALID_CANDLE_DATA"), ("nonpositive_price", "INVALID_CANDLE_DATA"),
     ("negative_volume", "INVALID_CANDLE_DATA"), ("ohlcv", "INVALID_CANDLE_DATA"),
     ("duplicate", "NON_CHRONOLOGICAL_CANDLES"), ("reversed", "NON_CHRONOLOGICAL_CANDLES"),
     ("gap", "DATA_GAP")],
)
def test_invalid_data_branches(case, reason):
    value = request(24)
    data: dict[str, Any] = copy.deepcopy(value.candles_by_timeframe)
    if case == "container":
        data["15m"] = "invalid"
    elif case == "row":
        data["15m"][0] = None
    elif case == "missing_time":
        del data["15m"][0]["time"]
    elif case == "missing_close":
        del data["15m"][0]["close"]
    elif case in {"boolean", "nonnumeric", "nan", "infinity"}:
        data["15m"][0]["close"] = {"boolean": True, "nonnumeric": "bad", "nan": math.nan,
                                 "infinity": math.inf}[case]
    elif case == "negative_time":
        data["15m"][0]["time"] = -900
    elif case == "fractional_time":
        data["15m"][0]["time"] += 0.5
    elif case == "unaligned":
        data["15m"][0]["time"] += 1
    elif case == "nonpositive_price":
        data["15m"][0]["low"] = 0
    elif case == "negative_volume":
        data["15m"][0]["volume"] = -1
    elif case == "ohlcv":
        data["15m"][0]["high"] = 99
    elif case == "duplicate":
        data["15m"][1]["time"] = data["15m"][0]["time"]
    elif case == "reversed":
        data["15m"][0], data["15m"][1] = data["15m"][1], data["15m"][0]
    else:
        del data["15m"][1]
    signal = strategy.evaluate(replace(value, candles_by_timeframe=data)).signal
    assert signal.reason == reason and signal.strategy_eligible is False
    assert signal.entry is None and signal.stop is None and signal.tp1 is None
    assert signal.diagnostics["data_error"]


@pytest.mark.parametrize("decision_time", [-1, 1.5, True])
def test_invalid_decision_time(decision_time):
    assert strategy.evaluate(replace(request(), decision_time=decision_time)).signal.reason == "INVALID_DECISION_TIME"


def test_missing_frame_and_wrong_timeframe():
    assert strategy.evaluate(replace(request(), candles_by_timeframe={})).signal.reason == "INSUFFICIENT_CLOSED_CANDLES"
    assert strategy.evaluate(replace(request(), required_intervals=("1h",))).signal.reason == "UNSUPPORTED_TIMEFRAME"


@pytest.mark.parametrize("measured", [0, -1, math.nan, math.inf])
def test_invalid_atr_is_explicit_wait(measured):
    with patch.object(strategy, "atr", return_value=measured):
        signal = strategy.evaluate(request()).signal
    assert signal.reason == "INVALID_ATR" and signal.atr is None


def test_atr_overflow_is_explicit_wait():
    with patch.object(strategy, "atr", side_effect=OverflowError("synthetic overflow")):
        assert strategy.evaluate(request()).signal.reason == "INVALID_ATR"


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_very_wide_atr_stop_or_targets_are_rejected_without_fabrication(side):
    value = request(side=side)
    for row in value.candles_by_timeframe["15m"][:-1]:
        row.update(high=1000, low=10)
    if side == "LONG":
        value.candles_by_timeframe["15m"][-1].update(close=1100, high=1101)
    else:
        value.candles_by_timeframe["15m"][-1].update(close=5, low=4)
    signal = strategy.evaluate(value).signal
    assert signal.reason == ("INVALID_STOP_PLAN" if side == "LONG" else "INVALID_TARGET_PLAN")
    assert signal.strategy_eligible is False and signal.tp1 is None


def test_existing_target_rounding_collision_is_rejected():
    assert strategy.evaluate(request(scale=1e-14)).signal.reason == "INVALID_TARGET_PLAN"


@pytest.mark.parametrize(
    "targets",
    [{}, {"tp1": 111, "tp2": 110, "tp3": 109},
     {"tp1": 111, "tp2": 111, "tp3": 112},
     {"tp1": math.nan, "tp2": 112, "tp3": 113},
     {"tp1": 0, "tp2": 112, "tp3": 113}],
)
def test_invalid_target_helper_result_never_becomes_a_ready_signal(targets):
    with patch.object(main, "v20_target_plan", return_value=targets):
        assert strategy.evaluate(request()).signal.reason == "INVALID_TARGET_PLAN"


def test_target_helper_exception_is_reported():
    with patch.object(main, "v20_target_plan", side_effect=ValueError("synthetic invalid target")):
        assert strategy.evaluate(request()).signal.diagnostics["target_error"] == "synthetic invalid target"


def test_determinism_no_input_mutation_or_io_and_reported_health_cannot_override():
    value = replace(request(), data_health={"healthy": False}, warmup={"15m": 0},
                    latest_closed_timestamps={"15m": 1})
    before = copy.deepcopy(value)
    first = strategy.evaluate(value)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("No filesystem access permitted")

    with ExitStack() as stack:
        for target in ("builtins.open", "pathlib.Path.open", "pathlib.Path.write_text",
                       "pathlib.Path.write_bytes", "pathlib.Path.mkdir", "pathlib.Path.touch"):
            stack.enter_context(patch(target, side_effect=forbidden))
        second = strategy.evaluate(value)
    assert first == second and value == before
    assert first.signal.latest_closed_timestamps == {"15m": AT - 900}
    assert first.signal.strategy_eligible is True
    short = replace(request(21), data_health={"healthy": True}, warmup={"15m": 10000})
    assert strategy.evaluate(short).signal.strategy_eligible is False


def test_fixed_params_hash_and_no_tuning():
    params = DonchianParams()
    assert params.provenance == DonchianParams(atr_multiplier=2).provenance
    assert params.provenance.strategy_id == "donchian_breakout"
    with pytest.raises(FrozenInstanceError):
        params.channel_period = 21
    for changes in ({"channel_period": 21}, {"atr_period": 15}, {"atr_multiplier": 3},
                    {"strict": False}, {"first_cross_only": False}, {"breakout_mode": "high_low"},
                    {"tp_multiples": (1, 2, 4)}, {"exit_policy_id": "B"},
                    {"zero_width_policy": "ALLOW"}, {"timeframe": "1h"},
                    {"tp_multiples": (True, 2, 3)}, {"atr_multiplier": 2 + 0j}):
        with pytest.raises(ValueError, match="Only the approved"):
            replace(params, **changes)


def test_original_overrides_and_foreign_exit_policy_are_not_silently_accepted():
    with pytest.raises(ValueError, match="Original historical"):
        strategy.evaluate(replace(request(), historical_policy_override={"confidence_threshold": 0}))
    with pytest.raises(ValueError, match="existing exit"):
        strategy.evaluate(replace(request(), exit_policy_id="other"))
    assert strategy.evaluate(replace(request(), exit_policy_id=EXISTING_EXIT_POLICY_ID)).signal.strategy_eligible is True


@pytest.mark.parametrize("period", [0, -1, True, 2.5])
def test_indicator_rejects_invalid_period(period):
    closed = closed_contract_candles(rows(), AT)
    with pytest.raises(ValueError):
        reference_channel(closed, 21, period)


@pytest.mark.parametrize("index", [-1, 22, True, 1.5])
def test_indicator_rejects_invalid_decision_index(index):
    with pytest.raises(ValueError):
        reference_channel(closed_contract_candles(rows(), AT), index)


def test_numeric_overflow_is_a_structured_data_error():
    raw = rows()
    raw[0]["high"] = 10 ** 1000
    with pytest.raises(CandleDataError, match="invalid number"):
        closed_contract_candles(raw, AT)
