import asyncio
import json
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
from app import assistant_api
from app.analyst_credits import AnalystCredits, CreditConfig, CreditStore
from app.assistant_api import AssistantService, router
from app.assistant_config import AssistantConfig
from app.assistant_llm import AnthropicTransport, build_request
from app.assistant_models import ChatInput, ModelAnswer, ToolCall
from app.assistant_prompt import SYSTEM_POLICY, system_prompt
from app.assistant_storage import SCHEMA, AssistantStore, month_key
from app.assistant_tools import AssistantTools
from app.commercial_core import default_commercial_state, issue_token
from app.main import owner_preview_gate
from fastapi import FastAPI


class AssistantLlmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = FastAPI()
        self.app.middleware("http")(owner_preview_gate)
        self.app.include_router(router)
        self.app.state.db_pool = None
        self.now = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).timestamp()
        self.config = AssistantConfig(ANTHROPIC_API_KEY="assistant-llm-test-only")
        state = default_commercial_state()
        state["users"] = [{"id": user, "role": "CUSTOMER", "active": True, "email_verified": True}
                          for user in ("a", "b", "premium")]
        state["subscriptions"] = [{
            "user_id": "premium", "plan": "MASTER_MODE", "status": "ACTIVE",
            "current_period_end": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }]
        secret = b"assistant-llm-tests-signing-key"
        self.app.state.v22_commercial = {"state": state, "secret": secret, "lock": asyncio.Lock()}
        self.tokens = {user: issue_token(user, "CUSTOMER", secret, kind="USER") for user in ("a", "b", "premium")}
        self.path = Path(self.directory.name) / "assistant.sqlite3"
        self.store = AssistantStore(self.app, path=self.path)
        self.app.state.assistant_service = AssistantService(self.store, clock=lambda: self.now)
        self.credits = AnalystCredits(CreditStore(self.app, path=Path(self.directory.name) / "credits.sqlite3"), CreditConfig(), clock=lambda: self.now)
        self.app.state.analyst_credits = self.credits
        self.app.state.analyst_analysis = AsyncMock(return_value={
            "symbol": "BTCUSDT", "direction": "LONG", "confidence": 80,
            "final_decision_score": 75, "opportunity_score": 75, "mtf_alignment": 90,
            "updated_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
            "entry": 987654.321, "stop_loss": 123456.789, "tp1": 999888.777,
            "reasons": ["private reason"],
        })
        self.transport = SimpleNamespace(count=AsyncMock(return_value=100), generate=AsyncMock(return_value=ModelAnswer("Helpful answer.", 100, 20)))
        for context in (
            patch("app.main.WEB_REQUIRE_AUTH", False),
            patch("app.main.hydrate_authenticated_user_state", AsyncMock()),
            patch.object(assistant_api, "load_assistant_config", lambda: self.config),
            patch.object(assistant_api, "AnthropicTransport", return_value=self.transport),
            patch.dict("os.environ", {"PROTREBOT_DURABLE_AUTH_REQUIRED": "false"}),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.app.state.assistant_service.close)

    def headers(self, user="a"):
        return {"Authorization": "Bearer " + self.tokens[user]}

    def tool_turn(self, name="get_my_access", arguments=None, identifier="call-1", **usage):
        return ModelAnswer("", usage.get("input_tokens", 100), usage.get("output_tokens", 20),
                           usage.get("cache_creation_input_tokens", 0), usage.get("cache_read_input_tokens", 0),
                           (ToolCall(identifier, name, arguments or {}),))

    async def chat(self, message="Help me understand the platform", user="a", **body):
        return await self.client.post("/api/assistant/chat", headers=self.headers(user), json={"message": message, **body})

    async def row(self, query, *args):
        async with self.store.connection(self.config.request_timeout_seconds) as tx:
            return await tx.row(query, *args)

    async def spent(self):
        row = await self.row("SELECT * FROM assistant_monthly_spend WHERE month_key=$1", month_key(self.now))
        return Decimal(row["spent_usd"])

    async def calls(self):
        return (await self.row("SELECT COUNT(*) AS n FROM assistant_calls"))["n"]

    async def remaining(self, user="a"):
        return (await self.client.get("/api/assistant/usage", headers=self.headers(user))).json()["remaining"]

    async def test_real_sdk_mocked_end_to_end_tool_use_tool_result_and_cache_controls(self):
        sdk = MagicMock()
        sdk.__aenter__ = AsyncMock(return_value=sdk)
        sdk.__aexit__ = AsyncMock(return_value=False)
        sdk.messages.count_tokens = AsyncMock(return_value=SimpleNamespace(input_tokens=100))
        sdk.messages.create = AsyncMock(side_effect=[
            SimpleNamespace(content=[SimpleNamespace(type="tool_use", id="call-1", name="get_my_access", input={})],
                            usage=SimpleNamespace(input_tokens=100, output_tokens=20)),
            SimpleNamespace(content=[SimpleNamespace(type="text", text="See your profile.")],
                            usage=SimpleNamespace(input_tokens=100, output_tokens=20)),
        ])
        with patch.object(assistant_api, "AnthropicTransport", AnthropicTransport), patch.object(anthropic, "AsyncAnthropic", return_value=sdk):
            response = await self.chat()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sources"], ["get_my_access"])
        self.assertEqual(sdk.messages.create.await_count, 2)
        self.assertEqual(sdk.messages.count_tokens.await_count, 2)
        second = sdk.messages.create.call_args_list[1].kwargs
        self.assertEqual(second["model"], self.config.model)
        self.assertEqual(second["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(second["tools"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(second["messages"][-1]["content"][0]["tool_use_id"], "call-1")
        data = json.loads(second["messages"][-1]["content"][0]["content"])
        self.assertFalse(data["data"]["isPremium"])
        for tool in second["tools"]:
            self.assertNotIn("user_id", tool["input_schema"].get("properties", {}))
            self.assertNotIn("confirm", tool["input_schema"].get("properties", {}))
        self.assertEqual(await self.calls(), 2)
        self.assertEqual(await self.remaining(), self.config.daily_limit - 1)

    async def test_three_turns_each_cost_is_committed_before_tool_and_next_call(self):
        self.transport.generate.side_effect = [
            self.tool_turn(), self.tool_turn("get_my_credits", identifier="call-2"),
            ModelAnswer("Your account information is available.", 100, 20),
        ]
        original = AssistantTools.dispatch
        observations = []

        async def dispatch(tools, user_id, name, arguments):
            observations.append((await self.calls(), await self.spent()))
            return await original(tools, user_id, name, arguments)

        with patch.object(AssistantTools, "dispatch", dispatch):
            response = await self.chat()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(observations, [(1, Decimal("0.0002")), (2, Decimal("0.0004"))])
        self.assertEqual(await self.calls(), 3)
        self.assertEqual(await self.spent(), Decimal("0.0006"))
        self.assertEqual(await self.remaining(), self.config.daily_limit - 1)

    async def test_three_call_limit_never_makes_fourth_generation(self):
        self.transport.generate.side_effect = [self.tool_turn(identifier=f"call-{index}") for index in range(3)]
        response = await self.chat()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.transport.generate.await_count, self.config.max_llm_calls_per_message)
        self.assertEqual(await self.calls(), 3)
        self.assertEqual(await self.remaining(), self.config.daily_limit - 1)

    async def test_configured_lower_call_limit_is_enforced(self):
        self.config = self.config.model_copy(update={"max_llm_calls_per_message": 1})
        self.transport.generate.return_value = self.tool_turn()
        response = await self.chat()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.transport.generate.await_count, 1)

    async def test_budget_exhausted_mid_loop_returns_known_data_and_no_more_provider_requests(self):
        self.config = self.config.model_copy(update={"monthly_budget_usd": Decimal("0.0002")})
        self.transport.generate.return_value = self.tool_turn()
        with self.assertLogs(assistant_api.logger, logging.WARNING):
            response = await self.chat()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "budget")
        self.assertIn("busy", response.json()["reply"])
        self.assertIn("EXPIRED", response.json()["reply"])
        self.assertEqual(response.json()["sources"], ["get_my_access"])
        self.assertEqual(self.transport.generate.await_count, 1)
        self.assertEqual(self.transport.count.await_count, 1)
        self.assertEqual(await self.calls(), 1)

    async def test_budget_zero_and_quota_exhaustion_do_not_even_send_token_count_request(self):
        self.config = self.config.model_copy(update={"monthly_budget_usd": Decimal(0)})
        self.assertEqual((await self.chat()).status_code, 503)
        self.transport.count.assert_not_awaited()
        self.transport.generate.assert_not_awaited()
        self.config = self.config.model_copy(update={"monthly_budget_usd": Decimal(50), "per_minute_limit": 1})
        self.assertEqual((await self.chat()).status_code, 200)
        before = self.transport.count.await_count
        limited = await self.chat()
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["error_code"], "minute")
        self.assertEqual(self.transport.count.await_count, before)

    async def test_input_preflight_and_cumulative_exact_boundary(self):
        self.config = self.config.model_copy(update={"max_total_input_tokens_per_message": 200})
        self.transport.generate.side_effect = [self.tool_turn(), ModelAnswer("Verified account data.", 100, 20)]
        self.assertEqual((await self.chat()).status_code, 200)
        self.assertEqual(await self.calls(), 2)
        self.transport.generate.side_effect = [self.tool_turn(identifier="call-3"), self.tool_turn(identifier="call-4")]
        self.transport.count.side_effect = [100, 100, 1]
        response = await self.chat()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.transport.generate.await_count, 4)
        self.assertEqual(await self.calls(), 4)

    async def test_initial_input_excess_has_no_generation_or_quota_cost(self):
        self.config = self.config.model_copy(update={"max_total_input_tokens_per_message": 99})
        self.assertEqual((await self.chat()).status_code, 429)
        self.transport.generate.assert_not_awaited()
        self.assertEqual(await self.calls(), 0)
        self.assertEqual(await self.remaining(), self.config.daily_limit)

    async def test_cached_tokens_are_counted_in_total_input_limit(self):
        self.config = self.config.model_copy(update={"max_total_input_tokens_per_message": 200})
        self.transport.generate.return_value = self.tool_turn(cache_read_input_tokens=100)
        self.assertEqual((await self.chat()).status_code, 429)
        self.assertEqual(self.transport.generate.await_count, 1)

    async def test_output_remaining_is_sent_as_max_tokens_exact_boundary(self):
        self.config = self.config.model_copy(update={"max_total_output_tokens_per_message": 30})
        self.transport.generate.side_effect = [self.tool_turn(output_tokens=20), ModelAnswer("Done.", 100, 10)]
        self.assertEqual((await self.chat()).status_code, 200)
        self.assertEqual([call.args[0].max_tokens for call in self.transport.generate.call_args_list], [30, 10])
        self.assertEqual(await self.calls(), 2)

    async def test_output_exhaustion_stops_before_next_count_or_generation(self):
        self.config = self.config.model_copy(update={"max_total_output_tokens_per_message": 20})
        self.transport.generate.return_value = self.tool_turn(output_tokens=20)
        self.assertEqual((await self.chat()).status_code, 429)
        self.assertEqual(self.transport.generate.await_count, 1)
        self.assertEqual(self.transport.count.await_count, 1)

    async def test_actual_usage_above_preflight_limit_is_still_billed_then_stops(self):
        self.config = self.config.model_copy(update={"max_total_input_tokens_per_message": 100})
        self.transport.generate.return_value = self.tool_turn(input_tokens=101)
        self.assertEqual((await self.chat()).status_code, 429)
        self.assertEqual(await self.calls(), 1)
        self.assertEqual(await self.spent(), Decimal("0.000201"))

    async def test_cache_read_write_prices_and_columns_are_accounted(self):
        self.transport.generate.return_value = ModelAnswer("Help.", 100, 20, 200, 300)
        self.assertEqual((await self.chat()).status_code, 200)
        self.assertEqual(await self.spent(), Decimal("0.00048"))
        record = await self.row("SELECT * FROM assistant_calls")
        self.assertEqual(record["cache_creation_input_tokens"], 200)
        self.assertEqual(record["cache_read_input_tokens"], 300)
        self.assertFalse(record["estimated"])

    async def test_generation_api_error_uses_safe_reply_estimated_receipt_and_one_quota(self):
        private = "private provider error detail"
        self.transport.generate.side_effect = anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://provider.test"), message=private,
        )
        with self.assertLogs(assistant_api.logger, logging.WARNING) as logs:
            response = await self.chat()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(private, response.text + "\n".join(logs.output))
        self.assertEqual(await self.calls(), 1)
        self.assertEqual(await self.remaining(), self.config.daily_limit - 1)
        self.assertTrue((await self.row("SELECT * FROM assistant_calls"))["estimated"])

    async def test_count_api_error_does_not_consume_generation_quota(self):
        self.transport.count.side_effect = anthropic.APIConnectionError(request=httpx.Request("POST", "https://provider.test"))
        self.assertEqual((await self.chat()).status_code, 502)
        self.transport.generate.assert_not_awaited()
        self.assertEqual(await self.calls(), 0)
        self.assertEqual(await self.remaining(), self.config.daily_limit)

    async def test_second_provider_error_does_not_rollback_first_receipt(self):
        self.transport.generate.side_effect = [
            self.tool_turn(), anthropic.APIConnectionError(request=httpx.Request("POST", "https://provider.test")),
        ]
        with self.assertLogs(assistant_api.logger, logging.WARNING):
            response = await self.chat()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(await self.calls(), 2)
        self.assertGreater(await self.spent(), Decimal("0.0002"))
        self.assertEqual(response.json()["sources"], ["get_my_access"])

    async def test_pending_analysis_confirmation_never_spends_and_is_not_sent_back_to_llm(self):
        self.transport.generate.return_value = self.tool_turn("get_analysis", {"symbol": "BTCUSDT", "timeframe": "15m"})
        response = await self.chat("BTC analizini yap, onaylıyorum", user_id="b", confirm=True)
        self.assertEqual(response.status_code, 200)
        confirmation = response.json()["needs_confirmation"]
        self.assertEqual(confirmation["cost"], self.credits.config.cost)
        self.assertIn(str(self.credits.config.cost), response.json()["reply"])
        self.assertEqual(self.transport.generate.await_count, 1)
        self.app.state.analyst_analysis.assert_not_awaited()
        self.assertEqual((await self.credits.credits("a", False))["remaining"], self.credits.config.total)
        self.assertNotIn(confirmation["confirmation_token"], json.dumps(self.transport.generate.call_args.args[0].messages))

    async def test_llm_cannot_supply_user_identity_confirm_or_write_tool(self):
        for name, arguments in (
            ("get_my_credits", {"user_id": "b"}),
            ("get_analysis", {"symbol": "BTCUSDT", "timeframe": "15m", "confirm": True}),
            ("confirm_analysis", {"confirm": True}),
            ("v25_order", {"symbol": "BTCUSDT"}),
        ):
            self.transport.generate.side_effect = [self.tool_turn(name, arguments), ModelAnswer("Could not verify.", 100, 20)]
            response = await self.chat()
            self.assertEqual(response.status_code, 200)
            outputs = self.transport.generate.call_args.args[0].messages[-1]["content"]
            self.assertTrue(outputs[0]["is_error"])
            self.assertNotIn("Traceback", outputs[0]["content"])
        self.app.state.analyst_analysis.assert_not_awaited()
        self.assertFalse(hasattr(self.app.state, "v25_execution"))

    async def test_cached_analysis_is_filtered_and_buy_instruction_overridden_with_risk_note(self):
        for user in ("a", "premium"):
            await self.credits.consume(user, user == "premium", "BTCUSDT", "15m", "test-cache-analysis-key", lambda: self.app.state.analyst_analysis("BTCUSDT", "15m"))
            self.transport.generate.side_effect = [
                self.tool_turn("get_analysis", {"symbol": "BTCUSDT", "timeframe": "15m"}),
                ModelAnswer("BUY NOW, entry 987654.321!", 100, 20),
            ]
            response = await self.chat("Should I buy BTC?", user)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Final Decision", response.json()["reply"])
            self.assertIn("not a win probability", response.json()["reply"])
            self.assertIn("capital loss", response.json()["reply"])
            for private in ("BUY NOW", "987654.321", "123456.789", "private reason"):
                self.assertNotIn(private, response.text)
                self.assertNotIn(private, json.dumps(self.transport.generate.call_args.args[0].messages))

    async def test_unverified_stop_never_returns_model_claim_that_it_works(self):
        self.transport.generate.side_effect = [
            self.tool_turn("get_protection_status", {"symbol": "BTCUSDT", "language": "en"}),
            ModelAnswer("Yes it works perfectly.", 100, 20),
        ]
        response = await self.chat("Does my stop loss work?")
        self.assertIn("could not be verified", response.json()["reply"])
        self.assertIn("Positions", response.json()["reply"])
        self.assertNotIn("works perfectly", response.json()["reply"])

    async def test_unverified_protection_reply_preserves_known_age(self):
        original = AssistantTools.dispatch

        async def dispatch(tools, user_id, name, arguments):
            if name == "get_protection_status":
                return tools.result({"verified": False, "protected": None, "data_age_seconds": 45}, stale=True)
            return await original(tools, user_id, name, arguments)

        self.transport.generate.side_effect = [self.tool_turn("get_protection_status", {"symbol": "BTCUSDT"}), ModelAnswer("It works.", 100, 20)]
        with patch.object(AssistantTools, "dispatch", dispatch):
            response = await self.chat("Does my stop loss work?")
        self.assertIn("45 seconds ago", response.json()["reply"])
        self.assertIn("could not be verified", response.json()["reply"])

    async def test_invented_private_levels_are_not_echoed_to_free_or_premium(self):
        self.transport.generate.return_value = ModelAnswer("Entry: 987654.321; SL: 123456.789", 100, 20)
        for user in ("a", "premium"):
            response = await self.chat(user=user)
            self.assertNotIn("987654.321", response.text)
            self.assertNotIn("123456.789", response.text)
        self.assertIn("could not verify", response.json()["reply"])

    async def test_failed_analysis_tool_is_explicit_and_error_has_fetch_timestamp(self):
        self.transport.generate.side_effect = [
            self.tool_turn("get_analysis", {"symbol": "BTCUSDT", "timeframe": "1h", "confirm": True}),
            ModelAnswer("Your analysis looks great.", 100, 20),
        ]
        response = await self.chat()
        self.assertIn("could not verify the analysis", response.json()["reply"])
        self.assertNotIn("looks great", response.json()["reply"])
        request = self.transport.generate.call_args_list[1].args[0]
        result = json.loads(request.messages[-1]["content"][0]["content"])
        self.assertEqual(result["fetched_at"], datetime.fromtimestamp(self.now, timezone.utc).isoformat())
        self.assertTrue(result["stale"])
        self.app.state.analyst_analysis.assert_not_awaited()

    async def test_help_language_is_server_selected_not_model_selected(self):
        self.transport.generate.side_effect = [
            self.tool_turn("search_help", {"query": "Stripe billing portal", "language": "tr"}),
            ModelAnswer("Open Billing.", 100, 20),
        ]
        response = await self.chat()
        self.assertEqual(response.json()["language"], "en")
        self.assertEqual(response.json()["sources"], ["search_help"])
        request = self.transport.generate.call_args_list[1].args[0]
        data = json.loads(request.messages[-1]["content"][0]["content"])["data"]["results"]
        self.assertEqual(data[0]["id"], "billing")
        self.assertTrue(all(row["language"] == "en" for row in data))

    async def test_duplicate_ids_or_too_many_tools_are_billed_but_not_executed(self):
        for calls in ((ToolCall("same", "get_my_access", {}),) * 2,
                      tuple(ToolCall(str(index), "get_my_access", {}) for index in range(8))):
            self.transport.generate.return_value = ModelAnswer("", 100, 20, tool_calls=calls)
            with patch.object(AssistantTools, "dispatch", AsyncMock()) as dispatch:
                self.assertEqual((await self.chat()).status_code, 502)
                dispatch.assert_not_awaited()
        self.assertEqual(await self.calls(), 2)

    async def test_bilingual_policy_has_required_boundaries_and_stable_cache_prefix(self):
        for language in ("tr", "en"):
            prompt = system_prompt(language)
            self.assertIn(SYSTEM_POLICY, prompt)
            for token in ("Final Decision", "Confidence", "get_protection_status", "verified=false", "needs_confirmation", "Entry/SL/TP", "API & Connection Center"):
                self.assertIn(token, prompt)
            first = build_request(self.config, language, ChatInput(message="one"))
            second = build_request(self.config, language, ChatInput(message="different", page_context="another page"))
            self.assertEqual(first.system, second.system)
            self.assertEqual(first.tools, second.tools)
            self.assertNotIn("user_id", first.tools[0]["input_schema"].get("properties", {}))

    def test_kais_ai_bilingual_identity_policy_never_invents_infrastructure_or_claims_to_be_human(self):
        for language, phrases in (
            ("tr", ("Adın Kais AI. Bu platformun yapay zeka asistanısın.",
                    "İnsan olduğunu asla iddia etme", "Bu altyapı bilgisini paylaşamıyorum",
                    "şirket veya model adı uydurma, tahmin etme")),
            ("en", ("Your name is Kais AI. You are this platform's AI assistant.",
                    "Never claim to be human", "I cannot share that infrastructure information",
                    "or guess a company or model name")),
        ):
            with self.subTest(language=language):
                prompt = system_prompt(language)
                for phrase in phrases:
                    self.assertIn(phrase, prompt)
                self.assertNotIn("customer assistant", prompt)
                self.assertNotIn("müşteri yardımcısısın", prompt)

    async def test_old_accounting_schema_upgrades_without_resetting_spend(self):
        old = Path(self.directory.name) / "old.sqlite3"
        with closing(sqlite3.connect(old)) as db, db:
            for statement in SCHEMA[:2]:
                db.execute(statement)
            db.execute("""CREATE TABLE assistant_calls (id TEXT PRIMARY KEY,user_id TEXT,month_key TEXT,
                          called_at REAL,input_tokens INTEGER,output_tokens INTEGER,cost_usd TEXT,estimated BOOLEAN)""")
            db.execute("INSERT INTO assistant_calls VALUES ('old','a','2026-01',0,100,20,'4',0)")
            db.execute("INSERT INTO assistant_monthly_spend VALUES ('2026-01','4',0,0)")
        async def snapshot(store):
            async with store.connection(self.config.request_timeout_seconds) as tx:
                return (await tx.row("SELECT * FROM assistant_calls WHERE id=$1", "old"),
                        await tx.row("SELECT * FROM assistant_monthly_spend WHERE month_key=$1", "2026-01"))

        snapshots = await asyncio.gather(*(snapshot(AssistantStore(self.app, path=old)) for _ in range(2)))
        for record, ledger in snapshots:
            self.assertEqual(record["cache_creation_input_tokens"], 0)
            self.assertEqual(record["cost_usd"], "4")
            self.assertEqual(ledger["spent_usd"], "4")

    async def test_transcript_and_usage_commit_even_when_client_disconnects(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def generate(request):
            started.set()
            await finish.wait()
            return ModelAnswer("Done.", 100, 20)

        self.transport.generate.side_effect = generate
        service = self.app.state.assistant_service
        caller = asyncio.create_task(service.start("a", "en", ChatInput(message="Help"), self.config))
        await started.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        finish.set()
        await service.close()
        self.assertEqual(await self.calls(), 1)
        self.assertEqual(await self.remaining(), self.config.daily_limit - 1)


if __name__ == "__main__":
    unittest.main()
