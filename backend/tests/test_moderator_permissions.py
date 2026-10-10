"""Permission allowlist and unapplied migration contract checks."""
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.moderator_access import PERMISSIONS, PermissionGrant


@pytest.mark.parametrize("permission", [
    "customers.view", "subscriptions.view", "payments.view", "support.view",
    "support.manage", "events.view", "approvals.create",
])
def test_supported_permission(permission):
    assert PermissionGrant(permission=permission).permission == permission


@pytest.mark.parametrize("permission", ["admin", "OWNER", "customers.manage", "events.view ", ""])
def test_unknown_permission_is_rejected(permission):
    with pytest.raises(ValidationError):
        PermissionGrant(permission=permission)


def test_permission_payload_cannot_supply_actor_or_timestamp():
    with pytest.raises(ValidationError):
        PermissionGrant(permission="events.view", granted_by="spoofed")


def test_numbered_migration_matches_allowlist_and_preserves_existing_stores():
    migration = Path(__file__).parents[1] / "migrations" / "20261010_001_moderator_permissions.sql"
    sql = migration.read_text(encoding="utf-8")
    check = re.search(r"CHECK \(permission IN \((.*?)\)\)", sql, re.S)
    assert check is not None
    assert tuple(re.findall(r"'([^']+)'", check.group(1))) == PERMISSIONS
    assert "PRIMARY KEY (user_id, permission)" in sql
    assert "REFERENCES commercial_auth_users(user_id) ON DELETE CASCADE" in sql
    assert "granted_by TEXT NOT NULL" in sql
    assert "granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in sql
    assert "commercial_erasure_user_guard()" in sql
    assert "application_state_snapshots" not in sql
    assert "INSERT INTO" not in sql
    assert sql.strip().endswith("COMMIT;")
