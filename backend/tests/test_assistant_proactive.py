import asyncio
import json
import re
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from app import assistant_api
from app.assistant_proactive import COPY, ProactiveAssistant, pnl_text
from app.assistant_storage import AssistantStore
from app.assistant_tools import ARGUMENT_MODELS, AssistantTools
from fastapi import HTTPException
from test_assistant_tools import AssistantToolsTestCase


class AssistantProactiveTests(AssistantToolsTestCase):
    async def check_in(self, user="a", language="tr", **body):
        return await self.client.post("/api/assistant/proactive/check-in", headers=self.headers(user), json={"language": language, **body})

    async def preference(self, user="a", **body):
        return await self.client.post("/api/assistant/proactive/preferences", headers=self.headers(user), json=body)

    async def test_new_endpoints_require_membership_and_are_not_public(self):
        for method, path, body in (
            ("GET", "/api/assistant/proactive/preferences", None),
            ("POST", "/api/assistant/proactive/preferences", {"enabled": False}),
            ("POST", "/api/assistant/proactive/check-in", {}),
        ):
            with self.subTest(path=path, method=method):
                response = await self.client.request(method, path, **({"json": body} if body is not None else {}))
                self.assertEqual(response.status_code, 401)

    async def test_free_member_preference_metadata_survives_projection(self):
        response = await self.client.get("/api/assistant/proactive/preferences", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": True, "available": True, "poll_interval_seconds": self.config.proactive_poll_seconds})

    async def test_status_templates_use_owned_tools_for_free_and_premium(self):
        for user, language in (("a", "tr"), ("premium", "en")):
            self.account(user)
            with self.subTest(user=user):
                response = await self.check_in(user, language)
                self.assertEqual(response.status_code, 200, response.text)
                message = response.json()["message"]
                self.assertEqual(message["language"], language)
                self.assertEqual(message["sources"], ["get_my_positions", "get_protection_status"])
                self.assertFalse(message["stale"])
                self.assertIn("12,30" if language == "tr" else "12.30", message["reply"])
                self.assertIn(COPY[language]["protected"], message["reply"])
                self.assertIn(COPY[language]["age"].format(seconds=0), message["reply"])
                for private in ("987654", "123456", "stop-a", "plan-a", "entry", "confirmation"):
                    self.assertNotIn(private, json.dumps(message))

    async def test_rolling_cooldown_crosses_midnight_and_releases_at_exact_boundary(self):
        self.account()
        first = await self.check_in()
        self.assertIsNotNone(first.json()["message"])
        self.now += timedelta(hours=self.config.proactive_cooldown_hours).total_seconds() - 1
        self.account()
        self.assertIsNone((await self.check_in()).json()["message"])
        self.now += 1
        self.account()
        self.assertIsNotNone((await self.check_in()).json()["message"])

    async def test_concurrent_tabs_and_separate_store_instances_deliver_once(self):
        self.account()
        store = self.app.state.assistant_service.store
        other = AssistantStore(self.app, path=store.path)
        services = [ProactiveAssistant(current, self.tools(), self.config) for current in (store, other)]
        results = await asyncio.gather(*(services[index % 2].check_in("a", "tr") for index in range(8)))
        self.assertEqual(sum(result["message"] is not None for result in results), 1)

    async def test_opt_out_persists_and_cannot_be_overridden_by_body_user_id(self):
        self.account()
        response = await self.preference(enabled=False, user_id="b")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enabled"])
        store = self.app.state.assistant_service.store
        self.app.state.assistant_service.store = AssistantStore(self.app, path=store.path)
        self.now += timedelta(days=self.config.proactive_inactive_days + 1).total_seconds()
        self.assertIsNone((await self.check_in(user_id="b")).json()["message"])
        saved = await self.client.get("/api/assistant/proactive/preferences", headers=self.headers())
        other = await self.client.get("/api/assistant/proactive/preferences", headers=self.headers("b"))
        self.assertFalse(saved.json()["enabled"])
        self.assertTrue(other.json()["enabled"])
        self.assertEqual((await self.preference(enabled=True)).status_code, 200)
        self.account()
        self.assertIsNotNone((await self.check_in()).json()["message"])

    async def test_other_users_cannot_read_positions_or_claim_another_users_cooldown(self):
        self.account("a")
        foreign = await self.check_in("b", user_id="a")
        self.assertIsNone(foreign.json()["message"])
        owner = await self.check_in("a", user_id="b")
        self.assertIsNotNone(owner.json()["message"])
        self.assertIn("12,30", owner.json()["message"]["reply"])
        with self.assertRaises(HTTPException):
            await ProactiveAssistant(self.app.state.assistant_service.store, self.tools("b"), self.config).check_in("a", "tr")

    async def test_explicit_reenable_refreshes_activity_without_false_absence(self):
        self.assertIsNone((await self.check_in()).json()["message"])
        await self.preference(enabled=False)
        self.now += timedelta(days=self.config.proactive_inactive_days + 1).total_seconds()
        await self.preference(enabled=True)
        self.assertIsNone((await self.check_in()).json()["message"])
        self.now += timedelta(days=self.config.proactive_inactive_days).total_seconds()
        self.assertIsNotNone((await self.check_in()).json()["message"])

    async def test_inactivity_uses_config_and_initial_unknown_history_is_not_a_return(self):
        self.assertIsNone((await self.check_in()).json()["message"])
        self.now += timedelta(days=self.config.proactive_inactive_days).total_seconds() - 1
        self.assertIsNone((await self.check_in()).json()["message"])
        self.now += timedelta(days=self.config.proactive_inactive_days).total_seconds()
        message = (await self.check_in(language="en")).json()["message"]
        self.assertIn(COPY["en"]["returning"], message["reply"])
        self.assertIn(COPY["en"]["stale"], message["reply"])
        self.assertEqual(message["sources"], ["get_my_activity", "get_my_positions"])
        self.config = self.config.model_copy(update={"proactive_inactive_days": self.config.proactive_inactive_days + 1})
        self.now += timedelta(days=self.config.proactive_inactive_days - 1).total_seconds()
        self.assertIsNone((await self.check_in(language="en")).json()["message"])

    async def test_positions_take_precedence_over_inactivity_and_visit_refreshes_during_cooldown(self):
        await self.check_in()
        self.now += timedelta(days=self.config.proactive_inactive_days).total_seconds()
        self.account()
        message = (await self.check_in()).json()["message"]
        self.assertIn("12,30", message["reply"])
        self.assertNotIn(COPY["tr"]["returning"], message["reply"])
        self.now += 1
        await self.check_in()
        async with self.app.state.assistant_service.store.connection(self.config.request_timeout_seconds) as transaction:
            row = await transaction.row("SELECT last_seen_at FROM assistant_proactive_state WHERE user_id=$1", "a")
        self.assertEqual(row["last_seen_at"], self.now)

    async def test_stale_and_unverified_protection_are_not_reported_as_safe(self):
        for mode in ("stale", "reconciliation", "missing", "age_unknown"):
            with self.subTest(mode=mode):
                self.now += timedelta(days=1).total_seconds()
                state = self.account()
                if mode == "stale":
                    self.now += 1000
                elif mode == "reconciliation":
                    state["reconciliation_required"] = True
                elif mode == "missing":
                    state["snapshot"]["open_algo_orders"] = []
                    state["plans"]["plan-a"]["protection_state"] = "MISSING"
                else:
                    state["connection"]["last_checked"] = None
                message = (await self.check_in()).json()["message"]
                self.assertNotIn(COPY["tr"]["protected"], message["reply"])
                self.assertIn(COPY["tr"]["missing"] if mode == "missing" else COPY["tr"]["unverified"], message["reply"])
                if mode in {"stale", "age_unknown"}:
                    self.assertTrue(message["stale"])
                    self.assertIn(COPY["tr"]["stale"], message["reply"])
                if mode == "stale":
                    self.assertIn(COPY["tr"]["age"].format(seconds=1000), message["reply"])
                if mode == "age_unknown":
                    self.assertIn(COPY["tr"]["age_unknown"], message["reply"])

    async def test_proactive_never_calls_llm_spend_or_credits_and_needs_no_api_key(self):
        self.account()
        self.config = self.config.model_copy(update={"api_key": self.config.api_key.__class__("")})
        with patch.object(AssistantTools, "confirm_analysis", AsyncMock()) as consume:
            response = await self.check_in()
        self.assertIsNotNone(response.json()["message"])
        consume.assert_not_awaited()
        self.provider.assert_not_awaited()
        self.app.state.analyst_analysis.assert_not_awaited()
        async with self.app.state.assistant_service.store.connection(self.config.request_timeout_seconds) as transaction:
            for table in ("assistant_usage", "assistant_calls", "assistant_monthly_spend"):
                self.assertEqual((await transaction.row(f"SELECT COUNT(*) AS count FROM {table}"))["count"], 0)
        self.assertEqual((await self.credits.credits("a", False))["remaining"], self.credits.config.total)

    async def test_disabled_feature_and_invalid_requests_are_explicit(self):
        self.account()
        for field in ("enabled", "proactive_enabled"):
            config = self.config.model_copy(update={field: False})
            with patch.object(assistant_api, "load_assistant_config", lambda config=config: config):
                response = await self.check_in()
                self.assertFalse(response.json()["available"])
                self.assertIsNone(response.json()["message"])
        for path, body in (("preferences", {"enabled": "false"}), ("preferences", {}), ("check-in", {"language": "fr"})):
            response = await self.client.post(f"/api/assistant/proactive/{path}", headers=self.headers(), json=body)
            self.assertEqual(response.status_code, 422)
        with patch.object(AssistantStore, "proactive_transaction", side_effect=OSError("private-storage-detail")):
            response = await self.check_in()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-storage-detail", response.text)

    def test_templates_contain_no_recommendations_or_profit_promises(self):
        forbidden = re.compile(r"\b(?:al|sat|fırsat|buy|sell|opportunity|recommend|guaranteed|guarantee)\b", re.IGNORECASE)
        for language, templates in COPY.items():
            for name, template in templates.items():
                with self.subTest(language=language, template=name):
                    self.assertIsNone(forbidden.search(template))
        self.assertNotIn("get_my_activity", ARGUMENT_MODELS)

    def test_invalid_pnl_is_not_guessed_or_zero_filled(self):
        for value in (None, True, "NaN", "Infinity", "unavailable"):
            with self.subTest(value=value):
                self.assertEqual(pnl_text([{"unrealized_pnl": value}], "tr"), COPY["tr"]["unknown"])
        self.assertEqual(pnl_text([{"unrealized_pnl": 12.3}, {"unrealized_pnl": -20}], "en"), "-7.70")
