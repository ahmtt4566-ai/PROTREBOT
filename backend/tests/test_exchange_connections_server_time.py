import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app import exchange_connections
from app.exchange_connections import SaveCredentialsRequest, TestCredentialsRequest, exchange_connection_activate, exchange_connection_save, exchange_connection_test


class FakeBinanceHttp:
    def __init__(self, *, time_status=200, time_headers=None, time_payload=None, time_error=None):
        self.time_status = time_status
        self.time_headers = time_headers or {}
        self.time_payload = time_payload if time_payload is not None else {"serverTime": 1_700_000_000_000}
        self.time_error = time_error
        self.paths = []

    async def get(self, url, headers=None):
        path = url.split("https://fapi.binance.com", 1)[-1].split("?", 1)[0]
        self.paths.append(path)
        if path == "/fapi/v1/time":
            if self.time_error:
                raise self.time_error
            return httpx.Response(
                self.time_status,
                headers=self.time_headers,
                json=self.time_payload,
                request=httpx.Request("GET", url),
            )
        if path == "/fapi/v3/account":
            return httpx.Response(
                200,
                json={
                    "totalWalletBalance": "100",
                    "availableBalance": "90",
                    "totalUnrealizedProfit": "0",
                    "positions": [],
                },
                request=httpx.Request("GET", url),
            )
        if path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": False}, request=httpx.Request("GET", url))
        raise AssertionError(f"Unexpected Binance path: {path}")


class HangingBinanceHttp:
    async def get(self, url, headers=None):
        await asyncio.Event().wait()


class ExchangeConnectionServerTimeTests(unittest.TestCase):
    def setUp(self):
        exchange_connections._SERVER_TIME_CACHE.clear()
        exchange_connections._SERVER_TIME_REJECTION_CACHE.clear()
        exchange_connections._SERVER_TIME_LOCKS.clear()
        exchange_connections._SESSION_CACHE.clear()
        exchange_connections._SESSION_META.clear()
        exchange_connections.BINANCE_RATE_LIMITER.reset()

    def test_server_time_200_continues_to_read_only_account_checks(self):
        http = FakeBinanceHttp()
        result = asyncio.run(exchange_connections.test_binance_credentials(http, "LIVE", "api-key-safe", "secret-safe"))
        self.assertEqual(result["active_positions"], 0)
        self.assertEqual(http.paths, ["/fapi/v1/time", "/fapi/v3/account", "/fapi/v1/positionSide/dual"])

    def test_fresh_cache_and_concurrent_requests_share_one_server_time_call(self):
        http = FakeBinanceHttp()

        async def run():
            return await asyncio.gather(
                exchange_connections._server_time_offset(http, "LIVE"),
                exchange_connections._server_time_offset(http, "LIVE"),
            )

        offsets = asyncio.run(run())
        self.assertEqual(offsets[0], offsets[1])
        self.assertEqual(http.paths, ["/fapi/v1/time"])

    def test_expired_cache_fetches_server_time_again(self):
        http = FakeBinanceHttp()
        asyncio.run(exchange_connections._server_time_offset(http, "LIVE"))
        expiry, offset = exchange_connections._SERVER_TIME_CACHE["LIVE"]
        exchange_connections._SERVER_TIME_CACHE["LIVE"] = (expiry - exchange_connections.SERVER_TIME_CACHE_TTL_SECONDS - 1, offset)
        asyncio.run(exchange_connections._server_time_offset(http, "LIVE"))
        self.assertEqual(http.paths, ["/fapi/v1/time", "/fapi/v1/time"])

    def test_http_418_fails_closed_and_does_not_check_account(self):
        http = FakeBinanceHttp(time_status=418, time_payload={"code": -1003, "msg": "rate limited"})
        with self.assertRaisesRegex(exchange_connections.VaultError, "HTTP 418.*wait briefly"):
            asyncio.run(exchange_connections.test_binance_credentials(http, "LIVE", "api-key-safe", "secret-safe"))
        self.assertEqual(http.paths, ["/fapi/v1/time"])

    def test_http_429_fails_closed_and_does_not_check_account(self):
        http = FakeBinanceHttp(time_status=429, time_payload={"code": -1003, "msg": "rate limited"})
        with self.assertRaisesRegex(exchange_connections.VaultError, "HTTP 429"):
            asyncio.run(exchange_connections.test_binance_credentials(http, "LIVE", "api-key-safe", "secret-safe"))
        self.assertEqual(http.paths, ["/fapi/v1/time"])

    def test_http_418_retry_after_is_guidance_only(self):
        http = FakeBinanceHttp(time_status=418, time_headers={"Retry-After": "5"})
        with self.assertLogs(exchange_connections.logger, level="WARNING") as logs:
            with self.assertRaisesRegex(exchange_connections.VaultError, "wait 5 seconds"):
                asyncio.run(exchange_connections._server_time_offset(http, "LIVE"))
        self.assertEqual(http.paths, ["/fapi/v1/time"])
        self.assertIn("retry_after_seconds=5", " ".join(logs.output))

    def test_http_418_is_negative_cached_for_retry_window(self):
        http = FakeBinanceHttp(time_status=418, time_headers={"Retry-After": "5"})

        async def run():
            errors = []
            for _ in range(2):
                try:
                    await exchange_connections._server_time_offset(http, "LIVE")
                except exchange_connections.BinanceServerTimeRejected as exc:
                    errors.append(exc)
            return errors

        errors = asyncio.run(run())
        self.assertEqual(http.paths, ["/fapi/v1/time"])
        self.assertEqual(errors[0].retry_after, 5)
        self.assertEqual(str(errors[0]), str(errors[1]))

    def test_malformed_retry_after_is_safe(self):
        http = FakeBinanceHttp(time_status=418, time_headers={"Retry-After": "999999999999999999999999999"})
        with self.assertRaisesRegex(exchange_connections.VaultError, "wait briefly"):
            asyncio.run(exchange_connections._server_time_offset(http, "LIVE"))

    def test_timeout_fails_closed_without_account_request(self):
        http = FakeBinanceHttp(time_error=httpx.ReadTimeout("timeout"))
        with self.assertRaisesRegex(exchange_connections.VaultError, "zaman aşımı"):
            asyncio.run(exchange_connections.test_binance_credentials(http, "LIVE", "api-key-safe", "secret-safe"))
        self.assertEqual(http.paths, ["/fapi/v1/time"])

    def test_verification_timeout_fails_closed(self):
        with patch.object(exchange_connections, "BINANCE_VERIFICATION_REQUEST_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(exchange_connections.VaultError, "zaman aşımı"):
                asyncio.run(exchange_connections.test_binance_credentials(HangingBinanceHttp(), "LIVE", "api-key-safe", "secret-safe"))

    def test_server_time_error_does_not_expose_credentials(self):
        api_key = "api-key-that-must-stay-hidden"
        secret_key = "secret-that-must-stay-hidden"
        http = FakeBinanceHttp(time_status=418)
        with self.assertRaises(exchange_connections.VaultError) as context:
            asyncio.run(exchange_connections.test_binance_credentials(http, "LIVE", api_key, secret_key))
        self.assertNotIn(api_key, str(context.exception))
        self.assertNotIn(secret_key, str(context.exception))

    def _request(self):
        pool = AsyncMock()
        pool.fetch.return_value = []
        application = SimpleNamespace(
            state=SimpleNamespace(
                db_pool=pool,
                http=AsyncMock(),
                exchange_vault={"ready": True, "storage": "POSTGRESQL + FERNET", "reason": None, "loaded_at": "now", "pool_id": id(pool)},
            )
        )
        request = SimpleNamespace(
            app=application,
            headers={"authorization": "Bearer server-time-test-token"},
            state=SimpleNamespace(member={"id": "owner-server-time", "role": "OWNER"}),
        )
        return request

    def test_test_endpoint_maps_server_time_418_to_controlled_rate_limit(self):
        request = self._request()
        rejection = exchange_connections.BinanceServerTimeRejected("Binance server-time temporarily rejected the request (HTTP 418). Please wait 5 seconds before retrying.", retry_after=5)
        with patch("app.exchange_connections.test_binance_credentials", new=AsyncMock(side_effect=rejection)):
            with self.assertRaises(exchange_connections.HTTPException) as context:
                asyncio.run(exchange_connection_test(request, exchange_connections.TestCredentialsRequest(mode="LIVE", api_key="api-key-safe", secret_key="secret-safe")))
        self.assertEqual(context.exception.status_code, 429)
        self.assertEqual(context.exception.headers["Retry-After"], "5")
        self.assertEqual(context.exception.detail["code"], "BINANCE_RATE_LIMITED")
        self.assertEqual(context.exception.detail["upstream_status"], 418)
        self.assertNotIn("HTTP 418", context.exception.detail["detail"])
        self.assertNotIn("server-time temporarily rejected", context.exception.detail["detail"])
        self.assertIn("5 saniye", context.exception.detail["detail"])
        self.assertNotIn("api-key-safe", str(context.exception.detail))
        self.assertNotIn("secret-safe", str(context.exception.detail))

    def test_test_endpoint_preserves_upstream_429_diagnostics(self):
        request = self._request()
        rejection = exchange_connections.BinanceServerTimeRejected(
            "Binance server-time temporarily rejected the request (HTTP 429).",
            retry_after=7,
            upstream_status=429,
            exchange_code=-1003,
            used_weight_1m="2401",
        )
        with patch("app.exchange_connections.test_binance_credentials", new=AsyncMock(side_effect=rejection)):
            with self.assertRaises(exchange_connections.HTTPException) as context:
                asyncio.run(exchange_connection_test(request, TestCredentialsRequest(mode="LIVE", api_key="api-key-safe", secret_key="secret-safe")))
        self.assertEqual(context.exception.status_code, 429)
        self.assertEqual(context.exception.headers["Retry-After"], "7")
        self.assertEqual(context.exception.detail["upstream_status"], 429)
        self.assertEqual(context.exception.detail["exchange_code"], -1003)
        self.assertEqual(context.exception.detail["used_weight_1m"], "2401")

    def test_test_endpoint_returns_safe_verification_metadata(self):
        request = self._request()
        account = {"tested_at": "2026-09-22T12:00:00+00:00", "wallet_balance": 100.0, "orders_created": False}
        with patch("app.exchange_connections.test_binance_credentials", new=AsyncMock(return_value=account)):
            result = asyncio.run(exchange_connection_test(request, TestCredentialsRequest(mode="LIVE", api_key="api-key-safe", secret_key="secret-safe")))
        self.assertTrue(result["ok"])
        self.assertEqual(result["account"], account)
        self.assertEqual(result["fingerprint"], exchange_connections.key_fingerprint("api-key-safe"))
        self.assertNotIn("api-key-safe", str(result))
        self.assertNotIn("secret-safe", str(result))

    def test_save_endpoint_maps_server_time_418_before_persisting(self):
        request = self._request()
        rejection = exchange_connections.BinanceServerTimeRejected("Binance server-time temporarily rejected the request (HTTP 418). Please wait briefly before retrying.", retry_after=10)
        with patch("app.exchange_connections.test_binance_credentials", new=AsyncMock(side_effect=rejection)):
            with self.assertRaises(exchange_connections.HTTPException) as context:
                asyncio.run(exchange_connection_save(request, SaveCredentialsRequest(mode="LIVE", api_key="api-key-safe", secret_key="secret-safe", confirmation="CANLI KASAYA KAYDET")))
        self.assertEqual(context.exception.status_code, 429)
        self.assertEqual(context.exception.headers["Retry-After"], "10")
        self.assertEqual(context.exception.detail["code"], "BINANCE_RATE_LIMITED")
        self.assertNotIn("HTTP 418", context.exception.detail["detail"])
        self.assertEqual(request.app.state.db_pool.execute.await_count, 2)

    def test_activate_endpoint_maps_server_time_418_without_storing_technical_detail(self):
        request = self._request()
        session_key = (exchange_connections.session_id(request), "LIVE")
        exchange_connections._SESSION_CACHE[session_key] = ("api-key-safe", "secret-safe")
        exchange_connections._SESSION_META[session_key] = {"active": True, "configured": True}
        rejection = exchange_connections.BinanceServerTimeRejected("Binance server-time temporarily rejected the request (HTTP 418). Please wait 7 seconds before retrying.", retry_after=7)
        with patch("app.exchange_connections.test_binance_credentials", new=AsyncMock(side_effect=rejection)):
            with self.assertRaises(exchange_connections.HTTPException) as context:
                asyncio.run(exchange_connection_activate(request, exchange_connections.ConnectionActionRequest(mode="LIVE", confirmation="CANLI SALT OKUNUR BAĞLANTIYI AÇ")))
        self.assertEqual(context.exception.status_code, 429)
        self.assertEqual(context.exception.headers["Retry-After"], "7")
        self.assertEqual(context.exception.detail["code"], "BINANCE_RATE_LIMITED")
        self.assertNotIn("HTTP 418", context.exception.detail["detail"])
        self.assertNotIn("HTTP 418", exchange_connections._SESSION_META[session_key]["last_error"])


if __name__ == "__main__":
    unittest.main()
