import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.web_security import bootstrap_access_allowed, bearer_token, cors_origins, env_flag, evaluate_access, is_allowed_cors_origin


class WebSecurityTests(unittest.TestCase):
    def test_custom_domain_and_legacy_domain_pass_real_auth_origin_checks(self):
        from fastapi.testclient import TestClient
        from app import main

        client = TestClient(main.app, base_url="https://protrebot-rkpt.onrender.com")
        for origin in ("https://kaistrade.com", "https://frontend-nu-two-18.vercel.app"):
            with self.subTest(origin=origin):
                response = client.options("/api/v22/auth/login", headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type,x-requested-with",
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get("access-control-allow-origin"), origin)
                self.assertEqual(response.headers.get("access-control-allow-credentials"), "true")
                invalid_login = client.post("/api/v22/auth/login", json={}, headers={
                    "Origin": origin, "X-Requested-With": "XMLHttpRequest",
                })
                self.assertEqual(invalid_login.status_code, 422)
                self.assertNotIn("Browser request origin is not allowed", invalid_login.text)
                with patch.object(main.app.state, "v22_commercial", {
                    "secret": b"synthetic-offline-domain-test-secret", "state": {"users": []},
                }, create=True):
                    protected = client.get("/api/v22/me", headers={"Origin": origin})
                self.assertEqual(protected.status_code, 401)
        client.close()

    def test_untrusted_domains_still_fail_real_auth_origin_checks(self):
        from fastapi.testclient import TestClient
        from app import main

        client = TestClient(main.app, base_url="https://protrebot-rkpt.onrender.com")
        for origin in ("https://evil.example", "https://kaistrade.com.evil.example", "http://kaistrade.com"):
            with self.subTest(origin=origin):
                response = client.post("/api/v22/auth/login", json={}, headers={
                    "Origin": origin, "X-Requested-With": "XMLHttpRequest",
                })
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["detail"], "Browser request origin is not allowed")
                self.assertNotIn("access-control-allow-origin", response.headers)
        client.close()

    def test_local_mode_can_be_disabled(self):
        decision = evaluate_access(
            required=False, configured_token="", authorization=None,
            path="/api/markets", method="GET",
        )
        self.assertTrue(decision.allowed)

    def test_health_is_public_for_host_monitoring(self):
        decision = evaluate_access(
            required=True, configured_token="x" * 32, authorization=None,
            path="/api/health", method="GET",
        )
        self.assertTrue(decision.allowed)

    def test_healthz_is_public_for_render_monitoring(self):
        decision = evaluate_access(
            required=True, configured_token="x" * 32, authorization=None,
            path="/healthz", method="GET",
        )
        self.assertTrue(decision.allowed)

    def test_database_health_is_public_for_host_monitoring(self):
        decision = evaluate_access(
            required=True, configured_token="x" * 32, authorization=None,
            path="/api/health/database", method="GET",
        )
        self.assertTrue(decision.allowed)

    def test_missing_server_secret_fails_closed(self):
        decision = evaluate_access(
            required=True, configured_token="short", authorization="Bearer anything",
            path="/api/markets", method="GET",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.status_code, 503)

    def test_wrong_owner_token_is_rejected(self):
        decision = evaluate_access(
            required=True, configured_token="a" * 32, authorization=f"Bearer {'b' * 32}",
            path="/api/web/access/check", method="GET",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.status_code, 401)

    def test_customer_bearer_reaches_normal_application_api(self):
        decision = evaluate_access(
            required=True, configured_token="a" * 32, authorization="Bearer customer-session",
            path="/api/markets", method="GET",
        )
        self.assertTrue(decision.allowed)

    def test_correct_owner_token_is_accepted(self):
        token = "owner-preview-token-1234567890"
        decision = evaluate_access(
            required=True, configured_token=token, authorization=f"Bearer {token}",
            path="/api/markets", method="GET",
        )
        self.assertTrue(decision.allowed)

    def test_dedicated_owner_header_does_not_conflict_with_customer_session(self):
        token = "owner-preview-token-1234567890"
        decision = evaluate_access(
            required=True, configured_token=token, authorization="Bearer customer-session",
            owner_access=token, path="/api/v22/me", method="GET",
        )
        self.assertTrue(decision.allowed)

    def test_customer_jwt_is_not_treated_as_owner_token_on_user_routes(self):
        token = "owner-preview-token-1234567890"
        decision = evaluate_access(
            required=True, configured_token=token, authorization="Bearer customer-session",
            owner_access=None, path="/api/v22/me", method="GET",
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.status_code, 200)

    def test_owner_gate_is_only_required_on_owner_paths(self):
        token = "owner-preview-token-1234567890"
        decision = evaluate_access(
            required=True, configured_token=token, authorization="Bearer customer-session",
            owner_access=None, path="/api/web/access/check", method="GET",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.status_code, 401)

    def test_first_owner_bootstrap_requires_local_connection(self):
        self.assertTrue(bootstrap_access_allowed("127.0.0.1", web_owner_authenticated=False))
        self.assertFalse(bootstrap_access_allowed("10.0.0.12", web_owner_authenticated=True))
        self.assertFalse(bootstrap_access_allowed("10.0.0.12", web_owner_authenticated=False))

    def test_helpers_normalize_inputs(self):
        self.assertEqual(bearer_token("Bearer abc"), "abc")
        self.assertEqual(cors_origins("https://app.example.com/, https://preview.example.com"), [
            "https://app.example.com", "https://preview.example.com",
        ])
        self.assertEqual(
            cors_origins("https://pro-tre-bot-web.vercel.app"),
            ["https://pro-tre-bot-web.vercel.app"],
        )
        self.assertEqual(
            cors_origins(" https://frontend-nu-two-18.vercel.app/ "),
            ["https://frontend-nu-two-18.vercel.app"],
        )
        self.assertEqual(
            cors_origins("*, https://app.example.com/api, https://app.example.com, https://app.example.com"),
            ["https://app.example.com"],
        )
        self.assertEqual(
            cors_origins('["https://frontend-nu-two-18.vercel.app/"]', fallback=[]),
            ["https://frontend-nu-two-18.vercel.app"],
        )
        self.assertEqual(cors_origins("", fallback=[]), [])
        self.assertTrue(is_allowed_cors_origin("https://frontend-nu-two-18.vercel.app", {"https://frontend-nu-two-18.vercel.app"}))
        self.assertTrue(is_allowed_cors_origin("https://frontend-nu-two-18-git-main-ahmtt9871-dot.vercel.app", {"https://frontend-nu-two-18.vercel.app"}))
        self.assertTrue(is_allowed_cors_origin("https://frontend-gh7asjvqj-ahmet-f11.vercel.app", {"https://frontend-nu-two-18.vercel.app"}))
        self.assertFalse(is_allowed_cors_origin("https://other-project.vercel.app", {"https://frontend-nu-two-18.vercel.app"}))
        self.assertFalse(is_allowed_cors_origin("http://frontend-nu-two-18.vercel.app", {"https://frontend-nu-two-18.vercel.app"}))

    def test_production_auth_preflight_allows_login_and_session_headers(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from fastapi.middleware.cors import CORSMiddleware

        main_source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('PRODUCTION_WEB_ORIGIN = "https://kaistrade.com"', main_source)
        self.assertIn('LEGACY_PRODUCTION_WEB_ORIGIN = "https://frontend-nu-two-18.vercel.app"', main_source)
        self.assertIn('"Authorization"', main_source)
        self.assertIn('"Content-Type"', main_source)
        self.assertIn('"X-ProTreBot-Session"', main_source)

        origin = "https://frontend-nu-two-18.vercel.app"
        requested_headers = "Authorization, Content-Type, X-ProTreBot-Session"
        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[origin],
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Accept", "Authorization", "Content-Type", "Origin", "X-ProTreBot-Session"],
        )
        with TestClient(app) as client:
            for path, method in (("/api/v22/auth/login", "POST"), ("/api/v22/session", "GET")):
                response = client.options(
                    path,
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": method,
                        "Access-Control-Request-Headers": requested_headers,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get("access-control-allow-origin"), origin)
                allowed_headers = response.headers.get("access-control-allow-headers", "").lower()
                self.assertIn("authorization", allowed_headers)
                self.assertIn("content-type", allowed_headers)
                self.assertIn("x-protrebot-session", allowed_headers)
        old = os.environ.get("PROTREBOT_TEST_FLAG")
        try:
            os.environ["PROTREBOT_TEST_FLAG"] = "yes"
            self.assertTrue(env_flag("PROTREBOT_TEST_FLAG"))
        finally:
            if old is None:
                os.environ.pop("PROTREBOT_TEST_FLAG", None)
            else:
                os.environ["PROTREBOT_TEST_FLAG"] = old


if __name__ == "__main__":
    unittest.main()
