import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402

from app.binance_demo import (  # noqa: E402
    BinanceDemoError,
    ClosePositionRequest,
    demo_close_position,
)


class DemoPositionCloseTests(unittest.TestCase):
    def setUp(self):
        self.application = SimpleNamespace(
            state=SimpleNamespace(
                _binance_demo_user_state={},
                binance_demo={"plans": {}},
            )
        )
        self.request = SimpleNamespace(
            app=self.application,
            headers={"authorization": "Bearer user-a-session"},
            state=SimpleNamespace(member={"id": "user-a"}),
        )
        self.client = SimpleNamespace(signed=AsyncMock())
        self.position = [{"symbol": "BRUSDT", "positionSide": "BOTH", "positionAmt": "20"}]

    def close_request(self, symbol="BRUSDT"):
        return ClosePositionRequest(symbol=symbol, confirmation="DEMO KAPAT")

    def run_close(self, *, plan=None, position=None, symbol="BRUSDT"):
        state = {
            "_user_id": "user-a",
            "_session_id": "session-a",
            "plans": {},
            "events": [],
        }
        if plan is not None:
            state["plans"][plan["id"]] = plan
        self.application.state._binance_demo_user_state["user-a"] = state
        self.client.signed.side_effect = [position if position is not None else self.position, {"orderId": 700}]
        with patch("app.binance_demo.client_for", return_value=self.client), \
                patch("app.binance_demo.persist_runtime"), \
                patch("app.binance_demo.add_event"):
            return asyncio.run(demo_close_position(self.request, self.close_request(symbol)))

    def test_existing_position_and_plan_close_and_finalize(self):
        plan = {
            "id": "plan-a",
            "user_id": "user-a",
            "symbol": "BRUSDT",
            "provenance_state": "CONFIRMED",
            "position_status": "OPEN",
            "status": "OPEN",
            "protection_ids": [],
        }

        result = self.run_close(plan=plan)

        self.assertTrue(result["ok"])
        self.assertEqual(result["order_id"], 700)
        self.assertEqual(plan["position_status"], "CLOSED")
        self.assertEqual(plan["status"], "KAPANDI")
        self.assertEqual([call.args[:2] for call in self.client.signed.await_args_list], [
            ("GET", "/fapi/v3/positionRisk"),
            ("POST", "/fapi/v1/order"),
        ])

    def test_non_confirmed_plan_close_keeps_local_lifecycle_unchanged(self):
        plan = {
            "id": "plan-a",
            "user_id": "user-a",
            "symbol": "BRUSDT",
            "provenance_state": "PROVISIONAL",
            "position_status": "OPEN",
            "status": "OPEN",
            "protection_ids": [],
        }

        result = self.run_close(plan=plan)

        self.assertTrue(result["ok"])
        self.assertEqual(plan["position_status"], "OPEN")
        self.assertEqual(plan["status"], "OPEN")

    def test_existing_position_without_plan_closes_successfully(self):
        result = self.run_close()

        self.assertTrue(result["ok"])
        self.assertEqual(result["order_id"], 700)
        self.assertEqual(self.application.state._binance_demo_user_state["user-a"]["plans"], {})

    def test_missing_position_keeps_no_position_behavior(self):
        self.client.signed.side_effect = [[]]
        self.application.state._binance_demo_user_state["user-a"] = {
            "_user_id": "user-a", "_session_id": "session-a", "plans": {}, "events": [],
        }
        with patch("app.binance_demo.client_for", return_value=self.client):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(demo_close_position(self.request, self.close_request()))

        self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(self.client.signed.await_count, 1)

    def test_missing_credentials_fails_closed_without_binance_call(self):
        with patch("app.binance_demo.client_for", side_effect=BinanceDemoError("credential missing", http_status=412)):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(demo_close_position(self.request, self.close_request()))

        self.assertEqual(error.exception.status_code, 412)
        self.client.signed.assert_not_awaited()

    def test_mismatched_session_fails_closed_without_binance_call(self):
        with patch("app.binance_demo.client_for", side_effect=BinanceDemoError("session mismatch", http_status=412)):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(demo_close_position(self.request, self.close_request()))

        self.assertEqual(error.exception.status_code, 412)
        self.client.signed.assert_not_awaited()

    def test_unrelated_symbol_is_not_touched(self):
        result = self.run_close(symbol="BRUSDT")

        self.assertEqual(result["symbol"], "BRUSDT")
        close_params = self.client.signed.await_args_list[1].args[2]
        self.assertEqual(close_params["symbol"], "BRUSDT")
        self.assertNotEqual(close_params["symbol"], "KASUSDT")
        self.assertNotEqual(close_params["symbol"], "INJUSDT")


if __name__ == "__main__":
    unittest.main()