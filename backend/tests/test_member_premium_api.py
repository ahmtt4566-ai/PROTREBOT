import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from app import main, v25_execution
from app.analyst_credits import AnalystCredits, CreditConfig, CreditStore, router
from app.commercial_core import default_commercial_state, issue_token
from app.premium_access import public_projection, requires_premium


class MemberPremiumApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.application = FastAPI()
        self.application.middleware("http")(main.owner_preview_gate)
        self.application.include_router(router)
        self.application.state.db_pool = None
        state = default_commercial_state()
        state["users"] = [{"id": user, "role": "CUSTOMER", "active": True, "email_verified": True} for user in ("a", "b", "premium")]
        state["subscriptions"] = [{
            "user_id": "premium", "plan": "MASTER_MODE", "status": "ACTIVE",
            "current_period_end": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }]
        secret = b"test-only-member-credit-session-secret"
        self.application.state.v22_commercial = {"state": state, "secret": secret, "lock": asyncio.Lock()}
        self.tokens = {user: issue_token(user, "CUSTOMER", secret, kind="USER") for user in ("a", "b", "premium")}
        self.producer = AsyncMock(return_value={"symbol": "BTCUSDT", "entry": 987654.321, "stop_loss": 123456.789, "tp1": 999888.777, "direction": "LONG"})
        self.application.state.analyst_analysis = self.producer
        self.application.state.analyst_credits = AnalystCredits(CreditStore(self.application, path=Path(self.directory.name) / "credits.sqlite3"), CreditConfig())
        self.entry_calls = 0

        @self.application.post("/api/v25/order")
        async def entry():
            self.entry_calls += 1
            return {"ok": True}

        @self.application.get("/api/analysis/BTCUSDT")
        async def analysis():
            return {"direction": "LONG", "entry": 987654.321, "stop_loss": 123456.789, "tp1": 999888.777, "long_case": ["private reason"], "nested": {"entryPrice": 987654.321}, "new_private_field": {"price": 333777.111}}

        @self.application.get("/api/analysis-universe")
        async def overview():
            return {"results": [{"symbol": "BTCUSDT", "direction": "LONG", "entry": 987654.321, "tp1": 999888.777}]}

        for name, value in (("WEB_REQUIRE_AUTH", False), ("hydrate_authenticated_user_state", AsyncMock())):
            context = patch.object(main, name, value)
            context.start()
            self.addCleanup(context.stop)
        environment = patch.dict("os.environ", {"PROTREBOT_DURABLE_AUTH_REQUIRED": "false"})
        environment.start()
        self.addCleanup(environment.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.application), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    def headers(self, user):
        return {"Authorization": f"Bearer {self.tokens[user]}"}

    async def test_membership_is_mandatory_and_owner_gate_is_not_guest_access(self):
        for path in ("/api/analyst/credits", "/api/analysis/BTCUSDT"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 401)
        with patch.object(main, "WEB_REQUIRE_AUTH", True), patch.object(main, "WEB_ACCESS_TOKEN", "test-owner-token-at-least-24-characters"):
            response = await self.client.get("/api/analyst/credits", headers={"X-Protrebot-Owner": "test-owner-token-at-least-24-characters"})
            self.assertEqual(response.status_code, 401)

    async def test_http_credits_are_per_member_and_premium_is_unlimited(self):
        response = await self.client.get("/api/analyst/credits", headers=self.headers("a"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 100)
        self.assertEqual(response.json()["analysis_cost"], 10)
        body = {"symbol": "BTCUSDT", "timeframe": "15m", "idempotency_key": "request-000000001"}
        response = await self.client.post("/api/analyst/consume", json=body, headers=self.headers("a"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["remaining"], 90)
        self.assertEqual(response.json()["analysis_cost"], 10)
        self.assertEqual(response.json()["result"]["entry"], 987654.321)
        response = await self.client.get("/api/analyst/credits", headers=self.headers("b"))
        self.assertEqual(response.json()["remaining"], 100)
        response = await self.client.post("/api/analyst/consume", json=body, headers=self.headers("premium"))
        self.assertTrue(response.json()["unlimited"])
        self.assertIsNone(response.json()["remaining"])
        self.assertEqual(response.json()["analysis_cost"], 10)
        response = await self.client.get("/api/analyst/credits", headers=self.headers("premium"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["analysis_cost"], 10)
        self.assertIsNone(response.json()["remaining"])

    async def test_free_member_cannot_call_entry_endpoint_even_without_valid_body(self):
        response = await self.client.post("/api/v25/order", json={}, headers=self.headers("a"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "PREMIUM_REQUIRED")
        self.assertEqual(self.entry_calls, 0)
        response = await self.client.post("/api/v25/order", json={}, headers=self.headers("premium"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.entry_calls, 1)

    async def test_free_read_payload_never_contains_real_private_levels(self):
        for path in ("/api/analysis/BTCUSDT", "/api/analysis-universe"):
            free = await self.client.get(path, headers=self.headers("a"))
            self.assertEqual(free.status_code, 200)
            for secret in ("987654.321", "123456.789", "999888.777", "333777.111", "private reason"):
                self.assertNotIn(secret, free.text)
            paid = await self.client.get(path, headers=self.headers("premium"))
            self.assertIn("987654.321", paid.text)

    async def test_invalid_or_reused_idempotency_key_cannot_authorize_another_purchase(self):
        response = await self.client.post("/api/analyst/consume", json={"symbol": "../../BTCUSDT", "timeframe": "15m", "idempotency_key": "request-000000001"}, headers=self.headers("a"))
        self.assertEqual(response.status_code, 422)
        self.producer.assert_not_awaited()

    async def test_existing_thirty_minute_timeframe_can_be_purchased(self):
        response = await self.client.post("/api/analyst/consume", json={"symbol": "BTCUSDT", "timeframe": "30m", "idempotency_key": "request-000000030"}, headers=self.headers("a"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["remaining"], 90)

    def test_entry_routes_are_guarded_and_read_routes_are_not(self):
        for path in ("/api/v25/order", "/api/v25/order/test", "/api/v25/arm", "/api/v25/auto/start", "/api/exchange-connections/activate", "/api/binance-demo/order", "/api/paper/open", "/api/grid/engine/start"):
            self.assertTrue(requires_premium(path, "POST"), path)
        self.assertFalse(requires_premium("/api/v25/status", "GET"))
        self.assertFalse(requires_premium("/api/analyst/consume", "POST"))

    def test_projection_removes_nested_fields_and_keeps_public_market_prices(self):
        payload = {"symbol": "BTCUSDT", "price": 10, "data": [{"entry": 99, "stopLoss": 80, "targets": [100], "direction": "SHORT"}]}
        self.assertEqual(public_projection(payload), {"symbol": "BTCUSDT", "price": 10, "data": [{"direction": "SHORT"}]})
