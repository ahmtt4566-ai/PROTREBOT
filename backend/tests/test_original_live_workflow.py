"""Offline workflow checks; no exchange transport or real orders."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import account_settings, exchange_connections as connections
from app import original_live_risk as risk, original_v2_live as original, v25_execution as live


@pytest.mark.parametrize("model", [live.Confirmation, live.AutoStartConfirmation, original.Start, risk.RiskUpdate, live.PolicyUpdate])
@pytest.mark.parametrize("value", ["true", 1, None])
def test_understanding_must_be_a_real_boolean(model, value):
    with pytest.raises(ValidationError):
        model(understood=value)


@pytest.mark.parametrize("understood,enabled,status", [(False, True, 422), (True, False, 403), (True, True, None)])
def test_normal_understanding_preserves_site_mfa_requirement_without_consuming_totp(monkeypatch, understood, enabled, status):
    request = SimpleNamespace()
    owner = {"id": "owner", "role": "OWNER"}
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=enabled))
    verifier = AsyncMock(side_effect=AssertionError("ordinary setup must not consume TOTP"))
    monkeypatch.setattr(account_settings, "require_totp", verifier)
    if status:
        with pytest.raises(HTTPException) as error:
            asyncio.run(live.require_live_understanding(request, owner, understood))
        assert error.value.status_code == status
    else:
        asyncio.run(live.require_live_understanding(request, owner, understood))
    verifier.assert_not_awaited()


@pytest.mark.parametrize("code", [None, "123456"])
def test_live_key_save_requires_current_mfa_before_transport_or_credential_write(monkeypatch, code):
    owner = {"id": "owner", "role": "OWNER"}
    pool = SimpleNamespace(execute=AsyncMock())
    request = SimpleNamespace(app=SimpleNamespace(), state=SimpleNamespace(member=owner),
                              headers={"x-protrebot-session": "offline-session"})
    monkeypatch.setattr(connections, "_require_ready", AsyncMock(return_value=pool))
    monkeypatch.setattr(connections, "ensure_schema", AsyncMock())
    monkeypatch.setattr(account_settings, "enabled", AsyncMock(return_value=True))
    verifier = AsyncMock(side_effect=HTTPException(401, "invalid or replayed TOTP"))
    monkeypatch.setattr(account_settings, "require_totp", verifier)
    transport = AsyncMock()
    monkeypatch.setattr(connections, "_verify_request_credentials", transport)
    body = connections.SaveCredentialsRequest(
        mode="LIVE", api_key="offline-key-value-long", secret_key="offline-secret-value-long",
        confirmation="CANLI KASAYA KAYDET", totp_code=code,
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(connections.exchange_connection_save(request, body))
    assert error.value.status_code == 401
    transport.assert_not_awaited()
    pool.execute.assert_not_awaited()
    assert verifier.await_count == (1 if code else 0)


@pytest.mark.parametrize("operation", ["deactivate", "delete"])
@pytest.mark.parametrize("role", ["OWNER", "CUSTOMER"])
def test_connection_removal_still_checks_owner_without_repeating_mfa(monkeypatch, operation, role):
    pool = SimpleNamespace(execute=AsyncMock())
    request = SimpleNamespace(app=SimpleNamespace(), state=SimpleNamespace(member={"id": "owner", "role": role}),
                              headers={"x-protrebot-session": "offline-session"})
    monkeypatch.setattr(connections, "_require_ready", AsyncMock(return_value=pool))
    monkeypatch.setattr(connections, "ensure_session_cache", AsyncMock())
    monkeypatch.setattr(connections, "_SESSION_META", {})
    monkeypatch.setattr(connections, "_SESSION_CACHE", {})
    monkeypatch.setattr(connections, "session_credentials", Mock(return_value=(None, None)))
    monkeypatch.setattr(connections, "_lock_runtime", Mock())
    monkeypatch.setattr(connections, "session_public_status", Mock(return_value={}))
    mfa = AsyncMock(side_effect=AssertionError("removal must not require TOTP"))
    monkeypatch.setattr(connections, "_require_live_mfa", mfa)
    body = connections.ConnectionActionRequest(
        mode="LIVE", confirmation="BAĞLANTIYI KAPAT" if operation == "deactivate" else "ANAHTARI KALICI SİL",
    )
    handler = connections.exchange_connection_deactivate if operation == "deactivate" else connections.exchange_connection_delete
    if role == "CUSTOMER":
        with pytest.raises(HTTPException) as error:
            asyncio.run(handler(request, body))
        assert error.value.status_code == 403
        pool.execute.assert_not_awaited()
    else:
        asyncio.run(handler(request, body))
        pool.execute.assert_awaited_once()
    mfa.assert_not_awaited()


def protection_fixture():
    plan = {
        "symbol": "BTCUSDT", "direction": "LONG", "intent_id": "offline-protection",
        "provenance_state": "CONFIRMED", "stop_loss": "90", "stop_algo_id": 1,
        "stop_client_id": live.client_id_for("SL", "offline-protection"),
        "targets": ["110", "115", "120"], "initial_quantity": "1", "quantity": "1", "step": "0.01",
    }
    common = {"symbol": "BTCUSDT", "side": "SELL", "status": "NEW", "working_type": "MARK_PRICE"}
    rows = [
        {**common, "algo_id": 1, "client_algo_id": plan["stop_client_id"], "type": "STOP_MARKET",
         "trigger_price": "90", "close_position": True},
        {**common, "algo_id": 2, "client_algo_id": live.client_id_for("TP12", plan["intent_id"]),
         "type": "TAKE_PROFIT_MARKET", "trigger_price": "110", "quantity": "0.60", "reduce_only": True},
        {**common, "algo_id": 3, "client_algo_id": live.client_id_for("TP3", plan["intent_id"]),
         "type": "TAKE_PROFIT_MARKET", "trigger_price": "120", "close_position": True},
    ]
    return plan, {"open_algo_orders_available": True, "open_algo_orders": rows}


def test_protection_confirmation_matches_each_target_without_mutation():
    plan, snapshot = protection_fixture()
    before = copy.deepcopy((plan, snapshot))
    assert live.exchange_protection_view(plan, snapshot) == {"stop": True, "tp1": True, "tp3": True}
    assert (plan, snapshot) == before
    snapshot["open_algo_orders"] = snapshot["open_algo_orders"][:1]
    assert live.exchange_protection_view(plan, snapshot) == {"stop": True, "tp1": False, "tp3": False}


@pytest.mark.parametrize("change", [
    {"trigger_price": "111"}, {"quantity": "0.59"}, {"side": "BUY"},
    {"status": "CANCELED"}, {"working_type": "CONTRACT_PRICE"}, {"reduce_only": False},
    {"client_algo_id": "unrelated-client"},
])
def test_tp1_cannot_be_confirmed_by_wrong_parameters_or_identity(change):
    plan, snapshot = protection_fixture()
    snapshot["open_algo_orders"][1].update(change)
    assert live.exchange_protection_view(plan, snapshot) == {"stop": True, "tp1": False, "tp3": True}


@pytest.mark.parametrize("reason", ["unavailable", "external", "duplicate", "invalid-direction"])
def test_protection_confirmation_fails_closed_when_evidence_is_not_exact(reason):
    plan, snapshot = protection_fixture()
    if reason == "unavailable":
        snapshot["open_algo_orders_available"] = False
    elif reason == "external":
        plan["provenance_state"] = "READ_ONLY_EXTERNAL"
    elif reason == "invalid-direction":
        plan["direction"] = "UNKNOWN"
    else:
        snapshot["open_algo_orders"].append(copy.deepcopy(snapshot["open_algo_orders"][1]))
        assert live.exchange_protection_view(plan, snapshot) == {"stop": True, "tp1": False, "tp3": True}
        return
    assert live.exchange_protection_view(plan, snapshot) == {"stop": False, "tp1": False, "tp3": False}
