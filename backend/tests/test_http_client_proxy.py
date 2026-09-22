import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()