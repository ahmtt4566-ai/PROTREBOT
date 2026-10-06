"""Synthetic branch matrix; no market history, persisted cache or global core patches.

Native Original: BUY/SELL/WAIT, STOP_FIRST/TP_FIRST, nonempty trades, ordered
gates, slipped fills, nonzero funding, exact net R and complete output parity.
Donchian: eight quality failures, all 18 mandatory native gate failures,
invalid gate schemas, raw legacy failures retained, before/after-spec notional,
spec/cost/minimum/Stop/margin/isolated/liquidation/balance failures, separate
version/data caches, future invariance, immutable projections and no real I/O.
"""

from __future__ import annotations

import asyncio
import copy
import math
import socket
import sqlite3
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import execution_core as core
from app import main
from app import v25_execution as live
from app.backtest_baseline import Config, Engine
from app.backtest_data import Dataset, Series
from app.strategies.donchian_params import DonchianParams, DonchianParamsV2
from app.strategies.offline_admission import (
    CORE_GATE_KEYS,
    LEGACY_QUALITY_KEYS,
    STRATEGY_QUALITY_KEYS,
    admit,
)
from app.strategies.offline_facade import (
    CACHE_NAMESPACE,
    DonchianOfflineEngine,
    OfflineFacade,
)

AT = 1585699200
SYMBOL = "BTCUSDT"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    # Initialize Windows' internal event-loop socket pair before blocking I/O.
    loop = asyncio.new_event_loop()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Synthetic offline tests forbid network, database and real orders")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(main, "fetch_candles", forbidden)
    monkeypatch.setattr(live, "execute_live_order", forbidden)
    monkeypatch.setattr(live.BinanceLiveClient, "public_get", forbidden)
    monkeypatch.setattr(live.BinanceLiveClient, "signed", forbidden)
    monkeypatch.setattr("app.binance_demo.execute_demo_order", forbidden)
    bindings = (core.evaluate_entry_gates, main.canonical_historical_decision, Dataset.canonical, Engine.replay)
    yield loop
    assert bindings == (core.evaluate_entry_gates, main.canonical_historical_decision, Dataset.canonical, Engine.replay)
    loop.close()


def candle(at, close=100.0, *, opening=None, high=None, low=None, volume=100.0):
    opening = close if opening is None else opening
    return {"time": at, "open": opening, "high": max(opening, close) + 1 if high is None else high,
            "low": min(opening, close) - 1 if low is None else low, "close": close,
            "volume": volume, "quote_volume": 20000000}


def metadata():
    return {
        "historical": False, "observed_at": "SYNTHETIC_NOT_MARKET_METADATA",
        "exchange_info": {"symbols": [{
            "symbol": SYMBOL, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "100000"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }]},
        "brackets": {SYMBOL: {"symbol": SYMBOL, "brackets": [{
            "notionalFloor": 0, "notionalCap": 1000000,
            "maintMarginRatio": 0.004, "cum": 0, "initialLeverage": 125,
        }]}},
    }


def dataset(mode="donchian", side="LONG"):
    frames = {}
    for interval, duration in (("15m", 900), ("1h", 3600), ("4h", 14400)):
        rows = []
        for index in range(260):
            close = 100.0
            opening, high, low = close, close + 1, close - 1
            # Flat 4h lowers native weighted confidence; 1h confirms the SHORT.
            if mode == "original" and not (side == "SHORT" and interval == "4h"):
                sign = 1 if side == "LONG" else -1
                close = 100 + sign * index * 0.002
                opening = close - sign * 2
                high, low = max(opening, close) + 0.1, min(opening, close) - 0.1
            rows.append(candle(AT - (260 - index) * duration, close, opening=opening,
                               high=high, low=low, volume=140 if index == 259 else 100))
        if mode == "donchian" and interval == "15m":
            close = 110.0 if side == "LONG" else 90.0
            rows[-1].update(close=close, high=max(close, 101), low=min(close, 99))
        entry = rows[-1]["close"]
        rows.append(candle(AT, entry, high=2000.0, low=0.1))
        frames[interval] = Series(interval, rows)
    entry = frames["15m"].rows[-2]["close"]
    mark = Series("15m", [candle(AT, entry, high=2000.0, low=0.1)])
    funding = [
        {"time": AT + 450 - 28800, "rate": 0.0001, "interval_hours": 8},
        {"time": AT + 450, "rate": 0.0001, "interval_hours": 8},
        {"time": AT + 450 + 28800, "rate": 0.0001, "interval_hours": 8},
    ]
    return Dataset({SYMBOL: frames}, {SYMBOL: mark}, {SYMBOL: funding}, metadata(),
                   {}, {SYMBOL: {"2020-03", "2020-04"}})


def config(**overrides):
    settings = {"max_leverage": 10, "mtf_allow_either_timeframe": True}
    settings.update(overrides.pop("policy", {}))
    return Config(AT, AT + 900, bootstrap_samples=8, conditional_current_metadata=True,
                  policy=settings, **overrides)


def donchian(data=None, cfg=None, params=None):
    facade = OfflineFacade(
        dataset() if data is None else data, config() if cfg is None else cfg,
        strategy_id="donchian_breakout", params=DonchianParamsV2() if params is None else params,
    )
    assert isinstance(facade.engine, DonchianOfflineEngine)
    return facade.engine


def exact(actual, expected):
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert list(actual) == list(expected)
        for key in expected:
            exact(actual[key], expected[key])
    elif isinstance(expected, (list, tuple)):
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            exact(left, right)
    elif isinstance(expected, float):
        assert actual.hex() == expected.hex()
    else:
        assert actual == expected


def record_native_gates(engine, trace):
    native = engine.gate_failures

    def recording(symbol, signal, at, notional=0):
        raw = core.evaluate_entry_gates(
            symbol=symbol, signal=signal, snapshot=engine.snapshot(at), policy=engine.policy,
            daily=core.daily_execution_metrics(engine.events, datetime.fromtimestamp(at, timezone.utc)),
            spread_bps=engine.config.spread_bps, armed=True, allowed_symbols=engine.policy["allowed_symbols"],
            candidate_notional_usdt=notional,
            active_plans=[{**position.spec, "status": "ACTIVE"} for position in engine.positions.values()],
        )
        trace.append(copy.deepcopy(raw))
        return native(symbol, signal, at, notional)

    return patch.object(engine, "gate_failures", side_effect=recording)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("ordering", ["STOP_FIRST", "TP_FIRST"])
def test_original_native_and_facade_full_nonempty_parity(side, ordering, offline_only):
    first, second = dataset("original", side), dataset("original", side)
    cfg = config(intrabar=ordering)
    native = Engine(first, cfg)
    facade = OfflineFacade(second, cfg)
    assert type(facade.engine) is Engine
    expected_decision = first.canonical(SYMBOL, AT, native.policy)
    actual_decision = facade.canonical(SYMBOL, AT)
    exact(actual_decision, expected_decision)
    assert expected_decision["decision"] == ("BUY" if side == "LONG" else "SELL")
    native_trace, facade_trace = [], []
    with record_native_gates(native, native_trace), record_native_gates(facade.engine, facade_trace):
        expected = offline_only.run_until_complete(native.replay())
        actual = offline_only.run_until_complete(facade.replay())
    exact(actual, expected)
    exact(facade_trace, native_trace)
    assert len(native_trace) == 3 and len(expected["trades"]) == 1
    assert all(tuple(gate["key"] for gate in trace["gates"]) == CORE_GATE_KEYS for trace in native_trace)
    trade = expected["trades"][0]
    assert trade["status"] == "CLOSED" and trade["funding_usdt"] != 0
    assert trade["actual_slippage_price"] != 0 and trade["net_r"] is not None
    assert trade["exits"] and trade["funding_complete"]


def test_original_wait_and_cache_are_literal_without_normalization(offline_only):
    first, second = dataset("flat"), dataset("flat")
    native, facade = Engine(first, config()), OfflineFacade(second, config())
    actual = facade.canonical(SYMBOL, AT)
    exact(actual, first.canonical(SYMBOL, AT, native.policy))
    assert actual["decision"] == "WAIT"
    assert actual is second.canonical(SYMBOL, AT, facade.engine.policy)
    exact(offline_only.run_until_complete(facade.replay()), offline_only.run_until_complete(native.replay()))


def valid_admission_inputs(side="LONG"):
    engine = donchian(dataset(side=side))
    request, result = engine.request_at(SYMBOL, AT), engine.canonical(SYMBOL, AT)
    assert result.signal.strategy_eligible
    context = {
        "symbol": SYMBOL, "signal": {"direction": side},
        "snapshot": {"positions": [], "open_orders": [], "hedge_mode": False, "unrealized_pnl": 0},
        "policy": engine.state.policy, "daily": {}, "spread_bps": 2, "armed": True,
        "allowed_symbols": [SYMBOL], "candidate_notional_usdt": 0.0, "active_plans": [],
    }
    return engine, request, result, context


def test_only_legacy_quality_is_inapplicable_and_raw_verdict_is_unchanged():
    engine, request, result, context = valid_admission_inputs()
    raw = core.evaluate_entry_gates(**context)
    before = copy.deepcopy(raw)
    report = admit(request, result, engine.params, raw_entry_gates=raw, candidate_notional_usdt=0)
    assert report.offline_eligible and all(check.passed for check in report.strategy_quality)
    assert raw == before == report.raw_entry_gates
    assert report.raw_entry_gates["passed"] is False and report.raw_entry_gates["decision"] == "BEKLE"
    assert tuple(item["key"] for item in report.legacy_quality) == LEGACY_QUALITY_KEYS
    assert all(item["raw_gate"]["passed"] is False for item in report.legacy_quality)
    assert all(item["applicability"] == "NOT_APPLICABLE_LEGACY_QUALITY" for item in report.legacy_quality)
    report.raw_entry_gates["gates"][0]["passed"] = False
    assert raw == before


MANDATORY_KEYS = tuple(key for key in CORE_GATE_KEYS if key not in LEGACY_QUALITY_KEYS)


@pytest.mark.parametrize("key", MANDATORY_KEYS)
def test_each_actual_native_safety_gate_rejects_with_passing_donchian_quality(key):
    side = "SHORT" if key == "short" else "LONG"
    engine, request, result, context = valid_admission_inputs(side)
    if key == "arm":
        context["armed"] = False
    elif key == "symbol":
        context["allowed_symbols"] = ["ETHUSDT"]
    elif key == "direction":
        context["signal"] = {"direction": "BEKLE"}
    elif key in {"long", "short"}:
        context["policy"] = {**context["policy"], f"allow_{key}": False}
    elif key == "spread":
        context["spread_bps"] = 8.01
    elif key == "one_way":
        context["snapshot"]["hedge_mode"] = True
    elif key in {"positions", "duplicate", "same_direction_positions"}:
        context["snapshot"]["positions"] = [{
            "symbol": SYMBOL if key == "duplicate" else "ETHUSDT",
            "direction": "LONG" if key == "same_direction_positions" else "SHORT",
            "quantity": 1, "mark_price": 100, "entry_price": 100,
        }]
        if key == "positions":
            context["policy"] = {**context["policy"], "max_positions": 1}
        if key == "same_direction_positions":
            context["policy"] = {**context["policy"], "max_same_direction_positions": 1}
    elif key == "active_plan":
        context["active_plans"] = [{"symbol": SYMBOL, "direction": side, "status": "ACTIVE"}]
    elif key == "exposure":
        context["candidate_notional_usdt"] = 350.01
    elif key == "direction_exposure":
        context["policy"] = {**context["policy"], "max_direction_exposure_usdt": 50}
        context["candidate_notional_usdt"] = 50.01
    elif key == "daily_trades":
        context["daily"]["entries"] = 3
    elif key == "daily_loss":
        context["daily"]["realized_pnl"] = -10
    elif key == "open_loss":
        context["snapshot"]["unrealized_pnl"] = -10
    elif key == "pnl_verified":
        context["daily"]["unverified_closures"] = 1
    elif key == "consecutive_losses":
        context["daily"]["consecutive_losses"] = 3
    else:
        raise AssertionError(f"Missing test scenario: {key}")
    raw = core.evaluate_entry_gates(**context)
    assert [gate["key"] for gate in raw["gates"] if not gate["passed"] and gate["key"] not in LEGACY_QUALITY_KEYS] == [key]
    report = admit(request, result, engine.params, raw_entry_gates=raw,
                   candidate_notional_usdt=context["candidate_notional_usdt"])
    assert all(check.passed for check in report.strategy_quality)
    assert not report.offline_eligible and key in report.failures


@pytest.mark.parametrize(
    "mutation", ["missing", "added", "duplicate", "reorder", "unknown", "passed_int", "detail_none",
                 "empty_label", "empty_detail", "empty_reason", "passed_string", "decision_none",
                 "row_extra", "row_missing", "gates_none", "top_missing", "top_extra", "wrong_aggregate",
                 "wrong_decision", "wrong_reason", "legacy_fake_pass", "not_dict"],
)
def test_missing_extra_or_malformed_native_output_is_fail_closed(mutation, caplog):
    engine, request, result, context = valid_admission_inputs()
    raw = core.evaluate_entry_gates(**context)
    if mutation == "missing":
        raw["gates"].pop()
    elif mutation == "added":
        raw["gates"].append({"key": "new_safety_gate", "passed": True, "label": "new", "detail": "new"})
    elif mutation == "duplicate":
        raw["gates"][-1] = copy.deepcopy(raw["gates"][0])
    elif mutation == "reorder":
        raw["gates"][0], raw["gates"][1] = raw["gates"][1], raw["gates"][0]
    elif mutation == "unknown":
        raw["gates"][0]["key"] = "other"
    elif mutation == "passed_int":
        raw["gates"][0]["passed"] = 1
    elif mutation == "detail_none":
        raw["gates"][0]["detail"] = None
    elif mutation == "empty_label":
        raw["gates"][0]["label"] = ""
    elif mutation == "empty_detail":
        raw["gates"][0]["detail"] = " "
    elif mutation == "empty_reason":
        raw["reason"] = ""
    elif mutation == "passed_string":
        raw["passed"] = "False"
    elif mutation == "decision_none":
        raw["decision"] = None
    elif mutation == "row_extra":
        raw["gates"][0]["bypass"] = True
    elif mutation == "row_missing":
        del raw["gates"][0]["passed"]
    elif mutation == "gates_none":
        raw["gates"] = None
    elif mutation == "top_missing":
        del raw["decision"]
    elif mutation == "top_extra":
        raw["permission"] = True
    elif mutation == "wrong_aggregate":
        raw["passed"] = True
    elif mutation == "wrong_decision":
        raw["decision"] = "LONG"
    elif mutation == "wrong_reason":
        raw["reason"] = "hidden failure"
    elif mutation == "legacy_fake_pass":
        raw["gates"][5]["passed"] = True
        raw["reason"] = raw["gates"][6]["detail"]
    elif mutation == "not_dict":
        raw = None
    report = admit(request, result, engine.params, raw_entry_gates=raw, candidate_notional_usdt=0)
    assert not report.offline_eligible and "gate_schema" in report.failures
    assert report.raw_entry_gates == raw and report.legacy_quality == ()
    assert "OFFLINE_ADMISSION_SCHEMA_REJECTED" in caplog.text


@pytest.mark.parametrize("notional", [-1, math.nan, math.inf, True])
def test_candidate_notional_cannot_be_negative_nonfinite_or_boolean(notional):
    engine, request, result, context = valid_admission_inputs()
    report = admit(request, result, engine.params, raw_entry_gates=core.evaluate_entry_gates(**context),
                   candidate_notional_usdt=notional)
    assert not report.offline_eligible and "candidate_notional_invalid" in report.failures


@pytest.mark.parametrize("key", STRATEGY_QUALITY_KEYS)
def test_every_strategy_quality_category_fails_closed_on_invalid_evidence(key):
    engine, request, result, context = valid_admission_inputs()
    if key == "data":
        request = replace(request, decision_time=AT + 900)
    elif key == "warmup":
        request = replace(request, candles_by_timeframe={"15m": request.candles_by_timeframe["15m"][-21:]})
    elif key == "channel":
        result.signal.diagnostics["channel"]["upper"] += 1
    elif key == "breakout":
        result = replace(result, signal=replace(result.signal, decision="WAIT", strategy_eligible=False))
    elif key == "atr":
        result = replace(result, signal=replace(result.signal, atr=0))
    elif key == "stop":
        result = replace(result, signal=replace(result.signal, stop=result.signal.entry))
    elif key == "tp":
        result = replace(result, signal=replace(result.signal, tp3=None))
    elif key == "provenance":
        result = replace(result, signal=replace(result.signal, provenance=replace(result.signal.provenance, parameter_hash="0" * 64)))
    report = admit(request, result, engine.params, raw_entry_gates=core.evaluate_entry_gates(**context),
                   candidate_notional_usdt=0)
    assert not report.offline_eligible and f"strategy_{key}" in report.failures


def test_cache_namespaces_never_mix_original_versions_or_changed_closed_data():
    data = dataset("original")
    original = OfflineFacade(data, config())
    original_result = original.canonical(SYMBOL, AT)
    original_cache = copy.deepcopy(data.decisions)
    first, second = donchian(data, params=DonchianParams()), donchian(data)
    before = first.canonical(SYMBOL, AT)
    second.canonical(SYMBOL, AT)
    assert data.decisions == original_cache
    assert original.canonical(SYMBOL, AT) is original_result
    assert set(first.cache_keys).isdisjoint(second.cache_keys)
    for key, params in ((first.cache_keys[0], first.params), (second.cache_keys[0], second.params)):
        assert key[:4] == (CACHE_NAMESPACE, "donchian_breakout", params.provenance.strategy_version,
                           params.provenance.parameter_hash)
        assert len(key[4]) == 64 and key[5:] == (SYMBOL, AT)
    before.signal.diagnostics["admission"] = "TAMPERED"
    assert first.canonical(SYMBOL, AT).signal.diagnostics["admission"] == "NOT_EVALUATED"
    rows = data.frames[SYMBOL]["15m"].rows
    rows[-1].update(high=1e9, close=1e8)
    first.canonical(SYMBOL, AT)
    assert len(first.cache_keys) == 1
    rows[-2]["volume"] += 1
    first.canonical(SYMBOL, AT)
    assert len(first.cache_keys) == 2 and first.cache_keys[0][4] != first.cache_keys[1][4]


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("ordering", ["STOP_FIRST", "TP_FIRST"])
def test_donchian_replay_uses_native_lifecycle_and_keeps_failed_raw_quality(side, ordering, offline_only):
    engine = donchian(dataset(side=side), config(intrabar=ordering))
    result = offline_only.run_until_complete(engine.replay())
    assert len(result["trades"]) == 1
    assert len(result["admissions"]) == 3
    assert [report["phase"] for report in result["admissions"]] == ["before_sizing", "before_spec", "after_spec"]
    assert all(report["raw_entry_gates"]["passed"] is False for report in result["admissions"])
    assert all(report["offline_eligible"] is True for report in result["admissions"])
    assert all(check["passed"] for report in result["admissions"] for check in report["strategy_quality"])
    trade = result["trades"][0]
    assert trade["status"] == "CLOSED" and trade["net_r"] is not None and trade["funding_usdt"] != 0
    assert trade["signal_id"].startswith("offline-donchian-")
    assert trade["intent_id"].startswith("offline-intent-")
    assert trade["strategy"] == result["strategy"]
    assert "analysis" not in result["decisions"][0]
    assert "confidence" not in result["decisions"][0]["signal"]["diagnostics"]
    amounts = [report["candidate_notional_usdt"] for report in result["admissions"]]
    assert amounts[0] == 0 and amounts[1] > 0
    assert amounts[2] == float(engine.state.trades[0].spec["notional_usdt"])
    assert amounts[1] != amounts[2]


def test_sized_notional_blocks_exposure_before_spec(offline_only):
    engine = donchian(cfg=config(policy={"max_total_exposure_usdt": 25}))
    offline_only.run_until_complete(engine.enter(SYMBOL, AT))
    assert not engine.state.trades
    assert [report["phase"] for report in engine.admissions] == ["before_sizing", "before_spec"]
    assert engine.admissions[0]["offline_eligible"]
    assert "exposure" in engine.admissions[1]["failures"]


@pytest.mark.parametrize("allowed", [[], ["ETHUSDT"]])
def test_empty_or_unlisted_scope_rejected_before_admission_even_with_valid_strategy(allowed, offline_only):
    engine = donchian(cfg=config(policy={"allowed_symbols": allowed}))
    assert engine.canonical(SYMBOL, AT).signal.strategy_eligible
    offline_only.run_until_complete(engine.enter(SYMBOL, AT))
    assert not engine.state.trades
    assert engine.admissions == []
    assert dict(engine.state.rejections) == {"allowed_symbols": 1}
    assert dict(engine.state.first_rejections) == {"allowed_symbols": 1}


def test_post_spec_rechecks_changed_account_exposure_without_global_patch(offline_only):
    engine = donchian()
    clean = engine.state.snapshot(AT)
    changed = copy.deepcopy(clean)
    changed["positions"] = [{"symbol": "ETHUSDT", "direction": "SHORT", "quantity": 3,
                             "mark_price": 100, "entry_price": 100, "margin_type": "ISOLATED"}]
    with patch.object(engine.state, "snapshot", side_effect=[clean, clean, changed, changed, changed]):
        offline_only.run_until_complete(engine.enter(SYMBOL, AT))
    assert not engine.state.trades and len(engine.admissions) == 3
    assert engine.admissions[1]["offline_eligible"]
    assert not engine.admissions[2]["offline_eligible"]
    assert "exposure" in engine.admissions[2]["failures"]
    assert engine.admissions[2]["candidate_notional_usdt"] > 0


@pytest.mark.parametrize(
    ("case", "reason"),
    [("stop_cap", "stop_risk"), ("margin", "minimum_margin"), ("min_notional", "min_notional"),
     ("min_qty", "min_notional"), ("cost", "cost_filter"), ("spec", "native_spec_rejected"),
     ("max_qty", "native_spec_rejected"), ("protection", "native_spec_rejected"),
     ("isolated", "isolated"), ("liquidation", "liquidation_buffer"),
     ("balance", "available_balance"), ("next_open", "data_missing_next_open")],
)
def test_native_downstream_safety_rejections_with_valid_donchian_quality(case, reason, offline_only):
    data, cfg, params = dataset(), config(), DonchianParamsV2()
    if case == "stop_cap":
        params = DonchianParams()
    elif case == "margin":
        cfg = config(policy={"max_leverage": 30})
    elif case in {"min_notional", "min_qty"}:
        filters = data.metadata["exchange_info"]["symbols"][0]["filters"]
        if case == "min_notional":
            filters[2]["notional"] = "100000"
        else:
            filters[0]["minQty"] = "1000"
    elif case == "cost":
        cfg = config(policy={"minimum_net_reward_usdt": 25})
    elif case == "max_qty":
        data.metadata["exchange_info"]["symbols"][0]["filters"][0]["maxQty"] = "0.1"
    elif case == "protection":
        cfg = config(policy={"stop_required": False})
    elif case == "spec":
        data.metadata["exchange_info"]["symbols"][0]["status"] = "BREAK"
    elif case == "isolated":
        cfg = config(policy={"require_isolated": False})
    elif case == "liquidation":
        data.metadata["brackets"] = {}
    elif case == "next_open":
        data.marks[SYMBOL] = Series("15m", [])
    engine = donchian(data, cfg, params)
    if case == "balance":
        engine.state.cash = 1
    offline_only.run_until_complete(engine.enter(SYMBOL, AT))
    assert not engine.state.trades and engine.state.rejections[reason] == 1
    assert all(check["passed"] for check in engine.admissions[0]["strategy_quality"])


def test_invalid_closed_data_is_reported_and_not_cached(offline_only, caplog):
    engine = donchian()
    engine.state.data.frames[SYMBOL]["15m"].rows[-2]["close"] = math.nan
    result = engine.canonical(SYMBOL, AT)
    assert result.signal.reason == "INVALID_CANDLE_DATA" and engine.cache_keys == ()
    offline_only.run_until_complete(engine.enter(SYMBOL, AT))
    assert not engine.state.trades and "strategy_data" in engine.admissions[0]["failures"]
    assert "OFFLINE_DONCHIAN_INVALID_CACHE_DATA" in caplog.text


def test_repeated_calls_are_deterministic_and_inputs_are_not_mutated(offline_only):
    data = dataset()
    before = copy.deepcopy((data.frames, data.marks, data.funding, data.metadata))
    first = offline_only.run_until_complete(OfflineFacade(
        data, config(), strategy_id="donchian_breakout", params=DonchianParamsV2(),
    ).replay())
    second = offline_only.run_until_complete(OfflineFacade(
        copy.deepcopy(data), config(), strategy_id="donchian_breakout", params=DonchianParamsV2(),
    ).replay())
    exact(first, second)
    assert (data.frames, data.marks, data.funding, data.metadata) == before and data.decisions == {}


@pytest.mark.parametrize("strategy,params", [("unknown", None), ("kais_original", DonchianParamsV2())])
def test_unknown_dispatch_or_original_parameter_override_is_explicit_error(strategy, params):
    with pytest.raises(ValueError):
        OfflineFacade(dataset(), config(), strategy_id=strategy, params=params)
