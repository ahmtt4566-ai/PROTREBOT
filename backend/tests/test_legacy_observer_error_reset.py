import asyncio
import copy
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import legacy_loss_guard, v21_demo
from test_legacy_demo_results import context

NOW = datetime(2026, 10, 10, 21, tzinfo=timezone.utc)


def fixture():
    state, demo, _ = context()
    legacy_loss_guard.update(state, demo, NOW)
    state["legacy_loss_guard"].update(paused=True, error="ValueError")
    state["legacy_result_observer_errors"] = ["RuntimeError"]
    state["auto"].update(enabled=False, status="PAUSED", pause_reason="CONSECUTIVE_LOSSES")
    return state, demo


async def reset(state, demo, *, confirmation="DEMO HATAYI SIFIRLA", acknowledged=True, armed=True, credentials=True, hedge=False):
    with patch.object(v21_demo, "state_for", return_value=state), \
            patch.object(v21_demo, "demo_state_for", return_value=demo), \
            patch.object(v21_demo, "armed", return_value=armed), \
            patch.object(v21_demo, "credentials_configured", return_value=credentials), \
            patch.object(v21_demo, "client_for", return_value=object()), \
            patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={"hedge_mode": hedge})), \
            patch.object(v21_demo, "summary_payload", side_effect=lambda value: value), \
            patch.object(v21_demo, "datetime") as clock, \
            patch("app.strategies.original_v2_demo.feature_enabled", return_value=False):
        clock.now.return_value = NOW
        return await v21_demo.v21_observer_error_reset(
            SimpleNamespace(), v21_demo.ObserverErrorResetRequest(confirmation=confirmation, acknowledged=acknowledged),
        )


def test_reset_clears_only_errors_and_allows_separate_manual_restart():
    state, demo = fixture()
    before_risk, before_auto, before_results = copy.deepcopy(state["risk"]), copy.deepcopy(state["auto"]), copy.deepcopy(state["legacy_trade_results"])
    with patch.object(v21_demo, "persist_state"):
        asyncio.run(reset(state, demo))
    assert state["legacy_result_observer_errors"] == []
    assert state["legacy_loss_guard"]["error"] is None
    assert state["legacy_loss_guard"]["paused"]
    assert state["risk"] == before_risk
    assert state["auto"] == before_auto
    assert state["legacy_trade_results"] == before_results
    assert state["legacy_result_journal"]["journal"][0]["kind"] == "LEGACY_OBSERVER_ERROR_RESET"
    async def start():
        with patch.object(v21_demo, "persist_state"), patch.object(v21_demo, "datetime") as clock, \
                patch("app.strategies.original_v2_demo.feature_enabled", return_value=False):
            clock.now.return_value = NOW
            assert await v21_demo._legacy_entry_block(state, demo, restart=True) is None
    asyncio.run(start())
    assert not state["legacy_loss_guard"]["paused"]


@pytest.mark.parametrize("fault,status", [
    ("session", 401), ("context", 409), ("confirmation", 422), ("active", 409),
    ("original", 409), ("arm", 423), ("credentials", 412), ("hedge", 409), ("counter", 409),
])
def test_existing_security_and_valid_accounting_are_required(fault, status):
    state, demo = fixture()
    options = {}
    if fault == "session":
        del state["_user_id"]
    elif fault == "context":
        demo["_user_id"] = "user-b"
    elif fault == "confirmation":
        options["confirmation"] = "WRONG"
    elif fault == "active":
        state["auto"]["enabled"] = True
    elif fault == "original":
        state["auto"]["strategy_id"] = "kais-original-v2-demo-v1"
    elif fault == "counter":
        state["risk"]["consecutive_losses"] = "invalid"
    else:
        options[{"arm": "armed", "credentials": "credentials", "hedge": "hedge"}[fault]] = fault == "hedge"
    before = copy.deepcopy(state)
    with patch.object(v21_demo, "persist_state") as persist, pytest.raises(v21_demo.HTTPException) as failure:
        asyncio.run(reset(state, demo, **options))
    assert failure.value.status_code == status
    assert state == before
    persist.assert_not_called()


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_acknowledgement_must_be_a_boolean(value):
    with pytest.raises(ValidationError):
        v21_demo.ObserverErrorResetRequest(confirmation="DEMO HATAYI SIFIRLA", acknowledged=value)


def test_explicit_acknowledgement_is_required():
    state, demo = fixture()
    with pytest.raises(v21_demo.HTTPException) as failure:
        asyncio.run(reset(state, demo, acknowledged=False))
    assert failure.value.status_code == 422
    assert state["legacy_result_observer_errors"]


def test_storage_failure_preserves_error_and_loss_locks():
    state, demo = fixture()
    before = copy.deepcopy(state)
    with patch.object(v21_demo, "persist_state", side_effect=OSError("write failed")), \
            pytest.raises(v21_demo.HTTPException) as failure:
        asyncio.run(reset(state, demo))
    assert failure.value.status_code == 503
    assert state == before


def test_new_error_during_async_storage_is_not_acknowledged_or_lost():
    state, demo = fixture()
    snapshots = []
    class Pool:
        async def execute(self, query, key, payload):
            snapshots.append(payload)
            if len(snapshots) == 1:
                state["_legacy_result_error_generation"] = 1
                state["legacy_result_observer_errors"].append("OSError")
    state["_app"] = SimpleNamespace(state=SimpleNamespace(db_pool=Pool()))
    with pytest.raises(v21_demo.HTTPException) as failure:
        asyncio.run(reset(state, demo))
    assert failure.value.status_code == 409
    assert state["legacy_result_observer_errors"] == ["RuntimeError", "OSError"]
    assert state["legacy_loss_guard"]["paused"]
    assert len(snapshots) == 2


def test_other_user_and_global_state_are_not_changed():
    state, demo = fixture()
    other, _ = fixture()
    other["_user_id"] = "user-b"
    global_state = v21_demo.initial_state()
    before_other, before_global = copy.deepcopy(other), copy.deepcopy(global_state)
    with patch.object(v21_demo, "persist_state"):
        asyncio.run(reset(state, demo))
    assert other == before_other
    assert global_state == before_global
