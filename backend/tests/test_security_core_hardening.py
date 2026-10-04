import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, Response
from starlette.requests import Request

from app import main
from app.assistant_storage import AssistantStorageError, AssistantStore
from app.browser_security import (
    OWNER_ACCESS_COOKIE, USER_SESSION_COOKIE, clear_browser_cookie, set_browser_cookie,
    validate_browser_request,
)
from app.premium_access import requires_premium


def request(application=None, *, method="POST", host="app.example", scheme="https", headers=None):
    return Request({
        "type": "http", "scheme": scheme, "server": (host, 443 if scheme == "https" else 80), "path": "/api/test",
        "method": method, "query_string": b"", "client": ("127.0.0.1", 1234),
        "headers": [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()],
        "app": application, "state": {"request_id": "synthetic-security-test"},
    })


class BrowserSecurityTests(unittest.TestCase):
    def test_cookie_flags_and_host_only_path_are_consistent_on_delete(self):
        response = Response()
        set_browser_cookie(response, request(), USER_SESSION_COOKIE, "synthetic-session-not-live", max_age=120)
        cookie = response.headers["set-cookie"]
        for expected in ("HttpOnly", "Secure", "SameSite=lax", "Path=/api", "Max-Age=120"):
            self.assertIn(expected, cookie)
        self.assertNotIn("Domain=", cookie)
        deleted = Response()
        clear_browser_cookie(deleted, request(), USER_SESSION_COOKIE)
        self.assertIn("Max-Age=0", deleted.headers["set-cookie"])
        self.assertIn("Path=/api", deleted.headers["set-cookie"])

    def test_secure_cookie_is_not_downgraded_by_forwarded_headers(self):
        response = Response()
        set_browser_cookie(response, request(headers={"X-Forwarded-Proto": "http"}), OWNER_ACCESS_COOKIE, "synthetic-owner")
        self.assertIn("Secure", response.headers["set-cookie"])

    def test_loopback_development_cookie_support(self):
        response = Response()
        with patch.dict("os.environ", {"PROTREBOT_DURABLE_AUTH_REQUIRED": "false"}):
            set_browser_cookie(response, request(host="localhost", scheme="http"), USER_SESSION_COOKIE, "synthetic-session-not-live")
        self.assertNotIn("Secure", response.headers["set-cookie"])
        self.assertIn("HttpOnly", response.headers["set-cookie"])

    def test_https_and_durable_loopback_cookies_cannot_downgrade(self):
        for current in (request(host="localhost"), request(host="localhost", scheme="http")):
            response = Response()
            with patch.dict("os.environ", {"PROTREBOT_DURABLE_AUTH_REQUIRED": "true"}):
                set_browser_cookie(response, current, USER_SESSION_COOKIE, "synthetic-session-not-live")
            self.assertIn("Secure", response.headers["set-cookie"])
    def test_cookie_mutations_require_browser_header_and_allowed_origin(self):
        for headers in (
            {"Cookie": f"{USER_SESSION_COOKIE}=synthetic-session-not-live"},
            {"Cookie": f"{USER_SESSION_COOKIE}=synthetic-session-not-live", "X-Requested-With": "XMLHttpRequest"},
            {"Cookie": f"{USER_SESSION_COOKIE}=synthetic-session-not-live", "X-Requested-With": "XMLHttpRequest", "Origin": "https://evil.example"},
        ):
            with self.assertRaises(HTTPException) as caught:
                validate_browser_request(request(headers=headers), ["https://frontend.example"])
            self.assertEqual(caught.exception.status_code, 403)
        validate_browser_request(request(headers={"Cookie": f"{USER_SESSION_COOKIE}=synthetic-session-not-live", "X-Requested-With": "XMLHttpRequest", "Origin": "https://frontend.example"}), ["https://frontend.example"])

    def test_native_bearer_and_read_only_requests_do_not_require_csrf_header(self):
        validate_browser_request(request(headers={"Authorization": "Bearer synthetic-native-not-live"}), [])
        validate_browser_request(request(method="GET", headers={"Cookie": f"{USER_SESSION_COOKIE}=synthetic-session-not-live"}), [])

    def test_public_docs_routes_are_not_registered(self):
        self.assertIsNone(main.app.docs_url)
        self.assertIsNone(main.app.redoc_url)
        self.assertIsNone(main.app.openapi_url)
        paths = {getattr(route, "path", None) for route in main.app.routes}
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertNotIn(path, paths)


class CoreSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_assistant_storage_cannot_initialize_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.sqlite3"
            store = AssistantStore(SimpleNamespace(state=SimpleNamespace(db_pool=None)), path=path)
            with patch.dict("os.environ", {"PROTREBOT_DURABLE_AUTH_REQUIRED": "true"}):
                with self.assertRaisesRegex(AssistantStorageError, "durable storage"):
                    async with store.connection(1):
                        self.fail("Durable storage accepted SQLite")
            self.assertFalse(path.exists())
            self.assertIsNone(store.storage_kind)

    async def test_explicit_local_assistant_storage_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.sqlite3"
            store = AssistantStore(SimpleNamespace(state=SimpleNamespace(db_pool=None)), path=path)
            with patch.dict("os.environ", {"PROTREBOT_DURABLE_AUTH_REQUIRED": "false"}):
                async with store.connection(1) as transaction:
                    self.assertFalse(transaction.postgres)
            self.assertTrue(path.exists())

    async def test_grid_list_and_clear_are_scoped_and_legacy_rows_fail_closed(self):
        plans = [
            {"id": "a-plan", "user_id": "a", "active": True},
            {"id": "b-plan", "user_id": "b", "active": True},
            {"id": "legacy-plan", "active": True},
        ]
        application = SimpleNamespace(state=SimpleNamespace(paper={"grid_plans": plans, "lock": asyncio.Lock()}))
        with patch.object(main, "authenticated_user", return_value={"id": "a"}), patch.object(main, "persist_paper_snapshot", AsyncMock()) as persist:
            current = request(application)
            result = await main.saved_grid_plans(current)
            self.assertEqual([row["id"] for row in result["plans"]], ["a-plan"])
            await main.clear_grid_plan("b-plan", current)
            await main.clear_grid_plan("legacy-plan", current)
            self.assertTrue(plans[1]["active"])
            self.assertTrue(plans[2]["active"])
            persist.assert_not_called()
            await main.clear_grid_plan("a-plan", current)
            await asyncio.sleep(0)
            self.assertFalse(plans[0]["active"])
            persist.assert_awaited_once_with(application)
        self.assertTrue(requires_premium("/api/grid/plan/clear/a-plan", "POST"))

    async def test_grid_save_does_not_deactivate_or_trim_another_members_plan(self):
        other = {"id": "b-plan", "user_id": "b", "symbol": "BTCUSDT", "interval": "15m", "active": True}
        own = [{"id": str(index), "user_id": "a", "symbol": "BTCUSDT", "interval": "15m", "active": True} for index in range(main.GRID_PLAN_LIMIT)]
        application = SimpleNamespace(state=SimpleNamespace(paper={"grid_plans": [other, *own], "lock": asyncio.Lock()}))
        generated = {"symbol": "BTCUSDT", "interval": "15m", "mode": "NEUTRAL", "paper_eligible": True}
        with patch.object(main, "authenticated_user", return_value={"id": "a"}), patch.object(main, "smart_grid_plan", AsyncMock(return_value=generated)), patch.object(main, "add_paper_notification"), patch.object(main, "persist_paper_snapshot", AsyncMock()):
            saved = await main.save_grid_plan(main.GridPlanRequest(symbol="BTCUSDT"), request(application))
            await asyncio.sleep(0)
        self.assertEqual(saved["plan"]["user_id"], "a")
        self.assertTrue(other["active"])
        self.assertIn(other, application.state.paper["grid_plans"])
        self.assertEqual(sum(row.get("user_id") == "a" for row in application.state.paper["grid_plans"]), main.GRID_PLAN_LIMIT)

    async def test_secondary_grid_and_notification_projections_are_scoped(self):
        paper = {"grid_plans": [{"id": "a", "user_id": "a"}, {"id": "b", "user_id": "b"}, {"id": "legacy"}]}
        self.assertEqual([row["id"] for row in main.owned_grid_plans(paper, "a")], ["a"])
        self.assertEqual(main.owned_grid_plans(paper, None), [])
        main.add_paper_notification(paper, "V5 GRID PLANI", "same-symbol", user_id="a")
        main.add_paper_notification(paper, "V5 GRID PLANI", "same-symbol", user_id="b")
        main.add_paper_notification(paper, "V5 GRID PLANI", "unknown-owner")
        main.add_paper_notification(paper, "SYSTEM", "public-status")
        own = main.owned_paper_notifications(paper, "a")
        self.assertEqual(len(own), 2)
        self.assertEqual({row.get("user_id") for row in own}, {None, "a"})
        self.assertEqual([row["kind"] for row in main.owned_paper_notifications(paper, None)], ["SYSTEM"])
        for index in range(25):
            main.add_paper_notification(paper, "V5 GRID PLANI", str(index), user_id="b")
        self.assertEqual(sum(row.get("user_id") == "b" for row in paper["notifications"]), 20)
        self.assertEqual(sum(row.get("user_id") == "a" for row in paper["notifications"]), 1)

    async def test_client_error_limit_is_persistent_and_cannot_trigger_critical_alert(self):
        application = SimpleNamespace(state=SimpleNamespace(db_pool=None))
        current = request(application)
        with patch.object(main, "log_event", AsyncMock()) as record, patch.object(main.time, "monotonic", return_value=100):
            for _ in range(30):
                await main.client_error(current, {"message": "synthetic-error", "severity": "CRITICAL"})
            with self.assertRaises(HTTPException) as caught:
                await main.client_error(current, {"message": "synthetic-error"})
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(record.await_count, 30)
            self.assertTrue(all(call.args[1]["severity"] == "ERROR" for call in record.await_args_list))
        self.assertEqual(len(application.state.client_error_buckets["127.0.0.1"]), 30)

    async def test_error_responses_also_get_browser_security_headers(self):
        response = main.apply_cors_headers(request(), Response(status_code=401))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    async def test_revocation_clears_cookie_but_storage_outage_preserves_it(self):
        application = SimpleNamespace(state=SimpleNamespace())
        for status in (401, 503):
            current = request(application, method="GET", headers={"Cookie": f"{USER_SESSION_COOKIE}=synthetic-session-not-live"})
            downstream = AsyncMock()
            with patch.object(main, "evaluate_access", return_value=SimpleNamespace(allowed=True)), patch.object(
                main, "authenticated_user_async", AsyncMock(side_effect=HTTPException(status, "Synthetic auth failure"))
            ) as authenticate:
                response = await main.owner_preview_gate(current, downstream)
            authenticate.assert_awaited_once_with(current)
            downstream.assert_not_awaited()
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            if status == 401:
                self.assertIn("Max-Age=0", response.headers["set-cookie"])
            else:
                self.assertNotIn("set-cookie", response.headers)
