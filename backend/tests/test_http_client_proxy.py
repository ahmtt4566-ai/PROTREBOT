import asyncio
import ast
import logging
import os
import sys
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.binance_rate_limit import BinanceRateLimiter


def load_http_helpers():
    """Test the actual transport functions independently of database startup."""
    source = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    names = {
        "_build_http_client", "build_http_client", "build_market_http_client",
        "_ensure_http_client", "ensure_http_client", "ensure_market_data_http_client",
        "market_data_request", "market_data_http_exception",
    }
    nodes = [
        node for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    required = {"build_http_client", "ensure_http_client", "market_data_request", "market_data_http_exception"}
    assert required <= {node.name for node in nodes}
    module = ModuleType("isolated_market_http")
    module.__dict__.update({
        "asyncio": asyncio, "httpx": httpx, "os": os, "time": time,
        "urlsplit": urlsplit, "Any": Any, "Callable": Callable,
        "FastAPI": FastAPI, "HTTPException": HTTPException,
        "logger": logging.getLogger("app.main"),
        "BINANCE_RATE_LIMITER": BinanceRateLimiter(),
        "FUTURES_MARKET_DATA_API": "https://primary.test",
        "FUTURES_MARKET_DATA_APIS": ("https://primary.test", "https://fallback.test"),
        "MARKET_DATA_REQUEST_TIMEOUT_SECONDS": 8,
    })
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<market-http-helpers>", "exec"), module.__dict__)
    return module


main_module = load_http_helpers()


class HttpClientProxyTests(unittest.TestCase):
    def setUp(self):
        main_module.BINANCE_RATE_LIMITER.reset()

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

    def test_market_data_request_falls_back_after_transient_primary_failures(self):
        requests = []

        class FailoverClient:
            async def get(self, url, params=None):
                requests.append(url)
                status = 503 if len(requests) <= 3 else 200
                return httpx.Response(status, json={"ok": status == 200}, request=httpx.Request("GET", url))

        application = SimpleNamespace(state=SimpleNamespace(http=FailoverClient()))
        with patch.object(main_module, "FUTURES_MARKET_DATA_APIS", ("https://primary.test", "https://fallback.test")):
            result = asyncio.run(main_module.market_data_request(application, "/fapi/v1/ping"))

        self.assertEqual(result.status_code, 200)
        self.assertEqual(requests, [
            "https://primary.test/fapi/v1/ping",
            "https://fallback.test/fapi/v1/ping",
            "https://fallback.test/fapi/v1/ping",
            "https://fallback.test/fapi/v1/ping",
        ])

    def test_market_data_request_is_bounded_when_rate_limit_wait_hangs(self):
        class HangingClient:
            async def get(self, url, params=None):
                await asyncio.Event().wait()

        application = SimpleNamespace(state=SimpleNamespace(http=HangingClient()))
        with patch.object(main_module, "MARKET_DATA_REQUEST_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(httpx.ReadTimeout):
                asyncio.run(main_module.market_data_request(application, "/fapi/v1/ping"))

    def test_public_market_data_recovers_from_proxy_failure_without_rerouting_signed_client(self):
        requests = []

        class ProxyClient:
            async def get(self, url, params=None):
                raise httpx.ProxyError("Synthetic proxy tunnel unavailable")

        class DirectClient:
            async def get(self, url, params=None):
                requests.append((url, params))
                return httpx.Response(200, json=[], request=httpx.Request("GET", url))

        signed_client = ProxyClient()
        direct_client = DirectClient()
        application = SimpleNamespace(state=SimpleNamespace(http=signed_client, market_http=direct_client))
        with patch.object(main_module, "FUTURES_MARKET_DATA_APIS", ("https://market.test",)), patch.object(main_module, "MARKET_DATA_REQUEST_TIMEOUT_SECONDS", 0.05):
            result = asyncio.run(main_module.market_data_request(application, "/fapi/v1/klines", {"symbol": "BTCUSDT"}))

        self.assertEqual(result.status_code, 200)
        self.assertEqual(requests, [("https://market.test/fapi/v1/klines", {"symbol": "BTCUSDT"})])
        self.assertIs(application.state.http, signed_client)

    def test_proxy_transport_error_is_not_reported_as_binance_http_503(self):
        error = main_module.market_data_http_exception("Market", httpx.ProxyError("Synthetic tunnel unavailable"))
        self.assertEqual(error.status_code, 503)
        self.assertIn("proxy", error.detail.lower())
        self.assertNotIn("HTTP 503", error.detail)

    def test_public_direct_client_never_inherits_proxy_or_environment(self):
        with patch.dict(os.environ, {"QUOTAGUARD_URL": "http://synthetic.test:9293", "HTTPS_PROXY": "http://unrelated.test:8080"}), patch.object(main_module.httpx, "AsyncClient") as factory:
            main_module.build_market_http_client()
        self.assertIsNone(factory.call_args.kwargs["proxy"])
        self.assertFalse(factory.call_args.kwargs["trust_env"])

    def test_failed_public_proxy_is_not_retried_by_every_market_refresh(self):
        requests = []

        class ProxyClient:
            async def get(self, url, params=None):
                requests.append("proxy")
                raise httpx.ProxyError("Synthetic proxy unavailable")

        class DirectClient:
            async def get(self, url, params=None):
                requests.append("direct")
                return httpx.Response(200, json=[], request=httpx.Request("GET", url))

        application = SimpleNamespace(state=SimpleNamespace(http=ProxyClient(), market_http=DirectClient()))

        async def run_case():
            for _ in range(2):
                result = await main_module.market_data_request(application, "/fapi/v1/klines")
                self.assertEqual(result.status_code, 200)

        asyncio.run(run_case())
        self.assertEqual(requests, ["proxy", "direct", "direct"])

    def test_signed_endpoints_cannot_use_public_transport(self):
        application = SimpleNamespace(state=SimpleNamespace())
        with self.assertRaisesRegex(ValueError, "public read-only"):
            asyncio.run(main_module.market_data_request(application, "/fapi/v1/order", {"signature": "synthetic"}))
        self.assertFalse(hasattr(application.state, "http"))
        self.assertFalse(hasattr(application.state, "market_http"))

    def test_rate_limit_and_region_denials_never_switch_transport(self):
        for status in [418, 429, 451]:
            calls = []

            class DeniedClient:
                async def get(self, url, params=None):
                    calls.append(url)
                    return httpx.Response(status, json={"msg": "Synthetic denial"}, request=httpx.Request("GET", url))

            application = SimpleNamespace(state=SimpleNamespace(http=DeniedClient()))
            with patch.dict(os.environ, {"QUOTAGUARD_URL": "http://synthetic.test:9293"}):
                result = asyncio.run(main_module.market_data_request(application, "/fapi/v1/klines"))
            self.assertEqual(result.status_code, status)
            self.assertEqual(len(calls), 1)
            self.assertFalse(hasattr(application.state, "market_http"))
            main_module.BINANCE_RATE_LIMITER.reset()

    def test_market_timeout_budget_is_total_not_multiplied_by_hosts(self):
        class HangingClient:
            async def get(self, url, params=None):
                await asyncio.Event().wait()

        application = SimpleNamespace(state=SimpleNamespace(http=HangingClient()))

        async def run_case():
            with self.assertRaises(httpx.ReadTimeout):
                await asyncio.wait_for(
                    main_module.market_data_request(application, "/fapi/v1/ping"),
                    timeout=0.08,
                )

        with patch.object(main_module, "FUTURES_MARKET_DATA_APIS", ("https://first.test", "https://second.test")), patch.object(main_module, "MARKET_DATA_REQUEST_TIMEOUT_SECONDS", 0.05):
            asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()