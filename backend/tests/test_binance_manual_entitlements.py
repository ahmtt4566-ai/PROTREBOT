import asyncio
import copy
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from app import binance_manual_admin as admin
from app import binance_manual_payments as payments
from app import member_entitlements as entitlements
from app.audit_log import AuditActor
from app.subscription_core import entitlement_snapshot
from app.v22_commercial import access_snapshot
from fastapi import HTTPException
from starlette.requests import Request
from test_binance_manual_payments import Connection
from test_moderator_role import headers
from test_moderator_role import setup as canonical_setup

setup = canonical_setup
ROOT = Path(__file__).parents[1]


class EntitlementConnection(Connection):
    def __init__(self):
        super().__init__()
        self.now = datetime.now(timezone.utc)
        self.periods = {}
        self.audits = []
        self.audit_fail = False
        self.period_fail = False
        self.commit_fail = False
        self.canonical_users = {}

    @asynccontextmanager
    async def transaction(self):
        previous = copy.deepcopy((self.periods, self.audits))
        outer = self._owner is not asyncio.current_task()
        try:
            async with super().transaction():
                yield
                if outer and self.commit_fail:
                    raise RuntimeError("commit failed")
        except BaseException:
            self.periods, self.audits = previous
            raise

    def is_in_transaction(self):
        return self._owner is asyncio.current_task()

    async def execute(self, sql, *args):
        if "INSERT INTO binance_manual_periods" in sql:
            self.check()
            if self.period_fail:
                raise OSError("period failed")
            rid, uid, start, end = args
            assert rid not in self.periods and self.rows[rid]["status"] == "approved"
            self.periods[rid] = {"payment_request_id": rid, "user_id": uid, "period_start": start,
                                 "period_end": end, "revoked_at": None}
            return "INSERT 0 1"
        if "INSERT INTO audit_log" in sql:
            self.check()
            if self.audit_fail:
                raise RuntimeError("audit failed")
            assert args[1] == "OWNER"
            self.audits.append(dict(zip(("actor_user_id", "actor_role", "action", "target_type", "target_id",
                                        "request_id", "approval_request_id", "reason", "before", "after", "ip_masked"), args)))
            return "INSERT 0 1"
        if "pg_advisory_xact_lock(71011002)" in sql:
            self.check()
            return "SELECT 1"
        return await super().execute(sql, *args)

    async def fetchrow(self, sql, *args):
        if "SELECT p.period_start,p.period_end FROM binance_manual_periods" in sql:
            period = self.periods.get(args[0])
            payment = self.rows.get(args[0])
            if period and payment and period["user_id"] == args[1] and payment["user_id"] == args[1] \
                    and period["revoked_at"] is None and payment["status"] == "approved" and payment["erased_at"] is None:
                return {key: period[key] for key in ("period_start", "period_end")}
            return None
        if "FROM binance_manual_periods p" in sql:
            if self.fail:
                raise OSError("manual database unavailable")
            candidates = []
            for period in self.periods.values():
                row = self.rows[period["payment_request_id"]]
                if period["user_id"] == args[0] and row["user_id"] == args[0] and row["status"] == "approved" \
                        and row["erased_at"] is None and period["revoked_at"] is None \
                        and period["period_start"] <= datetime.now(timezone.utc) < period["period_end"]:
                    candidates.append({**period, **{key: row[key] for key in
                                       ("status", "amount", "currency", "method", "decided_by", "decided_at")},
                                       "payment_user_id": row["user_id"]})
            return copy.deepcopy(max(candidates, key=lambda row: row["period_end"])) if candidates else None
        if "SELECT auth_version,security FROM commercial_auth_users" in sql:
            self.check()
            return copy.deepcopy(self.canonical_users.get(args[0]))
        if "WHERE id=$1 AND erased_at IS NULL FOR UPDATE" in sql:
            self.check()
            row = self.rows.get(args[0])
            return copy.deepcopy(row) if row and row["erased_at"] is None else None
        if "SET status=$3,decided_by=$4" in sql:
            self.check()
            rid, version, status, owner, reason = args
            row = self.rows[rid]
            if row["version"] != version or row["status"] != "pending":
                return None
            row.update(status=status, decided_by=owner, decided_at=self.now, decision_note=reason, version=version + 1)
            return copy.deepcopy(row)
        if "COUNT(*) FILTER (WHERE status='pending')" in sql:
            self.check()
            return {"pending": sum(r["status"] == "pending" for r in self.rows.values()),
                    "approved_today": sum(r["status"] == "approved" for r in self.rows.values()),
                    "rejected": sum(r["status"] == "rejected" for r in self.rows.values())}
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        if "MAX(COALESCE(trial_end,current_period_end))" in sql:
            self.check()
            return max((row.get("trial_end") or row["current_period_end"] for row in self.subscriptions
                        if row["user_id"] == args[0] and row["plan"] == "TRIAL" and row["status"] == "TRIALING"), default=None)
        if "MAX(p.period_end)" in sql:
            self.check()
            return max((period["period_end"] for period in self.periods.values()
                        if period["user_id"] == args[0] and period["revoked_at"] is None), default=None)
        if "SELECT clock_timestamp()" in sql:
            self.check()
            return self.now
        if "SELECT EXISTS (SELECT 1 FROM binance_manual_periods" in sql:
            self.check()
            return args[0] in self.periods
        if "COUNT(*) FROM binance_manual_payment_requests" in sql:
            self.check()
            return sum((args[0] is None or row["status"] == args[0]) and (len(args) < 2 or args[1] is None or row["id"] == args[1])
                       for row in self.rows.values())
        return await super().fetchval(sql, *args)

    async def fetch(self, sql, *args):
        if "FROM subscriptions WHERE user_id=$1" in sql:
            return copy.deepcopy([row for row in self.subscriptions if row["user_id"] == args[0]])
        if "LEFT JOIN commercial_auth_users" in sql:
            self.check()
            return [{**copy.deepcopy(row), "email": "customer@example.test", "display_name": "Customer"}
                    for row in self.rows.values() if row["erased_at"] is None
                    and (args[0] is None or row["status"] == args[0])
                    and (len(args) < 4 or args[3] is None or row["id"] == args[3])][args[2]:args[2]+args[1]]
        return await super().fetch(sql, *args)


def actor(role="OWNER"):
    return AuditActor("owner", role, str(uuid.uuid4()))


async def pending(conn, uid="user-a", number="binance-number"):
    row = await payments.create_request(conn, uid)
    return await payments.submit_transaction(conn, uid, row.id, number)


async def decide(conn, row, status="approved", **kwargs):
    async with conn.transaction():
        return await admin.decision(conn, actor(), row.id, status, **kwargs)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("BINANCE_MANUAL_ACCESS_ENABLED", "true")


@pytest.mark.parametrize("status,plan,end_days,grace_days", [
    ("ACTIVE", "MASTER_MODE", 30, None), ("TRIALING", "TRIAL", 7, None),
    ("PAST_DUE", "MASTER_MODE", -1, 2), ("PAST_DUE", "MASTER_MODE", -1, -1),
    ("UNPAID", "MASTER_MODE", 30, None), ("CANCELLED", "MASTER_MODE", 30, None),
    ("EXPIRED", "MASTER_MODE", -1, None), ("ACTIVE", "MASTER_MODE", -1, None),
])
def test_stripe_snapshot_and_access_are_exactly_unchanged(status, plan, end_days, grace_days):
    now = datetime.now(timezone.utc)
    row = {"user_id": "user-a", "plan": plan, "status": status,
           "current_period_end": (now + timedelta(days=end_days)).isoformat(),
           "trial_end": (now + timedelta(days=end_days)).isoformat() if plan == "TRIAL" else None,
           "grace_until": (now + timedelta(days=grace_days)).isoformat() if grace_days is not None else None}
    state = {"subscriptions": [row]}
    before = copy.deepcopy(state)
    result = asyncio.run(entitlements.resolve(EntitlementConnection(), state, "user-a"))
    assert result == entitlement_snapshot(state, "user-a")
    conn = EntitlementConnection()
    conn.subscriptions = [copy.deepcopy(row)]
    assert asyncio.run(entitlements.target_subscription(conn, {}, "user-a")) == result
    user = {"id": "user-a", "role": "CUSTOMER"}
    assert entitlements.access(result, user) == access_snapshot(state, user)
    assert state == before


def test_active_stripe_and_owner_do_not_depend_on_manual_database():
    now = datetime.now(timezone.utc)
    state = {"subscriptions": [{"user_id": "user-a", "plan": "MASTER_MODE", "status": "ACTIVE",
                                "current_period_end": (now + timedelta(days=1)).isoformat()}]}
    assert asyncio.run(entitlements.resolve(None, state, "user-a")) == entitlement_snapshot(state, "user-a")
    assert entitlements.access(entitlement_snapshot({"subscriptions": []}, "owner"),
                               {"id": "owner", "role": "OWNER"}) == access_snapshot({"subscriptions": []}, {"id": "owner", "role": "OWNER"})


def test_approval_grants_exactly_30_days_once_and_writes_atomic_audit():
    conn = EntitlementConnection()

    async def run():
        row = await pending(conn)
        approved = await decide(conn, row)
        repeated = await decide(conn, row)
        assert approved == repeated
        assert len(conn.periods) == len(conn.audits) == 1
        period = conn.periods[row.id]
        assert period["period_end"] - period["period_start"] == timedelta(days=30)
        result = await entitlements.resolve(conn, {"subscriptions": []}, "user-a")
        assert result["master_trade_access"] and result["mode"] == "BINANCE_MANUAL"
        assert result["next_payment_amount"] is None and result["currency"] == "USDT"
        assert not (await entitlements.resolve(conn, {"subscriptions": []}, "user-b"))["master_trade_access"]
        assert conn.audits[0]["action"] == "payment.binance.approved"
        assert json.loads(conn.audits[0]["after"]) == {"status": "approved"}
    asyncio.run(run())


def test_trial_and_early_renewal_preserve_remaining_days_without_early_access():
    conn = EntitlementConnection()
    trial_end = conn.now + timedelta(days=7)
    conn.subscriptions = [{"user_id": "user-a", "plan": "TRIAL", "status": "TRIALING",
                           "trial_end": trial_end, "current_period_end": trial_end, "stripe_subscription_id": "trial"}]

    async def run():
        row = await pending(conn)
        await decide(conn, row)
        assert conn.periods[row.id]["period_start"] == trial_end
        assert not (await entitlements.resolve(conn, {"subscriptions": []}, "user-a"))["master_trade_access"]
        other = await pending(conn, number="renewal")
        await decide(conn, other)
        assert conn.periods[other.id]["period_start"] == trial_end + timedelta(days=30)
        assert conn.periods[other.id]["period_end"] == trial_end + timedelta(days=60)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["audit_fail", "period_fail", "commit_fail"])
def test_any_atomic_failure_rolls_back_payment_period_and_audit(failure):
    conn = EntitlementConnection()

    async def run():
        row = await pending(conn)
        setattr(conn, failure, True)
        with pytest.raises((HTTPException, OSError, RuntimeError)):
            await decide(conn, row)
        assert conn.rows[row.id]["status"] == "pending"
        assert conn.periods == {} and conn.audits == []
        setattr(conn, failure, False)
        assert not (await entitlements.resolve(conn, {"subscriptions": []}, "user-a"))["master_trade_access"]
    asyncio.run(run())


def test_rejection_requires_reason_is_audited_and_cannot_be_approved_later():
    conn = EntitlementConnection()

    async def run():
        row = await pending(conn)
        with pytest.raises(HTTPException) as failure:
            await decide(conn, row, "rejected", reason=" ")
        assert failure.value.status_code == 422
        await decide(conn, row, "rejected", reason="Binance hesabında ödeme bulunamadı.")
        await decide(conn, row, "rejected", reason="Aynı karar")
        assert len(conn.audits) == 1 and conn.audits[0]["reason"] == "[REDACTED]"
        assert conn.periods == {}
        with pytest.raises(HTTPException) as failure:
            await decide(conn, row)
        assert failure.value.status_code == 409
    asyncio.run(run())


@pytest.mark.parametrize("change", ["wrong_user", "rejected", "expired", "revoked", "erased"])
def test_noneligible_payment_never_grants_access(change):
    conn = EntitlementConnection()

    async def run():
        row = await pending(conn)
        await decide(conn, row)
        if change == "wrong_user":
            conn.periods[row.id]["user_id"] = "user-b"
        elif change == "rejected":
            conn.rows[row.id]["status"] = "rejected"
        elif change == "expired":
            conn.periods[row.id].update(period_start=conn.now-timedelta(days=31), period_end=conn.now-timedelta(days=1))
        elif change == "revoked":
            conn.periods[row.id]["revoked_at"] = conn.now
        else:
            conn.rows[row.id]["erased_at"] = conn.now
        assert not (await entitlements.resolve(conn, {"subscriptions": []}, "user-a"))["master_trade_access"]
    asyncio.run(run())


def test_corrupt_record_is_error_not_a_success_shaped_denial():
    conn = EntitlementConnection()

    async def run():
        row = await pending(conn)
        await decide(conn, row)
        conn.rows[row.id]["amount"] = Decimal("1.00")
        with pytest.raises(HTTPException) as failure:
            await entitlements.resolve(conn, {"subscriptions": []}, "user-a")
        assert failure.value.status_code == 503
    asyncio.run(run())


@pytest.fixture
def api(setup):
    client, pool = setup
    conn = EntitlementConnection()
    conn.users = {"customer", "moderator", "owner"}
    conn.canonical_users = pool.users

    @asynccontextmanager
    async def acquire():
        yield conn
    pool.acquire = acquire
    client.app.include_router(admin.router)
    row = asyncio.run(pending(conn, "customer"))
    return client, conn, row


@pytest.mark.parametrize("method,suffix,body", [
    ("GET", "", None),
    ("POST", "/" + "a" * 32 + "/approve", {"checked_in_binance": True}),
    ("POST", "/" + "a" * 32 + "/reject", {"reason": "Bulunamadı"}),
])
@pytest.mark.parametrize("flag", [None, "false"])
def test_disabled_owner_payment_endpoints_never_acquire_database(setup, monkeypatch, method, suffix, body, flag):
    client, pool = setup
    client.app.include_router(admin.router)
    if flag is None:
        monkeypatch.delenv("BINANCE_MANUAL_ACCESS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("BINANCE_MANUAL_ACCESS_ENABLED", flag)
    monkeypatch.setenv("FREE_TRIAL_ENABLED", "false")

    @asynccontextmanager
    async def forbidden_acquire():
        raise AssertionError("Disabled payment handler must never acquire the database")
        yield

    pool.acquire = forbidden_acquire
    response = client.request(method, "/api/v22/admin/binance-payments" + suffix,
                              headers=headers("owner", "OWNER"), json=body)
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "message": "Binance ödemeleri şu an kapalı"}
    assert not any("binance_" in sql or "member_free_trials" in sql for sql in pool.queries)
    assert pool.audits == []


@pytest.mark.parametrize("uid,role,status", [
    ("customer", "CUSTOMER", 403), ("moderator", "MODERATOR", 403), (None, None, 401),
])
@pytest.mark.parametrize("method,suffix,body", [
    ("GET", "", None),
    ("POST", "/" + "a" * 32 + "/approve", {"checked_in_binance": True}),
    ("POST", "/" + "a" * 32 + "/reject", {"reason": "Bulunamadı"}),
])
def test_disabled_payment_endpoints_keep_owner_guard(setup, monkeypatch, uid, role, status, method, suffix, body):
    client, pool = setup
    client.app.include_router(admin.router)
    monkeypatch.setenv("BINANCE_MANUAL_ACCESS_ENABLED", "false")
    response = client.request(method, "/api/v22/admin/binance-payments" + suffix,
                              headers=headers(uid, role) if uid else {}, json=body)
    assert response.status_code == status
    assert not any("binance_" in sql for sql in pool.queries)


@pytest.mark.parametrize("method,path,body", [
    ("GET", "", None), ("POST", "/approve", {"checked_in_binance": True}), ("POST", "/reject", {"reason": "Bulunamadı"}),
])
def test_moderator_is_forbidden_from_all_owner_payment_endpoints(api, method, path, body):
    client, conn, row = api
    suffix = "/" + row.id + path if path else ""
    response = client.request(method, "/api/v22/admin/binance-payments" + suffix,
                              headers=headers("moderator", "MODERATOR"), json=body)
    assert response.status_code == 403
    assert conn.rows[row.id]["status"] == "pending" and conn.periods == {}


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_owner_approval_requires_strict_explicit_binance_confirmation(api, value):
    client, conn, row = api
    response = client.post("/api/v22/admin/binance-payments/" + row.id + "/approve",
                           headers=headers("owner", "OWNER"), json={"checked_in_binance": value})
    assert response.status_code == 422 and conn.periods == {}


def test_owner_api_idempotent_approval_and_member_profile_use_same_durable_right(api):
    client, conn, row = api
    path = "/api/v22/admin/binance-payments/" + row.id + "/approve"
    for _ in range(2):
        response = client.post(path, headers=headers("owner", "OWNER"), json={"checked_in_binance": True})
        assert response.status_code == 200, response.text
    assert len(conn.periods) == len(conn.audits) == 1
    profile = client.get("/api/v22/profile", headers=headers("customer", "CUSTOMER"))
    assert profile.status_code == 200, profile.text
    assert profile.json()["access"]["isPremium"]
    assert profile.json()["subscription"]["mode"] == "BINANCE_MANUAL"
    listing = client.get("/api/v22/admin/binance-payments", headers=headers("owner", "OWNER"))
    assert listing.status_code == 200, listing.text
    assert listing.json()["summary"]["approved_today"] == 1
    assert listing.json()["items"][0]["amount"] == "119.90"


def test_owner_deep_link_filter_returns_exactly_requested_card(api):
    client, conn, row = api
    asyncio.run(pending(conn, "moderator", "another-payment"))
    response = client.get("/api/v22/admin/binance-payments?request_id="+row.id, headers=headers("owner", "OWNER"))
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [item["id"] for item in response.json()["items"]] == [row.id]


def test_storage_error_is_closed_but_known_stripe_right_is_preserved(api):
    client, conn, _ = api
    conn.fail = True
    response = client.get("/api/v22/profile", headers=headers("customer", "CUSTOMER"))
    assert response.status_code == 503
    state = client.app.state.v22_commercial["state"]
    state["subscriptions"] = [{"user_id": "customer", "plan": "MASTER_MODE", "status": "ACTIVE",
                              "current_period_end": (conn.now + timedelta(days=5)).isoformat()}]
    response = client.get("/api/v22/profile", headers=headers("customer", "CUSTOMER"))
    assert response.status_code == 200 and response.json()["access"]["isPremium"]


def test_database_failure_blocks_entry_without_interrupting_safe_stop(api):
    client, conn, _ = api
    calls = {"entry": 0, "stop": 0}

    @client.app.post("/api/v25/order")
    async def entry():
        calls["entry"] += 1
        return {"ok": True}

    @client.app.post("/api/v21/auto/stop")
    async def safe_stop():
        calls["stop"] += 1
        return {"ok": True}

    conn.fail = True
    response = client.post("/api/v25/order", headers=headers("customer", "CUSTOMER"), json={})
    assert response.status_code == 503 and calls["entry"] == 0
    response = client.post("/api/v21/auto/stop", headers=headers("customer", "CUSTOMER"), json={})
    assert response.status_code == 200 and calls["stop"] == 1


def test_approved_manual_right_is_shared_by_entry_analyst_and_assistant(api):
    from app.analyst_credits import member_access
    from app.assistant_tools import AssistantTools

    client, conn, row = api
    calls = []

    @client.app.post("/api/v25/order")
    async def entry(request: Request):
        user, premium = member_access(request)
        tools = AssistantTools(request)
        assert premium and tools.premium(user["id"])
        assert (await tools.get_my_access(user["id"]))["data"]["isPremium"]
        calls.append(user["id"])
        return {"ok": True}

    path = "/api/v25/order"
    assert client.post(path, headers=headers("customer", "CUSTOMER"), json={}).status_code == 403
    asyncio.run(decide(conn, row))
    response = client.post(path, headers=headers("customer", "CUSTOMER"), json={})
    assert response.status_code == 200, response.text
    assert calls == ["customer"]
    assert client.post(path, headers=headers("moderator", "MODERATOR"), json={}).status_code == 403


def test_paid_stripe_acquired_after_submission_blocks_approval():
    conn = EntitlementConnection()

    async def run():
        row = await pending(conn)
        conn.subscriptions = [{"user_id": "user-a", "plan": "MASTER_MODE", "status": "ACTIVE",
                               "current_period_end": conn.now+timedelta(days=2), "stripe_subscription_id": "paid"}]
        with pytest.raises(HTTPException) as failure:
            await decide(conn, row)
        assert failure.value.status_code == 409
        assert conn.rows[row.id]["status"] == "pending" and conn.periods == {} and conn.audits == []
    asyncio.run(run())


def test_owner_demoted_in_canonical_storage_cannot_approve(api):
    client, conn, row = api
    conn.canonical_users["owner"]["security"]["role"] = "MODERATOR"
    response = client.post("/api/v22/admin/binance-payments/" + row.id + "/approve",
                           headers=headers("owner", "OWNER"), json={"checked_in_binance": True})
    assert response.status_code == 403 and conn.periods == {} and conn.audits == []


def test_future_manual_period_display_is_owned_and_does_not_change_trial_access(api, monkeypatch):
    from app.binance_manual_routes import router
    client, conn, row = api
    monkeypatch.setenv("BINANCE_MANUAL_PAY_ID", "test-pay-id")
    client.app.include_router(router)
    trial_end = conn.now+timedelta(days=7)
    conn.subscriptions = [{"user_id":"customer","plan":"TRIAL","status":"TRIALING",
                           "trial_end":trial_end,"current_period_end":trial_end,"stripe_subscription_id":"trial"}]
    asyncio.run(decide(conn, row))
    response = client.get("/api/v22/payments/binance-manual/requests/current", headers=headers("customer","CUSTOMER"))
    assert response.status_code == 200, response.text
    assert response.json()["period"] == {
        "period_start":trial_end.isoformat().replace("+00:00","Z"),
        "period_end":(trial_end+timedelta(days=30)).isoformat().replace("+00:00","Z"),
    }
    other = client.get("/api/v22/payments/binance-manual/requests/current", headers=headers("moderator","MODERATOR"))
    assert other.status_code == 200 and other.json() == {"request":None,"period":None}
    result = asyncio.run(entitlements.resolve(conn, {"subscriptions":conn.subscriptions}, "customer"))
    assert result["mode"] == "STRIPE" and result["status"] == "TRIALING"


def test_invalid_approved_period_returns_display_error_not_fake_dates(api, monkeypatch):
    from app.binance_manual_routes import router
    client, conn, row = api
    monkeypatch.setenv("BINANCE_MANUAL_PAY_ID", "test-pay-id")
    client.app.include_router(router)
    asyncio.run(decide(conn, row))
    conn.periods[row.id]["period_end"] += timedelta(days=1)
    response = client.get("/api/v22/payments/binance-manual/requests/current", headers=headers("customer","CUSTOMER"))
    assert response.status_code == 503
    assert response.json() == {"detail":"Onaylı ödeme dönemi doğrulanamadı."}


def test_request_proof_is_user_version_and_expiry_bound():
    user = {"id": "user-a", "role": "CUSTOMER", "auth_version": 1}
    app = SimpleNamespace(state=SimpleNamespace(v22_commercial={"state": {"subscriptions": []}}))
    request = Request({"type": "http", "app": app, "headers": []})
    request.state.member_entitlement_proof = entitlements.EntitlementProof(
        "user-b", 1, {"master_trade_access": True}, datetime.now(timezone.utc)+timedelta(days=1),
    )
    with pytest.raises(HTTPException):
        entitlements.access_for_request(request, user)
    request.state.member_entitlement_proof = entitlements.EntitlementProof(
        "user-a", 1, {"master_trade_access": True}, datetime.now(timezone.utc)-timedelta(seconds=1),
    )
    with pytest.raises(HTTPException) as failure:
        entitlements.access_for_request(request, user)
    assert failure.value.status_code == 403
    del request.state.member_entitlement_proof
    with pytest.raises(HTTPException) as failure:
        entitlements.access_for_request(request, user)
    assert failure.value.status_code == 503


def test_migration_retains_previous_audit_actions_and_requires_atomic_completion():
    old = (ROOT / "migrations" / "20261010_013_campaign_permissions_audit.sql").read_text(encoding="utf-8")
    sql = (ROOT / "migrations" / "20261011_002_binance_manual_entitlements.sql").read_text(encoding="utf-8")
    actions = lambda text: set(re.findall(r"'([^']+)'", re.search(r"CHECK \(action IN \((.*?)\)\)", text, re.DOTALL).group(1)))
    assert actions(sql) - actions(old) == {"payment.binance.approved", "payment.binance.rejected"}
    assert actions(old) < actions(sql)
    assert "DEFERRABLE INITIALLY DEFERRED" in sql
    assert "payment_request_id TEXT PRIMARY KEY" in sql and "INTERVAL '720 hours'" in sql
    assert "Approved payment required for period" in sql and "actor_role='OWNER'" in sql
    assert "OLD.status = 'pending' AND NEW.status IN ('approved','rejected')" in sql
    assert "Payment periods are immutable" in sql and "BEFORE DELETE OR TRUNCATE" in sql
    assert "ALTER TABLE subscriptions" not in sql and "INSERT INTO subscriptions" not in sql
    assert sql.strip().endswith("COMMIT;")
