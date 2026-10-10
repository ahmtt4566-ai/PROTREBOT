"""Mandatory data-free read evidence and a durable, per-actor shared rate cap."""
import copy
import hashlib
import json
import uuid
from pathlib import Path

import pytest

from app.audit_log import AUDIT_ACTIONS
from test_moderator_customers import reads
from test_moderator_role import headers, setup

DETAILS = [
    ("/api/mod/customers/customer", "customer.viewed"),
    ("/api/mod/customers/customer/subscription", "customer.subscription.viewed"),
    ("/api/mod/customers/customer/payments", "customer.payments.viewed"),
]


@pytest.mark.parametrize("path,action", DETAILS)
@pytest.mark.parametrize("actor,role", [("moderator", "MODERATOR"), ("owner", "OWNER")])
def test_each_detail_read_records_only_target_identity_without_customer_data(reads, path, action, actor, role):
    client, pool = reads
    rid = str(uuid.uuid4())
    before = copy.deepcopy((pool.users, pool.settings, pool.subscriptions, pool.persisted_users))
    response = client.get(path, headers={**headers(actor, role), "x-request-id": rid})
    assert response.status_code == 200
    assert len(pool.audits) == 1
    audit = pool.audits[0]
    assert audit["action"] == action
    assert audit["target_type"] == "USER"
    assert audit["target_id"] == "customer"
    assert audit["actor_user_id"] == actor and audit["actor_role"] == role
    assert audit["request_id"] == rid
    assert json.loads(audit["before"]) == json.loads(audit["after"]) == {}
    assert audit["reason"] is None
    assert audit["approval_request_id"] is None
    assert "customer@example.test" not in str(audit)
    assert "MASTER_MODE" not in str(audit)
    assert "FAILED" not in str(audit)
    assert (pool.users, pool.settings, pool.subscriptions, pool.persisted_users) == before


@pytest.mark.parametrize("path,action", DETAILS)
@pytest.mark.parametrize("failure", ["audit", "commit"])
def test_audit_or_commit_failure_returns_503_and_no_data(reads, path, action, failure):
    client, pool = reads
    pool.audit_fail = failure == "audit"
    pool.commit_fail = failure == "commit"
    response = client.get(path, headers=headers("moderator", "MODERATOR"))
    assert response.status_code == 503
    assert set(response.json()) == {"detail"}
    assert not any(key in response.text for key in ("email_masked", "plan", "payment_status", "created_at"))
    assert pool.audits == []
    assert pool.transaction_depth == 0
    assert next(iter(pool.counters.values()))[1] == 1


def test_limit_is_exactly_30_combined_requests_per_actor_per_60_seconds(reads):
    client, pool = reads
    h = headers("moderator", "MODERATOR")
    for index in range(30):
        path = ["/api/mod/customers"] + [entry[0] for entry in DETAILS]
        assert client.get(path[index % 4], headers=h).status_code == 200
    before = len(pool.audits)
    blocked = client.get(DETAILS[0][0], headers=h)
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "60"
    assert set(blocked.json()) == {"detail"}
    assert len(pool.audits) == before
    assert client.get(DETAILS[0][0], headers=headers("owner", "OWNER")).status_code == 200
    for bucket, (start, count) in list(pool.counters.items()):
        if bucket.endswith(hashlib.sha256(b"moderator").hexdigest()):
            pool.counters[bucket] = (start - 60, count)
    assert client.get(DETAILS[0][0], headers=h).status_code == 200


def test_new_token_and_ip_do_not_reset_actor_counter(reads):
    client, pool = reads
    for _ in range(30):
        assert client.get("/api/mod/customers", headers=headers("moderator", "MODERATOR")).status_code == 200
    blocked = client.get("/api/mod/customers", headers={
        **headers("moderator", "MODERATOR"), "x-forwarded-for": "203.0.113.123",
    })
    assert blocked.status_code == 429
    assert len(pool.counters) == 1
    bucket = next(iter(pool.counters))
    assert bucket == "moderator-customer-read:" + hashlib.sha256(b"moderator").hexdigest()
    assert "@" not in bucket


def test_rate_store_failure_cannot_return_customer_data(reads, caplog):
    client, pool = reads
    pool.rate_fail = True
    response = client.get("/api/mod/customers/customer", headers=headers("moderator", "MODERATOR"))
    assert response.status_code == 503
    assert set(response.json()) == {"detail"}
    assert pool.audits == []
    assert "Moderator customer read failed (RuntimeError)" in caplog.text
    assert "private rate storage failure" not in caplog.text


@pytest.mark.parametrize("path,action", DETAILS)
def test_ineligible_target_never_generates_view_evidence(reads, path, action):
    client, pool = reads
    path = path.replace("/customers/customer", "/customers/owner")
    assert client.get(path, headers=headers("moderator", "MODERATOR")).status_code == 404
    assert pool.audits == []


@pytest.mark.parametrize("change,status", [
    ("role", 403), ("mfa", 403), ("permission", 403), ("version", 401), ("active", 401),
])
def test_actor_is_canonically_rechecked_before_customer_disclosure(reads, change, status):
    client, pool = reads
    fetchrow = pool.fetchrow

    async def changed(sql, *args):
        if "AS allowed" in sql:
            if change == "role":
                pool.users["moderator"]["security"]["role"] = "CUSTOMER"
            elif change == "mfa":
                pool.settings["moderator"]["two_factor_enabled"] = False
            elif change == "permission":
                pool.permissions["moderator"].clear()
            elif change == "version":
                pool.users["moderator"]["auth_version"] += 1
            else:
                pool.users["moderator"]["security"]["active"] = False
        return await fetchrow(sql, *args)

    pool.fetchrow = changed
    response = client.get("/api/mod/customers/customer", headers=headers("moderator", "MODERATOR"))
    assert response.status_code == status
    assert pool.audits == []
    assert set(response.json()) == {"detail"}


def test_view_actions_have_empty_snapshot_allowlists_even_with_private_input():
    from app.audit_log import allowed_snapshot
    for action in ("customer.viewed", "customer.subscription.viewed", "customer.payments.viewed"):
        assert action in AUDIT_ACTIONS
        assert allowed_snapshot(action, {"email": "private@example.test", "token": "private-token"}) == {}


def test_migration_only_extends_action_check_without_modifying_audit_rows_or_triggers():
    sql = (Path(__file__).parents[1] / "migrations" / "20261010_003_moderator_read_audit_actions.sql").read_text()
    assert "ALTER TABLE audit_log DROP CONSTRAINT audit_log_action_check" in sql
    assert "ALTER TABLE audit_log ADD CONSTRAINT audit_log_action_check" in sql
    for action in AUDIT_ACTIONS:
        assert f"'{action}'" in sql
    assert not any(command in sql.upper() for command in (
        "UPDATE AUDIT_LOG", "DELETE FROM", "TRUNCATE", "DISABLE TRIGGER", "DROP TRIGGER",
    ))


def test_no_provider_or_customer_mutation_code_is_imported_by_customer_router():
    import ast
    from app import moderator_customers
    tree = ast.parse(Path(moderator_customers.__file__).read_text())
    imports = [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ] + [
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    ]
    assert not any(name and any(forbidden in name for forbidden in (
        "stripe", "subscription_core", "v25", "binance", "exchange", "requests", "httpx",
    )) for name in imports)
