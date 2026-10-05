"""Offline regressions for shared authentication, abuse limits and email safety."""

import asyncio
import base64
import copy
import hashlib
import json
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parents[1]))

from fastapi import HTTPException, Response
from app import local_storage
from app import commercial_core as core
from app import account_erasure as erasure
from app import server_cookie

with patch.object(local_storage, "migrate_legacy_files"):
    from app import v22_commercial as auth


SECRET = b"offline-security-auth-test-secret"
PASSWORD = "OfflineStrongPassword123!"


def legacy_password():
    salt = b"legacy-test-salt!"
    digest = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return {"algorithm": core.LEGACY_PASSWORD_ALGORITHM, "salt": encode(salt), "digest": encode(digest)}


def make_user():
    return {
        "id": "customer", "email": "customer@example.test", "display_name": "Customer",
        "role": "CUSTOMER", "active": True, "email_verified": True,
        "auth_version": 1, "password": legacy_password(), "created_at": auth.now_iso(),
    }


class SharedStore:
    """Model atomic SQL effects, not a local per-worker authentication cache."""

    def __init__(self, user):
        self.users = {user["id"]: {"auth_version": user["auth_version"], "security": auth.auth_security(user)}}
        self.limits = {}
        self.sql = []
        self.snapshot = None
        self.fail = False
        self.erased_hashes = set()
        self.account_settings = {}
        self.account_tokens = {}

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        previous = copy.deepcopy((self.users, self.account_settings))
        try:
            yield
        except BaseException:
            self.users, self.account_settings = previous
            raise

    def check(self, sql):
        self.sql.append(sql)
        if self.fail:
            raise RuntimeError("offline simulated outage")

    async def execute(self, sql, *args):
        self.check(sql)
        if "INSERT INTO commercial_account_tokens" in sql:
            self.account_tokens[args[0]] = {"args": args, "used": False}
            return
        if "INSERT INTO commercial_account_settings" in sql:
            self.account_settings[args[0]] = json.loads(args[1])
            return
        if "INSERT INTO commercial_auth_users" in sql and "VALUES" in sql:
            self.users.setdefault(args[0], {"auth_version": args[1], "security": json.loads(args[2])})
        elif "INSERT INTO application_state_snapshots" in sql:
            self.snapshot = json.loads(args[1])

    async def fetchrow(self, sql, *args):
        self.check(sql)
        if "commercial_account_settings" in sql:
            doc = self.account_settings.get(args[0])
            return {"payload": copy.deepcopy(doc)} if doc else None
        if "commercial_account_tokens" in sql:
            row = self.account_tokens.get(args[0])
            if "INSERT INTO" in sql:
                if row:
                    return None
                self.account_tokens[args[0]] = {"args": args, "used": True}
                return {"token_hash": args[0]}
            if "UPDATE" in sql:
                if not row or row["used"] or row["args"][:5] != args[:5] or row["args"][5] <= args[5]:
                    return None
                row["used"] = True
            return {"token_hash": args[0]} if row else None
        if "SELECT payload FROM application_state_snapshots" in sql:
            return {"payload": copy.deepcopy(self.snapshot)} if self.snapshot is not None else None
        if "commercial_auth_limits" in sql:
            self.limits[args[0]] = self.limits.get(args[0], 0) + 1
            return {"attempts": self.limits[args[0]]}
        row = self.users.get(args[0])
        if row is None:
            return None
        if "UPDATE commercial_auth_users" in sql:
            if len(args) == 5:
                if row["auth_version"] != args[3] or row["security"] != json.loads(args[4]):
                    return None
                delta, updates = args[1], args[2]
            else:
                if len(args) == 3 and args[2] is not None and row["auth_version"] != args[2]:
                    return None
                delta, updates = (1 if "auth_version = auth_version +" in sql else 0), args[1]
            row["auth_version"] += delta
            row["security"].update(json.loads(updates))
        return copy.deepcopy(row)

    async def fetch(self, sql, *args):
        self.check(sql)
        if "commercial_erased_users" in sql:
            return [{"user_hash": value} for value in self.erased_hashes]
        return []


def make_request(user, store=None, *, token=None, host="192.0.2.1"):
    state = core.default_commercial_state()
    state["users"] = [copy.deepcopy(user)]
    rt = {
        "state": state, "secret": SECRET, "lock": asyncio.Lock(), "storage_lock": asyncio.Lock(),
        "auth_baseline": {
            user["id"]: {"auth_version": user["auth_version"], **auth.auth_security(user)},
        },
    }
    application = SimpleNamespace(state=SimpleNamespace(v22_commercial=rt, db_pool=store))
    return SimpleNamespace(
        app=application, client=SimpleNamespace(host=host), state=SimpleNamespace(),
        headers={"authorization": "Bearer " + (token or core.issue_token(user["id"], user["role"], SECRET))},
        url=SimpleNamespace(path="/offline", scheme="https", hostname="api.example.test", netloc="api.example.test"),
        method="POST", cookies={},
    )


class SecurityAuthHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = make_user()
        self.store = SharedStore(self.user)
        self.request = make_request(self.user, self.store)
        self.patches = [
            patch.object(auth, "DURABLE_AUTH_REQUIRED", True),
            patch.object(auth, "save_state"),
            patch.object(auth, "schedule_log_event"),
            patch.object(auth, "restore_demo_state_for_user", new=AsyncMock()),
            patch.object(auth, "restore_v21_state_for_user", new=AsyncMock()),
            patch.object(erasure, "scrub_tombstoned_local_snapshots"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        auth.LOGIN_ATTEMPTS.clear()

    async def expect_status(self, status, coroutine):
        with self.assertRaises(HTTPException) as caught:
            await coroutine
        self.assertEqual(caught.exception.status_code, status)

    async def test_logout_revokes_other_worker_and_stale_snapshot_cannot_resurrect(self):
        worker2 = make_request(self.user, self.store, token=auth.bearer(self.request))
        await auth.authenticated_user_async(worker2)
        with patch("app.exchange_connections.clear_session_vault_for_request", new=AsyncMock()):
            self.assertEqual(await auth.v22_logout(self.request), {"ok": True})
        self.assertEqual(self.store.users["customer"]["auth_version"], 2)
        self.assertTrue(await auth.persist_v22_commercial(worker2.app))
        self.assertEqual(self.store.users["customer"]["auth_version"], 2)
        await self.expect_status(401, auth.authenticated_user_async(worker2))

    async def test_concurrent_invalidation_is_monotonic(self):
        other = make_request(self.user, self.store)
        await asyncio.gather(
            auth.invalidate_user_sessions(self.request, auth.runtime(self.request)["state"]["users"][0]),
            auth.invalidate_user_sessions(other, auth.runtime(other)["state"]["users"][0]),
        )
        self.assertEqual(self.store.users["customer"]["auth_version"], 3)

    async def test_durable_no_database_and_database_outage_fail_closed(self):
        absent = make_request(self.user)
        await self.expect_status(503, auth.authenticated_user_async(absent))
        self.store.fail = True
        await self.expect_status(503, auth.authenticated_user_async(self.request))
        await self.expect_status(503, auth.invalidate_user_sessions(self.request, self.user))
        self.assertEqual(self.user["auth_version"], 1)

    async def test_each_request_checks_shared_state_and_pool_loss_never_falls_back(self):
        await auth.authenticated_user_async(self.request)
        self.store.users["customer"]["auth_version"] = 2
        await self.expect_status(401, auth.authenticated_user_async(self.request))
        self.store.users["customer"]["auth_version"] = 1
        await auth.authenticated_user_async(self.request)
        self.request.app.state.db_pool = None
        with patch.object(auth, "DURABLE_AUTH_REQUIRED", False):
            await self.expect_status(503, auth.authenticated_user_async(self.request))
            await self.expect_status(503, auth.enforce_auth_limit(self.request, "login", self.user["email"]))
            await self.expect_status(503, auth.invalidate_user_sessions(self.request, self.user))

    async def test_background_validator_uses_original_version_each_cycle(self):
        result = await auth.validate_authoritative_session(self.request.app, "customer", 1)
        self.assertEqual(result["auth_version"], 1)
        self.assertNotIn("password", result)
        self.assertNotIn("email", result)
        self.store.users["customer"]["auth_version"] = 2
        await self.expect_status(401, auth.validate_authoritative_session(self.request.app, "customer", 1))
        await self.expect_status(401, auth.validate_authoritative_session(self.request.app, "customer", None))
        self.store.fail = True
        await self.expect_status(503, auth.validate_authoritative_session(self.request.app, "customer", 2))
        self.request.app.state.db_pool = None
        with patch.object(auth, "DURABLE_AUTH_REQUIRED", False):
            await self.expect_status(503, auth.validate_authoritative_session(self.request.app, "customer", 2))

    async def test_background_validator_rejects_erased_and_inactive_accounts(self):
        self.store.users["customer"]["security"]["active"] = False
        await self.expect_status(401, auth.validate_authoritative_session(self.request.app, "customer", 1))
        self.store.users.pop("customer")
        await self.expect_status(401, auth.validate_authoritative_session(self.request.app, "customer", 1))

    async def test_snapshot_write_projects_canonical_auth_version_not_stale_cache(self):
        self.store.users["customer"]["auth_version"] = 7
        self.store.users["customer"]["security"]["active"] = False
        self.assertTrue(await auth.persist_v22_commercial(self.request.app))
        self.assertEqual(self.store.snapshot["users"][0]["auth_version"], 7)
        self.assertFalse(self.store.snapshot["users"][0]["active"])
        self.assertEqual(self.store.users["customer"]["auth_version"], 7)

    async def test_snapshot_restore_projects_authoritative_revocation(self):
        self.store.snapshot = core.default_commercial_state()
        self.store.snapshot["users"] = [copy.deepcopy(self.user)]
        self.store.users["customer"]["auth_version"] = 7
        self.store.users["customer"]["security"]["active"] = False
        self.assertTrue(await auth.restore_v22_commercial(self.request.app))
        restored = auth.runtime(self.request)["state"]["users"][0]
        self.assertEqual(restored["auth_version"], 7)
        self.assertFalse(restored["active"])

    async def test_erasure_tombstone_scrubs_stale_restore_before_rehydration(self):
        self.store.snapshot = core.default_commercial_state()
        self.store.snapshot["users"] = [copy.deepcopy(self.user)]
        self.store.snapshot["profiles"] = [{"user_id": "customer", "full_name": self.user["display_name"]}]
        self.store.snapshot["subscriptions"] = [{"user_id": "customer", "amount_usd": 3, "message": self.user["email"]}]
        self.store.users.pop("customer")
        self.store.erased_hashes.add(erasure.user_hash("customer"))
        self.assertTrue(await auth.restore_v22_commercial(self.request.app))
        restored = auth.runtime(self.request)["state"]
        self.assertEqual(restored["users"], [])
        self.assertEqual(restored["profiles"], [])
        self.assertNotIn(self.user["email"], json.dumps(restored))
        self.assertEqual(restored["subscriptions"][0], {"amount_usd": 3, "user_id": "ERASED"})
        erasure.scrub_tombstoned_local_snapshots.assert_called()

    async def test_every_sync_scrubs_erased_cached_state_even_when_not_dirty(self):
        rt = auth.runtime(self.request)
        rt.update(storage_ready=True, restore_attempted=True)
        rt["state"]["_database_dirty"] = False
        self.store.users.pop("customer")
        self.store.erased_hashes.add(erasure.user_hash("customer"))
        self.request.app.state._binance_demo_user_state = {"customer": {"_user_id": "customer"}, "other": {"_user_id": "other"}}
        await auth.sync_v22_storage(self.request.app)
        self.assertEqual(rt["state"]["users"], [])
        self.assertEqual(self.store.snapshot["users"], [])
        self.assertNotIn(self.user["email"], json.dumps(self.store.snapshot))
        self.assertEqual(set(self.request.app.state._binance_demo_user_state), {"other"})

    async def test_tombstone_scrubs_orphan_profiles_when_users_list_was_already_removed(self):
        rt = auth.runtime(self.request)
        rt["state"]["users"] = []
        rt["state"]["profiles"] = [{"user_id": "customer", "full_name": "Deleted Name", "email": self.user["email"]}]
        rt["state"]["audit"] = [{"subject": "customer", "message": self.user["email"], "kind": "LOGIN"}]
        self.store.erased_hashes.add(erasure.user_hash("customer"))
        await erasure.apply_erasure_tombstones(self.request.app)
        self.assertEqual(rt["state"]["profiles"], [])
        self.assertNotIn(self.user["email"], json.dumps(rt["state"]))
        self.assertEqual(rt["state"]["audit"][0]["user_id"], "ERASED")

    async def test_erasure_metadata_sync_failure_blocks_requests_and_background_until_recovery(self):
        rt = auth.runtime(self.request)
        rt.update(storage_ready=True, restore_attempted=True)
        rt["state"]["_database_dirty"] = False
        with patch.object(auth, "apply_erasure_tombstones", new=AsyncMock(side_effect=RuntimeError("offline tombstone read"))):
            await auth.sync_v22_storage(self.request.app)
        await self.expect_status(503, auth.authenticated_user_async(self.request))
        await self.expect_status(503, auth.validate_authoritative_session(self.request.app, "customer", 1))
        await self.expect_status(503, auth.enforce_auth_limit(self.request, "login", self.user["email"]))
        await auth.sync_v22_storage(self.request.app)
        self.assertFalse(rt["auth_storage_sync_failed"])
        self.assertEqual((await auth.authenticated_user_async(self.request))["id"], "customer")

    async def test_sync_guard_requires_middleware_in_durable_mode(self):
        with self.assertRaises(HTTPException) as caught:
            auth.authenticated_user(self.request)
        self.assertEqual(caught.exception.status_code, 503)
        await auth.authenticated_user_async(self.request)
        self.assertEqual(auth.authenticated_user(self.request)["id"], "customer")

    async def test_sync_local_guard_keeps_signature_role_and_version_checks(self):
        request = make_request(self.user)
        with patch.object(auth, "DURABLE_AUTH_REQUIRED", False):
            self.assertEqual(auth.authenticated_user(request)["id"], "customer")
            with self.assertRaises(HTTPException) as caught:
                auth.authenticated_user(request, owner=True)
            self.assertEqual(caught.exception.status_code, 403)
            request.headers["authorization"] += "tampered"
            with self.assertRaises(HTTPException) as caught:
                auth.authenticated_user(request)
            self.assertEqual(caught.exception.status_code, 401)

    async def test_authoritative_inactive_unverified_and_role_are_enforced(self):
        for updates, owner, status in [
            ({"active": False}, False, 401),
            ({"email_verified": False}, False, 403),
            ({"role": "CUSTOMER"}, True, 403),
        ]:
            with self.subTest(updates=updates):
                self.store.users["customer"]["security"] = {**auth.auth_security(self.user), **updates}
                await self.expect_status(status, auth.authenticated_user_async(self.request, owner=owner))

    async def test_missing_authoritative_user_is_rejected(self):
        self.store.users.clear()
        await self.expect_status(401, auth.authenticated_user_async(self.request))

    async def test_background_boolean_predicate_is_shared_and_fail_closed(self):
        self.assertTrue(await auth.authoritative_user_version_active(self.request.app, "customer", 1))
        for version in (None, 0, 2, True, "1", 1.5):
            self.assertFalse(await auth.authoritative_user_version_active(self.request.app, "customer", version))
        for updates in ({"active": "false"}, {"email_verified": None}, {"email_verified": "true"}):
            self.store.users["customer"]["security"] = {**auth.auth_security(self.user), **updates}
            self.assertFalse(await auth.authoritative_user_version_active(self.request.app, "customer", 1))
        self.store.users["customer"]["security"] = auth.auth_security(self.user)
        self.store.users["customer"]["security"]["active"] = False
        self.assertFalse(await auth.authoritative_user_version_active(self.request.app, "customer", 1))
        self.store.users.clear()
        self.assertFalse(await auth.authoritative_user_version_active(self.request.app, "customer", 1))
        self.store.fail = True
        self.assertFalse(await auth.authoritative_user_version_active(self.request.app, "customer", 1))
        with patch.object(auth, "DURABLE_AUTH_REQUIRED", False):
            self.assertFalse(await auth.authoritative_user_version_active(make_request(self.user).app, "customer", 1))
        self.assertFalse(await auth.authoritative_user_version_active(SimpleNamespace(), "customer", 1))

    async def test_login_refreshes_version_and_lazily_rehashes_legacy(self):
        self.store.users["customer"]["auth_version"] = 4
        response = await auth.v22_login(auth.LoginRequest(email=self.user["email"], password=PASSWORD), self.request)
        record = self.store.users["customer"]["security"]["password"]
        self.assertEqual(record["algorithm"], core.PASSWORD_ALGORITHM)
        self.assertTrue(core.verify_password(PASSWORD, record))
        self.assertEqual(core.verify_token(response["token"], SECRET)["ver"], 4)

    async def test_invalid_password_never_rehashes_or_persists(self):
        with patch.object(auth, "hash_password") as hasher:
            await self.expect_status(401, auth.v22_login(
                auth.LoginRequest(email=self.user["email"], password="incorrect-password"), self.request,
            ))
            hasher.assert_not_called()
        self.assertIsNone(self.store.snapshot)

    async def test_stale_rehash_cannot_overwrite_password_reset(self):
        worker = auth.runtime(self.request)
        worker["state"]["users"][0]["password"] = {"algorithm": "stale-rehash"}
        new_record = {"algorithm": "new-password"}
        self.store.users["customer"]["auth_version"] = 2
        self.store.users["customer"]["security"]["password"] = new_record
        with patch.object(auth.logger, "exception"):
            self.assertFalse(await auth.persist_v22_commercial(self.request.app))
        self.assertEqual(self.store.users["customer"]["security"]["password"], new_record)
        self.assertIsNone(self.store.snapshot)

    async def test_rollback_snapshot_cannot_revert_newer_observed_security(self):
        self.store.users["customer"]["auth_version"] = 2
        self.store.users["customer"]["security"].update(active=False, password={})
        cached = auth.runtime(self.request)["state"]["users"][0]
        await auth.refresh_auth_security(self.request, cached)
        cached.update(copy.deepcopy(self.user))
        self.assertTrue(await auth.persist_v22_commercial(self.request.app))
        self.assertEqual(cached["auth_version"], 2)
        self.assertFalse(cached["active"])
        self.assertEqual(cached["password"], {})
        self.assertEqual(self.store.users["customer"]["security"]["password"], {})

    async def test_existing_bootstrap_user_rotates_sessions(self):
        auth.runtime(self.request)["storage_status"] = "POSTGRESQL_KALICI"
        with patch.object(auth, "BOOTSTRAP_OWNER_EMAIL", self.user["email"]), patch.object(
            auth, "bootstrap_access_allowed", return_value=True,
        ):
            result = await auth.v22_bootstrap(auth.BootstrapRequest(
                email=self.user["email"], password=PASSWORD, display_name="Local Owner",
            ), self.request)
        self.assertEqual(core.verify_token(result["token"], SECRET)["ver"], 2)
        self.assertEqual(self.store.users["customer"]["security"]["role"], "OWNER")
        await self.expect_status(401, auth.authenticated_user_async(self.request))

    async def test_login_durable_persistence_failure_does_not_issue_token(self):
        with patch.object(auth, "persist_v22_commercial", new=AsyncMock(return_value=False)):
            await self.expect_status(503, auth.v22_login(
                auth.LoginRequest(email=self.user["email"], password=PASSWORD), self.request,
            ))

    async def test_login_cannot_adopt_reset_version_after_verifying_old_password(self):
        original_record = core.hash_password(PASSWORD)
        user = auth.runtime(self.request)["state"]["users"][0]
        user["password"] = copy.deepcopy(original_record)
        auth.runtime(self.request)["auth_baseline"]["customer"]["password"] = copy.deepcopy(original_record)
        self.store.users["customer"]["security"]["password"] = copy.deepcopy(original_record)
        persist = auth.persist_v22_commercial

        async def concurrent_reset(application):
            self.store.users["customer"]["auth_version"] = 2
            self.store.users["customer"]["security"]["password"] = core.hash_password("NewStrongPassword456!")
            return await persist(application)

        with patch.object(auth, "persist_v22_commercial", new=concurrent_reset):
            await self.expect_status(401, auth.v22_login(
                auth.LoginRequest(email=self.user["email"], password=PASSWORD), self.request,
            ))
        self.assertEqual(self.store.users["customer"]["auth_version"], 2)

    async def test_all_actions_share_ip_and_normalized_account_limits(self):
        for action, (limit, _) in auth.AUTH_LIMITS.items():
            self.store.limits.clear()
            for index in range(limit):
                request = make_request(self.user, self.store, host=f"192.0.2.{index + 1}")
                await auth.enforce_auth_limit(request, action, " Customer@Example.Test ")
            await self.expect_status(429, auth.enforce_auth_limit(
                make_request(self.user, self.store, host="198.51.100.1"), action, "customer@example.test",
            ))
            self.store.limits.clear()
            for index in range(limit):
                await auth.enforce_auth_limit(self.request, action, f"user{index}@example.test")
            await self.expect_status(429, auth.enforce_auth_limit(self.request, action, "different@example.test"))

    async def test_limits_database_failure_and_missing_database_are_explicit(self):
        self.store.fail = True
        await self.expect_status(503, auth.enforce_auth_limit(self.request, "forgot", self.user["email"]))
        await self.expect_status(503, auth.enforce_auth_limit(make_request(self.user), "register", "new@example.test"))

    async def test_local_development_limits_work_without_database(self):
        request = make_request(self.user)
        with patch.object(auth, "DURABLE_AUTH_REQUIRED", False):
            for _ in range(auth.AUTH_LIMITS["login"][0]):
                await auth.enforce_auth_limit(request, "login", self.user["email"])
            await self.expect_status(429, auth.enforce_auth_limit(request, "login", self.user["email"]))

    async def test_register_duplicate_has_same_response_shape_without_account_data(self):
        payload = auth.RegisterRequest(
            email=self.user["email"], display_name="Submitted Name", password=PASSWORD,
            confirm_password=PASSWORD, terms_accepted=True,
        )
        with patch.object(auth, "has_stable_session_secret", return_value=True), \
             patch.object(auth, "gmail_configured", return_value=True), \
             patch.object(auth, "send_auth_email") as sender, \
             patch.object(auth, "env_flag", return_value=False):
            duplicate = await auth.v22_register(payload, self.request)
            payload.email = "new@example.test"
            new = await auth.v22_register(payload, self.request)
        self.assertEqual(set(duplicate), set(new))
        self.assertEqual(duplicate["message"], new["message"])
        self.assertEqual(set(duplicate["user"]), set(new["user"]))
        self.assertNotEqual(duplicate["user"]["id"], self.user["id"])
        self.assertEqual(duplicate["user"]["display_name"], "Submitted Name")
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(await auth.v22_verification_status(
            self.request, duplicate["verification_status_token"],
        ), {"verified": False})

    async def test_forgot_unconfigured_email_is_explicit_even_for_unknown_account(self):
        with patch.object(auth, "gmail_configured", return_value=False), \
             patch.object(auth, "env_flag", return_value=False):
            for email in (self.user["email"], "missing@example.test"):
                await self.expect_status(503, auth.v22_forgot_password(
                    auth.PasswordResetRequest(email=email), self.request,
                ))

    async def test_forgot_email_delivery_failure_is_explicit(self):
        with patch.object(auth, "gmail_configured", return_value=True), \
             patch.object(auth, "send_auth_email", side_effect=RuntimeError("offline delivery failure")), \
             patch.object(auth, "log_gmail_failure"):
            await self.expect_status(503, auth.v22_forgot_password(
                auth.PasswordResetRequest(email=self.user["email"]), self.request,
            ))

    async def test_registration_provider_failure_does_not_expose_existing_account(self):
        with patch.object(auth, "has_stable_session_secret", return_value=True), \
             patch.object(auth, "gmail_configured", return_value=True), \
             patch.object(auth, "send_auth_email", side_effect=RuntimeError("offline delivery failure")), \
             patch.object(auth, "log_gmail_failure"), \
             patch.object(auth, "env_flag", return_value=False):
            details = []
            for email in (self.user["email"], "new@example.test"):
                with self.assertRaises(HTTPException) as caught:
                    await auth.v22_register(auth.RegisterRequest(
                        email=email, display_name="Submitted Name", password=PASSWORD,
                        confirm_password=PASSWORD, terms_accepted=True,
                    ), self.request)
                self.assertEqual(caught.exception.status_code, 503)
                details.append(caught.exception.detail)
            self.assertEqual(details[0], details[1])
        users = auth.runtime(self.request)["state"]["users"]
        self.assertEqual([row["id"] for row in users], [self.user["id"]])
        self.assertEqual(users[0]["password"], self.user["password"])

    async def test_verify_and_reset_limits_precede_token_consumption(self):
        for action, endpoint, payload in [
            ("verify", auth.v22_verify_email, auth.EmailTokenRequest(token="x" * 30)),
            ("reset", auth.v22_reset_password, auth.PasswordResetConfirmRequest(
                token="x" * 30, password=PASSWORD, confirm_password=PASSWORD,
            )),
        ]:
            self.store.limits.clear()
            with patch.object(auth, "enforce_auth_limit", new=AsyncMock(side_effect=HTTPException(429, "limit"))), \
                 patch.object(auth, "consume_one_time_token") as consumer:
                await self.expect_status(429, endpoint(payload, self.request))
                consumer.assert_not_called()

    async def test_reset_atomically_updates_password_and_revokes_other_worker(self):
        state = auth.runtime(self.request)["state"]
        token = auth.issue_one_time_token(state, state["users"][0], SECRET, kind="PASSWORD_RESET")
        await auth.v22_reset_password(auth.PasswordResetConfirmRequest(
            token=token, password=PASSWORD, confirm_password=PASSWORD,
        ), self.request)
        self.assertEqual(self.store.users["customer"]["auth_version"], 2)
        self.assertEqual(self.store.users["customer"]["security"]["password"]["algorithm"], core.PASSWORD_ALGORITHM)
        await self.expect_status(401, auth.authenticated_user_async(make_request(self.user, self.store)))
        await self.expect_status(400, auth.v22_reset_password(auth.PasswordResetConfirmRequest(
            token=token, password=PASSWORD, confirm_password=PASSWORD,
        ), self.request))

    async def test_schema_migrates_snapshot_without_overwriting_authoritative_records(self):
        await auth.ensure_commercial_schema(self.request.app)
        migration = next(sql for sql in self.store.sql if "INSERT INTO commercial_auth_users" in sql and "jsonb_array_elements" in sql)
        self.assertIn("ON CONFLICT (user_id) DO NOTHING", migration)
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS commercial_auth_limits" in sql for sql in self.store.sql))

    async def test_startup_without_pool_does_not_skip_later_schema_initialization(self):
        absent = make_request(self.user)
        await auth.sync_v22_storage(absent.app)
        self.assertFalse(auth.runtime(absent).get("storage_ready", False))
        absent.app.state.db_pool = self.store
        with patch.object(auth, "restore_v22_commercial", new=AsyncMock(return_value=True)):
            await auth.sync_v22_storage(absent.app)
        self.assertTrue(auth.runtime(absent)["storage_ready"])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS commercial_auth_users" in sql for sql in self.store.sql))

    async def test_blocked_ip_does_not_allocate_additional_account_buckets(self):
        for _ in range(auth.AUTH_LIMITS["register"][0]):
            await auth.enforce_auth_limit(self.request, "register", "same@example.test")
        before = set(self.store.limits)
        await self.expect_status(429, auth.enforce_auth_limit(self.request, "register", "new@example.test"))
        self.assertEqual(set(self.store.limits), before)

    async def test_reset_revocation_failure_restores_local_token_for_retry(self):
        state = auth.runtime(self.request)["state"]
        token = auth.issue_one_time_token(state, state["users"][0], SECRET, kind="PASSWORD_RESET")
        with patch.object(auth, "invalidate_user_sessions", new=AsyncMock(side_effect=HTTPException(503, "offline"))):
            await self.expect_status(503, auth.v22_reset_password(auth.PasswordResetConfirmRequest(
                token=token, password=PASSWORD, confirm_password=PASSWORD,
            ), self.request))
        self.assertFalse(state["auth_tokens"][0]["used"])
        self.assertEqual(state["users"][0]["auth_version"], 1)

    async def test_browser_login_sets_httponly_cookie_without_exposing_signed_token(self):
        self.request.headers["x-requested-with"] = "XMLHttpRequest"
        response = Response()
        result = await auth.v22_login(auth.LoginRequest(
            email=self.user["email"], password=PASSWORD, remember=True,
        ), self.request, response)
        self.assertEqual(result["token"], "cookie-session:customer")
        cookie = response.headers["set-cookie"]
        self.assertIn("protrebot_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertIn("Path=/api", cookie)
        self.assertIn(f"Max-Age={auth.REMEMBER_SESSION_SECONDS}", cookie)
        cookie_token = cookie.split(";", 1)[0].split("=", 1)[1]
        self.assertEqual(core.verify_token(cookie_token, SECRET)["sub"], "customer")
        self.assertNotIn(cookie_token, json.dumps(result))

    async def test_browser_marker_without_cookie_is_not_a_credential(self):
        for headers in [
            {"x-protrebot-session": "cookie-session:customer"},
            {"authorization": "Bearer cookie-session:customer"},
            {},
        ]:
            self.request.headers = headers
            self.assertEqual(auth.request_session_token(self.request), "")
            await self.expect_status(401, auth.authenticated_user_async(self.request))

    async def test_signed_cookie_is_used_for_marker_or_absent_headers(self):
        token = auth.bearer(self.request)
        self.request.cookies[auth.SESSION_COOKIE_NAME] = token
        for headers in [
            {"x-protrebot-session": "cookie-session:customer"},
            {"authorization": "Bearer cookie-session:customer"},
            {},
        ]:
            self.request.headers = headers
            self.assertEqual(auth.request_session_token(self.request), token)
            self.assertEqual((await auth.authenticated_user_async(self.request))["id"], "customer")
        self.request.cookies[auth.SESSION_COOKIE_NAME] = "cookie-session:customer"
        self.assertEqual(auth.request_session_token(self.request), "")

    async def test_shared_cookie_module_exports_same_authentication_resolver(self):
        self.assertIs(auth.request_session_token, server_cookie.request_session_token)
        self.assertEqual(server_cookie.SESSION_COOKIE_NAME, "protrebot_session")
        token = auth.bearer(self.request)
        self.request.headers = {"x-protrebot-session": "COOKIE-SESSION:customer"}
        self.request.cookies[server_cookie.SESSION_COOKIE_NAME] = token
        self.assertEqual(server_cookie.request_session_token(self.request), token)

    async def test_native_bearer_takes_precedence_over_cookie_and_remains_supported(self):
        native = auth.bearer(self.request)
        self.request.cookies[auth.SESSION_COOKIE_NAME] = "invalid-cookie"
        self.assertEqual(auth.request_session_token(self.request), native)
        self.assertEqual((await auth.authenticated_user_async(self.request))["id"], "customer")
        response = Response()
        result = await auth.v22_login(auth.LoginRequest(
            email=self.user["email"], password=PASSWORD,
        ), self.request, response)
        self.assertEqual(core.verify_token(result["token"], SECRET)["sub"], "customer")
        self.assertNotIn("set-cookie", response.headers)

    async def test_browser_logout_revokes_session_and_clears_cookie(self):
        self.request.cookies[auth.SESSION_COOKIE_NAME] = auth.bearer(self.request)
        self.request.headers = {"x-protrebot-session": "cookie-session:customer"}
        response = Response()
        with patch("app.exchange_connections.clear_session_vault_for_request", new=AsyncMock()):
            self.assertEqual(await auth.v22_logout(self.request, response), {"ok": True})
        cookie = response.headers["set-cookie"]
        self.assertIn("Max-Age=0", cookie)
        self.assertIn("Path=/api", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(self.store.users["customer"]["auth_version"], 2)

    async def test_password_change_is_durable_and_clears_browser_cookie(self):
        self.request.cookies[auth.SESSION_COOKIE_NAME] = auth.bearer(self.request)
        self.request.headers = {"x-protrebot-session": "cookie-session:customer"}
        await auth.authenticated_user_async(self.request)
        response = Response()
        result = await auth.v22_change_password(auth.PasswordChangeRequest(
            current_password=PASSWORD, new_password="ChangedStrongPassword456!",
        ), self.request, response)
        self.assertTrue(result["reauthenticate"])
        self.assertEqual(self.store.users["customer"]["auth_version"], 2)
        self.assertTrue(core.verify_password("ChangedStrongPassword456!", self.store.users["customer"]["security"]["password"]))
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        await self.expect_status(401, auth.authenticated_user_async(self.request))

    async def test_password_change_cannot_overwrite_concurrent_session_rotation(self):
        local_user = auth.runtime(self.request)["state"]["users"][0]
        self.store.users["customer"]["auth_version"] = 2
        previous_password = copy.deepcopy(self.store.users["customer"]["security"]["password"])
        await self.expect_status(401, auth.invalidate_user_sessions(
            self.request, local_user, security_updates={"password": {"digest": "stale"}},
            expected_version=1,
        ))
        self.assertEqual(self.store.users["customer"]["security"]["password"], previous_password)
        self.assertEqual(self.store.users["customer"]["auth_version"], 2)

    async def test_password_reset_clears_target_cookie_but_not_another_account_cookie(self):
        state = auth.runtime(self.request)["state"]
        self.request.cookies[auth.SESSION_COOKIE_NAME] = auth.bearer(self.request)
        token = auth.issue_one_time_token(state, state["users"][0], SECRET, kind="PASSWORD_RESET")
        response = Response()
        await auth.v22_reset_password(auth.PasswordResetConfirmRequest(
            token=token, password=PASSWORD, confirm_password=PASSWORD,
        ), self.request, response)
        self.assertIn("Max-Age=0", response.headers["set-cookie"])
        self.request.cookies[auth.SESSION_COOKIE_NAME] = core.issue_token("another-user", "CUSTOMER", SECRET)
        response = Response()
        auth.clear_rotated_session_cookie(self.request, response, "customer")
        self.assertNotIn("set-cookie", response.headers)

    async def test_profile_update_does_not_rotate_or_expose_cookie(self):
        self.request.cookies[auth.SESSION_COOKIE_NAME] = auth.bearer(self.request)
        self.request.headers = {"x-protrebot-session": "cookie-session:customer"}
        await auth.authenticated_user_async(self.request)
        result = await auth.v22_update_profile(auth.ProfileUpdateRequest(display_name="Updated Name"), self.request)
        self.assertEqual(result["user"]["display_name"], "Updated Name")
        self.assertEqual(self.store.users["customer"]["auth_version"], 1)
        self.assertNotIn(self.request.cookies[auth.SESSION_COOKIE_NAME], json.dumps(result))
        self.assertEqual((await auth.authenticated_user_async(self.request))["id"], "customer")


class PasswordAndEmailSecurityTests(unittest.TestCase):
    def test_strong_scrypt_parameters_and_memory_limit(self):
        real_scrypt = hashlib.scrypt
        with patch.object(core.hashlib, "scrypt", wraps=real_scrypt) as scrypt:
            record = core.hash_password(PASSWORD)
        self.assertEqual(record["algorithm"], "SCRYPT-N131072-R8-P1")
        kwargs = scrypt.call_args.kwargs
        self.assertEqual((kwargs["n"], kwargs["r"], kwargs["p"]), (2**17, 8, 1))
        self.assertGreater(kwargs["maxmem"], 128 * kwargs["n"] * kwargs["r"])
        self.assertTrue(core.verify_password(PASSWORD, record))
        self.assertFalse(core.verify_password("wrong-password", record))

    def test_legacy_scrypt_verification_and_invalid_records(self):
        self.assertTrue(core.verify_password(PASSWORD, legacy_password()))
        self.assertFalse(core.verify_password("wrong-password", legacy_password()))
        for record in ({}, {"algorithm": "unknown"}, {"algorithm": core.PASSWORD_ALGORITHM, "salt": "bad"}):
            self.assertFalse(core.verify_password(PASSWORD, record))

    def test_email_escapes_every_interpolation_including_attribute_quotes(self):
        value = '<img src=x onerror="alert(1)">&\''
        body = auth.auth_email_html(value, value, 'https://example.test/?x=" onmouseover="bad', value, value)
        self.assertNotIn("<img", body)
        self.assertNotIn('href="https://example.test/?x="', body)
        self.assertIn("&lt;img", body)
        self.assertIn("&quot;", body)
        self.assertIn("&#x27;", body)
        self.assertIn("&amp;", body)


class ErasureStore(SharedStore):
    def __init__(self, user):
        super().__init__(user)
        self.rows = {
            name: [{"user_id": user["id"]}, {"user_id": "other-user"}]
            for name in erasure.PERSONAL_TABLES
        }
        self.rows["protrebot_exchange_session_vault"][0]["session_id"] = "erased-session"
        self.rows["protrebot_exchange_session_vault"][1]["session_id"] = "other-session"
        self.rows["assistant_calls"] = [
            {"user_id": user["id"], "cost_usd": "1.23", "month_key": "2026-10"},
            {"user_id": "other-user", "cost_usd": "2.34"},
        ]
        self.rows["subscriptions"] = [
            {"user_id": user["id"], "stripe_customer_id": "cus-private", "stripe_subscription_id": "sub-private", "stripe_price_id": "price-private"},
            {"user_id": "other-user", "stripe_customer_id": "cus-other"},
        ]
        self.rows["error_events"] = [{"user_id": user["id"], "message": user["email"]}, {"user_id": "other-user"}]
        self.payloads = {
            "application_state_snapshots": {
                "v22-commercial": {"users": [copy.deepcopy(user)], "audit": [{"subject": user["id"], "message": user["email"], "kind": "LOGIN"}]},
                f"binance_demo:user:{user['id']}": {
                    "events": [{"user_id": user["id"], "symbol": "BTCUSDT", "price": 10.0, "message": user["email"]}],
                    "settings": {"api_key": "private-key"},
                },
                "binance_demo:user:other-user": {"events": [{"symbol": "ETHUSDT"}]},
            },
            "protrebot_cloud_state": {"testnet-primary": {"journal": [{"user_id": user["id"], "price": 10, "message": user["email"]}]}},
            "protrebot_cloud_evidence": {"event": {"user_id": user["id"], "price": 10, "message": user["email"]}},
            "paper_account_snapshots": {"local": {"plans": [{"user_id": user["id"], "price": 10}, {"user_id": "other-user", "price": 20}]}},
        }
        self.fail_delete = None

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        previous = copy.deepcopy((self.users, self.rows, self.payloads, self.erased_hashes))
        try:
            yield self
        except BaseException:
            self.users, self.rows, self.payloads, self.erased_hashes = previous
            raise

    async def fetchval(self, sql, *args):
        self.check(sql)
        if "to_regclass" in sql:
            return args[0] if args[0] in self.rows or args[0] in self.payloads else None
        if "commercial_erased_users" in sql:
            return args[0] in self.erased_hashes
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if "SELECT user_hash" in sql:
            return await super().fetch(sql, *args)
        self.check(sql)
        if "SELECT session_id" in sql:
            return [copy.deepcopy(row) for row in self.rows["protrebot_exchange_session_vault"] if row["user_id"] == args[0]]
        for table, payloads in self.payloads.items():
            if f"FROM {table} " in sql:
                key = {"application_state_snapshots": "state_key", "protrebot_cloud_state": "state_key", "protrebot_cloud_evidence": "event_key", "paper_account_snapshots": "account_key"}[table]
                return [{key: name, "payload": copy.deepcopy(payload)} for name, payload in payloads.items()]
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.check(sql)
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO commercial_erased_users"):
            self.erased_hashes.add(args[0])
            return
        if normalized.startswith("DELETE FROM commercial_auth_users"):
            self.users.pop(args[0], None)
            return
        for table in self.rows:
            if normalized.startswith(f"DELETE FROM {table} "):
                if self.fail_delete == table:
                    raise RuntimeError("offline erasure failure")
                self.rows[table] = [row for row in self.rows[table] if row["user_id"] != args[0]]
                return
        if normalized.startswith("UPDATE assistant_calls") or normalized.startswith("UPDATE subscriptions"):
            table = normalized.split()[1]
            for row in self.rows[table]:
                if row["user_id"] == args[1]:
                    row["user_id"] = args[0]
                    if table == "subscriptions":
                        for key in ("stripe_customer_id", "stripe_subscription_id", "stripe_price_id"):
                            row[key] = None
            return
        if normalized.startswith("UPDATE error_events"):
            for row in self.rows["error_events"]:
                if row["user_id"] == args[0]:
                    row.update(user_id=None, message="Account erased")
            return
        for table, payloads in self.payloads.items():
            if normalized.startswith(f"UPDATE {table} "):
                if len(args) == 3:
                    payloads.pop(args[0], None)
                    payloads[args[2]] = json.loads(args[1])
                else:
                    payloads[args[0]] = json.loads(args[1])
                return
            if normalized.startswith(f"DELETE FROM {table} "):
                payloads.pop(args[0], None)
                return
            if normalized.startswith(f"INSERT INTO {table} "):
                payloads[args[0]] = json.loads(args[1])
                return
        await super().execute(sql, *args)


class AccountErasureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.real_scrub_files = erasure.scrub_local_files
        self.real_scrub_sqlite = erasure.scrub_sqlite_file
        self.real_tombstone_scrub = erasure.scrub_tombstoned_local_snapshots
        self.user = make_user()
        self.store = ErasureStore(self.user)
        self.request = make_request(self.user, self.store)
        state = auth.runtime(self.request)["state"]
        state["users"].append({"id": "other-user", "email": "other@example.test", "role": "CUSTOMER"})
        for key in erasure.PERSONAL_LISTS:
            if key != "users":
                state[key] = [{"user_id": self.user["id"], "email": self.user["email"]}, {"user_id": "other-user"}]
        for key in erasure.RETAINED_LISTS:
            state[key] = [{"user_id": self.user["id"], "amount_usd": 10.0, "message": self.user["email"], "stripe_customer_id": "private-customer"}, {"user_id": "other-user", "amount_usd": 20.0}]
        self.patches = [
            patch.object(auth, "DURABLE_AUTH_REQUIRED", True),
            patch.object(auth, "save_state"),
            patch.object(auth, "schedule_log_event"),
            patch.object(erasure, "scrub_local_files"),
            patch.object(erasure, "scrub_sqlite_file"),
            patch.object(erasure, "scrub_tombstoned_local_snapshots"),
        ]
        self.local_files = None
        for index, item in enumerate(self.patches):
            value = item.start()
            if index == 3:
                self.local_files = value
            self.addCleanup(item.stop)

    async def test_erasure_deletes_personal_data_and_anonymizes_accounting(self):
        result = await erasure.erase_user_account(self.request, auth.runtime(self.request)["state"]["users"][0])
        self.assertTrue(result["ok"])
        self.assertEqual(result["erasure"]["external_stores"], "not_erased")
        self.assertIn("provider_logs_and_backups", result["erasure"]["limitations"])
        self.assertNotIn(self.user["id"], self.store.users)
        self.assertIn(erasure.user_hash(self.user["id"]), self.store.erased_hashes)
        for table in erasure.PERSONAL_TABLES:
            self.assertEqual([row["user_id"] for row in self.store.rows[table]], ["other-user"])
        self.assertEqual(self.store.rows["assistant_calls"][0]["user_id"], "ERASED")
        self.assertEqual(self.store.rows["assistant_calls"][0]["cost_usd"], "1.23")
        self.assertEqual(self.store.rows["assistant_calls"][1]["user_id"], "other-user")
        self.assertIsNone(self.store.rows["subscriptions"][0]["stripe_customer_id"])
        self.assertIsNone(self.store.rows["error_events"][0]["user_id"])
        state = auth.runtime(self.request)["state"]
        self.assertEqual([user["id"] for user in state["users"]], ["other-user"])
        self.assertNotIn(self.user["email"], json.dumps(state))
        self.assertEqual(state["demo_invoices"][0], {"amount_usd": 10.0, "user_id": "ERASED"})
        self.assertTrue(self.local_files.called)
        revocation = next(index for index, sql in enumerate(self.store.sql) if "UPDATE commercial_auth_users" in sql)
        deletion = next(index for index, sql in enumerate(self.store.sql) if "DELETE FROM trading_accounts" in sql)
        self.assertLess(revocation, deletion)

    async def test_cloud_and_demo_personal_snapshots_scrubbed_trade_evidence_retained_unlinked(self):
        await erasure.erase_database(self.request.app, self.user)
        snapshots = self.store.payloads["application_state_snapshots"]
        self.assertNotIn("binance_demo:user:customer", snapshots)
        self.assertIn("binance_demo:user:other-user", snapshots)
        evidence = next(value for key, value in snapshots.items() if key.startswith("erased-trades:"))
        self.assertEqual(evidence["events"][0], {"symbol": "BTCUSDT", "price": 10.0, "user_id": "ERASED"})
        self.assertNotIn(self.user["email"], json.dumps(self.store.payloads))
        self.assertNotIn("private-key", json.dumps(self.store.payloads))
        self.assertEqual(self.store.payloads["paper_account_snapshots"]["local"]["plans"][1]["user_id"], "other-user")

    async def test_erasure_failure_rolls_back_database_but_keeps_revocation_committed(self):
        self.store.fail_delete = "assistant_usage"
        with self.assertRaises(HTTPException) as caught:
            await erasure.erase_user_account(self.request, auth.runtime(self.request)["state"]["users"][0])
        self.assertEqual(caught.exception.status_code, 503)
        self.assertFalse(self.store.users["customer"]["security"]["active"])
        self.assertEqual(self.store.users["customer"]["security"]["password"], {})
        self.assertEqual(self.store.users["customer"]["auth_version"], 2)
        self.assertFalse(self.store.erased_hashes)
        self.assertTrue(any(row["user_id"] == "customer" for row in self.store.rows["trading_accounts"]))

    async def test_erased_account_cannot_be_reintroduced_by_stale_commercial_worker(self):
        self.store.erased_hashes.add(erasure.user_hash(self.user["id"]))
        self.store.users.pop(self.user["id"])
        self.assertTrue(await auth.persist_v22_commercial(self.request.app))
        self.assertNotIn(self.user["id"], self.store.users)
        self.assertEqual([user["id"] for user in auth.runtime(self.request)["state"]["users"]], ["other-user"])

    async def test_self_and_admin_routes_use_the_same_erasure_flow(self):
        target = auth.runtime(self.request)["state"]["users"][0]
        owner = {"id": "owner", "role": "OWNER"}
        with patch.object(auth, "authenticated_user", return_value=target), \
             patch.object(auth, "erase_user_account", new=AsyncMock(return_value={"ok": True})) as flow:
            response = Response()
            self.assertEqual(await auth.v22_delete_profile(self.request, response), {"ok": True})
            flow.assert_awaited_once_with(self.request, target)
            self.assertIn("Max-Age=0", response.headers["set-cookie"])
        with patch.object(auth, "authenticated_user", return_value=owner), \
             patch.object(auth, "erase_user_account", new=AsyncMock(return_value={"ok": True})) as flow:
            payload = auth.UserDeleteRequest(email=self.user["email"], confirmation="DELETE USER")
            self.assertEqual(await auth.v22_admin_delete_user(self.user["id"], payload, self.request), {"ok": True})
            flow.assert_awaited_once_with(self.request, target)

    async def test_owner_cannot_be_erased(self):
        owner = {**self.user, "role": "OWNER"}
        with self.assertRaises(HTTPException) as caught:
            await erasure.erase_user_account(self.request, owner)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.store.users["customer"]["auth_version"], 1)

    async def test_erasure_does_not_trust_stale_customer_role_after_owner_promotion(self):
        self.store.users["customer"]["security"]["role"] = "OWNER"
        with self.assertRaises(HTTPException) as caught:
            await erasure.erase_user_account(self.request, auth.runtime(self.request)["state"]["users"][0])
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(self.store.users["customer"]["security"]["active"])
        self.assertEqual(self.store.users["customer"]["auth_version"], 1)

    async def test_admin_can_retry_incomplete_local_erasure_using_tombstone(self):
        auth.runtime(self.request)["state"]["users"] = []
        self.store.erased_hashes.add(erasure.user_hash(self.user["id"]))
        with patch.object(auth, "authenticated_user", return_value={"id": "owner", "role": "OWNER"}), \
             patch.object(auth, "erase_user_account", new=AsyncMock(return_value={"ok": True})) as flow:
            result = await auth.v22_admin_delete_user(self.user["id"], auth.UserDeleteRequest(
                email=self.user["email"], confirmation="DELETE USER",
            ), self.request)
        self.assertTrue(result["ok"])
        self.assertEqual(flow.await_args.args[1]["id"], self.user["id"])

    async def test_tombstoned_erasure_retry_does_not_require_deleted_auth_row(self):
        self.store.erased_hashes.add(erasure.user_hash(self.user["id"]))
        self.store.users.pop(self.user["id"])
        user = {"id": self.user["id"], "email": self.user["email"], "role": "CUSTOMER"}
        result = await erasure.erase_user_account(self.request, user)
        self.assertTrue(result["ok"])
        self.assertFalse(any("UPDATE commercial_auth_users" in sql for sql in self.store.sql))

    async def test_runtime_cleanup_preserves_other_users_and_removes_target_vault_cache(self):
        from app import exchange_connections as vault
        self.request.app.state._binance_demo_user_state = {"customer": {"_user_id": "customer", "auto": {"enabled": True}}, "other-user": {"_user_id": "other-user"}}
        self.request.app.state._v21_demo_user_state = {"customer": {"_user_id": "customer"}, "other-user": {"_user_id": "other-user"}}
        with patch.dict(vault._SESSION_META, {
            ("target", "LIVE"): {"user_id": "customer"}, ("other", "LIVE"): {"user_id": "other-user"},
        }, clear=True), patch.dict(vault._SESSION_CACHE, {
            ("target", "LIVE"): ("secret", "secret"), ("other", "LIVE"): ("other", "other"),
        }, clear=True):
            await erasure.erase_runtime(self.request.app, self.user, [])
            self.assertNotIn(("target", "LIVE"), vault._SESSION_CACHE)
            self.assertIn(("other", "LIVE"), vault._SESSION_CACHE)
        self.assertEqual(set(self.request.app.state._binance_demo_user_state), {"other-user"})
        self.assertEqual(set(self.request.app.state._v21_demo_user_state), {"other-user"})

    async def test_local_backup_scrub_and_sqlite_deletion_are_scoped(self):
        original = {"users": [self.user, {"id": "other-user"}], "profiles": [{"user_id": "customer", "email": self.user["email"]}]}
        paths = [Path("backend") / "tests" / "mock-primary.json", Path("backend") / "tests" / "mock-backup.json"]
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=json.dumps(original)), \
             patch.object(Path, "write_text") as writer, patch.object(Path, "replace") as replace:
            self.real_scrub_files(paths, self.user)
        self.assertEqual(writer.call_count, 2)
        self.assertEqual(replace.call_count, 2)
        for call in writer.call_args_list:
            self.assertNotIn(self.user["email"], call.args[0])
            self.assertIn("other-user", call.args[0])
        with patch.object(Path, "exists", return_value=True), patch.object(erasure.sqlite3, "connect") as connect:
            connection = connect.return_value.__enter__.return_value
            connection.execute.return_value = [
                ("assistant_usage",), ("assistant_calls",), ("assistant_monthly_spend",),
            ]
            self.real_scrub_sqlite(Path("backend") / "tests" / "mock.sqlite3", "customer")
        statements = [call.args for call in connection.execute.call_args_list]
        self.assertIn(("DELETE FROM assistant_usage WHERE user_id = ?", ("customer",)), statements)
        self.assertIn(("UPDATE assistant_calls SET user_id = ? WHERE user_id = ?", ("ERASED", "customer")), statements)
        self.assertFalse(any("DELETE FROM assistant_monthly_spend" in call[0] for call in statements))
        self.assertIn(("VACUUM",), statements)

    async def test_old_backup_is_scrubbed_using_hash_tombstone_without_plain_email_alias(self):
        payload = {
            "users": [copy.deepcopy(self.user), {"id": "other-user", "email": "other@example.test"}],
            "subscriptions": [{"user_id": "customer", "amount_usd": 5, "email": self.user["email"]}],
        }
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "read_text", return_value=json.dumps(payload)), \
             patch.object(Path, "write_text") as writer, patch.object(Path, "replace"):
            self.real_tombstone_scrub(
                [Path("backend") / "tests" / "mock-backup.json"], {erasure.user_hash("customer")},
            )
        cleaned = json.loads(writer.call_args.args[0])
        self.assertEqual([user["id"] for user in cleaned["users"]], ["other-user"])
        self.assertEqual(cleaned["subscriptions"][0], {"amount_usd": 5, "user_id": "ERASED"})
        self.assertNotIn(self.user["email"], writer.call_args.args[0])

    async def test_schema_guards_auth_snapshots_and_assistant_inflight_writes(self):
        await erasure.ensure_erasure_schema(self.store)
        sql = self.store.sql[-1]
        self.assertIn("commercial_erased_users", sql)
        self.assertIn("sha256(convert_to(NEW.user_id", sql)
        self.assertIn("TG_TABLE_NAME = 'assistant_calls'", sql)
        self.assertIn("BEFORE INSERT OR UPDATE ON application_state_snapshots", sql)
        self.assertIn("binance_demo:user:%", sql)
        self.assertIn("v21_demo:user:%", sql)
        self.assertIn("TG_TABLE_NAME = 'subscriptions'", sql)
        self.assertIn("TG_TABLE_NAME = 'error_events'", sql)
        self.assertIn("commercial_erasure_json", sql)
        self.assertIn("JOIN commercial_auth_users AS auth", sql)

    async def test_guards_are_installed_before_deleting_personal_rows(self):
        await erasure.erase_database(self.request.app, self.user)
        for table in erasure.PERSONAL_TABLES:
            guard = next(index for index, sql in enumerate(self.store.sql) if f"ON {table}" in sql and "CREATE TRIGGER" in sql)
            deletion = next(index for index, sql in enumerate(self.store.sql) if sql.startswith(f"DELETE FROM {table} "))
            self.assertLess(guard, deletion)

    async def test_forgot_password_does_not_email_erased_account_from_stale_worker(self):
        self.store.users.pop(self.user["id"])
        with patch.object(auth, "gmail_configured", return_value=True), \
             patch.object(auth, "send_auth_email") as sender:
            result = await auth.v22_forgot_password(auth.PasswordResetRequest(email=self.user["email"]), self.request)
        self.assertTrue(result["ok"])
        sender.assert_not_called()

    async def test_erasure_retry_cannot_target_another_accounts_email(self):
        auth.runtime(self.request)["state"]["users"] = [{"id": "other-user", "email": "other@example.test"}]
        self.store.erased_hashes.add(erasure.user_hash(self.user["id"]))
        with patch.object(auth, "authenticated_user", return_value={"id": "owner", "role": "OWNER"}), \
             patch.object(auth, "erase_user_account", new=AsyncMock()) as flow:
            with self.assertRaises(HTTPException) as caught:
                await auth.v22_admin_delete_user(self.user["id"], auth.UserDeleteRequest(
                    email="other@example.test", confirmation="DELETE USER",
                ), self.request)
        self.assertEqual(caught.exception.status_code, 422)
        flow.assert_not_awaited()

    async def test_owned_records_from_other_accounts_are_not_erased_by_email(self):
        row = {"id": "other", "role": "OWNER", "email": self.user["email"]}
        self.assertFalse(erasure.owned(row, self.user))
        row = {"id": "profile", "user_id": "other", "email": self.user["email"]}
        self.assertFalse(erasure.owned(row, self.user))
        self.assertEqual(erasure.retained({"cost_usd": "1.23", "message": "private"})["cost_usd"], "1.23")

    async def test_tombstone_recognizes_nonstandard_identifier_fields_without_email_aliases(self):
        payload = {"profiles": [{"userId": "customer", "name": "Deleted Name"}]}
        subjects = erasure.tombstoned_subjects(payload, {erasure.user_hash("customer")})
        self.assertEqual(subjects, [{"id": "customer"}])
        self.assertEqual(erasure.scrub_payload(payload, subjects[0])["profiles"], [])


if __name__ == "__main__":
    unittest.main()
