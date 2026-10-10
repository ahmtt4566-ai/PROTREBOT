"""Offline append-only audit contract; no PostgreSQL migration is executed."""
import re
import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.audit_log import AUDIT_ACTIONS, AuditActor, write_audit


def test_audit_migration_is_append_only_and_survives_user_erasure():
    from app.account_erasure import PERSONAL_TABLES
    sql = (Path(__file__).parents[1] / "migrations" / "20261010_002_audit_log.sql").read_text(encoding="utf-8")
    statements = re.sub(r"--[^\n]*", "", sql)
    for column in (
        "id", "created_at", "actor_user_id", "actor_role", "action", "target_type", "target_id",
        "request_id", "approval_request_id", "reason", '"before"', '"after"', "ip_masked",
    ):
        assert re.search(rf"^\s*{re.escape(column)}\s+", statements, re.M)
    assert not re.search(r"\b(REFERENCES|FOREIGN\s+KEY|CASCADE)\b", statements, re.I)
    assert "commercial_erasure" not in statements
    assert "audit_log" not in PERSONAL_TABLES
    check = re.search(r"CHECK \(action IN \((.*?)\)\)", statements, re.S)
    assert check is not None
    initial_actions = tuple(re.findall(r"'([^']+)'", check.group(1)))
    extension = (Path(__file__).parents[1] / "migrations" / "20261010_003_moderator_read_audit_actions.sql").read_text(encoding="utf-8")
    extension_check = re.search(r"CHECK \(action IN \((.*?)\)\)", extension, re.S)
    assert extension_check is not None
    previous_actions = tuple(re.findall(r"'([^']+)'", extension_check.group(1)))
    support_extension = (Path(__file__).parents[1] / "migrations" / "20261010_005_support_audit_actions.sql").read_text(encoding="utf-8")
    support_check = re.search(r"CHECK \(action IN \((.*?)\)\)", support_extension, re.S)
    approval_extension = (Path(__file__).parents[1] / "migrations" / "20261010_007_approval_audit_actions.sql").read_text(encoding="utf-8")
    approval_check = re.search(r"CHECK \(action IN \((.*?)\)\)", approval_extension, re.S)
    assert tuple(re.findall(r"'([^']+)'", approval_check.group(1))) == AUDIT_ACTIONS
    assert set(re.findall(r"'([^']+)'", support_check.group(1))) < set(AUDIT_ACTIONS)
    assert set(previous_actions) < set(AUDIT_ACTIONS)
    assert set(initial_actions) < set(AUDIT_ACTIONS)
    assert re.search(
        r"CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log"
        r"\s+FOR EACH STATEMENT EXECUTE FUNCTION audit_log_reject_mutation\(\)",
        statements,
    )
    assert "RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = '55000'" in statements
    assert "NEW.created_at := clock_timestamp()" in statements
    assert "DEFAULT clock_timestamp()" in statements
    assert "BEFORE INSERT ON audit_log" in statements
    for trigger in ("audit_log_insert_time", "audit_log_append_only"):
        assert f"ALTER TABLE audit_log ENABLE ALWAYS TRIGGER {trigger}" in statements
    for column in ("created_at", "actor_user_id", "target_id", "action"):
        assert re.search(rf"CREATE INDEX IF NOT EXISTS audit_log_{column} ON audit_log \({column}", statements)
    assert "application_state_snapshots" not in statements
    assert statements.strip().endswith("COMMIT;")


@pytest.mark.parametrize("action,before,after", [
    ("ROLE_CHANGED", {"role": "CUSTOMER"}, {"role": "MODERATOR"}),
    ("PERMISSION_GRANTED", {"permission": "events.view", "granted": False},
     {"permission": "events.view", "granted": True}),
    ("PERMISSION_REVOKED", {"permission": "events.view", "granted": True},
     {"permission": "events.view", "granted": False}),
])
def test_write_audit_drops_all_non_allowlisted_fields_and_masks_reason(action, before, after):
    conn = SimpleNamespace(is_in_transaction=lambda: True, execute=AsyncMock(return_value="INSERT 0 1"))
    private = {
        "email": "private@example.test", "name": "Private Name", "token": "private-token",
        "nested": {"api_key": "private-key"}, "password": "private-password",
    }
    actor = AuditActor("owner-id", "OWNER", str(uuid.uuid4()), "192.0.2.0/24")
    asyncio.run(write_audit(
        conn, actor, action, "USER" if action == "ROLE_CHANGED" else "MODERATOR_PERMISSION",
        "target-id", {**before, **private}, {**after, **private},
        reason="private@example.test private-token", approval_request_id="approval-id",
    ))
    sql, *values = conn.execute.call_args.args
    assert values[:5] == ["owner-id", "OWNER", action,
                          "USER" if action == "ROLE_CHANGED" else "MODERATOR_PERMISSION", "target-id"]
    assert values[6] == "approval-id"
    assert values[7] == "[REDACTED]"
    assert json.loads(values[8]) == before
    assert json.loads(values[9]) == after
    assert values[10] == "192.0.2.0/24"
    assert "created_at" not in sql
    assert not any(value in str(values) for value in (
        "private@example.test", "Private Name", "private-token", "private-key", "private-password",
    ))


@pytest.mark.parametrize("host,masked", [
    ("192.0.2.123", "192.0.2.0/24"),
    ("2001:db8:abcd:1234::99", "2001:db8:abcd::/48"),
    ("testclient", None),
])
def test_audit_actor_normalizes_request_id_and_masks_only_peer_address(host, masked):
    request = Request({
        "type": "http", "client": (host, 443),
        "headers": [(b"x-forwarded-for", b"203.0.113.123")],
        "state": {"request_id": "private@example.test private-token"},
    })
    actor = AuditActor.from_request(request, {
        "id": "owner-id", "role": "OWNER", "email": "private@example.test", "token": "private-token",
    })
    assert actor.ip_masked == masked
    uuid.UUID(actor.request_id)
    assert "private" not in str(actor)
    valid = str(uuid.uuid4())
    request.state.request_id = valid
    assert AuditActor.from_request(request, {"id": "owner-id", "role": "OWNER"}).request_id == valid


def test_audit_refuses_autocommit_connection():
    conn = SimpleNamespace(is_in_transaction=lambda: False, execute=AsyncMock())
    with pytest.raises(RuntimeError):
        asyncio.run(write_audit(conn, AuditActor("owner", "OWNER", str(uuid.uuid4())),
                               "ROLE_CHANGED", "USER", "target",
                               {"role": "CUSTOMER"}, {"role": "MODERATOR"}))
    conn.execute.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("action", "LOGIN"), ("actor_id", "private@example.test"), ("target_id", "private@example.test"),
    ("actor_role", "ADMIN"), ("request_id", "private-token"), ("ip", "192.0.2.123"),
    ("before", {"role": "private-token"}), ("before", {}),
    ("target_type", "EMAIL"), ("approval_request_id", "private@example.test"),
])
def test_audit_rejects_invalid_allowed_values_without_writing(field, value):
    conn = SimpleNamespace(is_in_transaction=lambda: True, execute=AsyncMock())
    args = {
        "actor": AuditActor(
            value if field == "actor_id" else "owner",
            value if field == "actor_role" else "OWNER",
            value if field == "request_id" else str(uuid.uuid4()),
            value if field == "ip" else None,
        ),
        "action": "ROLE_CHANGED", "target_type": "USER", "target_id": "target",
        "before": {"role": "CUSTOMER"}, "after": {"role": "MODERATOR"},
    }
    if field in args and field != "actor":
        args[field] = value
    if field == "approval_request_id":
        args[field] = value
    with pytest.raises(ValueError):
        asyncio.run(write_audit(conn, **args))
    conn.execute.assert_not_called()


@pytest.mark.parametrize("result", ["INSERT 0 0", "failure"])
def test_audit_persistence_failure_is_explicit_and_logs_only_type(result, caplog):
    execute = AsyncMock(return_value=result)
    if result == "failure":
        execute.side_effect = RuntimeError("private@example.test private-token")
    conn = SimpleNamespace(is_in_transaction=lambda: True, execute=execute)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(write_audit(conn, AuditActor("owner", "OWNER", str(uuid.uuid4())),
                               "ROLE_CHANGED", "USER", "target",
                               {"role": "CUSTOMER"}, {"role": "MODERATOR"}))
    assert caught.value.status_code == 503
    assert "Audit write failed (RuntimeError)" in caplog.text
    assert "private@example.test" not in caplog.text
    assert "private-token" not in caplog.text
