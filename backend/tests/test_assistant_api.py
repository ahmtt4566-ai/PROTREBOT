import asyncio
import logging
import os
import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
from app import assistant_api, main
from app.assistant_api import (
    AssistantService,
    ChatInput,
    ModelAnswer,
    estimated_input_tokens,
    router,
)
from app.assistant_config import AssistantConfig
from app.assistant_llm import AnthropicTransport, build_request
from app.assistant_storage import AssistantStorageError, AssistantStore, month_key
from app.commercial_core import default_commercial_state, issue_token
from fastapi import FastAPI


class AssistantApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.application = FastAPI()
        self.application.middleware("http")(main.owner_preview_gate)
        self.application.include_router(router)
        self.application.state.db_pool = None
        state = default_commercial_state()
        state["users"] = [
            {"id": user, "role": "CUSTOMER", "active": True, "email_verified": True}
            for user in ("a", "b", "premium")
        ]
        state["subscriptions"] = [{
            "user_id": "premium", "plan": "MASTER_MODE", "status": "ACTIVE",
            "current_period_end": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }]
        signing_key = b"assistant-tests-only-signing-key"
        self.application.state.v22_commercial = {"state": state, "secret": signing_key, "lock": asyncio.Lock()}
        self.tokens = {user: issue_token(user, "CUSTOMER", signing_key, kind="USER") for user in ("a", "b", "premium")}
        self.now = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).timestamp()
        self.config = AssistantConfig(ANTHROPIC_API_KEY="assistant-api-test-key-not-real")
        self.provider = AsyncMock(return_value=ModelAnswer("Read-only help.", 100, 20))
        self.path = Path(self.directory.name) / "assistant.sqlite3"
        self.store = AssistantStore(self.application, path=self.path)
        self.assistant = AssistantService(self.store, provider=self.provider, token_counter=AsyncMock(return_value=100), clock=lambda: self.now)
        self.application.state.assistant_service = self.assistant
        for context in (
            patch.object(main, "WEB_REQUIRE_AUTH", False),
            patch.object(main, "hydrate_authenticated_user_state", AsyncMock()),
            patch.dict(os.environ, {"PROTREBOT_DURABLE_AUTH_REQUIRED": "false"}),
            patch.object(assistant_api, "load_assistant_config", lambda: self.config),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.application), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.assistant.close)

    def headers(self, user="a"):
        return {"Authorization": "Bearer " + self.tokens[user]}

    async def chat(self, user="a", **body):
        return await self.client.post("/api/assistant/chat", headers=self.headers(user), json={"message": "How can you help?", **body})

    async def usage(self, user="a"):
        return await self.client.get("/api/assistant/usage", headers=self.headers(user))

    async def ledger(self):
        async with self.store.connection(self.config.request_timeout_seconds) as transaction:
            return await transaction.row("SELECT * FROM assistant_monthly_spend WHERE month_key=$1", month_key(self.now))

    async def call_record(self):
        async with self.store.connection(self.config.request_timeout_seconds) as transaction:
            return await transaction.row("SELECT * FROM assistant_calls ORDER BY called_at DESC LIMIT 1")

    def restart(self):
        self.store = AssistantStore(self.application, path=self.path)
        self.assistant = AssistantService(self.store, provider=self.provider, token_counter=AsyncMock(return_value=100), clock=lambda: self.now)
        self.application.state.assistant_service = self.assistant
        self.addAsyncCleanup(self.assistant.close)

    async def test_membership_required_and_owner_preview_is_not_authentication(self):
        self.assertEqual((await self.client.get("/api/assistant/usage")).status_code, 401)
        self.assertEqual((await self.client.post("/api/assistant/chat", json={"message": "help"})).status_code, 401)
        self.assertEqual((await self.client.get("/api/assistant/usage", headers={"Authorization": "Bearer invalid"})).status_code, 401)
        with patch.object(main, "WEB_REQUIRE_AUTH", True), patch.object(main, "WEB_ACCESS_TOKEN", "owner-preview-test-token-at-least-24"):
            response = await self.client.get("/api/assistant/usage", headers={"X-Protrebot-Owner": "owner-preview-test-token-at-least-24"})
            self.assertEqual(response.status_code, 401)
        self.provider.assert_not_awaited()

    def test_production_registration_and_not_public(self):
        paths = main.app.openapi()["paths"]
        self.assertIn("/api/assistant/chat", paths)
        self.assertIn("/api/assistant/usage", paths)
        self.assertNotIn("/api/assistant/chat", main.MEMBER_PUBLIC_PATHS)
        self.assertNotIn("/api/assistant/usage", main.MEMBER_PUBLIC_PATHS)

    async def test_identity_is_token_only_and_usage_survives_free_member_redaction(self):
        response = await self.client.post(
            "/api/assistant/chat?user_id=b",
            headers=self.headers(),
            json={"message": "Hello", "user_id": "b", "history": [{"role": "user", "content": "Help", "user_id": "b"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"reply": "Read-only help.", "language": "en", "sources": []})
        for user, spent in (("a", 1), ("b", 0), ("premium", 0)):
            usage = await self.usage(user)
            self.assertEqual(usage.status_code, 200)
            self.assertEqual(usage.json()["remaining"], self.config.daily_limit - spent)
            self.assertEqual(usage.json()["total"], self.config.daily_limit)
            self.assertIn("resetsAt", usage.json())
            self.assertEqual(usage.json()["limits"], {
                "max_input_chars": self.config.max_input_chars,
                "history_messages": self.config.history_messages,
                "history_message_max_chars": self.config.history_message_max_chars,
                "page_context_max_chars": self.config.page_context_max_chars,
                "secret_min_alphanumeric_chars": self.config.secret_min_alphanumeric_chars,
            })
        self.assertEqual((await self.call_record())["user_id"], "a")

    async def test_invalid_inputs_rejected_without_quota(self):
        bodies = [
            {"message": ""}, {"message": "   "}, {"message": 42},
            {"message": "x" * (self.config.max_input_chars + 1)},
            {"message": "hello", "page_context": "x" * (self.config.page_context_max_chars + 1)},
            {"message": "hello", "history": [{"role": "system", "content": "test"}]},
            {"message": "hello", "history": [{"role": "user", "content": " "}]},
            {"message": "hello", "history": [{"role": "user", "content": "test"}] * (self.config.history_messages + 1)},
        ]
        for body in bodies:
            with self.subTest(body=body):
                response = await self.client.post("/api/assistant/chat", json=body, headers=self.headers())
                self.assertEqual(response.status_code, 422)
        response = await self.client.post("/api/assistant/chat", content="{", headers=self.headers())
        self.assertEqual(response.status_code, 422)
        self.provider.assert_not_awaited()
        self.assertEqual((await self.usage()).json()["remaining"], self.config.daily_limit)

    async def test_exact_boundaries_and_long_history_truncation(self):
        history_content = "previous message " * self.config.history_message_max_chars
        response = await self.chat(
            message=("a " * self.config.max_input_chars)[:self.config.max_input_chars - 1] + "a",
            page_context=("p " * self.config.page_context_max_chars)[:self.config.page_context_max_chars - 1] + "p",
            history=[{"role": "user", "content": history_content}] * self.config.history_messages,
        )
        self.assertEqual(response.status_code, 200)
        payload = self.provider.call_args.args[2]
        self.assertEqual(len(payload.history), self.config.history_messages)
        self.assertEqual(payload.history[0].content, history_content[:self.config.history_message_max_chars])
        self.assertEqual(len(payload.message), self.config.max_input_chars)
        self.assertEqual(len(payload.page_context), self.config.page_context_max_chars)

    async def test_secrets_in_message_history_and_context_never_reach_provider(self):
        secrets = ["secret", "A" * self.config.secret_min_alphanumeric_chars, "sk-ant-test-only", "Bearer test-token", "password=test-value", "api_key=test-value"]
        with self.assertNoLogs(assistant_api.logger, logging.INFO):
            for value in secrets:
                for field in ("message", "history", "page_context"):
                    body = {field: [{"role": "assistant", "content": value}] if field == "history" else value}
                    response = await self.chat(**body)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(response.json()["language"], ("tr", "en"))
                    self.assertIn("secret", response.json()["reply"].casefold())
            response = await self.chat(history=[{
                "role": "user", "content": ("old message " * self.config.history_message_max_chars) + " secret",
            }])
            self.assertIn("secret", response.json()["reply"].casefold())
        self.provider.assert_not_awaited()
        self.assertEqual((await self.usage()).json()["remaining"], self.config.daily_limit)

    async def test_minute_limit_localized_and_reset(self):
        for _ in range(self.config.per_minute_limit):
            self.assertEqual((await self.chat()).status_code, 200)
        for message, language, fragment in (("Hello", "en", "60 seconds"), ("Merhaba", "tr", "60 saniye")):
            response = await self.chat(message=message)
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers["retry-after"], "60")
            self.assertEqual(response.json()["language"], language)
            self.assertIn(fragment, response.json()["reply"])
        self.assertEqual((await self.chat("b")).status_code, 200)
        self.now += timedelta(minutes=1).total_seconds()
        self.assertEqual((await self.chat()).status_code, 200)
        self.assertEqual(self.provider.await_count, self.config.per_minute_limit + 2)

    async def test_utc_calendar_day_limit_and_reset_not_rolling_window(self):
        for _ in range(self.config.daily_limit):
            self.assertEqual((await self.chat()).status_code, 200)
            self.now += timedelta(minutes=1).total_seconds()
        response = await self.chat()
        self.assertEqual(response.status_code, 429)
        self.assertIn("daily", response.json()["reply"])
        self.assertGreater(int(response.headers["retry-after"]), 0)
        self.assertEqual((await self.usage()).json()["remaining"], 0)
        self.now = datetime(2026, 1, 16, tzinfo=timezone.utc).timestamp()
        self.assertEqual((await self.usage()).json()["remaining"], self.config.daily_limit)
        self.assertEqual((await self.chat()).status_code, 200)
        self.assertEqual((await self.usage()).json()["remaining"], self.config.daily_limit - 1)

    async def test_premium_has_same_assistant_limits_and_no_analyst_or_trading_actions(self):
        self.config = self.config.model_copy(update={"per_minute_limit": 1})
        self.assertEqual((await self.chat("premium", message="Please ARM and execute a trade")).status_code, 200)
        self.assertEqual((await self.chat("premium")).status_code, 429)
        self.assertFalse(hasattr(self.application.state, "analyst_credits"))
        self.assertFalse(hasattr(self.application.state, "v25_execution"))

    async def test_budget_actual_cost_persistence_warning_once_and_next_month(self):
        self.config = self.config.model_copy(update={"monthly_budget_usd": Decimal(5),
                                                     "max_total_input_tokens_per_message": 5_000_000,
                                                     "max_total_output_tokens_per_message": 500_000})
        self.provider.return_value = ModelAnswer("Help", 3_000_000, 200_000)
        with self.assertLogs(assistant_api.logger, logging.INFO) as logs:
            self.assertEqual((await self.chat()).status_code, 200)
            self.provider.return_value = ModelAnswer("Help", 500_000, 0)
            self.assertEqual((await self.chat("b")).status_code, 200)
        self.assertEqual(len([item for item in logs.records if item.levelno == logging.WARNING]), 1)
        self.assertEqual(Decimal((await self.ledger())["spent_usd"]), Decimal("4.5"))
        self.assertEqual((await self.call_record())["estimated"], 0)
        self.restart()
        self.assertEqual((await self.usage()).json()["remaining"], self.config.daily_limit - 1)
        self.assertEqual((await self.chat()).status_code, 200)
        self.assertEqual(Decimal((await self.ledger())["spent_usd"]), Decimal(5))
        before = self.provider.await_count
        self.assertEqual((await self.chat("premium")).status_code, 503)
        self.assertEqual(self.provider.await_count, before)
        self.assertEqual((await self.usage("premium")).json()["remaining"], self.config.daily_limit)
        self.now = datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()
        self.assertEqual((await self.chat()).status_code, 200)
        self.assertEqual(Decimal((await self.ledger())["spent_usd"]), Decimal("0.5"))

    async def test_zero_budget_blocks_without_counting(self):
        self.config = self.config.model_copy(update={"monthly_budget_usd": Decimal(0)})
        response = await self.chat(message="Merhaba")
        self.assertEqual(response.status_code, 503)
        self.assertIn("yoğunuz", response.json()["reply"])
        self.provider.assert_not_awaited()
        self.assertEqual((await self.usage()).json()["remaining"], self.config.daily_limit)

    async def test_provider_timeout_counts_and_estimates_cost_without_disabling(self):
        self.provider.side_effect = anthropic.APIConnectionError(request=httpx.Request("POST", "https://provider.test"))
        payload = ChatInput(message="How can you help?")
        expected = (Decimal(estimated_input_tokens("en", payload)) * self.config.cache_write_price_usd_per_million
                    + self.config.max_output_tokens * self.config.output_price_usd_per_million) / assistant_api.TOKENS_PER_MILLION
        with self.assertLogs(assistant_api.logger, logging.WARNING) as logs:
            response = await self.chat()
        self.assertEqual(response.status_code, 502)
        self.assertTrue(any("estimated=True" in item for item in logs.output))
        record = await self.call_record()
        self.assertEqual(record["estimated"], 1)
        self.assertEqual(record["input_tokens"], estimated_input_tokens("en", payload))
        self.assertEqual(record["output_tokens"], self.config.max_output_tokens)
        self.assertEqual(Decimal(record["cost_usd"]), expected)
        self.assertEqual(Decimal((await self.ledger())["spent_usd"]), expected)
        self.assertEqual((await self.usage()).json()["remaining"], self.config.daily_limit - 1)
        self.provider.side_effect = None
        self.assertEqual((await self.chat()).status_code, 200)
        self.assertEqual((await self.ledger())["accounting_blocked"], 0)

    async def test_missing_or_invalid_usage_is_estimated_and_still_enforces_budget(self):
        for answer in (ModelAnswer("Help", None, None), ModelAnswer("Help", -1, 2)):
            self.provider.return_value = answer
            with self.assertLogs(assistant_api.logger, logging.WARNING):
                self.assertEqual((await self.chat()).status_code, 200)
            self.assertEqual((await self.call_record())["estimated"], 1)
        self.config = self.config.model_copy(update={"monthly_budget_usd": Decimal((await self.ledger())["spent_usd"])})
        self.assertEqual((await self.chat()).status_code, 503)
        self.assertEqual(self.provider.await_count, 2)

    async def test_empty_reply_still_accounts_actual_tokens(self):
        self.provider.return_value = ModelAnswer(" ", 100, 20)
        self.assertEqual((await self.chat()).status_code, 502)
        self.assertEqual(Decimal((await self.ledger())["spent_usd"]), Decimal("0.0002"))
        self.assertEqual((await self.call_record())["estimated"], 0)
        self.assertEqual((await self.usage()).json()["remaining"], self.config.daily_limit - 1)

    async def test_missing_key_disabled_and_invalid_config_are_safe(self):
        for config in (
            AssistantConfig(ANTHROPIC_API_KEY=""),
            self.config.model_copy(update={"enabled": False}),
        ):
            self.config = config
            self.assertEqual((await self.chat()).status_code, 503)
        self.provider.assert_not_awaited()
        with patch.object(assistant_api, "load_assistant_config", AssistantConfig), patch.dict(os.environ, {"ASSISTANT_DAILY_LIMIT": "invalid"}), self.assertLogs(assistant_api.logger, logging.WARNING):
            self.assertEqual((await self.chat()).status_code, 503)
            self.assertEqual((await self.usage()).status_code, 503)

    async def test_concurrent_requests_cannot_bypass_quota_or_budget(self):
        responses = await asyncio.gather(*(self.chat() for _ in range(self.config.per_minute_limit + 2)))
        self.assertEqual(sum(item.status_code == 200 for item in responses), self.config.per_minute_limit)
        self.assertEqual(sum(item.status_code == 429 for item in responses), 2)
        self.assertEqual(self.provider.await_count, self.config.per_minute_limit)
        self.now += timedelta(minutes=1).total_seconds()
        spent = Decimal((await self.ledger())["spent_usd"])
        self.config = self.config.model_copy(update={"monthly_budget_usd": spent + Decimal("0.0002")})
        before = self.provider.await_count
        responses = await asyncio.gather(self.chat("a"), self.chat("b"), self.chat("premium"))
        self.assertEqual(sum(item.status_code == 200 for item in responses), 1)
        self.assertEqual(sum(item.status_code == 503 for item in responses), 2)
        self.assertEqual(self.provider.await_count, before + 1)

    async def test_dispatch_time_after_lock_wait_controls_minute_and_day(self):
        await self.chat()
        original = self.store.monthly_transaction

        @asynccontextmanager
        async def delayed(month, timeout):
            async with original(month, timeout) as transaction:
                self.now = datetime(2026, 1, 16, tzinfo=timezone.utc).timestamp()
                yield transaction

        with patch.object(self.store, "monthly_transaction", delayed):
            self.assertEqual((await self.chat()).status_code, 200)
        self.assertEqual((await self.usage()).json()["remaining"], self.config.daily_limit - 1)

    async def test_month_changes_while_waiting_do_not_charge_previous_month(self):
        original = self.store.monthly_transaction

        @asynccontextmanager
        async def delayed(month, timeout):
            async with original(month, timeout) as transaction:
                self.now = datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()
                yield transaction

        with patch.object(self.store, "monthly_transaction", delayed):
            self.assertEqual((await self.chat()).status_code, 503)
        self.provider.assert_not_awaited()
        self.assertEqual((await self.chat()).status_code, 200)

    async def test_disconnect_does_not_cancel_accounting(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def slow_provider(config, language, payload):
            started.set()
            await finish.wait()
            return ModelAnswer("Help", 100, 20)

        self.assistant.provider = slow_provider
        caller = asyncio.create_task(self.assistant.start("a", "en", ChatInput(message="Hello"), self.config))
        await started.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        finish.set()
        await self.assistant.close()
        self.assertEqual((await self.usage()).json()["remaining"], self.config.daily_limit - 1)
        self.assertEqual(Decimal((await self.ledger())["spent_usd"]), Decimal("0.0002"))

    async def test_storage_failure_after_call_fails_closed_across_restart(self):
        original = self.store.monthly_transaction

        @asynccontextmanager
        async def broken(month, timeout):
            async with original(month, timeout) as transaction:
                yield transaction
                if self.provider.await_count:
                    raise sqlite3.OperationalError("simulated commit failure")

        with patch.object(self.store, "monthly_transaction", broken), self.assertLogs(assistant_api.logger, logging.WARNING):
            self.assertEqual((await self.chat()).status_code, 503)
        self.assertEqual((await self.ledger())["accounting_blocked"], 1)
        self.restart()
        self.assertEqual((await self.chat()).status_code, 503)
        self.assertEqual(self.provider.await_count, 1)

    async def test_database_accounting_block_falls_back_to_persistent_marker(self):
        with patch.object(self.store, "connection", MagicMock(side_effect=sqlite3.OperationalError("unavailable"))), self.assertRaises(sqlite3.OperationalError):
            await self.store.block_accounting(month_key(self.now), self.config.request_timeout_seconds)
        self.assertTrue(self.store.block_path(month_key(self.now)).exists())
        self.restart()
        self.assertEqual((await self.chat()).status_code, 503)
        self.provider.assert_not_awaited()

    async def test_total_provider_timeout_is_estimated_and_counts_attempt(self):
        self.config = self.config.model_copy(update={"request_timeout_seconds": 1})
        async def waiting(config, language, payload):
            await asyncio.Event().wait()

        self.assistant.provider = waiting
        with self.assertLogs(assistant_api.logger, logging.WARNING):
            response = await self.chat()
        self.assertEqual(response.status_code, 502)
        self.assertEqual((await self.call_record())["estimated"], 1)
        self.assertEqual((await self.usage()).json()["remaining"], self.config.daily_limit - 1)

    async def test_final_call_can_cross_budget_then_following_calls_stop(self):
        self.config = self.config.model_copy(update={"monthly_budget_usd": Decimal("0.0001")})
        with self.assertLogs(assistant_api.logger, logging.WARNING):
            self.assertEqual((await self.chat()).status_code, 200)
        self.assertEqual(Decimal((await self.ledger())["spent_usd"]), Decimal("0.0002"))
        self.assertEqual((await self.chat()).status_code, 503)
        self.assertEqual(self.provider.await_count, 1)

    async def test_storage_read_failure_is_not_success_shaped(self):
        with patch.object(self.store, "usage", AsyncMock(side_effect=sqlite3.OperationalError("failed"))), self.assertLogs(assistant_api.logger, logging.WARNING):
            self.assertEqual((await self.usage()).status_code, 503)

    async def test_metadata_audit_contains_no_message_key_history_or_context(self):
        message, history, context = "Hello uniquely-private-message", "private history", "private context"
        with self.assertLogs(assistant_api.logger, logging.INFO) as logs:
            await self.chat(message=message, history=[{"role": "user", "content": history}], page_context=context)
        combined = "\n".join(logs.output)
        for value in (message, history, context, self.config.api_key.get_secret_value(), self.tokens["a"]):
            self.assertNotIn(value, combined)
        for field in ("user_id=a", "input_tokens=100", "output_tokens=20", "cost_usd=0.0002", "sources=[]"):
            self.assertIn(field, combined)
        record = await self.call_record()
        self.assertEqual(set(record), {"id", "user_id", "month_key", "called_at", "input_tokens", "output_tokens", "cost_usd", "estimated",
                                      "cache_creation_input_tokens", "cache_read_input_tokens"})

    async def test_sdk_uses_configured_model_limits_cached_tools_and_suppresses_debug_content(self):
        sdk = MagicMock()
        sdk.__aenter__ = AsyncMock(return_value=sdk)
        sdk.__aexit__ = AsyncMock(return_value=False)

        async def create(**kwargs):
            for name in ("anthropic._base_client", "httpx2", "httpcore2.http11"):
                logging.getLogger(name).debug("unsafe SDK detail %s %s", kwargs["messages"], self.config.api_key.get_secret_value())
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="Help")], usage=SimpleNamespace(input_tokens=100, output_tokens=20))

        sdk.messages.create = AsyncMock(side_effect=create)
        self.config = self.config.model_copy(update={"model": "test-replacement-model", "max_output_tokens": 77})
        with patch.object(anthropic, "AsyncAnthropic", return_value=sdk) as constructor, self.assertLogs(level=logging.DEBUG) as logs:
            answer = await AnthropicTransport(self.config).generate(build_request(self.config, "en", ChatInput(message="Hello private message")))
            logging.getLogger("assistant-test").debug("capture remains active")
        combined = "\n".join(logs.output)
        self.assertNotIn("unsafe SDK detail", combined)
        self.assertNotIn("Hello private message", combined)
        self.assertNotIn(self.config.api_key.get_secret_value(), combined)
        self.assertEqual(answer.input_tokens, 100)
        arguments = sdk.messages.create.call_args.kwargs
        self.assertEqual(arguments["model"], self.config.model)
        self.assertEqual(arguments["max_tokens"], self.config.max_output_tokens)
        self.assertEqual(len(arguments["tools"]), 7)
        self.assertIn("read-only", arguments["system"][0]["text"])
        self.assertEqual(arguments["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(arguments["tools"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 0)
        self.assertEqual(constructor.call_args.kwargs["timeout"], self.config.request_timeout_seconds)
        sdk.messages.create.return_value = None
        sdk.messages.create.side_effect = AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Help")], usage=None,
        ))
        with patch.object(anthropic, "AsyncAnthropic", return_value=sdk):
            answer = await AnthropicTransport(self.config).generate(build_request(self.config, "en", ChatInput(message="Hello")))
        self.assertIsNone(answer.input_tokens)

    async def test_postgres_transaction_takes_month_row_lock(self):
        connection = SimpleNamespace(
            execute=AsyncMock(), fetchrow=AsyncMock(return_value={"month_key": "2026-01"}),
            transaction=MagicMock(return_value=AsyncMock()),
        )
        acquire = AsyncMock()
        acquire.__aenter__.return_value = connection
        self.application.state.db_pool = SimpleNamespace(execute=AsyncMock(), acquire=MagicMock(return_value=acquire))
        postgres = AssistantStore(self.application, path=self.path)
        async with postgres.monthly_transaction("2026-01", self.config.request_timeout_seconds) as transaction:
            self.assertTrue(transaction.postgres)
        connection.fetchrow.assert_awaited_once_with(
            "SELECT month_key FROM assistant_monthly_spend WHERE month_key=$1 FOR UPDATE", "2026-01",
        )
        connection.transaction.assert_called_once()
        connection.execute.assert_any_await(
            "SELECT set_config('lock_timeout', $1, true)", f"{self.config.request_timeout_seconds}s",
        )

        async def fetchrow(query, *args):
            if "SELECT * FROM assistant_monthly_spend" in query:
                return {"spent_usd": "0", "warned": 0, "accounting_blocked": 0}
            if "assistant_usage" in query:
                return {"day_key": "2026-01-15", "daily_count": 0, "minute_start": self.now, "minute_count": 0}
            return {"month_key": "2026-01"}

        connection.fetchrow.side_effect = fetchrow
        assistant = AssistantService(postgres, provider=self.provider, clock=lambda: self.now)
        response = await assistant.chat("a", "en", ChatInput(message="Help"), self.config)
        self.assertEqual(response.status_code, 200)
        connection.fetchrow.assert_any_await("SELECT * FROM assistant_usage WHERE user_id=$1 FOR UPDATE", "a")
        self.assertTrue(any(
            "ON CONFLICT (user_id) DO NOTHING" in call.args[0]
            for call in connection.execute.await_args_list
        ))

    async def test_storage_backend_cannot_silently_switch(self):
        await self.usage()
        self.application.state.db_pool = object()
        with self.assertRaises(AssistantStorageError):
            await self.store.usage("a", self.config.request_timeout_seconds)


if __name__ == "__main__":
    unittest.main()
