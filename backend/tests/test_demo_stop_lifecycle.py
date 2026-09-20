import sys
from decimal import Decimal
from pathlib import Path

import pytest

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import binance_demo  # noqa: E402


# Test-only model of the exchange-side price decision. Binance supplies this
# decision in production; the Demo lifecycle receives only the resulting state.
def _simulate_stop_hit(
    direction: str,
    current_price: Decimal,
    stop_price: Decimal,
    position_quantity: Decimal,
    plan: dict,
) -> bool:
    assert direction in {"LONG", "SHORT"}
    assert position_quantity > 0
    assert plan["position_status"] == "OPEN"
    if direction == "LONG":
        return current_price <= stop_price
    return current_price >= stop_price


class NoOrderFakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def signed(self, method: str, path: str, params: dict | None = None):
        request = dict(params or {})
        self.calls.append((method, path, request))
        if method != "DELETE":
            raise AssertionError(f"Unexpected exchange mutation: {method} {path}")
        if path != "/fapi/v1/algoOrder":
            raise AssertionError(f"Unexpected cleanup path: {path}")
        return {}


@pytest.mark.parametrize(
    ("current_price", "expected"),
    [(Decimal("100.01"), False), (Decimal("100"), True), (Decimal("99.99"), True)],
)
def test_long_stop_hit_boundaries(current_price: Decimal, expected: bool) -> None:
    plan = {"position_status": "OPEN"}
    assert _simulate_stop_hit("LONG", current_price, Decimal("100"), Decimal("2"), plan) is expected


@pytest.mark.parametrize(
    ("current_price", "expected"),
    [(Decimal("99.99"), False), (Decimal("100"), True), (Decimal("100.01"), True)],
)
def test_short_stop_hit_boundaries(current_price: Decimal, expected: bool) -> None:
    plan = {"position_status": "OPEN"}
    assert _simulate_stop_hit("SHORT", current_price, Decimal("100"), Decimal("2"), plan) is expected


def _plan(plan_id: str, symbol: str, direction: str) -> dict:
    entry = "100" if direction == "LONG" else "100"
    stop = "90" if direction == "LONG" else "110"
    return {
        "id": plan_id,
        "symbol": symbol,
        "direction": direction,
        "status": "OPEN",
        "position_status": "OPEN",
        "entry_price": entry,
        "initial_stop_loss": stop,
        "stop_loss": stop,
        "quantity": "2",
        "initial_quantity": "2",
        "remaining_quantity": "2",
        "protection_ids": [101, 102, 103],
        "stop_algo_id": 101,
        "protected_at": "2026-01-01T00:00:00+00:00",
    }


def _snapshot(*positions: tuple[str, str]) -> dict:
    return {
        "positions": [
            {"symbol": symbol, "quantity": quantity}
            for symbol, quantity in positions
        ],
        "actual_exchange_open_positions": sum(
            Decimal(quantity) != 0 for _, quantity in positions
        ),
    }


def _run_simulated_lifecycle(direction: str) -> tuple[dict, dict, NoOrderFakeClient]:
    symbol = f"SL{direction}TEST"
    unrelated_symbol = "UNRELATEDTEST"
    target = _plan("target", symbol, direction)
    unrelated = _plan("unrelated", unrelated_symbol, direction)
    state = {"plans": {target["id"]: target, unrelated["id"]: unrelated}}

    # The open snapshot exercises the existing position lifecycle update.
    binance_demo.reconcile_demo_plans(
        state,
        _snapshot((symbol, "2"), (unrelated_symbol, "3")),
    )
    assert target["remaining_quantity"] == "2"

    assert _simulate_stop_hit(
        direction,
        Decimal("90") if direction == "LONG" else Decimal("110"),
        Decimal(target["stop_loss"]),
        Decimal(target["remaining_quantity"]),
        target,
    )

    # The zero-quantity target is the simulated exchange result after SL fill.
    result = binance_demo.reconcile_demo_plans(
        state,
        _snapshot((symbol, "0"), (unrelated_symbol, "3")),
    )
    assert result["changed"] is True

    client = NoOrderFakeClient()
    return state, target, client


def test_long_lifecycle_closes_plan_and_cleans_owned_protection() -> None:
    state, target, client = _run_simulated_lifecycle("LONG")

    assert target["status"] == "KAPANDI"
    assert target["position_status"] == "CLOSED"
    assert target["remaining_quantity"] == "0"

    import asyncio

    asyncio.run(binance_demo.cleanup_closed_plan(client, target))

    assert target["protection_ids"] == []
    assert [call[2]["algoId"] for call in client.calls] == [101, 102, 103]
    assert "SAGAUSDT" not in state["plans"]
    assert "AVAAIUSDT" not in state["plans"]


def test_short_lifecycle_closes_plan_and_cleans_owned_protection() -> None:
    state, target, client = _run_simulated_lifecycle("SHORT")

    assert target["status"] == "KAPANDI"
    assert target["position_status"] == "CLOSED"
    assert target["remaining_quantity"] == "0"

    import asyncio

    asyncio.run(binance_demo.cleanup_closed_plan(client, target))

    assert target["protection_ids"] == []
    assert [call[2]["algoId"] for call in client.calls] == [101, 102, 103]
    assert "SAGAUSDT" not in state["plans"]
    assert "AVAAIUSDT" not in state["plans"]


def test_closed_lifecycle_is_idempotent_and_unrelated_plan_is_untouched() -> None:
    state, target, client = _run_simulated_lifecycle("LONG")
    unrelated = state["plans"]["unrelated"]
    unrelated_before = dict(unrelated)

    import asyncio

    asyncio.run(binance_demo.cleanup_closed_plan(client, target))
    first_calls = list(client.calls)
    second_result = binance_demo.reconcile_demo_plans(
        state,
        _snapshot(("UNRELATEDTEST", "3")),
    )
    asyncio.run(binance_demo.cleanup_closed_plan(client, target))

    assert second_result["changed"] is False
    assert client.calls == first_calls
    assert target["status"] == "KAPANDI"
    assert target["protection_ids"] == []
    assert {
        key: value for key, value in unrelated.items() if key != "last_reconciled"
    } == {
        key: value for key, value in unrelated_before.items() if key != "last_reconciled"
    }
    assert all(
        symbol not in {"SAGAUSDT", "AVAAIUSDT"}
        for _, _, params in client.calls
        for symbol in [params["symbol"]]
    )


def test_fake_client_rejects_unexpected_order_calls_and_lifecycle_makes_none() -> None:
    state, target, client = _run_simulated_lifecycle("SHORT")

    import asyncio

    asyncio.run(binance_demo.cleanup_closed_plan(client, target))

    assert all(method == "DELETE" for method, _, _ in client.calls)
    assert not any(method == "POST" for method, _, _ in client.calls)
    assert state["plans"]["unrelated"]["position_status"] == "OPEN"
    assert "SAGAUSDT" not in repr(state)
    assert "AVAAIUSDT" not in repr(state)
