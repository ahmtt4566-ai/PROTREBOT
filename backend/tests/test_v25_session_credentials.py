import asyncio
import sys
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import exchange_connections, v22_commercial, v25_execution  # noqa: E402
from app.commercial_core import issue_token  # noqa: E402


class V25SessionCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tokens = {}
        self.secret = b"offline-session-fixture-signing-key"

    def request(self, session: str, application=None, *, method="GET", bind=False):
        if session not in self.tokens:
            self.tokens[session] = issue_token("owner", "OWNER", self.secret, token_version=1)
        user = {"id": "owner", "role": "OWNER", "active": True, "email_verified": True, "auth_version": 1}
        application = application or SimpleNamespace(state=SimpleNamespace(v25_execution=v25_execution.initial_state()))
        application.state.v22_commercial = {"secret": self.secret, "state": {"users": [user]}}
        async def fetchrow(query, user_id):
            self.assertEqual(user_id, "owner")
            if "FROM commercial_account_settings" in query:
                return None
            self.assertIn("FROM commercial_auth_users", query)
            return {"auth_version": 1, "security": {"active": True, "role": "OWNER", "email_verified": True}}
        application.state.db_pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=fetchrow))
        request = SimpleNamespace(
            app=application, method=method, state=SimpleNamespace(),
            headers={"x-protrebot-session": self.tokens[session]}, cookies={},
        )
        request.state.member = asyncio.run(v22_commercial.authenticated_user_async(request))
        if bind:
            v25_execution.guard_execution_ownership(request, claim=True)
        return request

    def test_request_fixture_checks_canonical_auth_and_distinct_account_store(self):
        request = self.request("session-a")
        calls = request.app.state.db_pool.fetchrow.await_args_list
        self.assertTrue(any("FROM commercial_auth_users" in call.args[0] for call in calls))
        self.assertTrue(any("FROM commercial_account_settings" in call.args[0] for call in calls))
        self.assertEqual(request.state.member["id"], "owner")
        self.assertEqual(request.state.member["role"], "OWNER")
        self.assertEqual(request.state.v22_authoritative_token, self.tokens["session-a"])

    def live_metadata(self):
        return {
            "user_id": "owner", "active": True, "configured": True,
            "account": {"api_permissions": {"withdrawal_check": "VERIFIED", "enableWithdrawals": False}},
        }

    @contextmanager
    def rotation_context(self):
        old = ("OLD-LIVE-KEY-offline-AAA", "OLD-LIVE-SECRET-offline-AAA")
        new = ("NEW-LIVE-KEY-offline-BBB", "NEW-LIVE-SECRET-offline-BBB")
        account = {
            "account_identity": "account-one", "tested_at": "now", "active_positions": 0,
            "orders_created": False, **self.live_metadata()["account"],
        }
        state = v25_execution.initial_state()
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        request = self.request("rotation", application, method="POST")
        fingerprint = v25_execution.credential_fingerprint(old[0])
        state["live_session_authorization"].update(
            user_id="owner", session_id="owner:owner", fingerprint=fingerprint,
            trading_account_id="BINANCE:LIVE:account-one",
        )
        state.update(connected=True, armed_until=time.time() + 600, real_trading_locked=False)
        state["auto"]["enabled"] = True
        state["snapshot"] = {
            "account_identity": "account-one", "wallet_balance": 100,
            "positions": [], "open_orders": [], "open_algo_orders": [],
        }
        state["snapshot_session_id"] = "owner:owner"
        scope = ("owner:owner", "LIVE")
        with patch.dict("os.environ", {"PROTREBOT_VAULT_MASTER_KEY": "offline-rotation-vault-master-key"}), \
             patch.dict(exchange_connections._SESSION_CACHE, {scope: old}, clear=True), \
             patch.dict(exchange_connections._CREDENTIAL_LOCKS, {}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {
                 scope: {**self.live_metadata(), "fingerprint": exchange_connections.key_fingerprint(old[0]), "account": account},
             }, clear=True), \
             patch.object(exchange_connections, "_require_live_mfa", new=AsyncMock()), \
             patch.object(exchange_connections, "_verify_request_credentials", new=AsyncMock(return_value=account)), \
             patch.object(v25_execution, "persist_state"):
            holder = {"row": {
                "user_id": "owner", "mode": "LIVE",
                "encrypted_payload": exchange_connections.encrypt_credentials(*old, mode="LIVE"),
                "fingerprint": exchange_connections.key_fingerprint(old[0]),
                "active": True, "last_test_ok": True, "account_summary": account,
                "last_test_at": None, "last_error": None, "updated_at": None,
            }}

            async def execute(query, *args):
                if "VALUES ($1, 'LIVE'" in query:
                    holder["row"] = {
                        **(holder["row"] or {}), "user_id": "owner", "mode": "LIVE",
                        "encrypted_payload": args[1], "fingerprint": args[2],
                        "active": True, "last_test_ok": True, "account_summary": args[3],
                    }
                elif query.lstrip().startswith("UPDATE protrebot_exchange_account_vault"):
                    if "AND encrypted_payload = $4" in query and holder["row"]["encrypted_payload"] != args[3]:
                        return "UPDATE 0"
                    holder["row"]["active"] = "SET active = TRUE" in query
                    if "account_summary = $3" in query:
                        holder["row"]["account_summary"] = args[2]
                elif query.lstrip().startswith("DELETE FROM protrebot_exchange_account_vault"):
                    holder["row"] = None
                return "UPDATE 1"

            pool = SimpleNamespace(
                execute=AsyncMock(side_effect=execute),
                fetch=AsyncMock(return_value=[]),
                fetchrow=AsyncMock(side_effect=lambda *args: holder["row"]),
            )
            application.state.db_pool = pool
            application.state.exchange_vault = {"ready": True, "pool_id": id(pool)}
            application.state.live_account_cache = {"value": {"wallet_balance": 100}, "expires": 999999, "generation": 0}
            yield request, holder, old, new, account

    def save_rotated_key(self, request, new):
        return exchange_connections.exchange_connection_save(
            request, exchange_connections.SaveCredentialsRequest(
                mode="LIVE", api_key=new[0], secret_key=new[1],
                confirmation="CANLI KASAYA KAYDET", totp_code="654321",
            ),
        )

    def test_profile_hydration_does_not_wait_for_live_locks(self):
        from app import main

        for blocked_lock in ("credential", "controller"):
            with self.subTest(blocked_lock=blocked_lock), self.rotation_context() as (request, holder, old, new, account):
                request.url = SimpleNamespace(path="/api/v22/profile")
                state = request.app.state.v25_execution
                lock = (
                    exchange_connections._credential_lock(request, "LIVE")
                    if blocked_lock == "credential"
                    else state["lock"]
                )
                with patch.object(main, "restore_demo_state_for_user", new=AsyncMock(return_value=None)), \
                     patch.object(main, "restore_v21_state_for_user", new=AsyncMock(return_value=None)):
                    async def scenario():
                        await lock.acquire()
                        try:
                            await asyncio.wait_for(main.hydrate_authenticated_user_state(request), timeout=0.1)
                        finally:
                            lock.release()
                    asyncio.run(scenario())
                self.assertEqual(state["live_session_authorization"]["fingerprint"], v25_execution.credential_fingerprint(old[0]))
                self.assertTrue(state["connected"])
                self.assertTrue(state["auto"]["enabled"])
                self.assertEqual(request.app.state.live_account_cache["generation"], 0)

    def test_live_status_hydration_still_synchronizes_current_vault_key(self):
        from app import main

        with self.rotation_context() as (request, holder, old, new, account):
            request.url = SimpleNamespace(path="/api/v25/status")
            holder["row"].update(
                encrypted_payload=exchange_connections.encrypt_credentials(*new, mode="LIVE"),
                fingerprint=exchange_connections.key_fingerprint(new[0]),
            )
            with patch.object(main, "restore_demo_state_for_user", new=AsyncMock(return_value=None)), \
                 patch.object(main, "restore_v21_state_for_user", new=AsyncMock(return_value=None)):
                asyncio.run(main.hydrate_authenticated_user_state(request))
            self.assertEqual(v25_execution.live_credentials_status(request)[:2], new)
            v25_execution.guard_execution_ownership(request)
            self.assertFalse(request.app.state.v25_execution["connected"])
            self.assertEqual(request.app.state.v25_execution["armed_until"], 0)

    def test_blocked_live_binding_keeps_verified_vault_available_only_for_read_only_test(self):
        with self.rotation_context() as (request, holder, old, new, account):
            state = request.app.state.v25_execution
            state["execution_state"] = "UNKNOWN"
            holder["row"].update(
                encrypted_payload=exchange_connections.encrypt_credentials(*new, mode="LIVE"),
                fingerprint=exchange_connections.key_fingerprint(new[0]),
            )
            asyncio.run(exchange_connections.ensure_session_cache(request))
            connection = exchange_connections.session_public_status(request.app, request)["connections"]["LIVE"]
            self.assertTrue(connection["active"])
            self.assertIn("belirsiz", connection["last_error"])
            self.assertEqual(state["live_session_authorization"]["fingerprint"], v25_execution.credential_fingerprint(old[0]))
            self.assertEqual(state["execution_state"], "UNKNOWN")
            self.assertTrue(state["real_trading_locked"])
            self.assertEqual(state["armed_until"], 0)
            self.assertFalse(state["auto"]["enabled"])
            with self.assertRaises(exchange_connections.HTTPException) as failure:
                v25_execution.guard_execution_ownership(request)
            self.assertEqual(failure.exception.status_code, 403)
            result = asyncio.run(exchange_connections.exchange_connection_test(
                request, exchange_connections.TestCredentialsRequest(mode="LIVE"),
            ))
            self.assertEqual(result["fingerprint"], exchange_connections.key_fingerprint(new[0]))
            self.assertFalse(result["account"]["orders_created"])
            exchange_connections._verify_request_credentials.assert_awaited_once_with(
                request, "LIVE", new[0], new[1],
            )
            with patch.object(v25_execution, "background_authorization_active", new=AsyncMock(return_value=True)), \
                 patch.object(exchange_connections, "authoritative_live_owner_active", new=AsyncMock(return_value=True)):
                self.assertEqual(asyncio.run(v25_execution.auto_session_credentials(
                    request.app, state, force_refresh=True,
                )), ("", ""))

    def test_rotation_updates_vault_and_live_binding_without_opening_entries(self):
        with self.rotation_context() as (request, holder, old, new, account):
            result = asyncio.run(self.save_rotated_key(request, new))
            state = request.app.state.v25_execution
            self.assertEqual(exchange_connections.decrypt_credentials(holder["row"]["encrypted_payload"], mode="LIVE"), new)
            self.assertEqual(
                exchange_connections.normalize_fingerprint(state["live_session_authorization"]["fingerprint"]),
                exchange_connections.normalize_fingerprint(result["connections"]["LIVE"]["fingerprint"]),
            )
            v25_execution.guard_execution_ownership(request)
            self.assertFalse(state["connected"])
            self.assertEqual(state["armed_until"], 0)
            self.assertFalse(state["auto"]["enabled"])
            self.assertTrue(state["real_trading_locked"])
            self.assertIsNone(request.app.state.live_account_cache["value"])
            self.assertEqual(state["snapshot"], {})
            self.assertNotIn(old[0][:4], str(result))
            self.assertNotIn(new[0][:4], str(result))
            self.assertNotIn(new[1], str(result))

    def test_failed_rotation_keeps_old_key_and_closes_entry_locks(self):
        with self.rotation_context() as (request, holder, old, new, account):
            original_execute = request.app.state.db_pool.execute.side_effect

            async def fail_save(query, *args):
                if "VALUES ($1, 'LIVE'" in query:
                    raise RuntimeError(f"{new[0]} {new[1]} 654321 signature=offline-secret")
                return await original_execute(query, *args)

            request.app.state.db_pool.execute.side_effect = fail_save
            with self.assertLogs(exchange_connections.logger, level="ERROR") as logs:
                with self.assertRaises(exchange_connections.HTTPException) as failure:
                    asyncio.run(self.save_rotated_key(request, new))
            self.assertEqual(failure.exception.status_code, 503)
            for value in (*new, "654321", "signature=offline-secret"):
                self.assertNotIn(value, str(logs.output) + str(failure.exception.detail))
            self.assertEqual(exchange_connections.session_credentials(request, "LIVE", active_only=False), old)
            self.assertEqual(exchange_connections.decrypt_credentials(holder["row"]["encrypted_payload"], mode="LIVE"), old)
            self.assertTrue(request.app.state.v25_execution["real_trading_locked"])
            self.assertEqual(request.app.state.v25_execution["armed_until"], 0)

    def test_missing_vault_row_evicts_stale_live_key(self):
        with self.rotation_context() as (request, holder, old, new, account):
            holder["row"] = None
            asyncio.run(exchange_connections.ensure_session_cache(request))
            self.assertEqual(v25_execution.live_credentials_status(request), ("", "", None))
            self.assertFalse(exchange_connections.session_public_status(request.app, request)["connections"]["LIVE"]["configured"])

    def test_vault_fingerprint_mismatch_and_decrypt_failure_are_fail_closed(self):
        for invalid in ("fingerprint", "ciphertext"):
            with self.subTest(invalid=invalid), self.rotation_context() as (request, holder, old, new, account):
                if invalid == "fingerprint":
                    holder["row"]["fingerprint"] = exchange_connections.key_fingerprint(new[0])
                else:
                    holder["row"]["encrypted_payload"] = b"invalid-offline-ciphertext"
                asyncio.run(exchange_connections.ensure_session_cache(request))
                self.assertEqual(exchange_connections.session_credentials(request, "LIVE", active_only=False), ("", ""))
                self.assertFalse(exchange_connections.session_public_status(request.app, request)["connections"]["LIVE"]["active"])

    def test_reloaded_new_vault_key_repairs_locked_old_binding(self):
        with self.rotation_context() as (request, holder, old, new, account):
            exchange_connections._lock_runtime(request.app, "LIVE", request)
            holder["row"].update(
                encrypted_payload=exchange_connections.encrypt_credentials(*new, mode="LIVE"),
                fingerprint=exchange_connections.key_fingerprint(new[0]),
            )
            asyncio.run(exchange_connections.ensure_session_cache(request))
            v25_execution.guard_execution_ownership(request)
            self.assertEqual(v25_execution.live_credentials_status(request)[:2], new)
            self.assertTrue(request.app.state.v25_execution["real_trading_locked"])

    def test_disconnect_delete_save_reconnect_uses_only_new_key(self):
        with self.rotation_context() as (request, holder, old, new, account):
            async def scenario():
                await exchange_connections.exchange_connection_deactivate(
                    request, exchange_connections.ConnectionActionRequest(mode="LIVE", confirmation="BAĞLANTIYI KAPAT"),
                )
                self.assertEqual(v25_execution.live_credentials_status(request), ("", "", None))
                await exchange_connections.exchange_connection_delete(
                    request, exchange_connections.ConnectionActionRequest(mode="LIVE", confirmation="ANAHTARI KALICI SİL"),
                )
                await self.save_rotated_key(request, new)
                with patch.object(v25_execution, "connect_read_only_for_request", new=AsyncMock(return_value={"orders_created": False})) as connect:
                    await exchange_connections.exchange_connection_activate(
                        request, exchange_connections.ConnectionActionRequest(mode="LIVE", confirmation="CANLI SALT OKUNUR BAĞLANTIYI AÇ"),
                    )
                    connect.assert_awaited_once()
                self.assertEqual(v25_execution.live_credentials_status(request)[:2], new)
            asyncio.run(scenario())

    def test_unknown_account_rotation_with_open_position_is_rejected(self):
        with self.rotation_context() as (request, holder, old, new, account):
            account["account_identity"] = ""
            state = request.app.state.v25_execution
            state["snapshot"]["positions"] = [{"symbol": "BTCUSDT", "quantity": 1}]
            with self.assertRaises(exchange_connections.HTTPException) as failure:
                asyncio.run(self.save_rotated_key(request, new))
            self.assertEqual(failure.exception.status_code, 409)
            self.assertEqual(exchange_connections.decrypt_credentials(holder["row"]["encrypted_payload"], mode="LIVE"), old)
            self.assertTrue(state["real_trading_locked"])

    def test_invalid_key_pair_keeps_old_vault_record_and_entries_locked(self):
        for invalid in (("short", "valid-offline-secret"), ("valid-offline-api", "")):
            with self.subTest(invalid=invalid), self.rotation_context() as (request, holder, old, new, account):
                with self.assertRaises(exchange_connections.HTTPException) as failure:
                    asyncio.run(self.save_rotated_key(request, invalid))
                self.assertEqual(failure.exception.status_code, 422)
                self.assertEqual(exchange_connections.decrypt_credentials(holder["row"]["encrypted_payload"], mode="LIVE"), old)
                self.assertEqual(exchange_connections.session_credentials(request, "LIVE", active_only=False), old)
                self.assertTrue(request.app.state.v25_execution["real_trading_locked"])

    def test_vault_read_error_evicts_old_key_and_never_logs_secrets(self):
        with self.rotation_context() as (request, holder, old, new, account):
            request.app.state.db_pool.fetchrow.side_effect = RuntimeError(f"{old[0]} {old[1]} 654321 signature=offline-secret")
            with self.assertLogs(exchange_connections.logger, level="ERROR") as logs:
                with self.assertRaises(exchange_connections.HTTPException) as failure:
                    asyncio.run(exchange_connections.ensure_session_cache(request))
            self.assertEqual(failure.exception.status_code, 503)
            for value in (*old, "654321", "signature=offline-secret"):
                self.assertNotIn(value, str(logs.output) + str(failure.exception.detail))
            self.assertEqual(v25_execution.live_credentials_status(request), ("", "", None))
            self.assertTrue(request.app.state.v25_execution["real_trading_locked"])

    def test_save_and_status_reload_serialize_the_same_credential_record(self):
        with self.rotation_context() as (request, holder, old, new, account):
            async def scenario():
                entered, release = asyncio.Event(), asyncio.Event()

                async def verify(*args):
                    entered.set()
                    await release.wait()
                    return account

                with patch.object(exchange_connections, "_verify_request_credentials", new=AsyncMock(side_effect=verify)):
                    save = asyncio.create_task(self.save_rotated_key(request, new))
                    await entered.wait()
                    reload = asyncio.create_task(exchange_connections.ensure_session_cache(request))
                    await asyncio.sleep(0)
                    self.assertFalse(reload.done())
                    release.set()
                    await asyncio.gather(save, reload)
                v25_execution.guard_execution_ownership(request)
                self.assertEqual(v25_execution.live_credentials_status(request)[:2], new)
            asyncio.run(scenario())

    def test_rotated_key_connects_fresh_readonly_snapshot_and_guard_still_rejects_mismatch(self):
        with self.rotation_context() as (request, holder, old, new, account):
            snapshot = {
                "account_identity": "account-one", "wallet_balance": 250,
                "available_balance": 250, "positions": [], "open_orders": [],
                "open_algo_orders": [], "open_algo_orders_available": True,
            }

            def client_for(application, current_request):
                self.assertEqual(v25_execution.live_credentials_status(current_request)[:2], new)
                return SimpleNamespace(time_offset_ms=0)

            async def scenario():
                await self.save_rotated_key(request, new)
                with patch.object(v25_execution, "client_for", side_effect=client_for), \
                     patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=snapshot)) as read, \
                     patch.object(v25_execution, "submit_entry", new=AsyncMock()) as order:
                    result = await exchange_connections.exchange_connection_activate(
                        request, exchange_connections.ConnectionActionRequest(mode="LIVE", confirmation="CANLI SALT OKUNUR BAĞLANTIYI AÇ"),
                    )
                    self.assertTrue(result["connected"])
                    self.assertEqual(result["account"]["wallet_balance"], 250)
                    self.assertTrue(result["real_trading_locked"])
                    read.assert_awaited_once()
                    order.assert_not_awaited()
                exchange_connections._SESSION_CACHE[("owner:owner", "LIVE")] = old
                with self.assertRaises(exchange_connections.HTTPException) as failure:
                    v25_execution.guard_execution_ownership(request)
                self.assertEqual(failure.exception.status_code, 403)
            asyncio.run(scenario())

    def test_delayed_activation_cannot_publish_proof_for_a_replaced_secret(self):
        with self.rotation_context() as (request, holder, old, new, account):
            replacement = (old[0], new[1])
            ciphertext = exchange_connections.encrypt_credentials(*replacement, mode="LIVE")

            async def verify(*args):
                holder["row"]["encrypted_payload"] = ciphertext
                return account

            with patch.object(exchange_connections, "_verify_request_credentials", new=AsyncMock(side_effect=verify)), \
                 patch.object(v25_execution, "connect_read_only_for_request", new=AsyncMock()) as connect:
                with self.assertRaises(exchange_connections.HTTPException) as failure:
                    asyncio.run(exchange_connections.exchange_connection_activate(
                        request, exchange_connections.ConnectionActionRequest(mode="LIVE", confirmation="CANLI SALT OKUNUR BAĞLANTIYI AÇ"),
                    ))
            self.assertEqual(failure.exception.status_code, 409)
            connect.assert_not_awaited()
            self.assertEqual(exchange_connections.decrypt_credentials(holder["row"]["encrypted_payload"], mode="LIVE"), replacement)
            self.assertTrue(request.app.state.v25_execution["real_trading_locked"])

    def test_live_credentials_are_shared_between_owner_sessions(self):
        first = self.request("session-a")
        second = self.request("session-b")
        owner_key = ("owner:owner", "LIVE")

        with patch.dict(exchange_connections._CACHE, {}, clear=True), \
             patch.dict(exchange_connections._META, {}, clear=True), \
             patch.dict(exchange_connections._SESSION_CACHE, {owner_key: ("api-key-a", "secret-key-a")}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {owner_key: self.live_metadata()}, clear=True):
            self.assertEqual(
                v25_execution.live_credentials_status(first),
                ("api-key-a", "secret-key-a", v25_execution.credential_fingerprint("api-key-a")),
            )
            self.assertEqual(
                v25_execution.live_credentials_status(second),
                ("api-key-a", "secret-key-a", v25_execution.credential_fingerprint("api-key-a")),
            )

    def test_live_owner_credentials_reload_from_account_vault_after_cache_clear(self):
        first = self.request("session-a")
        second = self.request("session-b", first.app)
        first.app.state.db_pool = SimpleNamespace(
            fetch=AsyncMock(return_value=[]),
            fetchrow=AsyncMock(return_value={
                "user_id": "owner", "mode": "LIVE", "encrypted_payload": b"ciphertext",
                "fingerprint": v25_execution.credential_fingerprint("api-key-owner"),
                "active": True, "last_test_ok": True, "last_test_at": None,
                "last_error": None, "account_summary": self.live_metadata()["account"],
                "updated_at": None,
            }),
        )
        first.app.state.exchange_vault = {"ready": True, "pool_id": id(first.app.state.db_pool)}
        with patch.dict(exchange_connections._SESSION_CACHE, {}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {}, clear=True), \
             patch.object(exchange_connections, "ensure_schema", new=AsyncMock()), \
             patch.object(exchange_connections, "decrypt_credentials", return_value=("api-key-owner", "secret-key-owner")):
            asyncio.run(exchange_connections.ensure_session_cache(first))
            self.assertEqual(
                exchange_connections.session_credentials_for_request(second, "LIVE", active_only=False),
                ("api-key-owner", "secret-key-owner"),
            )
        first.app.state.db_pool.fetchrow.assert_awaited_once()

    def test_live_connection_scope_requires_owner_two_factor(self):
        from app import account_settings

        request = self.request("mfa-session")
        user = request.state.member
        with patch.object(account_settings, "enabled", new=AsyncMock(return_value=False)):
            with self.assertRaises(exchange_connections.HTTPException) as disabled:
                asyncio.run(exchange_connections._require_live_mfa(request, user, None))
        self.assertEqual(disabled.exception.status_code, 403)

        with patch.object(account_settings, "enabled", new=AsyncMock(return_value=True)), \
             patch.object(account_settings, "require_totp", new=AsyncMock()) as verify:
            asyncio.run(exchange_connections._require_live_mfa(request, user, "123456"))
        verify.assert_awaited_once_with(request, user, "123456", required=True)

        with self.assertRaises(exchange_connections.HTTPException) as non_owner:
            asyncio.run(exchange_connections._require_live_mfa(
                request, {"id": "member", "role": "CUSTOMER"}, "123456",
            ))
        self.assertEqual(non_owner.exception.status_code, 403)

    def test_logout_removes_only_browser_scoped_testnet_credentials(self):
        request = self.request("logout-session")
        request.app.state.db_pool.execute = AsyncMock()
        owner_key = ("owner:owner", "LIVE")
        live_state = request.app.state.v25_execution
        live_state["auto"]["enabled"] = True
        live_state["live_auto_trade"] = True
        with patch.dict(exchange_connections._SESSION_CACHE, {owner_key: ("api-key-a", "secret-key-a")}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {owner_key: self.live_metadata()}, clear=True):
            asyncio.run(exchange_connections.clear_session_vault_for_request(request))
            self.assertEqual(
                exchange_connections.session_credentials_for_request(request, "LIVE", active_only=False),
                ("api-key-a", "secret-key-a"),
            )
        delete_query = request.app.state.db_pool.execute.await_args.args[0]
        self.assertIn("protrebot_exchange_session_vault", delete_query)
        self.assertIn("mode = 'TESTNET'", delete_query)
        self.assertTrue(live_state["auto"]["enabled"])
        self.assertTrue(live_state["live_auto_trade"])

    def test_background_grant_outlives_browser_token_but_not_auth_version(self):
        state = v25_execution.initial_state()
        state["auto_authorization"] = {
            "user_id": "owner", "session_id": "owner:owner", "fingerprint": "fingerprint",
            "auth_version": 1, "expires_at_epoch": time.time() + 3600,
            "auth_expires_at_epoch": time.time() - 3600,
        }
        application = SimpleNamespace(state=SimpleNamespace(
            v25_execution=state, db_pool=object(),
        ))
        with patch.object(exchange_connections, "authoritative_user_version_active", new=AsyncMock(return_value=True)) as validate:
            self.assertTrue(asyncio.run(v25_execution.background_authorization_active(application, state)))
        validate.assert_awaited_once_with(application, "owner", 1)

        with patch.object(exchange_connections, "authoritative_user_version_active", new=AsyncMock(return_value=False)):
            self.assertFalse(asyncio.run(v25_execution.background_authorization_active(application, state)))
        self.assertTrue(state["real_trading_locked"])

    def test_missing_session_credential_is_unconfigured(self):
        request = self.request("session-missing")
        with patch.dict(exchange_connections._CACHE, {}, clear=True), \
             patch.dict(exchange_connections._META, {}, clear=True), \
             patch.dict(exchange_connections._SESSION_CACHE, {}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {}, clear=True):
            status = v25_execution.public_status(
                request.app,
                request,
            )
        self.assertFalse(status["credentials"]["configured"])
        self.assertEqual(status["credentials"]["storage"], "YOK")

    def test_status_exposes_snapshot_account_fields_without_private_material(self):
        state = v25_execution.initial_state()
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, v22_commercial={"authenticated": True}))
        request = self.request("session-a", application, bind=True)
        application.state.v25_execution["snapshot"] = {
            "wallet_balance": 1000.0,
            "available_balance": 800.0,
            "unrealized_pnl": 12.5,
            "positions": [{"symbol": "BTCUSDT", "quantity": 0.01, "mark_price": 50000}],
            "open_orders": [{"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": 49000, "quantity": 0.01, "status": "NEW"}],
        }
        owner_key = ("owner:owner", "LIVE")
        with patch.dict(exchange_connections._SESSION_CACHE, {owner_key: ("api-key-a", "secret-key-a")}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {owner_key: self.live_metadata()}, clear=True):
            application.state.v25_execution["snapshot_session_id"] = "owner:owner"
            application.state.v25_execution["live_session_authorization"].update(user_id="owner", session_id="owner:owner")
            application.state.v25_execution["connected"] = True
            status = v25_execution.public_status(application, request)
        self.assertTrue(status["connected"])
        self.assertTrue(status["readiness"]["gates"][2]["passed"])
        self.assertEqual(status["account"]["wallet_balance"], 1000.0)
        self.assertEqual(status["account"]["available_balance"], 800.0)
        self.assertEqual(status["account"]["unrealized_pnl"], 12.5)
        self.assertEqual(status["account"]["positions"][0]["symbol"], "BTCUSDT")
        self.assertEqual(status["account"]["open_orders"][0]["status"], "NEW")
        self.assertNotIn("api_key", status)
        self.assertNotIn("secret_key", status)

    def test_status_shares_snapshot_with_same_owner_across_sessions(self):
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=v25_execution.initial_state()))
        request = self.request("session-b", application, bind=True)
        application.state.v25_execution["connected"] = True
        application.state.v25_execution["snapshot"] = {
            "wallet_balance": 1000.0,
            "available_balance": 800.0,
            "positions": [],
            "open_orders": [],
        }
        application.state.v25_execution["snapshot_session_id"] = "owner:owner"
        application.state.v25_execution["live_session_authorization"].update(user_id="owner", session_id="owner:owner")
        status = v25_execution.public_status(application, request)
        self.assertTrue(status["connected"])
        self.assertEqual(status["account"]["wallet_balance"], 1000.0)

    def test_read_only_connect_uses_current_session_credentials_and_publishes_snapshot(self):
        state = v25_execution.initial_state()
        state["lock"] = asyncio.Lock()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state, v22_commercial={"authenticated": True}))
        request = self.request("session-a", application, method="POST")
        snapshot = {
            "wallet_balance": 1000.0,
            "available_balance": 800.0,
            "positions": [],
            "open_orders": [],
        }
        client = SimpleNamespace(time_offset_ms=0)
        with patch.dict(exchange_connections._SESSION_CACHE, {("owner:owner", "LIVE"): ("api-key-session-a", "secret-key-session-a")}, clear=True), \
             patch.dict(exchange_connections._SESSION_META, {("owner:owner", "LIVE"): self.live_metadata()}, clear=True), \
             patch.object(v25_execution, "client_for", return_value=client) as client_for, \
             patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value=snapshot)), \
             patch.object(v25_execution, "persist_state"), \
             patch.object(exchange_connections, "_require_live_mfa", new=AsyncMock()):
            status = asyncio.run(v25_execution.v25_connect(
                request, v25_execution.Confirmation(confirmation="LIVE HESABINI DOĞRULA", totp_code="123456"),
            ))
        client_for.assert_called_once_with(application, request)
        self.assertTrue(status["connected"])
        self.assertEqual(status["account"]["wallet_balance"], 1000.0)
        account_gate = next(gate for gate in status["readiness"]["gates"] if gate["key"] == "read_only")
        self.assertTrue(account_gate["passed"])

    def test_reconcile_preserves_authenticated_snapshot_binding(self):
        state = v25_execution.initial_state()
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=state))
        request = self.request("session-a", application, bind=True)
        snapshot = {
            "wallet_balance": 1000.0,
            "available_balance": 800.0,
            "positions": [],
            "open_orders": [],
        }
        state["snapshot"] = snapshot
        state["snapshot_session_id"] = "owner:owner"
        state["live_session_authorization"].update(user_id="owner", session_id="owner:owner")
        state["connected"] = True
        client = SimpleNamespace(time_offset_ms=0)
        with patch.object(v25_execution, "client_for", return_value=client), \
             patch.object(v25_execution, "account_snapshot", new=AsyncMock(return_value={**snapshot, "unrealized_pnl": 1.0})), \
             patch.object(v25_execution, "recover_orphan_plans", new=AsyncMock(return_value=0)), \
             patch.object(v25_execution, "persist_state"):
            asyncio.run(v25_execution.reconcile(application))
        self.assertEqual(state["snapshot_session_id"], "owner:owner")
        status = v25_execution.public_status(application, request)
        self.assertTrue(status["connected"])

    def test_snapshot_binding_must_match_its_account_authorization(self):
        application = SimpleNamespace(state=SimpleNamespace(v25_execution=v25_execution.initial_state()))
        state = application.state.v25_execution
        request = self.request("session-b", application, bind=True)
        state["connected"] = True
        state["snapshot"] = {
            "wallet_balance": 1000.0,
            "available_balance": 800.0,
            "positions": [],
            "open_orders": [],
        }
        state["live_session_authorization"].update(user_id="owner", session_id="owner:owner")
        state["snapshot_session_id"] = exchange_connections.session_id(self.request("session-a"))
        self.assertFalse(v25_execution.public_status(application, request)["connected"])
        foreign = self.request("foreign-session", application)
        foreign.state.member = {"id": "another-owner", "role": "OWNER", "auth_version": 1}
        with self.assertRaises(v25_execution.HTTPException) as denied:
            v25_execution.public_status(application, foreign)
        self.assertEqual(denied.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()