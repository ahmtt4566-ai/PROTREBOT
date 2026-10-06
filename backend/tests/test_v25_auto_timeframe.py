import asyncio
import copy
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1]))

from app import v25_execution as execution  # noqa: E402


class LiveAutoTimeframeTests(unittest.TestCase):
    def start(self, state, *, confirmation="CANLI OTOMATİK", gate=(True, "ready")):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(v25_execution=state)))
        with patch.multiple(
            execution,
            execution_owner=lambda request: {"id": "offline-owner"},
            live_auto_start_gate=lambda *args: gate,
            live_credentials_status=lambda request: ("offline-key", "offline-secret", "offline-fingerprint"),
            session_id=lambda request: "offline-session",
            authenticated_live_expiry=lambda request: time.time() + 600,
            public_status=lambda *args: {},
            persist_state=lambda state: None,
        ):
            return asyncio.run(execution.v25_auto_start(
                request, execution.Confirmation(confirmation=confirmation),
            ))

    def test_non_15m_returns_explicit_error_without_starting_or_mutating_state(self):
        for interval in ["1m", "5m", "1h", "4h"]:
            with self.subTest(interval=interval):
                state = execution.initial_state()
                state["policy"]["interval"] = interval
                before = copy.deepcopy(state)
                with self.assertRaises(execution.HTTPException) as caught:
                    self.start(state)
                self.assertEqual(caught.exception.status_code, 422)
                self.assertEqual(caught.exception.detail["code"], "LIVE_AUTO_TIMEFRAME_UNSUPPORTED")
                self.assertIn("15m", caught.exception.detail["message"])
                self.assertEqual(state, before)

    def test_15m_keeps_existing_start_flow(self):
        state = execution.initial_state()
        self.start(state)
        self.assertTrue(state["auto"]["enabled"])
        self.assertTrue(state["live_auto_trade"])
        self.assertGreater(state["auto"]["session_until"], time.time())
        self.assertTrue(any(event["kind"] == "LIVE_AUTO_START" for event in state["events"]))

    def test_15m_does_not_bypass_existing_safety_gates(self):
        state = execution.initial_state()
        with self.assertRaises(execution.HTTPException) as caught:
            self.start(state, gate=(False, "Scoped authorization required"))
        self.assertEqual(caught.exception.status_code, 423)
        self.assertFalse(state["auto"]["enabled"])
        self.assertFalse(state["live_auto_trade"])

    def test_invalid_confirmation_is_still_rejected_first(self):
        state = execution.initial_state()
        state["policy"]["interval"] = "1h"
        with self.assertRaises(execution.HTTPException) as caught:
            self.start(state, confirmation="WRONG CONFIRMATION")
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("CANLI OTOMATİK", caught.exception.detail)

    def test_non_15m_cannot_bypass_owner_control(self):
        state = execution.initial_state()
        state["policy"]["interval"] = "1h"
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(v25_execution=state)))
        with patch.object(execution, "execution_owner", side_effect=execution.HTTPException(403, "Owner required")), \
                patch.object(execution, "live_auto_start_gate") as gate, \
                patch.object(execution, "live_credentials_status") as credentials, \
                patch.object(execution, "persist_state") as persist:
            with self.assertRaises(execution.HTTPException) as caught:
                asyncio.run(execution.v25_auto_start(request, execution.Confirmation(confirmation="CANLI OTOMATİK")))
        self.assertEqual(caught.exception.status_code, 403)
        gate.assert_not_called()
        credentials.assert_not_called()
        persist.assert_not_called()

    def test_http_endpoint_returns_machine_readable_error_before_safety_or_credentials_reads(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        application = FastAPI()
        application.include_router(execution.router)
        state = execution.initial_state()
        state["policy"]["interval"] = "1h"
        application.state.v25_execution = state
        with patch.object(execution, "execution_owner", return_value={"id": "offline-owner"}), \
                patch.object(execution, "live_auto_start_gate") as gate, \
                patch.object(execution, "live_credentials_status") as credentials, \
                patch.object(execution, "persist_state") as persist, \
                self.assertLogs("app.v25_execution", level="WARNING") as logs, \
                TestClient(application) as client:
            response = client.post("/api/v25/auto/start", json={"confirmation": "CANLI OTOMATİK"})
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "LIVE_AUTO_TIMEFRAME_UNSUPPORTED")
        self.assertEqual(detail["required_interval"], "15m")
        self.assertIn("15m", detail["message"])
        self.assertIn("LIVE_AUTO_TIMEFRAME_UNSUPPORTED", "\n".join(logs.output))
        self.assertFalse(state["auto"]["enabled"])
        self.assertFalse(state["live_auto_trade"])
        gate.assert_not_called()
        credentials.assert_not_called()
        persist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
