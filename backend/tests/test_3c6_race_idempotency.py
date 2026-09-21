import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import binance_demo, stop_evidence, v21_demo  # noqa: E402


def plan_with_protection():
    return {
        "id": "plan-1",
        "user_id": "user-1",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "position_side": "BOTH",
        "provenance_state": "CONFIRMED",
        "status": "OPEN",
        "position_status": "OPEN",
        "stop_loss": "90",
        "stop_algo_id": 101,
        "protection_ids": [101, 102],
    }


def protection_snapshot():
    return {
        "open_algo_orders_available": True,
        "open_algo_orders": [
            {"symbol": "BTCUSDT", "algo_id": 101, "type": "STOP_MARKET", "side": "SELL", "status": "NEW"},
            {"symbol": "BTCUSDT", "algo_id": 102, "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
        ],
    }


def stop_algo_event():
    return {
        "e": "ALGO_UPDATE",
        "T": 100,
        "o": {"s": "BTCUSDT", "aid": 77, "ca": "PTB_SL_1", "ai": 9001, "ac": "PTB_ACTUAL_1", "X": "TRIGGERED"},
    }


def stop_trade_event():
    return {
        "e": "ORDER_TRADE_UPDATE",
        "T": 101,
        "o": {"s": "BTCUSDT", "i": 9001, "c": "PTB_ACTUAL_1", "t": 5001, "x": "TRADE", "X": "FILLED", "S": "SELL", "ps": "BOTH", "l": "2", "z": "2", "R": True},
    }


def confirmed_stop_state():
    plan = plan_with_protection() | {"stop_algo_id": 77, "stop_client_id": "PTB_SL_1", "tp1_status": "PENDING"}
    return {
        "evidence_sequence": 0,
        "evidence_observations": [],
        "stop_correlations": [],
        "plans": {plan["id"]: plan},
    }


def test_duplicate_stop_algo_and_trade_produce_one_evidence_path():
    state = confirmed_stop_state()
    stop_evidence.observe_position_snapshot(state, [{"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "2"}])
    stop_evidence.observe_stream_payload(state, stop_algo_event(), demo_state=state)
    stop_evidence.observe_stream_payload(state, stop_algo_event(), demo_state=state)
    stop_evidence.observe_stream_payload(state, stop_trade_event(), demo_state=state)
    stop_evidence.observe_stream_payload(state, stop_trade_event(), demo_state=state)

    assert len(state["evidence_observations"]) == 3
    assert len(state["stop_correlations"]) == 1
    assert state["plans"]["plan-1"]["stop_execution_trade_id"] == "5001"


def test_duplicate_lifecycle_stream_event_is_rejected_before_mutation():
    state = v21_demo.initial_state()
    payload = stop_trade_event()

    assert v21_demo.process_stream_event(state, payload) is True
    assert v21_demo.process_stream_event(state, payload) is False
    assert len(state["journal"]) == 1
    assert len(state["evidence_observations"]) == 1


def test_concurrent_cleanup_deletes_each_protection_once():
    asyncio.run(_test_concurrent_cleanup_deletes_each_protection_once())


async def _test_concurrent_cleanup_deletes_each_protection_once():
    plan = plan_with_protection()
    calls = []

    class Client:
        async def signed(self, method, path, params=None):
            calls.append((method, path, params["algoId"]))
            await asyncio.sleep(0)
            return {}

    state = {"plans": {"plan-1": plan}}
    snapshot = protection_snapshot()
    await asyncio.gather(
        binance_demo.cleanup_closed_plan(Client(), plan, snapshot=snapshot, plans=[plan]),
        binance_demo.cleanup_closed_plan(Client(), plan, snapshot=snapshot, plans=[plan]),
    )

    assert [item[2] for item in calls if item[0] == "DELETE"] == [101, 102]
    assert plan["protection_ids"] == []


def test_cleanup_delete_failure_retains_id_for_retry():
    asyncio.run(_test_cleanup_delete_failure_retains_id_for_retry())


async def _test_cleanup_delete_failure_retains_id_for_retry():
    plan = plan_with_protection()
    attempts = 0

    class Client:
        async def signed(self, method, path, params=None):
            nonlocal attempts
            if method == "DELETE":
                attempts += 1
                if attempts == 1:
                    raise binance_demo.BinanceDemoError("temporary delete failure")
            return {}

    await binance_demo.cleanup_closed_plan(Client(), plan, snapshot=protection_snapshot(), plans=[plan])
    assert plan["protection_ids"] == [101]
    await binance_demo.cleanup_closed_plan(Client(), plan, snapshot=protection_snapshot(), plans=[plan])
    assert plan["protection_ids"] == []
    assert attempts == 3


def test_concurrent_missing_stop_repair_posts_once():
    asyncio.run(_test_concurrent_missing_stop_repair_posts_once())


async def _test_concurrent_missing_stop_repair_posts_once():
    plan = plan_with_protection()
    state = {"plans": {"plan-1": plan}}
    snapshot = {
        "open_algo_orders_available": True,
        "open_algo_orders": [
            {"symbol": "BTCUSDT", "algo_id": 102, "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
        ],
    }
    post_calls = 0

    async def post_algo(_client, _params):
        nonlocal post_calls
        post_calls += 1
        await asyncio.sleep(0)
        return {"algoId": 201, "symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "SELL", "status": "NEW"}

    with patch.object(binance_demo, "post_algo", new=AsyncMock(side_effect=post_algo)), patch.object(binance_demo, "persist_runtime"):
        results = await asyncio.gather(
            binance_demo._repair_missing_stop_protection(None, state, plan, snapshot, plans=[plan]),
            binance_demo._repair_missing_stop_protection(None, state, plan, snapshot, plans=[plan]),
        )

    assert results.count(True) == 1
    assert post_calls == 1
    assert plan["stop_algo_id"] == 201
    assert plan["protection_repair_pending"] is False
