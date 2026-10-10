import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import v21_demo
from test_legacy_loss_guard import NOW, closed_trade, fixture


async def start(state, demo, cycle):
    app = SimpleNamespace(state=SimpleNamespace(v21_demo=state, binance_demo={}))
    with patch.object(v21_demo, "state_for", return_value=state), \
            patch.object(v21_demo, "demo_state_for", return_value=demo), \
            patch.object(v21_demo, "armed", return_value=True), \
            patch.object(v21_demo, "credentials_configured", return_value=True), \
            patch.object(v21_demo, "client_for", return_value=object()), \
            patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={})), \
            patch.object(v21_demo, "bind_demo_grant"), patch.object(v21_demo, "persist_state"), \
            patch.object(v21_demo, "summary_payload", side_effect=lambda value: value), \
            patch.object(v21_demo, "ensure_automation_task"), \
            patch.object(v21_demo, "automatic_cycle", new=cycle), \
            patch("app.strategies.original_v2_demo.feature_enabled", return_value=False), \
            patch.object(v21_demo, "datetime") as clock:
        clock.now.return_value = NOW
        return await v21_demo.v21_auto_start(
            SimpleNamespace(app=app), v21_demo.AutoStartRequest(confirmation="DEMO OTOMATİK"),
        )


def test_loss_pause_uses_new_presentation_and_manual_restart_notifies_only_once():
    state, demo, plan = fixture()
    state["settings"]["consecutive_loss_limit"] = 1
    closed_trade(state, demo, plan)
    with patch("app.strategies.original_v2_demo.feature_enabled", return_value=False), \
            patch.object(v21_demo, "datetime") as clock:
        clock.now.return_value = NOW
        v21_demo._refresh_legacy_loss_guard(state, demo)
        v21_demo._refresh_legacy_loss_guard(state, demo)
    assert state["legacy_loss_guard"]["paused"]
    assert not state["auto"]["enabled"]
    items = v21_demo.notification_payload(state)
    assert len(items) == 1
    assert (items[0]["type"], items[0]["severity"], items[0]["title"], items[0]["target"]) == (
        "CONSECUTIVE_LOSSES", "critical", "Ardışık zarar sınırına ulaşıldı", "risk-management",
    )
    assert items[0]["message"].startswith("1 ardışık")
    assert v21_demo.notification_payload(v21_demo.initial_state()) == []
    cycle = AsyncMock()

    async def run():
        await start(state, demo, cycle)
        await start(state, demo, cycle)

    asyncio.run(run())
    assert not state["legacy_loss_guard"]["paused"]
    assert state["risk"]["consecutive_losses"] == 0
    assert state["auto"]["enabled"]
    assert sorted(item["type"] for item in v21_demo.notification_payload(state)) == [
        "AUTO_STARTED", "CONSECUTIVE_LOSSES",
    ]
    assert cycle.await_count == 2


@pytest.mark.parametrize("enabled", [False, True])
def test_observer_error_rejects_start_without_success_notification_or_new_entries(enabled):
    state, demo, _ = fixture()
    state["auto"]["enabled"] = enabled
    state["risk"]["consecutive_losses"] = 2
    state["legacy_result_observer_errors"] = ["ValueError"]
    cycle = AsyncMock()
    with pytest.raises(v21_demo.HTTPException) as failure:
        asyncio.run(start(state, demo, cycle))
    assert failure.value.status_code == 423
    cycle.assert_not_awaited()
    assert state["legacy_loss_guard"]["paused"]
    assert state["risk"]["consecutive_losses"] == 2
    assert not state["auto"]["enabled"]
    assert v21_demo.notification_payload(state) == []
