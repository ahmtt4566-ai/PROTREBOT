"""Synthetic native-window regression; no historical files or exchange access.

Changed expectation: an interior contract gap no longer poisons Donchian after
it leaves the native closed-history window. Gaps inside the window still fail.
ATR is deliberately window-local, not numerically equal to full-history ATR.
Alternate windows simulate native signature changes, never a tuning API.
"""

from __future__ import annotations

import copy
import sys
from contextlib import contextmanager
from dataclasses import replace
from inspect import signature
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import test_offline_strategy_facade as synthetic_tests
from app.analysis import atr
from app.backtest_baseline import Engine
from app.backtest_data import Series
from app.strategies import offline_facade as facade_module
from app.strategies.donchian_breakout import evaluate
from app.strategies.donchian_params import DonchianParams, DonchianParamsV2
from app.strategies.offline_facade import DonchianOfflineEngine, OfflineFacade
from app.strategies.provenance import parameter_hash
from test_offline_strategy_facade import (
    AT,
    SYMBOL,
    candle,
    config,
    dataset,
    exact,
)

NATIVE_BARS = signature(Series.closed).parameters["limit"].default
ETH = "ETHUSDT"
offline_only = synthetic_tests.offline_only


def history_dataset(*, count=320, missing=None, shock=False, symbols=(SYMBOL,)):
    data = dataset()
    rows = [
        candle(
            AT - (count - index) * 900,
            high=1000000.0 if shock and index < count - NATIVE_BARS else None,
            low=1.0 if shock and index < count - NATIVE_BARS else None,
        )
        for index in range(count)
    ]
    rows[-1].update(close=110.0, high=110.0)
    frames, marks, funding, months, exchange_symbols, brackets = {}, {}, {}, {}, [], {}
    for symbol in symbols:
        frames[symbol] = copy.deepcopy(data.frames[SYMBOL])
        omitted = set() if missing is None else set(missing.get(symbol, ()))
        selected = [copy.deepcopy(row) for index, row in enumerate(rows) if index not in omitted]
        selected.append(copy.deepcopy(data.frames[SYMBOL]["15m"].rows[-1]))
        frames[symbol]["15m"] = Series("15m", selected)
        marks[symbol] = copy.deepcopy(data.marks[SYMBOL])
        funding[symbol] = copy.deepcopy(data.funding[SYMBOL])
        months[symbol] = set(data.funding_months[SYMBOL])
        info = copy.deepcopy(data.metadata["exchange_info"]["symbols"][0])
        info["symbol"] = symbol
        exchange_symbols.append(info)
        brackets[symbol] = copy.deepcopy(data.metadata["brackets"][SYMBOL])
        brackets[symbol]["symbol"] = symbol
    data.frames, data.marks, data.funding, data.funding_months = frames, marks, funding, months
    data.metadata["exchange_info"]["symbols"] = exchange_symbols
    data.metadata["brackets"] = brackets
    return data


def donor(data, params=None):
    facade = OfflineFacade(
        data, config(), strategy_id="donchian_breakout",
        params=DonchianParamsV2() if params is None else params,
    )
    assert isinstance(facade.engine, DonchianOfflineEngine)
    return facade.engine


@contextmanager
def simulated_native_default(bars):
    native_signature = signature(Series.closed)
    parameters = [
        item.replace(default=bars) if name == "limit" else item
        for name, item in native_signature.parameters.items()
    ]
    with patch.object(facade_module, "signature", return_value=native_signature.replace(parameters=parameters)):
        yield


def test_contract_gap_older_than_native_window_recovers():
    data = history_dataset(missing={SYMBOL: (20,)})
    engine = donor(data)
    series = data.frames[SYMBOL]["15m"]
    assert series.history_complete(AT) is True
    old_request = replace(
        engine.request_at(SYMBOL, AT),
        candles_by_timeframe={"15m": series.closed(AT, limit=len(series.rows))},
    )
    assert evaluate(old_request, engine.params).signal.reason == "DATA_GAP"
    actual = engine.canonical(SYMBOL, AT)
    assert actual.signal.strategy_eligible is True
    assert actual.signal.decision == "BUY"
    assert len(engine.request_at(SYMBOL, AT).candles_by_timeframe["15m"]) == NATIVE_BARS


def test_contract_gap_inside_native_window_remains_data_gap(caplog):
    data = history_dataset(missing={SYMBOL: (140,)})
    engine = donor(data)
    assert data.frames[SYMBOL]["15m"].history_complete(AT) is False
    result = engine.canonical(SYMBOL, AT)
    assert result.signal.strategy_eligible is False
    assert result.signal.decision == "WAIT"
    assert result.signal.reason == "DATA_GAP"
    assert engine.cache_keys == ()
    assert "OFFLINE_DONCHIAN_INVALID_CACHE_DATA" in caplog.text


@pytest.mark.parametrize(("offset", "eligible"), [(-2, True), (-1, True), (0, False)])
def test_exact_window_gap_boundary_has_no_off_by_one(offset, eligible):
    count = 320
    missing_index = count - NATIVE_BARS + offset
    data = history_dataset(count=count, missing={SYMBOL: (missing_index,)})
    engine = donor(data)
    request = engine.request_at(SYMBOL, AT)
    rows = request.candles_by_timeframe["15m"]
    assert NATIVE_BARS == 259
    exact(rows, data.frames[SYMBOL]["15m"].closed(AT))
    assert len(rows) == NATIVE_BARS
    assert rows[-1]["time"] == AT - 900
    assert all(row["time"] + 900 <= AT for row in rows)
    result = engine.canonical(SYMBOL, AT)
    assert result.signal.strategy_eligible is eligible
    assert result.signal.decision == ("BUY" if eligible else "WAIT")
    if not eligible:
        assert result.signal.reason == "DATA_GAP"


@pytest.mark.parametrize("params", [DonchianParams(), DonchianParamsV2()])
def test_native_window_smaller_than_strategy_warmup_raises(params):
    minimum = max(params.channel_period + 2, params.atr_period + 1)
    assert minimum == 22
    with simulated_native_default(minimum - 1), pytest.raises(ValueError, match="warmup"):
        donor(history_dataset(), params)


def test_exact_strategy_warmup_window_is_valid():
    with simulated_native_default(22):
        engine = donor(history_dataset())
    assert engine.data_window.closed_bars == 22
    assert len(engine.request_at(SYMBOL, AT).candles_by_timeframe["15m"]) == 22
    assert engine.canonical(SYMBOL, AT).signal.strategy_eligible is True


@pytest.mark.parametrize("invalid", [True, 0, -1, 259.0, "259", float("nan")])
def test_invalid_native_limit_fails_explicitly(invalid):
    with simulated_native_default(invalid), pytest.raises(ValueError, match="Native closed-history"):
        donor(history_dataset())


def test_window_hash_separates_identical_short_inputs_and_cache_entries():
    data = history_dataset(count=30)
    first = donor(data)
    with simulated_native_default(NATIVE_BARS + 1):
        second = donor(data)
    first_request, second_request = first.request_at(SYMBOL, AT), second.request_at(SYMBOL, AT)
    exact(first_request, second_request)
    first_result, second_result = first.canonical(SYMBOL, AT), second.canonical(SYMBOL, AT)
    exact(first_result, second_result)
    first_key, second_key = first.cache_keys[0], second.cache_keys[0]
    assert first_key[:4] == second_key[:4]
    assert first_key[5:] == second_key[5:] == (SYMBOL, AT)
    assert first_key[4] != second_key[4]
    assert set(first.cache_keys).isdisjoint(second.cache_keys)
    first_record, second_record = first.data_window.as_dict(), second.data_window.as_dict()
    assert first_record["parameters"]["closed_bars"] == NATIVE_BARS
    assert second_record["parameters"]["closed_bars"] == NATIVE_BARS + 1
    assert first_record["parameter_hash"] != second_record["parameter_hash"]
    for record in (first_record, second_record):
        assert record["parameter_hash"] == parameter_hash(record["parameters"])
    second._decisions[first_key] = first_result
    with patch.object(facade_module, "evaluate", wraps=evaluate) as evaluator:
        second._decisions.pop(second_key)
        assert second.canonical(SYMBOL, AT) == second_result
        assert evaluator.call_count == 1


def test_atr_is_window_local_and_differs_from_full_history_seed():
    data = history_dataset(count=600, shock=True)
    engine = donor(data)
    series = data.frames[SYMBOL]["15m"]
    request = engine.request_at(SYMBOL, AT)
    window = request.candles_by_timeframe["15m"]
    full = series.closed(AT, limit=len(series.rows))
    expected_window = atr(
        [row["high"] for row in window], [row["low"] for row in window],
        [row["close"] for row in window], engine.params.atr_period,
    )
    expected_full = atr(
        [row["high"] for row in full], [row["low"] for row in full],
        [row["close"] for row in full], engine.params.atr_period,
    )
    result = engine.canonical(SYMBOL, AT)
    assert len(window) == NATIVE_BARS and len(full) == 600
    assert result.signal.strategy_eligible is True
    assert result.signal.atr == expected_window == (2.0 * 13 + 11.0) / 14
    assert expected_full > expected_window
    full_result = evaluate(replace(request, candles_by_timeframe={"15m": full}), engine.params)
    assert full_result.signal.strategy_eligible is True
    assert full_result.signal.atr == expected_full
    assert full_result.signal.stop != result.signal.stop
    assert full_result.signal.tp1 != result.signal.tp1
    cached_key = engine.cache_keys[0]
    series.rows[0].update(high=2000000.0)
    assert engine.canonical(SYMBOL, AT) == result
    assert engine.cache_keys == (cached_key,)
    window[0]["high"] = 4000000.0
    assert engine.request_at(SYMBOL, AT).candles_by_timeframe["15m"][0]["high"] == 101.0


def test_one_symbol_gap_does_not_poison_other_symbol_replay(offline_only):
    data = history_dataset(symbols=(SYMBOL, ETH), missing={SYMBOL: (140,)})
    engine = donor(data)
    assert engine.canonical(SYMBOL, AT).signal.reason == "DATA_GAP"
    assert engine.canonical(ETH, AT).signal.strategy_eligible is True
    output = offline_only.run_until_complete(engine.replay())
    assert [trade["symbol"] for trade in output["trades"]] == [ETH]
    assert output["all_rejections"] == {"data_gap_history": 1}
    assert {key[5] for key in engine.cache_keys} == {ETH}
    record = engine.data_window.as_dict()
    assert output["data_window"] == record
    assert output["trades"][0]["data_window"] == record
    assert len(output["admissions"]) == 3
    assert all(row["data_window"] == record for row in output["admissions"])
    assert len(output["decisions"]) == 1
    assert output["decisions"][0]["data_window"] == record


@pytest.mark.parametrize("params", [DonchianParams(), DonchianParamsV2()])
def test_window_provenance_does_not_change_signal_preset_hashes(params, offline_only):
    before = copy.deepcopy(params.parameters())
    before_provenance = params.provenance
    engine = donor(history_dataset(), params)
    output = offline_only.run_until_complete(engine.replay())
    assert engine.params.parameters() == before
    assert engine.params.provenance == before_provenance
    assert output["strategy"] == {
        "strategy_id": before_provenance.strategy_id,
        "strategy_version": before_provenance.strategy_version,
        "parameter_hash": before_provenance.parameter_hash,
    }
    assert engine.canonical(SYMBOL, AT).signal.provenance == before_provenance
    assert output["data_window"]["parameter_hash"] != before_provenance.parameter_hash
    assert "closed_bars" not in engine.params.parameters()


def test_original_remains_literal_even_with_gap_and_invalid_simulated_window(offline_only):
    data = history_dataset(missing={SYMBOL: (140,)})
    reference_data = copy.deepcopy(data)
    native = Engine(reference_data, config())
    with simulated_native_default(1):
        facade = OfflineFacade(data, config())
        assert type(facade.engine) is Engine
        expected = reference_data.canonical(SYMBOL, AT, native.policy)
        actual = facade.canonical(SYMBOL, AT)
        exact(actual, expected)
        assert actual is data.canonical(SYMBOL, AT, facade.engine.policy)
        first = offline_only.run_until_complete(native.replay())
        second = offline_only.run_until_complete(facade.replay())
    exact(first, second)
    assert "data_window" not in second
