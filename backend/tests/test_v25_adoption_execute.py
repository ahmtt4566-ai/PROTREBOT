import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app import v25_execution


def adoption_candidate():
    return {
        "would_create_plan": True,
        "candidate_plan": {
            "symbol": "MUBARAKUSDT",
            "direction": "LONG",
            "quantity": "1000",
            "entry_price": "0.1000",
            "stop_loss": {
                "price": "0.0900",
                "close_position": True,
                "quantity": None,
                "algo_id": 101,
            },
            "targets": [{
                "price": "0.1200",
                "close_position": True,
                "quantity": None,
                "algo_id": 102,
            }],
            "protection_ids": [101, 102],
            "protection_schema": "PARTIAL_TARGETS_WITH_CLOSE_ALL_V1",
            "target_coverage": "PARTIAL_PLUS_CLOSE_ALL",
            "leverage": 5,
            "margin_type": "isolated",
            "provenance_state": "ADOPTED_EXTERNAL",
            "source": "external_adoption",
        },
        "confirm_token": "unused-preview-token",
        "matched_protection_orders": [],
        "unmatched_protection_orders": [],
        "warnings": [],
        "state_mutation": False,
    }


def confirm_token(candidate):
    serialized = json.dumps(candidate["candidate_plan"], sort_keys=True, separators=(",", ":"))
    return v25_execution.hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def adoption_request(state, token, *, db_pool=None):
    application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=db_pool))
    request = SimpleNamespace(app=application)
    body = v25_execution.AdoptExternalPositionRequest(
        symbol="MUBARAKUSDT",
        confirm_token=token,
    )
    return request, body


def ready_state(*, real_trading_locked=True):
    state = v25_execution.initial_state()
    state.update({
        "recovery_loaded": True,
        "recovery_ready": True,
        "reconciliation_required": False,
        "real_trading_locked": real_trading_locked,
        "plans": {},
    })
    state["lock"] = asyncio.Lock()
    return state


def active_plan(source):
    return {
        "id": "existing-plan",
        "symbol": "MUBARAKUSDT",
        "source": source,
        "status": "KORUMA AKTİF",
    }


async def call_adoption(state, token, *, candidate=None, persistence="INSERTED", persistence_error=None):
    candidate = candidate or adoption_candidate()
    request, body = adoption_request(state, token)
    fake_client = object()
    persistence_mock = AsyncMock()
    if persistence_error is not None:
        persistence_mock.side_effect = persistence_error
    else:
        persistence_mock.return_value = persistence
    with patch.object(v25_execution, "execution_owner", return_value={"id": "owner-1"}), patch.object(
        v25_execution, "client_for", return_value=fake_client
    ), patch.object(
        v25_execution, "build_external_position_candidate", new=AsyncMock(return_value=candidate)
    ), patch.object(v25_execution, "persist_state_awaited", new=persistence_mock):
        response = await v25_execution.v25_adopt_external_position(request, body)
    return response, persistence_mock, state


def test_execute_adopts_plan_persists_and_emits_event():
    async def exercise():
        state = ready_state()
        candidate = adoption_candidate()
        return await call_adoption(state, confirm_token(candidate), candidate=candidate)

    response, persistence_mock, state = asyncio.run(exercise())
    assert response["ok"] is True
    assert response["persistence_status"] == "PERSISTED"
    assert response["persistence_result"] == "INSERTED"
    assert response["plan"]["persistence_status"] == "PERSISTED"
    assert response["plan"]["mutation_policy"] == "READ_ONLY_EXTERNAL"
    assert any(event["kind"] == "LIVE_PLAN_ADOPTED" for event in state["events"])
    assert persistence_mock.await_count == 1


def test_execute_returns_plan_exists_for_other_active_plan():
    async def exercise():
        state = ready_state()
        state["plans"] = {"existing-plan": active_plan("MANUAL")}
        candidate = adoption_candidate()
        request, body = adoption_request(state, confirm_token(candidate))
        with patch.object(v25_execution, "execution_owner", return_value={"id": "owner-1"}), patch.object(
            v25_execution, "build_external_position_candidate", new=AsyncMock()
        ):
            with pytest.raises(HTTPException) as raised:
                await v25_execution.v25_adopt_external_position(request, body)
        return raised.value

    error = asyncio.run(exercise())
    assert error.status_code == 409
    assert error.detail == "PLAN_EXISTS"


def test_execute_returns_already_adopted_for_existing_external_plan():
    async def exercise():
        state = ready_state()
        state["plans"] = {"existing-plan": active_plan("external_adoption")}
        candidate = adoption_candidate()
        request, body = adoption_request(state, confirm_token(candidate))
        with patch.object(v25_execution, "execution_owner", return_value={"id": "owner-1"}):
            with pytest.raises(HTTPException) as raised:
                await v25_execution.v25_adopt_external_position(request, body)
        return raised.value

    error = asyncio.run(exercise())
    assert error.status_code == 409
    assert error.detail == "ALREADY_ADOPTED"


def test_execute_rejects_token_mismatch_after_fresh_candidate():
    async def exercise():
        state = ready_state()
        request, body = adoption_request(state, "0" * 64)
        candidate = adoption_candidate()
        with patch.object(v25_execution, "execution_owner", return_value={"id": "owner-1"}), patch.object(
            v25_execution, "client_for", return_value=object()
        ), patch.object(
            v25_execution, "build_external_position_candidate", new=AsyncMock(return_value=candidate)
        ):
            with pytest.raises(HTTPException) as raised:
                await v25_execution.v25_adopt_external_position(request, body)
        return raised.value

    error = asyncio.run(exercise())
    assert error.status_code == 409
    assert error.detail == "TOKEN_MISMATCH"


@pytest.mark.parametrize("persistence_error", [None, RuntimeError("database uncertain")])
def test_execute_marks_persistence_uncertain_for_skipped_or_exception(persistence_error):
    async def exercise():
        state = ready_state()
        candidate = adoption_candidate()
        persistence = "SKIPPED" if persistence_error is None else "INSERTED"
        return await call_adoption(
            state,
            confirm_token(candidate),
            candidate=candidate,
            persistence=persistence,
            persistence_error=persistence_error,
        )

    response, _, _ = asyncio.run(exercise())
    assert response["ok"] is False
    assert response["code"] == "ADOPTION_PERSISTENCE_UNCERTAIN"
    assert response["persistence_status"] == "UNKNOWN"
    assert response["plan"]["persistence_status"] == "UNKNOWN"
    assert response["reconciliation_required"] is True


def test_execute_releases_lock_when_endpoint_raises():
    async def exercise():
        state = ready_state()
        request, body = adoption_request(state, "0" * 64)
        with patch.object(v25_execution, "execution_owner", return_value={"id": "owner-1"}), patch.object(
            v25_execution, "client_for", return_value=object()
        ), patch.object(
            v25_execution,
            "build_external_position_candidate",
            new=AsyncMock(side_effect=v25_execution.LiveExchangeError("exchange failed")),
        ):
            with pytest.raises(HTTPException):
                await v25_execution.v25_adopt_external_position(request, body)
        return state["lock"].locked()

    assert asyncio.run(exercise()) is False


def test_adoption_is_allowed_when_real_trading_locked():
    async def exercise():
        state = ready_state(real_trading_locked=True)
        candidate = adoption_candidate()
        return await call_adoption(state, confirm_token(candidate), candidate=candidate)

    response, _, _ = asyncio.run(exercise())
    assert response["ok"] is True
    assert response["persistence_status"] == "PERSISTED"


def test_new_v25_auto_order_is_blocked_when_real_trading_locked():
    async def exercise():
        state = ready_state(real_trading_locked=True)
        state["auto"].update({"enabled": True, "session_until": v25_execution.time.time() + 3600})
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        body = v25_execution.LiveOrderRequest(
            symbol="MUBARAKUSDT",
            direction="LONG",
            margin_usdt=10,
            leverage=2,
            stop_loss=0.09,
            tp1=0.12,
            tp2=0.13,
            tp3=0.14,
        )
        with patch.object(v25_execution, "readiness_for", return_value={"ready": True}), patch.object(
            v25_execution, "account_snapshot", new=AsyncMock(return_value={"hedge_mode": False})
        ), patch.object(v25_execution, "client_for_with_credentials", return_value=object()):
            with pytest.raises(HTTPException) as raised:
                await v25_execution.execute_live_order(
                    application,
                    body,
                    source="V25_AUTO",
                    credentials=("a" * 12, "b" * 12),
                )
        return raised.value

    error = asyncio.run(exercise())
    assert error.status_code == 423
    assert "Gerçek işlem kilidi" in str(error.detail)