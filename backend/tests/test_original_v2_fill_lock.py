"""Post-fill risk checks and OWNER-only durable acknowledgment; offline only."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException

from app import account_settings, original_v2_live as original, v25_execution as live
from test_original_v2_live import native_plan, request_for, state
from test_v25_partial_fills import PartialFillClient


def run(value):
    return asyncio.run(value)


@pytest.mark.parametrize("entry,qty,stop,budget,violation", [
    ("51000","0.010","47000","100","%6"),
    ("50000","0.010","49000","5","risk bütçesi"),
    ("50000","0.000240","49000","100","5 USDT"),
])
def test_violation_latches_and_protection_is_unchanged(state, entry, qty, stop, budget, violation):
    plan = native_plan(quantity=qty, initial_quantity=qty, step="0.000001", min_qty="0.000001",
                       initial_stop_loss=stop, stop_loss=stop, original_risk_budget=budget)
    client = PartialFillClient(plan, quantity=qty)
    client.order["origQty"] = qty
    signed = client.signed
    async def actual(method, path, params=None):
        value = await signed(method, path, params)
        return [{**row, "entryPrice":entry} for row in value] if path == "/fapi/v3/positionRisk" else value
    client.signed = actual
    run(live.install_protection(client, state, plan))
    assert original.fill_locked(state)
    assert not original.running(state) and not state["auto"]["enabled"]
    assert state["real_trading_locked"] and state["armed_until"] == 0
    assert any(violation in reason for reason in state["original_v2_live"]["fill_lock"]["violations"])
    assert client.quantity == qty
    assert client.algos[0]["triggerPrice"] == stop
    assert client.algos[1]["triggerPrice"] == plan["targets"][0]
    assert client.algos[2]["triggerPrice"] == plan["targets"][2]
    assert client.algos[2]["closePosition"] == "true"
    assert not any(path == "/fapi/v1/order" and method == "POST" for method,path,_ in client.calls)
    assert state["original_v2_live"]["alerts"][0]["read"] is False
    assert state["original_v2_live"]["alerts"][0]["message"].endswith("Açık pozisyon otomatik kapatılmadı.")


@pytest.mark.parametrize("stop,qty,budget,locked", [
    ("47000","0.001","3",False),  # exact 6%, exact budget, margin > 5
    ("46999.99","0.001","100",True),
    ("49000","0.0003","0.3",False),  # exact 5 USDT margin, exact risk
    ("49000","0.00029999","100",True),
    ("49000","0.001","0.999999",True),
])
def test_exact_thresholds_without_relaxation(state, stop, qty, budget, locked):
    plan = native_plan(initial_stop_loss=stop, original_risk_budget=budget, original_cost_bps=0)
    before = copy.deepcopy(plan)
    run(original.audit_fill(state, plan, {"entry_price":"50000","quantity":qty}))
    assert original.fill_locked(state) is locked
    assert {key:plan[key] for key in before} == before


@pytest.mark.parametrize("field,value", [("entry_price",None),("entry_price","NaN"),("quantity","0")])
def test_invalid_fill_evidence_fails_closed(state, field, value):
    position = {"entry_price":"50000","quantity":"0.010",field:value}
    run(original.audit_fill(state, native_plan(), position))
    assert original.fill_locked(state)
    assert "kanıtı" in state["original_v2_live"]["fill_lock"]["violations"][0]


def test_tp_remainder_and_restart_never_reallocate_or_clear_latch(state):
    plan = native_plan(original_risk_budget=5)
    run(original.audit_fill(state, plan, {"entry_price":"50000","quantity":"0.010"}))
    lock = copy.deepcopy(state["original_v2_live"]["fill_lock"])
    alerts = copy.deepcopy(state["original_v2_live"]["alerts"])
    run(original.audit_fill(state, plan, {"entry_price":"50000","quantity":"0.004"}))
    assert state["original_v2_live"]["fill_lock"] == lock
    assert state["original_v2_live"]["alerts"] == alerts
    restored = live.sanitized_state(state)
    assert original.fill_locked(restored)
    assert restored["original_v2_live"]["fill_lock"] == lock
    assert not live.live_auto_start_gate(object(), restored)[0]


def test_risk_includes_costs_and_short_stop_distance(state):
    plan = native_plan(direction="SHORT", initial_stop_loss="53000",
                       original_risk_budget="3", original_cost_bps="20")
    run(original.audit_fill(state, plan, {"entry_price":"50000", "quantity":"0.001"}))
    lock = state["original_v2_live"]["fill_lock"]
    assert lock["evidence"]["stop_percent"] == "6"
    assert lock["evidence"]["risk_usdt"] == "3.1"
    assert lock["violations"] == ["Dolum riski işlem risk bütçesini aştı"]


def test_missing_legacy_cost_evidence_fails_closed_without_altering_plan(state):
    plan = native_plan(original_tp_attempted=True)
    plan.pop("original_cost_bps")
    run(original.audit_fill(state, plan, {"entry_price":"50000", "quantity":"0.004"}))
    assert original.fill_locked(state)
    assert plan["initial_quantity"] == "0.010"
    assert plan["original_tp_attempted"] is True


def test_recovered_intent_cannot_treat_requested_quantity_as_settled_fill(state):
    plan = native_plan(recovered_after_restart=True, original_tp_attempted=True)
    run(original.audit_fill(state, plan, {"entry_price":"50000", "quantity":"0.000240"}))
    assert original.fill_locked(state)
    assert plan["original_fill_audit"]["violations"] == ["Gerçek dolum risk kanıtı doğrulanamadı"]
    assert plan["initial_quantity"] == "0.010"


def test_failed_audit_persistence_keeps_latch_and_retries(state):
    plan = native_plan(original_risk_budget=5)
    live.persist_state_awaited.return_value = "SKIPPED"
    with pytest.raises(live.LiveExchangeError):
        run(original.audit_fill(state, plan, {"entry_price":"50000", "quantity":"0.010"}))
    assert original.fill_locked(state) and not original.running(state)
    assert "original_fill_audit" not in plan
    live.persist_state_awaited.return_value = "UPDATED"
    run(original.audit_fill(state, plan, {"entry_price":"50000", "quantity":"0.010"}))
    assert plan["original_fill_audit"]["violations"]
    assert live.persist_state_awaited.await_count == 2


def test_recovery_audits_final_settled_quantity_not_pending_partial_fill(state, monkeypatch):
    plan = native_plan(entry_order_status="PARTIALLY_FILLED", step="0.000001", min_qty="0.000001")
    client = PartialFillClient(plan, quantity="0.000240", final_quantity="0.000300")
    client.algos = [{
        "symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET",
        "closePosition": "true", "triggerPrice": "49000", "workingType": "MARK_PRICE",
        "clientAlgoId": live.client_id_for("SL", plan["intent_id"]),
    }]
    plan.update(stop_algo_id=101, stop_client_id=client.algos[0]["clientAlgoId"])
    state["plans"] = {plan["id"]: plan}
    monkeypatch.setattr(live, "client_for_with_credentials", Mock(return_value=client))
    monkeypatch.setattr(live, "account_snapshot", AsyncMock(side_effect=lambda *_: client.snapshot()))
    monkeypatch.setattr(live, "recover_orphan_plans", AsyncMock(return_value=0))
    monkeypatch.setattr(live, "cleanup_orphan_protection_orders", AsyncMock())
    run(live.reconcile(request_for(state).app))
    assert not original.fill_locked(state)
    assert plan["original_fill_audit"]["margin_usdt"] == "5"
    assert plan["initial_quantity"] == "0.0003"
    assert plan["entry_cancellation_state"] == "CANCELLED"
    assert client.algos[1]["quantity"] == "0.00018"


@pytest.fixture
def locked_context(state, monkeypatch):
    plan = native_plan(original_risk_budget=5)
    run(original.audit_fill(state, plan, {"entry_price":"50000","quantity":"0.010"}))
    request = request_for(state)
    monkeypatch.setattr(original, "authenticated_user", Mock(return_value={"id":"owner","role":"OWNER"}))
    monkeypatch.setattr(live, "guard_execution_ownership", Mock())
    monkeypatch.setattr(account_settings, "require_totp", AsyncMock())
    return request, state, original.FillLockApproval(
        lock_id=state["original_v2_live"]["fill_lock"]["id"],
        confirmation="DOLUM RİSKİNİ İNCELEDİM", totp_code="123456",
    )


def test_only_explicit_owner_mfa_approval_clears_lock_without_starting(locked_context):
    request,state,body = locked_context
    response = run(original.approve_fill_lock(request, body))
    account_settings.require_totp.assert_awaited_once_with(request, {"id":"owner","role":"OWNER"}, "123456", required=True)
    assert response["fill_lock"]["active"] is False
    assert response["fill_lock"]["approved_by"] == "owner"
    assert not response["enabled"] and state["real_trading_locked"] and state["armed_until"] == 0
    assert not state["auto"]["enabled"]


@pytest.mark.parametrize("failure", ["customer","owner-mismatch","phrase","stale-lock","invalid-mfa","persistence"])
def test_failed_approval_cannot_clear_lock(locked_context, failure):
    request,state,body = locked_context
    if failure == "customer":
        original.authenticated_user.side_effect = HTTPException(403,"OWNER required")
    elif failure == "owner-mismatch":
        original.authenticated_user.return_value = {"id":"other-owner","role":"OWNER"}
    elif failure == "phrase":
        body.confirmation = "wrong"
    elif failure == "stale-lock":
        body.lock_id = "old-lock"
    elif failure == "invalid-mfa":
        account_settings.require_totp.side_effect = HTTPException(401,"invalid MFA")
    else:
        live.persist_state_awaited.return_value = "SKIPPED"
    with pytest.raises((HTTPException,live.LiveExchangeError)):
        run(original.approve_fill_lock(request,body))
    assert original.fill_locked(state) and not state["auto"]["enabled"]


def test_original_start_cannot_clear_lock_even_with_arm_and_valid_mfa(locked_context, monkeypatch):
    request,state,_ = locked_context
    monkeypatch.setattr(account_settings,"enabled",AsyncMock(return_value=True))
    monkeypatch.setattr(live,"live_auto_start_gate",Mock(return_value=(True,"ready")))
    with pytest.raises(HTTPException) as error:
        run(original.start(request,original.Start(confirmation="ORIGINAL V2 CANLI",totp_code="123456")))
    assert error.value.status_code == 423
    assert original.fill_locked(state)
    account_settings.require_totp.assert_not_awaited()


def test_new_violation_during_mfa_invalidates_previous_approval(locked_context):
    request, state, body = locked_context
    async def new_violation(*args, **kwargs):
        await original.audit_fill(state, native_plan(id="second-plan", original_risk_budget=1),
                                  {"entry_price":"50000", "quantity":"0.010"})
    account_settings.require_totp.side_effect = new_violation
    with pytest.raises(HTTPException) as error:
        run(original.approve_fill_lock(request, body))
    assert error.value.status_code == 409
    assert original.fill_locked(state)
    assert state["original_v2_live"]["fill_lock"]["id"] != body.lock_id


@pytest.mark.parametrize("failure", [False, True])
def test_new_latch_during_approval_persistence_cannot_be_erased(locked_context, failure):
    request, state, body = locked_context
    async def persist(_state):
        state["original_v2_live"]["fill_lock"] = {
            **state["original_v2_live"]["fill_lock"], "id": "new-violation", "active": True,
        }
        return "SKIPPED" if failure else "UPDATED"
    live.persist_state_awaited.side_effect = persist
    with pytest.raises(HTTPException) as error:
        run(original.approve_fill_lock(request, body))
    assert error.value.status_code == (503 if failure else 409)
    assert original.fill_locked(state)
    assert state["original_v2_live"]["fill_lock"]["id"] == "new-violation"


def test_cancelled_approval_restores_active_latch(locked_context):
    request, state, body = locked_context
    live.persist_state_awaited.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run(original.approve_fill_lock(request, body))
    assert original.fill_locked(state)
    assert state["original_v2_live"]["fill_lock"]["id"] == body.lock_id


@pytest.mark.parametrize("source",["MANUAL","V25_AUTO",original.SOURCE])
def test_all_live_entry_helpers_are_blocked_but_not_position_management(locked_context, source):
    request,state,_ = locked_context
    request.app.state.http = SimpleNamespace(request=AsyncMock())
    with pytest.raises(HTTPException) as error:
        run(live.execute_live_order(request.app,None,source=source))
    assert error.value.status_code == 423
    request.app.state.http.request.assert_not_awaited()


def test_native_transport_blocks_entry_but_allows_reduceonly_and_tp(locked_context):
    _,state,_ = locked_context
    http = SimpleNamespace(request=AsyncMock(return_value=httpx.Response(
        200,json={"orderId":1},request=httpx.Request("POST","https://offline.invalid"))))
    client = live.BinanceLiveClient(http,"offline-key","offline-secret",diagnostic_state=state)
    with pytest.raises(live.LiveExchangeError) as error:
        run(client._request("POST","/fapi/v1/order",{"type":"MARKET"},signed=True))
    assert error.value.http_status == 423
    http.request.assert_not_awaited()
    run(client._request("POST","/fapi/v1/order",{"reduceOnly":"true"},signed=True))
    run(client._request("POST","/fapi/v1/algoOrder",{"type":"TAKE_PROFIT_MARKET","closePosition":"true"},signed=True))
    assert http.request.await_count == 2
