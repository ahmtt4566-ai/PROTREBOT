import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app import binance_notifications as notices
from app import notification_email
from app import notification_worker as worker
from app.email_service import EmailDeliveryError
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_binance_manual_entitlements import EntitlementConnection, decide, pending
from test_email_service import mocked_https, resend_configuration


class Connection(EntitlementConnection):
    def __init__(self):
        super().__init__()
        self.outbox = {}
        self.reads = set()
        self.enqueue_fail = False
        self.read_fail = False
        self.canonical_users = {
            uid: {"security": {"role": role, "active": True, "email_verified": True,
                               "email": uid+"@example.test", "display_name": uid}}
            for uid, role in [("owner", "OWNER"), ("user-a", "CUSTOMER"), ("other", "CUSTOMER"), ("mod", "MODERATOR")]
        }
        self.application = SimpleNamespace(state=SimpleNamespace(db_pool=self))

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        previous = copy.deepcopy((self.outbox, self.reads))
        try:
            async with super().transaction():
                yield
        except BaseException:
            self.outbox, self.reads = previous
            raise

    async def execute(self, sql, *args):
        if "INSERT INTO notification_outbox" in sql:
            self.check()
            if self.enqueue_fail:
                raise OSError("queue unavailable")
            ident, kind, uid, payload, key = args
            if not any(row["dedupe_key"] == key for row in self.outbox.values()):
                self.outbox[ident] = {"id":ident,"kind":kind,"recipient_user_id":uid,"payload":json.loads(payload),
                                     "dedupe_key":key,"status":"pending","attempts":0,"next_attempt_at":self.now,
                                     "last_error_code":None,"sent_at":None}
            return "INSERT 0 1"
        if "UPDATE notification_outbox SET status = CASE" in sql:
            for row in self.outbox.values():
                if row["status"] == "sending" and row["next_attempt_at"] <= self.now:
                    row["status"] = "dead" if row["attempts"] >= 8 else "pending"
            return "UPDATE 1"
        if "UPDATE notification_outbox SET status = $3" in sql:
            row = self.outbox[args[0]]
            if row["attempts"] != args[1] or row["status"] != "sending":
                return "UPDATE 0"
            row.update(status=args[2],last_error_code=args[3],next_attempt_at=self.now+timedelta(seconds=args[4]),
                       sent_at=self.now if args[2] == "sent" else None)
            return "UPDATE 1"
        if "INSERT INTO binance_notification_reads" in sql:
            if self.read_fail:
                raise OSError("receipt storage unavailable")
            self.reads.add(args[0])
            return "INSERT 0 1"
        return await super().execute(sql, *args)

    async def fetch(self, sql, *args):
        if "WITH sources AS" in sql:
            sources = []
            for row in self.rows.values():
                if row["erased_at"] is not None:
                    continue
                if row["status"] == "pending":
                    sources += [(row["id"],"pending",uid) for uid,u in self.canonical_users.items()
                                if u["security"]["role"] == "OWNER" and u["security"]["active"] and u["security"]["email_verified"]]
                if row["status"] in {"approved","rejected"}:
                    sources.append((row["id"],row["status"],row["user_id"]))
                period = self.periods.get(row["id"])
                if period and period["revoked_at"] is None and self.now < period["period_end"] <= self.now+timedelta(hours=72) \
                        and not any(p["period_end"] > period["period_end"] and p["user_id"] == row["user_id"]
                                    and p["revoked_at"] is None for p in self.periods.values()):
                    sources.append((row["id"],"renewal",row["user_id"]))
            return [{"payment_id":rid,"event":event,"recipient":uid,"channel":channel}
                    for rid,event,uid in sources for channel in ("bell","email")
                    if not any(r["dedupe_key"] == notices.key(rid,event,uid,channel) for r in self.outbox.values())][:100]
        if "FROM notification_outbox o LEFT JOIN binance_notification_reads" in sql:
            return [{**copy.deepcopy(row),"read":row["id"] in self.reads} for row in self.outbox.values()
                    if row["kind"] == notices.BELL and row["status"] == "sent" and row["recipient_user_id"] == args[0]][:args[1]]
        return await super().fetch(sql, *args)

    async def fetchrow(self, sql, *args):
        if "WITH due AS" in sql:
            rows = [r for r in self.outbox.values() if r["status"] in ("pending","failed") and r["attempts"] < 8 and r["next_attempt_at"] <= self.now]
            if not rows:
                return None
            row = rows[0]
            row.update(status="sending",attempts=row["attempts"]+1,next_attempt_at=self.now+timedelta(seconds=args[0]))
            return copy.deepcopy(row)
        if "SELECT * FROM notification_outbox WHERE id" in sql:
            row = self.outbox.get(args[0])
            return copy.deepcopy(row) if row and row["status"] == "sending" and row["attempts"] == args[1] else None
        if "SELECT security->>'email' AS email" in sql:
            user = self.canonical_users.get(args[0],{}).get("security",{})
            return {"email":user["email"]} if user.get("active") and user.get("email_verified") else None
        if "SELECT r.*,p.period_start,p.period_end" in sql:
            row = self.rows.get(args[0])
            if row is None or row["erased_at"] is not None:
                return None
            user = self.canonical_users.get(row["user_id"],{}).get("security",{})
            if not user.get("active") or not user.get("email_verified"):
                return None
            period = self.periods.get(row["id"],{})
            return {**copy.deepcopy(row), "period_start":period.get("period_start"), "period_end":period.get("period_end"),
                    "customer_email":user["email"],"customer_name":user["display_name"]}
        return await super().fetchrow(sql, *args)

    async def fetchval(self, sql, *args):
        if "MIN(next_attempt_at)" in sql:
            return None
        if "SELECT $2::timestamptz" in sql:
            return self.now < args[1] <= self.now+timedelta(hours=72) and not any(
                p["user_id"] == args[0] and p["revoked_at"] is None and p["period_end"] > args[1] for p in self.periods.values())
        if "AND NOT EXISTS (SELECT 1 FROM commercial_auth_users" in sql:
            return any(r["status"] == "pending" for r in self.rows.values()) and not any(
                u["security"]["role"] == "OWNER" and u["security"]["active"] and u["security"]["email_verified"] for u in self.canonical_users.values())
        if "SELECT EXISTS (SELECT 1 FROM commercial_auth_users WHERE user_id=$1" in sql:
            user = self.canonical_users.get(args[0],{}).get("security",{})
            return user.get("role") == "OWNER" and user.get("active") and user.get("email_verified")
        return await super().fetchval(sql, *args)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("BINANCE_MANUAL_NOTIFICATIONS_ENABLED","true")


def run(coro):
    return asyncio.run(coro)


def deliver(conn):
    run(worker.process_due(conn.application))


def test_pending_goes_to_owner_both_channels_once_and_link_targets_owned_card():
    conn = Connection()
    row = run(pending(conn))
    run(notices.sweep(conn.application));run(notices.sweep(conn.application))
    assert len(conn.outbox) == 2 and {r["recipient_user_id"] for r in conn.outbox.values()} == {"owner"}
    with patch.object(worker,"send_notification") as send:
        deliver(conn);deliver(conn)
        assert send.call_count == 1
    items = run(notices.bell_items(conn,"owner",100))
    assert len(items) == 1 and items[0]["target"] == f"/admin?section=binance-payments&payment={row.id}"
    assert "user-a" in items[0]["message"] and "119.90 USDT" in items[0]["message"] and row.reference_code in items[0]["message"]
    assert run(notices.bell_items(conn,"other",100)) == []
    assert run(notices.bell_items(conn,"mod",100)) == []


def test_email_error_cannot_undo_approval_or_block_bell_and_retry_does_not_duplicate():
    conn = Connection()
    row = run(pending(conn));run(decide(conn,row));run(notices.sweep(conn.application))
    error = EmailDeliveryError("private provider text",provider="resend",code="provider_timeout")
    with patch.object(worker,"send_notification",side_effect=error):
        deliver(conn)
    assert conn.rows[row.id]["status"] == "approved" and len(conn.periods) == len(conn.audits) == 1
    email = next(r for r in conn.outbox.values() if r["kind"] == notices.EMAIL)
    assert email["status"] == "failed" and email["last_error_code"] == "provider_timeout"
    items = run(notices.bell_items(conn,"user-a",100))
    assert len(items) == 1 and "MASTER MODE" in items[0]["message"]
    conn.now = email["next_attempt_at"]
    with patch.object(worker,"send_notification") as send:
        deliver(conn);deliver(conn)
        assert send.call_count == 1
    assert len(conn.outbox) == 2 and len(run(notices.bell_items(conn,"user-a",100))) == 1


def test_queue_failure_is_reconciled_from_committed_decision():
    conn = Connection()
    row = run(pending(conn));run(decide(conn,row))
    conn.enqueue_fail = True
    run(notices.sweep(conn.application))
    assert conn.outbox == {} and conn.rows[row.id]["status"] == "approved" and len(conn.periods) == 1
    conn.enqueue_fail = False
    run(notices.sweep(conn.application))
    assert len(conn.outbox) == 2


def test_missing_owner_is_logged_and_request_is_not_lost(caplog):
    conn = Connection()
    row = run(pending(conn))
    conn.canonical_users["owner"]["security"]["role"] = "MODERATOR"
    run(notices.sweep(conn.application))
    assert "no eligible OWNER" in caplog.text and conn.rows[row.id]["status"] == "pending"
    conn.canonical_users["owner"]["security"]["role"] = "OWNER"
    run(notices.sweep(conn.application))
    assert len(conn.outbox) == 2


def test_periodic_sweep_expires_only_unsubmitted_requests_at_exact_boundary():
    conn = Connection()
    from app.binance_manual_payments import create_request
    row = run(create_request(conn,"user-a"))
    conn.now = conn.rows[row.id]["expires_at"]-timedelta(microseconds=1)
    run(notices.sweep(conn.application))
    assert conn.rows[row.id]["status"] == "created"
    conn.now += timedelta(microseconds=1)
    run(notices.sweep(conn.application))
    assert conn.rows[row.id]["status"] == "expired" and conn.outbox == {}


def test_three_day_reminder_boundary_early_renewal_and_dedupe():
    conn = Connection()
    row = run(pending(conn));run(decide(conn,row))
    end = conn.periods[row.id]["period_end"]
    conn.now = end-timedelta(hours=72,microseconds=1)
    run(notices.sweep(conn.application))
    assert not any(r["payload"]["event"] == "renewal" for r in conn.outbox.values())
    conn.now += timedelta(microseconds=1)
    run(notices.sweep(conn.application));run(notices.sweep(conn.application))
    assert sum(r["payload"]["event"] == "renewal" for r in conn.outbox.values()) == 2
    renewed = run(pending(conn,number="renewal"));run(decide(conn,renewed))
    run(notices.sweep(conn.application))
    with patch.object(worker,"send_notification") as send:
        deliver(conn)
        assert all(c.kwargs["payload"]["event"] != "renewal" for c in send.call_args_list)
    assert all(r["status"] == "dead" for r in conn.outbox.values() if r["payload"]["event"] == "renewal")


def test_rejection_reason_and_recipient_identity_are_verified_at_delivery():
    conn = Connection()
    row = run(pending(conn));run(decide(conn,row,"rejected",reason="Binance hesabında ödeme bulunamadı."))
    run(notices.sweep(conn.application))
    with patch.object(worker,"send_notification") as send:
        deliver(conn)
        assert send.call_count == 1 and send.call_args.kwargs["to_email"] == "user-a@example.test"
    items = run(notices.bell_items(conn,"user-a",100))
    assert "Gerekçe: Binance hesabında ödeme bulunamadı." in items[0]["message"]
    assert run(notices.content(conn,"other",{"payment_id":row.id,"event":"rejected"})) is None


@pytest.mark.parametrize("event",["pending","approved","rejected","renewal"])
def test_turkish_email_uses_existing_design_escape_and_resend_idempotency(event,monkeypatch):
    resend_configuration(monkeypatch)
    monkeypatch.setenv("APP_BASE_URL","https://kaistrade.com")
    conn = Connection()
    row = run(pending(conn))
    if event in {"approved","renewal"}:
        run(decide(conn,row))
    elif event == "rejected":
        run(decide(conn,row,"rejected",reason="<script>özel gerekçe</script>"))
    if event == "renewal":
        conn.now = conn.periods[row.id]["period_end"]-timedelta(hours=72)
    payload = run(notices.content(conn,"owner" if event == "pending" else "user-a",{"payment_id":row.id,"event":event}))
    calls = []
    with mocked_https(lambda req: calls.append(req) or httpx.Response(200,json={"id":"accepted"})):
        for _ in range(2):
            notification_email.send_notification(to_email="user-a@example.test",kind=notices.EMAIL,payload=payload,dedupe_key=event)
    assert calls[0].headers["Idempotency-Key"] == calls[1].headers["Idempotency-Key"]
    body = json.loads(calls[0].content)
    assert '<html lang="tr">' in body["html"] and "KaisTrade" in body["subject"]
    assert "<script>" not in body["html"]
    if event == "pending":
        assert "119.90 USDT" in body["text"] and row.reference_code in body["text"] and "&payment=" in body["text"]
    if event == "approved":
        assert "Ödemen onaylandı, MASTER MODE" in body["text"]
    if event == "rejected":
        assert "özel gerekçe" in body["text"] and "&lt;script&gt;" in body["html"]
    if event == "renewal":
        assert "yenileme hatırlatması" in body["subject"]


def test_migration_keeps_previous_outbox_contracts_and_payload_has_no_pii():
    sql = (Path(__file__).parents[1]/"migrations"/"20261011_003_binance_notifications.sql").read_text(encoding="utf-8")
    for kind in ("approval.pending_digest","approval.decision","campaign.info",notices.EMAIL,notices.BELL):
        assert kind in sql
    assert "payload-ARRAY['payment_id','event']='{}'::jsonb" in sql
    assert "Notification receipts are immutable" in sql and "Delivered bell required" in sql
    assert "notification_outbox_guard" not in sql and "ALTER TABLE subscriptions" not in sql
    assert sql.strip().endswith("COMMIT;")


def test_bell_read_is_owned_idempotent_and_rolls_back_on_failure():
    conn = Connection()
    row = run(pending(conn));run(decide(conn,row));run(notices.sweep(conn.application))
    with patch.object(worker,"send_notification"):
        deliver(conn)
    item = run(notices.bell_items(conn,"user-a",100))[0]
    request = Request({"type":"http","app":conn.application,"headers":[]})
    with patch.object(notices,"authenticated_user_async",AsyncMock(return_value={"id":"other"})):
        with pytest.raises(HTTPException) as failure:
            run(notices.merge_bell(request,[],read_id=item["id"]))
        assert failure.value.status_code == 404 and conn.reads == set()
    with patch.object(notices,"authenticated_user_async",AsyncMock(return_value={"id":"user-a"})):
        conn.read_fail = True
        with pytest.raises(HTTPException) as failure:
            run(notices.merge_bell(request,[],read_id=item["id"]))
        assert failure.value.status_code == 503 and conn.reads == set()
        conn.read_fail = False
        first = run(notices.merge_bell(request,[],read_id=item["id"]))
        second = run(notices.merge_bell(request,[],read_all=True))
        assert first == second and first[0]["read"] and len(conn.reads) == 1


def test_owner_demotion_or_customer_erasure_blocks_delivery():
    conn = Connection()
    row = run(pending(conn));run(notices.sweep(conn.application))
    conn.canonical_users["owner"]["security"]["role"] = "MODERATOR"
    with patch.object(worker,"send_notification") as send:
        deliver(conn)
        send.assert_not_called()
    assert all(r["status"] == "dead" for r in conn.outbox.values())
    run(decide(conn,row));run(notices.sweep(conn.application))
    conn.rows[row.id]["erased_at"] = conn.now
    with patch.object(worker,"send_notification") as send:
        deliver(conn)
        send.assert_not_called()


def test_lifecycle_rearms_even_when_outbox_is_empty():
    conn = Connection()
    async def check():
        await worker.sweep_and_arm(conn.application)
        timer = conn.application.state.notification_timer
        delay = timer.when()-asyncio.get_running_loop().time()
        assert 59 < delay <= notices.INTERVAL_SECONDS
        timer.cancel()
    run(check())


def test_scheduling_error_is_logged_without_interrupting_payment_response(caplog):
    conn = Connection()
    with patch.object(worker,"schedule_sweep",side_effect=OSError("scheduler failed")):
        notices.wake(conn.application)
    assert "scheduling failed" in caplog.text


def test_free_rejected_customer_receives_full_owned_bell_without_execution_rights(monkeypatch):
    from app import main, v21_demo
    from app.commercial_core import default_commercial_state
    conn = Connection()
    row = run(pending(conn));run(decide(conn,row,"rejected",reason="Ödeme bulunamadı."));run(notices.sweep(conn.application))
    with patch.object(worker,"send_notification"):
        deliver(conn)
    application = FastAPI()
    application.state.v22_commercial = {"state":default_commercial_state()}
    application.state.db_pool = conn
    application.middleware("http")(main.owner_preview_gate)
    application.include_router(v21_demo.router)
    called = []
    @application.post("/api/v25/order")
    async def entry():
        called.append(True)
        return {"ok":True}
    async def identity(request):
        return {"id":request.headers.get("x-test-user","user-a"),"role":"CUSTOMER","auth_version":1}
    monkeypatch.setattr(main,"authenticated_user_async",identity)
    monkeypatch.setattr(notices,"authenticated_user_async",identity)
    monkeypatch.setattr(main,"hydrate_authenticated_user_state",AsyncMock())
    monkeypatch.setattr(main,"WEB_REQUIRE_AUTH",False)
    legacy = [
        {"id":"paid-"+event,"type":event,"title":"Premium signal title","message":"Paid entry 987654 TP 999888",
         "severity":"info","read":False,"target":"trade-history","timestamp":conn.now.isoformat(),
         "entry_price":987654,"reason":"Private analysis"}
        for event in ("MASTER_MODE_SIGNAL","TRADE_EXECUTED","ANALYSIS_READY","BINANCE_PENDING")
    ]
    monkeypatch.setattr(v21_demo,"notification_payload",lambda state, limit=100:copy.deepcopy(legacy))
    with TestClient(application) as client:
        response = client.get("/api/v21/notifications")
        assert response.status_code == 200
        item = next(item for item in response.json()["items"] if item["id"].startswith("binance:"))
        assert item["title"] == "Binance ödemen reddedildi"
        assert "Gerekçe: Ödeme bulunamadı." in item["message"]
        assert item["severity"] == "warning" and item["target"] == "/billing" and item["read"] is False
        for secret in ("Premium signal title","Paid entry","999888","987654","Private analysis"):
            assert secret not in response.text
        other = client.get("/api/v21/notifications",headers={"x-test-user":"other"}).json()["items"]
        assert not any(item["id"].startswith("binance:") for item in other)
        assert client.post("/api/v21/notifications/"+item["id"]+"/read",headers={"x-test-user":"other"}).status_code == 404
        read = client.post("/api/v21/notifications/"+item["id"]+"/read")
        assert read.status_code == 200 and next(item for item in read.json()["items"] if item["id"].startswith("binance:"))["read"] is True
        assert "Paid entry" not in read.text and "Premium signal title" not in read.text
        with patch.object(v21_demo,"persist_state"):
            bulk = client.post("/api/v21/notifications/read-all")
        assert bulk.status_code == 200 and "Paid entry" not in bulk.text and "Premium signal title" not in bulk.text
        assert client.post("/api/v25/order").status_code == 403 and called == []


@pytest.mark.parametrize("event", ["BINANCE_APPROVED", "BINANCE_REJECTED", "BINANCE_RENEWAL"])
def test_only_verified_binance_customer_event_shape_gets_full_text(event):
    from app.premium_access import public_notification_projection
    item = {"id":"binance:"+"a"*32,"type":event,"title":"Ödeme başlığı","message":"Ödeme açıklaması",
            "severity":"info","timestamp":"2026-10-11T00:00:00Z","read":False,"target":"/billing",
            "entry":12345,"analysis":"Secret analysis"}
    result = public_notification_projection({"items":[item]})["items"][0]
    assert result["title"] == item["title"] and result["message"] == item["message"]
    assert "entry" not in result and "analysis" not in result
    for changed in ({**item,"type":"MASTER_MODE_SIGNAL"},{**item,"type":"BINANCE_PENDING"},{**item,"id":"fake-id"}):
        hidden = public_notification_projection({"items":[changed]})["items"][0]
        assert "title" not in hidden and "message" not in hidden and "entry" not in hidden
