import sys
from pathlib import Path
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
