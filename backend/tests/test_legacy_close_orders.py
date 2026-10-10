import asyncio
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import binance_demo, legacy_demo_results, legacy_loss_guard, v21_demo
from test_legacy_demo_results import context, fill, result

RESPONSE = {"orderId": 99, "clientOrderId": "close-a", "symbol": "BTCUSDT", "side": "SELL", "positionSide": "BOTH"}
NOW = datetime(2026, 10, 10, 21, tzinfo=timezone.utc)


class MetadataWriteFailure(dict):
    def __setitem__(self, key, value):
        if key == "legacy_close_orders":
            raise OSError("metadata write failed")
        super().__setitem__(key, value)


@pytest.mark.parametrize("reason", ["SAFE_ROTATION", "APP_MANUAL_CLOSE"])
def test_owned_close_identity_verifies_full_trade_and_survives_restart(reason):
    state, demo, plan = context()
    before = copy.deepcopy(plan)
    assert legacy_demo_results.capture_close_order(demo, "BTCUSDT", "BOTH", RESPONSE, reason)
    assert {key: value for key, value in plan.items() if key != "legacy_close_orders"} == before
    restored_demo = binance_demo._state_from_demo_payload(
        json.loads(json.dumps({"plans": demo["plans"]})), "user-a",
        SimpleNamespace(state=SimpleNamespace()), "TEST", "restored", "none",
        normalize_plans=False,
    )
    legacy_demo_results.observe_stream(state, restored_demo, fill(entry=True))
    legacy_demo_results.observe_stream(state, restored_demo, fill(i=99, c="close-a", trade_id=2, pnl="1"))
    assert result(state)["status"] == "verified"
    assert result(state)["net_pnl"] == "0.8"
    assert result(state)["result"] == "profit"
    assert not legacy_demo_results.capture_close_order(demo, "BTCUSDT", "BOTH", RESPONSE, reason)
    assert len(plan["legacy_close_orders"]) == 1


def test_late_binding_resolves_result_but_does_not_retract_conservative_loss():
    state, demo, plan = context()
    state["risk"]["consecutive_losses"] = 0
    legacy_demo_results.observe_stream(state, demo, fill(entry=True))
    legacy_demo_results.observe_stream(state, demo, fill(i=99, c="close-a", trade_id=2, pnl="10"))
    assert result(state)["status"] == "unverified"
    legacy_loss_guard.update(state, demo, NOW)
    assert state["risk"]["consecutive_losses"] == 1
    assert legacy_demo_results.capture_close_order(demo, "BTCUSDT", "BOTH", RESPONSE, "APP_MANUAL_CLOSE")
    plan["position_status"] = "CLOSED"
    legacy_demo_results.observe_missing_positions(state, demo, None, {"positions": []})
    assert result(state)["status"] == "verified"
    assert result(state)["net_pnl"] == "9.8"
    legacy_loss_guard.update(state, demo, NOW)
    assert state["risk"]["consecutive_losses"] == 1


def test_partial_app_close_waits_for_complete_exit_quantities():
    state, demo, _ = context()
    legacy_demo_results.capture_close_order(demo, "BTCUSDT", "BOTH", RESPONSE, "APP_MANUAL_CLOSE")
    legacy_demo_results.observe_stream(state, demo, fill(entry=True))
    legacy_demo_results.observe_stream(state, demo, fill(i=99, c="close-a", quantity="1", cumulative="1", pnl="2"))
    assert result(state)["net_pnl"] is None
    legacy_demo_results.observe_stream(state, demo, fill(i=99, c="close-a", trade_id=2, quantity="1", cumulative="2", pnl="-3"))
    assert result(state)["status"] == "verified"
    assert result(state)["net_pnl"] == "-1.3"


@pytest.mark.parametrize("fault", [
    "owner", "context", "ambiguous", "mixed-manual", "unconfirmed", "order-id", "symbol",
    "position-side", "direction", "entry-collision", "other-order-collision", "storage",
])
def test_invalid_or_ambiguous_binding_is_logged_and_does_not_mutate_plans(fault, caplog):
    _, demo, plan = context()
    response = dict(RESPONSE)
    if fault == "owner":
        plan["user_id"] = "user-b"
    elif fault == "context":
        del demo["_user_id"]
    elif fault in {"ambiguous", "mixed-manual"}:
        other = copy.deepcopy(plan)
        other["id"] = "plan-b"
        if fault == "mixed-manual":
            other["source"] = "MANUAL"
        demo["plans"]["plan-b"] = other
    elif fault == "unconfirmed":
        plan["provenance_state"] = "UNCONFIRMED"
    elif fault == "order-id":
        response["orderId"] = None
    elif fault == "symbol":
        response["symbol"] = "ETHUSDT"
    elif fault == "position-side":
        response["positionSide"] = "LONG"
    elif fault == "direction":
        response["side"] = "BUY"
    elif fault == "entry-collision":
        response["orderId"] = plan["entry_order_id"]
    elif fault == "other-order-collision":
        other = copy.deepcopy(plan)
        other.update(id="plan-b", symbol="ETHUSDT", entry_order_id=99)
        demo["plans"]["plan-b"] = other
    else:
        plan = MetadataWriteFailure(plan)
        demo["plans"]["plan-a"] = plan
    before = json.dumps(demo, sort_keys=True)
    assert not legacy_demo_results.capture_close_order(demo, "BTCUSDT", "BOTH", response, "APP_MANUAL_CLOSE")
    assert json.dumps(demo, sort_keys=True) == before
    assert "closure remains unverified" in caplog.text


@pytest.mark.parametrize("source,strategy", [("MANUAL", None), ("AUTO_SCANNER", "kais-original-v2-demo-v1")])
def test_original_and_manual_entry_plans_are_unchanged(source, strategy):
    _, demo, plan = context()
    plan.update(source=source, strategy_id=strategy)
    before = copy.deepcopy(demo)
    assert not legacy_demo_results.capture_close_order(demo, "BTCUSDT", "BOTH", RESPONSE, "APP_MANUAL_CLOSE")
    assert demo == before


@pytest.mark.parametrize("failure", [None, "identity", "storage"])
def test_rotation_keeps_close_call_and_lifecycle_when_binding_fails(failure):
    state, demo, plan = context()
    if failure == "storage":
        plan = MetadataWriteFailure(plan)
        demo["plans"]["plan-a"] = plan
    app = SimpleNamespace(state=SimpleNamespace(binance_demo={}, v21_demo=state))
    response = {} if failure == "identity" else RESPONSE
    snapshot = {"positions": [{"symbol": "BTCUSDT", "unrealized_pnl": 1}]}
    async def run():
        with patch.object(v21_demo, "client_for_state", return_value="fake-client"), \
                patch.object(v21_demo, "close_symbol_position", new=AsyncMock(return_value=response)) as close, \
                patch.object(v21_demo, "persist_runtime") as persist, patch.object(v21_demo, "emit_notification"):
            assert await v21_demo.rotate_safe_demo_positions(app, snapshot, set(), demo_state=demo, v21_state=state) == 1
            close.assert_awaited_once_with("fake-client", "BTCUSDT")
            persist.assert_called_once_with(demo)
    asyncio.run(run())
    assert plan["position_status"] == "CLOSED"
    assert plan["close_reason"] == "SAFE_ROTATION"
    assert ("legacy_close_orders" in plan) is (failure is None)


@pytest.mark.parametrize("failure", [None, "identity", "storage"])
def test_app_close_keeps_owner_cleanup_and_response_when_binding_fails(failure):
    _, demo, plan = context()
    if failure == "storage":
        plan = MetadataWriteFailure(plan)
        demo["plans"]["plan-a"] = plan
    app = SimpleNamespace(state=SimpleNamespace())
    request = SimpleNamespace(app=app, state=SimpleNamespace(member={"id": "user-a"}))
    response = {"orderId": None} if failure == "identity" else RESPONSE
    async def run():
        with patch.object(binance_demo, "state_for", return_value=demo), \
                patch.object(binance_demo, "client_for", return_value="fake-client"), \
                patch.object(binance_demo, "close_symbol_position", new=AsyncMock(return_value=response)) as close, \
                patch.object(binance_demo, "cleanup_closed_plan", new=AsyncMock(return_value=True)) as cleanup, \
                patch.object(binance_demo, "persist_runtime") as persist, patch.object(binance_demo, "add_event"):
            payload = await binance_demo.demo_close_position(request, binance_demo.ClosePositionRequest(
                symbol="BTCUSDT", confirmation="DEMO KAPAT",
            ))
            assert payload == {"ok": True, "symbol": "BTCUSDT", "order_id": response["orderId"]}
            close.assert_awaited_once_with("fake-client", "BTCUSDT", "BOTH")
            cleanup.assert_awaited_once()
            assert cleanup.await_args.args[1] is plan
            persist.assert_called_once_with(demo)
    asyncio.run(run())
    assert plan["position_status"] == "CLOSED"
    assert ("legacy_close_orders" in plan) is (failure is None)


def test_app_close_wrong_confirmation_sends_no_order():
    async def run():
        with patch.object(binance_demo, "close_symbol_position", new=AsyncMock()) as close:
            with pytest.raises(binance_demo.HTTPException) as failure:
                await binance_demo.demo_close_position(SimpleNamespace(), binance_demo.ClosePositionRequest(
                    symbol="BTCUSDT", confirmation="WRONG",
                ))
            assert failure.value.status_code == 422
            close.assert_not_awaited()
    asyncio.run(run())
