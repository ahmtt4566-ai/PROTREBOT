import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.error_monitoring import build_error_event, error_fingerprint, maybe_alert_critical, redact
from app.v22_commercial import authenticated_user


def test_redact_removes_sensitive_keys_recursively():
    value = redact({"token": "secret", "nested": {"api_key": "key", "safe": "ok"}})
    assert value == {"token": "[REDACTED]", "nested": {"api_key": "[REDACTED]", "safe": "ok"}}


def test_fingerprint_is_stable_for_same_error():
    assert error_fingerprint(source="backend", kind="RuntimeError", message="boom", route="/x") == error_fingerprint(source="backend", kind="RuntimeError", message="boom", route="/x")


def test_event_normalizes_secrets_and_bounds_stack():
    event = build_error_event(source="frontend", kind="TypeError", message="token=abc", context={"password": "pw"}, stack="x" * 10000)
    assert "abc" not in event["message"]
    assert event["context"]["password"] == "[REDACTED]"
    assert len(event["stack"]) == 8000


def test_binance_error_body_url_headers_and_nested_strings_are_masked():
    leaked = ("LEAKED_SIGNATURE", "LEAKED_SECRET", "LEAKED_API_KEY", "LEAKED_KEY", "LEAKED_TOKEN", "LEAKED_PASSWORD", "LEAKED_AUTH", "LEAKED_LISTEN_KEY")
    event = build_error_event(
        source="backend",
        service="live_trading",
        kind="ExchangeError",
        message="https://fapi.binance.com/fapi/v1/order?signature=LEAKED_SIGNATURE&apiKey=LEAKED_API_KEY",
        route="https://fapi.binance.com/fapi/v1/order?listenKey=LEAKED_LISTEN_KEY",
        details={"body": {"msg": "secret=LEAKED_SECRET token=LEAKED_TOKEN", "headers": {"X-MBX-APIKEY": "LEAKED_KEY"}, "password": "LEAKED_PASSWORD"}},
        stack="authorization: Bearer LEAKED_AUTH signature=LEAKED_SIGNATURE",
    )
    serialized = json.dumps(event, default=str)
    for value in leaked:
        assert value not in serialized
    assert event["details"]["body"]["headers"]["X-MBX-APIKEY"] == "[REDACTED]"
    assert "signature=[REDACTED]" in event["message"]
    assert "listenKey=[REDACTED]" in event["route"]


def test_critical_telegram_alert_uses_masked_message():
    event = build_error_event(source="backend", service="live_trading", kind="ExchangeError", code="ORDER_REJECTED", severity="CRITICAL", message="signature=LEAKED_SIGNATURE secret=LEAKED_SECRET")

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            self.payload = kwargs
            return Response()

    client = Client()
    with patch.dict("os.environ", {"ALERT_TELEGRAM_TOKEN": "bot-token", "ALERT_TELEGRAM_CHAT_ID": "chat-id"}), patch("app.error_monitoring.httpx.AsyncClient", return_value=client):
        asyncio.run(maybe_alert_critical(event))
    text = client.payload["json"]["text"]
    assert "LEAKED_SIGNATURE" not in text
    assert "LEAKED_SECRET" not in text


def test_auth_failure_events_do_not_contain_password_token_or_header_values():
    request = SimpleNamespace(
        app=SimpleNamespace(),
        state=SimpleNamespace(request_id="request-1"),
        url=SimpleNamespace(path="/api/v22/private"),
        method="GET",
    )
    cases = (
        ("TOKEN_INVALID", ValueError("password=LEAKED_PASSWORD token=LEAKED_TOKEN Authorization: Bearer LEAKED_TOKEN cookie=LEAKED_TOKEN"), {"sub": "missing"}, []),
        ("USER_INACTIVE", None, {"sub": "inactive", "ver": 1}, [{"id": "inactive", "active": False, "role": "CUSTOMER", "email_verified": True, "auth_version": 1}]),
        ("SESSION_STALE", None, {"sub": "stale", "ver": 1}, [{"id": "stale", "active": True, "role": "CUSTOMER", "email_verified": True, "auth_version": 2}]),
    )
    for code, verify_error, payload, users in cases:
        runtime_state = {"secret": b"secret", "state": {"users": users}}
        with patch("app.v22_commercial.runtime", return_value=runtime_state), patch("app.v22_commercial.bearer", return_value="Bearer LEAKED_TOKEN; cookie=LEAKED_TOKEN"), patch("app.v22_commercial.verify_token", side_effect=verify_error, return_value=payload), patch("app.v22_commercial.schedule_log_event") as log_event:
            try:
                authenticated_user(request)
            except Exception:
                pass
        events = [call.args[1] for call in log_event.call_args_list if call.args[1].get("code") == code]
        assert len(events) == 1
        serialized = json.dumps(events[0], default=str)
        assert "LEAKED_PASSWORD" not in serialized
        assert "LEAKED_TOKEN" not in serialized
        assert "Authorization: Bearer" not in serialized
        assert "cookie=LEAKED_TOKEN" not in serialized
