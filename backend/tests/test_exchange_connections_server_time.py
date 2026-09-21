import asyncio
import sys
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app import exchange_connections


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


class ExchangeConnectionServerTimeTests(unittest.TestCase):
    def setUp(self):
        exchange_connections._SERVER_TIME_CACHE.clear()
        exchange_connections._SERVER_TIME_LOCKS.clear()

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

    def test_http_418_retry_after_is_guidance_only(self):
        http = FakeBinanceHttp(time_status=418, time_headers={"Retry-After": "5"})
        with self.assertRaisesRegex(exchange_connections.VaultError, "wait 5 seconds"):
            asyncio.run(exchange_connections._server_time_offset(http, "LIVE"))
        self.assertEqual(http.paths, ["/fapi/v1/time"])

    def test_malformed_retry_after_is_safe(self):
        http = FakeBinanceHttp(time_status=418, time_headers={"Retry-After": "999999999999999999999999999"})
        with self.assertRaisesRegex(exchange_connections.VaultError, "wait briefly"):
            asyncio.run(exchange_connections._server_time_offset(http, "LIVE"))

    def test_timeout_fails_closed_without_account_request(self):
        http = FakeBinanceHttp(time_error=httpx.ReadTimeout("timeout"))
        with self.assertRaisesRegex(exchange_connections.VaultError, "zaman aşımı"):
            asyncio.run(exchange_connections.test_binance_credentials(http, "LIVE", "api-key-safe", "secret-safe"))
        self.assertEqual(http.paths, ["/fapi/v1/time"])

    def test_server_time_error_does_not_expose_credentials(self):
        api_key = "api-key-that-must-stay-hidden"
        secret_key = "secret-that-must-stay-hidden"
        http = FakeBinanceHttp(time_status=418)
        with self.assertRaises(exchange_connections.VaultError) as context:
            asyncio.run(exchange_connections.test_binance_credentials(http, "LIVE", api_key, secret_key))
        self.assertNotIn(api_key, str(context.exception))
        self.assertNotIn(secret_key, str(context.exception))


if __name__ == "__main__":
    unittest.main()
