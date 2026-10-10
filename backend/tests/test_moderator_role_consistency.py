"""MODERATOR has CUSTOMER entitlements and identical LIVE ownership gates."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app import v22_commercial as auth
from app import v25_execution as live
from test_moderator_role import headers, setup


@pytest.mark.parametrize("role", ["CUSTOMER", "MODERATOR"])
def test_role_alone_never_grants_premium_or_paid_execution(role):
    state = {"subscriptions": [], "licenses": []}
    user = {"id": "member", "role": role, "isPremium": True, "plan": "MASTER_MODE"}
    access = auth.access_snapshot(state, user)
    assert access["isAdmin"] is False
    assert access["isPremium"] is False
    assert access["canExecuteMasterTrade"] is False
    assert access["entitlements"]["unlimitedAnalyst"] is False


def test_snapshot_owner_and_owner_token_cannot_grant_canonical_moderator_premium(setup):
    client, pool = setup
    projection = client.app.state.v22_commercial["state"]
    next(user for user in projection["users"] if user["id"] == "moderator")["role"] = "OWNER"
    assert pool.users["moderator"]["security"]["role"] == "MODERATOR"
    result = client.get("/api/v22/session", headers=headers("moderator", "OWNER"))
    assert result.status_code == 200
    assert result.json()["user"]["role"] == "MODERATOR"
    assert result.json()["access"]["isPremium"] is False
    assert result.json()["access"]["isAdmin"] is False


@pytest.mark.parametrize("method,paid,other_session,status", [
    ("POST", False, False, 403),
    ("POST", True, False, 200),
    ("GET", False, False, 200),
    ("POST", True, True, 403),
    ("GET", True, True, 403),
])
def test_live_guard_outcome_is_identical_for_customer_and_moderator(method, paid, other_session, status):
    outcomes = []
    for role in ("CUSTOMER", "MODERATOR"):
        user = {"id": "member", "role": role, "active": True, "auth_version": 1}
        state = live.initial_state()
        if other_session:
            state["live_session_authorization"].update(user_id="someone-else", session_id="other-session")
        request = SimpleNamespace(
            method=method, headers={"authorization": "Bearer offline-session"},
            state=SimpleNamespace(member=user, web_owner_authenticated=False),
            app=SimpleNamespace(state=SimpleNamespace(
                v25_execution=state, v22_commercial={"state": {"subscriptions": [], "licenses": []}},
            )),
        )
        before = copy.deepcopy(state)
        with patch.object(live, "authenticated_user", return_value=user), \
                patch.object(live, "subscription_for_user", return_value={"master_trade_access": paid}):
            try:
                result = live.execution_owner(request)
                outcome = (200, result["id"])
            except HTTPException as exc:
                outcome = (exc.status_code, exc.detail)
        assert outcome[0] == status
        if status != 200:
            assert state == before
        outcomes.append(outcome)
    assert outcomes[0] == outcomes[1]
