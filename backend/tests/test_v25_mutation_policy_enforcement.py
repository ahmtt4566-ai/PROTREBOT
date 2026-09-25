import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app import v25_execution  # noqa: E402
from app.v25_execution import live_plan_can_mutate  # noqa: E402


def test_read_only_external_policy_blocks_confirmed_plan_mutation():
    plan = {
        "provenance_state": "CONFIRMED",
        "mutation_policy": "READ_ONLY_EXTERNAL",
    }

    assert live_plan_can_mutate(plan) is False


def test_confirmed_legacy_plan_without_mutation_policy_remains_mutable():
    plan = {"provenance_state": "CONFIRMED"}

    assert live_plan_can_mutate(plan) is True


def emergency_request(state):
    application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
    request = SimpleNamespace(app=application)
    body = v25_execution.EmergencyRequest(
        confirmation="CANLI ACİL DURDUR",
        close_tracked_positions=False,
    )
    return request, body


def emergency_state(plan):
    state = v25_execution.initial_state()
    state["plans"] = {plan["id"]: plan}
    return state


async def run_emergency(state, open_orders):
    request, body = emergency_request(state)
    signed = AsyncMock(return_value={})
    client = SimpleNamespace(signed=signed)
    snapshot = {
        "positions": [],
        "open_orders": open_orders,
        "open_algo_orders": [],
    }
    with patch.object(v25_execution, "execution_owner", return_value={"id": "owner-1"}), patch.object(
        v25_execution, "client_for", return_value=client
    ), patch.object(
        v25_execution, "account_snapshot", new=AsyncMock(return_value=snapshot)
    ), patch.object(v25_execution, "persist_state"):
        response = await v25_execution.v25_emergency(request, body)
    return response, signed


def test_emergency_skips_unowned_live_prefixed_order():
    plan = {
        "id": "confirmed-plan",
        "provenance_state": "CONFIRMED",
        "entry_client_order_id": "PTBLV_ENTRY_owned",
        "close_client_order_ids": None,
    }
    unowned_order = {
        "symbol": "MUBARAKUSDT",
        "order_id": 501,
        "client_order_id": "PTBLV_ENTRY_unowned",
    }

    response, signed = asyncio.run(
        run_emergency(emergency_state(plan), [unowned_order])
    )

    assert response["cancelled_bot_orders"] == 0
    signed.assert_not_awaited()


def test_emergency_cancels_owned_live_prefixed_order():
    plan = {
        "id": "confirmed-plan",
        "provenance_state": "CONFIRMED",
        "entry_client_order_id": "PTBLV_ENTRY_owned",
        "close_client_order_ids": None,
    }
    owned_order = {
        "symbol": "MUBARAKUSDT",
        "order_id": 502,
        "client_order_id": "PTBLV_ENTRY_owned",
    }

    response, signed = asyncio.run(
        run_emergency(emergency_state(plan), [owned_order])
    )

    assert response["cancelled_bot_orders"] == 1
    signed.assert_awaited_once_with(
        "DELETE",
        "/fapi/v1/order",
        {"symbol": "MUBARAKUSDT", "orderId": 502},
    )
