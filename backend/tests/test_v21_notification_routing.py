import asyncio
import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import v21_demo


def user(uid):
    state = v21_demo.initial_state()
    state["_user_id"] = uid
    return state


async def scan(app, recipient=None):
    with patch.object(v21_demo, "market_client_for", return_value=object()), \
            patch.object(v21_demo, "credentials_configured", return_value=False), \
            patch.object(v21_demo, "scan_demo_universe", new=AsyncMock(return_value=[])), \
            patch.object(v21_demo, "persist_state"):
        await v21_demo.run_scanner_cycle(app, notification_state=recipient)


def test_manual_scan_notifies_only_requesting_user_and_persists_that_user():
    a, b = user("user-a"), user("user-b")
    global_state = v21_demo.initial_state()
    app = SimpleNamespace(state=SimpleNamespace(v21_demo=global_state))
    async def run():
        with patch.object(v21_demo, "state_for", return_value=a), \
                patch.object(v21_demo, "market_client_for", return_value=object()), \
                patch.object(v21_demo, "credentials_configured", return_value=False), \
                patch.object(v21_demo, "scan_demo_universe", new=AsyncMock(return_value=[])), \
                patch.object(v21_demo, "persist_state") as persist:
            await v21_demo.v21_manual_scan(SimpleNamespace(app=app), v21_demo.ScannerScanRequest())
            assert any(call.args[0] is a for call in persist.call_args_list)
    asyncio.run(run())
    assert [item["type"] for item in v21_demo.notification_payload(a)] == ["SCAN_STARTED"]
    assert v21_demo.notification_payload(b) == []
    assert v21_demo.notification_payload(global_state) == []


def test_global_scan_is_not_broadcast_to_any_user():
    a, b = user("user-a"), user("user-b")
    global_state = v21_demo.initial_state()
    app = SimpleNamespace(state=SimpleNamespace(v21_demo=global_state, _v21_demo_user_state={"user-a": a, "user-b": b}))
    asyncio.run(scan(app))
    assert [item["type"] for item in v21_demo.notification_payload(global_state)] == ["SCAN_STARTED"]
    assert v21_demo.notification_payload(a) == v21_demo.notification_payload(b) == []


def test_each_user_receives_own_scan_even_in_same_time_bucket():
    a, b = user("user-a"), user("user-b")
    app = SimpleNamespace(state=SimpleNamespace(v21_demo=v21_demo.initial_state()))
    async def run():
        with patch.object(v21_demo.time, "time", return_value=1700000000):
            await scan(app, a)
            await scan(app, b)
            await scan(app, a)
    asyncio.run(run())
    assert len(v21_demo.notification_payload(a)) == len(v21_demo.notification_payload(b)) == 1


def test_unscoped_recipient_is_logged_without_user_notification(caplog):
    recipient = v21_demo.initial_state()
    app = SimpleNamespace(state=SimpleNamespace(v21_demo=v21_demo.initial_state()))
    asyncio.run(scan(app, recipient))
    assert v21_demo.notification_payload(recipient) == []
    assert "has no user context" in caplog.text


def test_scan_decisions_are_identical_with_and_without_user_notification():
    global_app = SimpleNamespace(state=SimpleNamespace(v21_demo=v21_demo.initial_state()))
    user_app = SimpleNamespace(state=SimpleNamespace(v21_demo=v21_demo.initial_state()))
    async def run():
        with patch.object(v21_demo.time, "time", return_value=1700000000), \
                patch.object(v21_demo.time, "perf_counter", return_value=10), \
                patch.object(v21_demo, "now_iso", return_value="2026-10-10T00:00:00+00:00"):
            await scan(global_app)
            await scan(user_app, user("user-a"))
    asyncio.run(run())
    assert global_app.state.v21_demo["scanner"] == user_app.state.v21_demo["scanner"]
    assert global_app.state.v21_demo["auto"] == user_app.state.v21_demo["auto"]


def test_user_notification_storage_failure_does_not_abort_scan(caplog):
    a = user("user-a")
    app = SimpleNamespace(state=SimpleNamespace(v21_demo=v21_demo.initial_state()))
    async def run():
        def persist(state):
            if state is a:
                raise OSError("user snapshot failed")
        with patch.object(v21_demo, "market_client_for", return_value=object()), \
                patch.object(v21_demo, "credentials_configured", return_value=False), \
                patch.object(v21_demo, "scan_demo_universe", new=AsyncMock(return_value=[])) as scanner, \
                patch.object(v21_demo, "persist_state", side_effect=persist):
            await v21_demo.run_scanner_cycle(app, notification_state=a)
            scanner.assert_awaited_once()
    asyncio.run(run())
    assert v21_demo.notification_payload(a) == []
    assert not app.state.v21_demo["scanner"]["running"]
    assert "Scanner Demo notification failed" in caplog.text


def test_start_stop_repeats_do_not_notify_but_real_restart_does_even_same_second():
    state = user("user-a")
    app = SimpleNamespace(state=SimpleNamespace(v21_demo=state, binance_demo={}))
    request = SimpleNamespace(app=app)
    async def run():
        with patch.object(v21_demo, "state_for", return_value=state), \
                patch.object(v21_demo, "demo_state_for", return_value={}), \
                patch.object(v21_demo, "armed", return_value=True), \
                patch.object(v21_demo, "credentials_configured", return_value=True), \
                patch.object(v21_demo, "client_for", return_value=object()), \
                patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={})), \
                patch.object(v21_demo, "bind_demo_grant"), patch.object(v21_demo, "persist_state"), \
                patch.object(v21_demo, "summary_payload", side_effect=lambda value: value), \
                patch.object(v21_demo, "ensure_automation_task"), \
                patch("app.strategies.original_v2_demo.feature_enabled", return_value=False), \
                patch.object(v21_demo, "automatic_cycle", new=AsyncMock()) as cycle, \
                patch.object(v21_demo.time, "time", return_value=1700000000):
            body = v21_demo.AutoStartRequest(confirmation="DEMO OTOMATİK")
            await v21_demo.v21_auto_stop(request)
            assert v21_demo.notification_payload(state) == []
            await v21_demo.v21_auto_start(request, body)
            await v21_demo.v21_auto_start(request, body)
            assert len(v21_demo.notification_payload(state)) == 1
            await v21_demo.v21_auto_stop(request)
            await v21_demo.v21_auto_stop(request)
            assert len(v21_demo.notification_payload(state)) == 2
            await v21_demo.v21_auto_start(request, body)
            items = v21_demo.notification_payload(state)
            assert [item["type"] for item in items] == ["AUTO_STARTED", "AUTO_STOPPED", "AUTO_STARTED"]
            assert len({item["id"] for item in items}) == 3
            assert cycle.await_count == 3
    asyncio.run(run())


def test_notification_sessions_are_preserved_across_state_restore():
    state = user("user-a")
    state["auto"]["enabled"] = True
    with patch.object(v21_demo, "persist_state"):
        v21_demo._auto_transition_notification(state, False)
    restored = v21_demo._state_from_payload(copy.deepcopy(v21_demo.serializable_state(state)), "user-a")
    assert restored["notifications"]["auto_notification_session"] == state["notifications"]["auto_notification_session"]
    restored["auto"]["enabled"] = True
    with patch.object(v21_demo, "persist_state"):
        v21_demo._auto_transition_notification(restored, False)
    assert len(v21_demo.notification_payload(restored)) == 2


@pytest.mark.parametrize("enabled", [True, False])
def test_transition_notification_failures_do_not_change_automation_state(enabled, caplog):
    state = user("user-a")
    state["auto"].update(enabled=enabled, status="ON" if enabled else "OFF")
    before = copy.deepcopy(state["auto"])
    with patch.object(v21_demo, "persist_state", side_effect=OSError("write failed")):
        v21_demo._auto_transition_notification(state, not enabled)
    assert state["auto"] == before
    assert v21_demo.notification_payload(state) == []
    assert "Auto Demo notification failed" in caplog.text
