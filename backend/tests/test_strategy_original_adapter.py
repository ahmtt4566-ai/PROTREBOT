"""Synthetic-only parity and branch matrix; no research datasets or replay.

Native branches:
  rising: BUY; falling_single: SELL; flat: invalid entry direction/WAIT.
  confidence/breakout/trap: individual quality failure; multiple: combined.
  mismatch/either: strict MTF rejection / either-frame approval.
  falling: SHORT alignment >=80 rejection.
  short_below_80: approved SHORT MTF (explicit synthetic analysis fixture).
  invalid_rows/ohlcv/unsupported/order: native early validation returns.
  primary/higher warmup: insufficient closed-candle returns.
  boundary/future: closed-only selection, including malformed future rejection.
No account/risk branches are reachable: both native arguments must be None.
"""

from __future__ import annotations

import copy
import math
import socket
import sqlite3
import sys
from contextlib import ExitStack
from dataclasses import fields, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import analysis, main
from app.strategies.compatibility import legacy_output
from app.strategies.contracts import StrategyInput, StrategySignal
from app.strategies.kais_original import evaluate
from app.strategies.provenance import parameter_hash

AT = 1585699200  # 2020-04-01 UTC; all candles are generated, not market history.
INTERVALS = ("15m", "1h", "4h")
DURATIONS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")


def candles(interval: str, trend: str = "rising", count: int = 260) -> list[dict[str, Any]]:
    rows = []
    for index in range(count):
        close = 100 + index * 0.2 if trend == "rising" else (
            200 - index * 0.2 if trend == "falling" else 100.0
        )
        opening = close - 2 if trend == "rising" else close + 2 if trend == "falling" else close
        rows.append({
            "time": AT - (count - index) * DURATIONS[interval],
            "open": opening, "high": max(opening, close) + 0.1,
            "low": min(opening, close) - 0.1, "close": close,
            "volume": 140.0 if index == count - 1 else 100.0,
        })
    return rows


def request(trend: str = "rising", symbol: str = "BTCUSDT") -> StrategyInput:
    return StrategyInput(
        symbol=symbol, decision_time=AT,
        candles_by_timeframe={interval: candles(interval, trend) for interval in INTERVALS},
        required_intervals=INTERVALS,
        historical_policy_override={"confidence_threshold": 80},
    )


def native(request: StrategyInput) -> dict[str, Any]:
    return main.canonical_historical_decision(
        request.symbol, copy.deepcopy(request.candles_by_timeframe), request.decision_time,
        required_intervals=request.required_intervals, policy=None, account_context=None,
        historical_policy_override=copy.deepcopy(request.historical_policy_override),
    )


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Adapter tests must not perform I/O, sizing or account gates")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(main, "risk_sized_order", forbidden)
    monkeypatch.setattr(main, "evaluate_entry_gates", forbidden)
    monkeypatch.setattr(main, "fetch_candles", forbidden)
    monkeypatch.setattr("app.v25_execution.execute_live_order", forbidden)
    monkeypatch.setattr("app.binance_demo.execute_demo_order", forbidden)


def assert_exact(actual: Any, expected: Any):
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert list(actual) == list(expected)
        for key in expected:
            assert_exact(actual[key], expected[key])
    elif isinstance(expected, (list, tuple)):
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            assert_exact(left, right)
    elif isinstance(expected, float):
        assert actual.hex() == expected.hex()
    else:
        assert actual == expected


def assert_parity(value: StrategyInput):
    expected = native(value)
    actual = evaluate(value)
    assert legacy_output(actual) == expected
    assert_exact(legacy_output(actual), expected)
    assert list(actual.legacy) == list(expected)
    assert actual.signal.strategy_eligible == expected["entry_eligible"]
    assert actual.signal.decision == expected["decision"]
    assert actual.signal.reason == expected["reason"]
    assert actual.signal.reasons == tuple(expected["reasons"])
    return actual


@pytest.mark.parametrize("symbol", SYMBOLS)
@pytest.mark.parametrize(
    ("trend", "single", "decision"),
    [("rising", False, "BUY"), ("flat", False, "WAIT"),
     ("falling", False, "WAIT"), ("falling", True, "SELL")],
)
def test_native_analysis_full_parity(symbol, trend, single, decision):
    value = request(trend, symbol)
    if single:
        value = replace(value, required_intervals=("15m",))
    result = assert_parity(value)
    assert result.signal.decision == decision
    assert result.signal.invalidation is None
    assert result.signal.provenance.strategy_id == "kais_original"
    assert result.legacy["risk"] is None and result.legacy["gate"] is None
    if trend == "falling" and not single:
        assert result.legacy["mtf"]["blocked_by_short_filter"] is True
    if single:
        assert result.legacy["mtf"]["verdict"] == "SINGLE_TIMEFRAME"


@pytest.mark.parametrize("gate", ["confidence", "breakout", "trap", "multiple"])
def test_quality_rejections_preserve_reason_precedence(gate):
    value = request()
    override = {"confidence_threshold": 96 if gate in {"confidence", "multiple"} else 80,
                "breakout_quality_threshold": 96 if gate in {"breakout", "multiple"} else 50}
    if gate in {"trap", "multiple"}:
        for row in value.candles_by_timeframe["15m"][-8:]:
            row["high"] = row["close"] + 10
    value = replace(value, historical_policy_override=override)
    result = assert_parity(value)
    assert result.signal.decision == "WAIT"
    assert result.signal.reason == "QUALITY_OR_MTF_GATE"
    failed = {check.key for check in result.signal.quality_checks if not check.passed}
    assert ({"confidence", "trap", "breakout"} if gate == "multiple" else {gate}) <= failed


@pytest.mark.parametrize("either", [False, True])
def test_higher_timeframe_mismatch_and_either_override(either):
    value = request()
    value.candles_by_timeframe["4h"] = candles("4h", "falling")
    value = replace(value, historical_policy_override={
        "confidence_threshold": 80, "mtf_allow_either_timeframe": either,
    })
    result = assert_parity(value)
    assert result.signal.decision == ("BUY" if either else "WAIT")
    assert result.legacy["mtf"]["higher_timeframe_confirmation"] is either


def test_short_mtf_below_80_branch_with_explicit_synthetic_analysis():
    value = replace(request("falling"), historical_policy_override={"confidence_threshold": 70})
    original = analysis.analyze

    def synthetic_analysis(rows):
        result = original(rows)
        result["confidence"] = 75
        return result

    with patch.object(main, "analyze", side_effect=synthetic_analysis):
        result = assert_parity(value)
    assert result.signal.decision == "SELL"
    assert result.legacy["mtf"]["alignment"] == 75
    assert result.legacy["mtf"]["blocked_by_short_filter"] is False


@pytest.mark.parametrize(
    ("case", "reason"),
    [("container", "INVALID_CANDLE_DATA"), ("missing", "INVALID_CANDLE_DATA"),
     ("ohlcv", "INVALID_CANDLE_DATA"), ("volume", "INVALID_CANDLE_DATA"),
     ("duplicate", "NON_CHRONOLOGICAL_CANDLES"), ("reversed", "NON_CHRONOLOGICAL_CANDLES"),
     ("unsupported", "UNSUPPORTED_TIMEFRAME"), ("primary_warmup", "INSUFFICIENT_CLOSED_CANDLES"),
     ("higher_warmup", "INSUFFICIENT_CLOSED_CANDLES"),
     ("empty", "INSUFFICIENT_CLOSED_CANDLES"), ("timestamp", "INVALID_CANDLE_DATA")],
)
def test_early_validation_branches_preserve_sparse_output(case, reason):
    value = request()
    frames: dict[str, Any] = copy.deepcopy(value.candles_by_timeframe)
    required = value.required_intervals
    if case == "container":
        frames["15m"] = "not a candle list"
    elif case == "missing":
        del frames["15m"][0]["close"]
    elif case == "ohlcv":
        frames["15m"][0]["high"] = frames["15m"][0]["close"] - 1
    elif case == "volume":
        frames["15m"][0]["volume"] = -1
    elif case == "duplicate":
        frames["15m"][1]["time"] = frames["15m"][0]["time"]
    elif case == "reversed":
        frames["15m"][0], frames["15m"][1] = frames["15m"][1], frames["15m"][0]
    elif case == "unsupported":
        frames["2m"] = frames["15m"]
        required = ("2m",)
    elif case == "primary_warmup":
        frames["15m"] = frames["15m"][-219:]
    elif case == "higher_warmup":
        frames["1h"] = frames["1h"][-49:]
    elif case == "empty":
        frames["15m"] = []
    else:
        frames["15m"][0]["time"] = "invalid timestamp"
    result = assert_parity(replace(value, candles_by_timeframe=frames, required_intervals=required))
    assert result.signal.reason == reason
    assert result.signal.strategy_eligible is False
    assert result.signal.entry is None and result.signal.atr is None
    assert result.signal.quality_checks == ()
    assert "analysis" not in result.legacy and "risk" not in result.legacy


@pytest.mark.parametrize("interval", INTERVALS)
def test_exact_warmup_boundary(interval):
    value = request()
    minimum = 220 if interval == "15m" else 50
    value.candles_by_timeframe[interval] = value.candles_by_timeframe[interval][-minimum:]
    result = assert_parity(value)
    assert result.signal.reason != "INSUFFICIENT_CLOSED_CANDLES"


def test_default_required_intervals_including_daily_are_preserved():
    value = request()
    value.candles_by_timeframe["1d"] = candles("1d", count=50)
    value = replace(value, required_intervals=StrategyInput.__dataclass_fields__["required_intervals"].default)
    result = assert_parity(value)
    assert set(result.signal.latest_closed_timestamps) == {"15m", "1h", "4h", "1d"}


def test_closed_boundary_and_future_candles_do_not_change_output():
    value = request()
    baseline = assert_parity(value)
    with patch.object(main, "analyze", wraps=analysis.analyze) as spy:
        for interval in INTERVALS:
            future = dict(value.candles_by_timeframe[interval][-1])
            future.update(time=AT, open=9000, high=10000, low=8000, close=9500)
            value.candles_by_timeframe[interval].append(future)
        actual = assert_parity(value)
    assert actual == baseline
    for call in spy.call_args_list:
        rows = call.args[0]
        duration = rows[-1]["time"] - rows[-2]["time"]
        assert rows[-1]["time"] + duration == AT
        assert all(row["time"] + duration <= AT for row in rows)


def test_malformed_future_row_is_not_prefiltered_by_adapter():
    value = request()
    value.candles_by_timeframe["15m"].append({
        "time": AT, "open": 100, "high": 99, "low": 101, "close": 100, "volume": 100,
    })
    assert assert_parity(value).signal.reason == "INVALID_CANDLE_DATA"


def test_no_input_mutation_no_io_and_explicit_none_account_arguments():
    value = replace(request(), data_health={"healthy": False}, warmup={"15m": 0},
                    latest_closed_timestamps={"15m": 1}, exit_policy_id="caller-exit")
    before = copy.deepcopy(value)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Adapter must not access files")

    with ExitStack() as stack:
        for target in ("builtins.open", "pathlib.Path.open", "pathlib.Path.write_text",
                       "pathlib.Path.write_bytes", "pathlib.Path.mkdir", "pathlib.Path.touch"):
            stack.enter_context(patch(target, side_effect=forbidden))
        spy = stack.enter_context(patch.object(
            main, "canonical_historical_decision", wraps=main.canonical_historical_decision,
        ))
        result = evaluate(value)
    assert value == before
    assert spy.call_count == 1
    assert spy.call_args.kwargs["policy"] is None
    assert spy.call_args.kwargs["account_context"] is None
    assert spy.call_args.args[1] is not value.candles_by_timeframe
    assert result.signal.strategy_eligible is True
    assert result.signal.latest_closed_timestamps["15m"] == AT - 900
    assert result.signal.exit_policy_id == "caller-exit"


def test_input_and_legacy_and_projection_are_independent_snapshots():
    value = request()
    result = evaluate(value)
    expected = legacy_output(result)
    value.candles_by_timeframe["15m"][-1]["close"] = 9999
    result.signal.diagnostics["analysis"]["radar"]["trap_score"] = -1
    assert result.signal.overlay is not None
    result.signal.overlay["ema20"][0]["value"] = -1
    exported = legacy_output(result)
    exported["analysis"]["series"]["ema20"][0]["value"] = -2
    assert legacy_output(result) == expected


def test_repeatability_parameter_hash_order_and_effective_defaults():
    value = request()
    assert evaluate(value) == evaluate(copy.deepcopy(value))
    assert parameter_hash({"a": 1, "b": 2}) == parameter_hash({"b": 2, "a": 1})
    implicit = evaluate(replace(value, historical_policy_override=None))
    explicit = evaluate(replace(value, historical_policy_override=dict(main.HISTORICAL_POLICY_DEFAULT)))
    assert implicit.signal.provenance == explicit.signal.provenance
    changed = evaluate(replace(value, historical_policy_override={"confidence_threshold": 96}))
    assert changed.signal.provenance.parameter_hash != implicit.signal.provenance.parameter_hash
    single = evaluate(replace(value, required_intervals=("15m",)))
    assert single.signal.provenance.parameter_hash != evaluate(value).signal.provenance.parameter_hash


@pytest.mark.parametrize("override", [{"unknown": 1}, {"confidence_threshold": "bad"}, []])
def test_invalid_override_errors_are_not_suppressed(override):
    value = replace(request(), historical_policy_override=override)
    with pytest.raises((TypeError, ValueError)) as old:
        native(value)
    with pytest.raises(type(old.value)) as new:
        evaluate(value)
    assert str(new.value) == str(old.value)


def test_hash_rejects_nonfinite_parameters():
    with pytest.raises(ValueError):
        parameter_hash({"value": math.nan})


def test_contract_has_no_execution_authority_or_normalized_entry_eligible():
    input_fields = {field.name for field in fields(StrategyInput)}
    output_fields = {field.name for field in fields(StrategySignal)}
    assert not {"client", "credentials", "quantity", "leverage", "margin", "policy", "account_context"} & input_fields
    assert "strategy_eligible" in output_fields and "entry_eligible" not in output_fields
    assert "NOT order permission" in StrategySignal.__doc__
    assert evaluate(request()).signal.exit_policy_id is None


def test_reported_health_cannot_bypass_native_warmup():
    value = request()
    value.candles_by_timeframe["15m"] = value.candles_by_timeframe["15m"][-219:]
    value = replace(value, data_health={"healthy": True}, warmup={"15m": 100000},
                    latest_closed_timestamps={"15m": AT})
    result = assert_parity(value)
    assert result.signal.strategy_eligible is False
    assert result.signal.reason == "INSUFFICIENT_CLOSED_CANDLES"


def test_unknown_native_decision_is_an_explicit_contract_error():
    value = request()
    unexpected = native(value)
    unexpected["decision"] = "HOLD"
    with (
        patch.object(main, "canonical_historical_decision", return_value=unexpected),
        pytest.raises(ValueError, match="Unsupported native strategy decision"),
    ):
        evaluate(value)
