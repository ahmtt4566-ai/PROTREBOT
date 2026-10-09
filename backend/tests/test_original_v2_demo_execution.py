"""Fake exchange and in-memory persistence only; no global monkeypatch."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import CodeType, FunctionType, SimpleNamespace

import pytest
import test_original_v2_demo as decisions
from app import binance_demo as demo
from app import original_v2_demo_execution as original
from app import v21_demo as control
from app.maintenance import default_maintenance_state
from app.strategies import original_v2_demo as strategy

ROOT = Path(__file__).parents[2]


def result():
    return strategy.evaluate(decisions.request(), enabled=True)


def plan(quantity="3", minimum="0.1", notional="5"):
    return {
        **original.identity(result(), "original-plan"),
        "id": "original-plan", "symbol": "BTCUSDT", "direction": "LONG",
        "entry_price": "100", "stop_loss": "95", "initial_stop_loss": "95",
        "targets": ["105", "110", "115"], "step": "0.1", "min_qty": minimum,
        "min_notional": notional, "quantity": quantity, "initial_quantity": quantity,
        "remaining_quantity": quantity, "provenance_state": "CONFIRMED",
        "status": "OPEN", "position_status": "OPEN", "user_id": "owner",
        "entry_order_id": 10, "entry_client_order_id": "entry-owned",
    }


class MemoryConnection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, *args):
        return "INSERT 0 1"


class MemoryPool:
    def acquire(self):
        return MemoryConnection()


def states(current_plan=None):
    application = SimpleNamespace(state=SimpleNamespace(
        db_pool=MemoryPool(), maintenance=default_maintenance_state(),
    ))
    runtime = {
        "plans": {current_plan["id"]: current_plan} if current_plan else {},
        "events": [], "lock": asyncio.Lock(), "_user_id": "owner", "_app": application,
        "_persistence_blocked": True, "armed_until": 10**12,
    }
    state = control.initial_state()
    state.update({"_user_id": "owner", "_app": application})
    state["auto"].update({"enabled": True, "user_confirmed": True})
    application.state.binance_demo = runtime
    application.state.v21_demo = state
    return application, runtime, state


class FakeClient:
    def __init__(self, quantity="3"):
        self.quantity = quantity
        self.algos = []
        self.calls = []

    async def signed(self, method, path, params=None):
        params = dict(params or {})
        self.calls.append((method, path, params))
        if path == "/fapi/v3/positionRisk":
            return [{"symbol": "BTCUSDT", "positionAmt": self.quantity, "markPrice": "100"}]
        if path == "/fapi/v1/openAlgoOrders":
            return self.algos
        if path == "/fapi/v1/algoOrder" and method == "POST":
            row = {
                **params, "algoId": len(self.algos) + 101, "orderType": params["type"],
                "algoStatus": "NEW",
            }
            self.algos.append(row)
            return row
        if path == "/fapi/v1/algoOrder" and method == "DELETE":
            self.algos[:] = [row for row in self.algos if row["algoId"] != params["algoId"]]
            return {}
        raise AssertionError(f"Unexpected fake exchange call: {method} {path}")


class EntryClient(FakeClient):
    def __init__(self):
        super().__init__("0")
        self.price = str(result().legacy["analysis"]["entry"])
        self.metadata = decisions.fixtures.open_dataset().metadata["exchange_info"]

    async def public_get(self, path, params=None):
        self.calls.append(("PUBLIC_GET", path, dict(params or {})))
        if path == "/fapi/v1/ticker/price":
            return {"price": self.price}
        if path == "/fapi/v1/exchangeInfo":
            return self.metadata
        raise AssertionError(path)

    async def signed(self, method, path, params=None):
        if path == "/fapi/v1/order" and method == "POST":
            self.calls.append((method, path, dict(params)))
            self.quantity = params["quantity"]
            return {"orderId": 10, "clientOrderId": params["newClientOrderId"], "status": "FILLED"}
        if path == "/fapi/v3/positionRisk":
            self.calls.append((method, path, dict(params or {})))
            return [{"symbol": "BTCUSDT", "positionAmt": self.quantity, "markPrice": self.price}]
        return await super().signed(method, path, params)


def install(current):
    async def run():
        application, runtime, state = states(current)
        client = FakeClient(current["quantity"])
        await demo.install_protection(client, runtime, current)
        return application, runtime, state, client
    return asyncio.run(run())


@pytest.mark.parametrize(("quantity", "minimum", "notional", "expected"), [
    ("3.1", "0.1", "5", "1.8"),
    ("0.1", "0.1", "5", "0"),
    ("0.2", "0.1", "20", "0"),
])
def test_original_protection_60_percent_rounding_and_minimum(quantity, minimum, notional, expected):
    current = plan(quantity, minimum, notional)
    _, _, _, client = install(current)
    assert current["tp1_quantity"] == expected
    assert current["tp2_quantity"] == "0"
    target_posts = [params for method, path, params in client.calls
                    if method == "POST" and params["type"] == "TAKE_PROFIT_MARKET"]
    assert len(target_posts) == (2 if expected != "0" else 1)
    assert all(params["triggerPrice"] != "110" for params in target_posts)
    assert current["stop_loss"] == "95"
    assert current["tp3_algo_id"] in current["protection_ids"]
    assert current["monitoring_targets"] == []


def fill_payload(current, label, quantity, *, trade_id=1, cumulative=None):
    return {
        "e": "ORDER_TRADE_UPDATE", "T": trade_id,
        "o": {
            "s": current["symbol"], "i": current[f"{label}_actual_order_id"],
            "c": f"{label}-actual", "t": trade_id, "x": "TRADE", "X": "FILLED",
            "l": quantity, "z": cumulative or quantity, "rp": "1", "n": "0.01",
            "N": "USDT", "R": True, "S": "SELL", "ap": "105",
        },
    }


@pytest.mark.parametrize("exit_label", ["stop", "tp3"])
def test_tp1_then_original_stop_or_tp3_and_identity_journal(exit_label):
    current = plan()
    application, runtime, state, client = install(current)
    current["tp1_actual_order_id"] = 201
    current[f"{exit_label}_actual_order_id"] = 202
    first = fill_payload(current, "tp1", "1.8")
    assert control.process_stream_event(state, first, runtime)
    assert not control.process_stream_event(state, first, runtime)
    assert current["remaining_quantity"] == "1.2"
    assert current["tp1_fill_confirmed"] is True
    snapshot = {"positions": [{
        "symbol": "BTCUSDT", "quantity": "1.2", "direction": "LONG",
        "entry_price": 100, "mark_price": 110,
    }]}
    before = list(client.calls)
    assert asyncio.run(control.improve_dynamic_stops(
        application, snapshot, demo_state=runtime, v21_state=state, client=client,
    )) is False
    assert current["stop_loss"] == current["initial_stop_loss"] == "95"
    assert client.calls == before
    assert control.process_stream_event(state, fill_payload(current, exit_label, "1.2", trade_id=2), runtime)
    assert current["remaining_quantity"] == "0"
    assert current["position_status"] == "CLOSED"
    assert current["original_closure_verified"] is True
    assert Decimal(current["verified_net_pnl"]) == Decimal("1.98")
    for row in state["journal"]:
        assert {key: row[key] for key in original.IDENTITY_FIELDS} == {
            key: current[key] for key in original.IDENTITY_FIELDS
        }


def test_partial_tp1_is_not_a_verified_complete_fill():
    current = plan()
    _, runtime, state, _ = install(current)
    current["tp1_actual_order_id"] = 201
    assert control.process_stream_event(state, fill_payload(current, "tp1", "0.5"), runtime)
    assert current["tp1_fill_confirmed"] is False
    assert current["tp1_status"] == "PARTIALLY_FILLED"


def test_reinstallation_after_tp1_keeps_only_initial_stop_and_tp3():
    current = plan()
    current.update({"quantity": "1.2", "remaining_quantity": "1.2", "tp1_fill_confirmed": True})
    _, _, _, client = install(current)
    assert current["stop_loss"] == "95"
    targets = [params for method, _, params in client.calls
               if method == "POST" and params["type"] == "TAKE_PROFIT_MARKET"]
    assert [params["triggerPrice"] for params in targets] == ["115"]


def test_reinstallation_after_partial_tp1_uses_initial_allocation_not_60_of_remainder():
    current = plan()
    current.update({"quantity": "2.5", "remaining_quantity": "2.5", "tp1_filled_quantity": "0.5"})
    _, _, _, _ = install(current)
    assert current["tp1_quantity"] == "1.3"


def test_fill_matching_never_uses_symbol_alone_or_missing_identifiers():
    current = plan()
    runtime = {"plans": {current["id"]: current}}
    assert original.match_plan(runtime, {"s": "BTCUSDT", "i": 999}) is None
    assert original.match_plan(runtime, {"s": "BTCUSDT"}) is None
    assert original.match_plan(runtime, {"s": "BTCUSDT", "i": 10}) is current
    with pytest.raises(demo.BinanceDemoError, match="SYMBOL_MISMATCH"):
        original.match_plan(runtime, {"s": "ETHUSDT", "i": 10})


def test_restore_retains_immutable_original_exit_and_legacy_is_not_relabelled():
    current = plan()
    legacy = {"id": "legacy", "symbol": "ETHUSDT", "status": "OPEN"}
    restored = demo._state_from_demo_payload(
        {"plans": {current["id"]: deepcopy(current), "legacy": deepcopy(legacy)}},
        "owner", SimpleNamespace(state=SimpleNamespace()), "file", "restored", "none",
    )
    assert original.original_plan(restored["plans"][current["id"]])
    assert restored["plans"][current["id"]]["stop_loss"] == "95"
    assert "strategy_id" not in restored["plans"]["legacy"]
    invalid = deepcopy(current)
    invalid["stop_loss"] = "100"
    with pytest.raises(demo.BinanceDemoError, match="INITIAL_STOP_CHANGED"):
        demo._state_from_demo_payload(
            {"plans": {"broken": invalid}}, "owner", None, "file", "restored", "none",
        )


def test_unknown_strategy_is_an_explicit_entry_error():
    value = result()
    value = replace(value, signal=replace(value.signal, provenance=replace(
        value.signal.provenance, strategy_id="unknown",
    )))
    with pytest.raises(demo.BinanceDemoError, match="UNKNOWN_DEMO_STRATEGY_ID"):
        original.validate_result(value)


def test_snapshot_carries_identity_only_for_original_owned_plan():
    current = plan()
    snapshot = {"positions": [{"symbol": "BTCUSDT", "leverage": 3}]}
    demo.enrich_snapshot_with_plans(snapshot, {"plans": {current["id"]: current}})
    assert {key: snapshot["positions"][0][key] for key in original.IDENTITY_FIELDS} == {
        key: current[key] for key in original.IDENTITY_FIELDS
    }


def test_duplicate_candle_boundary_does_not_scan_or_submit_again():
    async def run():
        application, runtime, state = states()
        from datetime import datetime, timezone
        at = int(datetime.now(timezone.utc).timestamp()) // 900 * 900
        state["original_v2"] = {"last_decision_time": at}
        state["auto"]["strategy_id"] = strategy.STRATEGY_ID
        state["settings"].update({"allowed_symbols": ["LTCUSDT"], "max_positions": 3})
        state["risk"]["consecutive_losses"] = 3
        before = deepcopy(state["original_v2"])
        for _ in range(2):
            await control._automatic_cycle_impl(
                application, user_id="owner", demo_state=runtime, v21_state=state,
            )
        assert state["original_v2"] == before
        assert not runtime["plans"]
        assert not state["journal"]
        assert not hasattr(application.state, "http")
    asyncio.run(run())


def test_fixed_universe_native_closed_candles_and_ranking_no_dynamic_ticker():
    class CandleClient:
        def __init__(self):
            self.calls = []
            self.data = decisions.fixtures.open_dataset()

        async def public_get(self, path, params=None):
            params = params or {}
            self.calls.append((path, dict(params)))
            if path == "/fapi/v1/exchangeInfo":
                info = deepcopy(self.data.metadata["exchange_info"])
                reference = next(row for row in info["symbols"] if row["symbol"] == "BTCUSDT")
                info["symbols"] = [{**deepcopy(reference), "symbol": symbol} for symbol in decisions.SYMBOLS]
                return info
            assert path == "/fapi/v1/klines"
            assert params["symbol"] in decisions.SYMBOLS and params["limit"] == 260
            interval = params["interval"]
            duration = {"15m": 900, "1h": 3600, "4h": 14400}[interval]
            rows = self.data.frames["BTCUSDT"][interval].closed(decisions.fixtures.AT)[-259:]
            return [[
                row["time"] * 1000, row["open"], row["high"], row["low"],
                row["close"], row["volume"], (row["time"] + duration) * 1000 - 1,
                row.get("quote_volume") or 1_000_000,
            ] for row in rows]
    client = CandleClient()
    selected, results = asyncio.run(original.candidates(client, decisions.fixtures.AT, set()))
    assert set(results) == set(decisions.SYMBOLS)
    assert len(client.calls) == 25
    assert all(path != "/fapi/v1/ticker/24hr" for path, _ in client.calls)
    for value in results.values():
        assert value.signal.provenance.strategy_id == strategy.STRATEGY_ID
        assert value.signal.decision_time == decisions.fixtures.AT
        assert value.signal.latest_closed_timestamps == result().signal.latest_closed_timestamps
    assert all(value.signal.strategy_eligible for _, value in selected)


def test_available_balance_and_pending_position_limits_are_not_dropped():
    _, runtime, state = states()
    snapshot = {"positions": [], "open_orders": [], "available_balance": 4,
                "hedge_mode": False, "unrealized_pnl": 0}
    with pytest.raises(demo.BinanceDemoError, match="available_balance"):
        original.validate_gates(result(), snapshot, state, runtime, 15, margin_usdt=5)
    snapshot["available_balance"] = 1000
    snapshot["open_orders"] = [{"symbol": symbol, "reduce_only": False} for symbol in decisions.SYMBOLS[1:6]]
    with pytest.raises(demo.BinanceDemoError, match="positions"):
        original.validate_gates(result(), snapshot, state, runtime)


def test_fake_stop_first_same_bar_model_not_exchange_ordering_claim():
    from app.backtest_baseline import Position
    spec = {
        "quantity": "3", "step": Decimal("0.1"), "min_qty": Decimal("0.1"),
        "min_notional": Decimal(5), "entry_price": "100", "stop_loss": "95",
        "targets": ["105", "110", "115"],
    }
    position = Position("BTCUSDT", "LONG", 0, spec, "model", Decimal(0), Decimal(100), Decimal(100))
    bar = {"time": 0, "open": 100, "high": 116, "low": 94, "close": 100}
    position.advance(bar, bar, "STOP_FIRST", opening_only=False)
    assert position.remaining == 0
    assert position.tp1_hit is False


def clone(function, **overrides):
    """Bind fake dependencies in a new function namespace, never a module."""
    copied = FunctionType(function.__code__, {**function.__globals__, **overrides},
                          function.__name__, function.__defaults__, function.__closure__)
    copied.__kwdefaults__ = function.__kwdefaults__
    return copied


def baseline_function(module, name):
    relative = Path(module.__file__).relative_to(ROOT)
    source = subprocess.check_output(
        ["git", "show", f"7454f71:{relative.as_posix()}"], cwd=ROOT, text=True, encoding="utf-8",
    )
    node = next(node for node in ast.parse(source).body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    node.decorator_list = []
    namespace = dict(vars(module))
    compiled = compile(ast.Module(body=[node], type_ignores=[]), str(relative), "exec")
    code = next(value for value in compiled.co_consts if isinstance(value, CodeType) and value.co_name == name)
    current = getattr(module, name)
    function = FunctionType(code, namespace, name, current.__defaults__)
    function.__kwdefaults__ = current.__kwdefaults__
    return function


def test_flag_off_legacy_journal_and_serialized_outputs_are_identical():
    state = control.initial_state()
    old_record = baseline_function(control, "record_event")
    old_serialized = baseline_function(control, "serializable_state")
    left, right = deepcopy(state), deepcopy(state)
    stable = lambda: "2025-03-01T00:00:00+00:00"
    old_record = clone(old_record, now_iso=stable)
    new_record = clone(control.record_event, now_iso=stable)
    old_record(left, "FILL", "fixture", symbol="BTCUSDT", event_id="stable")
    new_record(right, "FILL", "fixture", symbol="BTCUSDT", event_id="stable")
    assert left["journal"] == right["journal"]
    assert clone(old_serialized, now_iso=stable)(left) == clone(control.serializable_state, now_iso=stable)(right)


def test_flag_off_legacy_order_protection_requests_and_plan_are_identical():
    async def run(function):
        current = plan()
        for key in original.IDENTITY_FIELDS:
            current.pop(key)
        _, runtime, _ = states(current)
        client = FakeClient()
        bound = clone(
            function, utc_now=lambda: "2025-03-01T00:00:00+00:00",
            new_client_id=lambda kind: f"fixed-{kind}",
            add_event=clone(demo.add_event, utc_now=lambda: "2025-03-01T00:00:00+00:00"),
        )
        await bound(client, runtime, current)
        return current, client.calls, runtime["events"]
    previous = baseline_function(demo, "_install_protection")
    assert asyncio.run(run(previous)) == asyncio.run(run(demo._install_protection))


def _legacy_cycle_parity():
    async def run(function):
        application, runtime, state = states()
        calls = []
        async def snapshot(*args):
            calls.append("account")
            return {"positions": [], "open_orders": [], "wallet_balance": 1000, "available_balance": 1000}
        async def scan(*args):
            calls.append("scan")
            return []
        completion = clone(control._apply_scan_completion_state, time=SimpleNamespace(time=lambda: 1704067200))
        bound = clone(
            function, client_for_state=lambda *args: FakeClient(),
            account_snapshot=snapshot, scan_demo_universe=scan, in_schedule=lambda settings: True,
            now_iso=lambda: "2024-01-01T00:00:00+00:00",
            persist_state=lambda state: None, _apply_scan_completion_state=completion,
        )
        await bound(application, user_id="owner", demo_state=runtime, v21_state=state)
        state.pop("_app")
        return state, calls
    previous = baseline_function(control, "_automatic_cycle_impl")
    assert asyncio.run(run(previous)) == asyncio.run(run(control._automatic_cycle_impl))


def test_flag_off_legacy_decision_cycle_is_identical():
    script = (
        "import sys;sys.path[:0]=['backend','backend/tests'];"
        "import test_original_v2_demo_execution as t;t._legacy_cycle_parity()"
    )
    subprocess.check_call(
        [sys.executable, "-B", "-c", script], cwd=ROOT,
        env={**os.environ, strategy.FEATURE_FLAG: "false", original.SEND_FLAG: "false",
             "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": ""},
    )


def _legacy_entry_parity():
    async def run(function):
        application, runtime, state = states()
        value = result()
        signal = value.legacy["analysis"]
        client = EntryClient()
        body = demo.DemoOrderRequest(
            symbol=value.signal.symbol, direction=signal["direction"], margin_usdt=10,
            leverage=2, stop_loss=signal["stop_loss"], tp1=signal["tp1"],
            tp2=signal["tp2"], tp3=signal["tp3"],
        )
        async def snapshot(*args):
            return {
                "positions": [], "open_orders": [], "open_algo_orders": [],
                "available_balance": 1000, "wallet_balance": 1000, "hedge_mode": False,
                "unrealized_pnl": 0, "open_algo_orders_available": True,
                "open_algo_orders_quality": "CONFIRMED",
            }
        async def one_way(*args):
            return 0
        async def isolated(*args):
            return None
        async def leverage(client, symbol, requested):
            assert requested == 2
            return {
                "requested_leverage": 2, "applied_leverage": 2, "margin_type": "ISOLATED",
                "leverage_verified": True, "configuration_source": "FAKE_VERIFIED",
                "max_notional_value": "200",
            }
        stable = lambda: "2025-03-01T00:00:00+00:00"
        event = clone(demo.add_event, utc_now=stable)
        leaf = clone(demo._install_protection, utc_now=stable,
                     new_client_id=lambda kind: f"fixed-{kind}", add_event=event)
        installer = clone(demo.install_protection, _install_protection=leaf)
        bound = clone(
            function, client_for_state=lambda *args: client, account_snapshot=snapshot,
            ensure_one_way_position_mode=one_way, set_isolated_margin=isolated,
            apply_verified_leverage=leverage, utc_now=stable, add_event=event,
            new_client_id=lambda kind: f"fixed-{kind}", install_protection=installer,
            uuid=SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="abc123abc123abc123")),
        )
        response = await bound(
            application, body, source="AUTO_SCANNER", demo_state=runtime, v21_state=state,
        )
        return response, runtime["plans"], runtime["events"], state["journal"], client.calls
    previous = baseline_function(demo, "execute_demo_order")
    assert asyncio.run(run(previous)) == asyncio.run(run(demo.execute_demo_order))


def test_flag_off_complete_legacy_entry_response_and_orders_are_identical():
    subprocess.check_call(
        [sys.executable, "-B", "-c",
         ("import sys;sys.path[:0]=['backend','backend/tests'];"
          "import test_original_v2_demo_execution as t;t._legacy_entry_parity()")],
        cwd=ROOT, env={**os.environ, strategy.FEATURE_FLAG: "false", original.SEND_FLAG: "false",
                       "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": ""},
    )


@pytest.mark.parametrize("failure", ["owner", "confirmation", "arm", "maintenance"])
def test_original_entry_preserves_authorization_even_in_dry_run(failure):
    async def run():
        application, runtime, state = states()
        value = result()
        signal = value.legacy["analysis"]
        body = demo.DemoOrderRequest(
            symbol=value.signal.symbol, direction=signal["direction"], margin_usdt=25,
            leverage=3, stop_loss=signal["stop_loss"], tp1=signal["tp1"],
            tp2=signal["tp2"], tp3=signal["tp3"],
        )
        if failure == "owner":
            runtime["_user_id"] = "someone-else"
        elif failure == "confirmation":
            state["auto"]["user_confirmed"] = False
        elif failure == "arm":
            runtime["armed_until"] = 0
        else:
            application.state.maintenance["mode"] = "MAINTENANCE"
        from fastapi import HTTPException
        with pytest.raises((demo.BinanceDemoError, HTTPException)):
            await demo.execute_demo_order(
                application, body, demo_state=runtime, v21_state=state, strategy_result=value,
            )
        assert not runtime["plans"]
        assert not state["journal"]
    asyncio.run(run())


@pytest.mark.parametrize(("enabled", "send"), [("false", "false"), ("true", "false"), ("false", "true")])
def test_two_flags_fail_closed_and_dry_run_has_no_client_calls(enabled, send):
    script = """
import asyncio,json,sys
sys.path[:0]=['backend','backend/tests']
import test_original_v2_demo_execution as t
from app import binance_demo as d
async def run():
 app,runtime,state=t.states()
 value=t.result()
 signal=value.legacy['analysis']
 body=d.DemoOrderRequest(symbol=value.signal.symbol,direction=signal['direction'],
  margin_usdt=25,leverage=3,stop_loss=signal['stop_loss'],tp1=signal['tp1'],tp2=signal['tp2'],tp3=signal['tp3'])
 response=await d.execute_demo_order(app,body,demo_state=runtime,v21_state=state,strategy_result=value)
 assert response['dry_run'] and response['order'] is None and not runtime['plans']
 assert response['plan']['order_authorized'] is False
 assert state['journal'][0]['plan_id']==response['plan']['plan_id']
 await asyncio.sleep(0)
 print(json.dumps({'dry_run':True,'exchange_calls':0}))
asyncio.run(run())
"""
    environment = {
        **os.environ, strategy.FEATURE_FLAG: enabled, original.SEND_FLAG: send,
        "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": "",
    }
    output = subprocess.check_output(
        [sys.executable, "-B", "-c", script], cwd=ROOT, env=environment, text=True,
    )
    assert json.loads(output.splitlines()[-1]) == {"dry_run": True, "exchange_calls": 0}


def test_both_flags_true_routes_only_the_fake_entry_and_fixed_exit():
    script = """
import asyncio,json,sys
sys.path[:0]=['backend','backend/tests']
import test_original_v2_demo_execution as t
from app import binance_demo as d
async def run():
 app,runtime,state=t.states()
 value=t.result(); signal=value.legacy['analysis']; client=t.EntryClient()
 body=d.DemoOrderRequest(symbol=value.signal.symbol,direction=signal['direction'],
  margin_usdt=25,leverage=3,stop_loss=signal['stop_loss'],tp1=signal['tp1'],tp2=signal['tp2'],tp3=signal['tp3'])
 async def snapshot(*args):
  return {'positions':[],'open_orders':[],'open_algo_orders':[],'available_balance':1000,
   'wallet_balance':1000,'hedge_mode':False,'unrealized_pnl':0,
   'open_algo_orders_available':True,'open_algo_orders_quality':'CONFIRMED'}
 async def one_way(*args): return 0
 async def isolated(*args): return None
 async def leverage(client,symbol,requested):
  assert requested==3
  return dict(requested_leverage=3,applied_leverage=3,margin_type='ISOLATED',
   leverage_verified=True,configuration_source='FAKE_VERIFIED',max_notional_value='350')
 call=t.clone(d.execute_demo_order,client_for_state=lambda *args:client,
  account_snapshot=snapshot,ensure_one_way_position_mode=one_way,
  set_isolated_margin=isolated,apply_verified_leverage=leverage)
 response=await call(app,body,source='ORIGINAL_V2',demo_state=runtime,v21_state=state,strategy_result=value)
 p=response['plan']
 assert p['strategy_id']==t.strategy.STRATEGY_ID and p['leverage']==3
 assert p['tp2_quantity']=='0' and p['stop_loss']==p['initial_stop_loss']
 assert len([c for c in client.calls if c[:2]==('POST','/fapi/v1/order')])==1
 assert state['journal'][0]['plan_id']==p['plan_id']
 assert p['execution_policy_id']==t.original.EXIT_ID
 await asyncio.sleep(0)
 print(json.dumps({'fake_entry_orders':1,'fixed_exit':True}))
asyncio.run(run())
"""
    environment = {
        **os.environ, strategy.FEATURE_FLAG: "true", original.SEND_FLAG: "true",
        "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": "",
    }
    output = subprocess.check_output(
        [sys.executable, "-B", "-c", script], cwd=ROOT, env=environment, text=True,
    )
    assert json.loads(output.splitlines()[-1]) == {"fake_entry_orders": 1, "fixed_exit": True}


def test_live_imported_helpers_and_certificate_are_bytecode_source_unchanged():
    from app import v25_execution as live
    imported = ast.parse(Path(live.__file__).read_text(encoding="utf-8"))
    names = [
        alias.name for node in imported.body
        if isinstance(node, ast.ImportFrom) and node.module == "binance_demo"
        for alias in node.names
    ]
    for name in names:
        value = getattr(demo, name)
        if isinstance(value, type) or not callable(value):
            continue
        source = subprocess.check_output(
            ["git", "show", "7454f71:backend/app/binance_demo.py"], cwd=ROOT, text=True, encoding="utf-8",
        )
        current_nodes = ast.parse(Path(demo.__file__).read_text(encoding="utf-8")).body
        old_nodes = ast.parse(source).body
        current = next(node for node in current_nodes if getattr(node, "name", None) == name)
        previous = next(node for node in old_nodes if getattr(node, "name", None) == name)
        assert ast.dump(current) == ast.dump(previous)
    source = subprocess.check_output(
        ["git", "show", "7454f71:backend/app/v21_demo.py"], cwd=ROOT, text=True, encoding="utf-8",
    )
    previous = next(node for node in ast.parse(source).body if getattr(node, "name", None) == "certificate_payload")
    current = next(node for node in ast.parse(Path(control.__file__).read_text(encoding="utf-8")).body
                   if getattr(node, "name", None) == "certificate_payload")
    assert ast.dump(current) == ast.dump(previous)
