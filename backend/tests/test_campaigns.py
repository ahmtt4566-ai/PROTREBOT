import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app import campaign_worker, notification_worker
from app.campaign_content import unsubscribe_token
from app.campaign_routes import router
from test_moderator_role import headers, setup
from test_notification_worker import WorkerPool

MOD = headers("moderator", "MODERATOR")
OWNER = headers("owner", "OWNER")
ROOT = "/api/mod/campaigns"
DRAFT = {"title": "İç duyuru", "subject": "Yeni özellik", "body": "Yeni özellik kullanıma açıldı.\n\nhttps://kaistrade.com/settings", "audience": "all_users"}


class CampaignPool(WorkerPool):
    def __init__(self, users):
        super().__init__(users)
        self.campaigns = {}
        self.recipients = {}
        self.preferences = {}
        self.subscription_rows = []
        self.tests = {}
        self.budget = {"day": self.now.date(), "attempts": 0, "next_send_at": self.now}
        self.campaign_enqueue_fail = False
        self.delivery_busy = False

    @asynccontextmanager
    async def transaction(self):
        old = copy.deepcopy((self.campaigns, self.recipients, self.preferences, self.tests, self.budget))
        try:
            async with super().transaction():
                yield
        except BaseException:
            self.campaigns, self.recipients, self.preferences, self.tests, self.budget = old
            raise

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock(71010011)" in sql or "pg_advisory_lock(71010011)" in sql or "pg_advisory_unlock(71010011)" in sql or "SELECT pg_sleep" in sql:
            self.check(sql)
            return "SELECT 1"
        if "UPDATE campaign_delivery_budget SET next_send_at=" in sql:
            self.check(sql)
            if self.budget["day"] < self.now.date():
                self.budget.update(day=self.now.date(), attempts=1)
            self.budget["next_send_at"] = self.now + timedelta(seconds=.5)
            return "UPDATE 1"
        if "campaign-pref:" in sql:
            self.check(sql)
            return "SELECT 1"
        if "INSERT INTO notification_outbox" in sql and "'campaign.info'" in sql:
            self.check(sql)
            if self.campaign_enqueue_fail:
                raise RuntimeError("private@example.test provider secret")
            assert not any(r["dedupe_key"] == args[3] for r in self.outbox.values())
            self.outbox[args[0]] = {"id": args[0], "kind": "campaign.info", "priority": 10, "recipient_user_id": args[1],
                "payload": json.loads(args[2]), "dedupe_key": args[3], "status": "pending", "attempts": 0,
                "next_attempt_at": (args[4] if len(args) > 4 else None) or self.now,
                "created_at": self.now, "last_error_code": None, "sent_at": None}
            return "INSERT 0 1"
        if "INSERT INTO campaign_recipients" in sql:
            self.check(sql)
            assert (args[0], args[1]) not in self.recipients
            self.recipients[args[:2]] = dict(zip(("campaign_id", "user_id", "status", "skip_reason", "outbox_id"), args))
            return "INSERT 0 1"
        if "UPDATE campaign_recipients" in sql:
            self.check(sql)
            for r in self.recipients.values():
                if r["status"] == "queued" and (r["outbox_id"] == args[0] if "outbox_id=$1" in sql else r["campaign_id"] == args[0]):
                    r.update(status=args[1] if len(args) > 1 else "skipped", skip_reason=args[2] if len(args) > 2 else "cancelled")
            return "UPDATE 1"
        if "UPDATE notification_outbox SET next_attempt_at=GREATEST" in sql:
            self.check(sql)
            b = self.budget
            self.outbox[args[0]]["next_attempt_at"] = (self.now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                if b["day"] == self.now.date() and b["attempts"] >= args[1] else max(self.now + timedelta(seconds=.5), b["next_send_at"]))
            return "UPDATE 1"
        if "UPDATE notification_outbox SET status='dead'" in sql:
            self.check(sql)
            for r in self.outbox.values():
                if r["kind"] == "campaign.info" and r["payload"]["campaign_id"] == args[0] and r["status"] in ("pending", "failed"):
                    r.update(status="dead", last_error_code="campaign_cancelled")
            return "UPDATE 1"
        if "INSERT INTO email_preferences" in sql:
            self.check(sql)
            if args[0] in self.users and args[0] not in self.erased:
                self.preferences[args[0]] = args[1]
            return "INSERT 0 1"
        if "INSERT INTO commercial_erased_users" in sql:
            from app.account_erasure import user_hash
            erased = {uid for uid in self.users if user_hash(uid) == args[0]}
            self.erased.update(erased)
            for uid in erased:
                self.preferences.pop(uid, None)
            self.recipients = {k: r for k, r in self.recipients.items() if r["user_id"] not in erased}
            for r in self.campaigns.values():
                if r["created_by"] in erased:
                    r.update(title="[erased]", subject="[erased]", body="[erased]", status="cancelled")
            for r in self.outbox.values():
                if r["recipient_user_id"] in erased:
                    r.update(recipient_user_id="ERASED", payload={}, dedupe_key="erased:" + r["id"],
                             status="sent" if r["status"] == "sent" else "dead", last_error_code="recipient_erased")
            return "INSERT 0 1"
        return await super().execute(sql, *args)

    async def fetch(self, sql, *args):
        if "SELECT id FROM campaigns c" in sql:
            self.check(sql)
            return [{"id": r["id"]} for r in self.campaigns.values() if r["status"] == "sending" and (
                r["recipient_count"] != sum(x["campaign_id"] == r["id"] for x in self.recipients.values())
                or any(x["campaign_id"] == r["id"] and x["status"] == "queued" and self.outbox[x["outbox_id"]]["status"] in ("sent", "dead") for x in self.recipients.values()))][:10]
        if "SELECT r.outbox_id,o.status" in sql:
            self.check(sql)
            return [{"outbox_id": r["outbox_id"], "status": self.outbox[r["outbox_id"]]["status"],
                     "last_error_code": self.outbox[r["outbox_id"]]["last_error_code"]}
                    for r in self.recipients.values() if r["campaign_id"] == args[0] and r["status"] == "queued"
                    and self.outbox[r["outbox_id"]]["status"] in ("sent", "dead")]
        if "FROM commercial_auth_users u LEFT JOIN email_preferences" in sql:
            self.check(sql)
            return [{"user_id": uid, "role": r["security"]["role"], "opt_out": self.preferences.get(uid, False)}
                    for uid, r in sorted(self.users.items()) if uid not in self.erased and r["security"]["active"] and r["security"]["email_verified"]]
        if "FROM subscriptions WHERE user_id=ANY" in sql:
            self.check(sql)
            return [r for r in self.subscription_rows if r["user_id"] in args[0]]
        if "SELECT * FROM campaigns WHERE ($1" in sql:
            self.check(sql)
            rows = [r for r in self.campaigns.values() if args[0] or r["created_by"] == args[1]]
            return copy.deepcopy(rows[args[3]:args[3] + args[2]])
        return await super().fetch(sql, *args)

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock(71010011)" in sql:
            self.check(sql)
            return not self.delivery_busy
        if "COUNT(*) FROM campaigns" in sql:
            self.check(sql)
            return sum(args[0] or r["created_by"] == args[1] for r in self.campaigns.values())
        if "SELECT announcements_opt_out" in sql:
            self.check(sql)
            return self.preferences.get(args[0], False)
        return await super().fetchval(sql, *args)

    async def fetchrow(self, sql, *args):
        if "INSERT INTO campaigns" in sql:
            self.check(sql)
            row = dict(zip(("id", "title", "subject", "body", "audience", "created_by", "created_by_role", "content_hash", "scheduled_at"), args))
            row.update(kind="info", status="draft", approval_request_id=None, recipient_count=0, queued_count=0,
                       sent_count=0, failed_count=0, skipped_count=0, version=1, created_at=self.now, updated_at=self.now,
                       started_at=None, finished_at=None, send_key_hash=None)
            self.campaigns[row["id"]] = row
            return copy.deepcopy(row)
        if "SELECT * FROM campaigns WHERE id=" in sql:
            self.check(sql)
            return copy.deepcopy(self.campaigns.get(args[0]))
        if "UPDATE campaigns SET" in sql:
            self.check(sql)
            r = self.campaigns[args[0]]
            if "SET title=" in sql:
                r.update(zip(("title", "subject", "body", "audience", "scheduled_at", "content_hash"), args[1:]))
            elif "SET status='sending'" in sql:
                r.update(status="sending", send_key_hash=args[1], started_at=self.now)
            elif "SET status='pending_approval'" in sql:
                r.update(status="pending_approval", approval_request_id=args[1])
            elif "SET recipient_count=" in sql:
                r.update(zip(("recipient_count", "queued_count", "sent_count", "failed_count", "skipped_count", "status"), args[1:7]))
                if args[7]:
                    r["finished_at"] = self.now
            elif "SET status=$2" in sql:
                r.update(status=args[1], queued_count=0,
                    skipped_count=sum(x["status"] == "skipped" for x in self.recipients.values() if x["campaign_id"] == args[0]),
                    finished_at=self.now if args[1] == "cancelled" else None)
                if args[1] == "draft":
                    r["approval_request_id"] = None
            else:
                raise AssertionError(sql)
            r.update(version=r["version"] + 1, updated_at=self.now)
            return copy.deepcopy(r)
        if "COUNT(*)::integer AS recipient" in sql:
            self.check(sql)
            rows = [r for r in self.recipients.values() if r["campaign_id"] == args[0]]
            return {"recipient": len(rows), **{s: sum(r["status"] == s for r in rows) for s in ("queued", "sent", "failed", "skipped")}}
        if "INSERT INTO campaign_test_limits" in sql:
            self.check(sql)
            count = self.tests.get(args[0], 0)
            if count >= 5:
                return None
            self.tests[args[0]] = count + 1
            return {"attempts": count + 1}
        if "SELECT user_id FROM moderator_permissions" in sql and len(args) == 1:
            self.check(sql)
            permission = "campaigns.manage" if "campaigns.manage" in sql else "approvals.create"
            return {"user_id": args[0]} if permission in self.permissions.get(args[0], {}) else None
        if "INSERT INTO approval_requests" in sql and "'campaign.send'" in sql:
            self.check(sql)
            r = {"id": args[0], "action_type": "campaign.send", "target_user_id": None, "requester_user_id": args[1],
                 "requester_role": "MODERATOR", "payload": json.loads(args[2]), "target_snapshot": {}, "reason": args[3],
                 "status": "pending", "decided_by": None, "decided_at": None, "decision_note": None, "executed_at": None,
                 "result_code": None, "expires_at": self.now + timedelta(hours=72), "version": 1, "created_at": self.now}
            self.approvals[r["id"]] = r
            return copy.deepcopy(r)
        if "WITH due AS" in sql:
            self.check(sql)
            rows = [r for r in self.outbox.values() if r["kind"] != "campaign.info" and r["status"] in ("pending", "failed") and r["next_attempt_at"] <= self.now]
            if not rows:
                return None
            r = rows[0]
            r.update(status="sending", attempts=r["attempts"] + 1, next_attempt_at=self.now + timedelta(seconds=120))
            if r["kind"] == "approval.pending_digest":
                r["payload"].setdefault("count", len(self.approvals))
            return copy.deepcopy(r)
        if "SELECT * FROM notification_outbox WHERE kind='campaign.info'" in sql:
            self.check(sql)
            rows = [r for r in self.outbox.values() if r["kind"] == "campaign.info" and r["status"] in ("pending", "failed") and r["attempts"] < 8 and r["next_attempt_at"] <= self.now]
            return copy.deepcopy(rows[0]) if rows else None
        if "UPDATE campaign_delivery_budget" in sql:
            self.check(sql)
            b = self.budget
            if b["next_send_at"] > self.now or b["day"] == self.now.date() and b["attempts"] >= args[0]:
                return None
            b.update(day=self.now.date(), attempts=b["attempts"] + 1 if b["day"] == self.now.date() else 1,
                     next_send_at=self.now + timedelta(seconds=.5))
            return {"id": 1}
        if "UPDATE notification_outbox SET status='sending'" in sql:
            self.check(sql)
            r = self.outbox[args[0]]
            r.update(status="sending", attempts=r["attempts"] + 1, next_attempt_at=self.now + timedelta(seconds=120))
            return copy.deepcopy(r)
        if "SELECT u.security->>'email' AS email" in sql:
            self.check(sql)
            r = self.users.get(args[0])
            if not r or args[0] in self.erased or not r["security"]["active"] or not r["security"]["email_verified"]:
                return None
            return {"email": r["security"]["email"], "opt_out": self.preferences.get(args[0], False)}
        return await super().fetchrow(sql, *args)


@pytest.fixture
def campaigns(setup, monkeypatch):
    client, _ = setup
    pool = CampaignPool(client.app.state.v22_commercial["state"]["users"])
    pool.settings["moderator"] = {"two_factor_enabled": True}
    pool.permissions["moderator"] = {"campaigns.manage": {}, "approvals.create": {}}
    client.app.state.db_pool = pool
    client.app.include_router(router)
    monkeypatch.setattr(notification_worker, "schedule_sweep", lambda _: None)
    monkeypatch.setenv("CAMPAIGN_EMAIL_ENABLED", "true")
    yield client, pool


def make(client, actor=MOD, **values):
    result = client.post(ROOT, headers=actor, json={**DRAFT, **values})
    assert result.status_code == 200, result.text
    return result.json()


def send(client, row, count=3, actor=OWNER, key="offline-campaign-send-key"):
    return client.post(ROOT + "/" + row["id"] + "/send", headers=actor,
        json={"idempotency_key": key, "confirmed_recipient_count": count, "content_hash": row["content_hash"]})


def process(client):
    with patch.object(campaign_worker, "send_campaign") as mail, patch.object(notification_worker, "send_notification") as approval_mail:
        asyncio.run(notification_worker.process_due(client.app))
    return mail, approval_mail


def test_permission_mfa_customer_own_drafts_and_direct_owner_only(campaigns):
    client, pool = campaigns
    row = make(client)
    assert send(client, row, actor=MOD).status_code == 403
    assert client.post(ROOT, headers=headers("customer", "CUSTOMER"), json=DRAFT).status_code == 403
    pool.permissions["moderator"].pop("campaigns.manage")
    assert client.get(ROOT, headers=MOD).status_code == 403
    pool.permissions["moderator"]["campaigns.manage"] = {}
    pool.settings["moderator"]["two_factor_enabled"] = False
    assert client.get(ROOT, headers=MOD).json()["detail"]["code"] == "mfa_required"
    pool.settings["moderator"]["two_factor_enabled"] = True
    other = make(client, actor=OWNER)
    assert client.get(ROOT + "/" + other["id"], headers=MOD).status_code == 404
    assert client.put(ROOT + "/" + other["id"], headers=MOD, json={**DRAFT, "version": 1}).status_code == 404
    assert client.get(ROOT, headers=MOD).json()["total"] == 1
    assert client.get(ROOT, headers=OWNER).json()["total"] == 2


def test_request_does_not_queue_campaign_mail_owner_approval_is_atomic(campaigns):
    client, pool = campaigns
    row = make(client)
    requested = client.post(ROOT + "/" + row["id"] + "/request-send", headers=MOD)
    assert requested.status_code == 200
    assert pool.recipients == {} and all(r["kind"] != "campaign.info" for r in pool.outbox.values())
    aid = requested.json()["approval_request_id"]
    detail = client.get("/api/v22/admin/approvals/" + aid, headers=OWNER)
    assert detail.status_code == 200 and detail.json()["campaign_preview"]["recipient_count"] == 3
    assert client.put(ROOT + "/" + row["id"], headers=MOD, json={**DRAFT, "version": 2}).status_code == 409
    result = client.post("/api/v22/admin/approvals/" + aid + "/approve", headers=OWNER)
    assert result.status_code == 200 and result.json()["status"] == "executed"
    assert len(pool.recipients) == 3 and len([r for r in pool.outbox.values() if r["kind"] == "campaign.info"]) == 3
    assert pool.canonical_changes == 0
    assert client.post("/api/v22/admin/approvals/" + aid + "/approve", headers=OWNER).status_code == 409


@pytest.mark.parametrize("change", ["body", "grant", "role", "expired"])
def test_stale_or_expired_request_never_starts_campaign(campaigns, change):
    client, pool = campaigns
    row = make(client)
    requested = client.post(ROOT + "/" + row["id"] + "/request-send", headers=MOD).json()
    aid = requested["approval_request_id"]
    if change == "body":
        pool.campaigns[row["id"]]["body"] = "Changed after approval request"
    elif change == "grant":
        pool.permissions["moderator"].pop("campaigns.manage")
    elif change == "role":
        pool.users["moderator"]["security"]["role"] = "CUSTOMER"
    else:
        pool.approvals[aid]["expires_at"] = pool.now - timedelta(hours=1)
    assert client.post("/api/v22/admin/approvals/" + aid + "/approve", headers=OWNER).status_code == 409
    assert pool.approvals[aid]["status"] == ("expired" if change == "expired" else "stale")
    assert pool.recipients == {}


def test_concurrent_send_count_guard_and_idempotency(campaigns):
    client, pool = campaigns
    row = make(client, actor=OWNER)
    assert send(client, row, count=2).status_code == 409
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda key: send(client, row, key=key).status_code, ["offline-campaign-key-one", "offline-campaign-key-two"]))
    assert sorted(results) == [200, 409] and len(pool.recipients) == 3
    key = "offline-campaign-key-one" if results[0] == 200 else "offline-campaign-key-two"
    assert send(client, row, key=key).status_code == 200
    assert len([r for r in pool.outbox.values() if r["kind"] == "campaign.info"]) == 3


def test_concurrent_approvals_only_one_starts(campaigns):
    client, pool = campaigns
    row = make(client)
    aid = client.post(ROOT + "/" + row["id"] + "/request-send", headers=MOD).json()["approval_request_id"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: client.post("/api/v22/admin/approvals/" + aid + "/approve", headers=OWNER).status_code, range(2)))
    assert sorted(results) == [200, 409] and len(pool.recipients) == 3


@pytest.mark.parametrize("group,expected", [("all_users", {"owner", "moderator", "customer"}), ("team_only", {"owner", "moderator"}), ("premium_users", {"customer"})])
def test_audience_uses_canonical_entitlement_and_skips_optout(campaigns, group, expected):
    client, pool = campaigns
    pool.subscription_rows = [{"user_id": "customer", "plan": "TRIAL", "status": "TRIALING",
                              "trial_end": pool.now + timedelta(days=1), "updated_at": pool.now}]
    pool.preferences["customer"] = True
    pool.users["inactive"] = copy.deepcopy(pool.users["customer"])
    pool.users["inactive"]["security"]["active"] = False
    pool.users["unverified"] = copy.deepcopy(pool.users["customer"])
    pool.users["unverified"]["security"]["email_verified"] = False
    pool.users["erased"] = copy.deepcopy(pool.users["customer"])
    pool.erased.add("erased")
    row = make(client, actor=OWNER, audience=group)
    assert send(client, row, count=len(expected)).status_code == 200
    assert {r["user_id"] for r in pool.recipients.values()} == expected
    if "customer" in expected:
        assert pool.recipients[row["id"], "customer"]["status"] == "skipped"


@pytest.mark.parametrize("change", ["inactive", "erased", "optout", "cancel"])
def test_delivery_rechecks_and_cancel_sends_no_mail(campaigns, change):
    client, pool = campaigns
    row = make(client, actor=OWNER)
    assert send(client, row).status_code == 200
    if change == "cancel":
        assert client.post(ROOT + "/" + row["id"] + "/cancel", headers=OWNER).status_code == 200
    else:
        for uid in pool.users:
            if change == "inactive":
                pool.users[uid]["security"]["active"] = False
            elif change == "erased":
                pool.erased.add(uid)
            else:
                pool.preferences[uid] = True
    mail, _ = process(client)
    assert mail.call_count == 0


def test_priority_campaign_rate_daily_limit_and_tomorrow(campaigns, monkeypatch):
    client, pool = campaigns
    row = make(client, actor=OWNER)
    assert send(client, row).status_code == 200
    aid = "urgent"
    pool.outbox[aid] = {"id": aid, "kind": "approval.decision", "recipient_user_id": "moderator",
        "payload": {"approval_id": aid, "result": 1}, "dedupe_key": aid, "status": "pending", "attempts": 0,
        "next_attempt_at": pool.now, "created_at": pool.now, "last_error_code": None, "sent_at": None}
    order = []
    monkeypatch.setenv("CAMPAIGN_DAILY_LIMIT", "1")
    with patch.object(campaign_worker, "send_campaign", side_effect=lambda **_: order.append("campaign")), \
         patch.object(notification_worker, "send_notification", side_effect=lambda **_: order.append("approval")):
        asyncio.run(notification_worker.process_due(client.app))
        assert order == ["approval", "campaign"]
        asyncio.run(notification_worker.process_due(client.app))
        assert len(order) == 2
        assert any(r["next_attempt_at"].date() > pool.now.date() for r in pool.outbox.values() if r["status"] == "pending")
        pool.now += timedelta(days=1)
        asyncio.run(notification_worker.process_due(client.app))
        assert order == ["approval", "campaign", "campaign"]
    assert pool.budget["attempts"] == 1


def test_provider_retry_and_permanent_failure_complete_with_safe_audit(campaigns):
    from app.email_service import EmailDeliveryError
    client, pool = campaigns
    row = make(client, actor=OWNER, audience="premium_users")
    # No eligible recipients completes atomically, with a real zero report.
    assert send(client, row, count=0).json()["status"] == "sent"
    row = make(client, actor=OWNER)
    assert send(client, row).status_code == 200
    with patch.object(campaign_worker, "send_campaign", side_effect=EmailDeliveryError("private secret", provider="resend", code="permanent_rejection")):
        for _ in range(3):
            asyncio.run(notification_worker.process_due(client.app))
            pool.now += timedelta(seconds=1)
    assert pool.campaigns[row["id"]]["status"] == "failed"
    assert pool.campaigns[row["id"]]["failed_count"] == 3
    assert all("private" not in str(r) for r in pool.audits)
    assert any(r["action"] == "campaign.completed" for r in pool.audits)


def test_test_mail_is_caller_only_limited_and_not_a_recipient(campaigns):
    client, pool = campaigns
    row = make(client)
    for _ in range(5):
        assert client.post(ROOT + "/" + row["id"] + "/test", headers=MOD).status_code == 200
    assert client.post(ROOT + "/" + row["id"] + "/test", headers=MOD).status_code == 429
    mail, _ = process(client)
    assert mail.call_count == 1 and mail.call_args.kwargs["to_email"] == "moderator@example.test"
    assert mail.call_args.kwargs["test"] is True and pool.recipients == {}


def test_unsubscribe_get_post_generic_token_and_resubscribe(campaigns):
    client, pool = campaigns
    token = unsubscribe_token(client.app.state.v22_commercial["secret"], "customer")
    url = "/announcements/unsubscribe?token=" + token
    before = copy.deepcopy(pool.preferences)
    assert client.get(url).status_code == 200 and pool.preferences == before
    result = client.post(url, content="List-Unsubscribe=One-Click")
    assert result.status_code == 200 and pool.preferences["customer"] is True
    assert client.post(url, content="action=subscribe").status_code == 200 and pool.preferences["customer"] is False
    assert client.post("/announcements/unsubscribe?token=broken").json() == result.json()


def test_erasure_scrubs_recipient_preferences_and_blocks_delivery(campaigns):
    from app import account_erasure
    client, pool = campaigns
    row = make(client, actor=OWNER)
    assert send(client, row).status_code == 200
    pool.preferences["customer"] = True
    with patch.object(account_erasure, "table_exists", AsyncMock(return_value=False)):
        asyncio.run(account_erasure.erase_database(client.app, {"id": "customer"}))
    assert "customer" not in pool.preferences
    assert all(r["user_id"] != "customer" for r in pool.recipients.values())
    mail, _ = process(client)
    assert all(call.kwargs["to_email"] != "customer@example.test" for call in mail.call_args_list)


def test_audit_or_outbox_failure_rolls_back_send_and_private_fields_never_escape(campaigns):
    client, pool = campaigns
    row = make(client, actor=OWNER)
    pool.fail_action = "campaign.send_started"
    assert send(client, row).status_code == 503
    assert pool.campaigns[row["id"]]["status"] == "draft" and pool.recipients == {} and pool.outbox == {}
    pool.fail_action = None
    pool.campaign_enqueue_fail = True
    assert send(client, row).status_code == 503
    assert pool.campaigns[row["id"]]["status"] == "draft" and pool.recipients == {}
    pool.campaign_enqueue_fail = False
    response = send(client, row)
    assert response.status_code == 200
    assert "@example.test" not in response.text and "@example.test" not in str(pool.outbox)
    assert all("body" not in r and "subject" not in r for r in pool.audits)


def test_withdrawal_allows_edit_and_schedule_delays_delivery(campaigns):
    client, pool = campaigns
    row = make(client)
    assert client.post(ROOT + "/" + row["id"] + "/request-send", headers=MOD).status_code == 200
    result = client.post(ROOT + "/" + row["id"] + "/cancel?withdraw=true", headers=MOD)
    assert result.status_code == 200 and result.json()["status"] == "draft"
    result = client.put(ROOT + "/" + row["id"], headers=MOD, json={**DRAFT, "version": result.json()["version"], "scheduled_at": (pool.now + timedelta(hours=1)).isoformat()})
    assert result.status_code == 200
    assert send(client, result.json()).status_code == 200
    mail, _ = process(client)
    assert mail.call_count == 0


def test_campaign_rate_never_claims_more_than_two_per_second(campaigns):
    client, pool = campaigns
    row = make(client, actor=OWNER)
    assert send(client, row).status_code == 200
    async def claim():
        async with pool.acquire() as conn, conn.transaction():
            return await campaign_worker.claim_campaign(conn)
    first = asyncio.run(claim())
    assert first is not None
    assert asyncio.run(claim()) is None
    pool.now += timedelta(seconds=.5)
    assert asyncio.run(claim()) is not None
    assert asyncio.run(claim()) is None
    assert pool.budget["attempts"] == 2


def test_crashed_final_lease_reconciles_recipient_and_completes(campaigns):
    client, pool = campaigns
    row = make(client, actor=OWNER)
    assert send(client, row).status_code == 200
    for outbox in pool.outbox.values():
        outbox.update(status="sending", attempts=8, next_attempt_at=pool.now)
    mail, _ = process(client)
    assert mail.call_count == 0
    assert pool.campaigns[row["id"]]["status"] == "failed"
    assert pool.campaigns[row["id"]]["failed_count"] == 3


def test_provider_unavailable_is_failed_retry_not_sent_and_eighth_attempt_dead(campaigns):
    from app.email_service import EmailDeliveryError
    client, pool = campaigns
    row = make(client, actor=OWNER, audience="team_only")
    assert send(client, row, count=2).status_code == 200
    target = next(iter(pool.outbox.values()))
    with patch.object(campaign_worker, "send_campaign", side_effect=EmailDeliveryError("not configured", provider="resend", code="provider_unavailable")):
        asyncio.run(notification_worker.process_due(client.app))
        assert target["status"] == "failed" and target["last_error_code"] == "provider_unavailable" and target["attempts"] == 1
        assert target["next_attempt_at"] > pool.now
        for attempt in range(2, 9):
            pool.now = target["next_attempt_at"]
            asyncio.run(notification_worker.process_due(client.app, limit=1))
            assert target["attempts"] == attempt
    assert target["status"] == "dead"


def test_unsubscribe_has_rate_limit_and_public_gate_keeps_other_paths_protected(campaigns):
    from app.web_security import evaluate_access
    client, _ = campaigns
    assert evaluate_access(required=True, configured_token="", authorization=None, path="/announcements/unsubscribe", method="POST").allowed
    assert not evaluate_access(required=True, configured_token="", authorization=None, path="/admin", method="GET").allowed
    for _ in range(30):
        assert client.post("/announcements/unsubscribe?token=bad").status_code == 200
    assert client.post("/announcements/unsubscribe?token=bad").status_code == 429


def test_slow_campaign_lane_does_not_block_approval_sweep(campaigns):
    client, _ = campaigns
    async def verify():
        campaign_started = asyncio.Event()
        release_campaign = asyncio.Event()
        approvals_finished = 0
        async def process(application, *, limit=10, include_campaigns=True):
            nonlocal approvals_finished
            if include_campaigns:
                campaign_started.set()
                await release_campaign.wait()
            else:
                approvals_finished += 1
        with patch.object(notification_worker, "process_due", side_effect=process):
            await notification_worker.sweep_and_arm(client.app)
            await asyncio.wait_for(campaign_started.wait(), .5)
            await asyncio.wait_for(notification_worker.sweep_and_arm(client.app), .5)
            assert approvals_finished == 2
            assert not client.app.state.campaign_notification_task.done()
            release_campaign.set()
            await client.app.state.campaign_notification_task
            await notification_worker.shutdown_notifications(client.app)
    asyncio.run(verify())


def test_provider_start_has_cross_process_lock_and_database_clock_spacing(campaigns):
    client, pool = campaigns
    row = make(client, actor=OWNER)
    assert send(client, row).status_code == 200
    mail, _ = process(client)
    assert mail.call_count == 1
    assert any("pg_advisory_xact_lock(71010011)" in sql for sql in pool.queries)
    assert any("pg_try_advisory_lock(71010011)" in sql for sql in pool.queries)
    assert any("pg_advisory_unlock(71010011)" in sql for sql in pool.queries)
    assert any("pg_sleep" in sql and "next_send_at-clock_timestamp()" in sql and "FOR UPDATE" in sql for sql in pool.queries)


def test_provider_start_charges_actual_utc_day_if_claim_crosses_midnight(campaigns):
    client, pool = campaigns
    row = make(client, actor=OWNER)
    assert send(client, row).status_code == 200
    async def run():
        async with pool.acquire() as conn, conn.transaction():
            lease = await campaign_worker.claim_campaign(conn)
        previous_day = pool.budget["day"]
        pool.now = pool.now.replace(hour=23, minute=59, second=59) + timedelta(seconds=2)
        with patch.object(campaign_worker, "send_campaign") as mail:
            async with pool.acquire() as conn, conn.transaction():
                await campaign_worker.deliver_campaign(conn, lease, client.app)
            assert mail.call_count == 1
        assert pool.budget["day"] > previous_day and pool.budget["attempts"] == 1
    asyncio.run(run())


def test_busy_global_delivery_lane_does_not_claim_or_wait(campaigns):
    client, pool = campaigns
    row = make(client, actor=OWNER)
    assert send(client, row).status_code == 200
    pool.delivery_busy = True
    mail, _ = process(client)
    assert mail.call_count == 0
    assert all(r["status"] == "pending" and r["attempts"] == 0 for r in pool.outbox.values())


def test_two_campaign_senders_and_old_lease_cannot_duplicate_provider_send(campaigns):
    client, pool = campaigns
    row = make(client, actor=OWNER)
    assert send(client, row).status_code == 200
    async def run():
        with patch.object(campaign_worker, "send_campaign") as mail:
            await asyncio.gather(notification_worker.process_due(client.app), notification_worker.process_due(client.app))
            assert mail.call_count == 1
            sent = next(r for r in pool.outbox.values() if r["status"] == "sent")
            before = copy.deepcopy(sent)
            async with pool.acquire() as conn, conn.transaction():
                await campaign_worker.deliver_campaign(conn, sent, client.app)
            assert mail.call_count == 1 and sent == before
    asyncio.run(run())


def test_expired_campaign_cancellation_preserves_lazy_expiry_and_empty_target_contract(campaigns):
    from app.approval_service import CampaignApprovalItem
    from app.moderator_approvals import router as approval_router
    from pydantic import ValidationError
    client, pool = campaigns
    client.app.include_router(approval_router)
    row = make(client)
    aid = client.post(ROOT + "/" + row["id"] + "/request-send", headers=MOD).json()["approval_request_id"]
    pool.approvals[aid]["expires_at"] = pool.now - timedelta(hours=1)
    result = client.post("/api/mod/approvals/" + aid + "/cancel", headers=MOD)
    assert result.status_code == 409 and pool.approvals[aid]["status"] == "expired"
    assert pool.recipients == {}
    with pytest.raises(ValidationError):
        CampaignApprovalItem.model_validate({**pool.approvals[aid], "needs_review": False, "target_snapshot": {"invented": None}})


def test_old_draft_unsubscribe_signature_is_issued_from_send_start(campaigns):
    from app.campaign_content import token_user
    client, pool = campaigns
    row = make(client, actor=OWNER)
    pool.campaigns[row["id"]]["created_at"] = pool.now - timedelta(days=181)
    assert send(client, row).status_code == 200
    mail, _ = process(client)
    assert mail.call_count == 1
    assert token_user(client.app.state.v22_commercial["secret"], mail.call_args.kwargs["token"]) is not None
