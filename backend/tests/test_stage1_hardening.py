import sys
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main


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
