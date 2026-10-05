import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import v25_execution as v25


class MasterTradeRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_live_journal_uses_only_verified_finite_closed_live_plans(self):
        closed = {
            "id": "live-closed", "symbol": "BTCUSDT", "side": "BUY",
            "status": "KAPANDI", "pnl_verified": True, "realized_pnl": 7,
            "closed_at": v25.now_iso(), "exit_price": 100,
        }
        state = v25.initial_state()
        state["plans"] = {
            "closed": closed,
            "pending": {**closed, "id": "pending", "pnl_verified": False},
            "open": {**closed, "id": "open", "status": "KORUMA AKTİF"},
            "nan": {**closed, "id": "nan", "realized_pnl": float("nan")},
            "boolean": {**closed, "id": "boolean", "realized_pnl": True},
        }
        app = SimpleNamespace(state=SimpleNamespace(
            v25_execution=state, v21_demo={"journal": [{"realized_pnl": 999}]},
        ))
        with patch.object(v25, "consent_status", return_value={}), \
                patch.object(v25, "readiness", return_value={"ready": False}):
            payload = v25.public_status(app)
        self.assertEqual([row["id"] for row in payload["journal"]], ["live-closed"])
        self.assertEqual(payload["performance"]["net_profit"], 7)
        self.assertEqual(payload["daily_performance"]["net_profit"], 7)
        self.assertEqual(payload["performance"]["total_trades"], 1)

    async def test_failed_readiness_revokes_all_auto_entry_authority(self):
        state = v25.initial_state()
        state["auto"].update(enabled=True, session_until=time.time() + 3600)
        state.update(live_auto_trade=True, real_trading_locked=False, armed_until=time.time() + 300)
        state["auto_authorization"].update(user_id="offline-owner", session_id="offline-session")
        app = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        with patch.object(v25, "readiness_for", return_value={"ready": False}), \
                patch.object(v25, "persist_state"), \
                patch.object(v25, "account_snapshot", new=AsyncMock()) as snapshot:
            await v25.automatic_cycle(app, credentials=("offline-key-only", "offline-secret-only"))
        snapshot.assert_not_awaited()
        self.assertFalse(state["auto"]["enabled"])
        self.assertFalse(state["live_auto_trade"])
        self.assertTrue(state["real_trading_locked"])
        self.assertEqual(state["armed_until"], 0)
        self.assertEqual(state["auto_authorization"], v25.initial_state()["auto_authorization"])

    async def test_duplicate_start_does_not_extend_or_replace_active_authority(self):
        state = v25.initial_state()
        deadline = time.time() + 120
        state["auto"].update(enabled=True, session_until=deadline)
        state["auto_authorization"].update(user_id="offline-owner", session_id="offline-session")
        authorization = dict(state["auto_authorization"])
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(v25_execution=state)),
            state=SimpleNamespace(member={"id": "offline-owner"}),
        )
        with patch.object(v25, "execution_owner", return_value={"id": "offline-owner"}), \
                patch.object(v25, "live_auto_start_gate", return_value=(True, "ready")), \
                patch.object(v25, "authenticated_live_expiry", return_value=time.time() + 900), \
                patch.object(v25, "public_status", return_value={}), \
                patch.object(v25, "persist_state") as persist:
            with self.assertRaises(HTTPException) as rejected:
                await v25.v25_auto_start(request, v25.Confirmation(confirmation="CANLI OTOMATİK"))
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertEqual(state["auto"]["session_until"], deadline)
        self.assertEqual(state["auto_authorization"], authorization)
        persist.assert_not_called()
