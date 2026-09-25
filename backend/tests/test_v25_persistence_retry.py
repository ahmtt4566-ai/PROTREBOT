import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import v25_execution


def retry_plan(plan_id="plan-1", *, retry_count=0):
    return {
        "id": plan_id,
        "symbol": "MUBARAKUSDT",
        "source": "external_adoption",
        "status": "KORUMA AKTİF",
        "provenance_state": "ADOPTED_EXTERNAL",
        "protection_state": "MATCHED",
        "protection_cleanup_state": "CLEAN",
        "persistence_status": "UNKNOWN",
        "persistence_retry_count": retry_count,
        "persistence_next_retry_at": 0.0,
        "persistence_last_error": "previous failure",
        "persistence_retry_exhausted": False,
    }


def retry_snapshot():
    return {
        "positions": [{"symbol": "MUBARAKUSDT"}],
        "open_algo_orders": [],
        "open_algo_orders_available": True,
        "algo_orders_quality": "VALID_EMPTY",
    }


def retry_application(plans):
    state = v25_execution.initial_state()
    state["lock"] = asyncio.Lock()
    state["reconciliation_required"] = True
    state["plans"] = plans
    application = SimpleNamespace(
        state=SimpleNamespace(v25_execution=state, db_pool=None),
    )
    return application, state


def persistence_writer(result):
    async def write(state):
        state["persistence_revision"] += 1
        return result

    return AsyncMock(side_effect=write)


async def run_reconcile(application, persistence_mock, cancel_algos_mock=None):
    client = SimpleNamespace(time_offset_ms=0)
    cancel_algos_mock = cancel_algos_mock or AsyncMock(return_value=0)
    with patch.object(v25_execution, "client_for_with_credentials", return_value=client), patch.object(
        v25_execution, "account_snapshot", new=AsyncMock(return_value=retry_snapshot())
    ), patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), patch.object(
        v25_execution, "cleanup_orphan_protection_orders", new=AsyncMock()
    ), patch.object(v25_execution, "cancel_owned_algos_for_symbol", new=cancel_algos_mock), patch.object(
        v25_execution, "settle_closed_plan", new=AsyncMock()
    ), patch.object(v25_execution, "reconcile_monitoring_targets"), patch.object(
        v25_execution, "classify_plan_protection", return_value=("MATCHED", [], "EXACT_IDENTITY")
    ), patch.object(v25_execution, "live_plan_can_mutate", return_value=True
    ), patch.object(v25_execution, "persist_state"), patch.object(
        v25_execution, "persist_state_awaited", new=persistence_mock
    ):
        async with application.state.v25_execution["lock"]:
            await v25_execution.reconcile(application)


def test_retry_inserted_marks_plan_persisted_and_clears_metadata():
    async def exercise():
        application, state = retry_application({"plan-1": retry_plan()})
        persistence_mock = persistence_writer("INSERTED")
        await run_reconcile(application, persistence_mock)
        return state, persistence_mock

    state, persistence_mock = asyncio.run(exercise())
    plan = state["plans"]["plan-1"]
    assert persistence_mock.await_count == 1
    assert plan["persistence_status"] == "PERSISTED"
    assert plan["persistence_next_retry_at"] is None
    assert plan["persistence_last_error"] is None
    assert plan["persistence_retry_exhausted"] is False
    assert state["reconciliation_required"] is False


def test_retry_updated_marks_plan_persisted_and_clears_metadata():
    async def exercise():
        application, state = retry_application({"plan-1": retry_plan()})
        persistence_mock = persistence_writer("UPDATED")
        await run_reconcile(application, persistence_mock)
        return state, persistence_mock

    state, persistence_mock = asyncio.run(exercise())
    plan = state["plans"]["plan-1"]
    assert persistence_mock.await_count == 1
    assert plan["persistence_status"] == "PERSISTED"
    assert plan["persistence_next_retry_at"] is None
    assert plan["persistence_last_error"] is None
    assert plan["persistence_retry_exhausted"] is False
    assert state["reconciliation_required"] is False


def test_retry_skipped_keeps_unknown_and_advances_backoff():
    async def exercise():
        application, state = retry_application({"plan-1": retry_plan()})
        persistence_mock = persistence_writer("SKIPPED")
        await run_reconcile(application, persistence_mock)
        return state, persistence_mock

    state, persistence_mock = asyncio.run(exercise())
    plan = state["plans"]["plan-1"]
    assert persistence_mock.await_count == 1
    assert plan["persistence_status"] == "UNKNOWN"
    assert plan["persistence_last_error"] == "persistence write returned SKIPPED"
    assert plan["persistence_retry_count"] == 1
    assert plan["persistence_next_retry_at"] is not None
    assert plan["persistence_retry_exhausted"] is False
    assert state["reconciliation_required"] is True


def test_retry_exception_keeps_unknown_and_advances_backoff():
    async def exercise():
        application, state = retry_application({"plan-1": retry_plan()})
        persistence_mock = AsyncMock(side_effect=RuntimeError("database token=secret123"))
        await run_reconcile(application, persistence_mock)
        return state, persistence_mock

    state, persistence_mock = asyncio.run(exercise())
    plan = state["plans"]["plan-1"]
    assert persistence_mock.await_count == 1
    assert plan["persistence_status"] == "UNKNOWN"
    assert plan["persistence_last_error"] == "database token=[REDACTED]"
    assert plan["persistence_retry_count"] == 1
    assert plan["persistence_next_retry_at"] is not None
    assert plan["persistence_retry_exhausted"] is False
    assert state["reconciliation_required"] is True


def test_retry_timeout_keeps_unknown_and_releases_lock(monkeypatch):
    monkeypatch.setattr(v25_execution, "ADOPTION_PERSISTENCE_RETRY_TIMEOUT_SECONDS", 0.1)

    async def slow_write(state):
        await asyncio.sleep(0.15)
        state["persistence_revision"] += 1
        return "UPDATED"

    async def exercise():
        application, state = retry_application({"plan-1": retry_plan()})
        persistence_mock = AsyncMock(side_effect=slow_write)
        await run_reconcile(application, persistence_mock)
        return state, persistence_mock

    state, persistence_mock = asyncio.run(exercise())
    plan = state["plans"]["plan-1"]
    assert persistence_mock.await_count == 1
    assert plan["persistence_status"] == "UNKNOWN"
    assert plan["persistence_last_error"] == "persistence timeout after 0.1s"
    assert plan["persistence_retry_count"] == 1
    assert plan["persistence_next_retry_at"] is not None
    assert state["lock"].locked() is False


def test_retry_exhausted_stops_future_retries_and_emits_event():
    async def exercise():
        application, state = retry_application({"plan-1": retry_plan(retry_count=4)})
        persistence_mock = persistence_writer("SKIPPED")
        await run_reconcile(application, persistence_mock)
        first_events = list(state["events"])
        await run_reconcile(application, persistence_mock)
        return state, persistence_mock, first_events

    state, persistence_mock, first_events = asyncio.run(exercise())
    plan = state["plans"]["plan-1"]
    assert persistence_mock.await_count == 1
    assert plan["persistence_status"] == "UNKNOWN"
    assert plan["persistence_retry_count"] == 5
    assert plan["persistence_retry_exhausted"] is True
    assert plan["persistence_next_retry_at"] is None
    assert any(event["kind"] == v25_execution.ADOPTION_PERSISTENCE_RETRY_EXHAUSTED for event in first_events)


def test_multiple_unknown_plans_use_one_batch_persistence_call_and_one_revision():
    async def exercise():
        plans = {
            plan_id: retry_plan(plan_id)
            for plan_id in ("plan-1", "plan-2", "plan-3")
        }
        application, state = retry_application(plans)
        state["persistence_revision"] = 10
        persistence_mock = persistence_writer("UPDATED")
        await run_reconcile(application, persistence_mock)
        return state, persistence_mock

    state, persistence_mock = asyncio.run(exercise())
    assert persistence_mock.await_count == 1
    assert state["persistence_revision"] == 11
    assert all(plan["persistence_status"] == "PERSISTED" for plan in state["plans"].values())


def test_retry_does_not_call_candidate_builder_or_exchange_mutations():
    async def exercise():
        application, state = retry_application({"plan-1": retry_plan()})
        persistence_mock = persistence_writer("UPDATED")
        with patch.object(v25_execution, "build_external_position_candidate", new=AsyncMock()) as candidate_builder, patch.object(
            v25_execution, "install_protection", new=AsyncMock()
        ) as install_protection, patch.object(
            v25_execution, "cancel_owned_algos_for_symbol", new=AsyncMock()
        ) as cancel_algos, patch.object(
            v25_execution, "set_live_isolated_margin", new=AsyncMock()
        ) as set_margin, patch.object(
            v25_execution, "apply_live_verified_leverage", new=AsyncMock()
        ) as apply_leverage:
            await run_reconcile(application, persistence_mock, cancel_algos_mock=cancel_algos)
        return state, candidate_builder, install_protection, cancel_algos, set_margin, apply_leverage

    state, candidate_builder, install_protection, cancel_algos, set_margin, apply_leverage = asyncio.run(exercise())
    assert state["plans"]["plan-1"]["persistence_status"] == "PERSISTED"
    assert candidate_builder.await_count == 0
    assert install_protection.await_count == 0
    assert cancel_algos.await_count == 0
    assert set_margin.await_count == 0
    assert apply_leverage.await_count == 0