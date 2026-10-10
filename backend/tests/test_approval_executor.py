import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Request

from app import approval_executor as executor
from app import v22_commercial as auth
from test_approval_requests import BODY, H, approvals, create
from test_moderator_role import headers, setup

OWNER = headers("owner", "OWNER")


def approve(client, row):
    return client.post(f"/api/v22/admin/approvals/{row['id']}/approve", headers=OWNER)


def test_owner_execution_and_replay_never_reapply(approvals):
    client, pool = approvals
    row = create(client)
    result = approve(client, row)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "executed"
    assert pool.users["customer"]["security"]["active"] is False
    assert pool.users["customer"]["auth_version"] == 2 and pool.canonical_changes == 1
    assert approve(client, row).status_code == 409
    assert pool.canonical_changes == 1
    assert [a["action"] for a in pool.audits] == ["approval.requested", "approval.approved", "approval.executed", "approval.executed"]
    assert not any("güvenliği" in str(a) or "example.test" in str(a) for a in pool.audits)
    # A previous session cannot authenticate after deactivation.
    assert client.get("/api/v22/session", headers=headers("customer", "CUSTOMER")).status_code == 401


def test_two_simultaneous_approvals_apply_exactly_once(approvals):
    client, pool = approvals
    row = create(client)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: approve(client, row).status_code, range(2)))
    assert sorted(results) == [200, 409]
    assert pool.canonical_changes == 1 and pool.users["customer"]["auth_version"] == 2


@pytest.mark.parametrize("change", ["active", "role", "auth_version", "requester_role", "permission"])
def test_target_or_requester_changes_make_request_stale_without_application(approvals, change):
    client, pool = approvals
    row = create(client)
    if change == "active":
        pool.users["customer"]["security"]["active"] = False
    elif change == "role":
        pool.users["customer"]["security"]["role"] = "OWNER"
    elif change == "auth_version":
        pool.users["customer"]["auth_version"] += 1
    elif change == "requester_role":
        pool.users["moderator"]["security"]["role"] = "CUSTOMER"
    else:
        pool.permissions["moderator"] = {}
    before = copy.deepcopy(pool.users)
    assert approve(client, row).status_code == 409
    assert pool.approvals[row["id"]]["status"] == "stale" and pool.users == before and pool.canonical_changes == 0


def test_expired_is_committed_and_never_applied(approvals):
    client, pool = approvals
    row = create(client)
    pool.approvals[row["id"]]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert approve(client, row).status_code == 409
    assert pool.approvals[row["id"]]["status"] == "expired" and pool.canonical_changes == 0


def test_protected_positions_fail_safely_and_no_secret_error(approvals):
    client, pool = approvals
    row = create(client)
    client.app.state._binance_demo_user_state = {"customer": {"snapshot": {"positions": [{"id": "private"}]}}}
    result = approve(client, row)
    assert result.status_code == 200 and result.json()["result_code"] == "protected_positions"
    assert result.json()["status"] == "failed" and pool.canonical_changes == 0
    assert "private" not in result.text


def test_canonical_change_rolls_back_when_execution_audit_fails(approvals):
    client, pool = approvals
    row = create(client)
    before = copy.deepcopy(pool.users["customer"])
    pool.fail_action = "approval.executed"
    result = approve(client, row)
    assert result.status_code == 503
    assert pool.users["customer"] == before and pool.canonical_changes == 0
    assert pool.approvals[row["id"]]["status"] == "executing"
    assert pool.approvals[row["id"]]["result_code"] is None
    assert "private@example" not in result.text
    # Never automatically rerun even if canonical execution did not finish.
    pool.fail_action = None
    assert client.get("/api/v22/admin/approvals", headers=OWNER).json()["items"][0]["needs_review"] is True
    assert approve(client, row).status_code == 409 and pool.canonical_changes == 0


def test_approval_audit_failure_does_not_claim_request(approvals):
    client, pool = approvals
    row = create(client)
    pool.fail_action = "approval.approved"
    assert approve(client, row).status_code == 503
    assert pool.approvals[row["id"]]["status"] == "pending" and pool.canonical_changes == 0


@pytest.mark.parametrize("failure", ["persist", "local_save"])
def test_agent_failure_invalidates_sessions_and_explicit_retry_does_not_touch_canonical(approvals, failure):
    client, pool = approvals
    row = create(client)
    rt = client.app.state.v22_commercial
    rt["state"]["agents"] = [{"id": "agent", "user_id": "customer", "status": "ACTIVE", "token_version": 1}]
    fault = patch.object(auth, "persist_v22_commercial", AsyncMock(return_value=False)) if failure == "persist" else patch.object(auth, "save_state", side_effect=RuntimeError("local save unavailable"))
    with fault:
        result = approve(client, row)
    assert result.status_code == 200 and result.json()["status"] == "failed"
    assert result.json()["result_code"] == "agents_revoke_pending" and result.json()["needs_review"] is True
    assert pool.users["customer"]["auth_version"] == 2 and pool.canonical_changes == 1
    assert rt["state"]["agents"][0]["status"] == "REVOKED" and rt["state"]["agents"][0]["token_version"] == 2
    assert client.get("/api/v22/session", headers=headers("customer", "CUSTOMER")).status_code == 401
    result = client.post(f"/api/v22/admin/approvals/{row['id']}/retry-agents", headers=OWNER)
    assert result.status_code == 200 and result.json()["status"] == "executed"
    assert pool.canonical_changes == 1 and pool.users["customer"]["auth_version"] == 2
    assert rt["state"]["agents"][0]["token_version"] == 2
    assert client.post(f"/api/v22/admin/approvals/{row['id']}/retry-agents", headers=OWNER).status_code == 409


def test_reactivation_never_restores_agents(approvals):
    client, pool = approvals
    pool.users["customer"]["security"]["active"] = False
    client.app.state.v22_commercial["state"]["agents"] = [{"id": "agent", "user_id": "customer", "status": "REVOKED", "token_version": 4}]
    row = create(client, action_type="account.reactivate")
    with patch.object(executor, "revoke_agents", AsyncMock(side_effect=AssertionError("Reactivation must not touch agents"))):
        result = approve(client, row)
    assert result.status_code == 200, result.text
    assert pool.users["customer"]["security"]["active"] is True and pool.users["customer"]["auth_version"] == 2
    assert client.app.state.v22_commercial["state"]["agents"][0]["status"] == "REVOKED"


def test_owner_reject_requires_note_and_plain_text_is_not_executed(approvals):
    client, pool = approvals
    row = create(client, reason="<script>alert(1)</script>")
    url = f"/api/v22/admin/approvals/{row['id']}/reject"
    assert client.post(url, headers=OWNER, json={"decision_note": " "}).status_code == 422
    result = client.post(url, headers=OWNER, json={"decision_note": "<b>Yeniden incele</b>"})
    assert result.status_code == 200 and result.json()["decision_note"] == "<b>Yeniden incele</b>"
    assert pool.canonical_changes == 0
    assert client.get(f"/api/v22/admin/approvals/{row['id']}", headers=OWNER).json()["request"]["reason"] == "<script>alert(1)</script>"


def test_owner_detail_reads_subscription_warning_and_real_summary(approvals):
    client, pool = approvals
    row = create(client)
    pool.subscription_active = True
    result = client.get(f"/api/v22/admin/approvals/{row['id']}", headers=OWNER)
    assert result.status_code == 200, result.text
    assert result.json()["active_subscription"] is True and result.json()["target_changed"] is False
    assert client.get("/api/v22/admin/approvals/summary", headers=OWNER).json() == {"pending_count": 1}


@pytest.mark.parametrize("erased", ["customer", "moderator"])
def test_db_self_decision_check_model_and_erasure_tombstone(approvals, erased):
    client, pool = approvals
    row = create(client)
    with pytest.raises(AssertionError, match="DB CHECK"):
        asyncio.run(pool.fetchrow("UPDATE approval_requests SET status", row["id"], "approved", "moderator", None, None, 1))
    from app import account_erasure
    with patch.object(account_erasure, "table_exists", AsyncMock(return_value=False)):
        asyncio.run(account_erasure.erase_database(client.app, {"id": erased, "email": erased + "@example.test"}))
    assert pool.approvals[row["id"]]["reason"] == "" and pool.approvals[row["id"]]["decision_note"] == ""
    assert erased in pool.erased


def test_internal_already_desired_state_is_idempotent_and_rechecked_target_race_is_stale(approvals):
    client, pool = approvals
    # Internal safety remains idempotent even if a preexisting stored request has the desired state.
    row = create(client)
    stored = pool.approvals[row["id"]]
    stored.update(status="executing", decided_by="owner", decided_at=datetime.now(timezone.utc))
    import json
    stored["target_snapshot"] = json.dumps({"active": False, "role": "CUSTOMER", "auth_version": 1})
    pool.users["customer"]["security"]["active"] = False
    request = Request({"type": "http", "app": client.app, "headers": [(b"authorization", OWNER["Authorization"].encode())]})
    result = asyncio.run(executor.canonical_execution(request, row["id"]))
    assert result.result_code == "canonical_applied" and pool.users["customer"]["auth_version"] == 1
    assert pool.canonical_changes == 0


def test_executor_rechecks_after_claim_and_rejects_owner_or_changed_target(approvals):
    client, pool = approvals
    row = create(client)
    stored = pool.approvals[row["id"]]
    stored.update(status="executing", decided_by="owner", decided_at=datetime.now(timezone.utc))
    pool.users["customer"]["security"]["role"] = "OWNER"
    request = Request({"type": "http", "app": client.app, "headers": [(b"authorization", OWNER["Authorization"].encode())]})
    result = asyncio.run(executor.canonical_execution(request, row["id"]))
    assert result.status == "stale" and result.result_code == "target_changed"
    assert pool.canonical_changes == 0


def test_agent_exception_is_sanitized_and_failed_requests_are_never_automatically_retried(approvals, caplog):
    client, pool = approvals
    row = create(client)
    with patch.object(executor, "revoke_agents", AsyncMock(side_effect=RuntimeError("private@example.test secret-token"))) as cleanup:
        result = approve(client, row)
        assert result.status_code == 200 and result.json()["result_code"] == "agents_revoke_pending"
        assert "private@example.test" not in result.text and "secret-token" not in caplog.text
        assert client.get("/api/v22/admin/approvals", headers=OWNER).status_code == 200
        assert approve(client, row).status_code == 409
        assert cleanup.await_count == 1
    assert pool.canonical_changes == 1


def test_requester_role_is_locked_before_grant_matching_existing_permission_mutations(approvals):
    client, pool = approvals
    row = create(client)
    assert approve(client, row).status_code == 200
    locks = [sql for sql in pool.queries if "FOR SHARE" in sql]
    assert len(locks) == 4
    for role_lock, permission_lock in zip(locks[::2], locks[1::2]):
        assert "FROM commercial_auth_users u" in role_lock
        assert "FROM moderator_permissions" in permission_lock


@pytest.mark.parametrize("operation", ["reject", "cancel", "expire", "stale"])
def test_each_nonexecution_transition_rolls_back_if_audit_cannot_persist(approvals, operation):
    client, pool = approvals
    row = create(client)
    if operation == "expire":
        pool.approvals[row["id"]]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    if operation == "stale":
        pool.users["customer"]["auth_version"] += 1
    previous = copy.deepcopy(pool.approvals[row["id"]])
    pool.audit_fail = True
    if operation == "cancel":
        response = client.post(f"/api/mod/approvals/{row['id']}/cancel", headers=H)
    elif operation == "reject":
        response = client.post(f"/api/v22/admin/approvals/{row['id']}/reject", headers=OWNER, json={"decision_note": "Yeniden inceleme"})
    else:
        response = approve(client, row)
    assert response.status_code == 503
    assert pool.approvals[row["id"]] == previous and pool.canonical_changes == 0


def test_snapshot_auth_persistence_cannot_reactivate_target_or_increment_version_again(approvals):
    client, pool = approvals
    row = create(client)
    assert approve(client, row).status_code == 200
    asyncio.run(auth.persist_auth_security(client.app))
    assert pool.users["customer"]["security"]["active"] is False
    assert pool.users["customer"]["auth_version"] == 2 and pool.canonical_changes == 1
    target = next(u for u in client.app.state.v22_commercial["state"]["users"] if u["id"] == "customer")
    assert target["active"] is False and target["auth_version"] == 2
