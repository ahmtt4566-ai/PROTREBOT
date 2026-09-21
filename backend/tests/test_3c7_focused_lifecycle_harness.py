import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app import binance_demo, stop_evidence, v21_demo


def entry_plan(plan_id="plan-1", *, state="PROVISIONAL", client_id="PTB_ENTRY_1", order_id=101):
    return {
        "id": plan_id,
        "symbol": "BTCUSDT",
        "position_side": "BOTH",
        "provenance_state": state,
        "provenance_entry_client_order_id": client_id,
        "provenance_entry_order_id": order_id,
        "provenance_trade_ids": [],
        "provenance_expected_quantity": "1",
        "provenance_last_observation_id": None,
        "provenance_broken_reason": None,
        "status": "OPEN",
    }


def entry_trade(*, trade_id="501", client_id="PTB_ENTRY_1", order_id=101, quantity="1", event_time=1):
    return {
        "e": "ORDER_TRADE_UPDATE",
        "T": event_time,
        "o": {
            "s": "BTCUSDT",
            "i": order_id,
            "c": client_id,
            "t": trade_id,
            "l": quantity,
            "z": quantity,
            "x": "TRADE",
            "X": "FILLED",
            "ps": "BOTH",
        },
    }


def lifecycle_plan(state="CONFIRMED"):
    return {
        "id": "plan-1",
        "symbol": "BTCUSDT",
        "position_side": "BOTH",
        "provenance_state": state,
        "status": "OPEN",
        "position_status": "OPEN",
        "quantity": "2",
        "initial_quantity": "2",
        "remaining_quantity": "2",
        "tp1_quantity": "0.6",
        "tp2_quantity": "0.6",
        "tp1_status": "PENDING",
        "tp2_status": "PENDING",
        "tp3_status": "PENDING",
    }


def stop_plan(**overrides):
    plan = {
        "id": "plan-1",
        "user_id": "user-1",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "provenance_state": "CONFIRMED",
        "status": "OPEN",
        "stop_loss": "90",
        "stop_algo_id": 77,
        "stop_client_id": "PTB_SL_1",
        "protection_ids": [77],
        "cleanup_pending_ids": [],
        "remaining_quantity": "2",
        "position_status": "OPEN",
        "tp1_status": "PENDING",
        "tp2_status": "PENDING",
        "tp3_status": "PENDING",
    }
    plan.update(overrides)
    return plan


def stop_algo_event(**overrides):
    event = {
        "e": "ALGO_UPDATE",
        "T": 100,
        "o": {"s": "BTCUSDT", "aid": 77, "ca": "PTB_SL_1", "ai": 9001, "ac": "PTB_ACTUAL_1", "X": "TRIGGERED"},
    }
    event["o"].update(overrides)
    return event


def stop_trade_event(**overrides):
    event = {
        "e": "ORDER_TRADE_UPDATE",
        "T": 101,
        "o": {"s": "BTCUSDT", "i": 9001, "c": "PTB_ACTUAL_1", "t": 5001, "x": "TRADE", "X": "FILLED", "l": "2", "z": "2", "R": True},
    }
    event["o"].update(overrides)
    return event


def stop_state(plan=None):
    return {
        "evidence_sequence": 0,
        "evidence_observations": [],
        "stop_correlations": [],
        "plans": {"plan-1": plan or stop_plan()},
    }


def test_provenance_transition_restart_and_foreign_protection_fail_closed():
    plan = entry_plan()
    state = {"plans": {plan["id"]: plan}, "_provenance_reconciliation_cycle": 1, "lock": asyncio.Lock()}
    with patch.object(v21_demo, "persist_runtime"), patch.object(binance_demo, "persist_runtime"):
        v21_demo.process_stream_event(v21_demo.initial_state(), entry_trade(), state)
        asyncio.run(binance_demo.confirm_provenance_from_snapshot(
            state, {"_provenance_positions": [{"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": "1"}]}, 1
        ))
        assert plan["provenance_state"] == "PROVISIONAL"
        asyncio.run(binance_demo.confirm_provenance_from_snapshot(
            state, {"_provenance_positions": [{"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": "1"}]}, 2
        ))
    assert plan["provenance_state"] == "CONFIRMED"
    assert binance_demo.can_mutate_lifecycle(plan)
    assert binance_demo.can_mutate_lifecycle({"provenance_state": "PROVISIONAL"}) is False
    assert binance_demo.find_conflicting_confirmed_plan(
        {"foreign": stop_plan(symbol="ETHUSDT")}, {"symbol": "BTCUSDT"}
    ) is None


def test_partial_reduction_updates_quantity_without_tp_or_stop_attribution():
    plan = lifecycle_plan()
    state = {"plans": {plan["id"]: plan}}
    binance_demo.reconcile_demo_plans(state, {"positions": [{"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": "1.4"}]})
    assert plan["remaining_quantity"] == "1.4"
    assert plan["position_status"] == "OPEN"
    assert plan["tp1_status"] == plan["tp2_status"] == plan["tp3_status"] == "PENDING"
    assert not plan.get("stop_fill_confirmed")


def test_zero_position_and_quantity_drop_do_not_attribute_stop():
    plan = stop_plan()
    state = {"plans": {plan["id"]: plan}}
    binance_demo.reconcile_demo_plans(state, {"positions": [{"symbol": "BTCUSDT", "quantity": "1"}]})
    binance_demo.reconcile_demo_plans(state, {"positions": []})
    assert not plan.get("stop_fill_confirmed")
    assert plan["position_status"] == "CLOSED"


def test_valid_stop_is_correlated_once_and_unrelated_or_foreign_is_blocked():
    state = stop_state()
    stop_evidence.observe_stream_payload(state, stop_algo_event(), demo_state=state)
    stop_evidence.observe_stream_payload(state, stop_trade_event(), demo_state=state)
    first = copy.deepcopy(state["stop_correlations"])
    stop_evidence.observe_stream_payload(state, stop_trade_event(), demo_state=state)
    assert state["plans"]["plan-1"]["stop_fill_confirmed"] is True
    assert state["stop_correlations"] == first
    foreign = stop_state(stop_plan(stop_algo_id=88, stop_client_id="FOREIGN"))
    stop_evidence.observe_stream_payload(foreign, stop_algo_event(), demo_state=foreign)
    assert foreign["stop_correlations"] == []
    assert not stop_state()["plans"]["plan-1"].get("stop_fill_confirmed")


def test_duplicate_lifecycle_event_and_restart_incomplete_state_are_idempotent():
    state = v21_demo.initial_state()
    payload = stop_trade_event()
    assert v21_demo.process_stream_event(state, payload) is True
    assert v21_demo.process_stream_event(state, payload) is False
    assert len(state["journal"]) == 1
    restarted = {"plans": {"plan-1": stop_plan()}, "stop_correlations": [{"status": "INCOMPLETE", "plan_id": "plan-1"}]}
    stop_evidence.observe_stream_payload(restarted, payload, demo_state=restarted)
    assert not restarted["plans"]["plan-1"].get("stop_fill_confirmed")


def test_confirmed_same_slot_entry_gate_rejects_before_exchange_mutation():
    existing = stop_plan()
    application = SimpleNamespace(state=SimpleNamespace())
    demo_state = {"plans": {"plan-1": existing}, "lock": asyncio.Lock(), "_user_id": "user-a", "_app": application, "armed_until": 0}
    v21_state = v21_demo.initial_state()
    v21_state["_user_id"] = "user-a"
    v21_state["_app"] = application
    application.state.binance_demo = demo_state
    application.state.v21_demo = v21_state
    snapshot = {"positions": [], "open_orders": [], "open_algo_orders": [], "open_algo_orders_available": True, "wallet_balance": 1000, "available_balance": 1000}
    client = SimpleNamespace(last_status_code=200, trace_request_id=None)
    body = binance_demo.DemoOrderRequest(symbol="BTCUSDT", direction="LONG", order_type="MARKET", margin_usdt=10, leverage=2, stop_loss=90, tp1=105, tp2=110, tp3=115)

    async def run():
        with patch.object(binance_demo, "client_for_state", return_value=client), patch.object(binance_demo, "ensure_one_way_position_mode", new=AsyncMock()), patch.object(binance_demo, "account_snapshot", new=AsyncMock(return_value=snapshot)), patch.object(binance_demo, "resolve_demo_symbol", new=AsyncMock(return_value="BTCUSDT")), patch.object(binance_demo, "reconcile_demo_plans", return_value={"changed": False}), patch.object(binance_demo, "stale_protection_entry_reason", return_value=None), patch.object(binance_demo, "build_order_spec", new=AsyncMock(side_effect=AssertionError)), patch.object(binance_demo, "persist_runtime"):
            with pytest.raises(HTTPException) as caught:
                await binance_demo.execute_demo_order(application, body, source="MANUAL", demo_state=demo_state, v21_state=v21_state)
        return caught.value

    error = asyncio.run(run())
    assert error.status_code == 409
    assert list(demo_state["plans"]) == ["plan-1"]


def test_protection_cleanup_race_deletes_each_owned_id_once():
    plan = stop_plan(protection_ids=[101, 102], stop_algo_id=101)
    snapshot = {"open_algo_orders_available": True, "open_algo_orders": [{"symbol": "BTCUSDT", "algo_id": 101, "type": "STOP_MARKET", "side": "SELL", "status": "NEW"}, {"symbol": "BTCUSDT", "algo_id": 102, "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"}]}
    calls = []

    class Client:
        async def signed(self, method, path, params=None):
            calls.append((method, params["algoId"]))
            return {}

    async def run():
        await asyncio.gather(
            binance_demo.cleanup_closed_plan(Client(), plan, snapshot=snapshot, plans=[plan]),
            binance_demo.cleanup_closed_plan(Client(), plan, snapshot=snapshot, plans=[plan]),
        )

    asyncio.run(run())
    assert [item[1] for item in calls if item[0] == "DELETE"] == [101, 102]
    assert plan["protection_ids"] == []


def test_summary_evidence_is_bounded_and_mutation_gate_is_fail_closed():
    state = v21_demo.initial_state()
    state["evidence_observations"] = [{"observation_id": str(index)} for index in range(250)]
    state["stop_correlations"] = [{"status": "INCOMPLETE"}]
    summary = v21_demo.summary_payload(state)
    assert len(summary["evidence"]["observations"]) == 200
    assert summary["evidence"]["stop_correlations"] == [{"status": "INCOMPLETE"}]
    assert all(binance_demo.can_mutate_lifecycle({"provenance_state": state}) is False for state in ("NO_PROVENANCE", "PROVISIONAL", "BROKEN"))