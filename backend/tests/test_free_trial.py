import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app import binance_demo, free_trial
from app import member_entitlements as entitlements
from app import v25_execution as live
from app.analyst_credits import member_access
from app.subscription_core import entitlement_snapshot
from fastapi import HTTPException, Request
from test_binance_manual_entitlements import EntitlementConnection, decide, pending
from test_moderator_role import headers
from test_moderator_role import setup as canonical_setup

setup = canonical_setup

USER = {"id": "user-a", "role": "CUSTOMER", "auth_version": 1}


class TrialConnection(EntitlementConnection):
    def __init__(self):
        super().__init__()
        self.trials = {}
        self.canonical_users["user-a"] = {"auth_version":1,"security":{"role":"CUSTOMER","active":True,"email_verified":True}}
        self.trial_failure = False

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        before = copy.deepcopy(self.trials)
        try:
            async with super().transaction():
                yield
        except BaseException:
            self.trials = before
            raise

    async def fetchrow(self, sql, *args):
        if "FROM member_free_trials" in sql:
            if self.trial_failure:
                raise OSError("trial storage unavailable")
            row = self.trials.get(args[0])
            return {**copy.deepcopy(row),"now":self.now} if row else None
        if "SELECT security->>'role' AS role" in sql:
            value = self.canonical_users.get(args[0])
            return {"role":value["security"]["role"]} if value else None
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        if "SELECT EXISTS" in sql and "FROM subscriptions" in sql and "plan='TRIAL'" in sql:
            return any(row["user_id"] == args[0] and row["plan"] == "TRIAL" for row in self.subscriptions)
        if "SELECT ends_at FROM member_free_trials" in sql:
            return next((r["ends_at"] for r in self.trials.values()
                         if r["user_id"] == args[0] and r["revoked_at"] is None and r["ends_at"]>self.now),None)
        if "SELECT EXISTS" in sql and "FROM binance_manual_periods" in sql and "period_end>" in sql:
            return any(r["user_id"] == args[0] and r["period_end"]>self.now for r in self.periods.values())
        return await super().fetchval(sql, *args)

    async def execute(self, sql, *args):
        if "INSERT INTO member_free_trials" in sql:
            assert self.is_in_transaction()
            self.trials.setdefault(args[0], {"user_id":args[1],"started_at":self.now,
                                           "ends_at":self.now+timedelta(days=7),"revoked_at":None})
            return "INSERT 0 1"
        return await super().execute(sql,*args)


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setenv("FREE_TRIAL_ENABLED","true")
    monkeypatch.setenv("BINANCE_MANUAL_ACCESS_ENABLED","true")


def test_concurrent_start_is_one_seven_day_period_and_never_writes_stripe():
    conn = TrialConnection()
    async def run():
        first, second = await asyncio.gather(free_trial.start(conn,USER),free_trial.start(conn,USER))
        assert first == second
        assert len(conn.trials) == 1
        assert first["ends_at"]-first["started_at"] == timedelta(days=7)
        assert conn.subscriptions == [] and conn.periods == {}
        conn.now = first["ends_at"]
        with pytest.raises(HTTPException) as failure:
            await free_trial.start(conn,USER)
        assert failure.value.status_code == 409
        assert next(iter(conn.trials.values()))["ends_at"] == first["ends_at"]
    asyncio.run(run())


@pytest.mark.parametrize("field,value,code",[
    ("email_verified",False,403),("active",False,403),("role","MODERATOR",403),
])
def test_canonical_verified_active_customer_required(field,value,code):
    conn = TrialConnection()
    conn.canonical_users["user-a"]["security"][field] = value
    with pytest.raises(HTTPException) as failure:
        asyncio.run(free_trial.start(conn,USER))
    assert failure.value.status_code == code and not conn.trials


def test_revoked_session_old_stripe_trial_and_paid_subscription_cannot_get_new_trial():
    conn = TrialConnection()
    conn.canonical_users["user-a"]["auth_version"] = 2
    with pytest.raises(HTTPException) as failure:
        asyncio.run(free_trial.start(conn,USER))
    assert failure.value.status_code == 401
    conn.canonical_users["user-a"]["auth_version"] = 1
    for row in (
        {"user_id":"user-a","plan":"TRIAL","status":"CANCELLED"},
        {"user_id":"user-a","plan":"MASTER_MODE","status":"ACTIVE","stripe_subscription_id":"stripe-paid",
         "current_period_end":conn.now+timedelta(days=30)},
    ):
        conn.subscriptions = [row]
        with pytest.raises(HTTPException) as failure:
            asyncio.run(free_trial.start(conn,USER))
        assert failure.value.status_code == 409 and not conn.trials


def test_trial_has_demo_and_analysis_access_but_not_assistant_live_or_unlimited_credits():
    conn = TrialConnection()
    async def run():
        await free_trial.start(conn,USER)
        sub = await entitlements.resolve(conn,{"subscriptions":[]},"user-a")
        rights = entitlements.access(sub,USER)
        assert sub["master_trade_access"] and rights["canExecuteMasterTrade"]
        assert not rights["assistantAllowed"] and not rights["canExecuteLive"]
        assert not rights["entitlements"]["unlimitedAnalyst"]
        request = SimpleNamespace(state=SimpleNamespace(member_entitlement_proof=entitlements.EntitlementProof("user-a",1,sub)))
        with patch("app.analyst_credits.authenticated_user",return_value=USER):
            assert member_access(request) == (USER,False)
        conn.now = next(iter(conn.trials.values()))["ends_at"]
        expired = await entitlements.resolve(conn,{"subscriptions":[]},"user-a")
        assert not expired["master_trade_access"] and expired["status"] == "EXPIRED"
        assert not entitlements.access(expired,USER)["assistantAllowed"]
    asyncio.run(run())


def test_paid_stripe_snapshot_unchanged_even_with_missing_trial_schema():
    conn = TrialConnection();conn.trial_failure = True
    state = {"subscriptions":[{"user_id":"user-a","plan":"MASTER_MODE","status":"ACTIVE",
                              "current_period_end":(conn.now+timedelta(days=30)).isoformat()}]}
    result = asyncio.run(entitlements.resolve(conn,state,"user-a"))
    assert result == entitlement_snapshot(state,"user-a")
    assert entitlements.access(result,USER)["canExecuteLive"]


def test_trial_payment_starts_at_trial_end_and_second_approval_does_not_extend():
    conn = TrialConnection()
    async def run():
        trial = await free_trial.start(conn,USER)
        row = await pending(conn)
        await decide(conn,row)
        await decide(conn,row)
        assert len(conn.periods) == 1
        assert conn.periods[row.id]["period_start"] == trial["ends_at"]
        assert conn.periods[row.id]["period_end"] == trial["ends_at"]+timedelta(days=30)
        sub = await entitlements.resolve(conn,{"subscriptions":[]},"user-a")
        assert sub["mode"] == "FREE_TRIAL"
        conn.trials[free_trial.account_hash("user-a")]["started_at"] = conn.now-timedelta(days=8)
        conn.trials[free_trial.account_hash("user-a")]["ends_at"] = conn.now-timedelta(days=1)
        conn.periods[row.id]["period_start"] = datetime.now(timezone.utc)-timedelta(days=1)
        conn.periods[row.id]["period_end"] = conn.periods[row.id]["period_start"]+timedelta(days=30)
        sub = await entitlements.resolve(conn,{"subscriptions":[]},"user-a")
        assert sub["mode"] == "BINANCE_MANUAL" and entitlements.access(sub,USER)["assistantAllowed"]
    asyncio.run(run())


def test_trial_schema_failure_denies_new_demo_entry_but_reduce_only_protection_still_works():
    conn = TrialConnection()
    app = SimpleNamespace(state=SimpleNamespace(db_pool=conn,v22_commercial={"state":{"subscriptions":[]}}))
    client = binance_demo.BinanceDemoClient(AsyncMock(),"offline-key","offline-secret")
    client = binance_demo.bind_demo_private_authority(client,app,{},"user-a",1,datetime.now(timezone.utc).timestamp()+3600)
    conn.trial_failure = True
    client.sync_clock = AsyncMock()
    client._request = AsyncMock(return_value={"orderId":1})
    async def run():
        with patch("app.v22_commercial.validate_authoritative_session",new=AsyncMock()):
            with pytest.raises(binance_demo.BinanceDemoError):
                await client.signed("POST","/fapi/v1/order",{"symbol":"BTCUSDT","type":"MARKET"})
            client._request.assert_not_awaited()
            await client.signed("POST","/fapi/v1/order",{"symbol":"BTCUSDT","type":"MARKET","reduceOnly":True})
            client._request.assert_awaited_once()
    asyncio.run(run())


def test_live_transport_blocks_trial_before_clock_or_network_and_preserves_protection():
    conn = TrialConnection()
    app = SimpleNamespace(state=SimpleNamespace(db_pool=conn,v22_commercial={"state":{"subscriptions":[]}}))
    client = live.BinanceLiveClient(
        AsyncMock(),"offline-key-123","offline-secret-123",application=app,
        diagnostic_state={"live_session_authorization":{"user_id":"user-a"}},
    )
    client.sync_clock = AsyncMock()
    client._request = AsyncMock(return_value={"orderId":1})
    async def run():
        await free_trial.start(conn,USER)
        for path in ("/fapi/v1/order","/fapi/v1/order/test"):
            with pytest.raises(live.LiveExchangeError) as failure:
                await client.signed("POST",path,{"symbol":"BTCUSDT","type":"MARKET"})
            assert failure.value.http_status == 403
            client.sync_clock.assert_not_awaited()
            client._request.assert_not_awaited()
        for protective in ({"reduceOnly":True},{"closePosition":True}):
            await client.signed("POST","/fapi/v1/order",{"symbol":"BTCUSDT",**protective})
        assert client._request.await_count == 2
        conn.trial_failure = True
        with pytest.raises(live.LiveExchangeError) as failure:
            await client.signed("POST","/fapi/v1/order",{"symbol":"BTCUSDT"})
        assert failure.value.http_status == 503 and client._request.await_count == 2
        conn.subscriptions = [{
            "user_id":"user-a","plan":"MASTER_MODE","status":"ACTIVE",
            "current_period_end":conn.now+timedelta(days=30),
        }]
        await client.signed("POST","/fapi/v1/order",{"symbol":"BTCUSDT","type":"MARKET"})
        assert client._request.await_count == 3
    asyncio.run(run())


@pytest.mark.parametrize("path",["/api/v25/order","/api/v25/order/test","/api/v25/auto/start","/api/v25/arm"])
def test_live_entry_endpoint_authority_denies_trial_before_any_exchange_or_ownership_change(path):
    from starlette.requests import Request
    conn = TrialConnection()
    async def run():
        await free_trial.start(conn,USER)
        sub = await entitlements.resolve(conn,{"subscriptions":[]},"user-a")
        request = Request({"type":"http","method":"POST","path":path,"headers":[]})
        request.state.member = USER
        request.state.member_entitlement_proof = entitlements.EntitlementProof("user-a",1,sub)
        with patch.object(live,"authenticated_user",return_value=USER),patch.object(live,"guard_execution_ownership") as claim:
            with pytest.raises(HTTPException) as failure:
                live.execution_owner(request)
            assert failure.value.status_code == 403 and free_trial.LIVE_MESSAGE in failure.value.detail
            claim.assert_not_called()
    asyncio.run(run())


def test_migration_is_prepare_only_has_identity_immutability_and_erasure_guards():
    sql = (Path(__file__).parents[1]/"migrations"/"20261011_004_member_free_trials.sql").read_text()
    for clause in ("user_hash TEXT PRIMARY KEY","INTERVAL '168 hours'","email_verified","ENABLE ALWAYS",
                   "Trial history cannot be deleted","Trial period is immutable","commercial_erased_users"):
        assert clause in sql
    assert "subscriptions SET" not in sql and "DROP TABLE" not in sql


@pytest.fixture
def trial_api(setup):
    client, pool = setup
    conn = TrialConnection()
    conn.users = set(pool.users)
    conn.canonical_users = pool.users
    pool.acquire = conn.acquire
    client.app.include_router(free_trial.router)
    return client, conn


def test_real_authenticated_start_profile_and_canonical_verification(trial_api):
    client, conn = trial_api
    auth = headers("customer", "CUSTOMER")
    conn.canonical_users["customer"]["security"]["email_verified"] = False
    denied = client.post("/api/v22/free-trial/start", headers=auth)
    assert denied.status_code == 403 and not conn.trials
    conn.canonical_users["customer"]["security"]["email_verified"] = True
    first = client.post("/api/v22/free-trial/start", headers=auth)
    again = client.post("/api/v22/free-trial/start", headers=auth)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    profile = client.get("/api/v22/profile", headers=auth)
    assert profile.status_code == 200, profile.text
    assert profile.json()["subscription"]["mode"] == "FREE_TRIAL"
    assert profile.json()["access"]["isPremium"] and not profile.json()["access"]["assistantAllowed"]
    assert not profile.json()["access"]["entitlements"]["unlimitedAnalyst"]
    conn.now += timedelta(days=7)
    assert client.post("/api/v22/free-trial/start", headers=auth).status_code == 409
    profile = client.get("/api/v22/profile", headers=auth)
    assert profile.status_code == 200 and not profile.json()["access"]["isPremium"]
    assert profile.json()["subscription"]["status"] == "EXPIRED"


@pytest.mark.parametrize("method,path",[
    ("POST","/api/assistant/chat"),("POST","/api/assistant/analysis/confirm"),
    ("POST","/api/assistant/proactive/check-in"),("POST","/api/assistant/proactive/preferences"),
    ("GET","/api/assistant/usage"),
])
def test_real_middleware_blocks_all_assistant_surfaces_and_fails_closed(trial_api,method,path):
    client, conn = trial_api
    calls = []
    async def handler():
        calls.append(path)
        return {"reply":"must never be delivered"}
    client.app.add_api_route(path,handler,methods=[method])
    asyncio.run(free_trial.start(conn,{"id":"customer","auth_version":1}))
    for days in (0,7):
        conn.now += timedelta(days=days)
        response = client.request(method,path,headers=headers("customer","CUSTOMER"),json={})
        assert response.status_code == 403 and not calls
        assert response.json()["code"] == "ASSISTANT_MASTER_MODE_REQUIRED"
    conn.trial_failure = True
    response = client.request(method,path,headers=headers("customer","CUSTOMER"),json={})
    assert response.status_code == 503 and not calls


def test_trial_ai_analyst_is_charged_and_cached_with_existing_budget(trial_api,tmp_path):
    from app import analyst_credits as analyst
    client, conn = trial_api
    asyncio.run(free_trial.start(conn,{"id":"customer","auth_version":1}))
    store = analyst.CreditStore(SimpleNamespace(state=SimpleNamespace(db_pool=None)),path=tmp_path/"trial-credits.sqlite3")
    credits = analyst.AnalystCredits(store,analyst.CreditConfig(total=100,cost=7))
    producer = AsyncMock(return_value={"symbol":"BTCUSDT","entry":100})
    @client.app.post("/api/analyst/trial-test")
    async def analysis(request:Request):
        user, unlimited = analyst.member_access(request)
        assert not unlimited
        payload, status = await credits.consume(user["id"],unlimited,"BTCUSDT","15m","trial-analysis-0001",producer)
        assert status == 200
        return payload
    for _ in range(2):
        response = client.post("/api/analyst/trial-test",headers=headers("customer","CUSTOMER"))
        assert response.status_code == 200, response.text
        assert response.json()["remaining"] == 93 and response.json()["analysis_cost"] == 7
    producer.assert_awaited_once()


def test_demo_new_entry_allowed_until_expiry_but_protection_and_close_continue():
    conn = TrialConnection()
    app = SimpleNamespace(state=SimpleNamespace(db_pool=conn,v22_commercial={"state":{"subscriptions":[]}}))
    client = binance_demo.bind_demo_private_authority(
        binance_demo.BinanceDemoClient(AsyncMock(),"offline-key","offline-secret"),
        app,{},"user-a",1,datetime.now(timezone.utc).timestamp()+3600)
    client.sync_clock = AsyncMock()
    client._request = AsyncMock(return_value={"orderId":1})
    async def run():
        await free_trial.start(conn,USER)
        with patch("app.v22_commercial.validate_authoritative_session",new=AsyncMock()):
            await client.signed("POST","/fapi/v1/order",{"symbol":"BTCUSDT","type":"MARKET"})
            assert client._request.await_count == 1
            conn.now += timedelta(days=7)
            with pytest.raises(binance_demo.BinanceDemoError) as denied:
                await client.signed("POST","/fapi/v1/order",{"symbol":"BTCUSDT","type":"MARKET"})
            assert denied.value.http_status == 403 and client._request.await_count == 1
            await client.signed("POST","/fapi/v1/order",{"symbol":"BTCUSDT","type":"MARKET","reduceOnly":True})
            await client.signed("POST","/fapi/v1/order",{"symbol":"BTCUSDT","type":"STOP_MARKET","closePosition":"true"})
            assert client._request.await_count == 3
    asyncio.run(run())
