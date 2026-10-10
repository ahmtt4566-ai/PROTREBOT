import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import v21_demo


def user_state(uid="user-a", active=True):
    state = v21_demo.initial_state()
    state["_user_id"] = uid
    state["auto"].update(enabled=active, user_confirmed=active, status="ON" if active else "OFF")
    return state


@pytest.mark.parametrize("kind,target", [
    ("ACİL KORUMA", "risk-management"),
    ("AUTO_LOOP_CRASH", "execution-status"),
    ("AUTO_START_ERROR", "execution-status"),
])
def test_critical_notification_is_deduplicated_and_persisted(kind, target):
    state = user_state()
    with patch.object(v21_demo, "persist_state") as persist:
        for _ in range(2):
            v21_demo.emit_critical_notification(state, kind, "Failure", event_id="one-failure")
    items = v21_demo.notification_payload(state)
    assert len(items) == 1
    assert items[0]["severity"] == "critical"
    assert items[0]["target"] == target
    assert not items[0]["read"]
    persist.assert_called_once_with(state)


@pytest.mark.parametrize("failure", ["emit", "record", "persist", "snapshot"])
def test_notification_failure_is_logged_and_does_not_poison_retry(failure, caplog):
    state = user_state()
    original_record = v21_demo.record_event

    def fail_notification_record(*args, **kwargs):
        if args[1] == "NOTIFICATION":
            raise RuntimeError("record failed")
        return original_record(*args, **kwargs)

    targets = {
        "emit": (v21_demo, "emit_notification", {"side_effect": RuntimeError("emit failed")}),
        "record": (v21_demo, "record_event", {"side_effect": fail_notification_record}),
        "persist": (v21_demo, "persist_state", {"side_effect": OSError("storage failed")}),
        "snapshot": (v21_demo.copy, "deepcopy", {"side_effect": RuntimeError("snapshot failed")}),
    }
    obj, name, kwargs = targets[failure]
    with patch.object(obj, name, **kwargs):
        v21_demo.emit_critical_notification(state, "ACİL KORUMA", "Failure", event_id="retry")
    assert "Critical Demo notification failed" in caplog.text
    assert state["journal"] == []
    assert state["notifications"]["seen"] == []
    with patch.object(v21_demo, "persist_state"):
        v21_demo.emit_critical_notification(state, "ACİL KORUMA", "Failure", event_id="retry")
    assert len(v21_demo.notification_payload(state)) == 1


@pytest.mark.parametrize("notification_failure", [None, "emit", "record", "persist"])
def test_failed_stop_repair_still_closes_position_when_notification_fails(notification_failure):
    state = user_state()
    plan = {
        "id": "plan-a", "user_id": "user-a", "symbol": "BTCUSDT",
        "status": "AÇIK", "stop_loss": "90", "provenance_state": "CONFIRMED",
        "protection_ids": [101], "stop_algo_id": 101,
    }
    demo = {"_user_id": "user-a", "plans": {"plan-a": plan}}
    app = SimpleNamespace(state=SimpleNamespace(v21_demo=v21_demo.initial_state(), binance_demo={}))
    snapshot = {"positions": [{"symbol": "BTCUSDT", "direction": "LONG"}], "open_algo_orders": []}
    client = SimpleNamespace(signed=AsyncMock(return_value=[]))
    original_record = v21_demo.record_event
    original_emit = v21_demo.emit_notification

    def record(*args, **kwargs):
        if notification_failure == "record" and args[1] == "NOTIFICATION":
            raise RuntimeError("notification record failed")
        return original_record(*args, **kwargs)

    def emit(*args, **kwargs):
        if notification_failure == "emit":
            raise RuntimeError("notification failed")
        return original_emit(*args, **kwargs)

    with patch.object(v21_demo, "post_algo", new=AsyncMock(side_effect=v21_demo.BinanceDemoError("repair failed"))) as repair, \
            patch.object(v21_demo, "close_symbol_position", new=AsyncMock()) as close, \
            patch.object(v21_demo, "persist_runtime") as persist_runtime, \
            patch.object(v21_demo, "persist_state", side_effect=OSError("storage failed") if notification_failure == "persist" else None), \
            patch.object(v21_demo, "record_event", side_effect=record), \
            patch.object(v21_demo, "emit_notification", side_effect=emit):
        changed = asyncio.run(v21_demo.ensure_stop_protection(
            app, snapshot, demo_state=demo, v21_state=state, client=client,
        ))
    assert changed
    repair.assert_awaited_once()
    assert repair.await_args.args[1]["type"] == "STOP_MARKET"
    close.assert_awaited_once_with(client, "BTCUSDT", "BOTH")
    client.signed.assert_awaited_once_with("GET", "/fapi/v3/positionRisk", {"symbol": "BTCUSDT"})
    assert plan["position_status"] == "CLOSED"
    assert plan["status"] == "GÜVENLİK İÇİN KAPATILDI"
    persist_runtime.assert_called_once_with(demo)
    assert state["journal"][-1]["kind"] == "ACİL KORUMA"
    assert len(v21_demo.notification_payload(state)) == (0 if notification_failure else 1)


@pytest.mark.parametrize("notification_failure", [None, "emit", "record", "persist"])
def test_auto_start_error_retains_safe_shutdown_when_notification_fails(notification_failure):
    state = user_state(active=False)
    app = SimpleNamespace(state=SimpleNamespace(v21_demo=state, binance_demo={}))
    request = SimpleNamespace(app=app, state=SimpleNamespace())
    original_emit = v21_demo.emit_notification
    original_record = v21_demo.record_event

    def emit(current, kind, message, *, event_id):
        if notification_failure == "emit" and kind == "AUTO_START_ERROR":
            raise RuntimeError("notification failed")
        return original_emit(current, kind, message, event_id=event_id)

    def record(*args, **kwargs):
        if notification_failure == "record" and args[1] == "NOTIFICATION" and kwargs.get("reason", "").startswith("AUTO_START_ERROR:"):
            raise RuntimeError("notification record failed")
        return original_record(*args, **kwargs)

    def persist(current):
        if notification_failure == "persist" and any(row["severity"] == "critical" for row in v21_demo.notification_payload(current)):
            raise OSError("notification storage failed")

    with patch.object(v21_demo, "state_for", return_value=state), \
            patch.object(v21_demo, "demo_state_for", return_value={}), \
            patch.object(v21_demo, "armed", return_value=True), \
            patch.object(v21_demo, "credentials_configured", return_value=True), \
            patch.object(v21_demo, "client_for", return_value=object()), \
            patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={})), \
            patch.object(v21_demo, "bind_demo_grant"), \
            patch.object(v21_demo, "persist_state", side_effect=persist), \
            patch.object(v21_demo, "ensure_automation_task"), \
            patch.object(v21_demo, "summary_payload", side_effect=lambda current: current), \
            patch.object(v21_demo, "automatic_cycle", new=AsyncMock(side_effect=RuntimeError("first cycle failed"))) as cycle, \
            patch.object(v21_demo, "emit_notification", side_effect=emit), \
            patch.object(v21_demo, "record_event", side_effect=record), \
            patch("app.strategies.original_v2_demo.feature_enabled", return_value=False):
        result = asyncio.run(v21_demo.v21_auto_start(
            request, v21_demo.AutoStartRequest(confirmation="DEMO OTOMATİK"),
        ))
    cycle.assert_awaited_once()
    assert not result["auto"]["enabled"]
    assert result["auto"]["status"] == "OFF"
    assert result["scanner"]["scan_status"] == "HATA"
    assert any(row["kind"] == "AUTO_START_ERROR" for row in state["journal"])
    critical = [row for row in v21_demo.notification_payload(state) if row["severity"] == "critical"]
    assert len(critical) == (0 if notification_failure else 1)


def test_loop_crash_notifies_only_matching_active_user_contexts():
    global_state = v21_demo.initial_state()
    active = user_state()
    inactive = user_state("user-b", active=False)
    mismatched = user_state("another-user")
    app = SimpleNamespace(state=SimpleNamespace(
        v21_demo=global_state,
        _v21_demo_user_state={"user-a": active, "user-b": inactive, "user-c": mismatched},
    ))
    task = Mock()
    task.cancelled.return_value = False
    task.exception.return_value = RuntimeError("loop failed")
    with patch.object(v21_demo, "persist_state"):
        v21_demo._automation_task_done(app, task)
    assert global_state["journal"][0]["kind"] == "AUTO_LOOP_CRASH"
    assert v21_demo.notification_payload(active)[0]["severity"] == "critical"
    assert v21_demo.notification_payload(inactive) == []
    assert v21_demo.notification_payload(mismatched) == []
    assert active["auto"]["enabled"]


def test_unattributed_loop_crash_remains_in_system_journal_and_log(caplog):
    state = v21_demo.initial_state()
    app = SimpleNamespace(state=SimpleNamespace(v21_demo=state))
    task = Mock()
    task.cancelled.return_value = False
    task.exception.return_value = RuntimeError("loop failed")
    with patch.object(v21_demo, "persist_state") as persist:
        v21_demo._automation_task_done(app, task)
    assert state["journal"][0]["kind"] == "AUTO_LOOP_CRASH"
    persist.assert_called_once_with(state)
    assert "Demo automation loop crashed" in caplog.text
    assert v21_demo.notification_payload(state) == []


def test_one_user_notification_failure_does_not_block_other_crash_recipients(caplog):
    active = user_state()
    other = user_state("user-b")
    state = v21_demo.initial_state()
    app = SimpleNamespace(state=SimpleNamespace(
        v21_demo=state, _v21_demo_user_state={"user-a": active, "user-b": other},
    ))
    task = Mock()
    task.cancelled.return_value = False
    task.exception.return_value = RuntimeError("loop failed")
    original_emit = v21_demo.emit_notification

    def emit(current, *args, **kwargs):
        if current is active:
            raise RuntimeError("notification failed")
        return original_emit(current, *args, **kwargs)

    with patch.object(v21_demo, "persist_state"), patch.object(v21_demo, "emit_notification", side_effect=emit):
        v21_demo._automation_task_done(app, task)
    assert state["journal"][0]["kind"] == "AUTO_LOOP_CRASH"
    assert v21_demo.notification_payload(active) == []
    assert v21_demo.notification_payload(other)[0]["severity"] == "critical"
    assert "Critical Demo notification failed" in caplog.text


def test_async_notification_storage_failure_is_logged_without_interrupting_recovery(caplog):
    async def run():
        state = user_state()
        app = SimpleNamespace(state=SimpleNamespace(db_pool=SimpleNamespace(
            execute=AsyncMock(side_effect=RuntimeError("database write failed")),
        )))
        state["_app"] = app
        v21_demo.emit_critical_notification(state, "ACİL KORUMA", "Failure", event_id="async-failure")
        tasks = list(app.state._v21_persistence_tasks)
        assert len(tasks) == 1
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert app.state._v21_persistence_tasks == set()
        assert v21_demo.notification_payload(state)[0]["severity"] == "critical"
    asyncio.run(run())
    assert "Demo snapshot persistence failed: error=RuntimeError" in caplog.text
