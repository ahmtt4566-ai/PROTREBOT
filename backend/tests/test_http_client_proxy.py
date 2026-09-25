import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import main as main_module


class HttpClientProxyTests(unittest.TestCase):
    def test_missing_quotaguard_url_uses_direct_client_configuration(self):
        with patch.dict(os.environ, {}, clear=False), patch.dict(os.environ, {"QUOTAGUARD_URL": ""}), patch.object(main_module.httpx, "AsyncClient") as async_client:
            main_module.build_http_client()

        kwargs = async_client.call_args.kwargs
        self.assertIsNone(kwargs["proxy"])
        self.assertFalse(kwargs["trust_env"])

    def test_quotaguard_url_is_applied_to_shared_client(self):
        proxy_url = "http://proxy-user:proxy-secret@eu-central-static-01.quotaguard.com:9293"
        with patch.dict(os.environ, {"QUOTAGUARD_URL": proxy_url}), patch.object(main_module.httpx, "AsyncClient") as async_client:
            main_module.build_http_client()

        kwargs = async_client.call_args.kwargs
        self.assertEqual(kwargs["proxy"], proxy_url)
        self.assertFalse(kwargs["trust_env"])

    def test_invalid_quotaguard_url_fails_without_exposing_value(self):
        proxy_url = "ftp://proxy-user:proxy-secret@eu-central-static-01.quotaguard.com:9293"
        with patch.dict(os.environ, {"QUOTAGUARD_URL": proxy_url}):
            with self.assertRaises(ValueError) as context:
                main_module.build_http_client()

        self.assertNotIn(proxy_url, str(context.exception))
        self.assertNotIn("proxy-secret", str(context.exception))
        self.assertIn("QUOTAGUARD_URL", str(context.exception))

    def test_market_data_request_returns_normal_response(self):
        response = httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", "https://example.test"))

        class FastClient:
            async def get(self, url, params=None):
                return response

        application = SimpleNamespace(state=SimpleNamespace(http=FastClient()))
        result = asyncio.run(main_module.market_data_request(application, "/fapi/v1/ping"))

        self.assertIs(result, response)

    def test_market_data_request_is_bounded_when_rate_limit_wait_hangs(self):
        class HangingClient:
            async def get(self, url, params=None):
                await asyncio.Event().wait()

        application = SimpleNamespace(state=SimpleNamespace(http=HangingClient()))
        with patch.object(main_module, "MARKET_DATA_REQUEST_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(httpx.ReadTimeout):
                asyncio.run(main_module.market_data_request(application, "/fapi/v1/ping"))


if __name__ == "__main__":
    unittest.main()