import asyncio
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import legacy_demo_results, legacy_loss_guard, v21_demo
from test_legacy_demo_results import context, fill

NOW = datetime(2026, 10, 10, 21, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def legacy_environment(monkeypatch):
    monkeypatch.setenv("PROTREBOT_BINANCE_DEMO_KAIS_ORIGINAL_V2_ENABLED", "false")


def fixture():
    state, demo, plan = context()
    state["risk"]["consecutive_losses"] = 0
    return state, demo, plan


def closed_trade(state, demo, plan, *, pnl="-1", at=NOW - timedelta(minutes=1), uncertain=False):
    timestamp = int(at.timestamp() * 1000)
    candidate = copy.deepcopy(plan)
    index = len(demo["plans"])
    candidate.update(
        id=f"trade-{index}", entry_order_id=100 + index, entry_client_order_id=f"entry-{index}",
        stop_actual_order_id=200 + index, stop_actual_client_order_id=f"exit-{index}",
        stop_algo_id=300 + index, stop_client_id=f"stop-{index}", protection_ids=[300 + index],
    )
    demo["plans"][candidate["id"]] = candidate
    for entry in (True, False):
        payload = fill(
            entry=entry, i=candidate["entry_order_id" if entry else "stop_actual_order_id"],
            c=candidate["entry_client_order_id" if entry else "stop_actual_client_order_id"],
            pnl="0" if entry else pnl, at=timestamp, n=None if uncertain and not entry else "0.1",
        )
        legacy_demo_results.observe_stream(state, demo, payload)
    return candidate["id"]


def update(state, demo, *, now=NOW, restart=False):
    return legacy_loss_guard.update(state, demo, now, restart=restart)


@pytest.mark.parametrize("sequence,expected", [
    (["-1", "-1"], 2), (["-1", "1"], 0), (["-1", "0.2"], 0),
    (["1", "-1"], 1), (["-1", "unknown", "-1"], 3),
])
def test_net_results_and_unknown_closures_change_the_streak(sequence, expected):
    state, demo, plan = fixture()
    for index, pnl in enumerate(sequence):
        closed_trade(state, demo, plan, pnl="10" if pnl == "unknown" else pnl,
                     uncertain=pnl == "unknown", at=NOW - timedelta(minutes=10 - index))
        update(state, demo)
    assert state["risk"]["consecutive_losses"] == expected


def test_unknown_count_is_not_retracted_or_reset_by_later_profit_evidence():
    state, demo, plan = fixture()
    pid = closed_trade(state, demo, plan, pnl="10", uncertain=True)
    notices = update(state, demo)
    assert state["risk"]["consecutive_losses"] == 1
    assert notices[0]["kind"] == "LEGACY_UNVERIFIED_LOSS"
    assert "fill_values_missing" in notices[0]["message"]
    row = state["legacy_trade_results"][pid]
    row.update(status="verified", result="profit", net_pnl="9.8", closed_at=(NOW - timedelta(minutes=1)).isoformat())
    assert update(state, demo) == []
    assert state["risk"]["consecutive_losses"] == 1
    assert state["legacy_loss_guard"]["applied"][pid]["result"] == "unverified"


def test_partial_exit_does_not_count_as_an_unknown_closure():
    state, demo, _ = fixture()
    legacy_demo_results.observe_stream(state, demo, fill(entry=True))
    legacy_demo_results.observe_stream(state, demo, fill(quantity="1", cumulative="1", n=None))
    update(state, demo)
    assert state["risk"]["consecutive_losses"] == 0
    assert state["legacy_loss_guard"]["applied"] == {}


@pytest.mark.parametrize("closure", ["snapshot", "closed-plan", "unlinked"])
def test_uncertain_complete_closures_count_once(closure):
    state, demo, plan = fixture()
    legacy_demo_results.observe_stream(state, demo, fill(entry=True))
    if closure == "unlinked":
        legacy_demo_results.observe_stream(state, demo, fill(i=99, c="manual-close", pnl="10"))
    else:
        if closure == "closed-plan":
            plan["position_status"] = "CLOSED"
        legacy_demo_results.observe_missing_positions(
            state, demo, {"positions": [{"symbol": "BTCUSDT"}]}, {"positions": []},
        )
    update(state, demo)
    assert state["risk"]["consecutive_losses"] == 1
    update(state, demo)
    assert state["risk"]["consecutive_losses"] == 1


def test_restart_and_repeated_updates_do_not_count_the_same_closure_twice():
    state, demo, plan = fixture()
    closed_trade(state, demo, plan)
    update(state, demo)
    restored = v21_demo._state_from_payload(json.loads(json.dumps(v21_demo.serializable_state(state))), "user-a")
    for _ in range(3):
        update(restored, demo)
    assert restored["risk"]["consecutive_losses"] == 1


def test_late_same_day_profit_is_ordered_before_later_losses():
    state, demo, plan = fixture()
    closed_trade(state, demo, plan, at=NOW - timedelta(minutes=2))
    update(state, demo)
    closed_trade(state, demo, plan, pnl="1", at=NOW - timedelta(minutes=3))
    update(state, demo)
    assert state["risk"]["consecutive_losses"] == 1


def test_limit_latches_even_when_later_profit_resets_the_counter():
    state, demo, plan = fixture()
    state["settings"]["consecutive_loss_limit"] = 2
    for index, pnl in enumerate(["-1", "-1", "1"]):
        closed_trade(state, demo, plan, pnl=pnl, at=NOW - timedelta(minutes=5 - index))
    update(state, demo)
    assert state["risk"]["consecutive_losses"] == 0
    assert state["legacy_loss_guard"]["paused"]
    assert state["auto"]["status"] == "PAUSED"
    assert not state["auto"]["enabled"]


def test_utc_day_reset_and_restore_keep_pause_until_explicit_manual_restart(tmp_path):
    state, demo, plan = fixture()
    state["settings"]["consecutive_loss_limit"] = 1
    closed_trade(state, demo, plan)
    update(state, demo)
    next_day = NOW + timedelta(days=1)
    update(state, demo, now=next_day)
    assert state["risk"]["consecutive_losses"] == 0
    assert state["auto"]["status"] == "PAUSED"
    payload = v21_demo.serializable_state(state)
    restored = v21_demo._state_from_payload(payload, "user-a")
    assert restored["auto"]["status"] == "PAUSED"
    assert not restored["auto"]["enabled"]
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with patch.object(v21_demo, "STATE_PATH", path), patch.object(v21_demo, "BACKUP_PATH", tmp_path / "backup.json"):
        assert v21_demo.load_state()["auto"]["status"] == "PAUSED"
    update(restored, demo, now=next_day, restart=True)
    assert not restored["legacy_loss_guard"]["paused"]
    assert restored["risk"]["consecutive_losses"] == 0
    assert not restored["auto"]["enabled"]


def test_manual_restart_does_not_recount_or_reset_on_late_old_closures():
    state, demo, plan = fixture()
    closed_trade(state, demo, plan)
    update(state, demo, restart=True)
    closed_trade(state, demo, plan, pnl="1", at=NOW - timedelta(seconds=10))
    update(state, demo, now=NOW + timedelta(seconds=1))
    assert state["risk"]["consecutive_losses"] == 0
    closed_trade(state, demo, plan, at=NOW + timedelta(seconds=2))
    update(state, demo, now=NOW + timedelta(seconds=3))
    assert state["risk"]["consecutive_losses"] == 1


def test_previous_day_late_result_does_not_modify_new_day_streak():
    state, demo, plan = fixture()
    closed_trade(state, demo, plan, at=NOW - timedelta(days=1))
    update(state, demo)
    assert state["risk"]["consecutive_losses"] == 0
    closed_trade(state, demo, plan)
    update(state, demo)
    assert state["risk"]["consecutive_losses"] == 1


@pytest.mark.parametrize("change", ["owner", "manual", "original"])
def test_other_users_and_nonlegacy_plans_do_not_change_counter(change):
    state, demo, plan = fixture()
    pid = closed_trade(state, demo, plan)
    if change == "owner":
        demo["plans"][pid]["user_id"] = "user-b"
    elif change == "manual":
        demo["plans"][pid]["source"] = "MANUAL"
    else:
        demo["plans"][pid]["strategy_id"] = "kais-original-v2-demo-v1"
    update(state, demo)
    assert state["risk"]["consecutive_losses"] == 0


def test_unknown_journal_and_dynamic_limit_notification_do_not_change_daily_pnl():
    state, demo, plan = fixture()
    state["settings"]["consecutive_loss_limit"] = 1
    closed_trade(state, demo, plan, uncertain=True)
    state["journal"] = [
        {"id": f"fill-{i}", "kind": "FILL", "created_at": NOW.isoformat(),
         "realized_pnl": -1, "verified_realized": True}
        for i in range(v21_demo.JOURNAL_LIMIT)
    ]
    before = copy.deepcopy(state["journal"])
    with patch.object(v21_demo, "datetime") as clock:
        clock.now.return_value = NOW
        metrics = v21_demo.daily_metrics(state)
        assert v21_demo._refresh_legacy_loss_guard(state, demo)
        assert v21_demo._refresh_legacy_loss_guard(state, demo) is False
        assert v21_demo.daily_metrics(state) == metrics
    assert state["journal"] == before
    notices = v21_demo.notification_payload(state)
    assert len(notices) == 1
    assert notices[0]["message"].startswith("1 ardışık")
    assert "Üç ardışık" not in notices[0]["message"]
    assert any("fill_values_missing" in row["message"] for row in v21_demo.user_journal_items(state))


@pytest.mark.parametrize("fault", ["counter", "lock", "code", "observer", "journal", "persist"])
def test_guard_fault_blocks_new_orders_and_keeps_actual_stop_repair_running(fault, caplog):
    state, demo, plan = fixture()
    plan.update(stop_loss="90", protection_ids=[20], direction="LONG")
    app = SimpleNamespace(state=SimpleNamespace(binance_demo={}, v21_demo=v21_demo.initial_state()))
    client = SimpleNamespace(signed=AsyncMock(return_value=[]))
    snapshot = {"positions": [{"symbol": "BTCUSDT", "direction": "LONG"}], "open_algo_orders": []}
    if fault == "counter":
        state["risk"]["consecutive_losses"] = "bad"
    elif fault == "lock":
        state["legacy_loss_guard"] = {"paused": "bad", "applied": {}}
    elif fault == "observer":
        state["legacy_result_observer_errors"] = ["RuntimeError"]
    elif fault == "journal":
        pid = closed_trade(state, demo, plan, uncertain=True)
        demo["plans"][pid].update(position_status="CLOSED", status="KAPANDI")

    async def run():
        with patch.object(v21_demo, "armed", return_value=True), \
                patch.object(v21_demo, "in_schedule", return_value=True), \
                patch.object(v21_demo, "datetime") as clock, \
                patch.object(v21_demo, "execute_demo_order", new=AsyncMock()) as entry, \
                patch.object(v21_demo, "update_legacy_loss_guard", side_effect=RuntimeError("guard failed") if fault == "code" else None,
                             wraps=None if fault == "code" else legacy_loss_guard.update), \
                patch.object(v21_demo, "record_event", side_effect=RuntimeError("journal failed") if fault == "journal" else None,
                             wraps=None if fault == "journal" else v21_demo.record_event), \
                patch.object(v21_demo, "persist_state", side_effect=OSError("storage failed") if fault == "persist" else None):
            clock.now.return_value = NOW
            await v21_demo._automatic_cycle_impl(app, user_id="user-a", demo_state=demo, v21_state=state)
            entry.assert_not_awaited()
            assert state["auto"]["rejection_gate"] == "CONSECUTIVE_LOSS_ERROR"
            assert state["auto"]["status"] == "PAUSED"
        with patch.object(v21_demo, "post_algo", new=AsyncMock(side_effect=v21_demo.BinanceDemoError("repair failed"))) as repair, \
                patch.object(v21_demo, "close_symbol_position", new=AsyncMock()) as close, \
                patch.object(v21_demo, "persist_state"), patch.object(v21_demo, "persist_runtime"):
            assert await v21_demo.ensure_stop_protection(app, snapshot, demo_state=demo, v21_state=state, client=client)
        repair.assert_awaited_once()
        assert repair.await_args.args[1]["type"] == "STOP_MARKET"
        close.assert_awaited_once_with(client, "BTCUSDT", "BOTH")
        assert plan["position_status"] == "CLOSED"
    asyncio.run(run())
    assert "new entries blocked" in caplog.text


def test_reconciliation_protects_before_failing_counter_and_repeats_next_cycle():
    state, demo, _ = fixture()
    state["risk"]["consecutive_losses"] = "bad"
    app = SimpleNamespace(state=SimpleNamespace(binance_demo={}, v21_demo=v21_demo.initial_state()))
    sequence = []
    snapshot = {"positions": [{"symbol": "BTCUSDT"}]}

    async def protect(*args, **kwargs):
        sequence.append("protect")
        return False

    def fail(*args, **kwargs):
        sequence.append("guard")
        raise RuntimeError("guard failed")

    sleeps = 0
    async def sleep(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    async def run():
        with patch.object(v21_demo, "_background_contexts", return_value=[("user-a", demo, state)]), \
                patch.object(v21_demo, "client_for_state", return_value=object()), \
                patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value=snapshot)), \
                patch.object(v21_demo, "reconcile_demo_plans", return_value={"changed": False}), \
                patch.object(v21_demo, "confirm_provenance_from_snapshot", new=AsyncMock()), \
                patch.object(v21_demo, "ensure_stop_protection", new=AsyncMock(side_effect=protect)), \
                patch.object(v21_demo, "improve_dynamic_stops", new=AsyncMock(return_value=False)), \
                patch.object(v21_demo, "update_legacy_loss_guard", side_effect=fail), \
                patch.object(v21_demo, "persist_state"), patch.object(v21_demo, "persist_runtime"), \
                patch.object(v21_demo.asyncio, "sleep", new=AsyncMock(side_effect=sleep)), \
                pytest.raises(asyncio.CancelledError):
            await v21_demo.reconciliation_loop(app)
    asyncio.run(run())
    assert sequence == ["protect", "guard", "protect", "guard"]


@pytest.mark.parametrize("enabled,selected", [(True, None), (False, "kais-original-v2-demo-v1")])
def test_original_cycle_never_calls_legacy_guard(enabled, selected):
    state, demo, _ = fixture()
    state["auto"]["strategy_id"] = selected
    state["legacy_loss_guard"] = {"error": "old failure", "paused": True}
    app = SimpleNamespace(state=SimpleNamespace())
    async def run():
        with patch("app.strategies.original_v2_demo.feature_enabled", return_value=enabled), \
                patch.object(v21_demo, "armed", return_value=True), \
                patch.object(v21_demo, "_legacy_entry_block", new=AsyncMock()) as guard, \
                patch("app.original_v2_demo_execution.automatic_cycle", new=AsyncMock()) as original:
            await v21_demo._automatic_cycle_impl(app, demo_state=demo, v21_state=state)
            guard.assert_not_awaited()
            original.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("gate", ["KILL_SWITCH", "DAILY_LOSS_LIMIT"])
def test_existing_kill_switch_and_daily_limit_still_block_before_execution(gate):
    state, demo, _ = fixture()
    state["risk"]["kill_switch"] = gate == "KILL_SWITCH"
    if gate == "DAILY_LOSS_LIMIT":
        state["journal"] = [{"created_at": NOW.isoformat(), "verified_realized": True, "realized_pnl": -31}]
    app = SimpleNamespace(state=SimpleNamespace())
    async def run():
        with patch.object(v21_demo, "datetime") as clock, patch.object(v21_demo, "armed", return_value=True), \
                patch.object(v21_demo, "in_schedule", return_value=True), patch.object(v21_demo, "persist_state"), \
                patch.object(v21_demo, "execute_demo_order", new=AsyncMock()) as entry:
            clock.now.return_value = NOW
            await v21_demo._automatic_cycle_impl(app, demo_state=demo, v21_state=state)
            entry.assert_not_awaited()
    asyncio.run(run())
    assert state["auto"]["rejection_gate"] == gate


def test_closed_plan_without_any_fill_history_counts_as_unknown():
    state, demo, plan = fixture()
    plan["position_status"] = "CLOSED"
    notices = update(state, demo)
    assert state["risk"]["consecutive_losses"] == 1
    assert "closure_history_missing" in notices[0]["message"]
    assert update(state, demo) == []


def test_limit_reached_during_scan_blocks_the_next_order():
    state, demo, plan = fixture()
    state["settings"]["consecutive_loss_limit"] = 1
    app = SimpleNamespace(state=SimpleNamespace())
    candidate = {
        "symbol": "ETHUSDT", "direction": "LONG", "status": "SELECTED",
        "score": 95, "analysis_score": 95, "opportunity_score": 95,
        "opportunity_breakdown": {"liquidity_quality": 90, "mtf_confirmation": 90},
        "entry": 100, "stop_loss": 99, "tp1": 101, "tp2": 102, "tp3": 103,
        "risk_reward": 3, "data_health": True, "signal_age_seconds": 0,
    }
    async def scan(*args, **kwargs):
        closed_trade(state, demo, plan)
        return [candidate]
    async def run():
        with patch.object(v21_demo, "armed", return_value=True), patch.object(v21_demo, "in_schedule", return_value=True), \
                patch.object(v21_demo, "datetime") as clock, patch.object(v21_demo, "persist_state"), \
                patch.object(v21_demo, "client_for_state", return_value=object()), \
                patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={
                    "positions": [], "open_orders": [], "wallet_balance": 1000,
                })), patch.object(v21_demo, "scan_demo_universe", new=AsyncMock(side_effect=scan)), \
                patch.object(v21_demo, "execute_demo_order", new=AsyncMock()) as entry:
            clock.now.return_value = NOW
            await v21_demo._automatic_cycle_impl(app, user_id="user-a", demo_state=demo, v21_state=state)
            entry.assert_not_awaited()
    asyncio.run(run())
    assert state["risk"]["consecutive_losses"] == 1
    assert state["auto"]["rejection_gate"] == "CONSECUTIVE_LOSSES"
    assert state["auto"]["status"] == "PAUSED"


def test_async_storage_failure_blocks_legacy_entry():
    state, demo, plan = fixture()
    closed_trade(state, demo, plan)
    class Pool:
        async def execute(self, *args):
            raise OSError("database failed")
    app = SimpleNamespace(state=SimpleNamespace(db_pool=Pool()))
    state["_app"] = app
    async def run():
        with patch.object(v21_demo, "datetime") as clock:
            clock.now.return_value = NOW
            block = await v21_demo._legacy_entry_block(state, demo)
        assert block[0] == "CONSECUTIVE_LOSS_ERROR"
        assert state["legacy_loss_guard"]["error"] == "OSError"
        assert not state["auto"]["enabled"]
    asyncio.run(run())


@pytest.mark.parametrize("confirmation,armed,credentials,hedge,expected", [
    ("WRONG", True, True, False, 422),
    ("DEMO OTOMATİK", False, True, False, 423),
    ("DEMO OTOMATİK", True, False, False, 412),
    ("DEMO OTOMATİK", True, True, True, 409),
    ("DEMO OTOMATİK", True, True, False, None),
])
def test_manual_start_releases_pause_only_after_existing_guards(confirmation, armed, credentials, hedge, expected):
    state, demo, plan = fixture()
    state["settings"]["consecutive_loss_limit"] = 1
    closed_trade(state, demo, plan)
    update(state, demo)
    app = SimpleNamespace(state=SimpleNamespace(binance_demo={}, v21_demo=state))
    request = SimpleNamespace(app=app)
    async def run():
        with patch.object(v21_demo, "state_for", return_value=state), patch.object(v21_demo, "demo_state_for", return_value=demo), \
                patch.object(v21_demo, "armed", return_value=armed), patch.object(v21_demo, "credentials_configured", return_value=credentials), \
                patch.object(v21_demo, "client_for", return_value=object()), \
                patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={"hedge_mode": hedge})), \
                patch.object(v21_demo, "bind_demo_grant"), patch.object(v21_demo, "persist_state"), \
                patch.object(v21_demo, "ensure_automation_task"), patch.object(v21_demo, "summary_payload", side_effect=lambda value: value), \
                patch.object(v21_demo, "datetime") as clock, patch.object(v21_demo, "automatic_cycle", new=AsyncMock()) as cycle:
            clock.now.return_value = NOW
            body = v21_demo.AutoStartRequest(confirmation=confirmation)
            if expected is not None:
                with pytest.raises(v21_demo.HTTPException) as failure:
                    await v21_demo.v21_auto_start(request, body)
                assert failure.value.status_code == expected
                cycle.assert_not_awaited()
                assert state["legacy_loss_guard"]["paused"]
            else:
                await v21_demo.v21_auto_start(request, body)
                cycle.assert_awaited_once()
                assert not state["legacy_loss_guard"]["paused"]
                assert state["risk"]["consecutive_losses"] == 0
                assert state["auto"]["enabled"]
    asyncio.run(run())


def test_guard_failure_requires_manual_restart_even_after_accounting_recovers():
    state, demo, _ = fixture()
    v21_demo._loss_guard_failed(state, RuntimeError("failed"))
    update(state, demo)
    assert state["auto"]["status"] == "PAUSED"
    assert state["legacy_loss_guard"]["error"] == "RuntimeError"
    update(state, demo, restart=True)
    assert state["legacy_loss_guard"]["error"] is None
    assert not state["legacy_loss_guard"]["paused"]


def test_separate_pause_notification_can_be_marked_read_and_is_user_scoped():
    state, demo, plan = fixture()
    state["settings"]["consecutive_loss_limit"] = 1
    closed_trade(state, demo, plan)
    with patch.object(v21_demo, "datetime") as clock:
        clock.now.return_value = NOW
        v21_demo._refresh_legacy_loss_guard(state, demo)
    notification_id = v21_demo.notification_payload(state)[0]["id"]
    other = v21_demo.initial_state()
    other["_user_id"] = "user-b"
    async def run():
        with patch.object(v21_demo, "state_for", return_value=other), patch.object(v21_demo, "persist_state"):
            with pytest.raises(v21_demo.HTTPException) as failure:
                await v21_demo.v21_notification_read(SimpleNamespace(), notification_id)
            assert failure.value.status_code == 404
        with patch.object(v21_demo, "state_for", return_value=state), patch.object(v21_demo, "persist_state"):
            await v21_demo.v21_notification_read(SimpleNamespace(), notification_id)
    asyncio.run(run())
    assert v21_demo.notification_payload(state)[0]["read"]
    assert other["notifications"]["read_ids"] == []


def test_closure_arriving_during_storage_is_counted_and_saved_before_entry():
    state, demo, plan = fixture()
    state["settings"]["consecutive_loss_limit"] = 1
    snapshots = []
    class Pool:
        async def execute(self, query, key, payload):
            snapshots.append(json.loads(payload))
            if len(snapshots) == 1:
                closed_trade(state, demo, plan)
    state["_app"] = SimpleNamespace(state=SimpleNamespace(db_pool=Pool()))
    async def run():
        with patch.object(v21_demo, "datetime") as clock:
            clock.now.return_value = NOW
            block = await v21_demo._legacy_entry_block(state, demo)
        assert block[0] == "CONSECUTIVE_LOSSES"
    asyncio.run(run())
    assert len(snapshots) == 2
    assert snapshots[-1]["risk"]["consecutive_losses"] == 1
    assert snapshots[-1]["legacy_loss_guard"]["paused"]


def test_previous_day_unverified_closed_plan_is_not_a_new_day_loss():
    state, demo, plan = fixture()
    plan.update(position_status="CLOSED", closed_at=(NOW - timedelta(days=1)).isoformat())
    update(state, demo)
    assert state["risk"]["consecutive_losses"] == 0
    assert state["legacy_loss_guard"]["applied"]["plan-a"]["result"] == "unverified"


def test_reconciliation_storage_failure_latches_pause_after_protection(caplog):
    state, demo, _ = fixture()
    app = SimpleNamespace(state=SimpleNamespace(binance_demo={}, v21_demo=v21_demo.initial_state()))
    async def run():
        with patch.object(v21_demo, "_background_contexts", return_value=[("user-a", demo, state)]), \
                patch.object(v21_demo, "client_for_state", return_value=object()), \
                patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={"positions": []})), \
                patch.object(v21_demo, "reconcile_demo_plans", return_value={"changed": False}), \
                patch.object(v21_demo, "confirm_provenance_from_snapshot", new=AsyncMock()), \
                patch.object(v21_demo, "ensure_stop_protection", new=AsyncMock(return_value=False)) as protect, \
                patch.object(v21_demo, "improve_dynamic_stops", new=AsyncMock(return_value=False)), \
                patch.object(v21_demo, "persist_state", side_effect=OSError("storage failed")), \
                patch.object(v21_demo, "persist_runtime"), \
                patch.object(v21_demo.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)), \
                pytest.raises(asyncio.CancelledError):
            await v21_demo.reconciliation_loop(app)
        protect.assert_awaited_once()
    asyncio.run(run())
    assert state["legacy_loss_guard"]["error"] == "OSError"
    assert state["auto"]["status"] == "PAUSED"
    assert not state["auto"]["enabled"]
    assert "new entries blocked" in caplog.text


@pytest.mark.parametrize("enabled,selected", [(True, None), (False, "kais-original-v2-demo-v1")])
def test_failed_old_legacy_snapshot_does_not_pause_running_original(enabled, selected):
    state, demo, _ = fixture()
    update(state, demo)
    state["auto"]["strategy_id"] = selected
    before = copy.deepcopy(state["auto"])
    class Pool:
        async def execute(self, *args):
            raise OSError("old snapshot failed")
    state["_app"] = SimpleNamespace(state=SimpleNamespace(db_pool=Pool()))
    async def run():
        with patch("app.strategies.original_v2_demo.feature_enabled", return_value=enabled):
            v21_demo.persist_state(state)
            tasks = tuple(state["_app"].state._v21_persistence_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
    asyncio.run(run())
    assert state["auto"] == before
    assert state["legacy_loss_guard"]["error"] == "OSError"
