"""Synthetic finite-input validation and exact pre-hygiene Original parity."""

from __future__ import annotations

import ast
import copy
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import test_strategy_original_adapter as fixtures
from app import main

offline_only = fixtures.offline_only


@pytest.mark.parametrize("interval", ["15m", "1h", "4h"])
@pytest.mark.parametrize("field", ["time", "open", "high", "low", "close", "volume"])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_ohlcv_returns_controlled_wait(interval, field, invalid):
    request = fixtures.request()
    request.candles_by_timeframe[interval][-1][field] = invalid
    result = fixtures.native(request)
    assert result == {
        "decision": "WAIT", "symbol": request.symbol, "signal_timestamp": None,
        "entry_eligible": False, "reason": "INVALID_CANDLE_DATA", "reasons": [f"{interval}: invalid OHLCV"],
    }


@pytest.mark.parametrize("invalid", [10**1000, -10**1000, "Infinity", "-Infinity", "NaN"])
def test_timestamp_overflow_and_nonfinite_text_are_controlled(invalid):
    request = fixtures.request()
    request.candles_by_timeframe["15m"][-1]["time"] = invalid
    result = fixtures.native(request)
    assert result["decision"] == "WAIT" and result["entry_eligible"] is False
    assert result["reason"] == "INVALID_CANDLE_DATA"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), 10**1000, None])
def test_invalid_decision_clock_returns_controlled_wait(invalid):
    request = fixtures.request()
    result = main.canonical_historical_decision(
        request.symbol, request.candles_by_timeframe, invalid, required_intervals=request.required_intervals)
    assert result["decision"] == "WAIT" and result["entry_eligible"] is False
    assert result["reason"] == "INVALID_CANDLE_DATA"


@pytest.fixture
def pre_hygiene_canonical():
    source = subprocess.check_output(
        ["git", "show", "pre-hygiene-fixes:backend/app/main.py"],
        cwd=Path(__file__).parents[2], text=True, encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "canonical_historical_decision")
    namespace = dict(vars(main))
    # Only the local, fixed pre-hygiene tag supplies this parity reference.
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<pre-hygiene-canonical>", "exec"), namespace)  # noqa: S102
    return namespace["canonical_historical_decision"]


@pytest.mark.parametrize("trend", ["rising", "falling", "flat"])
@pytest.mark.parametrize("either", [False, True])
def test_valid_original_output_is_exactly_pre_hygiene(trend, either, pre_hygiene_canonical):
    request = fixtures.request(trend)
    kwargs = {"required_intervals": request.required_intervals, "policy": None, "account_context": None,
              "historical_policy_override": {"confidence_threshold": 80, "mtf_allow_either_timeframe": int(either)}}
    before = pre_hygiene_canonical(request.symbol, copy.deepcopy(request.candles_by_timeframe), request.decision_time, **kwargs)
    after = main.canonical_historical_decision(
        request.symbol, copy.deepcopy(request.candles_by_timeframe), request.decision_time, **kwargs)
    assert after == before
    assert after.get("analysis") == before.get("analysis")
