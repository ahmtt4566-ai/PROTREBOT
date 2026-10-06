"""Synthetic execution proofs, independent of historical data and prior fixtures.

Runtime evidence is produced by assertions, not a regenerated gate trace.
Natural replay uses genuine canonical Original/Donchian decisions. The separate
identical-input test supplies Donchian levels to Engine.enter, retaining actual
canonical Original quality scores: it isolates execution, not strategy parity.
Only economic comparisons omit the two deliberately different namespace IDs.

Intentional differences are asserted explicitly: confidence tie-break, non-15m
gaps and NaN balance semantics. The initial int/float zero notional also differs.
Stop prefilter and allowlist precedence require native parity.
Native BE is absent: TP1 never moves the Stop; crossing entry is not a BE exit.
No Stage modules, real exchange, persisted cache or actual trading is used.
"""

from __future__ import annotations

import asyncio
import copy
import math
import socket
import sqlite3
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import execution_core as core
from app import main
from app import v25_execution as live
from app.backtest_baseline import Config, Engine, Position
from app.backtest_data import Dataset, Series
from app.strategies.donchian_params import DonchianParamsV2
from app.strategies.offline_facade import DonchianOfflineEngine, OfflineFacade

AT = 1585699200
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
ENTRY = 300.0


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    loop = asyncio.new_event_loop()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Only synthetic local data and simulated positions are permitted")

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
    gate_binding = core.evaluate_entry_gates
    yield loop
    assert core.evaluate_entry_gates is gate_binding
    loop.close()


def exact(actual, expected):
    assert type(actual) is type(expected)
    if isinstance(expected, dict):
        assert tuple(actual) == tuple(expected)
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


@contextmanager
def actual_gate_spy():
    """Call the actual evaluator ONCE and return its unchanged actual raw object."""
    actual = core.evaluate_entry_gates
    calls = []

    def recording(**kwargs):
        raw = actual(**kwargs)
        calls.append({"input": copy.deepcopy(kwargs), "raw": raw, "captured": copy.deepcopy(raw)})
        return raw

    with patch.object(core, "evaluate_entry_gates", side_effect=recording) as spy:
        yield calls, spy
        assert spy.call_count == len(calls)
        for call in calls:
            exact(call["raw"], call["captured"])


def candle(at, opening, high, low, close, volume=100.0):
    return {"time": at, "open": opening, "high": high, "low": low, "close": close,
            "volume": volume, "quote_volume": 20000000.0}


def metadata(symbols, tick):
    return {
        "historical": False, "observed_at": "SYNTHETIC_ONLY",
        "exchange_info": {"symbols": [
            {"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
             "filters": [
                 {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000"},
                 {"filterType": "PRICE_FILTER", "tickSize": tick},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"},
             ]} for symbol in symbols
        ]},
        "brackets": {
            symbol: {"symbol": symbol, "brackets": [
                {"notionalFloor": 0, "notionalCap": 1000000, "maintMarginRatio": 0.004,
                 "cum": 0, "initialLeverage": 125},
            ]} for symbol in symbols
        },
    }


def path_bar(index, side, outcome):
    sign = 1 if side == "LONG" else -1
    favorable, adverse = ENTRY + sign, ENTRY - sign
    if index == 1:
        favorable = ENTRY + sign * 3.5
        adverse = ENTRY - sign * 0.5
    if index == 2:
        favorable, adverse = ENTRY + sign, ENTRY - sign * 0.5
    if index == 33:
        favorable = ENTRY + sign * (10 if outcome in {"TP3", "AMBIGUOUS"} else 1)
        adverse = ENTRY - sign * (4 if outcome in {"STOP", "AMBIGUOUS"} else 1)
    return candle(AT + index * 900, ENTRY, max(favorable, adverse),
                  min(favorable, adverse), ENTRY)


def dataset(
    symbols=("BTCUSDT",), *, bars=1, side="LONG", outcome="TP3",
    tick="3", volumes=None, atr_values=None,
):
    frames, marks, funding, months = {}, {}, {}, {}
    sign = 1 if side == "LONG" else -1
    for symbol in symbols:
        frames[symbol] = {}
        width = 2.0 if atr_values is None else atr_values.get(symbol, 2.0)
        volume = 140.0 if volumes is None else volumes.get(symbol, 140.0)
        for interval, duration in (("15m", 900), ("1h", 3600), ("4h", 14400)):
            rows = []
            for index in range(260):
                close = ENTRY - sign * (0.2 + (259 - index) * 0.002)
                if index == 259:
                    close = ENTRY
                opening = close - sign * (width - 0.2)
                high, low = max(opening, close) + 0.1, min(opening, close) - 0.1
                if side == "SHORT" and interval == "4h":
                    opening = close = ENTRY
                    high, low = ENTRY + 1, ENTRY - 1
                rows.append(candle(AT - (260 - index) * duration, opening, high, low, close,
                                   volume if index == 259 else 100.0))
            if interval == "15m":
                rows.extend(path_bar(index, side, outcome) for index in range(bars))
            else:
                rows.extend(candle(AT + index * duration, ENTRY, ENTRY + 1, ENTRY - 1, ENTRY)
                            for index in range(bars))
            frames[symbol][interval] = Series(interval, rows)
        marks[symbol] = Series("15m", [path_bar(index, side, outcome) for index in range(bars)])
        funding[symbol] = [
            {"time": AT + 450 + offset * 28800, "rate": 0.0001, "interval_hours": 8}
            for offset in (-1, 0, 1, 2)
        ]
        months[symbol] = {"2020-03", "2020-04"}
    return Dataset(frames, marks, funding, metadata(symbols, tick), {}, months)


def config(*, bars=1, side="LONG", ordering="STOP_FIRST", multi=False, policy=None):
    settings = {} if policy is None else dict(policy)
    if side == "SHORT":
        settings["mtf_allow_either_timeframe"] = True
    if multi:
        settings.update(max_margin_per_trade=5, max_leverage=10, max_same_direction_positions=5)
    return Config(AT, AT + bars * 900, bootstrap_samples=8,
                  conditional_current_metadata=True, intrabar=ordering, policy=settings)


def make_donchian(data, cfg):
    facade = OfflineFacade(data, cfg, strategy_id="donchian_breakout", params=DonchianParamsV2())
    assert isinstance(facade.engine, DonchianOfflineEngine)
    return facade.engine


def genuine_original_signal(engine, symbol="BTCUSDT"):
    decision = engine.data.canonical(symbol, AT, engine.policy)
    assert decision["entry_eligible"] is True
    assert decision["decision"] in {"BUY", "SELL"}
    return copy.deepcopy(decision["analysis"])


def recorded_replay(driver, loop):
    with actual_gate_spy() as (gates, spy), patch.object(driver, "enter", wraps=driver.enter) as enters:
        result = loop.run_until_complete(driver.replay())
        attempts = [call.args[0] for call in enters.call_args_list]
        assert enters.await_count == len(attempts)
        assert spy.call_count == len(gates)
    return result, attempts, gates


def economic_row(position):
    row = position.row()
    assert {"signal_id", "intent_id"} <= row.keys()
    return {key: value for key, value in row.items() if key not in {"signal_id", "intent_id"}}


def assert_equal_positions(native, donchian):
    assert type(native) is type(donchian) is Position
    exact(native.spec, donchian.spec)
    for field in ("symbol", "direction", "opened_at", "slip", "mark_entry", "market_open",
                  "quantity", "remaining", "tp1_quantity", "actual_entry", "initial_risk",
                  "commission", "gross", "funding_amount", "funding_known", "tp1_hit",
                  "ambiguous_bars", "exits", "closed_at", "status"):
        exact(getattr(native, field), getattr(donchian, field))
    assert native.signal_identifier != donchian.signal_identifier
    exact(economic_row(native), economic_row(donchian))


@pytest.mark.parametrize("insertion", [SYMBOLS, tuple(reversed(SYMBOLS))])
def test_four_real_signals_equal_scores_volume_stable_order_first_three(insertion, offline_only):
    first, second = dataset(insertion), dataset(insertion)
    cfg = config(multi=True)
    native, donor = Engine(first, cfg), make_donchian(second, cfg)
    for symbol in SYMBOLS:
        genuine_original_signal(native, symbol)
        assert donor.canonical(symbol, AT).signal.strategy_eligible
    tickers = [first.frames[symbol]["15m"].ticker(AT, symbol) for symbol in sorted(SYMBOLS)]
    ranked = live.rank_market_tickers(first.metadata["exchange_info"], tickers, allowed_symbols=set(SYMBOLS))
    assert len(ranked) == 4
    assert len({item["opportunity_score"] for item in ranked}) == len({item["volume"] for item in ranked}) == 1
    expected = sorted(SYMBOLS)[:3]
    native_result, native_attempts, native_gates = recorded_replay(native, offline_only)
    donor_result, donor_attempts, donor_gates = recorded_replay(donor, offline_only)
    assert native_attempts == donor_attempts == expected
    assert [row["symbol"] for row in native_result["trades"]] == expected
    assert [row["symbol"] for row in donor_result["trades"]] == expected
    assert len(native_gates) == len(donor_gates) == 9
    assert all(call["raw"]["passed"] is True for call in native_gates)
    assert all(call["raw"]["passed"] is False for call in donor_gates)
    for actual, captured in zip(donor.admissions, donor_gates, strict=True):
        exact(actual["raw_entry_gates"], captured["raw"])


def test_rejected_first_of_four_has_no_fourth_candidate_replacement_both_paths(offline_only):
    first, second = dataset(SYMBOLS), dataset(SYMBOLS)
    rejected = min(SYMBOLS)
    for data in (first, second):
        rule = next(row for row in data.metadata["exchange_info"]["symbols"] if row["symbol"] == rejected)
        rule["filters"][0]["minQty"] = "1000"
    cfg = config(multi=True)
    native, donor = Engine(first, cfg), make_donchian(second, cfg)
    for symbol in SYMBOLS:
        genuine_original_signal(native, symbol)
        assert donor.canonical(symbol, AT).signal.strategy_eligible
    native_result, native_attempts, _ = recorded_replay(native, offline_only)
    donor_result, donor_attempts, _ = recorded_replay(donor, offline_only)
    assert native_attempts == donor_attempts == sorted(SYMBOLS)[:3]
    accepted = sorted(SYMBOLS)[1:3]
    assert [row["symbol"] for row in native_result["trades"]] == accepted
    assert [row["symbol"] for row in donor_result["trades"]] == accepted
    assert native.rejections["min_notional"] == donor.state.rejections["min_notional"] == 1
    assert sorted(SYMBOLS)[3] not in native_attempts + donor_attempts


def test_intentional_difference_native_confidence_tiebreak_changes_top_three_vs_donchian(offline_only):
    volumes = {"BNBUSDT": 100, "BTCUSDT": 140, "ETHUSDT": 100, "SOLUSDT": 140}
    first, second = dataset(SYMBOLS, volumes=volumes), dataset(SYMBOLS, volumes=volumes)
    cfg = config(multi=True)
    native, donor = Engine(first, cfg), make_donchian(second, cfg)
    confidence = {symbol: genuine_original_signal(native, symbol)["confidence"] for symbol in SYMBOLS}
    assert confidence["BTCUSDT"] == confidence["SOLUSDT"] > confidence["BNBUSDT"] == confidence["ETHUSDT"]
    tickers = [first.frames[symbol]["15m"].ticker(AT, symbol) for symbol in sorted(SYMBOLS)]
    ranked = live.rank_market_tickers(first.metadata["exchange_info"], tickers, allowed_symbols=set(SYMBOLS))
    assert len(ranked) == 4
    assert len({item["opportunity_score"] for item in ranked}) == len({item["volume"] for item in ranked}) == 1
    native_result, native_attempts, _ = recorded_replay(native, offline_only)
    donor_result, donor_attempts, _ = recorded_replay(donor, offline_only)
    assert native_attempts == ["BTCUSDT", "SOLUSDT", "BNBUSDT"]
    assert donor_attempts == ["BNBUSDT", "BTCUSDT", "ETHUSDT"]
    assert [row["symbol"] for row in native_result["trades"]] == native_attempts
    assert [row["symbol"] for row in donor_result["trades"]] == donor_attempts


@pytest.mark.parametrize("interval", ["1h", "4h"])
def test_intentional_difference_non15m_history_gap_native_rejects_donchian_admits(interval, offline_only):
    first, second = dataset(), dataset()
    for data in (first, second):
        series = data.frames["BTCUSDT"][interval]
        data.frames["BTCUSDT"][interval] = Series(interval, series.rows[:258] + series.rows[259:])
    cfg = config(multi=True)
    native, donor = Engine(first, cfg), make_donchian(second, cfg)
    native_result, native_attempts, native_gates = recorded_replay(native, offline_only)
    donor_result, donor_attempts, donor_gates = recorded_replay(donor, offline_only)
    assert native_result["trades"] == [] and native_attempts == [] and native_gates == []
    assert native.rejections["data_gap_history"] == 1
    assert donor_attempts == ["BTCUSDT"] and len(donor_result["trades"]) == 1 and len(donor_gates) == 3


@pytest.mark.parametrize("rejected_count", [1, 2, 3])
def test_aligned_stop_prefilter_rejections_do_not_consume_first_three(rejected_count, offline_only):
    """Both paths reject wide Stops before sorting, leaving up to three valid candidates."""
    rejected = set(sorted(SYMBOLS)[:rejected_count])
    atr_values = {symbol: 12.0 if symbol in rejected else 2.0 for symbol in SYMBOLS}
    first, second = dataset(SYMBOLS, atr_values=atr_values), dataset(SYMBOLS, atr_values=atr_values)
    cfg = config(multi=True)
    native, donor = Engine(first, cfg), make_donchian(second, cfg)
    for symbol in SYMBOLS:
        signal = genuine_original_signal(native, symbol)
        result = donor.canonical(symbol, AT)
        assert result.signal.strategy_eligible
        if symbol in rejected:
            assert abs(signal["entry"] - signal["stop_loss"]) / signal["entry"] * 100 > 5
            assert abs(result.signal.entry - result.signal.stop) / result.signal.entry * 100 > 5
    with patch.object(core, "dynamic_stop_distance_pct", wraps=core.dynamic_stop_distance_pct) as helper:
        native_result, native_attempts, native_gates = recorded_replay(native, offline_only)
        native_prefilter = [call.args for call in helper.call_args_list if call.args[1] is None]
    with patch.object(core, "dynamic_stop_distance_pct", wraps=core.dynamic_stop_distance_pct) as helper:
        donor_result, donor_attempts, donor_gates = recorded_replay(donor, offline_only)
        donor_prefilter = [call.args for call in helper.call_args_list if call.args[1] is None]
    expected = sorted(set(SYMBOLS) - rejected)[:3]
    assert native_attempts == donor_attempts == expected
    assert [row["symbol"] for row in native_result["trades"]] == expected
    assert [row["symbol"] for row in donor_result["trades"]] == expected
    exact(dict(native.rejections), {"stop_distance": rejected_count})
    exact(dict(donor.state.rejections), {"stop_distance": rejected_count})
    exact(dict(native.first_rejections), dict(donor.state.first_rejections))
    assert len(native_gates) == len(donor_gates) == 3 * len(expected)
    exact(native_prefilter, [(ENTRY, None, native.policy)] * len(SYMBOLS))
    exact(donor_prefilter, [(ENTRY, None, donor.state.policy)] * len(SYMBOLS))


@pytest.mark.parametrize(("relation", "accepted"), [("below", True), ("equal", True), ("above", False)])
def test_aligned_replay_stop_cap_boundary_has_strict_greater_than_without_tolerance(
    relation, accepted, offline_only,
):
    """Controlled common Stop isolates replay comparison, not strategy equivalence."""
    first, second = dataset(tick="0.01"), dataset(tick="0.01")
    baseline = Engine(first, config())
    decision = copy.deepcopy(first.canonical("BTCUSDT", AT, baseline.policy))
    assert decision["entry_eligible"]
    signal = make_donchian(second, config()).canonical("BTCUSDT", AT).signal
    assert signal.strategy_eligible
    assert signal.entry is not None and signal.stop is not None
    decision["analysis"]["stop_loss"] = signal.stop
    distance = abs(signal.entry - signal.stop) / signal.entry * 100
    cap = distance if relation == "equal" else math.nextafter(
        distance, math.inf if relation == "below" else -math.inf,
    )
    cfg = config(multi=True, policy={"max_stop_distance_pct": cap})
    native, donor = Engine(first, cfg), make_donchian(second, cfg)
    assert core.dynamic_stop_distance_pct(signal.entry, None, native.policy) == cap
    assert core.dynamic_stop_distance_pct(signal.entry, None, donor.state.policy) == cap
    assert (distance > cap) is (not accepted)
    if not accepted:
        assert distance < cap + 1e-9
    with patch.object(first, "canonical", return_value=decision):
        native_result, native_attempts, native_gates = recorded_replay(native, offline_only)
    donor_result, donor_attempts, donor_gates = recorded_replay(donor, offline_only)
    expected = ["BTCUSDT"] if accepted else []
    assert native_attempts == donor_attempts == expected
    assert [row["symbol"] for row in native_result["trades"]] == expected
    assert [row["symbol"] for row in donor_result["trades"]] == expected
    assert len(native_gates) == len(donor_gates) == (3 if accepted else 0)
    exact(dict(native.rejections), {} if accepted else {"stop_distance": 1})
    exact(dict(donor.state.rejections), dict(native.rejections))


@pytest.mark.parametrize("allowed", [[], ["ETHUSDT"]])
def test_aligned_allowlist_rejection_precedes_data_strategy_and_raw_gates(allowed, offline_only):
    """Both paths return allowed_symbols before any gate evaluation."""
    cfg = config(policy={"allowed_symbols": allowed})
    native, donor = Engine(dataset(), cfg), make_donchian(dataset(), cfg)
    signal = genuine_original_signal(native)
    with actual_gate_spy() as (native_gates, _):
        offline_only.run_until_complete(native.enter("BTCUSDT", signal, AT))
    with actual_gate_spy() as (donor_gates, _), \
            patch.object(donor, "request_at", wraps=donor.request_at) as requests, \
            patch.object(donor, "canonical", wraps=donor.canonical) as decisions:
        offline_only.run_until_complete(donor.enter("BTCUSDT", AT))
    assert not native.trades and not donor.state.trades
    assert native_gates == donor_gates == donor.admissions == []
    assert donor.cache_keys == ()
    requests.assert_not_called()
    decisions.assert_not_called()
    exact(dict(native.rejections), {"allowed_symbols": 1})
    exact(dict(donor.state.rejections), {"allowed_symbols": 1})
    exact(dict(native.first_rejections), dict(donor.state.first_rejections))


def independent_closed_economics(position):
    sign = position.sign
    entry = Decimal(ENTRY) * (1 + sign * Decimal("0.0003"))
    q, partial = position.quantity, position.tp1_quantity
    prices = [Decimal(str(fill["expected_price"])) * (1 - sign * Decimal("0.0003"))
              for fill in position.exits]
    quantities = [partial, q - partial]
    assert [Decimal(str(fill["quantity"])) for fill in position.exits] == quantities
    gross = sum((amount * sign * (price - entry) for amount, price in zip(quantities, prices, strict=True)), Decimal(0))
    commission = q * entry * Decimal("0.0005") + sum(
        (amount * price * Decimal("0.0005") for amount, price in zip(quantities, prices, strict=True)), Decimal(0),
    )
    funding = -sign * Decimal(ENTRY) * Decimal("0.0001") * (q + q - partial)
    risk = abs(Decimal(position.spec["entry_price"]) - Decimal(position.spec["stop_loss"])) * q
    net = float(gross - commission) + float(funding)
    return {"gross": gross, "commission": commission, "funding": funding,
            "risk": float(risk), "net": net, "r": float(Decimal(str(net)) / Decimal(str(float(risk))))}


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize("ordering", ["STOP_FIRST", "TP_FIRST"])
@pytest.mark.parametrize("outcome", ["STOP", "TP3", "AMBIGUOUS"])
def test_identical_supplied_levels_economic_parity_with_zero_type_difference_and_no_be(
    side, ordering, outcome, offline_only,
):
    """Same execution inputs; known int-0/float-0.0 trace difference is not parity.

    Quality scores are genuine. This is not Original strategy equivalence.
    Economic state and subsequent notional stages require exact equality.
    """
    cfg = config(bars=34, side=side, ordering=ordering)
    native = Engine(dataset(bars=34, side=side, outcome=outcome, tick="0.01"), cfg)
    donor = make_donchian(dataset(bars=34, side=side, outcome=outcome, tick="0.01"), cfg)
    signal = genuine_original_signal(native)
    actual = donor.canonical("BTCUSDT", AT).signal
    assert actual.strategy_eligible
    signal.update(entry=actual.entry, stop_loss=actual.stop, tp1=actual.tp1, tp2=actual.tp2,
                  tp3=actual.tp3, atr=actual.atr)
    assert (signal["entry"], signal["stop_loss"], signal["tp1"], signal["tp2"], signal["tp3"], signal["atr"]) == (
        actual.entry, actual.stop, actual.tp1, actual.tp2, actual.tp3, actual.atr,
    )
    with actual_gate_spy() as (native_gates, _):
        offline_only.run_until_complete(native.enter("BTCUSDT", signal, AT))
    with actual_gate_spy() as (donor_gates, _):
        offline_only.run_until_complete(donor.enter("BTCUSDT", AT))
    assert len(native.trades) == len(donor.state.trades) == 1
    assert len(native_gates) == len(donor_gates) == 3
    first, second = native.trades[0], donor.state.trades[0]
    assert_equal_positions(first, second)
    for stage, (left, right) in enumerate(zip(native_gates, donor_gates, strict=True)):
        if stage == 0:
            exact(left["input"]["candidate_notional_usdt"], 0)
            exact(right["input"]["candidate_notional_usdt"], 0.0)
        else:
            exact(left["input"]["candidate_notional_usdt"], right["input"]["candidate_notional_usdt"])
        exact([gate for gate in left["raw"]["gates"] if gate["key"] not in {"confidence", "trap"}],
              [gate for gate in right["raw"]["gates"] if gate["key"] not in {"confidence", "trap"}])
    stop, initial_risk = first.spec["stop_loss"], first.initial_risk
    for index in range(34):
        for state, position in ((native, first), (donor.state, second)):
            state.advance(position, AT + index * 900, opening_only=True)
            state.advance(position, AT + index * 900, opening_only=False)
        assert_equal_positions(first, second)
        exact(native.cash, donor.state.cash)
        assert first.initial_risk == initial_risk
        assert first.spec["stop_loss"] == second.spec["stop_loss"] == stop
        if index == 1:
            assert first.tp1_hit and first.remaining == first.quantity - first.tp1_quantity
            assert len(first.exits) == 1 and first.exits[0]["reason"] == "TP1"
        if index == 2:
            assert first.status == "OPEN" and first.remaining > 0
            assert len(first.exits) == 1, "Native has no automatic BE exit when entry is crossed"
    reason = "STOP" if outcome == "STOP" or (outcome == "AMBIGUOUS" and ordering == "STOP_FIRST") else "TP3"
    assert [fill["reason"] for fill in first.exits] == ["TP1", reason]
    assert first.ambiguous_bars == (1 if outcome == "AMBIGUOUS" else 0)
    assert first.closed_at == AT + 33 * 900 + 899
    row, expected = first.row(), independent_closed_economics(first)
    assert row["status"] == "CLOSED" and row["funding_complete"] is True and row["net_r"] is not None
    assert first.gross == expected["gross"] and first.commission == expected["commission"]
    assert first.funding_amount == expected["funding"] != 0
    exact(row["initial_risk_usdt"], expected["risk"])
    exact(row["net_pnl"], expected["net"])
    exact(row["net_r"], expected["r"])
    if side == "LONG":
        assert cfg.policy == {}, "LONG common-entry scenario uses the literal default policy"


@pytest.mark.parametrize("ordering", ["STOP_FIRST", "TP_FIRST"])
@pytest.mark.parametrize("outcome", ["STOP", "TP3", "AMBIGUOUS"])
def test_natural_closed_signals_same_rounded_spec_and_multibar_lifecycle(ordering, outcome, offline_only):
    """No supplied level overrides: independent real strategies converge at tick=3."""
    cfg = config(bars=34, ordering=ordering)
    native = Engine(dataset(bars=34, outcome=outcome), cfg)
    donor = make_donchian(dataset(bars=34, outcome=outcome), cfg)
    original = genuine_original_signal(native)
    signal = donor.canonical("BTCUSDT", AT).signal
    assert original["entry"] == signal.entry == ENTRY
    assert original["stop_loss"] != signal.stop
    native_result, native_attempts, native_gates = recorded_replay(native, offline_only)
    donor_result, donor_attempts, donor_gates = recorded_replay(donor, offline_only)
    assert native_attempts == donor_attempts == ["BTCUSDT"]
    assert len(native_result["trades"]) == len(donor_result["trades"]) == 1
    assert len(native_gates) == len(donor_gates) == 3
    assert_equal_positions(native.trades[0], donor.state.trades[0])
    expected = independent_closed_economics(native.trades[0])
    exact(native_result["trades"][0]["net_r"], expected["r"])
    exact(donor_result["trades"][0]["net_r"], expected["r"])
    assert [call["raw"] for call in donor_gates] == [report["raw_entry_gates"] for report in donor.admissions]
    assert cfg.policy == {}


@pytest.mark.parametrize("ordering", ["STOP_FIRST", "TP_FIRST"])
def test_literal_original_facade_full_default_policy_multibar_parity_and_actual_trace(ordering, offline_only):
    cfg = config(bars=34, ordering=ordering)
    native, facade = Engine(dataset(bars=34), cfg), OfflineFacade(dataset(bars=34), cfg)
    assert type(facade.engine) is Engine and cfg.policy == {}
    expected_policy = core.sanitize_execution_policy(
        {"fee_bps_per_side": 5, "slippage_bps_per_side": 3}, preserve_empty_allowed_symbols=True,
    )
    exact(native.policy, expected_policy)
    exact(facade.engine.policy, expected_policy)
    with actual_gate_spy() as (native_gates, _):
        expected = offline_only.run_until_complete(native.replay())
    with actual_gate_spy() as (facade_gates, _):
        actual = offline_only.run_until_complete(facade.replay())
    exact(actual, expected)
    exact([call["raw"] for call in facade_gates], [call["raw"] for call in native_gates])
    assert len(expected["trades"]) == 1 and len(native_gates) == len(facade_gates) == 3
    row = actual["trades"][0]
    assert row["status"] == "CLOSED" and row["net_r"] is not None and row["funding_complete"]
    assert [fill["reason"] for fill in row["exits"]] == ["TP1", "TP3"]


@pytest.mark.parametrize(("balance", "accepted"), [(9.999999, False), (10.0, True), (10.000001, True)])
def test_finite_balance_below_equal_above_margin_is_equivalent(balance, accepted, offline_only):
    cfg = config()
    native, donor = Engine(dataset(), cfg), make_donchian(dataset(), cfg)
    signal = genuine_original_signal(native)
    native.cash = donor.state.cash = balance
    offline_only.run_until_complete(native.enter("BTCUSDT", signal, AT))
    offline_only.run_until_complete(donor.enter("BTCUSDT", AT))
    assert bool(native.trades) is bool(donor.state.trades) is accepted
    if accepted:
        assert native.trades[0].spec["margin_usdt"] == donor.state.trades[0].spec["margin_usdt"] == 10.0
    else:
        assert native.rejections["available_balance"] == donor.state.rejections["available_balance"] == 1


def test_intentional_difference_nan_balance_native_accepts_donchian_rejects(offline_only):
    """Malformed state injection, not a valid Config or evidence of a LIVE exploit."""
    cfg = config()
    native, donor = Engine(dataset(), cfg), make_donchian(dataset(), cfg)
    signal = genuine_original_signal(native)
    native.cash = donor.state.cash = math.nan
    with actual_gate_spy() as (native_gates, _):
        offline_only.run_until_complete(native.enter("BTCUSDT", signal, AT))
    with actual_gate_spy() as (donor_gates, _):
        offline_only.run_until_complete(donor.enter("BTCUSDT", AT))
    assert len(native.trades) == 1 and donor.state.trades == []
    assert len(native_gates) == len(donor_gates) == 3
    assert donor.state.rejections["available_balance"] == 1
    assert donor.admissions[-1]["final_offline_eligible"] is False
    assert donor.admissions[-1]["protections"][-1] == {"key": "available_balance", "passed": False}
    assert math.isnan(native.cash) and math.isnan(donor.state.cash)
