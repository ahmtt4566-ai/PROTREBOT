import sys
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import error_monitoring, main


@pytest.mark.parametrize("role,status", [("OWNER", 200), ("ADMIN", 403), ("CUSTOMER", 403)])
def test_error_list_requires_owner(role, status):
    application = FastAPI()
    application.state.db_pool = None
    application.add_api_route("/api/v22/admin/errors", main.admin_errors, methods=["GET"])
    with patch.object(main, "authenticated_user", return_value={"id": "owner-test", "role": role}):
        with TestClient(application) as client:
            response = client.get("/api/v22/admin/errors")
    assert response.status_code == status
    if status == 200:
        assert response.json() == {"items": [], "total": 0}


def test_error_update_records_server_actor_and_time_without_replacing_event_data():
    class Pool:
        async def fetchrow(self, query, *args):
            self.query, self.args = query, args
            event_id, status, notes, update_details = args
            return {
                "id": event_id, "status": status, "notes": notes,
                "user_id": "affected-customer", "context": {"original": True},
                "details": {"existing": "preserved", **json.loads(update_details)},
            }

    pool = Pool()
    application = FastAPI()
    application.state.db_pool = pool
    application.add_api_route("/api/v22/admin/errors/{event_id}", main.admin_error_update, methods=["PATCH"])
    before = datetime.now(timezone.utc)
    with patch.object(main, "authenticated_user", return_value={"id": "actual-owner", "role": "OWNER"}):
        with TestClient(application) as client:
            response = client.patch("/api/v22/admin/errors/7", json={
                "status": "ACKNOWLEDGED", "notes": "  Reviewed  ",
                "details": {"admin_update": {"user_id": "spoofed", "updated_at": "spoofed"}},
            })
    after = datetime.now(timezone.utc)
    assert response.status_code == 200
    record = response.json()
    assert record["details"]["existing"] == "preserved"
    assert record["details"]["admin_update"]["user_id"] == "actual-owner"
    assert before <= datetime.fromisoformat(record["details"]["admin_update"]["updated_at"]) <= after
    assert record["user_id"] == "affected-customer"
    assert record["context"] == {"original": True}
    assert record["notes"] == "Reviewed"
    assert "details = COALESCE(details, '{}'::jsonb) || $4::jsonb" in pool.query
    assert "notes = COALESCE($3, notes)" in pool.query
    assert "WHERE id = $1 RETURNING *" in pool.query


@pytest.mark.parametrize("role", ["ADMIN", "CUSTOMER"])
def test_error_update_rejects_non_owner_without_writing(role):
    application = FastAPI()
    application.state.db_pool = SimpleNamespace(fetchrow=lambda *_: pytest.fail("Unauthorized write"))
    application.add_api_route("/api/v22/admin/errors/{event_id}", main.admin_error_update, methods=["PATCH"])
    with patch.object(main, "authenticated_user", return_value={"id": "non-owner", "role": role}):
        with TestClient(application) as client:
            response = client.patch("/api/v22/admin/errors/7", json={"status": "RESOLVED"})
    assert response.status_code == 403


@pytest.mark.parametrize("failed_write", [1, 2])
def test_error_logging_database_failure_warns_without_failing_request(caplog, failed_write):
    class Pool:
        async def execute(self, *_args):
            self.calls += 1
            if self.calls == failed_write:
                raise RuntimeError("PRIVATE_EXCEPTION_EMAIL@example.test token=PRIVATE_EXCEPTION_TOKEN")

        calls = 0

    application = FastAPI()
    application.state.db_pool = Pool()
    application.add_api_route("/api/client-errors", main.client_error, methods=["POST"])

    @application.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = "offline-request"
        return await call_next(request)

    with caplog.at_level("WARNING", logger="app.error_monitoring"):
        with TestClient(application) as client:
            response = client.post("/api/client-errors", json={
                "message": "PRIVATE_EVENT_CONTENT", "context": {"email": "PRIVATE_CONTEXT@example.test"},
            })
    assert response.status_code == 200
    assert response.json() == {"accepted": True, "request_id": "offline-request"}
    warnings = [record.getMessage() for record in caplog.records if record.name == "app.error_monitoring"]
    assert warnings == ["Error event logging failed: error_type=RuntimeError"]
    assert "PRIVATE_" not in caplog.text
    assert application.state.db_pool.calls == failed_write


@pytest.mark.parametrize("failure", ["transport", "http"])
def test_alarm_failure_warns_only_type_and_preserves_non_throwing_logging(caplog, failure):
    event = error_monitoring.build_error_event(
        source="backend", kind="OfflineTest", severity="CRITICAL", message="PRIVATE_EVENT_CONTENT",
        context={"email": "PRIVATE_CONTEXT@example.test"},
    )
    pool = SimpleNamespace(execute=AsyncMock())
    client = AsyncMock()
    client.__aenter__.return_value = client
    if failure == "transport":
        client.post.side_effect = httpx.TimeoutException("PRIVATE_EXCEPTION_TOKEN")
        expected_type = "TimeoutException"
    else:
        client.post.return_value = httpx.Response(
            503, request=httpx.Request("POST", "https://example.test/PRIVATE_EXCEPTION_TOKEN"),
            text="PRIVATE_RESPONSE_CONTENT",
        )
        expected_type = "HTTPStatusError"
    with patch.dict("os.environ", {"ALERT_TELEGRAM_TOKEN": "PRIVATE_ALERT_TOKEN", "ALERT_TELEGRAM_CHAT_ID": "PRIVATE_CHAT"}), \
            patch.object(error_monitoring, "_telegram_alerts", {}), \
            patch.object(error_monitoring.httpx, "AsyncClient", return_value=client), \
            caplog.at_level("WARNING", logger="app.error_monitoring"):
        asyncio.run(error_monitoring.log_event(pool, event))
    warnings = [record.getMessage() for record in caplog.records if record.name == "app.error_monitoring"]
    assert warnings == [f"Critical error alert failed: error_type={expected_type}"]
    assert "PRIVATE_" not in caplog.text
    assert pool.execute.await_count == 2
